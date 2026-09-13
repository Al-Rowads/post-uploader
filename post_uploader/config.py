import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


def load_local_environment(path: Path = Path(".env")):
    """Read literal KEY=value settings without evaluating shell commands or expansions."""
    if not path.is_file():
        return
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name = name.strip()
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            parts = ["", ""]
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or len(parts) > 1:
            raise ConfigurationError(f"Invalid .env setting on line {number}.")
        os.environ.setdefault(name, parts[0] if parts else "")


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Set {name} before starting the bot.")
    return value


def boolean(name: str) -> bool:
    value = required(name).lower()
    if value not in {"true", "false"}:
        raise ConfigurationError(f"{name} must be explicitly set to true or false.")
    return value == "true"


def positive_integer(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a positive integer.") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer.")
    return value


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(repr=False)
    owner_id: int
    upload_post_key: str = field(repr=False)
    profile: str
    declarations: dict[str, bool]
    telegram_url: str = "http://telegram-bot-api:8081"
    telegram_files: Path = Path("/var/lib/telegram-bot-api")
    data_directory: Path = Path("/data")
    max_video_bytes: int = 2_000_000_000
    disk_reserve_bytes: int = 2_000_000_000
    media_retention_hours: int = 72
    max_pending_jobs: int = 100
    tiktok_credentials_file: Path = Path("/data/tiktok-credentials.json")
    youtube_credentials_file: Path = Path("/data/youtube-credentials.json")
    youtube_privacy: str = "private"

    @classmethod
    def from_environment(cls) -> "Config":
        token = required("TELEGRAM_BOT_TOKEN")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ConfigurationError("TELEGRAM_BOT_TOKEN is not a BotFather token.")
        owner = positive_integer("TELEGRAM_ALLOWED_USER_ID", 0)
        endpoint = os.environ.get("TELEGRAM_API_URL", cls.telegram_url).rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ConfigurationError("TELEGRAM_API_URL must be an HTTP(S) server URL.")
        if parsed.query or parsed.fragment or parsed.path:
            raise ConfigurationError("TELEGRAM_API_URL must not contain a path or query.")
        if parsed.hostname == "api.telegram.org":
            raise ConfigurationError("Use the local Bot API server for large-file downloads.")
        declarations = {
            "selfDeclaredMadeForKids": boolean("YOUTUBE_MADE_FOR_KIDS"),
            "containsSyntheticMedia": boolean("YOUTUBE_CONTAINS_SYNTHETIC_MEDIA"),
            "hasPaidProductPlacement": boolean("YOUTUBE_PAID_PRODUCT_PLACEMENT"),
        }
        maximum = positive_integer("MAX_VIDEO_BYTES", cls.max_video_bytes)
        privacy = required("YOUTUBE_PRIVACY_STATUS")
        if privacy not in {"private", "unlisted", "public"}:
            raise ConfigurationError("YOUTUBE_PRIVACY_STATUS must be private, unlisted, or public.")
        if maximum > cls.max_video_bytes:
            raise ConfigurationError("MAX_VIDEO_BYTES must not exceed 2000000000 in this version.")
        return cls(
            telegram_token=token,
            owner_id=owner,
            upload_post_key=os.environ.get("UPLOAD_POST_API_KEY", ""),
            profile=os.environ.get("UPLOAD_POST_PROFILE", ""),
            declarations=declarations,
            telegram_url=endpoint,
            telegram_files=Path(os.environ.get("TELEGRAM_FILES_DIRECTORY", cls.telegram_files)),
            data_directory=Path(os.environ.get("DATA_DIRECTORY", cls.data_directory)),
            max_video_bytes=maximum,
            disk_reserve_bytes=positive_integer("DISK_RESERVE_BYTES", cls.disk_reserve_bytes),
            media_retention_hours=positive_integer(
                "MEDIA_RETENTION_HOURS", cls.media_retention_hours
            ),
            max_pending_jobs=positive_integer("MAX_PENDING_JOBS", cls.max_pending_jobs),
            tiktok_credentials_file=Path(required("TIKTOK_CREDENTIALS_FILE")),
            youtube_credentials_file=Path(required("YOUTUBE_CREDENTIALS_FILE")),
            youtube_privacy=privacy,
        )

    def redact(self, message: str, limit: int = 700) -> str:
        for secret in (self.telegram_token, self.upload_post_key):
            if secret:
                message = message.replace(secret, "[redacted]")
        message = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]+", "[redacted]", message)
        return message[:limit]
