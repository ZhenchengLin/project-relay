from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from project_relay import __version__

from project_relay.relay.engine import RelayEngine
from project_relay.relay.runner import RunResult
from project_relay.relay.server import make_handler
from project_relay.storage.database import RelayDatabase

URL = "https://chatgpt.com/c/0f0f0f0f-1111-4222-8333-444444444444"
TOKEN = "t0ken"
FENCE = "`" * 3


@pytest.fixture
def server(tmp_path):
    db = RelayDatabase(tmp_path / "relay.db", check_same_thread=False)
    runs = []

    def runner(script, *, cwd, timeout_seconds, on_start=None):
        runs.append(script)
        return RunResult(0, "ok\nCOMMAND_EXIT_CODE=0", False)

    engine = RelayEngine(
        db, config={}, runner=runner,
        judge=lambda p, r, c: {"status": "CONTINUE", "reason": "ok", "votes": [], "progress": True},
        git=lambda root: {"branch": "main", "head": "h", "git_status": ""},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, TOKEN))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    root = tmp_path / "repo"
    root.mkdir()
    yield httpd.server_address[1], engine, str(root), runs
    httpd.shutdown()
    db.close()


def call(port, method, path, body=None, *, token=TOKEN, origin=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Relay-Token"] = token
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_token_required(server):
    port, *_ = server
    assert call(port, "GET", "/v2/health", token=None)[0] == 403
    assert call(port, "GET", "/v2/health", token="wrong")[0] == 403
    assert call(port, "GET", "/v2/health") == (200, {"ok": True, "version": __version__})


def test_web_page_origin_is_rejected_even_with_token(server):
    port, *_ = server
    assert call(port, "GET", "/v2/health", origin="https://chatgpt.com")[0] == 403
    assert call(port, "GET", "/v2/health", origin="chrome-extension://abcdef")[0] == 200


def test_unknown_route_and_bad_body(server):
    port, *_ = server
    assert call(port, "GET", "/v2/nope")[0] == 404
    assert call(port, "POST", "/v2/browser/ready", {"lease": "x"})[0] == 400


def test_full_request_over_http(server):
    port, engine, root, runs = server
    engine.start(name="demo", root=root, conversation_url=URL)
    ext = "chrome-extension://abc"
    status, job = call(port, "POST", "/v2/browser/poll", {"lease": "L", "page_url": URL}, origin=ext)
    assert status == 200 and job["type"] == "submit"
    rid = job["request_id"]
    status, grant = call(port, "POST", "/v2/browser/ready",
                         {"lease": "L", "request_id": rid, "page_url": URL, "baseline": []}, origin=ext)
    assert grant == {"send": True}
    status, again = call(port, "POST", "/v2/browser/ready",
                         {"lease": "L", "request_id": rid, "page_url": URL, "baseline": []}, origin=ext)
    assert again["send"] is False
    status, body = call(port, "POST", "/v2/browser/accepted",
                        {"lease": "L", "request_id": rid, "user_turn_id": "group:user:pending-chatgpt-submit",
                         "page_url": URL}, origin=ext)
    assert status == 409
    assert call(port, "POST", "/v2/browser/accepted",
                {"lease": "L", "request_id": rid, "user_turn_id": "group:user:K", "page_url": URL}, origin=ext)[0] == 200
    assert call(port, "POST", "/v2/browser/complete",
                {"lease": "L", "request_id": rid, "assistant_turn_id": "group:assistant:K",
                 "text": f"{FENCE}bash\necho hi\n{FENCE}"}, origin=ext)[0] == 200
    while engine.step():
        pass
    assert runs == ["echo hi"]
    status, st = call(port, "GET", "/v2/status")
    assert st["runtimes"][0]["cycle_count"] == 1
    assert st["runtimes"][0]["request"]["state"] == "QUEUED"


def test_events_endpoint_and_ade_start(server, monkeypatch):
    port, engine, root, runs = server
    engine.start(name="demo", root=root, mode="ade", goal="G",
                 conversation_url=URL, pm_conversation_url="https://claude.ai/chat/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab")
    status, body = call(port, "GET", "/v2/events?project=demo&after=0&limit=5")
    assert status == 200 and len(body["events"]) == 5
    assert all("payload" in e for e in body["events"])
    status, st = call(port, "GET", "/v2/status")
    assert st["runtimes"][0]["mode"] == "ade"
    status, job = call(port, "POST", "/v2/browser/poll",
                       {"lease": "p", "page_url": "https://claude.ai/chat/0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"},
                       origin="chrome-extension://x")
    assert job["type"] == "submit" and job["role"] == "pm"
