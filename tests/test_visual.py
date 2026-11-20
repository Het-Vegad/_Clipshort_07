"""Three sampled frames are reviewed and the visual score affects candidate ranking."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-visual-")
sys.path.insert(0, ".")
from studio import ai, visual

work = Path(tempfile.mkdtemp(prefix="pc-visual-work-"))
fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


try:
    video = work / "test.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=640x360:r=12:d=4", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=4", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(video)], check=True)
    candidate = {"start": 0.2, "end": 3.8, "score": 80, "category": "funny", "title": "A test moment"}
    model = ai.current().get("vision_model")
    with patch.object(ai, "current", return_value={"vision_model": "test-vision"}), \
         patch.object(ai, "see", return_value=(json.dumps({"items": [{"id": "C1", "score": 100,
                                                                       "reason": "A clear visible payoff."}]}), 12, 4)):
        tokens, reviewed, note = visual.review([candidate], str(video))
    check("three frames form a visual contact sheet and get reviewed", reviewed == 1 and "three sampled frames" in note)
    check("visual score contributes to the candidate score", candidate.get("visual_score") == 100 and candidate["score"] == 86)
    check("vision token use is returned", tokens == 16)
    with patch.object(ai, "current", return_value={"vision_model": ""}):
        _, reviewed, note = visual.review([candidate], str(video))
    check("missing vision model falls back cleanly", reviewed == 0 and "off" in note)
finally:
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
