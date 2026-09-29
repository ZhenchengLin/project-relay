"""Managed local Bash execution: own process group, bounded output, hard timeout."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024
KILL_GRACE_SECONDS = 5.0


def bash_path() -> str:
    """bash on PATH (Homebrew, Linux distros), else the system /bin/bash."""
    return shutil.which("bash") or "/bin/bash"


@dataclass(frozen=True)
class RunResult:
    return_code: int
    output: str
    timed_out: bool


def _kill_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_bash(
    script: str,
    *,
    cwd: str | Path,
    timeout_seconds: float,
    on_start: Callable[[int], None] | None = None,
) -> RunResult:
    """
    Run one script with bash in a new session (process group). POSIX only
    (macOS, Linux, WSL): process groups do not exist on native Windows.

    stdout and stderr are merged in order. On timeout the whole group
    gets SIGTERM, then SIGKILL after a grace period, so background
    children cannot outlive the cycle.
    """
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".sh", prefix="project-relay-", delete=False
    ) as stream:
        stream.write(script)
        stream.write("\n")
        script_path = Path(stream.name)

    chunks: list[bytes] = []
    size = 0
    truncated = False

    try:
        proc = subprocess.Popen(
            [bash_path(), str(script_path)],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )

        if on_start is not None:
            on_start(proc.pid)

        def pump() -> None:
            nonlocal size, truncated
            assert proc.stdout is not None
            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                if size < OUTPUT_LIMIT_BYTES:
                    chunks.append(chunk[: OUTPUT_LIMIT_BYTES - size])
                    size += len(chunks[-1])
                if size >= OUTPUT_LIMIT_BYTES:
                    truncated = True

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()

        timed_out = False
        try:
            code = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _kill_group(proc, signal.SIGKILL)
                proc.wait()
            code = 124

        # The script exited. Background children that redirected their
        # output (e.g. `nohup server > log &`) may keep running, but any
        # still holding our pipe would hang the cycle, so kill those.
        reader.join(timeout=2)
        if reader.is_alive():
            _kill_group(proc, signal.SIGKILL)
            reader.join(timeout=10)

    finally:
        script_path.unlink(missing_ok=True)

    output = b"".join(chunks).decode("utf-8", errors="replace").rstrip()
    if truncated:
        output += f"\nPROJECT_RELAY_OUTPUT_TRUNCATED={OUTPUT_LIMIT_BYTES}B"
    if timed_out:
        output += f"\nPROJECT_RELAY_TIMEOUT={timeout_seconds}s"
    output = (output + "\n" if output else "") + f"COMMAND_EXIT_CODE={code}"

    return RunResult(return_code=int(code), output=output, timed_out=timed_out)
