import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from post_uploader.__main__ import bind_database_identity
from post_uploader.config import Config, ConfigurationError, owner_matches
from post_uploader.database import Database


class OwnerConfigurationTests(unittest.TestCase):
    def environment(self, **values):
        return patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "1:test-token",
                "OPENROUTER_API_KEY": "test-key",
                "TELEGRAM_ALLOWED_USERNAME": "@NotRshia",
                **values,
            },
            clear=True,
        )

    def test_username_is_normalized_without_requiring_numeric_id(self):
        for old_id in ("", "not-a-number", "123"):
            with self.subTest(old_id=old_id), self.environment(TELEGRAM_ALLOWED_USER_ID=old_id):
                self.assertEqual(Config.from_environment().owner_username, "notrshia")
        with self.environment(TELEGRAM_ALLOWED_USERNAME="NoTRsHia"):
            self.assertEqual(Config.from_environment().owner_username, "notrshia")

    def test_missing_or_malformed_username_is_rejected(self):
        for value in ("", "@", "@@NotRshia", "https://t.me/NotRshia", "Not Rshia"):
            with self.subTest(value=value), self.environment(TELEGRAM_ALLOWED_USERNAME=value):
                with self.assertRaisesRegex(ConfigurationError, "TELEGRAM_ALLOWED_USERNAME"):
                    Config.from_environment()

    def test_sender_username_is_authoritative_and_display_names_do_not_grant_access(self):
        self.assertTrue(owner_matches({"id": 9876, "username": "NoTRsHia"}, "@notrshia"))
        for user in (
            {"id": 1, "first_name": "NotRshia"},
            {"id": 1, "username": "other"},
            {"id": 1, "username": None},
            {"id": 1, "username": "NotRshia", "is_bot": True},
            {"id": True, "username": "NotRshia"},
            {"id": -1, "username": "NotRshia"},
            None,
        ):
            with self.subTest(user=user):
                self.assertFalse(owner_matches(user, "notrshia"))

    def test_legacy_database_identity_upgrades_without_removing_jobs(self):
        with self.environment():
            config = Config.from_environment()
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "jobs.sqlite3")
            try:
                database.set_setting(
                    "identity", json.dumps({"bot": "1", "owner": 42, "profile": ""})
                )
                job_id = database.create_job(
                    {"chat": {"id": 42}, "message_id": 1},
                    {"file_id": "test", "file_unique_id": "test", "file_size": 1},
                    {},
                )
                bind_database_identity(config, database)
                self.assertEqual(json.loads(database.get_setting("identity"))["owner"], "notrshia")
                self.assertEqual(database.job(job_id)["chat_id"], 42)
                bind_database_identity(config, database)
                for changed in (
                    replace(config, telegram_token="2:test-token"),
                    replace(config, owner_username="someone_else"),
                    replace(config, profile="another-profile"),
                ):
                    with self.assertRaises(ConfigurationError):
                        bind_database_identity(changed, database)
            finally:
                database.connection.close()
