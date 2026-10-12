import tempfile
import unittest
from pathlib import Path

from tools.re.analyze_ca_fullbody_matches import compare_events, read_events


class CAMessageFullBodyTest(unittest.TestCase):
    def test_exact_full_body_join_is_unique_and_reports_name_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lldb.log"
            send = {
                "label": "CA_RENDER_MACH_MSG_SEND_CALL",
                "process": "SpringBoard", "thread": 11, "time": 10.0,
                "mach_msg_call": {
                    "msgh_id": 0x9C42, "msgh_size": 28,
                    "message_full_body_hex": "aabbccdd",
                    "msgh_remote_port_task_local_name": 123,
                },
            }
            receive = {
                "message_id": 0x9C42, "size": 28, "body_full_hex": "AABBCCDD",
                "time": 10.02, "progname": "backboardd", "thread": 22,
                "local_port": 456,
            }
            path.write_text(
                "POSTHOME_PRESENT_JSON " + __import__("json").dumps(send) + "\n" +
                "CA_MSG_RECV_JSON " + __import__("json").dumps(receive) + "\n")
            sends, receives = read_events(path)
        report = compare_events(sends, receives)
        self.assertEqual(report["exact_match_count"], 1)
        self.assertTrue(report["all_matches_unique_prior_send"])
        self.assertEqual(report["matches"][0]["delta_ms"], 20.0)
        self.assertFalse(report["matches"][0]["port_names_compared_across_tasks"])

    def test_unmatched_receive_is_not_closed_by_remote_port_name(self):
        sends = [{"time": 10.0, "size": 28, "body": "aabb",
                  "task_local_remote_name": 5}]
        receives = [{"time": 10.1, "size": 28, "body": "ccdd",
                     "task_local_port_name": 5, "process": "backboardd"}]
        report = compare_events(sends, receives)
        self.assertEqual(report["exact_match_count"], 0)
        self.assertEqual(report["unmatched_receive_count"], 1)

    def test_after_time_filters_both_sides(self):
        sends = [
            {"time": 1.0, "size": 24, "body": ""},
            {"time": 2.0, "size": 24, "body": ""},
        ]
        receives = [
            {"time": 1.5, "size": 24, "body": "", "process": "old"},
            {"time": 2.5, "size": 24, "body": "", "process": "new"},
        ]
        report = compare_events(sends, receives, after_time=2.0)
        self.assertEqual(report["send_count"], 1)
        self.assertEqual(report["receive_count"], 1)
        self.assertEqual(report["exact_match_count"], 1)


if __name__ == "__main__":
    unittest.main()
