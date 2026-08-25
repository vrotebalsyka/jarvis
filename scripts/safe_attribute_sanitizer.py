#!/usr/bin/env python3
"""Bounded sanitizer for untrusted Home Assistant metadata and attributes."""

from __future__ import annotations

import ipaddress
import math
import re
import unicodedata
from typing import Any, Iterable


MAX_DEPTH = 3
MAX_ITEMS = 24
MAX_STRING_CHARS = 240
MAX_ATTRIBUTE_COUNT = 48
SAFE_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
DENIED_NAME_RE = re.compile(
    r"(?:^|_)(?:auth|authorization|token|password|passwd|secret|cookie|"
    r"credential|api_key|private_key|local_key|access_key|refresh_token|"
    r"connection|connections|identifier|identifiers|config_entry|entry_id|"
    r"device_id|unique_id|serial|mac|ip|host|hostname|ssid|url|uri)(?:_|$)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"(?:https?|wss?|ftp|file)://|www\.", re.IGNORECASE)
JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"
)
MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
CREDENTIAL_RE = re.compile(
    r"(?:bearer\s+\S+|(?:password|passwd|token|secret|cookie|api[_-]?key)\s*[:=])",
    re.IGNORECASE,
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class AttributeSanitizerError(ValueError):
    """Unsafe attribute input was rejected without returning its value."""


def _safe_key(value: object) -> str:
    if not isinstance(value, str):
        raise AttributeSanitizerError("attribute key is invalid")
    key = unicodedata.normalize("NFKC", value).strip().casefold()
    if SAFE_KEY_RE.fullmatch(key) is None or DENIED_NAME_RE.search(key):
        raise AttributeSanitizerError("attribute key is unsafe")
    return key


def _private_address_in_text(value: str) -> bool:
    for candidate in re.findall(r"(?<![0-9A-Fa-f:.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9.])", value):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.is_private or address.is_loopback or address.is_link_local:
            return True
    return False


def _safe_string(value: str) -> dict[str, str]:
    text = unicodedata.normalize("NFKC", value).strip()
    if (
        not text
        or len(text) > MAX_STRING_CHARS
        or CONTROL_RE.search(text)
        or URL_RE.search(text)
        or JWT_RE.search(text)
        or MAC_RE.search(text)
        or CREDENTIAL_RE.search(text)
        or _private_address_in_text(text)
    ):
        raise AttributeSanitizerError("attribute string is unsafe")
    return {"text": text, "trust": "untrusted_data"}


def sanitize_value(value: Any, *, depth: int = 0) -> Any:
    """Return JSON-safe bounded data; every accepted string carries a trust tag."""
    if depth > MAX_DEPTH:
        raise AttributeSanitizerError("attribute nesting is unsafe")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AttributeSanitizerError("attribute number is unsafe")
        return value
    if isinstance(value, str):
        return _safe_string(value)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_ITEMS:
            raise AttributeSanitizerError("attribute collection is too large")
        return [sanitize_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_ITEMS:
            raise AttributeSanitizerError("attribute object is too large")
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = _safe_key(raw_key)
            result[key] = sanitize_value(item, depth=depth + 1)
        return result
    raise AttributeSanitizerError("attribute type is unsafe")


def sanitize_attributes(
    attributes: object,
    *,
    allowed_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Sanitize selected attributes using name, type, content and size gates."""
    if not isinstance(attributes, dict) or len(attributes) > MAX_ATTRIBUTE_COUNT:
        raise AttributeSanitizerError("attribute map is unsafe")
    allowed = None
    if allowed_names is not None:
        allowed = {_safe_key(item) for item in allowed_names}
    result: dict[str, Any] = {}
    for raw_key, value in attributes.items():
        try:
            key = _safe_key(raw_key)
        except AttributeSanitizerError:
            continue
        if allowed is not None and key not in allowed:
            continue
        try:
            result[key] = sanitize_value(value)
        except AttributeSanitizerError:
            continue
    return result


def untrusted_text(value: object) -> str | None:
    """Read one sanitizer-produced string without accepting an untagged value."""
    if not isinstance(value, dict) or set(value) != {"text", "trust"}:
        return None
    text = value.get("text")
    return text if value.get("trust") == "untrusted_data" and isinstance(text, str) else None
