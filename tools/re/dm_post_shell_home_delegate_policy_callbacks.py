"""Post-shell probe combining the normal gesture/render observer with
read-only Cover Sheet delegate decision checkpoints.
"""

import os
import sys

import lldb


HERE = os.path.dirname(os.path.abspath(__file__))


def _import(debugger, name, filename):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % os.path.join(HERE, filename), result)
    if not result.Succeeded():
        raise RuntimeError(result.GetError() or result.GetOutput())
    module = sys.modules.get(name)
    if module is None:
        raise RuntimeError("LLDB did not register %s" % name)
    return module


def install(debugger, slide):
    base = _import(debugger,
                   "dm_post_shell_home_gesture_present_callbacks",
                   "dm_post_shell_home_gesture_present_callbacks.py")
    base.install(debugger, int(slide))
    policy = _import(debugger, "dm_lock_delegate_policy_callbacks",
                     "dm_lock_delegate_policy_callbacks.py")
    policy.install(debugger, int(slide))
    print("POST_SHELL_HOME_DELEGATE_POLICY_READY slide=%s" % hex(int(slide)),
          flush=True)
