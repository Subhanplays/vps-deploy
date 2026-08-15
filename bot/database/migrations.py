"""Schema migrations for the SQLite database.

Migrations are applied in order and tracked with ``PRAGMA user_version``.
Adding a new migration later is safe - existing data is never dropped.

Each entry's ``sql`` list may contain SQL strings or callables
``(conn) -> None`` for conditional changes (e.g. ``ALTER TABLE`` guards).
"""

from __future__ import annotations


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


MIGRATIONS = [
    {
        "id": 1,
        "name": "initial_schema",
        "sql": [
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS vps (
                id             TEXT PRIMARY KEY,
                user_id        INTEGER NOT NULL,
                name           TEXT NOT NULL,
                container_id   TEXT,
                container_name TEXT,
                os             TEXT NOT NULL,
                image          TEXT NOT NULL,
                hostname       TEXT NOT NULL,
                ram            REAL NOT NULL,
                cpu            REAL NOT NULL,
                disk           REAL NOT NULL,
                status         TEXT NOT NULL DEFAULT 'creating',
                suspended      INTEGER NOT NULL DEFAULT 0,
                ssh_command    TEXT,
                error          TEXT,
                created_at     TIMESTAMP DEFAULT (datetime('now')),
                updated_at     TIMESTAMP DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_vps_user ON vps (user_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_vps_status ON vps (status)
            """,
            """
            CREATE TABLE IF NOT EXISTS bans (
                user_id    INTEGER PRIMARY KEY,
                reason     TEXT,
                banned_by  INTEGER,
                created_at TIMESTAMP DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER,
                action    TEXT NOT NULL,
                details   TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs (created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS plans (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                ram        REAL NOT NULL,
                cpu        REAL NOT NULL,
                disk       REAL NOT NULL,
                enabled    INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT (datetime('now'))
            )
            """,
        ],
    },
    {
        "id": 2,
        "name": "user_cooldown_column",
        "sql": [
            lambda conn: _add_column_if_missing(conn, "users", "last_create_at", "TIMESTAMP"),
            """
            CREATE INDEX IF NOT EXISTS idx_vps_container ON vps (container_id)
            """,
        ],
    },
]
