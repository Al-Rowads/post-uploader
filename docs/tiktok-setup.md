# Native TikTok draft uploads

## Current setup — September 13, 2026

- App: **Level13 Publisher**, individually owned, ID `7684853871059372050`.
- Sandbox: **Level13 Draft Uploads**, ID `7684813967113701394`.
- [Developer app](https://developers.tiktok.com/app/7684853871059372050/pending).
- [Sandbox](https://developers.tiktok.com/app/7684853871059372050/sandbox/7684813967113701394).
- Saved sandbox settings: Login Kit, Content Posting API, Desktop, and Photo & Video.
- Scopes: `user.info.basic` and `video.upload`. Direct Post is off.
- Desktop redirect URI: `http://127.0.0.1:8765/callback/`.
- Website: `https://upload.al-rowads.com/`.
- Terms: `https://upload.al-rowads.com/terms/`.
- Privacy: `https://upload.al-rowads.com/privacy/`.
- The 1024×1024 app icon and all required URLs are saved. The URLs are live over HTTPS on the requested server.
- Real sandbox app credentials are saved in gitignored `data/tiktok-credentials.json`, mode 0600.
- Target account **@al.rowads** is registered in the sandbox.
- OAuth consent completed with `user.info.basic` and `video.upload`; access and refresh tokens
  are saved in the credential file. The live basic-profile API check returned `al.rowads`
  and verified draft-upload scope on September 13, 2026.
- No video has been uploaded. Production has not been submitted for review.

The app description entered is: “Send original videos from a private Telegram bot to your
TikTok inbox for review, editing, and manual publication.” Do not describe this as a public
multi-user service unless the application actually becomes one.

TikTok's [app review rules](https://developers.tiktok.com/docs/en/app-review-guidelines) exclude
personal-use apps. [Sandbox mode](https://developers.tiktok.com/docs/en/add-a-sandbox) supports
testing but does not provide public-video access. The draft endpoint does not remove those
approval requirements. Production availability and public posting from this app are unverified.

## Finish developer configuration

1. Website deployment is complete at `https://upload.al-rowads.com`. See [server operations](../deploy/README.md).
2. Target registration and upload OAuth consent for **@al.rowads** are complete.
3. Use the OAuth helper below only when renewing or replacing authorization; stop the worker first.
4. Complete any requested ownership verification. Production review is separate; sandbox
   credentials are not production credentials.

## Authorize on this Mac

Run from the repository using its existing virtual environment:

```sh
.venv/bin/python -m post_uploader.tiktok_auth authorize --credentials data/tiktok-credentials.json
```

Open the printed URL in Safari. The helper listens only on `127.0.0.1:8765`, checks a random
state value, uses TikTok's documented desktop PKCE encoding, and waits up to ten minutes.
Grant the displayed permissions to the intended TikTok account. The callback must exactly
match the registered URI, including the final slash. The code exchange saves credentials
atomically with owner-only permissions. It never prints tokens.

Then run the read-only account check:

```sh
.venv/bin/python -m post_uploader.tiktok_auth check --credentials data/tiktok-credentials.json
```

This verifies account access, not upload delivery or production approval. A failed exchange
does not create fallback tokens. The worker refreshes expiring tokens and saves any rotated
refresh token. Stop the worker before reauthorizing against its credential file.

## Install credentials for the worker

The local `.env` already points to `data/tiktok-credentials.json` using an absolute Mac path.
Compose mounts `./data` at `/credentials` and overrides the credential path for the container.
On the Linux server, both the directory and file must be writable by UID/GID 101, with modes
0700 and 0600. The parent must be writable for atomic token refresh. Do not use a read-only mount.
Never paste tokens into messages, tickets, or committed files.

YouTube now uses the native Google OAuth credential file named in `.env`. Upload-Post is
retained only to reconcile historical requests; it is not a fallback upload provider.

## Actual draft behavior

Sending a valid captioned video requests native inbox upload with `FILE_UPLOAD`. The bot
checks the existing publish ID after any uncertain transfer. Once TikTok reports inbox
delivery, Telegram reports `needs_action` and returns the original caption separately.
Open the TikTok inbox notification, paste/edit the caption, set content disclosures and
visibility, and publish. The video draft endpoint accepts no caption, visibility, or
commercial-disclosure fields. Polling ends at inbox delivery; the bot does not claim that
your later manual action has occurred.

Do not blindly resend an uncertain upload. `/retry` checks an existing publish ID. If the
initialization response was lost before an ID could be saved, no video bytes were sent;
the attempt is kept uncertain because there is no documented client-ID lookup endpoint.

## Validation and design notes

Local tests exercise chunk boundaries, callback state checks, private credential-file writes,
draft/public status distinctions, SQLite migration, and recovery between destinations.
They do not simulate a successful TikTok backend. Real authorization and a user-selected
video are still required for end-to-end validation.

**Learning Notes:** TikTok inbox initialization and media transfer are separate operations.
Saving the server ID before media transfer permits safe status checks after a restart.

**Why This Matters:** Draft delivery needs a human publishing step. Keeping it distinct from
public success avoids misleading Telegram notifications and accidental duplicate uploads.
