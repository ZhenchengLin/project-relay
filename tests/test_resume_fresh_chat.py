from __future__ import annotations

from project_relay.relay import store
from project_relay.relay.engine import RelayEngine
from project_relay.relay.runner import RunResult
from project_relay.storage.database import RelayDatabase

WORKER_URL = "https://chatgpt.com/c/0f0f0f0f-1111-4222-8333-444444444444"
PM_URL = "https://claude.ai/chat/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"


def make(tmp_path):
    db = RelayDatabase(tmp_path / "relay.db")
    engine = RelayEngine(db, config={}, runner=lambda *a, **k: RunResult(0, "", False),
                         judge=lambda p, r, c: {"status": "CONTINUE", "reason": "", "votes": [], "progress": True},
                         git=lambda root: {"branch": "main", "head": "h", "git_status": ""})
    root = tmp_path / "repo"
    root.mkdir()
    return engine, db, str(root)


def lose_until_human(engine, db, page):
    """The first message never shows up in the new chat, until Relay asks a human."""
    for _ in range(10):
        rt = store.all_runtimes(db.conn)[0]
        if rt["status"] == "HUMAN_REQUIRED":
            return rt
        job = engine.poll(lease="t", page_url=page)
        engine.ready(lease="t", request_id=job["request_id"], page_url=page, baseline=[])
        engine.failure(lease="t", request_id=job["request_id"], code="NOT_PERSISTED", message="gone")
        while engine.step():
            pass
    raise AssertionError("never asked a human")


def test_resume_into_a_chat_that_never_got_its_kickoff_resends_the_kickoff(tmp_path):
    engine, db, root = make(tmp_path)
    engine.start(name="demo", root=root, mode="ade", goal="Ship the parser", conversation_url=WORKER_URL)
    rt = lose_until_human(engine, db, "https://claude.ai/new")
    engine.resume("demo", "try again please")
    req = store.latest_request(db.conn, rt["session_id"])
    assert req["kind"] == "PM_PLAN" and req["role"] == "pm"
    assert "Ship the parser" in req["prompt_text"]
    assert req["prompt_text"].rstrip().endswith("Note from the human: try again please")


def test_resume_without_message_resends_the_kickoff_unchanged(tmp_path):
    engine, db, root = make(tmp_path)
    engine.start(name="demo", root=root, mode="ade", goal="Ship the parser", conversation_url=WORKER_URL)
    rt = lose_until_human(engine, db, "https://claude.ai/new")
    first = db.conn.execute("SELECT prompt_text FROM requests WHERE session_id = ? ORDER BY sequence_number LIMIT 1",
                            (rt["session_id"],)).fetchone()[0]
    engine.resume("demo")
    assert store.latest_request(db.conn, rt["session_id"])["prompt_text"] == first


def test_resume_into_a_known_chat_sends_a_note(tmp_path):
    engine, db, root = make(tmp_path)
    engine.start(name="demo", root=root, mode="ade", goal="Ship the parser",
                 conversation_url=WORKER_URL, pm_conversation_url=PM_URL)
    rt = lose_until_human(engine, db, PM_URL)
    engine.resume("demo", "try again please")
    req = store.latest_request(db.conn, rt["session_id"])
    assert req["kind"] == "USER" and req["prompt_text"].startswith("try again please")
