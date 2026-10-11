#!/usr/bin/env python3
"""Capture HMP frames at shell return, home completion, and bounded close.

The watcher follows LLDB and QEMU stderr without stopping the guest itself.
It requests a stop through the normal probe stop file only after the home
marker or the post-shell bound, and records a final screendump first.
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


def stats(path):
    image = Image.open(path).convert("RGB")
    total = image.width * image.height
    nonblack = sum(max(pixel) > 8 for pixel in image.getdata())
    return {
        "width": image.width,
        "height": image.height,
        "nonblack_gt8": nonblack,
        "nonblack_fraction": nonblack / total,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--post-shell-secs", type=int, default=1800)
    parser.add_argument("--total-secs", type=int, default=6000)
    parser.add_argument("--poll-secs", type=float, default=0.25)
    args = parser.parse_args()
    if min(args.post_shell_secs, args.total_secs) < 1 or args.poll_secs <= 0:
        parser.error("bounds and poll interval must be positive")

    root = Path("/tmp/dvm")
    lldb = Follower(root / f"{args.tag}.lldb.log")
    stderr = Follower(root / "probe" / f"{args.tag}.stderr.log")
    sock = root / f"{args.tag}.sock"
    stop_file = root / f"{args.tag}.stop"
    out_dir = root / f"{args.tag}.frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / "frames.jsonl"
    hmp = Path(__file__).resolve().parents[1] / "hmp.py"
    started = time.monotonic()
    shell_seen = None
    home_seen = None
    captured = set()
    present_ids = set()

    with manifest.open("w") as stream:
        def capture(label, marker):
            path = out_dir / f"{label}.png"
            result = subprocess.run(
                [sys.executable, str(hmp), str(sock), "screendump",
                 str(path), "-f", "png"],
                capture_output=True, text=True, timeout=40)
            row = {
                "label": label,
                "time": time.time(),
                "marker": marker,
                "path": str(path),
                "capture_ok": result.returncode == 0 and path.exists(),
                "capture_error": (result.stderr.strip() or
                                  result.stdout.strip()),
            }
            if row["capture_ok"]:
                row.update(stats(path))
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            print("POST_SHELL_FRAME " + json.dumps(row, sort_keys=True),
                  flush=True)
            captured.add(label)
            return row

        while True:
            for line in lldb.read_lines():
                if "POST_SYNC_LADDER " in line:
                    try:
                        row = json.loads(line.split("POST_SYNC_LADDER ", 1)[1])
                    except (IndexError, json.JSONDecodeError):
                        row = {}
                    if row.get("label") == "SHELL_SERVICE_RETURN" and shell_seen is None:
                        shell_seen = time.monotonic()
                        capture("shell-return", row)
                if "DM_ADFL_HOME_COMPLETE" in line and home_seen is None:
                    home_seen = time.monotonic()
                    capture("home-complete", line.strip())
                if "DM_ADFL_HOME_JSON " in line:
                    try:
                        row = json.loads(line.split("DM_ADFL_HOME_JSON ", 1)[1])
                    except (IndexError, json.JSONDecodeError):
                        row = {}
                    if (row.get("label") == "SB_HOME_ENTITY_INIT_RETURN" and
                            row.get("x0") and "home-entity-return" not in captured):
                        capture("home-entity-return", row)
                    elif (row.get("label") == "SB_ADFL_ENTRY" and
                          "adfl-entry" not in captured):
                        capture("adfl-entry", row)
            for line in stderr.read_lines():
                match = PRESENT.search(line)
                if match and home_seen is not None:
                    present_id = int(match.group(1))
                    if present_id not in present_ids:
                        present_ids.add(present_id)
                        capture(f"a407-id{present_id}", line.strip())

            now = time.monotonic()
            if home_seen is not None:
                reason = "post-shell-home-complete"
                capture("final-home", reason)
                write_stop(stop_file, reason)
                break
            if shell_seen is not None and now - shell_seen >= args.post_shell_secs:
                reason = "post-shell-ui-bound-no-home-marker"
                capture("post-shell-bound", reason)
                write_stop(stop_file, reason)
                break
            if now - started >= args.total_secs:
                reason = ("post-shell-total-bound-shell-seen" if shell_seen
                          else "post-shell-total-bound-no-shell-return")
                capture("total-bound", reason)
                write_stop(stop_file, reason)
                break
            time.sleep(args.poll_secs)


if __name__ == "__main__":
    main()
