#!/usr/bin/env python3
"""Build a secret-safe manifest of files tracked by the project Git index."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
MANIFEST_PATH = "reports/PHASE-66-REPOSITORY-MANIFEST.tsv"


def tracked_paths(project_dir: Path = PROJECT_DIR) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    return sorted(
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )


def repository_paths(project_dir: Path = PROJECT_DIR) -> list[str]:
    """Return tracked plus visible untracked files, excluding ignored runtime data."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )
    return sorted(
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item and item.decode("utf-8", errors="surrogateescape") != MANIFEST_PATH
    )


def categories_for(relative_path: str) -> tuple[str, ...]:
    path = relative_path.lower()
    name = Path(path).name
    categories: set[str] = set()

    if path.startswith("tests/"):
        categories.add("test")
    elif path.startswith("config/systemd/"):
        categories.add("systemd-config")
    elif path.startswith("scripts/") or name == "talk-to-home-butler.sh":
        categories.add("source")
    elif path.startswith("config/") or path.startswith("models/"):
        categories.add("config")
    elif path.endswith(".md") or path.startswith(("research/", "reports/", "skills/")):
        categories.add("docs")
    elif path.startswith("assets/"):
        categories.add("binary")
    else:
        categories.add("config")

    if path.startswith("hermes/cache/") or "_cache." in path:
        categories.update(("runtime", "cache"))
    if path.startswith("hermes/logs/") or name.endswith(".log"):
        categories.update(("runtime", "log"))
    if name.endswith((".db", ".sqlite", ".sqlite3")):
        categories.update(("runtime", "db", "sensitive"))
    if name.endswith((".db-wal", ".db-shm", "-wal", "-shm")):
        categories.update(("runtime", "wal", "sensitive"))
    if name in {
        ".hermes_history",
        ".mcp-discovery.lock",
        ".update_check",
        "auth.lock",
        "context_length_cache.yaml",
        ".curator_state",
        ".usage.json",
        ".usage.json.lock",
    }:
        categories.add("runtime")
    if name in {".hermes_history", "auth.lock"}:
        categories.add("sensitive")
    if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".bin")):
        categories.add("binary")

    return tuple(sorted(categories))


def _fingerprint(path: Path) -> tuple[int, str, str]:
    metadata = path.lstat()
    if path.is_symlink():
        payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
        kind = "symlink"
    else:
        payload = path.read_bytes()
        kind = "file"
    return metadata.st_size, kind, hashlib.sha256(payload).hexdigest()


def build_manifest(project_dir: Path = PROJECT_DIR) -> list[tuple[str, str, int, str, str]]:
    rows: list[tuple[str, str, int, str, str]] = []
    for relative_path in repository_paths(project_dir):
        size, kind, digest = _fingerprint(project_dir / relative_path)
        rows.append(
            (relative_path, ",".join(categories_for(relative_path)), size, kind, digest)
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    rows = build_manifest()
    if args.summary:
        counts: dict[str, int] = {}
        for _, categories, _, _, _ in rows:
            for category in categories.split(","):
                counts[category] = counts.get(category, 0) + 1
        print(f"repository_files\t{len(rows)}")
        print(f"repository_bytes\t{sum(row[2] for row in rows)}")
        for category in sorted(counts):
            print(f"category:{category}\t{counts[category]}")
        return 0

    print("path\tcategories\tbytes\tkind\tsha256")
    for row in rows:
        print("\t".join((row[0], row[1], str(row[2]), row[3], row[4])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
