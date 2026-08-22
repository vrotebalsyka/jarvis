#!/usr/bin/env python3
"""Private Yandex Dialogs gateway for full Home Butler conversations."""

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

import home_assistant_control as ha_control  # noqa: E402
import incident_status  # noqa: E402
import model_ha_control  # noqa: E402
import model_ha_proof  # noqa: E402
import owner_chat  # noqa: E402
from ollama_endpoint import EndpointConfigError  # noqa: E402


BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PENDING_SKILL_ID = "PENDING_PRIVATE_SKILL"
DEFAULT_CLAIM_FILE = Path(
    "/home/homebutler/.local/state/home-butler/alice/claim.json"
)
DEFAULT_ROTATION_MARKER = Path(
    "/home/homebutler/.local/state/home-butler/alice/webhook-next-used"
)
MAX_REQUEST_BYTES = 65_536
MAX_UTTERANCE_CHARS = 1024
MAX_SPEECH_CHARS = 900
MAX_MODEL_SPEECH_CHARS = 360
MAX_SESSIONS = 32
SESSION_TTL_SECONDS = 20 * 60
MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_CONTENT_CHARS = 1000
MODEL_TIMEOUT_SECONDS = 3.6
YANDEX_RESPONSE_LIMIT_SECONDS = 4.5
TURN_RESPONSE_BUDGET_SECONDS = 3.2
MAX_ACTIVE_TURNS = 4
VOICE_NUM_CTX = 2048
VOICE_NUM_PREDICT = 64
VOICE_KEEP_ALIVE = "24h"
VOICE_MODEL = owner_chat.MODEL
VOICE_TEMPERATURE = 0.15
MODEL_READINESS_RETRY_SECONDS = 10.0
RATE_WINDOW_SECONDS = 10
RATE_REQUESTS = 20
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,256}\Z")
EXIT_PHRASES = {
    "выход",
    "выйти",
    "закрой навык",
    "закрыть навык",
    "хватит",
    "до свидания",
}
PING_PHRASES = {"ping", "пинг"}


class GatewayError(RuntimeError):
    """A bounded, secret-free gateway rejection."""


def safe_failure_code(error: BaseException) -> str:
    """Return a fixed diagnostic code without serialising exception details."""
    if isinstance(error, GatewayError):
        return "gateway_rejected"
    if isinstance(error, owner_chat.OwnerChatError):
        return "owner_chat_failed"
    if isinstance(error, EndpointConfigError):
        return "ollama_endpoint_failed"
    if isinstance(error, model_ha_proof.ProofError):
        return "ha_read_proof_failed"
    if isinstance(error, model_ha_control.ControlProofError):
        return "ha_control_proof_failed"
    if isinstance(error, ha_control.ControlError):
        return "ha_control_failed"
    if isinstance(error, incident_status.IncidentStatusError):
        return "incident_status_failed"
    return "unexpected_failure"


def human_failure_message(error: BaseException, route: str) -> str:
    """Give Alice a concrete next step without exposing exception details."""

    if route == "home_assistant_control" or isinstance(
        error, (model_ha_control.ControlProofError, ha_control.ControlError)
    ):
        return (
            "Команда не отправлена: проверка Home Assistant не завершилась. "
            "Повторите действие и полное русское название устройства."
        )
    if route == "home_assistant" or isinstance(error, model_ha_proof.ProofError):
        return (
            "Проверка Home Assistant сейчас не завершилась. "
            "Я ничего не изменял; повторите запрос."
        )
    if route == "incidents" or isinstance(error, incident_status.IncidentStatusError):
        return (
            "Журнал состояния дома сейчас не ответил. "
            "Я ничего не изменял; повторите запрос."
        )
    if route == "general" or isinstance(
        error, (owner_chat.OwnerChatError, EndpointConfigError)
    ):
        return (
            "Локальная модель сейчас не ответила вовремя. "
            "Повторите последнюю фразу."
        )
    return "Запрос Алисы не прошёл проверку формата. Повторите фразу."


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
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
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
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(maximum + 2)
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
        if self.next_secret is None or secrets.compare_digest(
            self.next_secret, self.secret
        ):
            return None
        return f"/alice/{self.next_secret}"

    @property
    def rotation_staged(self) -> bool:
        return self.next_webhook_path is not None

    def secret_slot(self, path: str) -> str | None:
        if secrets.compare_digest(path, self.webhook_path):
            return "primary"
        next_path = self.next_webhook_path
        if next_path is not None and secrets.compare_digest(path, next_path):
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
        owner_ids = (
            ()
            if owners_raw == "-"
            else tuple(item.strip() for item in owners_raw.split(",") if item.strip())
        )
        port_text = os.environ.get("HOME_BUTLER_ALICE_PORT", str(DEFAULT_PORT))
        try:
            port = int(port_text, 10)
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
        not isinstance(original, str)
        or not isinstance(command, str)
        or len(original) > MAX_UTTERANCE_CHARS
        or len(command) > MAX_UTTERANCE_CHARS
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
        not isinstance(session_id, str)
        or not ID_RE.fullmatch(session_id)
        or not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or not 0 <= message_id <= 1_000_000
        or not isinstance(is_new, bool)
        or not isinstance(skill_id, str)
        or not ID_RE.fullmatch(skill_id)
        or skill_id == PENDING_SKILL_ID
        or (
            not config.pending
            and not secrets.compare_digest(skill_id, config.skill_id)
        )
        or (
            bool(config.owner_ids)
            and (
                not isinstance(user_id, str)
                or not any(
                    secrets.compare_digest(user_id, owner)
                    for owner in config.owner_ids
                )
            )
        )
    ):
        raise GatewayError("session identity is not allowed")
    utterance = " ".join((command or original).strip().split())
    return SkillTurn(session_id, message_id, is_new, utterance, skill_id, user_id)


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
    speech = speechify(value)
    boundaries = [
        match.end() for match in re.finditer(r"[.!?](?=\s|$)", speech)
    ]
    if len(boundaries) >= 2:
        speech = speech[: boundaries[1]].strip()
    elif len(boundaries) == 1 and len(speech) - boundaries[0] > 40:
        speech = speech[: boundaries[0]].strip()
    if len(speech) <= MAX_MODEL_SPEECH_CHARS:
        return speech
    shortened = speech[: MAX_MODEL_SPEECH_CHARS + 1]
    boundary = max(
        shortened.rfind(". "),
        shortened.rfind("! "),
        shortened.rfind("? "),
    )
    if boundary >= MAX_MODEL_SPEECH_CHARS // 3:
        return shortened[: boundary + 1].strip()
    word = shortened.rfind(" ")
    if word >= MAX_MODEL_SPEECH_CHARS // 2:
        shortened = shortened[:word]
    return shortened.rstrip(" ,;:-") + "…"


def skill_response(text: str, *, end_session: bool = False) -> dict[str, Any]:
    speech = speechify(text)
    return {
        "version": "1.0",
        "response": {
            "text": speech,
            "tts": speech,
            "end_session": end_session,
        },
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
                key: value
                for key, value in self._records.items()
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
            self._observed = [
                value for value in self._observed if now - value < RATE_WINDOW_SECONDS
            ]
            if len(self._observed) >= RATE_REQUESTS:
                return False
            self._observed.append(now)
            return True


def write_provisioning_claim(skill_id: str, user_id: str | None) -> None:
    path = Path(os.environ.get("HOME_BUTLER_ALICE_CLAIM_FILE", str(DEFAULT_CLAIM_FILE)))
    directory = path.parent
    try:
        metadata = directory.stat()
    except OSError as error:
        raise GatewayError("provisioning state directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise GatewayError("provisioning state directory is unsafe")
    document = {"skill_id": skill_id, "user_id": user_id}
    raw = json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            existing = path.read_bytes()
            existing_metadata = path.stat()
        except OSError as error:
            raise GatewayError("provisioning claim is unavailable") from error
        if (
            existing != raw
            or not stat.S_ISREG(existing_metadata.st_mode)
            or existing_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(existing_metadata.st_mode) != 0o600
            or existing_metadata.st_nlink != 1
        ):
            raise GatewayError("provisioning claim conflicts with the first request")
        return
    except OSError as error:
        raise GatewayError("provisioning claim could not be created") from error
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def rotation_marker(secret: str) -> str:
    if not SECRET_RE.fullmatch(secret):
        raise GatewayError("next webhook secret is invalid")
    return hashlib.blake2s(secret.encode("ascii"), digest_size=16).hexdigest()


def write_rotation_marker(secret: str) -> None:
    path = Path(
        os.environ.get(
            "HOME_BUTLER_ALICE_ROTATION_MARKER", str(DEFAULT_ROTATION_MARKER)
        )
    )
    directory = path.parent
    try:
        metadata = directory.stat()
    except OSError as error:
        raise GatewayError("rotation state directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise GatewayError("rotation state directory is unsafe")
    raw = (rotation_marker(secret) + "\n").encode("ascii")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            existing = path.read_bytes()
            existing_metadata = path.stat()
        except OSError as error:
            raise GatewayError("rotation marker is unavailable") from error
        if (
            existing != raw
            or not stat.S_ISREG(existing_metadata.st_mode)
            or existing_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(existing_metadata.st_mode) != 0o600
            or existing_metadata.st_nlink != 1
        ):
            raise GatewayError("rotation marker conflicts with the staged secret")
        return
    except OSError as error:
        raise GatewayError("rotation marker could not be created") from error
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fast_model_answer(
    question: str,
    context: dict[str, Any],
    history: list[dict[str, str]],
) -> str:
    route = owner_chat.classify_request(question)
    if route == "general":
        if owner_chat.is_free_dialogue_capability_question(question):
            profile = "voice_free_dialogue"
        elif owner_chat.is_capability_question(question):
            profile = "voice_identity"
        else:
            profile = "voice"
        return owner_chat.general_response(
            question,
            {"mode": "voice_conversation"},
            history,
            timeout_seconds=MODEL_TIMEOUT_SECONDS,
            num_ctx=VOICE_NUM_CTX,
            num_predict=VOICE_NUM_PREDICT,
            keep_alive=VOICE_KEEP_ALIVE,
            model_name=VOICE_MODEL,
            temperature=VOICE_TEMPERATURE,
            profile=profile,
        )
    if route == "home_assistant":
        return owner_chat.voice_ha_response(question)
    if route == "home_assistant_control":
        return owner_chat.control_response(question, voice=True)
    return owner_chat.answer(question, context, history)


def warm_voice_model() -> None:
    endpoint = owner_chat.load_runtime_ollama_endpoint()
    model_ha_proof.call_ollama(
        endpoint,
        "/api/generate",
        {
            "model": VOICE_MODEL,
            "prompt": "Ответь одним словом: готов",
            "stream": False,
            "think": False,
            "keep_alive": VOICE_KEEP_ALIVE,
            "options": {
                "temperature": 0,
                "num_ctx": VOICE_NUM_CTX,
                "num_predict": 8,
            },
        },
        timeout=120,
    )
    evidence = model_ha_proof.gpu_evidence(
        model_ha_proof.get_ollama(endpoint, "/api/ps"),
        expected_model=VOICE_MODEL,
    )
    if endpoint.host == "127.0.0.1" or evidence.get("fully_on_gpu") is not True:
        raise GatewayError("voice model is not fully loaded on the GPU")


def _history_item(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content[:MAX_HISTORY_CONTENT_CHARS]}


class SkillApplication:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        answerer: Callable[[str, dict[str, Any], list[dict[str, str]]], str] = fast_model_answer,
        context: dict[str, Any] | None = None,
        sessions: SessionStore | None = None,
        claim_writer: Callable[[str, str | None], None] = write_provisioning_claim,
    ) -> None:
        self.config = config
        self.answerer = answerer
        self.claim_writer = claim_writer
        self._stop = threading.Event()
        self._model_ready = threading.Event()
        self._readiness_thread: threading.Thread | None = None
        if context is None and not config.pending:
            # Bind the webhook immediately. Model warm-up and HA context loading are
            # deliberately asynchronous so Alice never receives a connection error
            # merely because the GPU runtime is still starting.
            self.context = {"mode": "voice_conversation", "runtime": "starting"}
            self._readiness_thread = threading.Thread(
                target=self._prepare_runtime,
                name="alice-model-readiness",
                daemon=True,
            )
            self._readiness_thread.start()
        else:
            self.context = {} if context is None else context
            if not config.pending:
                self._model_ready.set()
        self.sessions = SessionStore() if sessions is None else sessions

    def _prepare_runtime(self) -> None:
        while not self._stop.is_set():
            try:
                warm_voice_model()
                context = owner_chat.startup_context()
            except Exception as error:  # keep the lightweight webhook alive
                print(
                    json.dumps(
                        {
                            "component": "alice_skill_gateway",
                            "error_code": safe_failure_code(error),
                            "event": "model_not_ready",
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    flush=True,
                )
                self._stop.wait(MODEL_READINESS_RETRY_SECONDS)
                continue
            self.context = context
            self._model_ready.set()
            print(
                '{"component":"alice_skill_gateway","event":"model_ready"}',
                flush=True,
            )
            return

    def close(self) -> None:
        self._stop.set()
        if self._readiness_thread is not None:
            self._readiness_thread.join(timeout=1.0)

    def process(self, document: dict[str, Any]) -> tuple[dict[str, Any], str]:
        turn = validate_request(document, self.config)
        if self.config.pending:
            self.claim_writer(turn.skill_id, turn.user_id)
            return (
                skill_response(
                    "Привязка Дворецкого получена. Вернитесь к владельцу для завершения настройки."
                ),
                "provisioning",
            )
        record = self.sessions.get(turn.session_id, turn.is_new)
        with record.lock:
            if turn.message_id == record.last_message_id and record.last_response is not None:
                return record.last_response, "duplicate"
            if turn.message_id < record.last_message_id:
                raise GatewayError("out-of-order message")

            normalized = turn.utterance.casefold()
            if normalized in PING_PHRASES:
                response = skill_response("Дворецкий на связи.")
                route = "ping"
            elif normalized in EXIT_PHRASES:
                response = skill_response("До свидания.", end_session=True)
                route = "exit"
            elif turn.is_new and not turn.utterance:
                response = skill_response(
                    "Дворецкий на связи. Говорите свободно: я помню текущий разговор и могу работать с Home Assistant."
                )
                route = "welcome"
            elif not turn.utterance:
                response = skill_response("Я не расслышал. Повторите, пожалуйста.")
                route = "empty"
            elif not self._model_ready.is_set():
                response = skill_response(
                    "Дворецкий на связи, локальная модель ещё запускается. "
                    "Повторите вопрос через несколько секунд."
                )
                route = "model_starting"
            else:
                answer = self.answerer(turn.utterance, self.context, list(record.history))
                speech = compact_model_speech(answer)
                response = skill_response(speech)
                record.history.extend(
                    [_history_item("user", turn.utterance), _history_item("assistant", speech)]
                )
                record.history = record.history[-MAX_HISTORY_MESSAGES:]
                route = owner_chat.classify_request(turn.utterance)

            record.last_message_id = turn.message_id
            record.last_response = response
            record.touched_at = self.sessions.clock()
            return response, route


def session_fingerprint(session_id: str) -> str:
    return hashlib.blake2s(session_id.encode("utf-8"), digest_size=6).hexdigest()


def deadline_message(route: str, *, busy: bool = False) -> str:
    """Return an honest answer before Alice closes its 4.5 second window."""

    if busy:
        return (
            "Я на связи, но заканчиваю предыдущий запрос. "
            "Повторите фразу через несколько секунд."
        )
    if route == "home_assistant_control":
        return (
            "Команду принял и ещё проверяю результат. Не повторяйте её; "
            "через несколько секунд спросите состояние устройства."
        )
    if route == "home_assistant":
        return (
            "Проверка Home Assistant продолжается. Я ничего не меняю; "
            "через несколько секунд спросите ещё раз."
        )
    if route == "incidents":
        return (
            "Собираю журнал состояния дома. "
            "Повторите вопрос через несколько секунд."
        )
    return (
        "Локальная модель ещё формулирует ответ. "
        "Повторите вопрос через несколько секунд."
    )


class BoundedTurnExecutor:
    """Bound slow local work while keeping the Yandex webhook responsive."""

    def __init__(
        self,
        application: SkillApplication,
        *,
        timeout_seconds: float = TURN_RESPONSE_BUDGET_SECONDS,
        max_active_turns: int = MAX_ACTIVE_TURNS,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds >= YANDEX_RESPONSE_LIMIT_SECONDS:
            raise ValueError("turn response budget is outside the Yandex limit")
        if max_active_turns < 1:
            raise ValueError("active turn limit must be positive")
        self.application = application
        self.timeout_seconds = timeout_seconds
        self._slots = threading.BoundedSemaphore(max_active_turns)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_active_turns,
            thread_name_prefix="alice-turn",
        )

    def _finished(
        self,
        future: concurrent.futures.Future[tuple[dict[str, Any], str]],
    ) -> None:
        try:
            future.exception()
        except concurrent.futures.CancelledError:
            pass
        finally:
            self._slots.release()

    def run(
        self,
        document: dict[str, Any],
        fallback_route: str,
    ) -> tuple[dict[str, Any], str, str]:
        if not self._slots.acquire(blocking=False):
            return skill_response(deadline_message(fallback_route, busy=True)), fallback_route, "busy"
        try:
            future = self._executor.submit(self.application.process, document)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(self._finished)
        try:
            response, route = future.result(timeout=self.timeout_seconds)
            return response, route, "completed"
        except concurrent.futures.TimeoutError:
            return (
                skill_response(deadline_message(fallback_route)),
                fallback_route,
                "deferred",
            )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class GatewayHandler(http.server.BaseHTTPRequestHandler):
    server_version = "HomeButlerAlice/1"
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
        started_at = time.monotonic()
        session_id: object = "invalid"
        failure_route = "unknown"
        secret_slot = self.application.config.secret_slot(self.path)
        if secret_slot is None:
            self._reject(404)
            return
        if not self.limiter.accept():
            self._reject(429)
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._reject(400)
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
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
        try:
            document = parse_json(self.rfile.read(length))
            session = document.get("session")
            session_id = session.get("session_id") if isinstance(session, dict) else "invalid"
            request = document.get("request")
            command = request.get("command") if isinstance(request, dict) else None
            if isinstance(command, str):
                failure_route = owner_chat.classify_request(command)
            if secret_slot == "next":
                validate_request(document, self.application.config)
                assert self.application.config.next_secret is not None
                write_rotation_marker(self.application.config.next_secret)
            response, route, disposition = self.turns.run(document, failure_route)
            self._send(200, response)
            print(
                json.dumps(
                    {
                        "component": "alice_skill_gateway",
                        "event": f"turn_{disposition}",
                        "latency_ms": max(
                            0, round((time.monotonic() - started_at) * 1000)
                        ),
                        "secret_slot": secret_slot,
                        "route": route,
                        "session": session_fingerprint(str(session_id)),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                flush=True,
            )
        except (
            GatewayError,
            owner_chat.OwnerChatError,
            EndpointConfigError,
            model_ha_proof.ProofError,
            model_ha_control.ControlProofError,
            ha_control.ControlError,
            incident_status.IncidentStatusError,
        ) as error:
            self._send(
                200,
                skill_response(human_failure_message(error, failure_route)),
            )
            print(
                json.dumps(
                    {
                        "component": "alice_skill_gateway",
                        "error_code": safe_failure_code(error),
                        "event": "turn_failed",
                        "latency_ms": max(
                            0, round((time.monotonic() - started_at) * 1000)
                        ),
                        "secret_slot": secret_slot,
                        "session": session_fingerprint(str(session_id)),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                flush=True,
            )

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
        application = SkillApplication(config)
        server = GatewayServer(application)
    except GatewayError:
        print("Alice skill gateway configuration rejected.", file=sys.stderr)
        return 2
    try:
        print(
            json.dumps(
                {
                    "component": "alice_skill_gateway",
                    "event": "started",
                    "bind": f"{config.bind_host}:{config.port}",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
