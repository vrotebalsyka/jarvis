#!/usr/bin/env python3
"""Print a secret-free proof that the selected inference path is local only."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import yaml

sys.dont_write_bytecode = True

from ollama_endpoint import EndpointConfigError, ENV_PATH, load_ollama_endpoint
import model_runtime_policy


PROJECT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_DIR / "hermes" / "config.yaml"
HA_TOKEN_PATH = PROJECT_DIR / "secrets" / "home-assistant.token"
CLOUD_KEYS = ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")


def _active_env_keys(path: Path) -> set[str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise RuntimeError("environment configuration is unavailable") from error
    keys: set[str] = set()
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and value:
            keys.add(key)
    return keys


def _load_config(path: Path) -> dict[str, object]:
    try:
        metadata = path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o077
            or metadata.st_size > 1_048_576
        ):
            raise RuntimeError("Hermes configuration metadata is unsafe")
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise RuntimeError("Hermes configuration is unavailable") from error
    if not isinstance(document, dict):
        raise RuntimeError("Hermes configuration is invalid")
    return document


def _token_configured(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_nlink == 1
        and metadata.st_mode & 0o077 == 0
        and 0 < metadata.st_size <= 16_384
    )


def audit() -> tuple[bool, list[str]]:
    config = _load_config(CONFIG_PATH)
    env_keys = _active_env_keys(ENV_PATH)
    model = config.get("model")
    providers = config.get("providers")
    model_mapping = model if isinstance(model, dict) else {}
    provider_mapping = providers if isinstance(providers, dict) else {}
    local_provider = provider_mapping.get("local-ollama")
    local_mapping = local_provider if isinstance(local_provider, dict) else {}

    try:
        endpoint = load_ollama_endpoint()
        endpoint_text = endpoint.base_url
        endpoint_ok = True
    except EndpointConfigError:
        endpoint_text = "invalid"
        endpoint_ok = False

    key_statuses = {
        key: key not in env_keys and not bool(os.environ.get(key)) for key in CLOUD_KEYS
    }
    dialogue = model_runtime_policy.get_profile("dialogue")
    model_ok = (
        model_mapping.get("provider") == "local-ollama"
        and model_mapping.get("default") == dialogue.model
        and model_mapping.get("context_length") == dialogue.context_window
        and model_mapping.get("max_tokens") == dialogue.output_limit
        and local_mapping.get("default_model") == dialogue.model
        and local_mapping.get("context_length") == dialogue.context_window
        and local_mapping.get("request_timeout_seconds")
        == dialogue.request_timeout_seconds
        and local_mapping.get("api") == "${HOME_BUTLER_OLLAMA_BASE_URL}/v1"
    )
    fallback_ok = set(provider_mapping) == {"local-ollama"} and not any(
        "fallback" in str(key).lower() for key in config
    )
    token_configured = _token_configured(HA_TOKEN_PATH)
    lines = [f"{key}: {'absent' if ok else 'present'}" for key, ok in key_statuses.items()]
    lines.extend(
        [
            f"LOCAL_MODEL: {dialogue.model if model_ok else 'invalid'}",
            f"OLLAMA_ENDPOINT: {endpoint_text}",
            f"HA_TOKEN: {'configured' if token_configured else 'not configured'}",
            f"CLOUD_FALLBACK: {'absent' if fallback_ok else 'present'}",
        ]
    )
    return all(key_statuses.values()) and model_ok and endpoint_ok and fallback_ok, lines


def main() -> int:
    try:
        ok, lines = audit()
    except RuntimeError:
        print("NO_CLOUD_AUDIT_FAILED", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
