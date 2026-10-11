"""Compose the early AP-MIPI trace with the light migration/Home observer.

This module is imported by LLDB at reset and again after live slide
resolution.  The early session only installs the kernel start/property
probes; the main session re-arms those kernel probes and adds the gate-light
and post-shell UI callbacks.  All callbacks are observational.
"""

from pathlib import Path
import sys

import lldb


HERE = Path(__file__).resolve().parent


def _command_import(debugger, name, path):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % path.resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("LLDB import failed for %s: %s" %
                           (name, result.GetError() or result.GetOutput()))
    module = sys.modules.get(name)
    if module is None:
        raise RuntimeError("LLDB import did not register %s" % name)
    return module


def _modules(debugger):
    render = _command_import(
        debugger, "render_gate_callbacks", HERE / "render_gate_callbacks.py")
    gate = _command_import(
        debugger, "dm_gate_light_callbacks", HERE / "dm_gate_light_callbacks.py")
    return render, gate


def install_kernel(debugger):
    """Install only the early MIPI kernel probes while QEMU is at reset."""
    render, _gate = _modules(debugger)
    return render.install_kernel(debugger)


def install(debugger, slide):
    """Re-arm MIPI probes, then install gate and deferred Home observers."""
    render, gate = _modules(debugger)
    render.install(debugger, int(slide))
    gate.install(debugger, int(slide))
    print("GATE_LIGHT_MIPI_READY slide=0x%x" % int(slide), flush=True)
