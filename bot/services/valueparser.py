"""Type-aware parsing for /settings values."""

from __future__ import annotations


class ValueParser:
    """Parse raw text into a typed value for a settings key."""

    def parse(self, kind: str, raw: str):
        raw = raw.strip()
        if kind == "str":
            return raw
        if kind == "url":
            return raw
        if kind == "int":
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError("must be a whole number") from exc
        if kind == "float":
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError("must be a number") from exc
        if kind == "bool":
            low = raw.lower()
            if low in ("1", "true", "yes", "on", "enabled"):
                return True
            if low in ("0", "false", "no", "off", "disabled"):
                return False
            raise ValueError("must be true or false")
        if kind == "color":
            value = raw if raw.startswith("#") else f"#{raw}"
            if len(value) != 7:
                raise ValueError("must be a hex color like #5865F2")
            try:
                int(value[1:], 16)
            except ValueError as exc:
                raise ValueError("must be a valid hex color") from exc
            return value
        if kind == "ids":
            ids = []
            for part in raw.replace(",", " ").split():
                part = part.strip()
                if part.lstrip("-").isdigit():
                    ids.append(int(part))
            return ids
        if kind == "templates":
            return [part.strip() for part in raw.split("|") if part.strip()]
        if kind.startswith("choice:"):
            choices = [c.strip() for c in kind.split(":", 1)[1].split(",")]
            if raw.lower() not in [c.lower() for c in choices]:
                raise ValueError(f"must be one of: {', '.join(choices)}")
            return raw.lower()
        return raw
