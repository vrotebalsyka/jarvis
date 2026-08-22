#!/usr/bin/env python3
"""Speak one verified HA and Alice-path status after each WSL boot."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import alice_skill_gateway  # noqa: E402
import alice_skill_health  # noqa: E402
import daily_voice_report  # noqa: E402
import home_assistant_notify as ha_notify  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import startup_self_check  # noqa: E402


SCHEMA_VERSION = 1
STATE_PATH = Path("/run/home-butler-startup-voice/status.json")
MAX_STATE_BYTES = 16 * 1024
VALID_DELIVERY_STATES = frozenset(("accepted_unverified", "verified"))


class StartupVoiceError(RuntimeError):
    """A fixed, secret-free startup announcement failure."""


def _allowed_state_owners(*, read_only: bool) -> set[tuple[int, int]]:
    owners = {(os.geteuid(), os.getegid())}
    if read_only and os.geteuid() == 0:
        try:
            account = pwd.getpwnam("homebutler")
        except KeyError:
            pass
        else:
            owners.add((account.pw_uid, account.pw_gid))
    return owners


def _require_private_directory(path: Path, *, read_only: bool = False) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise StartupVoiceError("startup status directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_uid, metadata.st_gid)
        not in _allowed_state_owners(read_only=read_only)
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise StartupVoiceError("startup status directory is unsafe")


def _validate_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000:
        raise StartupVoiceError("Home Assistant entity counts are invalid")
    return value


def load_state(path: Path = STATE_PATH) -> dict[str, Any] | None:
    _require_private_directory(path.parent, read_only=True)
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StartupVoiceError("startup status is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_uid, metadata.st_gid)
        not in _allowed_state_owners(read_only=True)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= MAX_STATE_BYTES
    ):
        raise StartupVoiceError("startup status is unsafe")
    try:
        document = json.loads(path.read_bytes().decode("ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StartupVoiceError("startup status is invalid") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
        or startup_self_check.BOOT_ID_RE.fullmatch(str(document.get("boot_id", ""))) is None
        or document.get("delivery_status") not in VALID_DELIVERY_STATES
        or not isinstance(document.get("alice_local_ready"), bool)
        or not isinstance(document.get("alice_public_ready"), bool)
        or not isinstance(document.get("observed_epoch"), int)
        or isinstance(document.get("observed_epoch"), bool)
        or document["observed_epoch"] < 0
        or document.get("ha_status") not in {"healthy", "stale_data"}
        or not isinstance(document.get("speaker_entity_id"), str)
    ):
        raise StartupVoiceError("startup status is invalid")
    _validate_count(document.get("entity_count"))
    _validate_count(document.get("available_entity_count"))
    _validate_count(document.get("unavailable_entity_count"))
    return document


def write_state(document: dict[str, Any], path: Path = STATE_PATH) -> None:
    _require_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise StartupVoiceError("startup status is unsafe")
    raw = (
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")
    if len(raw) > MAX_STATE_BYTES:
        raise StartupVoiceError("startup status is too large")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".startup-voice.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, os.geteuid(), os.getegid())
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise StartupVoiceError("startup status could not be saved") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def check_alice_path() -> dict[str, bool]:
    """Perform exact owner pings locally and through the public Funnel."""

    try:
        config = alice_skill_gateway.GatewayConfig.load()
    except alice_skill_gateway.GatewayError:
        return {"local_ready": False, "public_ready": False}
    if config.pending or not config.owner_ids:
        return {"local_ready": False, "public_ready": False}
    local_ready = True
    public_ready = True
    try:
        alice_skill_health.probe_local(config)
    except alice_skill_health.HealthError:
        local_ready = False
    try:
        alice_skill_health.probe_public(config)
    except alice_skill_health.HealthError:
        public_ready = False
    return {"local_ready": local_ready, "public_ready": public_ready}


def build_message(
    *,
    ha_status: str,
    available: int,
    unavailable: int,
    alice_local_ready: bool,
    alice_public_ready: bool,
) -> str:
    if ha_status == "healthy":
        ha_sentence = "Home Assistant на связи."
    elif ha_status == "stale_data":
        ha_sentence = "Home Assistant на связи, но часть данных устарела."
    else:
        raise StartupVoiceError("Home Assistant status is invalid")
    if alice_local_ready and alice_public_ready:
        alice_sentence = "Навык Алисы и защищённый туннель до Яндекса отвечают."
    elif alice_local_ready:
        alice_sentence = (
            "Локальный шлюз навыка отвечает, но защищённый туннель до Яндекса "
            "не подтверждён."
        )
    elif alice_public_ready:
        alice_sentence = (
            "Публичный туннель отвечает, но локальный шлюз навыка не подтверждён."
        )
    else:
        alice_sentence = "Навык Алисы и защищённый туннель сейчас не подтверждены."
    message = (
        "Дворецкий запущен после включения компьютера. "
        f"{ha_sentence} Доступно сущностей: {available}; недоступно: {unavailable}. "
        f"{alice_sentence}"
    )
    if len(message) > ha_notify.MAX_MESSAGE_CHARS:
        raise StartupVoiceError("startup report is too long")
    return message


def execute(
    *,
    state_path: Path = STATE_PATH,
    boot_id_reader: Callable[[], str] = startup_self_check.read_boot_id,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    tunnel_checker: Callable[[], dict[str, bool]] = check_alice_path,
    config_loader: Callable[[], ha_read.AdapterConfig] = ha_read.load_config,
    state_reader: Callable[[ha_read.AdapterConfig, str], dict[str, Any]] = daily_voice_report.read_speaker_state,
    service_caller: Callable[[ha_read.AdapterConfig, str, str], None] = ha_notify.post_tts,
    delivery_verifier: Callable[[ha_read.AdapterConfig, str, dict[str, Any]], bool] = daily_voice_report.verify_speaker_transition,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    boot_id = boot_id_reader()
    previous = load_state(state_path)
    if previous is not None and previous.get("boot_id") == boot_id:
        verified = previous.get("delivery_status") == "verified"
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": verified,
            "status": "already_verified" if verified else "already_attempted_unverified",
            "service_calls": 0,
            "boot_id": boot_id,
        }

    snapshot, exit_code = snapshot_reader("snapshot")
    ha_status = snapshot.get("status") if isinstance(snapshot, dict) else None
    if exit_code != 0 or ha_status not in {"healthy", "stale_data"}:
        safe_status = ha_status if isinstance(ha_status, str) else "invalid"
        raise StartupVoiceError(
            f"Home Assistant snapshot is unavailable: {safe_status}"
        )
    entity_count = _validate_count(snapshot.get("entity_count"))
    available = _validate_count(snapshot.get("available_entity_count"))
    unavailable = _validate_count(snapshot.get("unavailable_entity_count"))
    if available + unavailable > entity_count:
        raise StartupVoiceError("Home Assistant entity counts are inconsistent")
    speaker = daily_voice_report.choose_daily_report_speaker(snapshot)
    alice = tunnel_checker()
    if (
        not isinstance(alice, dict)
        or not isinstance(alice.get("local_ready"), bool)
        or not isinstance(alice.get("public_ready"), bool)
    ):
        raise StartupVoiceError("Alice path result is invalid")
    message = build_message(
        ha_status=ha_status,
        available=available,
        unavailable=unavailable,
        alice_local_ready=alice["local_ready"],
        alice_public_ready=alice["public_ready"],
    )
    config = config_loader()
    baseline = state_reader(config, speaker)
    if baseline.get("muted") is True or baseline.get("volume_ready") is not True:
        raise StartupVoiceError("startup report speaker is not audible")
    observed_epoch = int(now())
    if observed_epoch < 0:
        raise StartupVoiceError("startup report clock is invalid")
    status_document = {
        "schema_version": SCHEMA_VERSION,
        "boot_id": boot_id,
        "observed_epoch": observed_epoch,
        "delivery_status": "accepted_unverified",
        "ha_status": ha_status,
        "entity_count": entity_count,
        "available_entity_count": available,
        "unavailable_entity_count": unavailable,
        "alice_local_ready": alice["local_ready"],
        "alice_public_ready": alice["public_ready"],
        "speaker_entity_id": speaker,
    }
    try:
        service_caller(config, speaker, message)
    except ha_notify.NotifyDeliveryUnknown:
        # The request crossed the local boundary. Record that fact before
        # returning an error so a timer retry cannot produce duplicate speech.
        write_state(status_document, state_path)
        raise
    # Persist acceptance before the readback wait. A service retry in the same
    # boot must never speak the same startup announcement twice.
    write_state(status_document, state_path)
    verified = delivery_verifier(config, speaker, baseline)
    if verified:
        status_document["delivery_status"] = "verified"
        write_state(status_document, state_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": verified,
        "status": "verified" if verified else "accepted_unverified",
        "service_calls": 1,
        "boot_id": boot_id,
        "ha_status": ha_status,
        "entity_count": entity_count,
        "available_entity_count": available,
        "unavailable_entity_count": unavailable,
        "alice_local_ready": alice["local_ready"],
        "alice_public_ready": alice["public_ready"],
        "speaker_entity_id": speaker,
        "message": message,
    }


def check_status(
    *,
    state_path: Path = STATE_PATH,
    boot_id_reader: Callable[[], str] = startup_self_check.read_boot_id,
) -> dict[str, Any]:
    boot_id = boot_id_reader()
    state = load_state(state_path)
    if state is None or state.get("boot_id") != boot_id:
        raise StartupVoiceError("startup report has not run for this boot")
    return state


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-status", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.check_status:
            result = check_status()
            print(
                "startup_voice_status="
                f"{result['delivery_status']} "
                f"ha={result['ha_status']} "
                f"alice_local={str(result['alice_local_ready']).lower()} "
                f"alice_public={str(result['alice_public_ready']).lower()}"
            )
            return 0 if result["delivery_status"] == "verified" else 4
        result = execute()
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["status"] in {"verified", "already_verified", "already_attempted_unverified"} else 4
    except ha_notify.NotifyDeliveryUnknown:
        result = {"schema_version": SCHEMA_VERSION, "ok": False, "status": "delivery_unknown"}
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 4
    except (
        StartupVoiceError,
        startup_self_check.SelfCheckError,
        daily_voice_report.DailyReportError,
        ha_notify.NotifyError,
        ha_read.AdapterError,
    ) as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "not_sent",
            "error": str(error)[:120],
        }
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 3


if __name__ == "__main__":
    raise SystemExit(run())
