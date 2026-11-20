"""The line under load: holds while the server is near full, moves the moment it is not. Usage: test_load_queue.py <root>"""
import os
import sys
import tempfile
import threading
import time

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, sys.argv[1])
from studio import config, jobs, load  # noqa: E402

load.MIN_FREE_RAM_GB = 0.0  # this checks the processor; how much memory the test machine has free is not the point
config.JOB_WORKERS = 8      # the ceiling on videos at once; the test is about load, not about how many cores this machine has

fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"   {extra}" if extra else ""))
    if not ok:
        fails.append(name)


# ---- the monitor measures something real
load.start()
time.sleep(2.5)
idle = load.state()
check("monitor reads the processor", 0 <= idle["cpu"] <= 100, str(idle))
check("monitor reads free memory and disk", idle["ram_free_gb"] > 0.1 and idle["disk_free_gb"] > 0.1)


if __name__ == "__main__":
    # The host may expose more CPUs than this process can use (containers and CI runners often do),
    # so a fixed 80% system-wide stress threshold is not a portable way to test the busy gate.
    # The monitor is checked against real system readings above; check the gate with controlled values.
    load._state["ram_free"] = 2.0
    load._state["cpu"] = max(0.0, load.BUSY_CPU - 1)
    check("a free processor lets another video start", not load.busy())
    load._state["cpu"] = load.BUSY_CPU + 1
    check("high processor load holds off another video", load.busy())
    load._state["cpu"] = 0.0

    # ---- the line: four people, one video each; pretend the server is full, then free
    q = jobs._FairQueue()
    started, release = [], threading.Event()

    def worker():
        while True:
            item = q.get()
            started.append((item[1], time.time()))
            release.wait()
            q.done()

    load._force = True
    for u in "abcd":
        q.put(("job", "job-" + u, None), user=u)
    for _ in range(4):
        threading.Thread(target=worker, daemon=True).start()
    time.sleep(8)
    check("server full: one video runs, the rest wait", len(started) == 1 and q.held, f"started {len(started)}")
    check("waiting people are told their place", q.ahead() == {"job-b": 0, "job-c": 1, "job-d": 2}, str(q.ahead()))
    t_free = time.time()
    load._force = False
    time.sleep(2.2)
    check("server frees up: the next video starts at once", len(started) == 2, f"{started[1][1] - t_free:.1f}s after it freed up" if len(started) > 1 else f"started {len(started)}")
    time.sleep(14)
    check("the rest follow a few seconds apart, not all at once", len(started) == 4, f"gaps {[round(b[1] - a[1], 1) for a, b in zip(started, started[1:])]}")
    release.set()
    time.sleep(1.5)
    check("line is empty and nothing is held", q.running == 0 and not q.turn)
    print("\n%d failed" % len(fails) if fails else "\nALL PASSED")
