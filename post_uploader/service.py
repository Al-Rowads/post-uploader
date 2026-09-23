import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from .clients import Publisher, RemoteError, Telegram
from .config import Config
from .database import Database
from .domain import TERMINAL_DESTINATIONS, Outcome, outcome_from_result, result_list
from .media import MediaError, safe_media_path
from .openrouter import OpenRouter
from .review import ReviewWorkflow
from .tiktok import TikTok, direct_outcome, direct_post_info, draft_outcome
from .web import PublishingWeb
from .youtube import YouTube, upload_metadata, upload_outcome, uploaded_offset

logger = logging.getLogger(__name__)

HELP = """Send a video, then reply with a title and a master caption.
The bot generates a caption and sends a separate preview for every active platform.
Accept publishes that version. TikTok Accept opens its settings and final Publish form.
Reject lets you replace only that platform's caption or video. Reply to the replacement prompt.
Videos are not cropped or transcoded.
YouTube Shorts must be square/vertical and at most 180 seconds.
Video documents stay documents in Telegram. Each album item is a separate job.

/platforms — choose platforms for future videos
/status [job_id] — review progress and platform results
/retry <job_id> — retry captions, review failures, or recheck saved submission IDs
/cancel <job_id> — cancel destinations not yet submitted
/pause — stop new publications (active submissions continue)
/resume — resume approved publications
/help — show these instructions"""


class Service:
    def __init__(self, config: Config, database: Database):
        self.config = config
        self.db = database
        self.telegram = Telegram(config)
        self.publisher = Publisher(config)
        self.tiktok = TikTok(config.tiktok_credentials_file)
        self.youtube = YouTube(config.youtube_credentials_file)
        self.uploads_in_flight: set[str] = set()
        self.last_cleanup = 0.0
        self.openrouter = OpenRouter(config)
        self.review = ReviewWorkflow(self)
        self.web = PublishingWeb(self)

    async def close(self):
        await self.web.close()
        await self.openrouter.client.aclose()
        await self.telegram.client.aclose()
        await self.publisher.client.aclose()
        await self.tiktok.client.aclose()
        await self.youtube.client.aclose()

    def handle_update(self, update: dict):
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        if message.get("from", {}).get("id") != self.config.owner_id:
            return
        if message.get("chat", {}).get("type") != "private":
            return
        chat_id = message["chat"]["id"]
        text = message.get("text", "")
        if text.startswith("/"):
            self.command(chat_id, text)
            return
        self.review.handle_message(message, edited="edited_message" in update)

    def command(self, chat_id: int, text: str):
        arguments = text.split()
        command = arguments[0].split("@")[0].lower()
        if command in {"/start", "/help"}:
            self.db.notify(chat_id, f"YouTube visibility: {self.config.youtube_privacy}\n\n{HELP}")
        elif command == "/platforms":
            self.review.platform_menu(chat_id)
        elif command in {"/pause", "/resume"}:
            paused = command == "/pause"
            self.db.set_setting("paused", str(paused).lower())
            self.db.notify(
                chat_id,
                "New uploads paused. Active submissions will finish."
                if paused
                else "Approved publications resumed.",
            )
        elif command == "/status" and len(arguments) == 1:
            jobs = self.db.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 10").fetchall()
            body = "Uploads paused.\n" if self.db.get_setting("paused") == "true" else ""
            body += "\n\n".join(self.db.summarize(job[0]) for job in jobs) or "No jobs yet."
            self.db.notify(chat_id, body)
        elif command in {"/status", "/retry", "/cancel"}:
            if len(arguments) != 2 or not arguments[1].lstrip("#").isdigit():
                self.db.notify(chat_id, f"Usage: {command} <job_id>")
                return
            job_id = int(arguments[1].lstrip("#"))
            if job_id > 2**63 - 1 or not (job := self.db.job(job_id)) or job["chat_id"] != chat_id:
                self.db.notify(chat_id, "Job not found.")
                return
            actions = {
                "/status": self.db.summarize,
                "/retry": self.db.retry,
                "/cancel": self.db.cancel,
            }
            self.db.notify(chat_id, actions[command](job_id), job_id)
        else:
            self.db.notify(chat_id, "Unknown command. Use /help.")

    async def ingest(self):
        while True:
            try:
                updates = await self.telegram.get_updates(int(self.db.get_setting("offset")))
                for update in updates:
                    if update["update_id"] < int(self.db.get_setting("offset")):
                        continue
                    if "callback_query" in update:
                        await self.review.callback(update["callback_query"])
                        self.db.set_setting("offset", str(update["update_id"] + 1))
                    else:
                        with self.db.transaction():
                            self.handle_update(update)
                            self.db.set_setting("offset", str(update["update_id"] + 1))
                self.db.set_setting("ingest_heartbeat", str(time.time()))
            except (httpx.HTTPError, RemoteError) as error:
                logger.warning("Telegram polling unavailable (%s)", type(error).__name__)
                await asyncio.sleep(error.retry_after if isinstance(error, RemoteError) else 10)

    async def deliver_notifications(self):
        while True:
            row = self.db.execute(
                "SELECT * FROM outbox WHERE done=0 AND next_try<=? ORDER BY id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row:
                try:
                    result = await self.review.deliver(row)
                    self.db.execute(
                        "UPDATE outbox SET sent_message_id=?,done=1 WHERE id=?",
                        (result["message_id"] if result else None, row["id"]),
                    )
                except (httpx.HTTPError, RemoteError, OSError) as error:
                    delay = error.retry_after if isinstance(error, RemoteError) else 30
                    self.db.execute(
                        "UPDATE outbox SET next_try=? WHERE id=?", (time.time() + delay, row["id"])
                    )
                    logger.warning("Telegram notification delayed (%s)", type(error).__name__)
            await asyncio.sleep(1)

    def fail_ready(self, job_id: int, message: str, state: str = "failed"):
        if self.db.job(job_id)["state"] == "cancelled":
            return
        for destination in self.db.destinations(job_id):
            if destination["state"] == "ready":
                self.db.outcome(job_id, destination["platform"], Outcome(state, message))
        self.db.finish_if_terminal(job_id)
        self.db.notify(self.db.job(job_id)["chat_id"], self.db.summarize(job_id), job_id)

    def defer(self, job_id: int, message: str, delay: float):
        if self.db.job(job_id)["state"] == "cancelled":
            return
        self.db.execute(
            "UPDATE jobs SET state='queued',note=?,next_run=?,updated_at=? WHERE id=?",
            (message, time.time() + delay, time.time(), job_id),
        )
        self.db.notify(self.db.job(job_id)["chat_id"], f"Job #{job_id}: {message}", job_id)

    async def prepare(self, job):
        job_id = job["id"]
        for destination in self.db.destinations(job_id):
            platform = destination["platform"]
            if destination["state"] != "ready" or not destination["approved"]:
                continue
            if self.db.get_setting("paused") == "true":
                break
            try:
                path, video = await self.review.ensure_media(destination["media_id"])
                publication = self.db.publication(job_id, platform)
                if platform == "youtube":
                    if reason := video.youtube_shorts_error():
                        raise MediaError(reason)
                    if len(json.loads(publication["declarations"])) != 3:
                        raise RemoteError(
                            "Configure all three YouTube declarations before uploading.", 400
                        )
                    await self.youtube.credentials()
                elif platform == "telegram":
                    channel_id = publication["settings"].get("chat_id")
                    if not channel_id:
                        raise RemoteError(
                            "Enable Telegram through /platforms to select a channel.", 400
                        )
                    await self.telegram.channel(channel_id)
                else:
                    self.review.check_direct_config()
                    await self.review.check_tiktok_identity("video.publish")
                    creator = await self.tiktok.creator_info()
                    if reason := video.tiktok_error(creator["max_video_post_duration_sec"]):
                        raise MediaError(reason)
                    publication["settings"]["post_info"] = direct_post_info(
                        publication["settings"].get("form_values", {}),
                        creator,
                        self.config.tiktok_direct_mode,
                    )
                # Commands and callbacks may run while network/media preflight is awaiting.
                current = self.db.destination(job_id, platform)
                if (
                    current["state"] != "ready"
                    or current["revision"] != destination["revision"]
                    or self.db.get_setting("paused") == "true"
                ):
                    continue
                if platform == "youtube":
                    await self.submit_youtube(publication, path)
                elif platform == "telegram":
                    await self.submit_telegram(publication)
                else:
                    await self.submit_tiktok_direct(publication)
            except (MediaError, RemoteError, httpx.HTTPError, OSError, ValueError) as error:
                current = self.db.destination(job_id, platform)
                if current["state"] != "ready":
                    continue
                message = (
                    str(error)
                    if isinstance(error, (ValueError, RemoteError))
                    else ("Media/account preflight unavailable; /retry.")
                )
                self.db.outcome(
                    job_id,
                    platform,
                    Outcome(
                        "invalid" if isinstance(error, MediaError) else "failed",
                        self.config.redact(message),
                    ),
                )
        self.db.execute(
            "UPDATE jobs SET state='reviewing' WHERE id=? AND state='preparing'", (job_id,)
        )
        self.db.queue_remaining_destinations()
        self.db.finish_if_terminal(job_id)
        self.db.notify(job["chat_id"], self.db.summarize(job_id), job_id)

    async def submit_telegram(self, publication):
        job_id = publication["id"]
        request_id = self.db.prepare_attempt(job_id, ["telegram"], "telegram", publication)
        self.uploads_in_flight.add(request_id)
        outcome = Outcome(
            "unknown",
            "Telegram delivery is uncertain. Inspect the channel; "
            "this post will not be automatically resent.",
        )
        try:
            result = await self.telegram.send_media(
                publication["settings"]["chat_id"],
                publication,
                publication["caption"],
            )
            if (
                isinstance(result, dict)
                and type(result.get("message_id")) is int
                and isinstance(result.get("chat"), dict)
                and result.get("chat", {}).get("id") == publication["settings"]["chat_id"]
            ):
                message_id = result["message_id"]
                username = publication["settings"].get("username")
                url = f"https://t.me/{username}/{message_id}" if username else None
                outcome = Outcome(
                    "published", "Published to the Telegram channel.", url, str(message_id)
                )
        except RemoteError as error:
            if error.status in {400, 401, 403, 404, 413, 429}:
                outcome = Outcome("failed", self.config.redact(str(error)))
        except (httpx.HTTPError, OSError):
            pass
        finally:
            self.uploads_in_flight.discard(request_id)
        self.db.outcome(job_id, "telegram", outcome)
        self.db.execute(
            "UPDATE attempts SET state=? WHERE request_id=?",
            ("unknown" if outcome.state == "unknown" else "done", request_id),
        )

    async def submit_tiktok_direct(self, publication):
        job_id = publication["id"]
        request_id = self.db.prepare_attempt(job_id, ["tiktok"], "tiktok_direct", publication)
        self.uploads_in_flight.add(request_id)
        url = self.web.media_link(
            job_id, publication["media_id"], publication["revision"], "publish"
        )
        try:
            result = await self.tiktok.initialize_direct(publication["settings"]["post_info"], url)
            self.db.execute(
                "UPDATE attempts SET publish_id=? WHERE request_id=?",
                (result["publish_id"], request_id),
            )
        except RemoteError as error:
            if error.status in {400, 401, 403, 404, 413, 415, 422, 429}:
                self.db.execute(
                    "UPDATE attempts SET state='rejected' WHERE request_id=?", (request_id,)
                )
                self.db.outcome(job_id, "tiktok", Outcome("failed", str(error)))
            else:
                self.submission_uncertain(job_id)
        except (httpx.HTTPError, OSError):
            self.submission_uncertain(job_id)
        finally:
            self.uploads_in_flight.discard(request_id)

    async def submit_youtube(self, job, path):
        metadata = upload_metadata(
            job, json.loads(job["declarations"]), self.config.youtube_privacy
        )
        request_id = self.db.prepare_attempt(
            job["id"], ["youtube"], provider="youtube", snapshot=dict(job)
        )
        self.uploads_in_flight.add(request_id)
        try:
            session = await self.youtube.initialize(metadata, path.stat().st_size)
            # Commit the resumable URL before any bytes; a restart queries this same session.
            self.db.execute(
                "UPDATE attempts SET session_uri=?,next_poll=0 WHERE request_id=?",
                (session, request_id),
            )
        except (RemoteError, httpx.HTTPError, OSError):
            # Initialization cannot publish a video without a subsequent media transfer.
            self.db.execute(
                "UPDATE attempts SET state='rejected' WHERE request_id=?", (request_id,)
            )
            self.db.outcome(
                job["id"],
                "youtube",
                Outcome(
                    "failed",
                    "YouTube initialization failed before media transfer. "
                    "Check access and quota; /retry.",
                ),
            )
        finally:
            self.uploads_in_flight.discard(request_id)

    async def reconcile_youtube(self, attempt):
        request_id, job_id = attempt["request_id"], attempt["job_id"]
        job = json.loads(attempt["snapshot"]) if attempt["snapshot"] else dict(self.db.job(job_id))
        outcome = None
        if not attempt["session_uri"]:
            outcome = Outcome("failed", "YouTube session was not saved; no media was sent. /retry.")
        elif time.time() - attempt["created_at"] > 86400 and attempt["polls"] > 0:
            outcome = Outcome(
                "unknown",
                "YouTube upload is unresolved. Inspect YouTube Studio before further action.",
            )
        else:
            self.db.execute(
                "UPDATE attempts SET next_poll=?,polls=polls+1 WHERE request_id=?",
                (time.time() + 30, request_id),
            )
            try:
                response = await self.youtube.status(attempt["session_uri"], job["file_size"])
                if response.status_code == 308:
                    offset = uploaded_offset(response, job["file_size"])
                    if offset == job["file_size"]:
                        return
                    path = safe_media_path(Path(job["local_path"]), self.config.telegram_files)
                    response = await self.youtube.transfer_chunk(
                        attempt["session_uri"], path, job["file_size"], offset
                    )
                    if response.status_code == 308:
                        confirmed = uploaded_offset(response, job["file_size"])
                        if confirmed > offset:
                            self.db.execute(
                                "UPDATE attempts SET next_poll=0 WHERE request_id=?", (request_id,)
                            )
                        return
                outcome = upload_outcome(self.youtube.result(response))
            except RemoteError as error:
                if error.status not in {404, 410}:
                    raise
                outcome = Outcome(
                    "unknown",
                    "YouTube resumable session expired or is unavailable. "
                    "Inspect YouTube Studio; no automatic duplicate upload will be sent.",
                )
            except (OSError, MediaError, TypeError):
                outcome = Outcome(
                    "unknown",
                    "Local media is unavailable for the saved YouTube session. "
                    "Restore the media before rechecking with /retry.",
                )
        self.db.outcome(job_id, "youtube", outcome)
        state = "unknown" if outcome.state == "unknown" else "done"
        self.db.execute("UPDATE attempts SET state=? WHERE request_id=?", (state, request_id))
        if state == "done":
            self.db.execute(
                "UPDATE attempts SET session_uri=NULL WHERE request_id=?", (request_id,)
            )
        self.db.finish_if_terminal(job_id)
        self.db.notify(job["chat_id"], self.db.summarize(job_id), job_id)

    async def submit_tiktok(self, job, path, size, mime_type):
        job_id = job["id"]
        request_id = self.db.prepare_attempt(job_id, ["tiktok"], provider="tiktok")
        self.uploads_in_flight.add(request_id)
        initialized = False
        try:
            async with asyncio.timeout(3600):
                data = await self.tiktok.initialize(size)
                # Persist the server ID before transferring any video bytes. TikTok has no
                # documented client idempotency key or history lookup for a lost init response.
                self.db.execute(
                    "UPDATE attempts SET publish_id=? WHERE request_id=?",
                    (data["publish_id"], request_id),
                )
                initialized = True
                await self.tiktok.transfer(path, data["upload_url"], size, mime_type)
        except RemoteError as error:
            if not initialized and error.status in {400, 401, 403, 404, 413, 415, 422, 429}:
                self.db.execute(
                    "UPDATE attempts SET state='rejected' WHERE request_id=?", (request_id,)
                )
                self.db.outcome(job_id, "tiktok", Outcome("failed", str(error)))
            else:
                self.submission_uncertain(job_id)
        except (httpx.HTTPError, TimeoutError, OSError):
            self.submission_uncertain(job_id)
        finally:
            self.uploads_in_flight.discard(request_id)

    async def reconcile_tiktok(self, attempt):
        request_id, job_id = attempt["request_id"], attempt["job_id"]
        if not attempt["publish_id"]:
            self.db.execute("UPDATE attempts SET state='unknown' WHERE request_id=?", (request_id,))
            self.db.outcome(
                job_id,
                "tiktok",
                Outcome(
                    "unknown",
                    "TikTok initialization response was lost. "
                    "Inspect TikTok before further action. This request cannot be looked up by "
                    "client ID and will not be automatically submitted again.",
                ),
            )
            self.db.notify(self.db.job(job_id)["chat_id"], self.db.summarize(job_id), job_id)
            return
        now = time.time()
        self.db.execute(
            "UPDATE attempts SET next_poll=?,polls=polls+1 WHERE request_id=?",
            (now + 30, request_id),
        )
        direct = attempt["provider"] == "tiktok_direct"
        await self.review.check_tiktok_identity("video.publish" if direct else "video.upload")
        data = await self.tiktok.status(attempt["publish_id"], direct=direct)
        snapshot = json.loads(attempt["snapshot"] or "{}")
        outcome = (
            direct_outcome(data, snapshot["settings"]["post_info"]["privacy_level"])
            if direct
            else draft_outcome(data)
        )
        current = next(row for row in self.db.destinations(job_id) if row["platform"] == "tiktok")
        if current["state"] == "published" or (
            current["state"] == "needs_action" and outcome.state == "pending"
        ):
            return
        if outcome.state == "pending" and now - attempt["created_at"] > 7200:
            self.db.execute("UPDATE attempts SET state='unknown' WHERE request_id=?", (request_id,))
            outcome = Outcome(
                "unknown", "TikTok processing is unresolved. /retry rechecks this upload."
            )
        if (current["state"], current["message"]) != (outcome.state, outcome.message):
            self.db.outcome(job_id, "tiktok", outcome)
            if outcome.state != "pending":
                self.db.notify(self.db.job(job_id)["chat_id"], self.db.summarize(job_id), job_id)
                if outcome.state == "needs_action" and not direct:
                    self.db.notify(
                        self.db.job(job_id)["chat_id"],
                        "Copy this caption when finishing the TikTok draft:\n\n"
                        + self.db.job(job_id)["caption"],
                        job_id,
                    )
        if outcome.state in TERMINAL_DESTINATIONS:
            self.db.execute("UPDATE attempts SET state='done' WHERE request_id=?", (request_id,))
            self.db.finish_if_terminal(job_id)

    def submission_uncertain(self, job_id: int):
        self.db.notify(
            self.db.job(job_id)["chat_id"],
            f"Job #{job_id}: upload response was lost. "
            "Checking the existing request before any further action.",
            job_id,
        )

    async def worker(self):
        while True:
            self.db.set_setting("worker_heartbeat", str(time.time()))
            await self.review.tick()
            if self.db.get_setting("paused") != "true":
                job = self.db.execute(
                    "SELECT * FROM jobs WHERE state='queued' AND next_run<=? ORDER BY id LIMIT 1",
                    (time.time(),),
                ).fetchone()
                if job:
                    self.db.execute("UPDATE jobs SET state='preparing' WHERE id=?", (job["id"],))
                    try:
                        await self.prepare(job)
                    except MediaError as error:
                        self.fail_ready(job["id"], str(error), "invalid")
                    except (httpx.HTTPError, RemoteError, TimeoutError, OSError) as error:
                        if self.db.job(job["id"])["state"] in {"submitted", "cancelled"}:
                            continue
                        failures = job["failures"] + 1
                        self.db.execute(
                            "UPDATE jobs SET failures=? WHERE id=?", (failures, job["id"])
                        )
                        message = (
                            self.config.redact(str(error))
                            if isinstance(error, RemoteError)
                            else f"Preparation failed ({type(error).__name__}); "
                            "check connectivity/storage."
                        )
                        if (
                            failures >= 5
                            or isinstance(error, RemoteError)
                            and error.status in {400, 401, 403}
                        ):
                            self.fail_ready(job["id"], message)
                        else:
                            delay = (
                                error.retry_after
                                if isinstance(error, RemoteError)
                                else min(600, 30 * 2**failures)
                            )
                            self.defer(job["id"], message, delay)
            await asyncio.sleep(1)

    def apply_results(self, job_id: int, platforms: list[str], results: list[dict], history=False):
        for result in results:
            platform = result.get("platform")
            if platform not in platforms:
                continue
            outcome = outcome_from_result(result, platform, history=history)
            current = self.db.destination(job_id, platform)
            if current is None:
                continue
            if current["state"] in {"published", "needs_action"} and outcome.state not in {
                "published",
                "needs_action",
            }:
                continue
            if current["state"] == "failed" and outcome.state == "pending":
                continue
            sanitized = Outcome(
                outcome.state, self.config.redact(outcome.message), outcome.url, outcome.post_id
            )
            if (current["state"], current["message"], current["url"]) != (
                sanitized.state,
                sanitized.message,
                sanitized.url,
            ):
                self.db.outcome(job_id, platform, sanitized)
                if outcome.state != "pending":
                    self.db.notify(
                        self.db.job(job_id)["chat_id"], self.db.summarize(job_id), job_id
                    )

    async def reconcile(self, attempt):
        if attempt["provider"] == "youtube":
            await self.reconcile_youtube(attempt)
            return
        if attempt["provider"] == "telegram":
            self.db.outcome(
                attempt["job_id"],
                "telegram",
                Outcome(
                    "unknown", "Telegram delivery was interrupted. Inspect the channel; no resend."
                ),
            )
            self.db.execute(
                "UPDATE attempts SET state='unknown' WHERE request_id=?", (attempt["request_id"],)
            )
            self.db.notify(
                self.db.job(attempt["job_id"])["chat_id"],
                self.db.summarize(attempt["job_id"]),
                attempt["job_id"],
            )
            return
        if attempt["provider"] in {"tiktok", "tiktok_direct"}:
            await self.reconcile_tiktok(attempt)
            return
        request_id, job_id = attempt["request_id"], attempt["job_id"]
        platforms = json.loads(attempt["platforms"])
        now = time.time()
        polls = attempt["polls"] + 1
        self.db.execute(
            "UPDATE attempts SET next_poll=?,polls=? WHERE request_id=?",
            (now + 10, polls, request_id),
        )
        status = {}
        try:
            status = await self.publisher.status(request_id)
            self.apply_results(job_id, platforms, result_list(status))
        except RemoteError as error:
            if error.status != 404:
                raise
        # History is also needed to recover late links and ambiguous aggregate results.
        if polls % 6 == 0 or status.get("status") in {"completed", "failed"}:
            history = await self.publisher.history(request_id)
            self.apply_results(job_id, platforms, result_list(history, "history"), history=True)
        destinations = [row for row in self.db.destinations(job_id) if row["platform"] in platforms]
        terminal = all(row["state"] in TERMINAL_DESTINATIONS for row in destinations)
        link_pending = any(row["state"] == "published" and not row["url"] for row in destinations)
        if terminal and (not link_pending or now - attempt["created_at"] > 3600):
            self.db.execute("UPDATE attempts SET state='done' WHERE request_id=?", (request_id,))
            self.db.finish_if_terminal(job_id)
            self.db.notify(self.db.job(job_id)["chat_id"], self.db.summarize(job_id), job_id)
        elif polls >= 360:
            self.db.execute("UPDATE attempts SET state='unknown' WHERE request_id=?", (request_id,))
            for row in destinations:
                if row["state"] not in TERMINAL_DESTINATIONS:
                    self.db.outcome(
                        job_id,
                        row["platform"],
                        Outcome(
                            "unknown",
                            "Publication outcome "
                            "is unresolved. Check Upload-Post; "
                            "/retry rechecks without reuploading.",
                        ),
                    )
            self.db.execute(
                "UPDATE jobs SET note='Provider outcome requires investigation.' WHERE id=?",
                (job_id,),
            )
            self.db.notify(self.db.job(job_id)["chat_id"], self.db.summarize(job_id), job_id)

    async def poll_attempts(self):
        while True:
            attempts = self.db.execute(
                "SELECT * FROM attempts WHERE state='tracking' AND next_poll<=?", (time.time(),)
            ).fetchall()
            for attempt in attempts:
                self.db.set_setting("poller_heartbeat", str(time.time()))
                if attempt["request_id"] in self.uploads_in_flight:
                    continue
                try:
                    await self.reconcile(attempt)
                except (httpx.HTTPError, RemoteError) as error:
                    delay = error.retry_after if isinstance(error, RemoteError) else 30
                    self.db.execute(
                        "UPDATE attempts SET next_poll=? WHERE request_id=?",
                        (time.time() + delay, attempt["request_id"]),
                    )
                    if attempt["polls"] == 0 or attempt["polls"] % 30 == 0:
                        self.db.notify(
                            self.db.job(attempt["job_id"])["chat_id"],
                            f"Job #{attempt['job_id']}: provider status unavailable "
                            f"({type(error).__name__}); keeping the existing submission.",
                            attempt["job_id"],
                        )
            self.db.set_setting("poller_heartbeat", str(time.time()))
            if time.time() - self.last_cleanup > 60:
                self.cleanup()
                self.last_cleanup = time.time()
            await asyncio.sleep(2)

    def cleanup(self):
        cutoff = time.time() - self.config.media_retention_hours * 3600
        self.db.execute("DELETE FROM media_links WHERE expires_at<=?", (time.time(),))
        media_rows = self.db.execute(
            "SELECT * FROM media_assets WHERE local_path IS NOT NULL "
            "AND COALESCE(saved_at,created_at)<?",
            (cutoff,),
        ).fetchall()
        for media in media_rows:
            # Paths may be shared across Telegram file IDs and across destination versions.
            referenced = self.db.execute(
                "SELECT 1 FROM destinations d JOIN media_assets m ON d.media_id=m.id "
                "JOIN jobs j ON j.id=d.job_id WHERE m.local_path=? "
                "AND (j.state NOT IN ('settled','cancelled') OR d.state IN ('pending','unknown'))",
                (media["local_path"],),
            ).fetchone()
            links = self.db.execute(
                "SELECT 1 FROM media_links l JOIN media_assets m "
                "ON m.id=l.media_id WHERE m.local_path=?",
                (media["local_path"],),
            ).fetchone()
            attempts = self.db.execute(
                "SELECT snapshot FROM attempts WHERE state IN ('tracking','unknown')"
            ).fetchall()
            if (
                referenced
                or links
                or any(
                    json.loads(a[0] or "{}").get("local_path") == media["local_path"]
                    for a in attempts
                )
            ):
                continue
            try:
                safe_media_path(Path(media["local_path"]), self.config.telegram_files).unlink()
            except FileNotFoundError:
                pass
            except (OSError, MediaError):
                logger.warning("Could not clean media asset %s", media["id"])
                continue
            self.db.execute(
                "UPDATE media_assets SET local_path=NULL WHERE local_path=?", (media["local_path"],)
            )
            self.db.execute(
                "UPDATE jobs SET local_path=NULL WHERE local_path=?", (media["local_path"],)
            )

    async def run(self):
        self.db.recover()
        await self.telegram.call("getMe")
        if self.config.tiktok_direct_mode != "disabled":
            await self.web.start()
        await self.telegram.call("deleteWebhook", drop_pending_updates=False)
        self.db.notify(
            self.config.owner_id,
            "Bot started. "
            + (
                "Uploads remain paused."
                if self.db.get_setting("paused") == "true"
                else "Send a video to start a review, or use /platforms."
            ),
        )
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.ingest())
            tasks.create_task(self.worker())
            tasks.create_task(self.poll_attempts())
            tasks.create_task(self.deliver_notifications())
