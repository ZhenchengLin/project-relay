from __future__ import annotations

import json
import sqlite3

import pytest

from project_relay.relay import prompts, store
from project_relay.relay.engine import RelayEngine, RelayRefused
from project_relay.relay.runner import RunResult
from project_relay.storage.database import RelayDatabase
from project_relay.storage.schema import MIGRATION_1_SQL, SCHEMA_VERSION

URL = "https://chatgpt.com/c/0f0f0f0f-1111-4222-8333-444444444444"
FENCE = "`" * 3


def bash(cmd: str) -> str:
    return f"Next step:\n\n{FENCE}bash\n{cmd}\n{FENCE}\n"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, script, *, cwd, timeout_seconds, on_start=None):
        self.calls.append(script)
        if on_start:
            on_start(4242)
        return RunResult(0, f"ran: {script}\nCOMMAND_EXIT_CODE=0", False)


class FakeJudge:
    def __init__(self) -> None:
        self.verdicts: list[str] = []

    def __call__(self, project, root, cycles):
        status = self.verdicts.pop(0) if self.verdicts else "CONTINUE"
        verdict = {"LOOP": "LOOP", "CONTINUE": "PROGRESS"}.get(status, "UNCERTAIN")
        return {"status": status, "reason": f"judge said {status}",
                "votes": [{"voter": "fake", "verdict": verdict, "reason": "x", "model": None}],
                "progress": status == "CONTINUE"}


def fake_git(root):
    return {"branch": "main", "head": "abc123", "git_status": "(clean)"}


@pytest.fixture
def env(tmp_path):
    db = RelayDatabase(tmp_path / "relay.db")
    clock = Clock()
    runner = FakeRunner()
    judge = FakeJudge()
    config = {"relay": {"rollover_char_budget": 10_000_000}, "watchdog": {"history_window": 4}}
    engine = RelayEngine(db, config=config, judge=judge, runner=runner, git=fake_git, clock=clock)
    root = tmp_path / "repo"
    root.mkdir()
    yield engine, db, runner, judge, clock, str(root)
    db.close()


def start(engine, root, **kw):
    kw.setdefault("conversation_url", URL)
    return engine.start(name="demo", root=root, **kw)


def drive(engine, reply: str, *, key: str, lease="tab-1", page=URL, new_chat_url=None) -> str:
    """Simulate the extension for one request; returns its request id."""
    job = engine.poll(lease=lease, page_url=page)
    assert job["type"] == "submit", job
    ready_page = "https://chatgpt.com/" if job["conversation_url"] is None else page
    assert engine.ready(lease=lease, request_id=job["request_id"], page_url=ready_page,
                        baseline=["group:user:old", "group:assistant:old"])["send"] is True
    accepted_page = new_chat_url or page
    engine.accepted(lease=lease, request_id=job["request_id"], user_turn_id=f"group:user:{key}",
                    page_url=accepted_page)
    engine.complete(lease=lease, request_id=job["request_id"],
                    assistant_turn_id=f"group:assistant:{key}", text=reply)
    return job["request_id"]


def run_engine(engine, limit=10) -> int:
    n = 0
    while engine.step():
        n += 1
        assert n < limit
    return n


def latest(db, engine=None):
    rt = store.all_runtimes(db.conn)[0]
    return rt, store.latest_request(db.conn, rt["session_id"])


# ---------------------------------------------------------------- happy path

def test_full_cycle_executes_once_and_queues_evidence(env):
    engine, db, runner, judge, clock, root = env
    start(engine, root)
    first = drive(engine, bash("echo hello"), key="k1")
    run_engine(engine)

    assert runner.calls == ["echo hello"]
    rt, nxt = latest(db)
    assert rt["cycle_count"] == 1
    assert store.get_request(db.conn, first)["state"] == "COMPLETED"
    assert store.get_request(db.conn, first)["successor_request_id"] == nxt["id"]
    assert nxt["state"] == "QUEUED" and nxt["kind"] == "CYCLE"
    assert "ran: echo hello" in nxt["prompt_text"]
    assert prompts.PROTOCOL in nxt["prompt_text"]
    execution = store.execution_for(db.conn, first)
    assert execution["pid"] == 4242 and execution["return_code"] == 0
    votes = db.conn.execute("SELECT COUNT(*) FROM watchdog_votes WHERE request_id = ?", (first,)).fetchone()[0]
    assert votes == 1


def test_ready_grants_send_exactly_once(env):
    engine, db, *_, root = env
    start(engine, root)
    job = engine.poll(lease="tab-1", page_url=URL)
    assert engine.ready(lease="tab-1", request_id=job["request_id"], page_url=URL, baseline=[])["send"]
    again = engine.ready(lease="tab-1", request_id=job["request_id"], page_url=URL, baseline=[])
    assert again["send"] is False
    # Afterwards the tab only gets observe jobs, never another submit.
    assert engine.poll(lease="tab-1", page_url=URL)["type"] == "observe"


def test_ready_refuses_wrong_page_and_paused_runtime(env):
    engine, db, *_, root = env
    start(engine, root)
    job = engine.poll(lease="tab-1", page_url=URL)
    other = "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"
    assert engine.ready(lease="tab-1", request_id=job["request_id"], page_url=other, baseline=[])["send"] is False
    engine.pause("demo")
    assert engine.ready(lease="tab-1", request_id=job["request_id"], page_url=URL, baseline=[])["send"] is False


def test_provisional_and_baseline_user_turns_are_not_accepted(env):
    engine, db, *_, root = env
    start(engine, root)
    job = engine.poll(lease="tab-1", page_url=URL)
    engine.ready(lease="tab-1", request_id=job["request_id"], page_url=URL, baseline=["group:user:old"])
    for bad in ("group:user:pending-chatgpt-submit", "group:user:old", ""):
        with pytest.raises(RelayRefused):
            engine.accepted(lease="tab-1", request_id=job["request_id"], user_turn_id=bad, page_url=URL)
    assert store.get_request(db.conn, job["request_id"])["state"] == "SUBMITTING"


def test_assistant_must_share_the_user_group_key(env):
    engine, db, *_, root = env
    start(engine, root)
    job = engine.poll(lease="tab-1", page_url=URL)
    engine.ready(lease="tab-1", request_id=job["request_id"], page_url=URL, baseline=[])
    engine.accepted(lease="tab-1", request_id=job["request_id"], user_turn_id="group:user:A", page_url=URL)
    with pytest.raises(RelayRefused):
        engine.complete(lease="tab-1", request_id=job["request_id"],
                        assistant_turn_id="group:assistant:B", text="hi")


def test_complete_is_idempotent_for_same_text(env):
    engine, db, *_, root = env
    start(engine, root)
    rid = drive(engine, "just words", key="k1")
    assert engine.complete(lease="tab-1", request_id=rid, assistant_turn_id="group:assistant:k1",
                           text="just words")["replay"] is True
    with pytest.raises(RelayRefused):
        engine.complete(lease="tab-1", request_id=rid, assistant_turn_id="group:assistant:k1", text="other")


def test_lease_is_exclusive_until_stale(env):
    engine, db, runner, judge, clock, root = env
    start(engine, root)
    job = engine.poll(lease="tab-1", page_url=URL)
    assert engine.poll(lease="tab-2", page_url=URL)["type"] == "idle"
    clock.now += 60
    taken = engine.poll(lease="tab-2", page_url=URL)
    assert taken["type"] == "submit" and taken["request_id"] == job["request_id"]
    # The old tab can no longer send.
    assert engine.ready(lease="tab-1", request_id=job["request_id"], page_url=URL, baseline=[])["send"] is False


# ---------------------------------------------------------------- new chat

def test_new_chat_binds_url_on_acceptance(env):
    engine, db, *_, root = env
    start(engine, root, conversation_url=None, new_chat=True)
    job = engine.poll(lease="tab-1", page_url="https://chatgpt.com/")
    assert job["conversation_url"] is None
    assert engine.ready(lease="tab-1", request_id=job["request_id"], page_url=URL, baseline=[])["send"] is False
    drive(engine, bash("true"), key="n1", page="https://chatgpt.com/", new_chat_url=URL + "?model=x")
    conv = store.active_conversation(db.conn, "project-demo")
    assert conv["conversation_url"] == URL


# ------------------------------------------------------ reply classification

def test_format_nudge_then_human(env):
    engine, db, runner, *_, root = env
    start(engine, root)
    drive(engine, bash("a") + bash("b"), key="k1")
    run_engine(engine)
    rt, nudge = latest(db)
    assert nudge["kind"] == "NUDGE" and "2 bash blocks" in nudge["prompt_text"]
    drive(engine, "no command here", key="k2")
    run_engine(engine)
    rt, last = latest(db)
    assert last["state"] == "HUMAN_REQUIRED" and rt["status"] == "HUMAN_REQUIRED"
    assert runner.calls == []


def test_dangerous_command_is_never_run(env):
    engine, db, runner, *_, root = env
    start(engine, root)
    drive(engine, bash("git reset --hard HEAD~3"), key="k1")
    run_engine(engine)
    rt, nudge = latest(db)
    assert runner.calls == [] and nudge["kind"] == "NUDGE"
    assert "git reset --hard" in nudge["prompt_text"]


def test_done_marker_finishes_run(env):
    engine, db, runner, *_, root = env
    start(engine, root)
    drive(engine, "All verified.\n\nRELAY_DONE", key="k1")
    run_engine(engine)
    rt, last = latest(db)
    assert rt["status"] == "FINISHED" and last["state"] == "COMPLETED"


def test_max_cycles_stops_for_human(env):
    engine, db, runner, *_, root = env
    start(engine, root, max_cycles=1)
    drive(engine, bash("echo 1"), key="k1")
    run_engine(engine)
    drive(engine, bash("echo 2"), key="k2")
    run_engine(engine)
    rt, last = latest(db)
    assert runner.calls == ["echo 1"] and rt["status"] == "HUMAN_REQUIRED"


# ---------------------------------------------------------------- escalation

def test_loop_escalates_then_human_on_repeat(env):
    engine, db, runner, judge, clock, root = env
    start(engine, root)
    judge.verdicts = ["LOOP", "LOOP"]
    drive(engine, bash("pytest"), key="k1")
    run_engine(engine)
    rt, nxt = latest(db)
    assert rt["model_mode"] == "ESCALATED"
    assert nxt["model"] == "Thinking"
    assert prompts.ESCALATION_NOTE in nxt["prompt_text"]
    job = engine.poll(lease="tab-1", page_url=URL)
    assert job["model_label"] == "Thinking"

    drive(engine, bash("pytest -x"), key="k2")
    run_engine(engine)
    rt, last = latest(db)
    assert rt["status"] == "HUMAN_REQUIRED" and last["state"] == "HUMAN_REQUIRED"
    assert "HUMAN INTERVENTION REQUIRED" in json.loads(last["detail"])["report"]


def test_deescalates_after_progress_streak(env):
    engine, db, runner, judge, clock, root = env
    start(engine, root)
    judge.verdicts = ["LOOP", "CONTINUE", "CONTINUE"]
    for i in range(3):
        drive(engine, bash(f"step {i}"), key=f"k{i}")
        run_engine(engine)
    rt, nxt = latest(db)
    assert rt["model_mode"] == "DEFAULT" and nxt["model"] is None


def test_model_unavailable_falls_back_to_default(env):
    engine, db, runner, judge, clock, root = env
    start(engine, root)
    judge.verdicts = ["LOOP"]
    drive(engine, bash("x"), key="k1")
    run_engine(engine)
    job = engine.poll(lease="tab-1", page_url=URL)
    engine.failure(lease="tab-1", request_id=job["request_id"], code="MODEL_UNAVAILABLE")
    rt, req = latest(db)
    assert rt["model_mode"] == "DEFAULT" and req["model"] is None and req["state"] == "PREPARING_BROWSER"
    assert engine.poll(lease="tab-1", page_url=URL)["model_label"] == ""


# ------------------------------------------------------------------ rollover

def test_budget_rollover_via_handoff(env):
    engine, db, runner, judge, clock, root = env
    engine.cfg["rollover_char_budget"] = 1500
    start(engine, root)
    drive(engine, bash("echo " + "x" * 800), key="k1")
    run_engine(engine)
    rt, handoff_req = latest(db)
    assert handoff_req["kind"] == "HANDOFF"
    pending = json.loads(handoff_req["detail"])["pending_prompt"]
    assert "ran: echo" in pending

    drive(engine, "RELAY_HANDOFF\nGoal: finish demo. Next: run tests.", key="k2")
    run_engine(engine)
    old = db.conn.execute("SELECT * FROM conversations WHERE status = 'RETIRED'").fetchone()
    assert old["retire_reason"] == "BUDGET"
    new = store.active_conversation(db.conn, "project-demo")
    assert new["conversation_url"] is None and new["predecessor_id"] == old["id"]
    rt, seed = latest(db)
    assert seed["kind"] == "ROLLOVER_SEED" and seed["conversation_id"] == new["id"]
    assert "Goal: finish demo" in seed["prompt_text"] and pending.strip() in seed["prompt_text"]
    assert "RELAY_HANDOFF" not in seed["prompt_text"].split("=== HANDOFF ===")[1].split("=== END")[0]

    # The new chat is opened from the home page and binds its own URL.
    new_url = "https://chatgpt.com/c/99999999-8888-7777-6666-555555555555"
    drive(engine, bash("pytest"), key="n1", page="https://chatgpt.com/", new_chat_url=new_url)
    assert store.active_conversation(db.conn, "project-demo")["conversation_url"] == new_url


def test_hard_limit_rollover_uses_fallback_handoff(env):
    engine, db, runner, *_, root = env
    start(engine, root)
    drive(engine, bash("echo one"), key="k1")
    run_engine(engine)
    job = engine.poll(lease="tab-1", page_url=URL)
    engine.failure(lease="tab-1", request_id=job["request_id"], code="CONVERSATION_LIMIT",
                   message="The conversation is too long")
    run_engine(engine)
    rt, seed = latest(db)
    assert seed["kind"] == "ROLLOVER_SEED"
    assert "Relay-built handoff" in seed["prompt_text"] and "echo one" in seed["prompt_text"]
    assert "ran: echo one" in seed["prompt_text"]  # the pending evidence prompt is carried over


# ------------------------------------------------------------------ recovery

def test_not_persisted_resubmits_once_as_new_request(env):
    engine, db, *_, root = env
    start(engine, root)
    job = engine.poll(lease="tab-1", page_url=URL)
    engine.ready(lease="tab-1", request_id=job["request_id"], page_url=URL, baseline=[])
    engine.failure(lease="tab-1", request_id=job["request_id"], code="NOT_PERSISTED")
    run_engine(engine)
    rt, retry = latest(db)
    assert retry["id"] != job["request_id"] and retry["state"] == "QUEUED"
    assert retry["prompt_text"] == store.get_request(db.conn, job["request_id"])["prompt_text"]

    job2 = engine.poll(lease="tab-1", page_url=URL)
    engine.ready(lease="tab-1", request_id=job2["request_id"], page_url=URL, baseline=[])
    engine.failure(lease="tab-1", request_id=job2["request_id"], code="NOT_PERSISTED")
    run_engine(engine)
    rt, last = latest(db)
    assert last["id"] == job2["request_id"] and last["state"] == "HUMAN_REQUIRED"
    assert rt["status"] == "HUMAN_REQUIRED"


def test_invalid_failure_code_for_state_is_refused(env):
    engine, db, *_, root = env
    start(engine, root)
    job = engine.poll(lease="tab-1", page_url=URL)
    engine.ready(lease="tab-1", request_id=job["request_id"], page_url=URL, baseline=[])
    with pytest.raises(RelayRefused):
        engine.failure(lease="tab-1", request_id=job["request_id"], code="REPLY_FAILED")


def test_restart_mid_execution_never_reruns(env, tmp_path):
    engine, db, runner, judge, clock, root = env
    start(engine, root)
    rid = drive(engine, bash("make deploy"), key="k1")
    engine.step()  # ASSISTANT_COMPLETE -> COMMAND_VALIDATED
    # Simulate a crash right after RUNNING_CLI was persisted.
    with db.transaction() as conn:
        execution = store.execution_for(conn, rid)
        store.mark_execution_started(conn, execution["id"])
        from project_relay.state import transition_request_in
        transition_request_in(conn, request_id=rid, to_state="RUNNING_CLI")

    fresh = RelayEngine(db, config={}, judge=judge, runner=runner, git=fake_git, clock=clock)
    run_engine(fresh)
    assert runner.calls == []
    assert store.get_request(db.conn, rid)["state"] == "RECOVERY_REQUIRED"
    rt, nxt = latest(db)
    assert nxt["kind"] == "RECOVERY" and "make deploy" in nxt["prompt_text"]
    assert "will NOT re-run" in nxt["prompt_text"]


def test_reply_failed_retries_once(env):
    engine, db, *_, root = env
    start(engine, root)
    job = engine.poll(lease="tab-1", page_url=URL)
    engine.ready(lease="tab-1", request_id=job["request_id"], page_url=URL, baseline=[])
    engine.accepted(lease="tab-1", request_id=job["request_id"], user_turn_id="group:user:z", page_url=URL)
    engine.failure(lease="tab-1", request_id=job["request_id"], code="REPLY_FAILED")
    run_engine(engine)
    rt, retry = latest(db)
    assert retry["kind"] == "RETRY"


# ------------------------------------------------------------------ control

def test_stop_cancels_unsent_request_and_resume_with_message(env):
    engine, db, *_, root = env
    start(engine, root)
    job = engine.poll(lease="tab-1", page_url=URL)
    engine.stop("demo")
    assert store.get_request(db.conn, job["request_id"])["state"] == "CANCELLED"
    assert engine.poll(lease="tab-1", page_url=URL)["type"] == "idle"
    with pytest.raises(RelayRefused):
        engine.pause("demo")  # stopped runs cannot be paused
    engine.resume("demo", message="Please re-check the test suite.")
    job = engine.poll(lease="tab-1", page_url=URL)
    assert job["type"] == "submit" and "re-check the test suite" in job["prompt"]


def test_start_refuses_while_running(env):
    engine, db, *_, root = env
    start(engine, root)
    with pytest.raises(RelayRefused):
        start(engine, root)


def test_start_on_different_url_retires_previous_conversation(env):
    engine, db, *_, root = env
    start(engine, root)
    engine.stop("demo")
    other = "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"
    start(engine, root, conversation_url=other)
    rows = db.conn.execute("SELECT status, conversation_url FROM conversations ORDER BY sequence_number").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("RETIRED", URL), ("ACTIVE", other)]


# ----------------------------------------------------------------- migration

def test_v1_database_migrates_to_v2(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATION_1_SQL + "\nPRAGMA user_version = 1;")
    conn.execute("INSERT INTO projects VALUES ('p', 'demo', '/r', NULL, ?, 'now', 'now')", (URL,))
    conn.commit()
    conn.close()
    with RelayDatabase(path) as db:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(requests)")}
        assert {"conversation_id", "kind", "model", "baseline_json", "successor_request_id"} <= cols
        assert db.conn.execute("SELECT conversation_url FROM projects").fetchone()[0] == URL


def test_diag_is_stored_bounded_and_lease_checked(env):
    engine, db, *_, root = env
    start(engine, root)
    job = engine.poll(lease="tab-1", page_url=URL)
    with pytest.raises(RelayRefused):
        engine.diag(lease="other", request_id=job["request_id"], stage="waiting", probe={})
    for _ in range(engine.DIAG_LIMIT_PER_REQUEST + 5):
        engine.diag(lease="tab-1", request_id=job["request_id"], stage="waiting", probe={"generating": True})
    count = db.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'BROWSER_DIAG'").fetchone()[0]
    assert count == engine.DIAG_LIMIT_PER_REQUEST
    big = engine.diag(lease="tab-1", request_id=job["request_id"], stage="x", probe={"a": "x" * 50000})
    assert big["stored"] is False


def test_evidence_prompt_clips_minified_lines_and_caps_size():
    huge_line = "<div>" + "x" * 7000 + "</div>"
    output = "\n".join(["ok"] * 5 + [huge_line] + ["y" * 100] * 400)
    prompt = prompts.evidence_prompt(
        project="p", root="/r", command="echo", return_code=2, output=output,
        git_before={}, git_after={}, watchdog_line="CONTINUE", max_chars=12_000)
    assert "Relay cut" in prompt and "x" * 500 not in prompt
    assert len(prompt) < 12_000 + 3_000


def test_truncated_script_is_not_run_and_nudged(env):
    engine, db, runner, *_, root = env
    start(engine, root)
    drive(engine, f"{FENCE}bash\nbash <<'BASH'\necho start\npython3 - <<'PY'\nprint(\"cut\n{FENCE}", key="k1")
    run_engine(engine)
    rt, nudge = latest(db)
    assert runner.calls == []
    assert nudge["kind"] == "NUDGE" and "did NOT run" in nudge["prompt_text"]


def test_hard_limit_on_a_handoff_keeps_the_pending_message(env):
    engine, db, runner, *_, root = env
    engine.cfg["rollover_char_budget"] = 1500
    start(engine, root)
    drive(engine, bash("echo " + "x" * 800), key="k1")
    run_engine(engine)
    rt, handoff_req = latest(db)
    pending = json.loads(handoff_req["detail"])["pending_prompt"]
    job = engine.poll(lease="tab-1", page_url=URL)
    engine.failure(lease="tab-1", request_id=job["request_id"], code="CONVERSATION_LIMIT")
    run_engine(engine)
    rt, seed = latest(db)
    assert seed["kind"] == "ROLLOVER_SEED" and pending.strip() in seed["prompt_text"]
