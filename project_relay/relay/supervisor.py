"""
Supervisor: watches every run from inside prelayd.

- Alerts (desktop notification + SUPERVISOR_ALERT event) when a run starts
  needing you, pauses, finishes, stalls (no activity for stall_minutes), or
  waits for a Claude/ChatGPT tab that is not open or not connected.
- Daily message budgets per role (Claude PM, ChatGPT worker/solo): reaching
  one pauses the runs that would send more.
- Quiet hours: no new messages are sent (replies in progress are still
  collected and commands still run); the engine asks is_quiet() before
  handing out a new send.

Config (~/.project-relay/config.json):

    "supervisor": {
      "notifications": true,
      "stall_minutes": 25,
      "claude_daily_messages": 0,      # 0 = unlimited
      "chatgpt_daily_messages": 0,
      "quiet_hours": ""                 # e.g. "23:00-07:00" (local time)
    }
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from datetime import datetime, time as dtime, timezone
from typing import Any, Callable

from . import store

DEFAULTS: dict[str, Any] = {
    "notifications": True,
    "stall_minutes": 25,
    "claude_daily_messages": 0,
    "chatgpt_daily_messages": 0,
    "quiet_hours": "",
    "interval_seconds": 30,
}

ALERT_STATUSES = {"HUMAN_REQUIRED": "needs you", "PAUSED": "paused", "FINISHED": "finished"}


def config_from(loaded: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update((loaded or {}).get("supervisor") or {})
    return merged


def desktop_notify(title: str, message: str) -> None:
    """Best-effort desktop notification (macOS / Linux); never raises."""
    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            script = f"display notification {_applescript(message)} with title {_applescript(title)}"
            subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], timeout=5, capture_output=True)
    except Exception:
        pass


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"')[:250] + '"'


def parse_quiet_hours(spec: str) -> tuple[dtime, dtime] | None:
    try:
        start, end = (part.strip() for part in spec.split("-", 1))
        return dtime.fromisoformat(start), dtime.fromisoformat(end)
    except (ValueError, AttributeError):
        return None


def in_quiet_hours(spec: str, local_now: datetime) -> bool:
    window = parse_quiet_hours(spec or "")
    if window is None:
        return False
    start, end = window
    now = local_now.time()
    return start <= now < end if start <= end else (now >= start or now < end)


class Supervisor:
    def __init__(self, engine: Any, config: dict[str, Any] | None = None, *,
                 notify: Callable[[str, str], None] = desktop_notify,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 local_now: Callable[[], datetime] = datetime.now) -> None:
        self.engine = engine
        self.cfg = config_from(config)
        self.notify = notify
        self.now = now
        self.local_now = local_now
        self._seen: dict[str, str] = {}
        self._stalled: set[str] = set()
        self._no_tab: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ queries

    def is_quiet(self) -> bool:
        return in_quiet_hours(self.cfg.get("quiet_hours", ""), self.local_now())

    def sends_today(self, conn) -> dict[str, int]:
        # Local midnight (a naive local time is interpreted as the machine's zone).
        midnight = self.local_now().replace(hour=0, minute=0, second=0, microsecond=0)
        since = midnight.astimezone(timezone.utc).isoformat()
        rows = conn.execute(
            """SELECT r.role, COUNT(*) AS n FROM events ev JOIN requests r ON r.id = ev.request_id
               WHERE ev.event_type = 'REQUEST_STATE_CHANGED' AND ev.created_at >= ?
               AND json_extract(ev.payload_json, '$.to_state') = 'SUBMITTING' GROUP BY r.role""", (since,))
        counts = {row["role"]: row["n"] for row in rows}
        return {"claude": counts.get("pm", 0), "chatgpt": counts.get("worker", 0)}

    # ------------------------------------------------------------ one pass

    def check(self) -> list[str]:
        """One supervision pass. Returns the alert messages it raised."""
        alerts: list[str] = []
        with self.engine.lock, self.engine.db.transaction() as conn:
            runtimes = store.all_runtimes(conn)
            first_pass = not self._seen and not self._stalled
            budget_hit = self._budget(conn)
            for rt in runtimes:
                name, status = rt["project_name"], rt["status"]
                if budget_hit and status == "RUNNING":
                    roles_used = {"pm", "worker"} if rt["mode"] == "ade" else {"worker"}
                    exhausted = [role for role in budget_hit if role in roles_used]
                    if exhausted:
                        reason = "Daily message budget reached: " + ", ".join(budget_hit[r] for r in exhausted)
                        store.update_runtime(conn, rt["project_id"], status="PAUSED", reason=reason[:500])
                        status = "PAUSED"
                        rt = rt | {"reason": reason}
                previous = self._seen.get(name)
                self._seen[name] = status
                if not first_pass and previous != status and status in ALERT_STATUSES:
                    alerts.append(self._alert(conn, rt, f"{name} {ALERT_STATUSES[status]}"
                                              + (f": {rt['reason']}" if rt.get("reason") else "")))
                missing = self._missing_tab(conn, rt) if status == "RUNNING" else None
                if missing and name not in self._no_tab:
                    self._no_tab.add(name)
                    site = "Claude" if missing["site"] == "claude" else "ChatGPT"
                    alerts.append(self._alert(conn, rt, f"{name} is waiting for a {site} tab, but none is "
                                              "connected. Open the dashboard and click Arrange windows."))
                elif not missing:
                    self._no_tab.discard(name)
                stalled = self._is_stalled(conn, rt) if status == "RUNNING" else False
                if stalled and name not in self._stalled:
                    self._stalled.add(name)
                    alerts.append(self._alert(conn, rt, f"{name} has had no activity for "
                                              f"{self.cfg['stall_minutes']} minutes"))
                elif not stalled:
                    self._stalled.discard(name)
        if self.cfg.get("notifications", True):
            for message in alerts:
                self.notify("Project Relay", message)
        return alerts

    def _budget(self, conn) -> dict[str, str]:
        used = self.sends_today(conn)
        hit = {}
        claude = int(self.cfg.get("claude_daily_messages") or 0)
        chatgpt = int(self.cfg.get("chatgpt_daily_messages") or 0)
        if claude and used["claude"] >= claude:
            hit["pm"] = f"Claude {used['claude']}/{claude}"
        if chatgpt and used["chatgpt"] >= chatgpt:
            hit["worker"] = f"ChatGPT {used['chatgpt']}/{chatgpt}"
        return hit

    def _missing_tab(self, conn, rt: dict[str, Any]) -> dict[str, Any] | None:
        check = getattr(self.engine, "missing_tab", None)
        if check is None or not rt.get("session_id"):
            return None
        return check(rt, store.latest_request(conn, rt["session_id"]))

    def _is_stalled(self, conn, rt: dict[str, Any]) -> bool:
        minutes = float(self.cfg.get("stall_minutes") or 0)
        if minutes <= 0 or self.is_quiet():
            return False
        last = conn.execute("SELECT MAX(created_at) FROM events WHERE project_id = ? "
                            "AND event_type NOT IN ('SUPERVISOR_ALERT')", (rt["project_id"],)).fetchone()[0]
        if not last:
            return False
        return (self.now() - datetime.fromisoformat(last)).total_seconds() >= minutes * 60

    def _alert(self, conn, rt: dict[str, Any], message: str) -> str:
        store.event(conn, project_id=rt["project_id"], event_type="SUPERVISOR_ALERT",
                    payload={"message": message[:500]})
        return message

    # ------------------------------------------------------------ thread

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                pass
            self._stop.wait(float(self.cfg.get("interval_seconds") or 30))

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, name="relay-supervisor", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
