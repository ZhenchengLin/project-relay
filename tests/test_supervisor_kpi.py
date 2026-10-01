from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from project_relay.relay import store
from project_relay.relay.engine import RelayEngine
from project_relay.relay.runner import RunResult
from project_relay.relay.supervisor import Supervisor, in_quiet_hours
from project_relay.storage.database import RelayDatabase

URL = "https://chatgpt.com/c/0f0f0f0f-1111-4222-8333-444444444444"
FENCE = "`" * 3


class Runner:
    def __call__(self, script, *, cwd, timeout_seconds, on_start=None):
        return RunResult(1 if "fail" in script else 0, "out\nCOMMAND_EXIT_CODE=0", False)


@pytest.fixture
def env(tmp_path):
    db = RelayDatabase(tmp_path / "relay.db")
    engine = RelayEngine(db, config={}, runner=Runner(),
                         judge=lambda p, r, c: {"status": "CONTINUE", "reason": "", "votes": [], "progress": True},
                         git=lambda root: {"branch": "main", "head": "h", "git_status": ""})
    root = tmp_path / "repo"
    root.mkdir()
    yield engine, db, str(root)
    db.close()


def cycle(engine, command, key):
    job = engine.poll(lease="t", page_url=URL)
    assert job["type"] == "submit", job
    engine.ready(lease="t", request_id=job["request_id"], page_url=URL, baseline=[])
    engine.accepted(lease="t", request_id=job["request_id"], user_turn_id=f"group:user:{key}", page_url=URL)
    engine.complete(lease="t", request_id=job["request_id"], assistant_turn_id=f"group:assistant:{key}",
                    text=f"{FENCE}bash\n{command}\n{FENCE}")
    while engine.step():
        pass


def test_kpis_after_two_cycles(env):
    engine, db, root = env
    engine.start(name="demo", root=root, conversation_url=URL)
    cycle(engine, "echo ok", "a")
    cycle(engine, "echo fail", "b")
    k = engine.status()[0]["kpi"]
    assert k["commands"] == 2 and k["succeeded"] == 1 and k["success_rate"] == 0.5
    assert k["messages"] == {"pm": 0, "worker": 2}
    assert k["loops_caught"] == 0 and k["rollovers"] == 0 and k["plan"]["total"] == 0
    assert k["idle_seconds"] is not None and k["idle_seconds"] < 60


def test_quiet_hours_windows():
    at = lambda h, m=0: datetime(2026, 1, 1, h, m)
    assert in_quiet_hours("23:00-07:00", at(23, 30)) and in_quiet_hours("23:00-07:00", at(3))
    assert not in_quiet_hours("23:00-07:00", at(7)) and not in_quiet_hours("23:00-07:00", at(12))
    assert in_quiet_hours("12:00-13:00", at(12, 30)) and not in_quiet_hours("12:00-13:00", at(13))
    assert not in_quiet_hours("", at(3)) and not in_quiet_hours("nonsense", at(3))


def test_quiet_hours_hold_new_sends_but_not_replies_in_progress(env):
    engine, db, root = env
    clock = {"local": datetime(2026, 1, 1, 12, 0)}
    engine.supervisor = Supervisor(engine, {"supervisor": {"quiet_hours": "23:00-07:00"}},
                                   notify=lambda t, m: None, local_now=lambda: clock["local"])
    engine.start(name="demo", root=root, conversation_url=URL)
    job = engine.poll(lease="t", page_url=URL)
    engine.ready(lease="t", request_id=job["request_id"], page_url=URL, baseline=[])
    clock["local"] = datetime(2026, 1, 1, 23, 30)
    # A send in flight is still observed during quiet hours.
    assert engine.poll(lease="t", page_url=URL)["type"] == "observe"
    engine.accepted(lease="t", request_id=job["request_id"], user_turn_id="group:user:k", page_url=URL)
    engine.complete(lease="t", request_id=job["request_id"], assistant_turn_id="group:assistant:k",
                    text=f"{FENCE}bash\necho hi\n{FENCE}")
    while engine.step():
        pass
    # The next (new) send waits for quiet hours to end.
    held = engine.poll(lease="t", page_url=URL)
    assert held["type"] == "idle" and "Quiet hours" in held["reason"]
    clock["local"] = datetime(2026, 1, 2, 8, 0)
    assert engine.poll(lease="t", page_url=URL)["type"] == "submit"


def test_alerts_on_status_changes_and_stalls(env):
    engine, db, root = env
    sent = []
    now = {"t": datetime.now(timezone.utc)}
    sup = Supervisor(engine, {"supervisor": {"stall_minutes": 25}}, notify=lambda t, m: sent.append(m),
                     now=lambda: now["t"])
    engine.start(name="demo", root=root, conversation_url=URL)
    assert sup.check() == []                      # first pass only records state
    engine.pause("demo")
    assert sup.check() == ["demo paused: Paused by user."] and sent == ["demo paused: Paused by user."]
    assert sup.check() == []                      # no repeat
    engine.resume("demo")
    sup.check()
    now["t"] += timedelta(minutes=30)
    alerts = sup.check()
    assert alerts == ["demo has had no activity for 25 minutes"]
    assert sup.check() == []                      # once per stall episode
    kinds = [r[0] for r in db.conn.execute("SELECT event_type FROM events WHERE event_type = 'SUPERVISOR_ALERT'")]
    assert len(kinds) == 2


def test_daily_budget_pauses_runs(env):
    engine, db, root = env
    sup = Supervisor(engine, {"supervisor": {"chatgpt_daily_messages": 1, "notifications": False}},
                     notify=lambda t, m: None)
    engine.start(name="demo", root=root, conversation_url=URL)
    sup.check()
    cycle(engine, "echo ok", "a")                 # one ChatGPT send today
    alerts = sup.check()
    rt = store.all_runtimes(db.conn)[0]
    assert rt["status"] == "PAUSED" and "Daily message budget reached: ChatGPT 1/1" in rt["reason"]
    assert alerts and "paused" in alerts[0]


def test_supervisor_state_route(env, tmp_path):
    from project_relay.relay.server import _supervisor_state
    engine, db, root = env
    assert _supervisor_state(engine) == {"enabled": False}
    engine.supervisor = Supervisor(engine, {"supervisor": {"claude_daily_messages": 40}}, notify=lambda t, m: None)
    state = _supervisor_state(engine)
    assert state["enabled"] and state["budget"]["claude"] == 40 and state["sends_today"] == {"claude": 0, "chatgpt": 0}
