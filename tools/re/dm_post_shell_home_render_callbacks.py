"""Install validated SpringBoard home callbacks and post-home render ladder.

This is a small post-shell composition module selected by
dm_post_sync_ladder_callbacks. It command-imports each callback module before
installation so LLDB can resolve their registered Python breakpoint commands.
All sites are observational; the IOMFB A408 site only records a hit.
"""

from pathlib import Path
import sys
import os

import lldb


HERE = Path(__file__).resolve().parent
IMPORTS = (
    ("render_gate_callbacks", HERE / "render_gate_callbacks.py"),
    ("welcome_abort_callbacks", HERE / "welcome_abort_callbacks.py"),
)
MODULES = (
    ("dm_post_shell_home_min_callbacks", HERE / "dm_post_shell_home_min_callbacks.py"),
    ("dm_posthome_present_callbacks", HERE / "dm_posthome_present_callbacks.py"),
    # Arm before the host-side DVMI2 trigger is allowed to send its single
    # Home/swipe sequence. These callbacks distinguish QMP/helper ACK from
    # delivery through BackBoard, SpringBoard, and UIKit hit testing.
    ("dm_unlock_input_callbacks", HERE / "dm_unlock_input_callbacks.py"),
)

CAN_UPDATE_MODULE = (
    "dm_posthome_can_update_callbacks",
    HERE / "dm_posthome_can_update_callbacks.py",
)


def _command_import(debugger, module_name, module_path):
    interpreter = debugger.GetCommandInterpreter()
    command_result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("command script import %s" % module_path,
                              command_result)
    if not command_result.Succeeded():
        raise RuntimeError("LLDB import failed for %s: %s" %
                           (module_name, command_result.GetError() or
                            command_result.GetOutput()))
    module = sys.modules.get(module_name)
    if module is None:
        raise RuntimeError("LLDB import did not register %s" % module_name)
    return module


def install(debugger, slide):
    # dm_unlock_input_callbacks uses the existing process-name and ObjC-class
    # readers from these modules. Import them explicitly in LLDB so a fresh
    # child does not depend on another probe having loaded either module.
    for name, path in IMPORTS:
        _command_import(debugger, name, path)
    selected = list(MODULES)
    can_update_enabled = os.environ.get("DVM_POSTHOME_CAN_UPDATE", "0") == "1"
    if can_update_enabled:
        selected.insert(2, CAN_UPDATE_MODULE)
    modules = [_command_import(debugger, name, path)
               for name, path in selected]
    for module in modules:
        module.install(debugger, slide)
    print("POST_SHELL_HOME_RENDER_READY slide=0x%x modules=%s" %
          (int(slide), ",".join(name for name, _ in selected)), flush=True)
