"""Offline analyzer for the RZ09-0328 IT8390E-256 EC ROM (256 KiB).

Decodes verified firmware structures without touching any hardware:
  - Header signature & ASCII version at 0x00080..0x000AF
  - Thermal/fan/power mode slots 0..7 at 0x3D000 (SKU 3 active bank)
  - Dynamic ADC current throttling table at 0x35000
  - Direct RPM-to-PWM LUT at 0x2081C and 20% PWM spin floor defaults at 0x2098C
  - Optional byte/table diff against a second EC ROM via --compare
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import struct

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROM = REPO_ROOT / "firmware" / "rz09-0328-ec-v01.00" / "ec-rom-00000-3ffff.bin"

SLOT_NAMES = {
    0: "Balanced",
    1: "Gaming (90W GPU CTGP=1)",
    2: "Creator / Perf",
    3: "Legacy / Mode 3",
    4: "Custom (0x0D07 Sliders)",
    5: "Silent (GPU 30W _QD5)",
    6: "Silent Clone (Slot 6)",
    7: "Silent Clone (Slot 7)",
}


def decode_slot(data: bytes, bank_base: int, slot: int) -> dict:
    b = bank_base + slot * 0x200
    hdr = data[b : b + 8]
    max_step = hdr[0] >> 3
    fans = hdr[0] & 7
    sensor_mask = hdr[1]
    tail_off = struct.unpack("<H", hdr[3:5])[0]
    steps = []
    for st in range(max_step + 1):
        r = data[b + 8 + st * 13 : b + 8 + (st + 1) * 13]
        steps.append({
            "step": st,
            "fan1_rpm": r[0] * 50,
            "fan2_rpm": r[1] * 50,
            "t0": (r[2], r[3]),
            "t1": (r[4], r[5]),
            "t2": (r[6], r[7]),
            "timers_flags": r[8:13].hex(),
        })
    f = data[b + tail_off : b + tail_off + 0x38]
    return {
        "slot": slot,
        "offset": b,
        "max_step": max_step,
        "fans": fans,
        "sensor_mask": sensor_mask,
        "tail_off": tail_off,
        "steps": steps,
        "ac_pl1_w": f[0x18],
        "ac_pl2_w": f[0x19],
        "manual_min_rpm": f[0x21] * 100,
        "manual_default_rpm": f[0x23] * 100,
        "manual_max_rpm": f[0x26] * 100,
        "throttle_pl1_w": f[0x2E],
        "throttle_pl2_w": f[0x31],
        "ndtt_c": f[0x32],
        "dc_pl1_w": f[0x33],
        "dc_pl2_w": f[0x34],
        "dgps_level": f[0x0B],
        "footer_hex": f.hex(),
    }


def inspect_rom(path: Path, bank_base: int = 0x3D000, verbose_steps: bool = False) -> bytes:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    sig_hex = data[0x80:0x90].hex(" ")
    ver_ascii = data[0x90:0xB0].split(b"\x00", 1)[0].decode("ascii", errors="replace")

    print(f"ROM: {path}")
    print(f"Size: {len(data):,} bytes | SHA-256: {sha}")
    print(f"0x00080 sig: {sig_hex}")
    print(f"0x00090 ver: {ver_ascii!r}")

    print(f"\n=== Thermal / Fan / Power Mode Slots @ 0x{bank_base:05X} (SKU 3 active bank) ===")
    print("Slot | Name                      | Steps | Mask | AC PL1/PL2 | DC PL1/PL2 | Throt PL1/2 | Manual Clamp (Def) | NDTT | DGPS")
    print("-" * 124)
    for s in range(8):
        info = decode_slot(data, bank_base, s)
        dgps_code = 0xD0 + info["dgps_level"] if info["dgps_level"] in range(1, 6) else info["dgps_level"]
        print(
            f"  {s}  | {SLOT_NAMES[s]:<25} | 0..{info['max_step']:<2} | 0x{info['sensor_mask']:02X} | "
            f"{info['ac_pl1_w']:>2}W / {info['ac_pl2_w']:>2}W  | {info['dc_pl1_w']:>2}W / {info['dc_pl2_w']:>2}W  | "
            f"{info['throttle_pl1_w']:>2}W / {info['throttle_pl2_w']:>2}W   | "
            f"{info['manual_min_rpm']:>4}-{info['manual_max_rpm']:<4} ({info['manual_default_rpm']:>4})  | "
            f"{info['ndtt_c']:>3}C | 0x{dgps_code:02X}"
        )
        if verbose_steps:
            for st in info["steps"]:
                print(
                    f"       st{st['step']:2d}: {st['fan1_rpm']:4d}/{st['fan2_rpm']:4d} RPM | "
                    f"S0(CPU)={st['t0'][0]:2d}/{st['t0'][1]:2d}C  S1(GPU)={st['t1'][0]:2d}/{st['t1'][1]:2d}C  "
                    f"S2(Ch)={st['t2'][0]:2d}/{st['t2'][1]:2d}C  t={st['timers_flags']}"
                )

    print("\n=== Dynamic ADC Current Stepper @ 0x35000 (Slot 0) ===")
    ac_thresh = struct.unpack("<4H", data[0x35010:0x35018])
    dc_thresh = struct.unpack("<4H", data[0x35018:0x35020])
    print(f"AC ADC Ch1 thresholds (raw 10-bit 0..1023): {ac_thresh}")
    print(f"DC ADC Ch4 thresholds (raw 10-bit 0..1023): {dc_thresh}")

    print("\n=== Runtime Defaults @ 0x2098C (8-byte {addr, val} records) & PWM LUT @ 0x2081C (50-RPM steps) ===")
    defaults = [struct.unpack("<II", data[0x2098C + i * 8 : 0x2098C + (i + 1) * 8]) for i in range(16)]
    floor_recs = [(f"0x{addr:06X}", val) for addr, val in defaults[10:14]]
    print(f"0x2098C[10..13] min spin floor (%): {floor_recs} (x183/100 = {[v * 183 // 100 for _, v in floor_recs]})")
    lut = list(data[0x2081C : 0x2081C + 132])
    samples = ", ".join(f"{rpm}->{lut[rpm // 50]}" for rpm in (0, 1500, 2400, 4300, 5500, 5800, 5900))
    print(f"0x2081C PWM LUT samples (RPM -> 0..183 ticks): {samples}")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rom_pos", nargs="?", type=Path, default=None, help="Path to 256 KiB IT8390E EC ROM binary")
    parser.add_argument("--rom", type=Path, default=None, help="Path to 256 KiB IT8390E EC ROM binary")
    parser.add_argument("--bank", type=lambda s: int(s, 16), default=0x3D000, help="Hex base of thermal bank (default 3D000)")
    parser.add_argument("--steps", action="store_true", help="Print all fan curve steps for each slot")
    parser.add_argument("--compare", type=Path, default=None, help="Optional second 256 KiB EC ROM to diff against")
    args = parser.parse_args()
    rom_path = args.rom or args.rom_pos or DEFAULT_ROM

    d0 = inspect_rom(rom_path, bank_base=args.bank, verbose_steps=args.steps)
    if args.compare is not None:
        d1 = args.compare.read_bytes()
        n = min(len(d0), len(d1))
        same_total = sum(a == b for a, b in zip(d0[:n], d1[:n]))
        same_code = sum(a == b for a, b in zip(d0[:0x28000], d1[:0x28000]))
        print(f"\n=== Diff vs {args.compare} ===")
        print(f"Total matching bytes: {same_total:,} / {n:,} ({100.0 * same_total / n:.1f}%)")
        print(f"Code region (0x00000..0x28000): {same_code:,} / {0x28000:,} ({100.0 * same_code / 0x28000:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
