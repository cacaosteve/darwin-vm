"""Render-gate trace with first-surface IOMMU-only kernel probes."""
import os
import sys

import lldb

CALLBACK_DIR = os.path.dirname(os.path.abspath(__file__))
if CALLBACK_DIR not in sys.path:
    sys.path.insert(0, CALLBACK_DIR)

# first_surface_callbacks reads this at install time. Set it before importing
# the combined callback module so its IOMFB kernel breakpoint list stays small.
os.environ["DVM_FIRST_SURFACE_IOMMU_SLIM"] = "1"

import render_gate_surface_callbacks


def install(debugger, slide):
    render_gate_surface_callbacks.install(debugger, slide)
