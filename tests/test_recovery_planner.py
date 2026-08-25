#!/usr/bin/env python3
"""Offline contracts for the bounded local-model recovery planner."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import incident_monitor  # noqa: E402
import recovery_planner as planner  # noqa: E402
from ollama_endpoint import OllamaEndpoint  # noqa: E402


class RecoveryPlannerTests(unittest.TestCase):
    def _store(self, temporary: str) -> incident_monitor.IncidentStore:
        state = Path(temporary) / "state"
        state.mkdir(mode=0o700)
        return incident_monitor.IncidentStore(
            state / incident_monitor.DATABASE_NAME
        )

    @staticmethod
    def _incident(*, safety: str = "light") -> dict[str, object]:
        return {
            "incident_id": 7,
            "status": "confirmed",
            "cause_code": "yandex_cloud_unreachable",
            "cause_confidence": "confirmed",
            "safety_class": safety,
            "action_code": "light.turn_on",
            "target_entity_id": "light.rele_2_garderob",
        }

    def test_retry_is_offered_only_after_all_safe_runtime_facts(self) -> None:
        incident = self._incident()
        incomplete = planner.build_facts(incident, {"yandex_cloud": "reachable"})
        incomplete_ids = {
            item["id"] for item in planner.build_candidates(incomplete)
        }
        self.assertNotIn("retry_original_intent_once", incomplete_ids)

        complete = planner.build_facts(incident, {
            "yandex_cloud": "reachable",
            "intent": "current",
            "target_state": "mismatched",
            "retry_budget": "available",
        })
        complete_ids = {item["id"] for item in planner.build_candidates(complete)}
        self.assertIn("retry_original_intent_once", complete_ids)
        self.assertNotIn("wait_yandex_backoff", complete_ids)

        restricted = planner.build_facts(self._incident(safety="restricted"), {
            "yandex_cloud": "reachable",
            "intent": "current",
            "target_state": "mismatched",
            "retry_budget": "available",
        })
        restricted_ids = {
            item["id"] for item in planner.build_candidates(restricted)
        }
        self.assertNotIn("retry_original_intent_once", restricted_ids)

    def test_model_can_select_only_offered_candidate_with_required_facts(self) -> None:
        facts = planner.build_facts(self._incident(), {
            "yandex_cloud": "reachable",
            "intent": "current",
            "target_state": "mismatched",
            "retry_budget": "available",
        })
        candidates = planner.build_candidates(facts)
        retry = next(
            item for item in candidates
            if item["id"] == "retry_original_intent_once"
        )
        captured: list[dict[str, object]] = []

        def call(_endpoint, path, payload, **kwargs):
            captured.append(payload)
            self.assertEqual(path, "/api/chat")
            self.assertEqual(
                kwargs["timeout"],
                planner.model_runtime_policy.get_profile("structured").request_timeout_seconds,
            )
            return {"message": {"content": json.dumps({
                "candidate_id": "retry_original_intent_once",
                "fact_ids": retry["required_fact_ids"],
            })}}

        decision = planner.choose(
            facts, candidates,
            endpoint_loader=lambda: OllamaEndpoint(
                "http://127.0.0.1:11434", "127.0.0.1", 11434
            ),
            ollama_call=call,
        )
        self.assertEqual(decision["source"], "model")
        self.assertEqual(decision["candidate_id"], "retry_original_intent_once")
        serialized = json.dumps(captured, ensure_ascii=True)
        self.assertIs(captured[0]["think"], False)
        self.assertEqual(
            captured[0]["options"]["num_predict"],
            planner.model_runtime_policy.get_profile("structured").output_limit,
        )
        self.assertNotIn("light.rele_2_garderob", serialized)
        self.assertNotIn("shell", serialized.split('"content": "')[0])

    def test_invalid_model_output_fails_to_observe_only(self) -> None:
        facts = planner.build_facts(self._incident(), {
            "yandex_cloud": "reachable",
            "intent": "current",
            "target_state": "mismatched",
            "retry_budget": "available",
        })
        candidates = planner.build_candidates(facts)
        decision = planner.choose(
            facts, candidates,
            endpoint_loader=lambda: OllamaEndpoint(
                "http://127.0.0.1:11434", "127.0.0.1", 11434
            ),
            ollama_call=lambda *_args, **_kwargs: {"message": {"content": json.dumps({
                "candidate_id": "run_arbitrary_shell",
                "fact_ids": ["incident:open"],
            })}},
        )
        self.assertEqual(decision, {
            "candidate_id": "observe_and_notify",
            "fact_ids": ["incident:open"],
            "source": "verified_fallback",
        })

    def test_universal_integration_reload_is_offered_only_by_profile(self) -> None:
        incident = self._incident()
        incident.update({
            "cause_code": "integration_not_loaded",
            "action_code": "integration.health",
            "target_entity_id": None,
            "safety_class": "unknown",
        })
        ready = planner.build_facts(incident, {
            "integration_profile": "idle_entry_reload",
            "entry_match": "single",
            "device_activity": "idle",
            "retry_budget": "available",
        })
        ready_ids = {item["id"] for item in planner.build_candidates(ready)}
        self.assertIn("reload_integration_entry_once", ready_ids)

        active = planner.build_facts(incident, {
            "integration_profile": "idle_entry_reload",
            "entry_match": "single",
            "device_activity": "active",
            "retry_budget": "available",
        })
        active_ids = {item["id"] for item in planner.build_candidates(active)}
        self.assertNotIn("reload_integration_entry_once", active_ids)

        unknown = planner.build_facts(incident, {
            "integration_profile": "diagnose_only",
            "entry_match": "single",
            "device_activity": "idle",
            "retry_budget": "available",
        })
        unknown_ids = {item["id"] for item in planner.build_candidates(unknown)}
        self.assertEqual(unknown_ids, {"observe_and_notify"})

    def test_model_candidate_is_kept_but_evidence_is_rebuilt_from_guards(self) -> None:
        facts = planner.build_facts(self._incident(), {
            "yandex_cloud": "reachable",
            "intent": "current",
            "target_state": "mismatched",
            "retry_budget": "available",
        })
        candidates = planner.build_candidates(facts)
        retry = next(
            item for item in candidates
            if item["id"] == "retry_original_intent_once"
        )
        decision = planner.choose(
            facts,
            candidates,
            endpoint_loader=lambda: OllamaEndpoint(
                "http://127.0.0.1:11434", "127.0.0.1", 11434
            ),
            ollama_call=lambda *_args, **_kwargs: {"message": {"content": json.dumps({
                "candidate_id": "retry_original_intent_once",
                "fact_ids": ["incident:open"],
            })}},
        )
        self.assertEqual(decision["source"], "model")
        self.assertEqual(
            decision["fact_ids"], sorted(retry["required_fact_ids"])
        )

    def test_plan_is_persisted_without_model_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = store.record_automation_run(
                    run_hash="a" * 64,
                    automation_entity_id="automation.garderob",
                    automation_item_hash="b" * 64,
                    outcome="failed",
                    started_epoch=100,
                    observed_epoch=101,
                    error_code="network_unreachable",
                    cause_code="yandex_cloud_unreachable",
                    cause_confidence="confirmed",
                    action_code="light.turn_on",
                    target_entity_id="light.rele_2_garderob",
                    display_name="Гардероб",
                )
                incident = store.operational_incident_candidates()[0]
                planned = planner.plan_one(
                    store, incident,
                    {"yandex_cloud": "unreachable"},
                    now=110,
                    chooser=lambda facts, candidates: {
                        "candidate_id": "wait_yandex_backoff",
                        "fact_ids": ["cause:yandex_cloud_unreachable"],
                        "source": "model",
                    },
                )
                self.assertEqual(planned["candidate_id"], "wait_yandex_backoff")
                row = store.connection.execute(
                    "SELECT * FROM recovery_decisions"
                ).fetchone()
                self.assertEqual(row["operational_incident_id"], result["incident_id"])
                self.assertEqual(row["decision_source"], "model")
                self.assertNotIn("Гардероб", row["fact_ids_json"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
