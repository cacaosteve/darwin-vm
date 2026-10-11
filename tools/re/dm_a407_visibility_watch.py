#!/usr/bin/env python3
"""Capture later A407 host frames while the guest LLDB probe stays attached.

This watches only the established CLASS16 home and A407-present log markers.
HMP screendump works while QEMU runs; a stop-file ends the bounded observation
after several post-home frames or a short post-home timeout. No guest state is
written and no swap-completion behavior is changed.
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


def image_stats(path):
    image = Image.open(path).convert("RGB")
    count = image.width * image.height
    nonblack = sum(max(pixel) > 8 for pixel in image.getdata())
    return {
        "width": image.width,
        "height": image.height,
        "nonblack_gt8": nonblack,
        "nonblack_fraction": nonblack / count,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--max-posthome", type=int, default=6)
    parser.add_argument("--posthome-secs", type=int, default=420)
    parser.add_argument("--max-secs", type=int, default=3300)
    parser.add_argument("--assume-home", action="store_true",
                        help="continue a frozen post-home guest with a fresh LLDB log")
    args = parser.parse_args()
    if args.max_posthome < 1 or args.posthome_secs < 1 or args.max_secs < 1:
        parser.error("bounds must be positive")

    root = Path("/tmp/dvm")
    lldb = Follower(root / f"{args.tag}.lldb.log")
    model = Follower(root / "probe" / f"{args.tag}.stderr.log")
    sock = root / f"{args.tag}.sock"
    out_dir = root / f"{args.tag}.frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = root / f"{args.tag}.frames.jsonl"
    stop_file = root / f"{args.tag}.stop"
    hmp = Path(__file__).resolve().parents[1] / "hmp.py"

    home = args.assume_home
    first_home = time.monotonic() if home else None
    frames = []
    waits = {}
    started = time.monotonic()
    with summary.open("w") as stream:
        while True:
            for line in lldb.read_lines():
                if "DM_ADFL_HOME_COMPLETE" in line and not home:
                    home = True
                    first_home = time.monotonic()
                    print("HOME_COMPLETE", flush=True)
                if line.startswith("POSTHOME_SWAP_BRANCH_JSON "):
                    try:
                        event = json.loads(line.split(" ", 1)[1])
                    except json.JSONDecodeError:
                        continue
                    if event.get("label") == "KERN_WAIT_RETURN":
                        fence = event.get("fence")
                        waits[fence] = event
                        print("WAIT_RETURN fence=%s result=%s elapsed=%s" %
                              (fence, event.get("result"), event.get("elapsed")),
                              flush=True)
            for line in model.read_lines():
                match = PRESENT.search(line)
                if not match or not home:
                    continue
                swap_id = int(match.group(1))
                path = out_dir / f"id{swap_id}.png"
                result = subprocess.run(
                    [sys.executable, str(hmp), str(sock), "screendump",
                     str(path), "-f", "png"],
                    capture_output=True, text=True, timeout=30,
                )
                row = {"id": swap_id, "path": str(path), "present": line,
                       "capture_ok": result.returncode == 0 and path.exists(),
                       "capture_error": result.stderr.strip() or result.stdout.strip()}
                if row["capture_ok"]:
                    row.update(image_stats(path))
                frames.append(row)
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                stream.flush()
                print("FRAME " + json.dumps(row, sort_keys=True), flush=True)

            now = time.monotonic()
            if len(frames) >= args.max_posthome:
                last_id = frames[-1]["id"]
                if last_id in waits or now - first_home >= args.posthome_secs:
                    reason = f"a407-visibility-frames={len(frames)} last-id={last_id} wait={last_id in waits}"
                    break
            if first_home is not None and now - first_home >= args.posthome_secs:
                reason = f"a407-visibility-posthome-bound frames={len(frames)} waits={len(waits)}"
                break
            if now - started >= args.max_secs:
                reason = f"a407-visibility-total-bound frames={len(frames)} waits={len(waits)}"
                break
            time.sleep(0.25)
    write_stop(stop_file, reason)
    print("STOP " + reason, flush=True)


if __name__ == "__main__":
    main()
