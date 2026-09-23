import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

import httpx

from .clients import RemoteError, Telegram
from .config import Config, ConfigurationError, load_local_environment, required
from .database import Database
from .openrouter import OpenRouter
from .service import Service
from .tiktok import TikTok
from .youtube import YouTube


async def doctor(config: Config):
    telegram, youtube = Telegram(config), YouTube(config.youtube_credentials_file)
    tiktok, openrouter = TikTok(config.tiktok_credentials_file), OpenRouter(config)
    try:
        bot = await telegram.call("getMe")
        print(f"Telegram bot: @{bot['username']}")
        if not config.telegram_files.is_dir():
            raise ConfigurationError("Shared Telegram files directory is missing.")
        await openrouter.check_model()
        print(f"OpenRouter model available: {config.openrouter_model} (no generation requested)")
        if config.youtube_credentials_file.is_file():
            await youtube.credentials(force_refresh=True)
            print(f"YouTube authorization refreshed (visibility: {config.youtube_privacy})")
        else:
            print("YouTube: credentials not configured")
        if config.telegram_channel_id:
            channel = await telegram.channel(config.telegram_channel_id)
            print(f"Telegram channel posting permission verified: {channel['title']}")
        else:
            print("Telegram channel: not configured")
        if config.tiktok_direct_mode != "disabled":
            if not config.public_site_url or not config.tiktok_media_verified:
                raise ConfigurationError(
                    "TikTok requires PUBLIC_SITE_URL and TIKTOK_MEDIA_VERIFIED."
                )
            await tiktok.credentials("video.publish")
            creator = await tiktok.creator_info()
            print(
                f"TikTok Direct Post: {creator['creator_nickname']} ({config.tiktok_direct_mode})"
            )
            print(
                "Domain verification and public posting eligibility are separate operator checks."
            )
        else:
            print("TikTok Direct Post: disabled")
        print("No video was published and no caption generation was charged.")
    finally:
        await telegram.client.aclose()
        await youtube.client.aclose()
        await tiktok.client.aclose()
        await openrouter.client.aclose()


async def check_youtube(path: Path):
    youtube = YouTube(path)
    try:
        await youtube.credentials(force_refresh=True)
        print("YouTube upload OAuth refresh verified. No video was uploaded.")
    finally:
        await youtube.client.aclose()


async def logout_cloud():
    token = required("TELEGRAM_BOT_TOKEN")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/deleteWebhook",
            json={"drop_pending_updates": False},
        )
        if response.status_code != 200 or response.json().get("ok") is not True:
            raise ConfigurationError("Could not remove the cloud webhook; verify the bot token.")
        response = await client.post(f"https://api.telegram.org/bot{token}/logOut")
        if response.status_code != 200 or response.json().get("ok") is not True:
            raise ConfigurationError("Cloud logOut failed; the bot may already be logged out.")
    print("Bot logged out of Telegram's cloud API. Start the local Bot API service.")


def healthcheck():
    path = Path(os.environ.get("DATA_DIRECTORY", "/data")) / "jobs.sqlite3"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            heartbeat = database.execute(
                "SELECT value FROM settings WHERE key='poller_heartbeat'"
            ).fetchone()
        return 0 if heartbeat and time.time() - float(heartbeat[0]) < 180 else 1
    except (sqlite3.Error, ValueError):
        return 1


async def run_service(config: Config):
    config.data_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (config.data_directory / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigurationError("Another bot instance is using this data directory.") from None
        database = Database(config.data_directory / "jobs.sqlite3")
        identity = json.dumps(
            {
                "bot": config.telegram_token.split(":")[0],
                "owner": config.owner_id,
                "profile": config.profile,
            },
            sort_keys=True,
        )
        saved_identity = database.get_setting("identity")
        if saved_identity and saved_identity != identity:
            database.connection.close()
            raise ConfigurationError(
                "This database belongs to a different bot, owner, or Upload-Post profile. "
                "Restore the original configuration or use a separate data directory."
            )
        database.set_setting("identity", identity)
        service = Service(config, database)
        task = asyncio.create_task(service.run())
        loop = asyncio.get_running_loop()
        for event in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(event, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            await service.close()
            database.connection.close()


def main():
    parser = argparse.ArgumentParser(
        description="Review and publish videos to YouTube, TikTok, and Telegram"
    )
    parser.add_argument(
        "command",
        choices=("run", "doctor", "youtube-check", "logout-cloud", "healthcheck"),
        nargs="?",
        default="run",
    )
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTPX includes Telegram's bot token in request URLs at INFO level.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    os.umask(0o077)
    try:
        load_local_environment()
        if arguments.command == "healthcheck":
            sys.exit(healthcheck())
        if arguments.command == "youtube-check":
            asyncio.run(check_youtube(Path(required("YOUTUBE_CREDENTIALS_FILE"))))
        elif arguments.command == "logout-cloud":
            asyncio.run(logout_cloud())
        else:
            config = Config.from_environment()
            asyncio.run(doctor(config) if arguments.command == "doctor" else run_service(config))
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except RemoteError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # Raw transport exceptions can contain credential-bearing URLs. Keep diagnostics safe.
        errors = list(error.exceptions) if isinstance(error, ExceptionGroup) else [error]
        for item in errors:
            # Transport errors can include TikTok's signed media-upload URL.
            print(f"Service stopped ({type(item).__name__}).", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
