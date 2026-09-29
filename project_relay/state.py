from __future__ import annotations

from dataclasses import dataclass
from datetime import (
    datetime,
    timezone,
)
import hashlib
import sqlite3
from typing import Any

from .events import insert_event
from .storage.database import (
    RelayDatabase,
)


REQUEST_STATES = frozenset(
    {
        "QUEUED",
        "PREPARING_BROWSER",
        "READY_TO_SUBMIT",
        "SUBMITTING",
        "PROMPT_ACCEPTED",
        "WAITING_ASSISTANT",
        "ASSISTANT_BOUND",
        "ASSISTANT_COMPLETE",
        "COMMAND_VALIDATED",
        "RUNNING_CLI",
        "CLI_COMPLETE",
        "RUNNING_WATCHDOG",
        "CONTINUE_READY",
        "COMPLETED",
        "RECOVERY_REQUIRED",
        "HUMAN_REQUIRED",
        "FAILED",
        "CANCELLED",
    }
)


TERMINAL_STATES = frozenset(
    {
        "COMPLETED",
        "HUMAN_REQUIRED",
        "FAILED",
        "CANCELLED",
    }
)


_COMMON_STOPS = frozenset(
    {
        "HUMAN_REQUIRED",
        "FAILED",
        "CANCELLED",
    }
)


ALLOWED_TRANSITIONS: dict[
    str,
    frozenset[str],
] = {
    "QUEUED":
        frozenset(
            {
                "PREPARING_BROWSER",
            }
        )
        | _COMMON_STOPS,

    "PREPARING_BROWSER":
        frozenset(
            {
                "READY_TO_SUBMIT",
                "RECOVERY_REQUIRED",
            }
        )
        | _COMMON_STOPS,

    "READY_TO_SUBMIT":
        frozenset(
            {
                "SUBMITTING",
            }
        )
        | _COMMON_STOPS,

    "SUBMITTING":
        frozenset(
            {
                "PROMPT_ACCEPTED",
                "RECOVERY_REQUIRED",
            }
        )
        | _COMMON_STOPS,

    "PROMPT_ACCEPTED":
        frozenset(
            {
                "WAITING_ASSISTANT",
                "RECOVERY_REQUIRED",
            }
        )
        | _COMMON_STOPS,

    "WAITING_ASSISTANT":
        frozenset(
            {
                "ASSISTANT_BOUND",
                "RECOVERY_REQUIRED",
            }
        )
        | _COMMON_STOPS,

    "ASSISTANT_BOUND":
        frozenset(
            {
                "ASSISTANT_COMPLETE",
                "RECOVERY_REQUIRED",
            }
        )
        | _COMMON_STOPS,

    # COMPLETED directly: replies that are not expected to carry
    # a command (V2 rollover handoff requests).
    "ASSISTANT_COMPLETE":
        frozenset(
            {
                "COMMAND_VALIDATED",
                "COMPLETED",
            }
        )
        | _COMMON_STOPS,

    "COMMAND_VALIDATED":
        frozenset(
            {
                "RUNNING_CLI",
            }
        )
        | _COMMON_STOPS,

    "RUNNING_CLI":
        frozenset(
            {
                "CLI_COMPLETE",
                "RECOVERY_REQUIRED",
                "HUMAN_REQUIRED",
                "FAILED",
            }
        ),

    "CLI_COMPLETE":
        frozenset(
            {
                "RUNNING_WATCHDOG",
            }
        )
        | _COMMON_STOPS,

    "RUNNING_WATCHDOG":
        frozenset(
            {
                "CONTINUE_READY",
            }
        )
        | _COMMON_STOPS,

    "CONTINUE_READY":
        frozenset(
            {
                "COMPLETED",
            }
        )
        | _COMMON_STOPS,

    # Detailed deterministic recovery paths
    # are intentionally deferred to V1-G.
    "RECOVERY_REQUIRED":
        frozenset(
            {
                "HUMAN_REQUIRED",
                "FAILED",
                "CANCELLED",
            }
        ),

    "COMPLETED":
        frozenset(),

    "HUMAN_REQUIRED":
        frozenset(),

    "FAILED":
        frozenset(),

    "CANCELLED":
        frozenset(),
}


TRANSITION_UPDATE_COLUMNS = frozenset(
    {
        "user_turn_id",
        "assistant_turn_id",
        "assistant_text",
        "assistant_text_sha256",
        "baseline_json",
        "detail",
        "browser_lease",
        "successor_request_id",
        "model",
    }
)


class StateError(
    RuntimeError
):
    pass


class UnknownRequestError(
    StateError
):
    pass


class InvalidTransitionError(
    StateError
):
    pass


class DuplicateTransitionError(
    StateError
):
    pass


@dataclass(
    frozen=True
)
class TransitionResult:
    request_id: str
    from_state: str
    to_state: str
    event_id: int


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def sha256_text(
    text: str,
) -> str:
    return hashlib.sha256(
        text.encode(
            "utf-8"
        )
    ).hexdigest()


def create_project(
    db: RelayDatabase,
    *,
    project_id: str,
    name: str,
    repository_root: str,
    expected_branch:
        str | None = None,
    conversation_url:
        str | None = None,
) -> None:

    now = utc_now()

    with db.transaction() as conn:

        conn.execute(
            """
            INSERT INTO projects (
                id,
                name,
                repository_root,
                expected_branch,
                conversation_url,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                name,
                repository_root,
                expected_branch,
                conversation_url,
                now,
                now,
            ),
        )

        insert_event(
            conn,
            event_key=(
                "project-created:"
                f"{project_id}"
            ),
            project_id=project_id,
            event_type=
                "PROJECT_CREATED",
            payload={
                "name": name,
            },
            created_at=now,
        )


def create_session(
    db: RelayDatabase,
    *,
    session_id: str,
    project_id: str,
) -> None:

    now = utc_now()

    with db.transaction() as conn:

        conn.execute(
            """
            INSERT INTO sessions (
                id,
                project_id,
                status,
                created_at,
                updated_at
            )
            VALUES (
                ?,
                ?,
                'OPEN',
                ?,
                ?
            )
            """,
            (
                session_id,
                project_id,
                now,
                now,
            ),
        )

        insert_event(
            conn,
            event_key=(
                "session-created:"
                f"{session_id}"
            ),
            project_id=project_id,
            session_id=session_id,
            event_type=
                "SESSION_CREATED",
            payload={
                "status": "OPEN",
            },
            created_at=now,
        )


def create_request(
    db: RelayDatabase,
    *,
    request_id: str,
    session_id: str,
    sequence_number: int,
    prompt_text: str,
) -> None:

    if sequence_number < 1:
        raise ValueError(
            "sequence_number must "
            "be >= 1"
        )

    now = utc_now()

    prompt_sha256 = (
        sha256_text(
            prompt_text
        )
    )

    with db.transaction() as conn:

        session = conn.execute(
            """
            SELECT project_id
            FROM sessions
            WHERE id = ?
            """,
            (
                session_id,
            ),
        ).fetchone()

        if session is None:
            raise StateError(
                "Unknown session: "
                f"{session_id}"
            )

        project_id = str(
            session["project_id"]
        )

        conn.execute(
            """
            INSERT INTO requests (
                id,
                session_id,
                sequence_number,
                state,
                prompt_text,
                prompt_sha256,
                created_at,
                updated_at
            )
            VALUES (
                ?,
                ?,
                ?,
                'QUEUED',
                ?,
                ?,
                ?,
                ?
            )
            """,
            (
                request_id,
                session_id,
                sequence_number,
                prompt_text,
                prompt_sha256,
                now,
                now,
            ),
        )

        insert_event(
            conn,
            event_key=(
                "request-created:"
                f"{request_id}"
            ),
            project_id=project_id,
            session_id=session_id,
            request_id=request_id,
            event_type=
                "REQUEST_CREATED",
            payload={
                "sequence_number":
                    sequence_number,

                "state":
                    "QUEUED",

                "prompt_sha256":
                    prompt_sha256,
            },
            created_at=now,
        )


def get_request_state(
    db: RelayDatabase,
    request_id: str,
) -> str:

    row = db.conn.execute(
        """
        SELECT state
        FROM requests
        WHERE id = ?
        """,
        (
            request_id,
        ),
    ).fetchone()

    if row is None:
        raise UnknownRequestError(
            "Unknown request: "
            f"{request_id}"
        )

    return str(
        row["state"]
    )


def transition_request_in(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    to_state: str,
    expected_from:
        str | None = None,
    payload:
        dict[str, Any]
        | None = None,
    updates:
        dict[str, Any]
        | None = None,
) -> TransitionResult:
    """
    `updates` sets extra request columns in the same transaction
    as the state change (whitelisted in TRANSITION_UPDATE_COLUMNS).
    """

    if to_state not in REQUEST_STATES:
        raise InvalidTransitionError(
            "Unknown request state: "
            f"{to_state}"
        )

    updates = dict(
        updates or {}
    )

    unknown_columns = (
        set(updates)
        - TRANSITION_UPDATE_COLUMNS
    )

    if unknown_columns:
        raise ValueError(
            "Unsupported transition update columns: "
            + ", ".join(
                sorted(unknown_columns)
            )
        )

    now = utc_now()

    row = conn.execute(
        """
        SELECT
            requests.state,
            requests.session_id,
            sessions.project_id
        FROM requests
        JOIN sessions
          ON sessions.id =
             requests.session_id
        WHERE requests.id = ?
        """,
        (
            request_id,
        ),
    ).fetchone()

    if row is None:
        raise UnknownRequestError(
            "Unknown request: "
            f"{request_id}"
        )

    from_state = str(
        row["state"]
    )

    session_id = str(
        row["session_id"]
    )

    project_id = str(
        row["project_id"]
    )

    if (
        expected_from
        is not None
        and from_state
        != expected_from
    ):
        raise (
            InvalidTransitionError(
                f"Request {request_id} "
                f"is in {from_state}, "
                "not expected state "
                f"{expected_from}."
            )
        )

    if from_state == to_state:
        raise (
            DuplicateTransitionError(
                f"Request {request_id} "
                f"is already in "
                f"{to_state}."
            )
        )

    allowed = (
        ALLOWED_TRANSITIONS.get(
            from_state,
            frozenset(),
        )
    )

    if to_state not in allowed:
        raise (
            InvalidTransitionError(
                "Invalid request "
                "transition: "
                f"{from_state} "
                "-> "
                f"{to_state}."
            )
        )

    extra_sql = "".join(
        f", {column} = ?"
        for column in updates
    )

    cursor = conn.execute(
        f"""
        UPDATE requests
        SET
            state = ?,
            updated_at = ?
            {extra_sql}
        WHERE
            id = ?
            AND state = ?
        """,
        (
            to_state,
            now,
            *updates.values(),
            request_id,
            from_state,
        ),
    )

    if cursor.rowcount != 1:
        raise StateError(
            "Concurrent state change "
            "detected for request "
            f"{request_id}."
        )

    event_key = (
        "request-transition:"
        f"{request_id}:"
        f"{from_state}:"
        f"{to_state}"
    )

    event_payload = {
        "from_state":
            from_state,

        "to_state":
            to_state,
    }

    if payload:
        event_payload[
            "details"
        ] = payload

    try:
        insert_event(
            conn,
            event_key=event_key,
            project_id=
                project_id,
            session_id=
                session_id,
            request_id=
                request_id,
            event_type=
                "REQUEST_STATE_CHANGED",
            payload=
                event_payload,
            created_at=now,
        )

    except sqlite3.IntegrityError as exc:
        raise (
            DuplicateTransitionError(
                "Transition event "
                "already exists: "
                f"{event_key}"
            )
        ) from exc

    event_row = conn.execute(
        """
        SELECT id
        FROM events
        WHERE event_key = ?
        """,
        (
            event_key,
        ),
    ).fetchone()

    if event_row is None:
        raise StateError(
            "Transition event was "
            "not persisted."
        )

    event_id = int(
        event_row["id"]
    )

    return TransitionResult(
        request_id=request_id,
        from_state=from_state,
        to_state=to_state,
        event_id=event_id,
    )


def transition_request(
    db: RelayDatabase,
    **kwargs: Any,
) -> TransitionResult:
    """Transactional wrapper; see transition_request_in."""

    with db.transaction() as conn:
        return transition_request_in(
            conn,
            **kwargs,
        )


def list_project_requests(
    db: RelayDatabase,
    *,
    project_id: str,
) -> list[
    dict[str, Any]
]:

    rows = db.conn.execute(
        """
        SELECT
            requests.id,
            requests.session_id,
            requests.sequence_number,
            requests.state,
            requests.prompt_sha256
        FROM requests
        JOIN sessions
          ON sessions.id =
             requests.session_id
        WHERE
            sessions.project_id = ?
        ORDER BY
            requests.session_id,
            requests.sequence_number
        """,
        (
            project_id,
        ),
    ).fetchall()

    return [
        dict(row)
        for row in rows
    ]
