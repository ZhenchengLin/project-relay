from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

from .migrations import apply_migrations


class RelayDatabase:

    def __init__(
        self,
        path: str | Path,
        *,
        check_same_thread: bool = True,
    ):
        # check_same_thread=False is for prelayd, which serializes all
        # access to this connection behind one lock.
        if str(path) == ":memory:":
            self.path = ":memory:"

        else:
            expanded = Path(
                path
            ).expanduser()

            expanded.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            self.path = str(
                expanded
            )

        self.conn = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=5.0,
            check_same_thread=check_same_thread,
        )

        self.conn.row_factory = (
            sqlite3.Row
        )

        self.conn.execute(
            "PRAGMA foreign_keys = ON"
        )

        self.conn.execute(
            "PRAGMA busy_timeout = 5000"
        )

        if self.path != ":memory:":
            self.conn.execute(
                "PRAGMA journal_mode = WAL"
            )

        apply_migrations(
            self.conn
        )

    @contextmanager
    def transaction(
        self,
    ) -> Iterator[
        sqlite3.Connection
    ]:
        self.conn.execute(
            "BEGIN IMMEDIATE"
        )

        try:
            yield self.conn

        except Exception:
            self.conn.rollback()
            raise

        else:
            self.conn.commit()

    def close(
        self,
    ) -> None:
        self.conn.close()

    def __enter__(
        self,
    ) -> "RelayDatabase":
        return self

    def __exit__(
        self,
        exc_type,
        exc,
        tb,
    ) -> None:
        self.close()
