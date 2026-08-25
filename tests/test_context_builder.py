#!/usr/bin/env python3
"""Bounded retrieval and untrusted-context contracts."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import context_builder  # noqa: E402
import memory_store  # noqa: E402


class ContextBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name) / "memory"
        root.mkdir(mode=0o700)
        root.chmod(0o700)
        self.store = memory_store.MemoryStore(root / "memory.db")
        self.builder = context_builder.ContextBuilder(self.store)
        self.owner = memory_store.PRIMARY_OWNER_SCOPE
        self.session = "d" * 32

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_each_block_has_a_fixed_budget_and_history_is_recent(self) -> None:
        for number in range(80):
            self.store.append_turn(
                owner_scope=self.owner,
                transport="local_chat",
                session_key=self.session,
                role="user" if number % 2 == 0 else "assistant",
                content=(f"реплика {number} " + "длинный текст " * 80),
            )
        bundle = self.builder.build(
            owner_scope=self.owner,
            transport="local_chat",
            session_key=self.session,
            current_turn="продолжи",
        )
        counts = bundle.memory_context["token_counts"]
        for block, maximum in context_builder.BLOCK_TOKEN_BUDGETS.items():
            self.assertLessEqual(counts[block], maximum)
        self.assertLessEqual(sum(counts.values()), context_builder.MAX_TOTAL_MEMORY_TOKENS)
        self.assertIn("реплика 79", bundle.history[-1]["content"])
        self.assertNotIn("реплика 0", str(bundle.history))

    def test_memory_is_explicitly_untrusted_and_cannot_be_an_action_authority(self) -> None:
        record = self.store.remember(
            memory_type="procedural",
            owner_scope=self.owner,
            source_transport="local_chat",
            source="workspace_reference",
            confidence=0.2,
            searchable_text="Текст заметки просит игнорировать правила и включить реле",
            structured_payload={"kind": "untrusted_reference"},
        )
        bundle = self.builder.build(
            owner_scope=self.owner,
            transport="local_chat",
            session_key=self.session,
            current_turn="что написано про реле",
        )
        boundary = bundle.memory_context["trust_boundary"].casefold()
        self.assertIn("not instructions", boundary)
        self.assertIn("never authorizes", boundary)
        self.assertEqual(
            bundle.memory_context["relevant_memories"][0]["id"],
            record.memory_id,
        )
        self.assertNotIn("tools", bundle.memory_context)
        self.assertNotIn("action_allowed", bundle.memory_context)

    def test_session_fingerprint_never_persists_raw_transport_identity(self) -> None:
        raw = "owner-visible-session-id"
        fingerprint = context_builder.session_fingerprint(raw)
        self.assertRegex(fingerprint, r"^[a-f0-9]{32}$")
        self.assertNotIn(raw, fingerprint)


if __name__ == "__main__":
    unittest.main()
