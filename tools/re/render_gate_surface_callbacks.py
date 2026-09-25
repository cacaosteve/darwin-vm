"""Combine render-gate bounds diagnostics with first-IOSurface capture."""
import os
import sys

import lldb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import first_surface_callbacks
import render_gate_callbacks
import ca_msg36_callbacks
import ca_msg36_send_callbacks


def install(debugger, slide):
    # Keep the bounds-from-display intervention in the render-gate callbacks,
    # while the surface callbacks capture the actual IOSurface at fb_swap.
    interpreter = debugger.GetCommandInterpreter()
    callback_dir = os.path.dirname(os.path.abspath(__file__))
    for filename in ("render_gate_callbacks.py",
                     "first_surface_callbacks.py",
                     "ca_msg36_callbacks.py",
                     "ca_msg36_send_callbacks.py"):
        result = lldb.SBCommandReturnObject()
        path = os.path.join(callback_dir, filename)
        interpreter.HandleCommand("command script import %s" % path, result)
        if not result.Succeeded():
            raise RuntimeError("LLDB could not register %s: %s" %
                               (filename, result.GetError()))
    render_gate_callbacks.install(debugger, slide)
    first_surface_callbacks.install(debugger, slide)
    ca_msg36_callbacks.install(debugger, slide)
    # QC-path send tracer (msgid store + send wrapper). Global syscall traps
    # remain behind DVM_CA_MSG36_SEND_GLOBAL=1 inside the send module.
    if os.environ.get("DVM_CA_MSG36_SEND_TRACE", "1") != "0":
        ca_msg36_send_callbacks.install(debugger, slide)
