import asyncio
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .clients import RemoteError
from .domain import Outcome
from .tiktok import save_credentials

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
CHUNK_SIZE = 8 * 1024 * 1024


def session_url(value: object) -> str:
    try:
        url = urlsplit(value) if isinstance(value, str) else None
        if (
            url is None
            or url.scheme != "https"
            or url.hostname != "www.googleapis.com"
            or url.path != "/upload/youtube/v3/videos"
            or url.username
            or url.password
            or url.port not in (None, 443)
            or url.fragment
        ):
            raise ValueError
    except ValueError:
        raise RemoteError("YouTube returned an invalid resumable session URL.") from None
    return value


def uploaded_offset(response: httpx.Response, size: int) -> int:
    value = response.headers.get("Range")
    if value is None:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", value)
    if not match or not 0 < int(match[1]) + 1 <= size:
        raise RemoteError("YouTube returned an invalid upload offset.")
    return int(match[1]) + 1


def upload_metadata(job, declarations: dict, privacy: str) -> dict:
    if privacy not in {"private", "unlisted", "public"}:
        raise RemoteError("Select a valid YouTube privacy status.", 400)
    for key in ("selfDeclaredMadeForKids", "containsSyntheticMedia", "hasPaidProductPlacement"):
        if not isinstance(declarations.get(key), bool):
            raise RemoteError("Explicit YouTube content declarations are required.", 400)
    if declarations["hasPaidProductPlacement"]:
        raise RemoteError(
            "Paid promotion metadata is not supported by this native upload path; "
            "upload and declare the promotion in YouTube Studio.",
            400,
        )
    if len(job["caption"].encode("utf-8")) > 5000:
        raise RemoteError("YouTube descriptions must be at most 5000 UTF-8 bytes.", 400)
    return {
        "snippet": {"title": job["title"], "description": job["caption"], "categoryId": "22"},
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": declarations["selfDeclaredMadeForKids"],
            "containsSyntheticMedia": declarations["containsSyntheticMedia"],
        },
    }


def upload_outcome(resource: dict) -> Outcome:
    identifier = resource.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
        raise RemoteError("YouTube completion response has no valid video ID.")
    status = resource.get("status", {})
    if not isinstance(status, dict):
        raise RemoteError("YouTube completion response has invalid status.")
    url = "https://www.youtube.com/watch?v=" + identifier
    if status.get("uploadStatus") in {"failed", "rejected", "deleted"}:
        return Outcome(
            "failed", "YouTube rejected the video; inspect YouTube Studio.", url, identifier
        )
    privacy = status.get("privacyStatus")
    if privacy == "public" and status.get("uploadStatus") == "processed":
        return Outcome("published", "YouTube reports a processed public video.", url, identifier)
    visibility = privacy if privacy in {"private", "unlisted", "public"} else "unconfirmed"
    return Outcome(
        "needs_action",
        f"Video uploaded to YouTube ({visibility}). "
        "Review processing and publication in YouTube Studio.",
        url,
        identifier,
    )


class YouTube:
    def __init__(self, credentials_path: Path):
        self.path = credentials_path
        self.lock = asyncio.Lock()
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=20), follow_redirects=False, trust_env=False
        )

    async def credentials(self, *, force_refresh=False) -> dict:
        async with self.lock:
            try:
                saved = json.loads(self.path.read_text())
                if (
                    not isinstance(saved, dict)
                    or not all(
                        isinstance(saved.get(key), str) and saved[key]
                        for key in ("client_id", "client_secret", "refresh_token")
                    )
                    or UPLOAD_SCOPE not in saved.get("scopes", [])
                ):
                    raise ValueError
                expires = datetime.fromisoformat(saved.get("expiry", "1970-01-01T00:00:00Z"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
            except (OSError, ValueError, TypeError, AttributeError):
                raise RemoteError(
                    "YouTube upload credentials are missing or invalid.", 401
                ) from None
            if not force_refresh and saved.get("token") and expires.timestamp() > time.time() + 300:
                return saved
            response = await self.client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": saved["client_id"],
                    "client_secret": saved["client_secret"],
                    "refresh_token": saved["refresh_token"],
                    "grant_type": "refresh_token",
                },
            )
            try:
                token = response.json()
                if (
                    response.is_error
                    or not isinstance(token, dict)
                    or not token.get("access_token")
                ):
                    raise ValueError
                if not isinstance(token.get("expires_in"), int) or token["expires_in"] <= 0:
                    raise ValueError
                if "scope" in token and UPLOAD_SCOPE not in token["scope"].split():
                    raise ValueError
            except (ValueError, AttributeError):
                raise RemoteError(
                    "Google rejected YouTube authorization; renew OAuth consent.", 401
                ) from None
            saved["token"] = token["access_token"]
            saved["expiry"] = datetime.fromtimestamp(
                time.time() + token["expires_in"], UTC
            ).isoformat()
            if token.get("refresh_token"):
                saved["refresh_token"] = token["refresh_token"]
            save_credentials(self.path, saved)
            return saved

    async def headers(self) -> dict:
        return {"Authorization": "Bearer " + (await self.credentials())["token"]}

    async def initialize(self, metadata: dict, size: int) -> str:
        response = await self.client.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={
                "uploadType": "resumable",
                "part": "snippet,status",
                "notifySubscribers": "false",
            },
            headers={
                **await self.headers(),
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "application/octet-stream",
            },
            json=metadata,
        )
        if response.status_code != 200:
            raise RemoteError("YouTube upload initialization failed.", response.status_code)
        return session_url(response.headers.get("Location"))

    async def status(self, session: str, size: int) -> httpx.Response:
        response = await self.client.put(
            session_url(session),
            content=b"",
            headers={
                **await self.headers(),
                "Content-Length": "0",
                "Content-Range": f"bytes */{size}",
            },
        )
        if response.status_code not in {200, 201, 308}:
            raise RemoteError("YouTube session status unavailable.", response.status_code)
        return response

    async def transfer_chunk(
        self, session: str, path: Path, size: int, offset: int
    ) -> httpx.Response:
        if path.stat().st_size != size or not 0 <= offset < size:
            raise RemoteError("Local video or YouTube transfer offset is invalid.")
        with path.open("rb") as video:
            video.seek(offset)
            content = video.read(min(CHUNK_SIZE, size - offset))
        if not content:
            raise RemoteError("No media bytes available for YouTube transfer.")
        response = await self.client.put(
            session_url(session),
            content=content,
            headers={
                **await self.headers(),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(content)),
                "Content-Range": f"bytes {offset}-{offset + len(content) - 1}/{size}",
            },
        )
        if response.status_code not in {200, 201, 308}:
            raise RemoteError(
                "YouTube transfer interrupted; checking saved session.", response.status_code
            )
        return response

    @staticmethod
    def result(response: httpx.Response) -> dict:
        try:
            resource = response.json()
            if not isinstance(resource, dict):
                raise ValueError
            return resource
        except ValueError:
            raise RemoteError("Invalid YouTube completion response.") from None
