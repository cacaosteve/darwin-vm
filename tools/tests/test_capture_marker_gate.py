"""Tests for retained post-transition capture gates."""

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "re"))
from capture_marker_gate import accepted_transition_marker, marker_is_open


class CaptureMarkerGateTests(unittest.TestCase):
    def test_empty_gate_is_open(self):
        self.assertTrue(marker_is_open(""))

    def test_primary_marker_opens_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "accepted.json"
            marker.touch()
            self.assertTrue(marker_is_open(marker))

    def test_retained_first_marker_keeps_gate_open_after_move(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "accepted.json"
            retained = Path(directory) / "accepted.first.json"
            retained.touch()
            self.assertFalse(marker.exists())
            self.assertTrue(marker_is_open(marker))

    def test_missing_primary_and_retained_markers_keep_gate_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "accepted.json"
            self.assertFalse(marker_is_open(marker))

    def test_valid_retained_transition_survives_later_unconfirmed_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "accepted.json"
            retained = Path(directory) / "accepted.first.json"
            retained.write_text(json_marker(True))
            marker.write_text(json_marker(False))
            value = accepted_transition_marker(marker)
            self.assertIsNotNone(value)
            self.assertTrue(value["dismissal_gesture"]["confirmed"])

    def test_unconfirmed_active_and_retained_markers_do_not_open_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "accepted.json"
            marker.write_text(json_marker(False))
            self.assertIsNone(accepted_transition_marker(marker))


def json_marker(confirmed):
    import json
    return json.dumps({
        "for_user_gesture": 1,
        "gesture_sequence_confirmed": confirmed,
        "dismissal_gesture": {"confirmed": confirmed},
    })


if __name__ == "__main__":
    unittest.main()
