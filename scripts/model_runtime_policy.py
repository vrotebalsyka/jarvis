#!/usr/bin/env python3
"""Small immutable policy for the single local conversational model."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence


POLICY_SCHEMA_VERSION = 2
PRODUCTION_MODEL = "qwen3.5:2b-q4_K_M"
LOCAL_MODELS = frozenset({PRODUCTION_MODEL})
PRODUCTION_CONTEXT_WINDOWS = frozenset({8_192, 32_768})
REQUIRED_PROFILES = frozenset({"voice_fast", "dialogue", "structured", "selector"})


class ModelRuntimePolicyError(ValueError):
    """A bounded policy validation error."""


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
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
    context_window: int,
    output_limit: int,
    temperature: float,
    timeout: float,
) -> RuntimeProfile:
    return RuntimeProfile(
        name=name,
        model=PRODUCTION_MODEL,
        context_window=context_window,
        output_limit=output_limit,
        temperature=temperature,
        top_p=0.9,
        top_k=20,
        think=False,
        keep_alive="24h",
        request_timeout_seconds=timeout,
        max_tool_iterations=0,
        latency_budget_seconds=timeout,
        fallback_route="none",
    )


PROFILES: Mapping[str, RuntimeProfile] = MappingProxyType({
    "voice_fast": _profile("voice_fast", 8_192, 384, 0.15, 60.0),
    "dialogue": _profile("dialogue", 32_768, 1_024, 0.25, 180.0),
    "structured": _profile("structured", 8_192, 1_024, 0.0, 180.0),
    "selector": _profile("selector", 8_192, 48, 0.0, 10.0),
})


def _validate_policy() -> None:
    if frozenset(PROFILES) != REQUIRED_PROFILES:
        raise ModelRuntimePolicyError("required runtime profiles are incomplete")
    for name, profile in PROFILES.items():
        if profile.name != name or profile.model not in LOCAL_MODELS:
            raise ModelRuntimePolicyError("profile identity is invalid")
        if profile.context_window not in PRODUCTION_CONTEXT_WINDOWS:
            raise ModelRuntimePolicyError("profile context window is invalid")
        if not 1 <= profile.output_limit <= 2_048:
            raise ModelRuntimePolicyError("profile output limit is invalid")
        if not 0 <= profile.temperature <= 1:
            raise ModelRuntimePolicyError("profile temperature is invalid")
        if profile.max_tool_iterations != 0:
            raise ModelRuntimePolicyError("model tool execution must remain disabled")


_validate_policy()


def get_profile(name: str) -> RuntimeProfile:
    try:
        return PROFILES[name]
    except (KeyError, TypeError) as error:
        raise ModelRuntimePolicyError("unknown model runtime profile") from error


def _messages(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ModelRuntimePolicyError("chat messages are required")
    copied: list[dict[str, Any]] = []
    for message in values:
        if not isinstance(message, Mapping):
            raise ModelRuntimePolicyError("chat message is invalid")
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise ModelRuntimePolicyError("chat message is invalid")
        copied.append({"role": role, "content": content})
    return copied


def build_chat_payload(
    profile_name: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Sequence[Mapping[str, Any]] | None = None,
    response_format: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a model request; production model tools are deliberately absent."""

    profile = get_profile(profile_name)
    if tools is not None:
        raise ModelRuntimePolicyError("model tools are disabled in read-only production")
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
        "messages": _messages(messages),
    }
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
    profile = get_profile(profile_name)
    if not isinstance(prompt, str) or not prompt:
        raise ModelRuntimePolicyError("generate prompt is required")
    payload: dict[str, Any] = {
        "model": profile.model,
        "prompt": prompt,
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
    metadata = asdict(get_profile(profile_name))
    metadata["policy_schema_version"] = POLICY_SCHEMA_VERSION
    return metadata


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-window", choices=sorted(REQUIRED_PROFILES))
    arguments = parser.parse_args(argv)
    if arguments.context_window is None:
        parser.error("--context-window is required")
    print(get_profile(arguments.context_window).context_window)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
