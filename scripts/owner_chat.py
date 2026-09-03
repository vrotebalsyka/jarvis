#!/usr/bin/env python3
"""Single owner-facing conversational path for read-only Home Assistant facts."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import bounded_ha_agent  # noqa: E402


MAX_HISTORY_MESSAGES = 10
MAX_MESSAGE_CHARS = 4_000
SENSITIVE_INPUT_PATTERN = re.compile(
    r"(?:\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b|"
    r"\b(?:token|токен|password|пароль)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


class OwnerChatError(RuntimeError):
    """A secret-free conversational failure."""


def startup_context() -> dict[str, Any]:
    """Create one ephemeral focus object for exactly one transport session."""

    return {
        "mode": "read_only", "home_graph": "home_assistant_inventory",
        "session_focus": bounded_ha_agent.SessionFocus(),
    }


def answer_natural(
    question: str,
    context: dict[str, Any],
    history: list[dict[str, str]],
    *,
    voice: bool = False,
    runtime_profile: str = "dialogue",
    natural_agent: Callable[..., str] | None = None,
    fallback_answerer: Any | None = None,
) -> str:
    """Send every ordinary utterance through the one bounded read-only core.

    ``fallback_answerer`` is accepted temporarily as a transport compatibility
    argument, but is deliberately never called.  There is no second natural-
    language router after the bounded agent.
    """

    del fallback_answerer
    if not isinstance(question, str):
        raise OwnerChatError("question is invalid")
    normalized = " ".join(question.strip().split())
    if not normalized or len(normalized) > MAX_MESSAGE_CHARS:
        raise OwnerChatError("question is empty or too long")
    if SENSITIVE_INPUT_PATTERN.search(normalized):
        return (
            "Не присылайте токены или пароли. Отзовите раскрытый секрет; "
            "доступ к Home Assistant уже хранится вне диалога."
        )
    if normalized.startswith("/"):
        if normalized.casefold() in {"/help", "/помощь"}:
            return (
                "Задайте обычный вопрос об устройстве или его текущем состоянии. "
                "Управление на этом этапе отключено."
            )
        raise OwnerChatError("unknown admin command")
    responder = bounded_ha_agent.respond if natural_agent is None else natural_agent
    safe_history = [
        {"role": item.get("role"), "content": item.get("content")}
        for item in history[-MAX_HISTORY_MESSAGES:]
        if item.get("role") in {"user", "assistant"}
        and isinstance(item.get("content"), str)
    ]
    try:
        result = responder(
            normalized,
            dict(context),
            safe_history,
            voice=voice,
            runtime_profile=runtime_profile,
        )
    except (bounded_ha_agent.BoundedAgentError, OSError, ValueError) as error:
        raise OwnerChatError("read-only conversational core is unavailable") from error
    if not isinstance(result, str) or not result.strip():
        raise OwnerChatError("read-only conversational core returned no answer")
    return result.strip()


def answer(
    question: str,
    context: dict[str, Any],
    history: list[dict[str, str]],
) -> str:
    """Compatibility name for explicit callers; it uses the same core."""

    return answer_natural(question, context, history)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        if len(arguments) != 2 or arguments[0] != "--oneshot":
            print('Использование: owner_chat.py [--oneshot "вопрос"]', file=sys.stderr)
            return 2
        try:
            print(answer_natural(arguments[1], startup_context(), []))
            return 0
        except OwnerChatError:
            print("Проверка не завершена. Ничего не менял.", file=sys.stderr)
            return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Для диалога нужен интерактивный терминал.", file=sys.stderr)
        return 2
    context = startup_context()
    history: list[dict[str, str]] = []
    print("Home Butler: read-only режим. /help — справка, /exit — выход.")
    while True:
        try:
            question = input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question.casefold() in {"/exit", "/выход"}:
            return 0
        try:
            response = answer_natural(question, context, history)
        except OwnerChatError:
            response = "Проверка не завершена. Ничего не менял."
        print(f"Home Butler: {response}")
        history.extend((
            {"role": "user", "content": question[:MAX_MESSAGE_CHARS]},
            {"role": "assistant", "content": response[:12_000]},
        ))
        history = history[-MAX_HISTORY_MESSAGES:]


if __name__ == "__main__":
    raise SystemExit(main())
