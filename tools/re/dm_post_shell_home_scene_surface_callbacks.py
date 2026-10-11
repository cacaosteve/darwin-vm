"""Post-shell Home probes plus early Setup scene and first-surface tracing.

The ordinary post-shell bundle observes SpringBoard after shell return. This
variant additionally arms FrontBoard scene creation immediately, arms Setup's
BuddySceneDelegate checkpoints at UIKit's UIApplicationMain entry, and installs
the existing read-only QuartzCore/IOMFB first-surface trace. It is intended for
a fresh child where Setup may launch well after the shell-service return.
"""

import os
from pathlib import Path
import sys

import lldb

HERE = Path(__file__).resolve().parent


def _command_import(debugger, module_name, module_path):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % module_path.resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("LLDB import failed for %s: %s" %
                           (module_name, result.GetError() or result.GetOutput()))
    module = sys.modules.get(module_name)
    if module is None:
        raise RuntimeError("LLDB did not register %s" % module_name)
    return module


def install(debugger, slide):
    # Keep Home-entity tracking and the validated lock-recognizer/render
    # bundle.  The smaller render bundle omits the recognizer callbacks, which
    # are necessary to distinguish delivered swipes from an accepted unlock.
    home = _command_import(
        debugger, "dm_post_shell_home_min_callbacks",
        HERE / "dm_post_shell_home_min_callbacks.py")
    home.install(debugger, slide)
    gesture_present = _command_import(
        debugger, "dm_post_shell_home_gesture_present_callbacks",
        HERE / "dm_post_shell_home_gesture_present_callbacks.py")
    gesture_present.install(debugger, slide)
    input_callbacks = _command_import(
        debugger, "dm_unlock_input_callbacks",
        HERE / "dm_unlock_input_callbacks.py")
    input_callbacks.install(debugger, slide)

    # Catch later scene creation in either client/host agent, rather than
    # interpreting a late-armed Setup-only checkpoint as a negative.
    scene_pipeline = _command_import(
        debugger, "scene_pipeline_callbacks", HERE / "scene_pipeline_callbacks.py")
    scene_pipeline.install(debugger, slide)

    # This anchor is UIKitCore's _UIApplicationMain entry. It sees Setup before
    # UIKit connects the scene and computes that process's executable slide
    # from the live return address.
    setup_scene = _command_import(
        debugger, "setup_scene_callbacks", HERE / "setup_scene_callbacks.py")
    setup_scene.install_on_launch(debugger, slide)

    # The kernel half is read-only and limited to the DART/surface path needed
    # to name the first IOSurface handoff. Keep it slim and never stop on success.
    os.environ["DVM_FIRST_SURFACE_IOMMU_SLIM"] = "1"
    surface = _command_import(
        debugger, "first_surface_callbacks", HERE / "first_surface_callbacks.py")
    event_dir = os.environ.get("DVM_PROBE_EVENT_DIR", "")
    if event_dir:
        surface.EVENT_DIR = str(Path(event_dir) / "first-surface")
    surface.STOP_ON_SUCCESS = False
    surface.install(debugger, slide)

    print("POST_SHELL_SCENE_SURFACE_READY slide=0x%x modules="
          "home-entity,lock-gesture-render,input-path,scene-pipeline,"
          "setup-scene-from-UIApplicationMain,"
          "first-surface-iommu-slim" % int(slide), flush=True)
