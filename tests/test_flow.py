"""Наскрізний сценарій: анкета → кілометраж → ціна → заявка → менеджер."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from delivery.config import Settings
from delivery.services.manager import ManagerService
from delivery.services.mapping import answers_to_pricing_input, route_points
from delivery.services.order_service import OrderService

ANSWERS = [
    "Олег", "0671234567", "Фізична особа",                       # блок 1
    "Київ", "вул. Хрещатик, 22", "Львів", "-", "1", "ні",        # блок 2
    "Звичайний (генеральний)", "800", "4", "3", "-",             # блок 3
    "Газель (до 1.5 т, 9 м³)", "Не потрібен", "1", "ні",
    "Наступного дня",                                            # блок 4
    "завтра", "Готівка", "ні", "-", "так",                       # блок 5
]


class RecordingNotifier:
    def __init__(self):
        self.managers, self.clients = [], []

    def notify_managers(self, text, handoff_id):
        self.managers.append((handoff_id, text))

    def send_to_client(self, client_ref, text):
        self.clients.append((client_ref, text))

    def send_to_manager(self, manager_ref, text):
        self.managers.append((manager_ref, text))


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = replace(
            Settings.load(),
            db_path=str(Path(self.tmp.name) / "test.sqlite3"),
            geo_provider="offline",
            geo_fallback="offline",
        )
        self.service = OrderService(self.settings)

    def _complete_session(self):
        session, question = self.service.start(channel="api", user_id="u1")
        for value in ANSWERS:
            if self.service.engine.current(session) is None:
                break
            result = self.service.answer(session, value)
            self.assertTrue(result.ok, f"{value}: {result.error}")
        return session

    def test_full_flow_produces_quote_and_order(self):
        session = self._complete_session()
        self.assertTrue(session.is_complete)

        result = self.service.quote(session)
        self.assertTrue(result.ok, result.error)
        self.assertGreater(result.quote.total, 0)
        self.assertGreater(result.quote.distance_km, 400)
        self.assertIsNotNone(result.order.id)

        stored = self.service.repo.get_order(result.order.id)
        self.assertEqual(stored["answers"]["client_phone"], "+380671234567")
        self.assertAlmostEqual(stored["total"], result.quote.total)

    def test_session_resumes_for_same_user(self):
        session, _ = self.service.start(channel="telegram", user_id="u42")
        self.service.answer(session, "Олег")
        again, question = self.service.start(channel="telegram", user_id="u42")
        self.assertEqual(again.session_id, session.session_id)
        self.assertEqual(question.id, "client_phone")

    def test_quote_blocked_when_answers_missing(self):
        session, _ = self.service.start(channel="api", user_id="u2")
        self.service.answer(session, "Олег")
        result = self.service.quote(session)
        self.assertFalse(result.ok)
        self.assertIn("Не заповнені", result.error)

    def test_unknown_city_saves_order_for_manager(self):
        """Невідоме місто → заявка все одно зберігається, кличемо менеджера."""
        session, _ = self.service.start(channel="api", user_id="u3")
        session.answers.update({
            "client_name": "Олег", "client_phone": "+380671234567",
            "client_type": "individual",
            "pickup_city": "Атлантида", "dropoff_city": "Львів",
            "extra_stops": 0, "return_trip": False,
            "cargo_type": "general", "weight_kg": 500, "volume_m3": 2, "packages": 1,
            "vehicle_type": "gazelle", "temperature_mode": "none",
            "loaders": 0, "tail_lift": False, "urgency": "standard",
            "pickup_date": "2026-12-25", "payment_method": "cash",
            "insurance": False, "manager_call": False,
        })
        session.cursor = len(self.service.questionnaire.questions)
        self.assertEqual(self.service.engine.missing(session), [])

        result = self.service.quote(session)
        self.assertFalse(result.ok)
        self.assertTrue(result.needs_manager)
        self.assertIsNotNone(result.order.id)
        self.assertEqual(
            self.service.repo.get_order(result.order.id)["status"], "manager_requested"
        )

    def test_manager_handoff_end_to_end(self):
        session = self._complete_session()
        result = self.service.quote(session)
        notifier = RecordingNotifier()
        manager = ManagerService(self.service.repo, self.settings, notifier)

        handoff = manager.request(
            session_id=session.session_id, channel="telegram", client_ref="555",
            client_name="Олег", order=result.order, reason="тест",
        )
        self.assertTrue(handoff.ok)
        self.assertEqual(len(notifier.managers), 1)

        # другий запит не створює дубль
        again = manager.request(
            session_id=session.session_id, channel="telegram", client_ref="555",
        )
        self.assertEqual(again.handoff_id, handoff.handoff_id)

        self.assertTrue(manager.claim(handoff.handoff_id, "900").ok)
        self.assertFalse(manager.claim(handoff.handoff_id, "901").ok)

        self.assertTrue(manager.relay_from_client("555", "Коли авто?"))
        self.assertTrue(manager.relay_from_manager("900", "О 14:00"))
        history = self.service.repo.history(handoff.handoff_id)
        self.assertEqual([m["sender"] for m in history], ["client", "manager"])

        manager.close(handoff.handoff_id)
        self.assertFalse(manager.relay_from_client("555", "ще питання"))

    def test_manager_requested_before_claim_keeps_history(self):
        notifier = RecordingNotifier()
        manager = ManagerService(self.service.repo, self.settings, notifier)
        handoff = manager.request(session_id="s", channel="telegram", client_ref="777")
        self.assertTrue(manager.relay_from_client("777", "Є хтось?"))
        self.assertEqual(len(self.service.repo.history(handoff.handoff_id)), 1)


class MappingTest(unittest.TestCase):
    def test_route_points_join_address_and_city(self):
        points = route_points({"pickup_city": "Київ", "pickup_address": "вул. Хрещатик, 22",
                               "dropoff_city": "Львів"})
        self.assertEqual(points, ["вул. Хрещатик, 22, Київ", "Львів"])

    def test_missing_values_get_safe_defaults(self):
        data = answers_to_pricing_input({}, distance_km=100)
        self.assertEqual(data.vehicle, "gazelle")
        self.assertEqual(data.weight_kg, 0)
        self.assertIsNone(data.pickup_date)

    def test_date_parsed_from_iso_string(self):
        data = answers_to_pricing_input({"pickup_date": "2026-12-25"}, distance_km=10)
        self.assertEqual(data.pickup_date.year, 2026)


if __name__ == "__main__":
    unittest.main()
