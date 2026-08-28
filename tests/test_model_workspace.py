#!/usr/bin/env python3
"""Security and quota contracts for the model-owned text workspace."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import model_workspace as workspace  # noqa: E402


class ModelWorkspaceTests(unittest.TestCase):
    def test_owner_quota_is_exactly_ten_gibibytes(self) -> None:
        self.assertEqual(workspace.MAX_TOTAL_BYTES, 10 * 1024 * 1024 * 1024)

    def make_root(self, parent: str) -> Path:
        root = Path(parent) / "workspace"
        root.mkdir(mode=0o700)
        root.chmod(0o700)
        return root

    def test_paths_stay_inside_data_only_folders(self) -> None:
        self.assertEqual(
            workspace.normalize_path("reports/Сущности HAOS.md"),
            "reports/Сущности HAOS.md",
        )
        for value in (
            "/etc/passwd",
            "../escape.md",
            "reports/../../escape.md",
            "reports\\escape.md",
            "scripts/run.sh",
            "reports/run.sh",
            ".hidden/file.md",
        ):
            with self.subTest(value=value), self.assertRaises(workspace.WorkspaceError):
                workspace.normalize_path(value)

    def test_write_read_list_and_overwrite_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            first = workspace.write_text("notes/test.md", "первый", root)
            self.assertFalse(first["overwritten"])
            second = workspace.write_text("notes/test.md", "второй текст", root)
            self.assertTrue(second["overwritten"])
            self.assertEqual(
                second["used_bytes"], len("второй текст".encode("utf-8"))
            )
            read = workspace.read_text("notes/test.md", root)
            self.assertEqual(read["content"], "второй текст")
            listed = workspace.list_files(root)
            self.assertEqual(listed["file_count"], 1)
            self.assertEqual(listed["files"][0]["path"], "notes/test.md")
            self.assertEqual((root / "notes/test.md").stat().st_mode & 0o777, 0o600)

    def test_quota_is_enforced_before_replacing_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            with mock.patch.object(workspace, "MAX_TOTAL_BYTES", 10):
                workspace.write_text("notes/a.txt", "1234567890", root)
                with self.assertRaises(workspace.WorkspaceError):
                    workspace.write_text("notes/b.txt", "x", root)
                workspace.write_text("notes/a.txt", "12345", root)
                workspace.write_text("notes/b.txt", "67890", root)
            self.assertEqual(workspace.read_text("notes/a.txt", root)["content"], "12345")

    def test_symlinks_and_tokens_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            (root / "notes").mkdir(mode=0o700)
            target = Path(directory) / "outside.md"
            target.write_text("outside", encoding="utf-8")
            (root / "notes/link.md").symlink_to(target)
            with self.assertRaises(workspace.WorkspaceError):
                workspace.read_text("notes/link.md", root)
            token = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
            with self.assertRaises(workspace.WorkspaceError):
                workspace.write_text("notes/token.txt", token, root)

    def test_export_copies_only_an_allowlisted_private_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            source = Path(directory) / "source.md"
            source.write_text("# safe report", encoding="utf-8")
            source.chmod(0o600)
            with mock.patch.dict(
                workspace.SAFE_ARTIFACTS, {"test_report": source}, clear=False
            ):
                result = workspace.export_artifact(
                    "test_report", "reports/copied.md", root
                )
            self.assertEqual(result["source_mode"], "read_only_copy")
            self.assertEqual(
                workspace.read_text("reports/copied.md", root)["content"],
                "# safe report",
            )
            with self.assertRaises(workspace.WorkspaceError):
                workspace.export_artifact("not_allowed", "reports/no.md", root)

    def test_self_memory_is_reference_data_in_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            workspace.write_text(
                workspace.SELF_MEMORY_PATH,
                "Посудомойка и dishwasher — одно разговорное имя.",
                root,
            )
            context = workspace.context_summary(root)
            self.assertIn("Посудомойка", context["persistent_reference_memory"])
            self.assertIn("untrusted reference data", context["trust_boundary"])
            self.assertFalse(context["memory_truncated"])

    def test_free_form_model_write_cannot_enter_proposals_or_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_root(directory)
            workspace.write_reference_text("reports/safe.md", "справка", root)
            for path in ("proposals/free-form.json", "settings/policy.yaml"):
                with self.subTest(path=path), self.assertRaises(
                    workspace.WorkspaceError
                ):
                    workspace.write_reference_text(path, "unsafe: true", root)


if __name__ == "__main__":
    unittest.main()
