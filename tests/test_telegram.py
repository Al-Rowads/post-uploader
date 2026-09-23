import asyncio
import hashlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from post_uploader.clients import RemoteError
from post_uploader.database import Database
from post_uploader.media import (
    MediaError,
    MediaStorageError,
    cleanup_telegram_partials,
    inspect_video,
    prepare_telegram_video,
    safe_telegram_video_path,
    telegram_video_directory,
)
from tests.test_review import ReviewFixture


class TelegramWorkflowTests(ReviewFixture):
    async def test_native_video_uses_existing_id_without_downloading(self):
        job_id = self.upload()
        media_id = self.db.destination(job_id, "telegram")["media_id"]
        with patch.object(self.service.telegram, "download", AsyncMock()) as download:
            media = await self.service.review.ensure_telegram_media(media_id)
        download.assert_not_awaited()
        await self.service.telegram.send_media(1, media, "Caption")
        self.assertEqual(
            self.sent[-1],
            (
                "sendVideo",
                {
                    "chat_id": 1,
                    "video": "video-10",
                    "caption": "Caption",
                    "reply_markup": {"inline_keyboard": []},
                },
            ),
        )

    async def test_unprepared_document_cannot_be_sent(self):
        with self.assertRaises(MediaError):
            await self.service.telegram.send_media(
                1, {"kind": "document", "file_id": "document-id"}, "Caption"
            )
        self.assertEqual(self.sent, [])

    async def test_document_response_is_not_cached_or_reported_as_published(self):
        job_id = await self.ready_for_review()
        await self.click(job_id, "telegram", "accept")
        response = {
            "message_id": 55,
            "chat": {"id": -100123},
            "document": {"file_id": "wrong-kind"},
        }
        with patch.object(self.service.telegram, "send_media", AsyncMock(return_value=response)):
            await self.service.submit_telegram(self.db.publication(job_id, "telegram"))
        self.assertEqual(self.db.destination(job_id, "telegram")["state"], "unknown")
        self.assertNotEqual(self.db.media(1)["telegram_video_file_id"], "wrong-kind")
        self.db.retry(job_id)
        self.db.recover()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    async def test_conversion_does_not_publish_a_cancelled_revision(self):
        job_id = await self.ready_for_review()
        await self.click(job_id, "telegram", "accept")

        async def prepare(media_id):
            self.db.cancel(job_id)
            return {"kind": "video", "file_id": "converted"}

        with (
            patch.object(self.service.review, "ensure_telegram_media", prepare),
            patch.object(self.service.telegram, "send_media", AsyncMock()) as send,
            self.assertRaises(ValueError),
        ):
            await self.service.submit_telegram(self.db.publication(job_id, "telegram"))
        send.assert_not_awaited()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

    async def test_conversion_failure_before_submission_creates_no_attempt(self):
        job_id = await self.ready_for_review()
        await self.click(job_id, "telegram", "accept")
        with (
            patch.object(
                self.service.review,
                "ensure_telegram_media",
                AsyncMock(side_effect=MediaError("conversion failed")),
            ),
            self.assertRaises(MediaError),
        ):
            await self.service.submit_telegram(self.db.publication(job_id, "telegram"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

    async def test_preview_changed_during_preparation_is_not_sent(self):
        job_id = await self.ready_for_review()
        self.db.change_review(job_id, "telegram", "review")

        async def prepare(media_id):
            self.db.cancel(job_id)
            return {"kind": "video", "file_id": "converted"}

        row = self.db.execute(
            "SELECT * FROM outbox WHERE done=0 AND kind='preview' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        with (
            patch.object(self.service.review, "ensure_telegram_media", prepare),
            patch.object(self.service.telegram, "send_media", AsyncMock()) as send,
        ):
            self.assertIsNone(await self.service.review.deliver(row))
        send.assert_not_awaited()

    async def test_temporary_storage_failure_leaves_preview_retryable(self):
        job_id = await self.ready_for_review()
        self.db.change_review(job_id, "telegram", "review")
        with (
            patch.object(
                self.service.review,
                "prepare_telegram_media",
                AsyncMock(side_effect=MediaStorageError("disk reserve")),
            ),
            self.assertRaises(RemoteError) as raised,
        ):
            await self.drain()
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(self.db.destination(job_id, "telegram")["state"], "review")
        self.assertFalse(self.service.review.preparing_telegram_media)

    async def test_v4_migration_preserves_approvals_media_and_attempt_snapshots(self):
        job_id = await self.ready_for_review()
        await self.click(job_id, "youtube", "accept")
        self.db.prepare_attempt(job_id, ["youtube"], "youtube")
        before = dict(self.db.execute("SELECT * FROM attempts").fetchone())
        self.db.execute("ALTER TABLE media_assets DROP COLUMN telegram_video_path")
        self.db.execute("ALTER TABLE media_assets DROP COLUMN telegram_video_file_id")
        self.db.execute("PRAGMA user_version = 4")
        self.db.connection.close()
        self.db = Database(self.db_path)
        self.service.db = self.service.review.db = self.db
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(dict(self.db.execute("SELECT * FROM attempts").fetchone()), before)
        self.assertEqual(self.db.destination(job_id, "youtube")["approved"], 1)
        media = self.db.media(1)
        self.assertEqual((media["kind"], media["file_id"]), ("video", "video-10"))
        self.assertIsNone(media["telegram_video_path"])
        self.assertIsNone(media["telegram_video_file_id"])


class TelegramPathTests(unittest.TestCase):
    def test_conversion_directory_and_files_reject_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "elsewhere"
            outside.mkdir()
            (root / "post-uploader").symlink_to(outside)
            with self.assertRaises(MediaError):
                telegram_video_directory(root)
            (root / "post-uploader").unlink()
            directory = telegram_video_directory(root)
            secret = outside / "database.sqlite"
            secret.touch()
            link = directory / "1.mp4"
            link.symlink_to(secret)
            with self.assertRaises(MediaError):
                safe_telegram_video_path(link, root)
            self.assertTrue(secret.exists())

    def test_startup_removes_only_owned_partial_conversions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = telegram_video_directory(root)
            for name in ("1.partial.mp4", "1.mp4", "unrelated.partial.mp4"):
                (directory / name).touch()
            cleanup_telegram_partials(root)
            self.assertEqual(
                sorted(p.name for p in directory.iterdir()), ["1.mp4", "unrelated.partial.mp4"]
            )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Install FFmpeg tools")
class TelegramConversionTests(ReviewFixture):
    async def ffmpeg(self, *arguments):
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        self.assertEqual(process.returncode, 0, stderr.decode())
        return stdout

    async def source(
        self,
        name="source.mp4",
        *,
        codec="libx264",
        audio="aac",
        pixel_format="yuv420p",
        dimensions="64x96",
    ):
        directory = self.root / "bot" / "documents"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        arguments = ["-f", "lavfi", "-i", f"testsrc=size={dimensions}:rate=24:duration=1.25"]
        if audio:
            arguments += [
                "-f",
                "lavfi",
                "-i",
                "sine=sample_rate=48000:duration=1.25",
                "-c:a",
                audio,
            ]
        arguments += [
            "-c:v",
            codec,
            "-threads",
            "2",
            "-filter_threads",
            "1",
            "-pix_fmt",
            pixel_format,
            str(path),
        ]
        await self.ffmpeg(*arguments)
        return path

    async def convert(self, source, name="1.mp4", **limits):
        target = telegram_video_directory(self.root) / name
        video = await inspect_video(source, source.stat().st_size, self.config.max_video_bytes)
        prepared = await prepare_telegram_video(
            source,
            video,
            target,
            max_size=limits.get("max_size", 2_000_000_000),
            disk_reserve=limits.get("disk_reserve", 0),
        )
        return target, prepared

    def document_job(self, source):
        job_id = self.upload(document=True)
        media_id = self.db.destination(job_id, "telegram")["media_id"]
        self.db.execute(
            "UPDATE media_assets SET local_path=?,file_size=?,saved_at=? WHERE id=?",
            (str(source), source.stat().st_size, time.time(), media_id),
        )
        self.db.execute(
            "UPDATE jobs SET title='Document video',state='reviewing' WHERE id=?", (job_id,)
        )
        self.db.execute(
            "UPDATE destinations SET caption='Approved caption',state='review' WHERE job_id=?",
            (job_id,),
        )
        return job_id, media_id

    async def test_mp4_remux_preserves_video_audio_and_enables_streaming(self):
        source = await self.source()
        original = hashlib.sha256(source.read_bytes()).digest()
        target, video = await self.convert(source)
        self.assertIsNone(video.telegram_error())
        content = target.read_bytes()
        self.assertLess(content.index(b"moov"), content.index(b"mdat"))
        for stream in ("0:v:0", "0:a:0"):
            before = await self.ffmpeg("-i", str(source), "-map", stream, "-f", "hash", "-")
            after = await self.ffmpeg("-i", str(target), "-map", stream, "-f", "hash", "-")
            self.assertEqual(before, after)
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), original)

    async def test_incompatible_streams_odd_dimensions_and_silent_video(self):
        for audio in ("pcm_s16le", None):
            with self.subTest(audio=audio):
                source = await self.source(
                    "input.mkv",
                    codec="ffv1",
                    audio=audio,
                    pixel_format="yuv444p",
                    dimensions="65x97",
                )
                _, prepared = await self.convert(source)
                self.assertIsNone(prepared.telegram_error())
                self.assertEqual((prepared.width, prepared.height), (66, 98))
                self.assertEqual(prepared.audio_codec, "aac" if audio else None)
                self.assertAlmostEqual(prepared.duration, 1.25, delta=0.1)

    async def test_rotation_is_preserved_for_remux_and_transcode(self):
        help_text = await self.ffmpeg("-h", "full")
        for codec in ("libx264", "mpeg4"):
            with self.subTest(codec=codec):
                source = await self.source(codec=codec, audio=None)
                rotated = source.with_name("rotated.mp4")
                # FFmpeg 6+ uses an input display matrix; Debian Bookworm ships FFmpeg 5.
                if b"-display_rotation" in help_text:
                    await self.ffmpeg(
                        "-display_rotation:v:0", "90", "-i", str(source), "-c", "copy", str(rotated)
                    )
                else:
                    await self.ffmpeg(
                        "-i",
                        str(source),
                        "-c",
                        "copy",
                        "-metadata:s:v:0",
                        "rotate=90",
                        str(rotated),
                    )
                original = await inspect_video(rotated, rotated.stat().st_size, 2_000_000_000)
                self.assertEqual(original.rotation % 180, 90)
                _, prepared = await self.convert(rotated)
                parameters = prepared.telegram_parameters()
                self.assertEqual((parameters["width"], parameters["height"]), (96, 64))

    async def test_previews_edits_and_publication_share_one_conversion(self):
        source = await self.source("unusual-name.mkv", codec="ffv1", audio="pcm_s16le")
        job_id, media_id = self.document_job(source)
        with patch(
            "post_uploader.review.prepare_telegram_video", wraps=prepare_telegram_video
        ) as convert:
            for platform in ("youtube", "tiktok", "telegram"):
                self.db.preview(job_id, platform)
            await self.drain()
            await self.click(job_id, "telegram", "accept")
            await self.drain()
            await self.service.submit_telegram(self.db.publication(job_id, "telegram"))
            self.assertEqual(convert.await_count, 1)
        media = self.db.media(media_id)
        self.assertEqual(media["kind"], "document")
        self.assertEqual(media["file_id"], "video-10")
        self.assertEqual(media["local_path"], str(source))
        self.assertEqual(media["telegram_video_file_id"], "converted-video")
        uploads = [
            values
            for method, values in self.sent
            if method == "sendVideo" and values["video"].startswith("file:")
        ]
        self.assertEqual(len(uploads), 1)
        self.assertTrue(uploads[0]["supports_streaming"])
        self.assertEqual((uploads[0]["width"], uploads[0]["height"]), (64, 96))
        self.assertFalse(any(method == "sendDocument" for method, _ in self.sent))
        edit = next(values for method, values in self.sent if method == "editMessageMedia")
        self.assertEqual(edit["media"]["type"], "video")
        self.assertEqual(edit["media"]["media"], "converted-video")
        self.assertEqual(self.db.destination(job_id, "telegram")["state"], "published")
        snapshot = json.loads(self.db.execute("SELECT snapshot FROM attempts").fetchone()[0])
        self.assertEqual(snapshot["file_id"], "video-10")
        self.assertEqual(snapshot["telegram_media"]["file_id"], "converted-video")
        self.assertEqual(self.sent[-1][1]["caption"], "Approved caption")

    async def test_prepared_copy_survives_restart_and_missing_copy_is_regenerated(self):
        source = await self.source()
        _, media_id = self.document_job(source)
        first = await self.service.review.ensure_telegram_media(media_id)
        self.db.connection.close()
        self.db = Database(self.db_path)
        self.service.db = self.service.review.db = self.db
        with patch(
            "post_uploader.review.prepare_telegram_video", wraps=prepare_telegram_video
        ) as convert:
            self.assertEqual(await self.service.review.ensure_telegram_media(media_id), first)
            convert.assert_not_awaited()
            Path(first["local_path"]).unlink()
            await self.service.review.ensure_telegram_media(media_id)
            convert.assert_awaited_once()

    async def test_existing_document_preview_is_edited_into_video_using_local_upload(self):
        source = await self.source()
        job_id, _ = self.document_job(source)
        self.db.execute("UPDATE destinations SET preview_message_id=42 WHERE platform='telegram'")
        self.db.preview(job_id, "telegram")
        await self.drain()
        edit = next(values for method, values in self.sent if method == "editMessageMedia")
        self.assertEqual(edit["message_id"], 42)
        self.assertEqual(edit["media"]["type"], "video")
        self.assertTrue(edit["media"]["media"].startswith("file:"))
        self.assertTrue(edit["media"]["supports_streaming"])
        self.assertEqual(edit["reply_markup"]["inline_keyboard"][0][0]["text"], "Accept")

    async def test_replacement_document_changes_only_selected_destination(self):
        job_id = await self.ready_for_review()
        original = self.db.destination(job_id, "youtube")["media_id"]
        await self.click(job_id, "telegram", "reject")
        await self.drain()
        await self.click(job_id, "telegram", "video")
        await self.drain()
        prompt = self.db.execute(
            "SELECT * FROM outbox WHERE kind='prompt' AND "
            "platform='telegram' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        source = await self.source("replacement.mkv", codec="ffv1", audio="pcm_s16le")
        self.service.handle_update(
            {
                "message": self.incoming(
                    message_id=300,
                    reply_to_message={"message_id": prompt["sent_message_id"]},
                    document={
                        "file_id": "replacement-document",
                        "file_unique_id": "replacement",
                        "file_size": source.stat().st_size,
                        "file_name": source.name,
                    },
                )
            }
        )
        replacement = self.db.destination(job_id, "telegram")
        self.db.execute(
            "UPDATE media_assets SET local_path=? WHERE id=?",
            (str(source), replacement["media_id"]),
        )
        await self.service.review.validate_replacement(replacement)
        self.assertEqual(self.db.destination(job_id, "telegram")["state"], "ready")
        await self.drain()
        self.assertNotEqual(original, replacement["media_id"])
        for platform in ("youtube", "tiktok"):
            self.assertEqual(self.db.destination(job_id, platform)["media_id"], original)
        edit = [values for method, values in self.sent if method == "editMessageMedia"][-1]
        self.assertEqual(edit["media"]["type"], "video")
        self.assertTrue(edit["media"]["media"].startswith("file:"))

    async def test_audio_conversion_keeps_compatible_video_unchanged(self):
        source = await self.source("audio.mkv", audio="pcm_s16le")
        target, prepared = await self.convert(source)
        self.assertEqual(prepared.audio_codec, "aac")
        before = await self.ffmpeg("-i", str(source), "-map", "0:v:0", "-f", "hash", "-")
        after = await self.ffmpeg("-i", str(target), "-map", "0:v:0", "-f", "hash", "-")
        self.assertEqual(before, after)

    async def test_transcoding_preserves_non_square_pixel_aspect_ratio(self):
        source = await self.source(audio=None)
        anamorphic = source.with_name("anamorphic.mp4")
        await self.ffmpeg("-i", str(source), "-vf", "setsar=2/1", "-c:v", "mpeg4", str(anamorphic))
        _, prepared = await self.convert(anamorphic)
        self.assertEqual(prepared.sample_aspect_ratio, 2)
        parameters = prepared.telegram_parameters()
        self.assertEqual((parameters["width"], parameters["height"]), (128, 96))

    async def test_failed_encoder_process_leaves_no_final_or_partial_copy(self):
        source = await self.source()
        video = await inspect_video(source, source.stat().st_size, 2_000_000_000)
        source.write_bytes(source.read_bytes()[:20])
        directory = telegram_video_directory(self.root)
        with self.assertRaisesRegex(MediaError, "convert"):
            await prepare_telegram_video(
                source, video, directory / "1.mp4", max_size=2_000_000_000, disk_reserve=0
            )
        self.assertEqual(list(directory.iterdir()), [])

    async def test_invalid_content_is_rejected_without_a_conversion(self):
        source = await self.source()
        source.write_bytes(source.read_bytes()[:20])
        _, media_id = self.document_job(source)
        with self.assertRaises(MediaError):
            await self.service.review.ensure_telegram_media(media_id)
        self.assertIsNone(self.db.media(media_id)["telegram_video_path"])

    async def test_concurrent_preparations_convert_once(self):
        source = await self.source()
        _, media_id = self.document_job(source)
        with patch(
            "post_uploader.review.prepare_telegram_video", wraps=prepare_telegram_video
        ) as convert:
            first, second = await asyncio.gather(
                self.service.review.ensure_telegram_media(media_id),
                self.service.review.ensure_telegram_media(media_id),
            )
            self.assertEqual(first, second)
            convert.assert_awaited_once()

    async def test_uncertain_publication_protects_copy_and_never_resends(self):
        source = await self.source()
        job_id, media_id = self.document_job(source)
        await self.click(job_id, "telegram", "accept")
        with patch.object(
            self.service.telegram, "send_media", AsyncMock(side_effect=httpx.ReadTimeout("lost"))
        ):
            await self.service.submit_telegram(self.db.publication(job_id, "telegram"))
        copy = Path(self.db.media(media_id)["telegram_video_path"])
        self.db.execute("UPDATE media_assets SET saved_at=0")
        self.db.execute("UPDATE jobs SET state='settled'")
        self.db.execute("UPDATE destinations SET state='cancelled' WHERE platform!='telegram'")
        self.service.cleanup()
        self.assertTrue(copy.exists())
        self.assertTrue(source.exists())
        self.db.retry(job_id)
        self.db.recover()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        self.assertEqual(self.db.destination(job_id, "telegram")["state"], "unknown")

    async def test_cleanup_protects_review_and_removes_settled_copies_but_keeps_video_id(self):
        source = await self.source()
        job_id, media_id = self.document_job(source)
        media = await self.service.review.ensure_telegram_media(media_id)
        copy = Path(media["local_path"])
        self.db.execute("UPDATE media_assets SET saved_at=0,telegram_video_file_id='cached'")
        self.service.cleanup()
        self.assertTrue(copy.exists())
        self.db.cancel(job_id)
        self.service.cleanup()
        self.assertFalse(copy.exists())
        self.assertFalse(source.exists())
        self.assertIsNone(self.db.media(media_id)["telegram_video_path"])
        self.assertEqual(
            await self.service.review.ensure_telegram_media(media_id),
            {"kind": "video", "file_id": "cached"},
        )

    async def test_in_progress_conversion_is_protected_when_job_is_cancelled(self):
        source = await self.source()
        job_id, media_id = self.document_job(source)
        media = await self.service.review.ensure_telegram_media(media_id)
        copy = Path(media["local_path"])
        self.db.cancel(job_id)
        self.db.execute("UPDATE media_assets SET saved_at=0")
        self.service.review.preparing_telegram_media.add(media_id)
        self.service.cleanup()
        self.assertTrue(source.exists())
        self.assertTrue(copy.exists())
        self.service.review.preparing_telegram_media.clear()
        self.service.cleanup()
        self.assertFalse(copy.exists())

    async def test_size_disk_timeout_and_cancellation_leave_no_partial_video(self):
        source = await self.source()
        directory = telegram_video_directory(self.root)
        with self.assertRaisesRegex(MediaError, "size"):
            await self.convert(source, max_size=1)
        with self.assertRaises(MediaStorageError):
            await self.convert(source, disk_reserve=shutil.disk_usage(self.root).total + 1)
        with patch("post_uploader.media.CONVERSION_TIMEOUT", 0), self.assertRaises(MediaError):
            await self.convert(source)
        self.assertEqual(list(directory.iterdir()), [])
        started = asyncio.Event()
        real_subprocess = asyncio.create_subprocess_exec

        async def start_process(*arguments, **options):
            process = await real_subprocess(*arguments, **options)
            started.set()
            return process

        with patch("post_uploader.media.asyncio.create_subprocess_exec", start_process):
            video = await inspect_video(source, source.stat().st_size, 2_000_000_000)
            started.clear()
            conversion = asyncio.create_task(
                prepare_telegram_video(
                    source, video, directory / "1.mp4", max_size=2_000_000_000, disk_reserve=0
                )
            )
            await started.wait()
            conversion.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await conversion
        self.assertEqual(list(directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
