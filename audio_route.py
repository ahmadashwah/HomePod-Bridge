"""Windows audio plumbing: VB-Cable as a "HomePod" output device.

Finds VB-Cable's playback endpoint, names it "HomePod" so it reads like a real
speaker in Windows' sound menus, and switches the default output to and from it.
"""

import ctypes
from ctypes import POINTER, Structure, c_int, c_ulong, c_ushort, c_void_p, c_wchar_p

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
from pycaw.api.endpointvolume import IAudioEndpointVolume
from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
from pycaw.api.mmdeviceapi.depend.structures import PROPERTYKEY as PycawPropertyKey
from pycaw.constants import CLSID_MMDeviceEnumerator

# Matched against the driver part of the name, which survives renaming:
# "HomePod (VB-Audio Virtual Cable)". VB-Cable also adds "CABLE In 16ch".
CABLE_DRIVER_HINT = "vb-audio virtual cable"
CABLE_SKIP_HINT = "16ch"
DEVICE_NAME = "HomePod"


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]


class PROPVARIANT(Structure):
    """Just enough of PROPVARIANT for a string (VT_LPWSTR) value."""

    _fields_ = [
        ("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
        ("pwszVal", c_void_p), ("pad", c_void_p),
    ]


VT_LPWSTR = 31
# The endpoint's own name, the part Settings > Sound > Rename changes.
PKEY_DEVICE_DESC = PROPERTYKEY(GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 2)


class IPolicyConfig(IUnknown):
    """Undocumented interface behind the Sound control panel ("Set Default", rename)."""

    _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
    # Methods that aren't called just hold their vtable slots.
    _methods_ = [
        COMMETHOD([], HRESULT, "GetMixFormat"),
        COMMETHOD([], HRESULT, "GetDeviceFormat"),
        COMMETHOD([], HRESULT, "ResetDeviceFormat"),
        COMMETHOD([], HRESULT, "SetDeviceFormat"),
        COMMETHOD([], HRESULT, "GetProcessingPeriod"),
        COMMETHOD([], HRESULT, "SetProcessingPeriod"),
        COMMETHOD([], HRESULT, "GetShareMode"),
        COMMETHOD([], HRESULT, "SetShareMode"),
        COMMETHOD(
            [], HRESULT, "GetPropertyValue",
            (["in"], c_wchar_p, "deviceId"), (["in"], c_int, "fxStore"),
            (["in"], POINTER(PROPERTYKEY), "key"), (["in"], POINTER(PROPVARIANT), "value"),
        ),
        COMMETHOD(
            [], HRESULT, "SetPropertyValue",
            (["in"], c_wchar_p, "deviceId"), (["in"], c_int, "fxStore"),
            (["in"], POINTER(PROPERTYKEY), "key"), (["in"], POINTER(PROPVARIANT), "value"),
        ),
        COMMETHOD(
            [], HRESULT, "SetDefaultEndpoint",
            (["in"], c_wchar_p, "deviceId"),
            (["in"], c_int, "role"),
        ),
        COMMETHOD(
            [], HRESULT, "SetEndpointVisibility",
            (["in"], c_wchar_p, "deviceId"),
            (["in"], c_int, "visible"),
        ),
    ]


_CLSID_POLICY_CONFIG = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
_ROLES = (0, 1, 2)  # console, multimedia, communications


def _com_init() -> None:
    try:
        comtypes.CoInitialize()
    except OSError:
        pass  # already initialised on this thread


def _policy() -> IPolicyConfig:
    _com_init()
    return comtypes.CoCreateInstance(
        _CLSID_POLICY_CONFIG, IPolicyConfig, comtypes.CLSCTX_ALL
    )


# pycaw's AudioUtilities helpers read every property of every audio device
# (~1 s per call on a typical laptop), which froze the flyout. These ask
# Windows only for the id and name.
_E_RENDER = 0
_E_MULTIMEDIA = 1
_DEVICE_STATE_ACTIVE = 1
_STGM_READ = 0
_PKEY_FRIENDLY_NAME = PycawPropertyKey()
_PKEY_FRIENDLY_NAME.fmtid = GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}")
_PKEY_FRIENDLY_NAME.pid = 14


def _enumerator():
    _com_init()
    return comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER
    )


def active_outputs():
    """(device, id, friendly name) for each enabled playback device."""
    devices = _enumerator().EnumAudioEndpoints(_E_RENDER, _DEVICE_STATE_ACTIVE)
    for i in range(devices.GetCount()):
        dev = devices.Item(i)
        try:
            store = dev.OpenPropertyStore(_STGM_READ)
            name = store.GetValue(ctypes.byref(_PKEY_FRIENDLY_NAME)).GetValue() or ""
        except comtypes.COMError:
            name = ""
        yield dev, dev.GetId(), name


def default_output_id() -> str | None:
    try:
        return _enumerator().GetDefaultAudioEndpoint(_E_RENDER, _E_MULTIMEDIA).GetId()
    except comtypes.COMError:
        return None  # no playback device at all


def set_default_output(device_id: str) -> None:
    policy = _policy()
    for role in _ROLES:
        policy.SetDefaultEndpoint(device_id, role)


def _is_cable(name: str) -> bool:
    name = name.lower()
    return CABLE_DRIVER_HINT in name and CABLE_SKIP_HINT not in name


def find_cable_output_id() -> str | None:
    """Endpoint id of VB-Cable's playback device, if installed."""
    for _dev, dev_id, name in active_outputs():
        if _is_cable(name):
            return dev_id
    return None


def real_speaker_id() -> str | None:
    """First active playback device that isn't a virtual cable."""
    for _dev, dev_id, name in active_outputs():
        name = name.lower()
        if "cable" not in name and "vb-audio" not in name:
            return dev_id
    return None


def device_name(device_id: str) -> str | None:
    value = PROPVARIANT()
    _policy().GetPropertyValue(
        device_id, 0, ctypes.byref(PKEY_DEVICE_DESC), ctypes.byref(value)
    )
    return ctypes.wstring_at(value.pwszVal) if value.vt == VT_LPWSTR else None


def rename_device(device_id: str, name: str) -> None:
    """Same as Settings > Sound > (device) > Rename; no admin rights needed."""
    text = ctypes.create_unicode_buffer(name)
    value = PROPVARIANT(vt=VT_LPWSTR, pwszVal=ctypes.cast(text, c_void_p).value)
    _policy().SetPropertyValue(
        device_id, 0, ctypes.byref(PKEY_DEVICE_DESC), ctypes.byref(value)
    )


def hide_unused_cable_outputs() -> None:
    """Disable VB-Cable's "CABLE In 16ch" output, which the bridge never uses,
    so the sound menu only offers "HomePod". Same as Settings > Sound >
    (device) > Don't allow, and reversible there."""
    for _dev, dev_id, name in active_outputs():
        name = name.lower()
        if CABLE_DRIVER_HINT in name and CABLE_SKIP_HINT in name:
            _policy().SetEndpointVisibility(dev_id, 0)


def ensure_cable_named(name: str = DEVICE_NAME) -> None:
    """Show VB-Cable under the speaker's name (e.g. "Kitchen") in Windows'
    output lists, and hide its other output."""
    cable = find_cable_output_id()
    if cable and device_name(cable) != name:
        rename_device(cable, name)
    hide_unused_cable_outputs()


def _endpoint_volume(device_id: str):
    for dev, dev_id, _name in active_outputs():
        if dev_id == device_id:
            iface = dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
            return iface.QueryInterface(IAudioEndpointVolume)
    return None


class AudioRoute:
    """Point Windows at VB-Cable while connected, then put things back.

    While routed, the cable's Windows volume (volume keys, taskbar slider) is
    what the user turns, so it doubles as the HomePod volume control.
    """

    def __init__(self) -> None:
        self.previous: str | None = None
        self._cable_volume = None

    @property
    def cable_installed(self) -> bool:
        return find_cable_output_id() is not None

    @property
    def routed(self) -> bool:
        return self._cable_volume is not None

    def route_to_cable(self) -> bool:
        cable = find_cable_output_id()
        if cable is None:
            return False
        current = default_output_id()
        if current != cable:
            self.previous = current
            set_default_output(cable)
        else:
            # The user picked "HomePod" in Windows themselves, so there's
            # nothing to go back to; use the real speakers instead.
            self.previous = real_speaker_id()
        self._cable_volume = _endpoint_volume(cable)
        return True

    def restore(self, switch_back: bool = True) -> None:
        """Stop following the cable; switch_back=False leaves the output alone
        (when the user already picked another device in Windows)."""
        self._cable_volume = None
        previous, self.previous = self.previous, None
        if switch_back and previous:
            set_default_output(previous)

    def read_volume(self) -> tuple[float, bool] | None:
        """(level 0-1, muted) of the cable, or None."""
        vol = self._cable_volume
        if vol is None:
            return None
        return vol.GetMasterVolumeLevelScalar(), bool(vol.GetMute())
