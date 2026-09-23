import json
import shutil
import time
from pathlib import Path

import httpx

from .clients import RemoteError
from .domain import (
    PLATFORM_LABELS,
    PLATFORMS,
    review_caption,
    validate_platform_caption,
    validate_title,
)
from .media import MediaError, inspect_video, safe_media_path

REVIEW_STATES = {"review", "rejecting", "waiting_caption", "waiting_video", "tiktok_settings"}


class ReviewWorkflow:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.config = service.config
        self.telegram = service.telegram

    def platform_menu(self, chat_id):
        active = self.db.active_platforms()
        keyboard = [
            [
                {
                    "text": f"{'On' if p in active else 'Off'} · {PLATFORM_LABELS[p]}",
                    "callback_data": f"platform:{p}:{0 if p in active else 1}",
                }
            ]
            for p in PLATFORMS
        ]
        self.db.enqueue(
            chat_id,
            "menu",
            {
                "text": "Active platforms for new videos:",
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )

    async def check_platform(self, platform):
        if platform == "telegram":
            channel = await self.telegram.channel(self.config.telegram_channel_id)
            self.db.set_setting("telegram_channel", json.dumps(channel))
        elif platform == "youtube":
            if len(self.config.declarations) != 3:
                raise RemoteError(
                    "Set all three YouTube content declarations before enabling it.", 400
                )
            await self.service.youtube.credentials()
        else:
            self.check_direct_config()
            await self.check_tiktok_identity("video.publish")
            await self.service.tiktok.creator_info()

    def check_direct_config(self):
        if (
            self.config.tiktok_direct_mode == "disabled"
            or not self.config.public_site_url
            or not self.config.tiktok_media_verified
        ):
            raise RemoteError(
                "TikTok Direct Post needs TIKTOK_DIRECT_MODE, PUBLIC_SITE_URL, "
                "and verified media hosting (TIKTOK_MEDIA_VERIFIED=true).",
                400,
            )

    async def check_tiktok_identity(self, scope):
        credentials = await self.service.tiktok.credentials(scope)
        saved = self.db.get_setting("tiktok_open_id")
        if saved and saved != credentials["open_id"]:
            raise RemoteError("This queue belongs to another TikTok account.", 403)
        self.db.set_setting("tiktok_open_id", credentials["open_id"])

    def prompt_is_current(self, row):
        stage = json.loads(row["payload"]).get("stage")
        if row["platform"]:
            destination = self.db.destination(row["job_id"], row["platform"])
            return (
                destination
                and destination["state"] == stage
                and destination["revision"] == row["revision"]
            )
        return self.db.job(row["job_id"])["state"] == stage

    def handle_message(self, message, *, edited=False):
        chat_id = message["chat"]["id"]
        media = message.get("video") or message.get("document")
        text = message.get("text", "")
        if edited:
            self.db.notify(chat_id, "Use the review buttons to replace a caption or video.")
            return
        reply = message.get("reply_to_message", {}).get("message_id")
        prompt = None
        if reply:
            prompt = self.db.execute(
                "SELECT * FROM outbox WHERE chat_id=? AND sent_message_id=? AND kind='prompt'",
                (chat_id, reply),
            ).fetchone()
            if prompt and not self.prompt_is_current(prompt):
                self.db.notify(chat_id, "That prompt is no longer active. Use the latest review.")
                return
        elif text:
            prompts = [
                row
                for row in self.db.execute(
                    "SELECT * FROM outbox WHERE chat_id=? AND kind='prompt' AND sent_message_id "
                    "IS NOT NULL",
                    (chat_id,),
                )
                if self.prompt_is_current(row)
                and json.loads(row["payload"])["stage"] != "waiting_video"
            ]
            if len(prompts) == 1:
                prompt = prompts[0]
        try:
            if prompt:
                self.save_reply(prompt, message, media, text)
            elif media:
                existing = self.db.execute(
                    "SELECT id FROM jobs WHERE chat_id=? AND message_id=?",
                    (chat_id, message["message_id"]),
                ).fetchone()
                if existing:
                    return
                self.validate_media_size(media)
                pending = self.db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE state NOT IN ('settled','cancelled')"
                ).fetchone()[0]
                if pending >= self.config.max_pending_jobs:
                    raise ValueError("Queue is full. Let existing jobs finish before resending.")
                self.db.create_job(message, media, self.config.declarations)
            else:
                self.db.notify(chat_id, "Reply to the relevant prompt, send a video, or use /help.")
        except ValueError as error:
            self.db.notify(chat_id, str(error), prompt["job_id"] if prompt else None)

    def validate_media_size(self, media):
        size = media.get("file_size")
        if type(size) is not int or not 0 < size <= self.config.max_video_bytes:
            raise ValueError(
                f"Send a video with a known size up to {self.config.max_video_bytes:,} bytes."
            )
        if not media.get("file_id") or not media.get("file_unique_id"):
            raise ValueError("Telegram did not identify the video; please resend it.")

    def save_reply(self, prompt, message, media, text):
        stage = json.loads(prompt["payload"])["stage"]
        job_id, platform = prompt["job_id"], prompt["platform"]
        if stage == "waiting_title":
            title = validate_title(text)
            self.db.execute(
                "UPDATE jobs SET title=?,state='waiting_master_caption' WHERE id=?", (title, job_id)
            )
            self.db.prompt(job_id, "waiting_master_caption")
        elif stage == "waiting_master_caption":
            if not text.strip() or len(text) > 4096:
                raise ValueError("Send a master caption of 1–4096 characters.")
            self.db.execute(
                "UPDATE jobs SET caption=?,state='captions_queued',next_run=0 WHERE id=?",
                (text, job_id),
            )
            self.db.notify(
                message["chat"]["id"], f"Job #{job_id}: generating platform captions.", job_id
            )
        elif stage == "waiting_caption":
            caption = validate_platform_caption(text)
            self.db.execute(
                "UPDATE destinations SET caption=? WHERE job_id=? AND platform=?",
                (caption, job_id, platform),
            )
            self.approve(job_id, platform)
        elif stage == "waiting_video":
            if not media:
                raise ValueError("Reply to this prompt with a replacement video.")
            self.validate_media_size(media)
            media_id = self.db.add_media(media, "video" if message.get("video") else "document")
            self.db.execute(
                "UPDATE destinations SET media_id=?,message='' WHERE job_id=? AND platform=?",
                (media_id, job_id, platform),
            )
            self.db.change_review(job_id, platform, "validating")
            self.db.notify(
                message["chat"]["id"],
                f"Job #{job_id} · {platform}: validating replacement.",
                job_id,
            )

    def approve(self, job_id, platform):
        state = "tiktok_settings" if platform == "tiktok" else "ready"
        self.db.execute(
            "UPDATE destinations SET approved=?,message='' WHERE job_id=? AND platform=?",
            (int(platform != "tiktok"), job_id, platform),
        )
        self.db.change_review(job_id, platform, state)
        self.db.queue_remaining_destinations()

    def keyboard(self, job_id, destination):
        platform, revision, state = (destination[k] for k in ("platform", "revision", "state"))

        def button(text, action):
            return {
                "text": text,
                "callback_data": f"review:{job_id}:{platform}:{revision}:{action}",
            }

        if state in {"review", "tiktok_settings"}:
            accept = button("Accept", "accept")
            if platform == "tiktok":
                try:
                    self.check_direct_config()
                except RemoteError:
                    pass
                else:
                    accept = {
                        "text": "Accept",
                        "web_app": {
                            "url": f"{self.config.public_site_url}/publisher/tiktok/"
                            f"?job={job_id}&revision={revision}"
                        },
                    }
            buttons = [accept, button("Reject", "reject")]
        elif state == "rejecting":
            buttons = [button("Reject caption", "caption"), button("Reject video", "video")]
        else:
            buttons = []
        return {"inline_keyboard": [buttons] if buttons else []}

    async def callback(self, callback):
        message = callback.get("message", {})
        allowed = self.service.is_owner_chat(callback.get("from"), message.get("chat"))
        try:
            await self.telegram.call(
                "answerCallbackQuery",
                callback_query_id=callback["id"],
                text="" if allowed else "Not authorized.",
            )
        except (RemoteError, httpx.HTTPError):
            pass
        if not allowed:
            return
        chat_id = message["chat"]["id"]
        parts = str(callback.get("data", "")).split(":")
        if (
            len(parts) == 3
            and parts[0] == "platform"
            and parts[1] in PLATFORMS
            and parts[2] in {"0", "1"}
        ):
            # Only buttons on an actual settings message may change the platform selection.
            if not self.db.execute(
                "SELECT 1 FROM outbox WHERE kind='menu' AND chat_id=? AND sent_message_id=?",
                (chat_id, message["message_id"]),
            ).fetchone():
                return
            platform, enabled = parts[1], parts[2] == "1"
            try:
                if enabled:
                    await self.check_platform(platform)
                active = self.db.active_platforms()
                if enabled and platform not in active:
                    active.append(platform)
                if not enabled and platform in active:
                    active.remove(platform)
                self.db.set_setting("active_platforms", json.dumps(active))
            except (RemoteError, httpx.HTTPError) as error:
                self.db.notify(
                    chat_id,
                    self.config.redact(str(error))
                    if isinstance(error, RemoteError)
                    else "Platform check unavailable; try again.",
                )
            self.platform_menu(chat_id)
            return
        try:
            kind, job_id, platform, revision, action = parts
            job_id, revision = int(job_id), int(revision)
            if not 0 < job_id < 2**63 or not 0 <= revision < 2**63:
                return
        except (ValueError, TypeError):
            return
        destination = self.db.destination(job_id, platform)
        job = self.db.job(job_id)
        if (
            kind != "review"
            or not job
            or job["chat_id"] != chat_id
            or not destination
            or destination["preview_message_id"] != message.get("message_id")
            or destination["revision"] != revision
        ):
            self.db.notify(chat_id, "That review is outdated. Use the latest platform preview.")
            return
        with self.db.transaction():
            state = destination["state"]
            if action == "accept" and state == "review" and platform != "tiktok":
                self.approve(job_id, platform)
            elif action == "accept" and state in {"review", "tiktok_settings"}:
                try:
                    self.check_direct_config()
                except RemoteError as error:
                    self.db.notify(chat_id, str(error), job_id)
                else:
                    self.db.change_review(job_id, platform, "tiktok_settings")
            elif action == "reject" and state in {"review", "tiktok_settings"}:
                self.db.change_review(job_id, platform, "rejecting")
            elif action in {"caption", "video"} and state == "rejecting":
                self.db.change_review(job_id, platform, "waiting_" + action)

    async def deliver(self, row):
        payload = json.loads(row["payload"])
        kind = row["kind"]
        if kind == "text":
            return await self.telegram.send(row["chat_id"], row["body"])
        if kind == "menu":
            return await self.telegram.send(row["chat_id"], **payload)
        if kind == "prompt":
            if not self.prompt_is_current(row):
                return None
            return await self.telegram.send(
                row["chat_id"],
                payload["text"],
                reply_markup={"force_reply": True, "selective": True},
            )
        destination = self.db.destination(row["job_id"], row["platform"])
        if not destination or destination["revision"] != row["revision"]:
            return None
        job = self.db.job(row["job_id"])
        if not destination["preview_message_id"] and destination["state"] not in REVIEW_STATES:
            return None
        try:
            validate_platform_caption(destination["caption"])
        except ValueError:
            # Historical captions are kept intact until the owner supplies a shorter version.
            if destination["state"] == "review":
                self.db.change_review(job["id"], destination["platform"], "waiting_caption")
            return None
        markup = self.keyboard(job["id"], destination)
        media = self.db.media(destination["media_id"])
        if media["kind"] == "unknown":
            if destination["state"] == "waiting_video":
                return None
            try:
                await self.ensure_media(media["id"])
            except (MediaError, RemoteError) as error:
                if isinstance(error, RemoteError) and error.status not in {400, 404}:
                    raise
                current = self.db.destination(job["id"], destination["platform"])
                if current["revision"] == destination["revision"]:
                    self.db.notify(
                        job["chat_id"],
                        "This older video's preview is unavailable. Reply with a replacement "
                        f"for {PLATFORM_LABELS[destination['platform']]}.",
                        job["id"],
                    )
                    self.db.change_review(job["id"], destination["platform"], "waiting_video")
                return None
            media = self.db.media(media["id"])
            current = self.db.destination(job["id"], destination["platform"])
            if current["revision"] != destination["revision"]:
                return None
        try:
            result = await self.telegram.send_media(
                row["chat_id"],
                media,
                review_caption(job, destination),
                reply_markup=markup,
                message_id=destination["preview_message_id"],
            )
        except RemoteError as error:
            if error.status == 400 and "message is not modified" in str(error).lower():
                return None
            if error.status == 400 and any(
                reason in str(error).lower()
                for reason in (
                    "message to edit not found",
                    "message can't be edited",
                    "message_id_invalid",
                )
            ):
                result = await self.telegram.send_media(
                    row["chat_id"],
                    media,
                    review_caption(job, destination),
                    reply_markup=markup,
                )
            else:
                raise
        self.db.execute(
            "UPDATE destinations SET preview_message_id=? WHERE job_id=? AND platform=?",
            (result["message_id"], job["id"], destination["platform"]),
        )
        return result

    async def ensure_media(self, media_id):
        media = self.db.media(media_id)
        path = None
        if media["local_path"]:
            try:
                path = safe_media_path(Path(media["local_path"]), self.config.telegram_files)
            except FileNotFoundError:
                pass
        if path is None:
            if shutil.disk_usage(self.config.telegram_files).free < (
                media["file_size"] + self.config.disk_reserve_bytes
            ):
                raise RemoteError(
                    "Insufficient free media storage; retry after freeing space.", 503, 600
                )
            path = safe_media_path(
                await self.telegram.download(media["file_id"]), self.config.telegram_files
            )
            self.db.execute(
                "UPDATE media_assets SET local_path=?,saved_at=? WHERE id=?",
                (str(path), time.time(), media_id),
            )
        if media["kind"] == "unknown":
            kind = "video" if path.parent.name == "videos" else "document"
            self.db.execute("UPDATE media_assets SET kind=? WHERE id=?", (kind, media_id))
        video = await inspect_video(path, media["file_size"], self.config.max_video_bytes)
        return path, video

    async def generate(self, job):
        if job["failures"] >= 3:
            self.db.execute("UPDATE jobs SET state='caption_failed' WHERE id=?", (job["id"],))
            self.db.notify(
                job["chat_id"], "Caption generation exhausted retries. Use /retry.", job["id"]
            )
            return
        self.db.execute(
            "UPDATE jobs SET state='generating_captions',failures=failures+1 WHERE id=?",
            (job["id"],),
        )
        try:
            platforms = [d["platform"] for d in self.db.destinations(job["id"])]
            captions = await self.service.openrouter.generate_captions(
                job["title"], job["caption"], platforms
            )
        except (RemoteError, httpx.HTTPError) as error:
            if self.db.job(job["id"])["state"] == "cancelled":
                return
            permanent = isinstance(error, RemoteError) and error.status in {400, 401, 402, 403, 404}
            exhausted = job["failures"] >= 2 or permanent
            delay = error.retry_after if isinstance(error, RemoteError) else 30
            detail = (
                self.config.redact(str(error))
                if isinstance(error, RemoteError)
                else "OpenRouter is unavailable."
            )
            self.db.execute(
                "UPDATE jobs SET state=?,next_run=?,note=? WHERE id=?",
                (
                    "caption_failed" if exhausted else "captions_queued",
                    time.time() + delay,
                    f"Caption generation failed: {detail} Use /retry."
                    if exhausted
                    else "Retrying captions.",
                    job["id"],
                ),
            )
            if exhausted:
                self.db.notify(job["chat_id"], self.db.job(job["id"])["note"], job["id"])
            return
        with self.db.transaction():
            if self.db.job(job["id"])["state"] == "cancelled":
                return
            for platform, caption in captions.items():
                self.db.execute(
                    "UPDATE destinations SET caption=?,state='review' "
                    "WHERE job_id=? AND platform=?",
                    (caption, job["id"], platform),
                )
                self.db.preview(job["id"], platform)
            self.db.execute(
                "UPDATE jobs SET state='reviewing',failures=0,note='' WHERE id=?", (job["id"],)
            )

    async def validate_replacement(self, destination):
        job_id, platform = destination["job_id"], destination["platform"]
        try:
            _, video = await self.ensure_media(destination["media_id"])
            if platform == "youtube" and (reason := video.youtube_shorts_error()):
                raise MediaError(reason)
            if platform == "tiktok":
                self.check_direct_config()
                await self.check_tiktok_identity("video.publish")
                creator = await self.service.tiktok.creator_info()
                if reason := video.tiktok_error(creator["max_video_post_duration_sec"]):
                    raise MediaError(reason)
        except (MediaError, RemoteError, httpx.HTTPError, OSError) as error:
            current = self.db.destination(job_id, platform)
            if current["state"] != "validating" or current["revision"] != destination["revision"]:
                return
            message = (
                str(error)
                if isinstance(error, (MediaError, RemoteError))
                else "Media check unavailable."
            )
            self.db.notify(self.db.job(job_id)["chat_id"], self.config.redact(message), job_id)
            self.db.change_review(job_id, platform, "waiting_video")
            return
        current = self.db.destination(job_id, platform)
        if current["state"] == "validating" and current["revision"] == destination["revision"]:
            self.approve(job_id, platform)

    async def tick(self):
        replacement = self.db.execute(
            "SELECT * FROM destinations WHERE state='validating' ORDER BY job_id LIMIT 1"
        ).fetchone()
        if replacement:
            await self.validate_replacement(replacement)
            return True
        job = self.db.execute(
            "SELECT * FROM jobs WHERE state='captions_queued' AND next_run<=? ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()
        if job:
            await self.generate(job)
            return True
        return False
