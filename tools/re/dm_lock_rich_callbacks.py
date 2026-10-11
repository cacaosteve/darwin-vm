"""Read-only lock content sources alongside the established A407/Wait bracket.

The SpringBoard time and PosterBoard wallpaper methods are sampled from boot,
because those views can be created before the first post-home A407 present.
No return value or guest state is modified.
"""

import json
import time
from collections import deque
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks
import dm_posthome_swap_branch_callbacks as swap
import dm_repaired_ls_adfl_home_callbacks as home
from dm_plugin_lifecycle_callbacks import _read


SITES = (
    (0x1B38D1E78, "TIME_CREATE_OR_DESTROY", "SpringBoard"),
    (0x1B38D21AC, "TIME_INIT", "SpringBoard"),
    (0x1B38BD1A8, "TIME_DISPLAY_DATE_UPDATE", "SpringBoard"),
    (0x1B38BD310, "TIME_STRING_UPDATE", "SpringBoard"),
    (0x1B38BE410, "TIME_ADAPTER_ATTRIBUTED_TEXT", "SpringBoard"),
    (0x1B38D4B4C, "TIME_ADAPTER_GLASS", "SpringBoard"),
    (0x1B38D67F0, "TIME_GLASS_CAPABILITY", "SpringBoard"),
    (0x223E8B60C, "COVER_DATE_TIME_UPDATE", "SpringBoard"),
    (0x223E921D8, "COVER_WALLPAPER_UPDATE", "SpringBoard"),
    (0x201EDEF20, "POSTER_LAYERS_UPDATE", "SpringBoard"),
    (0x201EE0EE8, "POSTER_HOST_SET_SCENE", "SpringBoard"),
    (0x201EE256C, "STATIC_WALLPAPER_IMAGE", ""),
    (0x201EE8D84, "WALLPAPER_CONFIG_IMAGE", ""),
    (0x18451A138, "CA_CONTEXT_SET_LAYER", ""),
    (0x1843FF180, "CA_COMMIT", "SpringBoard"),
    (0x184776174, "SERVER_RENDER_UPDATE", "backboardd"),
    # Natural display contexts can be added directly by render_for_time or
    # through a nested LayerHost in Updater::prepare_layer0. Keep both paths.
    (0x1844130C0, "SERVER_DISPLAY_CONTEXT_ID", "backboardd"),
    (0x184410B50, "SERVER_DISPLAY_ADD_WRAPPER", "backboardd"),
    (0x1844131F8, "SERVER_DISPLAY_ADD_ROOT", "backboardd"),
    # Updater::prepare_layer0 resolves a nested LayerHost before the
    # add_included_context and secure/policy checks below. Log this lookup
    # even when the client-side poster ID has not yet been observed.
    (0x18441EC84, "SERVER_HOSTED_RESOLVE_CALL", "backboardd"),
    (0x18441EC88, "SERVER_HOSTED_RESOLVE_RETURN", "backboardd"),
    (0x18441EC9C, "SERVER_HOSTED_POSTER_INCLUDE", "backboardd"),
    (0x18441ECCC, "SERVER_HOSTED_SECURE_RESULT", "backboardd"),
    (0x18441ECDC, "SERVER_HOSTED_POLICY_RESULT", "backboardd"),
    (0x18441ECE4, "SERVER_HOSTED_POSTER_ACCEPT", "backboardd"),
    (0x18441ED14, "SERVER_HOSTED_POSTER_REJECT", "backboardd"),
    # Same prepare_layer0 invocation after the hosted-context accept. These
    # sites expose the later context/layer policy and LocalState0; none is a
    # pixel sample by itself. The older 0x184440e60 hook is a different path.
    (0x18441F658, "SERVER_HOSTED_POST_ACCEPT", "backboardd"),
    (0x18441FDF8, "SERVER_HOSTED_POSTER_LAYER_GATE", "backboardd"),
    (0x18441FE80, "SERVER_HOSTED_ALLOWED_UPDATE", "backboardd"),
    (0x18441FEAC, "SERVER_HOSTED_HIDDEN_CULL_RESULT", "backboardd"),
    (0x1844218C4, "SERVER_HOSTED_HIDDEN_CULLED", "backboardd"),
    (0x18441FF28, "SERVER_HOSTED_PREPARED_STATE", "backboardd"),
    (0x184440E60, "SERVER_POSTER_PREPARED_RESULT", "backboardd"),
    (0x1844C0268, "TIME_COPY_RENDER_LAYER", "SpringBoard"),
    (0x1844C02E0, "TIME_RENDER_LAYER_ALLOCATED", "SpringBoard"),
    (0x1844C061C, "TIME_RENDER_CONTENTS_RESULT", "SpringBoard"),
    (0x1844510F0, "SERVER_DISPLAY_RENDER", "backboardd"),
    (0x18444B954, "SERVER_OGL_RENDER", "backboardd"),
    # OGL::Renderer::alloc_layer stores Render::LayerNode* at OGL Layer+0x10
    # (0x184466304). Use that identity, not Update* reuse, for draw attribution.
    (0x1844664F8, "SERVER_OGL_PREPARE_LAYER", "backboardd"),
    (0x1844662B0, "SERVER_OGL_ALLOC_LAYER", "backboardd"),
    (0x18448B62C, "SERVER_OGL_RENDER_LAYERS", "backboardd"),
    (0x1844881F4, "SERVER_OGL_RENDER_CONTENTS", "backboardd"),
    # Current QuartzCore: Update::add_context receives Context* in x1;
    # add_included_context at 0x1844400c4 is reached only after the
    # secure/include/update checks. Caller LR distinguishes the display
    # update from render_display_context_list_to_target's offscreen update;
    # inclusion in either is not evidence of opaque compositor pixels.
    (0x18443FFB8, "SERVER_ADD_CONTEXT", "backboardd"),
    (0x18444008C, "SERVER_ALLOWED_CONTEXT_RESULT", "backboardd"),
    (0x1844400B4, "SERVER_ALLOWED_UPDATE_RESULT", "backboardd"),
    (0x1844400C4, "SERVER_ADD_INCLUDED_CONTEXT", "backboardd"),
    (0x1844400D8, "SERVER_ADD_CONTEXT_REJECT", "backboardd"),
    (0x18444979C, "SERVER_UPDATE_HANDLE", "backboardd"),
)
META = {}
COUNTS = {}
PENDING = {}
POSTER_CONTEXTS = set()
RECENT_CA_LAYERS = deque(maxlen=128)
TIME_LAYERS = set()
DISPLAY_UPDATES = set()
DISPLAY_CONTEXT_IDS = {}
HOSTED_RESOLVES = {}
HOSTED_ACCEPTS = {}
TIME_COPIES = {}
POSTER_NODES = {}


def _pointer(process, address):
    if not address:
        return None
    raw = _read(process, address, 8)
    return int.from_bytes(raw, "little") if len(raw) == 8 else None


def _u32(process, address):
    if not address:
        return None
    raw = _read(process, address, 4)
    return int.from_bytes(raw, "little") if len(raw) == 4 else None


def _layer_state(process, view):
    """Read existing UIView/CALayer fields; do not message the guest object.

    UIKitCore UIView._layer is +0xb0. QuartzCore CALayer's inner CA::Layer
    is +0x10; isHidden reads inner+0x30 bit 7 at 0x1843e2e48, while
    contents reads inner+0x98 at 0x184469cdc. Nil contents can still mean
    delegate-drawn text or a remote context, so it is not a blank verdict.
    """
    layer = _pointer(process, view + 0xB0) if view else None
    inner = _pointer(process, layer + 0x10) if layer else None
    flags = _read(process, inner + 0x30, 1) if inner else b""
    return {"layer": layer, "inner": inner,
            "hidden": bool(flags[0] & 0x80) if flags else None,
            "contents": _pointer(process, inner + 0x98) if inner else None}


def _register(debugger, address, label, process_name="", thread=0):
    target = debugger.GetSelectedTarget()
    before = target.GetNumBreakpoints()
    bp = target.BreakpointCreateByAddress(address)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("could not arm %s at 0x%x" % (label, address))
    META[bp.GetID()] = (label, process_name, thread)
    bp.SetScriptCallbackFunction(__name__ + ".on_hit")
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
          (bp.GetID(), label, address), flush=True)
    return bp


def on_hit(frame, bp_loc, _dict):
    bp = bp_loc.GetBreakpoint()
    label, expected_process, expected_thread = META[bp.GetID()]
    process = frame.GetThread().GetProcess()
    name = bks._progname(process)
    if expected_process and name != expected_process:
        return False
    thread = bks._reg(frame, "tpidr_el1")
    if expected_thread and thread != expected_thread:
        return False
    if label == "CA_CONTEXT_SET_LAYER" and not (
            name in ("SpringBoard", "ClockPosterExtension", "PosterBoard")
            or name.startswith("MercuryPoster")
            or name.startswith("ClockPoster")):
        return False
    if label == "TIME_COPY_RENDER_LAYER":
        if bks._reg(frame, "x0") not in TIME_LAYERS:
            return False
        TIME_COPIES[thread] = bks._reg(frame, "x0")
    if label in ("TIME_RENDER_LAYER_ALLOCATED", "TIME_RENDER_CONTENTS_RESULT"):
        if thread not in TIME_COPIES:
            return False
        if bks._reg(frame, "x22") != TIME_COPIES[thread]:
            return False
    if label.startswith("SERVER_HOSTED_POST_") or label in (
            "SERVER_HOSTED_POSTER_LAYER_GATE", "SERVER_HOSTED_ALLOWED_UPDATE",
            "SERVER_HOSTED_HIDDEN_CULL_RESULT", "SERVER_HOSTED_HIDDEN_CULLED",
            "SERVER_HOSTED_PREPARED_STATE"):
        accepted = HOSTED_ACCEPTS.get(thread)
        if not accepted or bks._reg(frame, "x19") != accepted["local_state"]:
            return False
    if label.startswith("SERVER_") and not home.HOME_RETURNED:
        return False
    process = frame.GetThread().GetProcess()
    if label == "SERVER_DISPLAY_CONTEXT_ID":
        # render_for_time reads this u32 before Context::context_by_id.
        context_id = _u32(process, bks._reg(frame, "x23"))
        update = bks._reg(frame, "x19") + 0xB30
        ids = DISPLAY_CONTEXT_IDS.setdefault(update, set())
        if context_id is not None:
            ids.add(context_id)
        if context_id not in POSTER_CONTEXTS:
            return False
    if label in ("SERVER_DISPLAY_ADD_WRAPPER", "SERVER_DISPLAY_ADD_ROOT",
                 "SERVER_HOSTED_POSTER_INCLUDE", "SERVER_ADD_CONTEXT",
                 "SERVER_ADD_INCLUDED_CONTEXT", "SERVER_ALLOWED_CONTEXT_RESULT",
                 "SERVER_ALLOWED_UPDATE_RESULT", "SERVER_ADD_CONTEXT_REJECT",
                 "SERVER_HOSTED_SECURE_RESULT", "SERVER_HOSTED_POLICY_RESULT",
                 "SERVER_HOSTED_POSTER_ACCEPT", "SERVER_HOSTED_POSTER_REJECT",
                 "SERVER_POSTER_PREPARED_RESULT"):
        if label in ("SERVER_ALLOWED_CONTEXT_RESULT", "SERVER_ALLOWED_UPDATE_RESULT",
                     "SERVER_ADD_CONTEXT_REJECT", "SERVER_POSTER_PREPARED_RESULT"):
            context = bks._reg(frame, "x20")
        elif label.startswith("SERVER_HOSTED_"):
            context = bks._reg(frame, "x1") if label == "SERVER_HOSTED_POSTER_INCLUDE" else bks._reg(frame, "x26")
        else:
            context = bks._reg(frame, "x1")
        context_id = _u32(process, context + 0x10)
        if context_id not in POSTER_CONTEXTS:
            return False
    if label == "SERVER_OGL_RENDER" and bks._reg(frame, "x1") not in DISPLAY_UPDATES:
        return False
    if label in ("SERVER_OGL_PREPARE_LAYER", "SERVER_OGL_ALLOC_LAYER"):
        if bks._reg(frame, "x1") not in POSTER_NODES:
            return False
    if label in ("SERVER_OGL_RENDER_LAYERS", "SERVER_OGL_RENDER_CONTENTS"):
        layer = bks._reg(frame, "x1")
        if _pointer(process, layer + 0x10) not in POSTER_NODES:
            return False
    if label == "SERVER_UPDATE_HANDLE":
        # CA::Render::Context stores its generated ID at +0x10
        # (QuartzCore 0x184518b5c). This is a context participation marker,
        # not proof that the layer contributed non-black pixels.
        context_id = _u32(process, bks._reg(frame, "x2") + 0x10)
        if context_id not in POSTER_CONTEXTS:
            return False
    hosted = None
    if label == "SERVER_HOSTED_RESOLVE_CALL":
        host = bks._reg(frame, "x19") + 0x210
        hosted = (host, _u32(process, host + 0x1C), bks._reg(frame, "x23"))
        HOSTED_RESOLVES[thread] = hosted
    elif label == "SERVER_HOSTED_RESOLVE_RETURN":
        hosted = HOSTED_RESOLVES.pop(thread, (None, None, None))
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if hosted is not None and label == "SERVER_HOSTED_RESOLVE_CALL" and count > 8:
        # The host's own ID at +0x1c is not the resolved CAContext ID. Do not
        # use it to decide whether this resolver is poster-related: the
        # relevant ID is only available at RESOLVE_RETURN from host->context.
        # Keep the first eight call rows as controls; continue tracking later
        # calls so their paired returns can be scored against POSTER_CONTEXTS.
        return False
    limit = (512 if label in ("SERVER_RENDER_UPDATE", "SERVER_DISPLAY_RENDER",
                              "SERVER_OGL_RENDER") else
             128 if label in ("CA_CONTEXT_SET_LAYER", "CA_COMMIT") else 32)
    if hosted is None and count > limit:
        bp.SetEnabled(False)
        return False
    row = {"label": label, "count": count, "time": time.time(),
           "process": name, "thread": thread,
           "home_returned": bool(home.HOME_RETURNED),
           "pc": bks._reg(frame, "pc"), "lr": bks._reg(frame, "lr"),
           "x0": bks._reg(frame, "x0"), "x1": bks._reg(frame, "x1"),
           "x2": bks._reg(frame, "x2")}
    if label == "POSTER_HOST_SET_SCENE":
        row["scene_arg"] = row["x2"]
        row["context_before"] = _u32(process, row["x0"] + 0x1A8)
        row["scene_before"] = _pointer(process, row["x0"] + 0x1B8)
    if label == "POSTER_HOST_SET_SCENE_RETURN":
        entry = PENDING.get((label, thread), {})
        host = entry.get("receiver")
        row["host"] = host
        row["context_after"] = _u32(process, host + 0x1A8) if host else None
        row["scene_after"] = _pointer(process, host + 0x1B8) if host else None
        if row["context_after"]:
            POSTER_CONTEXTS.add(row["context_after"])
    if label == "POSTER_LAYERS_UPDATE":
        # PaperBoardUI's current-build ivar offsets: _realBackgroundView
        # at +0x520, PBUIPosterSceneLayerHostView._scene at +0x1b8.
        host = _pointer(process, row["x0"] + 0x520)
        row["background_host"] = host
        row["background_scene"] = _pointer(process, host + 0x1B8) if host else None
        row["background_context_id"] = _u32(process, host + 0x1A8) if host else None
        row["background_layer"] = _layer_state(process, host)
        if row["background_context_id"]:
            POSTER_CONTEXTS.add(row["background_context_id"])
    if label == "TIME_DISPLAY_DATE_UPDATE":
        # CoverSheetKit CSProminentDisplayView._timeView is +0x1c8.
        row["time_view"] = _pointer(process, row["x0"] + 0x1C8)
    if label == "TIME_STRING_UPDATE":
        # _textLabel is at CSProminentTimeView+0x1f0; UIKitCore UIView._layer
        # is +0xb0 (ivar at 0x1e4020a04). A layer pointer is not pixel proof.
        label_view = _pointer(process, row["x0"] + 0x1F0)
        row["text_label"] = label_view
        row["time_layer"] = _pointer(process, row["x0"] + 0xB0)
        row["label_layer"] = _pointer(process, label_view + 0xB0) if label_view else None
        row["time_layer_state"] = _layer_state(process, row["x0"])
        row["label_layer_state"] = _layer_state(process, label_view)
        TIME_LAYERS.update(ptr for ptr in (row["time_layer"], row["label_layer"]) if ptr)
    if label == "CA_CONTEXT_SET_LAYER":
        # -[CAContext contextId] at 0x1844adb70: inner=[outer+8], id=[inner+4].
        inner = _pointer(process, row["x0"] + 8)
        row["context_impl"] = inner
        row["context_id"] = _u32(process, inner + 4) if inner else None
        row["root_layer"] = row["x2"]
        row["time_root_match"] = row["x2"] in TIME_LAYERS
        RECENT_CA_LAYERS.append(row.copy())
        if name.startswith("MercuryPoster") and row["context_id"] and row["root_layer"]:
            POSTER_CONTEXTS.add(row["context_id"])
        if row["context_id"] not in POSTER_CONTEXTS and count > 12:
            return False
    if label == "CA_COMMIT":
        # This transaction commit retains all contexts at 0x1843ff230;
        # its x0 is not a single CAContext ID. Keep it as ordering only.
        row["transaction_owner"] = row["x0"]
    if label == "SERVER_RENDER_UPDATE":
        row["update"] = row["x1"]
        DISPLAY_UPDATES.add(row["x1"])
        row["candidate_context_ids"] = sorted(DISPLAY_CONTEXT_IDS.get(row["x1"], ()))[:128]
        if row["lr"]:
            _register(process.GetTarget().GetDebugger(), row["lr"],
                      "SERVER_RENDER_UPDATE_RETURN", name, thread)
    if label in ("SERVER_ADD_CONTEXT", "SERVER_ADD_INCLUDED_CONTEXT",
                 "SERVER_ALLOWED_CONTEXT_RESULT", "SERVER_ALLOWED_UPDATE_RESULT",
                 "SERVER_ADD_CONTEXT_REJECT"):
        row["context_id"] = context_id
        row["context"] = context
        row["update"] = row["x0"] if label == "SERVER_ADD_CONTEXT" else None
        if label == "SERVER_ADD_CONTEXT":
            row["root_layer"] = row["x2"]
            row["root_rgba_10"] = _read(process, row["x2"] + 0x10, 16).hex()
            row["root_size_58"] = _read(process, row["x2"] + 0x58, 16).hex()
        if label.endswith("_RESULT"):
            row["allowed"] = row["x0"] & 1
    if label in ("SERVER_DISPLAY_CONTEXT_ID", "SERVER_DISPLAY_ADD_WRAPPER",
                 "SERVER_DISPLAY_ADD_ROOT", "SERVER_HOSTED_POSTER_INCLUDE",
                 "SERVER_HOSTED_SECURE_RESULT", "SERVER_HOSTED_POLICY_RESULT",
                 "SERVER_HOSTED_POSTER_ACCEPT", "SERVER_HOSTED_POSTER_REJECT",
                 "SERVER_POSTER_PREPARED_RESULT"):
        row["context_id"] = context_id
        row["context"] = context if label != "SERVER_DISPLAY_CONTEXT_ID" else None
        if label == "SERVER_DISPLAY_CONTEXT_ID":
            row["update"] = bks._reg(frame, "x19") + 0xB30
            row["id_source"] = bks._reg(frame, "x23")
        elif label.startswith("SERVER_HOSTED_"):
            row["update"] = row["x0"] if label == "SERVER_HOSTED_POSTER_INCLUDE" else bks._reg(frame, "x23")
            if label in ("SERVER_HOSTED_SECURE_RESULT", "SERVER_HOSTED_POLICY_RESULT"):
                row["allowed"] = row["x0"] & 1
                row["policy_override"] = bks._reg(frame, "x25") & 1
        elif label == "SERVER_POSTER_PREPARED_RESULT":
            row["update"] = bks._reg(frame, "x21")
            row["prepared_word"] = bks._reg(frame, "x8")
            row["prepared_mask"] = bks._reg(frame, "x9")
            row["update_flags_140"] = _pointer(process, row["update"] + 0x140)
        else:
            row["update"] = row["x0"]
            row["root_layer"] = row["x2"] if label == "SERVER_DISPLAY_ADD_ROOT" else None
    if label == "SERVER_HOSTED_POSTER_ACCEPT":
        # This branch is inside Updater::prepare_layer0. x19 is its stack
        # LocalState0, x21 the Render::Layer, x23 the display Update, and
        # x26 the accepted hosted Context. These are prepared geometry and
        # layer inputs, not proof of a non-black compositor output.
        local = bks._reg(frame, "x19")
        layer = bks._reg(frame, "x21")
        row["local_state"] = local
        row["render_layer"] = layer
        row["local_bounds_10"] = _read(process, local + 0x10, 32).hex()
        row["render_bg_10"] = _read(process, layer + 0x10, 16).hex()
        row["render_size_58"] = _read(process, layer + 0x58, 16).hex()
        row["render_contents_68"] = _pointer(process, layer + 0x68)
        row["render_flags_c"] = _u32(process, layer + 0xC)
        row["hosted_context_ptr"] = bks._reg(frame, "x26")
        HOSTED_ACCEPTS[thread] = {"local_state": local, "update": row["update"],
                                  "context_id": context_id, "context": row["hosted_context_ptr"],
                                  "render_layer": layer, "time": row["time"]}
    if label.startswith("SERVER_HOSTED_POST_") or label in (
            "SERVER_HOSTED_POSTER_LAYER_GATE", "SERVER_HOSTED_ALLOWED_UPDATE",
            "SERVER_HOSTED_HIDDEN_CULL_RESULT", "SERVER_HOSTED_HIDDEN_CULLED",
            "SERVER_HOSTED_PREPARED_STATE"):
        accepted = HOSTED_ACCEPTS[thread]
        local = accepted["local_state"]
        row["accepted_context_id"] = accepted["context_id"]
        row["accepted_update"] = accepted["update"]
        row["accepted_render_layer"] = accepted["render_layer"]
        row["local_state"] = local
        row["local_bounds_10"] = _read(process, local + 0x10, 32).hex()
        row["local_flags_350"] = _pointer(process, local + 0x350)
        row["local_flags_360"] = _pointer(process, local + 0x360)
        row["local_hosted_context_378"] = _pointer(process, local + 0x378)
        row["render_flags_c"] = _u32(process, accepted["render_layer"] + 0xC)
        if label == "SERVER_HOSTED_ALLOWED_UPDATE":
            row["allowed"] = row["x0"] & 1
            row["layer_node"] = bks._reg(frame, "x25")
            row["layer_node_surface_58"] = _pointer(process, row["layer_node"] + 0x58)
            POSTER_NODES[row["layer_node"]] = {
                "context_id": accepted["context_id"], "update": accepted["update"],
                "render_layer": accepted["render_layer"],
            }
        if label == "SERVER_HOSTED_HIDDEN_CULL_RESULT":
            row["culled"] = row["x0"] & 1
            row["layer_node"] = bks._reg(frame, "x25")
        if label == "SERVER_HOSTED_HIDDEN_CULLED":
            row["layer_node"] = bks._reg(frame, "x25")
        if label == "SERVER_HOSTED_PREPARED_STATE":
            row["layer_node"] = bks._reg(frame, "x25")
            row["layer_node_surface_58"] = _pointer(process, row["layer_node"] + 0x58)
            row["shape"] = bks._reg(frame, "x28")
            row["shape_surface_0"] = _pointer(process, row["shape"])
            POSTER_NODES[row["layer_node"]] = {
                "context_id": accepted["context_id"], "update": accepted["update"],
                "render_layer": accepted["render_layer"],
            }
    if label in ("SERVER_OGL_PREPARE_LAYER", "SERVER_OGL_ALLOC_LAYER",
                 "SERVER_OGL_RENDER_LAYERS", "SERVER_OGL_RENDER_CONTENTS"):
        if label in ("SERVER_OGL_PREPARE_LAYER", "SERVER_OGL_ALLOC_LAYER"):
            node = row["x1"]
            row["ogl_layer"] = row["x2"]
        else:
            row["ogl_layer"] = row["x1"]
            node = _pointer(process, row["ogl_layer"] + 0x10)
        row["layer_node"] = node
        row["poster_node"] = POSTER_NODES.get(node)
        row["node_flags_18"] = _pointer(process, node + 0x18)
        row["node_render_layer_20"] = _pointer(process, node + 0x20)
        row["ogl_contents_60_68"] = _read(process, row["ogl_layer"] + 0x60, 16).hex()
    if label.startswith("TIME_") and label in (
            "TIME_COPY_RENDER_LAYER", "TIME_RENDER_LAYER_ALLOCATED",
            "TIME_RENDER_CONTENTS_RESULT"):
        row["client_calayer"] = TIME_COPIES.get(thread)
        if label != "TIME_COPY_RENDER_LAYER":
            row["client_render_layer"] = bks._reg(frame, "x21") if label == "TIME_RENDER_CONTENTS_RESULT" else row["x0"]
            row["client_render_layer_contents_68"] = _pointer(process, row["client_render_layer"] + 0x68)
        if label == "TIME_RENDER_CONTENTS_RESULT":
            row["source_contents_result"] = row["x0"]
    if label == "SERVER_HOSTED_RESOLVE_CALL":
        host, host_id, update = hosted
        row["host"] = host
        row["host_context_id"] = host_id
        row["update"] = update
    if label == "SERVER_HOSTED_RESOLVE_RETURN":
        host, host_id, update = hosted
        row["host"] = host
        row["host_context_id"] = host_id
        row["update"] = update
        # retain_hosted_context's return value is not the Context*. The
        # caller loads that pointer from LayerHost's first word at +0x210
        # in the instruction immediately after this stop (0x18441ec88).
        resolved = _pointer(process, host)
        row["resolved_context"] = resolved
        row["resolved_context_id"] = _u32(process, resolved + 0x10) if resolved else None
        row["return_x0"] = row["x0"]
        if (hosted is not None and count > 8 and
                row["resolved_context_id"] not in POSTER_CONTEXTS):
            # Keep later resolver callbacks armed, but only log a return after
            # the eight positive-control samples when its resolved context is
            # one of this boot's observed Mercury/scene IDs.
            return False
    if label == "SERVER_DISPLAY_RENDER":
        row["update"] = row["x2"]
        row["known_display_update"] = row["update"] in DISPLAY_UPDATES
    if label == "SERVER_OGL_RENDER":
        row["update"] = row["x1"]
    if label == "SERVER_UPDATE_HANDLE":
        row["context_id"] = context_id
        row["handle"] = row["x1"]
        row["handle_flags_70"] = _pointer(process, row["x1"] + 0x70)
        row["handle_bounds_90"] = _read(process, row["x1"] + 0x90, 16).hex()
    if label.endswith("_IMAGE") or label in ("TIME_INIT", "TIME_CREATE_OR_DESTROY", "TIME_GLASS_CAPABILITY", "POSTER_HOST_SET_SCENE"):
        return_label = label + "_RETURN"
        key = (return_label, thread)
        if key not in PENDING and row["lr"]:
            PENDING[key] = {"receiver": row["x0"], "start": row["time"]}
            _register(process.GetTarget().GetDebugger(), row["lr"],
                      return_label, name, thread)
    if label.endswith("_RETURN"):
        entry = PENDING.pop((label, thread), None)
        if entry:
            row["entry_receiver"] = entry["receiver"]
            row["elapsed"] = row["time"] - entry["start"]
        # Most return sites are per-call LR breakpoints. This one is an
        # explicitly armed static instruction and must cover later hosts.
        if label != "SERVER_HOSTED_RESOLVE_RETURN":
            bp.SetEnabled(False)
    if label == "POSTER_HOST_SET_SCENE_RETURN" and row.get("context_after"):
        matches = [event for event in RECENT_CA_LAYERS
                   if event.get("context_id") == row["context_after"]]
        print("DM_LOCK_RICH_CONTEXT_HISTORY " + json.dumps({
            "context_id": row["context_after"], "set_layer": matches,
        }, sort_keys=True), flush=True)
    print("DM_LOCK_RICH_JSON " + json.dumps(row, sort_keys=True), flush=True)
    return False


def install(debugger, slide):
    interpreter = debugger.GetCommandInterpreter()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("command script import %s" %
                              Path(swap.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("swap import failed: %s" % result.GetError())
    swap.install(debugger, slide)
    META.clear()
    COUNTS.clear()
    PENDING.clear()
    POSTER_CONTEXTS.clear()
    RECENT_CA_LAYERS.clear()
    TIME_LAYERS.clear()
    DISPLAY_UPDATES.clear()
    DISPLAY_CONTEXT_IDS.clear()
    HOSTED_RESOLVES.clear()
    HOSTED_ACCEPTS.clear()
    TIME_COPIES.clear()
    POSTER_NODES.clear()
    for static, label, process_name in SITES:
        _register(debugger, int(slide) + static, label, process_name)
    print("DM_LOCK_RICH_READY sites=%d" %
          debugger.GetSelectedTarget().GetNumBreakpoints(), flush=True)
