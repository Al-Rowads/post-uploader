import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

import httpx

from .clients import Publisher, RemoteError, Telegram
from .config import Config
from .database import Database
from .domain import TERMINAL_DESTINATIONS, Outcome, outcome_from_result, result_list
from .media import MediaError, inspect_video, safe_media_path
from .tiktok import TikTok, draft_outcome
from .youtube import YouTube, upload_metadata, upload_outcome, uploaded_offset

logger = logging.getLogger(__name__)

HELP = """Send a video with a caption to upload to YouTube and send a draft to TikTok.
First caption line: YouTube title (up to 100 characters).
Full caption: YouTube description; copy it into TikTok when finishing the draft.
Open the TikTok inbox notification to add captions, disclosures, and public visibility.
Each album item is a separate post and needs its own caption.
If prompted, reply to the job message or original video with the missing caption.

/status [job_id] — queue and platform results
/retry <job_id> — retry confirmed failures or recheck an uncertain submission
/cancel <job_id> — cancel before submission
/pause — stop new uploads (active submissions continue)
/resume — resume automatic uploads
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

    async def close(self):
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
        media = message.get("video") or message.get("document")
        if media:
            existing = self.db.execute(
                "SELECT id FROM jobs WHERE chat_id=? AND message_id=?",
                (chat_id, message["message_id"]),
            ).fetchone()
            if existing:
                if "edited_message" in update:
                    self.db.notify(
                        chat_id,
                        self.db.caption(existing[0], message.get("caption", "")),
                        existing[0],
                    )
                return
            size = media.get("file_size")
            if not isinstance(size, int) or size <= 0 or size > self.config.max_video_bytes:
                self.db.notify(
                    chat_id,
                    "Video needs a known, nonzero size of at most "
                    f"{self.config.max_video_bytes:,} bytes.",
                )
                return
            pending = self.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE state NOT IN ('settled','cancelled')"
            ).fetchone()[0]
            if pending >= self.config.max_pending_jobs:
                self.db.notify(chat_id, "Queue is full. Let existing jobs finish before resending.")
                return
            self.db.create_job(message, media, self.config.declarations)
            return
        reply = message.get("reply_to_message", {}).get("message_id")
        if reply and text:
            job = self.db.execute(
                "SELECT id FROM jobs WHERE chat_id=? AND message_id=? UNION "
                "SELECT job_id FROM outbox WHERE chat_id=? AND sent_message_id=? "
                "AND job_id IS NOT NULL LIMIT 1",
                (chat_id, reply, chat_id, reply),
            ).fetchone()
            if job:
                self.db.notify(chat_id, self.db.caption(job[0], text), job[0])
                return
        self.db.notify(chat_id, "Send a video with a caption, or use /help.")

    def command(self, chat_id: int, text: str):
        arguments = text.split()
        command = arguments[0].split("@")[0].lower()
        if command in {"/start", "/help"}:
            self.db.notify(chat_id, f"YouTube visibility: {self.config.youtube_privacy}\n\n{HELP}")
        elif command in {"/pause", "/resume"}:
            paused = command == "/pause"
            self.db.set_setting("paused", str(paused).lower())
            self.db.notify(
                chat_id,
                "New uploads paused. Active submissions will finish."
                if paused
                else "Automatic uploads resumed.",
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
                    with self.db.transaction():
                        if update["update_id"] < int(self.db.get_setting("offset")):
                            continue
                        self.handle_update(update)
                        self.db.set_setting("offset", str(update["update_id"] + 1))
                self.db.set_setting("ingest_heartbeat", str(time.time()))
            except (httpx.HTTPError, RemoteError) as error:
                logger.warning("Telegram polling unavailable (%s)", type(error).__name__)
                await asyncio.sleep(error.retry_after if isinstance(error, RemoteError) else 10)

    async def deliver_notifications(self):
        while True:
            row = self.db.execute(
                "SELECT * FROM outbox WHERE sent_message_id IS NULL "
                "AND next_try<=? ORDER BY id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row:
                try:
                    result = await self.telegram.send(row["chat_id"], row["body"])
                    self.db.execute(
                        "UPDATE outbox SET sent_message_id=? WHERE id=?",
                        (result["message_id"], row["id"]),
                    )
                except (httpx.HTTPError, RemoteError) as error:
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
        if shutil.disk_usage(self.config.telegram_files).free < (
            job["file_size"] + self.config.disk_reserve_bytes
        ):
            self.defer(job_id, "Download delayed: insufficient free media storage.", 600)
            return
        path = None
        if job["local_path"]:
            try:
                path = safe_media_path(Path(job["local_path"]), self.config.telegram_files)
            except FileNotFoundError:
                pass
        if path is None:
            path = safe_media_path(
                await self.telegram.download(job["file_id"]), self.config.telegram_files
            )
            self.db.execute(
                "UPDATE jobs SET local_path=?,media_saved_at=? WHERE id=?",
                (str(path), time.time(), job_id),
            )
        if self.db.job(job_id)["state"] == "cancelled":
            return
        video = await inspect_video(path, job["file_size"], self.config.max_video_bytes)
        if self.db.job(job_id)["state"] == "cancelled":
            return
        for platform in ("youtube", "tiktok"):
            current = self.db.job(job_id)
            if current["state"] == "cancelled":
                return
            if self.db.get_setting("paused") == "true":
                self.db.execute("UPDATE jobs SET state='queued' WHERE id=?", (job_id,))
                return
            destination = next(
                row for row in self.db.destinations(job_id) if row["platform"] == platform
            )
            if destination["state"] != "ready":
                continue
            try:
                if platform == "youtube":
                    await self.youtube.credentials()
                    if self.db.job(job_id)["state"] == "cancelled":
                        return
                    if self.db.get_setting("paused") == "true":
                        self.db.execute("UPDATE jobs SET state='queued' WHERE id=?", (job_id,))
                        return
                    await self.submit_youtube(job, path)
                else:
                    if reason := video.tiktok_error(600):
                        self.db.outcome(job_id, platform, Outcome("invalid", reason))
                        continue
                    await self.tiktok.credentials()
                    if self.db.job(job_id)["state"] == "cancelled":
                        return
                    if self.db.get_setting("paused") == "true":
                        self.db.execute("UPDATE jobs SET state='queued' WHERE id=?", (job_id,))
                        return
                    mime = "video/webm" if "webm" in video.format.split(",") else "video/mp4"
                    await self.submit_tiktok(job, path, video.size, mime)
            except (RemoteError, httpx.HTTPError, OSError) as error:
                # A platform preflight failure must not block the other destination.
                message = (
                    str(error)
                    if isinstance(error, RemoteError)
                    else "Account preflight unavailable; /retry."
                )
                self.db.outcome(job_id, platform, Outcome("failed", message))
        self.db.queue_remaining_destinations()
        self.db.finish_if_terminal(job_id)
        self.db.notify(job["chat_id"], self.db.summarize(job_id), job_id)

    async def submit_youtube(self, job, path):
        metadata = upload_metadata(
            job, json.loads(job["declarations"]), self.config.youtube_privacy
        )
        request_id = self.db.prepare_attempt(job["id"], ["youtube"], provider="youtube")
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
        job = self.db.job(job_id)
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
                    "No video bytes were sent. This request cannot be looked up by client ID; "
                    "it will not be automatically uploaded again.",
                ),
            )
            self.db.notify(self.db.job(job_id)["chat_id"], self.db.summarize(job_id), job_id)
            return
        now = time.time()
        self.db.execute(
            "UPDATE attempts SET next_poll=?,polls=polls+1 WHERE request_id=?",
            (now + 30, request_id),
        )
        outcome = draft_outcome(await self.tiktok.status(attempt["publish_id"]))
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
                if outcome.state == "needs_action":
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
            current = next(
                row for row in self.db.destinations(job_id) if row["platform"] == platform
            )
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
        if attempt["provider"] == "tiktok":
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
        jobs = self.db.execute(
            "SELECT * FROM jobs WHERE local_path IS NOT NULL AND state!='preparing'"
        ).fetchall()
        for job in jobs:
            destinations = self.db.destinations(job["id"])
            completed = all(row["state"] == "published" for row in destinations)
            if (
                job["state"] != "cancelled"
                and not completed
                and (job["media_saved_at"] or job["created_at"]) > cutoff
            ):
                continue
            if self.db.execute(
                "SELECT 1 FROM attempts WHERE job_id=? AND state='tracking'", (job["id"],)
            ).fetchone():
                continue
            if self.db.execute(
                "SELECT 1 FROM jobs WHERE local_path=? AND id!=? "
                "AND (state='preparing' OR id IN (SELECT job_id FROM attempts "
                "WHERE state='tracking'))",
                (job["local_path"], job["id"]),
            ).fetchone():
                continue
            try:
                safe_media_path(Path(job["local_path"]), self.config.telegram_files).unlink()
            except FileNotFoundError:
                pass
            except (OSError, MediaError):
                logger.warning("Could not clean media for job %s", job["id"])
                continue
            self.db.execute(
                "UPDATE jobs SET local_path=NULL WHERE local_path=?", (job["local_path"],)
            )

    async def run(self):
        self.db.recover()
        await self.telegram.call("getMe")
        await self.telegram.call("deleteWebhook", drop_pending_updates=False)
        self.db.notify(
            self.config.owner_id,
            "Bot started. "
            + (
                "Uploads remain paused."
                if self.db.get_setting("paused") == "true"
                else "Review YouTube uploads in Studio and finish TikTok drafts in TikTok."
            ),
        )
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.ingest())
            tasks.create_task(self.worker())
            tasks.create_task(self.poll_attempts())
            tasks.create_task(self.deliver_notifications())
