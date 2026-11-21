# Gaming streams and facecam layouts

ClipShorts is configured for creator streams where gameplay and a webcam appear together. The default layout for
creator workspaces and new clip jobs is **Facecam above gameplay** (`stacked`). Other layouts remain available in
the Add Clips options and for finished-clip layout versions.

## Automatic webcam detection and creator profiles

Webcam position and size vary by creator and can change between recording setups. ClipShorts handles new creators
automatically:

1. It checks the source locally for a persistent webcam frame and movement consistent with a camera.
2. If local detection cannot confirm a webcam and the requested layout needs one, it may send one sampled frame to
   the configured vision model for help. This is a fallback; it is not a visual review of the whole video.
3. When a camera is confirmed, its normalized position is saved to that creator's profile and reused on later videos.
   The saved position is scaled to the source dimensions.

The creator profile belongs to the local ClipShorts workspace. A new creator needs a suitable source with a visible
webcam for its first calibration. A stream with no visible webcam cannot produce a true facecam layout; for
Facecam above or Facecam below, ClipShorts reports that it could not confirm the camera instead of silently marking
a centre crop as the requested layout. If the creator changes their recording layout, the saved profile may need to
be refreshed by removing or correcting that profile before retrying.

For privacy, the sampled frame is sent only when a vision fallback is needed and a vision-capable AI service is
configured. Without that fallback, the local detector can still find a camera. Transcript prompts and images sent
for detection go to the AI provider configured in **Settings, AI**.

## Available layouts

| Layout | Result |
|---|---|
| **Facecam above gameplay** (`stacked`) | Webcam occupies the upper panel; gameplay fills the lower panel. The large hook text is omitted, and captions sit between panels. |
| **Facecam below gameplay** (`bottomcam`) | Gameplay fills the upper panel; webcam is in the lower panel. The large hook text is omitted, and captions sit in the gameplay area above the webcam. |
| **Gameplay + facecam** (`gameplay`) | Gameplay fills the vertical frame, with the webcam overlaid in a corner. |
| **Reaction focus** (`reaction`) | Larger webcam overlay with gameplay still visible. |
| **Side-by-side** (`split`) | Gameplay and webcam occupy separate side-by-side panels. |
| **Wide gameplay** (`wide`) | Wide source frame over a blurred background. |
| **Action zoom** (`action`) | Tighter full-screen gameplay crop. |
| **Blurred fill** (`blur`) | Full source frame with a blurred background filling the vertical canvas. |
| **Zoomed fill** (`zoom`) | Enlarged source with blurred fill. |
| **Fill screen** (`crop`) | Centre crop to fill the vertical canvas. |

Caption generation is a separate option from layout selection. Users can leave **Generate captions** unchecked. Layout
versions reuse the already-selected clip timestamps and do not auto-post; queue the version you choose.

## Clip selection and its limits

ClipShorts analyzes transcripts and audio signals such as loudness, and can use AI to rank moments and produce titles,
hooks, descriptions, and hashtags. Gaming title and creator cues help keep metadata relevant. It does not visually
inspect every frame to understand gameplay, verify every kill or punchline, or predict views. A facecam scan is for
locating the camera only, not judging whether a moment is entertaining.

If the AI provider is rate-limited or has exhausted its quota, the AI may be temporarily unavailable. The app rotates
configured keys and observes per-key cooldowns, but keys sharing an account or organization share that provider's
quota. No number of keys can raise a shared daily limit. Clip selection may continue with audio-based signals while
AI-assisted selection and metadata are unavailable.

Creator and game tags are selected to describe the channel and clip topic; they do not guarantee reach or virality.
Review clips and metadata before posting, especially when the source language or game context is unclear.

## Relevant code

| File | Purpose |
|---|---|
| `studio/games.py` | Recognizes game names and context. |
| `studio/brain.py` | AI prompts for clip selection, metadata, and webcam fallback. |
| `studio/facecam.py` | Local webcam detection and optional vision-assisted confirmation. |
| `studio/creators.py` | Creator defaults and saved webcam profile. |
| `studio/jobs.py` | Automatic webcam calibration, profile reuse, and layout-version jobs. |
| `studio/render.py` | Vertical layouts, caption placement, and rendering. |
| `studio/posting.py` | Creator-specific tags and post queue integration. |
| `tests/test_facecam.py` | Focused checks for camera detection, profile reuse, and rendering geometry. |
