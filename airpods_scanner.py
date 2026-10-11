"""
AirPods BLE scanner - Phase 1 of a MagicPods-style app.

Listens for Apple "proximity pairing" advertisements (manufacturer 0x004C,
message type 0x07) and decodes battery, charging, in-ear, lid and model info.

Byte layout (community-documented, verify against your own captures):
  [0]  0x07        message type (proximity pairing)
  [1]  0x19        payload length (25)
  [2]  0x01        prefix
  [3:5]            model id (e.g. 0E 20 = AirPods Pro)
  [5]  status      0x20 sender is the left bud   0x40 sender is the secondary bud
                   0x02 primary in ear           0x04 primary in case
                   0x08 secondary in ear         0x10 secondary in case
                   (primary = the bud currently connected to the phone/PC)
  [6]  battery     high nibble = sender, low nibble = other bud
                   0-10 = 0-100%, 15 = unknown
  [7]  charge/case high nibble: 0x1 sender charging, 0x2 other charging, 0x4 case charging
                   low nibble = case battery
  [8]  lid         low 3 bits = lid-open counter (changes when the lid opens)
                   0x08 = lid closed (set the moment it shuts; the buds keep
                          broadcasting for a few seconds afterwards)
                   0x20 = sender is in the case
  [9]  color
  [10] 0x00
  [11:] encrypted  (needs a key obtained over AAP - Phase 2)

Usage:
  python airpods_scanner.py             # decoded output, only on change
  python airpods_scanner.py --raw       # also print raw hex
  python airpods_scanner.py --rssi -80  # widen range (default -70)
"""

import argparse
import asyncio
from dataclasses import dataclass

from bleak import BleakScanner

APPLE_COMPANY_ID = 0x004C
PROXIMITY_PAIRING = 0x07

MODELS = {
    0x0220: "AirPods (1st gen)",
    0x0F20: "AirPods (2nd gen)",
    0x1320: "AirPods (3rd gen)",
    0x0E20: "AirPods Pro",
    0x1420: "AirPods Pro 2",
    0x2420: "AirPods Pro 2 (USB-C)",
    0x2720: "AirPods Pro 3",
    0x0A20: "AirPods Max",
    0x1F20: "AirPods Max (USB-C)",
    0x0520: "BeatsX",
    0x0620: "Beats Solo3",
    0x0B20: "Powerbeats Pro",
}


@dataclass(frozen=True)
class AirPodsState:
    model: str
    left: int | None
    right: int | None
    case: int | None
    left_charging: bool
    right_charging: bool
    case_charging: bool
    left_in_ear: bool
    right_in_ear: bool
    left_in_case: bool
    right_in_case: bool
    lid_counter: int
    color: int
    lid_closed: bool = False


def battery(nibble: int) -> int | None:
    if nibble <= 10:
        return nibble * 10
    return None  # 15 = unknown / not present


def decode(data: bytes) -> AirPodsState | None:
    # Other Apple devices also send type 0x07 with different lengths - AirPods use 0x19.
    if len(data) < 11 or data[0] != PROXIMITY_PAIRING or data[1] != 0x19:
        return None

    model_id = (data[3] << 8) | data[4]
    status = data[5]
    sender_is_left = bool(status & 0x20)
    sender_is_primary = not (status & 0x40)
    primary_is_left = sender_is_left == sender_is_primary

    # Battery and charging are relative to the sending bud.
    sender_nib, other_nib = data[6] >> 4, data[6] & 0x0F
    left_nib, right_nib = (sender_nib, other_nib) if sender_is_left else (other_nib, sender_nib)

    charge_flags = data[7] >> 4
    case_nib = data[7] & 0x0F
    sender_chg = bool(charge_flags & 0b0001)
    other_chg = bool(charge_flags & 0b0010)
    left_chg, right_chg = (sender_chg, other_chg) if sender_is_left else (other_chg, sender_chg)
    case_chg = bool(charge_flags & 0b0100)

    # Ear/case bits are relative to the primary bud.
    p_ear, p_case = bool(status & 0x02), bool(status & 0x04)
    s_ear, s_case = bool(status & 0x08), bool(status & 0x10)
    if primary_is_left:
        left_in, right_in, left_case, right_case = p_ear, s_ear, p_case, s_case
    else:
        left_in, right_in, left_case, right_case = s_ear, p_ear, s_case, p_case

    return AirPodsState(
        model=MODELS.get(model_id, f"Unknown (0x{model_id:04X})"),
        left=battery(left_nib),
        right=battery(right_nib),
        case=battery(case_nib),
        left_charging=left_chg,
        right_charging=right_chg,
        case_charging=case_chg,
        left_in_ear=left_in,
        right_in_ear=right_in,
        left_in_case=left_case,
        right_in_case=right_case,
        lid_counter=data[8] & 0x07,
        lid_closed=bool(data[8] & 0x08),
        color=data[9],
    )


def fmt_batt(value: int | None, charging: bool) -> str:
    text = "--" if value is None else f"{value}%"
    return text + (" (charging)" if charging else "")


def where(in_ear: bool, in_case: bool) -> str:
    return "in ear" if in_ear else "in case" if in_case else "out"


def print_state(s: AirPodsState, rssi: int) -> None:
    print(
        f"[{rssi} dBm] {s.model}\n"
        f"  Left : {fmt_batt(s.left, s.left_charging):<16} {where(s.left_in_ear, s.left_in_case)}\n"
        f"  Right: {fmt_batt(s.right, s.right_charging):<16} {where(s.right_in_ear, s.right_in_case)}\n"
        f"  Case : {fmt_batt(s.case, s.case_charging)}\n"
        f"  Lid opens: {s.lid_counter}  Color: 0x{s.color:02X}\n"
    )


async def main(min_rssi: int, raw: bool) -> None:
    last: AirPodsState | None = None

    def on_advert(device, adv):
        nonlocal last
        data = adv.manufacturer_data.get(APPLE_COMPANY_ID)
        if not data or adv.rssi < min_rssi:
            return
        state = decode(bytes(data))
        if state is None:
            return
        if raw:
            print(f"raw {device.address}: {bytes(data).hex(' ')}")
        if state != last:
            last = state
            print_state(state, adv.rssi)

    print(f"Scanning for AirPods (RSSI >= {min_rssi} dBm). Open the case near the PC. Ctrl+C to stop.\n")
    async with BleakScanner(detection_callback=on_advert):
        while True:
            await asyncio.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Decode AirPods BLE advertisements")
    parser.add_argument("--rssi", type=int, default=-70, help="minimum signal strength (default -70)")
    parser.add_argument("--raw", action="store_true", help="print raw advertisement bytes")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.rssi, args.raw))
    except KeyboardInterrupt:
        pass
