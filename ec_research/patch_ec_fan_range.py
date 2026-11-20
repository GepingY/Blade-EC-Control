"""Offline EC ROM patcher for RZ09-0328 (IT8390E-256, EC v01.00): Manual Fan Range 0-5500 RPM.

Strictly offline — never touches hardware or flashes anything.
Reads the verified golden stock ROM dump and emits a patched 256 KiB image where:
  1. All 8 thermal/fan mode slots (0..7) across all 4 identical SKU banks
     (0x3B000, 0x3C000, 0x3D000, 0x3E000) have:
       - footer +0x21 ([0x80555] Manual RPM Min) = 0x00 (0 RPM)
       - footer +0x22 ([0x80556])                = 0x00 (0 RPM)
       - footer +0x23 ([0x80557] Manual Default) = UNCHANGED (4900/5200/3100 safety seed)
       - footer +0x24 ([0x80558])                = 0x37 (5500 RPM)
       - footer +0x25 ([0x80559])                = 0x37 (5500 RPM)
       - footer +0x26 ([0x8055A] Manual RPM Max) = 0x37 (5500 RPM)
  2. External BIOS SPI slot cache (0x3FE000) version is bumped from 2 -> 3 cleanly
     without corrupting r7 (= 2, which is also the 0x0200 slot length high byte!):
       - 0x1211E: bnec r0, #0x2, 0x12134  ->  bnec r0, #0x3, 0x12134
       - 0x11316: load r9 = 0x3D000, r8 = 3 (r8 is callee-saved by push25 re=2),
                  jump to 0x11340
       - 0x11376, 0x113AE, 0x113E6, 0x1141E, 0x11456, 0x1148E, 0x114C6, 0x114FE:
                  sbi r7, [r6+0x218]      ->  sbi r8, [r6+0x218]
     Because stock v01.00 checks (version != 2), flashing stock back later also
     automatically rebuilds 0x3FE000 back to stock version 2.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct

from analyze_ec_rom import decode_slot
import dis_nds32 as dis

REPO_ROOT = Path(__file__).resolve().parent.parent
STOCK_ROM = REPO_ROOT / "firmware" / "rz09-0328-ec-v01.00" / "ec-rom-00000-3ffff.bin"
STOCK_SHA256 = "c676ff55da486c07cb8897c2e4bcb891e3140463864dbde7f615cef4c5a4b95d"
ROM_SIZE = 0x40000
DEFAULT_OUT_DIR = REPO_ROOT / "firmware" / "rz09-0328-ec-v01.00-mod-fan-0-5500"
DEFAULT_OUT_NAME = "rz09-0328-ec-v01.00-fan-0-5500.bin"

SKU_BANKS = (0x3B000, 0x3C000, 0x3D000, 0x3E000)
SLOT_VERSION_STORE_ADDRS = (
    0x11376,
    0x113AE,
    0x113E6,
    0x1141E,
    0x11456,
    0x1148E,
    0x114C6,
    0x114FE,
)


def _patch_bytes(buf: bytearray, addr: int, expected: bytes, replacement: bytes, label: str, log: list[dict]) -> None:
    if len(expected) != len(replacement):
        raise ValueError(f"{label}: length mismatch {len(expected)} != {len(replacement)}")
    actual = bytes(buf[addr : addr + len(expected)])
    if actual != expected:
        raise RuntimeError(
            f"{label} @ 0x{addr:05X}: expected {expected.hex(' ')}, got {actual.hex(' ')}"
        )
    if actual == replacement:
        return
    buf[addr : addr + len(replacement)] = replacement
    log.append({
        "addr": f"0x{addr:05X}",
        "size": len(replacement),
        "before": expected.hex(" "),
        "after": replacement.hex(" "),
        "reason": label,
    })


def build_patched_rom(stock_bytes: bytes, mirror_flag_ff: bool = False) -> tuple[bytes, list[dict]]:
    if len(stock_bytes) != ROM_SIZE:
        raise RuntimeError(f"Expected {ROM_SIZE} bytes, got {len(stock_bytes)}")
    stock_sha = hashlib.sha256(stock_bytes).hexdigest()
    if stock_sha != STOCK_SHA256:
        raise RuntimeError(f"Stock ROM SHA-256 mismatch: {stock_sha} != {STOCK_SHA256}")

    # Verify all 4 SKU banks are identical in stock image before patching
    for bank in SKU_BANKS:
        if stock_bytes[bank : bank + 0x1000] != stock_bytes[0x3D000 : 0x3E000]:
            raise RuntimeError(f"Unexpected divergence in SKU bank 0x{bank:05X}")

    buf = bytearray(stock_bytes)
    changes: list[dict] = []

    if mirror_flag_ff:
        _patch_bytes(
            buf,
            0x0008D,
            bytes([0x00]),
            bytes([0xFF]),
            "ITE 16-byte signature byte 13 (0x0008D): 0x00 -> 0xFF (matches Razer CH551_EC_01_01.bin external-SPI mirror image)",
            changes,
        )

    # 1. Patch manual fan RPM clamp range to 0-5500 RPM across all 4 banks x 8 slots
    #    Keep +0x23 (manual_default_rpm = 4900/5200/3100) unchanged as a safe initial seed.
    expected_footers = {
        0: (0x2B, 0x2B, 0x37, 0x37, 0x37),  # Balanced: 4300..5500
        1: (0x31, 0x31, 0x37, 0x37, 0x37),  # Gaming:   4900..5500
        2: (0x31, 0x31, 0x37, 0x37, 0x37),  # Creator:  4900..5500
        3: (0x31, 0x31, 0x37, 0x37, 0x37),  # Legacy:   4900..5500
        4: (0x31, 0x31, 0x37, 0x37, 0x37),  # Custom:   4900..5500
        5: (0x18, 0x1D, 0x2D, 0x32, 0x32),  # Silent:   2400..5000
        6: (0x18, 0x1D, 0x2D, 0x32, 0x32),  # Silent 6: 2400..5000
        7: (0x18, 0x1D, 0x2D, 0x32, 0x32),  # Silent 7: 2400..5000
    }
    for bank in SKU_BANKS:
        for slot in range(8):
            f_base = bank + slot * 0x200 + 0x1A8
            e21, e22, e24, e25, e26 = expected_footers[slot]
            _patch_bytes(buf, f_base + 0x21, bytes([e21, e22]), bytes([0x00, 0x00]),
                         f"Bank 0x{bank:05X} Slot {slot} manual RPM min (+0x21/+0x22 -> 0 RPM)", changes)
            _patch_bytes(buf, f_base + 0x24, bytes([e24, e25, e26]), bytes([0x37, 0x37, 0x37]),
                         f"Bank 0x{bank:05X} Slot {slot} manual RPM max (+0x24..+0x26 -> 5500 RPM)", changes)

    # 2. Bump 0x3FE000 external BIOS SPI cache slot version from 2 -> 3 cleanly:
    #    - 0x1211E: bnec r0, #0x2, 0x12134  ->  bnec r0, #0x3, 0x12134
    _patch_bytes(
        buf,
        0x1211E,
        bytes.fromhex("5a 08 02 0b"),
        bytes.fromhex("5a 08 03 0b"),
        "0x120ca: require cached 0x3FE000 slot version == 3 (triggers auto-rebuild from 0x3D000)",
        changes,
    )

    #    - 0x11316..0x1131D (inside 0x11314, which saves r6..r10 via push25 re=2):
    #      All 4 banks (0x3B000..0x3E000) are identical; load r9 = 0x3D000, r8 = 3, j8 0x11340.
    #      Preserves r7 = 2 at 0x1135A so [r6+0x215] (0x0200 slot length high byte) stays 2!
    _patch_bytes(
        buf,
        0x11316,
        bytes.fromhex("46 00 00 80 00 00 02 ed"),
        bytes.fromhex("44 93 d0 00 85 03 d5 12"),
        "0x11314: movi r9, #0x3d000; movi55 r8, #3; j8 0x11340 (sets r8=3 while keeping r7=2 for 0x0200 size)",
        changes,
    )

    #    - All 8 slot writes to [r6+0x218] (version byte): sbi r7,[r6+0x218] -> sbi r8,[r6+0x218]
    for idx, addr in enumerate(SLOT_VERSION_STORE_ADDRS):
        _patch_bytes(
            buf,
            addr,
            bytes.fromhex("10 73 02 18"),
            bytes.fromhex("10 83 02 18"),
            f"0x11314 slot {idx} (ID 0x{0x20 + idx:02X}): sbi r8, [r6+0x218] (write version=3, keep size_hi=r7=2)",
            changes,
        )

    patched = bytes(buf)
    verify_patched_rom(stock_bytes, patched, mirror_flag_ff=mirror_flag_ff)
    return patched, changes


def verify_patched_rom(stock: bytes, patched: bytes, mirror_flag_ff: bool = False) -> None:
    assert len(patched) == ROM_SIZE
    # Critical boot/flash structures must be 100% untouched (except optional 0x0008D mirror flag)
    if mirror_flag_ff:
        assert patched[0x00000:0x0008D] == stock[0x00000:0x0008D], "Vectors / ITE signature before 0x8D altered!"
        assert patched[0x0008D] == 0xFF and stock[0x0008D] == 0x00, "0x0008D mirror flag not set to 0xFF!"
        assert patched[0x0008E:0x00100] == stock[0x0008E:0x00100], "ITE signature after 0x8D altered!"
    else:
        assert patched[0x00000:0x00100] == stock[0x00000:0x00100], "Vectors / ITE signature header altered!"
    assert patched[0x02000:0x03000] == stock[0x02000:0x03000], "Flash-idle / ILM region 0x02000..0x02FFF altered!"
    assert patched[0x03000:0x03100] == stock[0x03000:0x03100], "Reset init altered!"
    assert patched[0x17180:0x17350] == stock[0x17180:0x17350], "PM1 0xDC entry altered!"

    # Verify exact disassembly of every modified code instruction
    assert dis.dec32(struct.unpack(">I", patched[0x11316:0x1131A])[0], 0x11316) == "movi r9, #0x3d000"
    assert dis.dec16(struct.unpack(">H", patched[0x1131A:0x1131C])[0], 0x1131A) == "movi55 r8, #3"
    assert dis.dec16(struct.unpack(">H", patched[0x1131C:0x1131E])[0], 0x1131C) == "j8 0x11340"
    assert dis.dec16(struct.unpack(">H", patched[0x1135A:0x1135C])[0], 0x1135A) == "movi55 r7, #2"
    for addr in SLOT_VERSION_STORE_ADDRS:
        assert dis.dec32(struct.unpack(">I", patched[addr - 8 : addr - 4])[0], addr - 8) == "sbi r7, [r6+0x215]"
        assert dis.dec32(struct.unpack(">I", patched[addr : addr + 4])[0], addr) == "sbi r8, [r6+0x218]"
    assert dis.dec32(struct.unpack(">I", patched[0x1211E:0x12122])[0], 0x1211E) == "bnec r0, #0x3, 0x12134"

    # Verify all 4 SKU banks x 8 slots decode cleanly with 0-5500 RPM manual range and unchanged power/auto curves
    for bank in SKU_BANKS:
        assert patched[bank : bank + 0x1000] == patched[0x3D000 : 0x3E000]
        for slot in range(8):
            s_old = decode_slot(stock, bank, slot)
            s_new = decode_slot(patched, bank, slot)
            assert s_new["manual_min_rpm"] == 0, f"bank 0x{bank:05X} slot {slot} min != 0"
            assert s_new["manual_max_rpm"] == 5500, f"bank 0x{bank:05X} slot {slot} max != 5500"
            assert s_new["manual_default_rpm"] == s_old["manual_default_rpm"]
            assert s_new["ac_pl1_w"] == s_old["ac_pl1_w"]
            assert s_new["ac_pl2_w"] == s_old["ac_pl2_w"]
            assert s_new["dc_pl1_w"] == s_old["dc_pl1_w"]
            assert s_new["dc_pl2_w"] == s_old["dc_pl2_w"]
            assert s_new["ndtt_c"] == s_old["ndtt_c"]
            assert s_new["dgps_level"] == s_old["dgps_level"]
            assert s_new["steps"] == s_old["steps"]

    # Verify exact total changed byte count:
    #   Code: 1 (0x12120) + 8 (0x11316..0x1131D) + 8 (8x slot version store) = 17 bytes
    #   Tables: 4 banks * (8 slots * 2 min bytes [0x21..0x22] + 3 Silent slots * 3 max bytes [0x24..0x26])
    #         = 4 * (16 + 9) = 100 bytes
    #   Optional mirror flag: +1 byte at 0x0008D (0x00 -> 0xFF)
    expected_diff = 118 if mirror_flag_ff else 117
    diff_count = sum(a != b for a, b in zip(stock, patched))
    assert diff_count == expected_diff, f"Expected exactly {expected_diff} changed bytes, got {diff_count}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock", type=Path, default=STOCK_ROM, help="Verified stock 256 KiB EC ROM")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Output directory")
    parser.add_argument("--out-name", default=None, help="Output ROM filename")
    parser.add_argument("--manifest-name", default=None, help="Output manifest JSON filename")
    parser.add_argument(
        "--mirror-flag-ff",
        action="store_true",
        help="Also patch ITE signature byte 0x0008D from 0x00 -> 0xFF (matches Razer CH551 external-SPI mirror header)",
    )
    parser.add_argument("--check-only", action="store_true", help="Build and verify in memory without writing files")
    args = parser.parse_args()

    out_name = args.out_name or (
        "rz09-0328-ec-v01.00-fan-0-5500-mirror-8dff.bin" if args.mirror_flag_ff else DEFAULT_OUT_NAME
    )
    manifest_name = args.manifest_name or (
        "patch-manifest-mirror-8dff.json" if args.mirror_flag_ff else "patch-manifest.json"
    )

    stock = args.stock.read_bytes()
    patched, changes = build_patched_rom(stock, mirror_flag_ff=args.mirror_flag_ff)
    patched_sha = hashlib.sha256(patched).hexdigest()
    diff_count = sum(a != b for a, b in zip(stock, patched))

    print(f"Stock ROM   : {args.stock} (SHA-256: {STOCK_SHA256})")
    print(f"Patched ROM : {args.out_dir / out_name} (SHA-256: {patched_sha})")
    extra = " + 1 header byte (0x0008D=0xFF)" if args.mirror_flag_ff else ""
    print(f"Verified    : {diff_count} bytes changed (17 code bytes + 100 table bytes{extra}); all assertions PASSED.")

    if args.check_only:
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_bin = args.out_dir / out_name
    out_bin.write_bytes(patched)

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "target_board": "Razer Blade 15 Base Early 2020 (RZ09-0328 / DA550-MB / SSID 0x2011)",
        "target_ec": "ITE IT8390E-256 (ITE8390EC-V01.00, 256 KiB internal eFlash + GD25Q64 external SPI)",
        "goal": "Set manual fan speed clamp range to 0-5500 RPM in all modes (0..7) and bump 0x3FE000 SPI cache slot version 2 -> 3"
        + (" (+ 0x0008D=0xFF external-SPI mirror flag)" if args.mirror_flag_ff else ""),
        "stock_rom": args.stock.name,
        "stock_sha256": STOCK_SHA256,
        "patched_rom": out_bin.name,
        "patched_sha256": patched_sha,
        "mirror_flag_ff": args.mirror_flag_ff,
        "total_bytes_changed": diff_count,
        "code_bytes_changed": 17,
        "table_bytes_changed": 100,
        "header_bytes_changed": 1 if args.mirror_flag_ff else 0,
        "changes": changes,
    }
    (args.out_dir / manifest_name).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest    : {args.out_dir / manifest_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
