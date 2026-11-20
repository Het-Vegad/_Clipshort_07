"""A small, bounded visual review of transcript/audio candidates.

We inspect three representative frames per candidate, not every source frame. This
keeps long recordings and API use practical while letting the model reject moments
whose words sound exciting but whose visible payoff is missing.
"""
import io
import json
import subprocess
from pathlib import Path

from . import ai, config, usage
from .media import NO_WINDOW

MAX_REVIEW = 24
BATCH_SIZE = 4
FRAME_W, FRAME_H = 320, 180


def _frame(path: Path, at: float):
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, at):.2f}", "-i", str(path),
                        "-frames:v", "1", "-vf", f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease,pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2",
                        "-f", "image2pipe", "-vcodec", "mjpeg", "-"], capture_output=True,
                       timeout=20, creationflags=NO_WINDOW)
    if p.returncode:
        return None
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(p.stdout)).convert("RGB")
        image.thumbnail((FRAME_W, FRAME_H))
        cell = Image.new("RGB", (FRAME_W, FRAME_H), "#111827")
        cell.paste(image, ((FRAME_W - image.width) // 2, (FRAME_H - image.height) // 2))
        return cell
    except Exception:  # noqa: BLE001
        return None


def _contact_sheet(candidates, source_path):
    from PIL import Image, ImageDraw
    sheet = Image.new("RGB", (FRAME_W * 3, FRAME_H * len(candidates)), "#111827")
    draw = ImageDraw.Draw(sheet)
    for row, clip in enumerate(candidates):
        path = Path(clip.get("src") or source_path)
        if not path.is_file():
            continue
        offset = float(clip.get("src_offset", 0) or 0) if clip.get("src") else 0
        start, end = float(clip["start"]), float(clip["end"])
        for col, fraction in enumerate((0.2, 0.5, 0.8)):
            frame = _frame(path, start + (end - start) * fraction - offset)
            if frame:
                x, y = col * FRAME_W, row * FRAME_H
                sheet.paste(frame, (x, y))
                draw.rectangle((x, y, x + 48, y + 24), fill="#050505")
                draw.text((x + 6, y + 5), f"C{row + 1} · {col + 1}", fill="white")
    out = io.BytesIO()
    sheet.save(out, format="JPEG", quality=78, optimize=True)
    return out.getvalue()


def review(candidates, source_path, log=lambda _message: None):
    """Attach visual score/reason to candidates; return API token usage and number reviewed."""
    if not ai.current().get("vision_model"):
        return 0, 0, "Visual review is off; add an image-capable model in Settings → AI."
    if (not source_path or not Path(source_path).is_file()) and not any(c.get("src") and Path(c["src"]).is_file() for c in candidates):
        return 0, 0, "Visual review could not access the video file; transcript/audio ranking was used."
    pool = candidates[:MAX_REVIEW]
    tokens, reviewed = 0, 0
    for base in range(0, len(pool), BATCH_SIZE):
        group = pool[base:base + BATCH_SIZE]
        try:
            image = _contact_sheet(group, source_path)
            prompt = ("You are reviewing short-form gaming clip candidates from a GTA 6 gaming stream. "
                      "Each row has three frames in time order. Candidate IDs are C1 to C" + str(len(group)) + ". "
                      "Score visible short-form value from 0-100: clear action/reaction, funny or surprising payoff, "
                      "readable facecam/game moment, and whether the payoff actually appears. Penalize loading screens, "
                      "menus, static scenes, confusing crops, or no visible event. Do not invent what happened from "
                      "the spoken words. Return JSON only: {\"items\":[{\"id\":\"C1\",\"score\":0,\"reason\":\"short\"}]}. "
                      "Include every candidate exactly once.")
            raw, sent, wrote = ai.see(prompt, image, timeout=45)
            if sent or wrote:
                usage.record(sent, wrote, "Video: visual candidate review")
            tokens += sent + wrote
            parsed = ai._parse(raw)
            by_id = {str(x.get("id", "")).upper(): x for x in parsed if isinstance(x, dict)} if isinstance(parsed, list) else {}
            for i, clip in enumerate(group, 1):
                result = by_id.get(f"C{i}")
                if not result:
                    continue
                clip["visual_score"] = max(0, min(100, int(float(result.get("score", 0)))))
                clip["visual_reason"] = str(result.get("reason", ""))[:240]
                clip["score"] = round(0.7 * float(clip.get("score", 0)) + 0.3 * clip["visual_score"])
                reviewed += 1
        except Exception as exc:  # noqa: BLE001 - a vision outage must not strand the clip job
            log(f"Visual review was unavailable for candidates {base + 1}–{base + len(group)}: {str(exc)[:180]}")
            continue
    if len(candidates) > len(pool):
        return tokens, reviewed, f"Visually reviewed {reviewed} of the top {len(pool)} candidates; the rest were ranked from speech and audio."
    return tokens, reviewed, (f"Visually reviewed {reviewed} candidate moments from three sampled frames each."
                              if reviewed else "Visual review did not return usable scores; transcript/audio ranking was used.")
