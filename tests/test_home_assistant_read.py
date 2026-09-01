from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import home_assistant_read as adapter


class Response:
    status = 200
    def __init__(self, body: object) -> None:
        self.body = json.dumps(body).encode()
    def read(self, _limit: int) -> bytes:
        return self.body


class Connection:
    def __init__(self, body: object) -> None:
        self.body, self.method, self.path = body, None, None
    def request(self, method: str, path: str, **_kwargs: object) -> None:
        self.method, self.path = method, path
    def getresponse(self) -> Response:
        return Response(self.body)
    def close(self) -> None:
        pass


class ReadAdapterTests(unittest.TestCase):
    def config(self) -> adapter.AdapterConfig:
        return adapter.AdapterConfig("http", "192.168.1.127", 8123, "x" * 32, (), True)

    def test_only_exact_get_paths_are_allowed(self) -> None:
        connection = Connection({"message": "API running"})
        result = adapter.request_json(self.config(), "/api/", connection_factory=lambda _c: connection)
        self.assertEqual(result["message"], "API running")
        self.assertEqual((connection.method, connection.path), ("GET", "/api/"))
        for path in ("/api/services", "/api/states/light.test", "/api/history"):
            with self.assertRaises(adapter.AdapterError):
                adapter.request_json(self.config(), path, connection_factory=lambda _c: connection)

    def test_state_sanitization_keeps_number_and_no_attributes(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        result = adapter._state("sensor.fixture", {
            "entity_id": "sensor.fixture", "state": "48.5", "last_updated": now,
            "attributes": {"token": "secret"},
        }, datetime.now(timezone.utc))
        self.assertEqual(result["state_value"], 48.5)
        self.assertNotIn("attributes", result)

    def test_duplicate_json_keys_fail_closed(self) -> None:
        with self.assertRaises(adapter.AdapterError):
            adapter.strict_json_loads(b'{"a":1,"a":2}')


if __name__ == "__main__":
    unittest.main()
