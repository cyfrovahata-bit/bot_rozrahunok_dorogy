"""Тести калькулятора: тарифні відрізки, коефіцієнти, надбавки, ПДВ."""

import unittest
from datetime import date

from delivery.models import PricingInput
from delivery.pricing import calculate, load_tariff
from delivery.pricing.calculator import chargeable_weight, distance_cost
from delivery.pricing.tariff import Tariff, TariffError

TARIFFS = "config/tariffs.yaml"


def item(quote, code):
    for line in quote.items:
        if line.code == code:
            return line
    return None


class DistanceCostTest(unittest.TestCase):
    def setUp(self):
        self.tariff = load_tariff(TARIFFS)

    def test_within_first_tier(self):
        cost, _ = distance_cost(30, self.tariff)
        self.assertAlmostEqual(cost, 30 * 26)

    def test_progressive_tiers(self):
        cost, _ = distance_cost(300, self.tariff)
        expected = 50 * 26 + 150 * 22 + 100 * 18
        self.assertAlmostEqual(cost, expected)

    def test_last_open_tier(self):
        cost, _ = distance_cost(1000, self.tariff)
        expected = 50 * 26 + 150 * 22 + 300 * 18 + 500 * 15
        self.assertAlmostEqual(cost, expected)

    def test_zero_distance(self):
        cost, _ = distance_cost(0, self.tariff)
        self.assertEqual(cost, 0)


class ChargeableWeightTest(unittest.TestCase):
    def setUp(self):
        self.tariff = load_tariff(TARIFFS)

    def test_volumetric_wins(self):
        # 10 м³ × 250 кг/м³ = 2500 кг > 800 кг фактичних
        self.assertEqual(chargeable_weight(800, 10, self.tariff), 2500)

    def test_actual_wins(self):
        self.assertEqual(chargeable_weight(3000, 2, self.tariff), 3000)


class CalculateTest(unittest.TestCase):
    def setUp(self):
        self.tariff = load_tariff(TARIFFS)

    def base_input(self, **kwargs):
        defaults = dict(
            distance_km=300, weight_kg=1000, volume_m3=4, vehicle="gazelle",
            cargo_type="general", urgency="standard", pickup_date=date(2026, 10, 5),  # пн
        )
        defaults.update(kwargs)
        return PricingInput(**defaults)

    def test_breakdown_sums_to_total(self):
        quote = calculate(self.base_input(loaders=2, tail_lift=True), self.tariff)
        self.assertAlmostEqual(sum(i.amount for i in quote.items), quote.total, places=0)

    def test_coefficients_increase_price(self):
        cheap = calculate(self.base_input(), self.tariff).total
        pricey = calculate(
            self.base_input(vehicle="truck_20t", cargo_type="dangerous", urgency="express"),
            self.tariff,
        ).total
        self.assertGreater(pricey, cheap * 2)

    def test_min_price_applied(self):
        quote = calculate(self.base_input(distance_km=3, weight_kg=10, volume_m3=0), self.tariff)
        self.assertGreaterEqual(quote.total, self.tariff.min_price)
        self.assertIsNotNone(item(quote, "min_price"))

    def test_weekend_coefficient(self):
        workday = calculate(self.base_input(pickup_date=date(2026, 10, 5)), self.tariff)
        saturday = calculate(self.base_input(pickup_date=date(2026, 10, 3)), self.tariff)
        self.assertIsNone(item(workday, "coef_weekend"))
        self.assertIsNotNone(item(saturday, "coef_weekend"))
        self.assertGreater(saturday.total, workday.total)

    def test_overweight_surcharge_and_warning(self):
        quote = calculate(self.base_input(weight_kg=3000, volume_m3=0), self.tariff)
        self.assertIsNotNone(item(quote, "overweight"))
        self.assertTrue(any("перевищує ліміт" in w for w in quote.warnings))

    def test_vat_added_on_top(self):
        without = calculate(self.base_input(), self.tariff)
        with_vat = calculate(self.base_input(vat=True), self.tariff)
        self.assertEqual(without.vat_amount, 0)
        self.assertAlmostEqual(with_vat.vat_amount, with_vat.subtotal * 0.2, delta=10)
        self.assertAlmostEqual(with_vat.total, with_vat.subtotal + with_vat.vat_amount, places=2)

    def test_insurance_minimum_without_declared_value(self):
        quote = calculate(self.base_input(insurance=True), self.tariff)
        self.assertEqual(item(quote, "insurance").amount, self.tariff.surcharge("insurance_min"))
        self.assertTrue(any("Оголошену вартість" in w for w in quote.warnings))

    def test_insurance_percent_with_declared_value(self):
        quote = calculate(self.base_input(insurance=True, declared_value=400000), self.tariff)
        self.assertAlmostEqual(item(quote, "insurance").amount, 2000)

    def test_extra_services(self):
        quote = calculate(
            self.base_input(loaders=3, tail_lift=True, extra_stops=2, return_trip=True),
            self.tariff,
        )
        self.assertAlmostEqual(item(quote, "loaders").amount, 1350)
        self.assertAlmostEqual(item(quote, "tail_lift").amount, 300)
        self.assertAlmostEqual(item(quote, "extra_stops").amount, 500)
        self.assertGreater(item(quote, "return_trip").amount, 0)

    def test_discount_reduces_total(self):
        full = calculate(self.base_input(distance_km=800), self.tariff).total
        discounted = calculate(
            self.base_input(distance_km=800, discount_percent=10), self.tariff
        ).total
        self.assertLess(discounted, full)

    def test_unknown_vehicle_raises_clear_error(self):
        with self.assertRaises(TariffError) as ctx:
            calculate(self.base_input(vehicle="вертоліт"), self.tariff)
        self.assertIn("Доступні", str(ctx.exception))


class TariffValidationTest(unittest.TestCase):
    def test_tiers_must_end_open(self):
        raw = {"distance_tiers": [{"up_to_km": 100, "rate": 10}]}
        with self.assertRaises(TariffError):
            Tariff.from_raw(raw)

    def test_tiers_must_be_sorted(self):
        raw = {
            "distance_tiers": [
                {"up_to_km": 200, "rate": 20},
                {"up_to_km": 50, "rate": 26},
                {"up_to_km": None, "rate": 15},
            ]
        }
        with self.assertRaises(TariffError):
            Tariff.from_raw(raw)

    def test_negative_coefficient_rejected(self):
        raw = {
            "distance_tiers": [{"up_to_km": None, "rate": 15}],
            "coefficients": {"vehicle": {"gazelle": 0}},
        }
        with self.assertRaises(TariffError):
            Tariff.from_raw(raw)


if __name__ == "__main__":
    unittest.main()
