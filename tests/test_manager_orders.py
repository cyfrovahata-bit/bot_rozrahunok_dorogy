"""Робота менеджера із замовленнями: свої заявки, статуси, розмова з клієнтом."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from delivery.config import Settings
from delivery.models import Order
from delivery.services.manager import ManagerService
from delivery.services.statuses import StatusError, load_statuses, order_context
from delivery.storage import Repository

STATUSES = "config/statuses.yaml"


class RecordingNotifier:
    def __init__(self):
        self.to_client: list[tuple[str, str]] = []
        self.to_manager: list[tuple[str, str]] = []
        self.cards: list[tuple[int, str]] = []

    def notify_managers(self, text, handoff_id):
        self.cards.append((handoff_id, text))

    def send_to_client(self, client_ref, text):
        self.to_client.append((str(client_ref), text))

    def send_to_manager(self, manager_ref, text):
        self.to_manager.append((str(manager_ref), text))


class StatusTemplateTest(unittest.TestCase):
    def setUp(self):
        self.catalog = load_statuses(STATUSES)

    def test_required_statuses_exist(self):
        ids = {s.id for s in self.catalog}
        for expected in ("departed", "delayed", "arrived", "delivered"):
            self.assertIn(expected, ids)

    def test_render_substitutes_order_data(self):
        order = {
            "id": 7,
            "total": 14300.0,
            "answers": {"pickup_city": "Київ", "dropoff_city": "Одеса"},
        }
        text = self.catalog.get("departed").render(order_context(order))
        self.assertIn("№7", text)
        self.assertIn("Київ → Одеса", text)

    def test_missing_data_does_not_break_template(self):
        text = self.catalog.get("accepted").render(order_context({"id": 1, "answers": {}}))
        self.assertIn("—", text)
        self.assertNotIn("{", text)

    def test_unknown_status_lists_available(self):
        with self.assertRaises(StatusError) as ctx:
            self.catalog.get("телепортація")
        self.assertIn("departed", str(ctx.exception))

    def test_every_template_renders_with_real_context(self):
        order = {"id": 1, "total": 100, "answers": {"pickup_city": "Київ",
                                                    "dropoff_city": "Львів"}}
        for status in self.catalog:
            rendered = status.render(order_context(order))
            self.assertTrue(rendered)
            self.assertNotIn("{", rendered)


class ManagerOrdersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = replace(
            Settings.load(), db_path=str(Path(self.tmp.name) / "t.sqlite3")
        )
        self.repo = Repository(self.settings.db_path)
        self.notifier = RecordingNotifier()
        self.manager = ManagerService(self.repo, self.settings, self.notifier)
        self.order_id = self._make_order(client_chat="555")

    def _make_order(self, client_chat: str | None) -> int:
        order = Order(
            id=None,
            session_id="s1",
            channel="telegram",
            external_user_id=client_chat,
            answers={
                "client_name": "Олег",
                "client_phone": "+380671234567",
                "pickup_city": "Київ",
                "dropoff_city": "Львів",
                "pickup_date": "2026-10-05",
            },
        )
        return self.repo.save_order(order)

    # ---------- статуси ----------
    def test_status_reaches_client_and_updates_order(self):
        result = self.manager.send_status(self.order_id, "900", "departed")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(self.notifier.to_client), 1)
        client_ref, text = self.notifier.to_client[0]
        self.assertEqual(client_ref, "555")
        self.assertIn("виїхало", text)

        stored = self.repo.get_order(self.order_id)
        self.assertEqual(stored["status"], "in_progress")
        self.assertEqual(stored["manager_ref"], "900")

    def test_delivered_closes_order(self):
        self.manager.send_status(self.order_id, "900", "delivered")
        self.assertEqual(self.repo.get_order(self.order_id)["status"], "done")
        self.assertEqual(self.manager.my_orders("900"), [])
        self.assertEqual(len(self.manager.my_orders("900", include_closed=True)), 1)

    def test_status_without_telegram_client_gives_phone(self):
        order_id = self._make_order(client_chat=None)
        result = self.manager.send_status(order_id, "900", "departed")
        self.assertFalse(result.ok)
        self.assertIn("+380671234567", result.message)
        self.assertEqual(self.notifier.to_client, [])

    def test_unknown_status_is_reported(self):
        result = self.manager.send_status(self.order_id, "900", "невідомо")
        self.assertFalse(result.ok)
        self.assertIn("Невідомий статус", result.message)

    # ---------- менеджер пише першим ----------
    def test_open_conversation_lets_manager_write_first(self):
        result = self.manager.open_conversation(self.order_id, "900")
        self.assertTrue(result.ok, result.message)
        self.assertTrue(self.manager.relay_from_manager("900", "Авто буде о 14:00"))
        self.assertEqual(self.notifier.to_client[-1][0], "555")
        self.assertIn("Авто буде о 14:00", self.notifier.to_client[-1][1])

    def test_opening_second_conversation_closes_the_first(self):
        other_order = self._make_order(client_chat="777")
        self.manager.open_conversation(self.order_id, "900")
        self.manager.open_conversation(other_order, "900")
        self.manager.relay_from_manager("900", "привіт")
        self.assertEqual(self.notifier.to_client[-1][0], "777")
        self.assertIsNone(self.repo.active_handoff_for_client("555"))

    def test_cannot_steal_client_from_another_manager(self):
        self.manager.open_conversation(self.order_id, "900")
        result = self.manager.open_conversation(self.order_id, "901")
        self.assertFalse(result.ok)
        self.assertIn("інший менеджер", result.message)

    def test_order_without_telegram_cannot_be_messaged(self):
        order_id = self._make_order(client_chat=None)
        result = self.manager.open_conversation(order_id, "900")
        self.assertFalse(result.ok)
        self.assertIn("+380671234567", result.message)

    # ---------- підказка проти втрачених повідомлень ----------
    def test_hint_when_request_not_claimed(self):
        """Саме той випадок: менеджер відповідає, не натиснувши «Взяти в роботу»."""
        self.manager.request(session_id="s1", channel="telegram", client_ref="555",
                             client_name="Олег")
        hint = self.manager.hint_for_idle_manager("900")
        self.assertIsNotNone(hint)
        self.assertIn("НЕ надіслано", hint)
        self.assertIn("#1", hint)

    def test_no_hint_during_active_conversation(self):
        handoff = self.manager.request(session_id="s1", channel="telegram", client_ref="555")
        self.manager.claim(handoff.handoff_id, "900")
        self.assertIsNone(self.manager.hint_for_idle_manager("900"))

    def test_hint_points_to_my_orders_when_queue_empty(self):
        self.repo.assign_order(self.order_id, "900")
        hint = self.manager.hint_for_idle_manager("900")
        self.assertIn("/my", hint)

    def test_no_hint_for_manager_without_any_work(self):
        self.assertIsNone(self.manager.hint_for_idle_manager("902"))

    # ---------- історія очікування ----------
    def test_claim_returns_what_client_wrote_while_waiting(self):
        handoff = self.manager.request(session_id="s1", channel="telegram", client_ref="555")
        self.manager.relay_from_client("555", "Чи можна раніше?")
        self.manager.relay_from_client("555", "Дуже терміново")
        result = self.manager.claim(handoff.handoff_id, "900")
        self.assertEqual(result.backlog, ("Чи можна раніше?", "Дуже терміново"))

    def test_claim_assigns_linked_order_to_manager(self):
        order = Order(id=self.order_id, session_id="s1", channel="telegram",
                      external_user_id="555", answers={})
        handoff = self.manager.request(
            session_id="s1", channel="telegram", client_ref="555", order=order
        )
        self.manager.claim(handoff.handoff_id, "900")
        self.assertEqual([o["id"] for o in self.manager.my_orders("900")], [self.order_id])


if __name__ == "__main__":
    unittest.main()
