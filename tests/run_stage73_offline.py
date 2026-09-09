#!/usr/bin/env python3
"""Measured fake-endpoint evidence. Explicitly NOT the real-home Phase A gate."""

from __future__ import annotations

import json
import sys
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"), str(Path(__file__).resolve().parent)]
import canary_write_adapter as adapter
import test_stage73_canary_security as suite


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 4)


def run() -> dict:
    counts = Counter({"FAKE_HA_POST": 0, "FAKE_HA_GET": 0, "HA_POST": 0, "SERVICE_CALLS": 0})
    statuses = Counter()
    durations: dict[str, list[float]] = {name: [] for name in ("post", "verification", "verified_total")}
    execute = adapter.CanaryWriteAdapter.execute

    def measured(instance, plan):
        started = time.perf_counter()
        result = execute(instance, plan)
        statuses[result.status] += 1
        if result.status == "verified" and result.service_calls == 1 and not result.replayed:
            durations["post"].append(result.post_s)
            durations["verification"].append(result.verification_s)
            durations["verified_total"].append(time.perf_counter() - started)
        return result

    class Results(unittest.TextTestResult):
        def stopTest(self, test):
            counts["FAKE_HA_POST"] += len(test.fake.posts)
            counts["FAKE_HA_GET"] += test.fake.reads
            counts.update(test.network)
            super().stopTest(test)

    with patch.object(adapter.CanaryWriteAdapter, "execute", measured):
        result = unittest.TextTestRunner(verbosity=1, resultclass=Results).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(suite.CanarySecurityTests))
    return {"status": "pass" if result.wasSuccessful() else "fail", "scope": "offline_fake_only",
            "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "network": dict(counts), "receipt_statuses_including_injected_faults": dict(statuses),
            "latency_fake_only": {name: {"n": len(values), "p50_s": percentile(values, .50),
                                            "p95_s": percentile(values, .95), "p99_s": percentile(values, .99)}
                                  for name, values in durations.items()},
            "not_real_home_acceptance": True}


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(0 if report["status"] == "pass" else 1)
