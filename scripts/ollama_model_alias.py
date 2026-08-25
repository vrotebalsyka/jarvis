#!/usr/bin/env python3
"""Ensure a fixed local voice profile derives from the reviewed Home Butler model."""

from __future__ import annotations

import http.client
import json
import re
import sys
from typing import Any, Callable


sys.dont_write_bytecode = True

from ollama_endpoint import OllamaEndpoint, load_runtime_ollama_endpoint
import model_ha_proof
import model_runtime_policy


VOICE_PROFILE = model_runtime_policy.get_profile("voice_fast")
SOURCE_MODEL = VOICE_PROFILE.model
VOICE_MODEL = "home-butler-voice"
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
MAX_CREATE_RESPONSE_BYTES = 4096
MAX_SHOW_RESPONSE_BYTES = 4 * 1_048_576
VOICE_PARAMETERS = {
    "num_ctx": VOICE_PROFILE.context_window,
    "num_predict": VOICE_PROFILE.output_limit,
}


class AliasError(RuntimeError):
    """A secret-free local model alias failure."""


def _model_digest(document: Any, model_name: str) -> str | None:
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, list) or len(models) > 1024:
        raise AliasError("Ollama model inventory is invalid")
    accepted_names = {model_name, f"{model_name}:latest"}
    matches: list[str] = []
    for item in models:
        if not isinstance(item, dict) or item.get("name") not in accepted_names:
            continue
        digest = item.get("digest")
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise AliasError("Ollama model digest is invalid")
        matches.append(digest)
    if not matches:
        return None
    if len(matches) != 1:
        raise AliasError("Ollama model name is ambiguous")
    return matches[0]


def _post_json(
    endpoint: OllamaEndpoint,
    path: str,
    document: dict[str, Any],
    *,
    timeout: int = 120,
    max_response_bytes: int = MAX_CREATE_RESPONSE_BYTES,
) -> dict[str, Any]:
    if path not in {"/api/create", "/api/show"}:
        raise AliasError("Ollama model profile path is not allow-listed")
    connection = http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=timeout)
    body = json.dumps(
        document, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        raw = response.read(max_response_bytes + 1)
        if response.status != 200 or not raw or len(raw) > max_response_bytes:
            raise AliasError("Ollama model profile request failed")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise AliasError("Ollama model profile response is invalid")
        return value
    except (OSError, TimeoutError, http.client.HTTPException, UnicodeError, json.JSONDecodeError) as error:
        raise AliasError("Ollama model profile endpoint is unavailable") from error
    finally:
        connection.close()


def post_create(endpoint: OllamaEndpoint, source: str, destination: str) -> None:
    if source != SOURCE_MODEL or destination != VOICE_MODEL:
        raise AliasError("Ollama voice profile creation is not allow-listed")
    result = _post_json(
        endpoint,
        "/api/create",
        {
            "model": destination,
            "from": source,
            "stream": False,
            "parameters": VOICE_PARAMETERS,
        },
    )
    if result.get("status") != "success":
        raise AliasError("Ollama voice profile creation failed")


def show_model(endpoint: OllamaEndpoint, model_name: str) -> dict[str, Any]:
    if model_name not in {SOURCE_MODEL, VOICE_MODEL}:
        raise AliasError("Ollama model inspection is not allow-listed")
    return _post_json(
        endpoint,
        "/api/show",
        {"model": model_name},
        timeout=30,
        max_response_bytes=MAX_SHOW_RESPONSE_BYTES,
    )


def _parameters(document: dict[str, Any]) -> dict[str, str]:
    raw = document.get("parameters")
    if not isinstance(raw, str) or len(raw) > 16_384:
        raise AliasError("Ollama model parameters are invalid")
    parsed: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0] in parsed:
            raise AliasError("Ollama model parameters are invalid")
        parsed[parts[0]] = parts[1]
    return parsed


def _validate_voice_profile(
    source: dict[str, Any], voice: dict[str, Any]
) -> None:
    source_details = source.get("details")
    voice_details = voice.get("details")
    if not isinstance(source_details, dict) or not isinstance(voice_details, dict):
        raise AliasError("voice profile model details are invalid")
    source_without_parent = {
        key: value for key, value in source_details.items() if key != "parent_model"
    }
    voice_without_parent = {
        key: value for key, value in voice_details.items() if key != "parent_model"
    }
    if (
        source_without_parent != voice_without_parent
        or voice_details.get("parent_model") not in {SOURCE_MODEL, f"{SOURCE_MODEL}:latest"}
    ):
        raise AliasError("voice profile does not derive from Home Butler")
    for field in ("model_info", "system", "template", "tensors"):
        if source.get(field) != voice.get(field):
            raise AliasError("voice profile does not derive from Home Butler")
    parameters = _parameters(voice)
    if any(parameters.get(name) != str(value) for name, value in VOICE_PARAMETERS.items()):
        raise AliasError("voice profile parameters are not fixed")


def ensure_alias(
    *,
    endpoint_loader: Callable[[], OllamaEndpoint] = load_runtime_ollama_endpoint,
    inventory_reader: Callable[[OllamaEndpoint, str], dict[str, Any]] = model_ha_proof.get_ollama,
    create_writer: Callable[[OllamaEndpoint, str, str], None] = post_create,
    model_reader: Callable[[OllamaEndpoint, str], dict[str, Any]] = show_model,
) -> dict[str, str]:
    endpoint = endpoint_loader()
    if endpoint.host == "127.0.0.1":
        raise AliasError("voice model requires the private Windows GPU endpoint")
    before = inventory_reader(endpoint, "/api/tags")
    source_digest = _model_digest(before, SOURCE_MODEL)
    voice_digest = _model_digest(before, VOICE_MODEL)
    if source_digest is None:
        raise AliasError("reviewed Home Butler model is absent")
    status = "already_present"
    if voice_digest is None or voice_digest == source_digest:
        create_writer(endpoint, SOURCE_MODEL, VOICE_MODEL)
        status = "created" if voice_digest is None else "upgraded_from_copy"
    after = inventory_reader(endpoint, "/api/tags")
    final_voice_digest = _model_digest(after, VOICE_MODEL)
    if final_voice_digest is None or final_voice_digest == source_digest:
        raise AliasError("isolated voice profile verification failed")
    _validate_voice_profile(
        model_reader(endpoint, SOURCE_MODEL),
        model_reader(endpoint, VOICE_MODEL),
    )
    return {
        "status": status,
        "source": SOURCE_MODEL,
        "voice_model": VOICE_MODEL,
        "endpoint": endpoint.base_url,
    }


def main() -> int:
    try:
        result = ensure_alias()
    except (AliasError, model_ha_proof.ProofError):
        print("Voice model profile setup rejected.", file=sys.stderr)
        return 2
    print(
        f"voice_model_profile={result['status']} model={result['voice_model']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
