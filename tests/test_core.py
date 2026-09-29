from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import project_relay.core as c


def test_extract_shell_block():
    text = "before\n```bash\necho hello\ngit status\n```\nafter"
    assert c.choose_shell_block(text) == "echo hello\ngit status"


def test_refuse_multiple_blocks():
    text = "```bash\necho one\n```\n```sh\necho two\n```"
    try:
        c.choose_shell_block(text)
        assert False, "expected RelayError"
    except c.RelayError:
        pass
    assert c.choose_shell_block(text, 2) == "echo two"


def test_raw_shell_fallback():
    text = "cd /tmp\necho hi\ngit status"
    assert c.choose_shell_block(text) == text


def test_command_warning_for_nounset():
    warnings = c.command_warnings("set -euo pipefail\necho hi")
    assert any("nounset" in w for w in warnings)


def test_deterministic_loop_vote():
    cfg = {"repeat_threshold": 3, "command_similarity": 0.9}
    base = {
        "command": "pytest tests/test_x.py",
        "terminal_output": "ERROR: file not found: tests/test_x.py",
        "git_after": {"head": "abc"},
    }
    cycles = [dict(base) for _ in range(3)]
    vote = c.deterministic_vote(cycles, cfg)
    assert vote.verdict == "LOOP"


def test_deterministic_progress_on_head_change():
    cycles = [{
        "command": "git commit -m x",
        "terminal_output": "committed",
        "git_before": {"head": "aaa"},
        "git_after": {"head": "bbb"},
    }]
    vote = c.deterministic_vote(cycles, {"repeat_threshold": 3, "command_similarity": 0.9})
    # A real HEAD change is direct evidence of repository progress.
    assert vote.verdict == "PROGRESS"


def test_aggregate_loop_majority():
    decision = c.aggregate_votes([
        c.Vote("a", "LOOP", "x"),
        c.Vote("b", "LOOP", "x"),
        c.Vote("c", "PROGRESS", "x"),
    ])
    assert decision.status == "HUMAN_REQUIRED"


def test_aggregate_progress_majority():
    decision = c.aggregate_votes([
        c.Vote("a", "PROGRESS", "x"),
        c.Vote("b", "PROGRESS", "x"),
        c.Vote("c", "UNCERTAIN", "x"),
    ])
    assert decision.status == "CONTINUE"


def test_build_gpt_prompt():
    project = c.Project("TEST", Path("/tmp/test"))
    cycle = {
        "cwd": "/tmp/test",
        "git_before": {"head": "aaa"},
        "git_after": {"branch": "main", "head": "bbb", "git_status": " M x.py"},
        "terminal_output": "9 passed",
    }
    decision = c.Decision("CONTINUE", [c.Vote("d", "PROGRESS", "ok")], "ok")
    with patch.object(c, "load_config", return_value=c.default_config()):
        prompt = c.build_gpt_prompt(project, cycle, decision)
    assert "Project: TEST" in prompt
    assert "HEAD before command: aaa" in prompt
    assert "HEAD after command: bbb" in prompt
    assert "9 passed" in prompt


def test_deterministic_single_cycle_head_change_is_progress():
    cycles = [{
        "command": "git commit -m test",
        "terminal_output": "commit complete",
        "git_before": {
            "head": "aaa",
        },
        "git_after": {
            "head": "bbb",
        },
    }]

    vote = c.deterministic_vote(
        cycles,
        {
            "repeat_threshold": 3,
            "command_similarity": 0.9,
        },
    )

    assert vote.verdict == "PROGRESS"


def test_deterministic_same_failure_different_commands_is_loop():
    cycles = [
        {
            "command": (
                "python -m pytest "
                "tests/test_missing.py"
            ),
            "terminal_output": (
                "ERROR: file or directory not found: "
                "tests/test_missing.py"
            ),
            "git_after": {
                "head": "abc",
            },
        },
        {
            "command": (
                "pytest -q "
                "tests/test_missing.py"
            ),
            "terminal_output": (
                "ERROR: file or directory not found: "
                "tests/test_missing.py"
            ),
            "git_after": {
                "head": "abc",
            },
        },
        {
            "command": (
                "cd backend && "
                "python -m pytest "
                "../tests/test_missing.py"
            ),
            "terminal_output": (
                "ERROR: file or directory not found: "
                "../tests/test_missing.py"
            ),
            "git_after": {
                "head": "abc",
            },
        },
    ]

    vote = c.deterministic_vote(
        cycles,
        {
            "repeat_threshold": 3,
            "command_similarity": 0.92,
        },
    )

    assert vote.verdict == "LOOP"


def test_deterministic_progressive_debugging_not_loop():
    cycles = [
        {
            "command": (
                "pytest tests/test_x.py"
            ),
            "terminal_output": (
                "ERROR: file not found: "
                "tests/test_x.py"
            ),
            "git_after": {
                "head": "abc",
            },
        },
        {
            "command": (
                "find tests -name '*x*'"
            ),
            "terminal_output": (
                "tests/test_real_x.py"
            ),
            "git_after": {
                "head": "abc",
            },
        },
        {
            "command": (
                "pytest tests/test_real_x.py"
            ),
            "terminal_output": (
                "9 passed in 0.12s"
            ),
            "git_after": {
                "head": "abc",
            },
        },
    ]

    vote = c.deterministic_vote(
        cycles,
        {
            "repeat_threshold": 3,
            "command_similarity": 0.92,
        },
    )

    assert vote.verdict == "PROGRESS"
