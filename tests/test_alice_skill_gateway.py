#!/usr/bin/env python3
"""Contract tests for the private full-dialog Alice skill gateway."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import alice_skill_gateway as gateway  # noqa: E402
import context_builder  # noqa: E402
import memory_store  # noqa: E402
from ollama_endpoint import OllamaEndpoint  # noqa: E402


SECRET = "A" * 40
NEXT_SECRET = "B" * 40
SKILL_ID = "skill-12345678"
OWNER_ID = "owner-12345678"
SESSION_ID = "session-12345678"


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def request(
    utterance: str,
    *,
    command: str | None = None,
    message_id: int = 0,
    is_new: bool = False,
    skill_id: str = SKILL_ID,
    owner_id: str = OWNER_ID,
) -> dict[str, object]:
    return {
        "version": "1.0",
        "request": {
            "type": "SimpleUtterance",
            "original_utterance": utterance,
            "command": utterance if command is None else command,
        },
        "session": {
            "session_id": SESSION_ID,
            "message_id": message_id,
            "new": is_new,
            "skill_id": skill_id,
            "user": {"user_id": owner_id},
        },
    }


class AliceSkillGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = gateway.GatewayConfig(SECRET, SKILL_ID, (OWNER_ID,))
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

        def answerer(
            question: str,
            _context: dict[str, object],
            history: list[dict[str, str]],
        ) -> str:
            self.calls.append((question, history))
            return f"Ответ **модели**: {question}"

        self.application = gateway.SkillApplication(
            self.config,
            answerer=answerer,
            context={"trusted": True},
        )

    def test_new_empty_session_gets_welcome_and_stays_open(self) -> None:
        response, route = self.application.process(request("", is_new=True))
        self.assertEqual(route, "welcome")
        self.assertFalse(response["response"]["end_session"])
        self.assertIn("Говорите свободно", response["response"]["text"])
        self.assertEqual(self.calls, [])

    def test_arbitrary_multi_turn_dialogue_passes_history(self) -> None:
        first, _route = self.application.process(
            request("Кто ты?", message_id=1, is_new=True)
        )
        second, _route = self.application.process(request("А что ты умеешь?", message_id=2))
        self.assertIn("Ответ модели", first["response"]["text"])
        self.assertIn("А что ты умеешь", second["response"]["text"])
        self.assertEqual(self.calls[1][1][0]["content"], "Кто ты?")
        self.assertEqual(self.calls[1][1][1]["role"], "assistant")
        self.assertFalse(second["response"]["end_session"])

    def test_verified_owner_turn_uses_shared_memory_without_raw_session_id(self) -> None:
        class Memory:
            def __init__(self) -> None:
                self.prepared = []
                self.recorded = []

            def prepare(self, **kwargs):
                self.prepared.append(kwargs)
                return context_builder.ContextBundle(
                    [{"role": "user", "content": "из прошлого разговора"}],
                    {"relevant_memories": [{"text": "Отвечать кратко"}]},
                    "trace",
                )

            def record_exchange(self, **kwargs):
                self.recorded.append(kwargs)

        memory = Memory()
        observed = {}

        def answerer(_question, context, history):
            observed["context"] = context
            observed["history"] = history
            return "Краткий ответ"

        application = gateway.SkillApplication(
            self.config,
            answerer=answerer,
            context={"trusted": True},
            conversation_memory=memory,
        )
        response, _route = application.process(
            request("продолжи", message_id=1, is_new=True)
        )
        self.assertEqual(response["response"]["text"], "Краткий ответ")
        self.assertEqual(observed["history"][0]["content"], "из прошлого разговора")
        self.assertIn("memory", observed["context"])
        self.assertNotEqual(memory.prepared[0]["session_key"], SESSION_ID)
        self.assertEqual(memory.recorded[0]["transport"], "alice")

    def test_verified_owner_turn_persists_secret_safe_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            store = memory_store.MemoryStore(directory / "memory.db")
            conversation = context_builder.ConversationMemory(store)
            application = gateway.SkillApplication(
                self.config,
                answerer=lambda _question, _context, _history: "Готово.",
                context={"trusted": True},
                conversation_memory=conversation,
            )
            response, route = application.process(
                request("обычный вопрос", message_id=1, is_new=True)
            )
            self.assertEqual(response["response"]["text"], "Готово.")
            traces = store.recent_agent_turn_traces(
                memory_store.PRIMARY_OWNER_SCOPE
            )
            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0]["transport"], "alice")
            self.assertEqual(traces[0]["route"], route)
            self.assertNotIn(OWNER_ID, str(traces[0]))
            self.assertNotIn(SESSION_ID, str(traces[0]))

    def test_duplicate_message_returns_cached_response_without_second_action(self) -> None:
        document = request("Включи switch.test", message_id=4)
        first, _route = self.application.process(document)
        second, route = self.application.process(document)
        self.assertEqual(route, "duplicate")
        self.assertIs(first, second)
        self.assertEqual(len(self.calls), 1)

    def test_out_of_order_message_is_rejected(self) -> None:
        self.application.process(request("Первый", message_id=7))
        with self.assertRaises(gateway.GatewayError):
            self.application.process(request("Старый", message_id=6))

    def test_exit_closes_session_without_calling_model(self) -> None:
        response, route = self.application.process(request("закрой навык", message_id=1))
        self.assertEqual(route, "exit")
        self.assertTrue(response["response"]["end_session"])
        self.assertEqual(self.calls, [])

    def test_ping_bypasses_model(self) -> None:
        response, route = self.application.process(request("ping", message_id=1))
        self.assertEqual(route, "ping")
        self.assertEqual(response["response"]["text"], "Дворецкий на связи.")
        self.assertEqual(self.calls, [])

    def test_health_probes_are_read_only_and_do_not_create_a_session(self) -> None:
        probes: list[str] = []
        sessions = gateway.SessionStore()
        application = gateway.SkillApplication(
            self.config,
            answerer=lambda _question, _context, _history: "unexpected",
            context={"trusted": True},
            sessions=sessions,
            model_health_probe=lambda: probes.append("model"),
            ha_health_probe=lambda: probes.append("ha_read"),
        )
        model, model_route = application.process(
            request(gateway.HEALTH_MODEL_COMMAND, message_id=1)
        )
        ha_read, ha_route = application.process(
            request(gateway.HEALTH_HA_READ_COMMAND, message_id=2)
        )
        self.assertEqual(model_route, "health_model")
        self.assertEqual(ha_route, "health_ha_read")
        self.assertEqual(model["response"]["text"], gateway.HEALTH_MODEL_TEXT)
        self.assertEqual(ha_read["response"]["text"], gateway.HEALTH_HA_READ_TEXT)
        self.assertEqual(probes, ["ha_read"])
        self.assertEqual(sessions._records, {})
        self.assertEqual(self.calls, [])

    def test_voice_warmup_accepts_private_gpu_endpoint(self) -> None:
        endpoint = OllamaEndpoint("http://172.27.192.1:11434", "172.27.192.1", 11434)
        with mock.patch.object(
            gateway.owner_chat, "load_runtime_ollama_endpoint", return_value=endpoint
        ), mock.patch.object(
            gateway.model_ha_proof, "get_ollama"
        ) as get_ollama, mock.patch.object(
            gateway.model_ha_proof,
            "call_ollama",
            return_value={"response": "готов"},
        ), mock.patch.object(
            gateway.model_ha_proof,
            "gpu_evidence",
            return_value={"fully_on_gpu": True},
        ):
            get_ollama.side_effect = [{"version": "test"}, {"models": []}]
            gateway.warm_voice_model()

    def test_voice_warmup_rejects_linux_loopback_endpoint(self) -> None:
        endpoint = OllamaEndpoint("http://127.0.0.1:11434", "127.0.0.1", 11434)
        with mock.patch.object(
            gateway.owner_chat, "load_runtime_ollama_endpoint", return_value=endpoint
        ), mock.patch.object(
            gateway.model_ha_proof, "get_ollama"
        ) as get_ollama, mock.patch.object(
            gateway.model_ha_proof,
            "call_ollama",
            return_value={"response": "готов"},
        ), mock.patch.object(
            gateway.model_ha_proof,
            "gpu_evidence",
            return_value={"fully_on_gpu": True},
        ):
            get_ollama.side_effect = [{"version": "test"}, {"models": []}]
            with self.assertRaises(gateway.GatewayError):
                gateway.warm_voice_model()

    def test_webhook_answers_while_background_model_is_starting(self) -> None:
        release = threading.Event()

        def delayed_warm() -> None:
            release.wait(1)

        with mock.patch.object(gateway, "warm_voice_model", side_effect=delayed_warm), mock.patch.object(
            gateway.owner_chat, "startup_context", return_value={"trusted": True}
        ):
            application = gateway.SkillApplication(
                self.config,
                answerer=lambda _question, _context, _history: "unexpected",
            )
            try:
                ping, ping_route = application.process(request("ping", message_id=1))
                response, route = application.process(request("привет", message_id=2))
                self.assertEqual(ping_route, "ping")
                self.assertEqual(ping["response"]["text"], "Дворецкий на связи.")
                self.assertEqual(route, "model_starting")
                self.assertIn("модель ещё запускается", response["response"]["text"])
            finally:
                release.set()
                application.close()

    def test_wrong_skill_or_owner_is_rejected(self) -> None:
        with self.assertRaises(gateway.GatewayError):
            self.application.process(request("тест", skill_id="skill-attacker"))
        with self.assertRaises(gateway.GatewayError):
            self.application.process(request("тест", owner_id="owner-attacker"))

    def test_staged_rotation_accepts_only_primary_and_next_paths(self) -> None:
        config = gateway.GatewayConfig(
            SECRET, SKILL_ID, (OWNER_ID,), next_secret=NEXT_SECRET
        )
        self.assertTrue(config.rotation_staged)
        self.assertEqual(config.secret_slot(f"/alice/{SECRET}"), "primary")
        self.assertEqual(config.secret_slot(f"/alice/{NEXT_SECRET}"), "next")
        self.assertIsNone(config.secret_slot("/alice/attacker"))

    def test_duplicate_rotation_secret_is_not_a_second_path(self) -> None:
        config = gateway.GatewayConfig(
            SECRET, SKILL_ID, (OWNER_ID,), next_secret=SECRET
        )
        self.assertFalse(config.rotation_staged)
        self.assertIsNone(config.next_webhook_path)

    def test_rotation_marker_is_private_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            marker = directory / "webhook-next-used"
            with mock.patch.dict(
                os.environ,
                {"HOME_BUTLER_ALICE_ROTATION_MARKER": str(marker)},
            ):
                gateway.write_rotation_marker(NEXT_SECRET)
                gateway.write_rotation_marker(NEXT_SECRET)
            self.assertEqual(stat_mode(marker), 0o600)
            contents = marker.read_text(encoding="ascii").strip()
            self.assertEqual(contents, gateway.rotation_marker(NEXT_SECRET))
            self.assertNotIn(NEXT_SECRET, contents)

    def test_private_skill_can_bootstrap_without_owner_id_allowlist(self) -> None:
        config = gateway.GatewayConfig(SECRET, SKILL_ID, ())
        application = gateway.SkillApplication(
            config,
            answerer=lambda _question, _context, _history: "готов",
            context={},
        )
        response, _route = application.process(
            request("тест", owner_id="owner-first-private-request")
        )
        self.assertEqual(response["response"]["text"], "готов")

    def test_pending_mode_claims_identity_but_never_calls_model(self) -> None:
        claims: list[tuple[str, str | None]] = []
        config = gateway.GatewayConfig(SECRET, gateway.PENDING_SKILL_ID, ())
        application = gateway.SkillApplication(
            config,
            answerer=lambda _question, _context, _history: self.fail(
                "pending mode must never call the model"
            ),
            context={},
            claim_writer=lambda skill_id, user_id: claims.append((skill_id, user_id)),
        )
        response, route = application.process(request("включи свет", message_id=1))
        self.assertEqual(route, "provisioning")
        self.assertIn("Привязка", response["response"]["text"])
        self.assertEqual(claims, [(SKILL_ID, OWNER_ID)])

    def test_pending_config_does_not_accept_the_pending_marker_as_request_id(self) -> None:
        config = gateway.GatewayConfig(SECRET, gateway.PENDING_SKILL_ID, ())
        application = gateway.SkillApplication(
            config,
            answerer=lambda _question, _context, _history: "unexpected",
            context={},
            claim_writer=lambda _skill_id, _user_id: None,
        )
        with self.assertRaises(gateway.GatewayError):
            application.process(request("ping", skill_id=gateway.PENDING_SKILL_ID))

    def test_only_simple_utterance_version_one_is_accepted(self) -> None:
        wrong_version = request("тест")
        wrong_version["version"] = "2.0"
        with self.assertRaises(gateway.GatewayError):
            self.application.process(wrong_version)
        wrong_type = request("тест")
        wrong_type["request"]["type"] = "ButtonPressed"
        with self.assertRaises(gateway.GatewayError):
            self.application.process(wrong_type)

    def test_response_is_speech_safe_and_within_yandex_limit(self) -> None:
        text = "**Заголовок** " + ("длинный текст " * 200)
        speech = gateway.speechify(text)
        self.assertNotIn("*", speech)
        self.assertLessEqual(len(speech), gateway.MAX_SPEECH_CHARS)

    def test_model_speech_is_limited_to_two_complete_sentences(self) -> None:
        speech = gateway.compact_model_speech(
            "Первое законченное предложение. Второе законченное предложение! "
            "Третье предложение не должно прозвучать."
        )
        self.assertEqual(
            speech,
            "Первое законченное предложение. Второе законченное предложение!",
        )
        self.assertLessEqual(len(speech), gateway.MAX_MODEL_SPEECH_CHARS)

    def test_parser_rejects_duplicate_keys_and_oversize(self) -> None:
        with self.assertRaises(gateway.GatewayError):
            gateway.parse_json(b'{"version":"1.0","version":"1.0"}')
        with self.assertRaises(gateway.GatewayError):
            gateway.parse_json(b"x" * (gateway.MAX_REQUEST_BYTES + 1))

    def test_log_fingerprint_does_not_disclose_session(self) -> None:
        fingerprint = gateway.session_fingerprint(SESSION_ID)
        self.assertNotIn(SESSION_ID, fingerprint)
        self.assertEqual(len(fingerprint), 12)

    def test_failure_log_code_does_not_disclose_exception_details(self) -> None:
        secret_text = "sensitive-token-value"
        code = gateway.safe_failure_code(gateway.GatewayError(secret_text))
        self.assertEqual(code, "gateway_rejected")
        self.assertNotIn(secret_text, code)
        self.assertEqual(
            gateway.safe_failure_code(gateway.owner_chat.OwnerChatError(secret_text)),
            "owner_chat_failed",
        )

    def test_failures_are_concrete_and_never_use_the_old_vague_phrase(self) -> None:
        control_message = gateway.human_failure_message(
            gateway.ha_control.ControlError("secret"), "home_assistant_control"
        )
        self.assertIn("Команда не отправлена", control_message)
        self.assertIn("полное русское название", control_message)
        model_message = gateway.human_failure_message(
            gateway.owner_chat.OwnerChatError("secret"), "general"
        )
        self.assertIn("Локальная модель", model_message)
        source = (SCRIPT_DIR / "alice_skill_gateway.py").read_text(encoding="utf-8")
        self.assertNotIn("Не успел безопасно завершить проверку", source)
        self.assertNotIn("не удалось", control_message.casefold())

    def test_response_envelope_is_small_valid_json(self) -> None:
        response = gateway.skill_response("Тест")
        raw = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.assertLess(len(raw), 5000)
        self.assertEqual(response["version"], "1.0")

    def test_slow_turn_returns_honest_response_before_yandex_deadline(self) -> None:
        release = threading.Event()

        def slow_answer(
            _question: str,
            _context: dict[str, object],
            _history: list[dict[str, str]],
        ) -> str:
            release.wait(1)
            return "Поздний ответ"

        application = gateway.SkillApplication(
            self.config,
            answerer=slow_answer,
            context={},
        )
        executor = gateway.BoundedTurnExecutor(
            application,
            timeout_seconds=0.03,
            max_active_turns=1,
        )
        try:
            started = time.monotonic()
            response, route, disposition = executor.run(
                request("что с Home Assistant", message_id=10),
                "home_assistant",
            )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.25)
            self.assertEqual(route, "home_assistant")
            self.assertEqual(disposition, "timeout_unpersisted")
            self.assertIn("продолжается", response["response"]["text"])
            self.assertIn("ничего не меняю", response["response"]["text"])

            busy, _route, busy_disposition = executor.run(
                request("ещё вопрос", message_id=11),
                "general",
            )
            self.assertEqual(busy_disposition, "busy")
            self.assertIn("предыдущий запрос", busy["response"]["text"])
        finally:
            release.set()
            executor.close()

    def test_slow_turn_becomes_durable_goal_and_result_is_retrievable(self) -> None:
        release = threading.Event()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            store = memory_store.MemoryStore(directory / "memory.db")
            conversation = context_builder.ConversationMemory(store)

            def slow_answer(
                _question: str,
                _context: dict[str, object],
                _history: list[dict[str, str]],
            ) -> str:
                release.wait(1)
                return "Проверка закончена: Home Assistant доступен."

            application = gateway.SkillApplication(
                self.config,
                answerer=slow_answer,
                context={},
                conversation_memory=conversation,
            )
            executor = gateway.BoundedTurnExecutor(
                application,
                timeout_seconds=0.03,
                max_active_turns=1,
            )
            try:
                response, route, disposition = executor.run(
                    request("что с Home Assistant", message_id=21),
                    "home_assistant",
                )
                self.assertEqual(route, "home_assistant")
                self.assertEqual(disposition, "deferred")
                self.assertIn("Задача", response["response"]["text"])
                goals = store.goals_by_intent_prefix(
                    memory_store.PRIMARY_OWNER_SCOPE,
                    gateway.DEFERRED_INTENT_PREFIX,
                    statuses=("active",),
                )
                self.assertEqual(len(goals), 1)
                short_id = goals[0]["goal_id"][:8]
                release.set()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    completed = store.goals_by_intent_prefix(
                        memory_store.PRIMARY_OWNER_SCOPE,
                        gateway.DEFERRED_INTENT_PREFIX,
                        statuses=("completed",),
                    )
                    if completed:
                        break
                    time.sleep(0.01)
                self.assertEqual(len(completed), 1)
                next_turn, next_route = application.process(
                    request("привет", message_id=22)
                )
                self.assertEqual(next_route, "general")
                self.assertIn("Результат задачи", next_turn["response"]["text"])
                self.assertIn("Home Assistant доступен", next_turn["response"]["text"])
                stored = store.get_goal(completed[0]["goal_id"], memory_store.PRIMARY_OWNER_SCOPE)
                self.assertEqual(stored["delivery_state"], "delivery_unknown")
                status, status_route = application.process(
                    request(f"статус задачи {short_id}", message_id=23)
                )
                self.assertEqual(status_route, "deferred_status")
                self.assertIn("Home Assistant доступен", status["response"]["text"])
            finally:
                release.set()
                executor.close()
                application.close()

    def test_restart_resumes_read_only_goal_but_never_replays_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            store = memory_store.MemoryStore(directory / "memory.db")
            conversation = context_builder.ConversationMemory(store)
            safe = store.start_goal(
                owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
                transport="alice",
                original_request="что с Home Assistant",
                canonical_intent=(
                    gateway.DEFERRED_INTENT_PREFIX
                    + "resume:home_assistant:aaaaaaaaaaaaaaaaaaaaaaaa"
                ),
            )
            unsafe = store.start_goal(
                owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
                transport="alice",
                original_request="включи зеркало",
                canonical_intent=(
                    gateway.DEFERRED_INTENT_PREFIX
                    + "no-replay:home_assistant_control:bbbbbbbbbbbbbbbbbbbbbbbb"
                ),
            )
            calls: list[str] = []

            def answerer(
                question: str,
                _context: dict[str, object],
                _history: list[dict[str, str]],
            ) -> str:
                calls.append(question)
                return "HA доступен после восстановления задачи."

            application = gateway.SkillApplication(
                self.config,
                answerer=answerer,
                context={},
                conversation_memory=conversation,
            )
            application.resume_deferred_turns()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                safe_state = store.get_goal(
                    safe["goal_id"], memory_store.PRIMARY_OWNER_SCOPE
                )
                if safe_state["status"] == "completed":
                    break
                time.sleep(0.01)
            unsafe_state = store.get_goal(
                unsafe["goal_id"], memory_store.PRIMARY_OWNER_SCOPE
            )
            self.assertEqual(safe_state["status"], "completed")
            self.assertEqual(unsafe_state["status"], "blocked")
            self.assertEqual(unsafe_state["blocker"], "mutating_turn_not_replayed")
            self.assertEqual(calls, ["что с Home Assistant"])
            application.close()

    def test_fast_turn_keeps_the_real_model_answer(self) -> None:
        executor = gateway.BoundedTurnExecutor(
            self.application,
            timeout_seconds=0.2,
            max_active_turns=1,
        )
        try:
            response, _route, disposition = executor.run(
                request("привет", message_id=12),
                "general",
            )
        finally:
            executor.close()
        self.assertEqual(disposition, "completed")
        self.assertIn("Ответ модели", response["response"]["text"])

    def test_deferred_control_never_claims_that_the_action_succeeded(self) -> None:
        message = gateway.deadline_message("home_assistant_control")
        self.assertIn("проверяю результат", message)
        self.assertIn("Не повторяйте", message)
        self.assertNotIn("выполнена", message.casefold())
        self.assertLess(
            gateway.TURN_RESPONSE_BUDGET_SECONDS,
            gateway.YANDEX_RESPONSE_LIMIT_SECONDS,
        )

    def test_general_turn_uses_the_unified_low_latency_gpu_profile(self) -> None:
        with mock.patch(
            "alice_skill_gateway.owner_chat.classify_request", return_value="general"
        ), mock.patch(
            "alice_skill_gateway.owner_chat.general_response", return_value="готов"
        ) as general:
            self.assertEqual(gateway.fast_model_answer("привет", {}, []), "готов")
        self.assertEqual(
            general.call_args.kwargs["runtime_profile"],
            gateway.VOICE_RUNTIME_PROFILE,
        )
        self.assertEqual(general.call_args.kwargs["profile"], "voice")
        self.assertEqual(
            general.call_args.args[1], {"mode": "voice_conversation"}
        )
        self.assertEqual(
            gateway.TURN_RESPONSE_BUDGET_SECONDS,
            gateway.VOICE_POLICY.latency_budget_seconds,
        )

    def test_capability_turn_uses_the_local_identity_profile(self) -> None:
        with mock.patch(
            "alice_skill_gateway.owner_chat.classify_request", return_value="general"
        ), mock.patch(
            "alice_skill_gateway.owner_chat.is_capability_question", return_value=True
        ), mock.patch(
            "alice_skill_gateway.owner_chat.general_response", return_value="готов"
        ) as general:
            self.assertEqual(gateway.fast_model_answer("кто ты", {}, []), "готов")
        self.assertEqual(general.call_args.kwargs["profile"], "voice_identity")
        self.assertEqual(
            general.call_args.kwargs["runtime_profile"], "voice_fast"
        )

    def test_free_dialogue_capability_uses_direct_non_identity_profile(self) -> None:
        with mock.patch(
            "alice_skill_gateway.owner_chat.classify_request", return_value="general"
        ), mock.patch(
            "alice_skill_gateway.owner_chat.is_free_dialogue_capability_question",
            return_value=True,
        ), mock.patch(
            "alice_skill_gateway.owner_chat.general_response", return_value="Да."
        ) as general:
            self.assertEqual(
                gateway.fast_model_answer(
                    "можешь говорить со мной на любые темы?", {}, []
                ),
                "Да.",
            )
        self.assertEqual(general.call_args.kwargs["profile"], "voice_free_dialogue")

    def test_ha_read_turn_uses_the_bounded_voice_proof(self) -> None:
        with mock.patch(
            "alice_skill_gateway.owner_chat.classify_request",
            return_value="home_assistant",
        ), mock.patch(
            "alice_skill_gateway.owner_chat.voice_ha_response",
            return_value="HA VOICE OK",
        ) as voice_ha, mock.patch(
            "alice_skill_gateway.owner_chat.answer"
        ) as full_answer:
            self.assertEqual(
                gateway.fast_model_answer("что с Home Assistant", {}, []),
                "HA VOICE OK",
            )
        voice_ha.assert_called_once_with("что с Home Assistant")
        full_answer.assert_not_called()

    def test_ha_control_turn_uses_natural_voice_result(self) -> None:
        with mock.patch(
            "alice_skill_gateway.owner_chat.classify_request",
            return_value="home_assistant_control",
        ), mock.patch(
            "alice_skill_gateway.owner_chat.control_response",
            return_value="Готово, свет включён.",
        ) as control, mock.patch(
            "alice_skill_gateway.owner_chat.answer"
        ) as full_answer:
            self.assertEqual(
                gateway.fast_model_answer("включи свет в коридоре", {}, []),
                "Готово, свет включён.",
            )
        control.assert_called_once_with("включи свет в коридоре", voice=True)
        full_answer.assert_not_called()

    def test_free_incident_questions_use_the_private_ledger_through_alice(self) -> None:
        phrases = (
            "Что сегодня ломалось?",
            "Почему не включился гардероб?",
            "Что ты восстановил?",
            "Какие устройства сейчас плохо себя чувствуют?",
        )
        with mock.patch(
            "alice_skill_gateway.owner_chat.answer", return_value="Журнал готов."
        ) as answer:
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    self.assertEqual(
                        gateway.fast_model_answer(phrase, {}, []), "Журнал готов."
                    )
        self.assertEqual([call.args[0] for call in answer.call_args_list], list(phrases))


if __name__ == "__main__":
    unittest.main()
