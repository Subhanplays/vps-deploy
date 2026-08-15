"""Central, white-label configuration.

Everything the bot does is driven by this module:

1. ``bot/config/config.json``   - default configuration (edit freely, no code)
2. ``.env``                     - secrets + operational paths only
3. ``settings`` table in SQLite - runtime overrides written by /settings

Overrides are stored as JSON values keyed by their dotted path (e.g.
``appearance.primary_color``). ``Settings.get`` resolves overrides first and
falls back to the config file, so nothing ever needs to be hard-coded in
Python.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# A handful of operational values may still be provided via the environment.
# These are not branding; they are paths/secrets.
_ENV_MAPPING = {
    "DATABASE_FILE": "paths.database",
    "LOG_FILE": "paths.log_file",
    "LOG_LEVEL": "logging.level",
}

_ACTIVITY_TYPES = {
    "playing": "playing",
    "watching": "watching",
    "listening": "listening",
    "streaming": "streaming",
    "competing": "competing",
}

_STATUS_TYPES = {
    "online": "online",
    "idle": "idle",
    "dnd": "dnd",
    "do_not_disturb": "dnd",
    "invisible": "invisible",
    "offline": "invisible",
}


def _deep_get(node, dotted: str, default=None):
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def _deep_set(node, dotted: str, value):
    parts = dotted.split(".")
    cursor = node
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def _safe_format(template: str, **kwargs) -> str:
    """Format a template without crashing when placeholders are missing."""
    if not template:
        return template
    try:
        return template.format_map(_SafeDict(kwargs))
    except (ValueError, KeyError):
        return template


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


class Settings:
    """Read-only-ish configuration facade with runtime DB overrides."""

    def __init__(self, config_path: str | Path | None = None, env_path: str | Path | None = None, db=None):
        self.config_path = Path(config_path or CONFIG_PATH)
        load_dotenv(env_path)
        self._db = db
        self._data: dict = {}
        self._overrides: dict = {}
        self._lock = threading.RLock()
        self.load()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if self.config_path.exists():
                try:
                    self._data = json.loads(self.config_path.read_text("utf-8"))
                except (OSError, json.JSONDecodeError):
                    self._data = {}
            else:
                self._data = {}
            self._apply_env()
            self._load_overrides()

    def reload(self) -> None:
        self.load()

    def _apply_env(self) -> None:
        for env_key, cfg_key in _ENV_MAPPING.items():
            value = os.getenv(env_key)
            if value:
                _deep_set(self._data, cfg_key, value)

    def _load_overrides(self) -> None:
        self._overrides = {}
        if self._db is None:
            return
        try:
            rows = self._db.all_settings()
        except Exception:
            return
        for key, raw in rows:
            try:
                self._overrides[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                self._overrides[key] = raw

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------
    def get(self, dotted: str, default=None):
        with self._lock:
            if dotted in self._overrides:
                return self._overrides[dotted]
            value = _deep_get(self._data, dotted, default)
            prefix = dotted + "."
            related = {k[len(prefix):]: v for k, v in self._overrides.items() if k.startswith(prefix)}
            if not related:
                return value
            if isinstance(value, dict):
                merged = json.loads(json.dumps(value))
                for key, val in related.items():
                    _deep_set(merged, key, val)
                return merged
            if value is None:
                return related
            return value

    def get_str(self, dotted: str, default: str = "") -> str:
        value = self.get(dotted, default)
        return str(value) if value is not None else default

    def get_int(self, dotted: str, default: int = 0) -> int:
        try:
            return int(self.get(dotted, default))
        except (TypeError, ValueError):
            return int(default)

    def get_float(self, dotted: str, default: float = 0.0) -> float:
        try:
            return float(self.get(dotted, default))
        except (TypeError, ValueError):
            return float(default)

    def get_bool(self, dotted: str, default: bool = False) -> bool:
        value = self.get(dotted, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def get_list(self, dotted: str, default=None) -> list:
        value = self.get(dotted, default or [])
        if value is None:
            return []
        if isinstance(value, list):
            return list(value)
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return list(value)

    def text(self, key: str, default: str = "", **kwargs) -> str:
        template = self.get_str(f"text.{key}", default)
        return _safe_format(template, **kwargs)

    # ------------------------------------------------------------------
    # Setters (persist runtime overrides to SQLite)
    # ------------------------------------------------------------------
    def set(self, dotted: str, value) -> None:
        if self._db is None:
            return
        raw = json.dumps(value)
        self._db.set_setting(dotted, raw)
        with self._lock:
            self._overrides[dotted] = value

    def delete(self, dotted: str) -> None:
        if self._db is None:
            return
        self._db.delete_setting(dotted)
        with self._lock:
            self._overrides.pop(dotted, None)

    # ------------------------------------------------------------------
    # Derived / convenience helpers
    # ------------------------------------------------------------------
    def color(self, kind: str) -> int:
        hex_value = self.get_str(f"appearance.{kind}_color", "#5865F2").lstrip("#")
        try:
            return int(hex_value, 16)
        except ValueError:
            return 0x5865F2

    def status_activity(self) -> str:
        return _ACTIVITY_TYPES.get(self.get_str("bot.status_type", "watching").lower(), "watching")

    def status_type(self) -> str:
        return _STATUS_TYPES.get(self.get_str("bot.online_status", "online").lower(), "online")

    def footer(self) -> str:
        return self.get_str("branding.footer", "Powered by YourBrand")

    def watermark(self) -> str:
        return self.get_str("branding.watermark", "YourBrand VPS")

    def brand_name(self) -> str:
        return self.get_str("branding.name", "YourBrand")

    def admin_user_ids(self) -> set[int]:
        ids = {int(i) for i in self.get_list("access.admin_ids") if str(i).lstrip("-").isdigit()}
        for raw in os.getenv("ADMIN_USER_IDS", "").split(","):
            raw = raw.strip()
            if raw.lstrip("-").isdigit():
                ids.add(int(raw))
        return ids

    def admin_role_ids(self) -> set[int]:
        roles = {int(r) for r in self.get_list("access.admin_roles") if str(r).lstrip("-").isdigit()}
        for raw in os.getenv("ADMIN_ROLE_IDS", "").split(","):
            raw = raw.strip()
            if raw.lstrip("-").isdigit():
                roles.add(int(raw))
        return roles

    def is_admin(self, interaction) -> bool:
        user = getattr(interaction, "user", None)
        if user is None:
            return False
        if user.id in self.admin_user_ids():
            return True
        member = getattr(interaction, "user", None)
        if isinstance(member, _HasRoles) and member.guild:
            return any(role.id in self.admin_role_ids() for role in member.roles)
        return False

    def is_banned(self, user_id: int) -> bool:
        if self._db is None:
            return False
        from database.models import is_banned
        return is_banned(self._db, user_id)

    def image_map(self) -> dict:
        return self.get("docker.images", {})

    def image_by_key(self, os_key: str) -> dict | None:
        images = self.image_map()
        if os_key in images:
            return images[os_key]
        for key, image in images.items():
            if image.get("name", "").lower() == os_key.lower():
                return image
        return None

    def plan(self, plan_key: str) -> dict | None:
        plans = self.get("plans", {})
        if plan_key in plans:
            return plans[plan_key]
        for key, plan in plans.items():
            if plan.get("name", "").lower() == plan_key.lower():
                return plan
        return None

    def enabled_plans(self) -> dict:
        plans = self.get("plans", {})
        return {k: v for k, v in plans.items() if v.get("enabled", True)}


class _HasRoles:
    """Marker for duck-typing admin role checks (avoids importing discord here)."""

    @property
    def roles(self):
        return []


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def sanitize_name(name: str) -> str:
    """Lowercase, strip invalid characters, enforce a safe DNS-ish name."""
    cleaned = re.sub(r"[^a-z0-9-]", "", str(name).lower().strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if len(cleaned) > 63:
        cleaned = cleaned[:63]
    return cleaned


def is_valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))
