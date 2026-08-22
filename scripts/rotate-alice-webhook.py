#!/usr/bin/env python3
"""Stage and commit a zero-downtime private Alice webhook rotation."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


SECRET_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
ORIGIN_RE = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\.ts\.net\Z"
)
SERVICE_NAME = "home-butler-alice-skill.service"
MAX_PRIVATE_BYTES = 4096


class RotationError(RuntimeError):
    """A bounded, secret-free webhook rotation failure."""


@dataclass(frozen=True)
class Layout:
    primary_file: Path
    next_file: Path
    url_file: Path
    origin_file: Path
    marker_file: Path
    service_uid: int
    service_gid: int


def production_layout() -> Layout:
    import pwd

    account = pwd.getpwnam("homebutler")
    project = Path("/root/Jarvis/home-butler")
    state = Path("/home/homebutler/.local/state/home-butler/alice")
    return Layout(
        primary_file=project / "secrets/alice-skill-secret",
        next_file=project / "secrets/alice-skill-secret-next",
        url_file=project / "secrets/alice-webhook-url.txt",
        origin_file=project / "secrets/alice-public-origin.txt",
        marker_file=state / "webhook-next-used",
        service_uid=account.pw_uid,
        service_gid=account.pw_gid,
    )


def marker_value(secret: str) -> str:
    if not SECRET_RE.fullmatch(secret):
        raise RotationError("webhook credential is malformed")
    return hashlib.blake2s(secret.encode("ascii"), digest_size=16).hexdigest()


def _require_directory(path: Path, uid: int, gid: int) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RotationError("private directory metadata is unsafe")


def _read_private(path: Path, uid: int, gid: int, maximum: int) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RotationError("required private file is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != uid
            or metadata.st_gid != gid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
        ):
            raise RotationError("private file metadata is unsafe")
        raw = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise RotationError("private file is malformed") from error
    if not value:
        raise RotationError("private file is empty")
    return value


def _read_root_secret(path: Path) -> str:
    value = _read_private(path, 0, 0, 256)
    if not SECRET_RE.fullmatch(value):
        raise RotationError("webhook credential is malformed")
    return value


def _read_root_origin(path: Path) -> str:
    value = _read_private(path, 0, 0, 256)
    if not ORIGIN_RE.fullmatch(value):
        raise RotationError("webhook origin is malformed")
    return value


def _atomic_write(path: Path, value: str, uid: int, gid: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".alice-rotate.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, uid, gid)
        os.write(descriptor, (value + "\n").encode("ascii"))
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


def _remove_marker(layout: Layout) -> None:
    try:
        metadata = layout.marker_file.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != layout.service_uid
        or metadata.st_gid != layout.service_gid
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise RotationError("rotation marker metadata is unsafe")
    layout.marker_file.unlink()


def _run_systemctl(arguments: Sequence[str]) -> None:
    completed = subprocess.run(
        ["/usr/bin/systemctl", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=180,
        check=False,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    if completed.returncode != 0:
        raise RotationError("Alice gateway did not accept the rotated credential")


def _restart(runner: Callable[[Sequence[str]], None]) -> None:
    runner(("restart", SERVICE_NAME))
    runner(("is-active", "--quiet", SERVICE_NAME))


def _url(origin: str, secret: str) -> str:
    if not ORIGIN_RE.fullmatch(origin) or not SECRET_RE.fullmatch(secret):
        raise RotationError("webhook URL input is malformed")
    return f"{origin}/alice/{secret}"


def stage(
    layout: Layout,
    runner: Callable[[Sequence[str]], None] = _run_systemctl,
) -> str:
    _require_directory(layout.primary_file.parent, 0, 0)
    _require_directory(layout.marker_file.parent, layout.service_uid, layout.service_gid)
    origin = _read_root_origin(layout.origin_file)
    primary = _read_root_secret(layout.primary_file)
    current_next = _read_root_secret(layout.next_file)
    if not secrets.compare_digest(primary, current_next):
        return "already_staged"

    next_secret = secrets.token_urlsafe(48)
    if not SECRET_RE.fullmatch(next_secret) or secrets.compare_digest(primary, next_secret):
        raise RotationError("secure webhook credential generation failed")
    _remove_marker(layout)
    _atomic_write(layout.next_file, next_secret, 0, 0)
    _atomic_write(layout.url_file, _url(origin, next_secret), 0, 0)
    try:
        _restart(runner)
    except Exception:
        _atomic_write(layout.next_file, primary, 0, 0)
        _atomic_write(layout.url_file, _url(origin, primary), 0, 0)
        try:
            _restart(runner)
        except Exception:
            pass
        raise
    return "staged"


def commit(
    layout: Layout,
    runner: Callable[[Sequence[str]], None] = _run_systemctl,
) -> str:
    _require_directory(layout.primary_file.parent, 0, 0)
    _require_directory(layout.marker_file.parent, layout.service_uid, layout.service_gid)
    primary = _read_root_secret(layout.primary_file)
    next_secret = _read_root_secret(layout.next_file)
    if secrets.compare_digest(primary, next_secret):
        return "not_staged"
    observed = _read_private(
        layout.marker_file,
        layout.service_uid,
        layout.service_gid,
        MAX_PRIVATE_BYTES,
    )
    if not secrets.compare_digest(observed, marker_value(next_secret)):
        raise RotationError("the new webhook has not passed an authenticated request")

    _atomic_write(layout.primary_file, next_secret, 0, 0)
    try:
        _restart(runner)
    except Exception:
        _atomic_write(layout.primary_file, primary, 0, 0)
        try:
            _restart(runner)
        except Exception:
            pass
        raise
    _remove_marker(layout)
    return "committed"


def abort(
    layout: Layout,
    runner: Callable[[Sequence[str]], None] = _run_systemctl,
) -> str:
    _require_directory(layout.primary_file.parent, 0, 0)
    _require_directory(layout.marker_file.parent, layout.service_uid, layout.service_gid)
    origin = _read_root_origin(layout.origin_file)
    primary = _read_root_secret(layout.primary_file)
    _read_root_secret(layout.next_file)
    _atomic_write(layout.next_file, primary, 0, 0)
    _atomic_write(layout.url_file, _url(origin, primary), 0, 0)
    _remove_marker(layout)
    _restart(runner)
    return "aborted"


def status(layout: Layout) -> str:
    _require_directory(layout.primary_file.parent, 0, 0)
    _require_directory(layout.marker_file.parent, layout.service_uid, layout.service_gid)
    primary = _read_root_secret(layout.primary_file)
    next_secret = _read_root_secret(layout.next_file)
    if secrets.compare_digest(primary, next_secret):
        return "idle"
    try:
        observed = _read_private(
            layout.marker_file,
            layout.service_uid,
            layout.service_gid,
            MAX_PRIVATE_BYTES,
        )
    except RotationError:
        return "staged_waiting_for_new_request"
    if secrets.compare_digest(observed, marker_value(next_secret)):
        return "staged_verified_ready_to_commit"
    raise RotationError("rotation marker conflicts with the staged credential")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--stage", action="store_true")
    action.add_argument("--commit", action="store_true")
    action.add_argument("--abort", action="store_true")
    action.add_argument("--status", action="store_true")
    arguments = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("Alice webhook rotation must run as root.", file=os.sys.stderr)
        return 2
    try:
        layout = production_layout()
        if arguments.stage:
            result = stage(layout)
        elif arguments.commit:
            result = commit(layout)
        elif arguments.abort:
            result = abort(layout)
        else:
            result = status(layout)
    except (KeyError, OSError, RotationError, subprocess.SubprocessError):
        print("Alice webhook rotation failed safely.", file=os.sys.stderr)
        return 2
    print(f"alice_webhook_rotation={result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
