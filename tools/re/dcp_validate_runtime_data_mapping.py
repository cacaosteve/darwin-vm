#!/usr/bin/env python3
"""Reconstruct a Type-4 DCP runtime Mach-O using its rtxt/rdat ranges.

This is an offline consistency check for the 24A5430a DCP investigation. It
uses an already extracted Mach-O as the header/load-command template, parses
the BUND range table, and applies the documented runtime layout: rtxt starts
at the first segment's file offset; rdat starts at the second segment's file
offset and continues through the remaining output. It never edits either
input and writes only to the caller-selected output path.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import uuid


BUND_TYPE4 = 4
MACHO_MAGIC_64 = 0xFEEDFACF
LC_SEGMENT_64 = 0x19
LC_SYMTAB = 0x2
LC_DYSYMTAB = 0xB
HEADER_SIZE = 16
TYPE4_SIZE = 936
TYPE4_RANGE_SIZE = 24
TYPE4_RANGE_COUNT = 13
TYPE4_RANGES_OFFSET = HEADER_SIZE + TYPE4_SIZE - TYPE4_RANGE_SIZE * TYPE4_RANGE_COUNT


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def checked_slice(blob, offset, size, label):
    if offset < 0 or size < 0 or offset + size > len(blob):
        raise ValueError("%s out of bounds: off=%#x size=%#x len=%#x" %
                         (label, offset, size, len(blob)))
    return blob[offset:offset + size]


def parse_type4_ranges(bundle):
    if len(bundle) < TYPE4_RANGES_OFFSET + TYPE4_RANGE_SIZE * TYPE4_RANGE_COUNT:
        raise ValueError("BUND is shorter than its Type-4 range table")
    # The Type-4 parser reverses this on-disk magic before comparing it with
    # the human-readable identifier BUND.
    if bundle[8:12] != b"DNUB":
        raise ValueError("bad BUND magic")
    bundle_type = struct.unpack_from("<H", bundle, 14)[0]
    if bundle_type != BUND_TYPE4:
        raise ValueError("expected Type-4 BUND, got type %d" % bundle_type)
    ranges = {}
    for index in range(TYPE4_RANGE_COUNT):
        pos = TYPE4_RANGES_OFFSET + index * TYPE4_RANGE_SIZE
        typ, reversed_name, offset, size = struct.unpack_from("<I4sQQ", bundle, pos)
        name = reversed_name[::-1].rstrip(b"\0").decode("ascii", "replace")
        if name and size:
            checked_slice(bundle, offset, size, "BUND range %s" % name)
            ranges[name] = {"type": typ, "offset": offset, "size": size}
    for required in ("rtxt", "rdat"):
        if required not in ranges:
            raise ValueError("Type-4 BUND missing %s range" % required)
    return ranges


def parse_segments(macho):
    if len(macho) < 32 or struct.unpack_from("<I", macho, 0)[0] != MACHO_MAGIC_64:
        raise ValueError("reference is not a little-endian 64-bit Mach-O")
    ncmds = struct.unpack_from("<I", macho, 16)[0]
    command_offset = 32
    segments = []
    for _ in range(ncmds):
        if command_offset + 8 > len(macho):
            raise ValueError("truncated Mach-O load-command header")
        command, command_size = struct.unpack_from("<II", macho, command_offset)
        if command_size < 8 or command_offset + command_size > len(macho):
            raise ValueError("invalid Mach-O command size %#x" % command_size)
        if command == LC_SEGMENT_64:
            if command_size < 72:
                raise ValueError("short LC_SEGMENT_64")
            raw_name = macho[command_offset + 8:command_offset + 24]
            name = raw_name.split(b"\0", 1)[0].decode("ascii", "replace")
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from(
                "<QQQQ", macho, command_offset + 24)
            segments.append({"name": name, "vmaddr": vmaddr, "vmsize": vmsize,
                             "fileoff": fileoff, "filesize": filesize})
        command_offset += command_size
    if len(segments) < 2 or segments[0]["name"] != "__TEXT":
        raise ValueError("expected __TEXT followed by a data segment")
    return segments


def parse_macho_uuid(macho):
    """Read LC_UUID from a Mach-O header without needing its payload."""
    if len(macho) < 32 or struct.unpack_from("<I", macho, 0)[0] != MACHO_MAGIC_64:
        raise ValueError("reference is not a little-endian 64-bit Mach-O")
    ncmds = struct.unpack_from("<I", macho, 16)[0]
    command_offset = 32
    for _ in range(ncmds):
        if command_offset + 8 > len(macho):
            raise ValueError("truncated Mach-O load-command header")
        command, command_size = struct.unpack_from("<II", macho, command_offset)
        if command_size < 8 or command_offset + command_size > len(macho):
            raise ValueError("invalid Mach-O command size %#x" % command_size)
        if command == 0x1B and command_size >= 24:  # LC_UUID
            return str(uuid.UUID(bytes=bytes(macho[
                command_offset + 8:command_offset + 24]))).upper()
        command_offset += command_size
    return None


def parse_external_file_references(macho):
    """Inventory file-offset tables not covered by LC_SEGMENT_64 payloads.

    A stitched segment candidate is not a complete Mach-O if load commands
    refer to symbol/string or linkedit bytes beyond its file-backed segments.
    Return ranges as declared; do not relocate or synthesize those bytes.
    """
    if len(macho) < 32 or struct.unpack_from("<I", macho, 0)[0] != MACHO_MAGIC_64:
        raise ValueError("reference is not a little-endian 64-bit Mach-O")
    ncmds = struct.unpack_from("<I", macho, 16)[0]
    command_offset = 32
    references = []
    for _ in range(ncmds):
        if command_offset + 8 > len(macho):
            raise ValueError("truncated Mach-O load-command header")
        command, command_size = struct.unpack_from("<II", macho, command_offset)
        if command_size < 8 or command_offset + command_size > len(macho):
            raise ValueError("invalid Mach-O command size %#x" % command_size)
        if command == LC_SYMTAB:
            if command_size < 24:
                raise ValueError("short LC_SYMTAB")
            symoff, nsyms, stroff, strsize = struct.unpack_from(
                "<IIII", macho, command_offset + 8)
            # arm64 nlist_64 entries are 16 bytes.
            for label, offset, size in (
                    ("LC_SYMTAB.symbols", symoff, nsyms * 16),
                    ("LC_SYMTAB.strings", stroff, strsize)):
                if size:
                    references.append({"label": label, "offset": offset,
                                       "size": size, "end": offset + size})
        elif command == LC_DYSYMTAB:
            if command_size < 80:
                raise ValueError("short LC_DYSYMTAB")
            fields = struct.unpack_from("<18I", macho, command_offset + 8)
            # Fields 6..17 are the file offsets/counts for the auxiliary tables.
            offset_count_pairs = (
                ("LC_DYSYMTAB.toc", fields[6], fields[7], 8),
                ("LC_DYSYMTAB.module_table", fields[8], fields[9], 56),
                ("LC_DYSYMTAB.external_references", fields[10], fields[11], 4),
                ("LC_DYSYMTAB.indirect_symbols", fields[12], fields[13], 4),
                ("LC_DYSYMTAB.external_relocations", fields[14], fields[15], 8),
                ("LC_DYSYMTAB.local_relocations", fields[16], fields[17], 8),
            )
            for label, offset, count, entry_size in offset_count_pairs:
                size = count * entry_size
                if size:
                    references.append({"label": label, "offset": offset,
                                       "size": size, "end": offset + size})
        command_offset += command_size
    return references


def parse_nold_images(nold, bundle_offset=0):
    """Inventory embedded 64-bit Mach-O headers in a BUND nold range.

    The `nold` directory contains headers/load commands, not a complete
    standalone image. This records the declared segment layouts without
    guessing where each non-main image's bytes live in rtxt/rdat.
    """
    images = []
    seen = set()
    for offset in range(0, max(0, len(nold) - 31)):
        if struct.unpack_from("<I", nold, offset)[0] != MACHO_MAGIC_64:
            continue
        if offset in seen or offset + 32 > len(nold):
            continue
        filetype, ncmds, sizeofcmds = struct.unpack_from(
            "<III", nold, offset + 12)
        if ncmds == 0 or ncmds > 4096 or sizeofcmds < 8:
            continue
        command_end = offset + 32 + sizeofcmds
        if command_end > len(nold):
            continue
        header_and_commands = nold[offset:command_end]
        try:
            segments = parse_segments(header_and_commands)
        except ValueError:
            continue
        text_segments = [segment for segment in segments
                         if segment["name"] == "__TEXT"]
        if len(text_segments) != 1:
            continue
        seen.add(offset)
        file_end = max(segment["fileoff"] + segment["filesize"]
                       for segment in segments)
        data_segments = [segment for segment in segments
                         if segment["name"] != "__TEXT" and
                         segment["filesize"] > 0]
        data_start = min((segment["fileoff"] for segment in data_segments),
                         default=file_end)
        images.append({
            "header_bundle_offset": hex(bundle_offset + offset),
            "header_nold_offset": hex(offset),
            "uuid": parse_macho_uuid(header_and_commands),
            "filetype": filetype,
            "ncmds": ncmds,
            "sizeofcmds": hex(sizeofcmds),
            "segments": segments,
            "text_vmsize": hex(text_segments[0]["vmsize"]),
            "text_filesize": hex(text_segments[0]["filesize"]),
            "data_file_span": hex(max(0, file_end - data_start)),
            "data_segment_filesize_sum": hex(sum(
                segment["filesize"] for segment in data_segments)),
        })
    return images


def inventory_nold_payload_fit(ranges, images):
    """Compare aggregate range capacities; do not infer secondary offsets."""
    if not images:
        return {"status": "no-valid-nold-mach-o-headers"}
    # Match the pinned ipsw implementation, which selects the main image by
    # largest __TEXT vmsize. Payload capacity below uses file sizes.
    main = max(images, key=lambda image: int(image["text_vmsize"], 16))
    other = [image for image in images if image is not main]
    largest_other = max(other, key=lambda image: int(image["text_vmsize"], 16),
                        default=None)
    rtxt_size = ranges["rtxt"]["size"]
    rdat_size = ranges["rdat"]["size"]
    main_text_size = int(main["text_filesize"], 16)
    main_data_span = int(main["data_file_span"], 16)
    text_tail = max(0, rtxt_size - main_text_size)
    data_tail = max(0, rdat_size - main_data_span)
    other_text_size = (int(largest_other["text_filesize"], 16)
                       if largest_other else 0)
    other_data_span = (int(largest_other["data_file_span"], 16)
                       if largest_other else 0)
    return {
        "status": "capacity-comparison-only-secondary-offsets-unproven",
        "main_image_header_bundle_offset": main["header_bundle_offset"],
        "main_text_vmsize": main["text_vmsize"],
        "main_text_filesize": hex(main_text_size),
        "main_data_file_span": hex(main_data_span),
        "rtxt_range_size": hex(rtxt_size),
        "rdat_range_size": hex(rdat_size),
        "rtxt_bytes_after_main_text": hex(text_tail),
        "largest_other_image_header_bundle_offset": (
            largest_other["header_bundle_offset"] if largest_other else None),
        "largest_other_text_vmsize": (
            largest_other["text_vmsize"] if largest_other else None),
        "largest_other_text_filesize": hex(other_text_size),
        "largest_other_data_file_span": hex(other_data_span),
        "rtxt_shortfall_if_naively_concatenated": hex(
            max(0, other_text_size - text_tail)),
        "rdat_bytes_after_main_data_span": hex(data_tail),
        "rdat_slack_after_largest_other_span": hex(
            max(0, data_tail - other_data_span)),
        "secondary_mapping_claimed": False,
    }


def reconstruct_nold_secondary_candidate(bundle, ranges, images):
    """Build a labeled, unverified next-image concatenation candidate.

    The main-image offsets are grounded by the hash-validated ipsw extraction.
    The next largest nold image is placed after the main __TEXT and data span.
    This hypothesis is useful for inspection but is not an established BUND
    secondary-image mapping.
    """
    if len(images) < 2:
        return None, {"status": "fewer-than-two-nold-images"}
    main = max(images, key=lambda image: int(image["text_vmsize"], 16))
    others = [image for image in images if image is not main]
    candidate = max(others,
                    key=lambda image: int(image["text_vmsize"], 16))
    nold_offset = int(candidate["header_bundle_offset"], 16)
    command_size = int(candidate["sizeofcmds"], 16)
    header_size = 32 + command_size
    header = checked_slice(bundle, nold_offset, header_size,
                           "candidate nold header/load commands")
    segments = candidate["segments"]
    output_size = max(segment["fileoff"] + segment["filesize"]
                      for segment in segments)
    external_references = parse_external_file_references(header)
    uncovered_references = [
        dict(reference, candidate_covered=reference["end"] <= output_size)
        for reference in external_references
        if reference["end"] > output_size
    ]
    output = bytearray(output_size)
    output[:len(header)] = header

    rtxt = ranges["rtxt"]
    rdat = ranges["rdat"]
    text_source = rtxt["offset"] + int(main["text_filesize"], 16)
    data_source = rdat["offset"] + int(main["data_file_span"], 16)
    rtxt_cursor = text_source
    rdat_cursor = data_source
    source_map = []
    for segment in segments:
        if segment["filesize"] <= 0:
            continue
        if segment["name"] == "__TEXT":
            source_cursor = rtxt_cursor
            source_range = rtxt
            rtxt_cursor += segment["filesize"]
        else:
            source_cursor = rdat_cursor
            source_range = rdat
            rdat_cursor += segment["filesize"]
        relative = source_cursor - source_range["offset"]
        payload = checked_slice(
            bundle[source_range["offset"]:
                   source_range["offset"] + source_range["size"]],
            relative, segment["filesize"],
            "candidate %s bytes" % segment["name"])
        output[segment["fileoff"]:
               segment["fileoff"] + segment["filesize"]] = payload
        source_map.append({
            "segment": segment["name"],
            "bundle_source_offset": hex(source_cursor),
            "macho_file_offset": hex(segment["fileoff"]),
            "size": hex(segment["filesize"]),
        })

    const_segments = [segment for segment in segments
                      if segment["name"] == "__DATA_CONST" and
                      segment["filesize"] > 0]
    const_samples = []
    for segment in const_segments:
        data = bytes(output[segment["fileoff"]:
                            segment["fileoff"] + segment["filesize"]])
        const_samples.append({
            "macho_file_offset": hex(segment["fileoff"]),
            "size": hex(segment["filesize"]),
            "sha256": sha256(data),
            "nonzero_byte_count": sum(value != 0 for value in data),
        })
    report = {
        "status": (
            "conditional-next-largest-nold-image-segment-candidate-incomplete-file-references"
            if uncovered_references else
            "conditional-next-largest-nold-image-concatenation-candidate"),
        "candidate_is_complete_macho": not uncovered_references,
        "secondary_mapping_claimed": False,
        "main_image_header_bundle_offset": main["header_bundle_offset"],
        "candidate_image_header_bundle_offset": candidate[
            "header_bundle_offset"],
        "candidate_output_size": hex(output_size),
        "candidate_output_sha256": sha256(bytes(output)),
        "external_file_references": external_references,
        "uncovered_file_references": uncovered_references,
        "candidate_rtxt_start": hex(text_source),
        "candidate_rdat_start": hex(data_source),
        "rtxt_end": hex(rtxt["offset"] + rtxt["size"]),
        "rdat_end": hex(rdat["offset"] + rdat["size"]),
        "source_map": source_map,
        "candidate_data_const_ranges": const_samples,
    }
    return bytes(output), report


def sample_virtual_address(macho, segments, address, size=32):
    """Map a VM address through its containing segment without reading BSS as file data."""
    if address < 0 or size <= 0:
        raise ValueError("virtual-address sample needs a nonnegative address and positive size")
    matches = [segment for segment in segments
               if segment["vmaddr"] <= address < segment["vmaddr"] + segment["vmsize"]]
    if len(matches) != 1:
        raise ValueError("virtual address %#x has %d containing segments" %
                         (address, len(matches)))
    segment = matches[0]
    delta = address - segment["vmaddr"]
    if delta + size > segment["vmsize"]:
        raise ValueError("virtual-address sample %#x+%#x crosses %s vmsize" %
                         (address, size, segment["name"]))

    file_backed = min(size, max(0, segment["filesize"] - delta))
    if file_backed:
        file_offset = segment["fileoff"] + delta
        file_bytes = checked_slice(macho, file_offset, file_backed,
                                   "virtual-address sample %s" % segment["name"])
    else:
        file_offset = None
        file_bytes = b""
    zero_fill = size - file_backed
    storage = ("file-backed" if zero_fill == 0 else
               "zero-fill" if file_backed == 0 else
               "file-backed+zero-fill")
    return {
        "virtual_address": hex(address),
        "size": hex(size),
        "segment": segment["name"],
        "storage": storage,
        "macho_file_offset": None if file_offset is None else hex(file_offset),
        "file_backed_size": hex(file_backed),
        "zero_fill_size": hex(zero_fill),
        "bytes": (file_bytes + b"\0" * zero_fill).hex(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--reference-macho", required=True, type=Path,
                        help="extracted Mach-O used for header/load commands")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--expected-bundle-sha256")
    parser.add_argument("--expected-reference-sha256")
    parser.add_argument("--sample-vaddr", action="append", default=[],
                        help="also report bytes at this reconstructed Mach-O VM address (repeatable)")
    parser.add_argument("--sample-size", type=lambda value: int(value, 0), default=32,
                        help="byte count for each --sample-vaddr sample (default: 32)")
    parser.add_argument("--secondary-candidate-output", type=Path,
                        help="write an explicitly unverified next-nold-image reconstruction candidate")
    args = parser.parse_args()

    bundle = args.bundle.read_bytes()
    reference = args.reference_macho.read_bytes()
    bundle_hash = sha256(bundle)
    reference_hash = sha256(reference)
    if args.expected_bundle_sha256 and bundle_hash != args.expected_bundle_sha256:
        raise SystemExit("unexpected bundle SHA-256: " + bundle_hash)
    if args.expected_reference_sha256 and reference_hash != args.expected_reference_sha256:
        raise SystemExit("unexpected reference SHA-256: " + reference_hash)

    ranges = parse_type4_ranges(bundle)
    segments = parse_segments(reference)
    nold = ranges.get("nold")
    nold_images = []
    nold_fit = None
    if nold is not None:
        nold_blob = checked_slice(bundle, nold["offset"], nold["size"],
                                  "BUND range nold")
        nold_images = parse_nold_images(nold_blob, nold["offset"])
        nold_fit = inventory_nold_payload_fit(ranges, nold_images)
    secondary_candidate_bytes = None
    secondary_candidate_report = None
    if nold_images:
        secondary_candidate_bytes, secondary_candidate_report = (
            reconstruct_nold_secondary_candidate(bundle, ranges, nold_images))
        if args.secondary_candidate_output and secondary_candidate_bytes:
            args.secondary_candidate_output.parent.mkdir(
                parents=True, exist_ok=True)
            args.secondary_candidate_output.write_bytes(
                secondary_candidate_bytes)
            secondary_candidate_report["candidate_output_path"] = str(
                args.secondary_candidate_output)
    text_segment = segments[0]
    data_start = segments[1]["fileoff"]
    output_size = max(s["fileoff"] + s["filesize"] for s in segments)
    if output_size != len(reference):
        raise ValueError("reference length %#x != max segment end %#x" %
                         (len(reference), output_size))
    text_source = ranges["rtxt"]["offset"]
    data_source = ranges["rdat"]["offset"]
    text_size = text_segment["filesize"]
    data_size = output_size - data_start
    text_bytes = checked_slice(bundle, text_source, text_size, "rtxt payload")
    data_bytes = checked_slice(bundle, data_source, data_size, "rdat payload")

    reconstructed = bytearray(reference)
    reconstructed[text_segment["fileoff"]:
                  text_segment["fileoff"] + text_size] = text_bytes
    reconstructed[data_start:data_start + data_size] = data_bytes
    virtual_samples = [sample_virtual_address(
        reconstructed, segments, int(value, 0), args.sample_size)
        for value in args.sample_vaddr]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(reconstructed)

    samples = []
    for label, file_offset in (("data_const_start", 0x952000),
                               ("selector1_vptr", 0x98A6B8),
                               ("selector1_vptr_plus_0x130", 0x98A7E8),
                               ("mod_init_func", 0x9A6350)):
        if file_offset + 16 <= output_size:
            source_offset = data_source + (file_offset - data_start)
            samples.append({
                "label": label,
                "macho_file_offset": hex(file_offset),
                "bundle_rdat_offset": hex(source_offset),
                "bytes": reconstructed[file_offset:file_offset + 16].hex(),
            })

    report = {
        "bundle_path": str(args.bundle),
        "bundle_size": len(bundle),
        "bundle_sha256": bundle_hash,
        "reference_macho_path": str(args.reference_macho),
        "reference_macho_sha256": reference_hash,
        "output_path": str(args.output),
        "output_size": len(reconstructed),
        "output_sha256": sha256(reconstructed),
        "type4_ranges": ranges,
        "macho_segments": segments,
        "mapping": {
            "rtxt_bundle_start": hex(text_source),
            "rtxt_macho_fileoff": hex(text_segment["fileoff"]),
            "rtxt_size": hex(text_size),
            "rdat_bundle_start": hex(data_source),
            "rdat_macho_fileoff": hex(data_start),
            "rdat_copied_size": hex(data_size),
        },
        "address_samples": samples,
        "virtual_address_samples": virtual_samples,
        "nold_images": nold_images,
        "nold_range_capacity_comparison": nold_fit,
        "nold_secondary_candidate": secondary_candidate_report,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
