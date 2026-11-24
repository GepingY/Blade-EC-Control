"""Offline EC ROM patcher v2 for RZ09-0328 (IT8390E-256, EC v01.00):
  1. Manual fan clamp range 0-5500 RPM across all 4 SKU banks x 8 mode slots (0..7)
  2. Silent mode (slots 5, 6, 7) CPU TDP (BOTH AC and DC) -> PL1 25 W / PL2 35 W
  3. External SPI thermal cache (0x3FE000) version bumped 2 -> 4 (rebuilds cleanly over v1's cache v3)
  4. Custom Mode (4) AC CPU TDP (DC unchanged) via 0x10070 -> 0x86900:
       - Custom Low  (CPU=0): AC 35 W / 45 W (DC 20/20 W)
       - Custom Med  (CPU=1): AC 60 W / 80 W (DC 25/25 W); fixes 0x0e162 fallback r0=#40 -> r1=#35
       - Custom High (CPU=2): AC 60 W / 90 W (DC 30/30 W)
  5. External SPI runtime defaults cache (0x050000) version bumped 7 -> 8 (auto-rebuilds + re-CRCs 0x86000)
  6. Custom Mode (4) GPU MAX 90 W (ACPI _Q33 / PSTA bit 3 -> PEGP.CTGP=1):
       - Custom Low  (CPU=0): GPU MAX 90 W (CTGP=1)
       - Custom Med  (CPU=1): GPU MAX 90 W (CTGP=1)
       - Custom High (CPU=2): GPU MAX 80 W (CTGP=0)
       (Tracks state = 0x80 | (cpu << 4) | gpu in 0x808E2 so _Q33 always fires right after _Q30
        on Mode 4 entry and on every Custom CPU/GPU slider change, preserving 75 C / 87 C NDTT.)
  7. ITE 16-byte signature byte 0x0008D set 0x00 -> 0xFF for GD25Q64 -> internal eFlash hardware mirror
     (also builds mirror-ready stock rollback image with ONLY 0x0008D=0xFF).

Strictly offline — never touches hardware or flashes anything.
Includes an NDS32 instruction-level emulator that executes the emitted 0x10070 and 0x0e50a
machine code bytes directly from the output ROM and asserts exact RAM/register/Q-event results.
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

OUT_DIR = REPO_ROOT / "firmware" / "rz09-0328-ec-v01.00-mod-v2-power-fan"
FLASH_KIT_DIR = REPO_ROOT / "firmware" / "rz09-0328-ec-v01.00-mod-fan-0-5500" / "windows_razer_flash"
OUT_BIN_NAME = "rz09-0328-ec-v01.00-v2-fan0-5500-tdp-gpu90w-8dff.bin"
STOCK_MIRROR_NAME = "stock-ec-rom-00000-3ffff-mirror-8dff.bin"

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


# ---------------------------------------------------------------------------
# Verified NDS32 big-endian instruction encoders (cross-checked by dis_nds32)
# ---------------------------------------------------------------------------

def _enc32(w: int) -> bytes:
    return struct.pack(">I", w & 0xFFFFFFFF)


def _enc16(h: int) -> bytes:
    return struct.pack(">H", h & 0xFFFF)


def enc_lbi(rt: int, ra: int, imm15: int) -> bytes:
    return _enc32((0x00 << 25) | (rt << 20) | (ra << 15) | (imm15 & 0x7FFF))


def enc_sbi(rt: int, ra: int, imm15: int) -> bytes:
    return _enc32((0x08 << 25) | (rt << 20) | (ra << 15) | (imm15 & 0x7FFF))


def enc_slli(rt: int, ra: int, ib5: int) -> bytes:
    return _enc32((0x20 << 25) | (rt << 20) | (ra << 15) | ((ib5 & 0x1F) << 10) | 8)


def enc_or(rt: int, ra: int, rb: int) -> bytes:
    return _enc32((0x20 << 25) | (rt << 20) | (ra << 15) | (rb << 10) | 4)


def enc_movi(rt: int, imm20: int) -> bytes:
    return _enc32((0x22 << 25) | (rt << 20) | (imm20 & 0xFFFFF))


def enc_sethi(rt: int, hi20: int) -> bytes:
    return _enc32((0x23 << 25) | (rt << 20) | (hi20 & 0xFFFFF))


def enc_jal(pc: int, target: int) -> bytes:
    disp = (target - pc) >> 1
    assert -(1 << 23) <= disp < (1 << 23) and (target - pc) % 2 == 0
    return _enc32((0x24 << 25) | (1 << 24) | (disp & 0xFFFFFF))


def enc_beq_bne(is_bne: bool, rt: int, ra: int, pc: int, target: int) -> bytes:
    disp = (target - pc) >> 1
    assert -(1 << 13) <= disp < (1 << 13) and (target - pc) % 2 == 0
    return _enc32((0x26 << 25) | (rt << 20) | (ra << 15) | ((1 if is_bne else 0) << 14) | (disp & 0x3FFF))


def enc_beqz(rt: int, pc: int, target: int) -> bytes:
    disp = (target - pc) >> 1
    assert -(1 << 15) <= disp < (1 << 15) and (target - pc) % 2 == 0
    return _enc32((0x27 << 25) | (rt << 20) | (2 << 16) | (disp & 0xFFFF))


def enc_addi(rt: int, ra: int, imm15: int) -> bytes:
    return _enc32((0x28 << 25) | (rt << 20) | (ra << 15) | (imm15 & 0x7FFF))


def enc_andi(rt: int, ra: int, imm15u: int) -> bytes:
    return _enc32((0x2A << 25) | (rt << 20) | (ra << 15) | (imm15u & 0x7FFF))


def enc_ori(rt: int, ra: int, imm15u: int) -> bytes:
    return _enc32((0x2C << 25) | (rt << 20) | (ra << 15) | (imm15u & 0x7FFF))


def enc_slti(rt: int, ra: int, imm15u: int) -> bytes:
    return _enc32((0x2E << 25) | (rt << 20) | (ra << 15) | (imm15u & 0x7FFF))


def enc_push25(re: int, imm: int = 0) -> bytes:
    return _enc16(0xFC00 | ((re & 3) << 5) | ((imm >> 3) & 0x1F))


def enc_pop25(re: int, imm: int = 0) -> bytes:
    return _enc16(0xFC80 | ((re & 3) << 5) | ((imm >> 3) & 0x1F))


def enc_movi55(rt: int, imm5: int) -> bytes:
    return _enc16(0x8400 | ((rt & 0x1F) << 5) | (imm5 & 0x1F))


def enc_nop16() -> bytes:
    return _enc16(0x9200)


# ---------------------------------------------------------------------------
# Code builders for 0x10070 (Custom 4 PL table) and 0x0e534 (Custom 4 GPU 90W)
# ---------------------------------------------------------------------------

def assemble_custom_pl_init() -> bytes:
    """Assemble 0x10070..0x101c8 (344 bytes total, padded with nop16 after pop25).

    Initializes all 4 SKU banks at 0x86900 (+0x00, +0x28, +0x50, +0x78), each 5 x 8-byte slots:
      - Slot 0 (Low) : AC 35/45 W, DC 20/20 W
      - Slot 1 (Med) : AC 60/80 W, DC 25/25 W
      - Slot 2 (High): AC 60/90 W, DC 30/30 W
      - Slot 3 (High): AC 60/90 W, DC 30/30 W
      - Slot 4 (High): AC 60/90 W, DC 30/30 W
    """
    base = 0x10070
    end = 0x101C8
    out = bytearray()

    def emit(b: bytes) -> None:
        out.extend(b)

    emit(enc_push25(re=2, imm=0))         # save r6..r10, lp
    emit(enc_sethi(0, 0x00086))           # r0 = 0x00086000
    emit(enc_addi(0, 0, 0x900))           # r0 = 0x00086900 (bank ptr)
    emit(enc_addi(10, 0, 0x0A0))          # r10 = 0x000869A0 (4 * 0x28 end ptr)
    emit(enc_movi(1, 35))                 # r1 = Low AC PL1 (35 W)
    emit(enc_movi(2, 45))                 # r2 = Low AC PL2 (45 W)
    emit(enc_movi(3, 20))                 # r3 = Low DC PL1/PL2 (20 W)
    emit(enc_movi(4, 60))                 # r4 = Med & High AC PL1 (60 W)
    emit(enc_movi(5, 80))                 # r5 = Med AC PL2 (80 W)
    emit(enc_movi(6, 25))                 # r6 = Med DC PL1/PL2 (25 W)
    emit(enc_movi(7, 90))                 # r7 = High AC PL2 (90 W)
    emit(enc_movi(8, 30))                 # r8 = High DC PL1/PL2 (30 W)

    loop_pc = base + len(out)
    # Slot 0 (Low)
    emit(enc_sbi(1, 0, 0x00))
    emit(enc_sbi(2, 0, 0x01))
    emit(enc_sbi(3, 0, 0x02))
    emit(enc_sbi(3, 0, 0x03))
    # Slot 1 (Med)
    emit(enc_sbi(4, 0, 0x08))
    emit(enc_sbi(5, 0, 0x09))
    emit(enc_sbi(6, 0, 0x0A))
    emit(enc_sbi(6, 0, 0x0B))
    # Slot 2..4 (High + duplicate slots)
    for slot_off in (0x10, 0x18, 0x20):
        emit(enc_sbi(4, 0, slot_off + 0))
        emit(enc_sbi(7, 0, slot_off + 1))
        emit(enc_sbi(8, 0, slot_off + 2))
        emit(enc_sbi(8, 0, slot_off + 3))

    emit(enc_addi(0, 0, 0x28))
    emit(enc_beq_bne(True, 0, 10, base + len(out), loop_pc))
    emit(enc_pop25(re=2, imm=0))          # restore r6..r10, lp and return

    total = end - base
    assert len(out) <= total and (total - len(out)) % 2 == 0
    while len(out) < total:
        out.extend(enc_nop16())
    return bytes(out)


def assemble_custom_gpu_ctgp_handler() -> bytes:
    """Assemble 0x0e534..0x0e592 (94 bytes; 0x0e592 ret5 remains untouched!).

    Entry conditions from untouched 0x0e50a..0x0e533:
      - r0 == 0x00080000
      - Active mode [0x8035F] == 4 and loaded mode [0x8034F] == 4
    Behavior:
      - Reads gpu = [0x808E1], cpu = [0x808E0], prev = [0x808E2]
      - Computes state = 0x80 | (cpu << 4) | gpu (never collides with 0x01 from 0x12cba)
      - If prev == state -> branches to 0x0e592 (ret5)
      - Otherwise:
          * Saves state to [0x808E2]
          * Sets NDTT [0x80205] = 75 C (0x4B) if gpu == 0 else 87 C (0x57)
          * Sets PSTA [0x802AA] bit 3 (0x08 -> ACPI _Q33 PEGP.CTGP=1 = 90 W GPU)
            if cpu < 2 (Custom Low=0 or Custom Med=1), else clears bit 3 (Custom High=2 = 80 W GPU)
          * Calls 0x0993e(0x33, 0) to fire ACPI _Q33
    """
    base = 0x0E534
    ret_pc = 0x0E592
    out = bytearray()

    def emit(b: bytes) -> None:
        out.extend(b)

    emit(enc_lbi(3, 0, 0x8E1))                        # 0x0e534: r3 = gpu
    emit(enc_lbi(4, 0, 0x8E0))                        # 0x0e538: r4 = cpu
    emit(enc_lbi(5, 0, 0x8E2))                        # 0x0e53c: r5 = prev_state
    emit(enc_slli(1, 4, 4))                           # 0x0e540: r1 = cpu << 4
    emit(enc_or(1, 1, 3))                             # 0x0e544: r1 = (cpu << 4) | gpu
    emit(enc_ori(1, 1, 0x80))                         # 0x0e548: r1 = 0x80 | (cpu << 4) | gpu
    emit(enc_beq_bne(False, 5, 1, base + len(out), ret_pc))  # 0x0e54c: if prev == state -> ret5 @ 0x0e592
    emit(enc_push25(re=0, imm=0))                     # 0x0e550: save r6, lp
    emit(enc_sbi(1, 0, 0x8E2))                        # 0x0e552: [0x808e2] = state
    emit(enc_movi(1, 0x4B))                           # 0x0e556: r1 = 75 C
    ndtt_store_pc = base + len(out) + 8               # skip movi r1, #0x57 (4B) after beqz (4B)
    emit(enc_beqz(3, base + len(out), ndtt_store_pc)) # 0x0e55a: if gpu == 0 -> store 75 C
    emit(enc_movi(1, 0x57))                           # 0x0e55e: else r1 = 87 C
    assert base + len(out) == ndtt_store_pc
    emit(enc_sbi(1, 0, 0x205))                        # 0x0e562: [0x80205] = NDTT
    emit(enc_lbi(1, 0, 0x2AA))                        # 0x0e566: r1 = PSTA
    emit(enc_andi(1, 1, 0xF7))                        # 0x0e56a: clear bit 3 (80 W default)
    emit(enc_slti(15, 4, 2))                          # 0x0e56e: r15 = (cpu < 2) ? 1 : 0 (unsigned)
    psta_store_pc = base + len(out) + 8               # skip ori r1, r1, #8 (4B) after beqz (4B)
    emit(enc_beqz(15, base + len(out), psta_store_pc))# 0x0e572: if cpu >= 2 -> keep 80 W
    emit(enc_ori(1, 1, 0x08))                         # 0x0e576: set bit 3 (90 W GPU!)
    assert base + len(out) == psta_store_pc
    emit(enc_sbi(1, 0, 0x2AA))                        # 0x0e57a: [0x802aa] = PSTA
    emit(enc_movi(0, 0x33))                           # 0x0e57e: arg0 = 0x33 (_Q33)
    emit(enc_movi55(1, 0))                            # 0x0e582: arg1 = 0
    emit(enc_jal(base + len(out), 0x0993E))           # 0x0e584: call EC_Send_QEvent(0x33, 0)
    emit(enc_pop25(re=0, imm=0))                      # 0x0e588: restore r6, lp and return

    total = ret_pc - base
    assert len(out) <= total and (total - len(out)) % 2 == 0
    while len(out) < total:
        out.extend(enc_nop16())
    return bytes(out)


# ---------------------------------------------------------------------------
# Minimal NDS32 emulator to prove emitted ROM bytes execute with 100% accuracy
# ---------------------------------------------------------------------------

def emu_nds32(rom: bytes, start_pc: int, init_regs: dict[int, int], init_mem: dict[int, int], stop_calls: set[int] | None = None):
    """Execute real NDS32 bytes from `rom` starting at `start_pc` until return to SENTINEL_LP."""
    sentinel_lp = 0xDEADBEEF
    regs = [0] * 32
    regs[30] = sentinel_lp  # lp
    regs[31] = 0x00083FF0   # sp
    for k, v in init_regs.items():
        regs[k] = v & 0xFFFFFFFF
    mem = bytearray(0x00090000)  # covers 0x80000..0x8FFFF SRAM
    for addr, val in init_mem.items():
        mem[addr] = val & 0xFF

    pc = start_pc
    calls: list[tuple[int, int, int]] = []
    stop_calls = stop_calls or set()
    steps = 0

    def push_u32(val: int) -> None:
        regs[31] -= 4
        struct.pack_into("<I", mem, regs[31], val & 0xFFFFFFFF)

    def pop_u32() -> int:
        val = struct.unpack_from("<I", mem, regs[31])[0]
        regs[31] += 4
        return val

    while pc != sentinel_lp:
        steps += 1
        if steps > 5000:
            raise AssertionError(f"Emulator runaway @ 0x{pc:05x}")
        if rom[pc] & 0x80:
            hw = struct.unpack_from(">H", rom, pc)[0]
            npc = pc + 2
            if hw == 0x9200:
                pass  # nop16
            elif hw == 0xDD9E:
                npc = regs[30]  # ret5
            elif (hw & 0xFC00) == 0x8000:
                rt5, ra5 = (hw >> 5) & 31, hw & 31
                regs[rt5] = regs[ra5]
            elif (hw & 0xFC00) == 0x8400:
                rt5 = (hw >> 5) & 31
                regs[rt5] = dis.sbits(hw & 31, 5) & 0xFFFFFFFF
            elif (hw & 0xFE00) == 0x8E00:
                rt4 = dis.R45MAP[(hw >> 5) & 15]
                regs[rt4] = (regs[rt4] - (hw & 31)) & 0xFFFFFFFF
            elif (hw & 0xFE00) == 0x9600 and (hw & 7) == 0:
                rt3, ra3 = (hw >> 6) & 7, (hw >> 3) & 7
                regs[rt3] = regs[ra3] & 0xFF
            elif (hw & 0xFE00) == 0xD400:  # j8
                npc = pc + (dis.sbits(hw & 0xFF, 8) << 1)
            elif (hw & 0xFE00) == 0xE600:  # slti45
                rt4 = dis.R45MAP[(hw >> 5) & 15]
                regs[15] = 1 if regs[rt4] < (hw & 31) else 0
            elif (hw & 0xFE00) == 0xE800:  # beqzs8
                if regs[15] == 0:
                    npc = pc + (dis.sbits(hw & 0xFF, 8) << 1)
            elif (hw & 0xFF80) == 0xFC00:  # push25
                re = (hw >> 5) & 3
                imm = (hw & 31) << 3
                reg_list = {0: [6, 30], 1: [6, 7, 30], 2: [6, 7, 8, 9, 10, 30]}[re]
                for r in reg_list:
                    push_u32(regs[r])
                regs[31] -= imm
            elif (hw & 0xFF80) == 0xFC80:  # pop25
                re = (hw >> 5) & 3
                imm = (hw & 31) << 3
                regs[31] += imm
                reg_list = {0: [6, 30], 1: [6, 7, 30], 2: [6, 7, 8, 9, 10, 30]}[re]
                for r in reversed(reg_list):
                    regs[r] = pop_u32()
                npc = regs[30]
            else:
                raise AssertionError(f"Unsupported 16-bit insn @ 0x{pc:05x}: 0x{hw:04x} ({dis.dec16(hw, pc)})")
            pc = npc
        else:
            w = struct.unpack_from(">I", rom, pc)[0]
            npc = pc + 4
            op6 = (w >> 25) & 0x3F
            rt, ra, rb = (w >> 20) & 31, (w >> 15) & 31, (w >> 10) & 31
            imm15s = dis.sbits(w & 0x7FFF, 15)
            imm15u = w & 0x7FFF
            if op6 == 0x00:  # lbi
                regs[rt] = mem[(regs[ra] + imm15s) & 0xFFFFFFFF]
            elif op6 == 0x08:  # sbi
                mem[(regs[ra] + imm15s) & 0xFFFFFFFF] = regs[rt] & 0xFF
            elif op6 == 0x20:
                sub = w & 0x1F
                if sub == 4:  # or
                    regs[rt] = (regs[ra] | regs[rb]) & 0xFFFFFFFF
                elif sub == 8:  # slli
                    regs[rt] = (regs[ra] << rb) & 0xFFFFFFFF
                else:
                    raise AssertionError(f"Unsupported ALU1 sub={sub} @ 0x{pc:05x}")
            elif op6 == 0x22:  # movi
                regs[rt] = dis.sbits(w & 0xFFFFF, 20) & 0xFFFFFFFF
            elif op6 == 0x23:  # sethi
                regs[rt] = ((w & 0xFFFFF) << 12) & 0xFFFFFFFF
            elif op6 == 0x24:  # j / jal
                tgt = (pc + (dis.sbits(w & 0xFFFFFF, 24) << 1)) & 0xFFFFFFFF
                is_jal = (w >> 24) & 1
                if is_jal and tgt in stop_calls:
                    calls.append((tgt, regs[0], regs[1]))
                else:
                    if is_jal:
                        regs[30] = npc
                    npc = tgt
            elif op6 == 0x26:  # beq / bne
                is_bne = (w >> 14) & 1
                tgt = (pc + (dis.sbits(w & 0x3FFF, 14) << 1)) & 0xFFFFFFFF
                if (regs[rt] != regs[ra]) if is_bne else (regs[rt] == regs[ra]):
                    npc = tgt
            elif op6 == 0x27 and ((w >> 16) & 0xF) == 2:  # beqz
                tgt = (pc + (dis.sbits(w & 0xFFFF, 16) << 1)) & 0xFFFFFFFF
                if regs[rt] == 0:
                    npc = tgt
            elif op6 == 0x28:  # addi
                regs[rt] = (regs[ra] + imm15s) & 0xFFFFFFFF
            elif op6 == 0x2A:  # andi
                regs[rt] = regs[ra] & imm15u
            elif op6 == 0x2C:  # ori
                regs[rt] = regs[ra] | imm15u
            elif op6 == 0x2D:  # beqc / bnec
                b = (w >> 19) & 1
                imm11 = (w >> 8) & 0x7FF
                tgt = (pc + (dis.sbits(w & 0xFF, 8) << 1)) & 0xFFFFFFFF
                if (regs[rt] != imm11) if b else (regs[rt] == imm11):
                    npc = tgt
            elif op6 == 0x2E:  # slti (unsigned)
                regs[rt] = 1 if regs[ra] < imm15u else 0
            else:
                raise AssertionError(f"Unsupported 32-bit insn @ 0x{pc:05x}: 0x{w:08x} ({dis.dec32(w, pc)})")
            pc = npc
    return regs, mem, calls


# ---------------------------------------------------------------------------
# Main patch builder
# ---------------------------------------------------------------------------

def _patch_bytes(buf: bytearray, addr: int, expected: bytes, replacement: bytes, label: str, log: list[dict]) -> None:
    if len(expected) != len(replacement):
        raise ValueError(f"{label}: length mismatch {len(expected)} != {len(replacement)}")
    actual = bytes(buf[addr : addr + len(expected)])
    if actual != expected:
        raise RuntimeError(
            f"{label} @ 0x{addr:05X}: expected {expected[:16].hex(' ')}, got {actual[:16].hex(' ')}"
        )
    if actual == replacement:
        return
    buf[addr : addr + len(replacement)] = replacement
    log.append({
        "addr": f"0x{addr:05X}",
        "size": len(replacement),
        "before_head": expected[:16].hex(" "),
        "after_head": replacement[:16].hex(" "),
        "reason": label,
    })


def build_v2_rom(stock: bytes, mirror_flag_ff: bool = True) -> tuple[bytes, list[dict]]:
    if len(stock) != ROM_SIZE:
        raise RuntimeError(f"Expected {ROM_SIZE} bytes, got {len(stock)}")
    if hashlib.sha256(stock).hexdigest() != STOCK_SHA256:
        raise RuntimeError("Stock ROM SHA-256 mismatch")
    for bank in SKU_BANKS:
        if stock[bank : bank + 0x1000] != stock[0x3D000 : 0x3E000]:
            raise RuntimeError(f"Unexpected SKU bank divergence at 0x{bank:05X}")

    buf = bytearray(stock)
    changes: list[dict] = []

    # 1. Optional ITE 16-byte signature byte 13 (0x0008D: 0x00 -> 0xFF) for GD25Q64 -> internal eFlash mirror
    if mirror_flag_ff:
        _patch_bytes(
            buf,
            0x0008D,
            bytes([0x00]),
            bytes([0xFF]),
            "ITE signature byte 0x0008D: 0x00 -> 0xFF (triggers IT8390E GD25Q64 -> internal eFlash hardware mirror)",
            changes,
        )

    # 2. Manual fan RPM range 0-5500 across all 4 SKU banks x 8 mode slots + Silent AC/DC CPU TDP 25W/35W
    expected_footers = {
        0: (0x2B, 0x2B, 0x37, 0x37, 0x37),
        1: (0x31, 0x31, 0x37, 0x37, 0x37),
        2: (0x31, 0x31, 0x37, 0x37, 0x37),
        3: (0x31, 0x31, 0x37, 0x37, 0x37),
        4: (0x31, 0x31, 0x37, 0x37, 0x37),
        5: (0x18, 0x1D, 0x2D, 0x32, 0x32),
        6: (0x18, 0x1D, 0x2D, 0x32, 0x32),
        7: (0x18, 0x1D, 0x2D, 0x32, 0x32),
    }
    for bank in SKU_BANKS:
        for slot in range(8):
            f_base = bank + slot * 0x200 + 0x1A8
            e21, e22, e24, e25, e26 = expected_footers[slot]
            _patch_bytes(buf, f_base + 0x21, bytes([e21, e22]), bytes([0x00, 0x00]),
                         f"Bank 0x{bank:05X} Slot {slot} manual RPM min -> 0 RPM", changes)
            _patch_bytes(buf, f_base + 0x24, bytes([e24, e25, e26]), bytes([0x37, 0x37, 0x37]),
                         f"Bank 0x{bank:05X} Slot {slot} manual RPM max -> 5500 RPM", changes)
            if slot in (5, 6, 7):
                _patch_bytes(buf, f_base + 0x18, bytes([35, 45]), bytes([25, 35]),
                             f"Bank 0x{bank:05X} Silent Slot {slot} AC PL1/PL2: 35/45W -> 25/35W", changes)
                _patch_bytes(buf, f_base + 0x2E, bytes([35]), bytes([25]),
                             f"Bank 0x{bank:05X} Silent Slot {slot} thermal PL1: 35W -> 25W", changes)
                _patch_bytes(buf, f_base + 0x31, bytes([80]), bytes([35]),
                             f"Bank 0x{bank:05X} Silent Slot {slot} thermal PL2: 80W -> 35W", changes)
                _patch_bytes(buf, f_base + 0x33, bytes([30, 30]), bytes([25, 35]),
                             f"Bank 0x{bank:05X} Silent Slot {slot} DC PL1/PL2: 30/30W -> 25/35W", changes)

    # 3. Bump 0x3FE000 external SPI thermal cache version 2 -> 4 (forces rebuild even over v1's cache v3!)
    _patch_bytes(
        buf,
        0x1211E,
        bytes.fromhex("5a 08 02 0b"),
        bytes.fromhex("5a 08 04 0b"),
        "0x120ca: require cached 0x3FE000 slot version == 4 (auto-rebuilds over stock v2 and mod v1's v3)",
        changes,
    )
    _patch_bytes(
        buf,
        0x11316,
        bytes.fromhex("46 00 00 80 00 00 02 ed"),
        bytes.fromhex("44 93 d0 00 85 04 d5 12"),
        "0x11314: movi r9, #0x3d000; movi55 r8, #4; j8 0x11340 (writes cache version=4, keeps r7=2 for 0x0200 len)",
        changes,
    )
    for idx, addr in enumerate(SLOT_VERSION_STORE_ADDRS):
        _patch_bytes(
            buf,
            addr,
            bytes.fromhex("10 73 02 18"),
            bytes.fromhex("10 83 02 18"),
            f"0x11314 slot {idx}: sbi r8, [r6+0x218] (write cache version=4)",
            changes,
        )

    # 4. Custom Mode 4 AC CPU TDP (Low 35/45W, Med 60/80W, High 60/90W; DC untouched)
    custom_pl_code = assemble_custom_pl_init()
    _patch_bytes(
        buf,
        0x10070,
        stock[0x10070:0x101C8],
        custom_pl_code,
        "0x10070: Custom Mode 4 PL table -> Low 35/45W, Med 60/80W, High 60/90W AC (DC 20/25/30W unchanged)",
        changes,
    )
    # Fix Custom Medium extreme-thermal fallback (0x0e162: PL2=40W -> PL1=35W so PL2 never drops below PL1=60W)
    _patch_bytes(
        buf,
        0x0E162,
        bytes.fromhex("fa 18"),
        bytes.fromhex("fa 33"),
        "0x0e162: Custom Med bit1 thermal fallback movpi45 r0,#40 -> movpi45 r1,#35 (matches Custom High 0x0e1aa)",
        changes,
    )

    # 5. Bump 0x050000 external SPI runtime defaults cache version 7 -> 8 (triggers automatic rebuild + re-CRC!)
    _patch_bytes(buf, 0x1046C, bytes.fromhex("84 07"), bytes.fromhex("84 08"),
                 "0x10466: write 0x86004 defaults cache version = 8", changes)
    _patch_bytes(buf, 0x104FC, bytes.fromhex("84 07"), bytes.fromhex("84 08"),
                 "0x104da: write 0x86004 defaults cache version = 8 on save", changes)
    _patch_bytes(buf, 0x105CC, bytes.fromhex("5a 00 07 10"), bytes.fromhex("5a 00 08 10"),
                 "0x105a2: require 0x050000 defaults cache version == 8 on cold boot", changes)
    _patch_bytes(buf, 0x10606, bytes.fromhex("5a 00 07 04"), bytes.fromhex("5a 00 08 04"),
                 "0x105f2: require 0x050000 defaults cache version == 8 on reload", changes)

    # 6. Custom Mode 4 GPU MAX 90W (CTGP=1) for Custom Low (CPU=0) & Custom Med (CPU=1), 80W for Custom High (CPU=2)
    gpu_ctgp_code = assemble_custom_gpu_ctgp_handler()
    _patch_bytes(
        buf,
        0x0E534,
        stock[0x0E534:0x0E592],
        gpu_ctgp_code,
        "0x0e534: Custom Mode 4 GPU CTGP handler -> 90W (PSTA bit 3=1 + _Q33) for Low/Med (cpu<2), 80W for High (cpu=2)",
        changes,
    )

    patched = bytes(buf)
    verify_v2_rom(stock, patched, mirror_flag_ff=mirror_flag_ff)
    return patched, changes


def build_stock_mirror_rom(stock: bytes) -> bytes:
    """Emit golden stock v01.00 with ONLY 0x0008D: 0x00 -> 0xFF so GD25Q64 mirror rollback restores stock."""
    if len(stock) != ROM_SIZE or hashlib.sha256(stock).hexdigest() != STOCK_SHA256:
        raise RuntimeError("Stock ROM verification failed")
    buf = bytearray(stock)
    assert buf[0x0008D] == 0x00
    buf[0x0008D] = 0xFF
    out = bytes(buf)
    assert sum(a != b for a, b in zip(stock, out)) == 1
    return out


def verify_v2_rom(stock: bytes, patched: bytes, mirror_flag_ff: bool) -> None:
    assert len(patched) == ROM_SIZE
    # Boot vectors, ILM flash loop, reset entry, and PM1 0xDC handler must be 100% untouched
    assert patched[0x00000:0x0008D] == stock[0x00000:0x0008D]
    assert patched[0x0008D] == (0xFF if mirror_flag_ff else 0x00)
    assert patched[0x0008E:0x00100] == stock[0x0008E:0x00100]
    assert patched[0x02000:0x03100] == stock[0x02000:0x03100]
    assert patched[0x17180:0x17350] == stock[0x17180:0x17350]
    # 0x0e592 ret5 untouched
    assert patched[0x0E592:0x0E594] == bytes.fromhex("dd 9e")

    # 1. Verify disassembly of key instructions
    assert dis.dec32(struct.unpack(">I", patched[0x11316:0x1131A])[0], 0x11316) == "movi r9, #0x3d000"
    assert dis.dec16(struct.unpack(">H", patched[0x1131A:0x1131C])[0], 0x1131A) == "movi55 r8, #4"
    assert dis.dec16(struct.unpack(">H", patched[0x1131C:0x1131E])[0], 0x1131C) == "j8 0x11340"
    assert dis.dec32(struct.unpack(">I", patched[0x1211E:0x12122])[0], 0x1211E) == "bnec r0, #0x4, 0x12134"
    assert dis.dec16(struct.unpack(">H", patched[0x0E162:0x0E164])[0], 0x0E162) == "movpi45 r1, #35"
    assert dis.dec16(struct.unpack(">H", patched[0x1046C:0x1046E])[0], 0x1046C) == "movi55 r0, #8"
    assert dis.dec16(struct.unpack(">H", patched[0x104FC:0x104FE])[0], 0x104FC) == "movi55 r0, #8"
    assert dis.dec32(struct.unpack(">I", patched[0x105CC:0x105D0])[0], 0x105CC) == "beqc r0, #0x8, 0x105ec"
    assert dis.dec32(struct.unpack(">I", patched[0x10606:0x1060A])[0], 0x10606) == "beqc r0, #0x8, 0x1060e"

    # 2. Verify all 4 SKU banks x 8 mode slots
    for bank in SKU_BANKS:
        assert patched[bank : bank + 0x1000] == patched[0x3D000 : 0x3E000]
        for slot in range(8):
            s_old = decode_slot(stock, bank, slot)
            s_new = decode_slot(patched, bank, slot)
            assert s_new["manual_min_rpm"] == 0
            assert s_new["manual_max_rpm"] == 5500
            assert s_new["manual_default_rpm"] == s_old["manual_default_rpm"]
            assert s_new["steps"] == s_old["steps"]
            assert s_new["ndtt_c"] == s_old["ndtt_c"]
            assert s_new["dgps_level"] == s_old["dgps_level"]
            if slot in (5, 6, 7):
                assert (s_new["ac_pl1_w"], s_new["ac_pl2_w"]) == (25, 35)
                assert (s_new["dc_pl1_w"], s_new["dc_pl2_w"]) == (25, 35)
                f = bank + slot * 0x200 + 0x1A8
                assert (patched[f + 0x2E], patched[f + 0x31]) == (25, 35)
            else:
                assert (s_new["ac_pl1_w"], s_new["ac_pl2_w"]) == (s_old["ac_pl1_w"], s_old["ac_pl2_w"])
                assert (s_new["dc_pl1_w"], s_new["dc_pl2_w"]) == (s_old["dc_pl1_w"], s_old["dc_pl2_w"])

    # 3. Execute emitted 0x10070 machine code in NDS32 emulator and verify all 4 banks of 0x86900..0x8699F
    canary = {6: 0x11111116, 7: 0x22222227, 8: 0x33333338, 9: 0x44444449, 10: 0x5555555A}
    regs, mem, calls = emu_nds32(patched, 0x10070, canary, {})
    assert not calls
    for r, val in canary.items():
        assert regs[r] == val, f"0x10070 clobbered callee-saved r{r}"
    assert regs[31] == 0x00083FF0, "0x10070 unbalanced sp"
    for b_idx in range(4):
        b_off = 0x86900 + b_idx * 0x28
        assert list(mem[b_off + 0x00 : b_off + 0x04]) == [35, 45, 20, 20], "Custom Low PL mismatch"
        assert list(mem[b_off + 0x08 : b_off + 0x0C]) == [60, 80, 25, 25], "Custom Med PL mismatch"
        for high_off in (0x10, 0x18, 0x20):
            assert list(mem[b_off + high_off : b_off + high_off + 4]) == [60, 90, 30, 30], "Custom High PL mismatch"

    # 4. Execute emitted 0x0e50a..0x0e592 machine code across ALL (cpu, gpu) states + steady-state + non-mode-4
    # Non-Mode-4 early exit (mode=1 Gaming -> must not touch PSTA or call _Q33)
    _, mem_m1, calls_m1 = emu_nds32(
        patched, 0x0E50A, canary,
        {0x802ED: 3, 0x8035F: 1, 0x8034F: 1, 0x808E0: 0, 0x808E1: 1, 0x808E2: 1, 0x802AA: 0x85},
        stop_calls={0x0993E},
    )
    assert calls_m1 == [] and mem_m1[0x802AA] == 0x85

    # All 9 (cpu, gpu) combinations in Mode 4 starting from post-mode-entry prev=1
    for cpu in (0, 1, 2):
        for gpu in (0, 1, 2):
            init_m = {
                0x802ED: 3, 0x8035F: 4, 0x8034F: 4,
                0x808E0: cpu, 0x808E1: gpu, 0x808E2: 1,
                0x802AA: 0x85 if cpu == 2 else 0x8D,  # flip starting bit 3 opposite of expected to prove update!
                0x80205: 0x00,
            }
            # Deliberately start bit 3 opposite of target
            init_m[0x802AA] = 0x8D if cpu == 2 else 0x85
            regs2, mem2, calls2 = emu_nds32(patched, 0x0E50A, canary, init_m, stop_calls={0x0993E})
            for r, val in canary.items():
                assert regs2[r] == val, f"0x0e50a clobbered callee-saved r{r}"
            assert regs2[31] == 0x00083FF0
            expected_state = 0x80 | (cpu << 4) | gpu
            expected_ndtt = 75 if gpu == 0 else 87
            expected_psta = 0x8D if cpu < 2 else 0x85  # bit 3 = 1 (90W) for Low/Med, 0 (80W) for High; other bits preserved!
            assert mem2[0x808E2] == expected_state
            assert mem2[0x80205] == expected_ndtt
            assert mem2[0x802AA] == expected_psta, f"cpu={cpu} gpu={gpu}: PSTA=0x{mem2[0x802AA]:02x} != 0x{expected_psta:02x}"
            assert calls2 == [(0x0993E, 0x33, 0)]

            # Second tick with unchanged state must NOT fire _Q33 again!
            init_steady = {k: mem2[k] for k in init_m}
            _, mem3, calls3 = emu_nds32(patched, 0x0E50A, canary, init_steady, stop_calls={0x0993E})
            assert calls3 == [], f"Redundant _Q33 on steady tick for cpu={cpu}, gpu={gpu}"
            assert mem3[0x802AA] == expected_psta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock", type=Path, default=STOCK_ROM)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--stage-windows-kit", action="store_true",
                        help="Also copy v2 ROM + mirror-ready stock rollback ROM and update ToolConfig.ini in windows_razer_flash/")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    stock = args.stock.read_bytes()
    patched, changes = build_v2_rom(stock, mirror_flag_ff=True)
    stock_mirror = build_stock_mirror_rom(stock)

    patched_sha = hashlib.sha256(patched).hexdigest()
    stock_mirror_sha = hashlib.sha256(stock_mirror).hexdigest()
    diff_count = sum(a != b for a, b in zip(stock, patched))

    print(f"Stock ROM        : {args.stock} (SHA-256: {STOCK_SHA256})")
    print(f"v2 Mod ROM (8DFF): {args.out_dir / OUT_BIN_NAME} (SHA-256: {patched_sha})")
    print(f"Stock Mirror 8DFF: {args.out_dir / STOCK_MIRROR_NAME} (SHA-256: {stock_mirror_sha})")
    print(f"Verified         : {diff_count} differing bytes vs stock; NDS32 disassembly & emulator tests ALL PASSED.")

    if args.check_only:
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_bin = args.out_dir / OUT_BIN_NAME
    out_bin.write_bytes(patched)
    (args.out_dir / STOCK_MIRROR_NAME).write_bytes(stock_mirror)

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "target_board": "Razer Blade 15 Base Early 2020 (RZ09-0328 / DA550-MB / SSID 0x2011)",
        "target_ec": "ITE IT8390E-256 (ITE8390EC-V01.00, internal 256 KiB eFlash mirrored from GD25Q64 via 0x0008D=0xFF)",
        "summary": {
            "manual_fan_rpm_all_modes": "0-5500 RPM (default seeds preserved)",
            "custom_high_ac_cpu_tdp": "60 W PL1 / 90 W PL2 (GPU 80 W, CTGP=0)",
            "custom_medium_ac_cpu_tdp_gpu": "60 W PL1 / 80 W PL2, GPU MAX 90 W (CTGP=1)",
            "custom_low_ac_cpu_tdp_gpu": "35 W PL1 / 45 W PL2, GPU MAX 90 W (CTGP=1)",
            "silent_ac_and_dc_cpu_tdp": "25 W PL1 / 35 W PL2",
            "cache_versions": {"spi_3fe000_thermal": 4, "spi_050000_defaults": 8},
            "ite_mirror_flag_0008d": "0xFF",
        },
        "stock_sha256": STOCK_SHA256,
        "v2_rom": out_bin.name,
        "v2_sha256": patched_sha,
        "stock_mirror_rom": STOCK_MIRROR_NAME,
        "stock_mirror_sha256": stock_mirror_sha,
        "total_bytes_changed": diff_count,
        "changes": changes,
    }
    (args.out_dir / "patch-manifest-v2.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if args.stage_windows_kit:
        (FLASH_KIT_DIR / OUT_BIN_NAME).write_bytes(patched)
        (FLASH_KIT_DIR / STOCK_MIRROR_NAME).write_bytes(stock_mirror)
        (FLASH_KIT_DIR / "ToolConfig.ini").write_text(
            f"[ToolConfig]\nFILE={OUT_BIN_NAME}\nSSID=2011\nREBOOT=0\n",
            encoding="ascii",
        )
        (FLASH_KIT_DIR / "ToolConfig.STOCK_ROLLBACK.ini").write_text(
            f"[ToolConfig]\nFILE={STOCK_MIRROR_NAME}\nSSID=2011\nREBOOT=0\n",
            encoding="ascii",
        )
        print(f"Staged in kit    : {FLASH_KIT_DIR}")
        print(f"ToolConfig.ini   : FILE={OUT_BIN_NAME}")
        print(f"Rollback INI     : FILE={STOCK_MIRROR_NAME}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
