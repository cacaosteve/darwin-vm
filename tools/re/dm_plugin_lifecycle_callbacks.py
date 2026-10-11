"""Read-only lifecycle trace for Passbook and SpringBoard migration RPCs.

The client request is keyed by its path and pluginIdentifier, not by timing.
Wrapper-side connection, receive, reply, and exit hooks are independent
checkpoints: a generic hit is not attributed to either plugin until its
message identity or connection is established.
"""

import json
import os
import time

import lldb

import bks_checkin_callbacks as bks


TARGET_IDS = {"com.apple.PassbookDataMigrator", "com.apple.sbmigrator"}
TARGET_PATHS = ("PassbookDataMigrator.migrator", "SpringBoard.migrator")
KERNEL_SLIDE = int(os.environ.get("DVM_KERNEL_SLIDE", "0x20000000"), 0)
SITES = (
    (0x25E8F1A10, "CLIENT_RUN_ENTRY"),
    (0x25E8F1F88, "CLIENT_SYNC_CALL"),
    (0x25E8F1F8C, "CLIENT_SYNC_RETURN"),
    (0x25E8EF458, "WRAPPER_CONNECTION_INIT"),
    (0x25E8EF374, "WRAPPER_XPC_RECEIVE"),
    (0x25E8F20CC, "CLIENT_WRAPPER_MESSAGE"),
    (0x25E8F044C, "DM_PERFORM_RETURN"),
    (0x18A147774, "BKS_CHECKIN_RETURN"),
)
KERNEL_SITES = ((0xFFFFFFF00AFF6BD8, "WRAPPER_KERNEL_EXIT"),)
DYNAMIC_SITES = ((0x1805BCDA8, "WRAPPER_XPC_REPLY"),
                 (0x1805B2480, "WRAPPER_XPC_SEND"))
META = {}
OWNERS = {}
COUNTS = {}
DYNAMIC_ARMED = False


def _read(process, address, size):
    return bks._read(process, address, size) if address else b""


def _u64(process, address):
    raw = _read(process, address, 8)
    return int.from_bytes(raw, "little") if len(raw) == 8 else 0


def _cstring(process, address, limit=160):
    raw = _read(process, address, limit)
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace") if raw else ""


def _nsstring(process, address):
    raw = _read(process, address, 0x100)
    if len(raw) < 32:
        return "<unreadable>"
    # The target path objects are inline UTF-16 CFStrings on this build.
    candidate = raw[12:]
    for end in range(0, min(len(candidate) - 1, 256), 2):
        if candidate[end:end + 2] == b"\0\0":
            try:
                value = candidate[:end].decode("utf-16-le")
            except UnicodeDecodeError:
                break
            if value.startswith("/System/Library/DataClassMigrators/"):
                return value
            break
    # Small/heap NSString variants used for identifiers.
    flags = int.from_bytes(raw[8:10], "little")
    if flags == 0x7c8:
        pointer = int.from_bytes(raw[16:24], "little")
        length = int.from_bytes(raw[24:32], "little")
        if 0 < length <= 160:
            value = _read(process, pointer, length)
            if len(value) == length:
                return value.decode("utf-8", "replace")
    if flags == 0x78c and raw[16] <= 15:
        return raw[17:17 + raw[16]].decode("utf-8", "replace")
    return "<string@0x%x flags=0x%x>" % (address, flags)


def _xpc_fields(process, dictionary):
    """Describe a bounded XPC dictionary chain using the observed guest layout."""
    fields = {}
    first = _u64(process, dictionary + 0x28)
    node, seen = first, set()
    for _ in range(32):
        if not node or node in seen or node >= 1 << 43:
            break
        seen.add(node)
        raw = _read(process, node, 0x50)
        if len(raw) < 0x40:
            break
        key = raw[0x20:0x50].split(b"\0", 1)[0]
        if not key or any(c < 32 or c > 126 for c in key):
            break
        name = key.decode("ascii")
        value = int.from_bytes(raw[0x10:0x18], "little")
        entry = {"object": "0x%x" % value}
        if value and value < 1 << 43:
            pointer = _u64(process, value + 0x20)
            text = _cstring(process, pointer, 240)
            if text and all(32 <= ord(c) < 127 for c in text):
                entry["text"] = text
        fields[name] = entry
        node = int.from_bytes(raw[:8], "little")
    return fields


def _emit(label, frame, extra=None):
    event = {"label": label, "time": time.time(),
             "thread": frame.GetThread().GetThreadID(),
             "tpidr_el1": "0x%x" % bks._reg(frame, "tpidr_el1"),
             "pc": "0x%x" % bks._reg(frame, "pc")}
    if extra:
        event.update(extra)
    print("DM_PLUGIN_LIFECYCLE " + json.dumps(event, sort_keys=True), flush=True)


def on_hit(frame, bp_loc, _context):
    global DYNAMIC_ARMED
    process = frame.GetThread().GetProcess()
    label = META[bp_loc.GetBreakpoint().GetID()]
    name = bks._progname(process) if label != "WRAPPER_KERNEL_EXIT" else ""
    owner = bks._reg(frame, "tpidr_el1")
    if label == "WRAPPER_KERNEL_EXIT":
        proc = bks._reg(frame, "x0")
        pname = _cstring(process, proc + 0x55c, 32)
        # proc.p_comm is limited to 16 bytes including NUL. The full wrapper
        # name is available in user space, but is truncated here in the kernel.
        if pname != "com.apple.migrat":
            return False
        _emit(label, frame, {"proc": "0x%x" % proc,
                             "pid": int.from_bytes(_read(process, proc + 0x60, 4), "little"),
                             "exit_args": "0x%x" % bks._reg(frame, "x1")})
        return False
    if label.startswith("CLIENT_") and name != "com.apple.datamigrator":
        return False
    if label.startswith("WRAPPER_") and name != "com.apple.migrationpluginwrapper":
        return False
    if label == "DM_PERFORM_RETURN" and name != "backboardd":
        return False
    if label == "BKS_CHECKIN_RETURN" and name != "SpringBoard":
        return False
    if label == "CLIENT_RUN_ENTRY":
        path = _nsstring(process, bks._reg(frame, "x2"))
        identifier = _nsstring(process, bks._reg(frame, "x3"))
        if identifier not in TARGET_IDS and not any(p in path for p in TARGET_PATHS):
            return False
        OWNERS[owner] = {"path": path, "identifier": identifier}
        _emit(label, frame, {"path": path, "identifier": identifier,
                             "path_object": "0x%x" % bks._reg(frame, "x2"),
                             "identifier_object": "0x%x" % bks._reg(frame, "x3")})
        return False
    if label in ("CLIENT_SYNC_CALL", "CLIENT_SYNC_RETURN"):
        item = OWNERS.get(owner)
        if not item:
            return False
        extra = dict(item)
        if label == "CLIENT_SYNC_CALL":
            request = bks._reg(frame, "x21")
            extra.update(request="0x%x" % request,
                         fields=_xpc_fields(process, request))
        else:
            extra["result"] = "0x%x" % bks._reg(frame, "x0")
            OWNERS.pop(owner, None)
        _emit(label, frame, extra)
        return False
    if label == "WRAPPER_XPC_RECEIVE":
        message = bks._reg(frame, "x2")
        fields = _xpc_fields(process, message)
        identifier = fields.get("pluginIdentifier", {}).get("text")
        if identifier not in TARGET_IDS:
            return False
        if not DYNAMIC_ARMED:
            for static, dynamic_label in DYNAMIC_SITES:
                _install_one(process.GetTarget().GetDebugger(),
                             bks.SLIDE[0] + static, dynamic_label)
            DYNAMIC_ARMED = True
        _emit(label, frame, {"message": "0x%x" % message,
                             "identifier": identifier, "fields": fields})
        return False
    if label in ("WRAPPER_XPC_REPLY", "WRAPPER_XPC_SEND",
                 "CLIENT_WRAPPER_MESSAGE"):
        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        if count > 128:
            return False
        message_register = ("x0" if label == "WRAPPER_XPC_REPLY" else
                            "x1" if label == "WRAPPER_XPC_SEND" else "x2")
        message = bks._reg(frame, message_register)
        _emit(label, frame, {"message": "0x%x" % message,
                             "fields": _xpc_fields(process, message),
                             "x0": "0x%x" % bks._reg(frame, "x0"),
                             "x1": "0x%x" % bks._reg(frame, "x1")})
        return False
    _emit(label, frame, {"x0": "0x%x" % bks._reg(frame, "x0"),
                         "x1": "0x%x" % bks._reg(frame, "x1")})
    return False


def _install_one(debugger, address, label):
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint set --address 0x%x" % address,
                              result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed to arm %s: %s" % (label, result.GetError()))
    bp = target.GetBreakpointAtIndex(before)
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand(
        "breakpoint command add -F dm_plugin_lifecycle_callbacks.on_hit %d" %
        bp.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("failed callback %s: %s" % (label, result.GetError()))
    META[bp.GetID()] = label
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(),
                              result)
    proof = result.GetOutput() or result.GetError()
    if "dm_plugin_lifecycle_callbacks.on_hit" not in proof:
        raise RuntimeError("missing command proof %s: %s" % (label, proof))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
          (bp.GetID(), label, address), flush=True)


def install(debugger, slide):
    bks.SLIDE[0] = int(slide)
    for static, label in SITES:
        _install_one(debugger, slide + static, label)
    for static, label in KERNEL_SITES:
        _install_one(debugger, static + KERNEL_SLIDE, label)
    print("DM_PLUGIN_LIFECYCLE_READY slide=0x%x sites=%d" %
          (slide, len(SITES) + len(KERNEL_SITES)), flush=True)
