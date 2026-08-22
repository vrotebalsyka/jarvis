#!/usr/bin/env python3
"""Protected browser chat backed by the same owner_chat engine as Alice."""

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
import incident_status  # noqa: E402
import startup_self_check  # noqa: E402
import qualification_status  # noqa: E402


DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8780
MAX_REQUEST_BYTES = 16 * 1024
MAX_MESSAGE_CHARS = 2_000
MAX_SESSIONS = 16
MAX_VISIBLE_ALERTS = 8
MAX_OPERATIONAL_ALERT_AGE_SECONDS = 24 * 60 * 60
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_COOKIE = "home_butler_session"
LAN_AUTH_COOKIE = "home_butler_lan_auth"
SESSION_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")

LOGIN_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><meta name="home-butler-csrf" content="__CSRF_TOKEN__"><title>Вход — Домашний дворецкий</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1118;color:#eef4f2;font-family:"Segoe UI",sans-serif}.card{width:min(430px,calc(100% - 32px));padding:28px;border:1px solid #294350;border-radius:22px;background:#111c26;box-shadow:0 24px 80px #0007}h1{margin:0 0 10px;font-size:30px;font-weight:500}p{color:#a6b7bd;line-height:1.5}form{display:grid;gap:12px;margin-top:24px}input,button{height:50px;border-radius:13px;font:inherit}input{border:1px solid #294350;background:#0d161e;color:#eef4f2;padding:0 14px;outline:none}input:focus{border-color:#63d4df}button{border:0;background:#63d4df;color:#071317;font-weight:700;cursor:pointer}.error{color:#ffad9e;min-height:22px;margin:0}
</style></head><body><main class="card"><h1>Домашний дворецкий</h1><p>Сетевой доступ защищён. Введите ключ владельца один раз — браузер запомнит это устройство.</p><form id="login"><input id="key" type="password" autocomplete="current-password" maxlength="128" placeholder="Ключ владельца" autofocus><button>Войти</button><p id="error" class="error"></p></form></main><script>
const csrf=document.querySelector('meta[name="home-butler-csrf"]').content,form=document.querySelector('#login'),key=document.querySelector('#key'),error=document.querySelector('#error');form.addEventListener('submit',async e=>{e.preventDefault();error.textContent='';try{const r=await fetch('/api/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Home-Butler-CSRF':csrf},body:JSON.stringify({key:key.value})});if(!r.ok)throw new Error('Ключ не принят');location.reload()}catch(e){error.textContent=e.message}});
</script></body></html>"""

HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <meta name="home-butler-csrf" content="__CSRF_TOKEN__">
  <title>Домашний дворецкий</title>
  <style>
    :root{--night:#0b1118;--panel:#111c26;--steel:#1b2d3a;--line:#294350;--ink:#eef4f2;--muted:#91a6ae;--pulse:#63d4df;--warm:#ffc56e;--danger:#ff8d78}
    *{box-sizing:border-box}html,body{height:100%}body{margin:0;background:radial-gradient(circle at 18% 0,#152a37 0,transparent 34%),var(--night);color:var(--ink);font-family:Aptos,"Segoe UI",sans-serif}
    .shell{min-height:100%;display:grid;grid-template-columns:15px minmax(0,880px);justify-content:center;padding:clamp(18px,4vw,56px)}
    .rail{position:relative;background:var(--steel);border:1px solid var(--line);border-radius:999px;margin-right:22px;overflow:hidden}.rail:after{content:"";position:absolute;left:3px;right:3px;top:12%;height:18%;border-radius:999px;background:var(--pulse);box-shadow:0 0 24px rgba(99,212,223,.7);animation:breathe 3.2s ease-in-out infinite}
    .app{min-width:0;display:grid;grid-template-rows:auto minmax(320px,1fr) auto;background:rgba(17,28,38,.92);border:1px solid var(--line);border-radius:24px;box-shadow:0 24px 80px rgba(0,0,0,.34);overflow:hidden}
    header{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;padding:24px 28px;border-bottom:1px solid var(--line)}
    .eyebrow{font:600 11px/1.2 "Cascadia Mono",monospace;letter-spacing:.16em;text-transform:uppercase;color:var(--pulse)}h1{margin:6px 0 0;font-family:"Bahnschrift SemiCondensed","Segoe UI",sans-serif;font-size:clamp(28px,5vw,46px);font-weight:500;letter-spacing:-.035em}
    .runtime{display:grid;justify-items:end;gap:7px}.status{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px;white-space:nowrap}.proof{max-width:430px;color:var(--muted);font-size:12px;line-height:1.35;text-align:right}.dot{width:8px;height:8px;border-radius:50%;background:var(--warm);box-shadow:0 0 12px rgba(255,197,110,.5)}.ready .dot{background:var(--pulse)}
    .alerts{padding:12px 28px;background:#2a2016;border-bottom:1px solid #624a28;color:#ffe0a7;font-size:13px;line-height:1.45}.alerts strong{color:var(--warm)}.alerts[hidden]{display:none}
    .qualification{padding:14px 28px 16px;border-bottom:1px solid var(--line);background:rgba(11,17,24,.62)}.qualification-title{display:flex;justify-content:space-between;gap:16px;margin-bottom:10px;color:var(--muted);font:600 11px/1.2 "Cascadia Mono",monospace;letter-spacing:.08em;text-transform:uppercase}.proof-rail{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:0;list-style:none;padding:0;margin:0}.proof-step{position:relative;padding:0 12px 0 18px;color:var(--muted);font-size:12px;line-height:1.3}.proof-step:before{content:"";position:absolute;left:0;top:3px;width:9px;height:9px;border:2px solid var(--line);border-radius:50%;background:var(--night);z-index:1}.proof-step:after{content:"";position:absolute;left:10px;right:-1px;top:8px;height:1px;background:var(--line)}.proof-step:last-child:after{display:none}.proof-step strong{display:block;margin-top:14px;color:var(--ink);font-weight:600}.proof-step.passed:before{border-color:var(--pulse);background:var(--pulse);box-shadow:0 0 12px rgba(99,212,223,.55)}.proof-step.active:before{border-color:var(--warm);background:var(--warm)}.proof-step.problem:before{border-color:var(--danger);background:var(--danger)}
    #messages{overflow:auto;padding:26px 28px;display:flex;flex-direction:column;gap:16px;scrollbar-color:var(--line) transparent}
    .message{max-width:min(78%,680px);padding:13px 16px;border-radius:16px;line-height:1.48;white-space:pre-wrap;overflow-wrap:anywhere}.assistant{align-self:flex-start;background:var(--steel);border:1px solid var(--line);border-bottom-left-radius:5px}.user{align-self:flex-end;background:#d8e9e8;color:#0c171b;border-bottom-right-radius:5px}.error{align-self:flex-start;background:#39231f;border:1px solid #70443a;color:#ffd9d1}
    form{display:grid;grid-template-columns:1fr auto;gap:12px;padding:18px 20px 20px;border-top:1px solid var(--line);background:rgba(11,17,24,.72)}textarea{resize:none;min-height:52px;max-height:160px;border:1px solid var(--line);border-radius:14px;background:#0d161e;color:var(--ink);padding:14px 15px;font:inherit;line-height:1.35;outline:none}textarea:focus{border-color:var(--pulse);box-shadow:0 0 0 3px rgba(99,212,223,.13)}button{align-self:stretch;border:0;border-radius:14px;padding:0 20px;background:var(--pulse);color:#071317;font:700 14px/1 "Cascadia Mono",monospace;cursor:pointer}button:hover{filter:brightness(1.08)}button:disabled{cursor:wait;opacity:.55}
    .hint{grid-column:1/-1;color:var(--muted);font-size:12px;margin:0 2px}.hint code{font-family:"Cascadia Mono",monospace;color:#bed0d5}.mode{display:inline-flex;align-items:center;gap:7px;margin-right:14px;color:var(--ink)}.mode input{accent-color:var(--pulse)}
    :focus-visible{outline:2px solid var(--warm);outline-offset:3px}@keyframes breathe{50%{transform:translateY(250%);opacity:.48}}
    @media(max-width:640px){.shell{grid-template-columns:7px minmax(0,1fr);padding:10px}.rail{margin-right:9px}.app{border-radius:16px}header,#messages{padding-left:17px;padding-right:17px}header{align-items:flex-start;flex-direction:column}.runtime{justify-items:start}.proof{text-align:left}.qualification{padding-left:17px;padding-right:17px}.proof-rail{grid-template-columns:1fr;gap:10px}.proof-step{min-height:38px}.proof-step:after{left:4px;right:auto;top:12px;bottom:-12px;width:1px;height:auto}.proof-step strong{margin-top:0}.message{max-width:91%}form{grid-template-columns:1fr}button{height:48px}}
    @media(prefers-reduced-motion:reduce){.rail:after{animation:none}}
  </style>
</head>
<body>
  <main class="shell">
    <div class="rail" aria-hidden="true"></div>
    <section class="app" aria-label="Диалог с Домашним дворецким">
      <header><div><div class="eyebrow">Локальный контур</div><h1>Домашний дворецкий</h1></div><div class="runtime"><div id="status" class="status"><span class="dot"></span><span>Проверяю связь</span></div><div id="proof" class="proof">Самопроверка запуска ещё не завершена</div></div></header>
      <div id="alerts" class="alerts" role="status" aria-live="polite" hidden></div>
      <section class="qualification" aria-label="Доказательные испытания"><div class="qualification-title"><span>Контрольная лента</span><span id="qualification-summary">Испытания ожидаются</span></div><ol id="proof-rail" class="proof-rail"></ol></section>
      <div id="messages" role="log" aria-live="polite"><div class="message assistant">Я на связи. Говорите как обычно — технические имена устройств не нужны.</div></div>
      <form id="chat"><textarea id="input" maxlength="2000" rows="2" aria-label="Сообщение" placeholder="Дайте модели вопрос или задачу"></textarea><button id="send" type="submit">Отправить</button><p class="hint"><label class="mode"><input id="direct-mode" type="checkbox" checked> Свободный ИИ без шаблонных ответов</label>Enter — отправить, Shift+Enter — новая строка. Для точного штатного отчёта HA снимите флажок.</p></form>
    </section>
  </main>
  <script>
    const csrf=document.querySelector('meta[name="home-butler-csrf"]').content,input=document.querySelector('#input'),directMode=document.querySelector('#direct-mode'),form=document.querySelector('#chat'),send=document.querySelector('#send'),messages=document.querySelector('#messages'),statusNode=document.querySelector('#status'),proofNode=document.querySelector('#proof'),alertsNode=document.querySelector('#alerts'),proofRail=document.querySelector('#proof-rail'),qualificationSummary=document.querySelector('#qualification-summary');
    function add(text,kind){const n=document.createElement('div');n.className='message '+kind;n.textContent=text;messages.appendChild(n);messages.scrollTop=messages.scrollHeight}
    function appendProof(label,detail,state){const item=document.createElement('li'),strong=document.createElement('strong'),text=document.createElement('span');item.className='proof-step '+state;strong.textContent=label;text.textContent=detail;item.append(strong,text);proofRail.appendChild(item)}
    function refresh(){fetch('/api/status',{credentials:'same-origin'}).then(r=>r.ok?r.json():Promise.reject()).then(data=>{const proof=data.self_check||{};statusNode.classList.toggle('ready',proof.ready===true);statusNode.lastElementChild.textContent=proof.ready===true?'Контур работает':'Самопроверка не завершена';proofNode.textContent=proof.ready===true?'HA на связи · наблюдение включено · модель на '+(proof.accelerator==='gpu'?'GPU':'CPU fallback')+' · уведомления готовы · Алиса готова':'Жду подтверждения HA, модели, наблюдения, уведомлений и Алисы';const items=Array.isArray(data.alerts)?data.alerts:[];alertsNode.hidden=!items.length;alertsNode.textContent='';if(items.length){const title=document.createElement('strong');title.textContent='Требует внимания: ';alertsNode.append(title,document.createTextNode(items.map(x=>x.name).join(', ')))}const qualification=data.qualification||{},devices=Array.isArray(qualification.devices)?qualification.devices:[];proofRail.textContent='';appendProof('Перезагрузки Windows',(qualification.verified_reboots||0)+' из '+(qualification.required_reboots||3),qualification.reboots_passed?'passed':qualification.verified_reboots>0?'active':'');devices.forEach(device=>{const state=device.state||'pending',detail=state==='passed'?'Тревога и восстановление подтверждены':state==='waiting_recovery'?'Тревога получена, ждём включения':state==='waiting_recovery_notice'?'Устройство вернулось, ждём сообщение':state==='delivery_problem'?'Колонка не приняла тревогу':state==='waiting_alert'||state==='confirming'?'Проверяю отключение':'Ожидает испытания';appendProof(device.label||'Устройство',detail,state==='passed'?'passed':state==='delivery_problem'?'problem':state==='pending'?'':'active')});const dialogue=qualification.dialogue||{},dialoguePassed=dialogue.state==='passed';appendProof('Свободный диалог',dialoguePassed?'Локальный чат и Алиса отвечают с памятью':'Ожидает проверки модели',dialoguePassed?'passed':'active');qualificationSummary.textContent=qualification.qualification_complete?'Все проверки пройдены':qualification.dialogue_proof_complete?'Диалог подтверждён, аппаратные тесты продолжаются':'Следующий шаг фиксируется автоматически'}).catch(()=>{statusNode.classList.remove('ready');statusNode.lastElementChild.textContent='Нет связи';proofNode.textContent='Самопроверка недоступна'})}
    refresh();setInterval(refresh,15000);
    form.addEventListener('submit',async e=>{e.preventDefault();const message=input.value.trim();if(!message||send.disabled)return;add(message,'user');input.value='';send.disabled=true;statusNode.lastElementChild.textContent='Думаю';try{const routedMessage=directMode.checked?'/модель '+message:message;const r=await fetch('/api/chat',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Home-Butler-CSRF':csrf},body:JSON.stringify({message:routedMessage})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Контур не ответил');add(data.answer,'assistant');statusNode.classList.add('ready');statusNode.lastElementChild.textContent='Контур работает'}catch(error){add(error.message,'error');statusNode.classList.remove('ready');statusNode.lastElementChild.textContent='Ошибка ответа'}finally{send.disabled=false;input.focus()}});
    input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();form.requestSubmit()}});input.focus();
  </script>
</body>
</html>"""


class LocalChatError(RuntimeError):
    """A secret-free local chat failure."""


def local_status() -> dict[str, Any]:
    """Return a small, sanitized alert digest for the loopback UI."""
    try:
        self_check_document = startup_self_check.read_status(
            current_boot_id=startup_self_check.read_boot_id()
        )
        self_check = {
            "ready": True,
            "accelerator": str(self_check_document["accelerator"]),
            "home_assistant": bool(self_check_document["home_assistant_ready"]),
            "observer": bool(self_check_document["observer_ready"]),
            "notifications": bool(self_check_document["notifications_ready"]),
            "alice_local": bool(self_check_document["alice_local_ready"]),
        }
    except startup_self_check.SelfCheckError:
        self_check = {"ready": False}
    try:
        summary = incident_status.read_summary()
    except incident_status.IncidentStatusError:
        return {
            "ready": True,
            "monitor": "unavailable",
            "alerts": [],
            "self_check": self_check,
            "qualification": {
                "verified_reboots": 0,
                "required_reboots": 3,
                "reboots_passed": False,
                "devices": [],
                "hardware_proof_complete": False,
                "dialogue": {"state": "pending"},
                "dialogue_proof_complete": False,
                "qualification_complete": False,
            },
        }
    try:
        qualification = qualification_status.read_status()
    except (qualification_status.QualificationError, incident_status.IncidentStatusError):
        qualification = {
            "verified_reboots": 0,
            "required_reboots": 3,
            "reboots_passed": False,
            "devices": [],
            "hardware_proof_complete": False,
            "dialogue": {"state": "pending"},
            "dialogue_proof_complete": False,
            "qualification_complete": False,
        }
    alerts: list[dict[str, str]] = []
    seen: set[str] = set()
    covered_entity_subjects: set[str] = set()
    policy_epoch = summary.get("device_notification_enabled_epoch")
    if not isinstance(policy_epoch, int) or policy_epoch < 0:
        policy_epoch = int(time.time())
    groups = (
        (summary.get("device_incidents"), "device", "display_name"),
        (summary.get("operational_incidents"), "integration", "display_name"),
        (summary.get("incidents"), "entity", "subject"),
    )
    device_values = summary.get("device_incidents")
    if isinstance(device_values, list):
        for item in device_values:
            if not isinstance(item, dict):
                continue
            member_subjects = item.get("member_subjects")
            if isinstance(member_subjects, list):
                covered_entity_subjects.update(
                    value for value in member_subjects if isinstance(value, str)
                )
    for values, kind, name_key in groups:
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict) or item.get("baseline") is True:
                continue
            first_observed_epoch = item.get("first_observed_epoch")
            if kind in {"device", "entity"} and (
                not isinstance(first_observed_epoch, int)
                or first_observed_epoch < policy_epoch
            ):
                continue
            last_observed_epoch = item.get("last_observed_epoch")
            if kind == "integration" and (
                not isinstance(last_observed_epoch, int)
                or last_observed_epoch
                < int(time.time()) - MAX_OPERATIONAL_ALERT_AGE_SECONDS
            ):
                continue
            name = item.get(name_key)
            status = item.get("status")
            if kind == "entity" and name in covered_entity_subjects:
                continue
            if (
                not isinstance(name, str)
                or not name.strip()
                or status not in {"observed", "confirmed", "escalated"}
            ):
                continue
            normalized = " ".join(name.split())[:100]
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            alerts.append({"name": normalized, "status": str(status), "kind": kind})
            if len(alerts) >= MAX_VISIBLE_ALERTS:
                break
        if len(alerts) >= MAX_VISIBLE_ALERTS:
            break
    return {
        "ready": True,
        "monitor": "ready",
        "alerts": alerts,
        "self_check": self_check,
        "qualification": qualification,
    }


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LocalChatError("duplicate JSON key")
        result[key] = value
    return result


def parse_message(raw: bytes) -> str:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise LocalChatError("invalid request size")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalChatError("invalid JSON") from error
    if not isinstance(document, dict) or set(document) != {"message"}:
        raise LocalChatError("invalid request shape")
    message = document.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
        raise LocalChatError("invalid message")
    return message.strip()


def parse_access_key(raw: bytes) -> str:
    if not raw or len(raw) > 512:
        raise LocalChatError("invalid access key request")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalChatError("invalid access key JSON") from error
    if not isinstance(document, dict) or set(document) != {"key"}:
        raise LocalChatError("invalid access key shape")
    key = document.get("key")
    if not isinstance(key, str) or not 20 <= len(key) <= 128:
        raise LocalChatError("invalid access key")
    return key


def load_bind_host() -> str:
    raw = os.environ.get("HOME_BUTLER_LOCAL_CHAT_BIND_HOST", DEFAULT_BIND_HOST).strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as error:
        raise LocalChatError("invalid local chat bind host") from error
    if address.version != 4 or (not address.is_loopback and not address.is_unspecified):
        raise LocalChatError("local chat may bind only to loopback or all IPv4 interfaces")
    return raw


def load_allowed_hosts(port: int) -> set[str]:
    default = f"127.0.0.1:{port},localhost:{port}"
    values = os.environ.get("HOME_BUTLER_LOCAL_CHAT_ALLOWED_HOSTS", default).split(",")
    hosts = {value.strip().casefold() for value in values if value.strip()}
    if not hosts or any("/" in value or "://" in value for value in hosts):
        raise LocalChatError("invalid local chat allowed hosts")
    return hosts


def load_allowed_networks() -> tuple[ipaddress.IPv4Network, ...]:
    raw = os.environ.get("HOME_BUTLER_LOCAL_CHAT_ALLOWED_NETWORKS", "127.0.0.0/8")
    try:
        networks = tuple(
            ipaddress.ip_network(value.strip(), strict=True)
            for value in raw.split(",") if value.strip()
        )
    except ValueError as error:
        raise LocalChatError("invalid local chat allowed networks") from error
    if not networks or any(network.version != 4 for network in networks):
        raise LocalChatError("invalid local chat allowed networks")
    return networks


def address_allowed(address: str, networks: tuple[ipaddress.IPv4Network, ...]) -> bool:
    try:
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    return candidate.version == 4 and any(candidate in network for network in networks)


def load_lan_access_key(*, required: bool) -> str | None:
    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    path = Path(credentials_dir) / "local-chat-lan.key" if credentials_dir else None
    if path is None or not path.is_file():
        if required:
            raise LocalChatError("LAN access key is unavailable")
        return None
    key = path.read_text(encoding="utf-8").strip()
    if not 20 <= len(key) <= 128 or any(character.isspace() for character in key):
        raise LocalChatError("invalid LAN access key")
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
        answerer: Callable[[str, dict[str, Any], list[dict[str, str]]], str] = owner_chat.answer,
        context_factory: Callable[[], dict[str, Any]] = owner_chat.startup_context,
        clock: Callable[[], float] = time.monotonic,
        lan_access_key: str | None = None,
    ) -> None:
        self.answerer = answerer
        self.context_factory = context_factory
        self.clock = clock
        self.lan_access_key = lan_access_key
        self.csrf_token = secrets.token_urlsafe(32)
        self.sessions: dict[str, Session] = {}
        self.sessions_lock = threading.Lock()

    def session(self, session_id: str) -> Session:
        if not SESSION_RE.fullmatch(session_id):
            raise LocalChatError("invalid session")
        now = self.clock()
        with self.sessions_lock:
            expired = [
                key for key, value in self.sessions.items()
                if now - value.touched_at > SESSION_TTL_SECONDS
            ]
            for key in expired:
                del self.sessions[key]
            if session_id not in self.sessions:
                if len(self.sessions) >= MAX_SESSIONS:
                    oldest = min(self.sessions, key=lambda key: self.sessions[key].touched_at)
                    del self.sessions[oldest]
                self.sessions[session_id] = Session(now, self.context_factory())
            record = self.sessions[session_id]
            record.touched_at = now
            return record

    def answer(self, session_id: str, question: str) -> str:
        record = self.session(session_id)
        with record.lock:
            response = self.answerer(question, record.context, list(record.history))
            if not isinstance(response, str) or not response.strip():
                raise LocalChatError("empty model response")
            record.history.extend(
                (
                    {"role": "user", "content": question[:MAX_MESSAGE_CHARS]},
                    {"role": "assistant", "content": response[:12_000]},
                )
            )
            record.history = record.history[-owner_chat.MAX_HISTORY_MESSAGES:]
            record.touched_at = self.clock()
            return response.strip()

    def verify_access_key(self, supplied: str) -> bool:
        return self.lan_access_key is not None and hmac.compare_digest(
            supplied, self.lan_access_key
        )

    def lan_signature(self, session_id: str) -> str:
        if self.lan_access_key is None:
            raise LocalChatError("LAN access is disabled")
        digest = hmac.new(
            self.lan_access_key.encode("utf-8"),
            session_id.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class LocalChatHandler(BaseHTTPRequestHandler):
    server_version = "HomeButlerLocal/1"
    application: ChatApplication
    allowed_hosts: set[str]
    allowed_networks: tuple[ipaddress.IPv4Network, ...]
    requires_lan_auth: bool

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _host_allowed(self) -> bool:
        return self.headers.get("Host", "").casefold() in self.allowed_hosts

    def _client_allowed(self) -> bool:
        return address_allowed(self.client_address[0], self.allowed_networks)

    def _loopback_client(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _session_id(self) -> tuple[str, bool]:
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            jar = cookies.SimpleCookie()
        morsel = jar.get(SESSION_COOKIE)
        if morsel is not None and SESSION_RE.fullmatch(morsel.value):
            return morsel.value, False
        return secrets.token_urlsafe(32), True

    def _authorized(self, session_id: str) -> bool:
        if not self.requires_lan_auth:
            return True
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return False
        supplied = jar.get(LAN_AUTH_COOKIE)
        if supplied is None:
            return False
        try:
            expected = self.application.lan_signature(session_id)
        except LocalChatError:
            return False
        return hmac.compare_digest(supplied.value, expected)

    def _json(
        self,
        status: int,
        document: dict[str, Any],
        *,
        session_id: str | None = None,
        lan_signature: str | None = None,
    ) -> None:
        body = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if session_id is not None:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/",
            )
        if lan_signature is not None:
            self.send_header(
                "Set-Cookie",
                f"{LAN_AUTH_COOKIE}={lan_signature}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000",
            )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self._client_allowed() or not self._host_allowed():
            self.send_error(421)
            return
        session_id, created = self._session_id()
        if self.path == "/":
            template = HTML if self._authorized(session_id) else LOGIN_HTML
            body = template.replace("__CSRF_TOKEN__", self.application.csrf_token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            if created:
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/",
                )
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/status":
            if not self._authorized(session_id):
                self._json(401, {"error": "Требуется ключ владельца."})
                return
            self._json(200, local_status(), session_id=session_id if created else None)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if (
            not self._client_allowed()
            or not self._host_allowed()
            or self.path not in {"/api/chat", "/api/login"}
        ):
            self.send_error(404)
            return
        expected_origin = {f"http://{host}" for host in self.allowed_hosts}
        if self.headers.get("Origin", "").casefold() not in expected_origin:
            self._json(403, {"error": "Запрос отклонён локальной защитой."})
            return
        supplied = self.headers.get("X-Home-Butler-CSRF", "")
        if not hmac.compare_digest(supplied, self.application.csrf_token):
            self._json(403, {"error": "Сессия устарела. Обновите страницу."})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Неверный формат запроса."})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        maximum = 512 if self.path == "/api/login" else MAX_REQUEST_BYTES
        if length < 1 or length > maximum:
            self._json(413, {"error": "Сообщение слишком большое."})
            return
        session_id, created = self._session_id()
        if self.path == "/api/login":
            try:
                supplied_key = parse_access_key(self.rfile.read(length))
            except LocalChatError:
                self._json(403, {"error": "Ключ не принят."})
                return
            if not self.application.verify_access_key(supplied_key):
                time.sleep(0.5)
                self._json(403, {"error": "Ключ не принят."})
                return
            self._json(
                200,
                {"ok": True},
                session_id=session_id if created else None,
                lan_signature=self.application.lan_signature(session_id),
            )
            return
        if not self._authorized(session_id):
            self._json(401, {"error": "Требуется ключ владельца."})
            return
        try:
            question = parse_message(self.rfile.read(length))
            answer = self.application.answer(session_id, question)
        except (LocalChatError, owner_chat.OwnerChatError, OSError) as error:
            error_code = (
                "local_chat_rejected"
                if isinstance(error, LocalChatError)
                else "owner_chat_failed"
                if isinstance(error, owner_chat.OwnerChatError)
                else "local_io_failed"
            )
            print(
                json.dumps(
                    {
                        "component": "local_chat_gateway",
                        "error_code": error_code,
                        "event": "turn_failed",
                        "route": owner_chat.classify_request(question)
                        if "question" in locals()
                        else "unknown",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                flush=True,
            )
            self._json(
                503,
                {"error": "Локальная модель не завершила ответ. Повторите фразу."},
                session_id=session_id if created else None,
            )
            return
        self._json(200, {"answer": answer}, session_id=session_id if created else None)


def load_port() -> int:
    raw = os.environ.get("HOME_BUTLER_LOCAL_CHAT_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw)
    except ValueError as error:
        raise LocalChatError("invalid local chat port") from error
    if not 1024 <= port <= 65535:
        raise LocalChatError("invalid local chat port")
    return port


def load_lan_backend_port() -> int | None:
    raw = os.environ.get("HOME_BUTLER_LAN_CHAT_BACKEND_PORT", "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError as error:
        raise LocalChatError("invalid LAN chat backend port") from error
    if not 1024 <= port <= 65535:
        raise LocalChatError("invalid LAN chat backend port")
    return port


def _configured_handler(
    name: str,
    *,
    application: ChatApplication,
    allowed_hosts: set[str],
    allowed_networks: tuple[ipaddress.IPv4Network, ...],
    requires_lan_auth: bool,
) -> type[LocalChatHandler]:
    return type(
        name,
        (LocalChatHandler,),
        {
            "application": application,
            "allowed_hosts": allowed_hosts,
            "allowed_networks": allowed_networks,
            "requires_lan_auth": requires_lan_auth,
        },
    )


def main() -> int:
    port = load_port()
    bind_host = load_bind_host()
    allowed_hosts = load_allowed_hosts(port)
    allowed_networks = load_allowed_networks()
    lan_backend_port = load_lan_backend_port()
    lan_enabled = lan_backend_port is not None
    application = ChatApplication(
        lan_access_key=load_lan_access_key(required=lan_enabled)
    )
    handler = _configured_handler(
        "ConfiguredLocalChatHandler",
        application=application,
        allowed_hosts=allowed_hosts,
        allowed_networks=allowed_networks,
        requires_lan_auth=False,
    )
    server = ThreadingHTTPServer((bind_host, port), handler)
    server.daemon_threads = True
    lan_server: ThreadingHTTPServer | None = None
    lan_thread: threading.Thread | None = None
    if lan_backend_port is not None:
        lan_hosts_raw = os.environ.get("HOME_BUTLER_LAN_CHAT_ALLOWED_HOSTS", "")
        lan_hosts = {
            value.strip().casefold()
            for value in lan_hosts_raw.split(",")
            if value.strip()
        }
        if not lan_hosts:
            raise LocalChatError("LAN chat allowed hosts are unavailable")
        lan_handler = _configured_handler(
            "ConfiguredLanChatHandler",
            application=application,
            allowed_hosts=lan_hosts,
            allowed_networks=allowed_networks,
            requires_lan_auth=True,
        )
        lan_server = ThreadingHTTPServer((bind_host, lan_backend_port), lan_handler)
        lan_server.daemon_threads = True
        lan_thread = threading.Thread(
            target=lan_server.serve_forever,
            kwargs={"poll_interval": 0.5},
            name="home-butler-lan-chat",
            daemon=True,
        )
        lan_thread.start()
    print(
        f"home_butler_local_chat=ready host={bind_host} port={port} lan_auth={lan_enabled}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if lan_server is not None:
            lan_server.shutdown()
            lan_server.server_close()
        if lan_thread is not None:
            lan_thread.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
