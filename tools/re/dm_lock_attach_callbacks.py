"""Read-only PosterBoard attachment bracket, with the existing draw hooks.

Score poster drawing only after this boot has a Mercury root and matching
SpringBoard hosted scene. These extra sites distinguish an unarmed poster
controller from a scene-settings callback that fails to attach the host.
"""

import json
import time
from pathlib import Path

import dm_lock_draw_callbacks as draw
import lldb

import bks_checkin_callbacks as bks
from dm_plugin_lifecycle_callbacks import _read


SITES = (
    (0x201F36DE8, "POSTER_CONTROLLER_INIT"),
    (0x201F3B3D8, "POSTER_CONTROLLER_VIEW_DID_LOAD"),
    # updateConfiguration: block obtains the rendering-role scene in x20,
    # then strong-stores it through the controller's _scene ivar address.
    # These are ready for the next child; LOCK_ATTACH1 did not arm them.
    (0x201F37E68, "POSTER_CONFIG_BLOCK"),
    (0x201F37E98, "POSTER_CONFIG_CONTROLLER_GATE"),
    # 0x201f37ed8 copies the returned scene from x0 into x20; stop after it.
    (0x201F37EDC, "POSTER_CONFIG_SCENE_RESOLVED"),
    (0x201F37FDC, "POSTER_CONFIG_SCENE_STORE"),
    (0x201ED15A0, "POSTER_SET_ACTIVE_REQUIRED"),
    (0x201ED1694, "POSTER_SET_ACTIVE_REASONS"),
    (0x201ED190C, "POSTER_UPDATE_SCENES"),
    # x8 is the loaded _scene pointer immediately before cbz to 0x201ed1a0c.
    (0x201ED1964, "POSTER_SCENE_NULL_GATE"),
    (0x201ED1968, "POSTER_SCENE_PRESENT_ARM"),
    (0x201ED1A0C, "POSTER_SCENE_NULL_ARM"),
    (0x201ED28E0, "POSTER_SCENE_WILL_UPDATE_SETTINGS"),
    (0x201EDAE80, "POSTER_SCENE_DID_UPDATE_CLIENT_SETTINGS"),
    (0x201EDF25C, "POSTER_SCENE_UPDATE_LAYERS_BLOCK"),
    (0x201EE0C48, "POSTER_HOST_SET_CONTEXT_SCENE"),
)
META = {}
COUNTS = {}


def _ptr(process, address):
    if not address:
        return None
    raw = _read(process, address, 8)
    return int.from_bytes(raw, "little") if len(raw) == 8 else None


def on_hit(frame, bp_loc, _dict):
    bp = bp_loc.GetBreakpoint()
    label = META[bp.GetID()]
    process = frame.GetThread().GetProcess()
    if bks._progname(process) != "SpringBoard":
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    # Do not remove the breakpoint: a late positive scene must still be seen.
    if count > 64:
        return False
    row = {"label": label, "count": count, "time": time.time(),
           "thread": bks._reg(frame, "tpidr_el1"),
           "pc": bks._reg(frame, "pc"), "lr": bks._reg(frame, "lr"),
           "x0": bks._reg(frame, "x0"), "x1": bks._reg(frame, "x1"),
           "x2": bks._reg(frame, "x2")}
    if label == "POSTER_SCENE_NULL_GATE":
        row["controller"] = bks._reg(frame, "x21")
        row["scene"] = bks._reg(frame, "x8")
        row["null_branch"] = row["scene"] == 0
    elif label == "POSTER_SET_ACTIVE_REQUIRED":
        row["requested"] = row["x2"] & 0xFF
    elif label == "POSTER_CONFIG_CONTROLLER_GATE":
        row["controller"] = bks._reg(frame, "x19")
        row["controller_present"] = bks._reg(frame, "x0") != 0
    elif label == "POSTER_CONFIG_SCENE_RESOLVED":
        row["controller"] = bks._reg(frame, "x19")
        row["candidate_scene"] = bks._reg(frame, "x20")
    elif label == "POSTER_CONFIG_SCENE_STORE":
        row["controller"] = bks._reg(frame, "x19")
        row["scene_field"] = bks._reg(frame, "x22")
        row["scene_before"] = _ptr(process, row["scene_field"])
        row["candidate_scene"] = bks._reg(frame, "x20")
    elif label == "POSTER_HOST_SET_CONTEXT_SCENE":
        row["context_before"] = int.from_bytes(
            _read(process, row["x0"] + 0x1A8, 4), "little")
        row["scene_before"] = _ptr(process, row["x0"] + 0x1B8)
        row["context_arg"] = row["x2"]
        row["scene_arg"] = bks._reg(frame, "x3")
    print("DM_LOCK_ATTACH_JSON " + json.dumps(row, sort_keys=True), flush=True)
    return False


def install(debugger, slide):
    # LLDB must know the rich callback module by command-import, not merely
    # Python import. draw.install performs that import before arming its sites.
    # LOCK_CONFIG1/2 lost LLDB at an unrelated hosted hidden-cull site before
    # any poster join. Those sites are unnecessary for the attachment decision;
    # retain the other post-accept sites for a positive-context draw sample.
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % Path(draw.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("draw callback import failed: %s" % result.GetError())
    # command script import reloads the module and resets KEEP; filter after it.
    draw.KEEP.difference_update({"SERVER_HOSTED_HIDDEN_CULL_RESULT",
                                 "SERVER_HOSTED_HIDDEN_CULLED"})
    draw.install(debugger, slide)
    if any(label in ("SERVER_HOSTED_HIDDEN_CULL_RESULT",
                     "SERVER_HOSTED_HIDDEN_CULLED")
           for _, label, _ in draw.rich.SITES):
        raise RuntimeError("unrelated hosted hidden-cull breakpoint still armed")
    # These draw hooks only log. Auto-continue prevents an unrelated server
    # breakpoint from ending a long attachment run if its callback misfires.
    target = debugger.GetSelectedTarget()
    for index in range(target.GetNumBreakpoints()):
        bp = target.GetBreakpointAtIndex(index)
        if bp.GetID() in draw.rich.META:
            bp.SetAutoContinue(True)
    META.clear()
    COUNTS.clear()
    for static, label in SITES:
        bp = target.BreakpointCreateByAddress(int(slide) + static)
        if not bp.IsValid():
            raise RuntimeError("could not arm %s" % label)
        META[bp.GetID()] = label
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (bp.GetID(), label, int(slide) + static), flush=True)
    print("DM_LOCK_ATTACH_READY sites=%d" % len(SITES), flush=True)
