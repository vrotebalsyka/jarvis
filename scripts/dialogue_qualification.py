#!/usr/bin/env python3
"""Prove real multi-turn dialogue through local chat and the public Alice route."""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import sys
import tempfile
import time
from http import cookies
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import alice_skill_gateway  # noqa: E402
import alice_tailscale_funnel  # noqa: E402
import startup_self_check  # noqa: E402


SCHEMA_VERSION = 1
STATE_DIR = Path("/home/homebutler/.local/state/home-butler")
STATUS_NAME = "dialogue-qualification.json"
MAX_STATUS_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
LOCAL_CHAT_HOST = "127.0.0.1"
LOCAL_CHAT_PORT = 8780
CSRF_RE = re.compile(
    rb'<meta name="home-butler-csrf" content="([A-Za-z0-9_-]{43})">'
)
BLOCKED_TEXT = (
    "не успел",
    "локальная модель не завершила",
    "ожидаю команду",
    "я вызываю",
    "snapshot",
)
SAFE_FAILURE_CODES = {
    "dialogue response size is invalid": "response_size",
    "dialogue response is invalid": "response_format",
    "dialogue answers are incomplete": "answers_incomplete",
    "local dialogue proof failed": "local_dialogue",
    "public Alice dialogue proof failed": "alice_dialogue",
    "public Alice dialogue failed": "alice_transport",
    "public Alice envelope is invalid": "alice_envelope",
    "local chat handshake failed": "local_handshake",
    "local chat session is invalid": "local_session",
    "local chat dialogue failed": "local_transport",
    "local chat answer is invalid": "local_answer",
    "dialogue proof directory is unavailable": "state_directory",
    "dialogue proof directory is unsafe": "state_directory",
    "dialogue proof is too large": "state_size",
    "dialogue proof is unsafe": "state_file",
    "dialogue proof is unavailable": "state_file",
    "dialogue proof is not ready for this boot": "stale_status",
}
PROMPTS = (
    "Запомни для этого разговора кодовое слово Аврора и ответь естественно одной фразой.",
    "Какое кодовое слово я попросил тебя запомнить?",
    "Объясни простыми словами, почему небо днём синее, а на закате красное.",
)


class DialogueQualificationError(RuntimeError):
    """A fixed, secret-free dialogue proof failure."""


def _strict_json(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise DialogueQualificationError("dialogue response size is invalid")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DialogueQualificationError("dialogue response is invalid") from error
    if not isinstance(document, dict):
        raise DialogueQualificationError("dialogue response is invalid")
    return document


def _validate_answers(answers: list[str]) -> dict[str, bool]:
    if len(answers) != len(PROMPTS) or any(
        not isinstance(answer, str) or not 3 <= len(answer) <= 900
        for answer in answers
    ):
        raise DialogueQualificationError("dialogue answers are incomplete")
    joined = " ".join(answers).casefold()
    history_verified = "аврор" in answers[1].casefold()
    science = answers[2].casefold()
    free_dialogue_verified = (
        len(answers[2]) >= 40
        and sum(
            token in science
            for token in ("свет", "рассе", "атмосфер", "волн", "красн", "син")
        )
        >= 2
    )
    fake_tool_claim_absent = not any(value in joined for value in BLOCKED_TEXT)
    if not all((history_verified, free_dialogue_verified, fake_tool_claim_absent)):
        raise DialogueQualificationError("dialogue proof did not pass")
    return {
        "history_verified": history_verified,
        "free_dialogue_verified": free_dialogue_verified,
        "fake_tool_claim_absent": fake_tool_claim_absent,
    }


def _alice_request(
    prompt: str,
    *,
    session_id: str,
    message_id: int,
    config: alice_skill_gateway.GatewayConfig,
) -> bytes:
    if not config.owner_ids:
        raise DialogueQualificationError("Alice owner identity is unavailable")
    document = {
        "version": "1.0",
        "request": {
            "type": "SimpleUtterance",
            "original_utterance": prompt,
            "command": prompt,
        },
        "session": {
            "session_id": session_id,
            "message_id": message_id,
            "new": message_id == 0,
            "skill_id": config.skill_id,
            "user": {"user_id": config.owner_ids[0]},
        },
    }
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def public_alice_dialogue(
    config: alice_skill_gateway.GatewayConfig,
    *,
    origin_loader: Callable[[], str] = alice_tailscale_funnel.current_origin,
    clock: Callable[[], float] = time.time,
) -> list[str]:
    origin = origin_loader()
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or parsed.port not in {None, 443}
        or not isinstance(parsed.hostname, str)
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise DialogueQualificationError("Alice public origin is invalid")
    session_id = f"qualification-{int(clock())}"
    if alice_skill_gateway.ID_RE.fullmatch(session_id) is None:
        raise DialogueQualificationError("dialogue session is invalid")
    answers: list[str] = []
    for message_id, prompt in enumerate(PROMPTS):
        connection = http.client.HTTPSConnection(parsed.hostname, 443, timeout=20)
        try:
            connection.request(
                "POST",
                config.webhook_path,
                body=_alice_request(
                    prompt,
                    session_id=session_id,
                    message_id=message_id,
                    config=config,
                ),
                headers={
                    "Accept": "application/json",
                    "Connection": "close",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise DialogueQualificationError("public Alice dialogue failed") from error
        finally:
            connection.close()
        if response.status != 200:
            raise DialogueQualificationError("public Alice dialogue failed")
        document = _strict_json(raw)
        item = document.get("response")
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(text, str) or item.get("end_session") is not False:
            raise DialogueQualificationError("public Alice envelope is invalid")
        answers.append(text)
    return answers


def local_chat_dialogue() -> list[str]:
    connection = http.client.HTTPConnection(
        LOCAL_CHAT_HOST, LOCAL_CHAT_PORT, timeout=10
    )
    try:
        connection.request("GET", "/", headers={"Connection": "close"})
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        set_cookie = response.getheader("Set-Cookie", "")
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise DialogueQualificationError("local chat handshake failed") from error
    finally:
        connection.close()
    csrf_match = CSRF_RE.search(raw)
    jar = cookies.SimpleCookie()
    try:
        jar.load(set_cookie)
        session_cookie = jar["home_butler_session"].value
    except (cookies.CookieError, KeyError) as error:
        raise DialogueQualificationError("local chat session is invalid") from error
    if response.status != 200 or csrf_match is None:
        raise DialogueQualificationError("local chat handshake failed")
    csrf = csrf_match.group(1).decode("ascii")
    answers: list[str] = []
    for prompt in PROMPTS:
        body = json.dumps(
            {"message": prompt}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        connection = http.client.HTTPConnection(
            LOCAL_CHAT_HOST, LOCAL_CHAT_PORT, timeout=30
        )
        try:
            connection.request(
                "POST",
                "/api/chat",
                body=body,
                headers={
                    "Connection": "close",
                    "Content-Type": "application/json",
                    "Cookie": f"home_butler_session={session_cookie}",
                    "Origin": f"http://{LOCAL_CHAT_HOST}:{LOCAL_CHAT_PORT}",
                    "X-Home-Butler-CSRF": csrf,
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise DialogueQualificationError("local chat dialogue failed") from error
        finally:
            connection.close()
        if response.status != 200:
            raise DialogueQualificationError("local chat dialogue failed")
        document = _strict_json(raw)
        answer = document.get("answer")
        if not isinstance(answer, str):
            raise DialogueQualificationError("local chat answer is invalid")
        answers.append(answer)
    return answers


def _status_path(state_dir: Path | None = None) -> Path:
    return (STATE_DIR if state_dir is None else state_dir) / STATUS_NAME


def write_status(document: dict[str, Any], state_dir: Path | None = None) -> Path:
    directory = STATE_DIR if state_dir is None else state_dir
    try:
        metadata = directory.lstat()
    except OSError as error:
        raise DialogueQualificationError("dialogue proof directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise DialogueQualificationError("dialogue proof directory is unsafe")
    raw = (json.dumps(
        document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("ascii")
    if len(raw) > MAX_STATUS_BYTES:
        raise DialogueQualificationError("dialogue proof is too large")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{STATUS_NAME}.", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _status_path(directory))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return _status_path(directory)


def read_status(
    state_dir: Path | None = None,
    *,
    current_boot_id: str | None = None,
) -> dict[str, Any]:
    path = _status_path(state_dir)
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= MAX_STATUS_BYTES
        ):
            raise DialogueQualificationError("dialogue proof is unsafe")
        document = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DialogueQualificationError("dialogue proof is unavailable") from error
    expected = {
        "schema_version", "observed_epoch", "boot_id", "ready",
        "local_chat_ready", "alice_public_ready", "history_verified",
        "free_dialogue_verified", "fake_tool_claim_absent",
        "local_answer_lengths", "alice_answer_lengths",
    }
    if (
        not isinstance(document, dict)
        or set(document) != expected
        or document.get("schema_version") != SCHEMA_VERSION
        or not isinstance(document.get("observed_epoch"), int)
        or isinstance(document.get("observed_epoch"), bool)
        or document["observed_epoch"] < 0
        or document.get("ready") is not True
        or not isinstance(document.get("boot_id"), str)
        or startup_self_check.BOOT_ID_RE.fullmatch(document["boot_id"]) is None
        or (
            current_boot_id is not None
            and document.get("boot_id") != current_boot_id
        )
        or not all(
            document.get(key) is True
            for key in (
                "local_chat_ready", "alice_public_ready", "history_verified",
                "free_dialogue_verified", "fake_tool_claim_absent",
            )
        )
        or any(
            not isinstance(document.get(key), list)
            or len(document[key]) != len(PROMPTS)
            or any(
                not isinstance(length, int)
                or isinstance(length, bool)
                or not 3 <= length <= 900
                for length in document[key]
            )
            for key in ("local_answer_lengths", "alice_answer_lengths")
        )
    ):
        raise DialogueQualificationError("dialogue proof is not ready for this boot")
    return document


def run_once(
    *,
    config_loader: Callable[[], alice_skill_gateway.GatewayConfig] = (
        alice_skill_gateway.GatewayConfig.load
    ),
    local_runner: Callable[[], list[str]] = local_chat_dialogue,
    public_runner: Callable[[alice_skill_gateway.GatewayConfig], list[str]] = (
        public_alice_dialogue
    ),
    boot_id_reader: Callable[[], str] = startup_self_check.read_boot_id,
    clock: Callable[[], float] = time.time,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    config = config_loader()
    local_answers = local_runner()
    try:
        local_checks = _validate_answers(local_answers)
    except DialogueQualificationError as error:
        raise DialogueQualificationError("local dialogue proof failed") from error
    public_answers = public_runner(config)
    try:
        public_checks = _validate_answers(public_answers)
    except DialogueQualificationError as error:
        raise DialogueQualificationError("public Alice dialogue proof failed") from error
    document = {
        "schema_version": SCHEMA_VERSION,
        "observed_epoch": int(clock()),
        "boot_id": boot_id_reader(),
        "ready": True,
        "local_chat_ready": True,
        "alice_public_ready": True,
        "history_verified": (
            local_checks["history_verified"]
            and public_checks["history_verified"]
        ),
        "free_dialogue_verified": (
            local_checks["free_dialogue_verified"]
            and public_checks["free_dialogue_verified"]
        ),
        "fake_tool_claim_absent": (
            local_checks["fake_tool_claim_absent"]
            and public_checks["fake_tool_claim_absent"]
        ),
        "local_answer_lengths": [len(answer) for answer in local_answers],
        "alice_answer_lengths": [len(answer) for answer in public_answers],
    }
    write_status(document, state_dir)
    return document


def main(argv: list[str] | None = None) -> int:
    check_only = list(sys.argv[1:] if argv is None else argv) == ["--check-status"]
    if not check_only and list(sys.argv[1:] if argv is None else argv):
        print("dialogue_qualification=unavailable", file=sys.stderr)
        return 2
    try:
        if check_only:
            document = read_status(current_boot_id=startup_self_check.read_boot_id())
        else:
            document = run_once()
    except DialogueQualificationError as error:
        reason = SAFE_FAILURE_CODES.get(str(error), "dialogue_internal")
        print(
            f"dialogue_qualification=unavailable reason={reason}",
            file=sys.stderr,
        )
        return 2
    except alice_skill_gateway.GatewayError:
        print(
            "dialogue_qualification=unavailable reason=alice_config",
            file=sys.stderr,
        )
        return 2
    except alice_tailscale_funnel.FunnelError:
        print(
            "dialogue_qualification=unavailable reason=alice_funnel",
            file=sys.stderr,
        )
        return 2
    except OSError:
        print(
            "dialogue_qualification=unavailable reason=local_io",
            file=sys.stderr,
        )
        return 2
    print("dialogue_qualification=ready" if check_only else json.dumps(
        document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
