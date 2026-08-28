#!/usr/bin/env python3
"""Versioned, immutable runtime profiles for local Home Butler model calls."""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import turn_observability


sys.dont_write_bytecode = True

POLICY_SCHEMA_VERSION = 1
LOCAL_MODELS = frozenset({"home-butler", "qwen3.5:4b-q4_K_M"})
PRODUCTION_MODEL = "qwen3.5:4b-q4_K_M"
PRODUCTION_CONTEXT_WINDOWS = frozenset({8_192, 16_384, 32_768})
REQUIRED_PROFILES = frozenset(
    {"voice_fast", "dialogue", "diagnostic", "structured", "summarizer"}
)

# These lessons are injected into every interactive model request.  They are
# deliberately short: the 4B model remains warm and fast, while the host keeps
# the final authority over facts and actions.  The examples come from verified
# owner feedback and sanitized Home Assistant observations.
GROUNDING_LESSONS = """
HOME_BUTLER_VERIFIED_LESSONS_V2:
1. TOOL_RESULT is the only source of current Home Assistant facts. Never replace
   its values with assumptions, prior knowledge or a plausible story.
2. State `charging` or `docked` means the robot is at/on its dock; it does not
   prove movement or active cleaning. State `active` must be reported literally
   unless another verified field explains it.
3. A battery value of 100 means 100 percent. Never call it low. Report resource
   percentages exactly as supplied; do not claim that data is absent when a
   value is present.
4. One unavailable feature does not prove that the physical device is offline.
   Say which feature is unavailable. Never invent causes such as a connection
   reset, frozen module or network failure without explicit evidence.
5. `accepted` is not `verified`. HTTP success or a stateless button readback only
   proves that the command was accepted. Claim physical success only for a
   `verified` receipt whose readback matches the requested outcome.
6. Conditional controls may be unavailable while an appliance is off. That is
   not an incident unless the observation explicitly marks it unexpected.
7. Never expose entity IDs, physical hashes, IP/MAC addresses or secrets in an
   owner-facing answer. Use the human device and feature names.
""".strip()


class ModelRuntimePolicyError(ValueError):
    """A bounded policy validation error without prompt or endpoint data."""


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """One complete local-model execution policy."""

    name: str
    model: str
    context_window: int
    output_limit: int
    temperature: float
    top_p: float
    top_k: int
    think: bool
    keep_alive: str
    request_timeout_seconds: float
    max_tool_iterations: int
    latency_budget_seconds: float
    fallback_route: str


def _profile(
    name: str,
    model: str,
    context_window: int,
    output_limit: int,
    temperature: float,
    keep_alive: str,
    request_timeout_seconds: float,
    max_tool_iterations: int,
    latency_budget_seconds: float,
    fallback_route: str,
) -> RuntimeProfile:
    return RuntimeProfile(
        name=name,
        model=model,
        context_window=context_window,
        output_limit=output_limit,
        temperature=temperature,
        top_p=0.9,
        top_k=20,
        think=False,
        keep_alive=keep_alive,
        request_timeout_seconds=request_timeout_seconds,
        max_tool_iterations=max_tool_iterations,
        latency_budget_seconds=latency_budget_seconds,
        fallback_route=fallback_route,
    )


PROFILES: Mapping[str, RuntimeProfile] = MappingProxyType(
    {
        "voice_fast": _profile(
            "voice_fast",
            PRODUCTION_MODEL,
            8_192,
            256,
            0.15,
            "24h",
            60.0,
            2,
            4.0,
            "verified_fallback",
        ),
        "dialogue": _profile(
            "dialogue",
            PRODUCTION_MODEL,
            32_768,
            1_024,
            0.35,
            "24h",
            180.0,
            4,
            45.0,
            "voice_fast",
        ),
        "diagnostic": _profile(
            "diagnostic",
            PRODUCTION_MODEL,
            32_768,
            2_048,
            0.1,
            "10m",
            240.0,
            6,
            180.0,
            "structured",
        ),
        "structured": _profile(
            "structured",
            PRODUCTION_MODEL,
            8_192,
            2_048,
            0.0,
            "24h",
            180.0,
            2,
            20.0,
            "verified_fallback",
        ),
        "summarizer": _profile(
            "summarizer",
            PRODUCTION_MODEL,
            16_384,
            1_024,
            0.1,
            "10m",
            180.0,
            0,
            120.0,
            "none",
        ),
    }
)


def _validate_profile(profile: RuntimeProfile) -> None:
    if not profile.name or profile.model not in LOCAL_MODELS:
        raise ModelRuntimePolicyError("profile model is not allow-listed")
    if profile.context_window not in PRODUCTION_CONTEXT_WINDOWS:
        raise ModelRuntimePolicyError("profile context window is invalid")
    if not 1 <= profile.output_limit <= 2_048:
        raise ModelRuntimePolicyError("profile output limit is invalid")
    if not 0 <= profile.temperature <= 1:
        raise ModelRuntimePolicyError("profile temperature is invalid")
    if not 0 < profile.top_p <= 1 or not 1 <= profile.top_k <= 100:
        raise ModelRuntimePolicyError("profile sampling policy is invalid")
    if not profile.keep_alive or profile.request_timeout_seconds <= 0:
        raise ModelRuntimePolicyError("profile lifecycle policy is invalid")
    if not 0 <= profile.max_tool_iterations <= 8:
        raise ModelRuntimePolicyError("profile tool limit is invalid")
    if not 0 < profile.latency_budget_seconds <= profile.request_timeout_seconds:
        raise ModelRuntimePolicyError("profile latency budget is invalid")


def _validate_policy() -> None:
    if frozenset(PROFILES) != REQUIRED_PROFILES:
        raise ModelRuntimePolicyError("required runtime profiles are incomplete")
    for name, profile in PROFILES.items():
        if profile.name != name:
            raise ModelRuntimePolicyError("profile name does not match its route")
        _validate_profile(profile)
        fallback = profile.fallback_route
        if fallback not in PROFILES and fallback not in {
            "none",
            "verified_fallback",
        }:
            raise ModelRuntimePolicyError("profile fallback route is invalid")
        if fallback == name:
            raise ModelRuntimePolicyError("profile fallback route is recursive")
    if {profile.model for profile in PROFILES.values()} != {PRODUCTION_MODEL}:
        raise ModelRuntimePolicyError("production profiles would thrash the single model slot")


_validate_policy()


def get_profile(name: str) -> RuntimeProfile:
    """Return one immutable profile or fail closed for an unknown route."""
    try:
        return PROFILES[name]
    except (KeyError, TypeError) as error:
        raise ModelRuntimePolicyError("unknown model runtime profile") from error


def _with_grounding_lessons(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    injected = False
    for message in messages:
        if not isinstance(message, Mapping):
            raise ModelRuntimePolicyError("chat message is invalid")
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ModelRuntimePolicyError("chat message role is invalid")
        if not isinstance(content, str):
            raise ModelRuntimePolicyError("chat message content is invalid")
        item = dict(message)
        if role == "system" and not injected:
            item["content"] = content + "\n\n" + GROUNDING_LESSONS
            injected = True
        copied.append(item)
    if not injected:
        copied.insert(0, {"role": "system", "content": GROUNDING_LESSONS})
    return copied


def build_chat_payload(
    profile_name: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Sequence[Mapping[str, Any]] | None = None,
    response_format: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an Ollama request without permitting ad-hoc runtime overrides."""
    profile = get_profile(profile_name)
    turn_observability.record_policy(profile.name, profile.model, "allowed")
    if not isinstance(messages, (list, tuple)) or not messages:
        raise ModelRuntimePolicyError("chat messages are required")
    copied_messages = _with_grounding_lessons(messages)

    payload: dict[str, Any] = {
        "model": profile.model,
        "stream": False,
        "think": profile.think,
        "keep_alive": profile.keep_alive,
        "options": {
            "temperature": profile.temperature,
            "top_p": profile.top_p,
            "top_k": profile.top_k,
            "num_ctx": profile.context_window,
            "num_predict": profile.output_limit,
        },
        "messages": copied_messages,
    }
    if tools is not None:
        if profile.max_tool_iterations == 0 or not isinstance(tools, (list, tuple)):
            raise ModelRuntimePolicyError("tools are forbidden for this profile")
        payload["tools"] = [dict(tool) for tool in tools]
    if response_format is not None:
        if not isinstance(response_format, Mapping):
            raise ModelRuntimePolicyError("response format is invalid")
        payload["format"] = dict(response_format)
    return payload


def build_generate_payload(
    profile_name: str,
    prompt: str,
    *,
    response_format: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an Ollama generate request from the same canonical policy."""
    profile = get_profile(profile_name)
    turn_observability.record_policy(profile.name, profile.model, "allowed")
    if not isinstance(prompt, str) or not prompt:
        raise ModelRuntimePolicyError("generate prompt is required")
    payload: dict[str, Any] = {
        "model": profile.model,
        "prompt": GROUNDING_LESSONS + "\n\nCURRENT_TASK:\n" + prompt,
        "stream": False,
        "think": profile.think,
        "keep_alive": profile.keep_alive,
        "options": {
            "temperature": profile.temperature,
            "top_p": profile.top_p,
            "top_k": profile.top_k,
            "num_ctx": profile.context_window,
            "num_predict": profile.output_limit,
        },
    }
    if response_format is not None:
        if not isinstance(response_format, Mapping):
            raise ModelRuntimePolicyError("response format is invalid")
        payload["format"] = dict(response_format)
    return payload


def trace_metadata(profile_name: str) -> dict[str, Any]:
    """Return secret-safe policy evidence; prompts and endpoint data are excluded."""
    profile = get_profile(profile_name)
    metadata = asdict(profile)
    metadata["policy_schema_version"] = POLICY_SCHEMA_VERSION
    metadata["grounding_lesson_version"] = 2
    return metadata


def run(argv: Sequence[str] | None = None) -> int:
    """Expose one secret-free scalar for bounded host supervisors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-window", choices=sorted(REQUIRED_PROFILES))
    arguments = parser.parse_args(argv)
    if arguments.context_window is None:
        parser.error("--context-window is required")
    print(get_profile(arguments.context_window).context_window)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
