#!/usr/bin/env python3
"""Read-only deployment preflight for the single Home Assistant container host."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
OFFICIAL_IMAGES = (
    "ghcr.io/home-assistant/home-assistant:",
    "homeassistant/home-assistant:",
)
COMPOSE_LABELS = {
    "project": "com.docker.compose.project",
    "service": "com.docker.compose.service",
    "working_dir": "com.docker.compose.project.working_dir",
    "config_files": "com.docker.compose.project.config_files",
}


class PreflightError(RuntimeError):
    """Secret-free preflight failure."""


def _run_json(arguments: list[str]) -> Any:
    completed = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    if completed.returncode != 0 or len(completed.stdout) > 8 * 1024 * 1024:
        raise PreflightError("docker query failed")
    try:
        return json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreflightError("docker response invalid") from error


def _run_text(arguments: list[str], limit: int = 512) -> str:
    completed = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    if completed.returncode != 0 or not 0 < len(completed.stdout) <= limit:
        raise PreflightError("host query failed")
    try:
        return completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise PreflightError("host response invalid") from error


def _safe_absolute_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return None
    normalized = os.path.normpath(value)
    if normalized != value or len(value) > 1024:
        return None
    return value


def _safe_compose_file(path_text: str) -> bool:
    path = Path(path_text)
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and metadata.st_uid in {0, os.environ.get("SUDO_UID") and int(os.environ["SUDO_UID"])}
        and not metadata.st_mode & 0o022
    )


def collect() -> dict[str, Any]:
    if os.geteuid() != 0:
        raise PreflightError("root required")
    ids_text = _run_text(
        [
            "/usr/bin/docker", "container", "ls", "--all", "--quiet",
            "--no-trunc", "--filter", "label=io.hass.type=core",
        ],
        limit=256,
    )
    container_ids = [line for line in ids_text.splitlines() if line]
    if len(container_ids) != 1 or not CONTAINER_ID_RE.fullmatch(container_ids[0]):
        raise PreflightError("container identity invalid")
    container_id = container_ids[0]
    inspected = _run_json(["/usr/bin/docker", "inspect", container_id])
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise PreflightError("container inspect invalid")
    item = inspected[0]
    config = item.get("Config")
    host_config = item.get("HostConfig")
    mounts = item.get("Mounts")
    if not isinstance(config, dict) or not isinstance(host_config, dict) or not isinstance(mounts, list):
        raise PreflightError("container structure invalid")
    image = config.get("Image")
    if not isinstance(image, str) or not image.startswith(OFFICIAL_IMAGES) or len(image) > 256:
        raise PreflightError("container image invalid")
    name = item.get("Name")
    if not isinstance(name, str) or not name.startswith("/") or not SAFE_NAME_RE.fullmatch(name[1:]):
        raise PreflightError("container name invalid")
    config_mounts = [mount for mount in mounts if isinstance(mount, dict) and mount.get("Destination") == "/config"]
    if len(config_mounts) != 1:
        raise PreflightError("config mount invalid")
    config_mount = config_mounts[0]
    mount_type = config_mount.get("Type")
    mount_source = _safe_absolute_path(config_mount.get("Source"))
    if mount_type not in {"bind", "volume"} or mount_source is None:
        raise PreflightError("config mount source invalid")

    labels = config.get("Labels")
    labels = labels if isinstance(labels, dict) else {}
    compose_values = {key: labels.get(label) for key, label in COMPOSE_LABELS.items()}
    compose_detected = all(isinstance(value, str) and value for value in compose_values.values())
    compose_files: list[str] = []
    compose_safe = False
    if compose_detected:
        project = compose_values["project"]
        service = compose_values["service"]
        working_dir = _safe_absolute_path(compose_values["working_dir"])
        raw_files = compose_values["config_files"]
        if (
            not SAFE_NAME_RE.fullmatch(str(project))
            or not SAFE_NAME_RE.fullmatch(str(service))
            or working_dir is None
            or not isinstance(raw_files, str)
        ):
            raise PreflightError("compose labels invalid")
        for raw_path in raw_files.split(","):
            candidate = raw_path.strip()
            if not candidate.startswith("/"):
                candidate = os.path.join(working_dir, candidate)
            candidate = _safe_absolute_path(os.path.normpath(candidate))
            if candidate is None:
                raise PreflightError("compose file path invalid")
            compose_files.append(candidate)
        compose_safe = bool(compose_files) and all(_safe_compose_file(path) for path in compose_files)

    restart = host_config.get("RestartPolicy")
    restart_name = restart.get("Name") if isinstance(restart, dict) else None
    network_mode = host_config.get("NetworkMode")
    if restart_name not in {"no", "always", "unless-stopped", "on-failure"}:
        restart_name = "other"
    if not isinstance(network_mode, str) or len(network_mode) > 128:
        network_mode = "other"

    docker_version = _run_text(["/usr/bin/docker", "version", "--format", "{{.Server.Version}}"])
    architecture = _run_text(["/usr/bin/docker", "info", "--format", "{{.Architecture}}"])
    compose_available = subprocess.run(
        ["/usr/bin/docker", "compose", "version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    ).returncode == 0
    disk = os.statvfs(mount_source)
    method = "docker_compose" if compose_detected and compose_safe and compose_available else "manual_recreate_required"
    return {
        "schema_version": 1,
        "container_identity_hash": hashlib.sha256(container_id.encode("ascii")).hexdigest(),
        "container_name": name[1:],
        "image": image,
        "image_identity_hash": hashlib.sha256(str(item.get("Image", "")).encode("ascii", "strict")).hexdigest(),
        "network_mode": network_mode,
        "restart_policy": restart_name,
        "config_mount_type": mount_type,
        "config_mount_source": mount_source,
        "docker_server_version": docker_version,
        "architecture": architecture,
        "config_free_bytes": disk.f_bavail * disk.f_frsize,
        "compose": {
            "detected": compose_detected,
            "available": compose_available,
            "files_safe": compose_safe,
            "project": compose_values["project"] if compose_detected else None,
            "service": compose_values["service"] if compose_detected else None,
            "working_dir": compose_values["working_dir"] if compose_detected else None,
            "config_files": compose_files,
        },
        "upgrade_method": method,
        "environment_exported": False,
        "read_only": True,
    }


def main() -> int:
    try:
        result = collect()
    except (PreflightError, OSError, subprocess.SubprocessError, ValueError):
        print("HA_CONTAINER_UPGRADE_PREFLIGHT_FAILED", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
