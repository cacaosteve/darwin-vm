#!/usr/bin/env python3
"""Tests for VM-address sampling in the offline Type-4 DCP reconstructor."""
import importlib.util
from pathlib import Path
import struct
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "dcp_validate_runtime_data_mapping",
    ROOT / "tools/re/dcp_validate_runtime_data_mapping.py",
)
mapping = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mapping)


class VirtualAddressSampleTests(unittest.TestCase):
    def setUp(self):
        self.macho = bytes(range(256)) + bytes(256)
        self.segments = [
            {"name": "__DATA_CONST", "vmaddr": 0x1000, "vmsize": 0x100,
             "fileoff": 0x20, "filesize": 0x80},
            {"name": "__DATA", "vmaddr": 0x2000, "vmsize": 0x100,
             "fileoff": 0x100, "filesize": 0x40},
        ]

    def test_file_backed_sample_maps_vm_delta_to_file_offset(self):
        row = mapping.sample_virtual_address(
            self.macho, self.segments, 0x1010, 4)
        self.assertEqual(row["segment"], "__DATA_CONST")
        self.assertEqual(row["storage"], "file-backed")
        self.assertEqual(row["macho_file_offset"], "0x30")
        self.assertEqual(row["bytes"], self.macho[0x30:0x34].hex())

    def test_zero_fill_sample_is_not_misreported_as_file_bytes(self):
        row = mapping.sample_virtual_address(
            self.macho, self.segments, 0x2050, 8)
        self.assertEqual(row["storage"], "zero-fill")
        self.assertIsNone(row["macho_file_offset"])
        self.assertEqual(row["zero_fill_size"], "0x8")
        self.assertEqual(row["bytes"], "00" * 8)

    def test_sample_straddling_file_and_zero_fill_reports_both(self):
        row = mapping.sample_virtual_address(
            self.macho, self.segments, 0x107e, 4)
        self.assertEqual(row["storage"], "file-backed+zero-fill")
        self.assertEqual(row["file_backed_size"], "0x2")
        self.assertEqual(row["zero_fill_size"], "0x2")
        self.assertEqual(row["bytes"], self.macho[0x9e:0xa0].hex() + "0000")

    def test_sample_cannot_cross_segment_virtual_extent(self):
        with self.assertRaisesRegex(ValueError, "crosses __DATA_CONST vmsize"):
            mapping.sample_virtual_address(
                self.macho, self.segments, 0x10ff, 2)

    def test_ambiguous_or_unmapped_address_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "has 0 containing segments"):
            mapping.sample_virtual_address(self.macho, self.segments, 0x3000, 4)
        overlapping = self.segments + [
            {"name": "duplicate", "vmaddr": 0x1000, "vmsize": 0x100,
             "fileoff": 0, "filesize": 0x100}
        ]
        with self.assertRaisesRegex(ValueError, "has 2 containing segments"):
            mapping.sample_virtual_address(self.macho, overlapping, 0x1010, 4)


def macho_with_text_and_data(text_size, data_size):
    def segment(name, vmaddr, vmsize, fileoff, filesize):
        return struct.pack(
            "<II16sQQQQiiII", mapping.LC_SEGMENT_64, 72,
            name.encode().ljust(16, b"\0"), vmaddr, vmsize,
            fileoff, filesize, 7, 5, 0, 0)

    commands = (segment("__TEXT", 0x4000000, text_size, 0x1000, text_size) +
                segment("__DATA", 0x4000000 + text_size,
                        data_size, 0x1000 + text_size, data_size))
    header = struct.pack("<IiiIIIII", mapping.MACHO_MAGIC_64,
                         0x100000C, 0, 2, 2, len(commands), 0, 0)
    return header + commands


class NoldInventoryTests(unittest.TestCase):
    def test_finds_embedded_headers_and_preserves_bundle_offsets(self):
        first = macho_with_text_and_data(0x4000, 0x2000)
        second = macho_with_text_and_data(0x1000, 0x800)
        nold = first + bytes(0x100) + second
        images = mapping.parse_nold_images(nold, bundle_offset=0x9000)
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]["header_bundle_offset"], "0x9000")
        self.assertEqual(images[1]["header_bundle_offset"],
                         hex(0x9000 + len(first) + 0x100))
        self.assertEqual(images[0]["text_filesize"], "0x4000")
        self.assertEqual(images[0]["data_file_span"], "0x2000")

    def test_reads_uuid_from_header_only_image(self):
        image = bytearray(macho_with_text_and_data(0x4000, 0x2000))
        uuid_command = struct.pack(
            "<II16s", 0x1B, 24,
            bytes.fromhex("7a8b83fb1ec83f13a7c4d9cd5bc71718"))
        struct.pack_into("<I", image, 16, 3)
        struct.pack_into("<I", image, 20, len(image) - 32 + len(uuid_command))
        image = image[:32] + image[32:] + uuid_command
        self.assertEqual(mapping.parse_macho_uuid(image),
                         "7A8B83FB-1EC8-3F13-A7C4-D9CD5BC71718")
        images = mapping.parse_nold_images(bytes(image), bundle_offset=0x9000)
        self.assertEqual(images[0]["uuid"],
                         "7A8B83FB-1EC8-3F13-A7C4-D9CD5BC71718")

    def test_capacity_report_never_claims_secondary_byte_mapping(self):
        images = [
            {"header_bundle_offset": "0x1000", "text_vmsize": "0x4000",
             "text_filesize": "0x4000",
             "data_file_span": "0x2000"},
            {"header_bundle_offset": "0x2000", "text_vmsize": "0x1800",
             "text_filesize": "0x1800",
             "data_file_span": "0x1000"},
        ]
        ranges = {"rtxt": {"size": 0x5000}, "rdat": {"size": 0x4000}}
        result = mapping.inventory_nold_payload_fit(ranges, images)
        self.assertEqual(result["status"],
                         "capacity-comparison-only-secondary-offsets-unproven")
        self.assertFalse(result["secondary_mapping_claimed"])
        self.assertEqual(result["rtxt_shortfall_if_naively_concatenated"],
                         "0x800")

    def test_secondary_candidate_is_labeled_and_uses_main_payload_tails(self):
        main = macho_with_text_and_data(0x4000, 0x2000)
        secondary = macho_with_text_and_data(0x1000, 0x800)
        nold_offset = 0x80
        nold = main + bytes(0x100) + secondary
        rtxt_offset = 0x1000
        rtxt_size = 0x5000
        rdat_offset = rtxt_offset + rtxt_size
        rdat_size = 0x3000
        bundle = bytearray(rdat_offset + rdat_size)
        bundle[nold_offset:nold_offset + len(nold)] = nold
        bundle[rtxt_offset:rtxt_offset + rtxt_size] = bytes(
            (index % 251) + 1 for index in range(rtxt_size))
        bundle[rdat_offset:rdat_offset + rdat_size] = bytes(
            (index % 239) + 2 for index in range(rdat_size))
        images = mapping.parse_nold_images(nold, nold_offset)
        ranges = {
            "nold": {"offset": nold_offset, "size": len(nold)},
            "rtxt": {"offset": rtxt_offset, "size": rtxt_size},
            "rdat": {"offset": rdat_offset, "size": rdat_size},
        }
        candidate, report = mapping.reconstruct_nold_secondary_candidate(
            bytes(bundle), ranges, images)
        self.assertIn("candidate", report["status"])
        self.assertFalse(report["secondary_mapping_claimed"])
        self.assertEqual(report["candidate_rtxt_start"], "0x5000")
        self.assertEqual(report["candidate_rdat_start"], "0x8000")
        self.assertTrue(report["candidate_is_complete_macho"])
        self.assertEqual(report["uncovered_file_references"], [])
        self.assertEqual(len(candidate), 0x2800)

    def test_secondary_candidate_flags_symbol_tables_past_segment_payload(self):
        main = macho_with_text_and_data(0x4000, 0x2000)
        secondary = bytearray(macho_with_text_and_data(0x1000, 0x800))
        symtab = struct.pack("<IIIIII", mapping.LC_SYMTAB, 24,
                             0x2800, 3, 0x2830, 0x40)
        header = bytearray(secondary[:32])
        struct.pack_into("<I", header, 16, 3)
        struct.pack_into("<I", header, 20, len(secondary) - 32 + len(symtab))
        secondary = header + secondary[32:] + symtab

        nold_offset = 0x80
        nold = main + bytes(0x100) + bytes(secondary)
        rtxt_offset = 0x1000
        rtxt_size = 0x5000
        rdat_offset = rtxt_offset + rtxt_size
        rdat_size = 0x3000
        bundle = bytearray(rdat_offset + rdat_size)
        bundle[nold_offset:nold_offset + len(nold)] = nold
        bundle[rtxt_offset:rtxt_offset + rtxt_size] = bytes(
            (index % 251) + 1 for index in range(rtxt_size))
        bundle[rdat_offset:rdat_offset + rdat_size] = bytes(
            (index % 239) + 2 for index in range(rdat_size))
        images = mapping.parse_nold_images(nold, nold_offset)
        ranges = {
            "nold": {"offset": nold_offset, "size": len(nold)},
            "rtxt": {"offset": rtxt_offset, "size": rtxt_size},
            "rdat": {"offset": rdat_offset, "size": rdat_size},
        }
        _, report = mapping.reconstruct_nold_secondary_candidate(
            bytes(bundle), ranges, images)
        self.assertFalse(report["candidate_is_complete_macho"])
        self.assertEqual(report["uncovered_file_references"], [
            {"label": "LC_SYMTAB.symbols", "offset": 0x2800,
             "size": 48, "end": 0x2830, "candidate_covered": False},
            {"label": "LC_SYMTAB.strings", "offset": 0x2830,
             "size": 0x40, "end": 0x2870, "candidate_covered": False},
        ])
        self.assertIn("incomplete-file-references", report["status"])

    def test_empty_nold_is_reported_without_guessing(self):
        self.assertEqual(mapping.parse_nold_images(b""), [])
        self.assertEqual(
            mapping.inventory_nold_payload_fit(
                {"rtxt": {"size": 1}, "rdat": {"size": 1}}, []),
            {"status": "no-valid-nold-mach-o-headers"})


if __name__ == "__main__":
    unittest.main()
