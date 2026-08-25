#!/usr/bin/env python3
"""Require the local model to emit one exact bounded device control call."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_control as control  # noqa: E402
import model_ha_proof  # noqa: E402
import model_runtime_policy  # noqa: E402
from ollama_endpoint import OllamaEndpoint, load_runtime_ollama_endpoint  # noqa: E402


MODEL = model_runtime_policy.get_profile("structured").model
TOOL_NAME = "ha_control_entity"


class ControlProofError(RuntimeError):
    """A secret-free model control proof failure."""


def extract_exact_call(
    response: dict[str, Any], entity_id: str, action: str, value: object = None
) -> dict[str, Any]:
    message = response.get("message")
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise ControlProofError("model did not emit exactly one control tool call")
    function = calls[0].get("function")
    if not isinstance(function, dict) or function.get("name") != TOOL_NAME:
        raise ControlProofError("model selected an unexpected control tool")
    expected = {"entity_id": entity_id, "action": action}
    if value is not None:
        expected["value"] = value
    if function.get("arguments") != expected:
        raise ControlProofError("model changed the requested control arguments")
    return calls[0]


def run_control_proof(
    entity_id: str,
    action: str,
    value: object = None,
    *,
    endpoint_loader: Callable[[], OllamaEndpoint] = load_runtime_ollama_endpoint,
    ollama_call: Callable[..., dict[str, Any]] = model_ha_proof.call_ollama,
    control_executor: Callable[..., tuple[dict[str, Any], int]] = control.execute_safely,
) -> dict[str, Any]:
    control.validate_request(entity_id, action, value)
    endpoint = endpoint_loader()
    runtime_profile = model_runtime_policy.get_profile("structured")
    payload = model_runtime_policy.build_chat_payload(
        "structured",
        [
            {
                "role": "system",
                "content": (
                    "Ты Home Butler. Владелец уже явно подтвердил ровно одно действие. "
                    "Вызови единственный инструмент с переданными ID и action без изменений."
                ),
            },
            {
                "role": "user",
                "content": f"Выполни {action} для {entity_id} со значением {value!r}.",
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "description": "Control one explicitly requested Home Assistant device feature",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "entity_id": {"type": "string", "enum": [entity_id]},
                            "action": {"type": "string", "enum": [action]},
                            **(
                                {"value": {"enum": [value]}}
                                if value is not None else {}
                            ),
                        },
                        "required": ["entity_id", "action"] + (
                            ["value"] if value is not None else []
                        ),
                        "additionalProperties": False,
                    },
                },
            }
        ],
    )
    response = ollama_call(
        endpoint,
        "/api/chat",
        payload,
        timeout=runtime_profile.request_timeout_seconds,
    )
    tool_call = extract_exact_call(response, entity_id, action, value)
    if value is None:
        result, exit_code = control_executor(entity_id, action)
    else:
        result, exit_code = control_executor(entity_id, action, value)
    return {
        "schema_version": 1,
        "tool_call_verified": True,
        "model": MODEL,
        "ollama_endpoint": endpoint.base_url,
        "tool_call": tool_call.get("function"),
        "control_result": result,
        "control_exit_code": exit_code,
    }
