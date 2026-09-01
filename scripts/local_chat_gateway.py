#!/usr/bin/env python3
"""Protected browser transport for the single owner_chat path."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import owner_chat  # noqa: E402


DEFAULT_PORT = 8780
MAX_REQUEST_BYTES = 16 * 1024
MAX_MESSAGE_CHARS = 2_000
MAX_SESSIONS = 16
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_COOKIE = "home_butler_session"
AUTH_COOKIE = "home_butler_lan_auth"
SESSION_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")

LOGIN_HTML = """<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content="width=device-width"><meta name=home-butler-csrf content="__CSRF__"><title>Вход</title><style>body{font:16px system-ui;background:#0b1118;color:#eef4f2;display:grid;place-items:center;min-height:90vh}main{width:min(420px,90%)}input,button{box-sizing:border-box;width:100%;padding:14px;margin:6px 0;border-radius:10px}</style><main><h1>Домашний дворецкий</h1><p>Введите ключ владельца.</p><form><input id=k type=password><button>Войти</button></form><p id=e></p></main><script>const c=document.querySelector('meta').content;document.querySelector('form').onsubmit=async x=>{x.preventDefault();let r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json','X-Home-Butler-CSRF':c},body:JSON.stringify({key:k.value})});if(r.ok)location.reload();else e.textContent='Ключ не принят'}</script></html>"""

HTML = """<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content="width=device-width"><meta name=home-butler-csrf content="__CSRF__"><title>Домашний дворецкий</title><style>body{margin:0;font:16px system-ui;background:#0b1118;color:#eef4f2}main{max-width:820px;margin:auto;padding:28px}#m{min-height:55vh;display:flex;flex-direction:column;gap:12px}.a,.u{padding:12px 15px;border-radius:14px;max-width:78%;white-space:pre-wrap}.a{background:#1b2d3a}.u{background:#d8e9e8;color:#0c171b;align-self:flex-end}form{display:flex;gap:10px}textarea{flex:1;padding:12px;border-radius:10px}button{padding:0 20px;border:0;border-radius:10px;background:#63d4df}</style><main><h1>Домашний дворецкий</h1><p>Только чтение текущих данных Home Assistant.</p><div id=m><div class=a>Я на связи. Спросите о состоянии устройства обычными словами.</div></div><form><textarea id=q maxlength=2000></textarea><button>Отправить</button></form></main><script>const c=document.querySelector('meta').content,m=document.querySelector('#m'),q=document.querySelector('#q'),f=document.querySelector('form');function add(t,k){let d=document.createElement('div');d.className=k;d.textContent=t;m.append(d)}f.onsubmit=async e=>{e.preventDefault();let v=q.value.trim();if(!v)return;add(v,'u');q.value='';try{let r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Home-Butler-CSRF':c},body:JSON.stringify({message:v})}),d=await r.json();add(r.ok?d.answer:d.error,'a')}catch(_){add('Нет связи с локальным контуром.','a')}};</script></html>"""


class LocalChatError(RuntimeError):
    """Secret-free transport rejection."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LocalChatError("duplicate JSON key")
        result[key] = value
    return result


def _document(raw: bytes, field: str, maximum: int) -> str:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise LocalChatError("invalid request")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LocalChatError("invalid request") from error
    if not isinstance(value, dict) or set(value) != {field}:
        raise LocalChatError("invalid request")
    text = value.get(field)
    if not isinstance(text, str) or not text.strip() or len(text) > maximum:
        raise LocalChatError("invalid request")
    return text.strip()


def parse_message(raw: bytes) -> str:
    return _document(raw, "message", MAX_MESSAGE_CHARS)


def parse_access_key(raw: bytes) -> str:
    value = _document(raw, "key", 128)
    if len(value) < 20:
        raise LocalChatError("invalid request")
    return value


def load_allowed_networks() -> tuple[ipaddress.IPv4Network, ...]:
    try:
        values = tuple(
            ipaddress.ip_network(item.strip(), strict=True)
            for item in os.environ.get("HOME_BUTLER_LOCAL_CHAT_ALLOWED_NETWORKS", "127.0.0.0/8").split(",")
            if item.strip()
        )
    except ValueError as error:
        raise LocalChatError("invalid networks") from error
    if not values or any(value.version != 4 for value in values):
        raise LocalChatError("invalid networks")
    return values


def address_allowed(address: str, networks: tuple[ipaddress.IPv4Network, ...]) -> bool:
    try:
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    return candidate.version == 4 and any(candidate in network for network in networks)


def load_lan_access_key(*, required: bool) -> str | None:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    path = Path(directory) / "local-chat-lan.key" if directory else None
    if path is None or not path.is_file():
        if required:
            raise LocalChatError("LAN key unavailable")
        return None
    key = path.read_text(encoding="utf-8").strip()
    if not 20 <= len(key) <= 128 or any(char.isspace() for char in key):
        raise LocalChatError("invalid LAN key")
    return key


@dataclass
class Session:
    touched_at: float
    context: dict[str, Any]
    history: list[dict[str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class ChatApplication:
    def __init__(
        self,
        *,
        answerer: Callable[[str, dict[str, Any], list[dict[str, str]]], str] = owner_chat.answer_natural,
        context_factory: Callable[[], dict[str, Any]] = owner_chat.startup_context,
        clock: Callable[[], float] = time.monotonic,
        lan_access_key: str | None = None,
    ) -> None:
        self.answerer, self.context_factory, self.clock = answerer, context_factory, clock
        self.lan_access_key = lan_access_key
        self.csrf_token = secrets.token_urlsafe(32)
        self.sessions: dict[str, Session] = {}
        self.sessions_lock = threading.Lock()

    def session(self, session_id: str) -> Session:
        if SESSION_RE.fullmatch(session_id) is None:
            raise LocalChatError("invalid session")
        now = self.clock()
        with self.sessions_lock:
            self.sessions = {key: value for key, value in self.sessions.items() if now - value.touched_at <= SESSION_TTL_SECONDS}
            if session_id not in self.sessions:
                if len(self.sessions) >= MAX_SESSIONS:
                    del self.sessions[min(self.sessions, key=lambda key: self.sessions[key].touched_at)]
                self.sessions[session_id] = Session(now, self.context_factory())
            return self.sessions[session_id]

    def answer(self, session_id: str, question: str) -> str:
        record = self.session(session_id)
        with record.lock:
            response = self.answerer(question, {**record.context, "transport": "local_chat"}, list(record.history))
            if not isinstance(response, str) or not response.strip():
                raise LocalChatError("empty response")
            record.history.extend(({"role": "user", "content": question}, {"role": "assistant", "content": response}))
            record.history = record.history[-owner_chat.MAX_HISTORY_MESSAGES:]
            record.touched_at = self.clock()
            return response.strip()

    def verify_access_key(self, supplied: str) -> bool:
        return self.lan_access_key is not None and hmac.compare_digest(supplied, self.lan_access_key)

    def signature(self, session_id: str) -> str:
        if self.lan_access_key is None:
            raise LocalChatError("LAN disabled")
        digest = hmac.new(self.lan_access_key.encode(), session_id.encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class Handler(BaseHTTPRequestHandler):
    application: ChatApplication
    allowed_hosts: set[str]
    allowed_networks: tuple[ipaddress.IPv4Network, ...]
    requires_auth: bool

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _session(self) -> tuple[str, bool]:
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        value = jar.get(SESSION_COOKIE)
        if value is not None and SESSION_RE.fullmatch(value.value):
            return value.value, False
        return secrets.token_urlsafe(32), True

    def _authorized(self, session: str) -> bool:
        if not self.requires_auth:
            return True
        supplied = cookies.SimpleCookie(self.headers.get("Cookie", "")).get(AUTH_COOKIE)
        return supplied is not None and hmac.compare_digest(supplied.value, self.application.signature(session))

    def _allowed(self) -> bool:
        return self.headers.get("Host", "").casefold() in self.allowed_hosts and address_allowed(self.client_address[0], self.allowed_networks)

    def _send(self, status: int, body: bytes, content_type: str, cookies_out: list[str] = []) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        for value in cookies_out:
            self.send_header("Set-Cookie", value)
        self.end_headers(); self.wfile.write(body)

    def _json(self, status: int, document: dict[str, Any], cookies_out: list[str] = []) -> None:
        self._send(status, json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(), "application/json; charset=utf-8", cookies_out)

    def do_GET(self) -> None:  # noqa: N802
        if not self._allowed() or self.path != "/":
            self.send_error(404); return
        session, created = self._session()
        page = HTML if self._authorized(session) else LOGIN_HTML
        outgoing = [f"{SESSION_COOKIE}={session}; HttpOnly; SameSite=Strict; Path=/"] if created else []
        self._send(200, page.replace("__CSRF__", self.application.csrf_token).encode(), "text/html; charset=utf-8", outgoing)

    def do_POST(self) -> None:  # noqa: N802
        if not self._allowed() or self.path not in {"/api/chat", "/api/login"}:
            self.send_error(404); return
        if not hmac.compare_digest(self.headers.get("X-Home-Butler-CSRF", ""), self.application.csrf_token):
            self._json(403, {"error": "Сессия устарела."}); return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._json(413, {"error": "Неверный размер запроса."}); return
        session, created = self._session()
        outgoing = [f"{SESSION_COOKIE}={session}; HttpOnly; SameSite=Strict; Path=/"] if created else []
        if self.path == "/api/login":
            try:
                key = parse_access_key(self.rfile.read(length))
            except LocalChatError:
                self._json(403, {"error": "Ключ не принят."}); return
            if not self.application.verify_access_key(key):
                self._json(403, {"error": "Ключ не принят."}); return
            outgoing.append(f"{AUTH_COOKIE}={self.application.signature(session)}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000")
            self._json(200, {"ok": True}, outgoing); return
        if not self._authorized(session):
            self._json(401, {"error": "Требуется ключ владельца."}); return
        try:
            answer = self.application.answer(session, parse_message(self.rfile.read(length)))
        except (LocalChatError, owner_chat.OwnerChatError, OSError):
            self._json(503, {"error": "Ответ не завершён. Повторите фразу."}, outgoing); return
        self._json(200, {"answer": answer}, outgoing)


def _handler(name: str, app: ChatApplication, hosts: set[str], networks: tuple[ipaddress.IPv4Network, ...], auth: bool) -> type[Handler]:
    return type(name, (Handler,), {"application": app, "allowed_hosts": hosts, "allowed_networks": networks, "requires_auth": auth})


def main() -> int:
    port = int(os.environ.get("HOME_BUTLER_LOCAL_CHAT_PORT", str(DEFAULT_PORT)))
    bind = os.environ.get("HOME_BUTLER_LOCAL_CHAT_BIND_HOST", "127.0.0.1")
    networks = load_allowed_networks()
    lan_port_raw = os.environ.get("HOME_BUTLER_LAN_CHAT_BACKEND_PORT", "").strip()
    lan_port = int(lan_port_raw) if lan_port_raw else None
    app = ChatApplication(lan_access_key=load_lan_access_key(required=lan_port is not None))
    hosts = {item.strip().casefold() for item in os.environ.get("HOME_BUTLER_LOCAL_CHAT_ALLOWED_HOSTS", f"127.0.0.1:{port},localhost:{port}").split(",") if item.strip()}
    server = ThreadingHTTPServer((bind, port), _handler("LocalHandler", app, hosts, networks, False))
    lan_server: ThreadingHTTPServer | None = None
    if lan_port is not None:
        lan_hosts = {item.strip().casefold() for item in os.environ.get("HOME_BUTLER_LAN_CHAT_ALLOWED_HOSTS", "").split(",") if item.strip()}
        lan_server = ThreadingHTTPServer((bind, lan_port), _handler("LanHandler", app, lan_hosts, networks, True))
        threading.Thread(target=lan_server.serve_forever, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if lan_server is not None:
            lan_server.shutdown(); lan_server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
