#!/usr/bin/env python3
"""Private Yandex Alice transport for the single read-only owner chat core."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.server
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import bounded_ha_agent  # noqa: E402
import home_assistant_read  # noqa: E402
import model_runtime_policy  # noqa: E402
import owner_chat  # noqa: E402
from ollama_endpoint import EndpointConfigError, load_runtime_ollama_endpoint  # noqa: E402


BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PENDING_SKILL_ID = "PENDING_PRIVATE_SKILL"
DEFAULT_CLAIM_FILE = Path("/home/homebutler/.local/state/home-butler/alice/claim.json")
DEFAULT_ROTATION_MARKER = Path(
    "/home/homebutler/.local/state/home-butler/alice/webhook-next-used"
)
MAX_REQUEST_BYTES = 65_536
MAX_UTTERANCE_CHARS = 1024
MAX_SPEECH_CHARS = 900
MAX_MODEL_SPEECH_CHARS = 900
MAX_SESSIONS = 32
SESSION_TTL_SECONDS = 20 * 60
MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_CONTENT_CHARS = 1000
YANDEX_RESPONSE_LIMIT_SECONDS = 4.5
VOICE_RUNTIME_PROFILE = "voice_fast"
VOICE_POLICY = model_runtime_policy.get_profile(VOICE_RUNTIME_PROFILE)
TURN_RESPONSE_BUDGET_SECONDS = 4.0
MAX_ACTIVE_TURNS = 4
MODEL_READINESS_RETRY_SECONDS = 10.0
RATE_WINDOW_SECONDS = 10
RATE_REQUESTS = 20
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,256}\Z")
EXIT_PHRASES = {"выход", "выйти", "закрой навык", "закрыть навык", "до свидания"}
PING_PHRASES = {"ping", "пинг"}
HEALTH_MODEL_COMMAND = "__homebutler_health_model_v1__"
HEALTH_HA_READ_COMMAND = "__homebutler_health_ha_read_v1__"


class GatewayError(RuntimeError):
    """A bounded, secret-free gateway rejection."""


def safe_failure_code(error: BaseException) -> str:
    if isinstance(error, GatewayError):
        return "gateway_rejected"
    if isinstance(error, owner_chat.OwnerChatError):
        return "owner_chat_failed"
    if isinstance(error, EndpointConfigError):
        return "ollama_endpoint_failed"
    if isinstance(error, bounded_ha_agent.BoundedAgentError):
        return "bounded_read_failed"
    return "unexpected_failure"


def human_failure_message(_error: BaseException, _route: str) -> str:
    return (
        "Проверка сейчас не завершилась. Я ничего не изменял; "
        "повторите запрос через несколько секунд."
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GatewayError("duplicate JSON key")
        result[key] = value
    return result


def parse_json(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise GatewayError("invalid request size")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GatewayError("invalid JSON") from error
    if not isinstance(document, dict):
        raise GatewayError("request is not an object")
    return document


def _read_credential(name: str, minimum: int, maximum: int) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise GatewayError("systemd credentials are unavailable")
    path = Path(directory) / name
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise GatewayError("required credential is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum + 1
        ):
            raise GatewayError("credential metadata is unsafe")
        raw = os.read(descriptor, maximum + 2)
    finally:
        os.close(descriptor)
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise GatewayError("credential is malformed") from error
    if not minimum <= len(value) <= maximum or any(ord(char) < 33 for char in value):
        raise GatewayError("credential value is invalid")
    return value


@dataclass(frozen=True)
class GatewayConfig:
    secret: str
    skill_id: str
    owner_ids: tuple[str, ...]
    next_secret: str | None = None
    bind_host: str = BIND_HOST
    port: int = DEFAULT_PORT

    def __post_init__(self) -> None:
        if not SECRET_RE.fullmatch(self.secret):
            raise GatewayError("webhook secret is invalid")
        if self.next_secret is not None and not SECRET_RE.fullmatch(self.next_secret):
            raise GatewayError("next webhook secret is invalid")
        if self.skill_id != PENDING_SKILL_ID and not ID_RE.fullmatch(self.skill_id):
            raise GatewayError("skill ID is invalid")
        if any(not ID_RE.fullmatch(value) for value in self.owner_ids):
            raise GatewayError("owner allow-list is invalid")
        if self.bind_host != BIND_HOST:
            raise GatewayError("gateway must bind to loopback")
        if isinstance(self.port, bool) or not 1024 <= self.port <= 65535:
            raise GatewayError("gateway port is invalid")

    @property
    def webhook_path(self) -> str:
        return f"/alice/{self.secret}"

    @property
    def next_webhook_path(self) -> str | None:
        if self.next_secret is None or secrets.compare_digest(self.next_secret, self.secret):
            return None
        return f"/alice/{self.next_secret}"

    @property
    def rotation_staged(self) -> bool:
        return self.next_webhook_path is not None

    def secret_slot(self, path: str) -> str | None:
        if secrets.compare_digest(path, self.webhook_path):
            return "primary"
        if self.next_webhook_path is not None and secrets.compare_digest(
            path, self.next_webhook_path
        ):
            return "next"
        return None

    @property
    def pending(self) -> bool:
        return self.skill_id == PENDING_SKILL_ID

    @classmethod
    def load(cls) -> "GatewayConfig":
        secret = _read_credential("alice-skill-secret", 32, 128)
        next_secret = _read_credential("alice-skill-secret-next", 32, 128)
        skill_id = _read_credential("alice-skill-id", 8, 256)
        owners_raw = _read_credential("alice-owner-ids", 1, 2048)
        owner_ids = () if owners_raw == "-" else tuple(
            item.strip() for item in owners_raw.split(",") if item.strip()
        )
        try:
            port = int(os.environ.get("HOME_BUTLER_ALICE_PORT", str(DEFAULT_PORT)), 10)
        except ValueError as error:
            raise GatewayError("gateway port is invalid") from error
        if secrets.compare_digest(secret, next_secret):
            next_secret = None
        return cls(secret, skill_id, owner_ids, next_secret=next_secret, port=port)


@dataclass(frozen=True)
class SkillTurn:
    session_id: str
    message_id: int
    is_new: bool
    utterance: str
    skill_id: str
    user_id: str | None


def validate_request(document: dict[str, Any], config: GatewayConfig) -> SkillTurn:
    if document.get("version") != "1.0":
        raise GatewayError("unsupported protocol version")
    request = document.get("request")
    session = document.get("session")
    if not isinstance(request, dict) or not isinstance(session, dict):
        raise GatewayError("required request fields are absent")
    if request.get("type") != "SimpleUtterance":
        raise GatewayError("unsupported request type")
    original = request.get("original_utterance")
    command = request.get("command")
    if (
        not isinstance(original, str) or not isinstance(command, str)
        or len(original) > MAX_UTTERANCE_CHARS or len(command) > MAX_UTTERANCE_CHARS
        or any(ord(char) < 32 and char not in "\t\n\r" for char in original + command)
    ):
        raise GatewayError("utterance is invalid")
    session_id = session.get("session_id")
    message_id = session.get("message_id")
    is_new = session.get("new")
    skill_id = session.get("skill_id")
    user = session.get("user")
    user_id = user.get("user_id") if isinstance(user, dict) else None
    if (
        not isinstance(session_id, str) or not ID_RE.fullmatch(session_id)
        or not isinstance(message_id, int) or isinstance(message_id, bool)
        or not 0 <= message_id <= 1_000_000
        or not isinstance(is_new, bool)
        or not isinstance(skill_id, str) or not ID_RE.fullmatch(skill_id)
        or skill_id == PENDING_SKILL_ID
        or (not config.pending and not secrets.compare_digest(skill_id, config.skill_id))
        or (
            bool(config.owner_ids)
            and (
                not isinstance(user_id, str)
                or not any(secrets.compare_digest(user_id, owner) for owner in config.owner_ids)
            )
        )
    ):
        raise GatewayError("session identity is not allowed")
    return SkillTurn(
        session_id, message_id, is_new,
        " ".join((command or original).strip().split()), skill_id, user_id,
    )


def speechify(value: str) -> str:
    if not isinstance(value, str):
        raise GatewayError("model response is invalid")
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", value)
    text = re.sub(r"[`*_#>]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise GatewayError("model response is empty")
    if len(text) <= MAX_SPEECH_CHARS:
        return text
    shortened = text[: MAX_SPEECH_CHARS + 1]
    boundary = max(shortened.rfind(". "), shortened.rfind("! "), shortened.rfind("? "))
    if boundary >= MAX_SPEECH_CHARS // 2:
        return shortened[: boundary + 1]
    return shortened[: MAX_SPEECH_CHARS - 1].rstrip() + "…"


def compact_model_speech(value: str) -> str:
    return speechify(value)[:MAX_MODEL_SPEECH_CHARS]


def skill_response(text: str, *, end_session: bool = False) -> dict[str, Any]:
    speech = speechify(text)
    return {
        "version": "1.0",
        "response": {"text": speech, "tts": speech, "end_session": end_session},
    }


@dataclass
class SessionRecord:
    touched_at: float
    history: list[dict[str, str]] = field(default_factory=list)
    last_message_id: int = -1
    last_response: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class SessionStore:
    def __init__(
        self,
        *,
        ttl_seconds: int = SESSION_TTL_SECONDS,
        max_sessions: int = MAX_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self.clock = clock
        self._records: dict[str, SessionRecord] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str, reset: bool) -> SessionRecord:
        now = self.clock()
        with self._lock:
            self._records = {
                key: value for key, value in self._records.items()
                if now - value.touched_at <= self.ttl_seconds
            }
            if reset:
                self._records.pop(session_id, None)
            record = self._records.get(session_id)
            if record is None:
                if len(self._records) >= self.max_sessions:
                    oldest = min(self._records, key=lambda key: self._records[key].touched_at)
                    self._records.pop(oldest, None)
                record = SessionRecord(now)
                self._records[session_id] = record
            record.touched_at = now
            return record


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._observed: list[float] = []
        self._lock = threading.Lock()

    def accept(self) -> bool:
        now = self.clock()
        with self._lock:
            self._observed = [value for value in self._observed if now - value < RATE_WINDOW_SECONDS]
            if len(self._observed) >= RATE_REQUESTS:
                return False
            self._observed.append(now)
            return True


def _private_state_path(environment_key: str, default: Path) -> Path:
    path = Path(os.environ.get(environment_key, str(default)))
    try:
        metadata = path.parent.stat()
    except OSError as error:
        raise GatewayError("Alice state directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise GatewayError("Alice state directory is unsafe")
    return path


def _create_private_file(path: Path, raw: bytes, conflict: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            existing = path.read_bytes()
            metadata = path.stat()
        except OSError as error:
            raise GatewayError("Alice state file is unavailable") from error
        if (
            existing != raw or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise GatewayError(conflict)
        return
    except OSError as error:
        raise GatewayError("Alice state file could not be created") from error
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_provisioning_claim(skill_id: str, user_id: str | None) -> None:
    path = _private_state_path("HOME_BUTLER_ALICE_CLAIM_FILE", DEFAULT_CLAIM_FILE)
    raw = json.dumps(
        {"skill_id": skill_id, "user_id": user_id},
        ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")
    _create_private_file(path, raw, "provisioning claim conflicts with first request")


def rotation_marker(secret: str) -> str:
    if not SECRET_RE.fullmatch(secret):
        raise GatewayError("next webhook secret is invalid")
    return hashlib.blake2s(secret.encode("ascii"), digest_size=16).hexdigest()


def write_rotation_marker(secret: str) -> None:
    path = _private_state_path(
        "HOME_BUTLER_ALICE_ROTATION_MARKER", DEFAULT_ROTATION_MARKER
    )
    _create_private_file(
        path, (rotation_marker(secret) + "\n").encode("ascii"),
        "rotation marker conflicts with staged secret",
    )


def natural_voice_answer(
    question: str,
    context: dict[str, Any],
    history: list[dict[str, str]],
) -> str:
    return owner_chat.answer_natural(
        question, context, history, voice=True, runtime_profile=VOICE_RUNTIME_PROFILE
    )


def warm_voice_model() -> None:
    endpoint = load_runtime_ollama_endpoint()
    result = bounded_ha_agent.call_ollama(
        endpoint,
        "/api/generate",
        model_runtime_policy.build_generate_payload(
            VOICE_RUNTIME_PROFILE, "Ответь одним словом: готов"
        ),
        timeout=VOICE_POLICY.request_timeout_seconds,
    )
    if not isinstance(result.get("response"), str) or not result["response"].strip():
        raise GatewayError("voice model returned an empty probe")


def synthetic_ha_read() -> None:
    snapshot, exit_code = home_assistant_read.execute_safely("snapshot")
    if (
        exit_code != 0 or not isinstance(snapshot, dict)
        or snapshot.get("status") not in {"healthy", "stale_data"}
        or snapshot.get("service_calls") not in {None, 0}
    ):
        raise GatewayError("Home Assistant read probe failed")


def _history_item(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content[:MAX_HISTORY_CONTENT_CHARS]}


class SkillApplication:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        answerer: Callable[[str, dict[str, Any], list[dict[str, str]]], str] = natural_voice_answer,
        context: dict[str, Any] | None = None,
        sessions: SessionStore | None = None,
        claim_writer: Callable[[str, str | None], None] = write_provisioning_claim,
        model_health_probe: Callable[[], None] = warm_voice_model,
        ha_health_probe: Callable[[], None] = synthetic_ha_read,
    ) -> None:
        self.config = config
        self.answerer = answerer
        self.claim_writer = claim_writer
        self.model_health_probe = model_health_probe
        self.ha_health_probe = ha_health_probe
        self.sessions = SessionStore() if sessions is None else sessions
        self.context = {} if context is None else context
        self._model_ready = threading.Event()
        self._stop = threading.Event()
        self._readiness_thread: threading.Thread | None = None
        if not config.pending and context is None:
            self._readiness_thread = threading.Thread(
                target=self._prepare_runtime, name="alice-model-readiness", daemon=True
            )
            self._readiness_thread.start()
        elif not config.pending:
            self._model_ready.set()

    def _prepare_runtime(self) -> None:
        while not self._stop.is_set():
            try:
                self.model_health_probe()
                self.context = owner_chat.startup_context()
            except Exception:
                self._stop.wait(MODEL_READINESS_RETRY_SECONDS)
                continue
            self._model_ready.set()
            print('{"component":"alice_skill_gateway","event":"model_ready"}', flush=True)
            return

    def close(self) -> None:
        self._stop.set()
        if self._readiness_thread is not None:
            self._readiness_thread.join(timeout=1.0)

    def process(self, document: dict[str, Any]) -> tuple[dict[str, Any], str]:
        turn = validate_request(document, self.config)
        if self.config.pending:
            self.claim_writer(turn.skill_id, turn.user_id)
            return skill_response("Привязка Дворецкого получена."), "provisioning"
        if turn.utterance == HEALTH_MODEL_COMMAND:
            if not self._model_ready.is_set():
                raise GatewayError("voice model readiness is not established")
            return skill_response("Локальная модель отвечает."), "health_model"
        if turn.utterance == HEALTH_HA_READ_COMMAND:
            self.ha_health_probe()
            return skill_response("Home Assistant доступен для чтения."), "health_ha_read"
        record = self.sessions.get(turn.session_id, turn.is_new)
        with record.lock:
            if turn.message_id == record.last_message_id and record.last_response is not None:
                return record.last_response, "duplicate"
            if turn.message_id < record.last_message_id:
                raise GatewayError("out-of-order message")
            normalized = turn.utterance.casefold()
            if normalized in PING_PHRASES:
                response, route = skill_response("Дворецкий на связи."), "ping"
            elif normalized in EXIT_PHRASES:
                response, route = skill_response("До свидания.", end_session=True), "exit"
            elif turn.is_new and not turn.utterance:
                response, route = skill_response(
                    "Дворецкий на связи. Задайте вопрос о текущем состоянии дома."
                ), "welcome"
            elif not turn.utterance:
                response, route = skill_response("Я не расслышал. Повторите."), "empty"
            elif not self._model_ready.is_set():
                response, route = skill_response(
                    "Локальная модель ещё запускается. Повторите через несколько секунд."
                ), "model_starting"
            else:
                answer = self.answerer(turn.utterance, dict(self.context), list(record.history))
                speech = compact_model_speech(answer)
                response, route = skill_response(speech), "read_only_conversation"
                record.history.extend([
                    _history_item("user", turn.utterance),
                    _history_item("assistant", speech),
                ])
                record.history = record.history[-MAX_HISTORY_MESSAGES:]
            record.last_message_id = turn.message_id
            record.last_response = response
            record.touched_at = self.sessions.clock()
            return response, route


def session_fingerprint(session_id: str) -> str:
    return hashlib.blake2s(session_id.encode("utf-8"), digest_size=6).hexdigest()


def deadline_message(_route: str, *, busy: bool = False) -> str:
    if busy:
        return "Заканчиваю предыдущий read-only запрос. Повторите через несколько секунд."
    return (
        "Проверка Home Assistant продолжается. Я ничего не меняю; "
        "повторите вопрос через несколько секунд."
    )


class BoundedTurnExecutor:
    def __init__(
        self,
        application: SkillApplication,
        *,
        timeout_seconds: float = TURN_RESPONSE_BUDGET_SECONDS,
        max_active_turns: int = MAX_ACTIVE_TURNS,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds >= YANDEX_RESPONSE_LIMIT_SECONDS:
            raise ValueError("turn response budget is outside the Yandex limit")
        self.application = application
        self.timeout_seconds = timeout_seconds
        self._slots = threading.BoundedSemaphore(max_active_turns)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_active_turns, thread_name_prefix="alice-read"
        )

    def _finished(self, future: concurrent.futures.Future[Any]) -> None:
        try:
            future.exception()
        except concurrent.futures.CancelledError:
            pass
        finally:
            self._slots.release()

    def run(self, document: dict[str, Any], route: str) -> tuple[dict[str, Any], str, str]:
        if not self._slots.acquire(blocking=False):
            return skill_response(deadline_message(route, busy=True)), route, "busy"
        try:
            future = self._executor.submit(self.application.process, document)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(self._finished)
        try:
            response, result_route = future.result(timeout=self.timeout_seconds)
            return response, result_route, "completed"
        except concurrent.futures.TimeoutError:
            return skill_response(deadline_message(route)), route, "timeout_read_only"

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class GatewayHandler(http.server.BaseHTTPRequestHandler):
    server_version = "HomeButlerAlice/2"
    sys_version = ""

    @property
    def application(self) -> SkillApplication:
        return self.server.application  # type: ignore[attr-defined,no-any-return]

    @property
    def limiter(self) -> RateLimiter:
        return self.server.limiter  # type: ignore[attr-defined,no-any-return]

    @property
    def turns(self) -> BoundedTurnExecutor:
        return self.server.turns  # type: ignore[attr-defined,no-any-return]

    def _send(self, status: int, document: dict[str, Any]) -> None:
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _reject(self, status: int) -> None:
        self._send(status, {"error": "request rejected"})

    def do_POST(self) -> None:  # noqa: N802
        started = time.monotonic()
        session_id: object = "invalid"
        slot = self.application.config.secret_slot(self.path)
        if slot is None:
            self._reject(404)
            return
        if not self.limiter.accept():
            self._reject(429)
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._reject(400)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
            self._reject(415)
            return
        try:
            length = int(self.headers.get("Content-Length", ""), 10)
        except ValueError:
            self._reject(411)
            return
        if not 1 <= length <= MAX_REQUEST_BYTES:
            self._reject(413)
            return
        route = "read_only_conversation"
        try:
            document = parse_json(self.rfile.read(length))
            session = document.get("session")
            session_id = session.get("session_id") if isinstance(session, dict) else "invalid"
            if slot == "next":
                validate_request(document, self.application.config)
                assert self.application.config.next_secret is not None
                write_rotation_marker(self.application.config.next_secret)
            response, result_route, disposition = self.turns.run(document, route)
            self._send(200, response)
            print(json.dumps({
                "component": "alice_skill_gateway",
                "event": f"turn_{disposition}",
                "latency_ms": max(0, round((time.monotonic() - started) * 1000)),
                "route": result_route,
                "secret_slot": slot,
                "session": session_fingerprint(str(session_id)),
            }, separators=(",", ":"), sort_keys=True), flush=True)
        except (
            GatewayError, owner_chat.OwnerChatError, EndpointConfigError,
            bounded_ha_agent.BoundedAgentError,
        ) as error:
            self._send(200, skill_response(human_failure_message(error, route)))
            print(json.dumps({
                "component": "alice_skill_gateway",
                "error_code": safe_failure_code(error),
                "event": "turn_failed",
                "latency_ms": max(0, round((time.monotonic() - started) * 1000)),
                "session": session_fingerprint(str(session_id)),
            }, separators=(",", ":"), sort_keys=True), flush=True)

    def do_GET(self) -> None:  # noqa: N802
        self._reject(405)

    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET

    def log_message(self, _format: str, *_args: object) -> None:
        return


class GatewayServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, application: SkillApplication) -> None:
        super().__init__((application.config.bind_host, application.config.port), GatewayHandler)
        self.application = application
        self.limiter = RateLimiter()
        self.turns = BoundedTurnExecutor(application)

    def server_close(self) -> None:
        self.turns.close()
        self.application.close()
        super().server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        config = GatewayConfig.load()
        if arguments.check:
            print(
                f"alice_skill_gateway=ready bind={config.bind_host}:{config.port} "
                f"owners={len(config.owner_ids)} rotation_staged={str(config.rotation_staged).lower()}"
            )
            return 0
        server = GatewayServer(SkillApplication(config))
    except GatewayError:
        print("Alice skill gateway configuration rejected.", file=sys.stderr)
        return 2
    try:
        print(json.dumps({
            "component": "alice_skill_gateway", "event": "started",
            "bind": f"{config.bind_host}:{config.port}",
        }, separators=(",", ":"), sort_keys=True), flush=True)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
