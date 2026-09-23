import email.utils
import time
from pathlib import Path

import httpx

from .config import Config


class RemoteError(Exception):
    def __init__(self, message: str, status: int = 0, retry_after: float = 60):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def retry_delay(response: httpx.Response, payload: dict) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return max(1, float(value))
        except ValueError:
            try:
                return max(1, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    parameters = payload.get("parameters", {})
    if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), (int, float)):
        return max(1, parameters["retry_after"])
    return 3600 if response.status_code == 429 else 60


def decode_response(response: httpx.Response, config: Config) -> dict:
    try:
        payload = response.json()
    except ValueError:
        raise RemoteError(
            "Remote server returned a non-JSON response.", response.status_code
        ) from None
    if not isinstance(payload, dict):
        raise RemoteError(
            "Remote server returned an unexpected response shape.", response.status_code
        )
    if response.is_error:
        message = payload.get("message") or payload.get("description") or payload.get("error")
        raise RemoteError(
            config.redact(str(message or f"HTTP {response.status_code}")),
            response.status_code,
            retry_delay(response, payload),
        )
    return payload


class Telegram:
    def __init__(self, config: Config):
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=f"{config.telegram_url}/bot{config.telegram_token}/",
            timeout=httpx.Timeout(65, connect=10),
            trust_env=False,
        )

    async def call(self, method: str, **parameters):
        timeout = parameters.pop("_timeout", None)
        options = {"timeout": timeout} if timeout is not None else {}
        response = await self.client.post(method, json=parameters, **options)
        payload = decode_response(response, self.config)
        if payload.get("ok") is not True:
            raise RemoteError(
                self.config.redact(str(payload.get("description", "Telegram error"))),
                payload.get("error_code", 0),
                retry_delay(response, payload),
            )
        return payload["result"]

    async def get_updates(self, offset: int):
        return await self.call(
            "getUpdates",
            offset=offset,
            timeout=30,
            allowed_updates=["message", "edited_message", "callback_query"],
        )

    async def download(self, file_id: str) -> Path:
        result = await self.call(
            "getFile", file_id=file_id, _timeout=httpx.Timeout(3600, connect=10)
        )
        path = result.get("file_path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise RemoteError("Local Bot API did not return an absolute file path.")
        return Path(path)

    async def send(self, chat_id: int, text: str, **parameters):
        return await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=self.config.redact(text, limit=4000),
            link_preview_options={"is_disabled": True},
            **parameters,
        )

    async def channel(self, channel_id: str | int) -> dict:
        if not channel_id:
            raise RemoteError("Set TELEGRAM_CHANNEL_ID before enabling Telegram publishing.", 400)
        chat = await self.call("getChat", chat_id=channel_id)
        bot = await self.call("getMe")
        member = await self.call("getChatMember", chat_id=chat["id"], user_id=bot["id"])
        if chat.get("type") != "channel" or not (
            member.get("status") == "creator"
            or member.get("status") == "administrator"
            and member.get("can_post_messages") is True
        ):
            raise RemoteError(
                "The bot needs channel administrator permission to post messages.", 403
            )
        return {
            "chat_id": chat["id"],
            "username": chat.get("username"),
            "title": chat.get("title", str(chat["id"])),
        }

    async def send_media(self, chat_id, media, caption, *, reply_markup=None, message_id=None):
        kind = media["kind"]
        # Telegram file IDs cannot change type: a document remains a document.
        parameters = {"chat_id": chat_id, "reply_markup": reply_markup or {"inline_keyboard": []}}
        if message_id is not None:
            return await self.call(
                "editMessageMedia",
                **parameters,
                message_id=message_id,
                media={"type": kind, "media": media["file_id"], "caption": caption},
            )
        return await self.call(
            "sendVideo" if kind == "video" else "sendDocument",
            **parameters,
            **{kind: media["file_id"]},
            caption=caption,
        )


class Publisher:
    def __init__(self, config: Config):
        self.config = config
        self.client = httpx.AsyncClient(
            base_url="https://api.upload-post.com",
            headers={"Authorization": f"Apikey {config.upload_post_key}"},
            timeout=httpx.Timeout(connect=20, read=300, write=120, pool=20),
            follow_redirects=False,
        )

    async def get(self, path: str, **parameters) -> dict:
        response = await self.client.get(path, params=parameters)
        payload = decode_response(response, self.config)
        if payload.get("success") is False:
            raise RemoteError(
                self.config.redact(
                    str(payload.get("message") or payload.get("error") or "Provider error")
                ),
                400,
            )
        return payload

    async def connections(self) -> dict:
        payload = await self.get("/api/uploadposts/users")
        profiles = payload.get("profiles")
        if not isinstance(profiles, list):
            raise RemoteError("Upload-Post profiles response is missing its profiles list.")
        profile = next(
            (
                item
                for item in profiles
                if isinstance(item, dict) and item.get("username") == self.config.profile
            ),
            None,
        )
        if profile is None:
            raise RemoteError("Configured Upload-Post profile was not found.", 400)
        accounts = profile.get("social_accounts")
        if not isinstance(accounts, dict):
            raise RemoteError("Upload-Post profile has no social account information.", 400)
        return accounts

    def upload_fields(
        self, job, platforms: list[str], request_id: str, declarations: dict, settings: dict
    ) -> dict:
        if platforms != ["youtube"]:
            raise ValueError("New Upload-Post submissions are restricted to YouTube.")
        fields = {
            "user": self.config.profile,
            "platform[]": platforms,
            "title": job["title"],
            "async_upload": "true",
            "request_id": request_id,
            "external_id": f"telegram:{job['chat_id']}:{job['message_id']}",
        }
        if "youtube" in platforms:
            fields.update(
                youtube_title=job["title"],
                youtube_description=job["caption"],
                privacyStatus="public",
                categoryId="22",
            )
            for key in (
                "selfDeclaredMadeForKids",
                "containsSyntheticMedia",
                "hasPaidProductPlacement",
            ):
                fields[key] = str(declarations[key]).lower()
        return fields

    async def upload(self, path: Path, fields: dict) -> dict:
        # Passing a file handle lets HTTPX stream multipart content with bounded memory.
        with path.open("rb") as video:
            response = await self.client.post(
                "/api/upload",
                data=fields,
                files={"video": (path.name, video, "application/octet-stream")},
                headers={"Idempotency-Key": fields["request_id"]},
            )
        return decode_response(response, self.config)

    async def status(self, request_id: str) -> dict:
        return await self.get("/api/uploadposts/status", request_id=request_id)

    async def history(self, request_id: str) -> dict:
        return await self.get("/api/uploadposts/history", request_id=request_id, limit=100)
