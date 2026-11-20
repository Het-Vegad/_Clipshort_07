"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import re
import secrets
import threading
import time
from pathlib import Path

import json

from . import accounts, alerts, config, creators, ctx, jobs, live

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
OPEN = ("scheduled", "posting")
PAUSED = "paused"
YOUTUBE_SCHEDULED = "youtube_scheduled"
CREATOR_HASHTAGS = {
    "gamerfleet": ("gamerfleet",), "ishowspeed": ("ishowspeed", "speed"),
    "mythpat": ("mythpat",), "carryislive": ("carryislive", "carryminati"),
    "carryminati": ("carryminati",), "snaxgaming": ("snaxgaming", "s8ul"),
    "soulregaltos": ("soulregaltos", "s8ul"), "technogamerz": ("technogamerz", "ujjwalgamer"),
}
CREATOR_PLAYBOOKS = {
    "gamerfleet": "GamerFleet audience: Hindi gaming storytelling, playful challenge setups and emotional/funny punchlines. Prefer an instantly clear setup followed by the payoff; keep his spoken Hindi/Hinglish when it carries the joke.",
    "ishowspeed": "IShowSpeed audience: global, personality-led first reactions and chaotic surprises. Favor a visible event plus an authentic reaction, start at the first reaction, and make the hook understandable in brief English even without context.",
    "mythpat": "Mythpat audience: Hindi comedy, character bits and compact story premises. Prefer a specific GTA situation that escalates into a clean punchline or twist; preserve a distinctive voice only when audible in the clip.",
    "carryislive": "CarryisLive audience: Hindi/Indian gaming, hype, banter and funny first-play reactions. Favor the cause of a reaction plus its payoff; use chat/donation context only when the clip itself explains it.",
    "snaxgaming": "Snax Gaming audience: Indian gaming and S8UL fans. Favor a legible clutch, fail, surprising play or celebration with the decisive gameplay visible; keep the Hindi reaction tied to what caused it.",
    "soulregaltos": "SOUL Regaltos audience: competitive gaming fans. Favor skill, clutch pressure, a decisive play or an unexpected reversal; show enough gameplay to understand the result and keep reaction audio as context.",
    "technogamerz": "Techno Gamerz audience: Hindi gaming stories, missions and challenge-style progress. Favor a clear objective, escalation and reveal/payoff; explain the game event with the clip itself and keep the title specific rather than generic.",
}
MOMENT_HASHTAGS = {
    "funny": "funnygaming", "embarrassing": "popat", "fail": "gamingfail",
    "roast": "gamerroast", "rage": "gamerage", "clutch": "clutchmoment",
    "shock": "gamerreaction", "surprise": "unexpectedmoment", "emotional": "gamerreaction",
    "wholesome": "wholesomegaming", "hot_take": "gamingopinion", "story": "gamingstory",
    "skill": "gaminghighlights", "twist": "plottwist",
}
GENERIC_TAGS = {"viral", "viralvideo", "viralshorts", "trending", "trendingshorts", "fyp", "shortsfeed", "shortvideo", "youtubeshorts"}
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]


def _personal_hashtags(job, clip):
    """Blend channel, creator, moment, and clip-specific hashtags, without unrelated tags."""
    creator = re.sub(r"[^a-z0-9]", "", str(job.get("creator") or "").lower())
    context = " ".join((str(job.get("title") or ""), str(job.get("game") or ""),
                        str((job.get("source") or {}).get("name") or ""))).lower()
    gta = any(x in context for x in ("gta", "grand theft auto", "vice city"))
    tags = list(CREATOR_HASHTAGS.get(creator, ()))
    tags.extend(("GTA6", "GTAVI") if gta else ("gaming",))
    moment = MOMENT_HASHTAGS.get(str(clip.get("category") or "").lower())
    if moment:
        tags.append(moment)
    for raw in clip.get("hashtags", []) if isinstance(clip.get("hashtags"), list) else []:
        tag = re.sub(r"[^a-zA-Z0-9_]", "", str(raw or "").lstrip("#"))[:30]
        if tag and tag.lower() not in GENERIC_TAGS and tag.lower() != "shorts":
            tags.append(tag)
    # Keep one broad topic tag, but leave room for the actual clip and its moment type.
    tags.append("gaming")
    clean, seen = [], set()
    for tag in tags:
        tag = re.sub(r"[^a-zA-Z0-9_]", "", str(tag or ""))[:30]
        if tag and tag.lower() not in seen:
            clean.append(tag)
            seen.add(tag.lower())
        if len(clean) >= 8:
            break
    return clean


def creator_playbook(creator_id: str = "") -> str:
    """Audience and style cues researched for the configured creators, if any."""
    key = re.sub(r"[^a-z0-9]", "", str(creator_id or "").lower())
    return CREATOR_PLAYBOOKS.get(key, "")


def _personalize_pending():
    """Refresh creator and moment hashtags on clips that are already queued."""
    by_job = {}
    posts_changed = False
    for post in _posts:
        if post.get("status") in ("scheduled", PAUSED) and post.get("user"):
            by_job.setdefault((post["user"], post["job"]), []).append(post)
    for (uid, jid), rows in by_job.items():
        with ctx.as_user(uid):
            job = jobs.get(jid)
            if not job:
                continue
            changed = False
            for post in rows:
                clip = next((c for c in job.get("clips", []) if c.get("id") == post.get("clip")), None)
                if clip:
                    tags = _personal_hashtags(job, clip)
                    if tags != clip.get("hashtags", []):
                        clip["hashtags"] = tags
                        changed = True
                    if tags != post.get("hashtags", []):
                        post["hashtags"] = tags
                        posts_changed = True
            if changed:
                jobs.save(job)
    if posts_changed:
        _save()


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def forget(uid: str):
    """Drop everything queued or recorded for a deleted account, so nothing is posted or retried for it any more."""
    with _lock:
        for p in _posts:
            if p.get("user") == uid:
                p["status"] = "deleted"          # the posting loop may be holding this one already: it then skips it
        _posts[:] = [p for p in _posts if p.get("user") != uid]
        _save()


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error")}
        jobs.save(job)


def _next_slot(account_id, gap_min, cap):
    """Earliest time that respects the gap between posts and the daily cap for this account."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted", YOUTUBE_SCHEDULED)]
    t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + gap_min * 60)
    for _ in range(30):
        if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) < cap:
            break
        lt = time.localtime(t + 86400)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))  # 9:00 next day
    return t


def _youtube_upload_limit_hold(user_id, account_id, now=None):
    """An active YouTube upload-limit delay shared by every pending post for this account."""
    now = time.time() if now is None else now
    return max((p for p in _posts if p.get("user") == user_id and p.get("account") == account_id
                and p.get("platform") == "youtube" and p.get("status") == "scheduled"
                and p.get("retry_at", 0) > now and "upload limit" in str(p.get("error", "")).lower()),
               key=lambda p: p.get("retry_at", 0), default=None)


def enqueue(job, clip, account_ids, now=False) -> list:
    tags = _personal_hashtags(job, clip)
    if tags != clip.get("hashtags", []):
        clip["hashtags"] = tags
        jobs.save(job)
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            if any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
                   and p["status"] in OPEN + ("posted", YOUTUBE_SCHEDULED) for p in _mine()):
                continue  # never post the same clip to the same account twice
            due = time.time() if now else _next_slot(aid, c["post_gap_min"], c["daily_cap"])
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": due, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                 "score": clip.get("score", 0), "category": clip.get("category", "other"),
                 "hashtags": list(clip.get("hashtags", []))[:8],
                 "tries": 0, "url": "", "remote_id": "", "error": None, "views": None,
                 "likes": None, "comments": None, "snapshots": [], "daily_analytics": []}
            hold = _youtube_upload_limit_hold(p["user"], aid) if acc["platform"] == "youtube" else None
            if hold:
                p.update(retry_at=hold["retry_at"], error=hold["error"])
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    return made


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c or not c["autopost"] or not c["accounts"]:
        return
    if clip.get("score", 0) < c["post_min_score"]:
        return  # includes hand-made clips (score 0): those wait for you to press Post
    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _caption(clip, creator):
    tags = " ".join("#" + h for h in clip.get("hashtags", []))
    return "\n\n".join(x for x in (clip.get("description", ""), tags, creator.get("post_footer", "")) if x)


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        tags = _personal_hashtags(job, clip)
        if tags != clip.get("hashtags", []):
            clip["hashtags"] = tags
            jobs.save(job)
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator)
        if acc["platform"] == "youtube":
            # Upload only when this queue slot is due; do not create remote YouTube schedules.
            rid, url = accounts.youtube_upload(acc, path, title, cap + "\n\n#Shorts", clip.get("hashtags", []), creator["privacy"])
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        if acc and acc.get("platform") == "youtube" and ("uploadLimitExceeded" in str(e) or "exceeded the number of videos" in str(e).lower()):
            # This is YouTube's channel upload limit, not a transient network failure. Keep the
            # requested publish slots; only delay the next upload attempt for about a day.
            retry_at = time.time() + 86400
            with _lock:
                for queued in _posts:
                    if (queued.get("user") == p.get("user") and queued.get("account") == p.get("account")
                            and queued.get("platform") == "youtube" and queued.get("status") in ("scheduled", "posting")):
                        queued.update(status="scheduled", retry_at=retry_at, tries=0,
                                      error="YouTube's channel upload limit was reached. Retrying in about 24 hours.")
            creators.log(p["creator"], "YouTube upload limit reached. The next upload attempt will be in about 24 hours; publish slots stay based on the queue schedule.")
            _save()
            _mirror(p)
            return
        p["tries"] += 1
        p["error"] = str(e)[-300:]
        if p["tries"] < 3 and clip and acc:
            p.update(status="scheduled", retry_at=time.time() + 600)  # try again in 10 minutes
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    if p.get("status") in ("posted", YOUTUBE_SCHEDULED):
        _delete_local_clip_when_fully_posted(p)
    _save()
    _mirror(p)


def _delete_local_clip_when_fully_posted(post):
    """Delete only the rendered MP4, and only after every queued destination has accepted it."""
    related = [x for x in _mine() if x.get("job") == post.get("job") and x.get("clip") == post.get("clip")]
    accepted = ("posted", YOUTUBE_SCHEDULED)
    if not related or any(x.get("status") not in accepted for x in related):
        return
    job = jobs.get(post.get("job"))
    clip = next((c for c in (job or {}).get("clips", []) if c.get("id") == post.get("clip")), None)
    if not job or not clip:
        return
    creator = creators.get(job.get("creator")) or creators.DEFAULTS
    if not creator.get("delete_after_post", True):
        return
    try:
        folder = (Path(job["dir"]) / "clips").resolve()
        name = str(clip.get("file") or "")
        path = (folder / name).resolve()
        if path.parent != folder or path.suffix.lower() != ".mp4" or not path.is_file():
            return
        path.unlink()
        clip["local_deleted"] = True
        jobs.save(job)
        if job.get("creator"):
            creators.log(job["creator"], f"Removed local clip file after all uploads succeeded: {clip.get('title') or clip.get('hook') or clip.get('id')}")
    except OSError as e:
        if job.get("creator"):
            creators.log(job["creator"], f"Could not remove the local clip file: {str(e)[:160]}")


def _reflow_overdue(now):
    """Keep a local queue from dumping missed posts all at once after a shutdown."""
    groups = {}
    with _lock:
        for p in _posts:
            if p.get("status") == "scheduled" and p.get("user") and p.get("retry_at", 0) <= now:
                groups.setdefault((p["user"], p["account"]), []).append(p)
    for (uid, aid), pending in groups.items():
        if not any(p.get("due_at", now + 1) <= now for p in pending):
            continue
        with ctx.as_user(uid):
            creator = creators.get(pending[0].get("creator")) or creators.DEFAULTS
            gap = max(5, int(creator.get("post_gap_min", 10))) * 60
            cap = max(1, int(creator.get("daily_cap", 6)))
            with _lock:
                mine = [p for p in _posts if p.get("user") == uid and p.get("account") == aid]
                fixed = [p for p in mine if p.get("status") in ("posted", YOUTUBE_SCHEDULED)]
                last = max((p.get("posted_at") or p.get("due_at", 0) for p in fixed), default=now - gap)
                cursor = max(now, last + gap)
                counts = {}
                for p in fixed:
                    stamp = p.get("posted_at") or p.get("due_at", 0)
                    counts[_day(stamp)] = counts.get(_day(stamp), 0) + 1
                rows = sorted((p for p in mine if p.get("status") == "scheduled" and p.get("retry_at", 0) <= now),
                              key=lambda p: p.get("due_at", 0))
                changed = []
                for p in rows:
                    candidate = cursor
                    while counts.get(_day(candidate), 0) >= cap:
                        lt = time.localtime(candidate + 86400)
                        candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))
                    if abs(candidate - p.get("due_at", candidate)) > 1:
                        p["due_at"] = candidate
                        changed.append(p)
                    counts[_day(candidate)] = counts.get(_day(candidate), 0) + 1
                    cursor = candidate + gap
                if changed:
                    _save()
            for p in changed:
                _mirror(p)


def _run():
    while True:
        time.sleep(15)
        now = time.time()
        # Legacy YouTube-scheduled items are already uploaded remotely; record them when due.
        for p in list(_posts):
            if p.get("status") == YOUTUBE_SCHEDULED and p.get("due_at", now + 1) <= now:
                with ctx.as_user(p["user"]):
                    with _lock:
                        if p.get("status") == YOUTUBE_SCHEDULED:
                            p.update(status="posted", posted_at=p.get("due_at", now))
                            _save()
                    _mirror(p)
        _reflow_overdue(now)
        due = []
        for p in list(_posts):
            if p.get("status") != "scheduled" or not p.get("user") or p.get("retry_at", 0) > now:
                continue
            if p.get("due_at", 0) > now:
                continue
            due.append(p)
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        hold = _youtube_upload_limit_hold(p["user"], p["account"], now) if p.get("platform") == "youtube" else None
                        if hold:
                            p.update(retry_at=hold["retry_at"], error=hold["error"])
                            _save()
                            held = True
                        else:
                            held = False
                        if held:
                            pass
                        else:
                            p["status"] = "posting"
                            p.pop("retry_at", None)
                    if held:
                        _mirror(p)
                        continue
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            stats = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in stats:
                    counts = stats[p["remote_id"]]
                    p.update(views=counts["views"], likes=counts["likes"], comments=counts["comments"])
                    day = _day(time.time())
                    snapshots = p.setdefault("snapshots", [])
                    sample = {"at": time.time(), "day": day, **counts}
                    if snapshots and snapshots[-1].get("day") == day:
                        snapshots[-1] = sample
                    else:
                        snapshots.append(sample)
                    p["snapshots"] = snapshots[-10:]
                    # Analytics reports lag behind live counts; retry transient/setup failures hourly, otherwise daily.
                    checked = p.get("analytics_checked_at", 0)
                    retry_after = 3600 if p.get("analytics_error") else 86400
                    if time.time() - p.get("posted_at", time.time()) > 86400 and time.time() - checked > retry_after:
                        yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
                        start = time.strftime("%Y-%m-%d", time.gmtime(p.get("posted_at", time.time())))
                        try:
                            p["daily_analytics"] = accounts.youtube_analytics(acc, p["remote_id"], start, yesterday)[-10:]
                            p["analytics_checked_at"] = time.time()
                            p.pop("analytics_error", None)
                        except Exception as e:  # noqa: BLE001; counts remain available if Analytics API is disabled
                            p["analytics_checked_at"] = time.time()
                            p["analytics_error"] = str(e)[-200:]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(300)


def category_guidance(creator_id: str = "") -> str:
    """Creator-specific style plus only that creator's recent YouTube performance."""
    playbook = creator_playbook(creator_id)
    cutoff = time.time() - 10 * 86400
    groups, tag_groups = {}, {}
    for p in _mine():
        if (p.get("status") != "posted" or p.get("platform") != "youtube" or p.get("posted_at", 0) < cutoff
                or (creator_id and p.get("creator") != creator_id)):
            continue
        cat = p.get("category") or "other"
        g = groups.setdefault(cat, {"n": 0, "views": [], "retention": []})
        g["n"] += 1
        snaps = p.get("snapshots", [])
        if len(snaps) > 1:
            g["views"].append(max(0, int(snaps[-1].get("views", 0)) - int(snaps[0].get("views", 0))))
        g["retention"] += [float(d["avg_view_percent"]) for d in p.get("daily_analytics", []) if d.get("avg_view_percent")]
        post_tags = p.get("hashtags") if isinstance(p.get("hashtags"), list) else []
        if not post_tags:
            source_job = jobs.get(p.get("job"))
            source_clip = next((c for c in (source_job or {}).get("clips", []) if c.get("id") == p.get("clip")), None)
            post_tags = (source_clip or {}).get("hashtags", [])
        for tag in post_tags:
            clean = re.sub(r"[^a-z0-9]", "", str(tag or "").lower())
            if clean:
                stat = tag_groups.setdefault(clean, {"n": 0, "views": []})
                stat["n"] += 1
                if snaps := p.get("snapshots", []):
                    delta = (int(snaps[-1].get("views", 0)) - int(snaps[0].get("views", 0))) if len(snaps) > 1 else int(snaps[-1].get("views", 0))
                    stat["views"].append(max(0, delta))
    ranked = sorted(((cat, vals) for cat, vals in groups.items() if vals["n"] >= 2),
                    key=lambda item: (sum(item[1]["retention"]) / len(item[1]["retention"]) if item[1]["retention"] else 0,
                                      sum(item[1]["views"]) / len(item[1]["views"]) if item[1]["views"] else 0), reverse=True)
    parts = [playbook] if playbook else []
    if ranked:
        parts.append("10-day creator results: stronger recent categories were "
                     + ", ".join(f"{cat} ({vals['n']} posts)" for cat, vals in ranked[:3])
                     + ". Use as a mild tie-breaker and keep variety; select only when the footage supports it.")
    else:
        parts.append("Not enough 10-day results for this creator yet; keep testing different real moment types.")
    tag_ranked = sorted((tag, v) for tag, v in tag_groups.items() if v["n"] >= 2 and v["views"])
    tag_ranked.sort(key=lambda item: sum(item[1]["views"]) / len(item[1]["views"]), reverse=True)
    if tag_ranked:
        parts.append("Hashtags on this creator's better recent posts: " + ", ".join(
            f"#{tag} ({vals['n']} posts)" for tag, vals in tag_ranked[:4])
            + ". Reuse only tags that accurately describe this game and moment.")
    return "\n".join(parts)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("scheduled", PAUSED):
            raise ValueError("Only posts that have not started uploading can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def pause(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p.get("status") != "scheduled":
            raise ValueError("Only posts that have not started uploading can be paused.")
        p.update(status=PAUSED, paused_at=time.time())
        _save()
    _mirror(p)


def resume(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p.get("status") != PAUSED:
            raise ValueError("That post is not paused.")
        creator = creators.get(p.get("creator")) or creators.DEFAULTS
        p.update(status="scheduled", due_at=_next_slot(p["account"], creator["post_gap_min"], creator["daily_cap"]))
        p.pop("paused_at", None)
        _save()
    _mirror(p)


def reschedule_all() -> int:
    """Give every local queued post a fresh slot; YouTube uploads are then scheduled by the worker."""
    now = time.time()
    groups = {}
    with _lock:
        for p in _posts:
            if p.get("status") == "scheduled" and p.get("user"):
                groups.setdefault((p["user"], p["account"]), []).append(p)
    changed = []
    for (uid, aid), rows in groups.items():
        with ctx.as_user(uid):
            rows.sort(key=lambda p: p.get("due_at", now))
            fixed = [p for p in _posts if p.get("user") == uid and p.get("account") == aid
                     and p.get("status") in ("posted", YOUTUBE_SCHEDULED)]
            last = max((p.get("posted_at") or p.get("due_at", 0) for p in fixed), default=now)
            first_creator = creators.get(rows[0].get("creator")) or creators.DEFAULTS
            first_gap = max(5, int(first_creator.get("post_gap_min", 10))) * 60
            cursor = max(now, last + first_gap)
            counts = {}
            for p in fixed:
                stamp = p.get("posted_at") or p.get("due_at", 0)
                counts[_day(stamp)] = counts.get(_day(stamp), 0) + 1
            with _lock:
                for p in rows:
                    if p.get("status") != "scheduled":
                        continue
                    creator = creators.get(p.get("creator")) or creators.DEFAULTS
                    gap = max(5, int(creator.get("post_gap_min", 10))) * 60
                    cap = max(1, int(creator.get("daily_cap", 6)))
                    candidate = cursor
                    while counts.get(_day(candidate), 0) >= cap:
                        lt = time.localtime(candidate + 86400)
                        candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))
                    p["due_at"] = candidate
                    counts[_day(candidate)] = counts.get(_day(candidate), 0) + 1
                    cursor = candidate + gap
                    changed.append(p)
                _save()
    for p in changed:
        with ctx.as_user(p["user"]):
            _mirror(p)
    return len(changed)


def shuffle_pending() -> int:
    """Randomize only this user's local pending order, then assign safe spaced publish slots."""
    uid, now = ctx.uid(), time.time()
    groups = {}
    with _lock:
        for p in _posts:
            if p.get("user") == uid and p.get("status") == "scheduled":
                groups.setdefault(p.get("account"), []).append(p)
    changed = []
    for aid, rows in groups.items():
        if len(rows) < 2:
            continue
        secrets.SystemRandom().shuffle(rows)
        fixed = [p for p in _mine() if p.get("account") == aid and p.get("status") in ("posted", YOUTUBE_SCHEDULED, "posting")]
        last = max((p.get("posted_at") or p.get("due_at", 0) for p in fixed), default=now)
        first_creator = creators.get(rows[0].get("creator")) or creators.DEFAULTS
        cursor = max(now, last + max(5, int(first_creator.get("post_gap_min", 10))) * 60)
        counts = {}
        for p in fixed:
            stamp = p.get("posted_at") or p.get("due_at", 0)
            counts[_day(stamp)] = counts.get(_day(stamp), 0) + 1
        with _lock:
            for p in rows:
                if p.get("status") != "scheduled":
                    continue
                creator = creators.get(p.get("creator")) or creators.DEFAULTS
                gap = max(5, int(creator.get("post_gap_min", 10))) * 60
                cap = max(1, int(creator.get("daily_cap", 6)))
                candidate = cursor
                while counts.get(_day(candidate), 0) >= cap:
                    lt = time.localtime(candidate + 86400)
                    candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))
                p["due_at"] = candidate
                counts[_day(candidate)] = counts.get(_day(candidate), 0) + 1
                cursor = candidate + gap
                changed.append(p)
            _save()
    for p in changed:
        _mirror(p)
    return len(changed)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
        _save()
    _mirror(p)


POSTS_PER_PAGE = 20


def overview(posts_page=0) -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN or p["status"] == YOUTUBE_SCHEDULED), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN or p["status"] in (PAUSED, YOUTUBE_SCHEDULED)), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "failed", "cancelled")),
                  key=lambda p: -(p.get("posted_at") or p["due_at"]))
    all_posts = queue + done
    posts_total = len(all_posts)
    pages = max(1, (posts_total + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
    posts_page = max(0, min(int(posts_page), pages - 1))
    start = posts_page * POSTS_PER_PAGE
    page_rows = all_posts[start:start + POSTS_PER_PAGE]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in page_rows]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    ten_day = [p for p in _mine() if p.get("status") == "posted" and p.get("platform") == "youtube"
               and time.time() - p.get("posted_at", time.time()) < 10 * 86400]
    by_category = {}
    for p in ten_day:
        cat = p.get("category") or "other"
        row = by_category.setdefault(cat, {"category": cat, "posts": 0, "views": 0, "likes": 0,
                                           "watch_seconds": 0, "view_percent": [], "latest_views": []})
        row["posts"] += 1
        row["views"] += int(p.get("views") or 0)
        row["likes"] += int(p.get("likes") or 0)
        for day in p.get("daily_analytics", []):
            row["watch_seconds"] += float(day.get("watch_minutes") or 0) * 60
            if day.get("avg_view_percent"):
                row["view_percent"].append(float(day["avg_view_percent"]))
        snaps = p.get("snapshots", [])
        if len(snaps) > 1:
            row["latest_views"].append(max(0, int(snaps[-1].get("views", 0)) - int(snaps[0].get("views", 0))))
    for row in by_category.values():
        row["avg_view_percent"] = round(sum(row["view_percent"]) / len(row["view_percent"]), 1) if row["view_percent"] else None
        row["avg_recent_views"] = round(sum(row["latest_views"]) / len(row["latest_views"])) if row["latest_views"] else None
        row.pop("view_percent", None); row.pop("latest_views", None)
    recent_yt = [p for p in _posts if p.get("status") == "posted" and p.get("platform") == "youtube"
                 and time.time() - p.get("posted_at", 0) < 30 * 86400]
    last_analytics_error = max((p.get("analytics_checked_at", 0) for p in recent_yt if p.get("analytics_error")), default=0)
    last_analytics_ok = max((p.get("analytics_checked_at", 0) for p in recent_yt if not p.get("analytics_error") and p.get("analytics_checked_at")), default=0)
    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "posts_total": posts_total,
            "posts_page": posts_page, "posts_per_page": POSTS_PER_PAGE,
            "local_pending": sum(1 for p in queue if p["status"] == "scheduled"), "daily": daily,
            "performance": sorted(by_category.values(), key=lambda x: (x["avg_view_percent"] or 0, x["avg_recent_views"] or x["views"]), reverse=True),
            "analytics_setup_needed": bool(last_analytics_error and last_analytics_error >= last_analytics_ok),
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["PeakCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    _personalize_pending()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
