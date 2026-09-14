"""Налаштування для Railway: де лежить база і коли ми попереджаємо про втрату даних."""

import json
import os
import unittest
from pathlib import Path

from delivery.config import Settings


def env(**values):
    """Тимчасове оточення: усі Railway-змінні під контролем тесту."""
    cleared = {
        key: None
        for key in (
            "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT",
            "RAILWAY_VOLUME_MOUNT_PATH", "DB_PATH",
        )
    }
    cleared.update(values)
    return _Patch(cleared)


class _Patch:
    def __init__(self, values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class StoragePathTest(unittest.TestCase):
    def test_local_default(self):
        with env():
            settings = Settings.load(env_file="не існує")
            self.assertEqual(settings.db_path, "data/delivery.sqlite3")
            self.assertEqual(settings.storage_warnings(), [])

    def test_railway_without_volume_warns_about_data_loss(self):
        with env(RAILWAY_ENVIRONMENT_NAME="production"):
            warnings = Settings.load(env_file="не існує").storage_warnings()
            self.assertEqual(len(warnings), 1)
            self.assertIn("Volume", warnings[0])

    def test_railway_with_volume_uses_it_automatically(self):
        with env(RAILWAY_ENVIRONMENT_NAME="production", RAILWAY_VOLUME_MOUNT_PATH="/data"):
            settings = Settings.load(env_file="не існує")
            self.assertEqual(settings.db_path, "/data/delivery.sqlite3")
            self.assertEqual(settings.storage_warnings(), [])

    def test_db_path_outside_volume_warns(self):
        with env(
            RAILWAY_ENVIRONMENT_NAME="production",
            RAILWAY_VOLUME_MOUNT_PATH="/data",
            DB_PATH="/app/delivery.sqlite3",
        ):
            warnings = Settings.load(env_file="не існує").storage_warnings()
            self.assertEqual(len(warnings), 1)
            self.assertIn("за межами тому", warnings[0])

    def test_explicit_db_path_inside_volume_is_fine(self):
        with env(
            RAILWAY_ENVIRONMENT_NAME="production",
            RAILWAY_VOLUME_MOUNT_PATH="/data",
            DB_PATH="/data/orders/db.sqlite3",
        ):
            self.assertEqual(Settings.load(env_file="не існує").storage_warnings(), [])

    def test_env_vars_win_over_dotenv_file(self):
        """На Railway змінні задаються в панелі, файлу .env там немає."""
        with env(RAILWAY_ENVIRONMENT_NAME="production", DB_PATH="/data/x.sqlite3"):
            self.assertEqual(Settings.load(env_file=".env.example").db_path, "/data/x.sqlite3")


class RailwayFilesTest(unittest.TestCase):
    def test_railway_json_valid_and_starts_bot(self):
        config = json.loads(Path("railway.json").read_text(encoding="utf-8"))
        # білдер не нав'язуємо: Railway сам обирає актуальний (зараз Railpack),
        # а примусовий NIXPACKS ламав збірку Python
        self.assertNotIn("builder", config.get("build", {}))
        self.assertEqual(
            config["deploy"]["startCommand"], "python -m delivery.bot.telegram"
        )
        # дві репліки = два polling-з'єднання, Telegram цього не дозволяє
        self.assertEqual(config["deploy"]["numReplicas"], 1)

    def test_requirements_contain_runtime_deps(self):
        text = Path("requirements.txt").read_text(encoding="utf-8")
        active = [line for line in text.splitlines()
                  if line.strip() and not line.strip().startswith("#")]
        self.assertTrue(any("aiogram" in line for line in active))
        self.assertTrue(any("PyYAML" in line for line in active))


if __name__ == "__main__":
    unittest.main()
