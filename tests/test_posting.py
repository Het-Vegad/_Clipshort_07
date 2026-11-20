"""A YouTube daily upload limit must be visible on posts added while the account is held."""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-posting-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

from studio import accounts, ctx, creators, jobs, posting  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


old_get, old_creator_get, old_job_get = accounts.get, creators.get, jobs.get
job_dir = Path(tempfile.mkdtemp(prefix="pc-posting-job-"))
accounts.get = lambda aid: {"id": aid, "platform": "youtube", "name": "Test channel"}
creators.get = lambda _cid: None
jobs.get = lambda _jid: None
try:
    with ctx.as_user("posttest"):
        creators_to_check = {
            "carryislive": "carryislive", "gamerfleet": "gamerfleet", "ishowspeed": "ishowspeed",
            "mythpat": "mythpat", "snax-gaming": "snaxgaming", "soul-regaltos": "soulregaltos",
            "techno-gamerz": "technogamerz",
        }
        check("every configured creator has a distinct playbook",
              all(posting.creator_playbook(cid) and "audience" in posting.creator_playbook(cid).lower()
                  for cid in creators_to_check))
        check("creator playbooks stay separate", "Speed" in posting.creator_playbook("ishowspeed")
              and "Speed" not in posting.creator_playbook("gamerfleet"))
        for cid, personal_tag in creators_to_check.items():
            tags = posting._personal_hashtags({"creator": cid, "title": "GTA 6 stream", "game": "Grand Theft Auto VI"},
                                              {"category": "funny", "hashtags": ["gta6", "viral"]})
            check(f"{cid} gets personal, game and moment tags", personal_tag in tags and "GTA6" in tags
                  and "funnygaming" in tags and len(tags) <= 8 and "shorts" not in [x.lower() for x in tags])
        check("clip tags keep relevant moment tags and drop generic viral bait",
              "funnygaming" in posting._personal_hashtags({"creator": "gamerfleet", "title": "GTA 6"},
                                                           {"category": "funny", "hashtags": ["viral"]})
              and "viral" not in posting._personal_hashtags({"creator": "gamerfleet", "title": "GTA 6"},
                                                             {"category": "funny", "hashtags": ["viral"]}))
        posting._posts[:] = []
        for creator_id, tag, category in (("gamerfleet", "gamerfleetshort", "funny"),
                                          ("mythpat", "mythpatonly", "story")):
            for i in range(2):
                posting._posts.append({"user": ctx.uid(), "creator": creator_id, "status": "posted",
                                       "platform": "youtube", "posted_at": time.time(), "category": category,
                                       "hashtags": [tag], "views": 1000,
                                       "snapshots": [{"views": 0}, {"views": 1000}], "daily_analytics": []})
        fleet_guidance = posting.category_guidance("gamerfleet").lower()
        mythpat_guidance = posting.category_guidance("mythpat").lower()
        check("10-day hashtag learnings stay with their creator",
              "#gamerfleetshort" in fleet_guidance and "#mythpatonly" not in fleet_guidance
              and "#mythpatonly" in mythpat_guidance and "#gamerfleetshort" not in mythpat_guidance)
        posting._posts.clear()
        retry_at = time.time() + 86400
        posting._posts.append({"id": "held", "user": ctx.uid(), "job": "old", "clip": "old-clip",
                               "creator": None, "account": "youtube-test", "account_name": "Test channel",
                               "platform": "youtube", "status": "scheduled", "due_at": time.time(),
                               "retry_at": retry_at,
                               "error": "YouTube's channel upload limit was reached. Retrying in about 24 hours."})
        queued = posting.enqueue({"id": "new-job", "creator": None, "user": ctx.uid(), "dir": str(job_dir),
                                  "source": {"type": "file", "name": "test"}, "clips": []},
                                 {"id": "new-clip", "score": 90},
                                 ["youtube-test"])
        check("new posts inherit the active YouTube retry time",
              len(queued) == 1 and queued[0].get("retry_at") == retry_at)
        check("new posts show why the YouTube queue is waiting",
              len(queued) == 1 and "upload limit" in queued[0].get("error", "").lower())
        posting._posts[:] = [{"id": str(i), "user": ctx.uid(), "job": "job", "clip": str(i), "creator": None,
                              "account": "youtube-test", "account_name": "Test channel", "platform": "youtube",
                              "status": "scheduled", "due_at": time.time() + i * 60}
                             for i in range(45)]
        first, second, last = posting.overview(0), posting.overview(1), posting.overview(99)
        check("the post table returns 20 rows per page",
              len(first["posts"]) == 20 and len(second["posts"]) == 20 and first["posts_total"] == 45)
        check("the next page advances without repeating rows",
              {p["id"] for p in first["posts"]}.isdisjoint(p["id"] for p in second["posts"]))
        check("the final page clamps to the remaining posts", len(last["posts"]) == 5 and last["posts_page"] == 2)
finally:
    accounts.get, creators.get, jobs.get = old_get, old_creator_get, old_job_get
    posting._posts.clear()
    import shutil
    shutil.rmtree(job_dir, ignore_errors=True)

print("ALL PASSED" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
