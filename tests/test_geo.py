"""Тести визначення кілометражу (офлайн-режим, кеш, резервний провайдер)."""

import tempfile
import unittest
from pathlib import Path

from delivery.geo.base import GeoError, normalize_place
from delivery.geo.cache import CachedProvider
from delivery.geo.providers import ChainProvider, OfflineProvider, haversine_km
from delivery.models import RouteInfo


class NormalizeTest(unittest.TestCase):
    def test_strips_city_prefix_and_case(self):
        self.assertEqual(normalize_place("м. Київ"), "київ")
        self.assertEqual(normalize_place("  ЛЬВІВ , "), "львів")


class OfflineProviderTest(unittest.TestCase):
    def setUp(self):
        self.provider = OfflineProvider()

    def test_known_routes_within_ten_percent(self):
        # реальні відстані по трасі, км
        expected = {("Київ", "Львів"): 540, ("Київ", "Харків"): 480,
                    ("Київ", "Дніпро"): 480, ("Львів", "Одеса"): 790}
        for (a, b), real in expected.items():
            distance = self.provider.route([a, b]).distance_km
            self.assertLess(abs(distance - real) / real, 0.11, f"{a}-{b}: {distance}")

    def test_resolves_address_with_street(self):
        self.assertEqual(self.provider.resolve("вул. Хрещатик, 22, Київ")[0], "Київ")
        self.assertEqual(self.provider.resolve("Kyiv")[0], "Київ")

    def test_waypoints_increase_distance(self):
        direct = self.provider.route(["Київ", "Львів"]).distance_km
        via = self.provider.route(["Київ", "Одеса", "Львів"]).distance_km
        self.assertGreater(via, direct)

    def test_unknown_city_suggests_alternative(self):
        with self.assertRaises(GeoError) as ctx:
            self.provider.route(["Мукачеве", "Київ"])
        self.assertIn("Мукачево", str(ctx.exception))

    def test_single_point_rejected(self):
        with self.assertRaises(GeoError):
            self.provider.route(["Київ"])

    def test_result_marked_as_estimate(self):
        self.assertTrue(self.provider.route(["Київ", "Львів"]).is_estimate)

    def test_haversine_symmetry(self):
        a = haversine_km(50.45, 30.52, 49.84, 24.03)
        b = haversine_km(49.84, 24.03, 50.45, 30.52)
        self.assertAlmostEqual(a, b, places=6)


class FailingProvider:
    name = "failing"

    def route(self, points):
        raise GeoError("сервіс лежить")


class CountingProvider:
    name = "counting"

    def __init__(self):
        self.calls = 0

    def route(self, points):
        self.calls += 1
        return RouteInfo(distance_km=100, duration_min=90, provider=self.name)


class ChainTest(unittest.TestCase):
    def test_falls_back_to_next_provider(self):
        chain = ChainProvider([FailingProvider(), OfflineProvider()])
        info = chain.route(["Київ", "Львів"])
        self.assertGreater(info.distance_km, 0)
        self.assertIn("резервний", info.note)

    def test_all_failed_raises(self):
        with self.assertRaises(GeoError):
            ChainProvider([FailingProvider(), FailingProvider()]).route(["Київ", "Львів"])


class CacheTest(unittest.TestCase):
    def test_second_call_served_from_cache(self):
        inner = CountingProvider()
        with tempfile.TemporaryDirectory() as tmp:
            cached = CachedProvider(inner, Path(tmp) / "cache.sqlite3")
            first = cached.route(["Київ", "Львів"])
            second = cached.route(["м. КИЇВ", "львів"])   # інший регістр і префікс
            self.assertEqual(inner.calls, 1)
            self.assertFalse(first.from_cache)
            self.assertTrue(second.from_cache)
            self.assertEqual(first.distance_km, second.distance_km)


if __name__ == "__main__":
    unittest.main()
