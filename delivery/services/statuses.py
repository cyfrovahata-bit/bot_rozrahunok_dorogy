"""Шаблони повідомлень клієнту про хід замовлення (config/statuses.yaml)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

ORDER_STATUSES = {"new", "manager_requested", "in_progress", "done", "cancelled"}


class StatusError(Exception):
    """Помилка у файлі статусів або звернення до невідомого статусу."""


class _SafeDict(dict):
    """Відсутнє поле не ламає шаблон, а підставляє прочерк."""

    def __missing__(self, key: str) -> str:
        return "—"


@dataclass(frozen=True)
class Status:
    id: str
    label: str
    text: str
    order_status: str | None = None

    def render(self, context: dict[str, Any]) -> str:
        try:
            return self.text.format_map(_SafeDict(context)).strip()
        except (IndexError, ValueError) as exc:
            raise StatusError(
                f"Некоректний шаблон статусу '{self.id}': {exc}. "
                "Перевірте фігурні дужки в config/statuses.yaml."
            ) from exc

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Status":
        sid = str(raw.get("id") or "").strip()
        if not sid:
            raise StatusError(f"статус без 'id': {raw!r}")
        text = str(raw.get("text") or "").strip()
        if not text:
            raise StatusError(f"статус '{sid}' без тексту повідомлення")
        order_status = raw.get("order_status")
        if order_status and order_status not in ORDER_STATUSES:
            raise StatusError(
                f"статус '{sid}': невідомий order_status '{order_status}'. "
                f"Доступні: {', '.join(sorted(ORDER_STATUSES))}"
            )
        return cls(
            id=sid,
            label=str(raw.get("label") or sid),
            text=text,
            order_status=str(order_status) if order_status else None,
        )


@dataclass(frozen=True)
class StatusCatalog:
    statuses: tuple[Status, ...]

    def __iter__(self) -> Iterator[Status]:
        return iter(self.statuses)

    def __len__(self) -> int:
        return len(self.statuses)

    def get(self, status_id: str) -> Status:
        for status in self.statuses:
            if status.id == status_id:
                return status
        raise StatusError(
            f"Невідомий статус '{status_id}'. "
            f"Доступні: {', '.join(s.id for s in self.statuses)}"
        )

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "StatusCatalog":
        statuses = tuple(Status.from_raw(s) for s in raw.get("statuses") or ())
        if not statuses:
            raise StatusError("config/statuses.yaml не містить жодного статусу")
        seen: set[str] = set()
        for status in statuses:
            if status.id in seen:
                raise StatusError(f"дубльований id статусу: '{status.id}'")
            seen.add(status.id)
        return cls(statuses=statuses)


def load_statuses(path: str | Path) -> StatusCatalog:
    p = Path(path)
    if not p.exists():
        raise StatusError(f"Файл статусів не знайдено: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        raw = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise StatusError("Для .yaml потрібен PyYAML (pip install PyYAML).") from exc
        raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise StatusError(f"Файл статусів {p} порожній або некоректний.")
    return StatusCatalog.from_raw(raw)


def order_context(order: dict[str, Any]) -> dict[str, Any]:
    """Дані заявки → підстановки для шаблону."""
    answers = order.get("answers") or {}
    pickup = answers.get("pickup_city") or "—"
    dropoff = answers.get("dropoff_city") or "—"
    total = order.get("total")
    return {
        "order_id": order.get("id", "—"),
        "route": f"{pickup} → {dropoff}",
        "pickup_city": pickup,
        "dropoff_city": dropoff,
        "pickup_date": answers.get("pickup_date") or "—",
        "client_name": answers.get("client_name") or "—",
        "client_phone": answers.get("client_phone") or "—",
        "total": f"{total:,.0f} грн".replace(",", " ") if total else "—",
    }
