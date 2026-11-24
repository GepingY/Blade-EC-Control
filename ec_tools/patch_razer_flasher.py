"""Offline .NET IL patcher for Razer_EC_Flash_v1.03.exe (fixes verify-failure bugs).

Strictly offline — never executes the flasher or touches hardware.

Fixes three verified bugs in Razer_EC_Flash.Program::Main (Razer_EC_Flash_v1.03.exe):
  1. Missing braces at IL_09F1 caused "Verify .........100%" to print even after ".......Fail".
  2. Verify failure at IL_095B jumped to IL_09E5 and fell straight into IL_0A24..IL_0A41
     (vExitFollowMode + EC_Exit_Idle 0xFC + Disable_EC_Flash), telling the EC to start
     the unverified/failed firmware instead of staying safely in RAM-resident Follow Mode.
  3. IL_0AB4..IL_0AD8 checked shut_down / reboot (DoExitWin(6) forced reboot) without
     checking verifyfail, forcing a reboot even after verify failed.

Emits two verified unsigned .NET executables next to the untouched original:
  - Razer_EC_Flash_v1.03_safe.exe:
      * IL_095B: br IL_09e5 -> br IL_0a79 (leave.s IL_0a90), skipping "Verify 100%",
        vESPI_MAP_EN, vExitFollowMode, EC_Exit_Idle (0xFC), Disable_EC_Flash, and "Update ok!".
      * IL_09F1: brfalse.s IL_09fb -> brfalse.s IL_0a07 (belt-and-suspenders braces fix).
      * IL_0A91..IL_0AB3: replaces unused `if (anykey)` block with
        `if (verifyfail) { Environment.Exit(2); }`, skipping DoExitWin even if REBOOT=1
        and exiting cleanly with exit code 2 (without blocking on ReadKey while EC is idle).
  - Razer_EC_Flash_v1.03_recovery_in_follow_mode.exe:
      * All fixes above PLUS NOPs IL_0325 (Send_EC_Update_Qevent) and IL_0348
        (EC_Write_Enable / PM1 0xDC) so if Razer_EC_Flash_v1.03_safe.exe ever exits with
        code 2 while leaving the EC in Follow Mode on 0x202/0x206, this recovery binary can
        immediately re-flash (mod or stock) without hanging on 0xDC/0x33.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct

REPO_ROOT = Path(__file__).resolve().parent.parent
KIT_DIR = REPO_ROOT / "firmware" / "rz09-0328-ec-v01.00-mod-fan-0-5500" / "windows_razer_flash"
ORIG_EXE = KIT_DIR / "Razer_EC_Flash_v1.03.exe"
ORIG_SHA256 = "3590f72531078eb9562f8ed864753ea710a1d2723f693923b99b3a981b78f07b"
ORIG_SIZE = 46592
MAIN_IL_BASE = 0x15D8
MAIN_CODE_SIZE = 0x0AE1


def _patch(buf: bytearray, il_off: int, expected: bytes, replacement: bytes, reason: str, log: list[dict]) -> None:
    if len(expected) != len(replacement):
        raise ValueError(f"{reason}: size mismatch {len(expected)} != {len(replacement)}")
    file_off = MAIN_IL_BASE + il_off
    actual = bytes(buf[file_off : file_off + len(expected)])
    if actual != expected:
        raise RuntimeError(
            f"{reason} @ IL_{il_off:04x} (file 0x{file_off:04x}): "
            f"expected {expected.hex(' ')}, got {actual.hex(' ')}"
        )
    buf[file_off : file_off + len(replacement)] = replacement
    log.append({
        "il_offset": f"IL_{il_off:04x}",
        "file_offset": f"0x{file_off:04x}",
        "size": len(replacement),
        "before": expected.hex(" "),
        "after": replacement.hex(" "),
        "reason": reason,
    })


def verify_original_pe(raw: bytes) -> None:
    if len(raw) != ORIG_SIZE:
        raise RuntimeError(f"Expected {ORIG_SIZE} bytes, got {len(raw)}")
    sha = hashlib.sha256(raw).hexdigest()
    if sha != ORIG_SHA256:
        raise RuntimeError(f"Original exe SHA-256 mismatch: {sha} != {ORIG_SHA256}")
    pe_off = struct.unpack_from("<I", raw, 0x3C)[0]
    opt_off = pe_off + 24
    magic = struct.unpack_from("<H", raw, opt_off)[0]
    if magic != 0x20B:
        raise RuntimeError(f"Unexpected PE magic 0x{magic:x}")
    sec_rva, sec_sz = struct.unpack_from("<II", raw, opt_off + 112 + 4 * 8)
    if (sec_rva, sec_sz) != (0, 0):
        raise RuntimeError(f"Unexpected Authenticode directory: ({sec_rva}, {sec_sz})")
    flags_sz, maxstack, codesize, locsig = struct.unpack_from("<HHII", raw, MAIN_IL_BASE - 12)
    if codesize != MAIN_CODE_SIZE or maxstack != 6:
        raise RuntimeError(f"Unexpected Main fat header: codesize=0x{codesize:x}, maxstack={maxstack}")


def build_safe_variants(orig: bytes) -> tuple[bytes, list[dict], bytes, list[dict]]:
    verify_original_pe(orig)

    # Variant 1: Razer_EC_Flash_v1.03_safe.exe
    safe = bytearray(orig)
    safe_log: list[dict] = []

    # 1. IL_095b: br IL_09e5 -> br IL_0a79 (leave.s IL_0a90 inside same try block 0x0250..0x0a7b)
    _patch(
        safe,
        0x095B,
        bytes.fromhex("38 85 00 00 00"),
        bytes.fromhex("38 19 01 00 00"),
        "On verify mismatch, branch straight to IL_0a79 (leave.s IL_0a90), skipping Verify 100%, "
        "vESPI_MAP_EN, vEnterFollowMode, vExitFollowMode, EC_Exit_Idle (0xFC), Disable_EC_Flash, and Update ok!",
        safe_log,
    )

    # 2. IL_09f1: brfalse.s IL_09fb -> brfalse.s IL_0a07 (fixes missing braces around if (verifyfail == 0))
    _patch(
        safe,
        0x09F1,
        bytes.fromhex("2c 08"),
        bytes.fromhex("2c 14"),
        "Belt-and-suspenders fix: skip Console.Write('Verify .........100%') when verifyfail != 0",
        safe_log,
    )

    # 3. IL_0a91..IL_0ab3: replace dead `if (anykey) { ... ReadKey(); }` with `if (verifyfail) { Environment.Exit(2); }`
    orig_a91 = bytes.fromhex(
        "7e 65 00 00 04"  # IL_0a91: ldsfld Program::anykey
        "13 39"           # IL_0a96: stloc.s V_57
        "11 39"           # IL_0a98: ldloc.s V_57
        "2c 18"           # IL_0a9a: brfalse.s IL_0ab4
        "00"              # IL_0a9c: nop
        "72 27 0c 00 70"  # IL_0a9d: ldstr "Please push any key to Exit !"
        "7e 61 00 00 04"  # IL_0aa2: ldsfld Program::filepath
        "28 23 00 00 0a"  # IL_0aa7: call Console::WriteLine(string, object)
        "00"              # IL_0aac: nop
        "28 24 00 00 0a"  # IL_0aad: call Console::ReadKey()
        "26"              # IL_0ab2: pop
        "00"              # IL_0ab3: nop
    )
    new_a91 = bytes.fromhex(
        "7e 67 00 00 04"  # IL_0a91: ldsfld Program::verifyfail
        "13 39"           # IL_0a96: stloc.s V_57
        "11 39"           # IL_0a98: ldloc.s V_57
        "2c 18"           # IL_0a9a: brfalse.s IL_0ab4 (normal pass path: shut_down / reboot / Exit(1))
        "18"              # IL_0a9c: ldc.i4.2
        "28 37 00 00 0a"  # IL_0a9d: call Environment::Exit(int32) (exit code 2; skips DoExitWin/reboot!)
        "2a"              # IL_0aa2: ret (clean verifier block terminator)
        + "00" * 17       # IL_0aa3..IL_0ab3: nop padding up to IL_0ab4
    )
    _patch(
        safe,
        0x0A91,
        orig_a91,
        new_a91,
        "If verifyfail is true, skip DoExitWin (shut_down / reboot) even when REBOOT=1 and exit immediately with code 2",
        safe_log,
    )

    # Variant 2: Razer_EC_Flash_v1.03_recovery_in_follow_mode.exe
    rec = bytearray(safe)
    rec_log = list(safe_log)
    _patch(
        rec,
        0x0325,
        bytes.fromhex("28 23 00 00 06"),
        bytes.fromhex("00 00 00 00 00"),
        "Recovery mode: NOP Send_EC_Update_Qevent() because EC is already paused in RAM-resident Follow Mode",
        rec_log,
    )
    _patch(
        rec,
        0x0348,
        bytes.fromhex("28 3c 00 00 06"),
        bytes.fromhex("00 00 00 00 00"),
        "Recovery mode: NOP EC_Write_Enable() (PM1 0xDC -> 0x33) so recovery does not hang waiting for 0x33 while already in Follow Mode",
        rec_log,
    )

    safe_bytes = bytes(safe)
    rec_bytes = bytes(rec)
    assert len(safe_bytes) == ORIG_SIZE and len(rec_bytes) == ORIG_SIZE
    assert sum(a != b for a, b in zip(orig, safe_bytes)) == 19
    assert sum(a != b for a, b in zip(safe_bytes, rec_bytes)) == 6  # 2x (28 xx 00 00 06 -> 00 00 00 00 00) = 6 non-zero bytes changed
    return safe_bytes, safe_log, rec_bytes, rec_log


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in-exe", "--orig", dest="in_exe", type=Path, default=ORIG_EXE,
                        help="Path to original Razer_EC_Flash_v1.03.exe (SHA-256 verified)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Output directory (defaults to the directory containing --in-exe)")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    out_dir = args.out_dir or args.in_exe.parent

    orig = args.in_exe.read_bytes()
    safe_bytes, safe_log, rec_bytes, rec_log = build_safe_variants(orig)
    safe_sha = hashlib.sha256(safe_bytes).hexdigest()
    rec_sha = hashlib.sha256(rec_bytes).hexdigest()

    print(f"Original : {args.in_exe} (SHA-256: {ORIG_SHA256})")
    print(f"Safe     : {out_dir / 'Razer_EC_Flash_v1.03_safe.exe'} (SHA-256: {safe_sha})")
    print(f"Recovery : {out_dir / 'Razer_EC_Flash_v1.03_recovery_in_follow_mode.exe'} (SHA-256: {rec_sha})")

    if args.check_only:
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    safe_path = out_dir / "Razer_EC_Flash_v1.03_safe.exe"
    rec_path = out_dir / "Razer_EC_Flash_v1.03_recovery_in_follow_mode.exe"
    safe_path.write_bytes(safe_bytes)
    rec_path.write_bytes(rec_bytes)

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "original_exe": args.in_exe.name,
        "original_sha256": ORIG_SHA256,
        "safe_exe": safe_path.name,
        "safe_sha256": safe_sha,
        "safe_changes": safe_log,
        "recovery_exe": rec_path.name,
        "recovery_sha256": rec_sha,
        "recovery_changes": rec_log,
    }
    (out_dir / "flasher-patch-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest : {out_dir / 'flasher-patch-manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
