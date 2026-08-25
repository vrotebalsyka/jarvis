#!/usr/bin/env python3
"""Call local structured inference and render a deterministic Russian report."""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

sys.dont_write_bytecode = True

from health_report_core import (
    MAX_INPUT_BYTES,
    Analysis,
    Finding,
    HealthReportError,
    analyze_snapshot,
    build_output_schema,
    ensure_snapshot_fresh,
    parse_snapshot_bytes,
    postvalidate_model_output,
    strict_json_loads,
)
from ollama_endpoint import EndpointConfigError, load_runtime_ollama_endpoint
import model_runtime_policy


OLLAMA_HOST: str | None = None
OLLAMA_PORT: int | None = None
OLLAMA_PATH = "/api/generate"
STRUCTURED_PROFILE = model_runtime_policy.get_profile("structured")
OLLAMA_TIMEOUT_SECONDS = STRUCTURED_PROFILE.request_timeout_seconds
MAX_MODEL_RESPONSE_BYTES = 1_048_576
ALLOWED_MODELS = {STRUCTURED_PROFILE.model, f"{STRUCTURED_PROFILE.model}:latest"}
HA_STATUS_TEXT = {
    "not_configured": "не настроен",
    "dns_failure": "ошибка разрешения имени",
    "host_unreachable": "хост недоступен",
    "port_closed": "порт закрыт",
    "unauthorized": "ошибка авторизации",
    "api_unavailable": "API недоступен",
    "stale_data": "API доступен; часть сущностей недоступна или скрыта фильтром",
    "healthy": "доступен",
}


def build_model_payload(
    snapshot: Mapping[str, Any],
    analysis: Analysis,
    model: str,
) -> dict[str, Any]:
    if model not in ALLOWED_MODELS:
        raise HealthReportError("model is not allowlisted")
    facts = [
        {"id": item.identifier, "value": item.model_value}
        for item in analysis.facts
    ]
    required = {
        "status": analysis.status,
        "fact_ids": [item.identifier for item in analysis.facts],
        "problem_ids": [item.identifier for item in analysis.problems],
        "missing_ids": [item.identifier for item in analysis.missing],
    }
    model_input = {
        "schema_version": 1,
        "observed_at": snapshot["observed_at"],
        "facts": facts,
        "required_output": required,
    }
    prompt = (
        "Сформируй структурированный health-отчёт. Верни только JSON по заданной "
        "схеме. Скопируй status и все ID из required_output ровно по одному разу; "
        "не добавляй и не пропускай ID. Свободный текст запрещён.\n"
        + json.dumps(model_input, ensure_ascii=False, separators=(",", ":"))
    )
    return model_runtime_policy.build_generate_payload(
        "structured",
        prompt,
        response_format=build_output_schema(analysis),
    )


ConnectionFactory = Callable[..., http.client.HTTPConnection]


def _ollama_target() -> tuple[str, int]:
    if OLLAMA_HOST is not None or OLLAMA_PORT is not None:
        if OLLAMA_HOST is None or OLLAMA_PORT is None:
            raise HealthReportError("local model endpoint override is incomplete")
        return OLLAMA_HOST, OLLAMA_PORT
    try:
        endpoint = load_runtime_ollama_endpoint()
    except EndpointConfigError as error:
        raise HealthReportError("local model endpoint guard failed") from error
    return endpoint.host, endpoint.port


def _close_quietly(
    connection: http.client.HTTPConnection,
    connection_socket: socket.socket | None = None,
) -> None:
    connection_socket = connection_socket or getattr(connection, "sock", None)
    if connection_socket is not None:
        try:
            connection_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        connection.close()
    except (OSError, TimeoutError, http.client.HTTPException):
        pass


def _abort_request(
    connection: http.client.HTTPConnection,
    socket_holder: list[socket.socket | None],
) -> None:
    _close_quietly(connection, socket_holder[0])


def call_ollama(
    payload: Mapping[str, Any],
    *,
    connection_factory: ConnectionFactory = http.client.HTTPConnection,
) -> Any:
    host, port = _ollama_target()
    try:
        connection = connection_factory(
            host,
            port,
            timeout=OLLAMA_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise HealthReportError("local model request failed") from error
    socket_holder: list[socket.socket | None] = [None]
    deadline_timer = threading.Timer(
        OLLAMA_TIMEOUT_SECONDS,
        _abort_request,
        args=(connection, socket_holder),
    )
    deadline_timer.daemon = True
    deadline_timer.start()
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        connection.request(
            "POST",
            OLLAMA_PATH,
            body=body,
            headers={"Content-Type": "application/json"},
        )
        socket_holder[0] = getattr(connection, "sock", None)
        response = connection.getresponse()
        raw = response.read(MAX_MODEL_RESPONSE_BYTES + 1)
        if response.status != 200:
            raise HealthReportError("local model request failed")
        if len(raw) > MAX_MODEL_RESPONSE_BYTES:
            raise HealthReportError("local model response is too large")
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise HealthReportError("local model request failed") from error
    finally:
        deadline_timer.cancel()
        _close_quietly(connection, socket_holder[0])

    try:
        envelope_text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HealthReportError("local model response is not UTF-8") from error
    envelope = strict_json_loads(envelope_text)
    if not isinstance(envelope, dict):
        raise HealthReportError("local model response has an invalid envelope")
    requested_model = payload.get("model")
    accepted_models = {requested_model}
    if isinstance(requested_model, str) and ":" not in requested_model:
        accepted_models.add(f"{requested_model}:latest")
    if envelope.get("model") not in accepted_models:
        raise HealthReportError("local model response came from an unexpected model")
    if envelope.get("done") is not True or envelope.get("done_reason") != "stop":
        raise HealthReportError("local model response is incomplete")
    inner = envelope.get("response")
    if (
        not isinstance(inner, str)
        or not inner
        or len(inner.encode("utf-8")) > MAX_MODEL_RESPONSE_BYTES
    ):
        raise HealthReportError("local model response has an invalid envelope")
    return strict_json_loads(inner)


def _percent(value: int | float) -> str:
    return f"{float(value):.1f}%"


def _gib(value: int) -> str:
    return f"{value / (1024 ** 3):.1f} GiB"


def _problem_text(problem: Finding, snapshot: Mapping[str, Any]) -> str:
    code = problem.code
    details = problem.details
    if code == "host.memory_pressure":
        fact = f"RAM использована на {_percent(snapshot['host']['memory_used_percent'])}"
        unknown = "неизвестно, является ли давление устойчивым"
        next_check = "повторить read-only health-check"
        severity = "высокая"
    elif code == "host.swap_pressure":
        fact = f"swap использован на {_percent(snapshot['host']['swap_used_percent'])}"
        unknown = "неизвестна длительность нагрузки"
        next_check = "повторить read-only проверку RAM и swap"
        severity = "средняя"
    elif code.startswith("disk.") and code.endswith(".usage_high"):
        disk = snapshot["disks"][details["index"]]
        fact = (
            f"диск {disk['filesystem']} использован на "
            f"{_percent(disk['used_percent'])}"
        )
        unknown = "неизвестна скорость дальнейшего заполнения"
        next_check = "повторить read-only проверку свободного места"
        severity = "высокая"
    elif code == "disks.probe_empty":
        fact = "список локальных дисков пуст"
        unknown = "свободное место на дисках не подтверждено"
        next_check = "повторить read-only проверку локальных файловых систем"
        severity = "высокая"
    elif code.startswith("temperature.") and code.endswith(".high"):
        temperature = snapshot["temperatures"][details["index"]]
        fact = (
            f"датчик температуры {details['index'] + 1} показывает "
            f"{float(temperature['celsius']):.1f} °C"
        )
        unknown = "конкретный аппаратный предел датчика неизвестен"
        next_check = "сверить read-only показание с лимитом этого компонента"
        severity = "высокая"
    elif code.startswith("temperature.") and code.endswith(".implausible"):
        temperature = snapshot["temperatures"][details["index"]]
        fact = f"датчик температуры {details['index'] + 1} показывает {float(temperature['celsius']):.1f} °C"
        unknown = "показание физически неправдоподобно для домашнего оборудования"
        next_check = "повторить read-only чтение этого датчика"
        severity = "высокая"
    elif code == "temperatures.probe_failed":
        fact = "проверка температур завершилась ошибкой"
        unknown = "температуры компонентов неизвестны"
        next_check = "проверить доступность sensors в read-only режиме"
        severity = "средняя"
    elif code == "systemd.probe_failed":
        fact = f"статус проверки systemd: {snapshot['probes']['systemd']}"
        unknown = "наличие failed units не подтверждено"
        next_check = "повторить read-only проверку failed systemd units"
        severity = "высокая"
    elif code.startswith("systemd.failed_unit."):
        unit = snapshot["failed_systemd_units"][details["index"]]
        fact = f"systemd unit {unit} находится в failed"
        unknown = "причина сбоя не исследована"
        next_check = f"прочитать status и ограниченный журнал unit {unit}"
        severity = "высокая"
    elif code == "ollama.unreachable":
        fact = "локальный Ollama недоступен"
        unknown = "причина недоступности не установлена"
        next_check = "повторить read-only проверку локального endpoint Ollama"
        severity = "высокая"
    elif code == "ollama.version_probe_failed":
        fact = f"ответ Ollama version имеет статус {snapshot['probes']['ollama_version']}"
        unknown = "версия Ollama не подтверждена"
        next_check = "повторить ограниченную read-only проверку /api/version"
        severity = "высокая"
    elif code == "ollama.models_probe_failed":
        fact = f"ответ Ollama models имеет статус {snapshot['probes']['ollama_models']}"
        unknown = "список загруженных моделей не подтверждён"
        next_check = "повторить ограниченную read-only проверку /api/ps"
        severity = "средняя"
    elif code == "hermes.not_installed":
        fact = "Hermes не обнаружен разрешёнными проверками"
        unknown = "неизвестно, был ли изменён путь установки"
        next_check = "повторить read-only проверку установки Hermes"
        severity = "высокая"
    elif code == "hermes.gateway_stopped":
        fact = "Hermes gateway настроен, но не активен"
        unknown = "причина остановки не исследована"
        next_check = "прочитать status пользовательского сервиса"
        severity = "высокая"
    elif code == "hermes.gateway_probe_failed":
        fact = "проверка состояния Hermes gateway завершилась ошибкой"
        unknown = "состояние gateway не подтверждено"
        next_check = "повторить read-only проверку пользовательского сервиса"
        severity = "высокая"
    elif code == "home_assistant.unhealthy":
        fact = f"Home Assistant: {HA_STATUS_TEXT[snapshot['home_assistant']['status']]}"
        if snapshot["home_assistant"]["status"] == "stale_data":
            unknown = "причина недоступности отдельных сущностей определяется журналом инцидентов"
            next_check = "прочитать очищенную сводку /инциденты"
            severity = "средняя"
        else:
            unknown = "причина недоступности не исследована"
            next_check = "выполнить разрешённую read-only диагностику Home Assistant"
            severity = "высокая"
    else:
        raise HealthReportError("unknown trusted problem code")
    return (
        f"- Серьёзность: {severity}. Факт: {fact}. "
        f"Неизвестно: {unknown}. Следующая безопасная проверка: {next_check}. "
        f"Источник: входной снимок; наблюдение: {snapshot['observed_at']}."
    )


def _missing_text(missing: Finding, snapshot: Mapping[str, Any]) -> str:
    if missing.code == "disks.unavailable":
        return "данные о локальных дисках отсутствуют"
    if missing.code == "temperatures.unavailable":
        status = snapshot["probes"]["temperatures"]
        return f"показания температуры отсутствуют (статус проверки: {status})"
    if missing.code == "systemd.failed_units_unavailable":
        status = snapshot["probes"]["systemd"]
        return f"список failed systemd units не подтверждён (статус: {status})"
    if missing.code == "ollama.version_unavailable":
        status = snapshot["probes"]["ollama_version"]
        return f"версия Ollama не подтверждена (статус: {status})"
    if missing.code == "ollama.models_unavailable":
        status = snapshot["probes"]["ollama_models"]
        return f"загруженные модели Ollama не подтверждены (статус: {status})"
    if missing.code == "hermes.gateway_state_unavailable":
        status = snapshot["probes"]["hermes_gateway"]
        return f"состояние Hermes gateway не подтверждено (статус: {status})"
    raise HealthReportError("unknown trusted missing-data code")


def render_report(
    snapshot: Mapping[str, Any],
    analysis: Analysis,
    selection: Mapping[str, Any] | None,
    *,
    model_failure: bool = False,
) -> str:
    if not model_failure:
        if selection is None:
            raise HealthReportError("validated model selection is required")
        postvalidate_model_output(selection, analysis)

    has_attention = bool(analysis.problems) or model_failure
    lines = [
        "ТРЕБУЕТСЯ ВНИМАНИЕ" if has_attention else "HEARTBEAT_OK",
        f"Наблюдение: {snapshot['observed_at']} (источник: входной снимок).",
        (
            "Хост: CPU (моментальный 0,2-секундный образец) "
            f"{_percent(snapshot['host']['cpu_load_percent'])}, RAM "
            f"{_percent(snapshot['host']['memory_used_percent'])}, swap "
            f"{_percent(snapshot['host']['swap_used_percent'])}."
        ),
    ]

    if snapshot["disks"]:
        lines.append("Диски:")
        for disk in snapshot["disks"]:
            lines.append(
                f"- {disk['filesystem']} ({disk['type']}): свободно "
                f"{_gib(disk['available_bytes'])} из {_gib(disk['total_bytes'])}; "
                f"использовано {_percent(disk['used_percent'])}."
            )
    else:
        lines.append("Диски: данные отсутствуют.")

    if snapshot["temperatures"]:
        lines.append("Температуры:")
        for index, temperature in enumerate(snapshot["temperatures"], start=1):
            lines.append(
                f"- Датчик {index}: {float(temperature['celsius']):.1f} °C."
            )
    else:
        lines.append("Температуры: показания в снимке отсутствуют.")

    failed_units = snapshot["failed_systemd_units"]
    if failed_units:
        lines.append("Systemd: failed units: " + ", ".join(failed_units) + ".")
    elif snapshot["probes"]["systemd"] == "ok":
        lines.append("Systemd: failed units отсутствуют.")
    else:
        lines.append(
            f"Systemd: проверка не подтверждена ({snapshot['probes']['systemd']})."
        )

    ollama = snapshot["ollama"]
    if ollama["reachable"]:
        version = ollama["version"] or "не подтверждена"
        if snapshot["probes"]["ollama_models"] != "ok":
            load_state = "состояние загруженных моделей не подтверждено"
        elif ollama["model_loaded"]:
            load_state = f"загружено моделей: {len(ollama['loaded_models'])}"
        else:
            load_state = "активная модель не загружена"
        lines.append(f"Ollama: доступен, версия {version}; {load_state}.")
    else:
        lines.append("Ollama: недоступен.")

    hermes_text = {
        "not_installed": "не установлен",
        "not_configured": "установлен; gateway не настроен",
        "running": "gateway работает",
        "stopped": "gateway настроен, но не работает",
        "unknown": "установлен; состояние gateway не подтверждено",
    }[snapshot["hermes"]["status"]]
    lines.append(f"Hermes: {hermes_text}.")

    ha = snapshot["home_assistant"]
    ha_text = HA_STATUS_TEXT[ha["status"]]
    lines.append(f"Home Assistant: {ha_text}.")

    if analysis.problems or model_failure:
        lines.append("Подтверждённые проблемы:")
        for problem in analysis.problems:
            lines.append(_problem_text(problem, snapshot))
        if model_failure:
            failure_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
            lines.append(
                "- Серьёзность: высокая. Факт: модельный этап отчёта не прошёл "
                "строгую проверку. Неизвестно: причина отказа не раскрывается, "
                "чтобы не выводить недоверенные данные. Следующая безопасная "
                "проверка: повторить локальный structured-output тест. "
                "Источник: локальный модельный этап; наблюдение отказа: "
                f"{failure_time}."
            )
    else:
        lines.append("Проблемы: в выполненных проверках не обнаружены.")

    if analysis.missing:
        lines.append("Недостающие данные:")
        for item in analysis.missing:
            lines.append(f"- {_missing_text(item, snapshot)}.")

    lines.append("Изменения и команды восстановления не выполнялись.")
    return "\n".join(lines)


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a fail-closed local health report.",
    )
    parser.add_argument(
        "--model",
        choices=sorted(ALLOWED_MODELS),
        default=STRUCTURED_PROFILE.model,
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        snapshot = parse_snapshot_bytes(raw)
        ensure_snapshot_fresh(snapshot)
        analysis = analyze_snapshot(snapshot)
    except HealthReportError:
        print("health-report: входной снимок отклонён", file=sys.stderr)
        return 2

    if not snapshot["ollama"]["reachable"]:
        try:
            ensure_snapshot_fresh(snapshot)
            print(render_report(
                snapshot,
                analysis,
                None,
                model_failure=True,
            ))
            return 3
        except HealthReportError:
            print("health-report: входной снимок больше не актуален", file=sys.stderr)
            return 2

    try:
        payload = build_model_payload(snapshot, analysis, args.model)
        model_output = call_ollama(payload)
        selection = postvalidate_model_output(model_output, analysis)
        ensure_snapshot_fresh(snapshot)
        report = render_report(snapshot, analysis, selection)
    except HealthReportError:
        try:
            ensure_snapshot_fresh(snapshot)
            print(render_report(
                snapshot,
                analysis,
                None,
                model_failure=True,
            ))
            return 3
        except HealthReportError:
            print("health-report: входной снимок больше не актуален", file=sys.stderr)
            return 2
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
