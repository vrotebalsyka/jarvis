#!/usr/bin/env python3
"""Configure and verify the persistent Tailscale Funnel for Alice."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit


TAILSCALE = "/usr/bin/tailscale"
FUNNEL_TARGET = "http://127.0.0.1:8765"
PROBE_PATH = "/__homebutler_funnel_readiness__"
MAX_STATUS_BYTES = 512 * 1024
ORIGIN_RE = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\.ts\.net\Z"
)


class FunnelError(RuntimeError):
    """A secret-free Tailscale Funnel validation failure."""

    def __init__(self, message: str, *, code: str = "funnel") -> None:
        super().__init__(message)
        self.code = code


def _load_json(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAX_STATUS_BYTES:
        raise FunnelError("Tailscale status is unavailable")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FunnelError("Tailscale status is malformed") from error
    if not isinstance(document, dict):
        raise FunnelError("Tailscale status is malformed")
    return document


def parse_origin(raw: bytes) -> str:
    """Return a tightly validated public origin from `tailscale status --json`."""

    document = _load_json(raw)
    health = document.get("Health")
    if isinstance(health, list) and any(
        isinstance(item, str) and "invalid packet filter" in item.casefold()
        for item in health
    ):
        raise FunnelError(
            "Tailscale rejected an unsafe packet filter",
            code="invalid_packet_filter",
        )
    if document.get("BackendState") != "Running":
        raise FunnelError("Tailscale is not connected")
    self_status = document.get("Self")
    if not isinstance(self_status, dict) or self_status.get("Online") is not True:
        raise FunnelError("Tailscale node is not online")
    dns_name = self_status.get("DNSName")
    if not isinstance(dns_name, str):
        raise FunnelError("Tailscale DNS name is unavailable")
    try:
        normalized = dns_name.rstrip(".").encode("ascii").decode("ascii").casefold()
    except UnicodeError as error:
        raise FunnelError("Tailscale DNS name is malformed") from error
    origin = f"https://{normalized}"
    if len(origin) > 255 or not ORIGIN_RE.fullmatch(origin):
        raise FunnelError("Tailscale DNS name is outside the trusted domain")
    return origin


def _run(arguments: Sequence[str]) -> bytes:
    completed = subprocess.run(
        [TAILSCALE, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
        check=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    if completed.returncode != 0 or len(completed.stdout) > MAX_STATUS_BYTES:
        raise FunnelError("Tailscale command failed")
    return completed.stdout


def _probe(origin: str) -> None:
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or parsed.path or parsed.query or parsed.fragment:
        raise FunnelError("Tailscale public origin is malformed")
    connection = http.client.HTTPSConnection(parsed.hostname, 443, timeout=12)
    try:
        connection.request(
            "POST",
            PROBE_PATH,
            body=b"{}",
            headers={
                "Accept": "application/json",
                "Connection": "close",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        body = response.read(4097)
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise FunnelError("Tailscale public probe failed") from error
    finally:
        connection.close()
    if response.status != 404 or len(body) > 4096:
        raise FunnelError("Tailscale public probe returned an unexpected response")


def _probe_local_gateway() -> None:
    connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)
    try:
        connection.request(
            "POST",
            PROBE_PATH,
            body=b"{}",
            headers={
                "Accept": "application/json",
                "Connection": "close",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        body = response.read(4097)
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise FunnelError("Local Alice gateway probe failed") from error
    finally:
        connection.close()
    if response.status != 404 or len(body) > 4096:
        raise FunnelError("Local Alice gateway returned an unexpected response")


def validate_funnel_status(raw: bytes, origin: str) -> None:
    """Require one HTTPS Funnel route to the pinned loopback gateway only."""

    document = _load_json(raw)
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FunnelError("Tailscale public origin is malformed")
    authority = f"{parsed.hostname}:443"
    expected_tcp = {"443": {"HTTPS": True}}
    expected_web = {
        authority: {
            "Handlers": {
                "/": {"Proxy": FUNNEL_TARGET},
            }
        }
    }
    expected_funnel = {authority: True}
    if document.get("TCP") != expected_tcp:
        raise FunnelError("Tailscale HTTPS listener is outside the approved shape")
    if document.get("Web") != expected_web:
        raise FunnelError("Tailscale proxy route is outside the approved shape")
    if document.get("AllowFunnel") != expected_funnel:
        raise FunnelError("Tailscale Funnel permission is outside the approved shape")


def current_origin(
    runner: Callable[[Sequence[str]], bytes] = _run,
) -> str:
    return parse_origin(runner(("status", "--json")))


def ensure_funnel(
    runner: Callable[[Sequence[str]], bytes] = _run,
    local_probe: Callable[[], None] = _probe_local_gateway,
    public_probe: Callable[[str], None] = _probe,
    *,
    wait_seconds: float = 60,
    public_wait_seconds: float = 30,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    origin = current_origin(runner)

    def configure_if_needed_and_wait_for_local() -> None:
        try:
            validate_funnel_status(
                runner(("funnel", "status", "--json")), origin
            )
        except FunnelError:
            runner(("funnel", "--bg", "--yes", FUNNEL_TARGET))
            validate_funnel_status(
                runner(("funnel", "status", "--json")), origin
            )
        deadline = clock() + wait_seconds
        while True:
            try:
                local_probe()
                return
            except FunnelError:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise
                sleeper(min(2.0, remaining))

    configure_if_needed_and_wait_for_local()
    deadline = clock() + public_wait_seconds
    while True:
        try:
            public_probe(origin)
            break
        except FunnelError:
            remaining = deadline - clock()
            if remaining <= 0:
                raise
            sleeper(min(2.0, remaining))
    return origin


def _require_private_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise FunnelError("private directory metadata is unsafe")


def write_origin(path: Path, origin: str) -> None:
    if not ORIGIN_RE.fullmatch(origin):
        raise FunnelError("Tailscale public origin is malformed")
    _require_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".alice-origin.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, 0, 0)
        os.write(descriptor, (origin + "\n").encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--origin", action="store_true")
    action.add_argument("--ensure", action="store_true")
    action.add_argument("--public-probe", action="store_true")
    action.add_argument("--write-origin", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.origin:
            print(current_origin())
        elif arguments.ensure:
            ensure_funnel()
            print("alice_tailscale_funnel=ready")
        elif arguments.public_probe:
            _probe(current_origin())
            print("alice_tailscale_public_probe=ready")
        else:
            if os.geteuid() != 0:
                raise FunnelError("origin file update requires root")
            assert arguments.write_origin is not None
            write_origin(arguments.write_origin, current_origin())
            print("alice_tailscale_origin=stored")
    except (FunnelError, OSError, subprocess.SubprocessError):
        print("Alice Tailscale Funnel check failed safely.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
