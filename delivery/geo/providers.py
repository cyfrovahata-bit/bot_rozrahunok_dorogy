"""Провайдери кілометражу: офлайн-оцінка, OSM (безкоштовно), Google."""

from __future__ import annotations

import difflib
import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from ..models import RouteInfo, RoutePoint
from .base import GeoError, normalize_place

CITIES_FILE = Path(__file__).with_name("cities.json")

#: Коефіцієнт «дорога проти прямої лінії» для офлайн-оцінки.
ROAD_FACTOR = 1.18
#: Середня технічна швидкість вантажівки, км/год.
AVG_SPEED_KMH = 62.0


# --------------------------------------------------------------------------
#  Офлайн: таблиця міст + формула гаверсинуса
# --------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _city_index() -> dict[str, tuple[str, float, float]]:
    data = json.loads(CITIES_FILE.read_text(encoding="utf-8"))
    index: dict[str, tuple[str, float, float]] = {}
    for city in data["cities"]:
        entry = (city["name"], float(city["lat"]), float(city["lon"]))
        index[normalize_place(city["name"])] = entry
        for alias in city.get("aliases", ()):
            index[normalize_place(alias)] = entry
    return index


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class OfflineProvider:
    """Оцінка без інтернету: пряма відстань між містами × коефіцієнт доріг.

    Похибка зазвичай 5–10%. Достатньо для попереднього прорахунку;
    точну цифру підтверджує менеджер або платний провайдер.
    """

    name = "offline"

    def resolve(self, place: str) -> tuple[str, float, float]:
        """Знаходить місто в довіднику, ігноруючи вулицю/будинок.

        Приймає «Київ», «м. Київ», «вул. Хрещатик, 22, Київ», «Kyiv».
        """
        index = _city_index()
        key = normalize_place(place)
        if key in index:
            return index[key]

        # частини через кому: місто зазвичай в кінці («вулиця, будинок, місто»),
        # але буває і на початку («Київ, вул. Хрещатик»)
        chunks = [normalize_place(c) for c in key.split(",") if c.strip()]
        for chunk in reversed(chunks):
            if chunk in index:
                return index[chunk]

        # n-грами слів: «івано-франківськ вул шевченка» → «івано-франківськ»
        for chunk in reversed(chunks) if chunks else [key]:
            tokens = chunk.split()
            for size in (3, 2, 1):
                for start in range(0, max(1, len(tokens) - size + 1)):
                    candidate = " ".join(tokens[start : start + size])
                    if candidate in index:
                        return index[candidate]

        close: list[str] = []
        for chunk in chunks or [key]:
            close += difflib.get_close_matches(chunk, list(index), n=2, cutoff=0.62)
        hint = ""
        if close:
            names = sorted({index[c][0] for c in close})
            hint = " Можливо, ви мали на увазі: " + ", ".join(names) + "?"
        raise GeoError(
            f"Не знайшов населений пункт «{place}» у довіднику офлайн-режиму.{hint}"
        )

    def route(self, points: Sequence[str]) -> RouteInfo:
        if len(points) < 2:
            raise GeoError("Потрібні щонайменше дві точки маршруту.")
        resolved = [self.resolve(p) for p in points]
        total = 0.0
        for (n1, la1, lo1), (n2, la2, lo2) in zip(resolved, resolved[1:]):
            total += haversine_km(la1, lo1, la2, lo2) * ROAD_FACTOR
        distance = round(total, 1)
        return RouteInfo(
            distance_km=distance,
            duration_min=round(distance / AVG_SPEED_KMH * 60, 0),
            provider=self.name,
            points=tuple(
                RoutePoint(query=q, display_name=n, lat=la, lon=lo)
                for q, (n, la, lo) in zip(points, resolved)
            ),
            is_estimate=True,
            note="Приблизна оцінка за довідником міст (без урахування реальних доріг).",
        )


# --------------------------------------------------------------------------
#  HTTP-помічник (стандартна бібліотека, без requests/httpx)
# --------------------------------------------------------------------------
def _get_json(url: str, timeout: float, user_agent: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # мережа, таймаут, некоректний JSON
        raise GeoError(f"Сервіс маршрутів недоступний: {exc}") from exc


# --------------------------------------------------------------------------
#  OSM: Nominatim (геокодинг) + OSRM (маршрут)
# --------------------------------------------------------------------------
@dataclass
class OsmProvider:
    """Безкоштовно, без ключа. Обмеження публічних серверів: ~1 запит/сек.

    Для продакшену краще підняти власний Nominatim/OSRM або взяти Google.
    """

    contact_email: str = ""
    timeout: float = 10.0
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    osrm_url: str = "https://router.project-osrm.org/route/v1/driving"
    min_interval: float = 1.1          # пауза між запитами до Nominatim
    name: str = "osm"

    _last_call: float = 0.0

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.monotonic()

    @property
    def _user_agent(self) -> str:
        contact = f" ({self.contact_email})" if self.contact_email else ""
        return f"delivery-calculator/1.0{contact}"

    def geocode(self, place: str) -> RoutePoint:
        self._throttle()
        query = urllib.parse.urlencode(
            {"q": place, "format": "json", "limit": 1, "accept-language": "uk"}
        )
        data = _get_json(f"{self.nominatim_url}?{query}", self.timeout, self._user_agent)
        if not data:
            raise GeoError(f"Не вдалося знайти адресу «{place}». Уточніть населений пункт.")
        item = data[0]
        return RoutePoint(
            query=place,
            display_name=item.get("display_name", place),
            lat=float(item["lat"]),
            lon=float(item["lon"]),
        )

    def route(self, points: Sequence[str]) -> RouteInfo:
        if len(points) < 2:
            raise GeoError("Потрібні щонайменше дві точки маршруту.")
        resolved = [self.geocode(p) for p in points]
        coords = ";".join(f"{p.lon},{p.lat}" for p in resolved)
        data = _get_json(
            f"{self.osrm_url}/{coords}?overview=false",
            self.timeout,
            self._user_agent,
        )
        if data.get("code") != "Ok" or not data.get("routes"):
            raise GeoError("Не вдалося побудувати маршрут між вказаними точками.")
        route = data["routes"][0]
        return RouteInfo(
            distance_km=round(route["distance"] / 1000, 1),
            duration_min=round(route["duration"] / 60, 0),
            provider=self.name,
            points=tuple(resolved),
            is_estimate=False,
        )


# --------------------------------------------------------------------------
#  Google Distance Matrix
# --------------------------------------------------------------------------
@dataclass
class GoogleProvider:
    """Найточніше (з урахуванням заторів), але платно і потрібен ключ."""

    api_key: str
    timeout: float = 10.0
    base_url: str = "https://maps.googleapis.com/maps/api/distancematrix/json"
    name: str = "google"

    def _leg(self, origin: str, destination: str) -> tuple[float, float]:
        query = urllib.parse.urlencode(
            {
                "origins": origin,
                "destinations": destination,
                "key": self.api_key,
                "language": "uk",
                "region": "ua",
                "mode": "driving",
            }
        )
        data = _get_json(f"{self.base_url}?{query}", self.timeout, "delivery-calculator/1.0")
        if data.get("status") != "OK":
            raise GeoError(
                f"Google Maps повернув помилку: {data.get('status')} "
                f"{data.get('error_message', '')}".strip()
            )
        element = data["rows"][0]["elements"][0]
        if element.get("status") != "OK":
            raise GeoError(
                f"Google не побудував маршрут «{origin}» → «{destination}» "
                f"({element.get('status')})."
            )
        return element["distance"]["value"] / 1000, element["duration"]["value"] / 60

    def route(self, points: Sequence[str]) -> RouteInfo:
        if len(points) < 2:
            raise GeoError("Потрібні щонайменше дві точки маршруту.")
        distance = duration = 0.0
        for origin, destination in zip(points, points[1:]):
            leg_km, leg_min = self._leg(origin, destination)
            distance += leg_km
            duration += leg_min
        return RouteInfo(
            distance_km=round(distance, 1),
            duration_min=round(duration, 0),
            provider=self.name,
            points=tuple(RoutePoint(query=p) for p in points),
            is_estimate=False,
        )


# --------------------------------------------------------------------------
#  Ланцюжок з резервом
# --------------------------------------------------------------------------
@dataclass
class ChainProvider:
    """Пробує провайдерів по черзі; якщо основний впав — бере наступний."""

    providers: Sequence[Any]
    name: str = "chain"

    def route(self, points: Sequence[str]) -> RouteInfo:
        errors: list[str] = []
        for provider in self.providers:
            try:
                info = provider.route(points)
            except GeoError as exc:
                errors.append(f"{getattr(provider, 'name', provider)}: {exc}")
                continue
            if errors:
                info = replace(
                    info,
                    note=(info.note + " Основний сервіс недоступний, "
                          "використано резервний.").strip(),
                )
            return info
        raise GeoError(" / ".join(errors) or "Жоден провайдер не відповів.")
