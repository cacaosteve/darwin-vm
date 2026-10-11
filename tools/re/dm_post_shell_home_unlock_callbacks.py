"""Compose SpringBoard, input-route, and post-gesture surface observers.

All probes are read-only and auto-continue. The IOSurface sampler is held
behind an event-directory marker that the host creates only after one ordinary
gesture receives a helper ACK, preserving its bounded capture budget for the
resulting UI state.
"""

import json
import os
from pathlib import Path
import sys

import lldb


HERE = Path(__file__).resolve().parent


def _command_import(debugger, module_name, module_path):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % module_path, result)
    if not result.Succeeded():
        raise RuntimeError("LLDB import failed for %s: %s" %
                           (module_name, result.GetError() or result.GetOutput()))
    module = sys.modules.get(module_name)
    if module is None:
        raise RuntimeError("LLDB did not register %s" % module_name)
    return module


def install(debugger, slide):
    home = _command_import(
        debugger, "dm_post_shell_home_min_callbacks",
        HERE / "dm_post_shell_home_min_callbacks.py")
    input_probe = _command_import(
        debugger, "dm_unlock_input_callbacks",
        HERE / "dm_unlock_input_callbacks.py")
    surface_probe = _command_import(
        debugger, "dm_unlock_surface_callbacks",
        HERE / "dm_unlock_surface_callbacks.py")

    event_dir = os.environ.get("DVM_PROBE_EVENT_DIR", "/tmp/dvm/unlock-events")
    os.makedirs(event_dir, exist_ok=True)
    start_file = str(Path(event_dir) / "capture-after-input")
    os.environ["DVM_UNLOCK_SURFACE_START_FILE"] = start_file

    home.install(debugger, slide)
    input_probe.install(debugger, slide)
    surface_probe.install(debugger, slide, event_dir)
    row = {
        "label": "POST_SHELL_UNLOCK_READY",
        "slide": "0x%x" % int(slide),
        "event_dir": event_dir,
        "surface_start_file": start_file,
        "surface_samples_gated_until_acknowledged_input": True,
    }
    print("POST_SHELL_UNLOCK_READY " + json.dumps(row, sort_keys=True),
          flush=True)
