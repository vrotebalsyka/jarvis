#!/usr/bin/env python3
"""Pin the first private Yandex skill identity without exposing credentials."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


PENDING_SKILL_ID = "PENDING_PRIVATE_SKILL"
IDENTITY_RE = re.compile(r"[A-Za-z0-9._:-]{8,256}\Z")
MAX_CLAIM_BYTES = 4096
SERVICE_NAME = "home-butler-alice-skill.service"


class FinalizeError(RuntimeError):
    """A secret-free automatic-finalization failure."""


@dataclass(frozen=True)
class Layout:
    claim_file: Path
    skill_file: Path
    owners_file: Path
    mode_file: Path
    service_uid: int
    service_gid: int


def production_layout() -> Layout:
    import pwd

    account = pwd.getpwnam("homebutler")
    project = Path("/root/Jarvis/home-butler")
    state = Path("/home/homebutler/.local/state/home-butler/alice")
    return Layout(
        claim_file=state / "claim.json",
        skill_file=project / "secrets/alice-skill-id",
        owners_file=project / "secrets/alice-owner-ids",
        mode_file=state / "mode",
        service_uid=account.pw_uid,
        service_gid=account.pw_gid,
    )


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FinalizeError("provisioning claim contains duplicate fields")
        result[key] = value
    return result


def _require_directory(path: Path, uid: int, gid: int) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise FinalizeError("provisioning directory metadata is unsafe")


def _read_private_text(path: Path, uid: int, gid: int, maximum: int) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FinalizeError("required private file is unavailable") from error
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
            raise FinalizeError("private file metadata is unsafe")
        raw = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise FinalizeError("private file is malformed") from error
    if not value:
        raise FinalizeError("private file is empty")
    return value


def _read_claim(layout: Layout) -> tuple[str, str, tuple[int, int]]:
    _require_directory(
        layout.claim_file.parent, layout.service_uid, layout.service_gid
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(layout.claim_file, flags)
    except OSError as error:
        raise FinalizeError("no safe provisioning claim is available") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != layout.service_uid
            or metadata.st_gid != layout.service_gid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_CLAIM_BYTES
        ):
            raise FinalizeError("provisioning claim metadata is unsafe")
        raw = os.read(descriptor, MAX_CLAIM_BYTES + 1)
        identity = (metadata.st_dev, metadata.st_ino)
    finally:
        os.close(descriptor)
    try:
        document = json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FinalizeError("provisioning claim is malformed") from error
    if not isinstance(document, dict) or set(document) != {"skill_id", "user_id"}:
        raise FinalizeError("provisioning claim fields are invalid")
    skill_id = document.get("skill_id")
    user_id = document.get("user_id")
    if (
        not isinstance(skill_id, str)
        or skill_id == PENDING_SKILL_ID
        or not IDENTITY_RE.fullmatch(skill_id)
        or (user_id is not None and (
            not isinstance(user_id, str) or not IDENTITY_RE.fullmatch(user_id)
        ))
    ):
        raise FinalizeError("provisioning claim identity is invalid")
    return skill_id, user_id or "-", identity


def _atomic_private_write(path: Path, value: str, uid: int, gid: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".alice-finalize.", dir=path.parent)
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
        raise FinalizeError("Alice gateway did not accept the pinned identity")


def finalize(
    layout: Layout,
    runner: Callable[[Sequence[str]], None] = _run_systemctl,
) -> None:
    skill_id, owner_id, claim_identity = _read_claim(layout)
    _require_directory(layout.skill_file.parent, 0, 0)
    current_skill = _read_private_text(layout.skill_file, 0, 0, 512)
    current_owner = _read_private_text(layout.owners_file, 0, 0, 512)
    if current_skill not in {PENDING_SKILL_ID, skill_id}:
        raise FinalizeError("provisioning claim conflicts with pinned skill")
    if current_owner not in {"-", owner_id}:
        raise FinalizeError("provisioning claim conflicts with pinned owner")

    _atomic_private_write(layout.skill_file, skill_id, 0, 0)
    _atomic_private_write(layout.owners_file, owner_id, 0, 0)
    _atomic_private_write(
        layout.mode_file, "pinned", layout.service_uid, layout.service_gid
    )
    runner(("restart", SERVICE_NAME))
    runner(("is-active", "--quiet", SERVICE_NAME))

    current = layout.claim_file.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != claim_identity
    ):
        raise FinalizeError("provisioning claim changed during finalization")
    layout.claim_file.unlink()


def main() -> int:
    try:
        finalize(production_layout())
    except (FinalizeError, KeyError, OSError, subprocess.SubprocessError):
        print("Alice identity automatic finalization failed safely.", file=sys.stderr)
        return 2
    print("Alice full-dialog identity pinned automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
