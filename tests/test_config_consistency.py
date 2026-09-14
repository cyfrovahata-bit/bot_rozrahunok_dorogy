"""Конфіги мають бути узгоджені між собою.

Цей тест ловить найчастішу помилку експлуатації: у questions.yaml додали
новий варіант (напр. авто «манiпулятор»), а коефіцієнт у tariffs.yaml
не завели — бот падав би вже на клієнті.
Запускайте після КОЖНОЇ правки конфігів:  python -m unittest discover tests
"""

import unittest

from delivery.pricing import load_tariff
from delivery.questionnaire import load_questionnaire
from delivery.services.mapping import answers_to_pricing_input

QUESTIONS = "config/questions.yaml"
TARIFFS = "config/tariffs.yaml"

# питання анкети → група коефіцієнтів у тарифах
LINKED = {
    "vehicle_type": "vehicle",
    "cargo_type": "cargo_type",
    "urgency": "urgency",
    "temperature_mode": "temperature_mode",
}

# id, без яких калькулятор не порахує
REQUIRED_IDS = {
    "client_name", "client_phone", "pickup_city", "dropoff_city",
    "weight_kg", "volume_m3", "vehicle_type", "cargo_type", "urgency",
    "pickup_date",
}


class ConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.questionnaire = load_questionnaire(QUESTIONS)
        self.tariff = load_tariff(TARIFFS)

    def test_every_option_has_coefficient(self):
        for question_id, group in LINKED.items():
            question = self.questionnaire.get(question_id)
            table = self.tariff.coefficients.get(group, {})
            for option in question.options:
                self.assertIn(
                    option.value, table,
                    f"У tariffs.yaml немає coefficients.{group}.{option.value} "
                    f"(варіант «{option.label}» з questions.yaml)",
                )

    def test_every_vehicle_has_capacity(self):
        for option in self.questionnaire.get("vehicle_type").options:
            self.assertIn(
                option.value, self.tariff.vehicle_capacity,
                f"У tariffs.yaml немає vehicle_capacity.{option.value}",
            )

    def test_ids_used_by_calculator_exist(self):
        ids = {q.id for q in self.questionnaire.questions}
        missing = REQUIRED_IDS - ids
        self.assertFalse(missing, f"В анкеті бракує питань: {sorted(missing)}")

    def test_required_ids_are_mandatory_questions(self):
        for question_id in REQUIRED_IDS:
            self.assertTrue(
                self.questionnaire.get(question_id).required,
                f"Питання '{question_id}' потрібне для розрахунку — не робіть його необов'язковим",
            )

    def test_exactly_five_blocks_each_with_questions(self):
        self.assertEqual(len(self.questionnaire.blocks), 5)
        for block in self.questionnaire.blocks:
            self.assertTrue(block.questions, f"Блок '{block.id}' порожній")

    def test_surcharge_keys_present(self):
        expected = {
            "loader_per_person", "tail_lift", "extra_stop", "return_trip_ratio",
            "weekend_coefficient", "fuel_surcharge_percent", "insurance_percent",
            "insurance_min", "overweight_percent_per_ton",
        }
        missing = expected - set(self.tariff.surcharges)
        self.assertFalse(missing, f"У tariffs.yaml бракує surcharges: {sorted(missing)}")

    def test_defaults_pass_their_own_validation(self):
        for question in self.questionnaire.questions:
            if question.default is not None:
                question.parse(question.default)

    def test_choice_defaults_are_valid_options(self):
        for question in self.questionnaire.questions:
            if question.type == "choice" and question.default is not None:
                self.assertIn(
                    question.default, [o.value for o in question.options],
                    f"default '{question.default}' немає серед варіантів '{question.id}'",
                )

    def test_mapping_accepts_every_option_combination(self):
        """Будь-яка комбінація варіантів анкети рахується без помилок."""
        from itertools import product
        from delivery.pricing import calculate

        groups = [self.questionnaire.get(q).options for q in LINKED]
        for combination in product(*groups):
            answers = {
                question_id: option.value
                for question_id, option in zip(LINKED, combination)
            }
            answers.update({"weight_kg": 500, "volume_m3": 2})
            quote = calculate(answers_to_pricing_input(answers, 250), self.tariff)
            self.assertGreater(quote.total, 0)


if __name__ == "__main__":
    unittest.main()
