"""Which output endpoint Windows currently considers the default.

PortAudio resolves ``device=None`` to a default exactly once -- when the stream
is opened -- and binds that stream to the endpoint for its lifetime. Windows
moving the default afterwards does not move an already-open stream. That is
invisible right up until something changes the default *after* the app has
started, which for this app is the normal case rather than the exception: the
reader autostarts at sign-in and opens the device immediately (the comfort
noise needs the path open), and a Bluetooth headset or a pair of hearing aids
finishes pairing several seconds later. Everything that watches for the change
follows it; a reader that does not is left talking to the television.

PortAudio cannot answer this question -- its device list, the default index
included, is a snapshot taken at ``Pa_Initialize`` -- so the default is read
from Core Audio directly. ``GetDefaultAudioEndpoint`` is a local call costing
microseconds, which is what makes it cheap enough to poll on a timer.

Only the endpoint *id* is read. It is an opaque string, but it is stable and
comparing two of them is all this needs; the human-readable name is easier to
get from PortAudio once the stream has actually moved.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import POINTER, c_uint32, c_void_p
from typing import Optional

log = logging.getLogger(__name__)

# EDataFlow.eRender / ERole.eMultimedia. eMultimedia rather than eConsole
# because that is the role PortAudio's host APIs resolve a default output to,
# so this tracks the same endpoint the stream would actually be opened on.
_E_RENDER = 0
_E_MULTIMEDIA = 1

_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IMMDevice = "{D666063F-1587-4E43-81F1-B948E807363F}"

# COM objects are apartment-affine, so the enumerator is cached per thread and
# never shared. In practice only the UI thread polls.
_tls = threading.local()
_warned = threading.Event()


def _interfaces():
    """Declare the two Core Audio interfaces, once, on first use.

    Built lazily because importing comtypes is not free and a caller that never
    asks about the default output should not pay for it. Only the methods used
    here are given signatures; the rest are placeholders that exist purely to
    keep the vtable slots in the right order.
    """
    cached = getattr(_interfaces, "_cached", None)
    if cached is not None:
        return cached

    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class IMMDevice(IUnknown):
        _iid_ = GUID(_IID_IMMDevice)
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate"),
            COMMETHOD([], HRESULT, "OpenPropertyStore"),
            COMMETHOD(
                [], HRESULT, "GetId",
                (["out"], POINTER(c_void_p), "ppstrId"),
            ),
            COMMETHOD([], HRESULT, "GetState"),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID(_IID_IMMDeviceEnumerator)
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints"),
            COMMETHOD(
                [], HRESULT, "GetDefaultAudioEndpoint",
                (["in"], c_uint32, "dataFlow"),
                (["in"], c_uint32, "role"),
                (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint"),
            ),
            COMMETHOD([], HRESULT, "GetDevice"),
            COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback"),
            COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback"),
        ]

    _interfaces._cached = (IMMDevice, IMMDeviceEnumerator)
    return _interfaces._cached


def _enumerator():
    enum = getattr(_tls, "enum", None)
    if enum is not None:
        return enum

    import comtypes

    _, IMMDeviceEnumerator = _interfaces()
    try:
        comtypes.CoInitialize()
    except Exception:
        # RPC_E_CHANGED_MODE: the thread is already in a different apartment
        # model. Not a problem, and not worth distinguishing from any other
        # failure here -- if COM is genuinely unusable, CoCreateInstance below
        # is where that shows up.
        pass
    enum = comtypes.CoCreateInstance(
        comtypes.GUID(_CLSID_MMDeviceEnumerator),
        interface=IMMDeviceEnumerator,
        clsctx=comtypes.CLSCTX_ALL,
    )
    _tls.enum = enum
    return enum


def default_output_id() -> Optional[str]:
    """Endpoint id of the current default output, or None if it can't be read.

    None is returned both when the machinery is unavailable and when Windows
    genuinely has no default output (every device unplugged), so callers must
    treat it as "no answer" rather than as a change. Returning None is always
    the safe answer here: it leaves an open stream where it is.
    """
    if sys.platform != "win32" or getattr(_tls, "broken", False):
        return None

    try:
        enum = _enumerator()
    except Exception:
        # comtypes missing or COM refusing to start: there is no point asking
        # again every second for the life of the process.
        _tls.broken = True
        if not _warned.is_set():
            _warned.set()
            log.warning(
                "cannot read the default audio endpoint; the reader will not "
                "follow output device changes", exc_info=True,
            )
        return None

    try:
        device = enum.GetDefaultAudioEndpoint(_E_RENDER, _E_MULTIMEDIA)
        ptr = device.GetId()
    except Exception:
        # E_NOTFOUND when there is no output device at all, which is transient
        # and recovers on its own. Drop the enumerator in case it is the thing
        # that went stale; the next call builds a fresh one.
        _tls.enum = None
        log.debug("could not read the default audio endpoint", exc_info=True)
        return None

    if not ptr:
        return None
    try:
        return ctypes.wstring_at(ptr)
    finally:
        # GetId hands back a CoTaskMemAlloc'd string that belongs to us now.
        # Polling forever without this would leak it a hundred bytes at a time.
        ctypes.windll.ole32.CoTaskMemFree(ctypes.c_void_p(ptr))
