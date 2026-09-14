"""Моделі даних, спільні для всіх модулів."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any


# --------------------------------------------------------------------------
#  Маршрут
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RoutePoint:
    query: str                 # що ввів користувач
    display_name: str = ""     # як розпізнав сервіс
    lat: float | None = None
    lon: float | None = None


@dataclass(frozen=True)
class RouteInfo:
    """Результат розрахунку кілометражу."""

    distance_km: float
    duration_min: float
    provider: str
    points: tuple[RoutePoint, ...] = ()
    is_estimate: bool = False   # True = приблизно (офлайн-формула)
    note: str = ""
    from_cache: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["points"] = [asdict(p) for p in self.points]
        return d


# --------------------------------------------------------------------------
#  Розрахунок
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PricingInput:
    """Нормалізовані дані для калькулятора (не залежать від тексту анкети)."""

    distance_km: float
    weight_kg: float
    volume_m3: float = 0.0
    vehicle: str = "gazelle"
    cargo_type: str = "general"
    urgency: str = "standard"
    temperature_mode: str = "none"
    loaders: int = 0
    tail_lift: bool = False
    extra_stops: int = 0
    return_trip: bool = False
    declared_value: float = 0.0
    insurance: bool = False
    pickup_date: date | None = None
    vat: bool = False
    discount_percent: float = 0.0


@dataclass(frozen=True)
class LineItem:
    """Рядок у деталізації розрахунку."""

    code: str
    label: str
    amount: float            # внесок у суму, грн (може бути від'ємним)
    detail: str = ""

    def format(self, currency: str = "грн") -> str:
        sign = "+" if self.amount >= 0 else "−"
        body = f"{sign}{abs(self.amount):,.0f} {currency}".replace(",", " ")
        return f"{self.label}: {body}" + (f"  ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class Quote:
    """Готовий прорахунок."""

    total: float
    subtotal: float
    vat_amount: float
    currency: str
    distance_km: float
    chargeable_weight_kg: float
    items: tuple[LineItem, ...]
    warnings: tuple[str, ...] = ()
    tariff_version: str = ""
    route: RouteInfo | None = None
    created_at: datetime = field(default_factory=datetime.now)

    def as_text(self) -> str:
        """Деталізація для чату/консолі."""
        lines = [f"Відстань: {self.distance_km:,.0f} км".replace(",", " ")]
        if self.route and self.route.is_estimate:
            lines.append("(кілометраж приблизний — уточнить менеджер)")
        lines.append(f"Розрахункова вага: {self.chargeable_weight_kg:,.0f} кг".replace(",", " "))
        lines.append("")
        lines += [f"• {item.format(self.currency)}" for item in self.items]
        lines.append("")
        if self.vat_amount:
            lines.append(f"Без ПДВ: {self.subtotal:,.0f} {self.currency}".replace(",", " "))
            lines.append(f"ПДВ: {self.vat_amount:,.0f} {self.currency}".replace(",", " "))
        lines.append(f"РАЗОМ: {self.total:,.0f} {self.currency}".replace(",", " "))
        for w in self.warnings:
            lines.append(f"⚠ {w}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "subtotal": self.subtotal,
            "vat_amount": self.vat_amount,
            "currency": self.currency,
            "distance_km": self.distance_km,
            "chargeable_weight_kg": self.chargeable_weight_kg,
            "items": [asdict(i) for i in self.items],
            "warnings": list(self.warnings),
            "tariff_version": self.tariff_version,
            "route": self.route.as_dict() if self.route else None,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


# --------------------------------------------------------------------------
#  Заявка
# --------------------------------------------------------------------------
@dataclass
class Order:
    id: int | None
    session_id: str
    channel: str                      # cli | api | telegram
    external_user_id: str | None
    answers: dict[str, Any]
    quote: Quote | None = None
    status: str = "new"               # new | manager_requested | in_progress | done | cancelled
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def client_name(self) -> str:
        return str(self.answers.get("client_name") or "—")

    @property
    def client_phone(self) -> str:
        return str(self.answers.get("client_phone") or "—")

    def summary(self) -> str:
        a = self.answers
        route = f"{a.get('pickup_city', '?')} → {a.get('dropoff_city', '?')}"
        return (
            f"Заявка #{self.id or '—'}\n"
            f"Клієнт: {self.client_name}, {self.client_phone}\n"
            f"Маршрут: {route}\n"
            f"Вантаж: {a.get('weight_kg', '?')} кг / {a.get('volume_m3', 0)} м³\n"
            f"Авто: {a.get('vehicle_type', '?')}, терміновість: {a.get('urgency', '?')}\n"
            f"Дата подачі: {a.get('pickup_date', '?')}\n"
            + (f"Коментар: {a['comment']}\n" if a.get("comment") else "")
            + (f"\n{self.quote.as_text()}" if self.quote else "")
        )
