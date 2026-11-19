"""Контейнер сервисов платформы: всё, что нужно хаб-боту, вебу и фоновым задачам."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from aiogram import Bot

from .backup import Backups
from .billing import Billing
from .catalog import Catalog
from .config import Config
from .crypto import Vault
from .cryptopay import CryptoPay
from .db import Database
from .instances import Instances
from .notify import Notifier
from .supervisor import Supervisor


@dataclass
class App:
    cfg: Config
    db: Database
    vault: Vault
    catalog: Catalog
    billing: Billing
    sup: Supervisor
    instances: Instances
    notifier: Notifier
    pay: CryptoPay
    backups: Backups
    bot: Bot
    hub_username: str = ""
    started_at: float = field(default_factory=time.time)

    def is_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.cfg.owner_ids

    def web_url(self, path: str = "") -> str:
        return f"{self.cfg.public_url}{path}" if self.cfg.public_url else ""

    @property
    def miniapp_url(self) -> str:
        return self.web_url("/app") if self.cfg.public_url.startswith("https://") else ""
