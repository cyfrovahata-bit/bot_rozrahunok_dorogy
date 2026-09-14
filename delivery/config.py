"""Налаштування застосунку: змінні оточення + .env."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("delivery.config")

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: str | Path = ".env") -> None:
    """Простий читач .env без зовнішніх залежностей.

    Значення з оточення мають пріоритет над файлом.
    """
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def railway_volume_path() -> str:
    """Шлях до змонтованого тому Railway (якщо він є).

    Railway задає RAILWAY_VOLUME_MOUNT_PATH автоматично, коли до сервісу
    прикріплено Volume. Без тому файлова система контейнера ефемерна:
    після кожного деплою вона стирається разом із базою заявок.
    """
    return (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()


def on_railway() -> bool:
    return bool(os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("RAILWAY_ENVIRONMENT"))


def _default_db_path() -> str:
    """На Railway кладемо базу на том, локально — у ./data."""
    volume = railway_volume_path()
    return f"{volume.rstrip('/')}/delivery.sqlite3" if volume else "data/delivery.sqlite3"


def _ids(name: str) -> tuple[int, ...]:
    raw = os.getenv(name, "") or ""
    out = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            try:
                out.append(int(chunk))
            except ValueError:
                continue
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    # Telegram
    telegram_bot_token: str = ""
    manager_chat_ids: tuple[int, ...] = ()
    orders_chat_id: int | None = None

    # Гео
    geo_provider: str = "offline"          # offline | osm | google
    geo_fallback: str = "offline"
    google_maps_api_key: str = ""
    nominatim_contact_email: str = ""
    geo_timeout_seconds: int = 10

    # Файли
    db_path: str = "data/delivery.sqlite3"
    tariffs_path: str = "config/tariffs.yaml"
    questions_path: str = "config/questions.yaml"
    statuses_path: str = "config/statuses.yaml"

    # Робочий час менеджерів
    timezone: str = "Europe/Kyiv"
    work_hours_start: int = 9
    work_hours_end: int = 19
    work_days: tuple[int, ...] = (1, 2, 3, 4, 5)   # 1 = понеділок

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_token: str = ""

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Settings":
        load_dotenv(env_file)
        days = _ids("WORK_DAYS") or (1, 2, 3, 4, 5)
        orders_chat = os.getenv("ORDERS_CHAT_ID", "").strip()
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            manager_chat_ids=_ids("MANAGER_CHAT_IDS"),
            orders_chat_id=int(orders_chat) if orders_chat.lstrip("-").isdigit() else None,
            geo_provider=(os.getenv("GEO_PROVIDER") or "offline").strip().lower(),
            geo_fallback=(os.getenv("GEO_FALLBACK") or "offline").strip().lower(),
            google_maps_api_key=os.getenv("GOOGLE_MAPS_API_KEY", "").strip(),
            nominatim_contact_email=os.getenv("NOMINATIM_CONTACT_EMAIL", "").strip(),
            geo_timeout_seconds=_int("GEO_TIMEOUT_SECONDS", 10),
            db_path=os.getenv("DB_PATH") or _default_db_path(),
            tariffs_path=os.getenv("TARIFFS_PATH") or "config/tariffs.yaml",
            questions_path=os.getenv("QUESTIONS_PATH") or "config/questions.yaml",
            statuses_path=os.getenv("STATUSES_PATH") or "config/statuses.yaml",
            timezone=(os.getenv("TIMEZONE") or os.getenv("TZ") or "Europe/Kyiv").strip(),
            work_hours_start=_int("WORK_HOURS_START", 9),
            work_hours_end=_int("WORK_HOURS_END", 19),
            work_days=days,
            api_host=os.getenv("API_HOST") or "0.0.0.0",
            api_port=_int("API_PORT", 8000),
            api_token=os.getenv("API_TOKEN", "").strip(),
        )

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else ROOT / p

    def now(self) -> datetime:
        """Поточний час у часовому поясі бізнесу, а не контейнера.

        Сервери (зокрема Railway) живуть за UTC, тому datetime.now() дав би
        зсув на 2-3 години — робочі години менеджерів рахувалися б неправильно.
        """
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(self.timezone))
        except Exception as exc:   # немає бази часових поясів або хибна назва
            log.warning(
                "Не вдалося застосувати часовий пояс '%s' (%s) — "
                "використано час сервера.",
                self.timezone,
                exc,
            )
            return datetime.now()

    def storage_warnings(self) -> list[str]:
        """Попередження про ризик втрати даних (насамперед на Railway).

        Порожній список = база лежить у надійному місці.
        """
        if not on_railway():
            return []

        volume = railway_volume_path()
        if not volume:
            return [
                "На Railway не прикріплено Volume: файлова система контейнера "
                "стирається при КОЖНОМУ деплої, тож заявки й листування будуть "
                "втрачені. Додайте том у налаштуваннях сервісу "
                "(Settings → Volumes, mount path /data) — застосунок підхопить "
                "його автоматично."
            ]

        db = self.path(self.db_path).resolve()
        mount = Path(volume).resolve()
        if mount not in db.parents and db != mount:
            return [
                f"База лежить за межами тому: DB_PATH={db}, том змонтовано в "
                f"{mount}. Після деплою дані зникнуть. Виправлення: "
                f"DB_PATH={mount}/delivery.sqlite3"
            ]
        return []
