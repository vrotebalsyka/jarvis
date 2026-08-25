#!/usr/bin/env python3
"""Owner-invoked CLI for the isolated Home Butler maintenance pipeline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import safe_maintenance  # noqa: E402


CORE_HEALTH_UNITS = (
    "home-butler-local-chat.service",
    "home-butler-incident-monitor.service",
)


def _fixed_process(command: Sequence[str], cwd: Path, timeout: int) -> bool:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        result = subprocess.run(
            list(command), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=timeout, check=False, shell=False, env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _install_adapter(repo: Path) -> bool:
    installer = repo / "scripts" / "install-home-butler-service.sh"
    return installer.is_file() and _fixed_process(
        ["/usr/bin/bash", str(installer)], repo, 900
    )


def _health_probe() -> bool:
    for unit in CORE_HEALTH_UNITS:
        if not _fixed_process(
            ["/usr/bin/systemctl", "is-active", "--quiet", unit], PROJECT_DIR, 15
        ):
            return False
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:8781/", method="GET",
            headers={"Host": "127.0.0.1:8781"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read(1)
            return response.status == 200
    except (OSError, ValueError):
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=PROJECT_DIR)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("proposal_id")
    prepare.add_argument("--owner-invoked", action="store_true", required=True)

    capture = commands.add_parser("capture")
    capture.add_argument("proposal_id")
    capture.add_argument("--worktree", type=Path, required=True)
    capture.add_argument("--owner-invoked", action="store_true", required=True)

    approve = commands.add_parser("approve")
    approve.add_argument("candidate_id")
    approve.add_argument("--confirmation", required=True)
    approve.add_argument("--owner-invoked", action="store_true", required=True)

    deploy = commands.add_parser("deploy")
    deploy.add_argument("candidate_id")
    deploy.add_argument("--confirmation", required=True)
    deploy.add_argument("--owner-invoked", action="store_true", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            result = safe_maintenance.prepare_isolated_worktree(
                arguments.proposal_id,
                active_repo=arguments.repo,
                owner_invoked=arguments.owner_invoked,
            )
        elif arguments.command == "capture":
            result = safe_maintenance.capture_patch_candidate(
                arguments.proposal_id,
                active_repo=arguments.repo,
                worktree=arguments.worktree,
                owner_invoked=arguments.owner_invoked,
            )
        elif arguments.command == "approve":
            result = safe_maintenance.approve_patch_candidate(
                arguments.candidate_id,
                arguments.confirmation,
                owner_invoked=arguments.owner_invoked,
            )
        else:
            result = safe_maintenance.deploy_approved_candidate(
                arguments.candidate_id,
                arguments.confirmation,
                active_repo=arguments.repo,
                owner_invoked=arguments.owner_invoked,
                deploy_adapter=_install_adapter,
                health_probe=_health_probe,
                rollback_adapter=_install_adapter,
            )
    except safe_maintenance.MaintenanceError as error:
        print(f"maintenance_error={error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
