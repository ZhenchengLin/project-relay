from __future__ import annotations

import pytest

from project_relay.relay import store
from project_relay.relay.engine import TAB_SILENT_SECONDS, RelayEngine
from project_relay.relay.runner import RunResult
from project_relay.relay.supervisor import Supervisor
from project_relay.storage.database import RelayDatabase

WORKER_URL = "https://chatgpt.com/c/0f0f0f0f-1111-4222-8333-444444444444"
PM_URL = "https://claude.ai/chat/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    db = RelayDatabase(tmp_path / "relay.db")
    clock = Clock()
    engine = RelayEngine(db, config={}, runner=lambda *a, **k: RunResult(0, "", False),
                         judge=lambda p, r, c: {"status": "CONTINUE", "reason": "", "votes": [], "progress": True},
                         git=lambda root: {"branch": "main", "head": "h", "git_status": ""}, clock=clock)
    root = tmp_path / "repo"
    root.mkdir()
    yield engine, db, clock, str(root)
    db.close()


def missing(engine):
    return {rt["project"]: rt["missing_tab"] for rt in engine.status()}


def test_no_warning_right_after_start(env):
    engine, db, clock, root = env
    engine.start(name="demo", root=root, mode="ade", goal="G", conversation_url=WORKER_URL, pm_conversation_url=PM_URL)
    assert missing(engine) == {"demo": None}


def test_missing_claude_tab_is_reported_until_one_connects(env):
    engine, db, clock, root = env
    engine.start(name="demo", root=root, mode="ade", goal="G", conversation_url=WORKER_URL, pm_conversation_url=PM_URL)
    engine.alive(lease="w", page_url=WORKER_URL)          # only a ChatGPT tab is open
    clock.t += TAB_SILENT_SECONDS + 5
    engine.alive(lease="w", page_url=WORKER_URL)
    assert missing(engine)["demo"] == {"role": "pm", "site": "claude", "silent_seconds": None}
    engine.poll(lease="p", page_url=PM_URL)               # a Claude tab connects
    assert missing(engine)["demo"] is None


def test_a_tab_that_went_silent_is_reported(env):
    engine, db, clock, root = env
    engine.start(name="demo", root=root, mode="ade", goal="G", conversation_url=WORKER_URL, pm_conversation_url=PM_URL)
    job = engine.poll(lease="p", page_url=PM_URL)
    engine.ready(lease="p", request_id=job["request_id"], page_url=PM_URL, baseline=[])
    clock.t += 60
    engine.alive(lease="p", page_url=PM_URL, project="demo")   # heartbeat while busy
    clock.t += 60
    assert missing(engine)["demo"] is None
    clock.t += TAB_SILENT_SECONDS
    assert missing(engine)["demo"]["silent_seconds"] == int(TAB_SILENT_SECONDS + 60)


def test_tabs_bound_to_another_project_do_not_count(env):
    engine, db, clock, root = env
    engine.start(name="demo", root=root, conversation_url=WORKER_URL)
    clock.t += TAB_SILENT_SECONDS + 5
    engine.alive(lease="x", page_url=WORKER_URL, project="other")
    assert missing(engine)["demo"]["site"] == "chatgpt"
    engine.alive(lease="y", page_url=WORKER_URL)          # an unbound tab serves any project
    assert missing(engine)["demo"] is None


def test_paused_runs_do_not_need_a_tab(env):
    engine, db, clock, root = env
    engine.start(name="demo", root=root, conversation_url=WORKER_URL)
    engine.pause("demo")
    clock.t += TAB_SILENT_SECONDS + 5
    assert missing(engine)["demo"] is None


def test_supervisor_alerts_once_per_missing_tab_episode(env):
    engine, db, clock, root = env
    sent = []
    sup = Supervisor(engine, {"supervisor": {"stall_minutes": 0}}, notify=lambda t, m: sent.append(m))
    engine.start(name="demo", root=root, mode="ade", goal="G", conversation_url=WORKER_URL, pm_conversation_url=PM_URL)
    sup.check()
    clock.t += TAB_SILENT_SECONDS + 5
    assert sup.check() == ["demo is waiting for a Claude tab, but none is connected. Relay is opening one; "
                           "if it does not appear, click Arrange windows in the dashboard."]
    assert sup.check() == []
    engine.poll(lease="p", page_url=PM_URL)
    assert sup.check() == []
    clock.t += TAB_SILENT_SECONDS + 5
    assert len(sup.check()) == 1                          # a new episode alerts again
    assert len(sent) == 2


def test_alive_route(env):
    from project_relay.relay.server import routes as build_routes
    engine, db, clock, root = env
    routes = build_routes(engine)
    assert routes[("POST", "/v2/browser/alive")]({"lease": "t", "page_url": PM_URL}) == {"ok": True}
