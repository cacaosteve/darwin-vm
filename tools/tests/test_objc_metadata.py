"""Address normalization tests for live Objective-C metadata probes."""

import os
import sys
import unittest


TOOLS = os.path.dirname(os.path.dirname(__file__))
RE = os.path.join(TOOLS, "re")
sys.path.insert(0, RE)
import objc_metadata


class ObjCMetadataTests(unittest.TestCase):
    def test_cstring_pointer_keeps_unaligned_low_bits(self):
        pointer = 0xABCD00001234567B
        self.assertEqual(objc_metadata.cstring_pointer(pointer),
                         0x000000001234567B)

    def test_null_cstring_pointer_stays_null(self):
        self.assertEqual(objc_metadata.cstring_pointer(0), 0)


if __name__ == "__main__":
    unittest.main()
