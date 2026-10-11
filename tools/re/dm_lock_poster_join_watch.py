#!/usr/bin/env python3
"""Bound one continuous poster-context/display-update probe.

Only a same-boot Mercury setLayer plus SpringBoard setScene makes a poster ID
scoreable. The watcher then captures the first A407 host frame after that
join and asks probe.sh to freeze shortly afterward. It never edits guest state.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from PIL import Image

from probe_watch import Follower, write_stop


PRESENT = re.compile(r"HYPOTHESIS A407 presented 1179x2556 BGRA id=(\d+)")


def _pixels(path):
    image = Image.open(path).convert("RGB")
    count = image.width * image.height
    nonblack = sum(max(pixel) > 8 for pixel in image.get_flattened_data())
    return {"nonblack_gt8": nonblack, "nonblack_fraction": nonblack / count,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--posthome-secs", type=int, default=1000)
    parser.add_argument("--postscene-secs", type=int, default=600)
    parser.add_argument("--after-present-secs", type=int, default=40)
    parser.add_argument("--require-display-accept", action="store_true",
                        help="capture only an A407 that follows SERVER_HOSTED_POSTER_ACCEPT for this boot's joined context")
    parser.add_argument("--max-secs", type=int, default=3500)
    args = parser.parse_args()
    if min(args.posthome_secs, args.postscene_secs,
           args.after_present_secs, args.max_secs) < 1:
        parser.error("bounds must be positive")

    root = Path("/tmp/dvm")
    lldb = Follower(root / f"{args.tag}.lldb.log")
    model = Follower(root / "probe" / f"{args.tag}.stderr.log")
    sock = root / f"{args.tag}.sock"
    out = root / f"{args.tag}.poster-join.json"
    stop = root / f"{args.tag}.stop"
    hmp = Path(__file__).resolve().parents[1] / "hmp.py"
    mercury = {}
    scenes = set()
    home_at = scene_at = present_at = None
    positive_id = None
    accepted_at = None
    accepted_event = None
    frame = None
    coverage_lost = False
    started = time.monotonic()
    while True:
        for line in lldb.read_lines():
            if line.strip() == "(lldb) quit":
                coverage_lost = True
            if "DM_ADFL_HOME_COMPLETE" in line and home_at is None:
                home_at = time.monotonic()
                print("HOME_COMPLETE", flush=True)
            if not line.startswith("DM_LOCK_RICH_JSON "):
                continue
            try:
                event = json.loads(line.split(" ", 1)[1])
            except json.JSONDecodeError:
                continue
            label = event.get("label")
            if label == "CA_CONTEXT_SET_LAYER" and event.get("process", "").startswith("MercuryPoster"):
                context_id = event.get("context_id")
                if context_id and event.get("root_layer"):
                    mercury[context_id] = event
                    print("MERCURY_CONTEXT id=0x%x" % context_id, flush=True)
            elif label == "POSTER_HOST_SET_SCENE_RETURN":
                context_id = event.get("context_after")
                if context_id and event.get("scene_after"):
                    scenes.add(context_id)
                    print("SB_POSTER_SCENE id=0x%x" % context_id, flush=True)
            elif (label == "SERVER_HOSTED_POSTER_ACCEPT" and
                  positive_id is not None and
                  event.get("context_id") == positive_id and
                  accepted_at is None):
                accepted_at = time.monotonic()
                accepted_event = event
                print("POSTER_DISPLAY_ACCEPT id=0x%x update=0x%x" %
                      (positive_id, event.get("update", 0)), flush=True)
            if positive_id is None:
                matched = scenes.intersection(mercury)
                if matched:
                    positive_id = sorted(matched)[0]
                    scene_at = time.monotonic()
                    print("POSTER_JOIN_ID id=0x%x" % positive_id, flush=True)

        for line in model.read_lines():
            match = PRESENT.search(line)
            if (not match or positive_id is None or frame is not None or
                    (args.require_display_accept and accepted_at is None)):
                continue
            swap_id = int(match.group(1))
            path = root / f"{args.tag}.poster-id{swap_id}.png"
            result = subprocess.run([sys.executable, str(hmp), str(sock),
                                     "screendump", str(path), "-f", "png"],
                                    capture_output=True, text=True, timeout=30)
            frame = {"id": swap_id, "path": str(path), "model_line": line,
                     "capture_ok": result.returncode == 0 and path.exists(),
                     "capture_error": result.stderr.strip() or result.stdout.strip()}
            if frame["capture_ok"]:
                frame.update(_pixels(path))
            present_at = time.monotonic()
            print("POSTER_FRAME " + json.dumps(frame, sort_keys=True), flush=True)

        now = time.monotonic()
        if coverage_lost:
            reason = "poster-join-coverage-lost-lldb-detached"
            break
        if (present_at is not None and now - present_at >= args.after_present_secs):
            reason = "poster-join-postscene-present id=0x%x a407=%d" % (positive_id, frame["id"])
            break
        if scene_at is not None and now - scene_at >= args.postscene_secs:
            reason = "poster-join-postscene-bound id=0x%x present=%s" % (positive_id, bool(frame))
            break
        if positive_id is None and home_at is not None and now - home_at >= args.posthome_secs:
            reason = "poster-join-no-positive-context-posthome-bound"
            break
        if now - started >= args.max_secs:
            reason = "poster-join-total-bound positive=%s" % (positive_id is not None)
            break
        time.sleep(0.25)

    out.write_text(json.dumps({"tag": args.tag, "reason": reason,
                               "positive_context_id": positive_id,
                               "mercury_context_ids": sorted(mercury),
                               "scene_context_ids": sorted(scenes),
                               "display_accept": accepted_event,
                               "frame": frame}, indent=2, sort_keys=True) + "\n")
    write_stop(stop, reason)
    print("STOP " + reason, flush=True)


if __name__ == "__main__":
    main()
