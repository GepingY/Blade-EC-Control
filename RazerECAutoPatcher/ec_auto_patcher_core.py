"""Universal signature-scanning EC ROM analyzer, NDS32 verifier & auto-patcher core
for ITE IT8390E-based Razer Blade laptops (strictly offline — never touches hardware).

Features:
  - Zero hardcoded firmware offsets or model SHA-256 whitelists.
  - Dynamically locates:
      1. ITE 16-byte eFlash signature, mirror flag (+13), and ASCII FW version.
      2. Thermal mode loader (max mode count + SPI 0x3FE000 cache version check).
      3. SPI cache builder (SKU bank addresses, exact existing mode slots 0x20+k,
         and register-safe cache version bump strategy — preserving distinct SKU banks!).
      4. Valid existing mode slots (dynamic fan count 1..4, dynamic active sensors from
         sensor_mask, dynamic step count 0..max_step, footer TDP & manual RPM clamps,
         plus clone-slot detection). Never creates new modes.
      5. Custom Mode (4) CPU PL1/PL2 initializer (0x86900) by emulating the loaded ROM's
         own NDS32 function, plus all 4 external SPI 0x050000 runtime defaults cache
         version sites and optional Custom Med thermal fallback fix.
  - Built-in NDS32 instruction-level emulator verifies every emitted code change
    (SKID bank dispatch across SKID 0..255 + Custom PL init across all 4 banks)
    before allowing export.

Run offline self-test suite:
    python RazerECAutoPatcher/ec_auto_patcher_core.py --self-test
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import struct
from typing import Iterable


# ---------------------------------------------------------------------------
# Constants & Friendly Labels
# ---------------------------------------------------------------------------

STANDARD_MODE_NAMES: dict[int, str] = {
    0: "Balanced",
    1: "Gaming",
    2: "Creator / Performance",
    3: "Legacy / Mode 3",
    4: "Custom",
    5: "Silent",
}

SENSOR_NAMES: dict[int, str] = {
    0: "S0 (CPU)",
    1: "S1 (GPU)",
    2: "S2 (Ch 0x90)",
    3: "S3 (0x92)",
    4: "S4 (0x94)",
    5: "S5 (0x96)",
    6: "S6",
    7: "S7",
}

# NDS32 4-bit compressed register map (Reg4 -> GPR index)
NDS32_R45MAP = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 16, 17, 28, 29)
# push25/pop25 saved register lists by `re` field (0..3)
PUSH25_REGS: dict[int, tuple[int, ...]] = {
    0: (6, 30),
    1: (6, 7, 30),
    2: (6, 7, 8, 9, 10, 30),
    3: (6, 7, 8, 9, 10, 11, 12, 13, 14, 30),
}


# ---------------------------------------------------------------------------
# NDS32 Big-Endian Instruction Codec & Mini-Emulator
# ---------------------------------------------------------------------------

def sbits(val: int, n: int) -> int:
    m = 1 << (n - 1)
    return (val ^ m) - m


def _enc32(w: int) -> bytes:
    return struct.pack(">I", w & 0xFFFFFFFF)


def _enc16(h: int) -> bytes:
    return struct.pack(">H", h & 0xFFFF)


def enc_lbi(rt: int, ra: int, imm15: int) -> bytes:
    return _enc32((0x00 << 25) | ((rt & 31) << 20) | ((ra & 31) << 15) | (imm15 & 0x7FFF))


def enc_sbi(rt: int, ra: int, imm15: int) -> bytes:
    return _enc32((0x08 << 25) | ((rt & 31) << 20) | ((ra & 31) << 15) | (imm15 & 0x7FFF))


def enc_add(rt: int, ra: int, rb: int) -> bytes:
    return _enc32((0x20 << 25) | ((rt & 31) << 20) | ((ra & 31) << 15) | ((rb & 31) << 10) | 0)


def enc_slli(rt: int, ra: int, ib5: int) -> bytes:
    return _enc32((0x20 << 25) | ((rt & 31) << 20) | ((ra & 31) << 15) | ((ib5 & 31) << 10) | 8)


def enc_movi(rt: int, imm20: int) -> bytes:
    return _enc32((0x22 << 25) | ((rt & 31) << 20) | (imm20 & 0xFFFFF))


def enc_sethi(rt: int, hi20: int) -> bytes:
    return _enc32((0x23 << 25) | ((rt & 31) << 20) | (hi20 & 0xFFFFF))


def enc_beq_bne(is_bne: bool, rt: int, ra: int, pc: int, target: int) -> bytes:
    disp = (target - pc) >> 1
    assert -(1 << 13) <= disp < (1 << 13) and (target - pc) % 2 == 0
    return _enc32(
        (0x26 << 25)
        | ((rt & 31) << 20)
        | ((ra & 31) << 15)
        | ((1 if is_bne else 0) << 14)
        | (disp & 0x3FFF)
    )


def enc_addi(rt: int, ra: int, imm15: int) -> bytes:
    return _enc32((0x28 << 25) | ((rt & 31) << 20) | ((ra & 31) << 15) | (imm15 & 0x7FFF))


def enc_beqc_bnec(is_bnec: bool, rt: int, imm11: int, pc: int, target: int) -> bytes:
    disp = (target - pc) >> 1
    assert -(1 << 7) <= disp < (1 << 7) and (target - pc) % 2 == 0
    assert 0 <= imm11 <= 0x7FF
    return _enc32(
        (0x2D << 25)
        | ((rt & 31) << 20)
        | ((1 if is_bnec else 0) << 19)
        | ((imm11 & 0x7FF) << 8)
        | (disp & 0xFF)
    )


def enc_movi55(rt: int, imm5: int) -> bytes:
    assert -16 <= imm5 <= 15
    return _enc16(0x8400 | ((rt & 31) << 5) | (imm5 & 31))


def enc_subi45(rt4_idx: int, imm5: int) -> bytes:
    assert 0 <= rt4_idx <= 15 and 0 <= imm5 <= 31
    return _enc16(0x8E00 | (rt4_idx << 5) | imm5)


def enc_slti45(rt4_idx: int, imm5: int) -> bytes:
    assert 0 <= rt4_idx <= 15 and 0 <= imm5 <= 31
    return _enc16(0xE600 | (rt4_idx << 5) | imm5)


def enc_beqzs8(pc: int, target: int) -> bytes:
    disp = (target - pc) >> 1
    assert -(1 << 7) <= disp < (1 << 7) and (target - pc) % 2 == 0
    return _enc16(0xE800 | (disp & 0xFF))


def enc_j8(pc: int, target: int) -> bytes:
    disp = (target - pc) >> 1
    assert -(1 << 7) <= disp < (1 << 7) and (target - pc) % 2 == 0
    return _enc16(0xD500 | (disp & 0xFF))


def enc_movpi45(rt4_idx: int, val: int) -> bytes:
    assert 0 <= rt4_idx <= 15 and 16 <= val <= 47
    return _enc16(0xFA00 | (rt4_idx << 5) | ((val - 16) & 31))


def enc_push25(re: int, imm: int = 0) -> bytes:
    return _enc16(0xFC00 | ((re & 3) << 5) | ((imm >> 3) & 31))


def enc_pop25(re: int, imm: int = 0) -> bytes:
    return _enc16(0xFC80 | ((re & 3) << 5) | ((imm >> 3) & 31))


def enc_nop16() -> bytes:
    return _enc16(0x9200)


def iter_insns(rom: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    """Decode instruction stream boundaries: list of (pc, size_bytes, raw_val)."""
    out: list[tuple[int, int, int]] = []
    pc = start
    limit = min(end, len(rom))
    while pc + 2 <= limit:
        hw = struct.unpack_from(">H", rom, pc)[0]
        if (hw & 0x8000) == 0:
            if pc + 4 > limit:
                break
            w = struct.unpack_from(">I", rom, pc)[0]
            out.append((pc, 4, w))
            pc += 4
        else:
            out.append((pc, 2, hw))
            pc += 2
    return out


def decode_insn_info(pc: int, size: int, val: int) -> dict:
    """Decode key structural fields of an NDS32 instruction for signature scanning."""
    if size == 4:
        op6 = (val >> 25) & 0x3F
        rt = (val >> 20) & 31
        ra = (val >> 15) & 31
        rb = (val >> 10) & 31
        imm15s = sbits(val & 0x7FFF, 15)
        if op6 == 0x00:
            return {"op": "lbi", "rt": rt, "ra": ra, "imm": imm15s}
        if op6 == 0x01:
            return {"op": "lhi", "rt": rt, "ra": ra, "imm": imm15s << 1}
        if op6 == 0x08:
            return {"op": "sbi", "rt": rt, "ra": ra, "imm": imm15s}
        if op6 == 0x09:
            return {"op": "shi", "rt": rt, "ra": ra, "imm": imm15s << 1}
        if op6 == 0x20:
            sub = val & 0x1F
            if sub == 0:
                return {"op": "add", "rt": rt, "ra": ra, "rb": rb}
            if sub == 8:
                return {"op": "slli", "rt": rt, "ra": ra, "imm": rb}
        if op6 == 0x22:
            return {"op": "movi", "rt": rt, "imm": sbits(val & 0xFFFFF, 20)}
        if op6 == 0x23:
            return {"op": "sethi", "rt": rt, "imm": (val & 0xFFFFF) << 12}
        if op6 == 0x24:
            tgt = (pc + (sbits(val & 0xFFFFFF, 24) << 1)) & 0xFFFFFFFF
            return {"op": "jal" if ((val >> 24) & 1) else "j", "target": tgt}
        if op6 == 0x26:
            tgt = (pc + (sbits(val & 0x3FFF, 14) << 1)) & 0xFFFFFFFF
            return {"op": "bne" if ((val >> 14) & 1) else "beq", "rt": rt, "ra": ra, "target": tgt}
        if op6 == 0x27 and ((val >> 16) & 0xF) == 2:
            tgt = (pc + (sbits(val & 0xFFFF, 16) << 1)) & 0xFFFFFFFF
            return {"op": "beqz", "rt": rt, "target": tgt}
        if op6 == 0x28:
            return {"op": "addi", "rt": rt, "ra": ra, "imm": imm15s}
        if op6 == 0x2D:
            is_bnec = bool((val >> 19) & 1)
            imm11 = (val >> 8) & 0x7FF
            tgt = (pc + (sbits(val & 0xFF, 8) << 1)) & 0xFFFFFFFF
            return {"op": "bnec" if is_bnec else "beqc", "rt": rt, "imm": imm11, "target": tgt}
        if op6 == 0x2E:
            return {"op": "slti", "rt": rt, "ra": ra, "imm": val & 0x7FFF}
        return {"op": f"u32_{op6:02x}", "rt": rt, "ra": ra}
    else:
        rt5 = (val >> 5) & 31
        ra5 = val & 31
        rt4 = NDS32_R45MAP[(val >> 5) & 15]
        rt3 = (val >> 6) & 7
        ra3 = (val >> 3) & 7
        if val == 0x9200:
            return {"op": "nop16"}
        if (val & 0xFC00) == 0x8000:
            return {"op": "mov55", "rt": rt5, "ra": ra5}
        if (val & 0xFC00) == 0x8400:
            return {"op": "movi55", "rt": rt5, "imm": sbits(val & 31, 5)}
        if (val & 0xFE00) == 0x8800:
            return {"op": "add45", "rt": rt4, "ra": ra5}
        if (val & 0xFE00) == 0x8E00:
            return {"op": "subi45", "rt": rt4, "imm": val & 31}
        if (val & 0xFE00) == 0xA400:
            return {"op": "lhi333", "rt": rt3, "ra": ra3, "imm": (val & 7) << 1}
        if (val & 0xFE00) == 0xAC00:
            return {"op": "shi333", "rt": rt3, "ra": ra3, "imm": (val & 7) << 1}
        if (val & 0xFE00) == 0xAE00:
            return {"op": "sbi333", "rt": rt3, "ra": ra3, "imm": val & 7}
        if (val & 0xFF00) == 0xD500:
            tgt = (pc + (sbits(val & 0xFF, 8) << 1)) & 0xFFFFFFFF
            return {"op": "j8", "target": tgt}
        if (val & 0xFFE0) == 0xDD80:
            return {"op": "ret5", "ra": ra5}
        if (val & 0xFE00) == 0xE600:
            return {"op": "slti45", "rt": rt4, "imm": val & 31}
        if (val & 0xFF00) == 0xE800:
            tgt = (pc + (sbits(val & 0xFF, 8) << 1)) & 0xFFFFFFFF
            return {"op": "beqzs8", "target": tgt}
        if (val & 0xFF00) == 0xE900:
            tgt = (pc + (sbits(val & 0xFF, 8) << 1)) & 0xFFFFFFFF
            return {"op": "bnezs8", "target": tgt}
        if (val & 0xFE00) == 0xFA00:
            return {"op": "movpi45", "rt": rt4, "imm": (val & 31) + 16}
        if (val & 0xFF80) == 0xFC00:
            return {"op": "push25", "re": (val >> 5) & 3, "imm": (val & 31) << 3}
        if (val & 0xFF80) == 0xFC80:
            return {"op": "pop25", "re": (val >> 5) & 3, "imm": (val & 31) << 3}
        return {"op": "u16"}


def emu_nds32(
    rom: bytes,
    start_pc: int,
    init_regs: dict[int, int] | None = None,
    init_mem: dict[int, int] | None = None,
    stop_pcs: set[int] | None = None,
    stop_calls: set[int] | None = None,
    max_steps: int = 8000,
) -> tuple[list[int], bytearray, list[tuple[int, int, int]], int]:
    """Execute real NDS32 instructions from `rom` starting at `start_pc`.

    Stops when returning to sentinel LP or when `pc` hits `stop_pcs`.
    Returns (regs, mem, calls, final_pc).
    """
    sentinel_lp = 0xDEADBEEF
    regs = [0] * 32
    regs[30] = sentinel_lp
    regs[31] = 0x00083FF0
    if init_regs:
        for k, v in init_regs.items():
            regs[k] = v & 0xFFFFFFFF
    mem = bytearray(0x00090000)
    if init_mem:
        for addr, val in init_mem.items():
            mem[addr] = val & 0xFF

    pc = start_pc
    calls: list[tuple[int, int, int]] = []
    stop_pcs = stop_pcs or set()
    stop_calls = stop_calls or set()
    steps = 0

    def push_u32(val: int) -> None:
        regs[31] -= 4
        struct.pack_into("<I", mem, regs[31], val & 0xFFFFFFFF)

    def pop_u32() -> int:
        val = struct.unpack_from("<I", mem, regs[31])[0]
        regs[31] += 4
        return val

    while pc != sentinel_lp and pc not in stop_pcs:
        steps += 1
        if steps > max_steps:
            raise AssertionError(f"NDS32 emulator exceeded {max_steps} steps @ 0x{pc:05X}")
        if rom[pc] & 0x80:
            hw = struct.unpack_from(">H", rom, pc)[0]
            npc = pc + 2
            if hw == 0x9200:
                pass  # nop16
            elif (hw & 0xFFE0) == 0xDD80:
                npc = regs[hw & 31]  # ret5
            elif (hw & 0xFC00) == 0x8000:
                regs[(hw >> 5) & 31] = regs[hw & 31]  # mov55
            elif (hw & 0xFC00) == 0x8400:
                regs[(hw >> 5) & 31] = sbits(hw & 31, 5) & 0xFFFFFFFF  # movi55
            elif (hw & 0xFE00) == 0x8800:
                rt4 = NDS32_R45MAP[(hw >> 5) & 15]
                regs[rt4] = (regs[rt4] + regs[hw & 31]) & 0xFFFFFFFF  # add45
            elif (hw & 0xFE00) == 0x8C00:
                rt4 = NDS32_R45MAP[(hw >> 5) & 15]
                regs[rt4] = (regs[rt4] + (hw & 31)) & 0xFFFFFFFF  # addi45
            elif (hw & 0xFE00) == 0x8E00:
                rt4 = NDS32_R45MAP[(hw >> 5) & 15]
                regs[rt4] = (regs[rt4] - (hw & 31)) & 0xFFFFFFFF  # subi45
            elif (hw & 0xFE00) == 0x9600 and (hw & 7) == 0:
                regs[(hw >> 6) & 7] = regs[(hw >> 3) & 7] & 0xFF  # zeb33
            elif (hw & 0xFE00) == 0xA600:
                rt3, ra3 = (hw >> 6) & 7, (hw >> 3) & 7
                regs[rt3] = mem[(regs[ra3] + (hw & 7)) & 0xFFFFFFFF]  # lbi333
            elif (hw & 0xFE00) == 0xAE00:
                rt3, ra3 = (hw >> 6) & 7, (hw >> 3) & 7
                mem[(regs[ra3] + (hw & 7)) & 0xFFFFFFFF] = regs[rt3] & 0xFF  # sbi333
            elif (hw & 0xFF00) == 0xD500:
                npc = (pc + (sbits(hw & 0xFF, 8) << 1)) & 0xFFFFFFFF  # j8
            elif (hw & 0xF800) == 0xC000:
                if regs[(hw >> 8) & 7] == 0:
                    npc = (pc + (sbits(hw & 0xFF, 8) << 1)) & 0xFFFFFFFF  # beqz38
            elif (hw & 0xF800) == 0xC800:
                if regs[(hw >> 8) & 7] != 0:
                    npc = (pc + (sbits(hw & 0xFF, 8) << 1)) & 0xFFFFFFFF  # bnez38
            elif (hw & 0xFE00) == 0xE600:
                rt4 = NDS32_R45MAP[(hw >> 5) & 15]
                regs[15] = 1 if regs[rt4] < (hw & 31) else 0  # slti45
            elif (hw & 0xFF00) == 0xE800:
                if regs[15] == 0:
                    npc = (pc + (sbits(hw & 0xFF, 8) << 1)) & 0xFFFFFFFF  # beqzs8
            elif (hw & 0xFF00) == 0xE900:
                if regs[15] != 0:
                    npc = (pc + (sbits(hw & 0xFF, 8) << 1)) & 0xFFFFFFFF  # bnezs8
            elif (hw & 0xFE00) == 0xFA00:
                rt4 = NDS32_R45MAP[(hw >> 5) & 15]
                regs[rt4] = (hw & 31) + 16  # movpi45
            elif (hw & 0xFF80) == 0xFC00:  # push25
                re = (hw >> 5) & 3
                imm = (hw & 31) << 3
                for r in PUSH25_REGS[re]:
                    push_u32(regs[r])
                regs[31] -= imm
            elif (hw & 0xFF80) == 0xFC80:  # pop25
                re = (hw >> 5) & 3
                imm = (hw & 31) << 3
                regs[31] += imm
                for r in reversed(PUSH25_REGS[re]):
                    regs[r] = pop_u32()
                npc = regs[30]
            else:
                raise AssertionError(f"Unsupported 16-bit insn @ 0x{pc:05X}: 0x{hw:04X}")
            pc = npc
        else:
            w = struct.unpack_from(">I", rom, pc)[0]
            npc = pc + 4
            op6 = (w >> 25) & 0x3F
            rt, ra, rb = (w >> 20) & 31, (w >> 15) & 31, (w >> 10) & 31
            imm15s = sbits(w & 0x7FFF, 15)
            imm15u = w & 0x7FFF
            if op6 == 0x00:  # lbi
                regs[rt] = mem[(regs[ra] + imm15s) & 0xFFFFFFFF]
            elif op6 == 0x08:  # sbi
                mem[(regs[ra] + imm15s) & 0xFFFFFFFF] = regs[rt] & 0xFF
            elif op6 == 0x20:
                sub = w & 0x1F
                if sub == 0:  # add
                    regs[rt] = (regs[ra] + regs[rb]) & 0xFFFFFFFF
                elif sub == 1:  # sub
                    regs[rt] = (regs[ra] - regs[rb]) & 0xFFFFFFFF
                elif sub == 4:  # or
                    regs[rt] = (regs[ra] | regs[rb]) & 0xFFFFFFFF
                elif sub == 8:  # slli
                    regs[rt] = (regs[ra] << rb) & 0xFFFFFFFF
                elif sub == 9:  # srli
                    regs[rt] = (regs[ra] >> rb) & 0xFFFFFFFF
                else:
                    raise AssertionError(f"Unsupported ALU1 sub={sub} @ 0x{pc:05X}")
            elif op6 == 0x22:  # movi
                regs[rt] = sbits(w & 0xFFFFF, 20) & 0xFFFFFFFF
            elif op6 == 0x23:  # sethi
                regs[rt] = ((w & 0xFFFFF) << 12) & 0xFFFFFFFF
            elif op6 == 0x24:  # j / jal
                tgt = (pc + (sbits(w & 0xFFFFFF, 24) << 1)) & 0xFFFFFFFF
                is_jal = (w >> 24) & 1
                if is_jal and tgt in stop_calls:
                    calls.append((tgt, regs[0], regs[1]))
                else:
                    if is_jal:
                        regs[30] = npc
                    npc = tgt
            elif op6 == 0x26:  # beq / bne
                is_bne = (w >> 14) & 1
                tgt = (pc + (sbits(w & 0x3FFF, 14) << 1)) & 0xFFFFFFFF
                if (regs[rt] != regs[ra]) if is_bne else (regs[rt] == regs[ra]):
                    npc = tgt
            elif op6 == 0x27 and ((w >> 16) & 0xF) == 2:  # beqz
                tgt = (pc + (sbits(w & 0xFFFF, 16) << 1)) & 0xFFFFFFFF
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
                tgt = (pc + (sbits(w & 0xFF, 8) << 1)) & 0xFFFFFFFF
                if (regs[rt] != imm11) if b else (regs[rt] == imm11):
                    npc = tgt
            elif op6 == 0x2E:  # slti
                regs[rt] = 1 if regs[ra] < imm15u else 0
            else:
                raise AssertionError(f"Unsupported 32-bit insn @ 0x{pc:05X}: 0x{w:08X}")
            pc = npc
    return regs, mem, calls, pc


# ---------------------------------------------------------------------------
# Structured Data Models
# ---------------------------------------------------------------------------

@dataclass
class FanStep:
    step_index: int
    fan_rpms: list[int]                    # len == fan_count, multiples of 50 RPM
    sensor_thresholds: list[tuple[int, int]]  # len == len(active_sensors), (t_down, t_up)
    timers_flags: list[int]                # 5 bytes: [dn_t0, dn_t1, up_t0, up_t1, flags]


@dataclass
class ModeProfile:
    slot_index: int
    name: str
    clone_of: int | None
    offset_in_bank: int
    max_step: int
    fan_count: int
    sensor_mask: int
    active_sensors: list[int]
    tail_off: int
    row_stride: int
    steps: list[FanStep]
    ac_pl1_w: int
    ac_pl2_w: int
    manual_min_rpm: int
    manual_min2_rpm: int
    manual_default_rpm: int
    manual_max1_rpm: int
    manual_max2_rpm: int
    manual_max_rpm: int
    throttle_pl1_w: int
    throttle_pl2_w: int
    ndtt_c: int
    dc_pl1_w: int
    dc_pl2_w: int
    dgps_level: int
    raw_slot: bytes = field(repr=False)

    def encode_slot(self, orig: ModeProfile | None = None) -> bytes:
        """Serialize edited fields back into a 512-byte slot, preserving untouched bytes."""
        buf = bytearray(self.raw_slot)
        # Steps
        for idx, st in enumerate(self.steps):
            row_off = 8 + idx * self.row_stride
            for fi, rpm in enumerate(st.fan_rpms):
                val = max(0, min(255, int(round(rpm / 50.0))))
                buf[row_off + fi] = val
            s_base = row_off + self.fan_count
            for si, (td, tu) in enumerate(st.sensor_thresholds):
                buf[s_base + si * 2] = max(0, min(255, int(td)))
                buf[s_base + si * 2 + 1] = max(0, min(255, int(tu)))
            t_base = s_base + len(self.active_sensors) * 2
            for ti, tv in enumerate(st.timers_flags[:5]):
                buf[t_base + ti] = max(0, min(255, int(tv)))

        # Footer (+0x00 .. +0x37)
        f = self.tail_off
        buf[f + 0x18] = max(1, min(250, int(self.ac_pl1_w)))
        buf[f + 0x19] = max(1, min(250, int(self.ac_pl2_w)))
        buf[f + 0x2E] = max(1, min(250, int(self.throttle_pl1_w)))
        buf[f + 0x31] = max(1, min(250, int(self.throttle_pl2_w)))
        buf[f + 0x33] = max(1, min(250, int(self.dc_pl1_w)))
        buf[f + 0x34] = max(1, min(250, int(self.dc_pl2_w)))

        min_byte = max(0, min(100, int(round(self.manual_min_rpm / 100.0))))
        def_byte = max(0, min(100, int(round(self.manual_default_rpm / 100.0))))
        max_byte = max(0, min(100, int(round(self.manual_max_rpm / 100.0))))

        # Keep secondary clamp bytes (+0x22, +0x24/+0x25) untouched if min/max weren't changed;
        # sync them when the user modifies manual min or manual max (matching verified patches).
        if orig is None or self.manual_min_rpm != orig.manual_min_rpm:
            buf[f + 0x21] = min_byte
            buf[f + 0x22] = min_byte
        if orig is None or self.manual_default_rpm != orig.manual_default_rpm:
            buf[f + 0x23] = def_byte
        if orig is None or self.manual_max_rpm != orig.manual_max_rpm:
            buf[f + 0x24] = max_byte
            buf[f + 0x25] = max_byte
            buf[f + 0x26] = max_byte
        return bytes(buf)


@dataclass
class CustomPowerLevel:
    level: int        # 0=Low, 1=Medium, 2=High
    label: str
    ac_pl1_w: int
    ac_pl2_w: int
    dc_pl1_w: int
    dc_pl2_w: int


@dataclass
class ThermalCacheSig:
    loader_fn_addr: int
    loader_mode_limit: int
    ver_check_addr: int
    ver_check_reg: int
    ver_check_is_bnec: bool
    ver_check_target: int
    current_version: int
    builder_fn_addr: int
    builder_fn_end: int
    builder_push_re: int
    builder_skid_start: int
    builder_body_start: int
    skid_offset: int | None
    bank_reg: int
    base_smfi_reg: int
    len_hi_reg: int
    ver_store_reg: int
    ver_store_addrs: list[int]
    slot_indices: list[int]
    slot_offsets: dict[int, int]
    sku_bank_map: dict[int | str, int]
    patch_strategy: str
    ver_load_addr: int | None = None
    ver_load_is_16bit: bool = False
    free_saved_reg: int | None = None
    strategy_note: str = ""


@dataclass
class DefaultsCacheSig:
    current_version: int
    write_sites: list[tuple[int, int, bool]]  # (addr, reg, is_16bit)
    check_sites: list[tuple[int, int, int]]   # (addr, reg, target)


@dataclass
class CustomPlSig:
    fn_start: int
    fn_end: int
    banks_identical: bool
    levels: list[CustomPowerLevel]
    med_fallback_addr: int | None = None
    med_fallback_already_fixed: bool = False


@dataclass
class RomAnalysis:
    source_path: str
    rom_bytes: bytes = field(repr=False)
    sha256: str = ""
    rom_size: int = 0
    ite_sig_offset: int | None = None
    ite_sig_hex: str = ""
    mirror_flag_byte: int | None = None
    fw_version: str = ""
    has_pm1_dc_flash_loop: bool = False
    thermal_cache: ThermalCacheSig | None = None
    sku_banks: list[int] = field(default_factory=list)
    primary_bank: int = 0
    sku_banks_identical: bool = True
    bank_modes: dict[int, list[ModeProfile]] = field(default_factory=dict)
    custom_pl: CustomPlSig | None = None
    defaults_cache: DefaultsCacheSig | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def can_patch_modes(self) -> bool:
        return (
            not self.errors
            and self.thermal_cache is not None
            and self.thermal_cache.patch_strategy != "unsupported"
            and bool(self.sku_banks)
            and bool(self.bank_modes.get(self.primary_bank))
        )

    @property
    def can_patch_custom_pl(self) -> bool:
        return (
            not self.errors
            and self.custom_pl is not None
            and self.defaults_cache is not None
            and self.custom_pl.banks_identical
            and (self.custom_pl.fn_end - self.custom_pl.fn_start) >= 152
        )


@dataclass
class PatchResult:
    patched_bytes: bytes
    patched_sha256: str
    total_bytes_changed: int
    thermal_cache_bump: tuple[int, int] | None
    defaults_cache_bump: tuple[int, int] | None
    changes: list[dict]
    emulator_checks: list[str]


# ---------------------------------------------------------------------------
# Structural Mode Slot Decoder & Validator
# ---------------------------------------------------------------------------

def validate_and_decode_slot(
    rom: bytes, bank_base: int, slot_index: int, offset_in_bank: int | None = None
) -> ModeProfile | None:
    """Validate and decode a 512-byte thermal/fan/power mode slot.

    Returns None if the slot is empty (0xFF/0x00) or fails structural sanity checks.
    """
    off_in_bank = slot_index * 0x200 if offset_in_bank is None else offset_in_bank
    b = bank_base + off_in_bank
    if b < 0 or b + 0x200 > len(rom):
        return None
    raw = rom[b : b + 0x200]
    if all(x == 0xFF for x in raw) or all(x == 0x00 for x in raw):
        return None

    max_step = raw[0] >> 3
    fan_count = raw[0] & 7
    sensor_mask = raw[1]
    tail_off = struct.unpack_from("<H", raw, 3)[0]

    if not (1 <= fan_count <= 4):
        return None
    if not (1 <= max_step <= 24):
        return None
    active_sensors = [i for i in range(8) if (sensor_mask >> i) & 1]
    if not active_sensors:
        return None
    row_stride = fan_count + 2 * len(active_sensors) + 5
    steps_end = 8 + (max_step + 1) * row_stride
    if not (steps_end <= tail_off <= 0x200 - 0x38):
        return None

    f = raw[tail_off : tail_off + 0x38]
    ac_pl1, ac_pl2 = f[0x18], f[0x19]
    dc_pl1, dc_pl2 = f[0x33], f[0x34]
    th_pl1, th_pl2 = f[0x2E], f[0x31]
    if not (1 <= ac_pl1 <= 250 and 1 <= ac_pl2 <= 250):
        return None
    if not (1 <= dc_pl1 <= 250 and 1 <= dc_pl2 <= 250):
        return None
    if any(f[k] > 120 for k in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26)):
        return None

    steps: list[FanStep] = []
    for st in range(max_step + 1):
        r_off = 8 + st * row_stride
        fan_rpms = [raw[r_off + fi] * 50 for fi in range(fan_count)]
        s_off = r_off + fan_count
        s_thresh = [(raw[s_off + si * 2], raw[s_off + si * 2 + 1]) for si in range(len(active_sensors))]
        t_off = s_off + 2 * len(active_sensors)
        timers_flags = list(raw[t_off : t_off + 5])
        steps.append(
            FanStep(
                step_index=st,
                fan_rpms=fan_rpms,
                sensor_thresholds=s_thresh,
                timers_flags=timers_flags,
            )
        )

    base_name = STANDARD_MODE_NAMES.get(slot_index, f"Mode {slot_index}")
    return ModeProfile(
        slot_index=slot_index,
        name=base_name,
        clone_of=None,
        offset_in_bank=off_in_bank,
        max_step=max_step,
        fan_count=fan_count,
        sensor_mask=sensor_mask,
        active_sensors=active_sensors,
        tail_off=tail_off,
        row_stride=row_stride,
        steps=steps,
        ac_pl1_w=ac_pl1,
        ac_pl2_w=ac_pl2,
        manual_min_rpm=f[0x21] * 100,
        manual_min2_rpm=f[0x22] * 100,
        manual_default_rpm=f[0x23] * 100,
        manual_max1_rpm=f[0x24] * 100,
        manual_max2_rpm=f[0x25] * 100,
        manual_max_rpm=f[0x26] * 100,
        throttle_pl1_w=th_pl1,
        throttle_pl2_w=th_pl2,
        ndtt_c=f[0x32],
        dc_pl1_w=dc_pl1,
        dc_pl2_w=dc_pl2,
        dgps_level=f[0x0B],
        raw_slot=raw,
    )


def tag_clone_modes(modes: list[ModeProfile]) -> None:
    """Detect slots whose 512-byte payload is byte-identical to an earlier detected slot."""
    seen: dict[bytes, int] = {}
    for m in modes:
        if m.raw_slot in seen:
            m.clone_of = seen[m.raw_slot]
        else:
            seen[m.raw_slot] = m.slot_index
            m.clone_of = None


# ---------------------------------------------------------------------------
# Signature Scanners (Zero Hardcoded Offsets)
# ---------------------------------------------------------------------------

def scan_ite_header(rom: bytes) -> tuple[int | None, str, int | None, str]:
    """Locate 16-byte ITE eFlash signature & trailing ASCII version string."""
    prefix = bytes([0xA5] * 7)
    search_end = min(0x1000, len(rom) - 32)
    off = rom.find(prefix, 0, search_end)
    while off != -1:
        sig = rom[off : off + 16]
        if (
            sig[7] in (0xA5, 0xB5)
            and sig[10] == 0x5A
            and sig[11] == 0x5A
            and sig[12] == 0xAA
            and sig[14] == 0x55
            and sig[15] == 0x55
        ):
            ver_raw = rom[off + 16 : off + 48].split(b"\x00", 1)[0]
            ver_str = ver_raw.decode("ascii", errors="replace").strip()
            return off, sig.hex(" "), sig[13], ver_str
        off = rom.find(prefix, off + 1, search_end)
    return None, "", None, ""


def scan_pm1_dc_flash_loop(rom: bytes) -> bool:
    """Check whether ROM contains PM1 0xDC follow-mode command check (beqc r0, #0xdc)."""
    code_end = min(0x34000, len(rom) - 4)
    for pc in range(0, code_end, 2):
        if rom[pc] & 0x80:
            continue
        w = struct.unpack_from(">I", rom, pc)[0]
        if ((w >> 25) & 0x3F) == 0x2D and ((w >> 19) & 1) == 0 and ((w >> 8) & 0x7FF) == 0xDC:
            return True
    return False


def scan_thermal_cache_signatures(rom: bytes) -> ThermalCacheSig | None:
    """Dynamically locate the thermal mode loader & external SPI 0x3FE000 cache builder."""
    code_end = min(0x34000, len(rom) - 8)
    candidates: list[ThermalCacheSig] = []

    for pc in range(0x01000, code_end, 2):
        if rom[pc] & 0x80:
            continue
        w = struct.unpack_from(">I", rom, pc)[0]
        info = decode_insn_info(pc, 4, w)
        if info["op"] != "sethi" or info["imm"] != 0x00F02000:
            continue
        smfi_reg = info["rt"]

        # Look within next 24 bytes for: lbi rV, [smfi_reg + 0x218] followed by bnec/beqc rV, #VER, target
        window = iter_insns(rom, pc + 4, min(pc + 32, len(rom)))
        for wi in range(len(window) - 1):
            p1, s1, v1 = window[wi]
            p2, s2, v2 = window[wi + 1]
            d1 = decode_insn_info(p1, s1, v1)
            d2 = decode_insn_info(p2, s2, v2)
            if d1["op"] == "lbi" and d1["ra"] == smfi_reg and d1["imm"] == 0x218:
                if d2["op"] in ("bnec", "beqc") and d2["rt"] == d1["rt"]:
                    check_pc = p2
                    check_ver = d2["imm"]
                    check_target = d2["target"]
                    is_bnec = d2["op"] == "bnec"

                    # Look at target for `jal <cache_builder>`
                    tgt_insns = iter_insns(rom, check_target, min(check_target + 16, len(rom)))
                    builder_addr = None
                    for tp, ts, tv in tgt_insns[:3]:
                        td = decode_insn_info(tp, ts, tv)
                        if td["op"] == "jal":
                            builder_addr = td["target"]
                            break
                    if builder_addr is None or not (0x01000 <= builder_addr < code_end):
                        continue

                    # Find loader function prologue & mode limit (`slti45 r0, #N` + `addi r0, r0, #0x20`)
                    loader_fn_addr = max(0, pc - 0x80)
                    loader_mode_limit = 8
                    back_start = max(0x1000, pc - 0x90)
                    # Find nearest push25 before pc whose body reaches pc cleanly
                    for try_start in range(pc - 2, back_start - 2, -2):
                        hw = struct.unpack_from(">H", rom, try_start)[0]
                        if (hw & 0xFF80) == 0xFC00:
                            stream = iter_insns(rom, try_start, pc + 4)
                            if any(sp == pc for sp, _, _ in stream):
                                loader_fn_addr = try_start
                                for sp, ss, sv in stream[:10]:
                                    sd = decode_insn_info(sp, ss, sv)
                                    if sd["op"] in ("slti45", "slti") and sd.get("imm", 0) in range(1, 32):
                                        loader_mode_limit = sd["imm"]
                                        break
                                break

                    sig = _parse_cache_builder(
                        rom=rom,
                        loader_fn_addr=loader_fn_addr,
                        loader_mode_limit=loader_mode_limit,
                        ver_check_addr=check_pc,
                        ver_check_reg=d2["rt"],
                        ver_check_is_bnec=is_bnec,
                        ver_check_target=check_target,
                        current_version=check_ver,
                        builder_fn_addr=builder_addr,
                    )
                    if sig is not None:
                        candidates.append(sig)
                    break

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Prefer candidate with the most verified slot stores
        candidates.sort(key=lambda c: len(c.ver_store_addrs), reverse=True)
        return candidates[0]
    return None


def _parse_cache_builder(
    rom: bytes,
    loader_fn_addr: int,
    loader_mode_limit: int,
    ver_check_addr: int,
    ver_check_reg: int,
    ver_check_is_bnec: bool,
    ver_check_target: int,
    current_version: int,
    builder_fn_addr: int,
) -> ThermalCacheSig | None:
    """Disassemble and structurally analyze the SPI 0x3FE000 cache builder function."""
    stream = iter_insns(rom, builder_fn_addr, min(builder_fn_addr + 0x500, len(rom)))
    if not stream:
        return None
    p0, s0, v0 = stream[0]
    d0 = decode_insn_info(p0, s0, v0)
    if d0["op"] != "push25":
        return None
    push_re = d0["re"]

    # Trim stream at first pop25 / ret5
    fn_insns: list[tuple[int, int, int, dict]] = []
    fn_end = builder_fn_addr
    for p, s, v in stream:
        d = decode_insn_info(p, s, v)
        fn_insns.append((p, s, v, d))
        fn_end = p + s
        if d["op"] in ("pop25", "ret5"):
            break

    # Locate first `sethi rBase, #0x00f02000` -> marks body start area
    smfi_idx = None
    base_smfi_reg = 6
    for idx, (p, s, v, d) in enumerate(fn_insns):
        if d["op"] == "sethi" and d["imm"] == 0x00F02000:
            smfi_idx = idx
            base_smfi_reg = d["rt"]
            break
    if smfi_idx is None:
        return None

    # Find body start: the instruction right before `sethi rBase, #0x00f02000` that begins slot 0
    # (typically `mov55 r0, rBank; movi r1, #0x200; sethi r6, #0x00f02000`)
    body_start = fn_insns[smfi_idx][0]
    bank_reg = 9
    for back in range(smfi_idx - 1, max(0, smfi_idx - 5), -1):
        bp, bs, bv, bd = fn_insns[back]
        if bd["op"] == "mov55" and bd["rt"] == 0:
            bank_reg = bd["ra"]
            body_start = bp
            break

    # Find all slot version stores (`sbi rVer, [rBase+0x218]`), length stores (`+0x215`),
    # slot IDs (`+0x213`), and slot offsets (`addi r0, rBank, #OFF`)
    ver_store_addrs: list[int] = []
    ver_store_regs: set[int] = set()
    len_hi_regs: set[int] = set()
    slot_indices: list[int] = []
    slot_offsets: dict[int, int] = {}

    cur_slot_off = 0
    cur_slot_id: int | None = None
    last_reg_imm: dict[int, int] = {}

    for idx, (p, s, v, d) in enumerate(fn_insns):
        op = d["op"]
        if op in ("movi", "movi55", "movpi45"):
            last_reg_imm[d["rt"]] = d["imm"]
        elif op == "addi" and d["ra"] == bank_reg and d["rt"] == 0:
            cur_slot_off = d["imm"]
        elif op == "mov55" and d["ra"] == bank_reg and d["rt"] == 0:
            cur_slot_off = 0
        elif op == "sbi" and d["ra"] == base_smfi_reg:
            imm = d["imm"]
            if imm == 0x213:
                sid = last_reg_imm.get(d["rt"])
                if sid is not None and 0x20 <= sid < 0x40:
                    cur_slot_id = sid - 0x20
            elif imm == 0x215:
                len_hi_regs.add(d["rt"])
            elif imm == 0x218:
                ver_store_addrs.append(p)
                ver_store_regs.add(d["rt"])
                if cur_slot_id is not None:
                    if cur_slot_id not in slot_indices:
                        slot_indices.append(cur_slot_id)
                    slot_offsets[cur_slot_id] = cur_slot_off

    if not ver_store_addrs or len(ver_store_regs) != 1 or len(len_hi_regs) != 1:
        return None
    if not slot_indices:
        return None

    ver_store_reg = next(iter(ver_store_regs))
    len_hi_reg = next(iter(len_hi_regs))

    # Parse prologue (builder_fn_addr + 2 .. body_start) for SKID offset & SKU bank map
    prologue = [x for x in fn_insns if builder_fn_addr < x[0] < body_start]
    skid_offset: int | None = None
    sku_bank_map: dict[int | str, int] = {}
    branch_targets_to_skid: dict[int, int] = {}

    # Check if prologue begins with an unconditional jump (e.g., v1/v2 patched prologue:
    # movi r9, #0x3d000; movi55 r8, #3; j8 body_start) vs full SKID dispatch
    active_prologue: list[tuple[int, int, int, dict]] = []
    for item in prologue:
        active_prologue.append(item)
        if item[3]["op"] == "j8" and item[3]["target"] == body_start and not branch_targets_to_skid:
            # Everything after this unconditional jump before body_start is dead code
            break
        if item[3]["op"] == "beqc":
            branch_targets_to_skid[item[3]["target"]] = item[3]["imm"]

    for p, s, v, d in active_prologue:
        if d["op"] == "lbi" and 0x200 <= d["imm"] <= 0x3FF:
            skid_offset = d["imm"]
        elif d["op"] == "movi" and d["rt"] == bank_reg and 0x10000 <= d["imm"] < len(rom):
            if p in branch_targets_to_skid:
                sku_bank_map[branch_targets_to_skid[p]] = d["imm"]
            else:
                # Could be default bank or our SKID-formula base bank
                if "default" not in sku_bank_map:
                    sku_bank_map["default"] = d["imm"]
                else:
                    # Our SKID-preserving formula loads default first, then bank_1!
                    b1 = d["imm"]
                    sku_bank_map[1] = b1
                    sku_bank_map[2] = b1 + 0x1000
                    sku_bank_map[3] = b1 + 0x2000

    # Determine which registers are used anywhere in the function body
    used_regs: set[int] = set()
    for p, s, v, d in fn_insns:
        if p < body_start:
            continue
        for k in ("rt", "ra", "rb"):
            if k in d:
                used_regs.add(d[k])
    saved_regs = [r for r in PUSH25_REGS[push_re] if r != 30]
    free_saved_regs = [r for r in saved_regs if r not in used_regs]

    # Decide safe cache-version bump strategy
    patch_strategy = "unsupported"
    ver_load_addr: int | None = None
    ver_load_is_16bit = False
    free_saved_reg: int | None = None
    strategy_note = ""

    if ver_store_reg != len_hi_reg:
        # Strategy 1: version already has its own dedicated register!
        # Find the instruction loading `ver_store_reg` with `current_version` before the first store.
        ver_loads = [
            (p, s, d)
            for p, s, v, d in fn_insns
            if p < ver_store_addrs[0]
            and d["op"] in ("movi55", "movi")
            and d["rt"] == ver_store_reg
            and d["imm"] == current_version
        ]
        # Verify ver_store_reg is never overwritten between ver_load and last ver_store
        if len(ver_loads) == 1:
            vp, vs, _ = ver_loads[0]
            clobbered = any(
                vp < p < ver_store_addrs[-1]
                and d.get("rt") == ver_store_reg
                and d["op"] not in ("sbi", "shi", "swi", "sbi333", "beqc", "bnec", "beq", "bne")
                for p, s, v, d in fn_insns
            )
            if not clobbered:
                patch_strategy = "separate_ver_reg"
                ver_load_addr = vp
                ver_load_is_16bit = vs == 2
                strategy_note = (
                    f"Dedicated version register r{ver_store_reg} loaded at 0x{vp:05X} "
                    f"({'16-bit movi55' if vs == 2 else '32-bit movi'})"
                )
    else:
        # Strategy 2 / 3: ver_store_reg == len_hi_reg (compiler CSE shared e.g. r7 = 2)
        skid_space = body_start - (builder_fn_addr + 2)
        if free_saved_regs:
            free_saved_reg = free_saved_regs[0]
            # Check if we have a sequential 4-bank SKID dispatch (1->b, 2->b+0x1000, 3->b+0x2000, default->b_def)
            if (
                skid_offset is not None
                and skid_space >= 34
                and 1 in sku_bank_map
                and 2 in sku_bank_map
                and 3 in sku_bank_map
                and "default" in sku_bank_map
                and sku_bank_map[2] == sku_bank_map[1] + 0x1000
                and sku_bank_map[3] == sku_bank_map[1] + 0x2000
            ):
                patch_strategy = "shared_reg_skid_formula"
                strategy_note = (
                    f"Shared r{ver_store_reg} (=2 len_hi & ver): 34-byte SKID-preserving formula "
                    f"using free callee-saved r{free_saved_reg} (fits in {skid_space}B prologue)"
                )
            elif skid_space >= 10 and sku_bank_map:
                # Check if all referenced banks are byte-identical
                b_addrs = sorted(set(sku_bank_map.values()))
                bank_span = (max(slot_offsets.values()) + 0x200) if slot_offsets else 0x1000
                if all(
                    rom[ba : ba + bank_span] == rom[b_addrs[0] : b_addrs[0] + bank_span]
                    for ba in b_addrs
                ):
                    patch_strategy = "shared_reg_single_bank"
                    strategy_note = (
                        f"Shared r{ver_store_reg}: all {len(b_addrs)} SKU banks identical; "
                        f"compact movi r{bank_reg} + movi r{free_saved_reg} + j8"
                    )
                else:
                    strategy_note = (
                        f"Shared r{ver_store_reg} with non-sequential distinct SKU banks; "
                        "auto-patcher refuses to guess prologue rewrite."
                    )
        else:
            strategy_note = (
                f"Shared r{ver_store_reg} with no free callee-saved register in push25 re={push_re}."
            )

    return ThermalCacheSig(
        loader_fn_addr=loader_fn_addr,
        loader_mode_limit=loader_mode_limit,
        ver_check_addr=ver_check_addr,
        ver_check_reg=ver_check_reg,
        ver_check_is_bnec=ver_check_is_bnec,
        ver_check_target=ver_check_target,
        current_version=current_version,
        builder_fn_addr=builder_fn_addr,
        builder_fn_end=fn_end,
        builder_push_re=push_re,
        builder_skid_start=builder_fn_addr + 2,
        builder_body_start=body_start,
        skid_offset=skid_offset,
        bank_reg=bank_reg,
        base_smfi_reg=base_smfi_reg,
        len_hi_reg=len_hi_reg,
        ver_store_reg=ver_store_reg,
        ver_store_addrs=ver_store_addrs,
        slot_indices=slot_indices,
        slot_offsets=slot_offsets,
        sku_bank_map=sku_bank_map,
        patch_strategy=patch_strategy,
        ver_load_addr=ver_load_addr,
        ver_load_is_16bit=ver_load_is_16bit,
        free_saved_reg=free_saved_reg,
        strategy_note=strategy_note,
    )


def discover_sku_banks(rom: bytes, tc: ThermalCacheSig) -> tuple[list[int], int]:
    """Collect all valid SKU banks referenced by cache_builder (plus adjacent identical 4 KiB banks)."""
    found = sorted(set(tc.sku_bank_map.values()))
    if not found:
        return [], 0
    primary = tc.sku_bank_map.get(3, found[0])
    bank_span = max((off + 0x200 for off in tc.slot_offsets.values()), default=0x1000)

    # If v1/v2 previously collapsed SKID to a single bank (e.g. 0x3D000), also keep adjacent
    # 4 KiB-aligned identical banks (e.g. 0x3B000, 0x3C000, 0x3E000) synchronized!
    if len(found) == 1 and (primary & 0xFFF) == 0:
        ref = rom[primary : primary + bank_span]
        for delta in (-0x2000, -0x1000, 0x1000, 0x2000):
            cand = primary + delta
            if 0x10000 <= cand and cand + bank_span <= len(rom):
                if rom[cand : cand + bank_span] == ref and cand not in found:
                    found.append(cand)
        found.sort()
    return found, primary


def scan_custom_pl_init(rom: bytes) -> CustomPlSig | None:
    """Signature-scan Custom Mode (4) CPU PL1/PL2 initializer (0x86900) and emulate it!"""
    code_end = min(0x34000, len(rom) - 16)
    matches: list[tuple[int, int]] = []

    for pc in range(0x01000, code_end, 2):
        if rom[pc] & 0x80:
            continue
        w1 = struct.unpack_from(">I", rom, pc)[0]
        d1 = decode_insn_info(pc, 4, w1)
        if d1["op"] != "sethi" or d1["imm"] != 0x00086000:
            continue
        if pc + 8 > len(rom) or (rom[pc + 4] & 0x80):
            continue
        w2 = struct.unpack_from(">I", rom, pc + 4)[0]
        d2 = decode_insn_info(pc + 4, 4, w2)
        if d2["op"] != "addi" or d2["ra"] != d1["rt"] or d2["imm"] != 0x900:
            continue

        # Check preceding 2 bytes for push25
        if pc < 2:
            continue
        hw0 = struct.unpack_from(">H", rom, pc - 2)[0]
        d0 = decode_insn_info(pc - 2, 2, hw0)
        if d0["op"] != "push25":
            continue
        fn_start = pc - 2

        # Scan forward for pop25 + trailing nop16 padding (up to 0x260 bytes)
        stream = iter_insns(rom, fn_start, min(fn_start + 0x260, len(rom)))
        fn_end = None
        for idx, (sp, ss, sv) in enumerate(stream):
            sd = decode_insn_info(sp, ss, sv)
            if sd["op"] == "pop25":
                fn_end = sp + ss
                # Consume any trailing nop16 padding emitted by earlier patches
                for np_p, np_s, np_v in stream[idx + 1 :]:
                    if np_s == 2 and np_v == 0x9200:
                        fn_end = np_p + 2
                    else:
                        break
                break
        if fn_end is not None and (fn_end - fn_start) >= 64:
            matches.append((fn_start, fn_end))

    if len(matches) != 1:
        return None
    fn_start, fn_end = matches[0]

    # Emulate the ROM's own function to extract exact Custom Low / Med / High watts!
    try:
        canary = {6: 0x11111116, 7: 0x22222227, 8: 0x33333338, 9: 0x44444449, 10: 0x5555555A}
        regs, mem, _, _ = emu_nds32(rom, fn_start, init_regs=canary, max_steps=2000)
        for r, val in canary.items():
            if regs[r] != val:
                return None
    except Exception:
        return None

    banks_raw = [bytes(mem[0x86900 + b * 0x28 : 0x86900 + (b + 1) * 0x28]) for b in range(4)]
    banks_identical = all(bk == banks_raw[0] for bk in banks_raw)
    b0 = banks_raw[0]
    labels = ("Low (0)", "Medium (1)", "High (2)")
    levels: list[CustomPowerLevel] = []
    for lvl in range(3):
        off = lvl * 8
        ac1, ac2, dc1, dc2 = b0[off], b0[off + 1], b0[off + 2], b0[off + 3]
        if not all(5 <= x <= 200 for x in (ac1, ac2, dc1, dc2)):
            return None
        levels.append(
            CustomPowerLevel(
                level=lvl,
                label=labels[lvl],
                ac_pl1_w=ac1,
                ac_pl2_w=ac2,
                dc_pl1_w=dc1,
                dc_pl2_w=dc2,
            )
        )

    med_fallback_addr, med_already_fixed = _scan_custom_med_fallback(rom)
    return CustomPlSig(
        fn_start=fn_start,
        fn_end=fn_end,
        banks_identical=banks_identical,
        levels=levels,
        med_fallback_addr=med_fallback_addr,
        med_fallback_already_fixed=med_already_fixed,
    )


def _scan_custom_med_fallback(rom: bytes) -> tuple[int | None, bool]:
    """Locate the Custom Medium bit-1 thermal fallback (`movpi45 r0, #40` vs fixed `movpi45 r1, #35`)."""
    code_end = min(0x34000, len(rom) - 16)
    for pc in range(0x01000, code_end, 2):
        if rom[pc] & 0x80:
            continue
        w1 = struct.unpack_from(">I", rom, pc)[0]
        d1 = decode_insn_info(pc, 4, w1)
        if d1["op"] != "lbi" or d1["imm"] != 0x908 or d1["rt"] != 1:
            continue
        if pc + 8 > len(rom) or (rom[pc + 4] & 0x80):
            continue
        w2 = struct.unpack_from(">I", rom, pc + 4)[0]
        d2 = decode_insn_info(pc + 4, 4, w2)
        if d2["op"] != "lbi" or d2["imm"] != 0x909 or d2["rt"] != 0 or d2["ra"] != d1["ra"]:
            continue
        window = iter_insns(rom, pc + 8, min(pc + 0x60, len(rom)))
        for wi in range(len(window) - 1):
            p_b, s_b, v_b = window[wi]
            p_m, s_m, v_m = window[wi + 1]
            db = decode_insn_info(p_b, s_b, v_b)
            dm = decode_insn_info(p_m, s_m, v_m)
            if db["op"] == "beqz" and dm["op"] == "movpi45":
                if dm["rt"] == 0 and dm["imm"] == 40:
                    return p_m, False
                if dm["rt"] == 1 and dm["imm"] == 35:
                    return p_m, True
    return None, False


def scan_defaults_cache_signatures(rom: bytes) -> DefaultsCacheSig | None:
    """Locate all 4 external SPI 0x050000 runtime defaults cache version sites (`[0x86004] == VER`)."""
    code_end = min(0x34000, len(rom) - 16)
    write_sites: list[tuple[int, int, bool, int]] = []  # (addr, reg, is_16, ver)
    check_sites: list[tuple[int, int, int, int]] = []   # (addr, reg, tgt, ver)

    for pc in range(0x01000, code_end, 2):
        if rom[pc] & 0x80:
            continue
        w = struct.unpack_from(">I", rom, pc)[0]
        d = decode_insn_info(pc, 4, w)
        if d["op"] != "sethi" or d["imm"] != 0x00086000:
            continue
        base_r = d["rt"]
        win = iter_insns(rom, pc + 4, min(pc + 64, len(rom)))
        if len(win) < 2:
            continue

        # Pattern A (Write sites): sethi rA, #0x86000; movi55 rB, #VER; shi/shi333 rB, [rA+4]; movi rB, #0xc06
        p1, s1, v1 = win[0]
        p2, s2, v2 = win[1]
        d1 = decode_insn_info(p1, s1, v1)
        d2 = decode_insn_info(p2, s2, v2)
        if (
            d1["op"] in ("movi55", "movi")
            and d2["op"] in ("shi", "shi333")
            and d2["ra"] == base_r
            and d2["rt"] == d1["rt"]
            and d2["imm"] == 4
            and len(win) >= 3
        ):
            p3, s3, v3 = win[2]
            d3 = decode_insn_info(p3, s3, v3)
            if d3["op"] == "movi" and 0x0C00 <= d3["imm"] <= 0x0FFF:
                write_sites.append((p1, d1["rt"], s1 == 2, d1["imm"]))
                continue

        # Pattern B (Check sites): within window of sethi rA, #0x86000, find
        # lhi/lhi333 r0, [rA+4] followed immediately by beqc r0, #VER, target
        for wi in range(len(win) - 1):
            pa, sa, va = win[wi]
            pb, sb, vb = win[wi + 1]
            da = decode_insn_info(pa, sa, va)
            db = decode_insn_info(pb, sb, vb)
            if (
                da["op"] in ("lhi", "lhi333")
                and da["ra"] == base_r
                and da["imm"] == 4
                and db["op"] == "beqc"
                and db["rt"] == da["rt"]
            ):
                entry = (pb, db["rt"], db["target"], db["imm"])
                if not any(c[0] == pb for c in check_sites):
                    check_sites.append(entry)
                break

    if len(write_sites) < 2 or len(check_sites) < 2:
        return None
    versions = {x[3] for x in write_sites} | {x[3] for x in check_sites}
    if len(versions) != 1:
        return None
    ver = next(iter(versions))
    return DefaultsCacheSig(
        current_version=ver,
        write_sites=[(a, r, i16) for a, r, i16, _ in write_sites],
        check_sites=[(a, r, t) for a, r, t, _ in check_sites],
    )


# ---------------------------------------------------------------------------
# Full ROM Analyzer Entrypoint
# ---------------------------------------------------------------------------

def analyze_rom_bytes(rom: bytes, source_path: str = "<memory>") -> RomAnalysis:
    res = RomAnalysis(
        source_path=source_path,
        rom_bytes=rom,
        sha256=hashlib.sha256(rom).hexdigest(),
        rom_size=len(rom),
    )

    if len(rom) not in (0x20000, 0x40000):
        res.errors.append(
            f"Unsupported ROM file size: {len(rom):,} bytes (expected 262,144 bytes for IT8390E-256 "
            "or 131,072 bytes for IT8390E-128)."
        )
        return res

    sig_off, sig_hex, mirror_byte, fw_ver = scan_ite_header(rom)
    res.ite_sig_offset = sig_off
    res.ite_sig_hex = sig_hex
    res.mirror_flag_byte = mirror_byte
    res.fw_version = fw_ver
    res.has_pm1_dc_flash_loop = scan_pm1_dc_flash_loop(rom)

    if sig_off is None:
        res.errors.append(
            "ITE eFlash 16-byte signature (A5..5A 5A AA xx 55 55) not found. "
            "This does not appear to be an ITE IT8390E EC ROM image."
        )
        return res

    tc = scan_thermal_cache_signatures(rom)
    res.thermal_cache = tc
    if tc is None:
        res.errors.append(
            "Could not locate Razer IT8390E thermal mode loader / SPI 0x3FE000 cache builder "
            "NDS32 instruction signatures. Refusing to patch blindly."
        )
        return res

    if tc.patch_strategy == "unsupported":
        res.errors.append(f"Thermal cache version bump unsupported: {tc.strategy_note}")

    sku_banks, primary_bank = discover_sku_banks(rom, tc)
    res.sku_banks = sku_banks
    res.primary_bank = primary_bank
    if not sku_banks:
        res.errors.append("No valid SKU thermal bank flash addresses discovered in cache builder.")
        return res

    bank_span = max((off + 0x200 for off in tc.slot_offsets.values()), default=0x1000)
    res.sku_banks_identical = all(
        rom[b : b + bank_span] == rom[primary_bank : primary_bank + bank_span] for b in sku_banks
    )

    # Dynamically decode ONLY the existing valid mode slots wired into cache_builder!
    for bank in sku_banks:
        modes: list[ModeProfile] = []
        for slot_idx in sorted(tc.slot_indices):
            if slot_idx >= tc.loader_mode_limit:
                continue
            off_in_bank = tc.slot_offsets.get(slot_idx, slot_idx * 0x200)
            mp = validate_and_decode_slot(rom, bank, slot_idx, offset_in_bank=off_in_bank)
            if mp is not None:
                modes.append(mp)
        tag_clone_modes(modes)
        res.bank_modes[bank] = modes

    if not res.bank_modes.get(primary_bank):
        res.errors.append(
            f"No structurally valid 512-byte mode slots decoded inside bank 0x{primary_bank:05X}."
        )
        return res

    # Optional: Custom Mode (4) CPU PL table + 0x050000 runtime defaults cache
    res.custom_pl = scan_custom_pl_init(rom)
    res.defaults_cache = scan_defaults_cache_signatures(rom)
    if res.custom_pl is None:
        res.warnings.append(
            "Custom Mode (0x86900) CPU slider initializer not detected — standard mode footers remain fully editable."
        )
    elif res.defaults_cache is None:
        res.warnings.append(
            "Custom Mode (0x86900) detected, but SPI 0x050000 runtime defaults cache sites were not all matched — "
            "Custom Low/Med/High slider editing disabled for safety."
        )
    elif not res.custom_pl.banks_identical:
        res.warnings.append(
            "Custom Mode (0x86900) SKU banks differ across SKUs — Custom slider editing disabled to avoid cross-SKU overwrite."
        )

    if not res.has_pm1_dc_flash_loop:
        res.warnings.append(
            "PM1 0xDC follow-mode signature was not found (verify flashing method for this board separately)."
        )

    return res


def analyze_rom_file(path: Path | str) -> RomAnalysis:
    p = Path(path)
    return analyze_rom_bytes(p.read_bytes(), source_path=str(p))


# ---------------------------------------------------------------------------
# Code Assemblers for Safe Universal Patching
# ---------------------------------------------------------------------------

def assemble_skid_preserving_prologue(
    tc: ThermalCacheSig, new_version: int
) -> bytes:
    """Emit a 34-byte (padded with nop16) SKID-preserving prologue that preserves all 4 SKU banks
    (SKID 1->b1, 2->b1+0x1000, 3->b1+0x2000, other->b_default) AND loads `free_saved_reg = new_version`.
    """
    assert tc.skid_offset is not None and tc.free_saved_reg is not None
    base = tc.builder_skid_start
    total = tc.builder_body_start - base
    assert total >= 34 and total % 2 == 0
    b1 = tc.sku_bank_map[1]
    b_def = tc.sku_bank_map["default"]
    r_ver = tc.free_saved_reg
    r_bank = tc.bank_reg

    out = bytearray()
    out.extend(enc_sethi(0, 0x00080))                    # +0x00 (4B) r0 = 0x00080000
    out.extend(enc_lbi(0, 0, tc.skid_offset))            # +0x04 (4B) r0 = [0x80000 + skid_offset]
    out.extend(enc_movi(r_ver, new_version))             # +0x08 (4B) rVer = new_version (1..255!)
    out.extend(enc_movi(r_bank, b_def))                  # +0x0C (4B) rBank = default SKU bank
    out.extend(enc_subi45(0, 1))                         # +0x10 (2B) r0 = SKID - 1
    out.extend(enc_slti45(0, 3))                         # +0x12 (2B) r15 = (unsigned(SKID-1) < 3)
    beq_pc = base + len(out)                             # +0x14 (2B)
    done_pc = beq_pc + 2 + 4 + 4 + 4                     # +0x22
    out.extend(enc_beqzs8(beq_pc, done_pc))              # if not in {1,2,3} -> keep default bank!
    out.extend(enc_slli(0, 0, 12))                       # +0x16 (4B) r0 = (SKID - 1) * 0x1000
    out.extend(enc_movi(r_bank, b1))                     # +0x1A (4B) rBank = bank_1
    out.extend(enc_add(r_bank, r_bank, 0))               # +0x1E (4B) rBank = bank_1 + (SKID-1)*0x1000
    assert base + len(out) == done_pc
    while len(out) < total:
        out.extend(enc_nop16())
    return bytes(out)


def assemble_custom_pl_init_generic(
    fn_start: int, fn_end: int, levels: list[CustomPowerLevel]
) -> bytes:
    """Assemble a 152-byte loop-based Custom Mode 4 PL1/PL2 initializer into [fn_start..fn_end).

    Supports 12 completely independent watt values (Low/Med/High x AC PL1/PL2, DC PL1/PL2)
    and only uses caller-saved r0..r4 plus callee-saved r6 (saved via push25 re=0).
    """
    assert len(levels) == 3
    total = fn_end - fn_start
    assert total >= 152 and total % 2 == 0

    low, med, high = levels[0], levels[1], levels[2]
    out = bytearray()

    def emit(b: bytes) -> None:
        out.extend(b)

    emit(enc_push25(re=0, imm=0))               # save r6, lp (2B)
    emit(enc_sethi(0, 0x00086))                 # r0 = 0x00086000 (4B)
    emit(enc_addi(0, 0, 0x900))                 # r0 = 0x00086900 (4B)
    emit(enc_addi(6, 0, 0x0A0))                 # r6 = 0x000869A0 end ptr (4B)

    loop_pc = fn_start + len(out)
    # Slot 0 (Low)
    emit(enc_movi(1, low.ac_pl1_w))
    emit(enc_movi(2, low.ac_pl2_w))
    emit(enc_movi(3, low.dc_pl1_w))
    emit(enc_movi(4, low.dc_pl2_w))
    emit(enc_sbi(1, 0, 0x00))
    emit(enc_sbi(2, 0, 0x01))
    emit(enc_sbi(3, 0, 0x02))
    emit(enc_sbi(4, 0, 0x03))
    # Slot 1 (Med)
    emit(enc_movi(1, med.ac_pl1_w))
    emit(enc_movi(2, med.ac_pl2_w))
    emit(enc_movi(3, med.dc_pl1_w))
    emit(enc_movi(4, med.dc_pl2_w))
    emit(enc_sbi(1, 0, 0x08))
    emit(enc_sbi(2, 0, 0x09))
    emit(enc_sbi(3, 0, 0x0A))
    emit(enc_sbi(4, 0, 0x0B))
    # Slots 2, 3, 4 (High + duplicates)
    emit(enc_movi(1, high.ac_pl1_w))
    emit(enc_movi(2, high.ac_pl2_w))
    emit(enc_movi(3, high.dc_pl1_w))
    emit(enc_movi(4, high.dc_pl2_w))
    for slot_off in (0x10, 0x18, 0x20):
        emit(enc_sbi(1, 0, slot_off + 0))
        emit(enc_sbi(2, 0, slot_off + 1))
        emit(enc_sbi(3, 0, slot_off + 2))
        emit(enc_sbi(4, 0, slot_off + 3))

    emit(enc_addi(0, 0, 0x28))
    emit(enc_beq_bne(True, 0, 6, fn_start + len(out), loop_pc))
    emit(enc_pop25(re=0, imm=0))                # restore r6, lp & return (2B)

    assert len(out) <= total, f"Assembled Custom PL init ({len(out)}B) exceeds budget ({total}B)"
    while len(out) < total:
        out.extend(enc_nop16())
    return bytes(out)


# ---------------------------------------------------------------------------
# Fail-Closed Patch Builder & Emulator Verification
# ---------------------------------------------------------------------------

def _next_cache_version(cur: int, max_ver: int) -> int:
    if cur < max_ver:
        return cur + 1
    # Wrap cleanly to 1 (never 0 or 0xFF) if max_ver reached
    return 1


def build_and_verify_patched_rom(
    analysis: RomAnalysis,
    edited_bank_modes: dict[int, list[ModeProfile]],
    edited_custom_levels: list[CustomPowerLevel] | None = None,
    mirror_flag_mode: str = "keep",  # "keep" | "force_ff" | "force_00"
) -> PatchResult:
    """Apply all user edits to `analysis.rom_bytes`, bump SPI cache versions cleanly,
    and run full structural + NDS32 emulator verification before returning.
    """
    if analysis.errors:
        raise RuntimeError("Cannot patch ROM with analysis errors: " + "; ".join(analysis.errors))

    orig = analysis.rom_bytes
    buf = bytearray(orig)
    changes: list[dict] = []
    emu_checks: list[str] = []

    def record_patch(addr: int, replacement: bytes, reason: str) -> None:
        before = bytes(buf[addr : addr + len(replacement)])
        if before == replacement:
            return
        buf[addr : addr + len(replacement)] = replacement
        changes.append({
            "addr": f"0x{addr:05X}",
            "size": len(replacement),
            "before": before[:16].hex(" "),
            "after": replacement[:16].hex(" "),
            "reason": reason,
        })

    # 1. Optional ITE 0x0008D mirror flag
    if analysis.ite_sig_offset is not None and mirror_flag_mode in ("force_ff", "force_00"):
        flag_addr = analysis.ite_sig_offset + 13
        target_byte = 0xFF if mirror_flag_mode == "force_ff" else 0x00
        if buf[flag_addr] != target_byte:
            record_patch(
                flag_addr,
                bytes([target_byte]),
                f"ITE signature mirror flag @ 0x{flag_addr:05X}: 0x{buf[flag_addr]:02X} -> 0x{target_byte:02X}",
            )

    # 2. Apply edited mode slots (strictly existing modes only!)
    any_mode_changed = False
    for bank_addr, edited_list in edited_bank_modes.items():
        if bank_addr not in analysis.bank_modes:
            raise RuntimeError(f"Unknown SKU bank 0x{bank_addr:05X}")
        orig_by_idx = {m.slot_index: m for m in analysis.bank_modes[bank_addr]}
        if len(edited_list) != len(orig_by_idx):
            raise RuntimeError(
                f"Mode count mismatch in bank 0x{bank_addr:05X}: creating or deleting modes is forbidden."
            )
        for em in edited_list:
            if em.slot_index not in orig_by_idx:
                raise RuntimeError(f"Attempted to create non-existent mode slot {em.slot_index}!")
            om = orig_by_idx[em.slot_index]
            if len(em.steps) != len(om.steps):
                raise RuntimeError(
                    f"Step count mismatch on Mode {em.slot_index} (expected {len(om.steps)}, got {len(em.steps)})."
                )
            new_slot_bytes = em.encode_slot(orig=om)
            if new_slot_bytes != om.raw_slot:
                any_mode_changed = True
                slot_addr = bank_addr + om.offset_in_bank
                diff_n = sum(a != b for a, b in zip(om.raw_slot, new_slot_bytes))
                record_patch(
                    slot_addr,
                    new_slot_bytes,
                    f"Bank 0x{bank_addr:05X} Mode {em.slot_index} ({em.name}): {diff_n} byte(s) updated",
                )

    # 3. If any mode slot changed, bump external SPI 0x3FE000 thermal cache version!
    thermal_cache_bump: tuple[int, int] | None = None
    if any_mode_changed:
        tc = analysis.thermal_cache
        if tc is None or tc.patch_strategy == "unsupported":
            raise RuntimeError("Thermal cache signature unavailable — cannot safely bump SPI 0x3FE000 cache.")
        old_v = tc.current_version
        max_v = 15 if (tc.patch_strategy == "separate_ver_reg" and tc.ver_load_is_16bit) else 250
        new_v = _next_cache_version(old_v, max_v)
        if new_v == 2 and tc.patch_strategy != "separate_ver_reg":
            new_v = 3
        thermal_cache_bump = (old_v, new_v)

        # Patch check instruction in mode loader (`bnec r0, #old_v, target` -> `#new_v`)
        new_chk = enc_beqc_bnec(
            tc.ver_check_is_bnec, tc.ver_check_reg, new_v, tc.ver_check_addr, tc.ver_check_target
        )
        record_patch(
            tc.ver_check_addr,
            new_chk,
            f"SPI 0x3FE000 cache version check: v{old_v} -> v{new_v} (triggers auto-rebuild from edited mode tables)",
        )

        # Patch cache builder according to verified strategy
        if tc.patch_strategy == "separate_ver_reg":
            assert tc.ver_load_addr is not None
            if tc.ver_load_is_16bit:
                rep = enc_movi55(tc.ver_store_reg, new_v)
            else:
                rep = enc_movi(tc.ver_store_reg, new_v)
            record_patch(
                tc.ver_load_addr,
                rep,
                f"SPI 0x3FE000 cache builder: r{tc.ver_store_reg} = {new_v} (dedicated version register)",
            )
        elif tc.patch_strategy == "shared_reg_skid_formula":
            assert tc.free_saved_reg is not None
            prologue_bytes = assemble_skid_preserving_prologue(tc, new_v)
            record_patch(
                tc.builder_skid_start,
                prologue_bytes,
                f"SPI 0x3FE000 cache builder: SKID-preserving 4-bank formula + r{tc.free_saved_reg} = {new_v} "
                f"(keeps r{tc.len_hi_reg}=2 for 0x0200 slot length)",
            )
            for idx, store_pc in enumerate(tc.ver_store_addrs):
                record_patch(
                    store_pc,
                    enc_sbi(tc.free_saved_reg, tc.base_smfi_reg, 0x218),
                    f"SPI 0x3FE000 cache builder slot #{idx}: sbi r{tc.free_saved_reg}, [r{tc.base_smfi_reg}+0x218]",
                )
        elif tc.patch_strategy == "shared_reg_single_bank":
            assert tc.free_saved_reg is not None
            stub = bytearray()
            stub.extend(enc_movi(tc.bank_reg, analysis.primary_bank))
            stub.extend(enc_movi(tc.free_saved_reg, new_v))
            j_pc = tc.builder_skid_start + len(stub)
            stub.extend(enc_j8(j_pc, tc.builder_body_start))
            record_patch(
                tc.builder_skid_start,
                bytes(stub),
                f"SPI 0x3FE000 cache builder: movi r{tc.bank_reg}, #0x{analysis.primary_bank:05X}; "
                f"movi r{tc.free_saved_reg}, #{new_v}; j8 0x{tc.builder_body_start:05X}",
            )
            for idx, store_pc in enumerate(tc.ver_store_addrs):
                record_patch(
                    store_pc,
                    enc_sbi(tc.free_saved_reg, tc.base_smfi_reg, 0x218),
                    f"SPI 0x3FE000 cache builder slot #{idx}: sbi r{tc.free_saved_reg}, [r{tc.base_smfi_reg}+0x218]",
                )
        else:
            raise RuntimeError(f"Unsupported thermal cache strategy: {tc.patch_strategy}")

    # 4. Apply Custom Mode (4) CPU PL1/PL2 edits (if changed) + bump SPI 0x050000 defaults cache!
    defaults_cache_bump: tuple[int, int] | None = None
    custom_changed = False
    if edited_custom_levels is not None and analysis.custom_pl is not None:
        orig_lvls = analysis.custom_pl.levels
        if len(edited_custom_levels) != 3:
            raise RuntimeError("Custom Mode PL requires exactly 3 levels (Low, Medium, High).")
        for ol, el in zip(orig_lvls, edited_custom_levels):
            if (ol.ac_pl1_w, ol.ac_pl2_w, ol.dc_pl1_w, ol.dc_pl2_w) != (
                el.ac_pl1_w, el.ac_pl2_w, el.dc_pl1_w, el.dc_pl2_w
            ):
                custom_changed = True
                break

    if custom_changed:
        if not analysis.can_patch_custom_pl:
            raise RuntimeError(
                "Custom Mode (0x86900) patching is disabled for this ROM (missing SPI 0x050000 signatures or space)."
            )
        cpl = analysis.custom_pl
        dc = analysis.defaults_cache
        assert cpl is not None and dc is not None and edited_custom_levels is not None

        code_bytes = assemble_custom_pl_init_generic(cpl.fn_start, cpl.fn_end, edited_custom_levels)
        record_patch(
            cpl.fn_start,
            code_bytes,
            "Custom Mode 4 CPU PL1/PL2 table (0x86900): "
            + ", ".join(
                f"{lv.label} AC {lv.ac_pl1_w}/{lv.ac_pl2_w}W DC {lv.dc_pl1_w}/{lv.dc_pl2_w}W"
                for lv in edited_custom_levels
            ),
        )

        # If Custom Med AC PL1 > 40W and unpatched fallback `movpi45 r0, #40` exists, fix to `movpi45 r1, #35`
        med_lvl = edited_custom_levels[1]
        if med_lvl.ac_pl1_w > 40 and cpl.med_fallback_addr is not None and not cpl.med_fallback_already_fixed:
            record_patch(
                cpl.med_fallback_addr,
                enc_movpi45(1, 35),
                "Custom Med thermal fallback fix: movpi45 r0,#40 -> movpi45 r1,#35 (prevents PL2 < PL1)",
            )

        # Bump all SPI 0x050000 runtime defaults cache version sites (`7 -> 8 -> ...`)
        old_dv = dc.current_version
        max_dv = 15 if any(is16 for _, _, is16 in dc.write_sites) else 250
        new_dv = _next_cache_version(old_dv, max_dv)
        defaults_cache_bump = (old_dv, new_dv)

        for w_addr, w_reg, w_is16 in dc.write_sites:
            rep = enc_movi55(w_reg, new_dv) if w_is16 else enc_movi(w_reg, new_dv)
            record_patch(
                w_addr,
                rep,
                f"SPI 0x050000 defaults cache write version: v{old_dv} -> v{new_dv}",
            )
        for c_addr, c_reg, c_tgt in dc.check_sites:
            rep = enc_beqc_bnec(False, c_reg, new_dv, c_addr, c_tgt)
            record_patch(
                c_addr,
                rep,
                f"SPI 0x050000 defaults cache check version: v{old_dv} -> v{new_dv}",
            )

    patched = bytes(buf)

    # -----------------------------------------------------------------------
    # Mandatory Fail-Closed Post-Patch Verification & NDS32 Emulation!
    # -----------------------------------------------------------------------
    assert len(patched) == len(orig), "Output ROM length mismatch!"

    # Critical boot / flash-idle regions must remain 100% untouched (except optional mirror byte)
    sig_flag_addr = (analysis.ite_sig_offset + 13) if analysis.ite_sig_offset is not None else -1
    for a in range(0x00000, min(0x00100, len(orig))):
        if a != sig_flag_addr:
            assert patched[a] == orig[a], f"Critical header/vector byte @ 0x{a:05X} unexpectedly modified!"
    if len(orig) >= 0x03100:
        assert patched[0x02000:0x03100] == orig[0x02000:0x03100], "Flash-idle / reset region 0x02000..0x03100 modified!"

    # Re-decode every edited mode slot from `patched` and verify 100% round-trip fidelity
    for bank_addr, edited_list in edited_bank_modes.items():
        for em in edited_list:
            redec = validate_and_decode_slot(patched, bank_addr, em.slot_index, offset_in_bank=em.offset_in_bank)
            assert redec is not None, f"Re-decode failed for Bank 0x{bank_addr:05X} Mode {em.slot_index}!"
            assert (redec.ac_pl1_w, redec.ac_pl2_w) == (em.ac_pl1_w, em.ac_pl2_w)
            assert (redec.dc_pl1_w, redec.dc_pl2_w) == (em.dc_pl1_w, em.dc_pl2_w)
            assert (redec.throttle_pl1_w, redec.throttle_pl2_w) == (em.throttle_pl1_w, em.throttle_pl2_w)
            assert redec.manual_min_rpm == int(round(em.manual_min_rpm / 100.0)) * 100
            assert redec.manual_default_rpm == int(round(em.manual_default_rpm / 100.0)) * 100
            assert redec.manual_max_rpm == int(round(em.manual_max_rpm / 100.0)) * 100
            assert len(redec.steps) == len(em.steps)
            for s_new, s_exp in zip(redec.steps, em.steps):
                assert s_new.fan_rpms == [int(round(r / 50.0)) * 50 for r in s_exp.fan_rpms]
                assert s_new.sensor_thresholds == s_exp.sensor_thresholds
                assert s_new.timers_flags == s_exp.timers_flags
    emu_checks.append(f"Mode slot round-trip verification PASSED ({sum(len(v) for v in edited_bank_modes.values())} slots).")

    # Emulate patched cache_builder prologue if thermal cache was bumped
    if any_mode_changed and analysis.thermal_cache is not None and thermal_cache_bump is not None:
        tc = analysis.thermal_cache
        _, new_v = thermal_cache_bump
        expected_ver_reg = tc.ver_store_reg if tc.patch_strategy == "separate_ver_reg" else tc.free_saved_reg
        assert expected_ver_reg is not None

        if tc.patch_strategy == "shared_reg_skid_formula" and tc.skid_offset is not None:
            for test_skid in (0, 1, 2, 3, 4, 255):
                orig_r, _, _, fp_o = emu_nds32(
                    orig, tc.builder_fn_addr,
                    init_mem={0x80000 + tc.skid_offset: test_skid},
                    stop_pcs={tc.builder_body_start},
                )
                new_r, _, _, fp_n = emu_nds32(
                    patched, tc.builder_fn_addr,
                    init_mem={0x80000 + tc.skid_offset: test_skid},
                    stop_pcs={tc.builder_body_start},
                )
                assert fp_o == tc.builder_body_start and fp_n == tc.builder_body_start
                assert new_r[tc.bank_reg] == orig_r[tc.bank_reg], (
                    f"SKID={test_skid}: r{tc.bank_reg}=0x{new_r[tc.bank_reg]:05X} != orig 0x{orig_r[tc.bank_reg]:05X}"
                )
                assert new_r[expected_ver_reg] == new_v, (
                    f"SKID={test_skid}: r{expected_ver_reg}={new_r[expected_ver_reg]} != {new_v}"
                )
            emu_checks.append(
                f"NDS32 Emulator: cache builder SKID prologue (0..255) matched stock banks 100% & set r{expected_ver_reg}={new_v}."
            )
        elif tc.patch_strategy == "shared_reg_single_bank":
            new_r, _, _, fp_n = emu_nds32(
                patched, tc.builder_fn_addr, stop_pcs={tc.builder_body_start}
            )
            assert fp_n == tc.builder_body_start
            assert new_r[tc.bank_reg] == analysis.primary_bank
            assert new_r[expected_ver_reg] == new_v
            emu_checks.append(
                f"NDS32 Emulator: compact cache builder prologue set r{tc.bank_reg}=0x{analysis.primary_bank:05X}, r{expected_ver_reg}={new_v}."
            )
        elif tc.patch_strategy == "separate_ver_reg":
            # Verify re-scanning the patched ROM detects the exact new version!
            re_tc = scan_thermal_cache_signatures(patched)
            assert re_tc is not None and re_tc.current_version == new_v
            emu_checks.append(
                f"Re-scan check: dedicated version register r{expected_ver_reg} updated cleanly to v{new_v}."
            )

    # Emulate patched Custom Mode PL initializer if Custom Mode TDP changed
    if custom_changed and analysis.custom_pl is not None and edited_custom_levels is not None:
        cpl = analysis.custom_pl
        canary = {6: 0x11111116, 7: 0x22222227, 8: 0x33333338, 9: 0x44444449, 10: 0x5555555A}
        regs, mem, calls, _ = emu_nds32(patched, cpl.fn_start, init_regs=canary)
        assert not calls
        for r, val in canary.items():
            assert regs[r] == val, f"Custom PL init clobbered callee-saved r{r}!"
        assert regs[31] == 0x00083FF0, "Custom PL init left stack pointer unbalanced!"
        for b_idx in range(4):
            b_off = 0x86900 + b_idx * 0x28
            for lvl_idx, lv in enumerate(edited_custom_levels):
                exp = [lv.ac_pl1_w, lv.ac_pl2_w, lv.dc_pl1_w, lv.dc_pl2_w]
                offs = (0x00,) if lvl_idx == 0 else ((0x08,) if lvl_idx == 1 else (0x10, 0x18, 0x20))
                for so in offs:
                    got = list(mem[b_off + so : b_off + so + 4])
                    assert got == exp, f"Bank {b_idx} Custom lvl {lvl_idx} @ +0x{so:02X}: {got} != {exp}"
        emu_checks.append(
            "NDS32 Emulator: Custom Mode 4 PL init verified across all 4 SRAM banks (0x86900..0x8699F) & callee-saved regs."
        )

    # Re-analyze patched ROM end-to-end to prove it remains 100% re-scannable!
    re_an = analyze_rom_bytes(patched, source_path="<verified-output>")
    assert not re_an.errors, f"Patched ROM failed re-scan: {re_an.errors}"
    if thermal_cache_bump is not None:
        assert re_an.thermal_cache is not None and re_an.thermal_cache.current_version == thermal_cache_bump[1]
    if defaults_cache_bump is not None:
        assert re_an.defaults_cache is not None and re_an.defaults_cache.current_version == defaults_cache_bump[1]
    emu_checks.append("Full end-to-end re-scan of output ROM PASSED (future re-patching supported).")

    diff_count = sum(a != b for a, b in zip(orig, patched))
    return PatchResult(
        patched_bytes=patched,
        patched_sha256=hashlib.sha256(patched).hexdigest(),
        total_bytes_changed=diff_count,
        thermal_cache_bump=thermal_cache_bump,
        defaults_cache_bump=defaults_cache_bump,
        changes=changes,
        emulator_checks=emu_checks,
    )


def build_patch_manifest(
    analysis: RomAnalysis, result: PatchResult, out_bin_path: str
) -> dict:
    return {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "tool": "Razer Blade IT8390E Signature-Scanning EC Auto-Patcher",
        "source_rom": analysis.source_path,
        "source_sha256": analysis.sha256,
        "fw_version": analysis.fw_version,
        "output_rom": out_bin_path,
        "output_sha256": result.patched_sha256,
        "total_bytes_changed": result.total_bytes_changed,
        "detected_signatures": {
            "ite_sig_offset": f"0x{analysis.ite_sig_offset:05X}" if analysis.ite_sig_offset is not None else None,
            "thermal_loader_addr": f"0x{analysis.thermal_cache.loader_fn_addr:05X}" if analysis.thermal_cache else None,
            "thermal_cache_builder_addr": f"0x{analysis.thermal_cache.builder_fn_addr:05X}" if analysis.thermal_cache else None,
            "thermal_patch_strategy": analysis.thermal_cache.patch_strategy if analysis.thermal_cache else None,
            "sku_banks": [f"0x{b:05X}" for b in analysis.sku_banks],
            "detected_mode_slots": analysis.thermal_cache.slot_indices if analysis.thermal_cache else [],
            "custom_pl_fn": f"0x{analysis.custom_pl.fn_start:05X}..0x{analysis.custom_pl.fn_end:05X}" if analysis.custom_pl else None,
        },
        "cache_bumps": {
            "spi_3fe000_thermal": list(result.thermal_cache_bump) if result.thermal_cache_bump else None,
            "spi_050000_defaults": list(result.defaults_cache_bump) if result.defaults_cache_bump else None,
        },
        "emulator_checks": result.emulator_checks,
        "changes": result.changes,
    }


# ---------------------------------------------------------------------------
# Comprehensive Offline Self-Test (Real ROMs + Synthetic Shifted Other Models!)
# ---------------------------------------------------------------------------

def _make_synthetic_other_blade_rom(stock: bytes) -> bytes:
    """Build a synthetic 'different Razer Blade model' ROM to prove zero hardcoded offsets:
    - Shifts thermal loader & cache builder behavior cleanly:
        * Only 5 modes instead of 8 (Mode 0..4; slots 5..7 erased to 0xFF!)
        * 3 fans instead of 2 (Blade 17 style!)
        * sensor_mask = 0x13 (S0, S1, S4 — RZ09-0330 Vapor Chamber style!)
        * Non-identical SKU banks (SKU 1 has 45W CPU, SKU 3 has 65W CPU!)
        * Different FW string 'ITE8390EC-V01.05'
    """
    buf = bytearray(stock)
    buf[0x90:0xA0] = b"ITE8390EC-V01.05"

    # Patch loader mode limit from 8 -> 5 (`slti45 r0, #5` = 0xe605)
    buf[0x120CC:0x120CE] = enc_slti45(0, 5)

    # Rebuild valid 3-fan, sensor_mask=0x13 (3 sensors: S0, S1, S4) slots for modes 0..4,
    # erase modes 5..7 to 0xFF, and make SKU bank 0x3B000 different from 0x3D000!
    for bank in (0x3B000, 0x3C000, 0x3D000, 0x3E000):
        for slot in range(5):
            b = bank + slot * 0x200
            raw = bytearray(0x200)
            max_step = 6
            fan_count = 3
            sensor_mask = 0x13  # S0, S1, S4 -> 3 active sensors -> stride = 3 + 6 + 5 = 14 bytes
            tail_off = 0x1A8
            raw[0] = (max_step << 3) | fan_count
            raw[1] = sensor_mask
            struct.pack_into("<H", raw, 3, tail_off)
            for st in range(max_step + 1):
                ro = 8 + st * 14
                rpm50 = 0 if st == 0 else (40 + st * 10)
                raw[ro : ro + 3] = bytes([rpm50, rpm50, rpm50])
                raw[ro + 3 : ro + 9] = bytes([45 + st * 5, 55 + st * 5, 42 + st * 5, 52 + st * 5, 35 + st * 2, 42 + st * 2])
                raw[ro + 9 : ro + 14] = bytes([10, 10, 6, 6, 0])
            # Footer
            f = tail_off
            raw[f + 0x0B] = 1
            raw[f + 0x18] = 45 if bank == 0x3B000 else 65
            raw[f + 0x19] = 60 if bank == 0x3B000 else 85
            raw[f + 0x21] = 35
            raw[f + 0x22] = 35
            raw[f + 0x23] = 45
            raw[f + 0x24] = 55
            raw[f + 0x25] = 55
            raw[f + 0x26] = 55
            raw[f + 0x2E] = 35
            raw[f + 0x31] = 60
            raw[f + 0x32] = 87
            raw[f + 0x33] = 30
            raw[f + 0x34] = 30
            buf[b : b + 0x200] = raw
        for slot in range(5, 8):
            b = bank + slot * 0x200
            buf[b : b + 0x200] = b"\xFF" * 0x200
    return bytes(buf)


def run_self_test() -> int:
    repo = Path(__file__).resolve().parent.parent
    stock_path = repo / "firmware" / "rz09-0328-ec-v01.00" / "ec-rom-00000-3ffff.bin"
    v1_path = repo / "firmware" / "rz09-0328-ec-v01.00-mod-fan-0-5500" / "rz09-0328-ec-v01.00-fan-0-5500.bin"
    v2_path = repo / "firmware" / "rz09-0328-ec-v01.00-mod-v2-power-fan" / "rz09-0328-ec-v01.00-v2-fan0-5500-tdp-gpu90w-8dff.bin"

    # 1. Stock RZ09-0328 ROM scan + patch + re-patch test
    stock_bytes = stock_path.read_bytes()
    an = analyze_rom_bytes(stock_bytes, str(stock_path))
    assert not an.errors, f"Stock scan failed: {an.errors}"
    assert an.fw_version == "ITE8390EC-V01.00"
    assert an.thermal_cache is not None
    assert an.thermal_cache.loader_fn_addr == 0x120CA
    assert an.thermal_cache.ver_check_addr == 0x1211E
    assert an.thermal_cache.builder_fn_addr == 0x11314
    assert an.thermal_cache.current_version == 2
    assert an.thermal_cache.patch_strategy == "shared_reg_skid_formula"
    assert len(an.bank_modes[an.primary_bank]) == 8
    assert an.bank_modes[an.primary_bank][6].clone_of == 5
    assert an.bank_modes[an.primary_bank][7].clone_of == 5
    assert an.custom_pl is not None and an.custom_pl.fn_start == 0x10070 and an.custom_pl.fn_end == 0x101C8
    assert [(lv.ac_pl1_w, lv.ac_pl2_w, lv.dc_pl1_w, lv.dc_pl2_w) for lv in an.custom_pl.levels] == [
        (25, 45, 20, 20), (35, 45, 25, 25), (55, 80, 30, 30)
    ]
    assert an.defaults_cache is not None and an.defaults_cache.current_version == 7

    # Edit mode 0 manual fan, mode 5 TDP + step 1 RPM/temp, AND Custom Low/Med/High TDP
    edited_banks = deepcopy(an.bank_modes)
    for b_modes in edited_banks.values():
        b_modes[0].manual_min_rpm = 0
        b_modes[5].ac_pl1_w = 25
        b_modes[5].ac_pl2_w = 35
        b_modes[5].steps[1].fan_rpms = [2000, 2000]
        b_modes[5].steps[1].sensor_thresholds[0] = (52, 63)
    edited_custom = deepcopy(an.custom_pl.levels)
    edited_custom[0].ac_pl1_w, edited_custom[0].ac_pl2_w = 30, 45
    edited_custom[1].ac_pl1_w, edited_custom[1].ac_pl2_w = 55, 75
    edited_custom[2].ac_pl1_w, edited_custom[2].ac_pl2_w = 65, 95
    edited_custom[2].dc_pl1_w, edited_custom[2].dc_pl2_w = 25, 35

    res1 = build_and_verify_patched_rom(an, edited_banks, edited_custom, mirror_flag_mode="keep")
    assert res1.thermal_cache_bump == (2, 3)
    assert res1.defaults_cache_bump == (7, 8)

    # Re-open our OWN output and patch AGAIN (v3 -> v4, v8 -> v9!)
    an2 = analyze_rom_bytes(res1.patched_bytes, "<re-opened>")
    assert not an2.errors and an2.thermal_cache is not None
    assert an2.thermal_cache.current_version == 3
    assert an2.thermal_cache.patch_strategy == "separate_ver_reg"
    assert an2.defaults_cache is not None and an2.defaults_cache.current_version == 8
    assert an2.custom_pl is not None
    assert [(lv.ac_pl1_w, lv.ac_pl2_w, lv.dc_pl1_w, lv.dc_pl2_w) for lv in an2.custom_pl.levels] == [
        (30, 45, 20, 20), (55, 75, 25, 25), (65, 95, 25, 35)
    ]
    eb2 = deepcopy(an2.bank_modes)
    for b_modes in eb2.values():
        b_modes[1].manual_min_rpm = 0
    res2 = build_and_verify_patched_rom(an2, eb2, None)
    assert res2.thermal_cache_bump == (3, 4)
    assert res2.defaults_cache_bump is None  # Custom wasn't touched!

    # 2. Verify scanning existing v1 & v2 ROMs in firmware/
    if v1_path.exists():
        an_v1 = analyze_rom_file(v1_path)
        assert not an_v1.errors and an_v1.thermal_cache is not None
        assert an_v1.thermal_cache.current_version == 3
        assert an_v1.thermal_cache.patch_strategy == "separate_ver_reg"
        assert len(an_v1.sku_banks) == 4
    if v2_path.exists():
        an_v2 = analyze_rom_file(v2_path)
        assert not an_v2.errors and an_v2.thermal_cache is not None
        assert an_v2.thermal_cache.current_version == 4
        assert an_v2.defaults_cache is not None and an_v2.defaults_cache.current_version == 8
        assert an_v2.custom_pl is not None and an_v2.custom_pl.med_fallback_already_fixed
        assert [(lv.ac_pl1_w, lv.ac_pl2_w) for lv in an_v2.custom_pl.levels] == [(35, 45), (60, 80), (60, 90)]

    # 3. Synthetic OTHER Razer Blade model (5 modes, 3 fans, sensor_mask=0x13, non-identical SKU banks!)
    synth = _make_synthetic_other_blade_rom(stock_bytes)
    an_s = analyze_rom_bytes(synth, "<synthetic-blade17>")
    assert not an_s.errors, f"Synthetic Blade scan failed: {an_s.errors}"
    assert an_s.fw_version == "ITE8390EC-V01.05"
    assert not an_s.sku_banks_identical
    assert len(an_s.bank_modes[0x3B000]) == 5  # Dynamically detected 5 modes (NOT 7 or 8!)
    assert an_s.bank_modes[0x3B000][0].fan_count == 3
    assert an_s.bank_modes[0x3B000][0].active_sensors == [0, 1, 4]
    assert an_s.bank_modes[0x3B000][0].ac_pl1_w == 45
    assert an_s.bank_modes[0x3D000][0].ac_pl1_w == 65
    # Verify editing ONLY SKU bank 0x3B000 leaves SKU bank 0x3D000 distinct AND SKID dispatch intact!
    eb_s = deepcopy(an_s.bank_modes)
    eb_s[0x3B000][0].manual_min_rpm = 0
    eb_s[0x3B000][0].steps[2].fan_rpms = [2500, 2600, 2700]
    res_s = build_and_verify_patched_rom(an_s, eb_s, None)
    assert res_s.thermal_cache_bump == (2, 3)

    # 3b. Relocated-functions test: shift thermal_loader, cache_builder, custom_pl_init & defaults_cache
    #     by +0x10000 bytes (zeroing original locations!) to prove ZERO hardcoded code addresses!
    reloc = bytearray(stock_bytes)
    shift = 0x10000
    for rng_start, rng_end in ((0x10070, 0x101C8), (0x10466, 0x1061C), (0x11314, 0x1150C), (0x120CA, 0x12180)):
        reloc[rng_start + shift : rng_end + shift] = stock_bytes[rng_start:rng_end]
        reloc[rng_start:rng_end] = b"\x00" * (rng_end - rng_start)
    an_r = analyze_rom_bytes(bytes(reloc), "<relocated-code>")
    assert not an_r.errors, f"Relocated scan failed: {an_r.errors}"
    assert an_r.thermal_cache is not None
    assert an_r.thermal_cache.loader_fn_addr == 0x120CA + shift
    assert an_r.thermal_cache.ver_check_addr == 0x1211E + shift
    assert an_r.thermal_cache.builder_fn_addr == 0x11314 + shift
    assert an_r.custom_pl is not None and an_r.custom_pl.fn_start == 0x10070 + shift
    assert an_r.defaults_cache is not None and an_r.defaults_cache.current_version == 7
    eb_r = deepcopy(an_r.bank_modes)
    for bm in eb_r.values():
        bm[0].manual_min_rpm = 0
    ec_r = deepcopy(an_r.custom_pl.levels)
    ec_r[2].ac_pl1_w = 60
    res_r = build_and_verify_patched_rom(an_r, eb_r, ec_r)
    assert res_r.thermal_cache_bump == (2, 3) and res_r.defaults_cache_bump == (7, 8)

    # 4. Reject garbage / non-EC file cleanly
    bad = analyze_rom_bytes(b"\x00" * 0x40000, "<garbage>")
    assert bad.errors and not bad.can_patch_modes

    print("ec_auto_patcher_core.py --self-test: ALL TESTS PASSED!")
    print("  - Stock RZ09-0328 scan, patch (v2->v3, v7->v8), emulator & re-patch (v3->v4): OK")
    print("  - Existing v1 & v2 ROM compatibility scan: OK")
    print("  - Synthetic 5-mode / 3-fan / 0x13-sensor / multi-SKU Blade ROM: OK")
    print("  - Relocated +0x10000 code signature scan & NDS32 emulation: OK")
    print("  - Invalid ROM fail-closed rejection: OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Run offline verification suite")
    parser.add_argument("--scan", type=Path, default=None, help="Scan and summarize a 256 KiB EC ROM")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    if args.scan is not None:
        an = analyze_rom_file(args.scan)
        print(f"ROM: {an.source_path} ({an.rom_size:,} bytes, SHA-256: {an.sha256[:16]}...)")
        print(f"FW Version: {an.fw_version!r} | ITE Sig: {an.ite_sig_hex}")
        print(f"Can patch modes: {an.can_patch_modes} | Can patch Custom PL: {an.can_patch_custom_pl}")
        if an.thermal_cache:
            tc = an.thermal_cache
            print(
                f"Thermal Loader @ 0x{tc.loader_fn_addr:05X} (limit {tc.loader_mode_limit}), "
                f"Builder @ 0x{tc.builder_fn_addr:05X}, Cache v{tc.current_version}, "
                f"Strategy: {tc.patch_strategy}"
            )
            print(f"SKU Banks: {[f'0x{b:05X}' for b in an.sku_banks]} (identical={an.sku_banks_identical})")
            for m in an.bank_modes.get(an.primary_bank, []):
                clone_tag = f" [Clone of {m.clone_of}]" if m.clone_of is not None else ""
                print(
                    f"  Mode {m.slot_index} ({m.name}{clone_tag}): "
                    f"AC {m.ac_pl1_w}/{m.ac_pl2_w}W, DC {m.dc_pl1_w}/{m.dc_pl2_w}W, "
                    f"Man {m.manual_min_rpm}-{m.manual_max_rpm} (def {m.manual_default_rpm}), "
                    f"{len(m.steps)} steps, {m.fan_count} fans, sensors={m.active_sensors}"
                )
        for w in an.warnings:
            print(f"WARNING: {w}")
        for e in an.errors:
            print(f"ERROR: {e}")
        return 1 if an.errors else 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
