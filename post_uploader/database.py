import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .domain import TERMINAL_DESTINATIONS, Outcome

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO settings VALUES ('offset', '0'), ('paused', 'false');
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    file_unique_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    caption TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    declarations TEXT NOT NULL,
    state TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    local_path TEXT,
    media_saved_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    next_run REAL NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    UNIQUE(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS destinations (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    platform TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready',
    message TEXT NOT NULL DEFAULT '',
    url TEXT,
    post_id TEXT,
    PRIMARY KEY(job_id, platform)
);
CREATE TABLE IF NOT EXISTS attempts (
    request_id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    platforms TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    next_poll REAL NOT NULL,
    polls INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    chat_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    sent_message_id INTEGER,
    next_try REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ready_jobs ON jobs(state, next_run);
CREATE INDEX IF NOT EXISTS due_attempts ON attempts(state, next_poll);
"""


class Database:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4):
            raise RuntimeError(
                "Unsupported database version; use the matching application version."
            )
        self.connection.executescript(SCHEMA)
        if version < 2:
            with self.transaction():
                self.execute(
                    "ALTER TABLE attempts ADD COLUMN provider TEXT NOT NULL DEFAULT 'upload_post'"
                )
                self.execute("ALTER TABLE attempts ADD COLUMN publish_id TEXT")
                self.execute("PRAGMA user_version = 2")
        if version < 3:
            with self.transaction():
                self.execute("ALTER TABLE attempts ADD COLUMN session_uri TEXT")
                self.execute("PRAGMA user_version = 3")

        if version < 4:
            self.migrate_reviews()

    def migrate_reviews(self):
        with self.transaction():
            for statement in (
                "CREATE TABLE media_assets (id INTEGER PRIMARY KEY, file_id TEXT NOT NULL, "
                "file_unique_id TEXT NOT NULL, filename TEXT NOT NULL, file_size INTEGER NOT NULL, "
                "kind TEXT NOT NULL, local_path TEXT, saved_at REAL, created_at REAL NOT NULL)",
                "ALTER TABLE destinations ADD COLUMN caption TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE destinations ADD COLUMN media_id INTEGER REFERENCES media_assets(id)",
                "ALTER TABLE destinations ADD COLUMN revision INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE destinations ADD COLUMN preview_message_id INTEGER",
                "ALTER TABLE destinations ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'",
                "ALTER TABLE destinations ADD COLUMN approved INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE attempts ADD COLUMN snapshot TEXT",
                "ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'",
                "ALTER TABLE outbox ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'",
                "ALTER TABLE outbox ADD COLUMN platform TEXT",
                "ALTER TABLE outbox ADD COLUMN revision INTEGER",
                "ALTER TABLE outbox ADD COLUMN done INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE outbox ADD COLUMN event_key TEXT",
                "CREATE UNIQUE INDEX outbox_events ON outbox(event_key)",
                "CREATE TABLE media_links (token_hash TEXT PRIMARY KEY, media_id INTEGER NOT NULL "
                "REFERENCES media_assets(id), job_id INTEGER NOT NULL REFERENCES jobs(id), "
                "revision INTEGER NOT NULL, purpose TEXT NOT NULL, expires_at REAL NOT NULL)",
            ):
                self.execute(statement)
            for job in self.execute("SELECT * FROM jobs").fetchall():
                media_id = self.add_media(
                    {
                        "file_id": job["file_id"],
                        "file_unique_id": job["file_unique_id"],
                        "file_name": job["filename"],
                        "file_size": job["file_size"],
                    },
                    "unknown",
                )
                self.execute(
                    "UPDATE media_assets SET local_path=?,saved_at=? WHERE id=?",
                    (job["local_path"], job["media_saved_at"], media_id),
                )
                self.execute(
                    "UPDATE destinations SET caption=?,media_id=? WHERE job_id=?",
                    (job["caption"], media_id, job["id"]),
                )
                snapshot = dict(job, media_id=media_id, kind="unknown")
                self.execute(
                    "UPDATE attempts SET snapshot=? WHERE job_id=?",
                    (json.dumps(snapshot), job["id"]),
                )
            self.execute("UPDATE destinations SET state='review' WHERE state='ready'")
            self.execute("UPDATE jobs SET state='reviewing' WHERE state IN ('queued','preparing')")
            self.execute("UPDATE jobs SET state='waiting_title' WHERE state='waiting_caption'")
            self.execute(
                "UPDATE destinations SET state='draft' WHERE job_id IN "
                "(SELECT id FROM jobs WHERE state='waiting_title') AND state='review'"
            )
            self.execute("UPDATE outbox SET done=1")
            self.set_setting("active_platforms", '["youtube"]')
            self.execute("PRAGMA user_version = 4")

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def execute(self, sql: str, values=()):
        return self.connection.execute(sql, values)

    def get_setting(self, key: str) -> str:
        row = self.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    def set_setting(self, key: str, value: str):
        self.execute(
            "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def notify(self, chat_id: int, body: str, job_id: int | None = None):
        self.execute(
            "INSERT INTO outbox(job_id,chat_id,body) VALUES (?,?,?)", (job_id, chat_id, body[:4000])
        )

    def job(self, job_id: int):
        return self.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def destinations(self, job_id: int):
        return self.execute(
            "SELECT * FROM destinations WHERE job_id=? ORDER BY platform", (job_id,)
        ).fetchall()

    def add_media(self, media: dict, kind: str) -> int:
        return self.execute(
            "INSERT INTO media_assets(file_id,file_unique_id,filename,file_size,kind,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                media["file_id"],
                media["file_unique_id"],
                media.get("file_name", "video.mp4"),
                media["file_size"],
                kind,
                time.time(),
            ),
        ).lastrowid

    def media(self, media_id: int):
        return self.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()

    def destination(self, job_id: int, platform: str):
        return self.execute(
            "SELECT * FROM destinations WHERE job_id=? AND platform=?", (job_id, platform)
        ).fetchone()

    def active_platforms(self) -> list[str]:
        return json.loads(self.get_setting("active_platforms") or '["youtube"]')

    def enqueue(self, chat_id, kind, payload, job_id=None, platform=None, revision=None, key=None):
        self.execute(
            "INSERT OR IGNORE INTO outbox(chat_id,body,job_id,kind,payload,platform,revision,"
            "event_key) VALUES (?,'',?,?,?,?,?,?)",
            (chat_id, job_id, kind, json.dumps(payload), platform, revision, key),
        )

    def prompt(self, job_id: int, stage: str, platform: str | None = None):
        job = self.job(job_id)
        revision = self.destination(job_id, platform)["revision"] if platform else None
        label = f"Job #{job_id}" + (f" · {platform}" if platform else "")
        prompts = {
            "waiting_title": "Reply with a title (one line, at most 100 characters).",
            "waiting_master_caption": "Reply with the master caption to adapt for each platform.",
            "waiting_caption": "Reply with the replacement caption (at most 800 characters). "
            "Sending it approves this version and continues publishing.",
            "waiting_video": "Reply to this message with the replacement video. "
            "Only this platform changes; a valid video continues publishing.",
        }
        self.enqueue(
            job["chat_id"],
            "prompt",
            {"stage": stage, "text": label + ": " + prompts[stage]},
            job_id,
            platform,
            revision,
            f"prompt:{job_id}:{platform}:{stage}:{revision}",
        )

    def preview(self, job_id: int, platform: str):
        destination = self.destination(job_id, platform)
        self.enqueue(
            self.job(job_id)["chat_id"],
            "preview",
            {},
            job_id,
            platform,
            destination["revision"],
            f"preview:{job_id}:{platform}:{destination['revision']}",
        )

    def change_review(self, job_id: int, platform: str, state: str):
        self.execute(
            "UPDATE destinations SET state=?,revision=revision+1 WHERE job_id=? AND platform=?",
            (state, job_id, platform),
        )
        self.preview(job_id, platform)
        if state in {"waiting_caption", "waiting_video"}:
            self.prompt(job_id, state, platform)

    def create_job(
        self, message: dict, media: dict, declarations: dict, platforms: list[str] | None = None
    ) -> int:
        existing = self.execute(
            "SELECT id FROM jobs WHERE chat_id=? AND message_id=?",
            (message["chat"]["id"], message["message_id"]),
        ).fetchone()
        if existing:
            return existing[0]
        platforms = self.active_platforms() if platforms is None else platforms
        if not platforms:
            raise ValueError("Activate a destination with /platforms first.")
        now = time.time()
        job_id = self.execute(
            "INSERT INTO jobs(chat_id,message_id,file_id,file_unique_id,filename,file_size,"
            "caption,title,declarations,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,'',?,'waiting_title',?,?)",
            (
                message["chat"]["id"],
                message["message_id"],
                media["file_id"],
                media["file_unique_id"],
                media.get("file_name", "video.mp4"),
                media["file_size"],
                message.get("caption", ""),
                json.dumps(declarations),
                now,
                now,
            ),
        ).lastrowid
        media_id = self.add_media(media, "video" if message.get("video") else "document")
        for platform in platforms:
            settings = self.get_setting("telegram_channel") if platform == "telegram" else "{}"
            self.execute(
                "INSERT INTO destinations(job_id,platform,state,media_id,settings) "
                "VALUES (?,?,'draft',?,?)",
                (job_id, platform, media_id, settings or "{}"),
            )
        self.prompt(job_id, "waiting_title")
        return job_id

    def publication(self, job_id: int, platform: str) -> dict:
        job = dict(self.job(job_id))
        destination = self.destination(job_id, platform)
        media = self.media(destination["media_id"])
        for key in ("file_id", "file_unique_id", "filename", "file_size", "local_path", "kind"):
            job[key] = media[key]
        job.update(
            caption=destination["caption"],
            media_id=media["id"],
            revision=destination["revision"],
            settings=json.loads(destination["settings"]),
        )
        return job

    def outcome(self, job_id: int, platform: str, outcome: Outcome):
        self.execute(
            "UPDATE destinations SET state=?,message=?,url=COALESCE(?,url),"
            "post_id=COALESCE(?,post_id) WHERE job_id=? AND platform=?",
            (outcome.state, outcome.message, outcome.url, outcome.post_id, job_id, platform),
        )

    def summarize(self, job_id: int) -> str:
        job = self.job(job_id)
        lines = [f"Job #{job_id}: {job['state']}", job["title"] or "Caption needed"]
        if job["note"]:
            lines.append(job["note"])
        for destination in self.destinations(job_id):
            lines.append(
                f"{destination['platform']}: {destination['state']} — {destination['message']}"
            )
            if destination["url"]:
                lines.append(destination["url"])
        return "\n".join(lines)

    def finish_if_terminal(self, job_id: int):
        if all(row["state"] in TERMINAL_DESTINATIONS for row in self.destinations(job_id)):
            self.execute(
                "UPDATE jobs SET state='settled',updated_at=? WHERE id=?", (time.time(), job_id)
            )

    def prepare_attempt(
        self,
        job_id: int,
        platforms: list[str],
        provider="upload_post",
        snapshot: dict | None = None,
    ) -> str:
        request_id = str(uuid.uuid4())
        now = time.time()
        with self.transaction():
            for platform in platforms:
                destination = self.destination(job_id, platform)
                if destination["state"] != "ready" or not destination["approved"]:
                    raise ValueError("Destination has not been approved for publication.")
                if snapshot and snapshot.get("revision") != destination["revision"]:
                    raise ValueError("The approved destination changed before publication.")
            snapshot = snapshot or self.publication(job_id, platforms[0])
            self.execute(
                "INSERT INTO attempts(request_id,job_id,platforms,state,created_at,"
                "next_poll,polls,provider) "
                "VALUES (?,?,?,'tracking',?,?,0,?)",
                (request_id, job_id, json.dumps(platforms), now, now + 10, provider),
            )
            self.execute(
                "UPDATE attempts SET snapshot=? WHERE request_id=?",
                (json.dumps(snapshot), request_id),
            )
            self.execute(
                "UPDATE jobs SET state='submitted',updated_at=?,note='' WHERE id=?", (now, job_id)
            )
            for platform in platforms:
                self.outcome(job_id, platform, Outcome("pending", "Submitting video."))
            self.notify(self.job(job_id)["chat_id"], f"Job #{job_id}: uploading.", job_id)
        return request_id

    def recover(self):
        self.execute("UPDATE jobs SET state='captions_queued' WHERE state='generating_captions'")
        self.execute("UPDATE jobs SET state='reviewing' WHERE state='preparing'")
        for job in self.execute(
            "SELECT * FROM jobs WHERE state IN ('waiting_title','waiting_master_caption')"
        ).fetchall():
            self.prompt(job["id"], job["state"])
        for destination in self.execute(
            "SELECT * FROM destinations WHERE state IN "
            "('review','rejecting','waiting_caption','waiting_video',"
            "'tiktok_settings')"
        ).fetchall():
            self.preview(destination["job_id"], destination["platform"])
            if destination["state"].startswith("waiting_"):
                self.prompt(destination["job_id"], destination["state"], destination["platform"])
        self.queue_remaining_destinations()

    def queue_remaining_destinations(self):
        self.execute(
            "UPDATE jobs SET state='queued' WHERE state NOT IN ('cancelled','waiting_title',"
            "'waiting_master_caption','captions_queued','generating_captions','caption_failed') "
            "AND EXISTS (SELECT 1 FROM destinations WHERE job_id=jobs.id "
            "AND state='ready' AND approved=1)"
        )

    def cancel(self, job_id: int) -> str:
        self.execute(
            "UPDATE destinations SET state='cancelled',revision=revision+1 "
            "WHERE job_id=? AND state NOT IN ('pending','unknown','published',"
            "'needs_action')",
            (job_id,),
        )
        active = self.execute(
            "SELECT 1 FROM attempts WHERE job_id=? AND state IN ('tracking','unknown')", (job_id,)
        ).fetchone()
        if not active:
            self.execute(
                "UPDATE jobs SET state='cancelled',updated_at=? WHERE id=?", (time.time(), job_id)
            )
        for destination in self.destinations(job_id):
            self.preview(job_id, destination["platform"])
        return f"Job #{job_id}: unsubmitted destinations cancelled; existing submissions continue."

    def retry(self, job_id: int) -> str:
        job = self.job(job_id)
        if job["state"] == "caption_failed":
            self.execute(
                "UPDATE jobs SET state='captions_queued',failures=0,next_run=0,note='' WHERE id=?",
                (job_id,),
            )
            return "Caption generation queued again."
        attempts = self.execute(
            "SELECT * FROM attempts WHERE job_id=? AND state='unknown'", (job_id,)
        ).fetchall()
        messages = []
        if attempts:
            for attempt in attempts:
                if attempt["provider"] == "telegram" or (
                    attempt["provider"] in {"tiktok", "tiktok_direct"} and not attempt["publish_id"]
                ):
                    continue
                self.execute(
                    "UPDATE attempts SET state='tracking',next_poll=0,polls=0 WHERE request_id=?",
                    (attempt["request_id"],),
                )
            messages.append(
                "Rechecking saved provider IDs only; no new upload will be sent. "
                "Unknown Telegram posts or missing TikTok IDs require manual inspection."
            )
        count = 0
        for destination in self.destinations(job_id):
            if destination["state"] not in {"failed", "invalid"}:
                continue
            count += 1
            self.execute(
                "UPDATE destinations SET approved=0,message='' WHERE job_id=? AND platform=?",
                (job_id, destination["platform"]),
            )
            self.change_review(job_id, destination["platform"], "review")
        if count:
            self.execute(
                "UPDATE jobs SET state='reviewing',failures=0,next_run=0 WHERE id=?", (job_id,)
            )
            messages.append(
                "Failed destinations returned to review. Accept again or replace invalid media."
            )
        return "\n".join(messages) or (
            "No confirmed failures to retry. Use /status to check progress."
        )
