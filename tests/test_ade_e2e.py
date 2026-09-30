"""
Relay ADE end-to-end (opt-in: RELAY_E2E=1).

Real: unpacked extension on two sites, prelayd, RelayEngine, SQLite, bash, git.
Mocked: claude.ai (PM) and chatgpt.com (Worker) pages, the loop judges.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from project_relay.relay import store
from project_relay.relay.cli import EXTENSION_FILES, EXTENSION_SOURCE
from project_relay.relay.engine import RelayEngine
from project_relay.relay.server import make_handler
from project_relay.relay.supervisor import Supervisor
from project_relay.storage.database import RelayDatabase

JS_DIR = Path(__file__).resolve().parent / "js"
CHROME = os.environ.get("CHROME_PATH") or (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" if sys.platform == "darwin"
    else shutil.which("google-chrome") or "")

pytestmark = pytest.mark.skipif(
    os.environ.get("RELAY_E2E") != "1" or not CHROME or not Path(CHROME).exists() or not shutil.which("node"),
    reason="set RELAY_E2E=1 (needs Google Chrome and node)",
)


def judge(project, root, cycles):
    return {"status": "CONTINUE", "reason": "scripted", "progress": True,
            "votes": [{"voter": "scripted", "verdict": "PROGRESS", "reason": "scripted", "model": None}]}


def test_ade_end_to_end(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)

    db = RelayDatabase(tmp_path / "relay.db", check_same_thread=False)
    engine = RelayEngine(db, config={}, judge=judge)
    engine.supervisor = Supervisor(engine, {"supervisor": {"notifications": False}})
    token = "e2e-token"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, token))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    engine.start_background()

    ext = tmp_path / "extension"
    ext.mkdir()
    for name in EXTENSION_FILES:
        shutil.copy2(EXTENSION_SOURCE / name, ext / name)
    timing = {"trace": True, "acceptAfterSendMs": 8000, "acceptObservedMs": 5000, "acceptSettleMs": 800,
              "pollMs": 700, "probeEveryMs": 2000, "completion": {"stoppedMs": 600, "stableMs": 900}}
    (ext / "relay-config.js").write_text(
        f"self.RELAY_CONFIG = {json.dumps({'port': port, 'token': token, 'timing': timing})};\n")

    engine.start(name="demo", root=str(repo), mode="ade", new_chat=True, pm_new_chat=True,
                 goal="Add hello.txt and commit it.", rules="Never push.", review_policy="risky")

    report_path = tmp_path / "report.json"
    try:
        proc = subprocess.run(
            ["node", str(JS_DIR / "e2e/run-e2e-ade.mjs"), "--port", str(port), "--token", token,
             "--ext", str(ext), "--profile", str(tmp_path / "chrome-profile"), "--report", str(report_path),
             "--timeout", "240000", "--screenshot", str(tmp_path / "dashboard.png")],
            cwd=JS_DIR, capture_output=True, text=True, timeout=420,
        )
    finally:
        engine.shutdown()
        httpd.shutdown()

    assert proc.returncode == 0, proc.stderr[-4000:]
    report = json.loads(report_path.read_text())
    print(json.dumps({"final": report["final"], "sends": report["sends"]}, indent=2, ensure_ascii=False))

    # The PM declared the goal done after seeing the commit in the evidence.
    assert report["final"]["status"] == "FINISHED", report["final"]
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout
    assert "add hello" in log and (repo / "hello.txt").read_text().strip() == "hello"

    conn = db.conn
    requests = [dict(r) for r in conn.execute("SELECT * FROM requests ORDER BY sequence_number")]
    kinds = [(r["role"], r["kind"], r["state"]) for r in requests]
    assert kinds == [
        ("pm", "PM_PLAN", "COMPLETED"),       # plan -> task
        ("worker", "TASK", "COMPLETED"),      # bash block (risky: git commit)
        ("pm", "PM_REVIEW", "COMPLETED"),     # approved; this request ran the command
        ("pm", "PM_PLAN", "COMPLETED"),       # evidence -> RELAY_DONE
    ], kinds

    # The approved review request owns the one execution; nothing ran unapproved.
    executions = conn.execute("SELECT request_id, return_code FROM executions").fetchall()
    assert [(e["request_id"], e["return_code"]) for e in executions] == [(requests[2]["id"], 0)]

    # Each site received only its own role's messages, each exactly once.
    sites = [s["site"] for s in report["sends"]]
    assert sites == ["claude", "chatgpt", "claude", "claude"], sites
    submitted = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'REQUEST_STATE_CHANGED' "
        "AND json_extract(payload_json, '$.to_state') = 'SUBMITTING'").fetchone()[0]
    assert submitted == len(report["sends"]) == 4

    # Both chats were created fresh and bound to their own site's URL.
    pm = store.active_conversation(conn, "project-demo", "pm")
    wk = store.active_conversation(conn, "project-demo", "worker")
    assert pm["conversation_url"].startswith("https://claude.ai/chat/")
    assert wk["conversation_url"].startswith("https://chatgpt.com/c/")
    assert all(r["user_turn_id"].startswith("claude:user:") for r in requests if r["role"] == "pm")

    # The dashboard rendered the finished run, with no script errors.
    assert report["consoleErrors"] == [], report["consoleErrors"]
    assert report["pmReloaded"]  # the Claude tab was reloaded mid-run and the run still finished
    text = report["dashboardText"]
    assert "Relay ADE" in text and "demo" in text and "Finished" in text and "prelayd running" in text
    assert "Sent (exactly once)." in text and "PM approved the command." in text  # timeline
    assert "chat #1" in text and "chat #2" not in text
    # Memory, plan, KPIs and the supervisor bar.
    assert "Project memory" in text and "1 note" in text and "plan · 2/2 done" in text.lower()
    # The goal is folded to its first line with its size; the full text is inside the closed fold.
    assert "Goal" in text and " chars" in text
    assert "commands run" in text and "PM approvals" in text and "Supervisor: watching" in text
    notes = [n["text"] for n in db.conn.execute("SELECT text FROM project_notes WHERE active = 1")]
    assert notes == ["commit only the files a task names"]
    shutil.copy(tmp_path / "dashboard.png", os.environ.get("RELAY_E2E_SCREENSHOT", tmp_path / "keep.png"))
    db.close()
