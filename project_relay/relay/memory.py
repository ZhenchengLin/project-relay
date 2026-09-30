"""
Project memory and the planner's task list.

Notes: any line `RELAY_NOTE: <fact>` in a planner reply (the ADE PM, or the
solo ChatGPT chat) is stored as a durable project note. You can add and remove
notes from the dashboard or `prelay notes`. Active notes are injected into
every kickoff, handoff and fresh chat, so a chat rollover never loses them.

Plan: a block

    RELAY_PLAN
    - [x] T1 Load the data
    - [~] T2 Train the baseline      (~ = in progress)
    - [ ] T3 Evaluate                (! = blocked)
    END_RELAY_PLAN

replaces the project's task list; the dashboard shows progress from it.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..state import utc_now
from . import store

NOTE_MARKER = "RELAY_NOTE:"
PLAN_START = "RELAY_PLAN"
PLAN_END = "END_RELAY_PLAN"
MAX_ACTIVE_NOTES = 50
MAX_NOTE_CHARS = 500
MAX_MEMORY_CHARS = 5000

STATUS_BY_MARK = {" ": "todo", "": "todo", "x": "done", "X": "done", "~": "doing", "!": "blocked"}
MARK_BY_STATUS = {"todo": " ", "done": "x", "doing": "~", "blocked": "!"}

_PLAN_ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])?\s*\[([ xX~!]?)\]\s*(.+?)\s*$")
_TASK_KEY = re.compile(r"^([A-Za-z]{0,3}\d+[A-Za-z]?)[:.)]?\s+(.+)$")


@dataclass(frozen=True)
class PlanTask:
    key: str
    title: str
    status: str


def _clean(line: str) -> str:
    return line.strip().strip("`*").strip()


def parse_notes(text: str) -> list[str]:
    notes = []
    for line in (text or "").splitlines():
        cleaned = _clean(line)
        if cleaned.startswith(NOTE_MARKER):
            note = cleaned[len(NOTE_MARKER):].strip().strip("*`").strip()
            if note:
                notes.append(note[:MAX_NOTE_CHARS])
    return notes


def parse_plan(text: str) -> list[PlanTask] | None:
    """The last complete RELAY_PLAN block, or None if the reply has none."""
    lines = (text or "").splitlines()
    cleaned = [_clean(line) for line in lines]
    starts = [i for i, line in enumerate(cleaned) if line == PLAN_START]
    for start in reversed(starts):
        end = next((i for i in range(start + 1, len(cleaned)) if cleaned[i] == PLAN_END), None)
        if end is None:
            continue
        tasks: list[PlanTask] = []
        for raw in lines[start + 1:end]:
            match = _PLAN_ITEM.match(raw)
            if not match:
                continue
            status = STATUS_BY_MARK.get(match.group(1), "todo")
            rest = match.group(2).strip()
            keyed = _TASK_KEY.match(rest)
            key, title = (keyed.group(1), keyed.group(2)) if keyed else (f"T{len(tasks) + 1}", rest)
            if key in {t.key for t in tasks}:
                key = f"T{len(tasks) + 1}"
            tasks.append(PlanTask(key=key, title=title[:200], status=status))
        return tasks
    return None


# ------------------------------------------------------------------ storage

def add_note(conn: sqlite3.Connection, project_id: str, text: str, *, source: str,
             request_id: str | None = None) -> int | None:
    """Store a note unless an identical active one exists. Returns its id."""
    text = " ".join((text or "").split())[:MAX_NOTE_CHARS]
    if not text:
        return None
    if conn.execute("SELECT 1 FROM project_notes WHERE project_id = ? AND active = 1 AND text = ?",
                    (project_id, text)).fetchone():
        return None
    cur = conn.execute(
        "INSERT INTO project_notes (project_id, source, text, active, request_id, created_at) VALUES (?, ?, ?, 1, ?, ?)",
        (project_id, source, text, request_id, utc_now()))
    # Keep the newest MAX_ACTIVE_NOTES active.
    conn.execute(
        """UPDATE project_notes SET active = 0 WHERE project_id = ? AND active = 1 AND id NOT IN (
               SELECT id FROM project_notes WHERE project_id = ? AND active = 1 ORDER BY id DESC LIMIT ?)""",
        (project_id, project_id, MAX_ACTIVE_NOTES))
    store.event(conn, project_id=project_id, request_id=request_id, event_type="NOTE_ADDED",
                payload={"id": cur.lastrowid, "source": source, "text": text})
    return cur.lastrowid


def remove_note(conn: sqlite3.Connection, project_id: str, note_id: int) -> bool:
    cur = conn.execute("UPDATE project_notes SET active = 0 WHERE id = ? AND project_id = ? AND active = 1",
                       (note_id, project_id))
    if cur.rowcount:
        store.event(conn, project_id=project_id, event_type="NOTE_REMOVED", payload={"id": note_id})
    return bool(cur.rowcount)


def active_notes(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, source, text, created_at FROM project_notes WHERE project_id = ? AND active = 1 ORDER BY id",
        (project_id,))]


def replace_plan(conn: sqlite3.Connection, project_id: str, tasks: list[PlanTask],
                 request_id: str | None = None) -> None:
    before = {t["task_key"]: t["status"] for t in plan(conn, project_id)}
    conn.execute("DELETE FROM plan_tasks WHERE project_id = ?", (project_id,))
    now = utc_now()
    for position, task in enumerate(tasks):
        conn.execute(
            "INSERT INTO plan_tasks (project_id, task_key, position, title, status, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, task.key, position, task.title, task.status, now))
    changed = {t.key: t.status for t in tasks if before.get(t.key) != t.status}
    store.event(conn, project_id=project_id, request_id=request_id, event_type="PLAN_UPDATED",
                payload={"tasks": len(tasks), "done": sum(t.status == "done" for t in tasks), "changed": changed})


def plan(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT task_key, title, status, updated_at FROM plan_tasks WHERE project_id = ? ORDER BY position",
        (project_id,))]


def capture(conn: sqlite3.Connection, project_id: str, text: str, *,
            request_id: str | None = None) -> dict[str, int]:
    """Store notes and the plan found in a planner reply."""
    added = sum(1 for note in parse_notes(text)
                if add_note(conn, project_id, note, source="model", request_id=request_id))
    tasks = parse_plan(text)
    if tasks is not None:
        replace_plan(conn, project_id, tasks, request_id=request_id)
    return {"notes": added, "plan_tasks": -1 if tasks is None else len(tasks)}


def render(conn: sqlite3.Connection, project_id: str) -> str:
    """Memory section for a kickoff / handoff / fresh chat ('' when empty)."""
    notes = active_notes(conn, project_id)
    tasks = plan(conn, project_id)
    if not notes and not tasks:
        return ""
    parts = ["=== Project memory (kept by Project Relay; always holds) ==="]
    parts += [f"- {n['text']}" for n in notes]
    if tasks:
        parts += ["", "Current plan:"]
        parts += [f"[{MARK_BY_STATUS.get(t['status'], ' ')}] {t['task_key']} {t['title']}" for t in tasks]
    parts.append("=== End of project memory ===")
    text = "\n".join(parts)
    if len(text) > MAX_MEMORY_CHARS:
        text = text[:MAX_MEMORY_CHARS - 40].rstrip() + "\n…\n=== End of project memory ==="
    return text


PLANNER_HINT = (
    f"- To make Relay remember a durable fact across chats, add a line `{NOTE_MARKER} <fact>`.\n"
    f"- Keep a short plan: when it changes, include a {PLAN_START} … {PLAN_END} block of "
    "`- [ ] T1 title` items ([x] done, [~] in progress, [!] blocked)."
)
