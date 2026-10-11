#!/usr/bin/env python3
"""Take HMP screendumps while the guest remains running after shell return.

Unlike dm_post_shell_ui_watch.py this sampler never writes a stop request or
freezes QEMU. It captures at a fixed interval and immediately on a matched
home-entity return or first A407-present line when those appear.
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

from probe_watch import Follower


SHELL = re.compile(r"POST_SYNC_LADDER (\{.*\})")
MIN_UI = re.compile(r"POST_SHELL_HOME_MIN (\{.*\})")
POSTHOME = re.compile(r"POSTHOME_PRESENT_JSON (\{.*\})")
PRESENT = re.compile(r"HYPOTHESIS A407 presented 1179x2556 BGRA id=(\d+)")


def frame_stats(path):
    image = Image.open(path).convert("RGB")
    count = sum(max(pixel) > 8 for pixel in image.getdata())
    total = image.width * image.height
    return {
        "width": image.width, "height": image.height,
        "nonblack_gt8": count, "nonblack_fraction": count / total,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--interval", type=float, default=120.0)
    parser.add_argument("--max-secs", type=float, default=5400.0,
                        help="wall time to wait after shell return")
    args = parser.parse_args()
    if args.interval <= 0 or args.max_secs <= 0:
        parser.error("interval and max-secs must be positive")

    root = Path("/tmp/dvm")
    lldb_log = root / f"{args.tag}.lldb.log"
    stderr_log = root / "probe" / f"{args.tag}.stderr.log"
    sock = root / f"{args.tag}.sock"
    output = root / f"{args.tag}.frames"
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "frames.jsonl"
    hmp = Path(__file__).resolve().parents[1] / "hmp.py"
    lldb_follow = Follower(lldb_log)
    stderr_follow = Follower(stderr_log)
    shell_time = None
    home_return = False
    present_ids = set()
    captured = set()
    posthome_seen = set()
    next_capture = None
    started = time.monotonic()

    with manifest.open("w") as stream:
        def capture(label, marker):
            path = output / (label + ".png")
            result = subprocess.run(
                [sys.executable, str(hmp), str(sock), "screendump",
                 str(path), "-f", "png"], capture_output=True, text=True,
                timeout=40)
            row = {"label": label, "time": time.time(), "marker": marker,
                   "path": str(path),
                   "capture_ok": result.returncode == 0 and path.exists(),
                   "capture_error": result.stderr.strip() or result.stdout.strip()}
            if row["capture_ok"]:
                row.update(frame_stats(path))
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            print("POST_SHELL_FRAME " + json.dumps(row, sort_keys=True),
                  flush=True)
            captured.add(label)

        while True:
            for line in lldb_follow.read_lines():
                match = SHELL.search(line)
                if match and shell_time is None:
                    try:
                        shell = json.loads(match.group(1))
                    except json.JSONDecodeError:
                        shell = {}
                    if shell.get("label") == "SHELL_SERVICE_RETURN":
                        shell_time = time.monotonic()
                        next_capture = shell_time
                        capture("shell-return", shell)
                match = MIN_UI.search(line)
                if match:
                    try:
                        event = json.loads(match.group(1))
                    except json.JSONDecodeError:
                        event = {}
                    if (event.get("label") in (
                            "SB_HOME_ENTITY_INIT_RETURN",
                            "SB_HOME_ENTITY_INIT_ENTRY_RETURN") and
                            event.get("x0") and not home_return):
                        home_return = True
                        capture("home-entity-return", event)
                match = POSTHOME.search(line)
                if match:
                    try:
                        event = json.loads(match.group(1))
                    except json.JSONDecodeError:
                        event = {}
                    label = event.get("label")
                    # First hit at each transition gives a framebuffer sample
                    # paired to the live CA/backboardd marker. The callback is
                    # read-only; these samples do not themselves prove that a
                    # particular surface was scanned out.
                    sample_labels = {
                        "CA_COMMIT_ENTRY": "ca-commit",
                        "SERVER_RENDER_UPDATE_ENTRY": "server-render-update",
                        "CREATE_SURFACE_ENTRY": "create-surface",
                        "CREATE_SURFACE_RETURN": "create-surface-return",
                        "ALLOCATE_IOSURFACE_RETURN": "allocate-iosurface-return",
                        "FB_SWAP_SET_LAYER_ENTRY": "fb-swap-set-layer",
                        "FB_SWAP_HANDOFF": "fb-swap-handoff",
                    }
                    if label in sample_labels and label not in posthome_seen:
                        posthome_seen.add(label)
                        capture(sample_labels[label], event)
            for line in stderr_follow.read_lines():
                match = PRESENT.search(line)
                if match:
                    present_id = int(match.group(1))
                    if present_id not in present_ids:
                        present_ids.add(present_id)
                        capture("a407-id%d" % present_id, line.strip())

            now = time.monotonic()
            if shell_time is not None and next_capture is not None and now >= next_capture:
                elapsed = int(now - shell_time)
                capture("post-shell-%04ds" % elapsed,
                        {"elapsed_s": elapsed, "home_return_seen": home_return})
                next_capture = now + args.interval
            if shell_time is not None and now - shell_time >= args.max_secs:
                capture("post-shell-final",
                        {"bound_s": args.max_secs,
                         "home_return_seen": home_return,
                         "a407_ids": sorted(present_ids)})
                break
            # If the shell marker never arrives, retain a useful bounded setup
            # result without stopping or disturbing the guest.
            if shell_time is None and now - started >= args.max_secs:
                print("POST_SHELL_FRAME no-shell-marker-within-bound", flush=True)
                break
            time.sleep(0.5)


if __name__ == "__main__":
    main()
