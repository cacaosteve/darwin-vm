"""Composite post-shell UI and SpringBoard setup-progress observer.

This installs the existing guarded home/unlock/render callbacks, then adds a
small register-only observer at ADFL's two setup-mode checks and scene
finalization.  It never injects input; a separate guarded trigger must wait for
a non-null home entity before sending an ordinary upward swipe.
"""

import os
import sys
from pathlib import Path

import lldb


HERE = Path(__file__).resolve().parent


def _command_import(debugger, name, path):
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
    render = _command_import(
        debugger, "dm_post_shell_home_unlock_render_callbacks",
        HERE / "dm_post_shell_home_unlock_render_callbacks.py")
    render.install(debugger, int(slide))

    setup = _command_import(
        debugger, "dm_p150_setup_progress_callbacks",
        HERE / "dm_p150_setup_progress_callbacks.py")
    event_dir = Path(os.environ.get("DVM_PROBE_EVENT_DIR", "/tmp/dvm"))
    setup.install(debugger, int(slide),
                  str(event_dir / "springboard-setup-progress.jsonl"))

    activation = _command_import(
        debugger, "dm_p150_setup_activation_callbacks",
        HERE / "dm_p150_setup_activation_callbacks.py")
    activation.install(
        debugger, int(slide),
        str(event_dir / "springboard-setup-activation.jsonl"))

    epilogue = _command_import(
        debugger, "dm_p150_adfl_epilogue_callbacks",
        HERE / "dm_p150_adfl_epilogue_callbacks.py")
    epilogue.install(
        debugger, int(slide),
        str(event_dir / "springboard-adfl-epilogue.jsonl"))

    scene_start = _command_import(
        debugger, "dm_springboard_scene_start_callbacks",
        HERE / "dm_springboard_scene_start_callbacks.py")
    scene_start.install(
        debugger, int(slide),
        str(event_dir / "springboard-scene-start.jsonl"))

    print("POST_SHELL_HOME_UNLOCK_SETUP_READY slide=0x%x event_dir=%s" %
          (int(slide), event_dir), flush=True)
