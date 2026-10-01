from __future__ import annotations

import pytest

from project_relay.relay import memory, store
from project_relay.relay.engine import RelayEngine, RelayRefused
from project_relay.relay.runner import RunResult
from project_relay.storage.database import RelayDatabase

FENCE = "`" * 3
WORKER_URL = "https://chatgpt.com/c/0f0f0f0f-1111-4222-8333-444444444444"
PM_URL = "https://claude.ai/chat/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"


@pytest.fixture
def env(tmp_path):
    db = RelayDatabase(tmp_path / "relay.db")
    engine = RelayEngine(db, config={}, runner=lambda *a, **k: RunResult(0, "ok\nCOMMAND_EXIT_CODE=0", False),
                         judge=lambda p, r, c: {"status": "CONTINUE", "reason": "", "votes": [], "progress": True},
                         git=lambda root: {"branch": "main", "head": "h", "git_status": ""})
    root = tmp_path / "repo"
    root.mkdir()
    yield engine, db, str(root)
    db.close()


def drive(engine, text, *, page, lease, user, assistant):
    job = engine.poll(lease=lease, page_url=page)
    assert job["type"] == "submit", job
    engine.ready(lease=lease, request_id=job["request_id"], page_url=page, baseline=[])
    engine.accepted(lease=lease, request_id=job["request_id"], user_turn_id=user, page_url=page)
    engine.complete(lease=lease, request_id=job["request_id"], assistant_turn_id=assistant, text=text)
    while engine.step():
        pass
    return job


def latest(engine, db):
    rt = store.all_runtimes(db.conn)[0]
    return store.latest_request(db.conn, rt["session_id"])


def test_message_goes_to_the_pms_next_message_only(env):
    engine, db, root = env
    engine.start(name="demo", root=root, mode="ade", goal="G", conversation_url=WORKER_URL, pm_conversation_url=PM_URL)
    drive(engine, "RELAY_TASK\necho hi\nEND_RELAY_TASK", page=PM_URL, lease="p",
          user="claude:user:r1", assistant="claude:assistant:r1")
    engine.tell("demo", "The repo is on GitHub: github.com/me/demo. Push to branch relay/work.")
    assert engine.status()[0]["pending_human"] == ["The repo is on GitHub: github.com/me/demo. Push to branch relay/work."]
    # The Worker's task does not get it; the PM's next message (the evidence) does, at the top.
    assert "GitHub" not in latest(engine, db)["prompt_text"]
    drive(engine, f"{FENCE}bash\necho hi\n{FENCE}", page=WORKER_URL, lease="w",
          user="group:user:a", assistant="group:assistant:a")
    evidence = latest(engine, db)
    assert evidence["role"] == "pm" and evidence["prompt_text"].startswith("Message from the human")
    assert "github.com/me/demo" in evidence["prompt_text"] and "Result of the Worker's command" in evidence["prompt_text"]
    assert engine.status()[0]["pending_human"] == []
    # Only once.
    drive(engine, "RELAY_TASK\necho again\nEND_RELAY_TASK", page=PM_URL, lease="p",
          user="claude:user:r2", assistant="claude:assistant:r2")
    drive(engine, f"{FENCE}bash\necho again\n{FENCE}", page=WORKER_URL, lease="w",
          user="group:user:b", assistant="group:assistant:b")
    assert "github.com/me/demo" not in latest(engine, db)["prompt_text"]


def test_remember_also_keeps_it_in_project_memory(env):
    engine, db, root = env
    engine.start(name="demo", root=root, mode="ade", goal="G", conversation_url=WORKER_URL, pm_conversation_url=PM_URL)
    engine.tell("demo", "Remote: github.com/me/demo", remember=True)
    assert [n["text"] for n in memory.active_notes(db.conn, "project-demo")] == ["Remote: github.com/me/demo"]


def test_message_waiting_at_start_goes_with_the_kickoff(env):
    engine, db, root = env
    engine.start(name="demo", root=root, mode="ade", goal="G", conversation_url=WORKER_URL, pm_conversation_url=PM_URL)
    engine.stop("demo")
    engine.tell("demo", "Use the GitHub repo")
    engine.start(name="demo", root=root, mode="ade", goal="G", conversation_url=WORKER_URL, pm_conversation_url=PM_URL)
    kickoff = latest(engine, db)
    assert kickoff["kind"] == "PM_PLAN" and kickoff["prompt_text"].startswith("Message from the human")


def test_solo_mode_tells_chatgpt(env):
    engine, db, root = env
    engine.start(name="demo", root=root, conversation_url=WORKER_URL)
    drive(engine, f"{FENCE}bash\necho hi\n{FENCE}", page=WORKER_URL, lease="w",
          user="group:user:a", assistant="group:assistant:a")
    assert engine.tell("demo", "There is a GitHub remote")["in_next_message_now"]
    job = drive(engine, f"{FENCE}bash\necho two\n{FENCE}", page=WORKER_URL, lease="w",
                user="group:user:b", assistant="group:assistant:b")
    assert job["prompt"].startswith("Message from the human")


def test_tell_needs_text_and_a_known_run(env):
    engine, db, root = env
    with pytest.raises(RelayRefused):
        engine.tell("nope", "hi")
    engine.start(name="demo", root=root, conversation_url=WORKER_URL)
    with pytest.raises(RelayRefused):
        engine.tell("demo", "   ")


def test_a_queued_pm_message_that_was_not_typed_yet_is_replaced_with_the_message_on_top(env):
    engine, db, root = env
    engine.start(name="demo", root=root, mode="ade", goal="Ship it", conversation_url=WORKER_URL,
                 pm_conversation_url=PM_URL)
    kickoff = latest(engine, db)
    assert kickoff["state"] == "QUEUED"
    result = engine.tell("demo", "The repo has a GitHub remote")
    assert result["in_next_message_now"]
    replacement = latest(engine, db)
    assert replacement["id"] != kickoff["id"] and replacement["kind"] == "PM_PLAN"
    assert replacement["prompt_text"].startswith("Message from the human")
    assert replacement["prompt_text"].endswith(kickoff["prompt_text"])
    old = store.get_request(db.conn, kickoff["id"])
    assert old["state"] == "CANCELLED" and old["successor_request_id"] == replacement["id"]
    assert engine.status()[0]["pending_human"] == []
    # Once the browser picked it up, a new message waits for the next one instead.
    job = engine.poll(lease="p", page_url=PM_URL)
    assert job["request_id"] == replacement["id"]
    assert not engine.tell("demo", "later")["in_next_message_now"]
