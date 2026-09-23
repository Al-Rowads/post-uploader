import argparse
import asyncio
import hashlib
import logging
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .clients import RemoteError
from .config import load_local_environment
from .tiktok import TikTok, read_credentials, save_credentials, token_response

REDIRECT_URI = "http://127.0.0.1:8765/callback/"
SCOPES = "user.info.basic,video.upload"


def authorization_url(client_key: str, state: str, verifier: str, mode="draft") -> str:
    # TikTok's desktop guide explicitly requires hex SHA-256, unlike base64url PKCE.
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(
        {
            "client_key": client_key,
            "response_type": "code",
            "scope": SCOPES if mode == "draft" else "user.info.basic,video.upload,video.publish",
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(),
            "code_challenge_method": "S256",
        }
    )


def callback_code(target: str, expected_state: str) -> str:
    parsed = urlsplit(target)
    query = parse_qs(parsed.query)
    states = query.get("state", [])
    if (
        parsed.path != "/callback/"
        or len(states) != 1
        or not secrets.compare_digest(states[0], expected_state)
    ):
        raise ValueError("Invalid authorization state or callback path.")
    if query.get("error"):
        raise ValueError("TikTok authorization was declined or unavailable.")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise ValueError("TikTok did not return an authorization code.")
    return codes[0]


def authorize(path: Path, mode="direct"):
    credentials = read_credentials(path)
    if not all(credentials.get(key) for key in ("client_key", "client_secret")):
        raise RemoteError(
            "Save the app client_key and client_secret in the credentials file first.", 401
        )
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    received = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *arguments):
            pass

        def do_GET(self):
            try:
                code = callback_code(self.path, state)
            except ValueError:
                self.send_response(400)
                body = b"Authorization failed. Return to the terminal and try again."
            else:
                received["code"] = code
                self.send_response(200)
                body = b"Authorization received. Return to the terminal for verification."
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 8765), Callback) as server:
        server.timeout = 1
        print("Open this URL in Safari and authorize the intended TikTok account:", flush=True)
        print(authorization_url(credentials["client_key"], state, verifier, mode), flush=True)
        deadline = time.monotonic() + 600
        while "code" not in received and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in received:
        raise RemoteError("Timed out waiting for TikTok authorization.")
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.post(
            "https://open.tiktokapis.com/v2/oauth/token/",
            data={
                "client_key": credentials["client_key"],
                "client_secret": credentials["client_secret"],
                "grant_type": "authorization_code",
                "code": received["code"],
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
    save_credentials(
        path,
        token_response(
            response, credentials, "video.publish" if mode == "direct" else "video.upload"
        ),
    )
    print("TikTok upload authorization saved. No video was uploaded.")


async def check(path: Path, mode="direct"):
    tiktok = TikTok(path)
    try:
        await tiktok.credentials("video.publish" if mode == "direct" else "video.upload")
        if mode == "direct":
            await tiktok.creator_info()
        account = await tiktok.account()
        print(f"TikTok: {account.get('display_name', account['open_id'])}")
        print(f"TikTok {mode} token and account verified. No video was uploaded.")
        print("App review and live publication or draft delivery are separate checks.")
    finally:
        await tiktok.client.aclose()


def main():
    load_local_environment()
    parser = argparse.ArgumentParser(description="Authorize native TikTok posting")
    parser.add_argument("command", choices=("authorize", "check"))
    parser.add_argument("--mode", choices=("direct", "draft"), default="direct")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path(os.environ.get("TIKTOK_CREDENTIALS_FILE", "data/tiktok-credentials.json")),
    )
    arguments = parser.parse_args()
    os.umask(0o077)
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        if arguments.command == "authorize":
            authorize(arguments.credentials, arguments.mode)
        else:
            asyncio.run(check(arguments.credentials, arguments.mode))
    except RemoteError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, httpx.HTTPError, ValueError) as error:
        print(f"TikTok authorization could not complete ({type(error).__name__}).", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
