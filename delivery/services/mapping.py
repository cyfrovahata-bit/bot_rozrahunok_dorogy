"""Міст між анкетою і калькулятором.

Анкету можна переписати (інші тексти, інший порядок) — достатньо, щоб
збереглися id питань, перелічені тут. Це єдине місце, де вони згадуються.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..models import PricingInput


def _num(answers: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = answers.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _flag(answers: dict[str, Any], key: str) -> bool:
    return bool(answers.get(key))


def route_points(answers: dict[str, Any]) -> list[str]:
    """Точки маршруту для гео-провайдера: «вулиця, місто» якщо адреса є."""

    def place(city_key: str, address_key: str) -> str:
        city = str(answers.get(city_key) or "").strip()
        address = str(answers.get(address_key) or "").strip()
        return f"{address}, {city}" if address and city else (city or address)

    points = [place("pickup_city", "pickup_address"), place("dropoff_city", "dropoff_address")]
    return [p for p in points if p]


def answers_to_pricing_input(answers: dict[str, Any], distance_km: float) -> PricingInput:
    pickup_date: date | None = None
    raw_date = answers.get("pickup_date")
    if isinstance(raw_date, date):
        pickup_date = raw_date
    elif raw_date:
        try:
            pickup_date = date.fromisoformat(str(raw_date))
        except ValueError:
            pickup_date = None

    return PricingInput(
        distance_km=distance_km,
        weight_kg=_num(answers, "weight_kg"),
        volume_m3=_num(answers, "volume_m3"),
        vehicle=str(answers.get("vehicle_type") or "gazelle"),
        cargo_type=str(answers.get("cargo_type") or "general"),
        urgency=str(answers.get("urgency") or "standard"),
        temperature_mode=str(answers.get("temperature_mode") or "none"),
        loaders=int(_num(answers, "loaders")),
        tail_lift=_flag(answers, "tail_lift"),
        extra_stops=int(_num(answers, "extra_stops")),
        return_trip=_flag(answers, "return_trip"),
        declared_value=_num(answers, "declared_value"),
        insurance=_flag(answers, "insurance"),
        pickup_date=pickup_date,
        vat=_flag(answers, "vat_invoice"),
        discount_percent=_num(answers, "discount_percent"),
    )
