from __future__ import annotations

import sqlite3

from .schema import (
    MIGRATION_1_SQL,
    MIGRATION_2_SQL,
    MIGRATION_3_SQL,
    MIGRATION_4_SQL,
    SCHEMA_VERSION,
)


# Ordered, append-only. Index i upgrades user_version i -> i + 1.
MIGRATIONS = (
    MIGRATION_1_SQL,
    MIGRATION_2_SQL,
    MIGRATION_3_SQL,
    MIGRATION_4_SQL,
)


def current_schema_version(
    conn: sqlite3.Connection,
) -> int:
    row = conn.execute(
        "PRAGMA user_version"
    ).fetchone()

    return int(row[0])


def apply_migrations(
    conn: sqlite3.Connection,
) -> None:
    version = current_schema_version(
        conn
    )

    if version > SCHEMA_VERSION:
        raise RuntimeError(
            "Database schema version "
            f"{version} is newer than supported "
            f"version {SCHEMA_VERSION}."
        )

    while version < SCHEMA_VERSION:
        target = version + 1

        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + MIGRATIONS[version]
                + (
                    "\nPRAGMA user_version = "
                    f"{target};\n"
                )
                + "COMMIT;"
            )

        except Exception:
            if conn.in_transaction:
                conn.rollback()

            raise

        version = current_schema_version(
            conn
        )

        if version != target:
            raise RuntimeError(
                "Database schema upgrade path is "
                "incomplete: "
                f"found {version}, "
                f"expected {target}."
            )
