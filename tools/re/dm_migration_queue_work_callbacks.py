"""Follow the exact DataMigrator async block preceding its sync queue barrier.

The msgID-0 server path calls dispatch_async on the migration queue, then
dispatch_sync with a no-op block on that same queue. Track the submitted block
by pointer, including if it runs on another guest thread.
"""

import dm_migration_server_migrate_callbacks as parent
import dm_perform_roundtrip_callbacks as common
import bks_checkin_callbacks as bks
from inspect_install_coordinators import string


parent.CALLBACK_MODULE = "dm_migration_queue_work_callbacks"
parent.MAIN.clear()
parent.MAIN.update({
    "DM_MSGID0_ARM": 0x1C78,
    "DM_MIGRATION_CALL": 0x2A8C,
    "DM_STATE_ENTRY": 0x8A98,
    "DM_STATE_ASYNC_CALL": 0x8D74,
    "DM_STATE_ASYNC_RETURN": 0x8D78,
    "DM_STATE_SYNC_CALL": 0x8E74,
    "DM_STATE_SYNC_RETURN": 0x8E78,
    "DM_STATE_RETURN": 0x8EE4,
})

BLOCK = None
BLOCK_CAPTURE = None
NESTED_SOURCE_INVOKE = None
QUEUE = None
BLOCK_OWNER = None
QUEUED_BLOCK = None
OTHER_BLOCK_ENTRIES = 0
STARTER_COUNTS = {}
PLUGIN_BLOCK = None
PLUGIN_OWNER = None
PLUGIN_OBJECT = None
RUN_EVENT_BLOCK = None
RUN_EVENT_OWNER = None
COORDINATOR = None
PLUGIN_DEADLINE_HITS = 0
BLOCK_SITES = {
    "DM_ASYNC_BLOCK_ENTRY": 0x905C,
    "DM_ASYNC_BLOCK_LOG_CALL": 0x9088,
    "DM_ASYNC_BLOCK_LOG_RETURN": 0x908C,
    "DM_ASYNC_BLOCK_CALLBACK_TEST": 0x9090,
    "DM_ASYNC_BLOCK_CALLBACK_CALL": 0x90A0,
    "DM_ASYNC_BLOCK_CALLBACK_RETURN": 0x90A4,
}
STARTER_SITES = {
    "DM_STARTER_TRAMPOLINE_ENTRY": 0x1000C,
    "DM_MIGRATE_CORE_ENTRY": 0x10020,
    "DM_MIGRATE_STAGE_0740": 0x10740,
    "DM_MIGRATE_PRELIM_CACHE_CALL": 0x10748,
    "DM_MIGRATE_PRELIM_CACHE_RETURN": 0x1074C,
    "DM_MIGRATE_PERFORM_CALL": 0x10768,
    "DM_MIGRATE_PERFORM_RETURN": 0x1076C,
    "DM_MIGRATE_STAGE_0794": 0x10794,
    "DM_MIGRATE_RETURN": 0x10AD8,
    "DM_PERFORM_ENTRY": 0xDB14,
    "DM_PERFORM_STAGE_DC98": 0xDC98,
    "DM_PERFORM_STAGE_DD60": 0xDD60,
    "DM_PERFORM_STAGE_DFD0": 0xDFD0,
    "DM_PERFORM_ITEM": 0xE010,
    "DM_PERFORM_ITEM_STAGE_E020": 0xE020,
    "DM_PERFORM_ITEM_STAGE_E050": 0xE050,
    "DM_PERFORM_ITEM_STAGE_E090": 0xE090,
    "DM_PERFORM_ITEM_STAGE_E0E4": 0xE0E4,
    "DM_PERFORM_TASK_READY": 0xE140,
    "DM_PERFORM_TASK_CALL": 0xE178,
    "DM_PERFORM_TASK_RETURN": 0xE17C,
    "DM_PERFORM_ITEM_POST_E180": 0xE180,
    "DM_PERFORM_ITEM_POST_E208": 0xE208,
    "DM_PERFORM_ITEM_POST_E230": 0xE230,
    "DM_PERFORM_ITEM_POST_E260": 0xE260,
    "DM_PERFORM_ITEM_FINISH_CALL": 0xE284,
    "DM_PERFORM_ITEM_FINISH_RETURN": 0xE288,
    "DM_PERFORM_LOOP_TAIL": 0xE294,
    "DM_PERFORM_LOOP_TEST": 0xE298,
    "DM_PERFORM_NEXT_BATCH_CALL": 0xE2B0,
    "DM_PERFORM_NEXT_BATCH_RETURN": 0xE2B4,
    "DM_PERFORM_AFTER_BATCH": 0xE2BC,
    "DM_PERFORM_POST_CALL_E2C8": 0xE2C8,
    "DM_PERFORM_POST_RETURN_E2CC": 0xE2CC,
    "DM_PERFORM_POST_CALL_E2D4": 0xE2D4,
    "DM_PERFORM_POST_RETURN_E2D8": 0xE2D8,
    "DM_PERFORM_POST_CALL_E2F0": 0xE2F0,
    "DM_PERFORM_POST_RETURN_E2F4": 0xE2F4,
    "DM_PERFORM_POST_STAGE_E300": 0xE300,
    "DM_PERFORM_POST_CALL_E308": 0xE308,
    "DM_PERFORM_POST_RETURN_E30C": 0xE30C,
    "DM_PERFORM_POST_CALL_E338": 0xE338,
    "DM_PERFORM_POST_RETURN_E33C": 0xE33C,
    "DM_PERFORM_POST_STAGE_E340": 0xE340,
    "DM_PERFORM_POST_STAGE_E400": 0xE400,
    "DM_PERFORM_POST_STAGE_E4D0": 0xE4D0,
    "DM_PERFORM_POST_CALL_E4E0": 0xE4E0,
    "DM_PERFORM_POST_RETURN_E4E4": 0xE4E4,
    "DM_PERFORM_PLUGIN_ITEM_E500": 0xE500,
    "DM_PERFORM_PLUGIN_GETTER_CALL": 0xE528,
    "DM_PERFORM_PLUGIN_GETTER_RETURN": 0xE52C,
    "DM_PERFORM_PLUGIN_RELEASE_CALL": 0xE538,
    "DM_PERFORM_PLUGIN_RELEASE_RETURN": 0xE53C,
    "DM_PERFORM_PLUGIN_STAGE_E540": 0xE540,
    "DM_PERFORM_PLUGIN_CATEGORY_CALL": 0xE54C,
    "DM_PERFORM_PLUGIN_CATEGORY_RETURN": 0xE550,
    "DM_PERFORM_PLUGIN_TEST_CALL": 0xE560,
    "DM_PERFORM_PLUGIN_TEST_RETURN": 0xE564,
    "DM_PERFORM_PLUGIN_RETRY_CALL": 0xE574,
    "DM_PERFORM_PLUGIN_RETRY_RETURN": 0xE578,
    "DM_PERFORM_PLUGIN_STAGE_E584": 0xE584,
    "DM_PERFORM_PLUGIN_CALLBACK_CALL": 0xE590,
    "DM_PERFORM_PLUGIN_CALLBACK_RETURN": 0xE594,
    "DM_PERFORM_PLUGIN_LOOP_TEST": 0xE598,
    "DM_PERFORM_PLUGIN_TAIL_E5B4": 0xE5B4,
    "DM_PERFORM_POST_STAGE_E5C0": 0xE5C0,
    "DM_PERFORM_POST_STAGE_E700": 0xE700,
    "DM_PERFORM_METHOD_RETURN": 0xE848,
}
MULTI_SITES = {
    "DM_PERFORM_ITEM", "DM_PERFORM_ITEM_STAGE_E020",
    "DM_PERFORM_ITEM_STAGE_E050", "DM_PERFORM_ITEM_STAGE_E090",
    "DM_PERFORM_ITEM_STAGE_E0E4", "DM_PERFORM_TASK_READY",
    "DM_PERFORM_TASK_CALL", "DM_PERFORM_TASK_RETURN",
    "DM_PERFORM_ITEM_POST_E180", "DM_PERFORM_ITEM_POST_E208",
    "DM_PERFORM_ITEM_POST_E230", "DM_PERFORM_ITEM_POST_E260",
    "DM_PERFORM_ITEM_FINISH_CALL", "DM_PERFORM_ITEM_FINISH_RETURN",
    "DM_PERFORM_LOOP_TAIL", "DM_PERFORM_LOOP_TEST",
    "DM_PERFORM_NEXT_BATCH_CALL", "DM_PERFORM_NEXT_BATCH_RETURN",
    "DM_PERFORM_PLUGIN_ITEM_E500", "DM_PERFORM_PLUGIN_GETTER_CALL",
    "DM_PERFORM_PLUGIN_GETTER_RETURN", "DM_PERFORM_PLUGIN_RELEASE_CALL",
    "DM_PERFORM_PLUGIN_RELEASE_RETURN", "DM_PERFORM_PLUGIN_STAGE_E540",
    "DM_PERFORM_PLUGIN_CATEGORY_CALL", "DM_PERFORM_PLUGIN_CATEGORY_RETURN",
    "DM_PERFORM_PLUGIN_TEST_CALL", "DM_PERFORM_PLUGIN_TEST_RETURN",
    "DM_PERFORM_PLUGIN_RETRY_CALL", "DM_PERFORM_PLUGIN_RETRY_RETURN",
    "DM_PERFORM_PLUGIN_STAGE_E584", "DM_PERFORM_PLUGIN_CALLBACK_CALL",
    "DM_PERFORM_PLUGIN_CALLBACK_RETURN", "DM_PERFORM_PLUGIN_LOOP_TEST",
    "DM_PERFORM_PLUGIN_TAIL_E5B4",
}
PLUGIN_BLOCK_SITES = {
    "DM_PLUGIN4_BLOCK_ENTRY": 0xE904,
    "DM_PLUGIN4_STAGE_E944": 0xE944,
    "DM_PLUGIN4_STAGE_E9A8": 0xE9A8,
    "DM_PLUGIN4_STAGE_EA08": 0xEA08,
    "DM_PLUGIN4_STAGE_EA90": 0xEA90,
    "DM_PLUGIN4_STAGE_EB0C": 0xEB0C,
    "DM_PLUGIN4_STAGE_EB60": 0xEB60,
    "DM_PLUGIN4_STAGE_EBC0": 0xEBC0,
    "DM_PLUGIN4_EBC4_CALL": 0xEBC4,
    "DM_PLUGIN4_EBC8_RETURN": 0xEBC8,
    "DM_PLUGIN4_STAGE_EC0C": 0xEC0C,
    "DM_PLUGIN4_EC1C_CALL": 0xEC1C,
    "DM_PLUGIN4_EC20_RETURN": 0xEC20,
    "DM_PLUGIN4_STAGE_EC90": 0xEC90,
    "DM_PLUGIN4_STAGE_ECD0": 0xECD0,
    "DM_PLUGIN4_STAGE_ED4C": 0xED4C,
    "DM_PLUGIN4_STAGE_EDA0": 0xEDA0,
    "DM_PLUGIN4_STAGE_EE00": 0xEE00,
    "DM_PLUGIN4_STAGE_EE80": 0xEE80,
    "DM_PLUGIN4_BLOCK_RETURN": 0xEF14,
}
PLUGIN_INNER_SITES = {
    "DM_PLUGIN4_MIGRATION_ENTRY": 0x13084,
    "DM_PLUGIN4_MIGRATION_CALL_130E0": 0x130E0,
    "DM_PLUGIN4_MIGRATION_RETURN_130E4": 0x130E4,
    "DM_PLUGIN4_NESTED_ENTRY_13224": 0x13224,
    "DM_PLUGIN4_NESTED_STAGE_132C0": 0x132C0,
    "DM_PLUGIN4_NESTED_STAGE_13340": 0x13340,
    "DM_PLUGIN4_NESTED_CALL_13364": 0x13364,
    "DM_PLUGIN4_NESTED_RETURN_13368": 0x13368,
    "DM_PLUGIN4_NESTED_EPILOGUE_133B0": 0x133B0,
}
PLUGIN_COORD_SITES = {
    "DM_PLUGIN4_COORD_ENTRY": 0x11BA4,
    "DM_PLUGIN4_COORD_FIRST_SYNC_CALL": 0x11D20,
    "DM_PLUGIN4_COORD_FIRST_SYNC_RETURN": 0x11D24,
    "DM_PLUGIN4_COORD_SECOND_SYNC_CALL": 0x11DE0,
    "DM_PLUGIN4_COORD_SECOND_SYNC_RETURN": 0x11DE4,
    "DM_PLUGIN4_COORD_THIRD_SYNC_CALL": 0x11EB8,
    "DM_PLUGIN4_COORD_THIRD_SYNC_RETURN": 0x11EBC,
    "DM_PLUGIN4_COORD_RETURN": 0x11F38,
}
PLUGIN_RUN_BLOCK_SITES = {
    "DM_PLUGIN4_RUN_BLOCK_ENTRY": 0x1341C,
    "DM_PLUGIN4_RUN_BLOCK_QUEUE_CREATED": 0x13474,
    "DM_PLUGIN4_RUN_BLOCK_STAGE_13588": 0x13588,
    "DM_PLUGIN4_RUN_BLOCK_STAGE_13618": 0x13618,
    "DM_PLUGIN4_RUN_BLOCK_STAGE_1361C": 0x1361C,
    "DM_PLUGIN4_RUN_BLOCK_CALL_1366C": 0x1366C,
    "DM_PLUGIN4_RUN_BLOCK_RETURN_13670": 0x13670,
    "DM_PLUGIN4_RUN_BLOCK_CALL_13684": 0x13684,
    "DM_PLUGIN4_RUN_BLOCK_RETURN_13688": 0x13688,
    "DM_PLUGIN4_RUN_BLOCK_CALL_1369C": 0x1369C,
    "DM_PLUGIN4_RUN_BLOCK_RETURN_136A0": 0x136A0,
    "DM_PLUGIN4_RUN_BLOCK_CALL_136A8": 0x136A8,
    "DM_PLUGIN4_RUN_BLOCK_RETURN_136AC": 0x136AC,
    "DM_PLUGIN4_RUN_BLOCK_CALL_136BC": 0x136BC,
    "DM_PLUGIN4_RUN_BLOCK_RETURN_136C0": 0x136C0,
    "DM_PLUGIN4_RUN_BLOCK_CALL_1370C": 0x1370C,
    "DM_PLUGIN4_RUN_BLOCK_RETURN_13710": 0x13710,
    "DM_PLUGIN4_RUN_BLOCK_CALL_13718": 0x13718,
    "DM_PLUGIN4_RUN_BLOCK_RETURN_1371C": 0x1371C,
    "DM_PLUGIN4_RUN_BLOCK_CALL_13740": 0x13740,
    "DM_PLUGIN4_RUN_BLOCK_RETURN_13744": 0x13744,
    "DM_PLUGIN4_RUN_BLOCK_CALL_13768": 0x13768,
    "DM_PLUGIN4_RUN_BLOCK_RETURN_1376C": 0x1376C,
    "DM_PLUGIN4_RUN_BLOCK_CALL_13774": 0x13774,
    "DM_PLUGIN4_RUN_BLOCK_RETURN_13778": 0x13778,
    "DM_PLUGIN4_RUN_BLOCK_EPILOGUE": 0x137C0,
}
def _u64(raw, offset):
    return int.from_bytes(raw[offset:offset + 8], "little") if len(raw) >= offset + 8 else None


def _queue_state(process):
    raw = bks._read(process, QUEUE, 0x90) if QUEUE else b""
    head = _u64(raw, 0x68)
    chain = []
    item = head
    seen = set()
    while item and item not in seen and len(chain) < 8:
        seen.add(item)
        data = bks._read(process, item, 0x40)
        chain.append({
            "item": item, "next": _u64(data, 0x10),
            "target_queue": _u64(data, 0x18),
            "context": _u64(data, 0x28),
            "bytes": data.hex(),
        })
        item = _u64(data, 0x10)
    return {
        "queue": QUEUE, "queue_bytes": raw.hex(),
        "target": _u64(raw, 0x18), "tail": _u64(raw, 0x30),
        "state": _u64(raw, 0x38), "head": head, "chain": chain,
    }


def on_hit(frame, bp_loc, context):
    global BLOCK, BLOCK_CAPTURE, NESTED_SOURCE_INVOKE
    global QUEUE, BLOCK_OWNER, QUEUED_BLOCK, OTHER_BLOCK_ENTRIES
    global PLUGIN_BLOCK, PLUGIN_OWNER, PLUGIN_OBJECT
    global RUN_EVENT_BLOCK, RUN_EVENT_OWNER, COORDINATOR
    process = frame.GetThread().GetProcess()
    debugger = process.GetTarget().GetDebugger()
    label = common.SITES[bp_loc.GetBreakpoint().GetID()]
    tpidr = bks._reg(frame, "tpidr_el1")
    if label == "DM_STATE_ASYNC_CALL" and tpidr == parent.SERVER_OWNER:
        QUEUE = bks._reg(frame, "x0")
        BLOCK = bks._reg(frame, "x1")
        # dispatch_async copies this stack block. Match its two captured
        # nested blocks by invoke function, not by their copied addresses.
        BLOCK_CAPTURE = bks._read(process, BLOCK + 0x20, 16)
        nested_source = _u64(BLOCK_CAPTURE, 8)
        NESTED_SOURCE_INVOKE = _u64(
            bks._read(process, nested_source + 0x10, 8), 0
        ) if nested_source else None
        parent._emit(frame, "DM_QUEUE_WORK_CAPTURE", {
            "queue": QUEUE, "source_block": BLOCK,
            "capture_20_30": BLOCK_CAPTURE.hex(),
            "nested_source_invoke": NESTED_SOURCE_INVOKE,
            "main_base": parent.MAIN_BASE,
        })
        for name, offset in BLOCK_SITES.items():
            parent._arm(debugger, name, parent.MAIN_BASE + offset)
    if label in ("DM_STATE_ASYNC_RETURN", "DM_STATE_SYNC_CALL") and tpidr == parent.SERVER_OWNER:
        parent._emit(frame, label + "_QUEUE_STATE", _queue_state(process))
    if label in BLOCK_SITES:
        if label == "DM_ASYNC_BLOCK_ENTRY":
            target = bks._reg(frame, "x0")
            capture = bks._read(process, target + 0x20, 16)
            nested_target = _u64(capture, 8)
            nested_invoke = _u64(
                bks._read(process, nested_target + 0x10, 8), 0
            ) if nested_target else None
            if (not NESTED_SOURCE_INVOKE or not nested_invoke or
                    (NESTED_SOURCE_INVOKE & 0x0000FFFFFFFFFFFF) !=
                    (nested_invoke & 0x0000FFFFFFFFFFFF)):
                OTHER_BLOCK_ENTRIES += 1
                if OTHER_BLOCK_ENTRIES <= 4:
                    parent._emit(frame, "DM_ASYNC_BLOCK_OTHER", {
                        "invoke_block": target, "capture_20_30": capture.hex(),
                        "expected_20_30": BLOCK_CAPTURE.hex() if BLOCK_CAPTURE else "",
                        "queued_block": QUEUED_BLOCK,
                        "nested_invoke": nested_invoke,
                        "nested_source_invoke": NESTED_SOURCE_INVOKE,
                        "count": OTHER_BLOCK_ENTRIES,
                    })
                return False
            QUEUED_BLOCK = target
            BLOCK_OWNER = tpidr
        elif tpidr != BLOCK_OWNER:
            return False
        parent._emit(frame, label, {
            "queue": QUEUE, "source_block": BLOCK,
            "queued_block": QUEUED_BLOCK,
            "x0": bks._reg(frame, "x0"), "x1": bks._reg(frame, "x1"),
        })
        parent._disable(label)
        if label == "DM_ASYNC_BLOCK_CALLBACK_CALL":
            for name, offset in STARTER_SITES.items():
                parent._arm(debugger, name, parent.MAIN_BASE + offset)
        return False
    if label in STARTER_SITES:
        if tpidr != BLOCK_OWNER:
            return False
        STARTER_COUNTS[label] = STARTER_COUNTS.get(label, 0) + 1
        count = STARTER_COUNTS[label]
        if label not in MULTI_SITES or count <= 5 or count in (10, 20, 50, 100) or count % 250 == 0:
            payload = {
                "queued_block": QUEUED_BLOCK, "count": count,
                "x0": bks._reg(frame, "x0"), "x1": bks._reg(frame, "x1"),
                "x2": bks._reg(frame, "x2"), "x3": bks._reg(frame, "x3"),
                "x19": bks._reg(frame, "x19"), "x20": bks._reg(frame, "x20"),
                "x21": bks._reg(frame, "x21"), "x26": bks._reg(frame, "x26"),
                "x28": bks._reg(frame, "x28"),
            }
            if label == "DM_PERFORM_ITEM":
                payload["item_bytes"] = bks._read(process, payload["x20"], 0x40).hex()
            if label == "DM_PERFORM_TASK_CALL":
                payload["block_bytes"] = bks._read(process, payload["x0"], 0x40).hex()
                payload["x8"] = bks._reg(frame, "x8")
                payload["x9"] = bks._reg(frame, "x9")
            if label == "DM_PERFORM_PLUGIN_CALLBACK_CALL":
                plugin_raw = bks._read(process, payload["x28"], 0x40)
                payload["plugin_bytes"] = plugin_raw.hex()
                if count == 4:
                    for offset in (0x10, 0x18):
                        pointer = _u64(plugin_raw, offset)
                        payload["plugin_field_%x" % offset] = pointer
                        payload["plugin_field_%x_bytes" % offset] = (
                            bks._read(process, pointer, 0x50).hex() if pointer else ""
                        )
            parent._emit(frame, label, payload)
        if label == "DM_PERFORM_PLUGIN_CALLBACK_CALL" and count == 4:
            PLUGIN_BLOCK = bks._reg(frame, "x0")
            PLUGIN_OWNER = tpidr
            for name, offset in PLUGIN_BLOCK_SITES.items():
                parent._arm(debugger, name, parent.MAIN_BASE + offset)
            parent._arm(debugger, "DM_PLUGIN4_DEADLINE_ENTRY", parent.MAIN_BASE + 0x13224)
        if label not in MULTI_SITES:
            parent._disable(label)
        return False
    if label in PLUGIN_BLOCK_SITES:
        if tpidr != PLUGIN_OWNER:
            return False
        if label == "DM_PLUGIN4_BLOCK_ENTRY" and bks._reg(frame, "x0") != PLUGIN_BLOCK:
            return False
        payload = {
            "plugin_block": PLUGIN_BLOCK,
            "x0": bks._reg(frame, "x0"), "x1": bks._reg(frame, "x1"),
            "x19": bks._reg(frame, "x19"), "x20": bks._reg(frame, "x20"),
            "x21": bks._reg(frame, "x21"), "x28": bks._reg(frame, "x28"),
        }
        if label in {"DM_PLUGIN4_EBC4_CALL", "DM_PLUGIN4_EC1C_CALL"}:
            slot = 0x2F558 if label == "DM_PLUGIN4_EBC4_CALL" else 0x2F9C0
            selector_pointer = _u64(bks._read(process, parent.MAIN_BASE + slot, 8), 0)
            payload["selector_pointer"] = selector_pointer
            payload["selector_bytes"] = (
                bks._read(process, selector_pointer, 0xc0).hex() if selector_pointer else ""
            )
            payload["receiver_bytes"] = bks._read(process, payload["x0"], 0x50).hex()
        if label == "DM_PLUGIN4_EC1C_CALL":
            PLUGIN_OBJECT = payload["x0"]
            for name, offset in PLUGIN_INNER_SITES.items():
                parent._arm(debugger, name, parent.MAIN_BASE + offset)
        parent._emit(frame, label, payload)
        parent._disable(label)
        return False
    if label in PLUGIN_INNER_SITES:
        if tpidr != PLUGIN_OWNER:
            return False
        obj = bks._reg(frame, "x0")
        if label in {"DM_PLUGIN4_MIGRATION_ENTRY", "DM_PLUGIN4_NESTED_ENTRY_13224"} and obj != PLUGIN_OBJECT:
            return False
        payload = {
            "plugin_object": PLUGIN_OBJECT, "x0": obj,
            "x2": bks._reg(frame, "x2"), "x3": bks._reg(frame, "x3"),
            "x4": bks._reg(frame, "x4"),
        }
        if label in {"DM_PLUGIN4_MIGRATION_CALL_130E0", "DM_PLUGIN4_NESTED_CALL_13364"}:
            slot = 0x2F330 if label == "DM_PLUGIN4_MIGRATION_CALL_130E0" else 0x2F4A0
            selector_pointer = _u64(bks._read(process, parent.MAIN_BASE + slot, 8), 0)
            payload["selector_pointer"] = selector_pointer
            payload["selector_bytes"] = (
                bks._read(process, selector_pointer, 0xc0).hex() if selector_pointer else ""
            )
        if label == "DM_PLUGIN4_NESTED_CALL_13364":
            COORDINATOR = obj
            RUN_EVENT_BLOCK = payload["x2"]
            payload["run_event_block"] = RUN_EVENT_BLOCK
            payload["run_event_block_bytes"] = bks._read(process, RUN_EVENT_BLOCK, 0x58).hex()
            for name, offset in PLUGIN_COORD_SITES.items():
                parent._arm(debugger, name, parent.MAIN_BASE + offset)
            for name, offset in PLUGIN_RUN_BLOCK_SITES.items():
                parent._arm(debugger, name, parent.MAIN_BASE + offset)
        parent._emit(frame, label, payload)
        parent._disable(label)
        return False
    if label in PLUGIN_COORD_SITES:
        if tpidr != PLUGIN_OWNER:
            return False
        obj = bks._reg(frame, "x0")
        if label == "DM_PLUGIN4_COORD_ENTRY" and (
            obj != COORDINATOR or bks._reg(frame, "x3") != PLUGIN_OBJECT
        ):
            return False
        parent._emit(frame, label, {
            "plugin_object": PLUGIN_OBJECT, "coordinator": COORDINATOR,
            "run_event_block": RUN_EVENT_BLOCK, "x0": obj,
            "x1": bks._reg(frame, "x1"), "x2": bks._reg(frame, "x2"),
        })
        parent._disable(label)
        return False
    if label in PLUGIN_RUN_BLOCK_SITES:
        if label == "DM_PLUGIN4_RUN_BLOCK_ENTRY":
            block = bks._reg(frame, "x0")
            raw = bks._read(process, block, 0x58)
            if _u64(raw, 0x20) != PLUGIN_OBJECT:
                return False
            RUN_EVENT_OWNER = tpidr
            payload = {"block": block, "block_bytes": raw.hex(), "plugin_object": PLUGIN_OBJECT}
        else:
            if tpidr != RUN_EVENT_OWNER:
                return False
            payload = {"block": RUN_EVENT_BLOCK, "plugin_object": PLUGIN_OBJECT,
                       "x0": bks._reg(frame, "x0"), "x1": bks._reg(frame, "x1")}
            if label in {"DM_PLUGIN4_RUN_BLOCK_CALL_136BC", "DM_PLUGIN4_RUN_BLOCK_CALL_13768"}:
                slot = 0x2F9B8 if label == "DM_PLUGIN4_RUN_BLOCK_CALL_136BC" else 0x2FAE0
                selector_pointer = _u64(bks._read(process, parent.MAIN_BASE + slot, 8), 0)
                payload["selector_pointer"] = selector_pointer
                payload["selector_bytes"] = (
                    bks._read(process, selector_pointer, 0x40).hex() if selector_pointer else ""
                )
            if label == "DM_PLUGIN4_RUN_BLOCK_CALL_13768":
                payload["arguments"] = {name: bks._reg(frame, name)
                                        for name in ("x2", "x3", "x4", "x5")}
                payload["receiver_bytes"] = bks._read(process, payload["x0"], 0x40).hex()
                class Reader:
                    def read(self, address, size):
                        raw = bks._read(process, address, size)
                        if len(raw) != size:
                            raise RuntimeError("unreadable object")
                        return raw

                for name in ("x2", "x3"):
                    pointer = payload["arguments"][name]
                    payload[name + "_bytes"] = bks._read(process, pointer, 0x40).hex()
                    try:
                        payload[name + "_string"] = string(
                            Reader(), pointer, bks.SLIDE[0] + 0x1e6f0c220
                        )
                    except (ValueError, RuntimeError) as error:
                        payload[name + "_string_error"] = str(error)
        parent._emit(frame, label, payload)
        parent._disable(label)
        return False
    if label == "DM_PLUGIN4_DEADLINE_ENTRY":
        global PLUGIN_DEADLINE_HITS
        PLUGIN_DEADLINE_HITS += 1
        obj = bks._reg(frame, "x0")
        raw = bks._read(process, obj, 0x50)
        name_pointer = _u64(raw, 0x18)

        class Reader:
            def read(self, address, size):
                data = bks._read(process, address, size)
                if len(data) != size:
                    raise RuntimeError("unreadable plugin name")
                return data

        try:
            name = string(Reader(), name_pointer, bks.SLIDE[0] + 0x1e6f0c220) if name_pointer else "<null>"
        except (ValueError, RuntimeError) as error:
            name = str(error)
        parent._emit(frame, label, {
            "hit": PLUGIN_DEADLINE_HITS, "object": obj, "name": name,
            "name_pointer": name_pointer, "object_bytes": raw.hex(),
            "before_watchdog_reboot": raw[0x28:0x38].hex(),
            "source_plugin": bks._reg(frame, "x19"),
        })
        if PLUGIN_DEADLINE_HITS >= 16:
            parent._disable(label)
        return False
    return parent.on_hit(frame, bp_loc, context)


def install(debugger, slide):
    global BLOCK, BLOCK_CAPTURE, NESTED_SOURCE_INVOKE
    global QUEUE, BLOCK_OWNER, QUEUED_BLOCK, OTHER_BLOCK_ENTRIES
    global PLUGIN_BLOCK, PLUGIN_OWNER, PLUGIN_OBJECT
    global RUN_EVENT_BLOCK, RUN_EVENT_OWNER, COORDINATOR
    global PLUGIN_DEADLINE_HITS
    BLOCK = BLOCK_CAPTURE = NESTED_SOURCE_INVOKE = None
    QUEUE = BLOCK_OWNER = QUEUED_BLOCK = None
    OTHER_BLOCK_ENTRIES = 0
    PLUGIN_BLOCK = PLUGIN_OWNER = PLUGIN_OBJECT = None
    RUN_EVENT_BLOCK = RUN_EVENT_OWNER = COORDINATOR = None
    PLUGIN_DEADLINE_HITS = 0
    STARTER_COUNTS.clear()
    parent.install(debugger, slide)
