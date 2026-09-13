import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .domain import PLATFORMS, TERMINAL_DESTINATIONS, Outcome, parse_caption

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
        if version not in (0, 1, 2, 3):
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

    def create_job(self, message: dict, media: dict, declarations: dict) -> int:
        caption = message.get("caption", "")
        try:
            title, caption = parse_caption(caption)
            state, note = "queued", ""
        except ValueError as error:
            title, state, note = "", "waiting_caption", str(error)
        now = time.time()
        cursor = self.execute(
            "INSERT INTO jobs(chat_id,message_id,file_id,file_unique_id,filename,file_size,"
            "caption,title,declarations,state,note,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,message_id) DO NOTHING",
            (
                message["chat"]["id"],
                message["message_id"],
                media["file_id"],
                media["file_unique_id"],
                media.get("file_name", "video.mp4"),
                media["file_size"],
                caption,
                title,
                json.dumps(declarations),
                state,
                note,
                now,
                now,
            ),
        )
        if not cursor.rowcount:
            return self.execute(
                "SELECT id FROM jobs WHERE chat_id=? AND message_id=?",
                (message["chat"]["id"], message["message_id"]),
            ).fetchone()[0]
        job_id = cursor.lastrowid
        for platform in PLATFORMS:
            self.execute(
                "INSERT INTO destinations(job_id,platform) VALUES (?,?)", (job_id, platform)
            )
        self.notify(message["chat"]["id"], f"Job #{job_id}: {state}. {note}", job_id)
        return job_id

    def caption(self, job_id: int, text: str) -> str:
        job = self.job(job_id)
        if (
            job["state"] not in {"waiting_caption", "queued"}
            or self.execute("SELECT 1 FROM attempts WHERE job_id=?", (job_id,)).fetchone()
        ):
            return "Caption is locked once upload preparation starts."
        try:
            title, caption = parse_caption(text)
        except ValueError as error:
            self.execute(
                "UPDATE jobs SET state='waiting_caption',note=? WHERE id=?", (str(error), job_id)
            )
            return str(error)
        self.execute(
            "UPDATE jobs SET caption=?,title=?,state='queued',note='',updated_at=? WHERE id=?",
            (caption, title, time.time(), job_id),
        )
        return f"Job #{job_id}: caption saved; queued for automatic publication."

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

    def prepare_attempt(self, job_id: int, platforms: list[str], provider="upload_post") -> str:
        request_id = str(uuid.uuid4())
        now = time.time()
        with self.transaction():
            self.execute(
                "INSERT INTO attempts(request_id,job_id,platforms,state,created_at,"
                "next_poll,polls,provider) "
                "VALUES (?,?,?,'tracking',?,?,0,?)",
                (request_id, job_id, json.dumps(platforms), now, now + 10, provider),
            )
            self.execute(
                "UPDATE jobs SET state='submitted',updated_at=?,note='' WHERE id=?", (now, job_id)
            )
            for platform in platforms:
                self.outcome(job_id, platform, Outcome("pending", "Submitting video."))
            self.notify(self.job(job_id)["chat_id"], f"Job #{job_id}: uploading.", job_id)
        return request_id

    def recover(self):
        # Destination states prevent resubmission when one platform was sent before a crash.
        self.execute("UPDATE jobs SET state='queued' WHERE state='preparing'")
        self.queue_remaining_destinations()

    def queue_remaining_destinations(self):
        self.execute(
            "UPDATE jobs SET state='queued' WHERE state='submitted' AND EXISTS "
            "(SELECT 1 FROM destinations WHERE job_id=jobs.id AND state='ready')"
        )

    def cancel(self, job_id: int) -> str:
        job = self.job(job_id)
        if job["state"] not in {"queued", "waiting_caption", "preparing"}:
            return "Cannot cancel a job already submitted to the provider."
        if self.execute(
            "SELECT 1 FROM attempts WHERE job_id=? AND state IN ('tracking','unknown')", (job_id,)
        ).fetchone():
            return "Cannot cancel while a previous submission has an unresolved outcome."
        self.execute(
            "UPDATE jobs SET state='cancelled',updated_at=? WHERE id=?", (time.time(), job_id)
        )
        self.execute(
            "UPDATE destinations SET state='cancelled' WHERE job_id=? "
            "AND state NOT IN ('published','needs_action')",
            (job_id,),
        )
        return f"Job #{job_id}: cancelled before submission."

    def retry(self, job_id: int) -> str:
        job = self.job(job_id)
        if self.execute(
            "SELECT 1 FROM attempts WHERE job_id=? AND state='unknown'", (job_id,)
        ).fetchone():
            self.execute(
                "UPDATE attempts SET state='tracking',next_poll=?,polls=0 "
                "WHERE job_id=? AND state='unknown'",
                (time.time(), job_id),
            )
            self.execute("UPDATE jobs SET state='submitted',note='' WHERE id=?", (job_id,))
            return "Rechecking the existing submission; no new upload will be sent."
        if job["state"] != "settled":
            return "This job is not a completed failed job. Use /status to check its progress."
        cursor = self.execute(
            "UPDATE destinations SET state='ready',message='' WHERE job_id=? AND state='failed'",
            (job_id,),
        )
        if not cursor.rowcount:
            return "No confirmed failed destinations to retry. Invalid media needs a new video."
        self.execute(
            "UPDATE jobs SET state='queued',next_run=0,note='',failures=0,updated_at=? WHERE id=?",
            (time.time(), job_id),
        )
        return f"Job #{job_id}: retry queued for failed destinations only."
