"""Бэкапы и восстановление.

Полный бэкап (раз в сутки и по кнопке в админке): база платформы + папки данных всех копий ботов.
Базы SQLite копируются через backup API - целостно, даже когда боты работают.
Восстановление всего: `python -m baksi restore <файл.zip>` на остановленной платформе.
Восстановление одной копии - из кабинета/админки (копия останавливается, данные заменяются, копия запускается).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from .config import Config
from .db import Database

log = logging.getLogger("baksi.backup")
SKIP_SUFFIXES = (".db-wal", ".db-shm", ".tmp", ".log", ".log.1")
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def _sqlite_snapshot(src: Path) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        dest = sqlite3.connect(tmp.name)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        return Path(tmp.name).read_bytes()


def _add_dir(zf: zipfile.ZipFile, root: Path, prefix: str) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(SKIP_SUFFIXES) or "backups" in path.relative_to(root).parts:
            continue
        arc = f"{prefix}/{path.relative_to(root).as_posix()}"
        if path.suffix in SQLITE_SUFFIXES:
            try:
                zf.writestr(arc, _sqlite_snapshot(path))
                continue
            except sqlite3.Error as exc:
                log.warning("Снимок %s не удался (%s), копирую файл", path, exc)
        zf.write(path, arc)


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    out = []
    for info in zf.infolist():
        p = PurePosixPath(info.filename)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Небезопасный путь в архиве: {info.filename}")
        out.append(info)
    return out


class Backups:
    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db

    async def full(self) -> Path:
        self.cfg.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        snapshot = self.cfg.backups_dir / f".db-{stamp}.tmp"
        await self.db.backup_to(snapshot)
        target = self.cfg.backups_dir / f"baksi-{stamp}.zip"

        def build() -> None:
            tmp = target.with_suffix(".zip.tmp")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                zf.write(snapshot, "baksi.db")
                if self.cfg.instances_dir.exists():
                    _add_dir(zf, self.cfg.instances_dir, "instances")
                zf.writestr("backup.json", json.dumps({"created_at": int(time.time()), "kind": "full"}))
            tmp.replace(target)
            snapshot.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(build)
        finally:
            snapshot.unlink(missing_ok=True)
        self.prune()
        log.info("Бэкап готов: %s (%.1f МБ)", target.name, target.stat().st_size / 1e6)
        return target

    def prune(self) -> None:
        cutoff = time.time() - self.cfg.backup_keep_days * 86400
        for path in self.cfg.backups_dir.glob("baksi-*.zip"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)

    def list(self) -> list[dict]:
        if not self.cfg.backups_dir.exists():
            return []
        return [{"name": p.name, "size": p.stat().st_size, "created_at": int(p.stat().st_mtime)}
                for p in sorted(self.cfg.backups_dir.glob("baksi-*.zip"), reverse=True)]

    def path(self, name: str) -> Path | None:
        p = self.cfg.backups_dir / name
        if p.parent != self.cfg.backups_dir or not p.name.startswith("baksi-") or not p.exists():
            return None
        return p

    # --- одна копия -----------------------------------------------------------
    async def instance(self, instance_id: int) -> bytes:
        root = self.cfg.instances_dir / str(instance_id)

        def build() -> bytes:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                if root.exists():
                    _add_dir(zf, root, "data")
                zf.writestr("backup.json", json.dumps({"created_at": int(time.time()), "kind": "instance",
                                                       "instance_id": instance_id}))
            return buf.getvalue()

        return await asyncio.to_thread(build)

    async def restore_instance(self, instance_id: int, blob: bytes) -> None:
        """Вызывать при остановленной копии."""
        root = self.cfg.instances_dir / str(instance_id)

        def apply() -> None:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                meta = json.loads(zf.read("backup.json"))
                if meta.get("kind") != "instance":
                    raise ValueError("Это не бэкап бота")
                members = [m for m in _safe_members(zf) if m.filename.startswith("data/") and not m.is_dir()]
                staging = root.with_name(f"{instance_id}.restore")
                shutil.rmtree(staging, ignore_errors=True)
                staging.mkdir(parents=True)
                for m in members:
                    dest = staging / m.filename[len("data/"):]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(m))
                platform_file = root / "platform.json"
                if platform_file.exists():  # актуальные данные платформы не откатываем
                    shutil.copy2(platform_file, staging / "platform.json")
                old = root.with_name(f"{instance_id}.old")
                shutil.rmtree(old, ignore_errors=True)
                if root.exists():
                    root.rename(old)
                staging.rename(root)
                shutil.rmtree(old, ignore_errors=True)

        await asyncio.to_thread(apply)

    def purge_instance_data(self, instance_id: int) -> None:
        shutil.rmtree(self.cfg.instances_dir / str(instance_id), ignore_errors=True)
        for suffix in (".log", ".log.1"):
            (self.cfg.logs_dir / f"{instance_id}{suffix}").unlink(missing_ok=True)


def restore_full(cfg: Config, archive: Path) -> None:
    """Восстановить всё из полного бэкапа. Платформа должна быть остановлена."""
    with zipfile.ZipFile(archive) as zf:
        meta = json.loads(zf.read("backup.json"))
        if meta.get("kind") != "full":
            raise SystemExit("Это не полный бэкап платформы")
        members = _safe_members(zf)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        if cfg.db_path.exists():
            cfg.db_path.rename(cfg.db_path.with_name(f"baksi.db.before-restore-{stamp}"))
        for suffix in ("-wal", "-shm"):
            Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)
        if cfg.instances_dir.exists():
            cfg.instances_dir.rename(cfg.instances_dir.with_name(f"instances.before-restore-{stamp}"))
        for m in members:
            if m.is_dir() or m.filename == "backup.json":
                continue
            if m.filename == "baksi.db":
                cfg.db_path.write_bytes(zf.read(m))
            elif m.filename.startswith("instances/"):
                dest = cfg.instances_dir / m.filename[len("instances/"):]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(m))
    print(f"Восстановлено из {archive.name}. Прежние данные сохранены с пометкой before-restore-{stamp}.")
