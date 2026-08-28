from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_training_dataset as export_training  # noqa: E402


def _example(expected: str, *, rejected: bool = False) -> bytes:
    item = {
        "id": "stage68-example",
        "source_device": "a" * 24,
        "source_snapshot_hash": "b" * 64,
        "category": "READ",
        "input": "Что с устройством?",
        "context": {"facts": {}},
        "expected": expected,
        "evidence": [],
        "validator_version": "stage68-v1",
        "teacher_model": "qwen3.5:4b-q4_K_M",
        "created_at": "2026-08-27T00:00:00+00:00",
    }
    if rejected:
        item["rejection_reasons"] = ["entity_id_exposed"]
    return (json.dumps(item, ensure_ascii=False) + "\n").encode()


class ExportTrainingDatasetTests(unittest.TestCase):
    def test_validated_output_rejects_entity_id(self) -> None:
        with self.assertRaises(export_training.ExportError):
            export_training._parse_jsonl(
                _example("Датчик sensor.private_entity недоступен."),
                validated=True,
            )

    def test_rejected_corpus_may_preserve_reason_evidence(self) -> None:
        parsed = export_training._parse_jsonl(
            _example("Датчик sensor.private_entity недоступен.", rejected=True),
            validated=False,
        )
        self.assertEqual(parsed[0]["rejection_reasons"], ["entity_id_exposed"])

    def test_validated_corpus_cannot_contain_rejection_reasons(self) -> None:
        with self.assertRaises(export_training.ExportError):
            export_training._parse_jsonl(
                _example("Причина не подтверждена.", rejected=True),
                validated=True,
            )

    def test_private_network_data_is_rejected_everywhere(self) -> None:
        with self.assertRaises(export_training.ExportError):
            export_training._parse_jsonl(
                _example("Устройство доступно по 192.168.1.2."),
                validated=False,
            )


if __name__ == "__main__":
    unittest.main()
