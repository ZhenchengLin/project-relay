from pathlib import Path
from unittest.mock import patch

import pytest

import project_relay.core as c


def project():
    return c.Project(
        "TEST",
        Path("/tmp/test"),
    )


def pending():
    return {
        "command": "pytest -q",
        "command_sha256": "cmd-sha",
        "git_before": {
            "branch": "main",
            "head": "aaa",
            "git_status": "",
        },
    }


def after():
    return {
        "branch": "main",
        "head": "bbb",
        "git_status": "",
    }


def test_complete_cycle_rejects_missing_pending():
    with patch.object(
        c,
        "load_pending_command",
        return_value=None,
    ):
        with pytest.raises(
            c.RelayError,
            match="No pending command",
        ):
            c.complete_cycle(
                project(),
                "1 passed",
                persist=False,
            )


def test_preview_cycle_does_not_persist():
    decision = c.Decision(
        "CONTINUE",
        [
            c.Vote(
                "deterministic",
                "PROGRESS",
                "ok",
            )
        ],
        "ok",
    )

    with (
        patch.object(
            c,
            "load_pending_command",
            return_value=pending(),
        ),
        patch.object(
            c,
            "git_metadata",
            return_value=after(),
        ),
        patch.object(
            c,
            "load_cycles",
            return_value=[],
        ),
        patch.object(
            c,
            "run_watchdog",
            return_value=decision,
        ),
        patch.object(
            c,
            "append_cycle",
        ) as append_cycle,
        patch.object(
            c,
            "clear_pending",
        ) as clear_pending,
    ):
        cycle, result = (
            c.complete_cycle(
                project(),
                "1 passed",
                persist=False,
            )
        )

    assert result.status == "CONTINUE"

    assert (
        cycle["command"]
        == "pytest -q"
    )

    assert (
        cycle["watchdog"]["status"]
        == "CONTINUE"
    )

    append_cycle.assert_not_called()
    clear_pending.assert_not_called()


def test_persist_completed_cycle_appends_then_clears():
    cycle = {
        "cycle_id": 123,
        "command": "pytest -q",
        "command_sha256": "cmd-sha",
        "terminal_output_sha256":
            "out-sha",
    }

    with (
        patch.object(
            c,
            "load_pending_command",
            return_value=pending(),
        ),
        patch.object(
            c,
            "load_cycles",
            return_value=[],
        ),
        patch.object(
            c,
            "append_cycle",
        ) as append_cycle,
        patch.object(
            c,
            "clear_pending",
        ) as clear_pending,
    ):
        c.persist_completed_cycle(
            project(),
            cycle,
        )

    append_cycle.assert_called_once()
    clear_pending.assert_called_once()


def test_persist_rejects_duplicate_cycle():
    cycle = {
        "cycle_id": 123,
        "command": "pytest -q",
        "command_sha256": "cmd-sha",
        "terminal_output_sha256":
            "out-sha",
    }

    with (
        patch.object(
            c,
            "load_pending_command",
            return_value=pending(),
        ),
        patch.object(
            c,
            "load_cycles",
            return_value=[cycle.copy()],
        ),
        patch.object(
            c,
            "append_cycle",
        ) as append_cycle,
    ):
        with pytest.raises(
            c.RelayError,
            match="already been recorded",
        ):
            c.persist_completed_cycle(
                project(),
                cycle,
            )

    append_cycle.assert_not_called()


def test_build_prompt_supports_legacy_head_fields():
    legacy = (
        "HEAD: {head}\n"
        "Git status:\n"
        "{git_status}\n"
        "Output:\n"
        "{terminal_output}"
    )

    decision = c.Decision(
        "CONTINUE",
        [
            c.Vote(
                "deterministic",
                "PROGRESS",
                "ok",
            )
        ],
        "ok",
    )

    cycle = {
        "cwd": "/tmp/test",
        "git_before": {
            "head": "aaa",
        },
        "git_after": {
            "branch": "main",
            "head": "bbb",
            "git_status":
                " M file.txt",
        },
        "terminal_output":
            "1 passed",
    }

    with patch.object(
        c,
        "load_config",
        return_value={
            "prompt_template": legacy,
        },
    ):
        text = c.build_gpt_prompt(
            project(),
            cycle,
            decision,
        )

    assert "HEAD: bbb" in text
    assert " M file.txt" in text
    assert "1 passed" in text
