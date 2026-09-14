"""Розрахунок вартості доставки за коефіцієнтами.

Порядок:
  1. подача + прогресивний тариф за км
  2. × коефіцієнти (авто × тип вантажу × терміновість × температура × вихідний)
  3. + надбавка за перевищення вантажопідйомності
  4. + додаткові послуги (вантажники, гідроборт, зайві точки, зворотний рейс)
  5. + страхування
  6. + паливний збір, − знижка
  7. мінімальна вартість замовлення
  8. + ПДВ
Кожен крок потрапляє окремим рядком у деталізацію (Quote.items).
"""

from __future__ import annotations

import math
from datetime import date

from ..models import LineItem, PricingInput, Quote, RouteInfo
from .tariff import Tariff

WEEKEND_DAYS = {5, 6}   # субота, неділя


def distance_cost(distance_km: float, tariff: Tariff) -> tuple[float, str]:
    """Прогресивний тариф: кожен відрізок шляху за своєю ставкою."""
    remaining = max(0.0, distance_km)
    previous = 0.0
    total = 0.0
    parts: list[str] = []
    for tier in tariff.distance_tiers:
        if remaining <= 0:
            break
        upper = tier.up_to_km if tier.up_to_km is not None else math.inf
        segment = min(distance_km, upper) - previous
        segment = max(0.0, min(segment, remaining))
        if segment > 0:
            total += segment * tier.rate
            parts.append(f"{segment:.0f} км × {tier.rate:g}")
            remaining -= segment
        previous = upper
    return total, "; ".join(parts)


def chargeable_weight(weight_kg: float, volume_m3: float, tariff: Tariff) -> float:
    """До оплати береться більша з ваг: фактична або об'ємна."""
    volumetric = (volume_m3 or 0) * tariff.volumetric_divisor
    return max(weight_kg or 0, volumetric)


def _is_weekend(day: date | None) -> bool:
    return day is not None and day.weekday() in WEEKEND_DAYS


def calculate(
    data: PricingInput,
    tariff: Tariff,
    route: RouteInfo | None = None,
) -> Quote:
    items: list[LineItem] = []
    warnings: list[str] = []

    # --- 1. транспортна складова ---------------------------------------
    items.append(LineItem("base_fee", "Подача авто", tariff.base_fee))
    km_cost, km_detail = distance_cost(data.distance_km, tariff)
    items.append(
        LineItem("distance", f"Перевезення {data.distance_km:.0f} км", km_cost, km_detail)
    )
    transport = tariff.base_fee + km_cost

    # --- 2. коефіцієнти -------------------------------------------------
    factors: list[tuple[str, str, float]] = [
        ("vehicle", "Тип транспорту", tariff.coefficient("vehicle", data.vehicle)),
        ("cargo_type", "Тип вантажу", tariff.coefficient("cargo_type", data.cargo_type)),
        ("urgency", "Терміновість", tariff.coefficient("urgency", data.urgency)),
        (
            "temperature_mode",
            "Температурний режим",
            tariff.coefficient("temperature_mode", data.temperature_mode, default=1.0),
        ),
    ]
    if _is_weekend(data.pickup_date):
        factors.append(
            ("weekend", "Вихідний день", tariff.surcharge("weekend_coefficient", 1.0))
        )

    running = transport
    for code, label, coefficient in factors:
        if abs(coefficient - 1.0) < 1e-9:
            continue
        delta = running * (coefficient - 1.0)
        items.append(LineItem(f"coef_{code}", label, delta, f"×{coefficient:g}"))
        running += delta
    transport_total = running

    # --- 3. вага понад ліміт авто ---------------------------------------
    weight = chargeable_weight(data.weight_kg, data.volume_m3, tariff)
    capacity = tariff.capacity(data.vehicle)
    limit = capacity.get("weight_kg")
    if limit:
        if weight > limit:
            tons_over = math.ceil((weight - limit) / 1000)
            percent = tariff.surcharge("overweight_percent_per_ton") * tons_over
            surcharge = transport_total * percent / 100
            items.append(
                LineItem(
                    "overweight",
                    "Перевищення вантажопідйомності",
                    surcharge,
                    f"+{percent:g}% за {tons_over} т понад {limit:.0f} кг",
                )
            )
            transport_total += surcharge
            warnings.append(
                f"Вага {weight:.0f} кг перевищує ліміт обраного авто ({limit:.0f} кг) — "
                "менеджер підбере більший транспорт, ціна може змінитися."
            )
        elif weight > limit * 0.9:
            warnings.append("Вага близька до межі вантажопідйомності авто.")
    volume_limit = capacity.get("volume_m3")
    if volume_limit and data.volume_m3 > volume_limit:
        warnings.append(
            f"Об'єм {data.volume_m3:g} м³ більший за кузов обраного авто "
            f"({volume_limit:g} м³) — потрібен більший транспорт."
        )

    # --- 4. додаткові послуги -------------------------------------------
    if data.loaders:
        rate = tariff.surcharge("loader_per_person")
        items.append(
            LineItem("loaders", "Вантажники", rate * data.loaders,
                     f"{data.loaders} × {rate:g}")
        )
    if data.tail_lift:
        items.append(LineItem("tail_lift", "Гідроборт", tariff.surcharge("tail_lift")))
    if data.extra_stops:
        rate = tariff.surcharge("extra_stop")
        items.append(
            LineItem("extra_stops", "Додаткові точки", rate * data.extra_stops,
                     f"{data.extra_stops} × {rate:g}")
        )
    if data.return_trip:
        ratio = tariff.surcharge("return_trip_ratio")
        vehicle_coefficient = tariff.coefficient("vehicle", data.vehicle)
        amount = km_cost * ratio * vehicle_coefficient
        items.append(
            LineItem("return_trip", "Зворотний рейс", amount, f"{ratio * 100:g}% тарифу за км")
        )

    # --- 5. страхування --------------------------------------------------
    if data.insurance:
        percent = tariff.surcharge("insurance_percent")
        amount = max(
            (data.declared_value or 0) * percent / 100,
            tariff.surcharge("insurance_min"),
        )
        items.append(
            LineItem("insurance", "Страхування вантажу", amount, f"{percent:g}% від вартості")
        )
        if not data.declared_value:
            warnings.append(
                "Оголошену вартість вантажу не вказано — страхування пораховано "
                "за мінімальним тарифом."
            )

    # --- 6. паливний збір і знижка ---------------------------------------
    subtotal = sum(i.amount for i in items)
    fuel_percent = tariff.surcharge("fuel_surcharge_percent")
    if fuel_percent:
        amount = subtotal * fuel_percent / 100
        items.append(LineItem("fuel", "Паливний збір", amount, f"{fuel_percent:g}%"))
        subtotal += amount
    if data.discount_percent:
        amount = -subtotal * data.discount_percent / 100
        items.append(LineItem("discount", "Знижка", amount, f"{data.discount_percent:g}%"))
        subtotal += amount

    # --- 7. мінімальна вартість ------------------------------------------
    if subtotal < tariff.min_price:
        items.append(
            LineItem(
                "min_price",
                "Добір до мінімального замовлення",
                tariff.min_price - subtotal,
                f"мінімум {tariff.min_price:g} {tariff.currency}",
            )
        )
        subtotal = tariff.min_price

    rounded = _round(subtotal, tariff.round_to)
    if abs(rounded - subtotal) > 0.004:
        items.append(
            LineItem("rounding", "Округлення", rounded - subtotal,
                     f"до {tariff.round_to} {tariff.currency}")
        )
    subtotal = rounded

    # --- 8. ПДВ ------------------------------------------------------------
    vat_amount = _round(subtotal * tariff.vat_percent / 100, tariff.round_to) if data.vat else 0.0
    if vat_amount:
        items.append(LineItem("vat", f"ПДВ {tariff.vat_percent:g}%", vat_amount))

    if route and route.is_estimate:
        warnings.append(
            "Кілометраж визначено приблизно — фінальну цифру підтвердить менеджер."
        )

    return Quote(
        total=subtotal + vat_amount,
        subtotal=subtotal,
        vat_amount=vat_amount,
        currency=tariff.currency,
        distance_km=data.distance_km,
        chargeable_weight_kg=weight,
        items=tuple(items),
        warnings=tuple(dict.fromkeys(warnings)),
        tariff_version=tariff.version,
        route=route,
    )


def _round(value: float, step: int) -> float:
    if step and step > 1:
        return float(math.ceil(value / step) * step)
    return round(value, 2)
