"""Compact SpringBoard/home and native-input observers after shell return.

This intentionally leaves the broad CA/backboardd render ladder unarmed. It
keeps only the narrow SpringBoard home/poster markers and the input delivery /
UIKit hit-test markers, which is enough to see whether one ordinary swipe
reaches a lock or home surface without adding the historical migration and
display breakpoint forests.
"""

from pathlib import Path
import sys

import lldb


HERE = Path(__file__).resolve().parent
IMPORTS = (
    ("render_gate_callbacks", HERE / "render_gate_callbacks.py"),
    ("welcome_abort_callbacks", HERE / "welcome_abort_callbacks.py"),
    ("dm_post_shell_home_min_callbacks",
     HERE / "dm_post_shell_home_min_callbacks.py"),
    ("dm_unlock_input_callbacks", HERE / "dm_unlock_input_callbacks.py"),
)


def _command_import(debugger, module_name, module_path):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % module_path.resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("LLDB import failed for %s: %s" %
                           (module_name, result.GetError() or result.GetOutput()))
    module = sys.modules.get(module_name)
    if module is None:
        raise RuntimeError("LLDB import did not register %s" % module_name)
    return module


def install(debugger, slide):
    modules = [_command_import(debugger, name, path)
               for name, path in IMPORTS]
    modules[2].install(debugger, slide)
    modules[3].install(debugger, slide)
    print("POST_SHELL_HOME_INPUT_READY slide=0x%x modules=%s" %
          (int(slide), ",".join(name for name, _ in IMPORTS)), flush=True)
