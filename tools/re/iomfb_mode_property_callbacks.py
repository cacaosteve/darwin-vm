"""Narrow BackBoard mode-property observer used with setup_gate_probe.

This delegates to the already-tested provider-only installer and avoids
arming the render, migration, or CLCD2 helper forests for a primary-property
and IORegistry-match capture.
"""

from pathlib import Path
import sys

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

def install(debugger, slide):
    import lldb

    path = Path(_HERE) / "render_gate_callbacks.py"
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % path, result)
    if not result.Succeeded():
        raise RuntimeError("could not register render-gate callbacks: %s" %
                           (result.GetError() or result.GetOutput()))
    module = sys.modules.get("render_gate_callbacks")
    if module is None:
        raise RuntimeError("render_gate_callbacks was not registered")
    return module.install_iomfb_mode_provider_only(debugger, slide)
