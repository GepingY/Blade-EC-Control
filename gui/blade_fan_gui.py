"""Interactive Tkinter GUI for Razer Blade 1532:0255 EC mode & fan control.

Tailored to RZ09-0328 (EC v01.00 / IT8390E-256) firmware behavior:
- Reuses native HID transport helpers from blade_inspect.py with an explicit
  command/argument allowlist (0x0081, 0x078F, 0x0D01, 0x0D02, 0x0D07, 0x0D81,
  0x0D82, 0x0D85, 0x0D87).
- Exposes Custom Mode (4)'s three individual CPU sub-modes (Low/Medium/High) and
  GPU Boost sub-levels, automatically re-applying 0x0D07 after 0x0D02 because
  EC firmware resets 0x808e0/0x808e1 to Medium (1) on every 0x0D02 mode write.
- Auto-detects EC power/fan/GPU tables from live silicon probes
  (diagnostics/verify-live-*/probe.json) or 256 KiB RZ09-0328 EC ROM binaries,
  with built-in v2 mod defaults (0–5500 RPM all modes, Custom 35/45·60/80·60/90 W,
  90 W GPU on Custom Low/Med, Silent 25/35 W AC & DC) and live nvidia-smi GPU
  enforced power-limit cross-checks.
- Supports minimizing to the Windows system tray (pure Win32 ctypes, zero
  third-party packages) with quick mode / Turbo Boost switching from the tray menu.

Run with --self-test to verify packet framing, clamp math, Custom 4 boost
re-application logic, live probe / ROM auto-detection, nvidia-smi parsing, tray
icon helpers, and UI build without touching hardware.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import Future, ThreadPoolExecutor
import ctypes
from dataclasses import dataclass, replace
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Callable
import zlib

from blade_inspect import MIN_REPORT, TRANSACTION, NativeHid, checksum, describe_reply

POLL_INTERVAL_MS = 1500
TRAY_POLL_INTERVAL_MS = 5000
POWER_POLL_EVERY_N_TICKS = 10
MAX_RPM = 5500
STEP_RPM = 100

TRAY_CMD_OPEN = 1001
TRAY_CMD_MODE_BALANCED = 1010
TRAY_CMD_MODE_GAMING = 1011
TRAY_CMD_MODE_CREATOR = 1012
TRAY_CMD_MODE_CUSTOM_LOW = 1013
TRAY_CMD_MODE_CUSTOM_HIGH = 1014
TRAY_CMD_MODE_SILENT = 1015
TRAY_CMD_MODE_CUSTOM_MED = 1016
TRAY_CMD_FAN_AUTO = 1020
TRAY_CMD_TURBO_OFF = 1030
TRAY_CMD_TURBO_ON = 1031
TRAY_CMD_EXIT = 1099

SUB_PROCESSOR_GUID = "54533251-82be-4824-96c1-47b60b740d00"
PERFBOOSTMODE_GUID = "be337238-0d82-4146-a960-4f3749d470c7"
DEFAULT_HIGH_PERF_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
DEFAULT_QUIET_PLAN_GUID = ""

_NDS32_R45MAP = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 16, 17, 28, 29)


@dataclass(frozen=True)
class ModeSpec:
    code: int
    name: str
    short_hint: str
    ac_pl1_w: int
    ac_pl2_w: int
    ac_thermal_note: str
    bat_pl1_w: int
    bat_pl2_w: int
    gpu_limit_w: int
    gpu_temp_c: int
    auto_range: str
    manual_min_rpm: int | None
    manual_max_rpm: int
    manual_default_rpm: int
    notes: str

    @property
    def bat_pl_w(self) -> int:
        return self.bat_pl1_w


@dataclass(frozen=True)
class CustomCpuSpec:
    level: int
    name: str
    ac_pl1_w: int
    ac_pl2_w: int
    ac_thermal_rule: str
    bat_pl1_w: int
    bat_pl2_w: int
    gpu_limit_w: int
    button_label: str

    @property
    def bat_pl_w(self) -> int:
        return self.bat_pl1_w


@dataclass(frozen=True)
class CustomGpuSpec:
    level: int
    name: str
    limit_w: int
    temp_target_c: int
    label: str


@dataclass(frozen=True)
class EcProfile:
    source_kind: str
    source_label: str
    profile_tag: str
    has_tdp_readback: bool
    modes: dict[int, ModeSpec]
    custom_cpu: dict[int, CustomCpuSpec]
    custom_gpu: dict[int, CustomGpuSpec]
    custom_gpu_90w_levels: tuple[int, ...]
    reference_notes: tuple[str, ...]
    clamp_summary_text: str
    custom_gpu_hint_text: str
    cache_ver: int | None = None
    defaults_ver: int | None = None

    def mode_label(self, code: int) -> str:
        spec = self.modes.get(code)
        return f"{spec.name} ({code})" if spec is not None else f"Mode {code}"

    def expected_gpu_limit_w(self, mode: int, cpu_boost: int) -> int | None:
        if mode == 4:
            cs = self.custom_cpu.get(cpu_boost, self.custom_cpu[1])
            return cs.gpu_limit_w
        spec = self.modes.get(mode)
        return spec.gpu_limit_w if spec is not None else None

    def tray_labels(self) -> dict[int, str]:
        sil = self.modes[5]
        bal = self.modes[0]
        gam = self.modes[1]
        cre = self.modes[2]
        cl = self.custom_cpu[0]
        cm = self.custom_cpu[1]
        ch = self.custom_cpu[2]
        return {
            TRAY_CMD_MODE_SILENT: f"Silent (5) — {sil.ac_pl1_w}/{sil.ac_pl2_w}W · GPU {sil.gpu_limit_w}W",
            TRAY_CMD_MODE_BALANCED: f"Balanced (0) — {bal.ac_pl1_w}/{bal.ac_pl2_w}W · GPU {bal.gpu_limit_w}W",
            TRAY_CMD_MODE_GAMING: f"Gaming (1) — {gam.ac_pl1_w}/{gam.ac_pl2_w}W · GPU {gam.gpu_limit_w}W",
            TRAY_CMD_MODE_CREATOR: f"Creator / Perf (2) — {cre.ac_pl1_w}/{cre.ac_pl2_w}W · GPU {cre.gpu_limit_w}W",
            TRAY_CMD_MODE_CUSTOM_LOW: f"Custom (4) Low — {cl.ac_pl1_w}/{cl.ac_pl2_w}W · GPU {cl.gpu_limit_w}W",
            TRAY_CMD_MODE_CUSTOM_MED: f"Custom (4) Med — {cm.ac_pl1_w}/{cm.ac_pl2_w}W · GPU {cm.gpu_limit_w}W",
            TRAY_CMD_MODE_CUSTOM_HIGH: f"Custom (4) High — {ch.ac_pl1_w}/{ch.ac_pl2_w}W · GPU {ch.gpu_limit_w}W",
        }


def _fmt_clamp_span(min_rpm: int | None, max_rpm: int) -> str:
    return f"{min_rpm}–{max_rpm}" if min_rpm is not None else f"<= {max_rpm} (no min)"


def _format_custom_cpu_button_label(
    level: int,
    name: str,
    ac_pl1_w: int,
    ac_pl2_w: int,
    bat_pl1_w: int,
    bat_pl2_w: int,
    gpu_limit_w: int,
    is_max_cpu: bool,
) -> str:
    tag = ""
    if is_max_cpu and gpu_limit_w >= 90:
        tag = " [MAX CPU+GPU]"
    elif is_max_cpu:
        tag = " [MAX CPU]"
    elif gpu_limit_w >= 90 and ac_pl1_w >= 55:
        tag = " [MAX GPU+CPU]"
    elif gpu_limit_w >= 90:
        tag = " [90W GPU]"
    default_suffix = " · 0x0D02 default" if level == 1 else ""
    return (
        f"Custom {name} ({level}){tag}\n"
        f"CPU {ac_pl1_w}W/{ac_pl2_w}W AC · GPU {gpu_limit_w}W\n"
        f"(Battery {bat_pl1_w}W/{bat_pl2_w}W{default_suffix})"
    )


def _compose_profile(
    *,
    source_kind: str,
    source_label: str,
    has_tdp_readback: bool,
    modes_in: dict[int, ModeSpec],
    custom_cpu_in: dict[int, CustomCpuSpec],
    custom_gpu_90w_levels: tuple[int, ...],
    cache_ver: int | None = None,
    defaults_ver: int | None = None,
) -> EcProfile:
    max_cpu_key = max(
        (0, 1, 2),
        key=lambda lv: (custom_cpu_in[lv].ac_pl1_w, custom_cpu_in[lv].ac_pl2_w, lv),
    )
    custom_cpu: dict[int, CustomCpuSpec] = {}
    for lv in (0, 1, 2):
        c = custom_cpu_in[lv]
        gpu_w = 90 if lv in custom_gpu_90w_levels else 80
        custom_cpu[lv] = replace(
            c,
            gpu_limit_w=gpu_w,
            button_label=_format_custom_cpu_button_label(
                lv,
                c.name,
                c.ac_pl1_w,
                c.ac_pl2_w,
                c.bat_pl1_w,
                c.bat_pl2_w,
                gpu_w,
                is_max_cpu=(lv == max_cpu_key),
            ),
        )

    all_unlocked_0_5500 = all(
        (modes_in[c].manual_min_rpm in (0, None)) and modes_in[c].manual_max_rpm == 5500
        for c in (0, 1, 2, 4, 5, 6)
    )

    modes: dict[int, ModeSpec] = {}
    for code, s in modes_in.items():
        if code == 0:
            short_hint = f"CPU {s.ac_pl1_w}/{s.ac_pl2_w}W · GPU {s.gpu_limit_w}W\nAuto 0 or 2500–5300"
        elif code == 1:
            gpu_tag = " (Max)" if s.gpu_limit_w >= 90 else ""
            short_hint = f"CPU {s.ac_pl1_w}/{s.ac_pl2_w}W · GPU {s.gpu_limit_w}W{gpu_tag}\nAuto 0 or 2700–5500"
        elif code == 2:
            short_hint = f"CPU {s.ac_pl1_w}/{s.ac_pl2_w}W · GPU {s.gpu_limit_w}W\nAuto 0 or 2700–5500"
        elif code == 3:
            clamp_note = "Manual 0–5500" if (s.manual_min_rpm in (0, None) and s.manual_max_rpm == 5500) else "No min manual clamp quirk"
            short_hint = f"CPU {s.ac_pl1_w}/{s.ac_pl2_w}W · GPU {s.gpu_limit_w}W\n{clamp_note}"
        elif code == 4:
            c0, c1, c2 = custom_cpu[0], custom_cpu[1], custom_cpu[2]
            gpu_span = "90W/80W" if (0 in custom_gpu_90w_levels or 1 in custom_gpu_90w_levels) else "80W"
            short_hint = (
                f"CPU {c0.ac_pl1_w}/{c0.ac_pl2_w} · {c1.ac_pl1_w}/{c1.ac_pl2_w} · {c2.ac_pl1_w}/{c2.ac_pl2_w}W\n"
                f"GPU {gpu_span} · Auto 0 or 2700–5500"
            )
        elif code == 5:
            short_hint = f"CPU {s.ac_pl1_w}/{s.ac_pl2_w}W AC/DC · GPU {s.gpu_limit_w}W\n0 RPM Cool (<60/45/43°C)"
            if (s.ac_pl1_w, s.ac_pl2_w) != (s.bat_pl1_w, s.bat_pl2_w):
                short_hint = f"CPU {s.ac_pl1_w}/{s.ac_pl2_w}W · GPU {s.gpu_limit_w}W\n0 RPM Cool (<60/45/43°C)"
        else:
            short_hint = f"CPU {s.ac_pl1_w}/{s.ac_pl2_w}W · GPU {s.gpu_limit_w}W\nIdentical slot table to Mode 5"
        modes[code] = replace(s, short_hint=short_hint)

    if custom_gpu_90w_levels:
        custom_gpu: dict[int, CustomGpuSpec] = {
            0: CustomGpuSpec(0, "Low", 90, 75, "GPU Low (0): 75°C target"),
            1: CustomGpuSpec(1, "Medium", 90, 87, "GPU Med (1): 87°C target"),
            2: CustomGpuSpec(2, "High", 80, 87, "GPU High (2): 87°C target"),
        }
        lv_names = "/".join(custom_cpu[lv].name for lv in custom_gpu_90w_levels if lv in custom_cpu)
        other_names = "/".join(custom_cpu[lv].name for lv in (0, 1, 2) if lv not in custom_gpu_90w_levels)
        if other_names:
            custom_gpu_hint_text = f"GPU Cap follows Custom CPU: {lv_names} = 90W (CTGP=1), {other_names} = 80W"
        else:
            custom_gpu_hint_text = "GPU Cap = 90W (CTGP=1) on all Custom CPU levels"
    else:
        custom_gpu = {
            0: CustomGpuSpec(0, "Low", 80, 75, "GPU Low (0): 80W · 75°C target"),
            1: CustomGpuSpec(1, "Medium", 80, 87, "GPU Med (1): 80W · 87°C target"),
            2: CustomGpuSpec(2, "High", 80, 87, "GPU High (2): 80W · 87°C target"),
        }
        custom_gpu_hint_text = "GPU Cap fixed at 80W in Custom (4); Gaming (1) = 90W"

    if all_unlocked_0_5500:
        clamp_summary_text = (
            "EC manual clamps: All modes 0–5500 RPM (unlocked mod). "
            "0 RPM = clean fan stop; 100–~1800 RPM sits below 20% PWM spin floor; ~2000–5500 RPM smooth PID."
        )
    else:
        s0, s1, s5 = modes[0], modes[1], modes[5]
        clamp_summary_text = (
            f"EC manual clamps: Silent (5) {_fmt_clamp_span(s5.manual_min_rpm, s5.manual_max_rpm)} RPM | "
            f"Balanced (0) {_fmt_clamp_span(s0.manual_min_rpm, s0.manual_max_rpm)} RPM | "
            f"Gaming/Creator/Custom {_fmt_clamp_span(s1.manual_min_rpm, s1.manual_max_rpm)} RPM. "
            "Use Auto for 0 RPM stop."
        )

    c0, c1, c2 = custom_cpu[0], custom_cpu[1], custom_cpu[2]
    sil = modes[5]
    is_stock_tdp = (
        (c0.ac_pl1_w, c0.ac_pl2_w) == (25, 45)
        and (c1.ac_pl1_w, c1.ac_pl2_w) == (35, 45)
        and (c2.ac_pl1_w, c2.ac_pl2_w) == (55, 80)
        and (sil.ac_pl1_w, sil.ac_pl2_w) == (35, 45)
        and not custom_gpu_90w_levels
    )
    is_v2_exact = (
        all_unlocked_0_5500
        and (c0.ac_pl1_w, c0.ac_pl2_w) == (35, 45)
        and (c1.ac_pl1_w, c1.ac_pl2_w) == (60, 80)
        and (c2.ac_pl1_w, c2.ac_pl2_w) == (60, 90)
        and (sil.ac_pl1_w, sil.ac_pl2_w, sil.bat_pl1_w, sil.bat_pl2_w) == (25, 35, 25, 35)
        and custom_gpu_90w_levels == (0, 1)
    )
    if is_v2_exact:
        profile_tag = "v2 Mod (0–5500 RPM · Custom 35/45·60/80·60/90W · GPU 90W Low/Med · Silent 25/35W)"
    elif is_stock_tdp and all_unlocked_0_5500:
        profile_tag = "v1 Fan Mod (0–5500 RPM · Stock TDP)"
    elif is_stock_tdp and not all_unlocked_0_5500:
        profile_tag = "Stock EC v01.00"
    else:
        profile_tag = (
            f"Custom EC ({'0–5500 RPM' if all_unlocked_0_5500 else 'Custom RPM'} · "
            f"Custom {c0.ac_pl1_w}/{c0.ac_pl2_w}·{c1.ac_pl1_w}/{c1.ac_pl2_w}·{c2.ac_pl1_w}/{c2.ac_pl2_w}W · "
            f"Silent {sil.ac_pl1_w}/{sil.ac_pl2_w}W)"
        )

    gpu_note = (
        f"• GPU Power Cap (30W / 80W / 90W): Driven by EC mode + ACPI _Q30/_Q33 + NVIDIA GPS. "
        f"Gaming (1) and Custom (4) {'/'.join(custom_cpu[lv].name for lv in custom_gpu_90w_levels)} enforce 90 W (CTGP=1); "
        f"Balanced (0), Creator (2), and Custom (4) High enforce 80 W; Silent (5) caps GPU at 30 W (_QD5). "
        f"Custom GPU Boost (0x0D07 zone=2) controls the EC's 75°C (Low) vs 87°C (Med/High) GPU NDTT target."
        if custom_gpu_90w_levels
        else "• GPU Power Cap (30W / 80W / 90W): Driven by EC mode + ACPI _Q30/_Q33 + NVIDIA GPS (not Custom GPU Boost). "
        "Gaming (1) gives 90 W; Modes 0/2/4 allow 80 W; Silent (5) caps GPU at 30 W. Custom GPU Boost 0 vs 1/2 only changes the EC's 75°C vs 87°C GPU thermal target."
    )
    clamp_note = (
        "• Manual RPM Clamps (0x0D01): Unlocked to 0–5500 RPM across all modes (0 RPM = clean fan stop; 100–~1800 RPM sits below the 20% PWM spin floor; ~2000–5500 RPM runs smooth closed-loop PID). Auto curves remain stock."
        if all_unlocked_0_5500
        else f"• Manual RPM Clamps (0x0D01): EC clamps requested RPM to [min, max] per mode ({_fmt_clamp_span(modes[0].manual_min_rpm, modes[0].manual_max_rpm)} Balanced; {_fmt_clamp_span(modes[1].manual_min_rpm, modes[1].manual_max_rpm)} Gaming/Creator/Custom; {_fmt_clamp_span(modes[5].manual_min_rpm, modes[5].manual_max_rpm)} Silent). For 0 RPM fan stop, use Auto Fan (curve) in Silent (5) while temps stay below 60°C CPU / 45°C GPU / 43°C Chassis."
    )
    reference_notes = (
        f"• Custom Mode 4 is the only mode controlled by 0x0D07 Boost levels: "
        f"CPU Low (0)={c0.ac_pl1_w}/{c0.ac_pl2_w}W AC ({c0.gpu_limit_w}W GPU), "
        f"Medium (1)={c1.ac_pl1_w}/{c1.ac_pl2_w}W AC ({c1.gpu_limit_w}W GPU), "
        f"High (2)={c2.ac_pl1_w}/{c2.ac_pl2_w}W AC ({c2.gpu_limit_w}W GPU).",
        "• 0x0D02 Mode-Change Reset Quirk: Writing any 0x0D02 packet resets EC CPU & GPU Boost (0x808e0/0x808e1) back to Medium (1). This UI automatically re-sends 0x0D07 after 0x0D02 whenever Custom Mode 4 is active.",
        gpu_note,
        f"• CPU Power Arbitration: Silicon enforces MIN(Windows MSR 0x610, MMIO, EC PECI, DPTF). Lowering PL1/PL2 from Windows works cleanly because EC only re-sends PECI on mode/thermal/AC changes. Raising CPU PL above the active mode's PECI cap does not stick unless EC is in a higher mode (use Custom 4 High for {c2.ac_pl1_w}W/{c2.ac_pl2_w}W).",
        clamp_note,
        f"• Windows Turbo Boost vs EC Silent (5): Turbo Boost (Windows PERFBOOSTMODE / power plan) is independent of EC mode. Changing Turbo Boost here leaves Silent (5) active; only Performance.cmd resets EC back to Balanced (0). In Silent (5), EC enforces {sil.ac_pl1_w}W/{sil.ac_pl2_w}W AC and {sil.bat_pl1_w}W/{sil.bat_pl2_w}W Battery.",
        "• 0x0D88 live tachometer readback is unsupported on RZ09-0328 HID; Stored Target (0x0D81) shows the setpoint register only. Live GPU power draw & enforced power cap come directly from nvidia-smi.",
    )

    return EcProfile(
        source_kind=source_kind,
        source_label=source_label,
        profile_tag=profile_tag,
        has_tdp_readback=has_tdp_readback,
        modes=modes,
        custom_cpu=custom_cpu,
        custom_gpu=custom_gpu,
        custom_gpu_90w_levels=custom_gpu_90w_levels,
        reference_notes=reference_notes,
        clamp_summary_text=clamp_summary_text,
        custom_gpu_hint_text=custom_gpu_hint_text,
        cache_ver=cache_ver,
        defaults_ver=defaults_ver,
    )


def build_builtin_stock_profile() -> EcProfile:
    """Verified stock RZ09-0328 EC v01.00 (0x3d000 slots + 0x10070 + 0x0e0f0..0x0e592)."""
    modes = {
        0: ModeSpec(0, "Balanced", "", 35, 45, "30W PL1 thermal fallback", 25, 25, 80, 80, "0 or 2500–5300 RPM", 4300, 5500, 4900, "Default mode; manual RPM clamped to 4300–5500."),
        1: ModeSpec(1, "Gaming", "", 35, 60, "30W PL1 thermal fallback", 30, 30, 90, 87, "0 or 2700–5500 RPM", 4900, 5500, 5200, "Highest GPU power limit (90 W via ACPI _Q30 / GPS)."),
        2: ModeSpec(2, "Creator / Perf", "", 45, 60, "35/50W thermal fallback", 30, 30, 80, 87, "0 or 2700–5500 RPM", 4900, 5500, 5200, "Higher sustained CPU PL1 (45 W) than Balanced/Gaming; 80 W GPU."),
        3: ModeSpec(3, "Legacy / Mode 3", "", 35, 45, "35W PL1 thermal fallback", 25, 25, 80, 87, "0 or 2700–5500 RPM", None, 5500, 5200, "EC skips min RPM clamp at 0x1237a/0x123d4; PWM <36 (~20%) still snaps to 0."),
        4: ModeSpec(4, "Custom", "", 35, 45, "Selected via Low/Med/High sub-mode", 25, 25, 80, 87, "0 or 2700–5500 RPM", 4900, 5500, 5200, "Only mode honoring 0x0D07 sub-levels; High (2) gives 55W/80W CPU max."),
        5: ModeSpec(5, "Silent", "", 35, 45, "35W PL1 thermal fallback", 30, 30, 30, 97, "0 or 2200–3250 RPM (steps 0–7)", 2400, 5000, 3100, "30 W GPU cap (_QD5); easiest 0 RPM stop; lowest stock manual floor (2400 RPM)."),
        6: ModeSpec(6, "Quiet Alt (6)", "", 35, 45, "35W PL1 thermal fallback", 30, 30, 30, 97, "0 or 2200–3250 RPM (steps 0–7)", 2400, 5000, 3100, "Uses identical 0x3dc00 table to Mode 5."),
    }
    custom_cpu = {
        0: CustomCpuSpec(0, "Low", 25, 45, "PL2->35W if bit1 set", 20, 20, 80, ""),
        1: CustomCpuSpec(1, "Medium", 35, 45, "PL2->40W if bit1 set", 25, 25, 80, ""),
        2: CustomCpuSpec(2, "High", 55, 80, "PL1->35W if bit0 set", 30, 30, 80, ""),
    }
    return _compose_profile(
        source_kind="builtin_stock",
        source_label="Built-in Stock v01.00 Defaults",
        has_tdp_readback=True,
        modes_in=modes,
        custom_cpu_in=custom_cpu,
        custom_gpu_90w_levels=(),
        cache_ver=2,
        defaults_ver=7,
    )


def build_builtin_v2_profile() -> EcProfile:
    """Verified RZ09-0328 EC v2 Mod (0–5500 RPM, Custom TDP + 90W GPU Low/Med, Silent 25/35W AC/DC)."""
    modes = {
        0: ModeSpec(0, "Balanced", "", 35, 45, "30W PL1 thermal fallback", 25, 25, 80, 80, "0 or 2500–5300 RPM", 0, 5500, 4900, "Default mode; unlocked manual 0–5500 RPM (0 = full stop)."),
        1: ModeSpec(1, "Gaming", "", 35, 60, "30W PL1 thermal fallback", 30, 30, 90, 87, "0 or 2700–5500 RPM", 0, 5500, 5200, "90 W GPU via ACPI _Q30 / GPS; unlocked manual 0–5500 RPM."),
        2: ModeSpec(2, "Creator / Perf", "", 45, 60, "35/50W thermal fallback", 30, 30, 80, 87, "0 or 2700–5500 RPM", 0, 5500, 5200, "Sustained 45 W PL1 / 60 W PL2 CPU, 80 W GPU; unlocked manual 0–5500 RPM."),
        3: ModeSpec(3, "Legacy / Mode 3", "", 35, 45, "35W PL1 thermal fallback", 25, 25, 80, 87, "0 or 2700–5500 RPM", 0, 5500, 5200, "Unlocked manual 0–5500 RPM; PWM <36 (~20%) snaps to 0."),
        4: ModeSpec(4, "Custom", "", 60, 80, "Selected via Low/Med/High sub-mode", 25, 25, 90, 87, "0 or 2700–5500 RPM", 0, 5500, 5200, "Low 35/45W+90W GPU; Med 60/80W+90W GPU; High 60/90W+80W GPU; manual 0–5500."),
        5: ModeSpec(5, "Silent", "", 25, 35, "25/35W thermal fallback", 25, 35, 30, 97, "0 or 2200–3250 RPM (steps 0–7)", 0, 5500, 3100, "25/35W CPU (AC & DC), 30W GPU (_QD5), unlocked manual 0–5500 RPM."),
        6: ModeSpec(6, "Quiet Alt (6)", "", 25, 35, "25/35W thermal fallback", 25, 35, 30, 97, "0 or 2200–3250 RPM (steps 0–7)", 0, 5500, 3100, "Uses identical 0x3dc00 table to Mode 5 (25/35W AC/DC, manual 0–5500)."),
    }
    custom_cpu = {
        0: CustomCpuSpec(0, "Low", 35, 45, "PL2->35W if bit1 set", 20, 20, 90, ""),
        1: CustomCpuSpec(1, "Medium", 60, 80, "PL1->35W if bit1 set (0x0e162 fix)", 25, 25, 90, ""),
        2: CustomCpuSpec(2, "High", 60, 90, "PL1->35W if bit0 set (97°C)", 30, 30, 80, ""),
    }
    return _compose_profile(
        source_kind="builtin_v2",
        source_label="Built-in v2 Mod Defaults",
        has_tdp_readback=True,
        modes_in=modes,
        custom_cpu_in=custom_cpu,
        custom_gpu_90w_levels=(0, 1),
        cache_ver=4,
        defaults_ver=8,
    )


def _sbits(val: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (val ^ sign) - sign


def _decode_custom_pl_from_rom_10070(rom: bytes) -> dict[int, tuple[int, int, int, int]] | None:
    """Simulate the tiny NDS32 initializer at 0x10070..0x101C8 to extract 0x86900 Custom Low/Med/High PL."""
    if len(rom) < 0x101C8:
        return None
    sentinel = 0xDEADBEEF
    regs = [0] * 32
    regs[30] = sentinel
    regs[31] = 0x00083FF0
    mem = bytearray(0x00090000)
    pc = 0x10070
    for _ in range(2500):
        if pc == sentinel:
            break
        if not (0x10070 <= pc < 0x101C8):
            return None
        if rom[pc] & 0x80:
            hw = struct.unpack_from(">H", rom, pc)[0]
            npc = pc + 2
            if hw == 0x9200:
                pass
            elif hw == 0xDD9E:
                npc = regs[30]
            elif (hw & 0xFE00) == 0xFA00:  # movpi45 rt4, imm5 (+16)
                rt4 = _NDS32_R45MAP[(hw >> 5) & 15]
                regs[rt4] = (hw & 31) + 16
            elif (hw & 0xFC00) == 0x8400:  # movi55
                regs[(hw >> 5) & 31] = _sbits(hw & 31, 5) & 0xFFFFFFFF
            elif (hw & 0xFE00) == 0xAE00:  # sbi333 rt3, [ra3 + imm3]
                rt3 = (hw >> 6) & 7
                ra3 = (hw >> 3) & 7
                imm3 = hw & 7
                addr = (regs[ra3] + imm3) & 0xFFFFFFFF
                if addr < len(mem):
                    mem[addr] = regs[rt3] & 0xFF
            elif (hw & 0xFF80) == 0xFC00:  # push25
                pass
            elif (hw & 0xFF80) == 0xFC80:  # pop25
                npc = regs[30]
            else:
                return None
            pc = npc
        else:
            w = struct.unpack_from(">I", rom, pc)[0]
            npc = pc + 4
            op6 = (w >> 25) & 0x3F
            rt, ra = (w >> 20) & 31, (w >> 15) & 31
            imm15s = _sbits(w & 0x7FFF, 15)
            if op6 == 0x08:  # sbi
                addr = (regs[ra] + imm15s) & 0xFFFFFFFF
                if addr < len(mem):
                    mem[addr] = regs[rt] & 0xFF
            elif op6 == 0x22:  # movi
                regs[rt] = _sbits(w & 0xFFFFF, 20) & 0xFFFFFFFF
            elif op6 == 0x23:  # sethi
                regs[rt] = ((w & 0xFFFFF) << 12) & 0xFFFFFFFF
            elif op6 == 0x26:  # beq / bne
                is_bne = (w >> 14) & 1
                tgt = (pc + (_sbits(w & 0x3FFF, 14) << 1)) & 0xFFFFFFFF
                if (regs[rt] != regs[ra]) if is_bne else (regs[rt] == regs[ra]):
                    npc = tgt
            elif op6 == 0x28:  # addi
                regs[rt] = (regs[ra] + imm15s) & 0xFFFFFFFF
            else:
                return None
            pc = npc
    else:
        return None

    out: dict[int, tuple[int, int, int, int]] = {}
    for lv, off in ((0, 0x86900), (1, 0x86908), (2, 0x86910)):
        vals = tuple(int(mem[off + i]) for i in range(4))
        if not all(5 <= v <= 150 for v in vals):
            return None
        out[lv] = vals
    return out


def build_profile_from_rom_bytes(rom: bytes, source_label: str, bank_base: int = 0x3D000) -> EcProfile:
    """Decode all 7 mode slots, Custom 4 PL table (0x10070), and GPU CTGP handler from a 256 KiB EC ROM."""
    if len(rom) != 0x40000:
        raise ValueError(f"Expected 256 KiB (262144-byte) RZ09-0328 EC ROM, got {len(rom)} bytes")
    if b"ITE8390" not in rom[0x80:0xB0]:
        raise ValueError("ROM header at 0x00080..0x000AF does not contain ITE8390 signature")

    base = build_builtin_stock_profile()
    modes = dict(base.modes)
    for s in range(7):
        sb = bank_base + s * 0x200
        tail_off = struct.unpack_from("<H", rom, sb + 3)[0]
        if not (0x20 <= tail_off <= 0x1C0):
            tail_off = 0x1A8
        f = rom[sb + tail_off : sb + tail_off + 0x38]
        ac1, ac2 = int(f[0x18]), int(f[0x19])
        min_rpm = int(f[0x21]) * 100
        def_rpm = int(f[0x23]) * 100
        max_rpm = int(f[0x26]) * 100
        th1, th2 = int(f[0x2E]), int(f[0x31])
        ndtt = int(f[0x32])
        dc1, dc2 = int(f[0x33]), int(f[0x34])
        dgps = int(f[0x0B])
        gpu_w = 30 if dgps == 5 else (90 if s == 1 else 80)
        old = modes[s]
        eff_min: int | None = None if (s == 3 and min_rpm > 0) else min_rpm
        th_note = f"{th1}/{th2}W thermal fallback" if th2 < 80 else f"{th1}W PL1 thermal fallback"
        if s == 4:
            th_note = "Selected via Low/Med/High sub-mode"
        modes[s] = replace(
            old,
            ac_pl1_w=ac1,
            ac_pl2_w=ac2,
            ac_thermal_note=th_note,
            bat_pl1_w=dc1,
            bat_pl2_w=dc2,
            gpu_limit_w=gpu_w,
            gpu_temp_c=ndtt,
            manual_min_rpm=eff_min,
            manual_default_rpm=def_rpm,
            manual_max_rpm=max_rpm,
            notes=(
                f"{ac1}/{ac2}W AC ({dc1}/{dc2}W Bat), {gpu_w}W GPU; "
                f"manual {_fmt_clamp_span(eff_min, max_rpm)} RPM."
            ),
        )

    custom_cpu = dict(base.custom_cpu)
    decoded_custom = _decode_custom_pl_from_rom_10070(rom)
    if decoded_custom is not None:
        med_rule = "PL1->35W if bit1 set (0x0e162 fix)" if rom[0x0E162:0x0E164] == bytes.fromhex("fa 33") else "PL2->40W if bit1 set"
        for lv, (ac1, ac2, dc1, dc2) in decoded_custom.items():
            rule = med_rule if lv == 1 else custom_cpu[lv].ac_thermal_rule
            custom_cpu[lv] = replace(
                custom_cpu[lv],
                ac_pl1_w=ac1,
                ac_pl2_w=ac2,
                bat_pl1_w=dc1,
                bat_pl2_w=dc2,
                ac_thermal_rule=rule,
            )

    custom_gpu_90w: tuple[int, ...] = ()
    if rom[0x0E548:0x0E54C] == bytes.fromhex("58 10 80 80"):
        w = struct.unpack_from(">I", rom, 0x0E56E)[0]
        if ((w >> 25) & 0x3F) == 0x2E and ((w >> 15) & 0x1F) == 4:
            cutoff = w & 0x7FFF
            custom_gpu_90w = tuple(lv for lv in (0, 1, 2) if lv < cutoff)
        else:
            custom_gpu_90w = (0, 1)

    cache_ver = int(rom[0x12120]) if rom[0x1211E:0x12120] == b"\x5a\x08" else None
    defaults_ver = int(rom[0x105CE]) if rom[0x105CC:0x105CE] == b"\x5a\x00" else None

    return _compose_profile(
        source_kind="rom_file",
        source_label=source_label,
        has_tdp_readback=True,
        modes_in=modes,
        custom_cpu_in=custom_cpu,
        custom_gpu_90w_levels=custom_gpu_90w,
        cache_ver=cache_ver,
        defaults_ver=defaults_ver,
    )


def load_profile_from_rom(rom_path: Path) -> EcProfile:
    return build_profile_from_rom_bytes(rom_path.read_bytes(), source_label=rom_path.name)


def build_profile_from_probe_dict(data: dict, source_label: str) -> EcProfile:
    """Build an EcProfile from an ec_sio_probe.py --verify-live probe.json payload."""
    vl = data.get("verify_live")
    if not isinstance(vl, dict) or not isinstance(vl.get("summary"), dict):
        raise ValueError("JSON is not a verify-live EC probe (missing verify_live.summary)")
    summary = vl["summary"]
    base = build_builtin_stock_profile()
    modes = dict(base.modes)
    custom_cpu = dict(base.custom_cpu)

    rom_slots = summary.get("rom_slots")
    has_rom_slots = isinstance(rom_slots, dict) and len(rom_slots) >= 6
    if has_rom_slots:
        for s in range(7):
            sd = rom_slots.get(str(s))
            if not isinstance(sd, dict):
                continue
            ac1, ac2 = int(sd["ac_pl1_w"]), int(sd["ac_pl2_w"])
            dc1, dc2 = int(sd["dc_pl1_w"]), int(sd["dc_pl2_w"])
            th1, th2 = int(sd["throttle_pl1_w"]), int(sd["throttle_pl2_w"])
            min_rpm = int(sd["manual_min_rpm"])
            def_rpm = int(sd["manual_default_rpm"])
            max_rpm = int(sd["manual_max_rpm"])
            ndtt = int(sd["ndtt_c"])
            dgps = int(sd["dgps_level"])
            if not (5 <= ac1 <= 150 and 5 <= ac2 <= 150 and 0 <= min_rpm <= max_rpm <= 7000):
                continue
            gpu_w = 30 if dgps == 5 else (90 if s == 1 else 80)
            eff_min: int | None = None if (s == 3 and min_rpm > 0) else min_rpm
            th_note = f"{th1}/{th2}W thermal fallback" if th2 < 80 else f"{th1}W PL1 thermal fallback"
            if s == 4:
                th_note = "Selected via Low/Med/High sub-mode"
            modes[s] = replace(
                modes[s],
                ac_pl1_w=ac1,
                ac_pl2_w=ac2,
                ac_thermal_note=th_note,
                bat_pl1_w=dc1,
                bat_pl2_w=dc2,
                gpu_limit_w=gpu_w,
                gpu_temp_c=ndtt,
                manual_min_rpm=eff_min,
                manual_default_rpm=def_rpm,
                manual_max_rpm=max_rpm,
                notes=f"{ac1}/{ac2}W AC ({dc1}/{dc2}W Bat), {gpu_w}W GPU; manual {_fmt_clamp_span(eff_min, max_rpm)} RPM.",
            )
    else:
        fan_mod = bool(
            summary.get("rom_has_v1_or_v2_fan_clamps")
            or (summary.get("rom_has_mod_3d1c9") and summary.get("rom_has_mod_3dbc9"))
        )
        if fan_mod:
            for s in range(7):
                modes[s] = replace(
                    modes[s],
                    manual_min_rpm=0,
                    manual_max_rpm=5500,
                    notes=f"{modes[s].ac_pl1_w}/{modes[s].ac_pl2_w}W AC, {modes[s].gpu_limit_w}W GPU; manual 0–5500 RPM.",
                )

    sil_pl = summary.get("rom_silent_pl_ac_dc_w")
    has_sil_pl = isinstance(sil_pl, dict) and isinstance(sil_pl.get("ac"), list) and isinstance(sil_pl.get("dc"), list)
    if has_sil_pl and not has_rom_slots:
        sac1, sac2 = int(sil_pl["ac"][0]), int(sil_pl["ac"][1])
        sdc1, sdc2 = int(sil_pl["dc"][0]), int(sil_pl["dc"][1])
        if all(5 <= v <= 150 for v in (sac1, sac2, sdc1, sdc2)):
            for s in (5, 6):
                modes[s] = replace(
                    modes[s],
                    ac_pl1_w=sac1,
                    ac_pl2_w=sac2,
                    bat_pl1_w=sdc1,
                    bat_pl2_w=sdc2,
                    ac_thermal_note=f"{sac1}/{sac2}W thermal fallback",
                    notes=f"{sac1}/{sac2}W AC ({sdc1}/{sdc2}W Bat), 30W GPU; manual {_fmt_clamp_span(modes[s].manual_min_rpm, modes[s].manual_max_rpm)} RPM.",
                )

    sram_custom = summary.get("sram_custom_pl_w")
    has_custom_pl = isinstance(sram_custom, dict) and all(k in sram_custom for k in ("low", "med", "high"))
    if has_custom_pl:
        for lv, key in ((0, "low"), (1, "med"), (2, "high")):
            entry = sram_custom[key]
            ac = entry.get("ac", [])
            dc = entry.get("dc", [])
            if len(ac) == 2 and len(dc) == 2 and all(5 <= int(v) <= 150 for v in (*ac, *dc)):
                rule = custom_cpu[lv].ac_thermal_rule
                if lv == 1 and int(ac[0]) >= 50:
                    rule = "PL1->35W if bit1 set (0x0e162 fix)"
                elif lv == 2 and int(ac[1]) >= 85:
                    rule = "PL1->35W if bit0 set (97°C)"
                custom_cpu[lv] = replace(
                    custom_cpu[lv],
                    ac_pl1_w=int(ac[0]),
                    ac_pl2_w=int(ac[1]),
                    bat_pl1_w=int(dc[0]),
                    bat_pl2_w=int(dc[1]),
                    ac_thermal_rule=rule,
                )

    custom_gpu_90w: tuple[int, ...] = ()
    if isinstance(summary.get("rom_custom_gpu_90w_cpu_levels"), list):
        custom_gpu_90w = tuple(int(x) for x in summary["rom_custom_gpu_90w_cpu_levels"] if int(x) in (0, 1, 2))
    elif summary.get("rom_has_v2_gpu_ctgp_handler"):
        custom_gpu_90w = (0, 1)

    cache_ver = summary.get("sram_cache_version_3fe000")
    if cache_ver is None:
        cache_ver = summary.get("sram_cache_version")
    defaults_ver = summary.get("sram_defaults_version_050000")
    has_tdp_readback = bool(has_custom_pl and (has_rom_slots or has_sil_pl))

    return _compose_profile(
        source_kind="live_probe",
        source_label=source_label,
        has_tdp_readback=has_tdp_readback,
        modes_in=modes,
        custom_cpu_in=custom_cpu,
        custom_gpu_90w_levels=custom_gpu_90w,
        cache_ver=int(cache_ver) if cache_ver is not None else None,
        defaults_ver=int(defaults_ver) if defaults_ver is not None else None,
    )


def load_profile_from_probe(probe_path: Path) -> EcProfile:
    data = json.loads(probe_path.read_text(encoding="utf-8"))
    label = f"{probe_path.parent.name}/{probe_path.name}" if probe_path.parent.name.startswith("verify-live-") else probe_path.name
    return build_profile_from_probe_dict(data, source_label=label)


def find_latest_live_probe(repo_root: Path, require_tdp_readback: bool = True) -> tuple[Path, EcProfile] | None:
    diag_dir = repo_root / "diagnostics"
    if not diag_dir.is_dir():
        return None
    candidates = sorted(
        diag_dir.glob("verify-live-*/probe.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for probe_path in candidates:
        try:
            prof = load_profile_from_probe(probe_path)
        except Exception:
            continue
        if require_tdp_readback and not prof.has_tdp_readback:
            continue
        return probe_path, prof
    return None


def autodetect_startup_profile(
    repo_root: Path,
    rom_path: Path | None = None,
    probe_path: Path | None = None,
    stock_defaults: bool = False,
    v2_defaults: bool = False,
) -> EcProfile:
    if rom_path is not None:
        return load_profile_from_rom(rom_path)
    if probe_path is not None:
        return load_profile_from_probe(probe_path)
    if v2_defaults:
        return build_builtin_v2_profile()
    if stock_defaults:
        return build_builtin_stock_profile()
    found = find_latest_live_probe(repo_root, require_tdp_readback=True)
    if found is not None:
        return found[1]
    return build_builtin_stock_profile()


STOCK_PROFILE = build_builtin_stock_profile()
DEFAULT_PROFILE = build_builtin_v2_profile()
MODE_SPECS: dict[int, ModeSpec] = DEFAULT_PROFILE.modes
CUSTOM_CPU_MODES: dict[int, CustomCpuSpec] = DEFAULT_PROFILE.custom_cpu
CUSTOM_GPU_MODES: dict[int, CustomGpuSpec] = DEFAULT_PROFILE.custom_gpu

PRIMARY_MODES = (0, 1, 2, 5)
SECONDARY_MODES = (3, 6)
MODE_LABELS = {code: f"{spec.name} ({code})" for code, spec in MODE_SPECS.items()}
ALLOWED_MODES = frozenset(MODE_SPECS.keys())
ALLOWED_ZONES = frozenset({1, 2})
ALLOWED_PREFIXES = frozenset({0, 1})

# Auto fan step tables decoded verbatim from 0x3d000..0x3ddff (13-byte records at +0x08).
# Balanced (Slot 0) & Gaming/Creator/Custom (Slots 1-4): steps 0..9 (0x4a >> 3 = 9).
# Silent (Slots 5-6): steps 0..7 (0x3a >> 3 = 7).
AUTO_CURVE_ROWS: tuple[tuple[str, ...], ...] = (
    ("0 (Stop)", "0 RPM (CPU<55, GPU<54, Ch<39°C)", "0 RPM (CPU<55, GPU<55, Ch<39°C)", "0 RPM (CPU<60, GPU<45, Ch<43°C)"),
    ("1", "2500 RPM (50–60 / 50–58 / 36–41°C)", "2700 RPM (50–59 / 50–59 / 36–41°C)", "2200 RPM (55–65 / 40–50 / 40–46°C)"),
    ("2", "2800 RPM (55–65 / 54–62 / 38–43°C)", "3000 RPM (54–63 / 54–63 / 38–43°C)", "2400 RPM (60–70 / 45–55 / 43–49°C)"),
    ("3", "3100 RPM (60–70 / 58–66 / 40–45°C)", "3300 RPM (58–67 / 58–67 / 40–45°C)", "2500 RPM (65–75 / 50–60 / 46–52°C)"),
    ("4", "3400 RPM (65–75 / 62–70 / 42–47°C)", "3600 RPM (62–71 / 62–71 / 42–47°C)", "2700 RPM (70–80 / 55–65 / 49–255°C)"),
    ("5", "3700 RPM (70–80 / 66–74 / 44–49°C)", "3900 RPM (66–75 / 65–74 / 44–49°C)", "2900 RPM (75–85 / 60–70 / --)"),
    ("6", "4100 RPM (75–85 / 70–78 / 46–51°C)", "4300 RPM (70–79 / 68–77 / 46–51°C)", "3100 RPM (80–90 / 65–75 / --)"),
    ("7", "4500 RPM (80–89 / 74–82 / 48–53°C)", "4700 RPM (74–83 / 71–80 / 48–53°C)", "3250 RPM (85–255 / 70–255 / --)"),
    ("8", "4900 RPM (84–93 / 78–86 / 50–55°C)", "5100 RPM (78–87 / 74–83 / 50–55°C)", "(max_step=7 in Silent; stops at 7)"),
    ("9", "5300 RPM (88–255 / 82–255 / 52–255°C)", "5500 RPM (82–255 / 77–255 / 52–255°C)", "(flash +0x70/+0x7d unused above 7)"),
)

REFERENCE_NOTES: tuple[str, ...] = DEFAULT_PROFILE.reference_notes


def clamp_manual_rpm(mode: int, requested_rpm: int, profile: EcProfile | None = None) -> tuple[int, int | None, int]:
    """Return (effective_rpm, min_rpm_or_none, max_rpm) matching EC 0x12370..0x12392."""
    modes = profile.modes if profile is not None else MODE_SPECS
    spec = modes.get(mode, modes[0])
    rpm = max(0, min(7000, int(round(requested_rpm / 100.0)) * 100))
    eff = min(rpm, spec.manual_max_rpm)
    if spec.manual_min_rpm is not None and eff < spec.manual_min_rpm:
        eff = spec.manual_min_rpm
    return eff, spec.manual_min_rpm, spec.manual_max_rpm


def describe_effective_behavior(
    mode: int,
    manual: bool,
    rpm: int,
    cpu_boost: int,
    gpu_boost: int,
    profile: EcProfile | None = None,
    live_gpu_limit_w: float | None = None,
) -> str:
    """Return a concise single-line summary of what the EC enforces for the given state."""
    prof = profile if profile is not None else DEFAULT_PROFILE
    spec = prof.modes.get(mode)
    if spec is None:
        return f"Unknown mode {mode}"
    if mode == 4:
        c = prof.custom_cpu.get(cpu_boost, prof.custom_cpu[1])
        g = prof.custom_gpu.get(gpu_boost, prof.custom_gpu[1])
        exp_gpu_w = c.gpu_limit_w
        power_txt = (
            f"Custom (4) -> CPU {c.name} ({c.level}): {c.ac_pl1_w}W/{c.ac_pl2_w}W AC "
            f"({c.bat_pl1_w}W/{c.bat_pl2_w}W Bat) | GPU {exp_gpu_w}W ({g.temp_target_c}°C target)"
        )
    else:
        exp_gpu_w = spec.gpu_limit_w
        power_txt = (
            f"{spec.name} ({spec.code}): CPU {spec.ac_pl1_w}W/{spec.ac_pl2_w}W AC "
            f"({spec.bat_pl1_w}W/{spec.bat_pl2_w}W Bat) | GPU {exp_gpu_w}W ({spec.gpu_temp_c}°C target)"
        )
    if live_gpu_limit_w is not None:
        live_int = int(round(live_gpu_limit_w))
        if abs(live_gpu_limit_w - exp_gpu_w) >= 5.0:
            power_txt += f" [Live GPU {live_int}W ≠ profile {exp_gpu_w}W]"
        else:
            power_txt += f" [Live GPU {live_int}W ✓]"
    if manual:
        eff, min_rpm, max_rpm = clamp_manual_rpm(mode, rpm, profile=prof)
        floor_txt = f"{min_rpm}" if min_rpm is not None else "none"
        fan_txt = f"Manual Req {rpm} -> EC Effective {eff} RPM (clamp {floor_txt}–{max_rpm})"
    else:
        fan_txt = f"Auto Curve ({spec.auto_range})"
    return f"{power_txt}  |  {fan_txt}"


def build_packet(command: int, arguments: tuple[int, ...], length: int) -> bytes:
    """Validate command/argument bounds and frame a Razer HID feature report."""
    arguments = tuple(int(a) for a in arguments)
    if not MIN_REPORT <= length <= 4096:
        raise ValueError(f"Unexpected HID report length: {length}")
    _validate_command(command, arguments)
    report = bytearray(length)
    report[2] = TRANSACTION
    report[6] = len(arguments)
    report[7:9] = command.to_bytes(2, "big")
    report[9:9 + len(arguments)] = bytes(arguments)
    report[89] = checksum(report)
    return bytes(report)


def _validate_command(command: int, arguments: tuple[int, ...]) -> None:
    if command == 0x0081 and arguments == (0, 0):
        return
    if command == 0x078F and arguments == (0,):
        return
    if command == 0x0D85 and arguments == (0, 0, 0):
        return
    if command == 0x0D82 and len(arguments) == 4 and arguments[0] == 0 and arguments[1] in ALLOWED_ZONES and arguments[2:] == (0, 0):
        return
    if command == 0x0D81 and len(arguments) == 3 and arguments[0] == 0 and arguments[1] in ALLOWED_ZONES and arguments[2] == 0:
        return
    if command == 0x0D87 and len(arguments) == 3 and arguments[0] == 0 and arguments[1] in ALLOWED_ZONES and arguments[2] == 0:
        return
    if command == 0x0D02 and len(arguments) == 4:
        prefix, zone, mode, fan_flag = arguments
        if prefix in ALLOWED_PREFIXES and zone in ALLOWED_ZONES and mode in ALLOWED_MODES and fan_flag in (0, 1):
            return
    if command == 0x0D01 and len(arguments) == 3:
        prefix, zone, rpm_hundreds = arguments
        if prefix in ALLOWED_PREFIXES and zone in ALLOWED_ZONES and 0 <= rpm_hundreds <= 70:
            return
    if command == 0x0D07 and len(arguments) == 3:
        prefix, cluster, level = arguments
        if prefix in ALLOWED_PREFIXES and cluster in ALLOWED_ZONES and 0 <= level <= 4:
            return
    raise ValueError(f"Disallowed command/arguments: 0x{command:04X} {arguments}")


@dataclass(frozen=True)
class ZoneSnapshot:
    zone: int
    mode: int
    fan_flag: int
    stored_rpm: int


@dataclass(frozen=True)
class NvidiaTelemetry:
    temp_c: int | None
    power_draw_w: float | None
    enforced_limit_w: float | None
    max_limit_w: float | None
    pstate: str
    display: str


@dataclass(frozen=True)
class BladeSnapshot:
    endpoint: str
    fw_version: str
    zones: tuple[ZoneSnapshot, ZoneSnapshot]
    cpu_ec_c: int
    gpu_ec_c: int
    cpu_boost: int
    gpu_boost: int
    max_fan_flag: int
    gpu_telem: str
    gpu_enforced_limit_w: float | None = None
    gpu_max_limit_w: float | None = None


def _parse_optional_float(token: str) -> float | None:
    t = token.strip().replace("W", "").strip()
    if not t or "N/A" in t.upper():
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_nvidia_csv(output: str) -> NvidiaTelemetry | None:
    """Parse `nvidia-smi --query-gpu=temperature.gpu,power.draw,enforced.power.limit,power.max_limit,pstate --format=csv,noheader,nounits`."""
    line = next((ln.strip() for ln in output.splitlines() if ln.strip()), "")
    if not line:
        return None
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return NvidiaTelemetry(None, None, None, None, "", line)
    temp_f = _parse_optional_float(parts[0])
    temp_c = int(round(temp_f)) if temp_f is not None else None
    draw_w = _parse_optional_float(parts[1])
    enf_w = _parse_optional_float(parts[2])
    if len(parts) >= 5:
        max_w = _parse_optional_float(parts[3])
        pstate = parts[4]
    else:
        max_w = None
        pstate = parts[3]
    segs: list[str] = []
    if temp_c is not None:
        segs.append(f"{temp_c}°C")
    if draw_w is not None and enf_w is not None:
        segs.append(f"{draw_w:.1f}W / {enf_w:.0f}W cap")
    elif enf_w is not None:
        segs.append(f"{enf_w:.0f}W cap")
    elif draw_w is not None:
        segs.append(f"{draw_w:.1f}W")
    if max_w is not None:
        segs.append(f"max {max_w:.0f}W")
    if pstate:
        segs.append(pstate)
    return NvidiaTelemetry(
        temp_c=temp_c,
        power_draw_w=draw_w,
        enforced_limit_w=enf_w,
        max_limit_w=max_w,
        pstate=pstate,
        display=", ".join(segs) if segs else line,
    )


def query_nvidia_telemetry_parsed() -> NvidiaTelemetry | None:
    """Query structured NVIDIA temp/draw/enforced-cap/max-cap/pstate, or None if unavailable."""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu,power.draw,enforced.power.limit,power.max_limit,pstate",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=2.5,
            creationflags=flags,
        ).strip()
        return parse_nvidia_csv(out)
    except Exception:
        return None


def query_nvidia_telemetry() -> str:
    """Return compact NVIDIA temp/power/limit text, or an explanatory fallback."""
    parsed = query_nvidia_telemetry_parsed()
    return parsed.display if parsed is not None else "nvidia-smi unavailable"


@dataclass(frozen=True)
class WindowsPowerStatus:
    scheme_guid: str
    scheme_name: str
    ac_boost: int | None
    dc_boost: int | None
    known_guids: frozenset[str]


_GUID_RE = r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
_BOOST_LABELS = {
    0: "Disabled (OFF)",
    1: "Enabled",
    2: "Aggressive (ON)",
    3: "Efficient Enabled",
    4: "Efficient Aggressive",
    5: "Aggressive At Guaranteed",
    6: "Efficient Aggressive At Guaranteed",
}


def _run_powercfg(args: list[str]) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    raw = subprocess.check_output(
        ["powercfg", *args],
        stderr=subprocess.STDOUT,
        timeout=3.0,
        creationflags=flags,
    )
    return raw.decode("mbcs", errors="replace") if sys.platform == "win32" else raw.decode("utf-8", errors="replace")


def load_saved_preset_guids() -> tuple[str, str]:
    """Return (boost_on_plan_guid, boost_off_plan_guid) from diagnostics/presets/state.json or defaults."""
    state_path = Path(__file__).resolve().parent.parent / "diagnostics" / "presets" / "state.json"
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        orig = str(data.get("OriginalGuid", "")).lower()
        quiet = str(data.get("QuietGuid", "")).lower()
        if re.fullmatch(_GUID_RE, orig) and re.fullmatch(_GUID_RE, quiet):
            return orig, quiet
    except Exception:
        pass
    return DEFAULT_HIGH_PERF_GUID, DEFAULT_QUIET_PLAN_GUID


def parse_active_scheme(output: str) -> tuple[str, str]:
    """Parse (guid, display_name) from locale-agnostic powercfg /getactivescheme output."""
    m = re.search(rf"({_GUID_RE})\s*\((.*)\)", output, re.IGNORECASE)
    if m:
        return m.group(1).lower(), m.group(2).strip()
    m_guid = re.search(_GUID_RE, output, re.IGNORECASE)
    return (m_guid.group(0).lower(), "Unknown Plan") if m_guid else ("", "Unknown")


def parse_boost_indices(output: str) -> tuple[int | None, int | None]:
    """Parse (ac_index, dc_index) from locale-agnostic powercfg /qh PERFBOOSTMODE output."""
    vals = re.findall(r"0x([0-9a-f]{8})", output, re.IGNORECASE)
    if len(vals) < 2:
        return None, None
    return int(vals[-2], 16), int(vals[-1], 16)


def parse_scheme_guids(output: str) -> frozenset[str]:
    return frozenset(g.lower() for g in re.findall(_GUID_RE, output, re.IGNORECASE))


def query_power_status() -> WindowsPowerStatus:
    """Query active Windows power plan and AC/DC PERFBOOSTMODE without mutating state."""
    active_out = _run_powercfg(["/getactivescheme"])
    guid, name = parse_active_scheme(active_out)
    qh_out = _run_powercfg(["/qh", "SCHEME_CURRENT", SUB_PROCESSOR_GUID, PERFBOOSTMODE_GUID])
    ac_boost, dc_boost = parse_boost_indices(qh_out)
    list_out = _run_powercfg(["/list"])
    return WindowsPowerStatus(
        scheme_guid=guid,
        scheme_name=name,
        ac_boost=ac_boost,
        dc_boost=dc_boost,
        known_guids=parse_scheme_guids(list_out),
    )


def set_turbo_boost(enabled: bool) -> WindowsPowerStatus:
    """Toggle Windows CPU Turbo Boost without altering any EC HID mode.

    If switching between the saved High Performance and Blade Quiet plans cleanly
    applies the requested state, switches plans; otherwise updates PERFBOOSTMODE
    (0 = Disabled, 2 = Aggressive) directly on SCHEME_CURRENT.
    """
    status = query_power_status()
    on_guid, off_guid = load_saved_preset_guids()
    target_val = "2" if enabled else "0"

    if enabled and off_guid and status.scheme_guid == off_guid and on_guid in status.known_guids:
        _run_powercfg(["/setactive", on_guid])
        updated = query_power_status()
        if updated.ac_boost != 0:
            return updated
    elif (not enabled) and off_guid and status.scheme_guid == on_guid and off_guid in status.known_guids:
        _run_powercfg(["/setactive", off_guid])
        updated = query_power_status()
        if updated.ac_boost == 0:
            return updated

    _run_powercfg(["/setacvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR_GUID, PERFBOOSTMODE_GUID, target_val])
    _run_powercfg(["/setdcvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR_GUID, PERFBOOSTMODE_GUID, target_val])
    _run_powercfg(["/setactive", "SCHEME_CURRENT"])
    return query_power_status()


def build_tray_icon_png(size: int = 32) -> bytes:
    """Render a crisp 2x2-supersampled Razer-green fan emblem as RGBA PNG bytes."""

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    half = size / 2.0
    two_pi = 2.0 * math.pi
    sector = two_pi / 3.0
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # PNG filter type 0 (None)
        for x in range(size):
            ar = ag = ab = aa = 0.0
            for sy in (0.25, 0.75):
                for sx in (0.25, 0.75):
                    dx = ((x + sx) - half) / half
                    dy = ((y + sy) - half) / half
                    r = math.hypot(dx, dy)
                    if r > 0.96:
                        continue
                    if r >= 0.80:
                        pr, pg, pb, pa = 68, 214, 44, 255  # Razer green ring
                    elif r <= 0.11:
                        pr, pg, pb, pa = 107, 240, 85, 255  # Center dot
                    elif r <= 0.22:
                        pr, pg, pb, pa = 20, 26, 29, 255  # Dark hub
                    else:
                        theta = (math.atan2(dy, dx) - 0.65 * r) % sector
                        if 0.22 < r <= 0.72 and theta < 0.98:
                            if r > 0.52:
                                pr, pg, pb, pa = 92, 232, 70, 255
                            else:
                                pr, pg, pb, pa = 68, 214, 44, 255
                        else:
                            pr, pg, pb, pa = 20, 26, 29, 255
                    ar += pr * (pa / 255.0)
                    ag += pg * (pa / 255.0)
                    ab += pb * (pa / 255.0)
                    aa += pa
            alpha = int(round(aa / 4.0))
            if alpha > 0:
                scale = aa / 255.0
                raw.extend((
                    min(255, int(round(ar / scale))),
                    min(255, int(round(ag / scale))),
                    min(255, int(round(ab / scale))),
                    min(255, alpha),
                ))
            else:
                raw.extend((0, 0, 0, 0))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b"")


def format_tray_tooltip(
    snap: BladeSnapshot | None,
    pw: WindowsPowerStatus | None,
    profile: EcProfile | None = None,
) -> str:
    """Build a concise <=127-character Windows tray hover tooltip."""
    prof = profile if profile is not None else DEFAULT_PROFILE
    parts: list[str] = []
    if snap is not None:
        z0 = snap.zones[0]
        if z0.mode == 4:
            cpu_name = prof.custom_cpu.get(snap.cpu_boost, prof.custom_cpu[1]).name
            mode_txt = f"Custom (4) {cpu_name}"
        else:
            mode_txt = prof.mode_label(z0.mode)
        if z0.fan_flag == 1:
            eff, _, _ = clamp_manual_rpm(z0.mode, z0.stored_rpm, profile=prof)
            fan_txt = f"Manual {eff}"
        else:
            fan_txt = "Auto"
        parts.append(f"Razer Blade: {mode_txt} | {fan_txt}")
        parts.append(f"EC Temps: CPU {snap.cpu_ec_c}°C / GPU {snap.gpu_ec_c}°C")
    else:
        parts.append("Razer Blade Controller")
    if pw is not None and pw.ac_boost is not None:
        parts.append(f"Turbo Boost: {'OFF' if pw.ac_boost == 0 else 'ON'}")
    return "\n".join(parts)[:127]


class WindowsTrayIcon:
    """Native Win32 system tray icon & context menu using ctypes (zero third-party deps)."""

    _WM_DESTROY = 0x0002
    _WM_CLOSE = 0x0010
    _WM_APP = 0x8000
    _WM_TRAYICON = _WM_APP + 1
    _WM_TRAY_SHOW = _WM_APP + 2
    _WM_TRAY_HIDE = _WM_APP + 3
    _WM_TRAY_TIP = _WM_APP + 4

    _WM_LBUTTONUP = 0x0202
    _WM_LBUTTONDBLCLK = 0x0203
    _WM_RBUTTONUP = 0x0205
    _WM_CONTEXTMENU = 0x007B

    _NIM_ADD = 0x00000000
    _NIM_MODIFY = 0x00000001
    _NIM_DELETE = 0x00000002

    _NIF_MESSAGE = 0x00000001
    _NIF_ICON = 0x00000002
    _NIF_TIP = 0x00000004

    _MF_STRING = 0x00000000
    _MF_CHECKED = 0x00000008
    _MF_SEPARATOR = 0x00000800

    _TPM_LEFTALIGN = 0x0000
    _TPM_RIGHTBUTTON = 0x0002
    _TPM_BOTTOMALIGN = 0x0020
    _TPM_NONOTIFY = 0x0080
    _TPM_RETURNCMD = 0x0100

    _SMTO_ABORTIFHUNG = 0x0002
    _IDI_APPLICATION = 32512

    def __init__(
        self,
        on_restore: Callable[[], None],
        on_command: Callable[[int], None],
        icon_png: bytes,
    ):
        self._on_restore = on_restore
        self._on_command = on_command
        self._icon_png = icon_png
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._hicon = None
        self._owns_hicon = False
        self._visible = False
        self._running = False
        self._class_name = f"RazerBladeTray_{os.getpid()}_{id(self)}"
        self._msg_taskbar_created = 0
        self._wndproc_ref = None

        self._tooltip = "Razer Blade Controller"
        self._mode: int | None = None
        self._fan_flag: int | None = None
        self._cpu_boost: int | None = None
        self._turbo_on: bool | None = None
        self._menu_labels: dict[int, str] = DEFAULT_PROFILE.tray_labels()

        self._api_ready = False
        self._init_win32_types()

    @property
    def is_running(self) -> bool:
        return bool(self._running and self._hwnd)

    @property
    def has_icon(self) -> bool:
        return bool(self._hicon)

    def _init_win32_types(self) -> None:
        if sys.platform != "win32":
            return
        from ctypes import wintypes as wt

        lresult_t = ctypes.c_ssize_t
        ulong_ptr_t = ctypes.c_size_t
        wndproc_t = ctypes.WINFUNCTYPE(lresult_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wt.DWORD),
                ("Data2", wt.WORD),
                ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class _NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.DWORD),
                ("hWnd", wt.HWND),
                ("uID", wt.UINT),
                ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT),
                ("hIcon", wt.HICON),
                ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD),
                ("dwStateMask", wt.DWORD),
                ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT),
                ("szInfoTitle", wt.WCHAR * 64),
                ("dwInfoFlags", wt.DWORD),
                ("guidItem", _GUID),
                ("hBalloonIcon", wt.HICON),
            ]

        class _WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.UINT),
                ("style", wt.UINT),
                ("lpfnWndProc", wndproc_t),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HANDLE),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR),
                ("hIconSm", wt.HICON),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wt.HMODULE

        user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
        user32.RegisterWindowMessageW.restype = wt.UINT
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
        user32.RegisterClassExW.restype = wt.ATOM
        user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
        user32.UnregisterClassW.restype = wt.BOOL
        user32.CreateWindowExW.argtypes = [
            wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
        ]
        user32.CreateWindowExW.restype = wt.HWND
        user32.DestroyWindow.argtypes = [wt.HWND]
        user32.DestroyWindow.restype = wt.BOOL
        user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.DefWindowProcW.restype = lresult_t
        user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
        user32.TranslateMessage.restype = wt.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        user32.DispatchMessageW.restype = lresult_t
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.PostQuitMessage.restype = None
        user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.PostMessageW.restype = wt.BOOL
        user32.SendMessageTimeoutW.argtypes = [
            wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT, ctypes.POINTER(ulong_ptr_t)
        ]
        user32.SendMessageTimeoutW.restype = lresult_t
        user32.CreateIconFromResourceEx.argtypes = [
            ctypes.c_void_p, wt.DWORD, wt.BOOL, wt.DWORD, ctypes.c_int, ctypes.c_int, wt.UINT
        ]
        user32.CreateIconFromResourceEx.restype = wt.HICON
        user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
        user32.LoadIconW.restype = wt.HICON
        user32.DestroyIcon.argtypes = [wt.HICON]
        user32.DestroyIcon.restype = wt.BOOL
        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = wt.HMENU
        user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ulong_ptr_t, wt.LPCWSTR]
        user32.AppendMenuW.restype = wt.BOOL
        user32.SetMenuDefaultItem.argtypes = [wt.HMENU, wt.UINT, wt.UINT]
        user32.SetMenuDefaultItem.restype = wt.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        user32.GetCursorPos.restype = wt.BOOL
        user32.SetForegroundWindow.argtypes = [wt.HWND]
        user32.SetForegroundWindow.restype = wt.BOOL
        user32.TrackPopupMenu.argtypes = [
            wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID
        ]
        user32.TrackPopupMenu.restype = wt.UINT
        user32.DestroyMenu.argtypes = [wt.HMENU]
        user32.DestroyMenu.restype = wt.BOOL

        shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wt.BOOL

        self._wt = wt
        self._ulong_ptr_t = ulong_ptr_t
        self._wndproc_t = wndproc_t
        self._NOTIFYICONDATAW = _NOTIFYICONDATAW
        self._WNDCLASSEXW = _WNDCLASSEXW
        self._kernel32 = kernel32
        self._user32 = user32
        self._shell32 = shell32
        self._api_ready = True

    def start(self) -> bool:
        if not self._api_ready:
            return False
        if self._running and self._hwnd:
            return True
        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, name="BladeTray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return bool(self._running and self._hwnd)

    def _send_timeout(self, msg: int) -> tuple[bool, int]:
        hwnd = self._hwnd
        if not hwnd or not self._running:
            return False, 0
        res = self._ulong_ptr_t(0)
        ok = self._user32.SendMessageTimeoutW(
            hwnd, msg, 0, 0, self._SMTO_ABORTIFHUNG, 1500, ctypes.byref(res)
        )
        return bool(ok), int(res.value)

    def show(self) -> bool:
        if not self.start():
            return False
        ok, val = self._send_timeout(self._WM_TRAY_SHOW)
        return bool(ok and val)

    def hide(self) -> None:
        if self._running and self._hwnd:
            self._send_timeout(self._WM_TRAY_HIDE)

    def update_state(
        self,
        tooltip: str,
        mode: int | None = None,
        fan_flag: int | None = None,
        cpu_boost: int | None = None,
        turbo_on: bool | None = None,
        menu_labels: dict[int, str] | None = None,
    ) -> None:
        with self._lock:
            self._tooltip = tooltip[:127]
            self._mode = mode
            self._fan_flag = fan_flag
            self._cpu_boost = cpu_boost
            self._turbo_on = turbo_on
            if menu_labels is not None:
                self._menu_labels = dict(menu_labels)
        if self._running and self._hwnd and self._visible:
            self._user32.PostMessageW(self._hwnd, self._WM_TRAY_TIP, 0, 0)

    def stop(self) -> None:
        if not self._api_ready:
            return
        thread = self._thread
        hwnd = self._hwnd
        if hwnd and self._running:
            ok, _ = self._send_timeout(self._WM_CLOSE)
            if not ok:
                self._user32.PostMessageW(hwnd, self._WM_CLOSE, 0, 0)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def _create_hicon(self) -> tuple[int | None, bool]:
        if self._icon_png:
            buf = ctypes.create_string_buffer(self._icon_png, len(self._icon_png))
            hicon = self._user32.CreateIconFromResourceEx(
                ctypes.byref(buf), len(self._icon_png), True, 0x00030000, 32, 32, 0
            )
            if hicon:
                return hicon, True
        fallback = self._user32.LoadIconW(None, ctypes.cast(self._IDI_APPLICATION, self._wt.LPCWSTR))
        return (fallback if fallback else None), False

    def _make_nid(self, flags: int) -> object:
        nid = self._NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(self._NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = flags
        if flags & self._NIF_MESSAGE:
            nid.uCallbackMessage = self._WM_TRAYICON
        if flags & self._NIF_ICON:
            nid.hIcon = self._hicon
        if flags & self._NIF_TIP:
            with self._lock:
                nid.szTip = self._tooltip[:127]
        return nid

    def _add_icon_unlocked(self) -> bool:
        if not self._hwnd or not self._hicon:
            return False
        nid = self._make_nid(self._NIF_MESSAGE | self._NIF_ICON | self._NIF_TIP)
        ok = bool(self._shell32.Shell_NotifyIconW(self._NIM_ADD, ctypes.byref(nid)))
        if ok:
            self._visible = True
        return ok

    def _modify_tip_unlocked(self) -> bool:
        if not self._hwnd or not self._visible:
            return False
        nid = self._make_nid(self._NIF_TIP)
        return bool(self._shell32.Shell_NotifyIconW(self._NIM_MODIFY, ctypes.byref(nid)))

    def _delete_icon_unlocked(self) -> None:
        if self._hwnd and self._visible:
            nid = self._make_nid(0)
            self._shell32.Shell_NotifyIconW(self._NIM_DELETE, ctypes.byref(nid))
        self._visible = False

    def _show_context_menu(self, hwnd) -> None:
        with self._lock:
            mode = self._mode
            fan_flag = self._fan_flag
            cpu_boost = self._cpu_boost
            turbo_on = self._turbo_on
            labels = dict(self._menu_labels)

        hmenu = self._user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            def _item(cmd: int, label: str, checked: bool = False) -> None:
                flags = self._MF_STRING | (self._MF_CHECKED if checked else 0)
                self._user32.AppendMenuW(hmenu, flags, cmd, label)

            def _sep() -> None:
                self._user32.AppendMenuW(hmenu, self._MF_SEPARATOR, 0, None)

            _item(TRAY_CMD_OPEN, "Open Razer Blade Controller")
            self._user32.SetMenuDefaultItem(hmenu, TRAY_CMD_OPEN, 0)
            _sep()
            _item(TRAY_CMD_MODE_SILENT, labels[TRAY_CMD_MODE_SILENT], mode == 5)
            _item(TRAY_CMD_MODE_BALANCED, labels[TRAY_CMD_MODE_BALANCED], mode == 0)
            _item(TRAY_CMD_MODE_GAMING, labels[TRAY_CMD_MODE_GAMING], mode == 1)
            _item(TRAY_CMD_MODE_CREATOR, labels[TRAY_CMD_MODE_CREATOR], mode == 2)
            _item(TRAY_CMD_MODE_CUSTOM_LOW, labels[TRAY_CMD_MODE_CUSTOM_LOW], mode == 4 and cpu_boost == 0)
            _item(TRAY_CMD_MODE_CUSTOM_MED, labels[TRAY_CMD_MODE_CUSTOM_MED], mode == 4 and cpu_boost == 1)
            _item(TRAY_CMD_MODE_CUSTOM_HIGH, labels[TRAY_CMD_MODE_CUSTOM_HIGH], mode == 4 and cpu_boost == 2)
            _sep()
            _item(TRAY_CMD_FAN_AUTO, "Fan: Auto Curve", fan_flag == 0)
            _sep()
            _item(TRAY_CMD_TURBO_ON, "Turbo Boost: ON (Aggressive)", turbo_on is True)
            _item(TRAY_CMD_TURBO_OFF, "Turbo Boost: OFF (Base Clock)", turbo_on is False)
            _sep()
            _item(TRAY_CMD_EXIT, "Exit")

            pt = self._wt.POINT()
            self._user32.GetCursorPos(ctypes.byref(pt))
            self._user32.SetForegroundWindow(hwnd)
            cmd = self._user32.TrackPopupMenu(
                hmenu,
                self._TPM_LEFTALIGN | self._TPM_BOTTOMALIGN | self._TPM_RIGHTBUTTON | self._TPM_RETURNCMD | self._TPM_NONOTIFY,
                pt.x,
                pt.y,
                0,
                hwnd,
                None,
            )
            self._user32.PostMessageW(hwnd, 0, 0, 0)
        finally:
            self._user32.DestroyMenu(hmenu)

        if cmd:
            self._on_command(int(cmd))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == self._WM_TRAYICON:
            evt = lparam & 0xFFFF
            if evt in (self._WM_LBUTTONUP, self._WM_LBUTTONDBLCLK):
                self._on_restore()
                return 0
            if evt in (self._WM_RBUTTONUP, self._WM_CONTEXTMENU):
                self._show_context_menu(hwnd)
                return 0
        elif msg == self._WM_TRAY_SHOW:
            if self._visible:
                self._modify_tip_unlocked()
                return 1
            return 1 if self._add_icon_unlocked() else 0
        elif msg == self._WM_TRAY_HIDE:
            self._delete_icon_unlocked()
            return 1
        elif msg == self._WM_TRAY_TIP:
            self._modify_tip_unlocked()
            return 1
        elif msg == self._WM_CLOSE:
            self._delete_icon_unlocked()
            self._user32.DestroyWindow(hwnd)
            return 0
        elif msg == self._WM_DESTROY:
            self._delete_icon_unlocked()
            self._user32.PostQuitMessage(0)
            return 0
        elif self._msg_taskbar_created and msg == self._msg_taskbar_created:
            if self._visible:
                self._visible = False
                self._add_icon_unlocked()
            return 0
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _thread_main(self) -> None:
        hinstance = self._kernel32.GetModuleHandleW(None)
        atom = 0
        try:
            self._hicon, self._owns_hicon = self._create_hicon()
            self._msg_taskbar_created = self._user32.RegisterWindowMessageW("TaskbarCreated")
            self._wndproc_ref = self._wndproc_t(self._wndproc)

            wc = self._WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(self._WNDCLASSEXW)
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinstance
            wc.lpszClassName = self._class_name
            atom = self._user32.RegisterClassExW(ctypes.byref(wc))
            if not atom:
                return

            hwnd = self._user32.CreateWindowExW(
                0, self._class_name, "RazerBladeTrayHost", 0, 0, 0, 0, 0, None, None, hinstance, None
            )
            if not hwnd:
                return

            self._hwnd = hwnd
            self._running = True
            self._ready.set()

            msg = self._wt.MSG()
            while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                self._user32.TranslateMessage(ctypes.byref(msg))
                self._user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._delete_icon_unlocked()
            self._running = False
            self._hwnd = None
            if self._hicon and self._owns_hicon:
                self._user32.DestroyIcon(self._hicon)
            self._hicon = None
            self._owns_hicon = False
            if atom:
                self._user32.UnregisterClassW(self._class_name, hinstance)
            self._wndproc_ref = None
            self._ready.set()


class BladeHidClient:
    """Serialized HID feature-report client for Razer Blade USB HID (default 1532:0255)."""

    def __init__(self, pid: int = 0x0255):
        self.pid = int(pid)
        self._native = NativeHid()
        self._lock = threading.Lock()
        self._handle = None
        self._length = 0
        self.endpoint = ""

    def connect(self) -> str:
        with self._lock:
            self._close_unlocked()
            last_error: Exception | None = None
            pid_tag = f"pid_{self.pid:04x}"
            for candidate in self._native.paths(pid=self.pid):
                if not candidate or "vid_1532" not in candidate.lower() or pid_tag not in candidate.lower():
                    continue
                handle = None
                try:
                    handle = self._native.open(candidate)
                    caps = self._native.caps(handle)
                    if caps["FeatureReportByteLength"] < MIN_REPORT:
                        continue
                    self._handle = handle
                    self._length = caps["FeatureReportByteLength"]
                    self.endpoint = candidate
                    handle = None
                    return candidate
                except OSError as exc:
                    last_error = exc
                finally:
                    if handle is not None:
                        self._native.kernel.CloseHandle(handle)
            if last_error is not None:
                raise last_error
            raise RuntimeError(f"No Razer Blade 1532:{self.pid:04X} HID interface with >=91-byte feature report found")

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        if self._handle is not None:
            self._native.kernel.CloseHandle(self._handle)
            self._handle = None
            self._length = 0

    def exchange(self, command: int, arguments: tuple[int, ...], retries: int = 3) -> dict:
        with self._lock:
            if self._handle is None:
                raise RuntimeError("HID device not connected")
            return self._exchange_unlocked(command, arguments, retries=retries)

    def _exchange_unlocked(self, command: int, arguments: tuple[int, ...], retries: int = 3) -> dict:
        import ctypes as C

        sent = build_packet(command, arguments, self._length)
        last: dict = {}
        for attempt in range(1, retries + 1):
            out_buf = C.create_string_buffer(sent, len(sent))
            time.sleep(0.001)
            if not self._native.hid.HidD_SetFeature(self._handle, out_buf, len(sent)):
                raise OSError(f"HidD_SetFeature failed: {self._native.error()}")
            time.sleep(0.003)
            in_buf = C.create_string_buffer(self._length)
            if not self._native.hid.HidD_GetFeature(self._handle, in_buf, self._length):
                raise OSError(f"HidD_GetFeature failed: {self._native.error()}")
            last = describe_reply(in_buf.raw, command, sent)
            if last.get("valid"):
                return last
            if last.get("valid_envelope") and last.get("status") == 5:
                raise RuntimeError(f"Command 0x{command:04X} not supported by controller")
            if attempt < retries:
                time.sleep(0.025 if last.get("status") == 1 else 0.150)
        raise RuntimeError(f"Command 0x{command:04X} failed: status={last.get('status')} checksum={last.get('checksum_valid')}")

    def read_snapshot(self, include_gpu: bool = True) -> BladeSnapshot:
        with self._lock:
            if self._handle is None:
                raise RuntimeError("HID device not connected")
            fw = self._exchange_unlocked(0x0081, (0, 0))
            fw_args = fw["args"]
            fw_str = f"{fw_args[0]:02d}.{fw_args[1]:02d}" if len(fw_args) >= 2 else str(fw_args)
            zones = []
            for zone in (1, 2):
                m = self._exchange_unlocked(0x0D82, (0, zone, 0, 0))["all_argument_bytes"]
                r = self._exchange_unlocked(0x0D81, (0, zone, 0))["all_argument_bytes"]
                zones.append(ZoneSnapshot(zone=zone, mode=m[2], fan_flag=m[3], stored_rpm=r[2] * 100))
            temps = self._exchange_unlocked(0x0D85, (0, 0, 0))["all_argument_bytes"]
            b_cpu = self._exchange_unlocked(0x0D87, (0, 1, 0))["all_argument_bytes"][2]
            b_gpu = self._exchange_unlocked(0x0D87, (0, 2, 0))["all_argument_bytes"][2]
            max_fan = self._exchange_unlocked(0x078F, (0,))["all_argument_bytes"][0]
        nv = query_nvidia_telemetry_parsed() if include_gpu else None
        gpu_telem = nv.display if nv is not None else ("nvidia-smi unavailable" if include_gpu else "")
        return BladeSnapshot(
            endpoint=self.endpoint,
            fw_version=fw_str,
            zones=(zones[0], zones[1]),
            cpu_ec_c=temps[1],
            gpu_ec_c=temps[2],
            cpu_boost=b_cpu,
            gpu_boost=b_gpu,
            max_fan_flag=max_fan,
            gpu_telem=gpu_telem,
            gpu_enforced_limit_w=nv.enforced_limit_w if nv is not None else None,
            gpu_max_limit_w=nv.max_limit_w if nv is not None else None,
        )

    def _apply_fan_settings_unlocked(
        self,
        zones: tuple[int, ...],
        mode: int,
        manual: bool,
        rpm: int,
        prefix: int,
        cpu_boost: int | None = None,
        gpu_boost: int | None = None,
    ) -> list[dict]:
        """Execute 0x0D02/0x0D01 and (for Custom mode 4) re-apply 0x0D07 sub-levels."""
        rpm_byte = max(0, min(70, int(round(rpm / 100))))
        flag = 1 if manual else 0
        events: list[dict] = []
        for zone in zones:
            if manual:
                pre = self._exchange_unlocked(0x0D82, (0, zone, 0, 0))
                events.append({"cmd": "0x0D82", "zone": zone, "reply": pre["args"]})
            mode_reply = self._exchange_unlocked(0x0D02, (prefix, zone, mode, flag))
            events.append({"cmd": "0x0D02", "req": [prefix, zone, mode, flag], "reply": mode_reply["args"]})
            if manual:
                rpm_reply = self._exchange_unlocked(0x0D01, (prefix, zone, rpm_byte))
                events.append({"cmd": "0x0D01", "req": [prefix, zone, rpm_byte], "reply": rpm_reply["args"]})
        # EC handler 0x12cb0 resets 0x808e0/0x808e1 to 1 on every 0x0D02 write.
        if mode == 4:
            if cpu_boost is not None:
                c_lvl = max(0, min(2, int(cpu_boost)))
                rep = self._exchange_unlocked(0x0D07, (prefix, 1, c_lvl))
                events.append({"cmd": "0x0D07", "req": [prefix, 1, c_lvl], "reply": rep["args"]})
            if gpu_boost is not None:
                g_lvl = max(0, min(2, int(gpu_boost)))
                rep = self._exchange_unlocked(0x0D07, (prefix, 2, g_lvl))
                events.append({"cmd": "0x0D07", "req": [prefix, 2, g_lvl], "reply": rep["args"]})
        return events

    def apply_fan_settings(
        self,
        zones: tuple[int, ...],
        mode: int,
        manual: bool,
        rpm: int,
        prefix: int,
        cpu_boost: int | None = None,
        gpu_boost: int | None = None,
    ) -> list[dict]:
        """Apply performance mode, Auto/Manual flag, Manual RPM, and Custom sub-modes."""
        with self._lock:
            if self._handle is None:
                raise RuntimeError("HID device not connected")
            return self._apply_fan_settings_unlocked(
                zones, mode, manual, rpm, prefix, cpu_boost=cpu_boost, gpu_boost=gpu_boost
            )

    def apply_boost(self, prefix: int, cpu_level: int, gpu_level: int) -> list[dict]:
        c_lvl = max(0, min(2, int(cpu_level)))
        g_lvl = max(0, min(2, int(gpu_level)))
        events: list[dict] = []
        with self._lock:
            if self._handle is None:
                raise RuntimeError("HID device not connected")
            for cluster, level in ((1, c_lvl), (2, g_lvl)):
                rep = self._exchange_unlocked(0x0D07, (prefix, cluster, level))
                events.append({"cmd": "0x0D07", "req": [prefix, cluster, level], "reply": rep["args"]})
        return events

    def restore_exact(self, startup: BladeSnapshot, prefix: int = 1) -> None:
        with self._lock:
            if self._handle is None:
                return
            for zs in startup.zones:
                self._exchange_unlocked(0x0D02, (prefix, zs.zone, zs.mode, zs.fan_flag))
                self._exchange_unlocked(0x0D01, (prefix, zs.zone, min(70, zs.stored_rpm // 100)))
            self._exchange_unlocked(0x0D07, (prefix, 1, startup.cpu_boost))
            self._exchange_unlocked(0x0D07, (prefix, 2, startup.gpu_boost))


def run_live_ec_probe(repo_root: Path) -> Path:
    """Invoke read-only ec_research/ec_sio_probe.py --verify-live (with UAC if needed) and return new probe.json."""
    if sys.platform != "win32":
        raise RuntimeError("Live EC silicon probe requires Windows")
    probe_script = repo_root / "ec_research" / "ec_sio_probe.py"
    if not probe_script.is_file():
        raise FileNotFoundError(f"Probe script not found: {probe_script}")
    diag_dir = repo_root / "diagnostics"
    before = {p: p.stat().st_mtime for p in diag_dir.glob("verify-live-*/probe.json")} if diag_dir.is_dir() else {}

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    is_admin = bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
    if is_admin:
        proc = subprocess.run(
            [sys.executable, str(probe_script), "--verify-live"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=90,
            creationflags=flags,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"ec_sio_probe.py --verify-live exited {proc.returncode}: {detail[:300]}")
    else:
        py_esc = sys.executable.replace("'", "''")
        wd_esc = str(repo_root).replace("'", "''")
        ps_cmd = (
            f"$p = Start-Process -FilePath '{py_esc}' "
            f"-ArgumentList 'ec_research\\ec_sio_probe.py --verify-live' "
            f"-WorkingDirectory '{wd_esc}' -Verb RunAs -PassThru -Wait; "
            f"exit $p.ExitCode"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=flags,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"Elevated live EC probe cancelled or failed (exit {proc.returncode}): {detail[:300]}")

    after = sorted(
        diag_dir.glob("verify-live-*/probe.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if diag_dir.is_dir() else []
    for p in after:
        if p not in before or p.stat().st_mtime > before[p]:
            return p
    if after:
        return after[0]
    raise RuntimeError("Live EC probe finished without writing diagnostics/verify-live-*/probe.json")


class FanTesterApp:
    """Tkinter desktop UI for RZ09-0328 EC mode control and reference tables."""

    def __init__(self, root: tk.Tk, auto_connect: bool = True, ec_profile: EcProfile | None = None, pid: int = 0x0255):
        self.root = root
        self.pid = int(pid)
        self.root.title(f"Razer Blade RZ09-0328 (HID 1532:{self.pid:04X}) — Mode, Power & Fan Controller")
        self.root.geometry("1040x920")
        self.root.minsize(920, 800)

        self._repo_root = Path(__file__).resolve().parent.parent
        self.ec_profile: EcProfile = (
            ec_profile if ec_profile is not None else autodetect_startup_profile(self._repo_root)
        )

        self._icon_png = build_tray_icon_png(32)
        self._window_icon: tk.PhotoImage | None = None
        try:
            self._window_icon = tk.PhotoImage(data=base64.b64encode(self._icon_png).decode("ascii"))
            self.root.iconphoto(True, self._window_icon)
        except tk.TclError:
            self._window_icon = None

        self.client = BladeHidClient(pid=self.pid)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="BladeHid")
        self.ui_queue: queue.Queue = queue.Queue()
        self.startup_snapshot: BladeSnapshot | None = None
        self.latest_snapshot: BladeSnapshot | None = None
        self.latest_power: WindowsPowerStatus | None = None
        self._busy = False
        self._poll_job: str | None = None
        self._drain_job: str | None = None
        self._tray_hide_job: str | None = None
        self._poll_ticks = 0
        self._in_tray = False
        self._closing = False
        self._restore_guard_until = 0.0
        self._tray: WindowsTrayIcon | None = (
            WindowsTrayIcon(
                on_restore=lambda: self._post_ui(self._restore_from_tray),
                on_command=lambda cmd: self._post_ui(lambda c=cmd: self._on_tray_command(c)),
                icon_png=self._icon_png,
            )
            if sys.platform == "win32"
            else None
        )

        log_dir = self._repo_root / "diagnostics" / "hid"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.action_log_path = log_dir / f"gui-actions-{datetime.now():%Y%m%d-%H%M%S}.jsonl"

        self.selected_mode = tk.IntVar(value=5)
        self.fan_manual = tk.BooleanVar(value=False)
        self.rpm_var = tk.IntVar(value=2400)
        self.zone_choice = tk.StringVar(value="both")
        self.prefix_var = tk.IntVar(value=0)
        self.auto_apply_slider = tk.BooleanVar(value=True)
        self.restore_on_exit = tk.BooleanVar(value=True)
        self.minimize_to_tray = tk.BooleanVar(value=True)
        self.cpu_boost_var = tk.IntVar(value=1)
        self.gpu_boost_var = tk.IntVar(value=1)

        self._build_ui()
        self._apply_ec_profile(self.ec_profile, log_change=False)
        self._update_rpm_readout()
        self._update_button_highlights()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Unmap>", self._on_unmap)
        self._drain_job = self.root.after(50, self._drain_ui_queue)
        if auto_connect:
            self._connect_and_init()
        else:
            self.conn_var.set("Offline / UI Preview Mode (auto_connect=False)")

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        ctrl_tab = ttk.Frame(notebook, padding=10)
        ref_tab = ttk.Frame(notebook, padding=10)
        notebook.add(ctrl_tab, text="Controller & Modes")
        notebook.add(ref_tab, text="Modes & Watt Tables")

        self._build_control_tab(ctrl_tab)
        self._build_reference_tab(ref_tab)

    def _build_control_tab(self, parent: ttk.Frame) -> None:
        # Header & Live Status
        hdr = ttk.LabelFrame(parent, text="Controller Status & Live Readback (EC v01.00)", padding=8)
        hdr.pack(fill="x", pady=(0, 8))

        self.conn_var = tk.StringVar(value=f"Connecting to 1532:{self.pid:04X}...")
        top_row = ttk.Frame(hdr)
        top_row.pack(fill="x")
        ttk.Label(top_row, textvariable=self.conn_var, font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Button(top_row, text="Refresh Now", command=self._refresh_now).pack(side="right")

        prof_row = ttk.Frame(hdr)
        prof_row.pack(fill="x", pady=(4, 0))
        self.profile_var = tk.StringVar(value="EC Profile: detecting...")
        ttk.Label(prof_row, textvariable=self.profile_var, foreground="#2c5f2d", font=("Segoe UI", 9, "bold")).pack(side="left")
        self.load_prof_btn = ttk.Button(prof_row, text="Load ROM / Probe...", command=self._on_load_profile_file)
        self.load_prof_btn.pack(side="right", padx=(4, 0))
        self.probe_ec_btn = ttk.Button(prof_row, text="Probe Live EC (Admin)", command=self._on_probe_live_ec)
        self.probe_ec_btn.pack(side="right")

        self.z1_var = tk.StringVar(value="Zone 1 (CPU): --")
        self.z2_var = tk.StringVar(value="Zone 2 (GPU): --")
        self.telem_var = tk.StringVar(value="Temps & Telemetry: --")
        self.effect_var = tk.StringVar(value="Effective EC Profile: --")
        ttk.Label(hdr, textvariable=self.z1_var).pack(anchor="w", pady=(5, 0))
        ttk.Label(hdr, textvariable=self.z2_var).pack(anchor="w", pady=(2, 0))
        ttk.Label(hdr, textvariable=self.telem_var).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            hdr,
            textvariable=self.effect_var,
            font=("Segoe UI", 9, "bold"),
            foreground="#0b4f8a",
        ).pack(anchor="w", pady=(4, 0))

        # Windows CPU Turbo Boost Switch (independent from EC HID modes)
        tb_frame = ttk.LabelFrame(
            parent,
            text="Windows CPU Turbo Boost (Independent of EC Mode — keeps Silent 5 / Custom 4)",
            padding=8,
        )
        tb_frame.pack(fill="x", pady=(0, 8))
        tb_row = ttk.Frame(tb_frame)
        tb_row.pack(fill="x")
        self.power_var = tk.StringVar(value="Windows Power Plan: checking...")
        ttk.Label(tb_row, textvariable=self.power_var, font=("Segoe UI", 9, "bold")).pack(side="left")
        self.turbo_off_btn = tk.Button(
            tb_row,
            text="Turbo Boost: OFF (Base Clock)",
            cursor="hand2",
            padx=10,
            command=lambda: self._on_turbo_click(False),
        )
        self.turbo_off_btn.pack(side="right", padx=(4, 0))
        self.turbo_on_btn = tk.Button(
            tb_row,
            text="Turbo Boost: ON (Aggressive)",
            cursor="hand2",
            padx=10,
            command=lambda: self._on_turbo_click(True),
        )
        self.turbo_on_btn.pack(side="right", padx=4)

        # 1. Fixed EC Performance Modes
        mode_frame = ttk.LabelFrame(parent, text="1. Standard EC Modes (click to apply immediately)", padding=8)
        mode_frame.pack(fill="x", pady=(0, 8))

        btn_grid = ttk.Frame(mode_frame)
        btn_grid.pack(fill="x")
        self.mode_buttons: dict[int, tk.Button] = {}
        for col, code in enumerate(PRIMARY_MODES):
            spec = self.ec_profile.modes[code]
            btn = tk.Button(
                btn_grid,
                text=f"{spec.name} ({code})\n{spec.short_hint}",
                width=18,
                height=3,
                relief="raised",
                cursor="hand2",
                command=lambda c=code: self._on_mode_click(c),
            )
            btn.grid(row=0, column=col, padx=4, pady=2, sticky="ew")
            btn_grid.columnconfigure(col, weight=1)
            self.mode_buttons[code] = btn

        sec_row = ttk.Frame(mode_frame)
        sec_row.pack(fill="x", pady=(6, 0))
        ttk.Label(sec_row, text="Additional EC slots:", foreground="#555555").pack(side="left", padx=(0, 6))
        for code in SECONDARY_MODES:
            spec = self.ec_profile.modes[code]
            btn = tk.Button(
                sec_row,
                text=f"{spec.name} · CPU {spec.ac_pl1_w}/{spec.ac_pl2_w}W",
                relief="raised",
                cursor="hand2",
                padx=8,
                command=lambda c=code: self._on_mode_click(c),
            )
            btn.pack(side="left", padx=4)
            self.mode_buttons[code] = btn
        ttk.Label(
            sec_row,
            text="(Any 0x0D02 mode change resets EC boost to Med 1)",
            foreground="#666666",
        ).pack(side="right")

        # 2. Custom Mode 4 Individual Sub-Modes
        custom_frame = ttk.LabelFrame(
            parent,
            text="2. Custom Mode (4) — Individual 3 CPU Power Modes + GPU Boost (automatically sets Mode 4 + 0x0D07)",
            padding=8,
        )
        custom_frame.pack(fill="x", pady=(0, 8))

        cpu_grid = ttk.Frame(custom_frame)
        cpu_grid.pack(fill="x")
        self.custom_cpu_buttons: dict[int, tk.Button] = {}
        for col, level in enumerate((0, 1, 2)):
            c_spec = self.ec_profile.custom_cpu[level]
            btn = tk.Button(
                cpu_grid,
                text=c_spec.button_label,
                height=3,
                relief="raised",
                cursor="hand2",
                command=lambda lv=level: self._on_custom_cpu_click(lv),
            )
            btn.grid(row=0, column=col, padx=4, pady=2, sticky="ew")
            cpu_grid.columnconfigure(col, weight=1)
            self.custom_cpu_buttons[level] = btn

        gpu_row = ttk.Frame(custom_frame)
        gpu_row.pack(fill="x", pady=(6, 0))
        ttk.Label(gpu_row, text="Custom GPU Boost (0x0D07):", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 6))
        self.custom_gpu_buttons: dict[int, tk.Button] = {}
        for level in (0, 1, 2):
            g_spec = self.ec_profile.custom_gpu[level]
            btn = tk.Button(
                gpu_row,
                text=g_spec.label,
                relief="raised",
                cursor="hand2",
                padx=8,
                command=lambda lv=level: self._on_custom_gpu_click(lv),
            )
            btn.pack(side="left", padx=3)
            self.custom_gpu_buttons[level] = btn
        self.custom_gpu_hint_var = tk.StringVar(value=self.ec_profile.custom_gpu_hint_text)
        ttk.Label(
            gpu_row,
            textvariable=self.custom_gpu_hint_var,
            foreground="#555555",
        ).pack(side="right")

        # 3. Fan Mode, Zones, Prefix & Clamped RPM Control
        rpm_frame = ttk.LabelFrame(parent, text="3. Fan Mode & Manual RPM Target", padding=8)
        rpm_frame.pack(fill="x", pady=(0, 8))

        ctrl_row = ttk.Frame(rpm_frame)
        ctrl_row.pack(fill="x", pady=(0, 6))
        ttk.Label(ctrl_row, text="Fan Mode:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 6))
        self.auto_btn = tk.Button(
            ctrl_row, text="Auto (Curve)", width=13, cursor="hand2",
            command=lambda: self._on_fan_mode_click(False),
        )
        self.auto_btn.pack(side="left", padx=2)
        self.manual_btn = tk.Button(
            ctrl_row, text="Manual (Fixed)", width=13, cursor="hand2",
            command=lambda: self._on_fan_mode_click(True),
        )
        self.manual_btn.pack(side="left", padx=(2, 16))

        ttk.Label(ctrl_row, text="Zones:").pack(side="left", padx=(0, 4))
        for val, lbl in (("both", "Both (1 & 2)"), ("1", "Zone 1"), ("2", "Zone 2")):
            ttk.Radiobutton(ctrl_row, text=lbl, value=val, variable=self.zone_choice).pack(side="left", padx=3)

        ttk.Label(ctrl_row, text="Prefix:").pack(side="left", padx=(12, 4))
        ttk.Radiobutton(ctrl_row, text="0 (Synapse)", value=0, variable=self.prefix_var).pack(side="left", padx=2)
        ttk.Radiobutton(ctrl_row, text="1 (VarStore)", value=1, variable=self.prefix_var).pack(side="left", padx=2)

        slider_row = ttk.Frame(rpm_frame)
        slider_row.pack(fill="x", pady=(2, 4))
        self.rpm_readout = tk.StringVar(value="")
        ttk.Label(slider_row, textvariable=self.rpm_readout, font=("Segoe UI", 9, "bold"), width=42).pack(side="left")

        self.rpm_scale = tk.Scale(
            slider_row,
            from_=0,
            to=MAX_RPM,
            resolution=STEP_RPM,
            orient="horizontal",
            tickinterval=500,
            showvalue=False,
            variable=self.rpm_var,
            command=self._on_slider_move,
        )
        self.rpm_scale.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.rpm_scale.bind("<ButtonRelease-1>", self._on_slider_release)
        ttk.Button(slider_row, text="Apply Manual RPM", command=self._apply_manual_rpm).pack(side="right")

        preset_row = ttk.Frame(rpm_frame)
        preset_row.pack(fill="x", pady=(4, 0))
        ttk.Label(preset_row, text="Quick RPM:").pack(side="left", padx=(0, 6))
        for val in (0, 2000, 2400, 2700, 3000, 3500, 4000, 4500, 4900, 5200, 5500):
            lbl = "0 (Off)" if val == 0 else str(val)
            w = 6 if val == 0 else 5
            ttk.Button(preset_row, text=lbl, width=w, command=lambda v=val: self._quick_rpm(v)).pack(side="left", padx=2)
        ttk.Checkbutton(preset_row, text="Auto-apply on release", variable=self.auto_apply_slider).pack(side="right")

        self.clamp_hint_var = tk.StringVar(value=self.ec_profile.clamp_summary_text)
        ttk.Label(
            rpm_frame,
            textvariable=self.clamp_hint_var,
            foreground="#555555",
        ).pack(anchor="w", pady=(4, 0))

        # 4. Safety Restore & Log
        restore_frame = ttk.LabelFrame(parent, text="4. Safety Restore, Tray & Exit Behavior", padding=8)
        restore_frame.pack(fill="x", pady=(0, 8))
        r_row = ttk.Frame(restore_frame)
        r_row.pack(fill="x")
        ttk.Checkbutton(
            r_row,
            text="Restore startup EC state when closing window (keeps Windows Turbo Boost)",
            variable=self.restore_on_exit,
        ).pack(side="left")
        ttk.Button(r_row, text="Restore Silent + Auto (5)", command=lambda: self._restore_preset(5)).pack(side="right", padx=2)
        ttk.Button(r_row, text="Restore Balanced + Auto (0)", command=lambda: self._restore_preset(0)).pack(side="right", padx=2)
        ttk.Button(r_row, text="Restore Startup", command=self._restore_startup).pack(side="right", padx=2)

        r_row2 = ttk.Frame(restore_frame)
        r_row2.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(
            r_row2,
            text="Minimize to system tray (left-click tray icon to restore, right-click for quick modes)",
            variable=self.minimize_to_tray,
        ).pack(side="left")
        self.tray_btn = ttk.Button(r_row2, text="Minimize to Tray", command=self._hide_to_tray)
        self.tray_btn.pack(side="right", padx=2)

        log_frame = ttk.LabelFrame(parent, text="Command & Readback Log", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_reference_tab(self, parent: ttk.Frame) -> None:
        canvas = tk.Canvas(parent, highlightthickness=0)
        v_scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=v_scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        v_scroll.pack(side="right", fill="y")

        inner = ttk.Frame(canvas, padding=4)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_id, width=e.width))

        def _on_wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        # Section A: Mode & Wattage Table
        self._ref_t1_frame = ttk.LabelFrame(
            inner,
            text="Table 1 — RZ09-0328 EC Modes, Power Limits (AC / Battery), GPU Caps & Fan Bounds",
            padding=8,
        )
        self._ref_t1_frame.pack(fill="x", pady=(0, 10))

        cols1 = ("mode", "ac_cpu", "bat_cpu", "gpu", "gpu_t", "auto_fan", "manual_clamp", "notes")
        headers1 = (
            ("mode", "Mode / Code", 145),
            ("ac_cpu", "AC CPU PL1/PL2", 105),
            ("bat_cpu", "Bat CPU", 80),
            ("gpu", "GPU Cap", 65),
            ("gpu_t", "GPU Tgt", 65),
            ("auto_fan", "Auto Fan Range", 160),
            ("manual_clamp", "Manual Clamp", 145),
            ("notes", "Notes", 240),
        )
        self._ref_t1_tree = self._make_table(self._ref_t1_frame, cols1, headers1, self._build_t1_rows(self.ec_profile))

        # Section B: Auto Fan Step Table
        t2_frame = ttk.LabelFrame(
            inner,
            text="Table 2 — EC Auto Fan Stop Thresholds & Step Curves (Down–Up°C for CPU / GPU / Chassis)",
            padding=8,
        )
        t2_frame.pack(fill="x", pady=(0, 10))
        cols2 = ("step", "bal", "perf", "silent")
        headers2 = (
            ("step", "Step", 70),
            ("bal", "Balanced (0)", 250),
            ("perf", "Gaming (1) / Creator (2) / Custom (4)", 260),
            ("silent", "Silent (5) / Alt (6)", 250),
        )
        self._make_table(t2_frame, cols2, headers2, AUTO_CURVE_ROWS)

        # Section C: Behavior Notes
        self._ref_notes_frame = ttk.LabelFrame(inner, text="Motherboard Arbitration & Firmware Quirk Summary", padding=8)
        self._ref_notes_frame.pack(fill="x")
        self._populate_reference_notes(self.ec_profile)

    @staticmethod
    def _build_t1_rows(profile: EcProfile) -> list[tuple[str, ...]]:
        rows1: list[tuple[str, ...]] = []
        for code in (0, 1, 2, 3):
            s = profile.modes[code]
            floor = _fmt_clamp_span(s.manual_min_rpm, s.manual_max_rpm)
            rows1.append((
                f"{s.name} ({code})",
                f"{s.ac_pl1_w}W / {s.ac_pl2_w}W",
                f"{s.bat_pl1_w}W / {s.bat_pl2_w}W",
                f"{s.gpu_limit_w} W",
                f"{s.gpu_temp_c} °C",
                s.auto_range,
                f"{floor} (def {s.manual_default_rpm})",
                s.notes,
            ))
        c4 = profile.modes[4]
        c4_floor = _fmt_clamp_span(c4.manual_min_rpm, c4.manual_max_rpm)
        for lv in (0, 1, 2):
            cs = profile.custom_cpu[lv]
            rows1.append((
                f"Custom (4) {cs.name} ({lv})",
                f"{cs.ac_pl1_w}W / {cs.ac_pl2_w}W",
                f"{cs.bat_pl1_w}W / {cs.bat_pl2_w}W",
                f"{cs.gpu_limit_w} W",
                "75°C / 87°C",
                c4.auto_range,
                f"{c4_floor} (def {c4.manual_default_rpm})",
                f"0x0D07 CPU={lv} ({cs.ac_thermal_rule})",
            ))
        for code in (5, 6):
            s = profile.modes[code]
            floor = _fmt_clamp_span(s.manual_min_rpm, s.manual_max_rpm)
            rows1.append((
                f"{s.name} ({code})",
                f"{s.ac_pl1_w}W / {s.ac_pl2_w}W",
                f"{s.bat_pl1_w}W / {s.bat_pl2_w}W",
                f"{s.gpu_limit_w} W",
                f"{s.gpu_temp_c} °C",
                s.auto_range,
                f"{floor} (def {s.manual_default_rpm})",
                s.notes,
            ))
        return rows1

    def _populate_reference_notes(self, profile: EcProfile) -> None:
        for child in self._ref_notes_frame.winfo_children():
            child.destroy()
        for line in profile.reference_notes:
            ttk.Label(self._ref_notes_frame, text=line, wraplength=860, justify="left").pack(anchor="w", pady=2)

    def _apply_ec_profile(self, profile: EcProfile, log_change: bool = True) -> None:
        """Hot-swap the active EcProfile across all buttons, clamps, tray labels, and reference tables."""
        self.ec_profile = profile
        ver_bits: list[str] = []
        if profile.cache_ver is not None:
            ver_bits.append(f"cache v{profile.cache_ver}")
        if profile.defaults_ver is not None:
            ver_bits.append(f"def v{profile.defaults_ver}")
        ver_suffix = f" [{', '.join(ver_bits)}]" if ver_bits else ""
        self.profile_var.set(f"EC Profile ({profile.source_label}{ver_suffix}): {profile.profile_tag}")

        for code in PRIMARY_MODES:
            spec = profile.modes[code]
            self.mode_buttons[code].configure(text=f"{spec.name} ({code})\n{spec.short_hint}")
        for code in SECONDARY_MODES:
            spec = profile.modes[code]
            self.mode_buttons[code].configure(text=f"{spec.name} · CPU {spec.ac_pl1_w}/{spec.ac_pl2_w}W")
        for level in (0, 1, 2):
            self.custom_cpu_buttons[level].configure(text=profile.custom_cpu[level].button_label)
            self.custom_gpu_buttons[level].configure(text=profile.custom_gpu[level].label)
        self.custom_gpu_hint_var.set(profile.custom_gpu_hint_text)
        self.clamp_hint_var.set(profile.clamp_summary_text)

        self._ref_t1_frame.configure(
            text=f"Table 1 — RZ09-0328 EC Modes, Power Limits, GPU Caps & Fan Bounds ({profile.source_label})"
        )
        for item in self._ref_t1_tree.get_children():
            self._ref_t1_tree.delete(item)
        for row in self._build_t1_rows(profile):
            self._ref_t1_tree.insert("", "end", values=row)
        self._populate_reference_notes(profile)

        if self.latest_snapshot is not None:
            self._apply_snapshot_to_ui(self.latest_snapshot)
        else:
            self._update_rpm_readout()
            self._sync_tray_state()

        if log_change:
            self._log(
                f"Loaded EC profile from {profile.source_label}: {profile.profile_tag}",
                {"event": "profile_loaded", "source_kind": profile.source_kind, "source": profile.source_label},
            )

    def _on_probe_live_ec(self) -> None:
        if sys.platform != "win32":
            self._log("Live EC silicon probe requires Windows.")
            return
        if self._busy:
            self._log("Controller busy; wait for active command to finish.")
            return
        self._busy = True
        self.probe_ec_btn.configure(state="disabled")
        self._log("Launching read-only live EC probe (ec_sio_probe.py --verify-live; approve UAC prompt if shown)...")

        def task():
            probe_path = run_live_ec_probe(self._repo_root)
            return probe_path, load_profile_from_probe(probe_path)

        def done(res, err):
            self._busy = False
            self.probe_ec_btn.configure(state="normal")
            if err is not None or res is None:
                self._log(f"Live EC probe failed: {err}", {"error": str(err)})
                return
            probe_path, prof = res
            self._apply_ec_profile(prof, log_change=True)
            self._log(f"Saved silicon readout to {probe_path}")

        self._submit(task, done)

    def _on_load_profile_file(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Select RZ09-0328 EC ROM (.bin) or Live Probe (probe.json)",
            initialdir=str(self._repo_root),
            filetypes=[
                ("EC ROM or Probe JSON", "*.bin *.json"),
                ("EC ROM Binary (256 KiB)", "*.bin"),
                ("EC Probe JSON", "*.json"),
                ("All Files", "*.*"),
            ],
        )
        if not chosen:
            return
        path = Path(chosen)
        try:
            prof = load_profile_from_rom(path) if path.suffix.lower() == ".bin" else load_profile_from_probe(path)
        except Exception as exc:
            self._log(f"Could not parse EC profile from {path.name}: {exc}")
            return
        self._apply_ec_profile(prof, log_change=True)

    @staticmethod
    def _make_table(
        parent: ttk.Frame,
        columns: tuple[str, ...],
        headers: tuple[tuple[str, str, int], ...],
        rows: tuple[tuple[str, ...], ...] | list[tuple[str, ...]],
    ) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=columns, show="headings", height=len(rows))
        for col_id, title, width in headers:
            tree.heading(col_id, text=title, anchor="w")
            tree.column(col_id, width=width, minwidth=60, anchor="w", stretch=True)
        for row in rows:
            tree.insert("", "end", values=row)
        tree.pack(fill="x", expand=True)
        return tree

    def _update_rpm_readout(self) -> None:
        mode = self.selected_mode.get()
        rpm = self.rpm_var.get()
        eff, min_rpm, max_rpm = clamp_manual_rpm(mode, rpm, profile=self.ec_profile)
        spec = self.ec_profile.modes.get(mode, self.ec_profile.modes[0])
        if eff != rpm:
            self.rpm_readout.set(f"Req {rpm} RPM (byte {rpm // 100}) -> EC clamps to {eff} RPM ({spec.name})")
        else:
            floor_txt = f"{min_rpm}" if min_rpm is not None else "0"
            self.rpm_readout.set(f"Req {rpm} RPM (byte {rpm // 100}) | Range {floor_txt}–{max_rpm}")
        if self.latest_snapshot is None:
            self.effect_var.set(
                "Offline Preview: "
                + describe_effective_behavior(
                    mode,
                    self.fan_manual.get(),
                    rpm,
                    self.cpu_boost_var.get(),
                    self.gpu_boost_var.get(),
                    profile=self.ec_profile,
                )
            )

    def _update_button_highlights(self) -> None:
        sel = self.selected_mode.get()
        for code, btn in self.mode_buttons.items():
            if code == sel:
                btn.configure(bg="#cce5ff", relief="sunken")
            else:
                btn.configure(bg="SystemButtonFace", relief="raised")

        c_lvl = self.cpu_boost_var.get()
        for lv, btn in self.custom_cpu_buttons.items():
            if sel == 4 and lv == c_lvl:
                bg = "#ffd9b3" if lv == 2 else "#cce5ff"
                btn.configure(bg=bg, relief="sunken")
            else:
                btn.configure(bg="SystemButtonFace", relief="raised")

        g_lvl = self.gpu_boost_var.get()
        for lv, btn in self.custom_gpu_buttons.items():
            if sel == 4 and lv == g_lvl:
                btn.configure(bg="#cce5ff", relief="sunken")
            else:
                btn.configure(bg="SystemButtonFace", relief="raised")

        if self.fan_manual.get():
            self.manual_btn.configure(bg="#ffe5cc", relief="sunken")
            self.auto_btn.configure(bg="SystemButtonFace", relief="raised")
        else:
            self.auto_btn.configure(bg="#d4edda", relief="sunken")
            self.manual_btn.configure(bg="SystemButtonFace", relief="raised")

        ac_boost = self.latest_power.ac_boost if self.latest_power is not None else None
        if ac_boost == 0:
            self.turbo_off_btn.configure(bg="#ffe5cc", relief="sunken")
            self.turbo_on_btn.configure(bg="SystemButtonFace", relief="raised")
        elif ac_boost is not None:
            self.turbo_on_btn.configure(bg="#d4edda", relief="sunken")
            self.turbo_off_btn.configure(bg="SystemButtonFace", relief="raised")
        else:
            self.turbo_on_btn.configure(bg="SystemButtonFace", relief="raised")
            self.turbo_off_btn.configure(bg="SystemButtonFace", relief="raised")

        self._update_rpm_readout()

    def _selected_zones(self) -> tuple[int, ...]:
        val = self.zone_choice.get()
        return (1,) if val == "1" else ((2,) if val == "2" else (1, 2))

    def _log(self, message: str, record: dict | None = None) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        if record is not None:
            payload = {"time": datetime.now().astimezone().isoformat(), "message": message, **record}
            try:
                with self.action_log_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(payload) + "\n")
            except OSError:
                pass

    def _submit(self, fn, callback) -> None:
        fut = self.executor.submit(fn)
        fut.add_done_callback(lambda done_fut: self.ui_queue.put((callback, done_fut)))

    def _post_ui(self, fn: Callable[[], None]) -> None:
        self.ui_queue.put((lambda _res, _err: fn(), None))

    def _drain_ui_queue(self) -> None:
        self._drain_job = None
        if self._closing:
            return
        while True:
            try:
                callback, fut = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if fut is None:
                try:
                    callback(None, None)
                except Exception:
                    pass
                continue
            try:
                callback(fut.result(), None)
            except Exception as exc:
                callback(None, exc)
        if not self._closing:
            self._drain_job = self.root.after(50, self._drain_ui_queue)

    @staticmethod
    def _safe_power_status() -> WindowsPowerStatus | None:
        if sys.platform != "win32":
            return None
        try:
            return query_power_status()
        except Exception:
            return None

    def _apply_power_status(self, pw: WindowsPowerStatus | None) -> None:
        if pw is None:
            return
        self.latest_power = pw
        ac_lbl = _BOOST_LABELS.get(pw.ac_boost, str(pw.ac_boost)) if pw.ac_boost is not None else "Unknown"
        dc_lbl = _BOOST_LABELS.get(pw.dc_boost, str(pw.dc_boost)) if pw.dc_boost is not None else "Unknown"
        self.power_var.set(f"Plan: {pw.scheme_name}  |  AC Boost: {ac_lbl}  |  DC: {dc_lbl}")
        self._sync_tray_state()

    def _connect_and_init(self) -> None:
        def task():
            ep = self.client.connect()
            snap = self.client.read_snapshot(include_gpu=True)
            pw = self._safe_power_status()
            return ep, snap, pw

        def done(res, err):
            if err is not None:
                self.conn_var.set(f"Connection failed: {err}")
                self._log(f"ERROR connecting: {err}", {"error": str(err)})
                return
            ep, snap, pw = res
            self.startup_snapshot = snap
            self._sync_vars_from_snapshot(snap)
            self._apply_power_status(pw)
            self._apply_snapshot_to_ui(snap)
            pw_msg = f", ac_boost={pw.ac_boost}" if pw is not None else ""
            self._log(
                f"Connected to {ep} (FW {snap.fw_version}). Startup: mode={snap.zones[0].mode}, "
                f"flag={snap.zones[0].fan_flag}, target={snap.zones[0].stored_rpm} RPM, "
                f"boost=({snap.cpu_boost}, {snap.gpu_boost}){pw_msg}",
                {"event": "connected", "fw": snap.fw_version},
            )
            self._schedule_poll()

        self._submit(task, done)

    def _sync_vars_from_snapshot(self, snap: BladeSnapshot) -> None:
        self.selected_mode.set(snap.zones[0].mode)
        self.fan_manual.set(snap.zones[0].fan_flag == 1)
        self.rpm_var.set(snap.zones[0].stored_rpm)
        self.cpu_boost_var.set(min(2, max(0, snap.cpu_boost)))
        self.gpu_boost_var.set(min(2, max(0, snap.gpu_boost)))

    def _schedule_poll(self) -> None:
        if self._closing:
            return
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)
        interval = TRAY_POLL_INTERVAL_MS if self._in_tray else POLL_INTERVAL_MS
        self._poll_job = self.root.after(interval, self._poll_tick)

    def _poll_tick(self) -> None:
        self._poll_job = None
        if self._closing:
            return
        if not self._busy:
            self._poll_ticks += 1
            include_power = (self._poll_ticks % POWER_POLL_EVERY_N_TICKS) == 0
            include_gpu = not self._in_tray

            def task():
                snap = self.client.read_snapshot(include_gpu=include_gpu)
                pw = self._safe_power_status() if include_power else None
                return snap, pw

            self._submit(task, self._on_poll_done)
        else:
            self._schedule_poll()

    def _on_poll_done(self, res, err: Exception | None) -> None:
        if err is None and res is not None:
            snap, pw = res
            if pw is not None:
                self._apply_power_status(pw)
            self._apply_snapshot_to_ui(snap)
        elif err is not None:
            self.conn_var.set(f"Poll error: {err}")
        self._schedule_poll()

    def _refresh_now(self) -> None:
        def task():
            snap = self.client.read_snapshot(include_gpu=True)
            pw = self._safe_power_status()
            return snap, pw

        self._submit(task, self._on_manual_refresh)

    def _on_manual_refresh(self, res, err: Exception | None) -> None:
        if err is not None:
            self._log(f"Refresh error: {err}")
        elif res is not None:
            snap, pw = res
            self._apply_power_status(pw)
            self._apply_snapshot_to_ui(snap)

    def _on_turbo_click(self, enabled: bool) -> None:
        if sys.platform != "win32":
            self._log("Turbo Boost switch requires Windows.")
            return
        self._busy = True

        def task():
            return set_turbo_boost(enabled)

        def done(pw: WindowsPowerStatus | None, err: Exception | None):
            self._busy = False
            if err is not None or pw is None:
                self._log(f"Turbo Boost {'ON' if enabled else 'OFF'} FAILED: {err}", {"error": str(err)})
                return
            self._apply_power_status(pw)
            self._update_button_highlights()
            self._log(
                f"Windows Turbo Boost -> {'ON' if enabled else 'OFF'} "
                f"(plan={pw.scheme_name}, ac_boost={pw.ac_boost}, dc_boost={pw.dc_boost}; EC mode unchanged)",
                {"event": "turbo_boost", "enabled": enabled, "plan": pw.scheme_guid, "ac_boost": pw.ac_boost},
            )

        self._submit(task, done)

    def _apply_snapshot_to_ui(self, snap: BladeSnapshot) -> None:
        self.latest_snapshot = snap
        self.conn_var.set(f"Connected: 1532:{self.pid:04X} (FW {snap.fw_version}) | MaxFanFlag: {snap.max_fan_flag}")
        for zs, var in zip(snap.zones, (self.z1_var, self.z2_var)):
            m_lbl = self.ec_profile.mode_label(zs.mode)
            f_lbl = "Manual (1)" if zs.fan_flag == 1 else f"Auto ({zs.fan_flag})"
            role = "CPU Fan" if zs.zone == 1 else "GPU Fan"
            eff, _, _ = clamp_manual_rpm(zs.mode, zs.stored_rpm, profile=self.ec_profile)
            clamp_note = f" -> EC clamp {eff} RPM" if (zs.fan_flag == 1 and eff != zs.stored_rpm) else ""
            var.set(f"Zone {zs.zone} ({role}): Mode={m_lbl}  |  Fan={f_lbl}  |  Stored Target={zs.stored_rpm} RPM{clamp_note}")
        gpu_str = snap.gpu_telem if snap.gpu_telem else "paused while in tray"
        self.telem_var.set(
            f"EC Temps: CPU-side={snap.cpu_ec_c} °C, GPU={snap.gpu_ec_c} °C  |  "
            f"EC Boost Regs: CPU={snap.cpu_boost}, GPU={snap.gpu_boost}  |  NVIDIA: {gpu_str}"
        )
        z0 = snap.zones[0]
        self.effect_var.set(
            "Active EC Readback: "
            + describe_effective_behavior(
                z0.mode,
                z0.fan_flag == 1,
                z0.stored_rpm,
                snap.cpu_boost,
                snap.gpu_boost,
                profile=self.ec_profile,
                live_gpu_limit_w=snap.gpu_enforced_limit_w,
            )
        )
        self._update_button_highlights()
        self._sync_tray_state()

    def _on_mode_click(self, mode_code: int) -> None:
        self.selected_mode.set(mode_code)
        self._update_button_highlights()
        self._send_fan_state(reason=f"Mode -> {self.ec_profile.mode_label(mode_code)}")

    def _on_custom_cpu_click(self, cpu_level: int) -> None:
        self.selected_mode.set(4)
        self.cpu_boost_var.set(cpu_level)
        self._update_button_highlights()
        spec = self.ec_profile.custom_cpu[cpu_level]
        self._send_fan_state(
            reason=f"Custom (4) CPU -> {spec.name} ({cpu_level}: {spec.ac_pl1_w}W/{spec.ac_pl2_w}W, GPU {spec.gpu_limit_w}W)"
        )

    def _on_custom_gpu_click(self, gpu_level: int) -> None:
        self.selected_mode.set(4)
        self.gpu_boost_var.set(gpu_level)
        self._update_button_highlights()
        spec = self.ec_profile.custom_gpu[gpu_level]
        self._send_fan_state(reason=f"Custom (4) GPU -> {spec.name} ({gpu_level}: {spec.temp_target_c}°C)")

    def _on_fan_mode_click(self, manual: bool) -> None:
        self.fan_manual.set(manual)
        self._update_button_highlights()
        self._send_fan_state(reason="Fan -> Manual" if manual else "Fan -> Auto")

    def _on_slider_move(self, _value: str) -> None:
        self._update_rpm_readout()

    def _on_slider_release(self, _event) -> None:
        if self.auto_apply_slider.get():
            self._apply_manual_rpm()

    def _quick_rpm(self, rpm: int) -> None:
        self.rpm_var.set(rpm)
        self._update_rpm_readout()
        self._apply_manual_rpm()

    def _apply_manual_rpm(self) -> None:
        self.fan_manual.set(True)
        self._update_button_highlights()
        self._send_fan_state(reason=f"Manual RPM -> {self.rpm_var.get()}")

    def _send_fan_state(self, reason: str) -> None:
        zones = self._selected_zones()
        mode = self.selected_mode.get()
        manual = self.fan_manual.get()
        rpm = self.rpm_var.get()
        prefix = self.prefix_var.get()
        cpu_boost = self.cpu_boost_var.get() if mode == 4 else None
        gpu_boost = self.gpu_boost_var.get() if mode == 4 else None
        self._busy = True
        include_gpu = not self._in_tray

        def task():
            events = self.client.apply_fan_settings(
                zones, mode, manual, rpm, prefix, cpu_boost=cpu_boost, gpu_boost=gpu_boost
            )
            time.sleep(0.08)
            snap = self.client.read_snapshot(include_gpu=include_gpu)
            return events, snap

        def done(res, err):
            self._busy = False
            if err is not None:
                self._log(f"{reason} FAILED: {err}", {"error": str(err)})
                return
            events, snap = res
            self._sync_vars_from_snapshot(snap)
            self._apply_snapshot_to_ui(snap)
            self._log(
                f"{reason} [zones={zones}, prefix={prefix}] -> z1=(m={snap.zones[0].mode}, f={snap.zones[0].fan_flag}, "
                f"rpm={snap.zones[0].stored_rpm}), z2=(m={snap.zones[1].mode}, f={snap.zones[1].fan_flag}, "
                f"rpm={snap.zones[1].stored_rpm}), boost=({snap.cpu_boost},{snap.gpu_boost})",
                {"event": "apply_fan", "events": events},
            )

        self._submit(task, done)

    def _restore_preset(self, mode: int) -> None:
        self.selected_mode.set(mode)
        self.fan_manual.set(False)
        self.zone_choice.set("both")
        self._update_button_highlights()
        self._send_fan_state(reason=f"Restore {self.ec_profile.mode_label(mode)} + Auto")

    def _restore_startup(self) -> None:
        if self.startup_snapshot is None:
            self._log("No startup snapshot captured.")
            return
        startup = self.startup_snapshot
        prefix = self.prefix_var.get()
        self._busy = True

        def task():
            self.client.restore_exact(startup, prefix=prefix)
            return self.client.read_snapshot(include_gpu=True)

        def done(snap, err):
            self._busy = False
            if err is not None:
                self._log(f"Restore startup FAILED: {err}")
                return
            self._sync_vars_from_snapshot(snap)
            self._apply_snapshot_to_ui(snap)
            self._log("Restored startup state.")

        self._submit(task, done)

    def _sync_tray_state(self) -> None:
        if self._tray is None:
            return
        snap = self.latest_snapshot
        pw = self.latest_power
        tooltip = format_tray_tooltip(snap, pw, profile=self.ec_profile)
        mode = snap.zones[0].mode if snap is not None else self.selected_mode.get()
        fan_flag = snap.zones[0].fan_flag if snap is not None else (1 if self.fan_manual.get() else 0)
        cpu_boost = snap.cpu_boost if snap is not None else self.cpu_boost_var.get()
        turbo_on = (pw.ac_boost != 0) if (pw is not None and pw.ac_boost is not None) else None
        self._tray.update_state(
            tooltip=tooltip,
            mode=mode,
            fan_flag=fan_flag,
            cpu_boost=cpu_boost,
            turbo_on=turbo_on,
            menu_labels=self.ec_profile.tray_labels(),
        )

    def _on_unmap(self, event) -> None:
        if event.widget is not self.root:
            return
        if self._closing or self._in_tray or not self.minimize_to_tray.get():
            return
        if time.monotonic() < self._restore_guard_until:
            return
        try:
            state = self.root.state()
        except tk.TclError:
            return
        if state == "iconic":
            if self._tray_hide_job is not None:
                self.root.after_cancel(self._tray_hide_job)
            self._tray_hide_job = self.root.after(10, self._enter_tray_if_iconic)

    def _enter_tray_if_iconic(self) -> None:
        self._tray_hide_job = None
        if self._closing or self._in_tray or not self.minimize_to_tray.get():
            return
        if time.monotonic() < self._restore_guard_until:
            return
        try:
            if self.root.state() != "iconic":
                return
        except tk.TclError:
            return
        self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        if self._closing or self._in_tray:
            return
        if self._tray is None:
            self._log("System tray is only supported on Windows.")
            return
        self._sync_tray_state()
        if not self._tray.show():
            self._log("System tray icon could not be added; keeping window on taskbar.")
            return
        self._in_tray = True
        self.root.withdraw()
        self._log("Minimized to system tray (left-click tray icon to restore; right-click for quick modes).")
        if self._poll_job is not None:
            self._schedule_poll()

    def _clear_topmost(self) -> None:
        if self._closing:
            return
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _restore_from_tray(self) -> None:
        if self._closing:
            return
        if self._tray_hide_job is not None:
            self.root.after_cancel(self._tray_hide_job)
            self._tray_hide_job = None
        was_in_tray = self._in_tray
        self._in_tray = False
        self._restore_guard_until = time.monotonic() + 0.35
        if self._tray is not None:
            self._tray.hide()
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            self.root.after(50, self._clear_topmost)
        except tk.TclError:
            return
        if was_in_tray:
            self._log("Restored window from system tray.")
            if self._poll_job is not None:
                self._schedule_poll()
            if not self._busy and self.startup_snapshot is not None:
                self._refresh_now()

    def _on_tray_command(self, cmd: int) -> None:
        if self._closing:
            return
        if cmd == TRAY_CMD_OPEN:
            self._restore_from_tray()
        elif cmd == TRAY_CMD_MODE_SILENT:
            self._on_mode_click(5)
        elif cmd == TRAY_CMD_MODE_BALANCED:
            self._on_mode_click(0)
        elif cmd == TRAY_CMD_MODE_GAMING:
            self._on_mode_click(1)
        elif cmd == TRAY_CMD_MODE_CREATOR:
            self._on_mode_click(2)
        elif cmd == TRAY_CMD_MODE_CUSTOM_LOW:
            self._on_custom_cpu_click(0)
        elif cmd == TRAY_CMD_MODE_CUSTOM_MED:
            self._on_custom_cpu_click(1)
        elif cmd == TRAY_CMD_MODE_CUSTOM_HIGH:
            self._on_custom_cpu_click(2)
        elif cmd == TRAY_CMD_FAN_AUTO:
            self._on_fan_mode_click(False)
        elif cmd == TRAY_CMD_TURBO_ON:
            self._on_turbo_click(True)
        elif cmd == TRAY_CMD_TURBO_OFF:
            self._on_turbo_click(False)
        elif cmd == TRAY_CMD_EXIT:
            self._on_close()

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for job_attr in ("_tray_hide_job", "_poll_job", "_drain_job"):
            job = getattr(self, job_attr, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, job_attr, None)
        if self._tray is not None:
            self._tray.stop()
        if self.restore_on_exit.get() and self.startup_snapshot is not None:
            try:
                fut = self.executor.submit(self.client.restore_exact, self.startup_snapshot, self.prefix_var.get())
                fut.result(timeout=3.0)
            except Exception:
                pass
        self.client.close()
        self.executor.shutdown(wait=False)
        self.root.destroy()


def self_test(verify_ui: bool = True) -> None:
    pkt = build_packet(0x0D82, (0, 1, 0, 0), MIN_REPORT)
    assert pkt[89] == 0x8A
    pkt_set = build_packet(0x0D02, (0, 1, 4, 1), MIN_REPORT)
    assert pkt_set[7:13] == bytes([0x0D, 0x02, 0x00, 0x01, 0x04, 0x01])
    assert pkt_set[89] == checksum(pkt_set)
    pkt_rpm = build_packet(0x0D01, (0, 2, 20), MIN_REPORT)
    assert pkt_rpm[7:12] == bytes([0x0D, 0x01, 0x00, 0x02, 20])
    pkt_boost = build_packet(0x0D07, (0, 1, 2), MIN_REPORT)
    assert pkt_boost[7:12] == bytes([0x0D, 0x07, 0x00, 0x01, 2])
    assert pkt_boost[89] == checksum(pkt_boost)

    for bad_cmd, bad_args in (
        (0x0D02, (0, 3, 4, 0)),
        (0x0D02, (0, 1, 9, 0)),
        (0x0D01, (0, 1, 71)),
        (0x0D07, (0, 1, 5)),
        (0x0300, (0, 0)),
    ):
        try:
            build_packet(bad_cmd, bad_args, MIN_REPORT)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Unexpectedly allowed {bad_cmd:#x} {bad_args}")

    # Verify v2 Mod manual RPM clamps (0–5500 RPM across all modes)
    for m in (0, 1, 2, 3, 4, 5, 6):
        assert clamp_manual_rpm(m, 0)[0] == 0
        assert clamp_manual_rpm(m, 2000)[0] == 2000
        assert clamp_manual_rpm(m, 5500)[0] == 5500
        assert clamp_manual_rpm(m, 6000)[0] == 5500

    # Verify Stock EC v01.00 manual RPM clamps decoded from 0x3d000 + 0x12370..0x12392
    assert clamp_manual_rpm(0, 2000, STOCK_PROFILE)[0] == 4300
    assert clamp_manual_rpm(0, 4600, STOCK_PROFILE)[0] == 4600
    assert clamp_manual_rpm(0, 6000, STOCK_PROFILE)[0] == 5500
    assert clamp_manual_rpm(1, 2000, STOCK_PROFILE)[0] == 4900
    assert clamp_manual_rpm(2, 4000, STOCK_PROFILE)[0] == 4900
    assert clamp_manual_rpm(4, 3000, STOCK_PROFILE)[0] == 4900
    assert clamp_manual_rpm(5, 2000, STOCK_PROFILE)[0] == 2400
    assert clamp_manual_rpm(5, 5500, STOCK_PROFILE)[0] == 5000
    assert clamp_manual_rpm(3, 2000, STOCK_PROFILE)[0] == 2000

    # Verify Custom Mode 4 automatically re-applies 0x0D07 after 0x0D02
    sent_seq: list[tuple[int, tuple[int, ...]]] = []

    class _FakeClient(BladeHidClient):
        def _exchange_unlocked(self, command: int, arguments: tuple[int, ...], retries: int = 3) -> dict:
            build_packet(command, arguments, MIN_REPORT)
            sent_seq.append((command, arguments))
            return {"args": list(arguments), "valid": True}

    fake = _FakeClient()
    fake._apply_fan_settings_unlocked((1, 2), 4, False, 4900, 0, cpu_boost=2, gpu_boost=1)
    assert sent_seq == [
        (0x0D02, (0, 1, 4, 0)),
        (0x0D02, (0, 2, 4, 0)),
        (0x0D07, (0, 1, 2)),
        (0x0D07, (0, 2, 1)),
    ], f"Unexpected Custom 4 command sequence: {sent_seq}"

    # Verify v2 & stock effective behavior descriptions + live GPU cap match/mismatch tags
    desc_high = describe_effective_behavior(4, False, 0, 2, 1, live_gpu_limit_w=80.0)
    assert "60W/90W" in desc_high and "GPU 80W" in desc_high and "Live GPU 80W ✓" in desc_high
    desc_med = describe_effective_behavior(4, True, 0, 1, 1, live_gpu_limit_w=90.0)
    assert "60W/80W" in desc_med and "GPU 90W" in desc_med and "Effective 0 RPM" in desc_med
    desc_low = describe_effective_behavior(4, False, 0, 0, 0, live_gpu_limit_w=80.0)
    assert "35W/45W" in desc_low and "GPU 90W" in desc_low and "≠ profile 90W" in desc_low
    desc_sil = describe_effective_behavior(5, False, 0, 1, 1)
    assert "25W/35W AC (25W/35W Bat)" in desc_sil and "GPU 30W" in desc_sil
    assert "55W/80W" in describe_effective_behavior(4, False, 4900, 2, 1, profile=STOCK_PROFILE)

    # Verify nvidia-smi CSV parser (including [N/A] graceful handling)
    nv = parse_nvidia_csv("40, 21.20, 80.00, 90.00, P8")
    assert nv is not None and nv.temp_c == 40 and nv.enforced_limit_w == 80.0 and nv.max_limit_w == 90.0 and "80W cap" in nv.display
    nv_na = parse_nvidia_csv("55, [N/A], 90.00, [N/A], P0")
    assert nv_na is not None and nv_na.power_draw_w is None and nv_na.enforced_limit_w == 90.0

    # Verify dynamic probe.json profile parser (full v2 silicon probe + legacy v1 probe)
    full_v2_probe = {
        "verify_live": {
            "summary": {
                "rom_slots": {
                    "0": {"ac_pl1_w": 35, "ac_pl2_w": 45, "dc_pl1_w": 25, "dc_pl2_w": 25, "throttle_pl1_w": 30, "throttle_pl2_w": 80, "manual_min_rpm": 0, "manual_default_rpm": 4900, "manual_max_rpm": 5500, "ndtt_c": 80, "dgps_level": 1},
                    "1": {"ac_pl1_w": 35, "ac_pl2_w": 60, "dc_pl1_w": 30, "dc_pl2_w": 30, "throttle_pl1_w": 30, "throttle_pl2_w": 80, "manual_min_rpm": 0, "manual_default_rpm": 5200, "manual_max_rpm": 5500, "ndtt_c": 87, "dgps_level": 1},
                    "2": {"ac_pl1_w": 45, "ac_pl2_w": 60, "dc_pl1_w": 30, "dc_pl2_w": 30, "throttle_pl1_w": 35, "throttle_pl2_w": 50, "manual_min_rpm": 0, "manual_default_rpm": 5200, "manual_max_rpm": 5500, "ndtt_c": 87, "dgps_level": 1},
                    "3": {"ac_pl1_w": 35, "ac_pl2_w": 45, "dc_pl1_w": 25, "dc_pl2_w": 25, "throttle_pl1_w": 35, "throttle_pl2_w": 80, "manual_min_rpm": 0, "manual_default_rpm": 5200, "manual_max_rpm": 5500, "ndtt_c": 87, "dgps_level": 1},
                    "4": {"ac_pl1_w": 35, "ac_pl2_w": 60, "dc_pl1_w": 30, "dc_pl2_w": 30, "throttle_pl1_w": 30, "throttle_pl2_w": 80, "manual_min_rpm": 0, "manual_default_rpm": 5200, "manual_max_rpm": 5500, "ndtt_c": 87, "dgps_level": 1},
                    "5": {"ac_pl1_w": 25, "ac_pl2_w": 35, "dc_pl1_w": 25, "dc_pl2_w": 35, "throttle_pl1_w": 25, "throttle_pl2_w": 35, "manual_min_rpm": 0, "manual_default_rpm": 3100, "manual_max_rpm": 5500, "ndtt_c": 97, "dgps_level": 5},
                    "6": {"ac_pl1_w": 25, "ac_pl2_w": 35, "dc_pl1_w": 25, "dc_pl2_w": 35, "throttle_pl1_w": 25, "throttle_pl2_w": 35, "manual_min_rpm": 0, "manual_default_rpm": 3100, "manual_max_rpm": 5500, "ndtt_c": 97, "dgps_level": 5},
                },
                "rom_has_v2_gpu_ctgp_handler": True,
                "rom_custom_gpu_90w_cpu_levels": [0, 1],
                "sram_custom_pl_w": {
                    "low": {"ac": [35, 45], "dc": [20, 20]},
                    "med": {"ac": [60, 80], "dc": [25, 25]},
                    "high": {"ac": [60, 90], "dc": [30, 30]},
                },
                "sram_cache_version_3fe000": 4,
                "sram_defaults_version_050000": 8,
            }
        }
    }
    p_v2 = build_profile_from_probe_dict(full_v2_probe, "test-v2/probe.json")
    assert p_v2.has_tdp_readback is True
    assert p_v2.custom_cpu[1].ac_pl1_w == 60 and p_v2.custom_cpu[1].gpu_limit_w == 90
    assert p_v2.custom_cpu[2].ac_pl2_w == 90 and p_v2.custom_cpu[2].gpu_limit_w == 80
    assert p_v2.modes[5].ac_pl1_w == 25 and p_v2.modes[5].bat_pl2_w == 35
    assert "v2 Mod" in p_v2.profile_tag

    legacy_v1_probe = {
        "verify_live": {
            "summary": {
                "rom_has_mod_3d1c9": True,
                "rom_has_mod_3dbc9": True,
                "sram_cache_version": 3,
            }
        }
    }
    p_v1 = build_profile_from_probe_dict(legacy_v1_probe, "test-v1/probe.json")
    assert p_v1.has_tdp_readback is False
    assert p_v1.modes[0].manual_min_rpm == 0 and p_v1.modes[5].manual_max_rpm == 5500
    assert "v1 Fan Mod" in p_v1.profile_tag

    # Verify pure-Python EC ROM decoder against workspace stock & v2 binaries when present
    repo_root = Path(__file__).resolve().parent.parent
    stock_rom = repo_root / "firmware" / "rz09-0328-ec-v01.00" / "ec-rom-00000-3ffff.bin"
    if stock_rom.is_file():
        ps = load_profile_from_rom(stock_rom)
        assert ps.modes[0].manual_min_rpm == 4300 and ps.modes[5].manual_max_rpm == 5000
        assert ps.modes[5].ac_pl1_w == 35 and ps.modes[5].bat_pl2_w == 30
        assert (ps.custom_cpu[0].ac_pl1_w, ps.custom_cpu[1].ac_pl1_w, ps.custom_cpu[2].ac_pl2_w) == (25, 35, 80)
        assert ps.custom_gpu_90w_levels == ()
    v2_rom = repo_root / "firmware" / "rz09-0328-ec-v01.00-mod-v2-power-fan" / "rz09-0328-ec-v01.00-v2-fan0-5500-tdp-gpu90w-8dff.bin"
    if v2_rom.is_file():
        pv = load_profile_from_rom(v2_rom)
        assert pv.modes[0].manual_min_rpm == 0 and pv.modes[5].manual_max_rpm == 5500
        assert (pv.modes[5].ac_pl1_w, pv.modes[5].ac_pl2_w, pv.modes[5].bat_pl1_w, pv.modes[5].bat_pl2_w) == (25, 35, 25, 35)
        assert (pv.custom_cpu[0].ac_pl1_w, pv.custom_cpu[0].ac_pl2_w, pv.custom_cpu[0].gpu_limit_w) == (35, 45, 90)
        assert (pv.custom_cpu[1].ac_pl1_w, pv.custom_cpu[1].ac_pl2_w, pv.custom_cpu[1].gpu_limit_w) == (60, 80, 90)
        assert (pv.custom_cpu[2].ac_pl1_w, pv.custom_cpu[2].ac_pl2_w, pv.custom_cpu[2].gpu_limit_w) == (60, 90, 80)
        assert pv.custom_gpu_90w_levels == (0, 1)

    # Verify locale-agnostic powercfg parsers (including nested parens on Blade Quiet)
    assert parse_active_scheme(
        "Power Scheme GUID: 11111111-2222-3333-4444-555555555555  (Blade Quiet (boost disabled))"
    ) == ("11111111-2222-3333-4444-555555555555", "Blade Quiet (boost disabled)")
    assert parse_boost_indices(
        "  当前交流电源设置索引: 0x00000000\r\n  当前直流电源设置索引: 0x00000002\r\n"
    ) == (0, 2)
    assert "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c" in parse_scheme_guids(
        "GUID: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c (高性能)"
    )

    # Verify tray PNG icon encoder, tooltip formatting, and Win32 tray host lifecycle (no taskbar icon added)
    png_bytes = build_tray_icon_png(32)
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n") and png_bytes.endswith(b"IEND\xaeB`\x82")
    fake_snap = BladeSnapshot(
        endpoint="test",
        fw_version="01.00",
        zones=(ZoneSnapshot(1, 4, 1, 5200), ZoneSnapshot(2, 4, 1, 5200)),
        cpu_ec_c=68,
        gpu_ec_c=61,
        cpu_boost=2,
        gpu_boost=1,
        max_fan_flag=0,
        gpu_telem="",
    )
    fake_pw = WindowsPowerStatus("guid", "Plan", ac_boost=0, dc_boost=2, known_guids=frozenset())
    tip = format_tray_tooltip(fake_snap, fake_pw)
    assert len(tip) <= 127 and "Custom (4) High" in tip and "Turbo Boost: OFF" in tip
    assert format_tray_tooltip(None, None) == "Razer Blade Controller"
    tray_lbls = DEFAULT_PROFILE.tray_labels()
    assert "25/35W" in tray_lbls[TRAY_CMD_MODE_SILENT]
    assert "60/80W · GPU 90W" in tray_lbls[TRAY_CMD_MODE_CUSTOM_MED]
    assert "60/90W · GPU 80W" in tray_lbls[TRAY_CMD_MODE_CUSTOM_HIGH]
    if sys.platform == "win32":
        tray = WindowsTrayIcon(on_restore=lambda: None, on_command=lambda _c: None, icon_png=png_bytes)
        assert tray.start() is True
        assert tray.is_running and tray.has_icon
        tray.update_state(tooltip=tip, mode=4, fan_flag=1, cpu_boost=2, turbo_on=False, menu_labels=tray_lbls)
        tray.stop()
        assert not tray.is_running

    if verify_ui and sys.platform == "win32":
        root = tk.Tk()
        root.withdraw()
        app = FanTesterApp(root, auto_connect=False, ec_profile=DEFAULT_PROFILE)
        root.update_idletasks()
        assert set(app.custom_cpu_buttons.keys()) == {0, 1, 2}
        assert set(app.custom_gpu_buttons.keys()) == {0, 1, 2}
        assert "60W/80W" in app.custom_cpu_buttons[1].cget("text")
        assert "60W/90W" in app.custom_cpu_buttons[2].cget("text")
        assert "25/35W" in app.mode_buttons[5].cget("text")
        assert app.probe_ec_btn.winfo_exists() and app.load_prof_btn.winfo_exists()
        # Verify dynamic hot-swap between Stock and v2 profiles refreshes buttons & tables live
        app._apply_ec_profile(STOCK_PROFILE, log_change=False)
        assert "55W/80W" in app.custom_cpu_buttons[2].cget("text")
        assert "35/45W" in app.mode_buttons[5].cget("text")
        app._apply_ec_profile(DEFAULT_PROFILE, log_change=False)
        assert "60W/90W" in app.custom_cpu_buttons[2].cget("text")
        assert app.turbo_on_btn.winfo_exists() and app.turbo_off_btn.winfo_exists()
        assert app.minimize_to_tray.get() is True and app.tray_btn.winfo_exists()
        app._closing = True
        if app._drain_job is not None:
            root.after_cancel(app._drain_job)
        app.executor.shutdown(wait=False)
        root.destroy()

    print(
        "Self-test passed: packet framing, allowlist bounds, v2 & stock RPM clamps, "
        "Custom 4 boost sequence, nvidia-smi parser, live probe & EC ROM auto-detection, "
        "powercfg parsers, system tray host, and UI hot-swap verified. No hardware accessed."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Verify packets, clamps, auto-detection, Custom 4 logic, and UI without hardware")
    parser.add_argument("--ec-rom", type=Path, default=None, help="Load EC mode/TDP/fan profile directly from a 256 KiB RZ09-0328 EC ROM (.bin)")
    parser.add_argument("--probe", type=Path, default=None, help="Load EC mode/TDP/fan profile directly from a verify-live probe.json")
    parser.add_argument("--stock-defaults", action="store_true", help="Force built-in stock EC v01.00 defaults")
    parser.add_argument("--v2-defaults", action="store_true", help="Use built-in v2 modded EC defaults")
    parser.add_argument("--pid", type=lambda s: int(s, 0), default=0x0255, help="Razer Blade USB HID PID in hex (default 0x0255)")
    args = parser.parse_args()
    self_test(verify_ui=args.self_test)
    if args.self_test:
        return 0
    if sys.platform != "win32":
        parser.error("Razer Blade HID GUI requires Windows")
    repo_root = Path(__file__).resolve().parent.parent
    profile = autodetect_startup_profile(
        repo_root,
        rom_path=args.ec_rom,
        probe_path=args.probe,
        stock_defaults=args.stock_defaults,
        v2_defaults=args.v2_defaults,
    )
    root = tk.Tk()
    FanTesterApp(root, ec_profile=profile, pid=args.pid)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
