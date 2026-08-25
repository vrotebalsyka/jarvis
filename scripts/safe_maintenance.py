#!/usr/bin/env python3
"""Approval-gated self-improvement artifacts for Home Butler.

The conversational process may only call :func:`create_change_proposal`.
Patch capture, qualification, approval and deployment are deliberately usable
only by the separate owner-invoked maintenance worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

import model_workspace


SCHEMA_VERSION = 1
MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_CHANGED_FILES = 128
MAX_COMMAND_OUTPUT = 16_000
MAINTENANCE_ROOT = Path(os.environ.get(
    "HOME_BUTLER_MAINTENANCE_ROOT", "/var/lib/home-butler-maintenance"
))
PROPOSAL_FIELDS = (
    "observed_problem",
    "evidence",
    "affected_components",
    "proposed_change",
    "expected_benefit",
    "risks",
    "proposed_tests",
)
LIST_LIMITS = {
    "evidence": (1, 20, 1_000),
    "affected_components": (1, 32, 320),
    "risks": (1, 20, 1_000),
    "proposed_tests": (1, 20, 1_000),
}
TEXT_LIMITS = {
    "observed_problem": 2_000,
    "proposed_change": 4_000,
    "expected_benefit": 2_000,
}
FORBIDDEN_COMPONENT_PARTS = frozenset({
    ".git", "secrets", "runtime", "cache", "__pycache__", "node_modules",
})
FORBIDDEN_COMPONENT_SUFFIXES = (
    ".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3", ".log", ".pem",
    ".key", ".token", ".env",
)
SECRET_RE = re.compile(
    r"(?:\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b|"
    r"\bAuthorization\s*:\s*Bearer\s+\S+|\b(?:password|token|secret)\s*=\s*\S+)",
    re.IGNORECASE,
)
ID_RE = re.compile(r"[a-f0-9]{16}")
HASH_RE = re.compile(r"[a-f0-9]{64}")


class MaintenanceError(RuntimeError):
    """A secret-free maintenance pipeline failure."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], Path, str | None, int], CommandResult]
QualificationRunner = Callable[[str, Sequence[str], Path, bool], CommandResult]


def _canonical(document: Mapping[str, Any]) -> str:
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _hash(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()


def _safe_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise MaintenanceError(f"change proposal {field} is invalid")
    normalized = unicodedata.normalize("NFKC", value.strip())
    try:
        encoded = normalized.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise MaintenanceError(f"change proposal {field} is invalid") from error
    if (
        not encoded
        or len(normalized) > maximum
        or SECRET_RE.search(normalized)
        or any(ord(character) < 32 and character not in "\n\t" for character in normalized)
    ):
        raise MaintenanceError(f"change proposal {field} is unsafe")
    return normalized


def _component(value: object) -> str:
    text = _safe_text(value, field="affected component", maximum=320)
    if "\\" in text or "\x00" in text:
        raise MaintenanceError("change proposal affected component is invalid")
    candidate = PurePosixPath(text)
    parts = candidate.parts
    if (
        candidate.is_absolute()
        or not parts
        or len(parts) > 8
        or any(part in {"", ".", ".."} or part.casefold() in FORBIDDEN_COMPONENT_PARTS for part in parts)
        or any(part.startswith(".") for part in parts)
        or candidate.name.casefold().endswith(FORBIDDEN_COMPONENT_SUFFIXES)
    ):
        raise MaintenanceError("change proposal affected component is outside source scope")
    return candidate.as_posix()


def validate_change_proposal(document: object) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != set(PROPOSAL_FIELDS):
        raise MaintenanceError("change proposal schema is invalid")
    result: dict[str, Any] = {}
    for field, maximum in TEXT_LIMITS.items():
        result[field] = _safe_text(document[field], field=field, maximum=maximum)
    for field, (minimum, maximum_items, maximum_chars) in LIST_LIMITS.items():
        raw = document[field]
        if not isinstance(raw, list) or not minimum <= len(raw) <= maximum_items:
            raise MaintenanceError(f"change proposal {field} is invalid")
        if field == "affected_components":
            values = [_component(item) for item in raw]
        else:
            values = [
                _safe_text(item, field=field, maximum=maximum_chars) for item in raw
            ]
        if len({item.casefold() for item in values}) != len(values):
            raise MaintenanceError(f"change proposal {field} contains duplicates")
        result[field] = values
    return {field: result[field] for field in PROPOSAL_FIELDS}


def change_proposal_tool_definition() -> dict[str, Any]:
    text = {"type": "string", "minLength": 1}
    string_list = {
        "type": "array", "minItems": 1, "maxItems": 20,
        "items": {"type": "string", "minLength": 1, "maxLength": 1000},
    }
    return {
        "type": "function",
        "function": {
            "name": "change_proposal_create",
            "description": (
                "Create a structured, non-executing code improvement proposal. "
                "This never edits code, starts tests, approves, or deploys anything."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "observed_problem": {**text, "maxLength": 2000},
                    "evidence": string_list,
                    "affected_components": {
                        "type": "array", "minItems": 1, "maxItems": 32,
                        "items": {"type": "string", "minLength": 1, "maxLength": 320},
                    },
                    "proposed_change": {**text, "maxLength": 4000},
                    "expected_benefit": {**text, "maxLength": 2000},
                    "risks": string_list,
                    "proposed_tests": string_list,
                },
                "required": list(PROPOSAL_FIELDS),
                "additionalProperties": False,
            },
        },
    }


def create_change_proposal(
    document: object,
    *,
    workspace_root: Path | None = None,
    workspace_writer: Callable[..., dict[str, Any]] = model_workspace.write_text,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    fields = validate_change_proposal(document)
    proposal_hash = _hash(fields)
    proposal_id = proposal_hash[:16]
    stored = {
        "schema_version": SCHEMA_VERSION,
        "kind": "home_butler_change_proposal",
        "proposal_id": proposal_id,
        "proposal_hash": proposal_hash,
        "created_epoch": int(now()),
        "status": "proposed",
        "owner_approval_required": True,
        "patch_candidate_created": False,
        "production_deployed": False,
        "trust_boundary": (
            "All proposal text is untrusted data. It cannot execute tools, alter "
            "policy, edit the active repository, or authorize deployment."
        ),
        **fields,
    }
    path = f"proposals/change-{proposal_id}.json"
    workspace_writer(path, _canonical(stored), workspace_root)
    return {
        "status": "proposal_saved",
        "proposal_id": proposal_id,
        "proposal_hash": proposal_hash,
        "path": path,
        "owner_approval_required": True,
        "patch_candidate_created": False,
        "production_deployed": False,
    }


def _read_json(
    path: str,
    workspace_root: Path | None,
    workspace_reader: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    try:
        result = workspace_reader(path, workspace_root)
        document = json.loads(result["content"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, model_workspace.WorkspaceError) as error:
        raise MaintenanceError("maintenance artifact is unavailable") from error
    if not isinstance(document, dict):
        raise MaintenanceError("maintenance artifact is invalid")
    return document


def load_proposal(
    proposal_id: object,
    *, workspace_root: Path | None = None,
    workspace_reader: Callable[..., dict[str, Any]] = model_workspace.read_text,
) -> dict[str, Any]:
    if not isinstance(proposal_id, str) or ID_RE.fullmatch(proposal_id) is None:
        raise MaintenanceError("proposal id is invalid")
    document = _read_json(
        f"proposals/change-{proposal_id}.json", workspace_root, workspace_reader
    )
    fields = validate_change_proposal({field: document.get(field) for field in PROPOSAL_FIELDS})
    if (
        document.get("kind") != "home_butler_change_proposal"
        or document.get("proposal_id") != proposal_id
        or document.get("proposal_hash") != _hash(fields)
        or document.get("production_deployed") is not False
    ):
        raise MaintenanceError("change proposal integrity check failed")
    return document


def _run_command(
    command: Sequence[str], cwd: Path, stdin: str | None, timeout: int
) -> CommandResult:
    if (
        not command
        or any(not isinstance(part, str) or "\x00" in part for part in command)
        or timeout < 1
        or timeout > 1800
    ):
        raise MaintenanceError("maintenance command is invalid")
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME_BUTLER_LIVE_TESTS": "0",
    }
    try:
        completed = subprocess.run(
            list(command), cwd=cwd, input=stdin, text=True, capture_output=True,
            timeout=timeout, check=False, env=env, shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MaintenanceError("maintenance command failed to start") from error
    return CommandResult(
        completed.returncode,
        completed.stdout[-MAX_COMMAND_OUTPUT:],
        completed.stderr[-MAX_COMMAND_OUTPUT:],
    )


def _git(
    repo: Path,
    arguments: Sequence[str],
    *, runner: CommandRunner,
    stdin: str | None = None,
    timeout: int = 60,
    allow_difference: bool = False,
) -> CommandResult:
    result = runner(["git", "-C", str(repo), *arguments], repo, stdin, timeout)
    allowed = {0, 1} if allow_difference else {0}
    if result.returncode not in allowed:
        raise MaintenanceError("isolated Git operation failed")
    return result


def _repo_root(path: Path, runner: CommandRunner) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as error:
        raise MaintenanceError("repository path is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise MaintenanceError("repository path is unsafe")
    output = _git(resolved, ["rev-parse", "--show-toplevel"], runner=runner).stdout.strip()
    try:
        root = Path(output).resolve(strict=True)
    except OSError as error:
        raise MaintenanceError("repository root is unavailable") from error
    if root != resolved:
        raise MaintenanceError("repository path is not its root")
    return root


def _clean(repo: Path, runner: CommandRunner) -> bool:
    return not _git(
        repo, ["status", "--porcelain=v1", "--untracked-files=all"], runner=runner
    ).stdout.strip()


def prepare_isolated_worktree(
    proposal_id: str,
    *,
    active_repo: Path,
    owner_invoked: bool,
    maintenance_root: Path = MAINTENANCE_ROOT,
    workspace_root: Path | None = None,
    runner: CommandRunner = _run_command,
) -> dict[str, Any]:
    if owner_invoked is not True:
        raise MaintenanceError("owner-invoked maintenance worker is required")
    proposal = load_proposal(proposal_id, workspace_root=workspace_root)
    active = _repo_root(active_repo, runner)
    if not _clean(active, runner):
        raise MaintenanceError("active repository must be clean before preparing a worktree")
    try:
        root = maintenance_root.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        metadata = root.lstat()
    except OSError as error:
        raise MaintenanceError("maintenance root is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise MaintenanceError("maintenance root is unsafe")
    target = root / f"change-{proposal_id}"
    if target.exists() or target.is_symlink():
        raise MaintenanceError("maintenance worktree already exists")
    branch = f"home-butler-change-{proposal_id}"
    _git(
        active,
        ["worktree", "add", "-b", branch, str(target), "HEAD"],
        runner=runner,
        timeout=120,
    )
    private_env = target / "hermes" / ".env"
    if private_env.is_file() and not private_env.is_symlink():
        private_env.chmod(0o600)
    base_commit = _git(target, ["rev-parse", "HEAD"], runner=runner).stdout.strip()
    return {
        "status": "isolated_worktree_ready",
        "proposal_id": proposal["proposal_id"],
        "proposal_hash": proposal["proposal_hash"],
        "worktree": str(target),
        "branch": branch,
        "base_commit": base_commit,
        "active_repository_modified": False,
        "production_deployed": False,
    }


def _assert_isolated_worktree(
    active_repo: Path, worktree: Path, runner: CommandRunner
) -> tuple[Path, Path, str]:
    active = _repo_root(active_repo, runner)
    isolated = _repo_root(worktree, runner)
    if active == isolated or (isolated / ".git").is_dir():
        raise MaintenanceError("patch candidate requires an isolated Git worktree")
    common_raw = _git(
        isolated, ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        runner=runner,
    ).stdout.strip()
    try:
        common = Path(common_raw).resolve(strict=True)
        active_git = (active / ".git").resolve(strict=True)
    except OSError as error:
        raise MaintenanceError("Git worktree metadata is unavailable") from error
    if common != active_git:
        raise MaintenanceError("worktree does not belong to the active repository")
    commit = _git(isolated, ["rev-parse", "HEAD"], runner=runner).stdout.strip()
    return active, isolated, commit


def _path_allowed_by_proposal(path: str, components: Sequence[str]) -> bool:
    return any(path == component or path.startswith(component.rstrip("/") + "/") for component in components)


QUALIFICATION_STAGES: tuple[tuple[str, tuple[str, ...], bool, int], ...] = (
    (
        "unit_tests",
        ("python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_safe_maintenance.py", "-v"),
        False,
        180,
    ),
    (
        "full_offline_tests",
        ("python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"),
        False,
        1200,
    ),
    ("security_audit", ("python3", "scripts/no_cloud_audit.py"), False, 180),
    ("model_evaluation", ("python3", "tests/evaluate_model.py"), True, 600),
    ("diff_check", ("git", "diff", "--check", "HEAD", "--"), False, 60),
)


def build_qualification_sandbox_command(
    command: Sequence[str], worktree: Path, *, local_model_network: bool
) -> list[str]:
    properties = [
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        f"ReadWritePaths={worktree}",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "CapabilityBoundingSet=",
    ]
    if local_model_network:
        properties.extend((
            "IPAddressDeny=any",
            "IPAddressAllow=127.0.0.0/8",
            "IPAddressAllow=::1/128",
            "IPAddressAllow=172.16.0.0/12",
        ))
    else:
        properties.append("RestrictAddressFamilies=AF_UNIX")
    argv = [
        "systemd-run", "--wait", "--pipe", "--collect", "--quiet",
        "--service-type=exec", f"--working-directory={worktree}",
    ]
    for value in properties:
        argv.extend(("--property", value))
    return [*argv, *command]


def _qualification_runner(
    stage: str, command: Sequence[str], worktree: Path, local_model_network: bool
) -> CommandResult:
    del stage
    sandboxed = build_qualification_sandbox_command(
        command, worktree, local_model_network=local_model_network
    )
    return _run_command(sandboxed, worktree, None, 1800)


def capture_patch_candidate(
    proposal_id: str,
    *,
    active_repo: Path,
    worktree: Path,
    owner_invoked: bool,
    workspace_root: Path | None = None,
    workspace_writer: Callable[..., dict[str, Any]] = model_workspace.write_text,
    runner: CommandRunner = _run_command,
    qualification_runner: QualificationRunner = _qualification_runner,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if owner_invoked is not True:
        raise MaintenanceError("owner-invoked maintenance worker is required")
    proposal = load_proposal(proposal_id, workspace_root=workspace_root)
    _active, isolated, base_commit = _assert_isolated_worktree(
        active_repo, worktree, runner
    )
    untracked_raw = _git(
        isolated, ["ls-files", "--others", "--exclude-standard", "-z"], runner=runner
    ).stdout
    untracked = [item for item in untracked_raw.split("\x00") if item]
    for relative in untracked:
        normalized = _component(relative)
        target = isolated / normalized
        try:
            metadata = target.lstat()
        except OSError as error:
            raise MaintenanceError("untracked patch file is unavailable") from error
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise MaintenanceError("patch candidate contains an unsafe file")
    if untracked:
        _git(isolated, ["add", "-N", "--", *untracked], runner=runner)
    names_raw = _git(
        isolated, ["diff", "--name-only", "-z", "HEAD", "--"], runner=runner
    ).stdout
    changed = [_component(item) for item in names_raw.split("\x00") if item]
    if not changed or len(changed) > MAX_CHANGED_FILES:
        raise MaintenanceError("patch candidate changed-file count is invalid")
    allowed_components = proposal["affected_components"]
    if any(not _path_allowed_by_proposal(path, allowed_components) for path in changed):
        raise MaintenanceError("patch changes a component outside the proposal")
    patch = _git(
        isolated,
        ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--unified=3", "HEAD", "--"],
        runner=runner,
    ).stdout
    try:
        patch_bytes = patch.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise MaintenanceError("patch candidate is not UTF-8 text") from error
    if (
        not patch_bytes
        or len(patch_bytes) > MAX_PATCH_BYTES
        or "GIT binary patch" in patch
        or "Binary files " in patch
        or SECRET_RE.search(patch)
    ):
        raise MaintenanceError("patch candidate content is unsafe")
    qualification: list[dict[str, Any]] = []
    passed = True
    for stage, command, local_network, timeout in QUALIFICATION_STAGES:
        started = time.monotonic()
        result = qualification_runner(stage, command, isolated, local_network)
        combined_output = (result.stdout + result.stderr).encode(
            "utf-8", errors="replace"
        )
        entry = {
            "stage": stage,
            "command_id": stage,
            "passed": result.returncode == 0,
            "returncode": result.returncode,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output_bytes": len(combined_output),
            "output_fingerprint": hashlib.sha256(combined_output).hexdigest()[:16],
            "network_scope": "local_model_only" if local_network else "none",
        }
        qualification.append(entry)
        if result.returncode != 0:
            passed = False
            break
        if timeout < 1:  # defensive proof that stage metadata was validated
            raise MaintenanceError("qualification timeout is invalid")
    candidate_core = {
        "schema_version": SCHEMA_VERSION,
        "kind": "home_butler_patch_candidate",
        "proposal_id": proposal_id,
        "proposal_hash": proposal["proposal_hash"],
        "base_commit": base_commit,
        "changed_files": changed,
        "patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
        "patch": patch,
        "qualification": qualification,
        "status": "qualified" if passed and len(qualification) == len(QUALIFICATION_STAGES) else "rejected",
        "created_epoch": int(now()),
        "owner_approval_required": True,
        "production_deployed": False,
    }
    candidate_hash = _hash(candidate_core)
    candidate_id = candidate_hash[:16]
    stored = {**candidate_core, "candidate_id": candidate_id, "candidate_hash": candidate_hash}
    path = f"proposals/candidate-{candidate_id}.json"
    workspace_writer(path, _canonical(stored), workspace_root)
    return {
        "status": stored["status"],
        "candidate_id": candidate_id,
        "candidate_hash": candidate_hash,
        "path": path,
        "changed_files": changed,
        "qualification_passed": stored["status"] == "qualified",
        "owner_approval_required": True,
        "production_deployed": False,
    }


def load_candidate(
    candidate_id: object,
    *, workspace_root: Path | None = None,
    workspace_reader: Callable[..., dict[str, Any]] = model_workspace.read_text,
) -> dict[str, Any]:
    if not isinstance(candidate_id, str) or ID_RE.fullmatch(candidate_id) is None:
        raise MaintenanceError("candidate id is invalid")
    document = _read_json(
        f"proposals/candidate-{candidate_id}.json", workspace_root, workspace_reader
    )
    core = {key: value for key, value in document.items() if key not in {"candidate_id", "candidate_hash"}}
    if (
        document.get("kind") != "home_butler_patch_candidate"
        or document.get("candidate_id") != candidate_id
        or not isinstance(document.get("candidate_hash"), str)
        or document["candidate_hash"] != _hash(core)
        or document.get("production_deployed") is not False
    ):
        raise MaintenanceError("patch candidate integrity check failed")
    return document


def approve_patch_candidate(
    candidate_id: str,
    confirmation: object,
    *,
    owner_invoked: bool,
    workspace_root: Path | None = None,
    workspace_writer: Callable[..., dict[str, Any]] = model_workspace.write_text,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if owner_invoked is not True:
        raise MaintenanceError("owner-invoked maintenance worker is required")
    candidate = load_candidate(candidate_id, workspace_root=workspace_root)
    if candidate.get("status") != "qualified":
        raise MaintenanceError("only a fully qualified candidate can be approved")
    expected = f"APPROVE {candidate['candidate_hash']}"
    if not isinstance(confirmation, str) or confirmation != expected:
        raise MaintenanceError("exact candidate approval is required")
    approval = {
        "schema_version": SCHEMA_VERSION,
        "kind": "home_butler_patch_approval",
        "candidate_id": candidate_id,
        "candidate_hash": candidate["candidate_hash"],
        "approved_epoch": int(now()),
        "scope": "this_exact_patch_candidate_only",
        "deployment_started": False,
        "production_deployed": False,
    }
    path = f"proposals/approval-{candidate_id}.json"
    workspace_writer(path, _canonical(approval), workspace_root)
    return {
        "status": "approved_for_owner_invoked_deployment",
        "candidate_id": candidate_id,
        "candidate_hash": candidate["candidate_hash"],
        "path": path,
        "production_deployed": False,
    }


def deployment_is_authorized(
    candidate_id: str,
    confirmation: object,
    *,
    workspace_root: Path | None = None,
    workspace_reader: Callable[..., dict[str, Any]] = model_workspace.read_text,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = load_candidate(
        candidate_id, workspace_root=workspace_root, workspace_reader=workspace_reader
    )
    approval = _read_json(
        f"proposals/approval-{candidate_id}.json", workspace_root, workspace_reader
    )
    expected = f"DEPLOY {candidate['candidate_hash']}"
    if (
        candidate.get("status") != "qualified"
        or not isinstance(confirmation, str)
        or confirmation != expected
        or approval.get("kind") != "home_butler_patch_approval"
        or approval.get("candidate_hash") != candidate.get("candidate_hash")
        or approval.get("production_deployed") is not False
    ):
        raise MaintenanceError("exact approved deployment confirmation is required")
    return candidate, approval


def deploy_approved_candidate(
    candidate_id: str,
    confirmation: str,
    *,
    active_repo: Path,
    owner_invoked: bool,
    deploy_adapter: Callable[[Path], bool],
    health_probe: Callable[[], bool],
    rollback_adapter: Callable[[Path], bool],
    workspace_root: Path | None = None,
    workspace_writer: Callable[..., dict[str, Any]] = model_workspace.write_text,
    runner: CommandRunner = _run_command,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    if owner_invoked is not True:
        raise MaintenanceError("owner-invoked maintenance worker is required")
    candidate, _approval = deployment_is_authorized(
        candidate_id, confirmation, workspace_root=workspace_root
    )
    active = _repo_root(active_repo, runner)
    if not _clean(active, runner):
        raise MaintenanceError("active repository must be clean before deployment")
    head = _git(active, ["rev-parse", "HEAD"], runner=runner).stdout.strip()
    if head != candidate.get("base_commit"):
        raise MaintenanceError("active repository no longer matches candidate base")
    patch = candidate.get("patch")
    if not isinstance(patch, str) or hashlib.sha256(patch.encode("utf-8")).hexdigest() != candidate.get("patch_sha256"):
        raise MaintenanceError("patch candidate integrity check failed")
    _git(active, ["apply", "--check", "--whitespace=error-all", "-"], runner=runner, stdin=patch)
    _git(active, ["apply", "--whitespace=error-all", "-"], runner=runner, stdin=patch)
    deployed = False
    rollback_ok = False
    try:
        deployed = deploy_adapter(active) is True and health_probe() is True
    except Exception:
        deployed = False
    if not deployed:
        try:
            _git(active, ["apply", "-R", "--whitespace=nowarn", "-"], runner=runner, stdin=patch)
            rollback_ok = rollback_adapter(active) is True
        except Exception as error:
            raise MaintenanceError("deployment failed and automatic rollback failed") from error
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": "home_butler_deployment_result",
        "candidate_id": candidate_id,
        "candidate_hash": candidate["candidate_hash"],
        "completed_epoch": int(now()),
        "status": "health_verified" if deployed else "rolled_back",
        "health_verified": deployed,
        "rollback_performed": not deployed,
        "rollback_verified": rollback_ok if not deployed else False,
        "production_deployed": deployed,
    }
    path = f"proposals/deployment-{candidate_id}.json"
    workspace_writer(path, _canonical(record), workspace_root)
    return {**record, "path": path}
