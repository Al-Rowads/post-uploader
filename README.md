# Level13 Publisher

A private Telegram bot for reviewing and publishing original videos to YouTube Shorts,
a Telegram channel, and optionally TikTok Direct Post. OpenRouter adapts a master caption
for each active platform. Only the configured Telegram owner can operate the bot.

## Review and publish

1. Use `/platforms` to choose destinations for future videos. YouTube is initially selected;
   Telegram and TikTok require explicit activation after setup.
2. Send a video or video document. Reply to the bot's title prompt, then its master-caption
   prompt. An attached video caption never skips these steps. Each album item is a separate job.
3. OpenRouter generates captions in the master caption's language. With three active
   platforms, the bot sends three separate video messages, each with its caption and
   **Accept / Reject** inline buttons.
4. **Accept** queues only that platform. TikTok opens a small in-Telegram form for account,
   visibility, interactions, disclosures, and final **Publish** consent.
5. **Reject** reveals **Reject caption / Reject video**. Reply to the corresponding prompt.
   A valid replacement caption is used verbatim; a replacement video changes only the selected
   platform. Sending a replacement approves that version and continues publishing, including
   TikTok's final form when applicable.

Reply directly to prompts when several jobs are pending. Unthreaded text is accepted only
when exactly one text prompt is active. Replacement videos must reply to their prompt;
otherwise a new video message starts a new job. Submitted versions cannot be changed.

Titles are one line, at most 100 UTF-16 code units. Master captions can be up to 4,096 characters.
Generated and replacement platform captions are limited to 800 UTF-16 code units, leaving room
for the title and platform label inside Telegram's 1,024-character media caption. Oversized text
is rejected rather than truncated. Emoji can count as two code units.

The entered title becomes the YouTube title. Each destination publishes its approved caption;
review-only labels and buttons are not included in published posts. Telegram video documents
remain documents because Telegram file IDs cannot change media type. There is no transcoding,
trimming, or cropping. Files can be up to 2,000,000,000 bytes, subject to configured disk limits.

YouTube Shorts must display square or vertical and have a positive duration of at most
180 seconds. TikTok validates format, codec, dimensions, frame rate, and its creator-specific
maximum duration, with the existing conservative 3–600 second bounds. Invalid media can be
returned to review with `/retry`, then replaced for that destination. One platform's failure
does not block approvals or submissions to the others.

| Command | Behavior |
| --- | --- |
| `/platforms` | Persistent activation switches; each new job captures the current selection |
| `/status [job_id]` | Recent jobs or detailed destination progress |
| `/retry <job_id>` | Retry failed caption generation; return confirmed publication failures to review; recheck recoverable unknown uploads |
| `/cancel <job_id>` | Cancel unsubmitted destinations; existing submissions continue |
| `/pause`, `/resume` | Pause/resume new publications; caption generation and reviews remain usable |
| `/help` | Show the workflow |

Instagram's caption profile includes relevant hashtags, but Instagram publishing and its
activation switch are deferred.

## Configuration

Python 3.13+ and `ffprobe` are required. Install the dependencies in `requirements.lock`.
Commands read `.env` literally; exported environment variables take precedence. Preserve
existing credentials, and keep `.env` and credential files outside version control with
owner-only permissions. Start from `.env.example` for a new installation.

| Setting | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID` | BotFather token and sole allowed owner's numeric ID |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Local Telegram Bot API application credentials |
| `OPENROUTER_API_KEY` | Required OpenRouter key with credit for caption generation |
| `OPENROUTER_MODEL` | Defaults to `google/gemini-2.5-flash-lite`; replacement models must support structured outputs |
| `TELEGRAM_CHANNEL_ID` | Optional `@channel` or numeric `-100…` channel ID; bot must be an administrator with posting permission |
| `YOUTUBE_CREDENTIALS_FILE` | Writable Google authorized-user JSON with refresh token |
| `YOUTUBE_PRIVACY_STATUS` | `private` (default), `unlisted`, or `public` |
| `YOUTUBE_MADE_FOR_KIDS`, `YOUTUBE_CONTAINS_SYNTHETIC_MEDIA`, `YOUTUBE_PAID_PRODUCT_PLACEMENT` | Explicit declarations required for YouTube; paid-placement uploads need YouTube Studio |
| `TIKTOK_CREDENTIALS_FILE` | Writable TikTok app credentials and OAuth tokens |
| `TIKTOK_DIRECT_MODE` | `disabled` by default; `private_test` or `approved` after the corresponding setup |
| `TIKTOK_MEDIA_VERIFIED` | Set true only after TikTok verifies the media domain or URL prefix |
| `PUBLIC_SITE_URL` | HTTPS origin serving the Mini App and expiring media links |
| `WEB_BIND`, `WEB_PORT` | Internal publishing server, default `127.0.0.1:8080`; Compose uses its private container network |

Enable Telegram through `/platforms` after adding the bot as a channel administrator. The
bot resolves and saves the channel's numeric identity. To change the channel for future jobs,
change the environment setting, restart, then disable/re-enable Telegram in `/platforms`.
Existing jobs retain their original target.

OpenRouter receives only the title, master caption, and platform instructions; it does not
receive video bytes or account credentials. One structured request generates all active
captions, with a 4,096-token output cap and at most three attempts per generation cycle.
Successful captions are stored. Transient failures are retried; exhausted retries require
`/retry`. There is no substitute caption or automatic model upgrade.

YouTube declarations are captured at intake. YouTube visibility uses configuration when the
resumable upload is initialized. Optional missing platform credentials do not prevent the bot
from starting or other destinations from operating. Review each destination's status.

## TikTok and account checks

See [TikTok setup](docs/tiktok-setup.md) before enabling Direct Post. It needs `video.publish`,
verified HTTPS media hosting, and the per-post settings form. `approved` is an operator
assertion of actual app approval, not a way to obtain or bypass it. This private utility does
not satisfy TikTok's intended-use requirement for public Direct Post approval.

```sh
.venv/bin/python -m post_uploader youtube-check
.venv/bin/python -m post_uploader.tiktok_auth authorize --mode direct
.venv/bin/python -m post_uploader.tiktok_auth check --mode direct
.venv/bin/python -m post_uploader doctor
```

OAuth helpers refresh or replace credential files; stop the worker before reauthorization.
`doctor` checks configured accounts, Telegram permissions, and the OpenRouter model listing
without posting or generating captions. Listing a model does not verify OpenRouter credit.
Public visibility, domain verification, and platform delivery need separate live checks.

## Deployment and upgrade

The existing Docker Compose layout uses a local Telegram Bot API for large downloads,
persistent queue/media volumes, and a credential directory writable by container UID/GID 101.
The website container proxies `/publisher/` to the bot; its existing public information pages
remain static. Both Nginx layers must use the supplied publishing routes so media capability
URLs are not written to access or error logs. See [server operations](deploy/README.md).

Before upgrading, stop the worker and back up the SQLite database and credential directory.
Schema version 4 adds per-destination captions, media references, review revisions, prompt
associations, and immutable attempt snapshots. Existing unsent destinations return to review;
existing Upload-Post, TikTok inbox, and YouTube resumable attempts retain their identifiers.
Older application versions cannot open version 4; rollback requires the pre-upgrade backup.

Supply the OpenRouter key before restarting. Configure optional destinations, update the bot
and website/Nginx routing, then explicitly activate those destinations. Do not overwrite newer
server OAuth refresh tokens with an older local credential copy. Never remove persistent
volumes as a restart procedure.

## Recovery and retention

SQLite persists ingestion offsets, prompts, captions, approval revisions, and publication
attempts. Duplicate Telegram deliveries and repeated review clicks do not create duplicate
publication attempts. Every attempt uses its saved media/caption snapshot, including during
YouTube resume. Private-chat preview messages can be repeated after an ambiguous Telegram
response; stale buttons cannot authorize publication.

A lost Telegram channel-send response becomes `unknown`; the bot cannot look up the result
and does not resend automatically. TikTok Direct Post can start pulling media during
initialization, so a lost initialization response is also kept unknown. `/retry` only polls
existing recoverable provider identifiers. Inspect unresolved posts on the platform before
creating another job. Historical Upload-Post requests are reconciled through their original
provider; new posts never fall back to it or to TikTok inbox drafts.

A private or unconfirmed YouTube/TikTok result is reported as `needs_action`, not public success.
YouTube determines Shorts classification after processing; upload completion alone does not
prove classification. Telegram channel publication means confirmed delivery to that channel,
which may itself be private.

Downloaded media is eligible for cleanup after 72 hours by default once its job is settled or
cancelled. Pending reviews, shared media references, live download links, and unresolved
attempts protect required files. Expiring TikTok preview links last 15 minutes; publishing
links last up to two hours and are restricted to the selected media revision. Queue history
and credentials remain until removed by the operator.

**Learning Notes:** Destination revisions identify the content being approved, while an
attempt stores the exact media and caption submitted to a provider.

**Why This Matters:** Replacing one platform's video cannot change an upload already underway
on another platform. Restarting resumes the recorded operation instead of inferring approval.

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
ruff check post_uploader tests
node --check post_uploader/static/tiktok.js
```

Tests exercise SQLite migrations, prompt/callback routing, independent reviews and media,
OpenRouter response validation, native protocol handling, signed Mini App authentication,
media range requests, and recovery. External calls are isolated in protocol tests; they do not
establish live platform approval or delivery. Set `MEDIA_TEST_VIDEO` to an existing real video
and install `ffprobe` to enable the optional media integration test.

## Primary references

- [OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs)
- [Default model pricing](https://openrouter.ai/google/gemini-2.5-flash-lite/pricing)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Telegram Mini App authentication](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
- [YouTube resumable uploads](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol)
- [TikTok Direct Post](https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post)
- [TikTok content sharing requirements](https://developers.tiktok.com/docs/en/content-sharing-guidelines)
