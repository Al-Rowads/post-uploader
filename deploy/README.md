# Server deployment

Deployed September 13, 2026 to `155.117.197.243`, SSH port `9011`, under
`/opt/post-uploader`. SSH credentials are not stored in this repository.

- Website: https://upload.al-rowads.com/
- Privacy: https://upload.al-rowads.com/privacy/
- Terms: https://upload.al-rowads.com/terms/
- Data deletion: https://upload.al-rowads.com/data-deletion/
- Telegram bot: https://t.me/al_rowads_uploader_post_bot
- Sole allowed Telegram owner: `694010337` (`@NotRshi`), obtained from the owner's `/start`.
- YouTube now requests public visibility (subject to YouTube API-project audit restrictions). TikTok uses the authorized `al.rowads` sandbox account and
  sends drafts; app review and public publication are separate from server deployment.

## Running services

Run from `/opt/post-uploader`:

```sh
docker compose -f compose.yaml -f compose.production.yaml ps
docker compose -f compose.yaml -f compose.production.yaml logs --tail 50 bot
docker compose -f compose.yaml -f compose.production.yaml exec -T bot python -m post_uploader healthcheck
```

The bot and local Telegram Bot API have no host-published ports. The website uses the existing
server's Nginx image digest and binds container port 80 to **127.0.0.1:3002**. Port 3002 was
unused by listeners and existing Nginx upstreams when selected. Ports 3000 and 3001 belong to
other sites and were preserved.

The host virtual host is `/etc/nginx/sites-available/upload.al-rowads.com`, symlinked in
`sites-enabled`. `upload.al-rowads.com.nginx` is a copy of that deployed file. It follows the
existing `course.al-rowads.com` file exactly apart from the domain, localhost port, and an ACME
challenge location needed for certificate renewal. Existing virtual hosts were not edited.
TLS uses `/etc/letsencrypt/live/upload.al-rowads.com/`, the existing SSL options and DH parameters,
and the existing Certbot scheduler. Renewal uses `/var/www/html` and reloads Nginx on success.
The initial certificate expires December 12, 2026.

The credentials directory is writable by container UID/GID 101 with mode 0700; credential JSON
files use 0600. The server `.env` uses the supplied local settings. Compose overrides the Mac
credential paths with `/credentials/...` and sets the queue directory to `/data`.

Both Google and TikTok authorization were refreshed and checked on the server. The resulting
credential files were copied back to the local checkout. **The running server becomes the source
of truth for subsequent token refreshes.** Do not overwrite its credential directory with an
older local copy during redeployment. Stop the worker before reauthorizing an account or
replacing credential files. Do not run a second worker against these Telegram credentials.

For a code update, transfer only the changed application files, then:

```sh
docker compose -f compose.yaml -f compose.production.yaml up -d --build bot
```

For website changes, update `website/dist/`; it is mounted read-only into Nginx. When changing
host Nginx configuration, run `nginx -t` before `systemctl reload nginx`. Keep the ACME path
available and do not remove the persistent `bot-data` or `telegram-media` volumes.

## Verification

The homepage, privacy, terms, deletion instructions, stylesheet, and icon returned HTTP 200
through public HTTPS. HTTP redirects to HTTPS. Requests for `.env` are denied and credential
paths and unknown routes return 404. `doctor` verified Telegram, YouTube OAuth refresh, and the
TikTok basic-profile API without sending a video. Google OAuth branding and TikTok sandbox
settings were inspected in Safari and already contain the exact live homepage/policy URLs.
The bot health check and Telegram notifications are checked independently of video delivery.
No real video has been uploaded as part of deployment.

The worker and website reached Docker healthy status, with fresh ingestion, worker, and poller
heartbeats. The startup notification was delivered to the configured owner. A targeted Certbot
dry run for `upload.al-rowads.com` completed successfully through the final Nginx configuration.
