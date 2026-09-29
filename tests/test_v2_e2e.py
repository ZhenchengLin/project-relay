"""
Extension end-to-end test against a mock chatgpt.com (opt-in: RELAY_E2E=1).

Real pieces: unpacked extension (manifest, service worker, content script),
prelayd HTTP server, RelayEngine, SQLite, /bin/bash runner, git.
Mocked: chatgpt.com (intercepted in a throwaway Chrome profile) and the
Ollama voters (deterministic judge).
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
    last = cycles[-1]["command"] if cycles else ""
    loop = "loop-try" in last
    return {"status": "LOOP" if loop else "CONTINUE", "reason": "scripted",
            "votes": [{"voter": "scripted", "verdict": "LOOP" if loop else "PROGRESS",
                       "reason": "scripted", "model": None}],
            "progress": not loop}


def test_extension_end_to_end(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)

    db = RelayDatabase(tmp_path / "relay.db", check_same_thread=False)
    engine = RelayEngine(db, config={"relay": {"rollover_char_budget": 3500}}, judge=judge)
    token = "e2e-token"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, token))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    engine.start_background()

    ext = tmp_path / "extension"
    ext.mkdir()
    for name in EXTENSION_FILES:
        shutil.copy2(EXTENSION_SOURCE / name, ext / name)
    timing = {"acceptAfterSendMs": 6000, "acceptObservedMs": 4000, "acceptSettleMs": 800,
              "pollMs": 700, "probeEveryMs": 1500, "completion": {"stoppedMs": 600, "stableMs": 900}}
    (ext / "relay-config.js").write_text(
        f"self.RELAY_CONFIG = {json.dumps({'port': port, 'token': token, 'timing': timing})};\n")

    engine.start(name="demo", root=str(repo), new_chat=True,
                 seed="Build the demo project step by step.", max_cycles=10)

    report_path = tmp_path / "report.json"
    try:
        proc = subprocess.run(
            ["node", str(JS_DIR / "e2e/run-e2e.mjs"), "--port", str(port), "--token", token,
             "--ext", str(ext), "--profile", str(tmp_path / "chrome-profile"), "--report", str(report_path),
             "--timeout", "240000"],
            cwd=JS_DIR, capture_output=True, text=True, timeout=300,
        )
    finally:
        engine.shutdown()
        httpd.shutdown()

    assert proc.returncode == 0, proc.stderr[-4000:]
    report = json.loads(report_path.read_text())
    print(json.dumps({k: report[k] for k in ("final", "sends")}, indent=2))

    # The run finished because ChatGPT (mock) said RELAY_DONE.
    assert report["final"]["status"] == "FINISHED", report["final"]

    # Real local execution happened in the project repo.
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout
    assert "one" in log and (repo / "one.txt").read_text().strip() == "step-one"

    conn = db.conn
    requests = [dict(r) for r in conn.execute("SELECT * FROM requests ORDER BY sequence_number")]
    submitted = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'REQUEST_STATE_CHANGED' "
        "AND json_extract(payload_json, '$.to_state') = 'SUBMITTING'").fetchone()[0]

    # Exactly-once: every Send click the mock saw maps to one SUBMITTING request.
    assert len(report["sends"]) == submitted

    # The dropped (non-persisted) send was recovered by a NEW request, not a resend.
    dropped = [r for r in requests if r["state"] == "RECOVERY_REQUIRED"]
    assert len(dropped) == 1 and json.loads(dropped[0]["detail"])["code"] == "NOT_PERSISTED"
    successor = store.get_request(conn, dropped[0]["successor_request_id"])
    assert successor["prompt_text"] == dropped[0]["prompt_text"] and successor["user_turn_id"]

    # pending-chatgpt-submit never became a durable identity.
    assert all("pending" not in (r["user_turn_id"] or "") for r in requests)

    # Every durable user turn in the mock belongs to exactly one request.
    mock_keys = {f"group:user:{t['key']}" for c in report["mockConversations"].values() for t in c["turns"]}
    bound = {r["user_turn_id"] for r in requests if r["user_turn_id"]}
    assert mock_keys == bound

    # Replies were only taken once final: no "thinking" status line was ever
    # captured as a reply, no format nudge was needed, and the extension
    # reported DOM diagnostics.
    assert all("规划验证流程" not in (r["assistant_text"] or "") for r in requests)
    assert not any(r["kind"] == "NUDGE" for r in requests)
    diags = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'BROWSER_DIAG'").fetchone()[0]
    assert diags > 0

    # Loop -> escalation: a send used the strong model after the loop cycle.
    assert any(s["model"] == "Thinking" for s in report["sends"])

    # Rollover: at least one retired conversation and a successor chat with its own URL.
    convs = [dict(r) for r in conn.execute("SELECT * FROM conversations ORDER BY sequence_number")]
    assert len(convs) >= 2 and convs[0]["status"] == "RETIRED" and convs[0]["retire_reason"] == "BUDGET"
    assert all(c["conversation_url"] for c in convs)
    assert any(r["kind"] == "HANDOFF" and r["state"] == "COMPLETED" for r in requests)
    db.close()
