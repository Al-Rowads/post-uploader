# Native TikTok Direct Post

The repository supports gated Direct Post and continues reconciling historical inbox drafts.
New TikTok destinations require Direct Post; there is no automatic fallback to inbox delivery.

## Existing account history

The September 13, 2026 setup recorded the **Level13 Publisher** app and **Level13 Draft Uploads**
sandbox, with `user.info.basic` and `video.upload` authorized for **@al.rowads**. Direct Post was
off, production review had not been submitted, and no real video was uploaded. Those checks
do not establish current token validity or Direct Post access.

- [Developer app](https://developers.tiktok.com/app/7684853871059372050/pending)
- [Sandbox](https://developers.tiktok.com/app/7684853871059372050/sandbox/7684813967113701394)
- Desktop OAuth redirect: `http://127.0.0.1:8765/callback/`
- Website: `https://upload.al-rowads.com/`

Do not describe this owner-only utility as a public multi-user product. TikTok's
[content sharing guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines)
exclude private account-upload utilities from the intended use of public Direct Post.
An unaudited client requires a private account and `SELF_ONLY` visibility. Building this
adapter does not grant app approval.

## Configure Direct Post

1. Enable Direct Post in an eligible TikTok app/sandbox and obtain access to `video.publish`.
   Keep the original account and desktop callback settings. Preserve `video.upload` access
   while historical inbox attempts still need reconciliation.
2. Stop the worker, then run the authorization helper:

   ```sh
   .venv/bin/python -m post_uploader.tiktok_auth authorize --mode direct
   .venv/bin/python -m post_uploader.tiktok_auth check --mode direct
   ```

   The direct authorization request includes `user.info.basic`, `video.upload`, and
   `video.publish`. The helper verifies state and desktop PKCE, then atomically saves tokens
   with private permissions. The check reads creator capabilities without submitting media.
   Use `--mode draft` only to maintain legacy inbox authorization.
3. Deploy the bot's `/publisher/` routes and update both Nginx configurations described in
   [server operations](../deploy/README.md). Configure the HTTPS `PUBLIC_SITE_URL` origin.
   Register the HTTPS domain for the bot's Mini App in BotFather if Telegram requests it.
4. Verify ownership of that domain or its `/publisher/media/` URL prefix in TikTok's developer
   console. Complete its verification-file or DNS challenge through the configured website.
   Do not expose a repository directory or credential files to satisfy verification.
5. Set `TIKTOK_MEDIA_VERIFIED=true` only after verification succeeds. Set
   `TIKTOK_DIRECT_MODE=private_test` for unaudited testing. Use `approved` only with a genuinely
   audited, eligible app. The mode never changes account privacy or upgrades app permissions.
6. Run `doctor`, restart the worker, and enable TikTok with `/platforms`. An API gate failure
   is reported without preventing YouTube or Telegram from operating.

The running server is the source of truth for refresh tokens after deployment. Stop the worker
before replacing its credential file and keep its directory writable by UID/GID 101. Do not
copy older local tokens over newer server credentials.

## Per-post behavior

The bot sends the TikTok video and generated caption alongside the other platform previews.
Accept opens the Mini App. It displays the current account, video, editable caption, a privacy
dropdown without a selected default, interaction controls initially off, commercial disclosures,
and AI-video disclosure. Interactions disabled by TikTok cannot be enabled in the form.
Branded content cannot use Only me visibility.

The owner must confirm the displayed music/content terms and press Publish. The server
validates Telegram's signed initialization data, owner identity, form age, destination revision,
creator capabilities, and media eligibility. Changing a caption or video invalidates older
forms. A replacement sent through the bot still requires TikTok's final form.

The bot uses the documented server-media `PULL_FROM_URL` workflow with a random expiring URL.
The URL permits only the selected file; it does not expose filesystem browsing. It remains
available during TikTok's download window and supports HEAD/range requests. Treat these URLs
as bearer credentials: both Nginx layers suppress logs for the publishing route.

A successful initialization is not a published post. The bot polls the saved `publish_id`.
Private results and completion without evidence of public visibility remain `needs_action`.
A lost initialization response may already have started publication; it becomes `unknown`,
with no automatic retry or inbox fallback. `/retry` rechecks only existing recoverable IDs.
Inspect TikTok before deliberately sending the video as another job.

## References

- [Direct Post API](https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post)
- [Creator information](https://developers.tiktok.com/docs/en/content-posting-api-reference-query-creator-info)
- [Media transfer and URL verification](https://developers.tiktok.com/docs/en/content-posting-api-media-transfer-guide)
- [Post status](https://developers.tiktok.com/docs/en/content-posting-api-reference-get-video-status)
- [Desktop OAuth](https://developers.tiktok.com/docs/en/login-kit-desktop)
