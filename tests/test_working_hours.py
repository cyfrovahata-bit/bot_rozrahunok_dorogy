"""Робочий час менеджерів рахується за часовим поясом бізнесу, не сервера."""

import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from delivery.config import Settings
from delivery.services.manager import ManagerService
from delivery.storage import Repository


class TimezoneTest(unittest.TestCase):
    def test_now_is_timezone_aware(self):
        now = Settings.load(env_file="не існує").now()
        self.assertIsNotNone(now.tzinfo)

    def test_kyiv_differs_from_utc(self):
        settings = replace(Settings.load(env_file="не існує"), timezone="Europe/Kyiv")
        kyiv = settings.now()
        utc = datetime.now(timezone.utc)
        offset_hours = round((kyiv.utcoffset().total_seconds()) / 3600)
        self.assertIn(offset_hours, (2, 3))          # зима / літо
        self.assertEqual(kyiv.hour, (utc.hour + offset_hours) % 24)

    def test_unknown_timezone_falls_back_without_crashing(self):
        settings = replace(Settings.load(env_file="не існує"), timezone="Марс/Олімп")
        with self.assertLogs("delivery.config", level="WARNING"):
            self.assertIsNotNone(settings.now())

    def test_timezone_read_from_env(self):
        saved = os.environ.get("TIMEZONE")
        os.environ["TIMEZONE"] = "Europe/Warsaw"
        try:
            self.assertEqual(Settings.load(env_file="не існує").timezone, "Europe/Warsaw")
        finally:
            os.environ.pop("TIMEZONE", None)
            if saved:
                os.environ["TIMEZONE"] = saved


class WorkingHoursTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = replace(
            Settings.load(env_file="не існує"),
            db_path=str(Path(self.tmp.name) / "t.sqlite3"),
            timezone="Europe/Kyiv",
            work_hours_start=9,
            work_hours_end=19,
            work_days=(1, 2, 3, 4, 5),
        )
        self.manager = ManagerService(Repository(self.settings.db_path), self.settings)

    def kyiv(self, day: int, hour: int) -> datetime:
        return datetime(2026, 10, day, hour, 0, tzinfo=ZoneInfo("Europe/Kyiv"))

    def test_workday_inside_hours(self):
        self.assertTrue(self.manager.is_working_hours(self.kyiv(5, 10)))   # пн 10:00

    def test_workday_before_opening(self):
        self.assertFalse(self.manager.is_working_hours(self.kyiv(5, 8)))

    def test_workday_after_closing(self):
        self.assertFalse(self.manager.is_working_hours(self.kyiv(5, 19)))

    def test_weekend_is_closed(self):
        self.assertFalse(self.manager.is_working_hours(self.kyiv(3, 12)))  # сб
        self.assertFalse(self.manager.is_working_hours(self.kyiv(4, 12)))  # нд

    def test_note_matches_state(self):
        note = self.manager.availability_note()
        self.assertTrue(note)
        if self.manager.is_working_hours():
            self.assertIn("кілька хвилин", note)
        else:
            self.assertIn("неробочий час", note)

    def test_utc_evening_is_already_off_hours_in_kyiv(self):
        """О 17:00 UTC у Києві вже 20:00 — менеджери не працюють."""
        utc_evening = datetime(2026, 10, 5, 17, 0, tzinfo=timezone.utc)
        kyiv_time = utc_evening.astimezone(ZoneInfo("Europe/Kyiv"))
        self.assertEqual(kyiv_time.hour, 20)
        self.assertFalse(self.manager.is_working_hours(kyiv_time))


if __name__ == "__main__":
    unittest.main()
