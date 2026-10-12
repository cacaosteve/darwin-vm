"""Tests for post-home surface-argument evidence labels."""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "re"))
from posthome_surface_status import classify_iosurface_x3


class PosthomeSurfaceStatusTests(unittest.TestCase):
    def test_null_is_no_surface_pointer(self):
        self.assertEqual(classify_iosurface_x3(0),
                         "no-surface-pointer-at-x3")
        self.assertEqual(classify_iosurface_x3(None),
                         "no-surface-pointer-at-x3")

    def test_non_guest_pointer_is_not_sampled(self):
        self.assertEqual(classify_iosurface_x3(0x1000),
                         "x3-not-a-valid-user-pointer")
        self.assertEqual(classify_iosurface_x3(0x5000000000),
                         "x3-not-a-valid-user-pointer")

    def test_guest_pointer_with_capacity_is_read_attempt(self):
        self.assertEqual(classify_iosurface_x3(0x7ABC123456),
                         "captured-or-read-attempted")

    def test_guest_pointer_after_sample_cap_is_not_claimed_read(self):
        self.assertEqual(classify_iosurface_x3(0x7ABC123456, False),
                         "valid-user-pointer-sample-limit-reached")


if __name__ == "__main__":
    unittest.main()
