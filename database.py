"""SQLite persistence layer.

Tables:
    users            - discord users known to the bot
    vps_instances    - every deployed KVM virtual machine
    deployment_jobs  - in-flight / historical deployment tracking
    host_settings    - runtime-adjustable host configuration (overrides .env)
    audit_logs       - security relevant audit trail
"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone

from config import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id   INTEGER PRIMARY KEY,
    username  TEXT NOT NULL,
    banned    INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vps_instances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    vps_id       TEXT UNIQUE NOT NULL,
    discord_user_id INTEGER NOT NULL,
    vm_name      TEXT UNIQUE NOT NULL,
    vm_uuid      TEXT NOT NULL,
    os           TEXT NOT NULL,
    cpu          INTEGER NOT NULL,
    ram          INTEGER NOT NULL,       -- GiB
    disk         INTEGER NOT NULL,       -- GiB (virtual size)
    status       TEXT DEFAULT 'stopped', -- running|stopped|deploying|error
    ip_address   TEXT,
    tmate_session TEXT,
    disk_path    TEXT,
    seed_path    TEXT,
    instance_dir TEXT,
    suspended    INTEGER DEFAULT 0,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (discord_user_id) REFERENCES users (user_id)
);

CREATE TABLE IF NOT EXISTS deployment_jobs (
    job_id     TEXT PRIMARY KEY,
    vps_id     TEXT,
    discord_user_id INTEGER,
    status     TEXT DEFAULT 'pending',   -- pending|running|done|failed
    message    TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS host_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_user_id INTEGER,
    action   TEXT NOT NULL,
    details  TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect():
    directory = os.path.dirname(config.database_file)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(config.database_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = _connect()
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


init_db()


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
def add_user(user_id, username):
    conn = _connect()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
        (user_id, str(username)[:64]),
    )
    conn.execute(
        "UPDATE users SET username = ? WHERE user_id = ?",
        (str(username)[:64], user_id),
    )
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = _connect()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def is_banned(user_id):
    conn = _connect()
    row = conn.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return bool(row and row["banned"])


def set_ban(user_id, banned):
    conn = _connect()
    conn.execute("UPDATE users SET banned = ? WHERE user_id = ?", (1 if banned else 0, user_id))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# VPS instances
# --------------------------------------------------------------------------
def _next_sequence():
    conn = _connect()
    row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS nid FROM vps_instances").fetchone()
    conn.close()
    return int(row["nid"])


def create_vps_record(discord_user_id, os_name, cpu, ram, disk):
    nid = _next_sequence()
    vps_id = f"VPS-{nid:04d}"
    vm_name = f"{config.vm_prefix}-{nid:04d}"
    conn = _connect()
    # Ensure the owning user exists so the FK constraint is satisfied even if
    # they never opened the panel first (e.g. admin-created deployments).
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
        (discord_user_id, str(discord_user_id)),
    )
    cur = conn.execute(
        """INSERT INTO vps_instances
           (vps_id, discord_user_id, vm_name, vm_uuid, os, cpu, ram, disk, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'deploying')""",
        (vps_id, discord_user_id, vm_name, str(uuid.uuid4()), os_name, cpu, ram, disk),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM vps_instances WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    conn.close()
    return dict(row)


def get_vps_by_vps_id(vps_id):
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM vps_instances WHERE vps_id = ?", (vps_id.upper(),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_vps_by_vm_name(vm_name):
    conn = _connect()
    row = conn.execute("SELECT * FROM vps_instances WHERE vm_name = ?", (vm_name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_user_vps(user_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM vps_instances WHERE discord_user_id = ? ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_vps():
    conn = _connect()
    rows = conn.execute("SELECT * FROM vps_instances ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_vps(vps_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    keys = ", ".join(f"{k} = ?" for k in fields)
    conn = _connect()
    conn.execute(
        f"UPDATE vps_instances SET {keys} WHERE vps_id = ?",
        (*fields.values(), vps_id.upper()),
    )
    conn.commit()
    conn.close()


def set_vps_status(vps_id, status):
    update_vps(vps_id, status=status)


def delete_vps_record(vps_id):
    conn = _connect()
    conn.execute("DELETE FROM vps_instances WHERE vps_id = ?", (vps_id.upper(),))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Resource accounting
# --------------------------------------------------------------------------
def allocated_ram():
    conn = _connect()
    row = conn.execute(
        "SELECT COALESCE(SUM(ram), 0) AS total FROM vps_instances WHERE status != 'error'"
    ).fetchone()
    conn.close()
    return float(row["total"])


def allocated_cpu():
    conn = _connect()
    row = conn.execute(
        "SELECT COALESCE(SUM(cpu), 0) AS total FROM vps_instances WHERE status != 'error'"
    ).fetchone()
    conn.close()
    return int(row["total"])


def allocated_disk():
    conn = _connect()
    row = conn.execute(
        "SELECT COALESCE(SUM(disk), 0) AS total FROM vps_instances WHERE status != 'error'"
    ).fetchone()
    conn.close()
    return int(row["total"])


def vps_counts():
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) AS c FROM vps_instances").fetchone()["c"]
    running = conn.execute(
        "SELECT COUNT(*) AS c FROM vps_instances WHERE status = 'running'"
    ).fetchone()["c"]
    stopped = conn.execute(
        "SELECT COUNT(*) AS c FROM vps_instances WHERE status = 'stopped'"
    ).fetchone()["c"]
    conn.close()
    return {"total": total, "running": running, "stopped": stopped}


# --------------------------------------------------------------------------
# Runtime host settings (admin adjustable, overrides config.py)
# --------------------------------------------------------------------------
def get_setting(key, default=None):
    conn = _connect()
    row = conn.execute("SELECT value FROM host_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    return row["value"]


def set_setting(key, value):
    conn = _connect()
    conn.execute(
        "INSERT INTO host_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_all_settings():
    conn = _connect()
    rows = conn.execute("SELECT key, value FROM host_settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def settings_bool(key, default):
    value = get_setting(key, "true" if default else "false")
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def settings_int(key, default):
    try:
        return int(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return int(default)


def settings_float(key, default):
    try:
        return float(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return float(default)


# --------------------------------------------------------------------------
# Deployment jobs
# --------------------------------------------------------------------------
def create_job(job_id, vps_id, user_id):
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO deployment_jobs (job_id, vps_id, discord_user_id, status, message) "
        "VALUES (?, ?, ?, 'pending', '')",
        (job_id, vps_id, user_id),
    )
    conn.commit()
    conn.close()


def update_job(job_id, status=None, message=None):
    conn = _connect()
    if status is not None:
        conn.execute("UPDATE deployment_jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                     (status, _now(), job_id))
    if message is not None:
        conn.execute("UPDATE deployment_jobs SET message = ?, updated_at = ? WHERE job_id = ?",
                     (message, _now(), job_id))
    conn.commit()
    conn.close()


def delete_job(job_id):
    conn = _connect()
    conn.execute("DELETE FROM deployment_jobs WHERE job_id = ?", (job_id,))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------
def log_audit(user_id, action, details=""):
    conn = _connect()
    conn.execute(
        "INSERT INTO audit_logs (discord_user_id, action, details) VALUES (?, ?, ?)",
        (user_id, action, str(details)[:2000]),
    )
    conn.commit()
    conn.close()


def recent_audit(limit=25):
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
