# Level13 Publisher

A private Telegram bot that uploads original videos directly to YouTube and sends TikTok
drafts to the connected account’s inbox. New uploads no longer use Upload-Post. YouTube is
configured **private** locally; review in YouTube Studio before public publication. TikTok
requires manual caption, disclosure, visibility, and publishing choices inside TikTok.

## Current setup

- Local `.env` points to real, gitignored credential files under `data/`, with mode `0600`.
- The supplied Google desktop client matches the saved YouTube OAuth grant. Its upload-only
  authorization was refreshed successfully on September 13, 2026. No video was uploaded.
- TikTok app **Level13 Publisher** and sandbox **Level13 Draft Uploads** exist. Sandbox app
  details, URLs, Desktop Login Kit, and draft Content Posting API settings have been saved.
  Target account **@al.rowads** is registered. Upload OAuth authorization is saved and the
  live basic-profile API check verified `video.upload` access on September 13, 2026.
  This is sandbox access; production approval and real draft delivery remain separate.
- The owner explicitly set all three YouTube content declarations to `false` in `.env`.
  Telegram credentials still need values. The bot has not been started against a live Telegram account.
- Information pages for `https://upload.al-rowads.com` are ready in `website/dist/`.
  They have not been deployed. See [website deployment](website/README.md).

See [TikTok setup](docs/tiktok-setup.md) for the exact remaining account steps.

## Local account checks

Python 3.13+ is required. The existing `.venv` includes the locked HTTPX dependency.
Commands automatically read the local `.env`; existing exported variables take precedence.

```sh
.venv/bin/python -m post_uploader youtube-check
.venv/bin/python -m post_uploader.tiktok_auth authorize
.venv/bin/python -m post_uploader.tiktok_auth check
```

The first command refreshes Google authorization without uploading. TikTok authorization
opens a loopback callback on port 8765 and prints a URL to open in Safari. Finish sandbox
target registration before running it. The credential file’s parent must be writable because
refreshes atomically replace the file. Never commit, print, or upload the credentials.

Google OAuth testing grants with the upload scope can expire after seven days. If Google
rejects refresh, repeat the Google desktop OAuth consent flow with the supplied client and
replace the authorized-user JSON while the bot is stopped. Google project testing and YouTube’s
API audit/private-only restriction are separate: a token does not prove public publishing access.

## Configure the bot

Keep the existing local `.env`. For a fresh checkout, copy `.env.example` to `.env` and give it
mode `0600`. Fill these settings before starting:

| Setting | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_ALLOWED_USER_ID` | Numeric owner ID; only private messages from this user are accepted |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Telegram application credentials from my.telegram.org for the local Bot API |
| `YOUTUBE_CREDENTIALS_FILE` | Writable Google authorized-user JSON, including refresh token |
| `YOUTUBE_CLIENT_SECRETS_FILE` | Saved original desktop OAuth client for future reauthorization; not the runtime token |
| `TIKTOK_CREDENTIALS_FILE` | Writable JSON containing TikTok app credentials and authorized user tokens |
| `YOUTUBE_PRIVACY_STATUS` | `private`, `unlisted`, or `public`; local setup uses `private` |
| `YOUTUBE_MADE_FOR_KIDS` | Explicit `true` or `false` for this owner’s videos |
| `YOUTUBE_CONTAINS_SYNTHETIC_MEDIA` | Explicit `true` or `false` for this owner’s videos |
| `YOUTUBE_PAID_PRODUCT_PLACEMENT` | Explicit declaration; `true` is rejected by this native path, so use Studio for paid-promotion uploads |

Content declarations are captured when each job arrives. Visibility uses the operator’s
current configuration when upload initialization occurs. Changing configuration requires a
restart. Do not guess declarations for videos that differ; use the platforms’ own interfaces.

Optional `UPLOAD_POST_API_KEY` and `UPLOAD_POST_PROFILE` are used only for historical request
reconciliation. Existing databases retain their original Upload-Post identity and attempts.
No native error falls back to that service.

## Linux deployment

Use the existing Docker Compose deployment and a local Telegram Bot API server for large media.
The bot runs as UID/GID 101. Securely transfer the repository’s `data/` credential directory to
the server, make it writable by UID/GID 101, and restrict it to that user (`0700` directory,
`0600` files). Compose mounts this directory at `/credentials` and overrides the Mac credential
paths. The existing named `bot-data` and `telegram-media` volumes remain persistent.

```sh
docker compose build bot
docker compose run --rm --no-deps bot python -m post_uploader logout-cloud
docker compose up -d telegram-bot-api
docker compose run --rm --no-deps bot python -m post_uploader doctor
docker compose up -d bot
```

Run `logout-cloud` only when migrating from Telegram’s hosted API, not at each restart. It
removes the hosted webhook without dropping updates and logs the bot out of the hosted API.
`doctor` checks both platform authorizations and Telegram access without submitting media.
The information website is separate static content; never expose the repository or `data/`
as its document root. Configure HTTPS for `upload.al-rowads.com` as described in the website README.

## Sending videos

Send the bot a video or video document with a caption. The first nonempty line is the YouTube
title (up to 100 characters); the full caption is its description and is returned for copying
into TikTok (up to 2,200 characters, and at most 5,000 UTF-8 bytes for YouTube). The bot does not
rewrite captions or modify videos. Every album item is a separate job. Reply with missing
caption text before any submission begins.

Files must be at most 2,000,000,000 bytes. `ffprobe` validates the original media. TikTok checks
container, codecs, dimensions, frame rate, and a conservative 3–600 second duration range;
TikTok may enforce additional account restrictions. An ineligible TikTok file can still go to YouTube.

| Command | Behavior |
| --- | --- |
| `/status [job_id]` | Queue status and destination links |
| `/retry <job_id>` | Retry confirmed failures; recheck uncertain attempts without duplicating uploads |
| `/cancel <job_id>` | Cancel before submission |
| `/pause`, `/resume` | Pause/resume new submissions; active transfers continue |
| `/help` | Caption and upload instructions |

Re-sending a video as a new Telegram message intentionally creates another job. Redelivery of
the same Telegram message does not. A private YouTube upload or delivered TikTok draft is
`needs_action`, not proof of public publication. Later manual publication is not tracked.

## Recovery and data handling

YouTube initialization saves its resumable session URL before transferring media. The poller
queries the same session for its acknowledged byte offset and sends one 8 MiB chunk at a time.
A lost completion response is recovered by querying that session. Expired or ambiguous sessions
are marked unknown rather than automatically creating a duplicate upload. Initialization failures
before any media is sent can be explicitly retried.

TikTok saves its `publish_id` before media transfer, then polls it until inbox delivery or a
terminal result. Interrupted TikTok bytes are not automatically replayed. A lost initialization
response has no documented client-ID lookup; it is kept uncertain. Finish successful drafts
inside TikTok. Both destinations keep independent state across restarts.

Schema version 3 adds native-provider tracking and the YouTube resumable session URL while
preserving historical attempts. Treat SQLite and backups as secrets because active sessions
are sensitive. Stop the service for filesystem backups or use SQLite’s online backup API.
Do not use `docker compose down -v` as a restart procedure.

Only one process may own a queue. The database is bound to its Telegram owner/bot and original
legacy profile, plus the authorized TikTok account. Do not replace a Google authorization with
another channel’s grant while queued uploads exist; use a separate data directory for another
channel. The upload-only grant does not let the bot independently list the channel’s identity.

Media is normally retained for 72 hours unless an active upload still needs it. Successfully
public destinations can trigger earlier cleanup. Queue history and credentials remain until
removed by the operator; see [data deletion](website/dist/data-deletion/index.html). Cleanup
never deletes original Telegram messages or destination-platform posts. Keep original media.

Other limits are documented in `.env.example`. Reserve enough disk for concurrent video files;
monitor storage and `/status`. A healthy process heartbeat does not prove platform availability.

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
ruff check post_uploader tests
```

Tests cover real SQLite migrations/restarts, duplicate prevention, captions, cleanup, callback
validation, native protocol parsing, and restricted credential writes. Set `MEDIA_TEST_VIDEO`
to an existing real video to enable the `ffprobe` integration test. No live video has been sent;
a user-selected real clip and completed account setup are required to verify delivery.

**Learning Notes:** Resumable YouTube sessions identify an upload separately from the OAuth
token. The database commits that session before any video bytes leave the worker.

**Why This Matters:** Restarting after a lost response can resume or confirm the same upload
instead of creating an accidental duplicate. Draft/private results stay distinct from public success.

## Primary references

- [YouTube resumable uploads](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol)
- [YouTube video insert fields and audit restriction](https://developers.google.com/youtube/v3/docs/videos/insert)
- [Google OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
- [TikTok draft uploads](https://developers.tiktok.com/docs/en/content-posting-api-reference-upload-video)
- [TikTok upload status](https://developers.tiktok.com/docs/en/content-posting-api-reference-get-video-status)
- [TikTok desktop OAuth](https://developers.tiktok.com/docs/en/login-kit-desktop)
- [Telegram local Bot API](https://github.com/tdlib/telegram-bot-api#usage)
