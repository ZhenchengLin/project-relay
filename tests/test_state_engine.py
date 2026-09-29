import sqlite3

import pytest

from project_relay.events import (
    list_events,
)

from project_relay.state import (
    DuplicateTransitionError,
    InvalidTransitionError,
    create_project,
    create_request,
    create_session,
    get_request_state,
    list_project_requests,
    transition_request,
)

from project_relay.storage.database import (
    RelayDatabase,
)

from project_relay.storage.schema import (
    SCHEMA_VERSION,
)


def seed_request(
    db,
    *,
    project_id="p1",
    session_id="s1",
    request_id="r1",
):
    create_project(
        db,
        project_id=project_id,
        name=project_id.upper(),
        repository_root=
            f"/tmp/{project_id}",
        expected_branch="main",
        conversation_url=(
            "https://chatgpt.com/c/"
            f"{project_id}"
        ),
    )

    create_session(
        db,
        session_id=session_id,
        project_id=project_id,
    )

    create_request(
        db,
        request_id=request_id,
        session_id=session_id,
        sequence_number=1,
        prompt_text="continue",
    )


def test_schema_initializes_all_v1_tables(
    tmp_path,
):
    path = (
        tmp_path
        / "relay.db"
    )

    with RelayDatabase(
        path
    ) as db:

        version = (
            db.conn.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
        )

        tables = {
            row[0]
            for row
            in db.conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            )
        }

    assert (
        version
        == SCHEMA_VERSION
    )

    assert {
        "projects",
        "sessions",
        "requests",
        "executions",
        "watchdog_votes",
        "watchdog_decisions",
        "events",
        "browser_leases",
    } <= tables


def test_transition_atomically_updates_state_and_event(
    tmp_path,
):
    with RelayDatabase(
        tmp_path / "relay.db"
    ) as db:

        seed_request(
            db
        )

        before = len(
            list_events(
                db,
                request_id="r1",
            )
        )

        result = (
            transition_request(
                db,
                request_id="r1",
                to_state=
                    "PREPARING_BROWSER",
                expected_from=
                    "QUEUED",
                payload={
                    "source":
                        "test",
                },
            )
        )

        after_events = (
            list_events(
                db,
                request_id="r1",
            )
        )

        assert (
            result.from_state
            == "QUEUED"
        )

        assert (
            result.to_state
            == "PREPARING_BROWSER"
        )

        assert (
            get_request_state(
                db,
                "r1",
            )
            == "PREPARING_BROWSER"
        )

        assert (
            len(
                after_events
            )
            == before + 1
        )

        assert (
            after_events[-1]
            ["event_type"]
            == "REQUEST_STATE_CHANGED"
        )

        assert (
            after_events[-1]
            ["payload"]
            ["from_state"]
            == "QUEUED"
        )

        assert (
            after_events[-1]
            ["payload"]
            ["to_state"]
            == "PREPARING_BROWSER"
        )

        assert (
            after_events[-1]
            ["payload"]
            ["details"]
            == {
                "source":
                    "test",
            }
        )


def test_invalid_transition_rolls_back_state_and_event(
    tmp_path,
):
    with RelayDatabase(
        tmp_path / "relay.db"
    ) as db:

        seed_request(
            db
        )

        before_events = (
            list_events(
                db,
                request_id="r1",
            )
        )

        with pytest.raises(
            InvalidTransitionError
        ):
            transition_request(
                db,
                request_id="r1",
                to_state=
                    "CLI_COMPLETE",
                expected_from=
                    "QUEUED",
            )

        assert (
            get_request_state(
                db,
                "r1",
            )
            == "QUEUED"
        )

        assert (
            list_events(
                db,
                request_id="r1",
            )
            == before_events
        )


def test_committed_state_survives_database_reopen(
    tmp_path,
):
    path = (
        tmp_path
        / "relay.db"
    )

    with RelayDatabase(
        path
    ) as db:

        seed_request(
            db
        )

        transition_request(
            db,
            request_id="r1",
            to_state=
                "PREPARING_BROWSER",
        )

    with RelayDatabase(
        path
    ) as reopened:

        assert (
            get_request_state(
                reopened,
                "r1",
            )
            == "PREPARING_BROWSER"
        )

        events = list_events(
            reopened,
            request_id="r1",
        )

        assert (
            events[-1]
            ["payload"]
            ["to_state"]
            == "PREPARING_BROWSER"
        )


def test_duplicate_transition_is_rejected_without_extra_event(
    tmp_path,
):
    with RelayDatabase(
        tmp_path / "relay.db"
    ) as db:

        seed_request(
            db
        )

        transition_request(
            db,
            request_id="r1",
            to_state=
                "PREPARING_BROWSER",
        )

        count = len(
            list_events(
                db,
                request_id="r1",
            )
        )

        with pytest.raises(
            DuplicateTransitionError
        ):
            transition_request(
                db,
                request_id="r1",
                to_state=
                    "PREPARING_BROWSER",
            )

        assert (
            get_request_state(
                db,
                "r1",
            )
            == "PREPARING_BROWSER"
        )

        assert (
            len(
                list_events(
                    db,
                    request_id="r1",
                )
            )
            == count
        )


def test_project_request_queries_are_isolated(
    tmp_path,
):
    with RelayDatabase(
        tmp_path / "relay.db"
    ) as db:

        seed_request(
            db,
            project_id="alpha",
            session_id=
                "alpha-session",
            request_id=
                "alpha-request",
        )

        seed_request(
            db,
            project_id="beta",
            session_id=
                "beta-session",
            request_id=
                "beta-request",
        )

        alpha = (
            list_project_requests(
                db,
                project_id="alpha",
            )
        )

        beta = (
            list_project_requests(
                db,
                project_id="beta",
            )
        )

        assert [
            row["id"]
            for row in alpha
        ] == [
            "alpha-request"
        ]

        assert [
            row["id"]
            for row in beta
        ] == [
            "beta-request"
        ]


def test_foreign_keys_are_enforced(
    tmp_path,
):
    with RelayDatabase(
        tmp_path / "relay.db"
    ) as db:

        with pytest.raises(
            sqlite3.IntegrityError
        ):
            create_session(
                db,
                session_id=(
                    "missing-project-"
                    "session"
                ),
                project_id=
                    "missing-project",
            )
