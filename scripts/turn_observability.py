#!/usr/bin/env python3
"""Secret-safe, bounded observability for one conversational agent turn."""

from __future__ import annotations

import contextvars
import secrets
import time
import re
from dataclasses import dataclass, field
from typing import Any, Mapping


MAX_EVENTS = 32
SAFE_CODE_RE = re.compile(r"[A-Za-z0-9_.:/+-]{1,96}\Z")
MEMORY_ID_RE = re.compile(r"[a-f0-9]{16,64}\Z")


def _code(value: object, fallback: str = "unknown") -> str:
    if isinstance(value, str) and SAFE_CODE_RE.fullmatch(value):
        return value
    return fallback


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _latency_ms(started: float) -> int:
    return max(0, min(86_400_000, round((time.monotonic() - started) * 1000)))


@dataclass(slots=True)
class TurnTrace:
    trace_id: str
    owner_scope: str
    transport: str
    session_key: str
    created_at: int
    started: float
    route: str = "unclassified"
    profiles: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    context_sections: list[str] = field(default_factory=list)
    retrieved_memory_ids: list[str] = field(default_factory=list)
    retrieval_trace_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    policy_results: list[str] = field(default_factory=list)
    playbooks: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    verifications: list[str] = field(default_factory=list)


_CURRENT: contextvars.ContextVar[TurnTrace | None] = contextvars.ContextVar(
    "home_butler_turn_trace", default=None
)


def begin_turn(
    *,
    owner_scope: str,
    transport: str,
    session_key: str,
    trace_id: str | None = None,
) -> contextvars.Token[TurnTrace | None]:
    """Start an in-process trace without storing user text or tool arguments."""
    identifier = trace_id if isinstance(trace_id, str) and MEMORY_ID_RE.fullmatch(trace_id) else secrets.token_hex(16)
    trace = TurnTrace(
        trace_id=identifier,
        owner_scope=_code(owner_scope),
        transport=_code(transport),
        session_key=(session_key if MEMORY_ID_RE.fullmatch(session_key) else secrets.token_hex(16)),
        created_at=int(time.time()),
        started=time.monotonic(),
    )
    return _CURRENT.set(trace)


def current_trace_id() -> str | None:
    trace = _CURRENT.get()
    return trace.trace_id if trace is not None else None


def observe_memory_context(context: Mapping[str, Any]) -> None:
    trace = _CURRENT.get()
    if trace is None or not isinstance(context, Mapping):
        return
    trace.context_sections = [
        _code(key) for key in context.keys()
        if isinstance(key, str) and SAFE_CODE_RE.fullmatch(key)
    ][:MAX_EVENTS]
    retrieval = context.get("retrieval_trace_id")
    if isinstance(retrieval, str) and MEMORY_ID_RE.fullmatch(retrieval):
        trace.retrieval_trace_id = retrieval
    memories = context.get("relevant_memories")
    if isinstance(memories, list):
        trace.retrieved_memory_ids = [
            item["id"] for item in memories
            if isinstance(item, Mapping)
            and isinstance(item.get("id"), str)
            and MEMORY_ID_RE.fullmatch(item["id"])
        ][:MAX_EVENTS]


def record_route(route: object) -> None:
    trace = _CURRENT.get()
    if trace is not None:
        trace.route = _code(route)


def record_policy(profile: object, model: object, result: object = "allowed") -> None:
    trace = _CURRENT.get()
    if trace is None:
        return
    safe_profile = _code(profile)
    safe_model = _code(model)
    safe_result = _code(result)
    if safe_profile not in trace.profiles and len(trace.profiles) < MAX_EVENTS:
        trace.profiles.append(safe_profile)
    if safe_model not in trace.models and len(trace.models) < MAX_EVENTS:
        trace.models.append(safe_model)
    if safe_result not in trace.policy_results and len(trace.policy_results) < MAX_EVENTS:
        trace.policy_results.append(safe_result)


def record_model_call(
    payload: Mapping[str, Any],
    response: Mapping[str, Any] | None,
    *,
    path: object,
    latency_ms: int,
    status: object,
) -> None:
    trace = _CURRENT.get()
    if trace is None or len(trace.model_calls) >= MAX_EVENTS:
        return
    model = _code(payload.get("model"))
    prompt_tokens = _count(response.get("prompt_eval_count")) if response is not None else 0
    output_tokens = _count(response.get("eval_count")) if response is not None else 0
    trace.input_tokens += prompt_tokens
    trace.output_tokens += output_tokens
    if model not in trace.models and len(trace.models) < MAX_EVENTS:
        trace.models.append(model)
    trace.model_calls.append({
        "model": model,
        "path": _code(path),
        "latency_ms": max(0, min(86_400_000, int(latency_ms))),
        "status": _code(status),
        "input_tokens": prompt_tokens,
        "output_tokens": output_tokens,
    })


def record_tool_call(
    name: object,
    *,
    latency_ms: int,
    policy_result: object,
    result_status: object,
) -> None:
    trace = _CURRENT.get()
    if trace is None or len(trace.tool_calls) >= MAX_EVENTS:
        return
    policy = _code(policy_result)
    trace.tool_calls.append({
        "name": _code(name),
        "latency_ms": max(0, min(86_400_000, int(latency_ms))),
        "policy_result": policy,
        "result_status": _code(result_status),
    })
    if policy not in trace.policy_results and len(trace.policy_results) < MAX_EVENTS:
        trace.policy_results.append(policy)


def record_playbook(playbook_id: object) -> None:
    trace = _CURRENT.get()
    value = _code(playbook_id)
    if trace is not None and value not in trace.playbooks and len(trace.playbooks) < MAX_EVENTS:
        trace.playbooks.append(value)


def record_action(action: object) -> None:
    trace = _CURRENT.get()
    value = _code(action)
    if trace is not None and len(trace.actions) < MAX_EVENTS:
        trace.actions.append(value)


def record_verification(verification: object) -> None:
    trace = _CURRENT.get()
    value = _code(verification)
    if trace is not None and len(trace.verifications) < MAX_EVENTS:
        trace.verifications.append(value)


def finish_turn(
    token: contextvars.Token[TurnTrace | None],
    *,
    final_disposition: object,
) -> dict[str, Any] | None:
    """Return a JSON-safe trace document and always clear the context variable."""
    trace = _CURRENT.get()
    try:
        if trace is None:
            return None
        return {
            "trace_id": trace.trace_id,
            "owner_scope": trace.owner_scope,
            "transport": trace.transport,
            "session_key": trace.session_key,
            "created_at": trace.created_at,
            "completed_at": int(time.time()),
            "route": trace.route,
            "profiles": trace.profiles,
            "models": trace.models,
            "token_counts": {
                "input": trace.input_tokens,
                "output": trace.output_tokens,
            },
            "context_sections": trace.context_sections,
            "retrieved_memory_ids": trace.retrieved_memory_ids,
            "retrieval_trace_id": trace.retrieval_trace_id,
            "model_calls": trace.model_calls,
            "tool_calls": trace.tool_calls,
            "policy_result": trace.policy_results or ["not_required"],
            "playbook": trace.playbooks,
            "action": trace.actions,
            "verification": trace.verifications,
            "total_latency_ms": _latency_ms(trace.started),
            "final_disposition": _code(final_disposition),
        }
    finally:
        _CURRENT.reset(token)
