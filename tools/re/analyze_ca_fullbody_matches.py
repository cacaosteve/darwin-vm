#!/usr/bin/env python3
"""Compare captured CoreAnimation 0x9c42 send/receive bodies exactly.

The report is a byte-identity and timing join only. It deliberately does not
equate task-local Mach port names or claim a kernel-right/transaction join.
"""

import argparse
import hashlib
import json
from pathlib import Path


SEND_PREFIX = "POSTHOME_PRESENT_JSON "
RECV_PREFIX = "CA_MSG_RECV_JSON "
MESSAGE_ID = 0x9C42


def read_events(path):
    sends = []
    receives = []
    for line in Path(path).read_text(errors="replace").splitlines():
        if line.startswith(SEND_PREFIX):
            try:
                event = json.loads(line[len(SEND_PREFIX):])
            except json.JSONDecodeError:
                continue
            message = event.get("mach_msg_call", {})
            body = message.get("message_full_body_hex")
            if (event.get("label") == "CA_RENDER_MACH_MSG_SEND_CALL" and
                    message.get("msgh_id") == MESSAGE_ID and body):
                sends.append({
                    "time": event.get("time"),
                    "size": message.get("msgh_size"),
                    "body": body.lower(),
                    "task_local_remote_name": message.get(
                        "msgh_remote_port_task_local_name"),
                    "thread": event.get("thread"),
                    "process": event.get("process"),
                })
        elif RECV_PREFIX in line:
            try:
                event = json.loads(line.split(RECV_PREFIX, 1)[1])
            except json.JSONDecodeError:
                continue
            body = event.get("body_full_hex")
            if (event.get("message_id") == MESSAGE_ID and body):
                receives.append({
                    "time": event.get("time"),
                    "size": event.get("size"),
                    "body": body.lower(),
                    "task_local_port_name": event.get("local_port"),
                    "thread": event.get("thread"),
                    "process": event.get("progname", event.get("process")),
                })
    return sends, receives


def compare_events(sends, receives, *, after_time=None):
    if after_time is not None:
        sends = [event for event in sends
                 if event.get("time") is not None and
                 event["time"] >= after_time]
        receives = [event for event in receives
                    if event.get("time") is not None and
                    event["time"] >= after_time]

    matches = []
    unmatched_receives = []
    for receive in receives:
        candidates = [send for send in sends
                      if send["size"] == receive["size"] and
                      send["body"] == receive["body"] and
                      send.get("time") is not None and
                      receive.get("time") is not None and
                      send["time"] <= receive["time"]]
        if not candidates:
            unmatched_receives.append({
                "time": receive.get("time"),
                "size": receive.get("size"),
                "process": receive.get("process"),
                "thread": receive.get("thread"),
                "task_local_port_name": receive.get("task_local_port_name"),
                "body_sha256": hashlib.sha256(
                    bytes.fromhex(receive["body"])).hexdigest(),
            })
            continue
        send = max(candidates, key=lambda item: item["time"])
        matches.append({
            "send_time": send["time"],
            "receive_time": receive["time"],
            "delta_ms": round((receive["time"] - send["time"]) * 1000, 3),
            "message_size": receive["size"],
            "body_size": len(receive["body"]) // 2,
            "body_sha256": hashlib.sha256(
                bytes.fromhex(receive["body"])).hexdigest(),
            "matching_prior_send_count": len(candidates),
            "send_process": send.get("process"),
            "send_thread": send.get("thread"),
            "send_task_local_remote_name": send.get(
                "task_local_remote_name"),
            "receive_process": receive.get("process"),
            "receive_thread": receive.get("thread"),
            "receive_task_local_port_name": receive.get(
                "task_local_port_name"),
            "port_names_compared_across_tasks": False,
        })

    deltas = [match["delta_ms"] for match in matches]
    return {
        "message_id": hex(MESSAGE_ID),
        "comparison": "exact-full-body-and-size-with-prior-send-time",
        "claim_limit": (
            "payload identity/timing only; not a transaction-id or shared "
            "kernel IPC-right join"),
        "send_count": len(sends),
        "receive_count": len(receives),
        "exact_match_count": len(matches),
        "unmatched_receive_count": len(unmatched_receives),
        "all_matches_unique_prior_send": all(
            match["matching_prior_send_count"] == 1 for match in matches),
        "delta_ms_min": min(deltas) if deltas else None,
        "delta_ms_max": max(deltas) if deltas else None,
        "matches": matches,
        "unmatched_receives": unmatched_receives,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lldb_log", type=Path)
    parser.add_argument("--after-time", type=float,
                        help="restrict both sides to events at/after this epoch")
    parser.add_argument("--output", type=Path,
                        help="write JSON report to this path instead of stdout")
    args = parser.parse_args()
    report = compare_events(*read_events(args.lldb_log),
                            after_time=args.after_time)
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized)
    else:
        print(serialized, end="")


if __name__ == "__main__":
    main()
