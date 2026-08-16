"""Dependency container - wires configuration, database and services together.

A single :class:`AppContext` is created in ``main.py`` and shared with every
command cog and view. No module imports another only to grab globals.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from config.settings import Settings
from database.database import Database
from lxd.manager import LxdManager
from lxd.stats import LxdStatsService
from services.cleanup import CleanupService
from services.logging import AuditLogger
from services.status import StatusService
from services.valueparser import ValueParser
from ui.embeds import EmbedBuilder
from ui.renderer import ViewRenderer
from vps.manager import VPSManager
from vps.resources import ResourceValidator
from vps.ssh import SSHManager


class AppContext:
    def __init__(self):
        load_dotenv()

        database_file = os.getenv("DATABASE_FILE", "data/vps.db")

        self.db = Database(database_file)
        self.db.migrate()

        self.settings = Settings(db=self.db)

        self.value_parser = ValueParser()

        self.version = "2.0.0"

        self.lxd = LxdManager(self)
        self.stats = LxdStatsService(self)
        self.resources = ResourceValidator(self)
        self.ssh = SSHManager(self)
        self.audit = AuditLogger(self)
        self.vps = VPSManager(self)

        seeded_plans = {}
        for key, plan in self.settings.get("plans", {}).items():
            seeded_plans[key] = {
                **plan,
                "ram": self.resources.parse_size(plan.get("ram", 0)),
                "cpu": self.resources.parse_cpu(plan.get("cpu", 0)),
                "disk": self.resources.parse_size(plan.get("disk", 0)),
            }
        self.db.seed_plans(seeded_plans)

        self.embeds = EmbedBuilder(self.settings)
        self.views = ViewRenderer(self)

        self.status = StatusService(self)
        self.cleanup = CleanupService(self)

        self.bot = None