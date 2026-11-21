# Deploying ClipShorts

ClipShorts can run as three Docker containers on one Linux server: the app, PostgreSQL, and Caddy (which handles
HTTPS). The app performs clipping and queue publishing in its own process, so the server must remain online for
those tasks to run.

## What you need

- A Linux server with Docker. Video decoding, rendering, and local transcription need CPU, memory, and disk; the
  speed depends on the source and server. Long recordings and source videos can require substantial storage. A free
  hosting tier may sleep, stop the container, limit disk, or impose usage caps, so it may not provide dependable
  24-hour processing or publishing.
- A domain name pointing at the server (an `A` record).
- An AI key (see "Choose your AI" in the README). Optional: a Google "Web application" OAuth client, and an SMTP
  service for emails.

## Steps

1. Copy this folder to the server (or `git clone` it there).
2. `cp .env.example .env` and fill it in: the AI lines if needed, `PEAKCUT_DOMAIN`, `POSTGRES_PASSWORD`, and
   `COMPOSE_PROFILES=own-https` if the server is dedicated to ClipShorts. Put key files, if you use files, in `./secrets/`.
3. `docker compose up -d --build`
   The first start downloads the speech model (about 480 MB) and creates the database tables.
4. Open `https://your-domain` and sign up. With Docker, accounts are on (`PEAKCUT_ACCOUNTS=1`), so everyone signs in.
   Every new account is a normal user.
5. Make yourself an admin, on the server: `docker compose exec app python -m studio.admin grant you@example.com`
   The admin dashboard is then at `https://your-domain/admin`. It answers "not found" for everyone who is not an
   admin, and no page or web request can make someone an admin: only that command can.
6. Sign in as that admin, open **Settings, AI**, and add your AI key or keys there (or configure one key in `.env`).

The creator's post queue uploads when each due time arrives. The default spacing is 10 minutes per account, subject
to creator settings and platform limits. The server must be running at that time; queue times are stored, but a
powered-off home PC cannot publish while it is off. After a restart, overdue posts are reflowed so the app does not
dump the missed queue all at once.

## Connecting YouTube, Instagram and TikTok

People connect their accounts with one click in **Settings, Connected apps**: they press Connect, sign in on the
platform's own page, and come back. For that, the server needs one app registered with each platform. An admin can
do it right there: press **Set up** on a platform, follow the steps shown, and paste the app's id and secret (or put
them in `.env`, see `.env.example`). The redirect address to register is shown in the panel, with a Copy button.

- **YouTube** (Google Cloud): an OAuth client of type Web application, with YouTube Data API v3 turned on. Until
  Google verifies the app, people see a warning screen and uploads stay private.
- **Instagram** (Meta for Developers): a Business app with Instagram (API setup with Instagram login). Works with
  Creator and Business accounts. Until Meta reviews the app, only accounts added as testers can connect. Instagram
  fetches each clip from a one-hour signed link on your site, so the site needs a public https address.
- **TikTok** (TikTok for Developers): an app with Login Kit and the Content Posting API (Direct Post), scopes
  `user.info.basic` and `video.publish`. Until TikTok audits the app, clips are posted as private (ClipShorts does
  this by itself and says so on the account).

Instagram and TikTok only accept https redirect addresses.

## Google setup (sign-in and YouTube)

In Google Cloud, create an OAuth client of type **Web application** with these redirect URIs:

- `https://your-domain/auth/google/callback`
- `https://your-domain/oauth/youtube/callback`

Enable **YouTube Data API v3** in the same project. Posting publicly to YouTube needs Google's API audit;
until then uploads from the API stay private.

## Email

Set the `PEAKCUT_SMTP_*` lines and `PEAKCUT_MAIL_FROM` to send password-reset and confirmation emails through any
SMTP service. On a public site also set `PEAKCUT_VERIFY_EMAIL=1`, so people confirm their address before clipping.
Without SMTP on an HTTPS site, no reset emails can be sent.

## Good to know

- **Data**: everything lives in two Docker volumes, `pgdata` (accounts, AI usage) and `data` (videos, clips,
  creator profiles, settings, and the saved AI key pool). Protect and back up both volumes securely.
  `docker compose exec db pg_dump -U peakcut peakcut > backup.sql` dumps the database.
- **Capacity**: `PEAKCUT_JOB_WORKERS` is how many videos are processed at once; `PEAKCUT_MAX_LIVE` is how many live
  streams are recorded at once. Raise them only as far as the CPU allows.
- **One server only**: jobs, live recordings, and queue publishing run inside the app process. Do not start a second copy of the app
  against the same data.
- **YouTube downloads from data centres** are sometimes refused by YouTube ("confirm you're not a bot"). If links
  fail on the server but work at home, the server's address is the cause. ClipShorts then pauses YouTube links for a
  few minutes and says so; uploads and Twitch or Kick links keep working.
- **Updates**: `git pull && docker compose up -d --build`. A restart pauses videos in progress (they pick up
  where they stopped) and live recordings (clips already found are finished from the saved stream).
- **AI key limits**: ClipShorts can store up to 100 keys for its configured provider and rotates keys when
  credential-specific rate limits occur. Keys belonging to the same provider account or organization do not create
  extra shared daily quota. When that quota or credit is exhausted, wait for provider reset or configure a different
  provider with available quota. Never put real keys in documentation or commit them.
- **Open sign-up**: anyone who can reach the site can make an account and use your AI key. On a public address,
  keep the limits in `.env` sensible, or put the site behind your own access control.

## A server that already runs other sites

Leave `COMPOSE_PROFILES=own-https` out of `.env`. ClipShorts then listens only on `127.0.0.1:8765` and the web
server that is already there forwards the domain to it: see `deploy/nginx-peakcut.conf`, or
`deploy/docker-compose.traefik.yml` for Traefik.

## Cloudflare CDN in front

With the record proxied (orange cloud):

- Set `PEAKCUT_CLIENT_IP_HEADER=CF-Connecting-IP` in `.env`, so rate limits see each visitor, not Cloudflare. The
  header is only believed for connections that really come from Cloudflare's address ranges.
- Images, videos and scripts under `/brand/` and `/assets/` are cached by Cloudflare automatically. Signed-in
  pages, clips and the API send `private` or `no-store` and are never cached by Cloudflare.
- Use SSL/TLS mode Full (strict) with a Cloudflare origin certificate in `secrets/` and
  `PEAKCUT_CADDYFILE=Caddyfile.origin`.
- Cloudflare's standard plans refuse requests over 100 MB. ClipShorts sends big uploads in pieces
  (`PEAKCUT_UPLOAD_PART_MB`, 40 by default), so large files still work.

## Speech to text on a graphics card

Optional. A second machine with an NVIDIA card can do most of the transcription faster:
`deploy/gpu/docker-compose.yml` runs it, and `PEAKCUT_ASR_URL` plus `PEAKCUT_ASR_TOKEN` on the main server point
at it. Keep its port closed to the internet. If it is unreachable, the main server carries on with its own
processor.
