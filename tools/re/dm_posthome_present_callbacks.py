"""Read-only post-home CA commit to first IOMFB handoff ladder.

The repaired-parent ADFL/home probe supplies the controls. These extra sites
are logged only after a non-null SBHomeScreenEntity init return; unrelated
early CA traffic does not count as post-home render progress.
"""

import json
import os
import time
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks
import dm_post_shell_home_min_callbacks as home
import first_surface_callbacks as surface_capture


USER_SITES = (
    # Setup's welcome UI has its own scene lifecycle.  Include its commits so
    # a visible client-side transaction can be distinguished from SB-only CA
    # activity when the display server receives no Update.
    (0x1843FF180, "CA_COMMIT_ENTRY", ("SpringBoard", "Setup")),
    (0x184776174, "SERVER_RENDER_UPDATE_ENTRY", "backboardd"),
    (0x1844510F0, "DISPLAY_RENDER_DISPLAY_ENTRY", "backboardd"),
    (0x18444B954, "SERVER_OGL_RENDER", "backboardd"),
    (0x18446BB14, "FINISH_UPDATE_ENTRY", "backboardd"),
    (0x18446CA68, "FINISH_UPDATE_SURFACE_BL", "backboardd"),
    (0x18446D0C0, "FINISH_SWAP_X22", "backboardd"),
    (0x18446D0CC, "FINISH_SWAP_X22_RETURN", "backboardd"),
    (0x1847AE2CC, "CREATE_SURFACE_ENTRY", "backboardd"),
    (0x1847AE314, "ALLOCATE_IOSURFACE_RETURN", "backboardd"),
    (0x18447238C, "FB_SWAP_SET_LAYER_ENTRY", "backboardd"),
    (0x184472488, "FB_SWAP_HANDOFF", "backboardd"),
)
KERNEL_SITES = ((0xFFFFFFF00A0C4664, "IOMFB_A408_BUILD"),)
KERNEL_SLIDE = 0x20000000
META = {}
ARMED = set()
COUNTS = {}
SURFACE_SAMPLES = 0
# Keep several post-shell swaps so a later ordinary touch transition can be
# observed without restarting LLDB. Captures remain read-only and bounded.
MAX_SURFACE_SAMPLES = 16
START_FILE = ""
GATED_SURFACE_SAMPLES = 0


def _register(debugger, address, label, expected, thread=0):
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed %s: %s" % (label, result.GetError()))
    bp = target.GetBreakpointAtIndex(before)
    META[bp.GetID()] = label, expected, thread
    bp.SetAutoContinue(True)
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand(
        "breakpoint command add -F dm_posthome_present_callbacks.on_hit %d" %
        bp.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("callback failure %s: %s" % (label, result.GetError()))
    proof = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), proof)
    proof_text = proof.GetOutput() or proof.GetError() or ""
    callback_name = "dm_posthome_present_callbacks.on_hit"
    if (not proof.Succeeded() or callback_name not in proof_text or
            bp.GetNumLocations() < 1):
        bp.SetEnabled(False)
        raise RuntimeError("callback/location proof failed for %s: %s" %
                           (label, proof_text))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
          (bp.GetID(), label, address), flush=True)
    return bp


def _arm_return(frame, label, expected, thread):
    address = bks._reg(frame, "lr")
    if not address:
        print("POSTHOME_RETURN_UNARMED label=%s reason=null-lr" % label,
              flush=True)
        return
    key = label, expected, thread, address
    if key in ARMED:
        return
    ARMED.add(key)
    debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
    _register(debugger, address, label, expected, thread)


def on_hit(frame, bp_loc, _context):
    global SURFACE_SAMPLES, GATED_SURFACE_SAMPLES
    bp = bp_loc.GetBreakpoint()
    label, expected, thread_filter = META[bp.GetID()]
    if not home.HOME_RETURNED:
        return False
    process = frame.GetThread().GetProcess()
    name = bks._progname(process)
    if expected != "kernel" and name not in (
            expected if isinstance(expected, tuple) else (expected,)):
        return False
    thread = bks._reg(frame, "tpidr_el1")
    if thread_filter and thread != thread_filter:
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    # Swap calls are commonly a batch of null-surface layer-state updates
    # before a real IOSurface arrives. Keep observing well beyond that batch;
    # a small fixed cap made P105's later surface status unknowable.
    limit = (256 if label in ("FB_SWAP_SET_LAYER_ENTRY", "FB_SWAP_HANDOFF")
             else 512 if label == "CA_COMMIT_ENTRY" else 32)
    if count > limit:
        bp.SetEnabled(False)
        return False
    row = {
        "label": label, "count": count, "time": time.time(),
        "process": name, "thread": thread, "pc": bks._reg(frame, "pc"),
        "lr": bks._reg(frame, "lr"), "sp": bks._reg(frame, "sp"),
        **{"x%d" % index: bks._reg(frame, "x%d" % index)
           for index in range(9)},
    }
    # At fb_swap_set_layer x1 is the framebuffer/layer reference. Preserve a
    # small read-only header when it is readable so successive null updates
    # can be distinguished by object state, without guessing pixel contents.
    if label == "FB_SWAP_SET_LAYER_ENTRY" and row.get("x1"):
        words = []
        for offset in range(0, 0x40, 8):
            words.append(surface_capture._u64(process, row["x1"] + offset))
        row["x1_words_0x00_0x38"] = words
    print("POSTHOME_PRESENT_JSON " + json.dumps(row, sort_keys=True),
          flush=True)
    # fb_swap_set_layer receives the IOSurface in x3 (confirmed by the
    # first-surface call-site analysis); FB_SWAP_HANDOFF is a later stub whose
    # x2 is often null or frame state. Sample the actual surface before the
    # handoff, without modifying guest memory or display state.
    if (label == "FB_SWAP_SET_LAYER_ENTRY" and START_FILE and
            not Path(START_FILE).exists()):
        GATED_SURFACE_SAMPLES += 1
        if GATED_SURFACE_SAMPLES in (1, 16, 128):
            print("POSTHOME_SURFACE_CAPTURE_GATED count=%d marker=%s" %
                  (GATED_SURFACE_SAMPLES, START_FILE), flush=True)
    if (label == "FB_SWAP_SET_LAYER_ENTRY" and
            (not START_FILE or Path(START_FILE).exists()) and
            SURFACE_SAMPLES < MAX_SURFACE_SAMPLES):
        iosurface = row.get("x3")
        if isinstance(iosurface, int) and iosurface > 0x10000:
            SURFACE_SAMPLES += 1
            try:
                metadata = surface_capture._surface_metadata(process, iosurface)
                capture = surface_capture._capture_surface(
                    process, iosurface, metadata, SURFACE_SAMPLES)
            except Exception as error:
                metadata = {"metadata_error": repr(error)}
                capture = {"capture_error": repr(error)}
            surface_row = {
                "label": "POSTHOME_IOSURFACE_PIXELS",
                "sample": SURFACE_SAMPLES,
                "time": time.time(),
                "process": name,
                "thread": thread,
                "surface": iosurface,
                "surface_metadata": metadata,
                "surface_capture": capture,
            }
            surface_capture._write_event(
                "posthome-iosurface-%02d.json" % SURFACE_SAMPLES,
                surface_row)
            print("POSTHOME_IOSURFACE_JSON " +
                  json.dumps(surface_row, sort_keys=True), flush=True)
    if label == "CA_COMMIT_ENTRY":
        _arm_return(frame, "CA_COMMIT_RETURN", expected, thread)
    elif label == "SERVER_RENDER_UPDATE_ENTRY":
        _arm_return(frame, "SERVER_RENDER_UPDATE_RETURN", expected, thread)
    elif label == "DISPLAY_RENDER_DISPLAY_ENTRY":
        _arm_return(frame, "DISPLAY_RENDER_DISPLAY_RETURN", expected, thread)
    elif label == "SERVER_OGL_RENDER":
        _arm_return(frame, "SERVER_OGL_RENDER_RETURN", expected, thread)
    elif label == "FINISH_UPDATE_ENTRY":
        _arm_return(frame, "FINISH_UPDATE_RETURN", expected, thread)
    elif label == "CREATE_SURFACE_ENTRY":
        _arm_return(frame, "CREATE_SURFACE_RETURN", expected, thread)
    elif label.endswith("_RETURN") and thread_filter:
        bp.SetEnabled(False)
    if label == "IOMFB_A408_BUILD":
        print("POSTHOME_A408_BUILD reached; outbound RPC/presentation unproved",
              flush=True)
    return False


def install(debugger, slide, install_home=True):
    global SURFACE_SAMPLES, START_FILE, GATED_SURFACE_SAMPLES
    interpreter = debugger.GetCommandInterpreter()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("command script import %s" %
                              Path(home.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("failed home import: %s" % result.GetError())
    if install_home:
        home.install(debugger, slide)
    META.clear()
    ARMED.clear()
    COUNTS.clear()
    SURFACE_SAMPLES = 0
    GATED_SURFACE_SAMPLES = 0
    START_FILE = os.environ.get("DVM_POSTHOME_PRESENT_START_FILE", "")
    surface_capture.SLIDE[0] = int(slide)
    surface_capture.EVENT_DIR = os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/posthome-surface-events")
    armed = []
    for static, label, expected in USER_SITES:
        bp = _register(debugger, int(slide) + static, label, expected)
        armed.append({"id": bp.GetID(), "label": label,
                      "address": "0x%x" % (int(slide) + static),
                      "locations": bp.GetNumLocations(),
                      "callback": "dm_posthome_present_callbacks.on_hit"})
    for static, label in KERNEL_SITES:
        bp = _register(debugger, KERNEL_SLIDE + static, label, "kernel")
        armed.append({"id": bp.GetID(), "label": label,
                      "address": "0x%x" % (KERNEL_SLIDE + static),
                      "locations": bp.GetNumLocations(),
                      "callback": "dm_posthome_present_callbacks.on_hit"})
    print("POSTHOME_PRESENT_READY " + json.dumps(
        {"sites": len(armed), "armed": armed, "slide": "0x%x" % int(slide),
         "surface_capture_start_file": START_FILE,
         "surface_capture_gated": bool(START_FILE)},
        sort_keys=True), flush=True)
