"""Checkpoint-only experiment: request RT memory for software display surfaces.

24A5430a QuartzCore IOMFBDisplay::create_surface (static 0x1847ae308)
passes w6 as IOSurfaceCacheMode. Server::render_update sets creates_cached_
surfaces, choosing 0x400, while IOMFB AP surface_map_dcp requires 0x700.
This changes the allocation request before IOSurfaceCreate; it never changes
an existing mapping, a driver return, or a validation branch. Default read-only.
Enable with RFT_FORCE_SURFACE_CACHE_700=1 (GATE129).
"""
import json
import os
import time
import first_surface_callbacks as fs

ENABLE = os.environ.get("RFT_FORCE_SURFACE_CACHE_700", "") == "1"
SLIDE = 0


def request(frame, bp_loc, _dict):
    process = frame.GetThread().GetProcess()
    if fs._progname(process) != 'backboardd':
        return False
    pc = fs._reg(frame, 'pc')
    display = fs._reg(frame, 'x0')
    before = fs._reg(frame, 'x6')
    flags = fs._u32(process, display + 0x3b8) if display else None
    caps = fs._u32(process, 0xfffffc020)
    event = dict(time=time.time(), pc=pc, display=display, flags=flags,
                 capabilities=caps, requested_cache=before, enabled=ENABLE)
    if ENABLE and before in (0x400, 0x700):
        # Require RT capability bit when forcing 0x700 (DISPLAY_RT_R24 contract).
        if caps is not None and (caps & 0x800):
            frame.FindRegister('x6').SetValueFromCString('0x700')
            event['requested_cache'] = 0x700
        else:
            event['force_skipped'] = 'no-uc-normal-memory-cap'
    print('SURFACE_CACHE_REQUEST ' + json.dumps(event, sort_keys=True))
    fs._write_event('surface-cache-request.json', event)
    return False


def install(debugger, slide):
    global SLIDE
    SLIDE = slide
    fs.SLIDE[0] = slide
    target = debugger.GetSelectedTarget()
    bp = target.BreakpointCreateByAddress(slide + 0x1847ae308)
    bp.SetScriptCallbackFunction(__name__ + '.request')
    print('SURFACE_CACHE_PROBE id=%d enabled=%s' % (bp.GetID(), ENABLE))
    return bp.GetID()
