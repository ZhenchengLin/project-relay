from __future__ import annotations

import json
import sqlite3
from typing import Any

from .storage.database import (
    RelayDatabase,
)


def insert_event(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    project_id: str,
    event_type: str,
    payload: dict[str, Any],
    created_at: str,
    session_id: str | None = None,
    request_id: str | None = None,
) -> None:
    """
    Insert one append-only event into an
    already-open transaction.

    This function never commits independently.
    """

    conn.execute(
        """
        INSERT INTO events (
            event_key,
            project_id,
            session_id,
            request_id,
            event_type,
            payload_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_key,
            project_id,
            session_id,
            request_id,
            event_type,
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ),
            created_at,
        ),
    )


def list_events(
    db: RelayDatabase,
    *,
    project_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
) -> list[dict[str, Any]]:

    clauses: list[str] = []
    values: list[str] = []

    if project_id is not None:
        clauses.append(
            "project_id = ?"
        )
        values.append(
            project_id
        )

    if session_id is not None:
        clauses.append(
            "session_id = ?"
        )
        values.append(
            session_id
        )

    if request_id is not None:
        clauses.append(
            "request_id = ?"
        )
        values.append(
            request_id
        )

    where = ""

    if clauses:
        where = (
            " WHERE "
            + " AND ".join(
                clauses
            )
        )

    rows = db.conn.execute(
        """
        SELECT
            id,
            event_key,
            project_id,
            session_id,
            request_id,
            event_type,
            payload_json,
            created_at
        FROM events
        """
        + where
        + " ORDER BY id",
        values,
    ).fetchall()

    return [
        {
            "id":
                int(
                    row["id"]
                ),

            "event_key":
                str(
                    row["event_key"]
                ),

            "project_id":
                str(
                    row["project_id"]
                ),

            "session_id":
                row["session_id"],

            "request_id":
                row["request_id"],

            "event_type":
                str(
                    row["event_type"]
                ),

            "payload":
                json.loads(
                    str(
                        row[
                            "payload_json"
                        ]
                    )
                ),

            "created_at":
                str(
                    row["created_at"]
                ),
        }
        for row in rows
    ]
