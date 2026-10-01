"""Relay ADE, milestone pace: the PM assigns a milestone, the Worker works through it on its
own (results go back to the Worker), and the PM checks in only when needed."""
from __future__ import annotations

from project_relay.relay import ade, memory, store
from test_ade_engine import (  # noqa: F401  (env is a fixture)
    PM_URL, WORKER_URL, bash, env, latest, reply_as, run)


def assign(text: str) -> str:
    return ("RELAY_PLAN\n- [~] M1 Parser\n- [ ] M2 Docs\nEND_RELAY_PLAN\n\n"
            f"{ade.ASSIGN_START}\n{text}\n{ade.ASSIGN_END}\n")


def start(engine, root, **kw):
    kw.setdefault("conversation_url", WORKER_URL)
    kw.setdefault("pm_conversation_url", PM_URL)
    return engine.start(name="demo", root=root, mode="ade", goal="Ship the parser", rules="never push", **kw)


def test_milestone_is_the_default_ade_pace_with_the_push_review_policy(env):
    engine, db, *_, root = env
    out = start(engine, root)
    assert out["pace"] == "milestone" and out["review_policy"] == "push" and out["checkin_every"] == 8
    rt, req = latest(db)
    assert rt["pace"] == "milestone"
    assert ade.ASSIGN_START in req["prompt_text"] and "First agree the plan" in req["prompt_text"]


def test_worker_works_through_its_milestone_and_the_pm_checks_in_periodically(env):
    engine, db, runner, judge, root = env
    start(engine, root, checkin_every=3)
    reply_as(engine, "pm", assign("Make tests/test_parser.py pass. Done when pytest is green."))
    run(engine)
    rt, req = latest(db)
    assert req["role"] == "worker" and req["kind"] == "ASSIGN"
    assert "Make tests/test_parser.py pass" in req["prompt_text"] and "[~] M1 Parser" in req["prompt_text"]
    assert ade.AGREE in req["prompt_text"]
    reply_as(engine, "worker", "RELAY_AGREE\n1. run tests\n2. fix\n\n" + bash("pytest -q"))
    run(engine)
    # The result goes back to the Worker, not the PM.
    rt, req = latest(db)
    assert req["role"] == "worker" and req["kind"] == "CYCLE" and "ran: " in req["prompt_text"]
    assert "Make tests/test_parser.py pass" in req["prompt_text"]
    reply_as(engine, "worker", bash("sed -i '' s/a/b/ parser.py"))
    run(engine)
    assert latest(db)[1]["role"] == "worker"
    reply_as(engine, "worker", bash("pytest -q"))
    run(engine)
    # Third command: periodic check-in with a progress report.
    rt, req = latest(db)
    assert req["role"] == "pm" and req["kind"] == "PM_PLAN"
    assert "Periodic check-in" in req["prompt_text"] and "Commands since your last message (3)" in req["prompt_text"]
    assert "1. run tests" in req["prompt_text"] and "[first command]" in req["prompt_text"]
    assert len(runner.calls) == 3
    # The PM lets the Worker continue with guidance.
    reply_as(engine, "pm", "RELAY_CONTINUE: focus on the tokenizer edge case")
    run(engine)
    rt, req = latest(db)
    assert req["role"] == "worker" and req["kind"] == "GUIDE" and "tokenizer edge case" in req["prompt_text"]
    reply_as(engine, "worker", "RELAY_MILESTONE_DONE: pytest green (12 passed), committed abc123")
    run(engine)
    rt, req = latest(db)
    assert req["role"] == "pm" and "reports the milestone done" in req["prompt_text"]
    assert "12 passed" in req["prompt_text"]
    reply_as(engine, "pm", "Verified.\n\nRELAY_DONE")
    run(engine)
    assert store.all_runtimes(db.conn)[0]["status"] == "FINISHED"
    # The plan the PM wrote when assigning is the project's plan.
    assert [(t["task_key"], t["status"]) for t in memory.plan(db.conn, "project-demo")] == [("M1", "doing"), ("M2", "todo")]


def test_worker_concern_goes_to_the_pm_without_running_anything(env):
    engine, db, runner, judge, root = env
    start(engine, root)
    reply_as(engine, "pm", assign("Rewrite the parser in Rust."))
    run(engine)
    reply_as(engine, "worker", "RELAY_CONCERN: the repo is pure Python and the rules forbid new toolchains")
    run(engine)
    rt, req = latest(db)
    assert req["role"] == "pm" and "raised a concern" in req["prompt_text"]
    assert "forbid new toolchains" in req["prompt_text"] and runner.calls == []


def test_blocked_and_loop_check_in_and_a_second_loop_stops(env):
    engine, db, runner, judge, root = env
    start(engine, root)
    reply_as(engine, "pm", assign("Fix the build."))
    run(engine)
    judge.verdicts = ["LOOP"]
    reply_as(engine, "worker", "RELAY_AGREE\n" + bash("make"))
    run(engine)
    rt, req = latest(db)
    assert req["role"] == "pm" and "going in circles" in req["prompt_text"]
    reply_as(engine, "pm", "RELAY_CONTINUE: read the error before retrying")
    run(engine)
    judge.verdicts = ["LOOP"]
    reply_as(engine, "worker", bash("make"))
    run(engine)
    assert store.all_runtimes(db.conn)[0]["status"] == "HUMAN_REQUIRED"


def test_human_message_triggers_a_check_in_at_the_next_command(env):
    engine, db, runner, judge, root = env
    start(engine, root)
    reply_as(engine, "pm", assign("Fix the build."))
    run(engine)
    reply_as(engine, "worker", "RELAY_AGREE\n" + bash("make"))
    engine.tell("demo", "The repo has a GitHub remote; push to relay/work")
    run(engine)
    rt, req = latest(db)
    assert req["role"] == "pm" and req["prompt_text"].startswith("Message from the human")
    assert "The human sent you a message" in req["prompt_text"]


def test_push_policy_reviews_only_pushes(env):
    engine, db, runner, judge, root = env
    start(engine, root)
    reply_as(engine, "pm", assign("Commit and push the fix."))
    run(engine)
    reply_as(engine, "worker", "RELAY_AGREE\n" + bash("git commit -qam fix"))
    run(engine)
    assert latest(db)[1]["role"] == "worker" and runner.calls == ["git commit -qam fix"]
    reply_as(engine, "worker", bash("git push origin relay/work"))
    run(engine)
    rt, req = latest(db)
    assert req["kind"] == "PM_REVIEW" and "pushes, merges or rebases" in req["prompt_text"]
    reply_as(engine, "pm", "RELAY_APPROVE")
    run(engine)
    assert runner.calls[-1] == "git push origin relay/work"
    rt, req = latest(db)
    assert req["role"] == "worker" and req["kind"] == "CYCLE"   # back to the Worker after the approved push


def test_worker_without_a_command_is_nudged_then_checked_in(env):
    engine, db, runner, judge, root = env
    start(engine, root)
    reply_as(engine, "pm", assign("Fix the build."))
    run(engine)
    for _ in range(5):
        rt, req = latest(db)
        if req["role"] == "pm":
            break
        reply_as(engine, "worker", "I think we should discuss this first.")
        run(engine)
    rt, req = latest(db)
    assert req["role"] == "pm" and "stopped producing usable commands" in req["prompt_text"]


def test_step_pace_still_works_and_assign_is_treated_as_a_task(env):
    engine, db, runner, judge, root = env
    start(engine, root, pace="step", review_policy="never")
    reply_as(engine, "pm", assign("echo hi"))
    run(engine)
    rt, req = latest(db)
    assert req["role"] == "worker" and req["kind"] == "TASK"
    reply_as(engine, "worker", bash("echo hi"))
    run(engine)
    assert latest(db)[1]["role"] == "pm"   # step pace: every result goes to the PM


def test_switching_a_step_run_to_milestone_tells_the_pm_and_takes_a_task_as_an_assignment(env):
    engine, db, runner, judge, root = env
    start(engine, root, pace="step", review_policy="never")
    out = engine.set_pace("demo", "milestone", checkin_every=5, review_policy="push")
    assert out == {"pace": "milestone", "checkin_every": 5, "review_policy": "push"}
    rt, req = latest(db)
    assert rt["pace"] == "milestone" and "switched this run to milestone pace" in req["prompt_text"]
    reply_as(engine, "pm", "RELAY_TASK\nMake the build green\nEND_RELAY_TASK")
    run(engine)
    rt, req = latest(db)
    assert req["role"] == "worker" and req["kind"] == "ASSIGN" and ade.AGREE in req["prompt_text"]
