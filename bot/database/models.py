"""High-level data access for users, VPS instances, bans and audit logs.

All functions take the :class:`Database` instance and use only parameterized
queries. Rows are returned as ``sqlite3.Row`` objects (dict-like, indexable).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .database import Database

VPS_STATUSES = {
    "creating",
    "starting",
    "running",
    "stopping",
    "stopped",
    "reinstalling",
    "suspended",
    "error",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
def add_user(db: Database, user_id: int, username: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
        (user_id, username),
    )
    db.execute(
        "UPDATE users SET username = ? WHERE user_id = ? AND username != ?",
        (username, user_id, username),
    )


def get_user(db: Database, user_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return dict(row) if row else None


def count_users(db: Database) -> int:
    return db.scalar("SELECT COUNT(*) FROM users")


def get_last_create(db: Database, user_id: int) -> str | None:
    row = db.query_one("SELECT last_create_at FROM users WHERE user_id = ?", (user_id,))
    return row["last_create_at"] if row else None


def set_last_create(db: Database, user_id: int) -> None:
    db.execute(
        "UPDATE users SET last_create_at = ? WHERE user_id = ?",
        (_now(), user_id),
    )


# --------------------------------------------------------------------------
# VPS
# --------------------------------------------------------------------------
def add_vps(db: Database, *, user_id: int, name: str, os_key: str, image: str,
            hostname: str, ram: float, cpu: float, disk: float,
            status: str = "creating", container_id: str | None = None,
            container_name: str | None = None) -> str:
    vps_id = str(uuid.uuid4())
    now = _now()
    db.execute(
        """
        INSERT INTO vps (id, user_id, name, container_id, container_name, os, image,
                         hostname, ram, cpu, disk, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (vps_id, user_id, name, container_id, container_name, os_key, image,
         hostname, ram, cpu, disk, status, now, now),
    )
    return vps_id


def get_vps(db: Database, vps_id: str, user_id: int | None = None) -> dict | None:
    if user_id is None:
        row = db.query_one("SELECT * FROM vps WHERE id = ?", (vps_id,))
    else:
        row = db.query_one("SELECT * FROM vps WHERE id = ? AND user_id = ?", (vps_id, user_id))
    return dict(row) if row else None


def get_vps_by_container(db: Database, container_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM vps WHERE container_id = ?", (container_id,))
    return dict(row) if row else None


def get_user_vps(db: Database, user_id: int) -> list[dict]:
    rows = db.query("SELECT * FROM vps WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    return [dict(r) for r in rows]


def get_all_vps(db: Database, limit: int | None = None) -> list[dict]:
    sql = "SELECT v.*, u.username AS owner_name FROM vps v LEFT JOIN users u ON v.user_id = u.user_id ORDER BY v.created_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in db.query(sql)]


def count_vps(db: Database) -> int:
    return db.scalar("SELECT COUNT(*) FROM vps")


def count_running_vps(db: Database) -> int:
    return db.scalar("SELECT COUNT(*) FROM vps WHERE status = 'running'")


def count_user_vps(db: Database, user_id: int) -> int:
    return db.scalar("SELECT COUNT(*) FROM vps WHERE user_id = ?", (user_id,))


def update_vps_status(db: Database, vps_id: str, status: str) -> None:
    db.execute(
        "UPDATE vps SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), vps_id),
    )


def update_vps_ssh(db: Database, vps_id: str, ssh_command: str | None) -> None:
    db.execute(
        "UPDATE vps SET ssh_command = ?, updated_at = ? WHERE id = ?",
        (ssh_command, _now(), vps_id),
    )


def update_vps_suspended(db: Database, vps_id: str, suspended: bool) -> None:
    db.execute(
        "UPDATE vps SET suspended = ?, updated_at = ? WHERE id = ?",
        (1 if suspended else 0, _now(), vps_id),
    )


def update_vps_error(db: Database, vps_id: str, error: str | None) -> None:
    db.execute(
        "UPDATE vps SET error = ?, updated_at = ? WHERE id = ?",
        (error, _now(), vps_id),
    )


def update_vps_container(db: Database, vps_id: str, container_id: str, container_name: str) -> None:
    db.execute(
        "UPDATE vps SET container_id = ?, container_name = ?, updated_at = ? WHERE id = ?",
        (container_id, container_name, _now(), vps_id),
    )


def delete_vps(db: Database, vps_id: str) -> None:
    db.execute("DELETE FROM vps WHERE id = ?", (vps_id,))


def delete_user_vps(db: Database, user_id: int) -> list[dict]:
    vps_list = get_user_vps(db, user_id)
    for vps in vps_list:
        delete_vps(db, vps["id"])
    return vps_list


def allocated_totals(db: Database) -> dict:
    row = db.query_one(
        """
        SELECT COUNT(*) AS count,
               COALESCE(SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END), 0) AS running,
               COALESCE(SUM(ram), 0) AS ram,
               COALESCE(SUM(cpu), 0) AS cpu,
               COALESCE(SUM(disk), 0) AS disk
        FROM vps
        """
    )
    if not row:
        return {"count": 0, "running": 0, "ram": 0.0, "cpu": 0.0, "disk": 0.0}
    return dict(row)


def running_allocated(db: Database) -> dict:
    row = db.query_one(
        """
        SELECT COALESCE(SUM(ram), 0) AS ram,
               COALESCE(SUM(cpu), 0) AS cpu,
               COALESCE(SUM(disk), 0) AS disk
        FROM vps WHERE status = 'running'
        """
    )
    return dict(row) if row else {"ram": 0.0, "cpu": 0.0, "disk": 0.0}


# --------------------------------------------------------------------------
# Bans
# --------------------------------------------------------------------------
def add_ban(db: Database, user_id: int, reason: str = "", banned_by: int | None = None) -> None:
    db.execute(
        "INSERT OR IGNORE INTO bans (user_id, reason, banned_by) VALUES (?, ?, ?)",
        (user_id, reason, banned_by),
    )


def remove_ban(db: Database, user_id: int) -> None:
    db.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))


def is_banned(db: Database, user_id: int) -> bool:
    row = db.query_one("SELECT 1 FROM bans WHERE user_id = ?", (user_id,))
    return row is not None


def list_bans(db: Database) -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM bans ORDER BY created_at DESC")]


# --------------------------------------------------------------------------
# Audit logs
# --------------------------------------------------------------------------
def add_audit_log(db: Database, *, user_id: int | None, action: str, details: str = "") -> None:
    db.execute(
        "INSERT INTO audit_logs (user_id, action, details) VALUES (?, ?, ?)",
        (user_id, action, details),
    )


def recent_audit_logs(db: Database, limit: int = 50) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (int(limit),)
    )]


def prune_audit_logs(db: Database, keep_days: int = 90) -> int:
    return db.execute(
        "DELETE FROM audit_logs WHERE created_at < datetime('now', ?)",
        (f"-{int(keep_days)} days",),
    )
