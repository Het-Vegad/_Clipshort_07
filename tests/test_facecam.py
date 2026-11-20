"""Where the webcam is in a gaming stream.

The webcam is found by the one thing a webcam always has and a game almost never has: a straight
border that stays in the same place in every frame.

Runs against the code directly, with its own empty data folder."""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-facecam-")
sys.path.insert(0, ".")
from studio import facecam, render
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

work = tempfile.mkdtemp(prefix="pc-facecam-work-")
FRAMES = "100"          # four seconds at 25 frames. Noise does not compress, so the picture stays small.
NOISE = "nullsrc=s=1280x720:r=25,noise=alls=24:allf=t"


def make(name, extra, where="0:0"):
    """A video with a moving background and one still box in it.

    The background is noise, which never repeats, so the only straight line that stays in the
    same place is the edge of the box."""
    path = os.path.join(work, name)
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", NOISE,
                    "-f", "lavfi", "-i", extra,
                    "-filter_complex", f"[0:v][1:v]overlay={where}[v]", "-map", "[v]",
                    "-frames:v", FRAMES,
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path], check=True)
    return path


try:
    # ---- a webcam in the top-left: a still, lit box
    cam = make("cam.mp4", "color=c=0xC08050:size=300x170:rate=25")
    box = facecam.detect(cam)
    check("a webcam in the top-left is found", (box or {}).get("corner"), "top-left")
    # the border lies between two pixels, so the box is a pixel short of the drawn one
    check("its size is right", abs((box or {}).get("w", 0) - 300) <= 1, True)
    check("its height is right", abs((box or {}).get("h", 0) - 170) <= 1, True)
    check("it is sure of it", (box or {}).get("conf"), "high")

    # ---- the same box in another corner
    cam_br = make("cam_br.mp4", "color=c=0xC08050:size=300x170:rate=25", where="980:500")
    box = facecam.detect(cam_br)
    check("a webcam in the bottom-right is found too", (box or {}).get("corner"), "bottom-right")
    check("its position is right", abs((box or {}).get("x", 0) - 980) <= 1, True)

    # ---- a video with no webcam in it: nothing that stays still, so nothing is found
    plain = os.path.join(work, "plain.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", NOISE, "-frames:v", FRAMES,
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", plain], check=True)
    check("a video with nothing still in it finds no webcam", (facecam.detect(plain) or {}).get("conf", "none"), "none")

    # ---- a file that cannot be read must not stop a job
    check("a file that cannot be read gives nothing, not an error",
          facecam.detect(os.path.join(work, "nope.mp4")), None)

    # ---- the job hands the webcam on to the render
    from studio import jobs
    from studio import creators, ctx
    job = {"source_path": cam}
    box = jobs._facecam(job)
    check("the job finds the webcam for its clips", bool(box), True)
    check("the job keeps it on the job", "facecam" in job, True)
    check("the box is the right size", abs(box[2] - 300) <= 1 and abs(box[3] - 170) <= 1, True)
    check("a job with no video finds no webcam", jobs._facecam({"source_path": os.path.join(work, "nope.mp4")}), None)

    with ctx.as_user("u_local"):
        profile = creators.create({"name": "Facecam profile test"})
        creators.update(profile["id"], {"facecam": [0.79, 0.11, 0.20, 0.22]})
        profiled_job = {"creator": profile["id"], "width": 1920, "height": 1080}
        check("a creator's saved facecam position is reused on new uploads",
              jobs._facecam(profiled_job), [1517, 119, 384, 238])

    # ---- a webcam that is square and runs along one edge, not into a corner
    sq = make("cam_sq.mp4", "color=c=0xC08050:size=360x355:rate=25", where="920:165")
    box = facecam.detect(sq)
    check("a square webcam along an edge is found", (box or {}).get("corner"), "right")
    check("it starts where the picture starts", abs((box or {}).get("x", 0) - 920) <= 3, True)
    check("it is as tall as the picture", abs((box or {}).get("h", 0) - 355) <= 6, True)

    # ---- one frame goes to the AI, and its answer is measured again before it is used
    def say(looked, box=None):
        return lambda jpeg: {"looked": looked, "box": box}

    box = facecam.detect(sq, ask=say(True, (0.71, 0.22, 0.29, 0.44)))      # rough, as a model gives it
    check("the box the AI suggests is put onto the borders", abs((box or {}).get("x", 0) - 920) <= 3, True)
    check("a rough box is corrected to the picture's real size", abs((box or {}).get("h", 0) - 355) <= 6, True)
    check("it is still sure of it", (box or {}).get("conf"), "high")
    check("the AI saying there is no webcam is believed", facecam.detect(sq, ask=say(True, None)), None)
    box = facecam.detect(sq, ask=say(True, (0.02, 0.02, 0.06, 0.06)))      # a box on nothing at all
    check("a box that lands on nothing is thrown away and the search carries on",
          abs((box or {}).get("x", 0) - 920) <= 3, True)
    box = facecam.detect(sq, ask=say(False))                              # a model that only reads text
    check("a model that cannot look at pictures changes nothing", abs((box or {}).get("x", 0) - 920) <= 3, True)

    # ---- what the model's one line is turned into
    from unittest.mock import patch
    from studio import brain, ai as _ai
    with patch.object(_ai, "see", return_value=("CAME x=71% y=20% w=29% h=38%", 0, 0)):
        got = brain.look_for_facecam(b"jpeg")
    check("the box in the model's answer is read", [round(v, 2) for v in (got["box"] or ())],
          [0.71, 0.20, 0.29, 0.38])
    with patch.object(_ai, "see", return_value=("NO CAM", 0, 0)):
        check("'no cam' is read as no webcam", brain.look_for_facecam(b"jpeg"), {"looked": True, "box": None})
    with patch.object(_ai, "see", return_value=("I am a text model.", 0, 0)):
        check("an answer that is neither is not believed", brain.look_for_facecam(b"jpeg")["looked"], False)

    # ---- the layout: the game fills the screen and the webcam sits in the corner
    f = render._filter("gameplay", 1920, [0, 0, 300, 170])
    check("the game fills the screen", "scale=1080:1920" in f and "crop=1080:1920" in f, True)
    check("the webcam is cropped out of the source", "crop=300:170:0:0" in f, True)
    check("the webcam is put in the corner", "overlay=" in f, True)
    check("without a webcam the game simply fills the screen",
          "scale=1080:1920" in render._filter("gameplay", 1920, None) and "overlay=" not in render._filter("gameplay", 1920, None), True)
    check("the layout is offered to the user", "gameplay" in render.LAYOUTS, True)
    reaction = render._filter("reaction", 1920, [0, 0, 300, 170])
    check("reaction layout uses the bigger facecam", "scale=480:272" in reaction, True)
    check("reaction layout has a bright frame", "drawbox=" in reaction, True)
    check("reaction layout is offered to the user", "reaction" in render.LAYOUTS, True)
    stacked = render._filter("stacked", 1920, [0, 0, 300, 170])
    check("facecam-above puts the camera before gameplay", "[f][g]vstack" in stacked, True)
    check("facecam-above fills the gameplay panel with a crop that excludes its corner camera",
          "crop=948:1080:486:0,scale=1080:1230" in stacked, True)
    bottomcam = render._filter("bottomcam", 1920, [0, 0, 300, 170])
    check("facecam-below puts gameplay before the camera", "[g][f]vstack" in bottomcam, True)
    check("facecam-below fills the gameplay panel with a crop that excludes its corner camera",
          "crop=948:1080:486:0,scale=1080:1230" in bottomcam, True)
    gx, gy, gw, gh = render._game_panel_crop(1920, 1080, 1080, 1230, [1518, 122, 378, 236])
    check("the game zoom crop stays clear of the upper-right webcam", gx + gw <= 1518 or gx >= 1896, True)
    check("facecam-above title sits over the gameplay picture", render._hook_y("stacked", 180) > int(render.H * 0.36), True)
    check("facecam-below title sits inside the gameplay panel", render._hook_y("bottomcam", 250) < int(render.H * 0.64), True)
    above_geom = render.geometry(1920, 1080, "stacked")
    below_geom = render.geometry(1920, 1080, "bottomcam")
    check("facecam-above captions sit at the facecam/gameplay seam", above_geom[3], int(render.H * 0.36) // 2 * 2)
    check("facecam-below captions sit near the top", below_geom[3] < 300, True)
    check("facecam-above removes the large hook banner", render._layout_hook("stacked", "BIG HOOK"), "")
    check("facecam-below removes the large hook banner", render._layout_hook("bottomcam", "BIG HOOK"), "")
    check("other layouts keep their hook banner", render._layout_hook("gameplay", "BIG HOOK"), "BIG HOOK")
    try:
        render.render_clip(Path(cam), Path(work), "no_cam", {"start": 0, "end": 2, "layout": "stacked"}, [], 1280, 720)
        no_cam_error = False
    except ValueError as e:
        no_cam_error = "needs a detected facecam" in str(e)
    check("a requested stacked layout does not silently become a plain crop", no_cam_error, True)
    check("impact captions are offered", "impact" in render.STYLES, True)
    # the heading must not run under the webcam picture
    pip_h = 380 * 376 / 363
    check("the heading goes below the webcam picture", render._hook_y("gameplay", 300, [0, 0, 363, 376]) > 26 + pip_h, True)
    check("another picture moves the heading with it",
          render._hook_y("gameplay", 300, [0, 0, 300, 170]) < render._hook_y("gameplay", 300, [0, 0, 363, 376]), True)
    check("without a webcam the heading stays at the top", render._hook_y("crop", 300, None), 300)
finally:
    shutil.rmtree(work, ignore_errors=True)      # the pictures are big; they must not be left behind

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
