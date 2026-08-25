#!/usr/bin/env python3
"""Supervise the pinned Windows Ollama GPU server directly from Ubuntu."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import ollama_endpoint  # noqa: E402
import model_runtime_policy  # noqa: E402


OLLAMA_EXE = Path("/mnt/h/Ollama/ollama.exe")
WINDOWS_MODEL_ROOT = r"H:\OllamaModels"
PINNED_SHA256 = "82e3b496c059720fa1c40a09af7803778f4bb40f32fb459a1d799c822a217843"
POLL_SECONDS = 10
STARTUP_SECONDS = 45
DEFAULT_CONTEXT_PROFILE = "dialogue"
LOCK_PATH = Path(
    os.environ.get(
        "HOME_BUTLER_GPU_SUPERVISOR_LOCK",
        "/home/homebutler/.local/state/home-butler/windows-gpu-supervisor.lock",
    )
)


class GpuSupervisorError(RuntimeError):
    """Fixed, secret-free GPU supervisor failure."""


class GpuSupervisorAlreadyRunning(GpuSupervisorError):
    """Another verified local supervisor already owns the process lock."""


def acquire_lock(path: Path = LOCK_PATH) -> int:
    """Hold one private non-following flock for the supervisor lifetime."""
    try:
        parent = path.parent
        metadata = parent.stat(follow_symlinks=False)
    except OSError as error:
        raise GpuSupervisorError("GPU supervisor state directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or parent.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise GpuSupervisorError("GPU supervisor state directory is unsafe")
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        lock_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or lock_metadata.st_nlink != 1
        ):
            raise GpuSupervisorError("GPU supervisor lock is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        return descriptor
    except BlockingIOError as error:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise GpuSupervisorAlreadyRunning("GPU supervisor is already running") from error
    except (OSError, GpuSupervisorError):
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise


def validate_binary(path: Path = OLLAMA_EXE) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise GpuSupervisorError("pinned Ollama executable is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1_048_576:
        raise GpuSupervisorError("pinned Ollama executable is unsafe")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise GpuSupervisorError("pinned Ollama executable is unreadable") from error
    if digest.hexdigest() != PINNED_SHA256:
        raise GpuSupervisorError("pinned Ollama executable changed")


def current_endpoint() -> ollama_endpoint.OllamaEndpoint:
    address = ollama_endpoint._read_default_gateway(ollama_endpoint.ROUTE_PATH)
    return ollama_endpoint.OllamaEndpoint(
        f"http://{address}:{ollama_endpoint.OLLAMA_PORT}",
        address,
        ollama_endpoint.OLLAMA_PORT,
    )


def launch(path: Path, endpoint: ollama_endpoint.OllamaEndpoint) -> subprocess.Popen[bytes]:
    context_window = model_runtime_policy.get_profile(
        DEFAULT_CONTEXT_PROFILE
    ).context_window
    environment = {
        **os.environ,
        "OLLAMA_HOST": f"{endpoint.host}:{endpoint.port}",
        "OLLAMA_MODELS": WINDOWS_MODEL_ROOT,
        "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_VULKAN": "1",
        "OLLAMA_LLM_LIBRARY": "vulkan",
        "OLLAMA_CONTEXT_LENGTH": str(context_window),
        "OLLAMA_FLASH_ATTENTION": "0",
        "OLLAMA_KV_CACHE_TYPE": "q8_0",
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_KEEP_ALIVE": "5m",
    }
    try:
        return subprocess.Popen(
            [str(path), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            cwd=str(path.parent),
            close_fds=True,
        )
    except OSError as error:
        raise GpuSupervisorError("Windows Ollama launch failed") from error


def supervise(
    *,
    once: bool = False,
    probe: Callable[[ollama_endpoint.OllamaEndpoint], bool] = ollama_endpoint._probe,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    validate_binary()
    child: subprocess.Popen[bytes] | None = None
    while True:
        endpoint = current_endpoint()
        if probe(endpoint):
            if once:
                print('{"schema_version":1,"status":"gpu_endpoint_ready","launched":false}')
                return 0
            sleeper(POLL_SECONDS)
            continue
        if child is None or child.poll() is not None:
            child = launch(OLLAMA_EXE, endpoint)
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline and child.poll() is None:
            if probe(endpoint):
                if once:
                    print('{"schema_version":1,"status":"gpu_endpoint_ready","launched":true}')
                    return 0
                break
            sleeper(1)
        else:
            if child.poll() is None:
                child.terminate()
            raise GpuSupervisorError("Windows Ollama did not become ready")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    descriptor: int | None = None
    try:
        descriptor = acquire_lock()
        return supervise(once=args.once)
    except GpuSupervisorAlreadyRunning:
        print('{"schema_version":1,"status":"gpu_supervisor_already_running"}')
        return 0
    except GpuSupervisorError as error:
        print(str(error), file=sys.stderr)
        return 3
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
