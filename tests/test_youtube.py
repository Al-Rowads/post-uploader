import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from post_uploader.clients import RemoteError
from post_uploader.config import ConfigurationError, load_local_environment
from post_uploader.database import Database
from post_uploader.youtube import (
    CHUNK_SIZE,
    session_url,
    upload_metadata,
    upload_outcome,
    uploaded_offset,
)


class YouTubeProtocolTests(unittest.TestCase):
    def test_resume_offset_follows_acknowledged_range(self):
        self.assertEqual(uploaded_offset(httpx.Response(308), 20), 0)
        self.assertEqual(
            uploaded_offset(httpx.Response(308, headers={"Range": "bytes=0-9"}), 20), 10
        )
        for value in ("bytes=1-9", "bytes=0-20", "bytes=0--1", "10"):
            with self.subTest(value=value), self.assertRaises(RemoteError):
                uploaded_offset(httpx.Response(308, headers={"Range": value}), 20)
        self.assertEqual(CHUNK_SIZE % (256 * 1024), 0)

    def test_resumable_session_cannot_send_bearer_token_to_another_host(self):
        valid = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=protocol-test"
        self.assertEqual(session_url(valid), valid)
        for value in (
            None,
            "https://[",
            "https://www.googleapis.com.evil.test/upload/youtube/v3/videos",
            "http://www.googleapis.com/upload/youtube/v3/videos",
            "https://www.googleapis.com/other",
            "https://user@www.googleapis.com/upload/youtube/v3/videos",
        ):
            with self.subTest(value=value), self.assertRaises(RemoteError):
                session_url(value)

    def test_metadata_preserves_caption_and_explicit_disclosures(self):
        job = {"title": "My title", "caption": "My title\nMy description"}
        declarations = {
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
            "hasPaidProductPlacement": False,
        }
        metadata = upload_metadata(job, declarations, "private")
        self.assertEqual(metadata["snippet"]["title"], job["title"])
        self.assertEqual(metadata["snippet"]["description"], job["caption"])
        self.assertEqual(
            metadata["status"],
            {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": True,
            },
        )
        for invalid in ({}, {**declarations, "hasPaidProductPlacement": True}):
            with self.assertRaises(RemoteError):
                upload_metadata(job, invalid, "private")
        with self.assertRaises(RemoteError):
            upload_metadata({**job, "caption": "é" * 2501}, declarations, "private")

    def test_uploaded_private_video_is_never_reported_as_public(self):
        resource = {
            "id": "abcdefghijk",
            "status": {"privacyStatus": "private", "uploadStatus": "processed"},
        }
        self.assertEqual(upload_outcome(resource).state, "needs_action")
        resource["status"]["privacyStatus"] = "public"
        resource["status"]["uploadStatus"] = "uploaded"
        self.assertEqual(upload_outcome(resource).state, "needs_action")
        resource["status"]["uploadStatus"] = "processed"
        self.assertEqual(upload_outcome(resource).state, "published")
        resource["status"]["uploadStatus"] = "rejected"
        self.assertEqual(upload_outcome(resource).state, "failed")

    def test_restart_preserves_session_without_requeueing_youtube(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            db = Database(path)
            job = db.create_job(
                {"chat": {"id": 1}, "message_id": 1, "caption": "Resume test"},
                {"file_id": "", "file_unique_id": "", "file_size": 1},
                {},
            )
            request = db.prepare_attempt(job, ["youtube"], "youtube")
            uri = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=protocol-test"
            db.execute("UPDATE attempts SET session_uri=? WHERE request_id=?", (uri, request))
            db.connection.close()
            db = Database(path)
            try:
                db.recover()
                row = db.execute("SELECT * FROM attempts WHERE request_id=?", (request,)).fetchone()
                self.assertEqual(row["session_uri"], uri)
                self.assertEqual(row["provider"], "youtube")
                states = {r["platform"]: r["state"] for r in db.destinations(job)}
                self.assertEqual(states, {"youtube": "pending", "tiktok": "ready"})
            finally:
                db.connection.close()


class EnvironmentTests(unittest.TestCase):
    def test_dotenv_is_literal_and_preserves_exported_values(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('VALUE="$(do_not_execute)"\nKEEP=file\nEMPTY=\n')
            os.environ["KEEP"] = "exported"
            load_local_environment(path)
            self.assertEqual(os.environ["VALUE"], "$(do_not_execute)")
            self.assertEqual(os.environ["KEEP"], "exported")
            self.assertEqual(os.environ["EMPTY"], "")
            path.write_text('BAD="unterminated\n')
            with self.assertRaises(ConfigurationError):
                load_local_environment(path)
