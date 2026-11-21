# ClipShorts

ClipShorts is a locally customized build of the open-source [PeakCut project](https://github.com/haziqahmedqasmi/peakcut), with its original MIT license and attribution retained. The upstream project name remains in the code and license.

Turn long videos and live streams into captioned vertical clips, ready to review and post. Run it on your own computer
or on a server that stays online. AI is optional; you bring your own provider and pay that provider directly.

Paste a YouTube, Twitch or Kick link (or upload a file) and ClipShorts analyzes it for moments, renders vertical clips,
and can queue clips for connected accounts. You can also record supported live streams and clip moments from the
recording. Selection uses the transcript and audio signals, with optional AI analysis; it does not visually understand
every gameplay event. No service can guarantee that a clip will go viral.

## What it does

- **Clips from videos.** Transcribes the audio, ranks candidate moments with transcript and audio signals, and uses
  the configured AI when available to select moments and write clip metadata. Without a responding AI, it can still
  select by audio signals, but AI-assisted selection and copy are unavailable.
- **Live clipping.** Records a stream into a rolling buffer and clips as it happens, using loudness, chat activity
  and the transcript.
- **Creator workspaces.** Each creator has a channel, clip settings, posting rules, optional saved webcam position, and activity
  log. Facecam above is the default layout. On the first suitable video, ClipShorts checks for a webcam locally, may
  use one vision request if local detection cannot confirm it, and saves a successful position for later videos.
- **Layouts.** Choose facecam above gameplay, facecam below gameplay, side-by-side, gameplay with a facecam overlay,
  reaction focus, wide gameplay, action zoom, blurred fill, zoom, or centre crop. Finished clips can be re-rendered
  into alternate layouts; layout versions are added for review and are not automatically posted.
- **Posting.** Connect YouTube, Instagram and TikTok, then queue clips or turn on creator auto-posting. The local
  queue publishes when its scheduled time arrives, using a default 10-minute spacing per account and the creator's
  configured daily cap. The server must be running at publish time. See
  [DEPLOY.md](DEPLOY.md#connecting-youtube-instagram-and-tiktok) for account setup.
- **No sign-in on your own computer.** It opens straight into the dashboard. On a shared server, switch accounts on
  (`PEAKCUT_ACCOUNTS=1`) for email or Google sign-in and an admin dashboard.
- **Your AI keys.** Save up to 100 keys for a provider. ClipShorts rotates among keys after per-key rate limits and
  records cooldowns without storing key text in the cooldown file. Multiple keys do not multiply a shared account,
  organization, model, or daily allowance. When provider quota or credit is exhausted, wait for reset or configure
  another provider; adding keys under the same shared quota will not restore it. Ollama can run a model locally.

## Quick start

You need Python 3.10 or newer and `ffmpeg`. On Windows: `winget install Gyan.FFmpeg`. On a Mac: `brew install ffmpeg`.
On Linux: `sudo apt install ffmpeg`. ClipShorts finds it by itself and tells you in the dashboard if it is missing.

Put ClipShorts in a short folder that stays put: `C:\peakcut` on Windows, `~/peakcut` on a Mac or Linux. Not inside a
temporary folder or an AI assistant's workspace: those get cleaned up, and on Windows a folder that deep stops the
speech model from loading (Windows can't open files whose full path is over 260 characters). If an AI assistant
installs it for you, it should ask you where to put it first ([AGENTS.md](AGENTS.md) tells it to).

On Windows:

```bash
git clone <this repository> C:\peakcut
```

```bash
cd C:\peakcut
```

On a Mac or Linux:

```bash
git clone <this repository> ~/peakcut
```

```bash
cd ~/peakcut
```

```bash
pip install -r requirements.txt
```

Start the server:

```bash
python -m studio.server
```

The first start downloads the speech model by itself (about 480 MB, once).

Open http://127.0.0.1:8765. On a local install it opens straight into the dashboard. Go to **Settings, AI**, choose
your service and model, add one or more keys (one per line), and press **Save and test**. Keep the server running
while videos process or queued posts are due.

## Choose your AI

ClipShorts needs a chat model that can return JSON. The easy way is in the dashboard: **Settings, AI**. Pick the
service, paste your key, press **Find models** to choose from the service's own list, then **Save and test**. The
key is kept in the data folder on your machine and is never shown again.

If you prefer a file (or run a server), provider, model and key can go in `.env`. Dashboard settings take precedence:

```ini
PEAKCUT_AI_PROVIDER=gemini
PEAKCUT_AI_MODELS=gemini-2.5-flash
PEAKCUT_AI_KEY=your-key-here
```

| `PEAKCUT_AI_PROVIDER` | Service | Where to get a key |
|---|---|---|
| `gemini` | Google Gemini | https://aistudio.google.com/apikey |
| `openai` | OpenAI | https://platform.openai.com/api-keys |
| `xai` | Grok (xAI) | https://console.x.ai |
| `groq` | Groq | https://console.groq.com/keys |
| `openrouter` | OpenRouter (many models, one key) | https://openrouter.ai/keys |
| `anthropic` | Claude | https://console.anthropic.com |
| `ollama` | A model on your own machine, no key | https://ollama.com |
| `custom` | Any OpenAI-compatible address: also set `PEAKCUT_AI_BASE_URL` | |

- `PEAKCUT_AI_MODELS` is the model id exactly as the service names it. Model names and limits change; check the
  provider's current documentation. Several ids separated by commas are tried in order. Groq also has compatible
  fallback models in the app.
- You may save up to 100 keys in **Settings, AI**. Keys are rotated when a provider returns a credential-specific
  error or rate limit; limited keys enter cooldown and are retried later. A daily or account-wide quota can affect
  every key in that account, so key rotation cannot bypass it. The dashboard shows provider errors and AI usage.
- You pay the AI service directly for what you use. The **AI usage** page in the app shows how many requests and
  tokens each video took.
- Speech-to-text runs through the local model by default. If you configure a remote ASR worker, audio is sent to that
  worker. AI prompts and any webcam frame used for facecam detection are sent to the AI provider you configured.

## Settings

Everything is set through environment variables, normally in a `.env` file next to this README.
[`.env.example`](.env.example) lists every setting with a short explanation. The ones that matter first:

| Setting | What it is for |
|---|---|
| `PEAKCUT_AI_PROVIDER`, `PEAKCUT_AI_MODELS`, `PEAKCUT_AI_KEY` | The AI provider and model (or configure it in the dashboard) |
| `PEAKCUT_ACCOUNTS` | `1` makes everyone sign in. Use it on any server other people can reach |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Optional: Sign in with Google, and connecting YouTube |
| `PEAKCUT_SMTP_*`, `PEAKCUT_MAIL_FROM` | Optional: confirmation and password emails |
| `PEAKCUT_DATA` | Where videos, clips and the database are kept (default `./data`) |

`.env`, the `secrets/` folder and all user data are ignored by git. Keep keys there, never in the code.

## Admins

This only matters with accounts switched on (`PEAKCUT_ACCOUNTS=1`); without them you are the owner and can do
everything. An account is an admin only when the database says so. No email address is special in the code and no web request
can change a role. Do it on the machine that runs ClipShorts:

```bash
python -m studio.admin grant you@example.com
```

`revoke` and `list` work the same way. The admin dashboard is at `/admin` and answers "not found" to everyone else.

## How it is built

| Part | What it is |
|---|---|
| `studio/` | The server: FastAPI, the clip pipeline, live recording, accounts |
| `studio/ai.py` | The only file that talks to an AI service. Add a provider here |
| `web/` | The pages: landing, sign-in, the app, the admin dashboard (plain HTML, CSS and JavaScript, no build step) |
| `tests/` | Tests that run against a throwaway copy of the app |
| `deploy/`, `Dockerfile`, `docker-compose.yml` | One-server deployment with PostgreSQL and automatic HTTPS |

Video work is done with `ffmpeg` and `yt-dlp`. Speech is transcribed with `sherpa-onnx` and the Parakeet model.
Data is kept per user: accounts in the database (SQLite on your own computer, PostgreSQL on a server), videos and
clips in each user's own folder. [AGENTS.md](AGENTS.md) is a short map of the code for contributors and AI coding
assistants.

## Where your videos and clips are kept

Everything ClipShorts keeps is in one folder: videos, clips, recordings, creators, settings and the speech model. By
default it is the `data` folder inside ClipShorts. **Settings, Storage** shows the folder and how much room its drive
has, opens it in one click, and lets you move it to another drive: choose the new folder, and ClipShorts moves
everything there the next time it starts. If the drive is nearly full, ClipShorts says so and stops taking new videos
until there is room. (`PEAKCUT_DATA` in `.env` still works and takes priority.)

## Updating

ClipShorts checks GitHub once a minute while it runs. When a newer version is out, the dashboard shows a notice
within a minute or two (no restart needed), with what changed
and how to update: ask your AI coding assistant to do it (the notice has a sentence to paste), or run
`git pull && pip install -r requirements.txt` in the ClipShorts folder and restart it (with Docker:
`git pull && docker compose up -d --build`). Your `.env`, keys and clips are not touched. Only the two commit ids
are sent to GitHub; set `PEAKCUT_UPDATE_CHECK=0` to turn the check off.

## Deploy

[DEPLOY.md](DEPLOY.md) walks through a single server with Docker: the app, PostgreSQL, and Caddy for HTTPS.

```bash
docker compose up -d --build
```

## How it behaves under load

- The page asks the server one small question every few seconds. When nothing has changed, the answer comes
  straight from memory.
- Each person's videos wait in their own line and the lines take turns, so one person cannot hold everyone up.
- The server measures its own processor, memory and disk every second. Extra videos and recordings start only
  while there is room, wait briefly when it is nearly full, and continue the moment it frees up.
- Clip jobs default to up to 50 requested clips; the supported maximum is 500. Creator workspaces can set their own
  clip count and posting rules. Server upload, job, live-recording, storage, and concurrency limits can be configured
  in `.env`; processing speed depends on available CPU, memory, disk, network, and the selected AI provider.
- The server needs to stay on for local processing, live recording, and queue publishing. A deployed server can run
  continuously only if its host does not sleep, stop, or exceed its free-tier limits.

## Tests

```bash
python tests/run.py
```

This starts a copy of the app on a spare port with an empty temporary data folder, runs the permission, abuse and
queue tests against it, then removes it. It needs no keys and never touches real data.

## Security

- With sign-in off, ClipShorts listens on this computer only (`127.0.0.1`), refuses changes sent by other websites,
  and only answers to its own address. Do not expose it to the internet that way: turn accounts on.
- Passwords are stored only as salted scrypt hashes. Sessions are random tokens stored hashed; cookies are HTTP-only.
- Every route works out who is asking from the session, never from the request, so users only reach their own data.
- Only known video sites are fetched, and text from videos is treated as data when it is shown to the AI.
- Found a problem? Please report it privately through the repository's security advisories instead of opening a
  public issue.

## Using it responsibly

Clip videos you made, or have permission to use. You are responsible for the rights to what you download, clip and
post, and for following the terms of the sites you fetch from and post to.

## Licence

[MIT](LICENSE). Caption fonts in `studio/fonts/` (Anton, Archivo Black, Poppins) are under the SIL Open Font
License; the licence texts are in the same folder.
