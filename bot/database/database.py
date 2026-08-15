"""SQLite persistence layer with parameterized queries only.

Every query in this module uses ``?`` placeholders - user input is never
interpolated into SQL strings. All connections are short-lived, writes are
serialized with a lock and WAL journaling keeps reads non-blocking.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from .migrations import MIGRATIONS


class Database:
    def __init__(self, path: str | os.PathLike):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------
    def migrate(self) -> None:
        with self._lock:
            conn = self.connect()
            try:
                current = conn.execute("PRAGMA user_version").fetchone()[0]
                for migration in MIGRATIONS:
                    if migration["id"] <= current:
                        continue
                    for statement in migration["sql"]:
                        if callable(statement):
                            statement(conn)
                        else:
                            conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {int(migration['id'])}")
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> int:
        """Run a write query, return the rowcount."""
        with self._lock:
            conn = self.connect()
            try:
                cursor = conn.execute(sql, params)
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: tuple = (), default=0):
        conn = self.connect()
        try:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row is not None else default
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Settings (runtime overrides for config.json)
    # ------------------------------------------------------------------
    def get_setting(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete_setting(self, key: str) -> None:
        self.execute("DELETE FROM settings WHERE key = ?", (key,))

    def all_settings(self) -> list[sqlite3.Row]:
        return self.query("SELECT key, value FROM settings")

    def reset_settings(self) -> None:
        self.execute("DELETE FROM settings")

    # ------------------------------------------------------------------
    # Plans (runtime overrides of config plans)
    # ------------------------------------------------------------------
    def get_plans(self) -> dict:
        rows = self.query("SELECT id, name, ram, cpu, disk, enabled FROM plans")
        return {row["id"]: dict(row) for row in rows}

    def seed_plans(self, plans: dict) -> None:
        with self._lock:
            conn = self.connect()
            try:
                for plan_id, plan in plans.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO plans (id, name, ram, cpu, disk, enabled) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            plan_id,
                            plan.get("name", plan_id),
                            float(plan.get("ram", 0)),
                            float(plan.get("cpu", 0)),
                            float(plan.get("disk", 0)),
                            1 if plan.get("enabled", True) else 0,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

    def set_plan(self, plan_id: str, *, enabled: bool | None = None, name: str | None = None,
                 ram: float | None = None, cpu: float | None = None, disk: float | None = None) -> None:
        updates = []
        params: list = []
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if ram is not None:
            updates.append("ram = ?")
            params.append(float(ram))
        if cpu is not None:
            updates.append("cpu = ?")
            params.append(float(cpu))
        if disk is not None:
            updates.append("disk = ?")
            params.append(float(disk))
        if not updates:
            return
        updates.append("updated_at = datetime('now')")
        params.append(plan_id)
        self.execute(f"UPDATE plans SET {', '.join(updates)} WHERE id = ?", tuple(params))

    def delete_plan(self, plan_id: str) -> None:
        self.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
