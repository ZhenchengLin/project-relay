"""Relay ADE: PM on claude.ai plans/reviews, Worker on chatgpt.com writes one bash block."""
from __future__ import annotations

import json

import pytest

from project_relay.relay import ade, store
from project_relay.relay.engine import RelayEngine, RelayRefused
from project_relay.relay.runner import RunResult
from project_relay.storage.database import RelayDatabase

WORKER_URL = "https://chatgpt.com/c/0f0f0f0f-1111-4222-8333-444444444444"
PM_URL = "https://claude.ai/chat/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"
CLAUDE_NEW = "https://claude.ai/new"
FENCE = "`" * 3


def bash(cmd: str) -> str:
    return f"{FENCE}bash\n{cmd}\n{FENCE}\n"


def task(text: str) -> str:
    return f"Plan.\n\n{ade.TASK_START}\n{text}\n{ade.TASK_END}\n"


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, script, *, cwd, timeout_seconds, on_start=None):
        self.calls.append(script)
        return RunResult(0, f"ran: {script}\nCOMMAND_EXIT_CODE=0", False)


class Judge:
    def __init__(self):
        self.verdicts = []

    def __call__(self, project, root, cycles):
        status = self.verdicts.pop(0) if self.verdicts else "CONTINUE"
        return {"status": status, "reason": f"judge {status}", "progress": status == "CONTINUE",
                "votes": [{"voter": "fake", "verdict": "LOOP" if status == "LOOP" else "PROGRESS",
                           "reason": "x", "model": None}]}


@pytest.fixture
def env(tmp_path):
    db = RelayDatabase(tmp_path / "relay.db")
    runner, judge = Runner(), Judge()
    engine = RelayEngine(db, config={"relay": {"rollover_char_budget": 10_000_000}}, judge=judge, runner=runner,
                         git=lambda root: {"branch": "main", "head": "abc", "git_status": " M a.py"})
    root = tmp_path / "repo"
    root.mkdir()
    yield engine, db, runner, judge, str(root)
    db.close()


def start_ade(engine, root, **kw):
    kw.setdefault("conversation_url", WORKER_URL)
    kw.setdefault("pm_conversation_url", PM_URL)
    return engine.start(name="demo", root=root, mode="ade", goal="Ship the parser", rules="never push", **kw)


counter = {"n": 100}


def reply_as(engine, role: str, text: str, *, lease=None, page=None) -> str:
    """Drive the tab for `role` through one request; returns the request id."""
    lease = lease or f"tab-{role}"
    page = page or (PM_URL if role == "pm" else WORKER_URL)
    job = engine.poll(lease=lease, page_url=page)
    assert job["type"] == "submit" and job["role"] == role, job
    ready_page = page if job["conversation_url"] else (CLAUDE_NEW if role == "pm" else "https://chatgpt.com/")
    assert engine.ready(lease=lease, request_id=job["request_id"], page_url=ready_page, baseline=[])["send"]
    counter["n"] += 1
    n = counter["n"]
    if role == "pm":
        user, assistant = f"claude:user:{n}:h{n}", f"claude:assistant:{n}"
    else:
        user, assistant = f"group:user:k{n}", f"group:assistant:k{n}"
    engine.accepted(lease=lease, request_id=job["request_id"], user_turn_id=user, page_url=page)
    engine.complete(lease=lease, request_id=job["request_id"], assistant_turn_id=assistant, text=text)
    return job["request_id"]


def run(engine, limit=12):
    n = 0
    while engine.step():
        n += 1
        assert n < limit


def latest(db):
    rt = store.all_runtimes(db.conn)[0]
    return rt, store.latest_request(db.conn, rt["session_id"])


def test_ade_start_goes_to_the_pm_first(env):
    engine, db, *_, root = env
    out = start_ade(engine, root)
    assert out["mode"] == "ade" and out["pm_conversation_url"] == PM_URL
    rt, req = latest(db)
    assert rt["mode"] == "ade" and rt["review_policy"] == "risky"
    assert req["role"] == "pm" and req["kind"] == "PM_PLAN"
    assert "Ship the parser" in req["prompt_text"] and "never push" in req["prompt_text"]
    assert ade.TASK_START in req["prompt_text"] and " M a.py" in req["prompt_text"]
    # The ChatGPT (worker) tab gets nothing while the PM is up.
    assert engine.poll(lease="w", page_url=WORKER_URL)["type"] == "idle"


def test_pm_task_worker_command_evidence_back_to_pm_then_done(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    reply_as(engine, "pm", task("Run the parser tests and print the summary."))
    run(engine)
    rt, worker_req = latest(db)
    assert worker_req["role"] == "worker" and worker_req["kind"] == "TASK"
    assert "Run the parser tests" in worker_req["prompt_text"]
    assert engine.poll(lease="p", page_url=PM_URL)["type"] == "idle"

    reply_as(engine, "worker", bash("pytest -q tests/test_parser.py"))
    run(engine)
    assert runner.calls == ["pytest -q tests/test_parser.py"]
    rt, pm_req = latest(db)
    assert pm_req["role"] == "pm" and pm_req["kind"] == "PM_PLAN"
    assert "ran: pytest -q" in pm_req["prompt_text"] and ade.TASK_START in pm_req["prompt_text"]
    assert "EXACTLY ONE fenced bash block" not in pm_req["prompt_text"]  # no worker protocol for the PM

    reply_as(engine, "pm", "Verified.\n\nRELAY_DONE")
    run(engine)
    rt, _ = latest(db)
    assert rt["status"] == "FINISHED" and rt["cycle_count"] == 1


def test_risky_command_waits_for_pm_approval(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    reply_as(engine, "pm", task("Commit the parser."))
    run(engine)
    reply_as(engine, "worker", bash("git add parser.py && git commit -m parser"))
    run(engine)
    assert runner.calls == []
    rt, review = latest(db)
    assert review["role"] == "pm" and review["kind"] == "PM_REVIEW"
    assert "git commit -m parser" in review["prompt_text"] and "changes Git history" in review["prompt_text"]

    reply_as(engine, "pm", "Fine.\nRELAY_APPROVE")
    run(engine)
    assert runner.calls == ["git add parser.py && git commit -m parser"]
    assert store.execution_for(db.conn, review["id"]) is not None
    rt, pm_req = latest(db)
    assert pm_req["kind"] == "PM_PLAN" and "ran: git add" in pm_req["prompt_text"]


def test_pm_revise_sends_feedback_to_worker(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    reply_as(engine, "pm", task("Commit the parser."))
    run(engine)
    reply_as(engine, "worker", bash("git commit -am parser && git push"))
    run(engine)
    reply_as(engine, "pm", "RELAY_REVISE: do not push; commit only parser.py")
    run(engine)
    assert runner.calls == []
    rt, revise = latest(db)
    assert revise["role"] == "worker" and revise["kind"] == "REVISE"
    assert "do not push" in revise["prompt_text"] and "git push" in revise["prompt_text"]
    assert json.loads(revise["detail"])["task"] == "Commit the parser."


def test_worker_without_command_goes_to_pm(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    reply_as(engine, "pm", task("Delete the production database."))
    run(engine)
    reply_as(engine, "worker", "I won't do that: it is destructive and irreversible.")
    run(engine)
    rt, pm_req = latest(db)
    assert pm_req["role"] == "pm" and "did not produce a command" in pm_req["prompt_text"]
    assert runner.calls == []


def test_blocked_command_is_nudged_then_escalated_to_pm(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    reply_as(engine, "pm", task("Clean the tree."))
    run(engine)
    reply_as(engine, "worker", bash("git reset --hard"))
    run(engine)
    rt, nudge = latest(db)
    assert nudge["role"] == "worker" and nudge["kind"] == "NUDGE"
    assert json.loads(nudge["detail"])["task"] == "Clean the tree."
    reply_as(engine, "worker", bash("git reset --hard HEAD"))
    run(engine)
    rt, pm_req = latest(db)
    assert pm_req["role"] == "pm" and "did not run the Worker's command" in pm_req["prompt_text"]
    assert runner.calls == []


def test_pm_format_nudge_then_human(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    reply_as(engine, "pm", "Let me think about it.")
    run(engine)
    rt, nudge = latest(db)
    assert nudge["role"] == "pm" and "could not find a valid RELAY_TASK" in nudge["prompt_text"]
    reply_as(engine, "pm", "Still thinking.")
    run(engine)
    rt, last = latest(db)
    assert rt["status"] == "HUMAN_REQUIRED" and last["state"] == "HUMAN_REQUIRED"


def test_pm_ask_human(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    reply_as(engine, "pm", "RELAY_ASK_HUMAN: which branch should I target?")
    run(engine)
    rt, _ = latest(db)
    assert rt["status"] == "HUMAN_REQUIRED" and "which branch" in rt["reason"]


def test_loop_notes_pm_then_stops_on_second_loop(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    judge.verdicts = ["LOOP", "LOOP"]
    reply_as(engine, "pm", task("Fix test A."))
    run(engine)
    reply_as(engine, "worker", bash("pytest -k a"))
    run(engine)
    rt, pm_req = latest(db)
    assert "LOOPING" in pm_req["prompt_text"] and rt["model_mode"] == "DEFAULT"
    reply_as(engine, "pm", task("Fix test A differently."))
    run(engine)
    reply_as(engine, "worker", bash("pytest -k a -x"))
    run(engine)
    rt, last = latest(db)
    assert rt["status"] == "HUMAN_REQUIRED" and "Loop persisted" in rt["reason"]


def test_new_pm_chat_binds_claude_url(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root, pm_conversation_url=None, pm_new_chat=True)
    job = engine.poll(lease="p", page_url=CLAUDE_NEW)
    assert job["conversation_url"] is None
    # A chatgpt new-chat page is not a valid place for a PM request.
    assert engine.ready(lease="p", request_id=job["request_id"], page_url="https://chatgpt.com/",
                        baseline=[])["send"] is False
    assert engine.ready(lease="p", request_id=job["request_id"], page_url=CLAUDE_NEW, baseline=[])["send"]
    engine.accepted(lease="p", request_id=job["request_id"], user_turn_id="claude:user:0:x", page_url=PM_URL)
    assert store.active_conversation(db.conn, "project-demo", "pm")["conversation_url"] == PM_URL


def test_claude_assistant_must_follow_the_user_turn(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    job = engine.poll(lease="p", page_url=PM_URL)
    engine.ready(lease="p", request_id=job["request_id"], page_url=PM_URL, baseline=[])
    engine.accepted(lease="p", request_id=job["request_id"], user_turn_id="claude:user:4:abc", page_url=PM_URL)
    with pytest.raises(RelayRefused):
        engine.complete(lease="p", request_id=job["request_id"], assistant_turn_id="claude:assistant:3", text="x")
    engine.complete(lease="p", request_id=job["request_id"], assistant_turn_id="claude:assistant:4", text="x")


def test_url_validation(env):
    engine, db, runner, judge, root = env
    with pytest.raises(RelayRefused):
        engine.start(name="demo", root=root, mode="ade", conversation_url=PM_URL)
    with pytest.raises(RelayRefused):
        engine.start(name="demo", root=root, mode="ade", conversation_url=WORKER_URL,
                     pm_conversation_url=WORKER_URL)
    with pytest.raises(RelayRefused):
        engine.start(name="demo", root=root, mode="ade", review_policy="sometimes")


def test_resume_with_message_while_paused_goes_to_pm(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    reply_as(engine, "pm", "RELAY_ASK_HUMAN: which branch?")
    run(engine)
    engine.resume("demo", message="Use branch main.")
    rt, req = latest(db)
    assert req["role"] == "pm" and req["kind"] == "USER" and "Use branch main." in req["prompt_text"]
    assert ade.TASK_START in req["prompt_text"]


def test_resume_message_refused_while_request_in_flight(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    engine.poll(lease="p", page_url=PM_URL)
    engine.pause("demo")
    with pytest.raises(RelayRefused):
        engine.resume("demo", message="hello")
    assert engine.resume("demo")["resumed"]


def test_status_reports_both_roles(env):
    engine, db, runner, judge, root = env
    start_ade(engine, root)
    st = engine.status()[0]
    assert st["mode"] == "ade" and st["pm_conversation"]["site"] == "claude"
    assert st["conversation"]["site"] == "chatgpt" and st["request"]["role"] == "pm"


def test_pm_handoff_rolls_over_the_pm_chat_only(env):
    engine, db, runner, judge, root = env
    engine.cfg["rollover_char_budget"] = 2500
    start_ade(engine, root)
    reply_as(engine, "pm", task("Print a lot."))
    run(engine)
    reply_as(engine, "worker", bash("echo " + "y" * 1500))
    run(engine)
    rt, handoff = latest(db)
    assert handoff["role"] == "pm" and handoff["kind"] == "HANDOFF"
    reply_as(engine, "pm", "RELAY_HANDOFF\nGoal: parser. Done: tests. Next: commit.")
    run(engine)
    rt, seed = latest(db)
    assert seed["role"] == "pm" and seed["kind"] == "ROLLOVER_SEED"
    assert "Goal: parser" in seed["prompt_text"] and "ran: echo" in seed["prompt_text"]
    pm_conv = store.active_conversation(db.conn, "project-demo", "pm")
    worker_conv = store.active_conversation(db.conn, "project-demo", "worker")
    assert pm_conv["site"] == "claude" and pm_conv["conversation_url"] is None
    assert worker_conv["conversation_url"] == WORKER_URL
