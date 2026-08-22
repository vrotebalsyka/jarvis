#!/usr/bin/env python3
"""Strict input validation and trusted analysis for local health reports."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


MAX_INPUT_BYTES = 1_048_576
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096
MAX_SNAPSHOT_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
GENERIC_CRITICAL_TEMPERATURE_C = 90.0
MAX_UNSIGNED_64 = (1 << 64) - 1
MAX_DISKS = 16
MAX_TEMPERATURES = 32
MAX_FAILED_UNITS = 32
MAX_LOADED_MODELS = 16
MAX_REPORT_ITEMS = 192
UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,256}$")
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,128}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
FILESYSTEM_TYPE_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
FILESYSTEM_NAME_RE = re.compile(r"^/[A-Za-z0-9_./:+@-]{1,255}$")


class HealthReportError(RuntimeError):
    """A safe-to-handle validation or local inference failure."""


@dataclass(frozen=True)
class Finding:
    identifier: str
    code: str
    model_value: bool | int | float | str | None
    details: Mapping[str, Any]


@dataclass(frozen=True)
class Analysis:
    status: str
    facts: tuple[Finding, ...]
    problems: tuple[Finding, ...]
    missing: tuple[Finding, ...]


def _reject_constant(_: str) -> None:
    raise HealthReportError("non-finite JSON number")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HealthReportError("duplicate JSON key")
        result[key] = value
    return result


def strict_json_loads(text: str) -> Any:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError, OverflowError) as error:
        raise HealthReportError("invalid JSON") from error
    _check_json_complexity(value)
    return value


def _check_json_complexity(value: Any) -> None:
    nodes = 0

    def visit(current: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise HealthReportError("JSON is too complex")
        if isinstance(current, dict):
            for key, item in current.items():
                visit(key, depth + 1)
                visit(item, depth + 1)
        elif isinstance(current, list):
            for item in current:
                visit(item, depth + 1)

    visit(value, 0)


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HealthReportError(f"{path} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    if set(value) != expected:
        raise HealthReportError(f"{path} has an unexpected shape")


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise HealthReportError(f"{path} must be boolean")
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_UNSIGNED_64,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise HealthReportError(f"{path} must be an integer")
    return value


def _number(
    value: Any,
    path: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HealthReportError(f"{path} must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise HealthReportError(f"{path} is outside its allowed range") from error
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise HealthReportError(f"{path} is outside its allowed range")
    return result


def _safe_text(
    value: Any,
    path: str,
    *,
    maximum_length: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum_length:
        raise HealthReportError(f"{path} must be a bounded string")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise HealthReportError(f"{path} contains control characters")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise HealthReportError(f"{path} has an unsupported value")
    return value


def _optional_text(
    value: Any,
    path: str,
    *,
    maximum_length: int,
    pattern: re.Pattern[str] | None = None,
) -> str | None:
    if value is None:
        return None
    return _safe_text(
        value,
        path,
        maximum_length=maximum_length,
        pattern=pattern,
    )


def _status(value: Any, allowed: set[str], path: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise HealthReportError(f"{path} has an unsupported status")
    return value


def _parse_observed_at(value: Any) -> datetime:
    text = _safe_text(value, "observed_at", maximum_length=64)
    try:
        observed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise HealthReportError("observed_at is invalid") from error
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise HealthReportError("observed_at must include a timezone")
    return observed


def validate_snapshot(value: Any) -> dict[str, Any]:
    snapshot = _mapping(value, "snapshot")
    _exact_keys(
        snapshot,
        {
            "schema_version",
            "observed_at",
            "host",
            "disks",
            "temperatures",
            "failed_systemd_units",
            "probes",
            "ollama",
            "hermes",
            "home_assistant",
        },
        "snapshot",
    )
    if _integer(snapshot["schema_version"], "schema_version", minimum=1) != 1:
        raise HealthReportError("unsupported schema_version")
    _parse_observed_at(snapshot["observed_at"])

    host = _mapping(snapshot["host"], "host")
    _exact_keys(
        host,
        {"cpu_load_percent", "memory_used_percent", "swap_used_percent"},
        "host",
    )
    for key in ("cpu_load_percent", "memory_used_percent", "swap_used_percent"):
        _number(host[key], f"host.{key}", minimum=0, maximum=100)

    disks = snapshot["disks"]
    if not isinstance(disks, list) or len(disks) > MAX_DISKS:
        raise HealthReportError("disks must be a bounded array")
    for index, raw_disk in enumerate(disks):
        disk = _mapping(raw_disk, f"disks[{index}]")
        _exact_keys(
            disk,
            {
                "filesystem",
                "type",
                "total_bytes",
                "used_bytes",
                "available_bytes",
                "used_percent",
            },
            f"disks[{index}]",
        )
        _safe_text(
            disk["filesystem"],
            f"disks[{index}].filesystem",
            maximum_length=256,
            pattern=FILESYSTEM_NAME_RE,
        )
        _safe_text(
            disk["type"],
            f"disks[{index}].type",
            maximum_length=64,
            pattern=FILESYSTEM_TYPE_RE,
        )
        total = _integer(disk["total_bytes"], f"disks[{index}].total_bytes")
        used = _integer(disk["used_bytes"], f"disks[{index}].used_bytes")
        available = _integer(
            disk["available_bytes"],
            f"disks[{index}].available_bytes",
        )
        used_percent = _number(
            disk["used_percent"],
            f"disks[{index}].used_percent",
            minimum=0,
            maximum=100,
        )
        if total == 0 or used > total or available > total or used + available > total:
            raise HealthReportError(f"disks[{index}] has inconsistent byte counts")
        denominator = used + available
        expected_percent = 0 if denominator == 0 else math.ceil(100 * used / denominator)
        if abs(used_percent - expected_percent) > 0.1:
            raise HealthReportError(f"disks[{index}] has inconsistent usage percent")

    temperatures = snapshot["temperatures"]
    if not isinstance(temperatures, list) or len(temperatures) > MAX_TEMPERATURES:
        raise HealthReportError("temperatures must be a bounded array")
    for index, raw_temperature in enumerate(temperatures):
        temperature = _mapping(raw_temperature, f"temperatures[{index}]")
        _exact_keys(temperature, {"chip", "sensor", "celsius"}, f"temperatures[{index}]")
        _safe_text(
            temperature["chip"],
            f"temperatures[{index}].chip",
            maximum_length=128,
        )
        _safe_text(
            temperature["sensor"],
            f"temperatures[{index}].sensor",
            maximum_length=128,
        )
        _number(
            temperature["celsius"],
            f"temperatures[{index}].celsius",
            minimum=-273.15,
            maximum=1000,
        )

    failed_units = snapshot["failed_systemd_units"]
    if not isinstance(failed_units, list) or len(failed_units) > MAX_FAILED_UNITS:
        raise HealthReportError("failed_systemd_units must be a bounded array")
    for index, unit in enumerate(failed_units):
        _safe_text(
            unit,
            f"failed_systemd_units[{index}]",
            maximum_length=256,
            pattern=UNIT_NAME_RE,
        )
    if len(set(failed_units)) != len(failed_units):
        raise HealthReportError("failed_systemd_units contains duplicates")

    probes = _mapping(snapshot["probes"], "probes")
    _exact_keys(
        probes,
        {
            "temperatures",
            "systemd",
            "ollama_version",
            "ollama_models",
            "hermes_gateway",
        },
        "probes",
    )
    temperature_probe = _status(
        probes["temperatures"],
        {"ok", "unavailable", "error"},
        "probes.temperatures",
    )
    systemd_probe = _status(
        probes["systemd"],
        {"ok", "unavailable", "error"},
        "probes.systemd",
    )
    version_probe = _status(
        probes["ollama_version"],
        {"ok", "unreachable", "invalid_response"},
        "probes.ollama_version",
    )
    models_probe = _status(
        probes["ollama_models"],
        {"ok", "not_run", "request_failed", "invalid_response"},
        "probes.ollama_models",
    )
    hermes_gateway_probe = _status(
        probes["hermes_gateway"],
        {"ok", "not_configured", "error"},
        "probes.hermes_gateway",
    )
    if temperature_probe != "ok" and temperatures:
        raise HealthReportError("temperature probe state contradicts its data")
    if systemd_probe != "ok" and failed_units:
        raise HealthReportError("systemd probe state contradicts its data")

    ollama = _mapping(snapshot["ollama"], "ollama")
    _exact_keys(
        ollama,
        {"reachable", "version", "model_loaded", "loaded_models"},
        "ollama",
    )
    reachable = _boolean(ollama["reachable"], "ollama.reachable")
    version = _optional_text(
        ollama["version"],
        "ollama.version",
        maximum_length=64,
        pattern=VERSION_RE,
    )
    model_loaded = _boolean(ollama["model_loaded"], "ollama.model_loaded")
    loaded_models = ollama["loaded_models"]
    if not isinstance(loaded_models, list) or len(loaded_models) > MAX_LOADED_MODELS:
        raise HealthReportError("ollama.loaded_models must be a bounded array")
    loaded_names: list[str] = []
    for index, raw_model in enumerate(loaded_models):
        loaded_model = _mapping(raw_model, f"ollama.loaded_models[{index}]")
        _exact_keys(
            loaded_model,
            {
                "name",
                "size_bytes",
                "size_vram_bytes",
                "context_length",
                "expires_at",
            },
            f"ollama.loaded_models[{index}]",
        )
        loaded_names.append(
            _safe_text(
                loaded_model["name"],
                f"ollama.loaded_models[{index}].name",
                maximum_length=128,
                pattern=MODEL_NAME_RE,
            )
        )
        _integer(
            loaded_model["size_bytes"],
            f"ollama.loaded_models[{index}].size_bytes",
        )
        _integer(
            loaded_model["size_vram_bytes"],
            f"ollama.loaded_models[{index}].size_vram_bytes",
        )
        if loaded_model["context_length"] is not None:
            _integer(
                loaded_model["context_length"],
                f"ollama.loaded_models[{index}].context_length",
            )
        _optional_text(
            loaded_model["expires_at"],
            f"ollama.loaded_models[{index}].expires_at",
            maximum_length=64,
        )
    if len(set(loaded_names)) != len(loaded_names):
        raise HealthReportError("ollama.loaded_models contains duplicate names")
    if model_loaded != bool(loaded_models):
        raise HealthReportError("ollama.model_loaded contradicts loaded_models")
    if version_probe == "ok" and (not reachable or version is None):
        raise HealthReportError("Ollama version probe contradicts reachability")
    if version_probe == "unreachable" and (reachable or version is not None):
        raise HealthReportError("Ollama unreachable state is inconsistent")
    if version_probe == "invalid_response" and (not reachable or version is not None):
        raise HealthReportError("Ollama invalid response state is inconsistent")
    if models_probe == "ok" and not reachable:
        raise HealthReportError("Ollama models probe contradicts reachability")
    if models_probe != "ok" and (model_loaded or loaded_models):
        raise HealthReportError("Ollama models probe state contradicts model data")
    if not reachable and models_probe != "not_run":
        raise HealthReportError("Ollama unreachable state must skip models probe")

    hermes = _mapping(snapshot["hermes"], "hermes")
    _exact_keys(
        hermes,
        {"installed", "gateway_configured", "gateway_running", "status"},
        "hermes",
    )
    installed = _boolean(hermes["installed"], "hermes.installed")
    gateway_configured = _boolean(
        hermes["gateway_configured"],
        "hermes.gateway_configured",
    )
    gateway_running = _boolean(hermes["gateway_running"], "hermes.gateway_running")
    hermes_status = _status(
        hermes["status"],
        {"not_installed", "not_configured", "running", "stopped", "unknown"},
        "hermes.status",
    )
    expected_hermes_status = (
        "not_installed"
        if not installed
        else "not_configured"
        if not gateway_configured
        else "unknown"
        if hermes_gateway_probe != "ok"
        else "running"
        if gateway_running
        else "stopped"
    )
    if hermes_status != expected_hermes_status:
        raise HealthReportError("Hermes state is inconsistent")
    if gateway_running and not gateway_configured:
        raise HealthReportError("Hermes gateway state is inconsistent")
    if not gateway_configured and hermes_gateway_probe != "not_configured":
        raise HealthReportError("Hermes gateway probe state is inconsistent")
    if gateway_configured and hermes_gateway_probe == "not_configured":
        raise HealthReportError("Hermes gateway probe state is inconsistent")

    home_assistant = _mapping(snapshot["home_assistant"], "home_assistant")
    _exact_keys(home_assistant, {"configured", "status"}, "home_assistant")
    ha_configured = _boolean(
        home_assistant["configured"],
        "home_assistant.configured",
    )
    ha_status = _status(
        home_assistant["status"],
        {"not_configured", "dns_failure", "host_unreachable", "port_closed", "unauthorized", "api_unavailable", "stale_data", "healthy"},
        "home_assistant.status",
    )
    if ha_configured == (ha_status == "not_configured"):
        raise HealthReportError("Home Assistant state is inconsistent")

    return snapshot


def parse_snapshot_bytes(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise HealthReportError("snapshot size is invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HealthReportError("snapshot must be UTF-8") from error
    return validate_snapshot(strict_json_loads(text))


def ensure_snapshot_fresh(
    snapshot: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    observed = _parse_observed_at(snapshot["observed_at"]).astimezone(timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - observed).total_seconds()
    if age > MAX_SNAPSHOT_AGE_SECONDS:
        raise HealthReportError("snapshot is stale")
    if age < -MAX_FUTURE_SKEW_SECONDS:
        raise HealthReportError("snapshot timestamp is in the future")


def _make_findings(
    prefix: str,
    raw_items: Sequence[tuple[str, bool | int | float | str | None, Mapping[str, Any]]],
) -> tuple[Finding, ...]:
    return tuple(
        Finding(
            identifier=f"{prefix}{index:03d}",
            code=code,
            model_value=model_value,
            details=details,
        )
        for index, (code, model_value, details) in enumerate(raw_items, start=1)
    )


def analyze_snapshot(snapshot: Mapping[str, Any]) -> Analysis:
    facts: list[tuple[str, bool | int | float | str | None, Mapping[str, Any]]] = []
    problems: list[tuple[str, bool | int | float | str | None, Mapping[str, Any]]] = []
    missing: list[tuple[str, bool | int | float | str | None, Mapping[str, Any]]] = []

    host = snapshot["host"]
    for key in ("cpu_load_percent", "memory_used_percent", "swap_used_percent"):
        facts.append((f"host.{key}", host[key], {"value": host[key]}))
    if host["memory_used_percent"] >= 95:
        problems.append(
            (
                "host.memory_pressure",
                host["memory_used_percent"],
                {"value": host["memory_used_percent"]},
            )
        )
    if host["swap_used_percent"] >= 95:
        problems.append(
            (
                "host.swap_pressure",
                host["swap_used_percent"],
                {"value": host["swap_used_percent"]},
            )
        )

    disks = snapshot["disks"]
    if not disks:
        missing.append(("disks.unavailable", 0, {}))
        problems.append(("disks.probe_empty", 0, {}))
    for index, disk in enumerate(disks):
        details = {"index": index}
        facts.append((f"disk.{index}.available_bytes", disk["available_bytes"], details))
        facts.append((f"disk.{index}.used_percent", disk["used_percent"], details))
        if disk["used_percent"] >= 90:
            problems.append((f"disk.{index}.usage_high", disk["used_percent"], details))

    probes = snapshot["probes"]
    temperatures = snapshot["temperatures"]
    facts.append(("probe.temperatures", probes["temperatures"], {}))
    for index, temperature in enumerate(temperatures):
        facts.append(
            (
                f"temperature.{index}.celsius",
                temperature["celsius"],
                {"index": index},
            )
        )
        if temperature["celsius"] >= GENERIC_CRITICAL_TEMPERATURE_C:
            problems.append(
                (
                    f"temperature.{index}.high",
                    temperature["celsius"],
                    {"index": index},
                )
            )
        elif temperature["celsius"] <= -100:
            problems.append((f"temperature.{index}.implausible", temperature["celsius"], {"index": index}))
    if not temperatures:
        missing.append(
            (
                "temperatures.unavailable",
                probes["temperatures"],
                {"probe_status": probes["temperatures"]},
            )
        )
    if probes["temperatures"] == "error":
        problems.append(("temperatures.probe_failed", "error", {}))

    failed_units = snapshot["failed_systemd_units"]
    facts.append(("probe.systemd", probes["systemd"], {}))
    facts.append(("systemd.failed_unit_count", len(failed_units), {}))
    if probes["systemd"] != "ok":
        missing.append(
            (
                "systemd.failed_units_unavailable",
                probes["systemd"],
                {"probe_status": probes["systemd"]},
            )
        )
        problems.append(
            (
                "systemd.probe_failed",
                probes["systemd"],
                {"probe_status": probes["systemd"]},
            )
        )
    for index, _unit in enumerate(failed_units):
        problems.append(
            (
                f"systemd.failed_unit.{index}",
                True,
                {"index": index},
            )
        )

    ollama = snapshot["ollama"]
    facts.extend(
        [
            ("ollama.reachable", ollama["reachable"], {}),
            ("ollama.version_present", ollama["version"] is not None, {}),
            ("probe.ollama_version", probes["ollama_version"], {}),
            ("probe.ollama_models", probes["ollama_models"], {}),
        ]
    )
    if probes["ollama_models"] == "ok":
        facts.extend([
            ("ollama.model_loaded", ollama["model_loaded"], {}),
            ("ollama.loaded_model_count", len(ollama["loaded_models"]), {}),
        ])
    if not ollama["reachable"]:
        problems.append(("ollama.unreachable", False, {}))
    elif probes["ollama_version"] != "ok":
        problems.append(
            (
                "ollama.version_probe_failed",
                probes["ollama_version"],
                {"probe_status": probes["ollama_version"]},
            )
        )
    if probes["ollama_models"] != "ok" and ollama["reachable"]:
        problems.append(
            (
                "ollama.models_probe_failed",
                probes["ollama_models"],
                {"probe_status": probes["ollama_models"]},
            )
        )
    if probes["ollama_version"] != "ok":
        missing.append(
            (
                "ollama.version_unavailable",
                probes["ollama_version"],
                {"probe_status": probes["ollama_version"]},
            )
        )
    if probes["ollama_models"] != "ok":
        missing.append(
            (
                "ollama.models_unavailable",
                probes["ollama_models"],
                {"probe_status": probes["ollama_models"]},
            )
        )

    hermes = snapshot["hermes"]
    facts.extend(
        [
            ("probe.hermes_gateway", probes["hermes_gateway"], {}),
            ("hermes.installed", hermes["installed"], {}),
            ("hermes.gateway_configured", hermes["gateway_configured"], {}),
            ("hermes.gateway_running", hermes["gateway_running"], {}),
            ("hermes.status", hermes["status"], {}),
        ]
    )
    if not hermes["installed"]:
        problems.append(("hermes.not_installed", False, {}))
    elif hermes["gateway_configured"] and probes["hermes_gateway"] != "ok":
        problems.append(
            (
                "hermes.gateway_probe_failed",
                probes["hermes_gateway"],
                {"probe_status": probes["hermes_gateway"]},
            )
        )
        missing.append(
            (
                "hermes.gateway_state_unavailable",
                probes["hermes_gateway"],
                {"probe_status": probes["hermes_gateway"]},
            )
        )
    elif hermes["gateway_configured"] and not hermes["gateway_running"]:
        problems.append(("hermes.gateway_stopped", False, {}))

    home_assistant = snapshot["home_assistant"]
    facts.extend(
        [
            ("home_assistant.configured", home_assistant["configured"], {}),
            ("home_assistant.status", home_assistant["status"], {}),
        ]
    )
    if home_assistant["configured"] and home_assistant["status"] != "healthy":
        problems.append(
            (
                "home_assistant.unhealthy",
                home_assistant["status"],
                {"status": home_assistant["status"]},
            )
        )

    fact_findings = _make_findings("F", facts)
    problem_findings = _make_findings("P", problems)
    missing_findings = _make_findings("M", missing)
    if len(fact_findings) + len(problem_findings) + len(missing_findings) > MAX_REPORT_ITEMS:
        raise HealthReportError("snapshot exceeds structured report capacity")
    return Analysis(
        status="attention" if problem_findings else "ok",
        facts=fact_findings,
        problems=problem_findings,
        missing=missing_findings,
    )


def _id_schema(identifiers: Sequence[str]) -> dict[str, Any]:
    item_schema: dict[str, Any] = {"type": "string"}
    if identifiers:
        item_schema["enum"] = list(identifiers)
    return {
        "type": "array",
        "items": item_schema,
        "minItems": len(identifiers),
        "maxItems": len(identifiers),
        "uniqueItems": True,
    }


def build_output_schema(analysis: Analysis) -> dict[str, Any]:
    fact_ids = [item.identifier for item in analysis.facts]
    problem_ids = [item.identifier for item in analysis.problems]
    missing_ids = [item.identifier for item in analysis.missing]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": [analysis.status]},
            "fact_ids": _id_schema(fact_ids),
            "problem_ids": _id_schema(problem_ids),
            "missing_ids": _id_schema(missing_ids),
        },
        "required": ["status", "fact_ids", "problem_ids", "missing_ids"],
    }


def _validated_ids(
    value: Any,
    expected: Sequence[str],
    path: str,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise HealthReportError(f"{path} must be an ID array")
    if len(value) != len(set(value)):
        raise HealthReportError(f"{path} contains duplicate IDs")
    if set(value) != set(expected):
        raise HealthReportError(f"{path} does not match trusted facts")
    return tuple(expected)


def postvalidate_model_output(value: Any, analysis: Analysis) -> dict[str, Any]:
    output = _mapping(value, "model output")
    _exact_keys(output, {"status", "fact_ids", "problem_ids", "missing_ids"}, "model output")
    if output["status"] != analysis.status:
        raise HealthReportError("model status does not match trusted analysis")
    fact_ids = _validated_ids(
        output["fact_ids"],
        [item.identifier for item in analysis.facts],
        "fact_ids",
    )
    problem_ids = _validated_ids(
        output["problem_ids"],
        [item.identifier for item in analysis.problems],
        "problem_ids",
    )
    missing_ids = _validated_ids(
        output["missing_ids"],
        [item.identifier for item in analysis.missing],
        "missing_ids",
    )
    if set(fact_ids) & set(problem_ids) or set(fact_ids) & set(missing_ids):
        raise HealthReportError("model output categories overlap")
    return {
        "status": analysis.status,
        "fact_ids": fact_ids,
        "problem_ids": problem_ids,
        "missing_ids": missing_ids,
    }
