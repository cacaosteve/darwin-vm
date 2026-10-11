"""Arm the established ADFL/home, poster, CA, and A407 probes after shell.

This module is loaded dynamically by dm_post_sync_ladder_callbacks at the
matched SpringBoard SHELL_SERVICE_RETURN. It keeps the long pre-shell
migration window light and does not change guest state.
"""

from pathlib import Path
import json
import time

import lldb

import bks_checkin_callbacks as bks
import dm_lock_attach_callbacks as attach


def install(debugger, slide):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % Path(attach.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("lock/home callback import failed: %s" %
                           (result.GetError() or result.GetOutput()))

    # attach.install imports and composes the corrected lock/CA draw hooks,
    # ADFL/workspace/home markers, and non-hidden-cull poster callbacks.
    attach.install(debugger, int(slide))
    row = {
        "label": "POST_SHELL_UI_PROBE_READY",
        "time": time.time(),
        "slide": "0x%x" % int(slide),
        "hook_set": "dm_lock_attach_callbacks + dm_lock_draw_callbacks + "
                    "dm_repaired_ls_adfl_home_callbacks",
        "hidden_cull": "excluded",
    }
    bks._write_event("ready.post-shell-ui-probes.json", row)
    print("POST_SHELL_UI_PROBES_READY " + json.dumps(row, sort_keys=True),
          flush=True)
