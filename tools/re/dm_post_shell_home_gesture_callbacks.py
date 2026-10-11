"""Minimal read-only Home lifecycle and unlock-gesture observers.

This deliberately omits CoreAnimation, renderer, framebuffer, and broad
BackBoard input hooks. It is intended for a native swipe capture where LLDB
must stay attached through SpringBoard gesture recognition.
"""

import json
import sys
from pathlib import Path

import lldb


HERE = Path(__file__).resolve().parent


def _import(debugger, name, path):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % path, result)
    if not result.Succeeded():
        raise RuntimeError("LLDB import failed for %s: %s" %
                           (name, result.GetError() or result.GetOutput()))
    module = sys.modules.get(name)
    if module is None:
        raise RuntimeError("LLDB did not register %s" % name)
    return module


def install(debugger, slide):
    home = _import(debugger, "dm_post_shell_home_min_callbacks",
                   HERE / "dm_post_shell_home_min_callbacks.py")
    gesture = _import(debugger, "dm_lock_gesture_callbacks",
                      HERE / "dm_lock_gesture_callbacks.py")
    home.install(debugger, int(slide))
    gesture.install(debugger, int(slide))
    print("POST_SHELL_HOME_GESTURE_READY " + json.dumps({
        "slide": "0x%x" % int(slide),
        "home_sites": 7,
        "gesture_sites": len(gesture.USER_SITES),
        "renderer_hooks": False,
        "backboard_input_hooks": False,
    }, sort_keys=True), flush=True)
