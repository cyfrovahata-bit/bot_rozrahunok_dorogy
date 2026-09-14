"""Кеш кілометражу в SQLite.

Маршрути повторюються (Київ→Львів рахують сотні разів), а запити до
Nominatim/Google коштують часу й грошей. Кеш зберігає результат назавжди —
дороги змінюються рідко; щоб скинути, видаліть рядок або таблицю geo_cache.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

from ..models import RouteInfo, RoutePoint
from .base import normalize_place

SCHEMA = """
CREATE TABLE IF NOT EXISTS geo_cache (
    cache_key  TEXT PRIMARY KEY,
    provider   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@dataclass
class CachedProvider:
    """Обгортка над будь-яким провайдером із кешуванням у SQLite."""

    inner: Any
    db_path: str | Path
    name: str = "cached"

    def __post_init__(self) -> None:
        self.name = f"cached:{getattr(self.inner, 'name', 'provider')}"
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path), timeout=10)

    def _key(self, points: Sequence[str]) -> str:
        inner_name = getattr(self.inner, "name", "provider")
        return inner_name + "|" + "|".join(normalize_place(p) for p in points)

    def route(self, points: Sequence[str]) -> RouteInfo:
        key = self._key(points)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM geo_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row:
            return _from_payload(json.loads(row[0]))

        info = self.inner.route(points)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO geo_cache (cache_key, provider, payload) "
                "VALUES (?, ?, ?)",
                (key, info.provider, json.dumps(info.as_dict(), ensure_ascii=False)),
            )
        return info


def _from_payload(data: dict[str, Any]) -> RouteInfo:
    return RouteInfo(
        distance_km=float(data["distance_km"]),
        duration_min=float(data["duration_min"]),
        provider=data["provider"],
        points=tuple(RoutePoint(**p) for p in data.get("points") or ()),
        is_estimate=bool(data.get("is_estimate")),
        note=data.get("note", ""),
        from_cache=True,
    )
