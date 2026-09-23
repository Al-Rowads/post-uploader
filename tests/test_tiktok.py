import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from post_uploader.clients import RemoteError
from post_uploader.database import SCHEMA, Database
from post_uploader.tiktok import chunk_plan, draft_outcome, save_credentials, validate_upload_url
from post_uploader.tiktok_auth import authorization_url, callback_code


class TikTokProtocolTests(unittest.TestCase):
    def test_chunk_boundaries_cover_whole_file_with_documented_limits(self):
        for size in (1, 4_999_999, 5_000_000, 10_000_001, 50_000_123, 64_000_001, 2_000_000_000):
            with self.subTest(size=size):
                chunk, count = chunk_plan(size)
                lengths = [chunk] * (count - 1) + [size - chunk * (count - 1)]
                self.assertEqual(sum(lengths), size)
                self.assertEqual(count, size // chunk)
                self.assertTrue(all(5_000_000 <= n <= 64_000_000 for n in lengths[:-1]))
                self.assertLessEqual(lengths[-1], 128_000_000)
                self.assertLessEqual(count, 1000)
        for size in (0, -1, 2_000_000_001):
            with self.assertRaises(ValueError):
                chunk_plan(size)

    def test_upload_destination_cannot_redirect_media_to_unrelated_hosts(self):
        for url in (
            "http://open-upload.tiktokapis.com/video/",
            "https://tiktokapis.com.evil.test/",
            "https://evil.test/?host=tiktokapis.com",
            "https://127.0.0.1/video/",
            "https://secret@open-upload.tiktokapis.com/video/",
            "https://[",
            None,
        ):
            with self.subTest(url=url), self.assertRaises(RemoteError):
                validate_upload_url(url)
        url = "https://open-upload.tiktokapis.com/video/"
        self.assertEqual(validate_upload_url(url), url)

    def test_inbox_and_private_publication_are_not_public_success(self):
        self.assertEqual(draft_outcome({"status": "SEND_TO_USER_INBOX"}).state, "needs_action")
        self.assertEqual(draft_outcome({"status": "PUBLISH_COMPLETE"}).state, "needs_action")
        self.assertEqual(draft_outcome({"status": "PROCESSING_UPLOAD"}).state, "pending")
        self.assertEqual(draft_outcome({}).state, "pending")
        self.assertEqual(draft_outcome({"status": "FAILED"}).state, "failed")

    def test_callback_requires_exact_state_and_single_code(self):
        self.assertEqual(callback_code("/callback/?state=expected&code=a%2Bb", "expected"), "a+b")
        for target in (
            "/callback/?state=other&code=x",
            "/callback/?code=x",
            "/other/?state=expected&code=x",
            "/callback/?state=expected&code=x&code=y",
            "/callback/?state=expected&state=other&code=x",
            "/callback/?state=expected&error=access_denied",
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                callback_code(target, "expected")

    def test_oauth_requests_draft_scope_and_tiktok_hex_pkce(self):
        query = parse_qs(urlsplit(authorization_url("", "state", "verifier")).query)
        self.assertEqual(query["scope"], ["user.info.basic,video.upload"])
        self.assertEqual(len(query["code_challenge"][0]), 64)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("client_secret", query)

    def test_credentials_are_atomically_saved_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            save_credentials(path, {})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            save_credentials(path, {"scope": "video.upload"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(os.listdir(directory), ["credentials.json"])


class MigrationTests(unittest.TestCase):
    def test_existing_v1_attempt_keeps_original_request_and_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript(SCHEMA)
                connection.execute("PRAGMA user_version = 1")
                connection.execute(
                    "INSERT INTO jobs(id,chat_id,message_id,file_id,file_unique_id,filename,"
                    "file_size,caption,declarations,state,created_at,updated_at) "
                    "VALUES (1,1,1,'','','',1,'','{}','submitted',1,1)"
                )
                connection.execute(
                    "INSERT INTO attempts VALUES ('original',1,'[\"youtube\",\"tiktok\"]',"
                    "'tracking',1,1,7)"
                )
            connection.close()
            database = Database(path)
            try:
                attempt = database.execute("SELECT * FROM attempts").fetchone()
                self.assertEqual(attempt["request_id"], "original")
                self.assertEqual(attempt["provider"], "upload_post")
                self.assertEqual(attempt["polls"], 7)
                self.assertIsNone(attempt["publish_id"])
            finally:
                database.connection.close()

    def test_v1_migration_preserves_attempt_and_requires_review_for_unsent_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(SCHEMA)
            connection.execute("PRAGMA user_version = 1")
            connection.execute(
                "INSERT INTO jobs(id,chat_id,message_id,file_id,file_unique_id,filename,file_size,"
                "caption,title,declarations,state,created_at,updated_at) "
                "VALUES (1,1,1,'source','source','video.mp4',100,'Original caption','Title',"
                "'{}','submitted',1,1)"
            )
            connection.execute(
                "INSERT INTO destinations(job_id,platform,state) VALUES "
                "(1,'youtube','pending'),(1,'tiktok','ready')"
            )
            connection.execute(
                "INSERT INTO attempts VALUES ('original',1,'[\"youtube\"]','tracking',1,1,7)"
            )
            connection.commit()
            connection.close()
            database = Database(path)
            try:
                database.recover()
                attempt = database.execute("SELECT * FROM attempts").fetchone()
                self.assertEqual(attempt["request_id"], "original")
                self.assertEqual(attempt["provider"], "upload_post")
                self.assertEqual(attempt["polls"], 7)
                self.assertEqual(database.destination(1, "tiktok")["state"], "review")
                self.assertEqual(database.destination(1, "tiktok")["caption"], "Original caption")
                self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 4)
                with self.assertRaises(ValueError):
                    database.prepare_attempt(1, ["tiktok"], "tiktok_direct")
            finally:
                database.connection.close()
