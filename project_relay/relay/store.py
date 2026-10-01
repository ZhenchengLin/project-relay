"""
V2 SQL helpers. Every function takes an open connection and runs inside
the caller's transaction, so multi-row changes commit atomically.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from ..events import insert_event
from ..state import StateError, sha256_text, utc_now


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def event(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    event_type: str,
    payload: dict[str, Any],
    session_id: str | None = None,
    request_id: str | None = None,
    key: str | None = None,
) -> None:
    insert_event(
        conn,
        event_key=key or f"{event_type.lower()}:{uuid.uuid4().hex}",
        project_id=project_id,
        session_id=session_id,
        request_id=request_id,
        event_type=event_type,
        payload=payload,
        created_at=utc_now(),
    )


# ---------------------------------------------------------------- projects

def project_by_name(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone())


def ensure_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    root: str,
    branch: str | None,
) -> dict[str, Any]:
    existing = project_by_name(conn, name)
    if existing is not None:
        if existing["repository_root"] != root:
            raise StateError(
                f"Project {name} is stored with root {existing['repository_root']}, "
                f"not {root}."
            )
        return existing

    project_id = f"project-{name}"
    now = utc_now()
    conn.execute(
        """
        INSERT INTO projects (id, name, repository_root, expected_branch,
                              conversation_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, NULL, ?, ?)
        """,
        (project_id, name, root, branch or None, now, now),
    )
    event(conn, project_id=project_id, event_type="PROJECT_CREATED",
          payload={"name": name}, key=f"project-created:{project_id}")
    return project_by_name(conn, name)  # type: ignore[return-value]


# ----------------------------------------------------------- conversations

def active_conversation(conn: sqlite3.Connection, project_id: str,
                        role: str = "worker") -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM conversations WHERE project_id = ? AND role = ? AND status = 'ACTIVE'",
            (project_id, role),
        ).fetchone()
    )


def get_conversation(conn: sqlite3.Connection, conversation_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise StateError(f"Unknown conversation: {conversation_id}")
    return dict(row)


def create_conversation(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    url: str | None,
    predecessor_id: str | None,
    role: str = "worker",
    site: str = "chatgpt",
) -> dict[str, Any]:
    if active_conversation(conn, project_id, role) is not None:
        raise StateError(f"Project already has an active {role} conversation.")
    seq = conn.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM conversations WHERE project_id = ?",
        (project_id,),
    ).fetchone()[0]
    conversation_id = new_id("conv")
    conn.execute(
        """
        INSERT INTO conversations (id, project_id, sequence_number, conversation_url,
                                   predecessor_id, status, created_at, role, site)
        VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
        """,
        (conversation_id, project_id, seq, url, predecessor_id, utc_now(), role, site),
    )
    event(conn, project_id=project_id, event_type="CONVERSATION_STARTED",
          payload={"conversation_id": conversation_id, "url": url, "role": role, "site": site,
                   "predecessor_id": predecessor_id, "sequence_number": seq})
    return get_conversation(conn, conversation_id)


def retire_conversation(conn: sqlite3.Connection, conversation_id: str, reason: str) -> None:
    conv = get_conversation(conn, conversation_id)
    if conv["status"] != "ACTIVE":
        return
    conn.execute(
        "UPDATE conversations SET status = 'RETIRED', retire_reason = ?, retired_at = ? WHERE id = ?",
        (reason, utc_now(), conversation_id),
    )
    event(conn, project_id=conv["project_id"], event_type="CONVERSATION_RETIRED",
          payload={"conversation_id": conversation_id, "reason": reason,
                   "char_count": conv["char_count"]})


def set_conversation_url(conn: sqlite3.Connection, conversation_id: str, url: str) -> None:
    conv = get_conversation(conn, conversation_id)
    if conv["conversation_url"] == url:
        return
    if conv["conversation_url"] is not None:
        raise StateError(
            f"Conversation {conversation_id} is bound to {conv['conversation_url']}, not {url}."
        )
    conn.execute("UPDATE conversations SET conversation_url = ? WHERE id = ?", (url, conversation_id))
    event(conn, project_id=conv["project_id"], event_type="CONVERSATION_URL_BOUND",
          payload={"conversation_id": conversation_id, "url": url})


def add_conversation_chars(conn: sqlite3.Connection, conversation_id: str, count: int) -> None:
    conn.execute(
        "UPDATE conversations SET char_count = char_count + ? WHERE id = ?",
        (max(int(count), 0), conversation_id),
    )


# ----------------------------------------------------------------- runtime

def runtime(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM project_runtime WHERE project_id = ?", (project_id,)).fetchone())


def all_runtimes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT project_runtime.*, projects.name AS project_name,
                   projects.repository_root AS repository_root
            FROM project_runtime JOIN projects ON projects.id = project_runtime.project_id
            ORDER BY projects.name
            """
        )
    ]


def put_runtime(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session_id: str,
    max_cycles: int,
    mode: str = "solo",
    goal: str | None = None,
    review_policy: str = "risky",
    pace: str = "step",
    checkin_every: int = 8,
) -> None:
    conn.execute(
        """
        INSERT INTO project_runtime (project_id, session_id, status, reason, model_mode,
                                     progress_streak, cycle_count, max_cycles, updated_at,
                                     mode, goal, review_policy, pace, checkin_every)
        VALUES (?, ?, 'RUNNING', NULL, 'DEFAULT', 0, 0, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id) DO UPDATE SET
            session_id = excluded.session_id, status = 'RUNNING', reason = NULL,
            model_mode = 'DEFAULT', progress_streak = 0, cycle_count = 0,
            max_cycles = excluded.max_cycles, updated_at = excluded.updated_at,
            mode = excluded.mode, goal = excluded.goal, review_policy = excluded.review_policy,
            pace = excluded.pace, checkin_every = excluded.checkin_every
        """,
        (project_id, session_id, max_cycles, utc_now(), mode, goal, review_policy, pace, checkin_every),
    )


RUNTIME_FIELDS = frozenset({"status", "reason", "model_mode", "progress_streak", "cycle_count", "pace",
                            "checkin_every", "review_policy"})


def update_runtime(conn: sqlite3.Connection, project_id: str, **fields: Any) -> None:
    unknown = set(fields) - RUNTIME_FIELDS
    if unknown:
        raise ValueError(f"Unknown runtime fields: {sorted(unknown)}")
    before = runtime(conn, project_id)
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE project_runtime SET {sets}, updated_at = ? WHERE project_id = ?",
        (*fields.values(), utc_now(), project_id),
    )
    changed = {k: v for k, v in fields.items() if before is None or before.get(k) != v}
    if changed.keys() & {"status", "model_mode"}:
        event(conn, project_id=project_id, event_type="RUNTIME_CHANGED", payload=changed)


# ---------------------------------------------------------------- sessions

def create_session(conn: sqlite3.Connection, project_id: str) -> str:
    session_id = new_id("sess")
    now = utc_now()
    conn.execute(
        "INSERT INTO sessions (id, project_id, status, created_at, updated_at) VALUES (?, ?, 'OPEN', ?, ?)",
        (session_id, project_id, now, now),
    )
    event(conn, project_id=project_id, session_id=session_id, event_type="SESSION_CREATED",
          payload={"status": "OPEN"}, key=f"session-created:{session_id}")
    return session_id


# ---------------------------------------------------------------- requests

def get_request(conn: sqlite3.Connection, request_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT requests.*, sessions.project_id AS project_id
        FROM requests JOIN sessions ON sessions.id = requests.session_id
        WHERE requests.id = ?
        """,
        (request_id,),
    ).fetchone()
    if row is None:
        raise StateError(f"Unknown request: {request_id}")
    return dict(row)


def latest_request(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id FROM requests WHERE session_id = ? ORDER BY sequence_number DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return get_request(conn, row["id"]) if row else None


def recent_requests(conn: sqlite3.Connection, session_id: str, limit: int) -> list[dict[str, Any]]:
    """Newest first."""
    rows = conn.execute(
        "SELECT id FROM requests WHERE session_id = ? ORDER BY sequence_number DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [get_request(conn, r["id"]) for r in rows]


def create_request(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    conversation_id: str,
    kind: str,
    prompt: str,
    model: str | None,
    detail: str | None = None,
    role: str = "worker",
) -> str:
    project_id = conn.execute(
        "SELECT project_id FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()["project_id"]
    seq = conn.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM requests WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    request_id = new_id("req")
    now = utc_now()
    prompt_sha = sha256_text(prompt)
    conn.execute(
        """
        INSERT INTO requests (id, session_id, sequence_number, state, prompt_text, prompt_sha256,
                              conversation_id, kind, model, detail, created_at, updated_at, role)
        VALUES (?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (request_id, session_id, seq, prompt, prompt_sha, conversation_id, kind,
         model or None, detail, now, now, role),
    )
    event(conn, project_id=project_id, session_id=session_id, request_id=request_id,
          event_type="REQUEST_CREATED",
          payload={"sequence_number": seq, "state": "QUEUED", "kind": kind, "role": role,
                   "model": model, "prompt_sha256": prompt_sha,
                   "conversation_id": conversation_id},
          key=f"request-created:{request_id}")
    return request_id


REQUEST_FIELDS = frozenset({"model", "baseline_json", "browser_lease", "successor_request_id", "detail"})


def set_request_fields(conn: sqlite3.Connection, request_id: str, **fields: Any) -> None:
    unknown = set(fields) - REQUEST_FIELDS
    if unknown:
        raise ValueError(f"Unknown request fields: {sorted(unknown)}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE requests SET {sets}, updated_at = ? WHERE id = ?",
        (*fields.values(), utc_now(), request_id),
    )


def detail_json(request: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(request.get("detail") or "{}")
    except json.JSONDecodeError:
        return {"text": request.get("detail")}
    return value if isinstance(value, dict) else {"value": value}


# -------------------------------------------------------------- executions

def create_execution(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    command: str,
    cwd: str,
    git_before: dict[str, Any],
) -> str:
    execution_id = new_id("exec")
    conn.execute(
        """
        INSERT INTO executions (id, request_id, command_sha256, command_text, cwd, git_before_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (execution_id, request_id, sha256_text(command), command, cwd, json.dumps(git_before)),
    )
    return execution_id


def execution_for(conn: sqlite3.Connection, request_id: str) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM executions WHERE request_id = ?", (request_id,)).fetchone())


def mark_execution_started(conn: sqlite3.Connection, execution_id: str) -> None:
    cur = conn.execute(
        "UPDATE executions SET started_at = ? WHERE id = ? AND started_at IS NULL",
        (utc_now(), execution_id),
    )
    if cur.rowcount != 1:
        raise StateError(f"Execution {execution_id} was already started.")


def set_execution_pid(conn: sqlite3.Connection, execution_id: str, pid: int) -> None:
    conn.execute("UPDATE executions SET pid = ? WHERE id = ?", (pid, execution_id))


def finish_execution(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    return_code: int,
    output: str,
    git_after: dict[str, Any],
) -> None:
    cur = conn.execute(
        """
        UPDATE executions
        SET completed_at = ?, return_code = ?, stdout = ?, stderr = '',
            terminal_output_sha256 = ?, git_after_json = ?
        WHERE id = ? AND started_at IS NOT NULL AND completed_at IS NULL
        """,
        (utc_now(), return_code, output, sha256_text(output), json.dumps(git_after), execution_id),
    )
    if cur.rowcount != 1:
        raise StateError(f"Execution {execution_id} is not running.")


def cycle_from_execution(execution: dict[str, Any]) -> dict[str, Any]:
    """Shape expected by core.deterministic_vote / core.ollama_vote."""
    return {
        "command": execution.get("command_text") or "",
        "command_sha256": execution.get("command_sha256"),
        "terminal_output": execution.get("stdout") or "",
        "return_code": execution.get("return_code"),
        "git_before": json.loads(execution.get("git_before_json") or "{}"),
        "git_after": json.loads(execution.get("git_after_json") or "{}"),
    }


def recent_cycles(conn: sqlite3.Connection, session_id: str, limit: int) -> list[dict[str, Any]]:
    """Completed executions for one run, oldest first."""
    rows = conn.execute(
        """
        SELECT executions.* FROM executions
        JOIN requests ON requests.id = executions.request_id
        WHERE requests.session_id = ? AND executions.completed_at IS NOT NULL
        ORDER BY requests.sequence_number DESC LIMIT ?
        """,
        (session_id, limit),
    ).fetchall()
    return [cycle_from_execution(dict(r)) for r in reversed(rows)]


def record_watchdog(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    status: str,
    reason: str,
    votes: list[dict[str, Any]],
) -> None:
    now = utc_now()
    for vote in votes:
        conn.execute(
            """
            INSERT INTO watchdog_votes (request_id, voter, model, verdict, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (request_id, vote["voter"], vote.get("model"), vote["verdict"], vote["reason"], now),
        )
    conn.execute(
        """
        INSERT INTO watchdog_decisions (request_id, status, reason, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(request_id) DO UPDATE SET status = excluded.status,
            reason = excluded.reason, created_at = excluded.created_at
        """,
        (request_id, status, reason, now),
    )


def recent_events(conn: sqlite3.Connection, project_id: str, *, after_id: int = 0,
                  limit: int = 200, include_diag: bool = False) -> list[dict[str, Any]]:
    """Timeline for the dashboard, oldest first."""
    rows = conn.execute(
        f"""
        SELECT id, created_at, event_type, request_id, payload_json FROM events
        WHERE project_id = ? AND id > ? {"" if include_diag else "AND event_type != 'BROWSER_DIAG'"}
        ORDER BY id DESC LIMIT ?
        """,
        (project_id, after_id, limit),
    ).fetchall()
    return [dict(r) | {"payload": json.loads(r["payload_json"])} for r in reversed(rows)]
