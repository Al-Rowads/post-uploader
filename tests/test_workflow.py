import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from post_uploader.config import Config
from post_uploader.database import Database
from post_uploader.domain import Outcome, outcome_from_result, parse_caption, public_url
from post_uploader.media import MediaError, Video, inspect_video, safe_media_path
from post_uploader.service import Service


class CaptionTests(unittest.TestCase):
    def test_title_and_caption_are_preserved(self):
        caption = "A walk by the river\nRecorded this morning. #nature"
        self.assertEqual(parse_caption(caption), ("A walk by the river", caption))

    def test_invalid_text_is_rejected_not_truncated(self):
        for caption in ("", " \n ", "x" * 101, "title\n" + "x" * 2200, "<title>"):
            with self.subTest(caption=caption[:20]), self.assertRaises(ValueError):
                parse_caption(caption)

    def test_redaction_preserves_long_bot_messages_without_exposing_credentials(self):
        config = Config(
            telegram_token="123456:secret",
            owner_id=1,
            upload_post_key="private-api-key",
            profile="",
            declarations={},
        )
        message = "Instructions " * 100 + config.telegram_token + config.upload_post_key
        sanitized = config.redact(message, limit=4000)
        self.assertGreater(len(sanitized), 700)
        self.assertNotIn(config.telegram_token, sanitized)
        self.assertNotIn(config.upload_post_key, sanitized)


class OutcomeTests(unittest.TestCase):
    def test_queue_acknowledgment_is_not_publication(self):
        result = {"success": True, "status": "queued"}
        self.assertEqual(outcome_from_result(result, "youtube").state, "pending")
        self.assertEqual(outcome_from_result({"success": True}, "youtube").state, "pending")

    def test_skipped_platform_and_inbox_are_not_success(self):
        self.assertEqual(
            outcome_from_result({"success": True, "skipped": True}, "youtube").state, "failed"
        )
        self.assertEqual(
            outcome_from_result({"success": True, "fallback_to_inbox": True}, "tiktok").state,
            "needs_action",
        )

    def test_provider_managed_retry_stays_pending(self):
        self.assertEqual(
            outcome_from_result({"success": False, "status": "retryable"}, "tiktok").state,
            "pending",
        )

    def test_confirmed_publication_can_wait_for_link(self):
        self.assertEqual(outcome_from_result({"status": "completed"}, "tiktok").state, "published")
        self.assertIsNone(outcome_from_result({"status": "completed"}, "tiktok").url)

    def test_warning_requires_inspection(self):
        result = {"status": "completed", "warnings": ["privacy_level ignored"]}
        self.assertEqual(outcome_from_result(result, "tiktok").state, "needs_action")

    def test_untrusted_links_are_not_exposed_as_post_links(self):
        for url in (
            "http://www.youtube.com/watch",
            "https://youtube.com.evil.test/watch",
            "https://youtube.com@evil.test/watch",
            "Video sent to Inbox (No Public URL)",
            "https://www.youtube.com/",
            "https://[",
        ):
            self.assertIsNone(public_url(url, "youtube"))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "jobs.sqlite3"
        self.db = Database(self.path)
        self.message = {
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1},
            "message_id": 1,
            "caption": "A walk by the river\n#nature",
        }
        self.media = {
            "file_id": "unit-test-file",
            "file_unique_id": "unit-test-unique",
            "file_size": 100,
            "file_name": "walk.mp4",
        }

    def tearDown(self):
        self.db.connection.close()
        self.directory.cleanup()

    def create(self):
        return self.db.create_job(self.message, self.media, {})

    def test_duplicate_telegram_delivery_creates_one_job(self):
        first = self.create()
        self.assertEqual(self.create(), first)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(len(self.db.destinations(first)), 2)

    def test_ingestion_checkpoint_rolls_back_with_job(self):
        with self.assertRaises(RuntimeError), self.db.transaction():
            self.create()
            self.db.set_setting("offset", "2")
            raise RuntimeError("Interrupted transaction")
        self.assertEqual(self.db.get_setting("offset"), "0")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_missing_caption_waits_for_reply(self):
        self.message.pop("caption")
        job_id = self.create()
        self.assertEqual(self.db.job(job_id)["state"], "waiting_caption")
        self.db.caption(job_id, "River walk")
        self.assertEqual(self.db.job(job_id)["state"], "queued")

    def test_restart_recovers_preparation_but_does_not_resubmit_attempt(self):
        job_id = self.create()
        self.db.execute("UPDATE jobs SET state='preparing' WHERE id=?", (job_id,))
        self.db.recover()
        self.assertEqual(self.db.job(job_id)["state"], "queued")
        request_id = self.db.prepare_attempt(job_id, ["youtube", "tiktok"])
        self.db.connection.close()
        self.db = Database(self.path)
        self.db.recover()
        self.assertEqual(self.db.job(job_id)["state"], "submitted")
        attempt = self.db.execute(
            "SELECT * FROM attempts WHERE request_id=?", (request_id,)
        ).fetchone()
        self.assertEqual(attempt["state"], "tracking")

    def test_retry_only_resets_confirmed_failure(self):
        job_id = self.create()
        self.db.outcome(job_id, "youtube", Outcome("published"))
        self.db.outcome(job_id, "tiktok", Outcome("failed"))
        self.db.finish_if_terminal(job_id)
        self.db.retry(job_id)
        states = {row["platform"]: row["state"] for row in self.db.destinations(job_id)}
        self.assertEqual(states, {"youtube": "published", "tiktok": "ready"})

    def test_unknown_attempt_retry_only_rechecks_existing_request(self):
        job_id = self.create()
        request_id = self.db.prepare_attempt(job_id, ["youtube", "tiktok"])
        self.db.execute("UPDATE attempts SET state='unknown'")
        self.db.retry(job_id)
        self.assertEqual(self.db.job(job_id)["state"], "submitted")
        attempts = self.db.execute("SELECT * FROM attempts").fetchall()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["request_id"], request_id)
        self.assertEqual(attempts[0]["state"], "tracking")

    def test_cannot_cancel_or_change_caption_after_submission(self):
        job_id = self.create()
        self.db.prepare_attempt(job_id, ["youtube", "tiktok"])
        self.db.cancel(job_id)
        self.db.caption(job_id, "Changed title")
        self.assertEqual(self.db.job(job_id)["state"], "submitted")
        self.assertEqual(self.db.job(job_id)["title"], "A walk by the river")


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = Database(self.root / "jobs.sqlite3")
        self.config = Config(
            telegram_token="",
            owner_id=1,
            upload_post_key="",
            profile="",
            declarations={},
            data_directory=self.root,
            telegram_files=self.root,
        )
        self.service = Service(self.config, self.db)

    async def asyncTearDown(self):
        await self.service.close()
        self.db.connection.close()
        self.directory.cleanup()

    def update(self, user=1, chat_type="private"):
        return {
            "update_id": 1,
            "message": {
                "from": {"id": user},
                "chat": {"id": user, "type": chat_type},
                "message_id": 1,
                "caption": "River walk",
                "video": {
                    "file_id": "unit-test-file",
                    "file_unique_id": "unit-test-unique",
                    "file_size": 100,
                },
            },
        }

    async def test_unauthorized_and_group_messages_do_not_create_jobs_or_notifications(self):
        self.service.handle_update(self.update(user=2))
        self.service.handle_update(self.update(chat_type="group"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    async def test_pause_is_persistent(self):
        self.service.command(1, "/pause")
        self.assertEqual(self.db.get_setting("paused"), "true")
        self.service.command(1, "/resume")
        self.assertEqual(self.db.get_setting("paused"), "false")

    async def test_explicit_failed_result_does_not_erase_prior_success(self):
        self.service.handle_update(self.update())
        self.db.outcome(1, "youtube", Outcome("published"))
        self.service.apply_results(
            1,
            ["youtube", "tiktok"],
            [
                {"platform": "youtube", "success": False},
                {"platform": "tiktok", "success": False},
            ],
        )
        states = {row["platform"]: row["state"] for row in self.db.destinations(1)}
        self.assertEqual(states, {"youtube": "published", "tiktok": "failed"})

    async def test_cleanup_only_removes_tracked_inactive_media(self):
        self.service.handle_update(self.update())
        folder = self.root / "bot" / "videos"
        folder.mkdir(parents=True)
        media = folder / "video.mp4"
        media.write_bytes(b"")
        self.db.execute(
            "UPDATE jobs SET local_path=?,media_saved_at=? WHERE id=1",
            (str(media), time.time() - 73 * 3600),
        )
        request_id = self.db.prepare_attempt(1, ["youtube", "tiktok"])
        self.service.cleanup()
        self.assertTrue(media.exists())
        self.db.execute("UPDATE attempts SET state='unknown' WHERE request_id=?", (request_id,))
        self.service.cleanup()
        self.assertFalse(media.exists())
        self.assertTrue((self.root / "jobs.sqlite3").exists())

    async def test_upload_post_only_receives_youtube_with_exact_caption(self):
        self.service.handle_update(self.update())
        declarations = dict.fromkeys(
            (
                "selfDeclaredMadeForKids",
                "containsSyntheticMedia",
                "hasPaidProductPlacement",
                "is_aigc",
                "brand_content_toggle",
                "brand_organic_toggle",
            ),
            False,
        )
        fields = self.service.publisher.upload_fields(
            self.db.job(1), ["youtube"], "unit-test-request", declarations, {}
        )
        self.assertEqual(fields["privacyStatus"], "public")
        self.assertNotIn("privacy_level", fields)
        self.assertNotIn("tiktok_title", fields)
        self.assertEqual(fields["youtube_description"], "River walk")
        self.assertEqual(fields["platform[]"], ["youtube"])
        with self.assertRaises(ValueError):
            self.service.publisher.upload_fields(
                self.db.job(1), ["tiktok"], "unit-test-request", declarations, {}
            )


class MediaTests(unittest.TestCase):
    def test_tiktok_duration_limit_does_not_change_original_video(self):
        video = Video(100, 601, 1920, 1080, 30, "h264", "mov,mp4")
        self.assertIn("duration", video.tiktok_error(600))
        self.assertEqual(video.duration, 601)
        self.assertIsNone(Video(100, 30, 1920, 1080, 30, "h264", "mov,mp4").tiktok_error(600))

    def test_account_specific_duration_limit_is_enforced(self):
        video = Video(100, 120, 1920, 1080, 30, "h264", "mov,mp4")
        self.assertIsNotNone(video.tiktok_error(60))

    def test_cleanup_paths_cannot_escape_media_or_target_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "bot" / "database.sqlite3"
            database.parent.mkdir()
            database.touch()
            with self.assertRaises(MediaError):
                safe_media_path(database, root)
            media_directory = root / "other" / "videos"
            media_directory.mkdir(parents=True)
            link = media_directory / "file.mp4"
            link.symlink_to(database)
            with self.assertRaises(MediaError):
                safe_media_path(link, root)

    @unittest.skipUnless(os.environ.get("MEDIA_TEST_VIDEO"), "Set MEDIA_TEST_VIDEO to a real video")
    def test_ffprobe_on_real_video(self):
        path = Path(os.environ["MEDIA_TEST_VIDEO"]).resolve()
        video = asyncio.run(inspect_video(path, path.stat().st_size, 2_000_000_000))
        self.assertGreater(video.duration, 0)
        self.assertGreater(video.width, 0)
        self.assertGreater(video.frames_per_second, 0)


if __name__ == "__main__":
    unittest.main()
