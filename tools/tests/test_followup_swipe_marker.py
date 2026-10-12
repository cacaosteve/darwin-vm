"""Tests for the delayed follow-up swipe's append-only marker join."""

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "re"))
from send_one_followup_swipe import (  # noqa: E402
    is_confirmed_transition,
    read_confirmed_transition,
    transition_records_since,
)


def marker(confirmed):
    return {
        "label": "ACCEPTED_LOCKSCREEN_GESTURE_TRANSITION",
        "for_user_gesture": 1,
        "gesture_sequence_confirmed": confirmed,
        "dismissal_gesture": {"confirmed": confirmed},
    }


class FollowupSwipeMarkerTests(unittest.TestCase):
    def test_confirmation_requires_full_dismissal_sequence(self):
        self.assertTrue(is_confirmed_transition(marker(True)))
        self.assertFalse(is_confirmed_transition(marker(False)))
        value = marker(True)
        value["dismissal_gesture"]["confirmed"] = False
        self.assertFalse(is_confirmed_transition(value))

    def test_first_confirmed_marker_survives_unconfirmed_active_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "accepted.json"
            first = Path(directory) / "accepted.first.json"
            active.write_text(json.dumps(marker(False)))
            first.write_text(json.dumps(marker(True)))
            self.assertEqual(read_confirmed_transition(active), marker(True))

    def test_append_only_log_ignores_unconfirmed_and_partial_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock-gesture.jsonl"
            path.write_text("old record\n")
            cursor = path.stat().st_size
            unconfirmed = TRANSITION_PREFIX + json.dumps(marker(False)) + "\n"
            confirmed = TRANSITION_PREFIX + json.dumps(marker(True)) + "\n"
            with path.open("a") as output:
                output.write(unconfirmed + confirmed[:-1])
            records, new_cursor = transition_records_since(path, cursor)
            self.assertEqual(records, [marker(False)])
            self.assertEqual(new_cursor, cursor + len(unconfirmed.encode()))
            with path.open("a") as output:
                output.write(confirmed[-1:])
            records, _ = transition_records_since(path, new_cursor)
            self.assertEqual(records, [marker(True)])


TRANSITION_PREFIX = "LOCK_GESTURE_TRANSITION_MARKER "


if __name__ == "__main__":
    unittest.main()
