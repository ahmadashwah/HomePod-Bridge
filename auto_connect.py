"""
Auto-connect AirPods to Windows when you open the case or put a bud in your ear.

Connecting uses the Bluetooth audio driver's own one-shot property
(KSPROPSETID_BtAudio / KSPROPERTY_ONESHOT_RECONNECT) - the same thing the
"Connect" button in Windows Bluetooth settings triggers.

Usage:
  python auto_connect.py                # watch and auto-connect
  python auto_connect.py --connect      # connect once now
  python auto_connect.py --disconnect   # disconnect once now
"""

import argparse
import asyncio
import ctypes
import ctypes.wintypes as w
import re
import time
import uuid
import winreg

from bleak import BleakScanner
from winrt.windows.devices.bluetooth import BluetoothConnectionStatus, BluetoothDevice

from airpods_scanner import APPLE_COMPANY_ID, decode

KSCATEGORY_AUDIO = "6994AD04-93EF-11D0-A3CC-00A0C9223196"
KSPROPSETID_BTAUDIO = "7FA06C40-B8F6-4C7E-8556-E8C33A12E54D"
KSPROPERTY_ONESHOT_RECONNECT = 0
KSPROPERTY_ONESHOT_DISCONNECT = 1
IOCTL_KS_PROPERTY = 0x002F0003
A2DP_UUID = "0000110b-0000-1000-8000-00805f9b34fb"

MMDEVICES_RENDER = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
PKEY_DEVICE_PARENT_ID = "{b3f8fa53-0004-438e-9003-51a46e139bfc},2"  # e.g. "{1}.BTHENUM\{0000110B-...}..."
DEVICE_STATE_ACTIVE = 1  # 8 = unplugged


def audio_endpoint_state(mac: int) -> int | None:
    """DeviceState of the stereo ("Headphones") output of the Bluetooth device with this address.

    Matched on the endpoint's parent device id, e.g.
    BTHENUM\\{0000110B-...}_VID&0001004C_PID&2027\\7&...&307AD243C7A4_C00000000 - the A2DP profile
    plus the address. (Matching the name breaks when the AirPods are renamed: Windows keeps the
    name they had when paired.)"""
    address = f"{mac:012x}"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, MMDEVICES_RENDER) as endpoints:
        for i in range(winreg.QueryInfoKey(endpoints)[0]):
            key = winreg.EnumKey(endpoints, i)
            try:
                with winreg.OpenKey(endpoints, key + r"\Properties") as props:
                    parent = winreg.QueryValueEx(props, PKEY_DEVICE_PARENT_ID)[0].lower()
                    if A2DP_UUID not in parent or address not in parent:
                        continue
                with winreg.OpenKey(endpoints, key) as endpoint:
                    return winreg.QueryValueEx(endpoint, "DeviceState")[0]
            except OSError:
                continue
    return None

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_byte * 16)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        g = cls()
        ctypes.memmove(g.data, uuid.UUID(text).bytes_le, 16)
        return g


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("guid", GUID), ("flags", w.DWORD), ("reserved", ctypes.c_void_p)]


class KSPROPERTY(ctypes.Structure):
    _fields_ = [("Set", GUID), ("Id", w.ULONG), ("Flags", w.ULONG)]


setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), w.LPCWSTR, w.HWND, w.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), w.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, w.DWORD,
    ctypes.POINTER(w.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
kernel32.CreateFileW.restype = w.HANDLE
kernel32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
kernel32.DeviceIoControl.argtypes = [
    w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [w.HANDLE]


def list_audio_interfaces() -> list[str]:
    category = GUID.parse(KSCATEGORY_AUDIO)
    devs = setupapi.SetupDiGetClassDevsW(ctypes.byref(category), None, None, 0x12)  # PRESENT | DEVICEINTERFACE
    paths = []
    try:
        i = 0
        while True:
            data = SP_DEVICE_INTERFACE_DATA(cbSize=ctypes.sizeof(SP_DEVICE_INTERFACE_DATA))
            if not setupapi.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(category), i, ctypes.byref(data)):
                break
            need = w.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(data), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            ctypes.cast(buf, ctypes.POINTER(w.DWORD))[0] = 8  # sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W) on x64
            if setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(data), buf, need, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
            i += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)
    return paths


class PairedAirPods:
    """The Apple headphones paired with this PC, found through their A2DP audio driver."""

    def __init__(self, model_id: int | None = None):
        # The BLE model id 0x2720 shows up as PID 2027 in the Windows device id.
        pid = f"pid&{model_id & 0xFF:02x}{model_id >> 8:02x}" if model_id else ""
        for path in list_audio_interfaces():
            p = path.lower()
            if A2DP_UUID in p and "vid&0001004c" in p and pid in p:
                self.ks_path = path
                self.mac = int(re.search(r"&([0-9a-f]{12})_c", p).group(1), 16)
                found_pid = int(re.search(r"pid&([0-9a-f]{4})", p).group(1), 16)
                self.model_id = ((found_pid & 0xFF) << 8) | (found_pid >> 8)  # back to BLE byte order
                return
        raise RuntimeError("No paired AirPods found - pair them in Windows Bluetooth settings first.")

    async def is_connected(self) -> bool:
        """True when the AirPods are this PC's audio output.

        The Bluetooth link itself (BluetoothDevice.connection_status) often stays up after the
        audio disconnects, so instead check the "Headphones" audio endpoint Windows keeps for them.
        """
        state = audio_endpoint_state(self.mac)
        if state is None:  # no endpoint found - fall back to the Bluetooth link
            device = await BluetoothDevice.from_bluetooth_address_async(self.mac)
            return device is not None and device.connection_status == BluetoothConnectionStatus.CONNECTED
        return state == DEVICE_STATE_ACTIVE

    def _oneshot(self, prop_id: int) -> None:
        handle = kernel32.CreateFileW(self.ks_path, 0xC0000000, 3, None, 3, 0, None)  # RW, share RW, OPEN_EXISTING
        if handle in (None, w.HANDLE(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            prop = KSPROPERTY(GUID.parse(KSPROPSETID_BTAUDIO), prop_id, 1)  # KSPROPERTY_TYPE_GET
            returned = w.DWORD()
            if not kernel32.DeviceIoControl(handle, IOCTL_KS_PROPERTY, ctypes.byref(prop), ctypes.sizeof(prop),
                                            None, 0, ctypes.byref(returned), None):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)

    async def connect(self, timeout: float = 10) -> bool:
        self._oneshot(KSPROPERTY_ONESHOT_RECONNECT)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.is_connected():
                return True
            await asyncio.sleep(0.1)
        return False

    def disconnect(self) -> None:
        self._oneshot(KSPROPERTY_ONESHOT_DISCONNECT)


async def watch(min_rssi: int, cooldown: float, verbose: bool) -> None:
    airpods = PairedAirPods()
    print(f"Paired AirPods: {airpods.mac:012X}. Open the case or put a bud in to connect. Ctrl+C to stop.")

    previous: dict[str, tuple] = {}  # per BLE address: (lid counter, left in ear, right in ear)
    started = time.monotonic()
    last_attempt = 0.0
    busy = False

    async def try_connect(reason: str) -> None:
        nonlocal last_attempt, busy
        busy, last_attempt = True, time.monotonic()
        try:
            if await airpods.is_connected():
                return
            print(f"{time.strftime('%H:%M:%S')} {reason} -> connecting...")
            ok = await airpods.connect()
            print(f"{time.strftime('%H:%M:%S')} {'connected' if ok else 'did not connect (still in use by another device?)'}")
        except OSError as e:
            print(f"connect failed: {e}")
        finally:
            busy = False

    def on_advert(device, adv):
        data = adv.manufacturer_data.get(APPLE_COMPANY_ID)
        if not data or adv.rssi < min_rssi:
            return
        s = decode(bytes(data))
        if s is None:
            return
        now = (s.lid_counter, s.left_in_ear, s.right_in_ear)
        before = previous.get(device.address)
        previous[device.address] = now
        if verbose and now != before:
            print(f"  {device.address[-5:]} {adv.rssi} dBm lid#{s.lid_counter} "
                  f"L ear={s.left_in_ear} R ear={s.right_in_ear} case={s.case}")
        warming_up = time.monotonic() - started < 3  # don't grab AirPods that were already open at startup
        if warming_up or busy or time.monotonic() - last_attempt < cooldown:
            return
        # AirPods switch to a new random address when the lid opens, so a fresh
        # address that shows the case open (case battery visible) or a bud in an ear counts too.
        if before is None:
            if s.case is None and not (s.left_in_ear or s.right_in_ear):
                return
            reason = "case opened"
        elif now[0] != before[0]:
            reason = "case opened"
        elif (s.left_in_ear and not before[1]) or (s.right_in_ear and not before[2]):
            reason = "bud put in ear"
        else:
            return
        asyncio.get_running_loop().create_task(try_connect(reason))

    async with BleakScanner(detection_callback=on_advert):
        while True:
            await asyncio.sleep(1)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-connect AirPods to Windows")
    parser.add_argument("--connect", action="store_true", help="connect once and exit")
    parser.add_argument("--disconnect", action="store_true", help="disconnect once and exit")
    parser.add_argument("--rssi", type=int, default=-60, help="minimum signal strength to react to (default -60)")
    parser.add_argument("--cooldown", type=float, default=15, help="seconds between connect attempts")
    parser.add_argument("--verbose", action="store_true", help="print every AirPods state change")
    args = parser.parse_args()

    if args.connect:
        ok = await PairedAirPods().connect()
        print("connected" if ok else "did not connect")
    elif args.disconnect:
        PairedAirPods().disconnect()
        print("disconnect sent")
    else:
        await watch(args.rssi, args.cooldown, args.verbose)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
