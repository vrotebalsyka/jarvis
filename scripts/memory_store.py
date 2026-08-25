#!/usr/bin/env python3
"""Private, indexed and revocable memory for one verified Home Butler owner."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
PRIMARY_OWNER_SCOPE = "primary-owner"
DEFAULT_DB_PATH = Path(
    os.environ.get(
        "HOME_BUTLER_MEMORY_DB",
        "/home/homebutler/.local/state/home-butler/memory/memory.db",
    )
)
MAX_DB_BYTES = 512 * 1024 * 1024
MAX_TEXT_CHARS = 16_000
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_RECENT_TURNS = 200
MEMORY_TYPES = frozenset(
    {"owner", "device", "episodic", "procedural", "conversation_summary"}
)
MEMORY_STATUSES = frozenset({"active", "superseded", "revoked", "expired"})
GOAL_STATUSES = frozenset({"active", "blocked", "completed", "cancelled"})
DELIVERY_STATES = frozenset(
    {"not_required", "pending", "delivered", "delivery_unknown", "failed"}
)
ROLE_VALUES = frozenset({"user", "assistant"})
SCOPE_RE = re.compile(r"[a-z][a-z0-9_-]{2,63}\Z")
TRANSPORT_RE = re.compile(r"[a-z][a-z0-9_-]{1,31}\Z")
SESSION_RE = re.compile(r"[a-f0-9]{16,64}\Z")
TRACE_CODE_RE = re.compile(r"[A-Za-z0-9_.:/+-]{1,96}\Z")
WORD_RE = re.compile(r"[a-zа-яё0-9]{2,48}", re.IGNORECASE)
SECRET_RE = re.compile(
    r"(?:\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b|"
    r"Authorization\s*:\s*Bearer\s+\S+|"
    r"-----BEGIN\s+(?:OPENSSH|RSA|EC|PRIVATE)\s+PRIVATE\s+KEY-----|"
    r"\b(?:password|passwd|парол[ья])\s*[:=]\s*\S+|"
    r"\b(?:cookie|set-cookie)\s*:\s*\S+)",
    re.IGNORECASE,
)
DEVICE_WORD_RE = re.compile(
    r"\b(?:робот|пылесос|посудомойк\w*|увлажнител\w*|зеркал\w*|"
    r"кондиционер\w*|реле|датчик\w*|колонк\w*|станци\w*)\b",
    re.IGNORECASE,
)


class MemoryStoreError(RuntimeError):
    """The private memory store rejected unsafe or inconsistent state."""


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    memory_type: str
    owner_scope: str
    source_transport: str
    source: str
    confidence: float
    created_at: int
    updated_at: int
    valid_until: int | None
    supersedes: str | None
    status: str
    searchable_text: str
    structured_payload: dict[str, Any]
    memory_key: str | None


def _json(value: Any) -> str:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise MemoryStoreError("memory payload is not JSON-safe") from error
    if len(rendered.encode("utf-8")) > MAX_PAYLOAD_BYTES or SECRET_RE.search(rendered):
        raise MemoryStoreError("memory payload is unsafe")
    return rendered


def _text(value: object, *, field: str, maximum: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise MemoryStoreError(f"{field} is invalid")
    rendered = unicodedata.normalize("NFKC", value).strip()
    if (
        not rendered
        or len(rendered) > maximum
        or "\x00" in rendered
        or SECRET_RE.search(rendered)
        or any(ord(character) < 9 for character in rendered)
    ):
        raise MemoryStoreError(f"{field} is unsafe")
    return rendered


def _scope(value: object) -> str:
    rendered = _text(value, field="owner scope", maximum=64)
    if SCOPE_RE.fullmatch(rendered) is None:
        raise MemoryStoreError("owner scope is invalid")
    return rendered


def _transport(value: object) -> str:
    rendered = _text(value, field="transport", maximum=32)
    if TRANSPORT_RE.fullmatch(rendered) is None:
        raise MemoryStoreError("transport is invalid")
    return rendered


def _session(value: object) -> str:
    rendered = _text(value, field="session", maximum=64)
    if SESSION_RE.fullmatch(rendered) is None:
        raise MemoryStoreError("session is invalid")
    return rendered


def _trace_code(value: object, *, field: str) -> str:
    rendered = _text(value, field=field, maximum=96)
    if TRACE_CODE_RE.fullmatch(rendered) is None:
        raise MemoryStoreError(f"{field} is invalid")
    return rendered


def _safe_file(path: Path, expected_uid: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise MemoryStoreError("memory database is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size > MAX_DB_BYTES
    ):
        raise MemoryStoreError("memory database metadata is unsafe")


def _summary_values(
    raw_payload: object,
    user: str,
    assistant: str,
) -> tuple[dict[str, Any], str, str]:
    try:
        summary = json.loads(raw_payload) if isinstance(raw_payload, str) else {}
    except (json.JSONDecodeError, TypeError):
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    topics = [
        value for value in summary.get("recent_topics", [])
        if isinstance(value, str)
    ]
    topics.append(user[:280])
    devices = [
        value for value in summary.get("mentioned_devices", [])
        if isinstance(value, str)
    ]
    if DEVICE_WORD_RE.search(user):
        devices.append(user[:180])
    corrections = [
        value for value in summary.get("important_corrections", [])
        if isinstance(value, str)
    ]
    if re.search(r"\b(?:нет|исправ|называй|не\s+так)\b", user, re.IGNORECASE):
        corrections.append(user[:220])
    unresolved = [
        value for value in summary.get("unresolved_questions", [])
        if isinstance(value, str)
    ]
    if "?" in user and re.search(
        r"\b(?:не\s+заверш|не\s+удалось|заблокирован|blocked)\b",
        assistant,
        re.IGNORECASE,
    ):
        unresolved.append(user[:220])
    result = {
        "current_topic": user[:280],
        "recent_topics": list(dict.fromkeys(topics))[-6:],
        "mentioned_devices": list(dict.fromkeys(devices))[-8:],
        "unresolved_questions": list(dict.fromkeys(unresolved))[-6:],
        "important_corrections": list(dict.fromkeys(corrections))[-6:],
    }
    rendered = _json(result)
    summary_text = (
        f"Текущая тема: {result['current_topic']}. "
        f"Устройства: {'; '.join(result['mentioned_devices']) or 'не выделены'}. "
        f"Коррекции: {'; '.join(result['important_corrections']) or 'нет'}."
    )[:2_000]
    return result, summary_text, rendered


class MemoryStore:
    """SQLite FTS5 memory with explicit provenance, expiry and correction."""

    def __init__(
        self,
        path: Path = DEFAULT_DB_PATH,
        *,
        expected_uid: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.expected_uid = os.geteuid() if expected_uid is None else expected_uid
        self.clock = clock
        self._prepare_file()
        self._migrate()

    def _prepare_file(self) -> None:
        try:
            parent = self.path.parent.lstat()
        except OSError as error:
            raise MemoryStoreError("memory directory is unavailable") from error
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != self.expected_uid
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise MemoryStoreError("memory directory metadata is unsafe")
        if self.path.exists() or self.path.is_symlink():
            _safe_file(self.path, self.expected_uid)
            return
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise MemoryStoreError("memory database could not be created") from error
        os.close(descriptor)
        _safe_file(self.path, self.expected_uid)

    def _connect(self) -> sqlite3.Connection:
        _safe_file(self.path, self.expected_uid)
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            return connection
        except sqlite3.Error as error:
            raise MemoryStoreError("memory database connection failed") from error

    def _migrate(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS memory_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
            memory_id TEXT PRIMARY KEY,
            memory_type TEXT NOT NULL,
            owner_scope TEXT NOT NULL,
            source_transport TEXT NOT NULL,
            source_session TEXT,
            source TEXT NOT NULL,
            confidence REAL NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            valid_until INTEGER,
            supersedes TEXT REFERENCES memories(memory_id),
            status TEXT NOT NULL,
            searchable_text TEXT NOT NULL,
            structured_payload TEXT NOT NULL,
            memory_key TEXT
        );
        CREATE INDEX IF NOT EXISTS memories_owner_status_idx
            ON memories(owner_scope, status, memory_type, updated_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS memories_current_key_idx
            ON memories(owner_scope, memory_type, memory_key)
            WHERE status='active' AND memory_key IS NOT NULL;
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            memory_id UNINDEXED,
            owner_scope UNINDEXED,
            memory_type UNINDEXED,
            searchable_text,
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE IF NOT EXISTS conversation_sessions (
            owner_scope TEXT NOT NULL,
            transport TEXT NOT NULL,
            session_key TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            summary_text TEXT NOT NULL DEFAULT '',
            summary_payload TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(owner_scope, transport, session_key)
        );
        CREATE TABLE IF NOT EXISTS conversation_turns (
            turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_scope TEXT NOT NULL,
            transport TEXT NOT NULL,
            session_key TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS conversation_turns_session_idx
            ON conversation_turns(owner_scope, transport, session_key, turn_id DESC);
        CREATE TABLE IF NOT EXISTS goals (
            goal_id TEXT PRIMARY KEY,
            owner_scope TEXT NOT NULL,
            transport TEXT NOT NULL,
            original_request TEXT NOT NULL,
            canonical_intent TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            completed_steps TEXT NOT NULL,
            next_step TEXT,
            blocker TEXT,
            result TEXT,
            delivery_state TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS goals_owner_status_idx
            ON goals(owner_scope, status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS retrieval_traces (
            trace_id TEXT PRIMARY KEY,
            owner_scope TEXT NOT NULL,
            transport TEXT NOT NULL,
            session_key TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            memory_ids TEXT NOT NULL,
            reasons TEXT NOT NULL,
            token_counts TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_turn_traces (
            trace_id TEXT PRIMARY KEY,
            owner_scope TEXT NOT NULL,
            transport TEXT NOT NULL,
            session_key TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            completed_at INTEGER NOT NULL,
            route TEXT NOT NULL,
            profiles TEXT NOT NULL,
            models TEXT NOT NULL,
            token_counts TEXT NOT NULL,
            context_sections TEXT NOT NULL,
            retrieved_memory_ids TEXT NOT NULL,
            retrieval_trace_id TEXT,
            model_calls TEXT NOT NULL,
            tool_calls TEXT NOT NULL,
            policy_result TEXT NOT NULL,
            playbook TEXT NOT NULL,
            action TEXT NOT NULL,
            verification TEXT NOT NULL,
            total_latency_ms INTEGER NOT NULL,
            final_disposition TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS agent_turn_owner_created_idx
            ON agent_turn_traces(owner_scope, created_at DESC);
        """
        try:
            with self._connect() as connection:
                # Journal mode is persistent database metadata. Changing it on
                # every short read/write connection adds needless locking and
                # disk work, so establish it once during schema migration.
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(schema)
                current = connection.execute(
                    "SELECT value FROM memory_meta WHERE key='schema_version'"
                ).fetchone()
                if current is None:
                    connection.execute(
                        "INSERT INTO memory_meta(key,value) VALUES('schema_version',?)",
                        (str(SCHEMA_VERSION),),
                    )
                elif int(current["value"]) != SCHEMA_VERSION:
                    raise MemoryStoreError("unsupported memory schema version")
        except (sqlite3.Error, ValueError) as error:
            if isinstance(error, MemoryStoreError):
                raise
            raise MemoryStoreError("memory schema migration failed") from error

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM memory_meta WHERE key='schema_version'"
            ).fetchone()
        if row is None:
            raise MemoryStoreError("memory schema version is missing")
        return int(row["value"])

    def remember(
        self,
        *,
        memory_type: str,
        owner_scope: str,
        source_transport: str,
        source: str,
        searchable_text: str,
        structured_payload: dict[str, Any],
        confidence: float,
        source_session: str | None = None,
        valid_until: int | None = None,
        supersedes: str | None = None,
        memory_key: str | None = None,
    ) -> MemoryRecord:
        if memory_type not in MEMORY_TYPES:
            raise MemoryStoreError("memory type is invalid")
        owner = _scope(owner_scope)
        transport = _transport(source_transport)
        session = _session(source_session) if source_session is not None else None
        source_value = _text(source, field="source", maximum=128)
        text_value = _text(searchable_text, field="searchable text")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise MemoryStoreError("memory confidence is invalid")
        now = int(self.clock())
        if valid_until is not None and (
            isinstance(valid_until, bool)
            or not isinstance(valid_until, int)
            or valid_until <= now
        ):
            raise MemoryStoreError("memory validity is invalid")
        key = (
            _text(memory_key, field="memory key", maximum=128)
            if memory_key is not None
            else None
        )
        payload = _json(structured_payload)
        memory_id = secrets.token_hex(16)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replaced = supersedes
            if key is not None and replaced is None:
                prior = connection.execute(
                    """
                    SELECT memory_id FROM memories
                    WHERE owner_scope=? AND memory_type=? AND memory_key=?
                      AND status='active'
                    """,
                    (owner, memory_type, key),
                ).fetchone()
                replaced = str(prior["memory_id"]) if prior is not None else None
            if replaced is not None:
                prior = connection.execute(
                    """
                    SELECT memory_id FROM memories
                    WHERE memory_id=? AND owner_scope=? AND status='active'
                    """,
                    (replaced, owner),
                ).fetchone()
                if prior is None:
                    raise MemoryStoreError("superseded memory is unavailable")
                connection.execute(
                    "UPDATE memories SET status='superseded',updated_at=? WHERE memory_id=?",
                    (now, replaced),
                )
                connection.execute(
                    "DELETE FROM memory_fts WHERE memory_id=?", (replaced,)
                )
            connection.execute(
                """
                INSERT INTO memories(
                    memory_id,memory_type,owner_scope,source_transport,
                    source_session,source,confidence,created_at,updated_at,
                    valid_until,supersedes,status,searchable_text,
                    structured_payload,memory_key
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    memory_id,
                    memory_type,
                    owner,
                    transport,
                    session,
                    source_value,
                    float(confidence),
                    now,
                    now,
                    valid_until,
                    replaced,
                    "active",
                    text_value,
                    payload,
                    key,
                ),
            )
            connection.execute(
                "INSERT INTO memory_fts VALUES(?,?,?,?)",
                (memory_id, owner, memory_type, text_value),
            )
        return self.get_memory(memory_id, owner)

    def correct_memory(
        self,
        memory_id: str,
        *,
        owner_scope: str,
        source_transport: str,
        searchable_text: str,
        structured_payload: dict[str, Any],
        source_session: str | None = None,
    ) -> MemoryRecord:
        previous = self.get_memory(memory_id, owner_scope)
        return self.remember(
            memory_type=previous.memory_type,
            owner_scope=owner_scope,
            source_transport=source_transport,
            source="owner_correction",
            searchable_text=searchable_text,
            structured_payload=structured_payload,
            confidence=1.0,
            source_session=source_session,
            supersedes=previous.memory_id,
            memory_key=previous.memory_key,
        )

    def revoke(self, memory_id: str, owner_scope: str) -> None:
        owner = _scope(owner_scope)
        now = int(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE memories SET status='revoked',updated_at=?
                WHERE memory_id=? AND owner_scope=? AND status='active'
                """,
                (now, memory_id, owner),
            ).rowcount
            if changed != 1:
                raise MemoryStoreError("memory cannot be revoked")
            connection.execute(
                "DELETE FROM memory_fts WHERE memory_id=?", (memory_id,)
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> MemoryRecord:
        try:
            payload = json.loads(row["structured_payload"])
        except (json.JSONDecodeError, TypeError) as error:
            raise MemoryStoreError("stored memory payload is invalid") from error
        if not isinstance(payload, dict):
            raise MemoryStoreError("stored memory payload is invalid")
        return MemoryRecord(
            memory_id=str(row["memory_id"]),
            memory_type=str(row["memory_type"]),
            owner_scope=str(row["owner_scope"]),
            source_transport=str(row["source_transport"]),
            source=str(row["source"]),
            confidence=float(row["confidence"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            valid_until=(
                int(row["valid_until"]) if row["valid_until"] is not None else None
            ),
            supersedes=(
                str(row["supersedes"]) if row["supersedes"] is not None else None
            ),
            status=str(row["status"]),
            searchable_text=str(row["searchable_text"]),
            structured_payload=payload,
            memory_key=(
                str(row["memory_key"]) if row["memory_key"] is not None else None
            ),
        )

    def get_memory(self, memory_id: str, owner_scope: str) -> MemoryRecord:
        owner = _scope(owner_scope)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE memory_id=? AND owner_scope=?",
                (memory_id, owner),
            ).fetchone()
        if row is None:
            raise MemoryStoreError("memory is unavailable")
        return self._record(row)

    def active_memories(
        self,
        owner_scope: str,
        *,
        memory_types: Iterable[str] | None = None,
        limit: int = 16,
    ) -> list[MemoryRecord]:
        owner = _scope(owner_scope)
        now = int(self.clock())
        selected = tuple(memory_types or ())
        if any(value not in MEMORY_TYPES for value in selected) or not 1 <= limit <= 100:
            raise MemoryStoreError("memory query is invalid")
        where = ["owner_scope=?", "status='active'", "(valid_until IS NULL OR valid_until>?)"]
        parameters: list[Any] = [owner, now]
        if selected:
            where.append("memory_type IN (" + ",".join("?" for _ in selected) + ")")
            parameters.extend(selected)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memories WHERE "
                + " AND ".join(where)
                + " ORDER BY updated_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._record(row) for row in rows]

    def search(
        self,
        owner_scope: str,
        query: str,
        *,
        memory_types: Iterable[str] | None = None,
        limit: int = 8,
    ) -> list[MemoryRecord]:
        owner = _scope(owner_scope)
        query_value = _text(query, field="memory query", maximum=2_000)
        selected = tuple(memory_types or ())
        if any(value not in MEMORY_TYPES for value in selected) or not 1 <= limit <= 32:
            raise MemoryStoreError("memory query is invalid")
        tokens = list(dict.fromkeys(WORD_RE.findall(query_value.casefold())))[:12]
        if not tokens:
            return self.active_memories(owner, memory_types=selected, limit=limit)
        match = " OR ".join(f'"{token}"*' for token in tokens)
        now = int(self.clock())
        where = [
            "m.owner_scope=?",
            "m.status='active'",
            "(m.valid_until IS NULL OR m.valid_until>?)",
            "memory_fts MATCH ?",
        ]
        parameters: list[Any] = [owner, now, match]
        if selected:
            where.append("m.memory_type IN (" + ",".join("?" for _ in selected) + ")")
            parameters.extend(selected)
        parameters.append(limit)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT m.* FROM memory_fts
                    JOIN memories AS m ON m.memory_id=memory_fts.memory_id
                    WHERE """
                    + " AND ".join(where)
                    + " ORDER BY bm25(memory_fts),m.updated_at DESC LIMIT ?",
                    parameters,
                ).fetchall()
        except sqlite3.Error as error:
            raise MemoryStoreError("memory search failed") from error
        return [self._record(row) for row in rows]

    def append_turn(
        self,
        *,
        owner_scope: str,
        transport: str,
        session_key: str,
        role: str,
        content: str,
    ) -> int:
        owner = _scope(owner_scope)
        channel = _transport(transport)
        session = _session(session_key)
        if role not in ROLE_VALUES:
            raise MemoryStoreError("conversation role is invalid")
        text_value = _text(content, field="conversation content")
        now = int(self.clock())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_sessions(
                    owner_scope,transport,session_key,created_at,updated_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(owner_scope,transport,session_key)
                DO UPDATE SET updated_at=excluded.updated_at
                """,
                (owner, channel, session, now, now),
            )
            cursor = connection.execute(
                """
                INSERT INTO conversation_turns(
                    owner_scope,transport,session_key,role,content,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (owner, channel, session, role, text_value, now),
            )
            connection.execute(
                """
                DELETE FROM conversation_turns
                WHERE turn_id IN (
                    SELECT turn_id FROM conversation_turns
                    WHERE owner_scope=? AND transport=? AND session_key=?
                    ORDER BY turn_id DESC LIMIT -1 OFFSET ?
                )
                """,
                (owner, channel, session, MAX_RECENT_TURNS),
            )
            return int(cursor.lastrowid)

    def record_exchange(
        self,
        *,
        owner_scope: str,
        transport: str,
        session_key: str,
        user_text: str,
        assistant_text: str,
    ) -> dict[str, Any]:
        """Persist both turns and their compact summary in one durable commit."""
        owner = _scope(owner_scope)
        channel = _transport(transport)
        session = _session(session_key)
        user = _text(user_text, field="conversation user content")
        assistant = _text(assistant_text, field="conversation assistant content")
        now = int(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT summary_payload FROM conversation_sessions
                WHERE owner_scope=? AND transport=? AND session_key=?
                """,
                (owner, channel, session),
            ).fetchone()
            summary, summary_text, rendered = _summary_values(
                row["summary_payload"] if row is not None else None,
                user,
                assistant,
            )
            connection.execute(
                """
                INSERT INTO conversation_sessions(
                    owner_scope,transport,session_key,created_at,updated_at,
                    summary_text,summary_payload
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(owner_scope,transport,session_key)
                DO UPDATE SET
                    updated_at=excluded.updated_at,
                    summary_text=excluded.summary_text,
                    summary_payload=excluded.summary_payload
                """,
                (owner, channel, session, now, now, summary_text, rendered),
            )
            connection.executemany(
                """
                INSERT INTO conversation_turns(
                    owner_scope,transport,session_key,role,content,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    (owner, channel, session, "user", user, now),
                    (owner, channel, session, "assistant", assistant, now),
                ),
            )
            connection.execute(
                """
                DELETE FROM conversation_turns
                WHERE turn_id IN (
                    SELECT turn_id FROM conversation_turns
                    WHERE owner_scope=? AND transport=? AND session_key=?
                    ORDER BY turn_id DESC LIMIT -1 OFFSET ?
                )
                """,
                (owner, channel, session, MAX_RECENT_TURNS),
            )
        return summary

    def recent_turns(
        self,
        owner_scope: str,
        transport: str,
        session_key: str,
        *,
        limit: int = 24,
    ) -> list[dict[str, str]]:
        owner = _scope(owner_scope)
        channel = _transport(transport)
        session = _session(session_key)
        if not 1 <= limit <= MAX_RECENT_TURNS:
            raise MemoryStoreError("conversation limit is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role,content FROM conversation_turns
                WHERE owner_scope=? AND transport=? AND session_key=?
                ORDER BY turn_id DESC LIMIT ?
                """,
                (owner, channel, session, limit),
            ).fetchall()
        return [
            {"role": str(row["role"]), "content": str(row["content"])}
            for row in reversed(rows)
        ]

    def update_summary(
        self,
        *,
        owner_scope: str,
        transport: str,
        session_key: str,
        user_text: str,
        assistant_text: str,
    ) -> dict[str, Any]:
        owner = _scope(owner_scope)
        channel = _transport(transport)
        session = _session(session_key)
        user = _text(user_text, field="summary user text")
        assistant = _text(assistant_text, field="summary assistant text")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT summary_payload FROM conversation_sessions
                WHERE owner_scope=? AND transport=? AND session_key=?
                """,
                (owner, channel, session),
            ).fetchone()
            summary, summary_text, rendered = _summary_values(
                row["summary_payload"] if row is not None else None,
                user,
                assistant,
            )
            now = int(self.clock())
            connection.execute(
                """
                UPDATE conversation_sessions
                SET updated_at=?,summary_text=?,summary_payload=?
                WHERE owner_scope=? AND transport=? AND session_key=?
                """,
                (now, summary_text, rendered, owner, channel, session),
            )
        return summary

    def conversation_summary(
        self,
        owner_scope: str,
        transport: str,
        session_key: str,
        *,
        latest_owner_fallback: bool = True,
    ) -> dict[str, Any] | None:
        owner = _scope(owner_scope)
        channel = _transport(transport)
        session = _session(session_key)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT transport,session_key,summary_text,summary_payload,updated_at
                FROM conversation_sessions
                WHERE owner_scope=? AND transport=? AND session_key=?
                """,
                (owner, channel, session),
            ).fetchone()
            if row is None and latest_owner_fallback:
                row = connection.execute(
                    """
                    SELECT transport,session_key,summary_text,summary_payload,updated_at
                    FROM conversation_sessions
                    WHERE owner_scope=? AND summary_text!=''
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (owner,),
                ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["summary_payload"])
        except (json.JSONDecodeError, TypeError) as error:
            raise MemoryStoreError("conversation summary is invalid") from error
        if not isinstance(payload, dict):
            raise MemoryStoreError("conversation summary is invalid")
        return {
            "summary_text": str(row["summary_text"]),
            "structured_payload": payload,
            "updated_at": int(row["updated_at"]),
            "same_session": (
                str(row["transport"]) == channel and str(row["session_key"]) == session
            ),
        }

    def start_goal(
        self,
        *,
        owner_scope: str,
        transport: str,
        original_request: str,
        canonical_intent: str,
        next_step: str | None = None,
    ) -> dict[str, Any]:
        owner = _scope(owner_scope)
        channel = _transport(transport)
        original = _text(original_request, field="goal request")
        canonical = _text(canonical_intent, field="goal intent", maximum=2_000)
        next_value = (
            _text(next_step, field="goal next step", maximum=2_000)
            if next_step is not None
            else None
        )
        now = int(self.clock())
        with self._connect() as connection:
            prior = connection.execute(
                """
                SELECT * FROM goals
                WHERE owner_scope=? AND canonical_intent=?
                  AND status IN ('active','blocked')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (owner, canonical),
            ).fetchone()
            if prior is not None:
                return dict(prior)
            goal_id = secrets.token_hex(16)
            connection.execute(
                """
                INSERT INTO goals(
                    goal_id,owner_scope,transport,original_request,
                    canonical_intent,status,created_at,updated_at,
                    completed_steps,next_step,blocker,result,delivery_state
                ) VALUES(?,?,?,?,?,'active',?,?,?, ?,NULL,NULL,'pending')
                """,
                (
                    goal_id,
                    owner,
                    channel,
                    original,
                    canonical,
                    now,
                    now,
                    "[]",
                    next_value,
                ),
            )
        return self.get_goal(goal_id, owner)

    def get_goal(self, goal_id: str, owner_scope: str) -> dict[str, Any]:
        owner = _scope(owner_scope)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM goals WHERE goal_id=? AND owner_scope=?",
                (goal_id, owner),
            ).fetchone()
        if row is None:
            raise MemoryStoreError("goal is unavailable")
        result = dict(row)
        result["completed_steps"] = json.loads(str(result["completed_steps"]))
        return result

    def update_goal(
        self,
        goal_id: str,
        *,
        owner_scope: str,
        status: str | None = None,
        completed_steps: list[str] | None = None,
        next_step: str | None = None,
        blocker: str | None = None,
        result: str | None = None,
        delivery_state: str | None = None,
    ) -> dict[str, Any]:
        owner = _scope(owner_scope)
        if status is not None and status not in GOAL_STATUSES:
            raise MemoryStoreError("goal status is invalid")
        if delivery_state is not None and delivery_state not in DELIVERY_STATES:
            raise MemoryStoreError("goal delivery state is invalid")
        values: dict[str, Any] = {"updated_at": int(self.clock())}
        if status is not None:
            values["status"] = status
        if completed_steps is not None:
            values["completed_steps"] = _json(
                [_text(item, field="completed step", maximum=1_000) for item in completed_steps]
            )
        for key, value in (
            ("next_step", next_step),
            ("blocker", blocker),
            ("result", result),
        ):
            if value is not None:
                values[key] = _text(value, field=f"goal {key}", maximum=4_000)
        if delivery_state is not None:
            values["delivery_state"] = delivery_state
        assignments = ",".join(f"{key}=?" for key in values)
        with self._connect() as connection:
            changed = connection.execute(
                f"UPDATE goals SET {assignments} WHERE goal_id=? AND owner_scope=?",
                (*values.values(), goal_id, owner),
            ).rowcount
        if changed != 1:
            raise MemoryStoreError("goal cannot be updated")
        return self.get_goal(goal_id, owner)

    def active_goals(self, owner_scope: str, *, limit: int = 4) -> list[dict[str, Any]]:
        owner = _scope(owner_scope)
        if not 1 <= limit <= 20:
            raise MemoryStoreError("goal limit is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM goals
                WHERE owner_scope=? AND status IN ('active','blocked')
                ORDER BY updated_at DESC LIMIT ?
                """,
                (owner, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["completed_steps"] = json.loads(str(item["completed_steps"]))
            result.append(item)
        return result

    def goals_by_intent_prefix(
        self,
        owner_scope: str,
        prefix: str,
        *,
        statuses: Iterable[str] = ("active", "blocked", "completed"),
        delivery_states: Iterable[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Return a bounded private goal view for one namespaced worker.

        The prefix is application-owned metadata, not model-supplied SQL.  This
        lets transports resume only their own durable work without introducing
        a second task database or exposing unrelated owner goals.
        """

        owner = _scope(owner_scope)
        intent_prefix = _text(prefix, field="goal intent prefix", maximum=128)
        selected_statuses = tuple(dict.fromkeys(statuses))
        selected_delivery = (
            tuple(dict.fromkeys(delivery_states))
            if delivery_states is not None
            else ()
        )
        if (
            not selected_statuses
            or any(value not in GOAL_STATUSES for value in selected_statuses)
            or any(value not in DELIVERY_STATES for value in selected_delivery)
            or not 1 <= limit <= 20
        ):
            raise MemoryStoreError("goal query is invalid")
        clauses = [
            "owner_scope=?",
            "canonical_intent LIKE ? ESCAPE '\\'",
            "status IN (" + ",".join("?" for _ in selected_statuses) + ")",
        ]
        parameters: list[Any] = [
            owner,
            intent_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%",
            *selected_statuses,
        ]
        if selected_delivery:
            clauses.append(
                "delivery_state IN ("
                + ",".join("?" for _ in selected_delivery)
                + ")"
            )
            parameters.extend(selected_delivery)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM goals WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["completed_steps"] = json.loads(str(item["completed_steps"]))
            result.append(item)
        return result

    def write_trace(
        self,
        *,
        owner_scope: str,
        transport: str,
        session_key: str,
        memory_ids: list[str],
        reasons: dict[str, str],
        token_counts: dict[str, int],
    ) -> str:
        owner = _scope(owner_scope)
        channel = _transport(transport)
        session = _session(session_key)
        trace_id = secrets.token_hex(16)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO retrieval_traces(
                    trace_id,owner_scope,transport,session_key,created_at,
                    memory_ids,reasons,token_counts
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    trace_id,
                    owner,
                    channel,
                    session,
                    int(self.clock()),
                    _json(memory_ids),
                    _json(reasons),
                    _json(token_counts),
                ),
            )
        return trace_id

    def read_trace(self, trace_id: str, owner_scope: str) -> dict[str, Any]:
        owner = _scope(owner_scope)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_traces WHERE trace_id=? AND owner_scope=?",
                (trace_id, owner),
            ).fetchone()
        if row is None:
            raise MemoryStoreError("retrieval trace is unavailable")
        return {
            "trace_id": str(row["trace_id"]),
            "created_at": int(row["created_at"]),
            "memory_ids": json.loads(str(row["memory_ids"])),
            "reasons": json.loads(str(row["reasons"])),
            "token_counts": json.loads(str(row["token_counts"])),
        }

    def write_agent_turn_trace(self, document: dict[str, Any]) -> str:
        """Persist one bounded trace that contains no prompts, arguments or results."""
        required = {
            "trace_id", "owner_scope", "transport", "session_key", "created_at",
            "completed_at", "route", "profiles", "models", "token_counts",
            "context_sections", "retrieved_memory_ids", "retrieval_trace_id",
            "model_calls", "tool_calls", "policy_result", "playbook", "action",
            "verification", "total_latency_ms", "final_disposition",
        }
        if not isinstance(document, dict) or set(document) != required:
            raise MemoryStoreError("agent turn trace is malformed")
        trace_id = _session(document["trace_id"])
        owner = _scope(document["owner_scope"])
        channel = _transport(document["transport"])
        session = _session(document["session_key"])
        route = _trace_code(document["route"], field="route")
        disposition = _trace_code(
            document["final_disposition"], field="final disposition"
        )
        created_at = document["created_at"]
        completed_at = document["completed_at"]
        total_latency = document["total_latency_ms"]
        if (
            any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (created_at, completed_at, total_latency))
            or completed_at < created_at
            or total_latency > 86_400_000
        ):
            raise MemoryStoreError("agent turn timing is invalid")
        retrieval = document["retrieval_trace_id"]
        if retrieval is not None:
            retrieval = _session(retrieval)
        encoded = {
            key: _json(document[key]) for key in (
                "profiles", "models", "token_counts", "context_sections",
                "retrieved_memory_ids", "model_calls", "tool_calls",
                "policy_result", "playbook", "action", "verification",
            )
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_turn_traces(
                    trace_id,owner_scope,transport,session_key,created_at,
                    completed_at,route,profiles,models,token_counts,
                    context_sections,retrieved_memory_ids,retrieval_trace_id,
                    model_calls,tool_calls,policy_result,playbook,action,
                    verification,total_latency_ms,final_disposition
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trace_id, owner, channel, session, created_at, completed_at,
                    route, encoded["profiles"], encoded["models"],
                    encoded["token_counts"], encoded["context_sections"],
                    encoded["retrieved_memory_ids"], retrieval,
                    encoded["model_calls"], encoded["tool_calls"],
                    encoded["policy_result"], encoded["playbook"],
                    encoded["action"], encoded["verification"], total_latency,
                    disposition,
                ),
            )
        return trace_id

    def read_agent_turn_trace(
        self, trace_id: str, owner_scope: str
    ) -> dict[str, Any]:
        identifier = _session(trace_id)
        owner = _scope(owner_scope)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_turn_traces WHERE trace_id=? AND owner_scope=?",
                (identifier, owner),
            ).fetchone()
        if row is None:
            raise MemoryStoreError("agent turn trace is unavailable")
        result = dict(row)
        for key in (
            "profiles", "models", "token_counts", "context_sections",
            "retrieved_memory_ids", "model_calls", "tool_calls",
            "policy_result", "playbook", "action", "verification",
        ):
            result[key] = json.loads(str(result[key]))
        return result

    def recent_agent_turn_traces(
        self, owner_scope: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        owner = _scope(owner_scope)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise MemoryStoreError("agent turn trace limit is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT trace_id FROM agent_turn_traces WHERE owner_scope=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        return [
            self.read_agent_turn_trace(str(row["trace_id"]), owner)
            for row in rows
        ]

    def stats(self, owner_scope: str) -> dict[str, int]:
        owner = _scope(owner_scope)
        with self._connect() as connection:
            memories = connection.execute(
                "SELECT COUNT(*) AS count FROM memories WHERE owner_scope=?",
                (owner,),
            ).fetchone()
            turns = connection.execute(
                "SELECT COUNT(*) AS count FROM conversation_turns WHERE owner_scope=?",
                (owner,),
            ).fetchone()
            goals = connection.execute(
                "SELECT COUNT(*) AS count FROM goals WHERE owner_scope=?",
                (owner,),
            ).fetchone()
        return {
            "memories": int(memories["count"]),
            "turns": int(turns["count"]),
            "goals": int(goals["count"]),
        }
