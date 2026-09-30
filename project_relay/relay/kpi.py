"""Per-run KPIs for the dashboard, computed from what SQLite already records."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def run_kpis(conn: sqlite3.Connection, project_id: str, session_id: str | None,
             now: datetime | None = None) -> dict[str, Any]:
    """KPIs for the project's current run (its session)."""
    now = now or datetime.now(timezone.utc)
    if not session_id:
        return {}
    session = conn.execute("SELECT created_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
    started = _parse(session["created_at"]) if session else None
    hours = max((now - started).total_seconds() / 3600, 1e-9) if started else None

    executions = conn.execute(
        """SELECT e.return_code, e.started_at, e.completed_at FROM executions e
           JOIN requests r ON r.id = e.request_id WHERE r.session_id = ? AND e.completed_at IS NOT NULL""",
        (session_id,)).fetchall()
    commands = len(executions)
    succeeded = sum(1 for e in executions if e["return_code"] == 0)
    durations = [(_parse(e["completed_at"]) - _parse(e["started_at"])).total_seconds()
                 for e in executions if _parse(e["completed_at"]) and _parse(e["started_at"])]

    def count(sql: str, *args: Any) -> int:
        return int(conn.execute(sql, args).fetchone()[0])

    loops = count("""SELECT COUNT(*) FROM watchdog_decisions d JOIN requests r ON r.id = d.request_id
                     WHERE r.session_id = ? AND d.status = 'LOOP'""", session_id)
    reviews = count("SELECT COUNT(*) FROM requests WHERE session_id = ? AND kind = 'PM_REVIEW' "
                    "AND COALESCE(json_extract(detail, '$.nudge'), 0) = 0", session_id)
    approved = count("""SELECT COUNT(*) FROM events WHERE event_type = 'REQUEST_STATE_CHANGED'
                        AND json_extract(payload_json, '$.details.approved_by') = 'pm'
                        AND request_id IN (SELECT id FROM requests WHERE session_id = ?)""", session_id)
    revised = count("SELECT COUNT(*) FROM requests WHERE session_id = ? AND kind = 'REVISE'", session_id)
    sends = {row["role"]: row["n"] for row in conn.execute(
        """SELECT r.role, COUNT(*) AS n FROM events ev JOIN requests r ON r.id = ev.request_id
           WHERE r.session_id = ? AND ev.event_type = 'REQUEST_STATE_CHANGED'
           AND json_extract(ev.payload_json, '$.to_state') = 'SUBMITTING' GROUP BY r.role""", (session_id,))}
    rollovers = count("""SELECT COUNT(*) FROM conversations WHERE project_id = ? AND status = 'RETIRED'
                         AND retired_at >= ?""", project_id, session["created_at"] if session else "")
    recoveries = count("""SELECT COUNT(*) FROM events WHERE event_type = 'RECOVERY_SUCCESSOR'
                          AND request_id IN (SELECT id FROM requests WHERE session_id = ?)""", session_id)
    last_event = conn.execute("SELECT MAX(created_at) FROM events WHERE project_id = ?", (project_id,)).fetchone()[0]
    last = _parse(last_event)
    tasks = conn.execute("SELECT status, COUNT(*) AS n FROM plan_tasks WHERE project_id = ? GROUP BY status",
                         (project_id,)).fetchall()
    by_status = {t["status"]: t["n"] for t in tasks}

    return {
        "started_at": session["created_at"] if session else None,
        "hours": round(hours, 2) if hours else None,
        "commands": commands,
        "succeeded": succeeded,
        "success_rate": round(succeeded / commands, 3) if commands else None,
        # A rate over a few minutes is noise; show it once the run is 15 min old.
        "commands_per_hour": round(commands / hours, 2) if hours and hours >= 0.25 and commands else None,
        "avg_command_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "loops_caught": loops,
        "reviews": reviews,
        "approved": approved,
        "revised": revised,
        "messages": {"pm": sends.get("pm", 0), "worker": sends.get("worker", 0)},
        "rollovers": rollovers,
        "recoveries": recoveries,
        "idle_seconds": int((now - last).total_seconds()) if last else None,
        "plan": {"total": sum(by_status.values()), "done": by_status.get("done", 0),
                 "doing": by_status.get("doing", 0), "blocked": by_status.get("blocked", 0)},
    }
