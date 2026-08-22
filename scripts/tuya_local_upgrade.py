#!/usr/bin/env python3
"""Owner-only fixed Tuya Local upgrade and planned Core restart."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_core_recovery as core_recovery  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402


EXPECTED_CORE_VERSION = "2026.7.4"
EXPECTED_INSTALLED_VERSION = "2026.5.4"
TARGET_VERSION = "2026.7.2"
UPDATE_ENTITY_ID = "update.tuya_local_update"
UPDATE_STATE_PATH = f"/api/states/{UPDATE_ENTITY_ID}"
UPDATE_INSTALL_PATH = "/api/services/update/install"
MAX_UPDATE_SECONDS = 300
MAX_RESTART_SECONDS = 600
POLL_SECONDS = 3
MAINTENANCE_ENV = "HOME_BUTLER_PLANNED_MAINTENANCE"


class TuyaLocalUpgradeError(RuntimeError):
    """A fixed, secret-free planned-maintenance failure."""


def _update_facts(document: Any) -> tuple[str, str, bool]:
    if not isinstance(document, dict) or document.get("entity_id") != UPDATE_ENTITY_ID:
        raise TuyaLocalUpgradeError("Tuya Local update entity response is invalid")
    attributes = document.get("attributes")
    if not isinstance(attributes, dict):
        raise TuyaLocalUpgradeError("Tuya Local update attributes are invalid")
    installed = attributes.get("installed_version")
    latest = attributes.get("latest_version")
    in_progress = attributes.get("in_progress")
    if (
        not isinstance(installed, str)
        or not isinstance(latest, str)
        or not isinstance(in_progress, bool)
    ):
        raise TuyaLocalUpgradeError("Tuya Local update facts are invalid")
    return installed, latest, in_progress


def _read_update(
    config: ha_read.AdapterConfig,
    request: Callable[..., Any] = core_recovery._request_json,
) -> tuple[str, str, bool]:
    return _update_facts(request(config, "GET", UPDATE_STATE_PATH))


def _read_core_version(
    config: ha_read.AdapterConfig,
    request: Callable[..., Any] = core_recovery._request_json,
) -> str:
    document = request(config, "GET", "/api/config")
    version = document.get("version") if isinstance(document, dict) else None
    if version != EXPECTED_CORE_VERSION:
        raise TuyaLocalUpgradeError("Unexpected Home Assistant Core version")
    return version


def install_exact_update(
    config: ha_read.AdapterConfig,
    *,
    request: Callable[..., Any] = core_recovery._request_json,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    _read_core_version(config, request)
    installed, latest, in_progress = _read_update(config, request)
    if (
        installed != EXPECTED_INSTALLED_VERSION
        or latest != TARGET_VERSION
        or in_progress
    ):
        raise TuyaLocalUpgradeError("Tuya Local update precondition failed")
    payload = json.dumps(
        {
            "entity_id": UPDATE_ENTITY_ID,
            "version": TARGET_VERSION,
            "backup": False,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    try:
        response = request(config, "POST", UPDATE_INSTALL_PATH, body=payload)
        if not isinstance(response, (list, dict)):
            raise TuyaLocalUpgradeError("Tuya Local update response is invalid")
    except core_recovery.CoreDeliveryUnknown:
        pass

    deadline = monotonic() + MAX_UPDATE_SECONDS
    while monotonic() < deadline:
        try:
            installed, latest, in_progress = _read_update(config, request)
        except core_recovery.CoreRecoveryError:
            sleeper(POLL_SECONDS)
            continue
        if installed == TARGET_VERSION and not in_progress:
            if latest != TARGET_VERSION:
                raise TuyaLocalUpgradeError("Unexpected Tuya Local release after update")
            return
        if installed not in {EXPECTED_INSTALLED_VERSION, TARGET_VERSION}:
            raise TuyaLocalUpgradeError("Unexpected Tuya Local version during update")
        sleeper(POLL_SECONDS)
    raise TuyaLocalUpgradeError("Tuya Local update verification timed out")


def restart_and_verify(
    config: ha_read.AdapterConfig,
    *,
    request: Callable[..., Any] = core_recovery._request_json,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    core_recovery.post_check_config(config)
    try:
        core_recovery.post_restart(config)
    except core_recovery.CoreDeliveryUnknown:
        pass

    deadline = monotonic() + MAX_RESTART_SECONDS
    while monotonic() < deadline:
        try:
            _read_core_version(config, request)
            installed, latest, in_progress = _read_update(config, request)
            core_recovery.probe_core(config)
        except (core_recovery.CoreRecoveryError, TuyaLocalUpgradeError):
            sleeper(POLL_SECONDS)
            continue
        if installed == TARGET_VERSION and latest == TARGET_VERSION and not in_progress:
            return
        sleeper(POLL_SECONDS)
    raise TuyaLocalUpgradeError("Home Assistant post-update verification timed out")


def run() -> dict[str, object]:
    if os.environ.get(MAINTENANCE_ENV) != "1":
        raise TuyaLocalUpgradeError("Owner maintenance wrapper is required")
    config = ha_read.load_config()
    install_exact_update(config)
    restart_and_verify(config)
    return {
        "schema_version": 1,
        "status": "tuya_local_upgrade_verified",
        "core_version": EXPECTED_CORE_VERSION,
        "installed_version": TARGET_VERSION,
        "update_calls": 1,
        "restart_calls": 1,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.check:
        print("TUYA_LOCAL_UPGRADE_CHECK_OK")
        return 0
    try:
        result = run()
    except (
        TuyaLocalUpgradeError,
        core_recovery.CoreRecoveryError,
        ha_read.AdapterError,
        OSError,
    ):
        print("TUYA_LOCAL_UPGRADE_FAILED", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
