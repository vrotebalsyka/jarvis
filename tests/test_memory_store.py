#!/usr/bin/env python3
"""Persistence, privacy, correction and goal contracts for owner memory."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import context_builder  # noqa: E402
import behavior_preferences  # noqa: E402
import memory_store  # noqa: E402


class Clock:
    def __init__(self, value: int = 1_800_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return float(self.value)


class MemoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "memory"
        self.root.mkdir(mode=0o700)
        self.root.chmod(0o700)
        self.clock = Clock()
        self.path = self.root / "memory.db"
        self.store = memory_store.MemoryStore(self.path, clock=self.clock)

    def tearDown(self) -> None:
        self.temporary.cleanup()


class MemoryStoreTests(MemoryFixture):
    def test_schema_file_and_records_are_private_and_versioned(self) -> None:
        self.assertEqual(self.store.schema_version(), memory_store.SCHEMA_VERSION)
        metadata = self.path.stat()
        self.assertEqual(metadata.st_uid, os.geteuid())
        self.assertEqual(metadata.st_mode & 0o777, 0o600)
        record = self.store.remember(
            memory_type="owner",
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            source_transport="local_chat",
            source_session="a" * 32,
            source="explicit_owner_statement",
            confidence=1.0,
            memory_key="response_style",
            searchable_text="Владелец предпочитает краткие ответы",
            structured_payload={"preference": "response_style", "value": "кратко"},
        )
        self.assertEqual(record.status, "active")
        self.assertEqual(record.source, "explicit_owner_statement")
        self.assertIsNone(record.valid_until)

        reopened = memory_store.MemoryStore(self.path, clock=self.clock)
        self.assertEqual(
            reopened.get_memory(record.memory_id, memory_store.PRIMARY_OWNER_SCOPE),
            record,
        )

    def test_correction_supersedes_old_alias_and_retrieval_returns_only_new(self) -> None:
        old = self.store.remember(
            memory_type="device",
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            source_transport="local_chat",
            source="explicit_owner_statement",
            confidence=1.0,
            memory_key="vacuum_alias",
            searchable_text="Робота пылесоса зовут Андрей",
            structured_payload={"alias": "Андрей", "device": "vacuum"},
        )
        new = self.store.correct_memory(
            old.memory_id,
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            source_transport="alice",
            searchable_text="Робота пылесоса теперь зовут Альфред",
            structured_payload={"alias": "Альфред", "device": "vacuum"},
        )
        self.assertEqual(
            self.store.get_memory(old.memory_id, memory_store.PRIMARY_OWNER_SCOPE).status,
            "superseded",
        )
        self.assertEqual(new.supersedes, old.memory_id)
        results = self.store.search(
            memory_store.PRIMARY_OWNER_SCOPE,
            "как зовут робота пылесоса",
        )
        self.assertEqual([item.memory_id for item in results], [new.memory_id])
        self.assertIn("Альфред", results[0].searchable_text)

    def test_expired_revoked_and_secret_values_are_not_retrieved(self) -> None:
        expiring = self.store.remember(
            memory_type="episodic",
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            source_transport="local_chat",
            source="verified_incident",
            confidence=0.9,
            valid_until=self.clock.value + 10,
            searchable_text="Посудомойка временно была недоступна",
            structured_payload={"status": "resolved"},
        )
        revoked = self.store.remember(
            memory_type="procedural",
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            source_transport="local_chat",
            source="owner_approved_playbook",
            confidence=1.0,
            searchable_text="Безопасно повторить только read-only проверку",
            structured_payload={"action": "observe"},
        )
        self.store.revoke(revoked.memory_id, memory_store.PRIMARY_OWNER_SCOPE)
        self.clock.value += 11
        self.assertEqual(
            self.store.search(memory_store.PRIMARY_OWNER_SCOPE, "посудомойка проверка"),
            [],
        )
        self.assertEqual(
            self.store.get_memory(expiring.memory_id, memory_store.PRIMARY_OWNER_SCOPE).status,
            "active",
        )
        for unsafe in (
            "Authorization: Bearer secret-value",
            "password=hunter2",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "eyJaaaaaaaaaaaaaaaa.eyJbbbbbbbbbbbbbbbb.cccccccccccccccccccc",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(
                memory_store.MemoryStoreError
            ):
                self.store.remember(
                    memory_type="owner",
                    owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
                    source_transport="local_chat",
                    source="test",
                    confidence=1.0,
                    searchable_text=unsafe,
                    structured_payload={"value": unsafe},
                )

    def test_new_goal_does_not_destroy_previous_active_goal(self) -> None:
        first = self.store.start_goal(
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            transport="local_chat",
            original_request="Следить за посудомойкой",
            canonical_intent="monitor dishwasher",
            next_step="Собрать baseline",
        )
        second = self.store.start_goal(
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            transport="alice",
            original_request="Проверить робот",
            canonical_intent="inspect vacuum",
            next_step="Прочитать состояние",
        )
        goals = self.store.active_goals(memory_store.PRIMARY_OWNER_SCOPE)
        self.assertEqual({item["goal_id"] for item in goals}, {first["goal_id"], second["goal_id"]})
        completed = self.store.update_goal(
            first["goal_id"],
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            status="completed",
            completed_steps=["Baseline сохранён", "Проверка завершена"],
            result="Устройство доступно",
            delivery_state="delivered",
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            [item["goal_id"] for item in self.store.active_goals(memory_store.PRIMARY_OWNER_SCOPE)],
            [second["goal_id"]],
        )

    def test_namespaced_goal_query_is_bounded_and_filters_delivery(self) -> None:
        deferred = self.store.start_goal(
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            transport="alice",
            original_request="Проверь Home Assistant",
            canonical_intent="alice-deferred-v1:resume:home_assistant:abcdef",
        )
        self.store.start_goal(
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            transport="local_chat",
            original_request="Другая задача",
            canonical_intent="unrelated:goal",
        )
        self.store.update_goal(
            deferred["goal_id"],
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            status="completed",
            result="Готово",
            delivery_state="pending",
        )
        selected = self.store.goals_by_intent_prefix(
            memory_store.PRIMARY_OWNER_SCOPE,
            "alice-deferred-v1:",
            statuses=("completed",),
            delivery_states=("pending",),
        )
        self.assertEqual([item["goal_id"] for item in selected], [deferred["goal_id"]])


class HundredTurnContinuityTests(MemoryFixture):
    def test_100_turns_restart_and_cross_transport_keep_alias_goal_and_preference(self) -> None:
        memory = context_builder.ConversationMemory(self.store)
        session = "b" * 32
        seed = (
            ("Робота зовут Андрей", "Запомнил имя робота."),
            ("Нет, называй его Альфред", "Буду называть его Альфред."),
            ("Отвечай кратко", "Хорошо."),
            (
                "Твоя задача следить за посудомойкой и сообщать о сбоях",
                "Задача принята как активная.",
            ),
        )
        for user, assistant in seed:
            memory.record_exchange(
                owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
                transport="local_chat",
                session_key=session,
                user_text=user,
                assistant_text=assistant,
            )
        behavior_preferences.behavior_set(
            "verbosity",
            "concise",
            store=self.store,
            source_transport="local_chat",
            source_session=session,
        )
        for number in range(96):
            memory.record_exchange(
                owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
                transport="local_chat",
                session_key=session,
                user_text=f"Обычная реплика номер {number}",
                assistant_text=f"Краткий ответ номер {number}",
            )

        reopened_store = memory_store.MemoryStore(self.path, clock=self.clock)
        reopened = context_builder.ConversationMemory(reopened_store)
        bundle = reopened.prepare(
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            transport="alice",
            session_key="c" * 32,
            current_turn="Как зовут робота и какая у тебя активная задача?",
        )
        rendered = str(bundle.memory_context)
        self.assertIn("Альфред", rendered)
        self.assertNotIn("Андрей", rendered)
        self.assertIn("concise", rendered)
        self.assertIn("посудомой", rendered.casefold())
        self.assertLessEqual(
            sum(bundle.memory_context["token_counts"].values()),
            context_builder.MAX_TOTAL_MEMORY_TOKENS,
        )
        self.assertLess(len(bundle.history), 100)
        self.assertFalse(
            bundle.memory_context["conversation_summary"]["same_session"]
        )
        self.assertEqual(reopened_store.stats(memory_store.PRIMARY_OWNER_SCOPE)["turns"], 200)

        trace = reopened_store.read_trace(
            bundle.trace_id, memory_store.PRIMARY_OWNER_SCOPE
        )
        self.assertNotIn("query", trace)
        self.assertNotIn("Альфред", str(trace))
        self.assertTrue(trace["memory_ids"])


if __name__ == "__main__":
    unittest.main()
