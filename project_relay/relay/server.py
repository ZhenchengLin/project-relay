"""
prelayd: the long-lived Relay daemon.

Listens on 127.0.0.1 only. Every request must carry X-Relay-Token. Browser
requests may only come from a chrome-extension:// origin; any web page origin
(including chatgpt.com itself) is rejected, so page scripts cannot drive Relay.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import signal
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .. import __version__, core
from ..state import StateError
from ..storage.database import RelayDatabase
from . import config as relay_cfg
from . import memory, store
from .supervisor import Supervisor
from .engine import RelayEngine, RelayRefused

VERSION = __version__
MAX_BODY_BYTES = 8 * 1024 * 1024


def load_token(create: bool = True) -> str:
    path = relay_cfg.TOKEN_PATH
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    if not create:
        raise core.RelayError(f"No Relay token at {path}. Run: prelay init")
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value + "\n")
    return value


def _start_payload(engine: RelayEngine, body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("project") or "").strip()
    if not name:
        raise ValueError("project required")
    project = core.project_from_config(name)
    return engine.start(
        name=project.name,
        root=str(project.root),
        branch=project.preferred_branch or None,
        conversation_url=body.get("conversation_url") or None,
        new_chat=bool(body.get("new_chat")),
        seed=body.get("seed") or None,
        max_cycles=body.get("max_cycles") or None,
        mode=str(body.get("mode") or "solo"),
        goal=body.get("goal") or None,
        rules=str(body.get("rules") or ""),
        review_policy=str(body.get("review_policy") or "risky"),
        pm_conversation_url=body.get("pm_conversation_url") or None,
        pm_new_chat=bool(body.get("pm_new_chat")),
    )


def _events_payload(engine: RelayEngine, body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("project") or "").strip()
    with engine.lock:
        project = store.project_by_name(engine.db.conn, name)
        if project is None:
            raise ValueError(f"unknown project {name!r}")
        events = store.recent_events(engine.db.conn, project["id"], after_id=int(body.get("after") or 0),
                                     limit=min(int(body.get("limit") or 100), 500))
    return {"events": [{k: e[k] for k in ("id", "created_at", "event_type", "request_id", "payload")}
                       for e in events]}


def _project_id(engine: RelayEngine, body: dict[str, Any]) -> str:
    project = store.project_by_name(engine.db.conn, str(body.get("project") or "").strip())
    if project is None:
        raise ValueError(f"unknown project {body.get('project')!r}")
    return project["id"]


def _notes(engine: RelayEngine, body: dict[str, Any]) -> dict[str, Any]:
    with engine.lock:
        pid = _project_id(engine, body)
        return {"notes": memory.active_notes(engine.db.conn, pid), "plan": memory.plan(engine.db.conn, pid)}


def _notes_add(engine: RelayEngine, body: dict[str, Any]) -> dict[str, Any]:
    with engine.lock, engine.db.transaction() as conn:
        note_id = memory.add_note(conn, _project_id(engine, body), str(body.get("text") or ""), source="user")
    return {"ok": True, "id": note_id}


def _notes_remove(engine: RelayEngine, body: dict[str, Any]) -> dict[str, Any]:
    with engine.lock, engine.db.transaction() as conn:
        removed = memory.remove_note(conn, _project_id(engine, body), int(body["id"]))
    return {"ok": removed}


def _supervisor_state(engine: RelayEngine) -> dict[str, Any]:
    sup = engine.supervisor
    if sup is None:
        return {"enabled": False}
    with engine.lock:
        used = sup.sends_today(engine.db.conn)
    return {"enabled": True, "quiet": sup.is_quiet(), "quiet_hours": sup.cfg.get("quiet_hours", ""),
            "sends_today": used, "budget": {"claude": sup.cfg.get("claude_daily_messages", 0),
                                            "chatgpt": sup.cfg.get("chatgpt_daily_messages", 0)},
            "stall_minutes": sup.cfg.get("stall_minutes")}


def routes(engine: RelayEngine) -> dict[tuple[str, str], Callable[[dict[str, Any]], Any]]:
    return {
        ("GET", "/v2/health"): lambda b: {"ok": True, "version": VERSION},
        ("GET", "/v2/status"): lambda b: {"runtimes": engine.status()},
        ("GET", "/v2/events"): lambda b: _events_payload(engine, b),
        ("GET", "/v2/notes"): lambda b: _notes(engine, b),
        ("POST", "/v2/notes/add"): lambda b: _notes_add(engine, b),
        ("POST", "/v2/notes/remove"): lambda b: _notes_remove(engine, b),
        ("GET", "/v2/supervisor"): lambda b: _supervisor_state(engine),
        ("GET", "/v2/projects"): lambda b: {"projects": sorted(core.load_config().get("projects", {}))},
        ("POST", "/v2/browser/poll"): lambda b: engine.poll(
            lease=str(b.get("lease") or ""), page_url=b.get("page_url"), project=b.get("project") or None),
        ("POST", "/v2/browser/ready"): lambda b: engine.ready(
            lease=b["lease"], request_id=b["request_id"], page_url=b["page_url"],
            baseline=list(b.get("baseline") or [])),
        ("POST", "/v2/browser/accepted"): lambda b: engine.accepted(
            lease=b["lease"], request_id=b["request_id"], user_turn_id=b["user_turn_id"],
            page_url=b["page_url"]),
        ("POST", "/v2/browser/bound"): lambda b: engine.bound(
            lease=b["lease"], request_id=b["request_id"], assistant_turn_id=b["assistant_turn_id"]),
        ("POST", "/v2/browser/complete"): lambda b: engine.complete(
            lease=b["lease"], request_id=b["request_id"], assistant_turn_id=b["assistant_turn_id"],
            text=b["text"]),
        ("POST", "/v2/browser/diag"): lambda b: engine.diag(
            lease=b["lease"], request_id=b["request_id"], stage=str(b.get("stage") or ""),
            probe=b.get("probe") if isinstance(b.get("probe"), dict) else {}),
        ("POST", "/v2/browser/failure"): lambda b: engine.failure(
            lease=b["lease"], request_id=b["request_id"], code=b["code"],
            message=str(b.get("message") or ""), evidence=b.get("evidence") or {}),
        ("POST", "/v2/control/start"): lambda b: _start_payload(engine, b),
        ("POST", "/v2/control/pause"): lambda b: engine.pause(b["project"]) or {"ok": True},
        ("POST", "/v2/control/resume"): lambda b: engine.resume(b["project"], b.get("message")),
        ("POST", "/v2/control/stop"): lambda b: engine.stop(b["project"]) or {"ok": True},
    }


def make_handler(engine: RelayEngine, token: str) -> type[BaseHTTPRequestHandler]:
    table = routes(engine)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"prelayd/{VERSION}"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet; events live in SQLite
            pass

        def _send(self, code: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and not origin.startswith("chrome-extension://"):
                return False
            supplied = self.headers.get("X-Relay-Token") or ""
            return hmac.compare_digest(supplied.encode(), token.encode())

        def _dispatch(self, method: str) -> None:
            if not self._authorized():
                self._send(403, {"error": "forbidden"})
                return
            handler = table.get((method, self.path.split("?", 1)[0]))
            if handler is None:
                self._send(404, {"error": "not found"})
                return
            body: dict[str, Any] = {}
            if method == "GET" and "?" in self.path:
                query = urllib.parse.parse_qs(self.path.split("?", 1)[1])
                body = {k: v[-1] for k, v in query.items()}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    self._send(413, {"error": "body too large"})
                    return
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._send(400, {"error": "invalid JSON"})
                    return
                if not isinstance(body, dict):
                    self._send(400, {"error": "JSON object required"})
                    return
            try:
                self._send(200, handler(body))
            except (RelayRefused, StateError) as exc:
                self._send(409, {"error": str(exc)})
            except (KeyError, ValueError, TypeError, core.RelayError) as exc:
                self._send(400, {"error": f"{type(exc).__name__}: {exc}"})
            except Exception as exc:  # pragma: no cover - surfaced to the caller
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    return Handler


def serve(port: int | None = None) -> None:
    cfg = relay_cfg.relay_config()
    port = int(port or cfg["port"])
    token = load_token()
    db = RelayDatabase(relay_cfg.DB_PATH, check_same_thread=False)
    engine = RelayEngine(db)
    engine.supervisor = Supervisor(engine, core.load_config())
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine, token))

    relay_cfg.DAEMON_PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def terminate(*_: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    engine.start_background()
    engine.supervisor.start_background()
    print(f"prelayd {VERSION} listening on 127.0.0.1:{port} (db {relay_cfg.DB_PATH})", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        engine.supervisor.shutdown()
        engine.shutdown()
        server.server_close()
        db.close()
        try:
            if relay_cfg.DAEMON_PID_PATH.read_text().strip() == str(os.getpid()):
                relay_cfg.DAEMON_PID_PATH.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="prelayd")
    ap.add_argument("--port", type=int)
    args = ap.parse_args(argv)
    serve(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
