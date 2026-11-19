"""Настройки платформы из окружения (.env). Всё остальное меняется в админке и хранится в базе."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(";", ",").replace(" ", ",").split(",") if x.strip().lstrip("-").isdigit())


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else default


@dataclass(frozen=True, slots=True)
class Config:
    brand: str
    hub_token: str
    owner_ids: frozenset[int]
    secret_key: str
    data_dir: Path
    modules_dir: Path
    public_url: str          # https://домен — без него нет веб-входа и Mini App, бот работает
    web_host: str
    web_port: int
    cryptopay_token: str
    cryptopay_testnet: bool
    proxy_url: str           # socks5://… если сервер не достаёт до Telegram напрямую
    log_level: str
    instance_mem_mb: int     # лимит памяти одной копии бота по умолчанию (манифест может задать свой)
    max_instances: int       # защита от перегрузки сервера
    backup_keep_days: int
    purge_after_days: int    # сколько хранить данные истёкшей копии
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "baksi.db"

    @property
    def instances_dir(self) -> Path:
        return self.data_dir / "instances"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")


def load_config(env_file: Path | None = None) -> Config:
    _load_dotenv(env_file or ROOT / ".env")
    token = os.environ.get("HUB_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("HUB_BOT_TOKEN не задан: токен хаб-бота из @BotFather (см. .env.example)")
    owners = _ids(os.environ.get("OWNER_IDS", ""))
    if not owners:
        raise SystemExit("OWNER_IDS не задан: ваш Telegram ID (через запятую, если владельцев несколько)")
    secret = os.environ.get("SECRET_KEY", "").strip()
    if len(secret) < 32:
        raise SystemExit("SECRET_KEY не задан или короче 32 символов. Сгенерируйте: openssl rand -hex 32")
    data_dir = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve()
    return Config(
        brand=os.environ.get("BRAND", "Baksi Cloud").strip() or "Baksi Cloud",
        hub_token=token,
        owner_ids=owners,
        secret_key=secret,
        data_dir=data_dir,
        modules_dir=Path(os.environ.get("MODULES_DIR", str(ROOT / "modules"))).resolve(),
        public_url=os.environ.get("PUBLIC_URL", "").strip().rstrip("/"),
        web_host=os.environ.get("WEB_HOST", "0.0.0.0"),
        web_port=_int("WEB_PORT", 8080),
        cryptopay_token=os.environ.get("CRYPTOPAY_TOKEN", "").strip(),
        cryptopay_testnet=os.environ.get("CRYPTOPAY_TESTNET", "0").strip() == "1",
        proxy_url=os.environ.get("PROXY_URL", "").strip(),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        instance_mem_mb=_int("INSTANCE_MEM_MB", 160),
        max_instances=_int("MAX_INSTANCES", 300),
        backup_keep_days=_int("BACKUP_KEEP_DAYS", 7),
        purge_after_days=_int("PURGE_AFTER_DAYS", 30),
    )
