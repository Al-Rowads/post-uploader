import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from aiohttp import web

from .clients import RemoteError
from .config import owner_matches
from .media import MediaError, safe_media_path
from .tiktok import direct_post_info


def validate_init_data(raw: str, bot_token: str, owner_username: str, now: float | None = None):
    now = time.time() if now is None else now
    try:
        pairs = parse_qsl(raw, strict_parsing=True, keep_blank_values=True, max_num_fields=30)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError
        signature = fields.pop("hash")
        secret = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
        expected = hmac.new(
            secret,
            "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        age = now - int(fields["auth_date"])
        user = json.loads(fields["user"])
        if not 0 <= age <= 900 or not owner_matches(user, owner_username):
            raise ValueError
        return user
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise ValueError("Open a fresh TikTok form from your bot's private chat.") from error


class PublishingWeb:
    def __init__(self, service):
        self.service = service
        self.config = service.config
        self.db = service.db
        self.runner = None
        self.app = web.Application(client_max_size=16_384, middlewares=[self.errors])
        self.app.router.add_get("/publisher/tiktok/", self.page)
        self.app.router.add_get("/publisher/tiktok.js", self.script)
        self.app.router.add_get("/publisher/tiktok.css", self.styles)
        self.app.router.add_get("/publisher/api/tiktok/{job:[0-9]+}", self.context)
        self.app.router.add_post("/publisher/api/tiktok/{job:[0-9]+}", self.publish)
        self.app.router.add_get("/publisher/media/{token}", self.media)

    @web.middleware
    async def errors(self, request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as error:
            response = web.Response(text=error.text, status=error.status, headers=error.headers)
        except (ValueError, MediaError) as error:
            response = web.json_response({"error": str(error)}, status=400)
        except (RemoteError, httpx.HTTPError, OSError) as error:
            message = (
                self.config.redact(str(error))
                if isinstance(error, RemoteError)
                else ("Publishing service unavailable; return to the bot and try again.")
            )
            response = web.json_response({"error": message}, status=503)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def authenticate(self, request):
        try:
            user = validate_init_data(
                request.headers.get("X-Telegram-Init-Data", ""),
                self.config.telegram_token,
                self.config.owner_username,
            )
        except ValueError as error:
            raise web.HTTPUnauthorized(text=str(error)) from error
        if (
            request.method == "POST"
            and request.headers.get("Origin") != self.config.public_site_url
        ):
            raise web.HTTPForbidden(text="Invalid form origin.")
        return user

    def destination(self, request, revision, user_id):
        job_id = int(request.match_info["job"])
        if not 0 < job_id < 2**63 or type(revision) is not int or not 0 <= revision < 2**63:
            raise web.HTTPNotFound()
        job = self.db.job(job_id)
        destination = self.db.destination(job_id, "tiktok")
        if not job or job["chat_id"] != user_id or not destination:
            raise web.HTTPNotFound()
        if destination["revision"] != revision or destination["state"] not in {
            "review",
            "tiktok_settings",
        }:
            raise web.HTTPConflict(
                text="This preview changed or was submitted. Reopen the latest review."
            )
        return destination

    def static(self, name):
        return web.FileResponse(Path(__file__).parent / "static" / name)

    async def page(self, request):
        response = self.static("tiktok.html")
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; "
            "connect-src 'self'; media-src 'self'; img-src 'self'; "
            "base-uri 'none'; form-action 'none'"
        )
        return response

    async def script(self, request):
        return self.static("tiktok.js")

    async def styles(self, request):
        return self.static("tiktok.css")

    def media_link(self, job_id, media_id, revision, purpose):
        token = secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO media_links VALUES (?,?,?,?,?,?)",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                media_id,
                job_id,
                revision,
                purpose,
                time.time() + (7200 if purpose == "publish" else 900),
            ),
        )
        return f"{self.config.public_site_url}/publisher/media/{token}"

    async def context(self, request):
        user = self.authenticate(request)
        revision = int(request.query.get("revision", "-1"))
        destination = self.destination(request, revision, user["id"])
        self.service.review.check_direct_config()
        await self.service.review.check_tiktok_identity("video.publish")
        creator = await self.service.tiktok.creator_info()
        _, video = await self.service.review.ensure_media(destination["media_id"])
        if reason := video.tiktok_error(creator["max_video_post_duration_sec"]):
            raise MediaError(reason)
        destination = self.destination(request, revision, user["id"])
        url = self.media_link(destination["job_id"], destination["media_id"], revision, "preview")
        return web.json_response(
            {
                "creator": creator,
                "caption": destination["caption"],
                "video_url": url,
                "title": self.db.job(destination["job_id"])["title"],
                "revision": revision,
                "private_test": self.config.tiktok_direct_mode == "private_test",
            }
        )

    async def publish(self, request):
        user = self.authenticate(request)
        values = await request.json()
        if not isinstance(values, dict):
            raise ValueError("Invalid publishing form.")
        destination = self.destination(request, values.get("revision"), user["id"])
        self.service.review.check_direct_config()
        await self.service.review.check_tiktok_identity("video.publish")
        creator = await self.service.tiktok.creator_info()
        post_info = direct_post_info(values, creator, self.config.tiktok_direct_mode)
        _, video = await self.service.review.ensure_media(destination["media_id"])
        if reason := video.tiktok_error(creator["max_video_post_duration_sec"]):
            raise MediaError(reason)
        with self.db.transaction():
            destination = self.destination(request, values.get("revision"), user["id"])
            self.db.execute(
                "UPDATE destinations SET caption=?,settings=?,approved=1 WHERE job_id=? "
                "AND platform='tiktok'",
                (
                    post_info["title"],
                    json.dumps(
                        {
                            "post_info": post_info,
                            "form_values": values,
                            "consent_at": time.time(),
                        }
                    ),
                    destination["job_id"],
                ),
            )
            self.db.change_review(destination["job_id"], "tiktok", "ready")
            self.db.queue_remaining_destinations()
        return web.json_response(
            {
                "status": "queued",
                "message": "TikTok publication queued. Processing may take a few minutes; "
                "the bot will report the result.",
            }
        )

    async def media(self, request):
        digest = hashlib.sha256(request.match_info["token"].encode()).hexdigest()
        link = self.db.execute(
            "SELECT * FROM media_links WHERE token_hash=? AND expires_at>?", (digest, time.time())
        ).fetchone()
        if not link:
            raise web.HTTPNotFound()
        destination = self.db.destination(link["job_id"], "tiktok")
        allowed = (
            {"review", "tiktok_settings"}
            if link["purpose"] == "preview"
            else {"pending", "unknown"}
        )
        if (
            not destination
            or destination["media_id"] != link["media_id"]
            or destination["revision"] != link["revision"]
            or destination["state"] not in allowed
        ):
            raise web.HTTPNotFound()
        media = self.db.media(link["media_id"])
        try:
            path = safe_media_path(Path(media["local_path"]), self.config.telegram_files)
        except (TypeError, OSError, MediaError):
            raise web.HTTPNotFound() from None
        return web.FileResponse(
            path, headers={"Content-Disposition": 'inline; filename="video.mp4"'}
        )

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.config.web_bind, self.config.web_port).start()

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
