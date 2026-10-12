"""Tests for exact context pairing at post-home kernel return PCs."""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "re"))
from posthome_return_context import return_context_status


class ReturnContextTests(unittest.TestCase):
    def test_matching_thread_and_restored_sp(self):
        self.assertEqual(
            return_context_status(0x111, 0x222, 0x111, 0x222),
            "matched-entry-thread-and-restored-sp")

    def test_shared_return_pc_on_other_thread_is_rejected(self):
        self.assertEqual(
            return_context_status(0x111, 0x222, 0x333, 0x222),
            "thread-mismatch")

    def test_same_thread_with_different_sp_is_rejected(self):
        self.assertEqual(
            return_context_status(0x111, 0x222, 0x111, 0x444),
            "stack-pointer-mismatch")

    def test_missing_registers_are_not_a_match(self):
        self.assertEqual(
            return_context_status(0x111, None, 0x111, 0x222),
            "entry-context-incomplete")
        self.assertEqual(
            return_context_status(0x111, 0x222, 0x111, None),
            "return-context-incomplete")


if __name__ == "__main__":
    unittest.main()
