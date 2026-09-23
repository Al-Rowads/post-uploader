import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .clients import RemoteError
from .domain import Outcome


def read_credentials(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (OSError, ValueError):
        raise RemoteError(
            "TikTok credentials missing or invalid; run the TikTok authorization tool.", 401
        ) from None


def save_credentials(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tiktok-")
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(payload, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def token_response(response: httpx.Response, previous: dict, required_scope="video.upload") -> dict:
    try:
        payload = response.json()
        if response.is_error or not isinstance(payload, dict) or payload.get("error"):
            raise ValueError
        for key in ("access_token", "refresh_token", "open_id", "scope"):
            if not isinstance(payload.get(key), str) or not payload[key]:
                raise ValueError
        if required_scope not in payload["scope"].split(","):
            raise ValueError
        if previous.get("open_id") and payload["open_id"] != previous["open_id"]:
            raise ValueError
        for key in ("expires_in", "refresh_expires_in"):
            if not isinstance(payload.get(key), int) or payload[key] <= 0:
                raise ValueError
    except (ValueError, TypeError):
        raise RemoteError(
            f"TikTok token exchange failed; authorize {required_scope} again.", 401
        ) from None
    return dict(
        previous,
        **payload,
        expires_at=time.time() + payload["expires_in"],
        refresh_expires_at=time.time() + payload["refresh_expires_in"],
    )


def chunk_plan(size: int) -> tuple[int, int]:
    if not 0 < size <= 2_000_000_000:
        raise ValueError("TikTok upload size must be between 1 and 2000000000 bytes.")
    chunk = min(size, 10_000_000)
    return chunk, max(1, size // chunk)


def validate_upload_url(value: object) -> str:
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.fragment
            or not (parsed.hostname or "").endswith(".tiktokapis.com")
        ):
            raise ValueError
    except ValueError:
        raise RemoteError("TikTok returned an invalid upload destination.") from None
    return value


def draft_outcome(data: dict) -> Outcome:
    status = data.get("status")
    if status == "SEND_TO_USER_INBOX":
        return Outcome(
            "needs_action",
            "Draft delivered. Open TikTok inbox, add the caption and "
            "disclosures, choose Everyone, then publish. This is not a public post yet.",
        )
    if status == "PUBLISH_COMPLETE":
        identifiers = data.get("publicaly_available_post_id")
        if isinstance(identifiers, list) and identifiers and str(identifiers[0]).isdigit():
            return Outcome(
                "published", "TikTok confirmed public publication.", post_id=str(identifiers[0])
            )
        return Outcome("needs_action", "Posted in TikTok; public visibility is not confirmed.")
    if status == "FAILED":
        # Avoid echoing arbitrary remote text or credential-bearing URLs.
        reason = data.get("fail_reason", "unknown")
        reason = (
            reason if isinstance(reason, str) and reason.replace("_", "").isalnum() else "unknown"
        )
        return Outcome("failed", f"TikTok upload failed ({reason}).")
    return Outcome("pending", "TikTok is processing the draft.")


class TikTok:
    def __init__(self, credentials_path: Path):
        self.path = credentials_path
        self.lock = asyncio.Lock()
        self.request_lock = asyncio.Lock()
        self.next_request: dict[str, float] = {}
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=20), follow_redirects=False, trust_env=False
        )

    async def credentials(self, required_scope="video.upload") -> dict:
        async with self.lock:
            saved = read_credentials(self.path)
            if required_scope not in str(saved.get("scope", "")).split(","):
                raise RemoteError(f"Authorize TikTok with the {required_scope} scope first.", 401)
            if saved.get("expires_at", 0) > time.time() + 300 and saved.get("access_token"):
                return saved
            if saved.get("refresh_expires_at", 0) <= time.time() or not all(
                saved.get(k) for k in ("client_key", "client_secret", "refresh_token")
            ):
                raise RemoteError("TikTok authorization expired; authorize the account again.", 401)
            response = await self.client.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                data={
                    "client_key": saved["client_key"],
                    "client_secret": saved["client_secret"],
                    "grant_type": "refresh_token",
                    "refresh_token": saved["refresh_token"],
                },
            )
            saved = token_response(response, saved, required_scope)
            save_credentials(self.path, saved)
            return saved

    async def api(self, endpoint: str, *, data: dict | None = None, scope="video.upload") -> dict:
        credentials = await self.credentials(scope)
        headers = {"Authorization": f"Bearer {credentials['access_token']}"}
        url = "https://open.tiktokapis.com/v2/" + endpoint
        async with self.request_lock:
            delay = self.next_request.get(endpoint, 0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            interval = 10.1 if endpoint.endswith("video/init/") else 3.1
            self.next_request[endpoint] = time.monotonic() + interval
            response = (
                await self.client.get(url, headers=headers)
                if data is None
                else await self.client.post(url, headers=headers, json=data)
            )
        try:
            payload = response.json()
            error = payload.get("error", {})
            if response.is_error or error.get("code") != "ok":
                code = error.get("code", "unknown")
                code = (
                    code if isinstance(code, str) and code.replace("_", "").isalnum() else "unknown"
                )
                raise RemoteError(
                    f"TikTok request failed ({code}).",
                    response.status_code if response.status_code >= 400 else 400,
                    3600 if response.status_code == 429 else 60,
                )
            if not isinstance(payload.get("data"), dict):
                raise ValueError
            return payload["data"]
        except (ValueError, AttributeError):
            raise RemoteError(
                "TikTok returned an invalid response.", response.status_code
            ) from None

    async def account(self) -> dict:
        data = await self.api("user/info/?fields=open_id,display_name", scope="user.info.basic")
        user = data.get("user")
        if not isinstance(user, dict) or not user.get("open_id"):
            raise RemoteError("TikTok account response is incomplete.", 401)
        return user

    async def initialize(self, size: int) -> dict:
        chunk, count = chunk_plan(size)
        data = await self.api(
            "post/publish/inbox/video/init/",
            data={
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk,
                    "total_chunk_count": count,
                }
            },
        )
        if not isinstance(data.get("publish_id"), str) or not data["publish_id"]:
            raise RemoteError("TikTok did not return an upload identifier.")
        validate_upload_url(data.get("upload_url"))
        return data

    async def transfer(self, path: Path, upload_url: str, size: int, mime_type: str):
        url = validate_upload_url(upload_url)
        chunk, count = chunk_plan(size)
        with path.open("rb") as video:
            for index in range(count):
                start = index * chunk
                length = size - start if index == count - 1 else chunk
                content = video.read(length)
                if len(content) != length:
                    raise RemoteError("Video changed during TikTok transfer.")
                response = await self.client.put(
                    url,
                    content=content,
                    headers={
                        "Content-Type": mime_type,
                        "Content-Length": str(length),
                        "Content-Range": f"bytes {start}-{start + length - 1}/{size}",
                    },
                )
                if response.status_code != (201 if index == count - 1 else 206):
                    raise RemoteError("TikTok transfer interrupted; checking the existing upload.")

    async def status(self, publish_id: str, *, direct=False) -> dict:
        return await self.api(
            "post/publish/status/fetch/",
            data={"publish_id": publish_id},
            scope="video.publish" if direct else "video.upload",
        )

    async def creator_info(self) -> dict:
        data = await self.api("post/publish/creator_info/query/", data={}, scope="video.publish")
        options = data.get("privacy_level_options")
        if (
            not isinstance(options, list)
            or not options
            or any(
                not isinstance(option, str) or option not in PRIVACY_LEVELS for option in options
            )
            or not isinstance(data.get("creator_nickname"), str)
            or type(data.get("max_video_post_duration_sec")) is not int
            or data["max_video_post_duration_sec"] <= 0
            or any(
                type(data.get(key)) is not bool
                for key in ("comment_disabled", "duet_disabled", "stitch_disabled")
            )
        ):
            raise RemoteError("TikTok creator information is incomplete.")
        return data

    async def initialize_direct(self, post_info: dict, video_url: str) -> dict:
        data = await self.api(
            "post/publish/video/init/",
            scope="video.publish",
            data={
                "post_info": post_info,
                "source_info": {"source": "PULL_FROM_URL", "video_url": video_url},
            },
        )
        if not isinstance(data.get("publish_id"), str) or not data["publish_id"]:
            raise RemoteError("TikTok did not return a Direct Post identifier.")
        return data


PRIVACY_LEVELS = {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"}


def direct_post_info(values: dict, creator: dict, mode: str) -> dict:
    from .domain import validate_platform_caption

    if mode not in {"private_test", "approved"}:
        raise ValueError("TikTok Direct Post is disabled.")
    if values.get("consent") is not True:
        raise ValueError("Confirm TikTok publishing consent first.")
    privacy = values.get("privacy_level")
    if privacy not in creator["privacy_level_options"]:
        raise ValueError("Choose one of the currently available privacy options.")
    if mode == "private_test" and privacy != "SELF_ONLY":
        raise ValueError("Unaudited mode only permits Only me on a private TikTok account.")
    caption = validate_platform_caption(values.get("caption"))
    fields = (
        "allow_comment",
        "allow_duet",
        "allow_stitch",
        "commercial_content",
        "brand_organic_toggle",
        "brand_content_toggle",
        "is_aigc",
    )
    if any(type(values.get(key)) is not bool for key in fields):
        raise ValueError("Select the interaction and content-disclosure settings.")
    commercial = values["commercial_content"]
    branded, own_brand = values["brand_content_toggle"], values["brand_organic_toggle"]
    if commercial != (branded or own_brand):
        raise ValueError("Commercial content requires Your brand, Branded content, or both.")
    if branded and privacy == "SELF_ONLY":
        raise ValueError("Branded content cannot use Only me visibility.")
    result = {
        "title": caption,
        "privacy_level": privacy,
        "brand_content_toggle": branded,
        "brand_organic_toggle": own_brand,
        "is_aigc": values["is_aigc"],
    }
    for interaction in ("comment", "duet", "stitch"):
        enabled = values[f"allow_{interaction}"]
        if enabled and creator[f"{interaction}_disabled"]:
            raise ValueError(f"TikTok has disabled {interaction}; refresh the form.")
        result[f"disable_{interaction}"] = not enabled
    return result


def direct_outcome(data: dict, privacy: str) -> Outcome:
    if data.get("status") == "PUBLISH_COMPLETE" and privacy != "PUBLIC_TO_EVERYONE":
        return Outcome("needs_action", f"TikTok posted with the selected visibility: {privacy}.")
    outcome = draft_outcome(data)
    if outcome.state == "pending":
        return Outcome("pending", "TikTok is processing the post.")
    return outcome
