#!/usr/bin/env python3
"""Run a frozen 25-phrase review draft through production, with HA writes blocked."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

import live_stage73_shadow_capability as shadow

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ("b4fce23a0462ba0c23626ac63cd506c368ede500003a8ad5807a9a0007e01f06", 25)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-replay", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--html-output", type=Path, required=True)
    args = parser.parse_args()
    manifest = ROOT / "tests/data/stage73_owner_review_25.jsonl"
    result = shadow.run(manifest, args.token_file, inventory_replay=args.inventory_replay, frozen_contract=FROZEN)
    result["owner_reviewed"] = False
    result["live_authorized"] = False
    rows = [json.loads(line) for line in manifest.read_text().splitlines() if line]
    owners = {row["case_id"]: row["owner_room"] for row in rows}
    content = ["<!doctype html><html lang=ru><meta charset=utf-8><title>Stage73 — owner review</title>",
        "<style>body{font:16px system-ui;margin:32px;max-width:1300px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #aaa;padding:9px;text-align:left}th{background:#eee}small{color:#555}</style>",
        "<h1>25 фраз: черновик для проверки владельцем</h1>",
        "<p>Это не разрешение на live. Registry rooms не изменялись. Plan означает только shadow-план, не выполненное действие.</p>",
        "<p>Источник: " + ("metadata snapshot, НЕ свежая HA-приёмка" if args.inventory_replay else "fresh HA read-only") + ".</p>",
        "<p>Комната владельца и комната, найденная production path, показаны раздельно. У пяти выбранных сущностей registry area отсутствует.</p>",
        "<table><thead><tr><th>Фраза</th><th>Какое устройство понял</th><th>Комната в production / владелец</th><th>Действие</th><th>Результат</th></tr></thead><tbody>"]
    for case in result["cases"]:
        candidates = list(dict.fromkeys(c.get("label", "") for c in case["candidates"] if c.get("label")))
        understood = case["selected_human_target"] or ("Уточнение: " + "; ".join(candidates)
                                                       if candidates else "Однозначная цель не выбрана")
        rooms = ", ".join(case["selected_areas"]) or "не определена"
        action = case["selected_action"] or case["intent"].get("action")
        cells = (case["utterance"], understood, f"{rooms} / владелец: {owners[case['case_id']]}",
                 {"turn_on": "включить", "turn_off": "выключить"}.get(action, "не определено"), case["actual_outcome"])
        content.append("<tr>" + "".join("<td>" + html.escape(str(value)) + "</td>" for value in cells) + "</tr>")
    content.append("</tbody></table><p>У двух разных physical устройств одинаковое имя «реле вентилятора»: автоматический выбор запрещён. Подтверждение tuya_local для allowlist не устраняет неоднозначность короткой команды. Короткая «вытяжка» тоже неоднозначна: в графе есть «Вытяжка на кухне» и другой physical target «вытяжка». Ожидания draft не переписывались после ответов.</p></html>")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.html_output.write_text("\n".join(content), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
