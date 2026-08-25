#!/usr/bin/env python3
"""Regression checks for the Phase 66 repository/runtime boundary."""

from __future__ import annotations

import fnmatch
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import repository_manifest  # noqa: E402


FORBIDDEN_TRACKED_RUNTIME = (
    "hermes/.hermes_history",
    "hermes/.mcp-discovery.lock",
    "hermes/.update_check",
    "hermes/auth.lock",
    "hermes/cache/*",
    "hermes/context_length_cache.yaml",
    "hermes/logs/*",
    "hermes/models_dev_cache.json",
    "hermes/ollama_cloud_models_cache.json",
    "hermes/skills/.curator_state",
    "hermes/skills/.hub/*",
    "hermes/skills/.usage.json",
    "hermes/skills/.usage.json.lock",
    "hermes/state.db",
    "hermes/state.db-shm",
    "hermes/state.db-wal",
)


class RepositoryHygieneTests(unittest.TestCase):
    def test_every_repository_file_has_a_manifest_category_and_fingerprint(self) -> None:
        rows = repository_manifest.build_manifest(PROJECT_DIR)
        repository_paths = repository_manifest.repository_paths(PROJECT_DIR)
        self.assertEqual([row[0] for row in rows], repository_paths)
        self.assertTrue(rows)
        for _, categories, size, kind, digest in rows:
            self.assertTrue(categories)
            self.assertGreaterEqual(size, 0)
            self.assertIn(kind, {"file", "symlink"})
            self.assertEqual(len(digest), 64)

    def test_runtime_databases_logs_caches_and_history_are_not_tracked(self) -> None:
        tracked = repository_manifest.tracked_paths(PROJECT_DIR)
        offenders = sorted(
            path
            for path in tracked
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in FORBIDDEN_TRACKED_RUNTIME)
        )
        self.assertEqual(offenders, [])

    def test_runtime_paths_are_ignored(self) -> None:
        probes = [pattern.replace("*", "probe") for pattern in FORBIDDEN_TRACKED_RUNTIME]
        result = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=PROJECT_DIR,
            input="\n".join(probes) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(sorted(result.stdout.splitlines()), sorted(probes))


if __name__ == "__main__":
    unittest.main()
