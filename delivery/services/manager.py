"""Підключення живого менеджера до розмови з клієнтом.

Логіка не залежить від Telegram: канал спілкування передається через
`Notifier` (у бота це надсилання повідомлень, у тестах — заглушка).

Сценарій:
  1. клієнт просить менеджера (кнопка, команда, або збій розрахунку);
  2. усім менеджерам летить картка заявки з кнопкою «Взяти»;
  3. перший, хто взяв, стає співрозмовником — решта бачать, що заявку взято;
  4. повідомлення клієнта і менеджера ретранслюються один одному;
  5. будь-хто з них завершує діалог — бот повертається до звичайного режиму.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..config import Settings
from ..models import Order
from ..storage import Handoff, Repository
from .statuses import StatusCatalog, StatusError, load_statuses, order_context


class Notifier(Protocol):
    """Канал доставки повідомлень (реалізується адаптером, напр. Telegram)."""

    def notify_managers(self, text: str, handoff_id: int) -> None: ...

    def send_to_client(self, client_ref: str, text: str) -> None: ...

    def send_to_manager(self, manager_ref: str, text: str) -> None: ...


@dataclass(frozen=True)
class HandoffResult:
    ok: bool
    handoff_id: int | None = None
    message: str = ""
    backlog: tuple[str, ...] = ()   # що клієнт написав, поки чекав


class ManagerService:
    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        notifier: Notifier | None = None,
        statuses: StatusCatalog | None = None,
    ):
        self.repo = repo
        self.settings = settings
        self.notifier = notifier
        self._statuses = statuses

    @property
    def statuses(self) -> StatusCatalog:
        """Шаблони повідомлень клієнту (config/statuses.yaml)."""
        if self._statuses is None:
            self._statuses = load_statuses(self.settings.path(self.settings.statuses_path))
        return self._statuses

    def reload_statuses(self) -> StatusCatalog:
        self._statuses = load_statuses(self.settings.path(self.settings.statuses_path))
        return self._statuses

    # ---------------- робочий час ----------------
    def is_working_hours(self, now: datetime | None = None) -> bool:
        now = now or self.settings.now()
        if (now.weekday() + 1) not in self.settings.work_days:
            return False
        return self.settings.work_hours_start <= now.hour < self.settings.work_hours_end

    def availability_note(self) -> str:
        if self.is_working_hours():
            return "Менеджер підключиться за кілька хвилин."
        days = "-".join(str(d) for d in (min(self.settings.work_days), max(self.settings.work_days)))
        return (
            "Зараз неробочий час. Заявку збережено — менеджер напише "
            f"у робочі години ({self.settings.work_hours_start}:00–"
            f"{self.settings.work_hours_end}:00, дні тижня {days})."
        )

    # ---------------- запит на підключення ----------------
    def request(
        self,
        session_id: str,
        channel: str,
        client_ref: str,
        client_name: str = "",
        client_phone: str = "",
        order: Order | None = None,
        reason: str = "",
    ) -> HandoffResult:
        existing = self.repo.active_handoff_for_client(str(client_ref))
        if existing:
            return HandoffResult(
                ok=True,
                handoff_id=existing["id"],
                message="Ви вже в черзі на розмову з менеджером."
                if existing["status"] == "requested"
                else "Менеджер уже на зв'язку.",
            )

        handoff = Handoff(
            id=None,
            session_id=session_id,
            channel=channel,
            client_ref=str(client_ref),
            client_name=client_name or (order.client_name if order else ""),
            client_phone=client_phone or (order.client_phone if order else ""),
            order_id=order.id if order else None,
            reason=reason,
        )
        handoff_id = self.repo.create_handoff(handoff)

        if self.notifier:
            self.notifier.notify_managers(self._card(handoff, order, reason), handoff_id)
        return HandoffResult(ok=True, handoff_id=handoff_id, message=self.availability_note())

    def _card(self, handoff: Handoff, order: Order | None, reason: str) -> str:
        lines = [
            "🔔 Клієнт просить менеджера",
            f"Ім'я: {handoff.client_name or '—'}",
            f"Телефон: {handoff.client_phone or '—'}",
        ]
        if reason:
            lines.append(f"Причина: {reason}")
        if order:
            lines.append("")
            lines.append(order.summary())
        return "\n".join(lines)

    # ---------------- робота менеджера ----------------
    def claim(self, handoff_id: int, manager_ref: str) -> HandoffResult:
        handoff = self.repo.get_handoff(handoff_id)
        if not handoff:
            return HandoffResult(ok=False, message="Заявку не знайдено.")
        if handoff["status"] == "closed":
            return HandoffResult(ok=False, message="Цю розмову вже завершено.")
        if not self.repo.claim_handoff(handoff_id, str(manager_ref)):
            taken_by = self.repo.get_handoff(handoff_id)["manager_ref"]
            if str(taken_by) == str(manager_ref):
                return HandoffResult(ok=True, handoff_id=handoff_id,
                                     message="Ви вже ведете цю розмову.")
            return HandoffResult(ok=False, message="Заявку вже взяв інший менеджер.")

        if handoff.get("order_id"):
            self.repo.assign_order(handoff["order_id"], str(manager_ref))

        backlog = tuple(
            m["text"] for m in self.repo.history(handoff_id) if m["sender"] == "client"
        )
        if self.notifier:
            self.notifier.send_to_client(
                handoff["client_ref"],
                "👤 Менеджер підключився до розмови. Пишіть — він відповість тут.",
            )
            self.notifier.send_to_manager(
                str(manager_ref),
                f"Ви на зв'язку з клієнтом {handoff['client_name'] or handoff['client_ref']}. "
                "Усі ваші повідомлення йдуть клієнту. /close — завершити.",
            )
        return HandoffResult(
            ok=True, handoff_id=handoff_id, message="Розмову розпочато.", backlog=backlog
        )

    def close(self, handoff_id: int, by: str = "manager") -> HandoffResult:
        handoff = self.repo.get_handoff(handoff_id)
        if not handoff:
            return HandoffResult(ok=False, message="Заявку не знайдено.")
        self.repo.close_handoff(handoff_id)
        if self.notifier:
            note = (
                "Розмову з менеджером завершено. Щоб порахувати ще одну доставку — /start"
                if by == "manager"
                else "Клієнт завершив розмову."
            )
            self.notifier.send_to_client(handoff["client_ref"], note)
            if handoff["manager_ref"]:
                self.notifier.send_to_manager(
                    handoff["manager_ref"], f"Розмову #{handoff_id} закрито."
                )
        return HandoffResult(ok=True, handoff_id=handoff_id, message="Розмову закрито.")

    # ---------------- ретрансляція ----------------
    def relay_from_client(self, client_ref: str, text: str) -> bool:
        """True = повідомлення переслано менеджеру (клієнт у режимі розмови)."""
        handoff = self.repo.active_handoff_for_client(str(client_ref))
        if not handoff:
            return False
        self.repo.log_message(handoff["id"], "client", text)
        if handoff["status"] != "active" or not handoff["manager_ref"]:
            return True   # ще чекаємо, поки хтось візьме; історія збережена
        if self.notifier:
            name = handoff["client_name"] or handoff["client_ref"]
            self.notifier.send_to_manager(handoff["manager_ref"], f"💬 {name}: {text}")
        return True

    def relay_from_manager(self, manager_ref: str, text: str) -> bool:
        handoff = self.repo.active_handoff_for_manager(str(manager_ref))
        if not handoff:
            return False
        self.repo.log_message(handoff["id"], "manager", text)
        if self.notifier:
            self.notifier.send_to_client(handoff["client_ref"], f"👤 Менеджер: {text}")
        return True

    # ---------------- менеджер починає розмову сам ----------------
    def open_conversation(self, order_id: int, manager_ref: str) -> HandoffResult:
        """Менеджер пише клієнту першим (авто виїхало, затримка тощо)."""
        order = self.repo.get_order(order_id)
        if not order:
            return HandoffResult(ok=False, message=f"Заявку №{order_id} не знайдено.")
        client_ref = order.get("external_user_id")
        if not client_ref:
            phone = (order.get("answers") or {}).get("client_phone") or "—"
            return HandoffResult(
                ok=False,
                message=f"Заявку №{order_id} створено не через Telegram — "
                f"написати в чат неможливо. Телефон клієнта: {phone}",
            )

        self.repo.assign_order(order_id, str(manager_ref))

        existing = self.repo.active_handoff_for_client(str(client_ref))
        if existing and str(existing.get("manager_ref") or "") == str(manager_ref):
            return HandoffResult(
                ok=True, handoff_id=existing["id"], message="Розмову вже відкрито."
            )
        if existing and existing["status"] == "active":
            return HandoffResult(
                ok=False,
                message="З цим клієнтом уже спілкується інший менеджер.",
            )

        # менеджер може вести лише одну розмову — закриваємо попередню
        previous = self.repo.active_handoff_for_manager(str(manager_ref))
        if previous:
            self.close(previous["id"], by="manager")

        answers = order.get("answers") or {}
        handoff = Handoff(
            id=existing["id"] if existing else None,
            session_id=order.get("session_id") or "—",
            channel=order.get("channel") or "telegram",
            client_ref=str(client_ref),
            client_name=str(answers.get("client_name") or ""),
            client_phone=str(answers.get("client_phone") or ""),
            order_id=order_id,
            reason="менеджер написав першим",
        )
        handoff_id = existing["id"] if existing else self.repo.create_handoff(handoff)
        self.repo.claim_handoff(handoff_id, str(manager_ref))
        return HandoffResult(
            ok=True,
            handoff_id=handoff_id,
            message=f"Розмову за заявкою №{order_id} відкрито. "
            "Ваші повідомлення йдуть клієнту, /close — завершити.",
        )

    def send_status(self, order_id: int, manager_ref: str, status_id: str) -> HandoffResult:
        """Надіслати клієнту готове повідомлення про хід замовлення."""
        order = self.repo.get_order(order_id)
        if not order:
            return HandoffResult(ok=False, message=f"Заявку №{order_id} не знайдено.")
        try:
            status = self.statuses.get(status_id)
        except StatusError as exc:
            return HandoffResult(ok=False, message=str(exc))

        client_ref = order.get("external_user_id")
        if not client_ref:
            phone = (order.get("answers") or {}).get("client_phone") or "—"
            return HandoffResult(
                ok=False,
                message=f"Клієнта немає в Telegram — зателефонуйте: {phone}",
            )

        text = status.render(order_context(order))
        self.repo.assign_order(order_id, str(manager_ref))
        if status.order_status:
            self.repo.set_order_status(order_id, status.order_status)

        handoff = self.repo.active_handoff_for_client(str(client_ref))
        if handoff:
            self.repo.log_message(handoff["id"], "manager", text)

        if self.notifier:
            self.notifier.send_to_client(str(client_ref), text)
        return HandoffResult(
            ok=True,
            handoff_id=handoff["id"] if handoff else None,
            message=f"Клієнту надіслано: {status.label}",
        )

    def my_orders(self, manager_ref: str, include_closed: bool = False) -> list[dict[str, Any]]:
        return self.repo.orders_for_manager(str(manager_ref), include_closed=include_closed)

    def hint_for_idle_manager(self, manager_ref: str) -> str | None:
        """Підказка менеджеру, який пише текст, не відкривши розмову.

        Саме тут губилися повідомлення: менеджер відповідав на картку заявки,
        не натиснувши «Взяти в роботу», і клієнт нічого не отримував.
        """
        if self.repo.active_handoff_for_manager(str(manager_ref)):
            return None
        queue = self.waiting()
        if queue:
            ids = ", ".join(f"#{item['id']}" for item in queue[:5])
            return (
                "⚠ Повідомлення НЕ надіслано клієнту — ви ще не взяли заявку в роботу.\n"
                f"У черзі: {ids}. Натисніть «Взяти в роботу» під карткою "
                "або надішліть /take <номер>."
            )
        if self.my_orders(manager_ref):
            return (
                "⚠ Повідомлення нікуди не пішло — активної розмови немає.\n"
                "Відкрийте замовлення через /my і натисніть «Написати клієнту»."
            )
        return None

    def waiting(self) -> list[dict[str, Any]]:
        """Заявки в черзі (для команди /queue у менеджера)."""
        with self.repo._connect() as conn:   # noqa: SLF001 - службовий доступ
            rows = conn.execute(
                "SELECT id, client_name, client_phone, created_at FROM handoffs"
                " WHERE status = 'requested' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]
