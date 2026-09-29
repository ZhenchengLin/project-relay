import io
import json
from pathlib import Path
from unittest.mock import patch

import project_relay.core as c


def project():
    return c.Project(
        "TEST",
        Path("/tmp/test"),
    )


def cycle(output="STAGE COMPLETE"):
    return {
        "command": "echo run",
        "terminal_output": output,
        "git_before": {
            "head": "aaa",
        },
        "git_after": {
            "head": "aaa",
        },
    }


def voter(
    name="logic_model",
    model="qwen3:1.7b",
    role="logic_judge",
):
    return {
        "name": name,
        "model": model,
        "role": role,
        "enabled": True,
    }


def fake_ollama(payload):
    body = json.dumps(
        payload
    ).encode(
        "utf-8"
    )

    return io.BytesIO(
        body
    )


def test_single_cycle_loop_vote_is_downgraded():
    with patch.object(
        c.urllib.request,
        "urlopen",
        return_value=fake_ollama(
            {
                "response": "LOOP",
            }
        ),
    ):
        vote = c.ollama_vote(
            project(),
            [cycle()],
            voter(),
            "http://127.0.0.1:11434",
        )

    assert vote.verdict == "UNCERTAIN"
    assert "at least two" in vote.reason


def test_qwen3_vote_requests_direct_response():
    observed = {}

    def urlopen(req, timeout):
        observed.update(
            json.loads(
                req.data.decode(
                    "utf-8"
                )
            )
        )

        return fake_ollama(
            {
                "response": "PROGRESS",
            }
        )

    with patch.object(
        c.urllib.request,
        "urlopen",
        side_effect=urlopen,
    ):
        vote = c.ollama_vote(
            project(),
            [cycle()],
            voter(
                name="local_llm",
                model="qwen3.5:4b",
                role="progress_judge",
            ),
            "http://127.0.0.1:11434",
        )

    assert observed["think"] is False
    assert vote.verdict == "PROGRESS"


def test_thinking_text_is_valid_fallback():
    with patch.object(
        c.urllib.request,
        "urlopen",
        return_value=fake_ollama(
            {
                "response": "",
                "thinking":
                    "Evidence shows completion.\nPROGRESS",
            }
        ),
    ):
        vote = c.ollama_vote(
            project(),
            [cycle()],
            voter(
                name="local_llm",
                model="qwen3.5:4b",
                role="progress_judge",
            ),
            "http://127.0.0.1:11434",
        )

    assert vote.verdict == "PROGRESS"


def test_progress_plus_uncertain_votes_continues_without_loop():
    decision = c.aggregate_votes(
        [
            c.Vote(
                "deterministic",
                "PROGRESS",
                "success",
            ),
            c.Vote(
                "local_llm",
                "UNCERTAIN",
                "uncertain",
                model="qwen3.5:4b",
            ),
            c.Vote(
                "logic_model",
                "UNCERTAIN",
                "not enough cycles",
                model="qwen3:1.7b",
            ),
        ]
    )

    assert decision.status == "CONTINUE"
