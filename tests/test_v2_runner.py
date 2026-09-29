from __future__ import annotations

import os
import time

from project_relay.relay.runner import run_bash


def test_exit_code_and_merged_output(tmp_path):
    result = run_bash("echo out; echo err >&2; exit 3", cwd=tmp_path, timeout_seconds=10)
    assert result.return_code == 3 and not result.timed_out
    assert "out" in result.output and "err" in result.output
    assert result.output.endswith("COMMAND_EXIT_CODE=3")


def test_runs_in_project_directory(tmp_path):
    result = run_bash("pwd", cwd=tmp_path, timeout_seconds=10)
    assert os.path.realpath(str(tmp_path)) in result.output


def test_timeout_kills_whole_process_group(tmp_path):
    marker = tmp_path / "child.pid"
    started = time.monotonic()
    result = run_bash(
        f"sleep 60 & echo $! > {marker}; sleep 60",
        cwd=tmp_path, timeout_seconds=1,
    )
    assert result.timed_out and result.return_code == 124
    assert time.monotonic() - started < 15
    assert "PROJECT_RELAY_TIMEOUT=1" in result.output
    child = int(marker.read_text())
    time.sleep(0.2)
    try:
        os.kill(child, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive


def test_detached_background_child_does_not_hang_cycle(tmp_path):
    pidfile = tmp_path / "bg.pid"
    started = time.monotonic()
    result = run_bash(
        f"nohup sleep 30 > /dev/null 2>&1 & echo $! > {pidfile}; echo started",
        cwd=tmp_path, timeout_seconds=20,
    )
    assert result.return_code == 0 and "started" in result.output
    assert time.monotonic() - started < 5
    os.kill(int(pidfile.read_text()), 9)


def test_on_start_receives_pid(tmp_path):
    seen = []
    run_bash("true", cwd=tmp_path, timeout_seconds=10, on_start=seen.append)
    assert len(seen) == 1 and seen[0] > 0
