"""Спільний інтерфейс провайдерів кілометражу."""

from __future__ import annotations

import re
from typing import Protocol, Sequence, runtime_checkable

from ..models import RouteInfo


class GeoError(Exception):
    """Не вдалося визначити маршрут. Текст придатний для показу клієнту."""


def normalize_place(text: str) -> str:
    """«м. Київ, вул. Хрещатик 22» → «київ, вул. хрещатик 22» (для кешу/пошуку)."""
    value = str(text or "").strip().lower()
    value = value.replace("’", "'").replace("`", "'").replace("ʼ", "'")
    value = re.sub(r"^(м\.|м\s|місто|город|г\.)\s*", "", value)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip(" ,.")


@runtime_checkable
class DistanceProvider(Protocol):
    """Провайдер, який уміє порахувати маршрут між точками."""

    name: str

    def route(self, points: Sequence[str]) -> RouteInfo:
        """points — від 2 точок: [звідки, (проміжні...), куди]."""
        ...


def build_provider(settings) -> "DistanceProvider":
    """Збирає провайдера за налаштуваннями, з кешем і резервним варіантом.

    GEO_PROVIDER=google → Google, у разі помилки — GEO_FALLBACK.
    GEO_PROVIDER=osm    → Nominatim+OSRM, у разі помилки — GEO_FALLBACK.
    GEO_PROVIDER=offline→ лише офлайн-оцінка.
    """
    from .cache import CachedProvider
    from .providers import ChainProvider, GoogleProvider, OfflineProvider, OsmProvider

    def make(kind: str):
        kind = (kind or "").lower()
        if kind == "google":
            if not settings.google_maps_api_key:
                raise GeoError(
                    "GEO_PROVIDER=google, але GOOGLE_MAPS_API_KEY не заданий у .env"
                )
            return GoogleProvider(
                api_key=settings.google_maps_api_key,
                timeout=settings.geo_timeout_seconds,
            )
        if kind == "osm":
            return OsmProvider(
                contact_email=settings.nominatim_contact_email,
                timeout=settings.geo_timeout_seconds,
            )
        return OfflineProvider()

    primary = make(settings.geo_provider)
    chain = [primary]
    if settings.geo_fallback and settings.geo_fallback != settings.geo_provider:
        chain.append(make(settings.geo_fallback))

    provider = primary if len(chain) == 1 else ChainProvider(chain)
    return CachedProvider(provider, db_path=settings.path(settings.db_path))
