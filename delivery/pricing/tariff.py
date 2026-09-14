"""Завантаження і перевірка тарифів (config/tariffs.yaml)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class TariffError(Exception):
    """Помилка в тарифах або звернення до невідомого коефіцієнта."""


@dataclass(frozen=True)
class Tier:
    up_to_km: float | None     # None = «і далі»
    rate: float


@dataclass(frozen=True)
class Tariff:
    version: str
    currency: str
    base_fee: float
    min_price: float
    round_to: int
    distance_tiers: tuple[Tier, ...]
    coefficients: dict[str, dict[str, float]]
    vehicle_capacity: dict[str, dict[str, float]]
    surcharges: dict[str, float]
    volumetric_divisor: float
    vat_percent: float

    # -- доступ із зрозумілими помилками --------------------------------
    def coefficient(self, group: str, key: str, default: float | None = None) -> float:
        table = self.coefficients.get(group)
        if table is None:
            raise TariffError(f"У тарифах немає групи коефіцієнтів '{group}'.")
        if key in table:
            return float(table[key])
        if default is not None:
            return default
        raise TariffError(
            f"Невідоме значення '{key}' у групі '{group}'. "
            f"Доступні: {', '.join(sorted(table))}"
        )

    def surcharge(self, key: str, default: float = 0.0) -> float:
        return float(self.surcharges.get(key, default))

    def capacity(self, vehicle: str) -> dict[str, float]:
        return self.vehicle_capacity.get(vehicle, {})

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Tariff":
        try:
            tiers_raw = raw["distance_tiers"]
            tiers = tuple(
                Tier(
                    up_to_km=None if t.get("up_to_km") is None else float(t["up_to_km"]),
                    rate=float(t["rate"]),
                )
                for t in tiers_raw
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TariffError(f"Некоректний distance_tiers: {exc}") from exc

        if not tiers:
            raise TariffError("distance_tiers не може бути порожнім.")
        if tiers[-1].up_to_km is not None:
            raise TariffError(
                "Останній відрізок distance_tiers мусить мати up_to_km: null "
                "(тариф для будь-якої відстані понад попередній поріг)."
            )
        bounds = [t.up_to_km for t in tiers[:-1]]
        if any(b is None for b in bounds) or bounds != sorted(b for b in bounds):
            raise TariffError("Відрізки distance_tiers мають іти за зростанням up_to_km.")

        coefficients = {
            group: {str(k): float(v) for k, v in table.items()}
            for group, table in (raw.get("coefficients") or {}).items()
        }
        for group, table in coefficients.items():
            for key, value in table.items():
                if value <= 0:
                    raise TariffError(f"Коефіцієнт {group}.{key} мусить бути > 0 (зараз {value}).")

        return cls(
            version=str(raw.get("version") or "—"),
            currency=str(raw.get("currency") or "грн"),
            base_fee=float(raw.get("base_fee") or 0),
            min_price=float(raw.get("min_price") or 0),
            round_to=int(raw.get("round_to") or 1),
            distance_tiers=tiers,
            coefficients=coefficients,
            vehicle_capacity={
                str(k): {str(kk): float(vv) for kk, vv in v.items()}
                for k, v in (raw.get("vehicle_capacity") or {}).items()
            },
            surcharges={str(k): float(v) for k, v in (raw.get("surcharges") or {}).items()},
            volumetric_divisor=float(raw.get("volumetric_divisor_kg_per_m3") or 0),
            vat_percent=float(raw.get("vat_percent") or 0),
        )


def load_tariff(path: str | Path) -> Tariff:
    """Читає тарифи з .yaml або .json."""
    p = Path(path)
    if not p.exists():
        raise TariffError(f"Файл тарифів не знайдено: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        raw = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise TariffError("Для .yaml потрібен PyYAML (pip install PyYAML).") from exc
        raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise TariffError(f"Файл тарифів {p} порожній або некоректний.")
    return Tariff.from_raw(raw)
