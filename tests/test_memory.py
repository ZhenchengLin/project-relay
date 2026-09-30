from __future__ import annotations

import pytest

from project_relay.relay import memory, store
from project_relay.relay.engine import RelayEngine
from project_relay.relay.runner import RunResult
from project_relay.storage.database import RelayDatabase

FENCE = "`" * 3
WORKER_URL = "https://chatgpt.com/c/0f0f0f0f-1111-4222-8333-444444444444"
PM_URL = "https://claude.ai/chat/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"


# ------------------------------------------------------------------ parsing

def test_parse_notes():
    text = "Done.\nRELAY_NOTE: raw HTML evidence fails git diff --check\n**RELAY_NOTE:** keep main linear\nRELAY_NOTE:"
    assert memory.parse_notes(text) == ["raw HTML evidence fails git diff --check", "keep main linear"]


def test_parse_plan_statuses_and_keys():
    text = """Plan:
RELAY_PLAN
- [x] T1 Load the data
- [~] T2: Train the baseline
- [ ] Evaluate on holdout
- [!] T4 Ship
END_RELAY_PLAN
"""
    tasks = memory.parse_plan(text)
    assert [(t.key, t.status) for t in tasks] == [("T1", "done"), ("T2", "doing"), ("T3", "todo"), ("T4", "blocked")]
    assert tasks[1].title == "Train the baseline" and tasks[2].title == "Evaluate on holdout"


def test_parse_plan_absent_or_unclosed():
    assert memory.parse_plan("no plan here") is None
    assert memory.parse_plan("RELAY_PLAN\n- [ ] T1 x") is None
    assert memory.parse_plan("RELAY_PLAN\nEND_RELAY_PLAN") == []  # an explicit empty plan clears it


def test_last_plan_block_wins():
    text = "RELAY_PLAN\n- [ ] T1 a\nEND_RELAY_PLAN\nlater\nRELAY_PLAN\n- [x] T1 a\nEND_RELAY_PLAN"
    assert memory.parse_plan(text)[0].status == "done"


# ------------------------------------------------------------------ storage

@pytest.fixture
def db(tmp_path):
    database = RelayDatabase(tmp_path / "relay.db")
    with database.transaction() as conn:
        store.ensure_project(conn, name="demo", root=str(tmp_path), branch=None)
    yield database
    database.close()


def test_notes_dedupe_remove_and_cap(db):
    with db.transaction() as conn:
        first = memory.add_note(conn, "project-demo", "  keep   main linear ", source="user")
        assert memory.add_note(conn, "project-demo", "keep main linear", source="model") is None
        assert [n["text"] for n in memory.active_notes(conn, "project-demo")] == ["keep main linear"]
        assert memory.remove_note(conn, "project-demo", first)
        assert memory.active_notes(conn, "project-demo") == []
        for i in range(memory.MAX_ACTIVE_NOTES + 5):
            memory.add_note(conn, "project-demo", f"note {i}", source="model")
        notes = memory.active_notes(conn, "project-demo")
        assert len(notes) == memory.MAX_ACTIVE_NOTES and notes[-1]["text"] == f"note {memory.MAX_ACTIVE_NOTES + 4}"


def test_capture_and_render(db):
    reply = "RELAY_NOTE: use Python 3.12\nRELAY_PLAN\n- [x] T1 Setup\n- [ ] T2 Tests\nEND_RELAY_PLAN"
    with db.transaction() as conn:
        assert memory.capture(conn, "project-demo", reply) == {"notes": 1, "plan_tasks": 2}
        text = memory.render(conn, "project-demo")
    assert "- use Python 3.12" in text and "[x] T1 Setup" in text and "[ ] T2 Tests" in text
    assert text.startswith("=== Project memory") and text.endswith("=== End of project memory ===")


def test_render_empty(db):
    with db.transaction() as conn:
        assert memory.render(conn, "project-demo") == ""


# ------------------------------------------------------------ engine wiring

class Runner:
    def __call__(self, script, *, cwd, timeout_seconds, on_start=None):
        return RunResult(0, "ok\nCOMMAND_EXIT_CODE=0", False)


def make_engine(tmp_path, budget=10_000_000):
    database = RelayDatabase(tmp_path / "e.db")
    engine = RelayEngine(database, config={"relay": {"rollover_char_budget": budget}}, runner=Runner(),
                         judge=lambda p, r, c: {"status": "CONTINUE", "reason": "", "votes": [], "progress": True},
                         git=lambda root: {"branch": "main", "head": "h", "git_status": ""})
    root = tmp_path / "repo"
    root.mkdir()
    return engine, database, str(root)


def drive(engine, text, *, key, page, lease, user_id=None):
    job = engine.poll(lease=lease, page_url=page)
    assert job["type"] == "submit", job
    ready_page = page if job["conversation_url"] else (
        "https://claude.ai/new" if "claude" in page else "https://chatgpt.com/")
    engine.ready(lease=lease, request_id=job["request_id"], page_url=ready_page, baseline=[])
    user = user_id or f"group:user:{key}"
    engine.accepted(lease=lease, request_id=job["request_id"], user_turn_id=user, page_url=page)
    assistant = "claude:assistant:" + user.split(":")[2] if user.startswith("claude:") else f"group:assistant:{key}"
    engine.complete(lease=lease, request_id=job["request_id"], assistant_turn_id=assistant, text=text)
    while engine.step():
        pass
    return job


def test_solo_reply_notes_and_plan_are_stored_and_injected_into_rollover(tmp_path):
    engine, database, root = make_engine(tmp_path, budget=2500)
    engine.start(name="demo", root=root, conversation_url=WORKER_URL)
    reply = ("RELAY_NOTE: evidence files are committed as-is\nRELAY_PLAN\n- [x] T1 Capture\n- [ ] T2 Audit\n"
             f"END_RELAY_PLAN\n{FENCE}bash\necho " + "z" * 1200 + f"\n{FENCE}")
    drive(engine, reply, key="a", page=WORKER_URL, lease="w")
    rt = store.all_runtimes(database.conn)[0]
    handoff_req = store.latest_request(database.conn, rt["session_id"])
    assert handoff_req["kind"] == "HANDOFF"
    drive(engine, "RELAY_HANDOFF\nGoal: demo.", key="b", page=WORKER_URL, lease="w")
    seed = store.latest_request(database.conn, rt["session_id"])
    assert seed["kind"] == "ROLLOVER_SEED"
    assert "evidence files are committed as-is" in seed["prompt_text"] and "[x] T1 Capture" in seed["prompt_text"]


def test_ade_worker_replies_do_not_write_memory(tmp_path):
    engine, database, root = make_engine(tmp_path)
    engine.start(name="demo", root=root, mode="ade", goal="G", conversation_url=WORKER_URL, pm_conversation_url=PM_URL)
    drive(engine, "RELAY_NOTE: pm fact\nRELAY_TASK\nprint hi\nEND_RELAY_TASK", key="p0", page=PM_URL, lease="p",
          user_id="claude:user:0")
    drive(engine, f"RELAY_NOTE: worker fact\n{FENCE}bash\necho hi\n{FENCE}", key="w0", page=WORKER_URL, lease="w")
    notes = [n["text"] for n in memory.active_notes(database.conn, "project-demo")]
    assert notes == ["pm fact"]


def test_memory_is_injected_into_new_runs(tmp_path):
    engine, database, root = make_engine(tmp_path)
    with database.transaction() as conn:
        store.ensure_project(conn, name="demo", root=root, branch=None)
        memory.add_note(conn, "project-demo", "never push to main", source="user")
    engine.start(name="demo", root=root, mode="ade", goal="G", conversation_url=WORKER_URL, pm_conversation_url=PM_URL)
    rt = store.all_runtimes(database.conn)[0]
    kickoff = store.latest_request(database.conn, rt["session_id"])
    assert "never push to main" in kickoff["prompt_text"]
    engine.stop("demo")
    engine.start(name="demo", root=root, conversation_url=WORKER_URL)
    rt = store.all_runtimes(database.conn)[0]
    assert "never push to main" in store.latest_request(database.conn, rt["session_id"])["prompt_text"]


def test_protocols_mention_notes_and_plan():
    from project_relay.relay import ade, prompts
    assert "RELAY_NOTE:" in prompts.PROTOCOL and "RELAY_PLAN" in prompts.PROTOCOL
    assert "RELAY_NOTE:" in ade.PM_PROTOCOL and "RELAY_NOTE" not in ade.WORKER_PROTOCOL
