# Public information pages

Deploy the **contents of `website/dist/`** as the document root for
`https://upload.al-rowads.com`. This is a static website with no build, package installation,
JavaScript, cookies, or analytics on the public information pages. The optional TikTok
Mini App is served separately by the bot under `/publisher/`, through the same HTTPS origin.

| Page | URL to enter in developer consoles |
| --- | --- |
| App homepage | `https://upload.al-rowads.com/` |
| Privacy policy | `https://upload.al-rowads.com/privacy/` |
| Terms of service | `https://upload.al-rowads.com/terms/` |
| Data deletion instructions | `https://upload.al-rowads.com/data-deletion/` |

Each route has its own `index.html`; configure directory-index serving, not a single-page-app
fallback. Preserve `assets/`. Configure HTTPS and redirect HTTP to HTTPS. Make these pages
publicly readable without login. Do not publish `.env`, `data/`, the Python source directory,
or a directory listing of the repository. `404.html` is available for the host’s error-page setting.
The `.openai/hosting.json` manifest also describes the static output for optional later Sites hosting.
The site is deployed on the owner’s server at https://upload.al-rowads.com using the existing
Nginx layout and a Docker container at 127.0.0.1:3002. See [server operations](../deploy/README.md).
No separate Sites-hosted project was registered.

Local preview:

```sh
.venv/bin/python -m http.server 8093 --bind 127.0.0.1 --directory website/dist
```

Google Auth Platform branding has been set to Level13 Publisher with the homepage, privacy,
and terms URLs above, and `al-rowads.com` is registered in its authorized-domain list. Complete
any ownership verification requested by Google. Keep the actual desktop OAuth client and loopback redirect flow; the information
website is not an OAuth token callback service. Do not submit brand verification until the
pages are live and the app’s displayed branding matches them.

TikTok sandbox URLs are already saved with these values. The desktop callback remains
`http://127.0.0.1:8765/callback/`, not the public website. The new Direct Post integration uses
verified HTTPS media URLs under `/publisher/media/`; complete domain or URL-prefix verification
before enabling it. See [TikTok setup](../docs/tiktok-setup.md).

The pages identify the operator as Level13 Publisher and use the existing account contact
`level13.alrowads@gmail.com`. The privacy policy describes the current code: no platform
passwords, local OAuth token storage, 72-hour default media retention with unresolved-upload
exceptions, and operator-handled deletion. The operator must handle those requests and remove
local records and backups as appropriate. The website does not implement automated deletion.

[YouTube policy requirements](https://developers.google.com/youtube/terms/developer-policies)
and [Google brand verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification)
were used to check the required links and disclosure topics. These pages do not themselves
establish approval or domain verification.

The updated privacy and overview pages describe OpenRouter caption processing, per-platform
approval, channel publishing, and the optional TikTok Mini App. Deploy those pages together
with the new bot release. The Mini App uses the official Telegram JavaScript bridge and sends
signed initialization data to the bot backend; it is not served by the static-file preview command.
