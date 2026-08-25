#!/usr/bin/env python3
"""Safety tests for the persistent Alice Tailscale Funnel helper."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_DIR / "scripts" / "alice_tailscale_funnel.py"
SPEC = importlib.util.spec_from_file_location("alice_tailscale_funnel", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
funnel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = funnel
SPEC.loader.exec_module(funnel)


def status_document(**overrides: object) -> bytes:
    document: dict[str, object] = {
        "BackendState": "Running",
        "Self": {
            "DNSName": "home-butler.example-tail.ts.net.",
            "Online": True,
        },
    }
    document.update(overrides)
    return json.dumps(document).encode("utf-8")


def funnel_document() -> bytes:
    authority = "home-butler.example-tail.ts.net:443"
    return json.dumps(
        {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                authority: {
                    "Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:8765"},
                    }
                }
            },
            "AllowFunnel": {authority: True},
        }
    ).encode("utf-8")


class AliceTailscaleFunnelTests(unittest.TestCase):
    def test_origin_accepts_only_online_ts_net_node(self) -> None:
        self.assertEqual(
            funnel.parse_origin(status_document()),
            "https://home-butler.example-tail.ts.net",
        )
        with self.assertRaises(funnel.FunnelError):
            funnel.parse_origin(status_document(BackendState="NeedsLogin"))
        with self.assertRaises(funnel.FunnelError):
            funnel.parse_origin(
                status_document(Self={"DNSName": "attacker.invalid.", "Online": True})
            )

    def test_ensure_uses_one_pinned_loopback_target_and_exact_status(self) -> None:
        calls: list[tuple[str, ...]] = []
        probes: list[str] = []

        def runner(arguments: object) -> bytes:
            call = tuple(arguments)
            calls.append(call)
            if call == ("status", "--json"):
                return status_document()
            if call == ("funnel", "status", "--json"):
                return funnel_document()
            return b""

        self.assertEqual(
            funnel.ensure_funnel(
                runner,
                lambda: probes.append("local"),
                lambda origin: probes.append(f"public:{origin}"),
            ),
            "https://home-butler.example-tail.ts.net",
        )
        self.assertEqual(
            calls,
            [
                ("status", "--json"),
                ("funnel", "status", "--json"),
            ],
        )
        self.assertEqual(probes, [
            "local", "public:https://home-butler.example-tail.ts.net",
        ])

    def test_inspect_is_read_only_and_requires_the_exact_route(self) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(arguments: object) -> bytes:
            call = tuple(arguments)
            calls.append(call)
            if call == ("status", "--json"):
                return status_document()
            if call == ("funnel", "status", "--json"):
                return funnel_document()
            raise AssertionError(call)

        self.assertEqual(
            funnel.inspect_funnel(runner),
            "https://home-butler.example-tail.ts.net",
        )
        self.assertEqual(
            calls,
            [("status", "--json"), ("funnel", "status", "--json")],
        )

    def test_rejects_extra_or_changed_funnel_routes(self) -> None:
        origin = "https://home-butler.example-tail.ts.net"
        funnel.validate_funnel_status(funnel_document(), origin)
        changed = json.loads(funnel_document())
        changed["Web"]["attacker.example-tail.ts.net:443"] = {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}
        }
        with self.assertRaises(funnel.FunnelError):
            funnel.validate_funnel_status(json.dumps(changed).encode("utf-8"), origin)

    def test_ensure_waits_for_the_local_model_gateway_to_finish_warming(self) -> None:
        attempts = []
        moments = iter((0.0, 0.0, 1.0, 1.0))

        def runner(arguments: object) -> bytes:
            call = tuple(arguments)
            if call == ("status", "--json"):
                return status_document()
            if call == ("funnel", "status", "--json"):
                return funnel_document()
            return b""

        def probe() -> None:
            attempts.append("probe")
            if len(attempts) == 1:
                raise funnel.FunnelError("gateway warming")

        funnel.ensure_funnel(
            runner,
            probe,
            lambda _origin: None,
            wait_seconds=5,
            clock=lambda: next(moments),
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(attempts, ["probe", "probe"])

    def test_ensure_retries_public_tls_without_resetting_funnel(self) -> None:
        calls: list[tuple[str, ...]] = []
        local_probes: list[str] = []
        public_probes: list[str] = []

        def runner(arguments: object) -> bytes:
            call = tuple(arguments)
            calls.append(call)
            if call == ("status", "--json"):
                return status_document()
            if call == ("funnel", "status", "--json"):
                return funnel_document()
            return b""

        def public_probe(origin: str) -> None:
            public_probes.append(origin)
            if len(public_probes) == 1:
                raise funnel.FunnelError("stale tls")

        funnel.ensure_funnel(
            runner,
            lambda: local_probes.append("local"),
            public_probe,
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(calls.count(("funnel", "reset")), 0)
        self.assertEqual(
            calls.count((
                "funnel", "--bg", "--yes", "http://127.0.0.1:8765",
            )),
            0,
        )
        self.assertEqual(local_probes, ["local"])
        self.assertEqual(len(public_probes), 2)

    def test_missing_route_is_configured_once(self) -> None:
        calls: list[tuple[str, ...]] = []
        status_reads = 0

        def runner(arguments: object) -> bytes:
            nonlocal status_reads
            call = tuple(arguments)
            calls.append(call)
            if call == ("status", "--json"):
                return status_document()
            if call == ("funnel", "status", "--json"):
                status_reads += 1
                return b"{}" if status_reads == 1 else funnel_document()
            return b""

        funnel.ensure_funnel(
            runner,
            lambda: None,
            lambda _origin: None,
        )
        self.assertEqual(calls.count((
            "funnel", "--bg", "--yes", "http://127.0.0.1:8765",
        )), 1)

    def test_invalid_packet_filter_has_a_machine_readable_code(self) -> None:
        raw = status_document(Health=[
            "The coordination server sent an invalid packet filter permitting "
            "traffic to unlocked nodes; rejecting all packets for safety"
        ])
        with self.assertRaises(funnel.FunnelError) as caught:
            funnel.parse_origin(raw)
        self.assertEqual(caught.exception.code, "invalid_packet_filter")

    def test_origin_file_is_atomic_root_private(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("root metadata contract")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            target = directory / "alice-public-origin.txt"
            funnel.write_origin(target, "https://home-butler.example-tail.ts.net")
            metadata = target.stat(follow_symlinks=False)
            self.assertEqual(metadata.st_mode & 0o777, 0o600)
            self.assertEqual(metadata.st_uid, 0)
            self.assertEqual(
                target.read_text(encoding="ascii"),
                "https://home-butler.example-tail.ts.net\n",
            )


if __name__ == "__main__":
    unittest.main()
