"""Offline AndesCore NDS32 (16/32-bit mixed) disassembler for IT8390E EC ROM images.

Usage:
    python ec_tools/dis_nds32.py 12356:123a0
    python ec_tools/dis_nds32.py --bin path/to/rom.bin 0e0f0:0e2c0
"""

import argparse
from pathlib import Path
import struct

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BIN = str(REPO_ROOT / "firmware" / "rz09-0328-ec-v01.00" / "ec-rom-00000-3ffff.bin")
R45MAP = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 16, 17, 18, 19]
GPRS = [f"r{i}" for i in range(28)] + ["fp", "gp", "lp", "sp"]


def sbits(val, n):
    m = 1 << (n - 1)
    return (val ^ m) - m


def dec32(w, pc):
    op6 = (w >> 25) & 0x3F
    rt, ra, rb, rd = (w >> 20) & 31, (w >> 15) & 31, (w >> 10) & 31, (w >> 5) & 31
    imm15s = sbits(w & 0x7FFF, 15)
    imm15u = w & 0x7FFF
    if op6 == 0x00:
        return f"lbi {GPRS[rt]}, [{GPRS[ra]}+0x{imm15s & 0xffffffff:x}]"
    if op6 == 0x01:
        return f"lhi {GPRS[rt]}, [{GPRS[ra]}+{imm15s << 1}]"
    if op6 == 0x02:
        return f"lwi {GPRS[rt]}, [{GPRS[ra]}+{imm15s << 2}]"
    if op6 == 0x03:
        return f"ldi {GPRS[rt]}, [{GPRS[ra]}+{imm15s << 3}]"
    if op6 == 0x04:
        return f"lbi.bi {GPRS[rt]}, [{GPRS[ra]}], {imm15s}"
    if op6 == 0x05:
        return f"lhi.bi {GPRS[rt]}, [{GPRS[ra]}], {imm15s << 1}"
    if op6 == 0x06:
        return f"lwi.bi {GPRS[rt]}, [{GPRS[ra]}], {imm15s << 2}"
    if op6 == 0x08:
        return f"sbi {GPRS[rt]}, [{GPRS[ra]}+0x{imm15s & 0xffffffff:x}]"
    if op6 == 0x09:
        return f"shi {GPRS[rt]}, [{GPRS[ra]}+{imm15s << 1}]"
    if op6 == 0x0A:
        return f"swi {GPRS[rt]}, [{GPRS[ra]}+{imm15s << 2}]"
    if op6 == 0x0C:
        return f"sbi.bi {GPRS[rt]}, [{GPRS[ra]}], {imm15s}"
    if op6 == 0x0D:
        return f"shi.bi {GPRS[rt]}, [{GPRS[ra]}], {imm15s << 1}"
    if op6 == 0x0E:
        return f"swi.bi {GPRS[rt]}, [{GPRS[ra]}], {imm15s << 2}"
    if op6 == 0x10:
        return f"lbsi {GPRS[rt]}, [{GPRS[ra]}+0x{imm15s & 0xffffffff:x}]"
    if op6 == 0x11:
        return f"lhsi {GPRS[rt]}, [{GPRS[ra]}+{imm15s << 1}]"
    if op6 == 0x14:
        return f"lbsi.bi {GPRS[rt]}, [{GPRS[ra]}], {imm15s}"
    if op6 == 0x17:
        sub = (w >> 19) & 1
        imm19 = sbits(w & 0x7FFFF, 19)
        return f"lbi.gp {GPRS[rt]}, [gp+0x{imm19 & 0xffffffff:x}]" if sub == 0 else f"lbsi.gp {GPRS[rt]}, [gp+0x{imm19 & 0xffffffff:x}]"
    if op6 == 0x1E:  # HWGP
        sub3 = (w >> 17) & 7
        sub2 = (w >> 18) & 3
        if sub2 in (0, 1):
            imm18 = sbits(w & 0x3FFFF, 18) << 1
            mn = "lhi.gp" if sub2 == 0 else "lhsi.gp"
            return f"{mn} {GPRS[rt]}, [gp+0x{imm18 & 0xffffffff:x}]"
        if sub2 == 2:
            imm18 = sbits(w & 0x3FFFF, 18) << 1
            return f"shi.gp {GPRS[rt]}, [gp+0x{imm18 & 0xffffffff:x}]"
        imm17 = sbits(w & 0x1FFFF, 17) << 2
        mn = "lwi.gp" if sub3 == 6 else "swi.gp"
        return f"{mn} {GPRS[rt]}, [gp+0x{imm17 & 0xffffffff:x}]"
    if op6 == 0x1F:
        sub = (w >> 19) & 1
        imm19 = sbits(w & 0x7FFFF, 19)
        return f"sbi.gp {GPRS[rt]}, [gp+0x{imm19 & 0xffffffff:x}]" if sub == 0 else f"addi.gp {GPRS[rt]}, #0x{imm19 & 0xffffffff:x}"
    if op6 == 0x1C:  # MEM
        sub = w & 0xFF
        sv = (w >> 8) & 3
        names = {
            0x00: "lb", 0x01: "lh", 0x02: "lw",
            0x04: "lb.bi", 0x05: "lh.bi", 0x06: "lw.bi",
            0x08: "sb", 0x09: "sh", 0x0A: "sw",
            0x0C: "sb.bi", 0x0D: "sh.bi", 0x0E: "sw.bi",
            0x10: "lbs", 0x11: "lhs",
        }
        mn = names.get(sub)
        if mn:
            return f"{mn} {GPRS[rt]}, [{GPRS[ra]}+({GPRS[rb]}<<{sv})]"
    if op6 == 0x1D:  # LSMW
        en4 = (w >> 6) & 0xF
        ls = (w >> 5) & 1
        mn = "smw" if ls else "lmw"
        return f"{mn} {GPRS[rt]}..{GPRS[rb]}, [{GPRS[ra]}], en4=0x{en4:x}, flags=0x{w & 0x1f:02x}"
    if op6 == 0x20:  # ALU1
        sub = w & 0x1F
        sh5 = (w >> 5) & 0x1F
        ib5 = (w >> 10) & 0x1F
        if sub in (0, 1, 2, 3, 4):
            mn = {0: "add", 1: "sub", 2: "and", 3: "xor", 4: "or"}[sub]
            return f"{mn}_slli {GPRS[rt]}, {GPRS[ra]}, ({GPRS[rb]}<<{sh5})" if sh5 else f"{mn} {GPRS[rt]}, {GPRS[ra]}, {GPRS[rb]}"
        if sub in (5, 6, 7):
            mn = {5: "nor", 6: "slt", 7: "slts"}[sub]
            return f"{mn} {GPRS[rt]}, {GPRS[ra]}, {GPRS[rb]}"
        if sub in (8, 9, 10, 11):
            mn = {8: "slli", 9: "srli", 10: "srai", 11: "rotri"}[sub]
            return f"{mn} {GPRS[rt]}, {GPRS[ra]}, #{ib5}"
        if sub in (12, 13, 14, 15):
            mn = {12: "sll", 13: "srl", 14: "sra", 15: "rotr"}[sub]
            return f"{mn} {GPRS[rt]}, {GPRS[ra]}, {GPRS[rb]}"
        if sub in (16, 17, 19, 20):
            mn = {16: "seb", 17: "seh", 19: "zeh", 20: "wsbh"}[sub]
            return f"{mn} {GPRS[rt]}, {GPRS[ra]}"
        if sub == 18:
            return f"bitc {GPRS[rt]}, {GPRS[ra]}, {GPRS[rb]}"
        if sub == 21:
            return f"or_srli {GPRS[rt]}, {GPRS[ra]}, ({GPRS[rb]}>>{sh5})"
        if sub in (22, 23):
            mn = "divsr" if sub == 22 else "divr"
            return f"{mn} q={GPRS[rt]}, r={GPRS[rd]}, {GPRS[ra]}, {GPRS[rb]}"
        if sub in (24, 25, 26, 27):
            mn = {24: "sva", 25: "svs", 26: "cmovz", 27: "cmovn"}[sub]
            return f"{mn} {GPRS[rt]}, {GPRS[ra]}, {GPRS[rb]}"
        if sub in (28, 29, 30, 31):
            mn = {28: "add_srli", 29: "sub_srli", 30: "and_srli", 31: "xor_srli"}[sub]
            return f"{mn} {GPRS[rt]}, {GPRS[ra]}, ({GPRS[rb]}>>{sh5})"
    if op6 == 0x21:  # ALU2
        mid = (w >> 6) & 0xF
        sub6 = w & 0x3F
        if mid == 0:
            m0 = {
                0x00: "max", 0x01: "min", 0x02: "ave", 0x03: "abs",
                0x08: "bset", 0x09: "bclr", 0x0A: "btgl", 0x0B: "btst",
                0x24: "mul", 0x2E: "divs", 0x2F: "div", 0x31: "mult32", 0x33: "madd32",
            }
            if sub6 in m0:
                if sub6 == 0x03:
                    return f"abs {GPRS[rt]}, {GPRS[ra]}"
                if sub6 in (0x08, 0x09, 0x0A, 0x0B):
                    return f"{m0[sub6]} {GPRS[rt]}, {GPRS[ra]}, #bit={rb}"
                return f"{m0[sub6]} {GPRS[rt]}, {GPRS[ra]}, {GPRS[rb]}"
        if mid == 1:
            m1 = {0x28: "mulsr64", 0x29: "mulr64", 0x33: "maddr32", 0x35: "msubr32"}
            if sub6 in m1:
                return f"{m1[sub6]} {GPRS[rt]}, {GPRS[ra]}, {GPRS[rb]}"
    if op6 == 0x22:
        return f"movi {GPRS[rt]}, #0x{sbits(w & 0xfffff, 20) & 0xffffffff:x}"
    if op6 == 0x23:
        return f"sethi {GPRS[rt]}, #0x{(w & 0xfffff) << 12:08x}"
    if op6 == 0x24:
        tgt = (pc + (sbits(w & 0xFFFFFF, 24) << 1)) & 0xFFFFFFFF
        mn = "jal" if (w >> 24) & 1 else "j"
        return f"{mn} 0x{tgt:05x}"
    if op6 == 0x25:
        sub = w & 0x1F
        if sub == 4:
            return f"ret {GPRS[rb]}"
        if sub == 1:
            return f"jral {GPRS[rt]}, {GPRS[rb]}"
        if sub == 0:
            return f"jr {GPRS[rb]}"
    if op6 == 0x26:
        tgt = pc + (sbits(w & 0x3FFF, 14) << 1)
        mn = "bne" if (w >> 14) & 1 else "beq"
        return f"{mn} {GPRS[rt]}, {GPRS[ra]}, 0x{tgt:05x}"
    if op6 == 0x27:
        sub = (w >> 16) & 0xF
        tgt = pc + (sbits(w & 0xFFFF, 16) << 1)
        m = {2: "beqz", 3: "bnez", 4: "bgez", 5: "bltz", 6: "bgtz", 7: "blez", 12: "bgezal", 13: "bltzal"}
        if sub in m:
            return f"{m[sub]} {GPRS[rt]}, 0x{tgt:05x}"
    if op6 == 0x28:
        return f"addi {GPRS[rt]}, {GPRS[ra]}, #0x{imm15s & 0xffffffff:x}"
    if op6 == 0x29:
        return f"subri {GPRS[rt]}, {GPRS[ra]}, #0x{imm15s & 0xffffffff:x}"
    if op6 == 0x2A:
        return f"andi {GPRS[rt]}, {GPRS[ra]}, #0x{imm15u:x}"
    if op6 == 0x2B:
        return f"xori {GPRS[rt]}, {GPRS[ra]}, #0x{imm15u:x}"
    if op6 == 0x2C:
        return f"ori {GPRS[rt]}, {GPRS[ra]}, #0x{imm15u:x}"
    if op6 == 0x2D:
        b = (w >> 19) & 1
        imm11 = (w >> 8) & 0x7FF
        imm8 = sbits(w & 0xFF, 8)
        tgt = (pc + (imm8 << 1)) & 0xFFFFFFFF
        mn = "bnec" if b else "beqc"
        return f"{mn} {GPRS[rt]}, #0x{imm11:x}, 0x{tgt:05x}"
    if op6 == 0x2E:
        return f"slti {GPRS[rt]}, {GPRS[ra]}, #0x{imm15u:x}"
    if op6 == 0x2F:
        return f"sltsi {GPRS[rt]}, {GPRS[ra]}, #0x{imm15s:x}"
    if op6 == 0x33:
        return f"bitci {GPRS[rt]}, {GPRS[ra]}, #0x{imm15u:x}"
    return f"u32(0x{op6:02x}) 0x{w:08x}"


def dec16(hw, pc):
    rt5 = (hw >> 5) & 0x1F
    ra5 = hw & 0x1F
    rt4 = GPRS[R45MAP[(hw >> 5) & 0xF]]
    rt3 = GPRS[(hw >> 6) & 7]
    ra3 = GPRS[(hw >> 3) & 7]
    rb3 = GPRS[hw & 7]
    rt38 = GPRS[(hw >> 8) & 7]
    if (hw & 0xFC00) == 0x8000:
        return f"mov55 {GPRS[rt5]}, {GPRS[ra5]}"
    if (hw & 0xFC00) == 0x8400:
        return f"movi55 {GPRS[rt5]}, #{sbits(hw & 0x1F, 5)}"
    if (hw & 0xFE00) == 0x8800:
        return f"add45 {rt4}, {GPRS[ra5]}"
    if (hw & 0xFE00) == 0x8A00:
        return f"sub45 {rt4}, {GPRS[ra5]}"
    if (hw & 0xFE00) == 0x8C00:
        return f"addi45 {rt4}, #{hw & 0x1F}"
    if (hw & 0xFE00) == 0x8E00:
        return f"subi45 {rt4}, #{hw & 0x1F}"
    if (hw & 0xFE00) == 0x9000:
        return f"srai45 {rt4}, #{hw & 0x1F}"
    if (hw & 0xFE00) == 0x9200:
        return "nop16" if hw == 0x9200 else f"srli45 {rt4}, #{hw & 0x1F}"
    if (hw & 0xFE00) == 0x9400:
        return f"slli333 {rt3}, {ra3}, #{hw & 7}"
    if (hw & 0xFE00) == 0x9600:
        sub = hw & 7
        ia3 = (hw >> 3) & 7
        if sub == 6:
            return f"bmski33 {rt3}, #bit{ia3}"
        if sub == 7:
            return f"fexti33 {rt3}, #bits{ia3 + 1}"
        m = {0: "zeb33", 1: "zeh33", 2: "seb33", 3: "seh33", 4: "xlsb33", 5: "x11b33"}
        return f"{m[sub]} {rt3}, {ra3}"
    if (hw & 0xFE00) == 0x9800:
        return f"add333 {rt3}, {ra3}, {rb3}"
    if (hw & 0xFE00) == 0x9A00:
        return f"sub333 {rt3}, {ra3}, {rb3}"
    if (hw & 0xFE00) == 0x9C00:
        return f"addi333 {rt3}, {ra3}, #{hw & 7}"
    if (hw & 0xFE00) == 0x9E00:
        return f"subi333 {rt3}, {ra3}, #{hw & 7}"
    if (hw & 0xFE00) == 0xA000:
        return f"lwi333 {rt3}, [{ra3}+{(hw & 7) << 2}]"
    if (hw & 0xFE00) == 0xA200:
        return f"lwi333.bi {rt3}, [{ra3}], {(hw & 7) << 2}"
    if (hw & 0xFE00) == 0xA400:
        return f"lhi333 {rt3}, [{ra3}+{(hw & 7) << 1}]"
    if (hw & 0xFE00) == 0xA600:
        return f"lbi333 {rt3}, [{ra3}+{hw & 7}]"
    if (hw & 0xFE00) == 0xA800:
        return f"swi333 {rt3}, [{ra3}+{(hw & 7) << 2}]"
    if (hw & 0xFE00) == 0xAA00:
        return f"swi333.bi {rt3}, [{ra3}], {(hw & 7) << 2}"
    if (hw & 0xFE00) == 0xAC00:
        return f"shi333 {rt3}, [{ra3}+{(hw & 7) << 1}]"
    if (hw & 0xFE00) == 0xAE00:
        return f"sbi333 {rt3}, [{ra3}+{hw & 7}]"
    if (hw & 0xFE00) == 0xB000:
        return f"addri36.sp {rt3}, #{(hw & 0x3F) << 2}"
    if (hw & 0xFE00) == 0xB400:
        return f"lwi450 {rt4}, [{GPRS[ra5]}]"
    if (hw & 0xFE00) == 0xB600:
        return f"swi450 {rt4}, [{GPRS[ra5]}]"
    if (hw & 0xFF80) == 0xB800:
        return f"lwi37 {rt38}, [fp+{(hw & 0x7F) << 2}]"
    if (hw & 0xFF80) == 0xB880:
        return f"swi37 {rt38}, [fp+{(hw & 0x7F) << 2}]"
    if (hw & 0xF800) == 0xC000:
        return f"beqz38 {rt38}, 0x{pc + (sbits(hw & 0xFF, 8) << 1):05x}"
    if (hw & 0xF800) == 0xC800:
        return f"bnez38 {rt38}, 0x{pc + (sbits(hw & 0xFF, 8) << 1):05x}"
    if (hw & 0xFF00) == 0xD500:
        return f"j8 0x{pc + (sbits(hw & 0xFF, 8) << 1):05x}"
    if (hw & 0xFFE0) == 0xDD00:
        return f"jr5 {GPRS[ra5]}"
    if (hw & 0xFFE0) == 0xDD20:
        return f"jral5 {GPRS[ra5]}"
    if (hw & 0xFFE0) == 0xDD80:
        return "ret5" if ra5 == 30 else f"ret5 {GPRS[ra5]}"
    if (hw & 0xF800) == 0xD000:
        return f"beqs38 r5, {rt38}, 0x{pc + (sbits(hw & 0xFF, 8) << 1):05x}"
    if (hw & 0xF800) == 0xD800:
        return f"bnes38 r5, {rt38}, 0x{pc + (sbits(hw & 0xFF, 8) << 1):05x}"
    if (hw & 0xFE00) == 0xE000:
        return f"slts45 {rt4}, {GPRS[ra5]}"
    if (hw & 0xFE00) == 0xE200:
        return f"slt45 {rt4}, {GPRS[ra5]}"
    if (hw & 0xFE00) == 0xE400:
        return f"sltsi45 {rt4}, #{hw & 0x1F}"
    if (hw & 0xFE00) == 0xE600:
        return f"slti45 {rt4}, #{hw & 0x1F}"
    if (hw & 0xFF00) == 0xE800:
        return f"beqzs8 0x{pc + (sbits(hw & 0xFF, 8) << 1):05x}"
    if (hw & 0xFF00) == 0xE900:
        return f"bnezs8 0x{pc + (sbits(hw & 0xFF, 8) << 1):05x}"
    if (hw & 0xFC00) == 0xEC00:
        return f"addi10.sp #{sbits(hw & 0x3FF, 10)}"
    if (hw & 0xFF80) == 0xF000:
        return f"lwi37.sp {rt38}, [sp+{(hw & 0x7F) << 2}]"
    if (hw & 0xFF80) == 0xF080:
        return f"swi37.sp {rt38}, [sp+{(hw & 0x7F) << 2}]"
    if (hw & 0xFE00) == 0xFA00:
        return f"movpi45 {rt4}, #{(hw & 0x1F) + 16}"
    if (hw & 0xFF80) == 0xFC00:
        return f"push25 re={(hw >> 5) & 3}, imm={(hw & 0x1F) << 3}"
    if (hw & 0xFF80) == 0xFC80:
        return f"pop25 re={(hw >> 5) & 3}, imm={(hw & 0x1F) << 3}"
    if (hw & 0xFF00) == 0xFD00:
        return f"movd44 r{((hw>>4)&0xf)*2}, r{(hw&0xf)*2}"
    if (hw & 0xFE07) == 0xFE07:
        return f"or33 {rt3}, {ra3}"
    if (hw & 0xFE07) == 0xFE06:
        return f"and33 {rt3}, {ra3}"
    if (hw & 0xFE07) == 0xFE05:
        return f"xor33 {rt3}, {ra3}"
    if (hw & 0xFE07) == 0xFE04:
        return f"mul33 {rt3}, {ra3}"
    if (hw & 0xFE07) == 0xFE02:
        return f"neg33 {rt3}, {ra3}"
    if (hw & 0xFE07) == 0xFE03:
        return f"not33 {rt3}, {ra3}"
    return f"u16 0x{hw:04x}"


def dis(data, a, b):
    pc = a
    while pc < b:
        hw = struct.unpack(">H", data[pc : pc + 2])[0]
        if (hw & 0x8000) == 0:
            w = struct.unpack(">I", data[pc : pc + 4])[0]
            print(f"0x{pc:05x}: {w:08x}  {dec32(w, pc)}")
            pc += 4
        else:
            print(f"0x{pc:05x}: {hw:04x}      {dec16(hw, pc)}")
            pc += 2


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--bin", default=DEFAULT_BIN)
    p.add_argument("ranges", nargs="+")
    args = p.parse_args()
    data = open(args.bin, "rb").read()
    for r in args.ranges:
        a, b = [int(x, 16) for x in r.split(":")]
        print(f"=== 0x{a:05x}..0x{b:05x} ===")
        dis(data, a, b)
