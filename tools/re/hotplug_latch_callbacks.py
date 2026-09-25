"""Read-only hot-plug latch + A408/surface witnesses (docs/re/iomfb-hotplug-swap-gate.md).

Runtime addresses already include the project kernel slide (+0x20000000).
Install after the guest is past A401; freeze before D575 runs if possible.

    script hotplug_latch_callbacks.install(lldb.debugger)
"""
import lldb

# Unslid + 0x20000000, from docs/re/iomfb-hotplug-swap-gate.md
SITES = (
    (0xfffffff02a0d9e4c, "D575_HANDLER", 8),
    (0xfffffff02a0bcad8, "HOTPLUG_ENTRY", 8),
    (0xfffffff02a0bcb94, "HOTPLUG_POST_LATCH", 8),
    (0xfffffff02a0c8f8c, "A407_BUILD", 4),
    (0xfffffff02a0c91d8, "A408_BUILD", 4),
    (0xfffffff02a0b9600, "SURFACE_MAP", 4),
    (0xfffffff02a0daa9c, "D589", 4),
    (0xfffffff02a0dad28, "D591", 4),
    # +0x389 writers / A489 getter (docs: fIsSharedDisplay via A489)
    (0xfffffff02a0b4d9c, "SET389_ENTRY", 8),
    (0xfffffff02a0bac08, "SET389_PATH2", 8),
    (0xfffffff02a0cc684, "A489_HELPER", 8),
)

CONFIG = {}
HITS = {}
# Set True via `script hotplug_latch_callbacks.FORCE_389 = True` before install,
# or env DVM_HOTPLUG_FORCE_389=1, to write +0x389=1 at HOTPLUG_ENTRY.
FORCE_389 = False


def _u8(process, addr):
    err = lldb.SBError()
    data = process.ReadMemory(addr, 1, err)
    return data[0] if err.Success() and data else None


def _reg(frame, name):
    # Prefer FindRegister — GetRegisters()/child names are empty under QEMU gdbstub.
    v = frame.FindRegister(name)
    if v.IsValid():
        return v.GetValueAsUnsigned()
    regs = frame.GetRegisters()
    for set_ in regs:
        set_name = set_.GetName() or ""
        if "General" not in set_name and set_name != "General Purpose Registers":
            continue
        child = set_.GetChildMemberWithName(name)
        if child.IsValid():
            return child.GetValueAsUnsigned()
    return None


def on_break(frame, bp_loc, _dict):
    thread = frame.GetThread()
    process = thread.GetProcess()
    bp_id = bp_loc.GetBreakpoint().GetID()
    cfg = CONFIG[bp_id]
    label = cfg["label"]
    HITS[label] = HITS.get(label, 0) + 1
    hit = HITS[label]
    limit = cfg["limit"]

    x0, x1, x3, x19, w20, w21 = (_reg(frame, n) for n in (
        "x0", "x1", "x3", "x19", "w20", "w21"))
    print("=== %s hit=%d/%d ===" % (label, hit, limit), flush=True)
    print("registers x0=%s x1=%s x3=%s x19=%s w20=%s w21=%s" % (
        hex(x0) if x0 is not None else None,
        hex(x1) if x1 is not None else None,
        hex(x3) if x3 is not None else None,
        hex(x19) if x19 is not None else None,
        hex(w20) if w20 is not None else None,
        hex(w21) if w21 is not None else None,
    ), flush=True)
    base = x19 if label == "HOTPLUG_POST_LATCH" and x19 else (
        x0 if label in ("HOTPLUG_ENTRY", "D575_HANDLER") and x0 else None)
    if base:
        b389 = _u8(process, base + 0x389)
        b38a = _u8(process, base + 0x38a)
        b430 = _u8(process, base + 0x430)
        print("latch_bytes self=%s +0x389=%s +0x38a=%s +0x430=%s" % (
            hex(base), b389, b38a, b430), flush=True)
        # Opt-in: force display-ready bit so the hot-plug latch can run
        # (A489 getter never fires on current boots; see UI_HOTPLUG10/11).
        if label == "HOTPLUG_ENTRY" and FORCE_389 and (b389 is None or (b389 & 1) == 0):
            err = lldb.SBError()
            process.WriteMemory(base + 0x389, bytes([1]), err)
            print("FORCE_389 wrote +0x389=1 ok=%s" % err.Success(), flush=True)
            b389 = _u8(process, base + 0x389)
            print("latch_bytes_after_force +0x389=%s" % b389, flush=True)
    if hit >= limit:
        bp_loc.GetBreakpoint().SetEnabled(False)
        print("bounded-disabled breakpoint=%d after=%d" % (bp_id, hit), flush=True)
    return False


def install(debugger, _slide=0):
    global FORCE_389
    import os
    if os.environ.get("DVM_HOTPLUG_FORCE_389", "").strip() in ("1", "true", "yes"):
        FORCE_389 = True
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    print("HOTPLUG_LATCH_READY force_389=%s" % FORCE_389, flush=True)
    for addr, label, limit in SITES:
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % addr, result)
        if not result.Succeeded():
            raise RuntimeError("bp failed %s: %s" % (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        CONFIG[bp.GetID()] = {"label": label, "limit": limit}
        HITS[label] = 0
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F hotplug_latch_callbacks.on_break %d" %
            bp.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("callback failed %s: %s" % (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" % (
            bp.GetID(), label, addr))
