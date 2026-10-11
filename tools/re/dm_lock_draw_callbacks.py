"""Thin, read-only JOIN2 follow-up: scene -> accepted node -> OGL -> A407.

Reuses dm_lock_rich_callbacks' decoders and the existing home/swap controls,
but omits the high-volume global CA/cover-sheet sites during a long boot.
"""

import dm_lock_rich_callbacks as rich
import lldb
from pathlib import Path


KEEP = {
    "TIME_STRING_UPDATE",
    "POSTER_LAYERS_UPDATE",
    "POSTER_HOST_SET_SCENE",
    "CA_CONTEXT_SET_LAYER",
    "SERVER_RENDER_UPDATE",
    "SERVER_HOSTED_RESOLVE_CALL",
    "SERVER_HOSTED_RESOLVE_RETURN",
    "SERVER_HOSTED_POSTER_INCLUDE",
    "SERVER_HOSTED_SECURE_RESULT",
    "SERVER_HOSTED_POLICY_RESULT",
    "SERVER_HOSTED_POSTER_ACCEPT",
    "SERVER_HOSTED_POSTER_REJECT",
    "SERVER_HOSTED_POST_ACCEPT",
    "SERVER_HOSTED_POSTER_LAYER_GATE",
    "SERVER_HOSTED_ALLOWED_UPDATE",
    "SERVER_HOSTED_HIDDEN_CULL_RESULT",
    "SERVER_HOSTED_HIDDEN_CULLED",
    "SERVER_HOSTED_PREPARED_STATE",
    "TIME_COPY_RENDER_LAYER",
    "TIME_RENDER_LAYER_ALLOCATED",
    "TIME_RENDER_CONTENTS_RESULT",
    "SERVER_DISPLAY_RENDER",
    "SERVER_OGL_RENDER",
    "SERVER_OGL_PREPARE_LAYER",
    "SERVER_OGL_ALLOC_LAYER",
    "SERVER_OGL_RENDER_LAYERS",
    "SERVER_OGL_RENDER_CONTENTS",
}


def install(debugger, slide):
    # LLDB resolves scripted breakpoint callbacks through command-imported
    # modules. Import rich explicitly; a plain Python import leaves its
    # dm_lock_rich_callbacks.on_hit callbacks unresolved at the first hit.
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % Path(rich.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("rich callback import failed: %s" % result.GetError())
    rich.SITES = tuple(site for site in rich.SITES if site[1] in KEEP)
    rich.install(debugger, slide)
    print("DM_LOCK_DRAW_READY rich_sites=%d" % len(rich.SITES), flush=True)
