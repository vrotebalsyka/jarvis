#!/usr/bin/env python3
"""Independent real-home room/type closeout over the production shadow path."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASE_RUNNER = ROOT / "tests" / "live_stage72_real_home_acceptance.py"
EXPECTED_SHA256 = "4182f1a75a494effc607440810b5c05c218e8b8822df3a4e9e62fd1b58088b2d"
REQUIRED_FIELDS = frozenset({
    "case_id", "category", "utterance", "expected_outcome",
    "expected_human_target", "expected_area", "expected_domain", "expected_action",
})
EXPECTED_CATEGORIES = Counter({"room_type_plan": 30, "capability_ambiguity": 12})


def _load_base_runner() -> Any:
    spec = importlib.util.spec_from_file_location("stage72_real_home_base", BASE_RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError("base acceptance runner is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_owner_manifest(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Load frozen human expectations without consulting production resolution."""

    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
    digest = hashlib.sha256(raw).hexdigest()
    plan_rows = [row for row in rows if row.get("expected_outcome") == "plan"]
    target_counts = Counter(row.get("expected_human_target") for row in plan_rows)
    if (
        digest != EXPECTED_SHA256 or len(rows) != 42
        or Counter(row.get("category") for row in rows) != EXPECTED_CATEGORIES
        or len({row.get("case_id") for row in rows}) != len(rows)
        or any(not REQUIRED_FIELDS <= set(row) for row in rows)
        or len(plan_rows) != 30 or len(target_counts) != 10
        or set(target_counts.values()) != {3}
        or any(row.get("expected_outcome") != "clarification" for row in rows[30:])
    ):
        raise ValueError("room/type owner-reviewed manifest contract failed")
    production = "\n".join(
        source.read_text(encoding="utf-8") for source in (ROOT / "scripts").glob("*.py")
    ).casefold()
    if any(str(row["utterance"]).casefold() in production for row in rows):
        raise ValueError("owner utterance was hardcoded in production")
    return rows, digest


def run(manifest_path: Path, inventory_path: Path) -> dict[str, Any]:
    manifest, digest = load_owner_manifest(manifest_path)
    base = _load_base_runner()
    original_loader = base.load_owner_manifest
    base.load_owner_manifest = lambda _path: (manifest, digest)
    try:
        report = base.run(manifest_path, inventory_path)
    finally:
        base.load_owner_manifest = original_loader

    cases = {case["case_id"]: case for case in report["cases"]}
    expected_plans = [row for row in manifest if row["expected_outcome"] == "plan"]
    real_targets_tested = len({row["expected_human_target"] for row in expected_plans})
    real_room_type_plans = sum(
        bool(cases[row["case_id"]]["pass"])
        and cases[row["case_id"]]["actual_outcome"] == "plan"
        and bool(cases[row["case_id"]]["requested_areas"])
        and bool(cases[row["case_id"]]["requested_types"])
        for row in expected_plans
    )
    real_clarifications = sum(
        case["actual_outcome"] == "clarification" for case in report["cases"]
    )
    real_no_plans = sum(
        case["actual_outcome"] == "no_plan" for case in report["cases"]
    )
    closeout_pass = (
        report["status"] == "pass" and report["owner_reviewed_passed"] == len(manifest)
        and real_targets_tested >= 10 and real_room_type_plans >= 20
        and report["wrong_target"] == report["cross_room_target"]
        == report["ambiguous_plan"] == report["forbidden_plan"]
        == report["missed_expected_plan"] == report["wrong_action"]
        == report["false_action_intent"] == report["ha_post"]
        == report["ha_service_paths"] == report["service_calls"] == 0
        and report["latency"]["p95_s"] <= 2.5
    )
    report.update({
        "status": "pass" if closeout_pass else "fail",
        "REAL_TARGETS_TESTED": real_targets_tested,
        "REAL_ROOM_TYPE_PLANS": real_room_type_plans,
        "REAL_CLARIFICATIONS": real_clarifications,
        "REAL_NO_PLANS": real_no_plans,
    })
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        report = run(arguments.manifest, arguments.inventory)
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if arguments.output is not None:
        arguments.output.write_text(raw, encoding="utf-8")
    print(json.dumps(
        {key: value for key, value in report.items() if key != "cases"},
        ensure_ascii=False, separators=(",", ":"),
    ))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
