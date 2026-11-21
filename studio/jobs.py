"""Job pipeline: get video -> audio -> transcript -> AI picks -> rendered clips. One heavy task at a time."""
import collections
import copy
import json
import os
import math
import queue
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

import numpy as np

from . import ai, brain, config, creators, ctx, facecam, games, load, media, quality, render, roman, safe, transcribe, usage

class _FairQueue:
    """Whose video is next. Each person has their own line and the lines take turns, so someone who adds ten
    videos does not make everyone else wait behind all ten. Small tasks (re-making one clip) go first."""

    def __init__(self):
        self.cv = threading.Condition()
        self.quick = collections.deque()
        self.lines = {}                       # user -> their waiting videos, oldest first
        self.turn = collections.deque()       # users with something waiting, in the order they will be served
        self.epoch = 0                        # goes up whenever the line moves
        self._pos = (0.0, {})
        self.running = 0                      # videos being worked on right now
        self.last_start = 0.0
        self.held = False                     # videos are waiting only because the machine is near full

    def _room(self) -> bool:
        """May one more video start? One always may. More only while the machine has room, and a few seconds
        after the last one started, so its weight is already showing in the measurements."""
        if self.running == 0:
            return True
        return self.running < config.JOB_WORKERS and time.time() - self.last_start > 6 and not load.busy()

    def done(self):
        with self.cv:
            self.running = max(0, self.running - 1)
            self.cv.notify_all()

    def put(self, item, user=None):
        with self.cv:
            if user is None:
                self.quick.append(item)
            else:
                if user not in self.lines:
                    self.lines[user] = collections.deque()
                    self.turn.append(user)
                self.lines[user].append(item)
            self.epoch += 1
            self.cv.notify()

    def remove_job(self, jid):
        """Remove a not-yet-started video from its owner's fair queue."""
        removed = False
        with self.cv:
            for item in list(self.quick):
                if item[0] == "job" and item[1] == jid:
                    self.quick.remove(item)
                    removed = True
            for user, line in list(self.lines.items()):
                kept = collections.deque(item for item in line if not (item[0] == "job" and item[1] == jid))
                if len(kept) != len(line):
                    removed = True
                    if kept:
                        self.lines[user] = kept
                    else:
                        del self.lines[user]
                        self.turn = collections.deque(u for u in self.turn if u != user)
            if removed:
                self.epoch += 1
                self._pos = (0.0, {})
                self.cv.notify_all()
        return removed

    def get(self, quick_only=False):
        with self.cv:
            while True:
                if self.quick:
                    self.epoch += 1
                    return self.quick.popleft()
                if not quick_only and self.turn:
                    if self._room():
                        break
                    if not self.held:
                        self.held = True
                        self.epoch += 1
                elif self.held:
                    self.held = False
                    self.epoch += 1
                self.cv.wait(1.0)             # looked at again every second: the line moves the moment there is room
            self.epoch += 1
            self.held = False
            self.running += 1
            self.last_start = time.time()
            user = self.turn.popleft()
            item = self.lines[user].popleft()
            if self.lines[user]:
                self.turn.append(user)
            else:
                del self.lines[user]
            return item

    def ahead(self) -> dict:
        """job id -> how many videos will start before it. Worked out at most once a second."""
        with self.cv:
            if time.time() - self._pos[0] < 1:
                return self._pos[1]
            out, lines, order, n = {}, {u: list(q) for u, q in self.lines.items()}, list(self.turn), 0
            depth = 0
            while order:
                nxt = []
                for u in order:
                    if depth < len(lines[u]):
                        out[lines[u][depth][1]] = n
                        n += 1
                        if depth + 1 < len(lines[u]):
                            nxt.append(u)
                order, depth = nxt, depth + 1
            self._pos = (time.time(), out)
            return out


_q = _FairQueue()
_live_q: "queue.Queue" = queue.Queue()   # live clips never wait behind a long video
_jobs: dict = {}
_by_user: dict = {}                       # user -> {job id: job}: listing one person's videos never scans everyone's
_lock = threading.RLock()
on_clip_ready: list = []   # callbacks (job, clip), run once when a clip first finishes

DEFAULTS = {"max_clips": 50, "min_len": 15, "max_len": 60, "min_score": 80, "style": "bounce", "layout": "stacked", "quality": 720,
            "range_start": None, "range_end": None, "captions": True}


LIMITS = {"max_clips": (1, 500), "min_len": (5, 120), "max_len": (10, 180), "min_score": (50, 100), "quality": (360, 1080)}


def clean_options(options) -> dict:
    """Only known settings, each inside its allowed range. Raises ValueError on nonsense."""
    out = {}
    for k, v in (options if isinstance(options, dict) else {}).items():
        if k == "captions":
            if not isinstance(v, bool):
                raise ValueError("Captions must be on or off.")
            out[k] = v
            continue
        if k not in DEFAULTS or v in ("", None):
            continue
        if k in LIMITS:
            lo, hi = LIMITS[k]
            v = max(lo, min(hi, int(float(v))))
        elif k == "style":
            v = v if v in render.STYLES else DEFAULTS["style"]
        elif k == "layout":
            v = v if v in render.LAYOUTS else DEFAULTS["layout"]
        else:                                    # range_start, range_end: seconds into the video
            v = float(v)
            if not math.isfinite(v) or v < 0:
                raise ValueError("That part of the video isn't valid.")
        out[k] = v
    if out.get("range_end") is not None and out["range_end"] <= out.get("range_start", 0):
        raise ValueError("The end of the chosen part must be after its start.")
    return out


def job_dir(jid) -> Path:
    return Path(_jobs[jid]["dir"])


HIDDEN = ("dir", "user", "source_path", "recover", "_facecam_checked")


def public(job) -> dict:
    """What the browser may see: no server paths, no owner id."""
    out = {k: v for k, v in job.items() if k not in HIDDEN}
    out["source"] = {k: v for k, v in job["source"].items() if k != "path"}
    out["clips"] = [{k: v for k, v in c.items() if k not in ("src", "fixed_words", "fixed_for")} for c in job["clips"]]
    ai = job.get("ai") or {}
    out["ai"] = {"tokens": ai.get("tokens", 0), "note": ai.get("note", ""),
                  "visual_review": ai.get("visual_review", ""), "used": bool(ai.get("model"))}   # never the model, never raw errors
    if job.get("status") == "queued":
        n = _q.ahead().get(job["id"])
        if n:
            out["stage"] = (f"High demand right now: {n} video{'s' if n != 1 else ''} ahead of yours" if _q.held else
                            f"Waiting in line: {n} video{'s' if n != 1 else ''} ahead of yours")
        elif n == 0 and _q.held:
            out["stage"] = "High demand right now: yours is next"
    return out


def save(job):
    ctx.bump(job.get("user"))
    with _lock:
        d = Path(job["dir"])
        for _ in range(5):
            try:
                text = json.dumps(job, indent=1)
                break
            except RuntimeError:  # changed mid-dump by another thread
                time.sleep(0.05)
        else:
            return
        tmp = d / "job.json.tmp"
        tmp.write_text(text, encoding="utf-8")
        destination = d / "job.json"
        for attempt in range(8):
            try:
                tmp.replace(destination)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                # Windows scanners can briefly hold a just-updated JSON file open.
                # Retry instead of letting a yt-dlp progress callback abort the download.
                time.sleep(0.1 * (attempt + 1))


def _set(job, **kw):
    with _lock:
        job.update(kw)
    save(job)


def _remap(path, folder: Path):
    """Paths saved before data moved into per-user folders (or on another machine): find the file by name."""
    if path and not Path(path).exists():
        cand = folder / Path(str(path).replace("\\", "/")).name
        if cand.exists():
            return str(cand)
    return path


def _load_file(f: Path, uid: str):
    try:
        job = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    job.update(dir=str(f.parent), user=uid)
    job.setdefault("kind", "video")
    job.setdefault("creator", None)
    job.setdefault("options", {}).setdefault("captions", True)
    if job.get("source_path"):
        job["source_path"] = _remap(job["source_path"], f.parent)
    if job.get("source", {}).get("path"):
        job["source"]["path"] = _remap(job["source"]["path"], f.parent)
    if job.get("status") in ("queued", "running") and job.get("kind") != "live":
        # Picked up again as soon as the server is back (jobs.start), from the last finished part of the transcript.
        job.update(status="queued", stage="Picking up where it stopped", error=None, resume=True)
        job.pop("ai_retrying", None)
        job.pop("retry_started", None)
    if (job.get("kind") != "live" and job.get("status") == "error"
            and any(marker in str(job.get("error", "")) for marker in
                    ("Unable to allocate", "ArrayMemoryError", "job.json.tmp", "Access is denied"))):
        # Retry older audio-memory failures with chunked reads; recover transient Windows save failures too.
        job.update(status="queued", stage="Retrying with low-memory audio processing", error=None, resume=True)
    if job.get("resume") and job.get("done_blocks") and job.get("duration"):
        options = job.get("options") or {}
        start = float(options.get("range_start") or 0)
        end = float(options.get("range_end") or job["duration"])
        span = max(1.0, end - start)
        restored = 0.14 + 0.78 * min(1.0, int(job["done_blocks"]) * BLOCK / span)
        job["progress"] = max(float(job.get("progress") or 0), min(0.92, restored))
    if job.get("status") == "live":
        job.update(status="done", stage="Recording paused by a server update", was_live=True)
    buffered = job.get("kind") == "live" and any((f.parent / "buffer").glob("*.ts"))
    for c in job.get("clips", []):
        c.setdefault("captions", job["options"]["captions"])
        if c.get("src"):
            c["src"] = _remap(c["src"], f.parent / "clips")
        if c.get("status") in ("rendering", "queued"):
            if buffered and not c.get("src"):
                c["status"] = "queued"  # the stream is still on disk: live.recover() finishes these
                job["recover"] = True
            elif job.get("resume"):
                c["status"] = "queued"  # the video is being picked up again: this clip is made then
            elif job.get("kind") != "live" and (f.parent / "words.json").is_file():
                c["status"] = "queued"  # a single clip being re-made: jobs.start() makes it again
                job.setdefault("remake", []).append(c["id"])
            else:
                c.update(status="error", error="Interrupted by a restart. Open it and press Save and re-make.")
    _jobs[job["id"]] = job
    _by_user.setdefault(uid, {})[job["id"]] = job


def load_user(uid: str):
    d = config.USERS_DIR / uid
    for f in list((d / "jobs").glob("*/job.json")) + list((d / "creators").glob("*/*/*/job.json")):
        _load_file(f, uid)


def load_all():
    for d in config.USERS_DIR.iterdir():
        if d.is_dir():
            load_user(d.name)


def all_jobs() -> list:
    """Every user's jobs. Only for background work; never send this to a browser."""
    with _lock:
        return list(_jobs.values())


def list_jobs():
    u = ctx.uid()
    with _lock:
        return sorted((dict(j) for j in _by_user.get(u, {}).values()), key=lambda j: -j["created"])


def get(jid):
    """The job, only if it belongs to whoever this code is running for."""
    job = _jobs.get(jid)
    return job if job and job["user"] == ctx.uid() else None


def busy_count() -> int:
    u = ctx.uid()
    return sum(1 for j in list(_by_user.get(u, {}).values()) if j["kind"] == "video" and j["status"] in ("queued", "running", "pausing"))


def waiting_total() -> int:
    return sum(1 for j in list(_jobs.values()) if j["kind"] == "video" and j["status"] == "queued")


def queue_epoch() -> int:
    return _q.epoch


def running_now() -> int:
    return _q.running


def create(source: dict, options: dict, enqueue=True, creator=None, kind="video") -> dict:
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]
    c = creators.get(creator) if creator else None
    if c:
        d = creators.root(c["id"]) / ("live" if kind == "live" else "videos") / jid
        base = {**DEFAULTS, **{k: c[k] for k in ("max_clips", "min_len", "max_len", "min_score", "style", "layout", "captions")}}
    else:
        d, base, creator = config.user_dir(ctx.uid()) / "jobs" / jid, dict(DEFAULTS), None
    opts = {**base, **clean_options(options)}
    if opts["min_len"] >= opts["max_len"]:
        opts["min_len"] = max(5, opts["max_len"] - 5)
    job = {"id": jid, "user": ctx.uid(), "kind": kind, "creator": creator, "dir": str(d), "created": time.time(), "source": source,
           "options": opts, "status": "queued", "stage": "Waiting", "progress": 0.0,
           "title": source.get("name", ""), "uploader": "", "duration": 0, "clips": [], "error": None, "ai": {}}
    d.mkdir(parents=True)
    with _lock:
        _jobs[jid] = job
        _by_user.setdefault(job["user"], {})[jid] = job
    save(job)
    if enqueue:
        _q.put(("job", jid, None), user=job["user"])
    return job


def enqueue(job):
    save(job)
    _q.put(("job", job["id"], None), user=job["user"])


class _PauseAtCheckpoint(Exception):
    pass


def pause(jid):
    job = get(jid)
    if not job or job.get("kind") != "video":
        raise ValueError("Only video clipping jobs can be paused.")
    if job.get("status") == "queued":
        removed = _q.remove_job(jid)
        job.update(status="paused" if removed else "pausing",
                   stage="Paused before processing" if removed else "Pause requested · finishing the current step",
                   resume=bool((Path(job["dir"]) / "words.json").is_file()), pause_requested=not removed,
                   paused_from="queued")
        save(job)
    elif job.get("status") == "running":
        job.update(status="pausing", pause_requested=True, stage="Pause requested · finishing the current step")
        save(job)
    else:
        raise ValueError("This video is not currently processing.")
    return job


def resume(jid):
    job = get(jid)
    if not job or job.get("kind") != "video" or job.get("status") != "paused":
        raise ValueError("This video is not paused.")
    job.update(status="queued", stage="Waiting to resume", error=None, pause_requested=False,
               resume=(Path(job["dir"]) / "words.json").is_file())
    save(job)
    enqueue(job)
    return job


def _pause_checkpoint(job):
    if job.get("pause_requested"):
        job.update(status="paused", stage="Paused · safe to resume", pause_requested=False,
                   resume=(Path(job["dir"]) / "words.json").is_file())
        save(job)
        raise _PauseAtCheckpoint()


def retry_ai_selection(job):
    """Re-rank a saved transcript after an AI outage, without downloading or transcribing the full video again."""
    if job.get("kind") != "video" or job.get("status") not in ("done", "error"):
        raise ValueError("Only a finished or stopped video can retry AI selection.")
    if job.get("clips"):
        raise ValueError("This video already has clips.")
    if job.get("ai_retrying"):
        raise ValueError("AI selection is already running for this video.")
    d = Path(job["dir"])
    transcript = d / "words.json"
    if not transcript.is_file():
        raise ValueError("The saved transcript is missing, so this video would need to be processed again.")
    if (job.get("source") or {}).get("type") != "url" or not (job.get("source") or {}).get("url"):
        raise ValueError("The original video link is missing, so selected clips cannot be fetched.")
    if not brain.ai.ready():
        raise ValueError("Set up a working AI model in Settings → AI first.")

    job.update(status="running", stage="Checking AI before re-ranking the saved transcript", progress=0.05,
               error=None, ai_retrying=True, ai={**(job.get("ai") or {}), "note": ""})
    save(job)

    def work():
        ai_log = []
        try:
            # Fail fast on a provider outage instead of spending minutes retrying each transcript window.
            brain.ai.test()
            brain.ai.clear_problem()
            usage.forget_all()
            words = json.loads(transcript.read_text(encoding="utf-8"))
            options = job.get("options") or DEFAULTS
            start = float(options.get("range_start") or 0)
            end = min(float(job.get("duration") or 0), float(options.get("range_end") or job.get("duration") or 0))
            span = max(0.0, end - start)
            if span < 10:
                raise ValueError("The saved transcript does not cover a usable section of this video.")
            review = [dict(w, s=round(w["s"] - start, 2), e=round(w["e"] - start, 2))
                      for w in words if start <= w.get("s", -1) < end]
            if len(review) < 8:
                raise ValueError("There is too little speech in the saved transcript to select clips.")
            job.update(stage="Ranking moments from the saved transcript", progress=0.25)
            save(job)
            from . import posting as post_queue
            game = job.get("game") or games.detect(job.get("title"), job.get("uploader"))[0] or ""

            def ai_progress(message):
                ai_log.append(message)
                if message.startswith("AI reviewing "):
                    job.update(stage=message)
                    save(job)

            found, info = brain.find_clips(
                review, np.zeros(max(1, math.ceil(span)), dtype=np.float32), span, job.get("title", ""),
                job.get("uploader", ""), int(options.get("max_clips", 6)), int(options.get("min_len", 15)),
                int(options.get("max_len", 60)), ai_progress, game=game,
                category_guidance=post_queue.category_guidance(job.get("creator", "")),
                min_score=int(options.get("min_score", 80)))
            if not info.get("model"):
                raise RuntimeError(info.get("note") or "The AI could not rank this transcript.")
            job["game"] = game
            candidates = []
            for c in found:
                c.update(id=f"clip{len(candidates) + 1:02d}", start=round(c["start"] + start, 2),
                         end=round(c["end"] + start, 2), style=options.get("style", DEFAULTS["style"]),
                         layout=options.get("layout", DEFAULTS["layout"]),
                         captions=options.get("captions", True), status="queued", version=0)
                c["hashtags"] = post_queue._personal_hashtags(job, c)
                job["clips"].append(c)
                candidates.append(c)
            job["ai"] = {"model": info.get("model"), "tokens": info.get("tokens", 0),
                          "note": "Selected from the saved transcript; the original full audio was not downloaded again.",
                          "log": []}
            job.update(stage=f"Cutting {len(candidates)} selected clips from the original link", progress=0.88)
            save(job)
            for i, clip in enumerate(candidates):
                render_one(job, clip, words)
                job.update(progress=0.88 + 0.12 * (i + 1) / max(1, len(candidates)))
                save(job)
            job.update(status="done", stage=f"Done · {sum(c.get('status') == 'done' for c in candidates)} clips recovered",
                        progress=1.0, took=round(time.time() - job.get("retry_started", time.time())))
            try:
                from . import notices
                notices.add(job.get("user"), "clips", f"{len(candidates)} clips recovered", job.get("title") or "your video",
                            f"/app/creators/{job['creator']}" if job.get("creator") else "/app/videos")
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            brain.ai.note_problem(e)
            job["ai"] = {**(job.get("ai") or {}), "note": brain.ai.explain(e) or safe.friendly(e, 300),
                          "log": ai_log[-10:] or (job.get("ai") or {}).get("log", [])}
            job.update(status="done", stage="AI retry stopped before full video processing", progress=1.0)
        finally:
            job.pop("ai_retrying", None)
            job.pop("retry_started", None)
            save(job)

    job["retry_started"] = time.time()
    submit(work)
    return job


def submit(fn):
    """Run any heavy task (a render, a cleanup) on the single worker, in order."""
    _q.put(("fn", ctx.bind(fn), None))


def submit_live(fn):
    """Live-stream renders and cleanups: their own worker, so a long video can never hold them up."""
    _live_q.put(ctx.bind(fn))


def _live_worker():
    while True:
        fn = _live_q.get()
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def delete(jid):
    with _lock:
        job = _jobs.pop(jid, None)
        if job:
            _by_user.get(job["user"], {}).pop(jid, None)
    if job:
        ctx.bump(job["user"])
        shutil.rmtree(job["dir"], ignore_errors=True)
    return bool(job)


def request_render(jid, cid, changes: dict):
    job = _jobs[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    new = dict(clip)
    if "captions" in changes:
        new.update(clean_options({"captions": changes["captions"]}))
    for k in ("start", "end"):
        if k in changes:
            new[k] = round(float(changes[k]), 2)
    if "src_offset" in clip:  # live clip: can only move inside the saved piece of stream
        new["start"] = max(new["start"], clip["src_offset"])
        new["end"] = min(new["end"], clip["src_end"])
    if not (math.isfinite(new["start"]) and math.isfinite(new["end"])) or new["start"] < 0:
        raise ValueError("Those times aren't valid.")
    if not 3 <= new["end"] - new["start"] <= 180:
        raise ValueError("A clip can be 3 to 180 seconds long.")
    for k in ("hook", "title", "description", "style", "layout"):
        if k in changes:
            new[k] = str(changes[k])[:{"hook": 60, "title": 100, "description": 2000}.get(k, 20)]
    if new.get("style") not in render.STYLES:
        new["style"] = DEFAULTS["style"]
    if new.get("layout") not in render.LAYOUTS:
        new["layout"] = DEFAULTS["layout"]
    clip.update(new, status="queued")
    save(job)
    _q.put(("clip", jid, cid))


def create_layout_variants(jid, layouts):
    """Queue alternate renders of the finished moments in a video, without repeating analysis."""
    job = get(jid)
    if not job or job.get("kind") == "live" or job.get("status") != "done":
        raise ValueError("Layout versions are available after a video has finished clipping.")
    if not isinstance(layouts, list):
        raise ValueError("Choose at least one layout.")
    wanted = list(dict.fromkeys(str(x) for x in layouts if str(x) in render.LAYOUTS))
    if not wanted:
        raise ValueError("Choose at least one valid layout.")
    bases = [c for c in job.get("clips", []) if c.get("status") == "done" and not c.get("local_deleted") and not c.get("variant_of")]
    existing = {(c.get("variant_of"), c.get("layout")) for c in job.get("clips", []) if c.get("variant_of")}
    pairs = [(base, layout) for base in bases for layout in wanted
             if layout != base.get("layout") and (base["id"], layout) not in existing]
    if not pairs:
        raise ValueError("There are no new layout versions to make for this video.")
    if len(pairs) > 100:
        raise ValueError("This would create more than 100 clips at once. Choose fewer layouts and run the button again for another batch.")
    face_box = job.get("facecam")
    if not face_box and any(layout in ("gameplay", "reaction", "stacked", "split", "bottomcam") for _, layout in pairs):
        job["_facecam_attempts"] = 0  # a user-requested layout pass may inspect a new set of source pieces
        candidates, seen = [], set()
        for source in ([job.get("source_path")] if job.get("source_path") else []) + [c.get("src") for c in bases]:
            if not source or not Path(source).is_file():
                continue
            resolved = str(Path(source).resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(source)
            if len(candidates) == 3:
                break
        for candidate_source in candidates:
            try:
                face_box = _facecam(job, candidate_source,
                                    require_visual=any(layout in ("stacked", "bottomcam") for _, layout in pairs))
            except Exception:  # noqa: BLE001 - a facecam search failure should not block other layouts
                face_box = None
            if face_box:
                break
    if not face_box and any(layout in ("stacked", "bottomcam") for _, layout in pairs):
        raise ValueError("ClipShorts could not confirm a facecam in this video, so it did not queue these layouts. Check that the source has a visible facecam and that AI Settings has a working vision model, then try again.")
    made = []
    for base, layout in pairs:
        variant = copy.deepcopy(base)
        variant.update(id="variant_" + uuid.uuid4().hex[:14], status="queued", version=0, layout=layout,
                       variant_of=base["id"], no_autopost=True)
        if face_box and layout in ("gameplay", "reaction", "stacked", "split", "bottomcam"):
            variant["facecam"] = face_box
        for key in ("file", "cover", "error", "made_at", "local_deleted", "posts"):
            variant.pop(key, None)
        job["clips"].append(variant)
        made.append(variant)
    save(job)
    for variant in made:
        _q.put(("clip", jid, variant["id"]))
    if job.get("creator"):
        creators.log(job["creator"], f"Making {len(made)} layout version{'s' if len(made) != 1 else ''} from finished moments.")
    return len(made)


def update_clip_metadata(jid, cid, changes: dict):
    """Save post copy and category without re-encoding the video."""
    job = _jobs[jid]
    clip = next(c for c in job["clips"] if c["id"] == cid)
    for key, limit in (("title", 100), ("description", 2000)):
        if key in changes:
            clip[key] = brain._text(changes[key], limit)
    if "category" in changes:
        category = str(changes["category"] or "other").strip().lower()
        if category not in brain.CATEGORIES:
            raise ValueError("Choose a valid clip type.")
        clip["category"] = category
    if "hashtags" in changes:
        raw = changes["hashtags"]
        values = raw if isinstance(raw, list) else str(raw or "").split(",")
        tags, seen = [], set()
        for value in values:
            tag = re.sub(r"[^\w]", "", str(value or "").lstrip("#"))[:30]
            if tag and tag.lower() not in seen:
                tags.append(tag)
                seen.add(tag.lower())
            if len(tags) >= 8:
                break
        clip["hashtags"] = tags
    d = Path(job["dir"]) / "clips" / f"{cid}.json"
    d.write_text(json.dumps({k: clip.get(k) for k in ("title", "description", "hashtags", "hook", "start", "end", "score")}),
                 encoding="utf-8")
    save(job)
    return job


def render_one(job, clip, words):
    d = Path(job["dir"])
    clip["status"] = "rendering"
    save(job)
    try:
        if not clip.get("src") and not job.get("source_path"):
            _piece_again(job, clip)
        off = clip.get("src_offset", 0.0)
        src = Path(clip["src"]) if clip.get("src") else Path(job["source_path"])
        if clip.get("layout") in ("gameplay", "reaction", "stacked", "split", "bottomcam") and not clip.get("facecam"):
            box = _facecam(job, src, require_visual=clip.get("layout") in ("stacked", "bottomcam"))
            if box:
                clip["facecam"] = box
        if not clip.get("src"):
            clip["end"] = min(clip["end"], job["duration"])
        local = dict(clip, start=clip["start"] - off, end=clip["end"] - off, hook=roman.to_roman(clip.get("hook") or ""))
        near = _caption_words(job, clip, words, off) if clip.get("captions", True) else []
        out = render.render_clip(src, d / "clips", clip["id"], local, near, job["width"], job["height"])
        if clip.get("layout") in ("stacked", "bottomcam") and out.get("layout") != clip.get("layout"):
            raise RuntimeError(f"Requested {clip['layout']} layout, but the renderer returned {out.get('layout') or 'no layout'}. The clip was not marked ready.")
        clip.update(out, status="done", version=clip.get("version", 0) + 1, error=None)
        post = {k: clip[k] for k in ("title", "description", "hashtags", "hook", "start", "end", "score")}
        (d / "clips" / f"{clip['id']}.json").write_text(json.dumps(post, indent=1), encoding="utf-8")
        if clip["version"] == 1:
            clip.setdefault("made_at", time.time())
            if not clip.get("no_autopost"):
                creators.log(job["creator"], f"Clip ready (score {clip['score']}): {clip['title'] or clip['hook']}")
                save(job)
                for fn in on_clip_ready:
                    try:
                        fn(job, clip)
                    except Exception:  # noqa: BLE001
                        traceback.print_exc()
    except Exception as e:  # noqa: BLE001
        clip.update(status="error", error=safe.friendly(e, 400))
        creators.log(job["creator"], f"Clip failed: {safe.friendly(e, 200)}")
    save(job)


def _caption_words(job, clip, words, off):
    span = [round(clip["start"], 2), round(clip["end"], 2), "en2"]      # "en2": captions made always-English
    if clip.get("fixed_for") == span and clip.get("fixed_words") and not any(
            roman.needs_roman(w.get("w", "")) for w in clip["fixed_words"]):
        raw = clip["fixed_words"]          # already tidied for this exact span: no second AI call
    else:
        raw = [w for w in words if clip["start"] - 1 <= w["s"] <= clip["end"] + 1]
        try:
            usage.set_context(job.get("creator"), job.get("title") or "")
            raw = brain.english_captions(raw, job.get("lang") or "", job.get("title") or "",
                                         youtube_english=lambda: _english_part(job, clip))
            clip.update(fixed_words=raw, fixed_for=span)
        except Exception:  # noqa: BLE001 - captions still render with the words as heard
            pass
    # the last word on it: whatever was said, nothing is drawn in Hindi or Urdu script
    return [dict(w, s=w["s"] - off, e=w["e"] - off) for w in roman.words(raw)]


CLIP_MINUTES = float(config.env("PEAKCUT_CLIP_MINUTES", "15"))     # the longest one clip may take to make


def _wait_for_clips(job, futures, began, workers):
    """Wait for a video's clips, but not for ever. A clip that hangs (a download that stalls, an encoder that
    freezes) used to keep the whole video "in progress", and no other clip could be re-made. Now it is marked as
    failed after CLIP_MINUTES and the video finishes with the clips that did work."""
    left, given_up = list(futures), 0
    late = "This clip took too long and was stopped. Open it and press Save and re-make to try again."
    while left:
        still = []
        for f, clip in left:
            if f.done():
                f.result()
                continue
            t = began.get(clip["id"])
            if (t and time.time() - t > CLIP_MINUTES * 60) or (not t and given_up >= workers):
                given_up += 1
                clip.update(status="error", error=late)
                creators.log(job["creator"], f"Clip stopped after {CLIP_MINUTES:g} minutes: {clip.get('title') or clip.get('hook') or clip['id']}")
                save(job)
                continue
            still.append((f, clip))
        left = still
        if left:
            time.sleep(1)


def _english_part(job, clip):
    """YouTube's English captions for just this clip, for a video in another language. Fetched once per video."""
    if (job.get("source") or {}).get("type") != "url":
        return None
    f = Path(job["dir"]) / "words_en.json"
    if f.is_file():
        en = json.loads(f.read_text(encoding="utf-8"))
    else:
        try:
            en = media.youtube_english_words(job["source"]["url"]) or []
        except Exception:  # noqa: BLE001 - YouTube often refuses its translations: the AI translates instead
            en = []
        f.write_text(json.dumps(en), encoding="utf-8")       # asked once per video, refused or not
    return [w for w in en if clip["start"] - 0.5 <= w["s"] <= clip["end"] + 0.5]


LONG_VIDEO = 40 * 60   # above this, fetch only the pieces that become clips
PAD = 8                # seconds kept either side of a fetched piece, so clips can be re-trimmed


def _piece_again(job, clip):
    """Remaking a clip whose own piece of the video was never fetched (its first try failed) and whose whole video
    is not here: fetch the piece now, cut exactly at the asked time, so no sound matching is needed."""
    src = job.get("source") or {}
    if src.get("type") != "url" or not src.get("url"):
        raise RuntimeError("The original video is no longer here, so this clip can't be made again. Add the video again.")
    d = Path(job["dir"])
    want = max(0.0, clip["start"] - PAD)
    until = clip["end"] + PAD if not job.get("duration") else min(job["duration"], clip["end"] + PAD)
    try:
        piece = media.download_section(src["url"], d / "clips", f"{clip['id']}.src", want, until,
                                       int(job["options"]["quality"]), exact=True)
        meta = media.probe(piece)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Could not fetch this part of the video: {safe.friendly(e, 250)}") from e
    clip.update(src=str(piece), src_offset=round(want, 2), src_end=round(want + meta["duration"], 2))
    clip["end"] = min(clip["end"], clip["src_end"])
    job.setdefault("width", meta["width"])
    job.setdefault("height", meta["height"])


def _fetch_piece(job, clip, full_audio):
    """Long videos: download just this clip's stretch and pin down exactly where it starts."""
    d = Path(job["dir"])
    o = job["options"]
    try:
        want = max(0.0, clip["start"] - PAD)
        piece = media.download_section(job["source"]["url"], d / "clips", f"{clip['id']}.src", want,
                                       min(job["duration"], clip["end"] + PAD), int(o["quality"]))
        wav = d / "clips" / f"{clip['id']}.wav"
        media.extract_audio(piece, wav)
        part = media.load_audio(wav)
        wav.unlink(missing_ok=True)
        meta = media.probe(piece)
        off = media.find_offset(full_audio, part, want)
        clip.update(src=str(piece), src_offset=round(off, 2), src_end=round(off + meta["duration"], 2))
        clip["start"], clip["end"] = max(clip["start"], clip["src_offset"]), min(clip["end"], clip["src_end"])
        job.setdefault("width", meta["width"])
        job.setdefault("height", meta["height"])
    except Exception as e:  # noqa: BLE001
        clip.update(status="error", error=f"Could not fetch this part of the video: {safe.friendly(e, 250)}")


BLOCK = 10 * 60         # long videos are transcribed and clipped one block at a time
PROGRESSIVE_FROM = 20 * 60


def _covers(words: list, start: float, end: float) -> bool:
    """Is a borrowed transcript good enough to find clips with? It has to reach the end of the part being clipped
    and have a normal amount of talking in it; otherwise our own model writes the transcript."""
    inside = [w for w in words if start <= w["s"] <= end]
    minutes = max(0.5, (end - start) / 60)
    return len(inside) >= 25 * minutes and inside[-1]["s"] >= start + 0.85 * (end - start)


def _facecam(job, source_path=None, require_visual=False) -> list | None:
    """Where the webcam is in the source: once for the whole video, for every clip of it.

    Only a sure answer is used. A box that is a scoreboard drawn over the game would
    put a picture of a scoreboard in the corner of the clip. One frame is also shown to
    the AI, which tells a person from a game character better than any measurement can;
    what it says is measured again on the picture, so it cannot do any harm."""
    if job.get("facecam"):
        return job["facecam"]
    # A creator can keep a verified, normalized camera position. Stream layouts tend to stay
    # fixed for that channel, and this avoids asking vision for every new upload.
    try:
        profile = (creators.get(job.get("creator")) or {}).get("facecam") if job.get("creator") else None
        if isinstance(profile, (list, tuple)) and len(profile) == 4:
            width, height = int(job.get("width") or 0), int(job.get("height") or 0)
            if (not width or not height) and source_path and Path(source_path).is_file():
                width, height = facecam._size(source_path)
            if width > 0 and height > 0:
                box = [round(float(profile[0]) * width), round(float(profile[1]) * height),
                       round(float(profile[2]) * width), round(float(profile[3]) * height)]
                if box[2] > 0 and box[3] > 0 and box[0] + box[2] <= width and box[1] + box[3] <= height:
                    job["facecam"] = box
                    return box
    except (TypeError, ValueError, OSError):
        pass
    path = job.get("source_path") or source_path
    if not path or not Path(path).is_file():
        return None
    if int(job.get("_facecam_attempts", 0) or 0) >= 3:
        return None
    job["_facecam_attempts"] = int(job.get("_facecam_attempts", 0) or 0) + 1
    job["_facecam_checked"] = True
    try:
        # First look locally so a new creator normally costs no vision API call. If the local
        # border/motion scan cannot confirm a camera, ask vision once as a fallback.
        box = facecam.detect(path)
        if box and require_visual:
            width, height = int(job.get("width") or 0), int(job.get("height") or 0)
            if width <= 0 or height <= 0:
                width, height = facecam._size(path)
            if width > 0 and height > 0 and (box["w"] > width * 0.40 or box["h"] > height * 0.40 or box.get("corner") == "middle"):
                box = None  # a large/central still panel is ambiguous; let vision confirm it
        if not box and require_visual and not job.get("_facecam_vision_attempted"):
            job["_facecam_vision_attempted"] = True
            box = facecam.detect(path, ask=brain.look_for_facecam, require_visual=True)
    except Exception:  # noqa: BLE001 - a missing facecam is not a failure
        return None
    if not box or box.get("conf") != "high":
        return None
    job["facecam"] = [box["x"], box["y"], box["w"], box["h"]]
    if job.get("creator"):
        try:
            width, height = int(job.get("width") or 0), int(job.get("height") or 0)
            if width <= 0 or height <= 0:
                width, height = facecam._size(path)
            if width > 0 and height > 0:
                creators.update(job["creator"], {"layout": "stacked", "facecam": [
                    box["x"] / width, box["y"] / height, box["w"] / width, box["h"] / height]})
        except (KeyError, TypeError, ValueError, OSError):
            pass  # a profile write must never stop the clip being made
    return job["facecam"]


def _run_job(job):
    import math
    from concurrent.futures import ThreadPoolExecutor
    d = Path(job["dir"])
    o = job["options"]
    src = job["source"]
    # Which game this is: what the creator set, or what the title says. It decides the extra rules the AI
    # reads and the tags every post carries.
    game = job.get("game") or ""
    if not game and job.get("creator"):
        try:
            game = (creators.get(job["creator"]) or {}).get("game") or ""
        except Exception:  # noqa: BLE001
            game = ""
    if not game:
        game = games.detect(job["title"], src.get("name"))[0] or ""
    if game and job.get("game") != game:
        job["game"] = game
    t0 = time.time()
    notes = []
    face = False       # where the webcam is in the source, found once; False means not looked for yet
    usage.set_context(job["creator"], job["title"] or src.get("name"))
    if job.get("status") == "queued":
        job.pop("pause_requested", None)
    resuming = bool(job.pop("resume", False)) and (d / "words.json").is_file()
    if resuming:   # after a restart: keep the transcript parts, clips and AI answers already made, and carry on
        _set(job, status="running", stage="Picking up where it stopped", error=None)
    else:
        _set(job, status="running", stage="Getting the audio", progress=0.03, error=None, clips=[], ai={}, done_blocks=0)
    if not transcribe.model_ready() and not transcribe.REMOTE:
        from . import fetch_model
        _set(job, stage="Getting the speech model (one time, about 480 MB)")
        fetch_model.ensure()
        _set(job, stage="Getting the audio")
    fetch = ThreadPoolExecutor(1)
    video = None
    wav = d / "audio.wav"
    cached_audio = wav.is_file()
    if src["type"] == "url":
        meta = media.info(src["url"])
        if meta["kind"] != "video":
            raise RuntimeError("That link is a channel or playlist, not a single video. Paste the link of one video.")
        if ((o["range_end"] or meta["duration"]) - (o["range_start"] or 0)) > config.MAX_VIDEO_HOURS * 3600:
            raise RuntimeError(f"That is longer than {config.MAX_VIDEO_HOURS:g} hours. Choose a shorter part of it under Options.")
        if meta["is_live"]:
            raise RuntimeError("This link is a live stream, so it would never finish downloading. Open the creator, "
                               "put this link under Setup, and press Start recording instead.")
        _set(job, title=meta["title"], uploader=meta["uploader"], duration=meta["duration"])
        if not job.get("thumb") and media.save_thumb(d, url=src["url"]):
            _set(job, thumb=True)
        usage.set_context(job["creator"], meta["title"])
        span = (o["range_end"] or meta["duration"]) - (o["range_start"] or 0)
        pieces = span > LONG_VIDEO or meta["duration"] <= 0
        if not pieces:  # shorter video: fetch all of it in the background while the audio is worked on
            video = fetch.submit(media.download, src["url"], d, int(o["quality"]))
        audio_file = None if cached_audio else media.download_audio(src["url"], d,
                                         lambda p: _set(job, progress=max(job.get("progress", 0), 0.03 + 0.1 * p)))
    else:
        pieces, audio_file = False, Path(src["path"])
        info = media.probe(audio_file)
        if info["duration"] > config.MAX_VIDEO_HOURS * 3600:
            raise RuntimeError(f"That video is longer than {config.MAX_VIDEO_HOURS:g} hours, which is the limit.")
        _set(job, source_path=str(audio_file), width=info["width"], height=info["height"], duration=info["duration"])
        if not job.get("thumb") and media.save_thumb(d, video=audio_file):
            _set(job, thumb=True)
    if not cached_audio:
        media.extract_audio(audio_file, wav)
    audio_len = media.audio_samples(wav)
    if src["type"] == "url" and audio_file:
        audio_file.unlink(missing_ok=True)
    _pause_checkpoint(job)
    if not job["duration"]:
        job["duration"] = audio_len / 16000
    lo = int((o["range_start"] or 0) * 16000)
    hi = min(audio_len, int(o["range_end"] * 16000)) if o["range_end"] else audio_len
    span = (hi - lo) / 16000
    db = media.loudness_per_second(wav, lo, hi)

    vlock, vstate = threading.Lock(), {}

    def whole_video():
        """Wait for the background download once; retry it, or give up and use pieces."""
        with vlock:
            if "path" not in vstate:
                path = None
                try:
                    path = video.result()
                except Exception as e:  # noqa: BLE001 - sites sometimes refuse a download
                    notes.append(f"Video download refused once ({str(e)[-80:]}), retrying")
                    try:
                        path = media.download(src["url"], d, int(o["quality"]))
                    except Exception as e2:  # noqa: BLE001
                        notes.append(f"Whole-video download failed ({str(e2)[-80:]}), fetching clip parts instead")
                if path:
                    info = media.probe(path)
                    job.update(source_path=str(path), width=info["width"], height=info["height"])
                vstate["path"] = path
            return vstate["path"]

    exact = {}      # clip id -> (from, to, words): our own model's words for just that clip

    began = {}      # clip id -> when work on it started

    def make(clip, words_now):
        began[clip["id"]] = time.time()
        if src["type"] == "url" and not clip.get("src") and (pieces or not whole_video()):
            _fetch_piece(job, clip, wav)
        if clip["status"] != "error" and clip.get("captions", True) and yt and yt[1].lower().startswith("en"):
            # The clip was found with YouTube's transcript. What is shown on screen comes from our own model,
            # run on just this clip: full punctuation, nothing blanked out, timings to the word.
            a0, b0 = max(lo, int((clip["start"] - 1.5) * 16000)), min(hi, int((clip["end"] + 1.5) * 16000))
            try:
                tw = [dict(w, s=round(w["s"] + a0 / 16000, 2), e=round(w["e"] + a0 / 16000, 2)) for w in transcribe.transcribe(media.read_audio(wav, a0, b0))]
            except Exception:  # noqa: BLE001 - the clip is still made, with YouTube's words
                tw = []
            if len(tw) >= 3:
                exact[clip["id"]] = (a0 / 16000, b0 / 16000, tw)
                words_now = tw
        if clip["status"] != "error":
            render_one(job, clip, words_now)

    cpus = os.cpu_count() or 4
    renders = ThreadPoolExecutor(2 if cpus < 8 else min(6, cpus // 6))  # more clips made at once on a bigger machine
    futures = []
    step = BLOCK * 16000 if span > PROGRESSIVE_FROM else hi - lo
    blocks = [(x, min(hi, x + step)) for x in range(lo, hi, step)] or [(lo, hi)]
    words, tokens, model = [], 0, None
    done_blocks = 0
    yt = None
    if src["type"] == "url" and config.YT_TRANSCRIPT and media.is_youtube(src["url"]):
        try:
            yt = media.youtube_words(src["url"])
        except Exception:  # noqa: BLE001 - no transcript to borrow: ours is written as usual
            yt = None
        if yt and not _covers(yt[0], lo / 16000, hi / 16000):
            yt = None
        job["transcript_from"] = "youtube" if yt else "own"
        job["lang"] = yt[1] if yt else ""          # the language spoken, when YouTube says (decides the captions)
    if resuming:
        try:
            words = json.loads((d / "words.json").read_text(encoding="utf-8"))
        except ValueError:
            words = []
        done_blocks = min(int(job.get("done_blocks", 0)), len(blocks)) if words else 0
        tokens, model = int((job.get("ai") or {}).get("tokens", 0)), (job.get("ai") or {}).get("model")
        if done_blocks < len(blocks):     # words past the last finished part are transcribed again
            cut = blocks[done_blocks][0] / 16000
            words = [w for w in words if w["s"] < cut]
        for c in job["clips"]:            # clips found before the restart that were not finished yet
            if c.get("status") not in ("done", "error"):
                c["status"] = "queued"
                futures.append((renders.submit(ctx.bind(make), c, list(words)), c))
        save(job)
        if done_blocks:
            completed = blocks[min(done_blocks, len(blocks)) - 1][1] - lo
            restored = 0.14 + 0.78 * completed / max(1, hi - lo)
            _set(job, progress=max(job.get("progress", 0), min(0.92, restored)))
    # The transcript is collected for the complete recording before moments are ranked. That lets the final
    # selection compare candidates from the beginning, middle, and end instead of reserving a quota per block.
    ahead = ThreadPoolExecutor(2, thread_name_prefix="ahead")
    heard, now_on = {}, [done_blocks]

    def hear(i):
        if i < len(blocks) and i >= done_blocks and i not in heard:
            a_, b_ = blocks[i]
            last = [0.0]

            def tp(p, i=i, a_=a_, b_=b_):
                if now_on[0] == i and p - last[0] > 0.05:      # only the part being shown moves the bar
                    last[0] = p
                    _set(job, progress=max(job["progress"], 0.14 + 0.78 * ((a_ - lo) + p * (b_ - a_)) / max(1, hi - lo)))
            heard[i] = ahead.submit(ctx.bind(transcribe.transcribe), media.read_audio(wav, a_, b_), tp)

    # The ad breaks and static screens of the whole video, found once. It reads the video end to end,
    # so it is done here rather than for each clip, and only when asked for.
    statics = quality.static_ranges(job.get("source_path") or "") if config.SKIP_STATIC else []
    for bi, (a, b) in enumerate(blocks):
        if bi < done_blocks:
            continue                      # transcribed and checked by the AI before the restart
        _pause_checkpoint(job)
        shift = a / 16000
        where = f" (minute {round((a - lo) / 16000 / 60)} of {round(span / 60)})" if len(blocks) > 1 else ""
        if yt:
            _set(job, stage="Reading the transcript" + where)
            bw = [dict(w, s=round(w["s"] - shift, 2), e=round(w["e"] - shift, 2)) for w in yt[0] if a / 16000 <= w["s"] < b / 16000]
        else:
            _set(job, stage="Writing the transcript" + where)
            now_on[0] = bi
            hear(bi)
            hear(bi + 1)
            bw = heard.pop(bi).result()
        _pause_checkpoint(job)
        _set(job, progress=max(job["progress"], 0.14 + 0.78 * (b - lo) / max(1, hi - lo)))
        words += [dict(w, s=round(w["s"] + shift, 2), e=round(w["e"] + shift, 2)) for w in bw]
        (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
        _set(job, stage="Reading the whole video" + where, word_count=len(words))
        job["done_blocks"] = bi + 1
        save(job)
        _pause_checkpoint(job)

    if not job["clips"]:
        _pause_checkpoint(job)
        _set(job, stage="Ranking the strongest moments across the full video", progress=max(job["progress"], 0.92),
             word_count=len(words))
        clip_shift = lo / 16000
        review_words = [dict(w, s=round(w["s"] - clip_shift, 2), e=round(w["e"] - clip_shift, 2))
                        for w in words if lo / 16000 <= w["s"] < hi / 16000]
        from . import posting
        guidance = posting.category_guidance(job.get("creator", "")) if job.get("creator") else ""
        desired_clips = int(o["max_clips"])
        review_limit = min(60, max(desired_clips, min(24, desired_clips * 2)))

        def ai_progress(message):
            notes.append(message)
            if message.startswith("AI reviewing "):
                _set(job, stage=message)

        found, ai_info = brain.find_clips(review_words, db, span, job["title"], job["uploader"],
                                          desired_clips, int(o["min_len"]), int(o["max_len"]), ai_progress,
                                          game=game, category_guidance=guidance,
                                          min_score=int(o.get("min_score", 80)), return_pool=review_limit)
        tokens += ai_info.get("tokens", 0)
        model = ai_info.get("model") or model
        if ai_info.get("note"):
            notes.append(ai_info["note"])
        if found and statics:
            # A commercial or a "be right back" slate is not a clip. Any clip that sits mostly inside one
            # is dropped.
            keep = []
            for c in found:
                a_s, b_s = c["start"] + clip_shift, c["end"] + clip_shift
                inside = quality.static_overlap(a_s, b_s, statics)
                if inside > 0.5 * max(1.0, b_s - a_s):
                    notes.append(f"A clip from {round(a_s)}s was left out: it sat in an ad break or a static screen.")
                else:
                    keep.append(c)
            found = keep
        # From this point forward candidates use the source video's clock. That is
        # required for frame extraction and for downloading candidate sections.
        for candidate in found:
            candidate["start"] = round(candidate["start"] + clip_shift, 2)
            candidate["end"] = round(candidate["end"] + clip_shift, 2)
        visual_note = ("No candidates cleared the transcript/audio quality threshold, so there were no frames to review."
                       if ai.current().get("vision_model") else
                       "Visual review is off; add an image-capable model in Settings → AI.")
        if found and ai.current().get("vision_model"):
            # Reuse the full download for regular videos. For long videos, fetch just
            # each candidate section and keep that section for rendering.
            visual_source = job.get("source_path") or ""
            if src["type"] == "url" and not pieces:
                try:
                    visual_source = str(whole_video() or "")
                except Exception as e:  # noqa: BLE001
                    notes.append(f"Visual review will fetch candidate sections: {safe.friendly(e, 160)}")
            if src["type"] == "url" and (pieces or not visual_source):
                for candidate in found[:24]:
                    _fetch_piece(job, candidate, wav)
                found = [c for c in found if c.get("status") != "error"]
            from . import visual
            vt, reviewed, visual_note = visual.review(found, visual_source, notes.append)
            tokens += vt
            if reviewed:
                found.sort(key=lambda c: -float(c.get("score", 0)))
                selected, categories = [], set()
                ranked = list(found)
                while ranked and len(selected) < desired_clips:
                    top_score = float(ranked[0].get("score", 0))
                    pick_i = next((i for i, c in enumerate(ranked)
                                   if str(c.get("category", "other")).lower() not in categories
                                   and top_score - float(c.get("score", 0)) <= 10), 0)
                    clip = ranked.pop(pick_i)
                    selected.append(clip)
                    categories.add(str(clip.get("category", "other")).lower())
                for c in found:
                    if c not in selected and c.get("src"):
                        Path(c["src"]).unlink(missing_ok=True)
                found = sorted(selected, key=lambda c: c["start"])
            notes.append(visual_note)
        for c in found:
            c.update(id=f"clip{len(job['clips']) + 1:02d}", start=round(c["start"], 2),
                     end=round(c["end"], 2), style=o["style"], layout=o["layout"],
                     captions=o.get("captions", True), status="queued", version=0)
            c["hashtags"] = posting._personal_hashtags(job, c)
            # Only the gameplay layout uses a webcam, so it is only looked for then, and only once: a
            # stream with no webcam is not looked for again on every clip of it.
            if o["layout"] in ("gameplay", "reaction", "stacked", "split", "bottomcam") and face is False:
                face = _facecam(job, require_visual=o["layout"] in ("stacked", "bottomcam"))
            if face:
                c["facecam"] = face
            job["clips"].append(c)
            futures.append((renders.submit(ctx.bind(make), c, list(words)), c))
        job["ai"] = {"model": model, "tokens": tokens, "note": "", "visual_review": visual_note, "log": notes[-5:]}
        save(job)
    _pause_checkpoint(job)
    if futures:
        _set(job, stage="Finishing the clips", progress=max(job["progress"], 0.92))
    _wait_for_clips(job, futures, began, renders._max_workers)
    _pause_checkpoint(job)
    wav.unlink(missing_ok=True)
    if exact:           # keep our own words for the clips in the saved transcript, so editing a clip later uses them
        for a_s, b_s, tw in exact.values():
            words = [w for w in words if not a_s <= w["s"] < b_s] + tw
        words.sort(key=lambda w: w["s"])
        (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
    renders.shutdown(wait=False)
    ahead.shutdown(wait=False)
    fetch.shutdown(wait=False)
    clips = job["clips"]
    note = ""
    if not model and not clips:
        problem = ai.problem
        note = (problem.get("text") if problem.get("config") == ai.config_signature()
                and time.time() - problem.get("at", 0) < 6 * 3600 else "")
        note = note or "The AI was unavailable, so no clips were created. Check AI settings and retry."
    elif not clips:
        note = next((n for n in reversed(notes) if "minimum quality score" in n), "")
    job["ai"] = {"model": model, "tokens": tokens, "log": notes[-5:], "note": note}
    stage = "Done" if clips else "Done, but nothing clip-worthy was found"
    _set(job, status="done", stage=stage, progress=1.0, took=round(time.time() - t0))
    try:
        from . import notices
        name = job.get("title") or job["source"].get("name") or "your video"
        notices.add(job.get("user"), "clips", f"{len(clips)} clip{'s' if len(clips) != 1 else ''} ready" if clips else "Nothing clip-worthy found",
                    name, f"/app/creators/{job['creator']}" if job.get("creator") else "/app/videos")
    except Exception:  # noqa: BLE001
        pass
    creators.log(job["creator"], f"Video finished with {len(clips)} clips in {round(time.time() - t0)}s: {job['title']}")


def _worker(quick_only=False):
    while True:
        kind, a, cid = _q.get(quick_only)
        if kind == "fn":
            try:
                a()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            continue
        job = _jobs.get(a)
        if not job:
            if kind == "job":
                _q.done()
            continue
        token = ctx._uid.set(job["user"])  # everything this job does runs as its owner
        try:
            if kind == "job":
                _run_job(job)
            else:
                clip = next(c for c in job["clips"] if c["id"] == cid)
                words = []
                if clip.get("captions", True):
                    words = json.loads((Path(job["dir"]) / "words.json").read_text(encoding="utf-8"))
                render_one(job, clip, words)
        except _PauseAtCheckpoint:
            pass  # _pause_checkpoint already saved the resumable state and stage
        except Exception as e:  # noqa: BLE001
            (Path(job["dir"]) / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            if kind == "job":
                for c in job["clips"]:
                    if c.get("status") in ("queued", "rendering"):
                        c.update(status="error", error="The video stopped before this clip was made.")
                _set(job, status="error", error=safe.friendly(e), stage="Stopped")
                creators.log(job["creator"], f"Video failed: {safe.friendly(e)[:200]}")
        finally:
            ctx._uid.reset(token)
            if kind == "job":
                _q.done()


def _pick_up_after_restart():
    """Videos and single clips that were being made when the server stopped: queued again, oldest first."""
    waiting = sorted((j for j in all_jobs() if j.get("status") == "queued" and j.get("resume")), key=lambda j: j["created"])
    for j in waiting:
        _q.put(("job", j["id"], None), user=j["user"])
    for j in all_jobs():
        for cid in j.pop("remake", []):
            _q.put(("clip", j["id"], cid))
        if j.get("status") == "done":
            save(j)


def start():
    load.start()
    load_all()
    _pick_up_after_restart()
    for _ in range(max(1, config.JOB_WORKERS)):
        threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_worker, args=(True,), daemon=True).start()   # re-making a single clip never waits behind whole videos
    threading.Thread(target=_live_worker, daemon=True).start()
