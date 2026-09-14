"""Тести анкети: валідація відповідей, залежні питання, навігація."""

import unittest
from datetime import date, timedelta

from delivery.questionnaire import QuestionnaireEngine, ValidationError, load_questionnaire

QUESTIONS = "config/questions.yaml"


class ParsingTest(unittest.TestCase):
    def setUp(self):
        self.q = load_questionnaire(QUESTIONS)

    def test_phone_normalised(self):
        phone = self.q.get("client_phone")
        for raw in ["0671234567", "+380671234567", "067 123 45 67", "38 (067) 123-45-67"]:
            self.assertEqual(phone.parse(raw), "+380671234567", raw)

    def test_phone_rejected(self):
        with self.assertRaises(ValidationError):
            self.q.get("client_phone").parse("123")

    def test_choice_by_number_label_and_value(self):
        vehicle = self.q.get("vehicle_type")
        self.assertEqual(vehicle.parse("3"), "truck_5t")
        self.assertEqual(vehicle.parse("truck_5t"), "truck_5t")
        self.assertEqual(vehicle.parse("Фура 20 т (86 м³)"), "truck_20t")

    def test_choice_rejects_unknown(self):
        with self.assertRaises(ValidationError):
            self.q.get("vehicle_type").parse("вертоліт")

    def test_number_bounds_and_comma(self):
        weight = self.q.get("weight_kg")
        self.assertEqual(weight.parse("1 200,5"), 1200.5)
        with self.assertRaises(ValidationError):
            weight.parse("999999")
        with self.assertRaises(ValidationError):
            weight.parse("не знаю")

    def test_bool_words(self):
        flag = self.q.get("tail_lift")
        self.assertIs(flag.parse("так"), True)
        self.assertIs(flag.parse("ні"), False)
        with self.assertRaises(ValidationError):
            flag.parse("можливо")

    def test_date_shortcuts_and_past(self):
        pickup = self.q.get("pickup_date")
        self.assertEqual(pickup.parse("завтра"), (date.today() + timedelta(days=1)).isoformat())
        self.assertEqual(pickup.parse("25.12.2026"), "2026-12-25")
        with self.assertRaises(ValidationError):
            pickup.parse("01.01.2020")

    def test_optional_can_be_skipped(self):
        self.assertIsNone(self.q.get("comment").parse("-"))
        with self.assertRaises(ValidationError):
            self.q.get("client_name").parse("-")

    def test_default_applied_on_empty(self):
        self.assertEqual(self.q.get("extra_stops").parse(""), 0)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.engine = QuestionnaireEngine(load_questionnaire(QUESTIONS))

    def test_five_blocks(self):
        self.assertEqual(len(self.engine.q.blocks), 5)

    def test_dependent_question_shown_and_hidden(self):
        session = self.engine.start()
        self.engine.submit(session, "Олег")
        self.engine.submit(session, "0671234567")
        self.engine.submit(session, "Юридична особа / ФОП")
        self.assertEqual(self.engine.current(session).id, "company_name")

        # крок назад повертає до питання про тип клієнта
        self.assertEqual(self.engine.back(session).id, "client_type")
        # змінюємо відповідь — залежне питання зникає разом з відповіддю
        self.engine.submit(session, "Фізична особа")
        self.assertEqual(self.engine.current(session).id, "pickup_city")
        self.assertNotIn("company_name", session.answers)

    def test_invalid_answer_keeps_position(self):
        session = self.engine.start()
        self.engine.submit(session, "Олег")
        before = self.engine.current(session).id
        result = self.engine.submit(session, "абв")
        self.assertFalse(result.ok)
        self.assertEqual(self.engine.current(session).id, before)

    def test_progress_and_completion(self):
        session = self.engine.start()
        self.assertEqual(self.engine.progress(session).percent, 0)
        for value in _walk_answers():
            if self.engine.current(session) is None:
                break
            self.engine.submit(session, value)
        self.assertTrue(session.is_complete)
        self.assertEqual(self.engine.progress(session).percent, 100)
        self.assertEqual(self.engine.missing(session), [])

    def test_session_roundtrip(self):
        from delivery.questionnaire.engine import Session

        session = self.engine.start(channel="telegram", external_user_id="42")
        self.engine.submit(session, "Олег")
        restored = Session.from_dict(session.to_dict())
        self.assertEqual(restored.answers, session.answers)
        self.assertEqual(restored.cursor, session.cursor)
        self.assertEqual(restored.external_user_id, "42")


def _walk_answers():
    """Відповіді, яких вистачає, щоб пройти анкету до кінця."""
    return [
        "Олег", "0671234567", "Фізична особа",
        "Київ", "-", "Львів", "-", "0", "ні",
        "Звичайний (генеральний)", "500", "2", "1", "-",
        "Газель (до 1.5 т, 9 м³)", "Не потрібен", "0", "ні", "Стандартно (2-3 дні)",
        "завтра", "Готівка", "ні", "-", "ні",
    ]


if __name__ == "__main__":
    unittest.main()
