import hashlib
import hmac
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import httpx
from aiohttp.test_utils import TestClient, TestServer

from post_uploader.clients import RemoteError
from post_uploader.config import Config
from post_uploader.database import Database
from post_uploader.domain import review_caption, text_length, validate_platform_caption
from post_uploader.media import MediaError, Video
from post_uploader.openrouter import OpenRouter
from post_uploader.service import Service
from post_uploader.tiktok import direct_outcome, direct_post_info
from post_uploader.web import validate_init_data

DECLARATIONS = {
    "selfDeclaredMadeForKids": False,
    "containsSyntheticMedia": False,
    "hasPaidProductPlacement": False,
}
CREATOR = {
    "creator_nickname": "Test creator",
    "creator_username": "testcreator",
    "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
    "comment_disabled": False,
    "duet_disabled": True,
    "stitch_disabled": False,
    "max_video_post_duration_sec": 180,
}


def form_values(**overrides):
    return {
        "revision": 0,
        "caption": "Approved TikTok caption",
        "privacy_level": "SELF_ONLY",
        "allow_comment": False,
        "allow_duet": False,
        "allow_stitch": False,
        "commercial_content": False,
        "brand_organic_toggle": False,
        "brand_content_toggle": False,
        "is_aigc": False,
        "consent": True,
        **overrides,
    }


def signed_init_data(token, user=1, timestamp=None):
    fields = {
        "user": json.dumps({"id": user}),
        "auth_date": str(int(time.time()) if timestamp is None else timestamp),
    }
    secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
    fields["hash"] = hmac.new(
        secret, "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


class ReviewFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db_path = self.root / "jobs.sqlite3"
        self.db = Database(self.db_path)
        self.config = Config(
            telegram_token="1:test-token",
            owner_id=1,
            upload_post_key="",
            profile="",
            declarations=DECLARATIONS,
            telegram_files=self.root,
            data_directory=self.root,
            openrouter_key="test-openrouter-key",
            public_site_url="https://publisher.example",
            tiktok_direct_mode="private_test",
            tiktok_media_verified=True,
        )
        self.service = Service(self.config, self.db)
        self.db.set_setting("active_platforms", '["youtube","tiktok","telegram"]')
        self.db.set_setting("telegram_channel", '{"chat_id":-100123,"username":"testchannel"}')
        self.sent = []
        self.sequence = 100

        async def telegram_response(request):
            method = request.url.path.split("/")[-1]
            values = json.loads(request.content)
            self.sent.append((method, values))
            if method == "answerCallbackQuery":
                result = True
            elif method == "getMe":
                result = {"id": 123, "username": "testbot"}
            elif method == "getChat":
                result = {"id": -100123, "type": "channel", "title": "Test channel"}
            elif method == "getChatMember":
                result = {"status": "administrator", "can_post_messages": True}
            elif method in {"sendMessage", "sendVideo", "sendDocument", "editMessageMedia"}:
                self.sequence += 1
                result = {
                    "message_id": values.get("message_id", self.sequence),
                    "chat": {"id": values["chat_id"]},
                }
            else:
                raise AssertionError(f"Unexpected Telegram request: {method}")
            return httpx.Response(200, json={"ok": True, "result": result})

        await self.service.telegram.client.aclose()
        self.service.telegram.client = httpx.AsyncClient(
            base_url="https://telegram.example/bot1:test-token/",
            transport=httpx.MockTransport(telegram_response),
        )

    async def asyncTearDown(self):
        await self.service.close()
        self.db.connection.close()
        self.directory.cleanup()

    def incoming(self, **fields):
        return {"from": {"id": 1}, "chat": {"id": 1, "type": "private"}, "message_id": 10, **fields}

    def upload(self, message_id=10, *, document=False):
        media = {
            "file_id": f"video-{message_id}",
            "file_unique_id": f"unique-{message_id}",
            "file_size": 100,
            "file_name": "video.mp4",
        }
        message = self.incoming(
            message_id=message_id, **{"document" if document else "video": media}
        )
        self.service.handle_update({"message": message})
        return self.db.execute("SELECT id FROM jobs WHERE message_id=?", (message_id,)).fetchone()[
            0
        ]

    async def drain(self):
        for _ in range(30):
            row = self.db.execute(
                "SELECT * FROM outbox WHERE done=0 ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return
            result = await self.service.review.deliver(row)
            self.db.execute(
                "UPDATE outbox SET done=1,sent_message_id=? WHERE id=?",
                (result["message_id"] if result else None, row["id"]),
            )
        self.fail("Outbox did not drain")

    def reply(self, job_id, text="", platform=None, media=None):
        prompt = self.db.execute(
            "SELECT * FROM outbox WHERE job_id=? AND kind='prompt' "
            "AND platform IS ? ORDER BY id DESC LIMIT 1",
            (job_id, platform),
        ).fetchone()
        message = self.incoming(
            message_id=200, text=text, reply_to_message={"message_id": prompt["sent_message_id"]}
        )
        if media:
            message["video"] = media
        self.service.handle_update({"message": message})

    async def ready_for_review(self):
        job_id = self.upload()
        await self.drain()
        self.reply(job_id, "A river walk")
        await self.drain()
        self.reply(job_id, "Walking beside the river this morning.")
        with patch.object(
            self.service.openrouter,
            "generate_captions",
            AsyncMock(
                return_value={
                    "youtube": "YouTube description",
                    "tiktok": "TikTok caption #river",
                    "telegram": "Telegram channel caption",
                }
            ),
        ) as generate:
            await self.service.review.tick()
            generate.assert_awaited_once()
        await self.drain()
        return job_id

    async def click(self, job_id, platform, action, *, revision=None, user=1):
        destination = self.db.destination(job_id, platform)
        revision = destination["revision"] if revision is None else revision
        await self.service.review.callback(
            {
                "id": "callback-test",
                "from": {"id": user},
                "message": {
                    "chat": {"id": 1, "type": "private"},
                    "message_id": destination["preview_message_id"],
                },
                "data": f"review:{job_id}:{platform}:{revision}:{action}",
            }
        )


class ReviewTests(ReviewFixture):
    async def test_three_independent_previews_and_explicit_intake(self):
        job_id = await self.ready_for_review()
        previews = [parameters for method, parameters in self.sent if method == "sendVideo"]
        self.assertEqual(len(previews), 3)
        self.assertEqual({p["video"] for p in previews}, {"video-10"})
        for preview in previews:
            self.assertEqual(
                [b["text"] for b in preview["reply_markup"]["inline_keyboard"][0]],
                ["Accept", "Reject"],
            )
        self.assertIn(
            "YouTube description",
            previews[0]["caption"] + previews[1]["caption"] + previews[2]["caption"],
        )
        self.assertEqual(self.db.job(job_id)["title"], "A river walk")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

    async def test_duplicate_or_unauthorized_accept_does_not_resubmit(self):
        job_id = await self.ready_for_review()
        await self.click(job_id, "youtube", "accept", user=99)
        self.assertEqual(self.db.destination(job_id, "youtube")["state"], "review")
        await self.click(job_id, "youtube", "accept")
        revision = self.db.destination(job_id, "youtube")["revision"]
        self.db.prepare_attempt(job_id, ["youtube"], "youtube")
        await self.click(job_id, "youtube", "accept", revision=revision - 1)
        self.assertEqual(self.db.destination(job_id, "youtube")["state"], "pending")
        self.assertEqual(self.db.destination(job_id, "telegram")["state"], "review")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    async def test_unconfigured_tiktok_review_retains_reject_and_reports_setup_on_accept(self):
        self.service.review.config = replace(
            self.config, tiktok_direct_mode="disabled", public_site_url=""
        )
        job_id = await self.ready_for_review()
        destination = self.db.destination(job_id, "tiktok")
        buttons = self.service.review.keyboard(job_id, destination)["inline_keyboard"][0]
        self.assertEqual([button["text"] for button in buttons], ["Accept", "Reject"])
        self.assertIn("callback_data", buttons[0])
        await self.click(job_id, "tiktok", "accept")
        self.assertEqual(self.db.destination(job_id, "tiktok")["state"], "review")
        self.assertIn(
            "TIKTOK_DIRECT_MODE",
            self.db.execute("SELECT body FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0],
        )
        await self.click(job_id, "tiktok", "reject")
        self.assertEqual(self.db.destination(job_id, "tiktok")["state"], "rejecting")

    async def test_replacement_caption_is_exact_and_only_approves_one_platform(self):
        job_id = await self.ready_for_review()
        await self.click(job_id, "telegram", "reject")
        await self.drain()
        edit = [p for m, p in self.sent if m == "editMessageMedia"][-1]
        self.assertEqual(
            [b["text"] for b in edit["reply_markup"]["inline_keyboard"][0]],
            ["Reject caption", "Reject video"],
        )
        await self.click(job_id, "telegram", "caption")
        await self.drain()
        caption = "  نص عربي\nAn exact replacement. #river  "
        self.reply(job_id, caption, "telegram")
        destination = self.db.destination(job_id, "telegram")
        self.assertEqual(destination["caption"], caption)
        self.assertEqual(destination["state"], "ready")
        self.assertEqual(self.db.destination(job_id, "youtube")["caption"], "YouTube description")
        self.assertEqual(self.db.destination(job_id, "youtube")["state"], "review")
        self.assertEqual(self.db.job(job_id)["caption"], "Walking beside the river this morning.")

    async def test_replacement_video_keeps_other_media_and_attempt_snapshot(self):
        job_id = await self.ready_for_review()
        await self.click(job_id, "youtube", "accept")
        request = self.db.prepare_attempt(job_id, ["youtube"], "youtube")
        old_snapshot = self.db.execute(
            "SELECT snapshot FROM attempts WHERE request_id=?", (request,)
        ).fetchone()[0]
        original_media = self.db.destination(job_id, "youtube")["media_id"]
        await self.click(job_id, "telegram", "reject")
        await self.drain()
        await self.click(job_id, "telegram", "video")
        await self.drain()
        self.reply(
            job_id,
            platform="telegram",
            media={"file_id": "replacement", "file_unique_id": "replacement", "file_size": 120},
        )
        destination = self.db.destination(job_id, "telegram")
        self.assertNotEqual(destination["media_id"], original_media)
        with patch.object(
            self.service.review,
            "ensure_media",
            AsyncMock(return_value=(Path("unused"), Video(120, 30, 1080, 1920, 30, "h264", "mp4"))),
        ):
            await self.service.review.validate_replacement(destination)
        self.assertEqual(self.db.destination(job_id, "telegram")["state"], "ready")
        self.assertEqual(self.db.destination(job_id, "tiktok")["media_id"], original_media)
        self.assertEqual(
            self.db.execute(
                "SELECT snapshot FROM attempts WHERE request_id=?", (request,)
            ).fetchone()[0],
            old_snapshot,
        )
        self.assertEqual(self.db.job(job_id)["file_id"], "video-10")

    async def test_interleaved_jobs_require_targeted_replies_and_ignore_old_prompt(self):
        first, second = self.upload(10), self.upload(11)
        await self.drain()
        self.service.handle_update(
            {"message": self.incoming(message_id=12, text="Ambiguous title")}
        )
        self.assertEqual(self.db.job(first)["state"], "waiting_title")
        self.assertEqual(self.db.job(second)["state"], "waiting_title")
        old_prompt = self.db.execute(
            "SELECT sent_message_id FROM outbox WHERE job_id=? AND kind='prompt'", (second,)
        ).fetchone()[0]
        self.reply(second, "Second title")
        self.service.handle_update(
            {
                "message": self.incoming(
                    text="Not a master caption", reply_to_message={"message_id": old_prompt}
                )
            }
        )
        self.assertEqual(self.db.job(second)["state"], "waiting_master_caption")
        self.assertEqual(self.db.job(second)["caption"], "")

    async def test_platform_changes_apply_to_future_jobs_only(self):
        first = self.upload()
        self.service.command(1, "/platforms")
        await self.drain()
        menu = self.db.execute("SELECT * FROM outbox WHERE kind='menu'").fetchone()
        callback = {
            "id": "toggle",
            "from": {"id": 1},
            "data": "platform:tiktok:0",
            "message": {
                "chat": {"id": 1, "type": "private"},
                "message_id": menu["sent_message_id"],
            },
        }
        await self.service.review.callback(callback)
        await self.service.review.callback(callback)
        second = self.upload(11)
        self.assertEqual(len(self.db.destinations(first)), 3)
        self.assertEqual(
            {d["platform"] for d in self.db.destinations(second)}, {"youtube", "telegram"}
        )

    async def test_no_platforms_does_not_create_job(self):
        self.db.set_setting("active_platforms", "[]")
        self.service.handle_update(
            {
                "message": self.incoming(
                    video={"file_id": "f", "file_unique_id": "u", "file_size": 10}
                )
            }
        )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    async def test_generation_failure_is_bounded_and_retry_does_not_publish(self):
        job_id = self.upload()
        self.db.execute("UPDATE jobs SET state='captions_queued',title='Title',caption='Master'")
        with patch.object(
            self.service.openrouter,
            "generate_captions",
            AsyncMock(side_effect=RemoteError("Unavailable")),
        ):
            for _ in range(3):
                await self.service.review.generate(self.db.job(job_id))
        self.assertEqual(self.db.job(job_id)["state"], "caption_failed")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.db.retry(job_id)
        self.assertEqual(self.db.job(job_id)["state"], "captions_queued")

    async def test_restart_keeps_reviews_and_does_not_regenerate_captions(self):
        job_id = await self.ready_for_review()
        initial_outbox = self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.db.recover()
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], initial_outbox
        )
        with patch.object(self.service.openrouter, "generate_captions", AsyncMock()) as generate:
            await self.service.review.tick()
            generate.assert_not_awaited()
        self.assertEqual(self.db.job(job_id)["state"], "reviewing")

    async def test_pause_allows_review_but_does_not_start_attempt(self):
        job_id = await self.ready_for_review()
        self.service.command(1, "/pause")
        await self.click(job_id, "telegram", "accept")
        await self.service.prepare(self.db.job(job_id))
        self.assertEqual(self.db.destination(job_id, "telegram")["state"], "ready")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

    async def test_channel_delivery_is_confirmed_and_uses_only_approved_caption(self):
        job_id = await self.ready_for_review()
        await self.click(job_id, "telegram", "accept")
        publication = self.db.publication(job_id, "telegram")
        await self.service.submit_telegram(publication)
        result = self.db.destination(job_id, "telegram")
        self.assertEqual(result["state"], "published")
        self.assertTrue(result["url"].startswith("https://t.me/testchannel/"))
        send = self.sent[-1][1]
        self.assertEqual(send["chat_id"], -100123)
        self.assertEqual(send["caption"], publication["caption"])
        self.assertEqual(send["reply_markup"]["inline_keyboard"], [])

    async def test_uncertain_telegram_send_never_retries_submission(self):
        job_id = await self.ready_for_review()
        await self.click(job_id, "telegram", "accept")
        with patch.object(
            self.service.telegram, "send_media", AsyncMock(side_effect=httpx.ReadTimeout("lost"))
        ):
            await self.service.submit_telegram(self.db.publication(job_id, "telegram"))
        self.assertEqual(self.db.destination(job_id, "telegram")["state"], "unknown")
        self.db.retry(job_id)
        self.db.recover()
        self.assertEqual(self.db.execute("SELECT state FROM attempts").fetchone()[0], "unknown")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    async def test_unknown_post_does_not_block_review_of_another_failed_destination(self):
        job_id = await self.ready_for_review()
        await self.click(job_id, "telegram", "accept")
        with patch.object(
            self.service.telegram, "send_media", AsyncMock(side_effect=httpx.ReadTimeout("lost"))
        ):
            await self.service.submit_telegram(self.db.publication(job_id, "telegram"))
        self.db.execute("UPDATE destinations SET state='failed' WHERE platform='youtube'")
        result = self.db.retry(job_id)
        self.assertIn("returned to review", result)
        self.assertEqual(self.db.destination(job_id, "youtube")["state"], "review")
        self.assertEqual(self.db.destination(job_id, "youtube")["approved"], 0)
        self.assertEqual(self.db.destination(job_id, "telegram")["state"], "unknown")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    async def test_obsolete_approved_snapshot_cannot_start_an_attempt(self):
        job_id = await self.ready_for_review()
        await self.click(job_id, "youtube", "accept")
        obsolete = self.db.publication(job_id, "youtube")
        self.db.change_review(job_id, "youtube", "ready")
        with self.assertRaises(ValueError):
            self.db.prepare_attempt(job_id, ["youtube"], "youtube", obsolete)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

    async def test_unreadable_legacy_preview_requests_replacement_once(self):
        job_id = await self.ready_for_review()
        self.db.execute("UPDATE media_assets SET kind='unknown'")
        self.db.change_review(job_id, "youtube", "review")
        with patch.object(
            self.service.review, "ensure_media", AsyncMock(side_effect=MediaError("unreadable"))
        ) as ensure:
            await self.drain()
            ensure.assert_awaited_once()
        self.assertEqual(self.db.destination(job_id, "youtube")["state"], "waiting_video")
        self.assertEqual(self.db.destination(job_id, "telegram")["state"], "review")
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM outbox WHERE kind='prompt' AND platform='youtube'"
            ).fetchone()[0],
            1,
        )

    async def test_video_documents_use_document_method(self):
        self.db.set_setting("active_platforms", '["telegram"]')
        job_id = self.upload(document=True)
        self.db.execute("UPDATE jobs SET title='Document',state='reviewing'")
        self.db.execute("UPDATE destinations SET caption='Document caption',state='review'")
        self.db.preview(job_id, "telegram")
        await self.drain()
        self.assertTrue(any(method == "sendDocument" for method, _ in self.sent))
        self.assertFalse(any(method == "sendVideo" for method, _ in self.sent))

    async def test_unknown_tiktok_initialization_does_not_fall_back_to_inbox(self):
        job_id = await self.ready_for_review()
        self.db.execute(
            "UPDATE destinations SET state='ready',approved=1,settings=? WHERE platform='tiktok'",
            (json.dumps({"post_info": {"privacy_level": "SELF_ONLY"}}),),
        )
        with (
            patch.object(
                self.service.tiktok,
                "initialize_direct",
                AsyncMock(side_effect=httpx.ReadTimeout("lost")),
            ),
            patch.object(self.service.tiktok, "initialize", AsyncMock()) as inbox,
        ):
            await self.service.submit_tiktok_direct(self.db.publication(job_id, "tiktok"))
            attempt = self.db.execute("SELECT * FROM attempts").fetchone()
            await self.service.reconcile_tiktok(attempt)
            inbox.assert_not_awaited()
        self.assertEqual(self.db.destination(job_id, "tiktok")["state"], "unknown")
        self.db.retry(job_id)
        self.assertEqual(self.db.execute("SELECT state FROM attempts").fetchone()[0], "unknown")

    async def test_direct_post_uses_publish_scope_pull_url_and_saved_id_for_status(self):
        requests = []

        def transport(request):
            self.assertEqual(request.headers["Authorization"], "Bearer protocol-test-token")
            requests.append((request.url.path, json.loads(request.content)))
            responses = {
                "/v2/post/publish/creator_info/query/": CREATOR,
                "/v2/post/publish/video/init/": {"publish_id": "direct-id"},
                "/v2/post/publish/status/fetch/": {"status": "PROCESSING_UPLOAD"},
            }
            return httpx.Response(
                200, json={"data": responses[request.url.path], "error": {"code": "ok"}}
            )

        await self.service.tiktok.client.aclose()
        self.service.tiktok.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        with patch.object(
            self.service.tiktok,
            "credentials",
            AsyncMock(return_value={"access_token": "protocol-test-token"}),
        ) as credentials:
            creator = await self.service.tiktok.creator_info()
            post_info = direct_post_info(form_values(), creator, "private_test")
            result = await self.service.tiktok.initialize_direct(
                post_info, "https://publisher.example/publisher/media/capability"
            )
            await self.service.tiktok.status(result["publish_id"], direct=True)
        self.assertEqual(
            [call.args for call in credentials.await_args_list], [("video.publish",)] * 3
        )
        self.assertEqual(requests[1][1]["post_info"], post_info)
        self.assertEqual(
            requests[1][1]["source_info"],
            {
                "source": "PULL_FROM_URL",
                "video_url": "https://publisher.example/publisher/media/capability",
            },
        )
        self.assertEqual(requests[2][1], {"publish_id": "direct-id"})

    async def test_draft_only_credentials_cannot_initialize_direct_post(self):
        self.service.tiktok.path = self.root / "tiktok-credentials.json"
        self.service.tiktok.path.write_text(json.dumps({"scope": "video.upload"}))
        with (
            patch.object(self.service.tiktok.client, "post", AsyncMock()) as send,
            self.assertRaises(RemoteError),
        ):
            await self.service.tiktok.initialize_direct({}, "https://publisher.example/video")
        send.assert_not_awaited()


class OpenRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = Config("1:token", 1, "", "", {}, openrouter_key="test-key")
        self.router = OpenRouter(self.config)
        self.requests = []

    async def asyncTearDown(self):
        await self.router.client.aclose()

    async def response(self, payload, status=200):
        await self.router.client.aclose()

        def transport(request):
            self.requests.append(request)
            return httpx.Response(status, json=payload)

        self.router.client = httpx.AsyncClient(
            base_url="https://openrouter.ai/api/v1/", transport=httpx.MockTransport(transport)
        )

    async def test_one_structured_request_preserves_language_and_requests_instagram_hashtags(self):
        captions = {"youtube": "على ضفاف النهر", "instagram": "جولة صباحية #طبيعة #نهر #صباح"}
        await self.response(
            {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(captions)}}]}
        )
        self.assertEqual(
            await self.router.generate_captions("عنوان", "جولة", list(captions)), captions
        )
        self.assertEqual(len(self.requests), 1)
        request = json.loads(self.requests[0].content)
        self.assertEqual(request["max_completion_tokens"], 4096)
        self.assertEqual(request["model"], "google/gemini-2.5-flash-lite")
        self.assertIn("Preserve its language", request["messages"][0]["content"])
        self.assertIn("hashtags", request["messages"][1]["content"])
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        self.assertNotIn("video", request)

    async def test_invalid_or_partial_output_never_becomes_a_caption(self):
        for content, finish in [
            ("{}", "stop"),
            ("not json", "stop"),
            ('{"youtube": ""}', "stop"),
            (json.dumps({"youtube": "x" * 801}), "stop"),
            ('{"youtube":"ok"}', "length"),
        ]:
            with self.subTest(content=content[:20], finish=finish):
                await self.response(
                    {"choices": [{"finish_reason": finish, "message": {"content": content}}]}
                )
                with self.assertRaises(RemoteError):
                    await self.router.generate_captions("Title", "Master", ["youtube"])

    async def test_rate_limit_and_key_redaction(self):
        await self.response({"error": {"message": "test-key"}}, 429)
        with self.assertRaises(RemoteError) as captured:
            await self.router.generate_captions("Title", "Master", ["youtube"])
        self.assertEqual(captured.exception.status, 429)
        self.assertNotIn("test-key", str(captured.exception))


class TikTokFormTests(unittest.TestCase):
    def test_disclosure_privacy_and_interaction_validation(self):
        result = direct_post_info(form_values(), CREATOR, "private_test")
        self.assertEqual(result["privacy_level"], "SELF_ONLY")
        self.assertTrue(result["disable_comment"])
        for changes in (
            {"consent": False},
            {"privacy_level": ""},
            {"allow_duet": True},
            {"commercial_content": True},
            {"privacy_level": "PUBLIC_TO_EVERYONE"},
            {"commercial_content": True, "brand_content_toggle": True},
            {"allow_comment": "true"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                direct_post_info(form_values(**changes), CREATOR, "private_test")

    def test_private_post_is_never_reported_as_public(self):
        self.assertEqual(
            direct_outcome({"status": "PUBLISH_COMPLETE"}, "SELF_ONLY").state, "needs_action"
        )
        self.assertEqual(
            direct_outcome({"status": "PUBLISH_COMPLETE"}, "PUBLIC_TO_EVERYONE").state,
            "needs_action",
        )
        self.assertEqual(
            direct_outcome(
                {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["123"]},
                "PUBLIC_TO_EVERYONE",
            ).state,
            "published",
        )

    def test_init_data_requires_signature_owner_freshness_and_unique_fields(self):
        raw = signed_init_data("token", timestamp=1000)
        self.assertEqual(validate_init_data(raw, "token", 1, now=1001)["id"], 1)
        for value in (
            raw,
            raw + "&user=2",
            raw.replace("1000", "1001"),
            signed_init_data("token", user=2, timestamp=1000),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_init_data(value, "token", 1, now=3000 if value == raw else 1001)

    def test_unicode_caption_budget_leaves_room_for_review_header(self):
        caption = "😀" * 400
        validate_platform_caption(caption)
        text = review_caption(
            {"id": 2**63 - 1, "title": "x" * 100}, {"platform": "youtube", "caption": caption}
        )
        self.assertLessEqual(text_length(text), 1024)
        with self.assertRaises(ValueError):
            validate_platform_caption(caption + "😀")


class PublishingWebTests(ReviewFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = TestClient(TestServer(self.service.web.app))
        await self.client.start_server()
        self.headers = {
            "X-Telegram-Init-Data": signed_init_data(self.config.telegram_token),
            "Origin": self.config.public_site_url,
        }

    async def asyncTearDown(self):
        await self.client.close()
        await super().asyncTearDown()

    async def test_form_post_queues_once_and_requires_signed_owner(self):
        job_id = await self.ready_for_review()
        url = f"/publisher/api/tiktok/{job_id}"
        response = await self.client.post(url, json=form_values())
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        with (
            patch.object(self.service.review, "check_tiktok_identity", AsyncMock()),
            patch.object(self.service.tiktok, "creator_info", AsyncMock(return_value=CREATOR)),
            patch.object(
                self.service.review,
                "ensure_media",
                AsyncMock(
                    return_value=(Path("unused"), Video(100, 30, 1080, 1920, 30, "h264", "mp4"))
                ),
            ),
        ):
            response = await self.client.post(url, json=form_values(), headers=self.headers)
            self.assertEqual(response.status, 200, await response.text())
            response = await self.client.post(url, json=form_values(), headers=self.headers)
            self.assertEqual(response.status, 409)
        destination = self.db.destination(job_id, "tiktok")
        self.assertEqual(destination["state"], "ready")
        self.assertEqual(destination["caption"], "Approved TikTok caption")
        self.assertEqual(self.db.destination(job_id, "youtube")["state"], "review")

    async def test_media_capability_is_scoped_expires_and_supports_range(self):
        job_id = await self.ready_for_review()
        destination = self.db.destination(job_id, "tiktok")
        folder = self.root / "bot" / "videos"
        folder.mkdir(parents=True)
        path = folder / "range-test.mp4"
        path.write_bytes(b"0123456789")
        self.db.execute(
            "UPDATE media_assets SET local_path=? WHERE id=?", (str(path), destination["media_id"])
        )
        url = self.service.web.media_link(job_id, destination["media_id"], 0, "preview")
        route = url.removeprefix(self.config.public_site_url)
        response = await self.client.get(route, headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b"2345")
        response = await self.client.get("/publisher/media/not-a-valid-token")
        self.assertEqual(response.status, 404)
        self.db.execute("UPDATE media_links SET expires_at=0")
        response = await self.client.get(route)
        self.assertEqual(response.status, 404)

    async def test_media_link_cannot_expose_outside_root_or_another_revision(self):
        job_id = await self.ready_for_review()
        destination = self.db.destination(job_id, "tiktok")
        self.db.execute(
            "UPDATE media_assets SET local_path=? WHERE id=?",
            (str(self.db_path), destination["media_id"]),
        )
        url = self.service.web.media_link(job_id, destination["media_id"], 0, "preview")
        response = await self.client.get(url.removeprefix(self.config.public_site_url))
        self.assertEqual(response.status, 404)
        self.db.change_review(job_id, "tiktok", "rejecting")
        response = await self.client.get(url.removeprefix(self.config.public_site_url))
        self.assertEqual(response.status, 404)
