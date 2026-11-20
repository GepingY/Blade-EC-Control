# Razer Blade `IT8390E` EC Reverse Engineering, Dump/Flash Toolkit & HID Controller

A reverse-engineering knowledge base and open-source Python toolkit for **ITE `IT8390E-256` (AndesCore `NDS32`)** Embedded Controllers on Razer Blade laptops — developed and live-verified on the **Razer Blade 15 Base Model (Early 2020, `RZ09-0328`, `DA550-MB`, `SSID 0x2011`, `EC v01.00`, USB HID `1532:0255`)**.

> [!WARNING]
> **Hardware Risk Disclaimer:** Reading/dumping your EC ROM via `ec_sio_probe.py` is strictly read-only, and HID fan/mode control is volatile (resets on reboot). **Flashing modified EC firmware always carries brick risk.** Never flash a ROM dumped from a different board model or SSID, and read [docs/FLASH_GUIDE.md](docs/FLASH_GUIDE.md) before touching any flasher.
>
> **No proprietary binaries or firmware images are included in this repository.** All patchers operate offline on files you dump from your own hardware or obtain directly from official vendor releases.

---

## 1. How This Repository Can Help You

Even if you do **not** own the exact `RZ09-0328` model, the EC extraction, analysis, and flashing tools and protocol documentation apply broadly across **ITE `IT8390E` Razer Blades** (and much of the NDS32 tooling applies to ITE `IT8390` / `IT89xx` / `IT557x` ECs in general):

1. **Dump Your Own EC ROM Cleanly on Stock Windows 11 — Without Disabling HVCI/Secure Boot (`ec_research/ec_sio_probe.py`)**
   - Most legacy guides rely on `WinRing0` (blocked by Windows 11 Memory Integrity / Vulnerable Driver Blocklist) or raw ACPI EC `0x62/0x66` polling (which collides with Windows `ACPI.sys` and triggers ACPI Event 15).
   - Our dumper uses the official WHCP-signed **[PawnIO](https://pawnio.eu/)** driver (`LpcIO.bin`) to discover Super I/O (`0x2E/0x2F`) and read the entire 256 KiB EC ROM over the EC's own **BRAM0 (`0x0302`) command `0x90`** read-only mailbox — zero `0x62/0x66` contention, zero WinRing0, and automatic driver cleanup afterward.
   - Includes a fast read-only `--verify-live` mode (`~0.35 s`) to inspect live EC ROM bytes, SRAM cache versions, active PECI power limits, and hardware PWM duty registers.

2. **Disassemble & Inspect `IT8390E` (`NDS32`) EC Firmware Offline (`ec_research/dis_nds32.py`, `ec_research/analyze_ec_rom.py`)**
   - Pure-Python mixed 16/32-bit **AndesCore `NDS32`** disassembler (no external toolchain required).
   - Decodes Razer `IT8390E` thermal/fan/power mode slots, 50-RPM PWM lookup tables, `20%` spin-floor runtime defaults, and dynamic ADC current-throttling tables — plus a `--compare` mode to diff two EC ROMs across models/versions.

3. **Safely Verify & Flash Your Own EC (`ec_research/ite_ec_flash_linux.py`, `ec_research/patch_razer_flasher.py`)**
   - **Linux Live USB Verifier & Flasher (`ite_ec_flash_linux.py`)**: Implements the exact `LDN 0x11` (`0x62/0x66 -> 0x202/0x206`) remap + Intel PCH `PCI 0:31:0 reg 0x82` + `PM1 0xDC -> 0x33` Follow-Mode protocol, adding a mandatory read-only `--verify-only` dry run, automatic in-Follow-Mode per-sector retry, and a fail-safe guard that refuses to send `0xFC` (Exit Idle) if verification fails.
   - **Offline `.NET IL` Safety Patcher for Razer's Official Windows Flasher (`patch_razer_flasher.py`)**: Fixes a verified verify-failure brick bug in `Razer_EC_Flash_v1.03.exe` (see Discovery #7 below) and generates a `_safe.exe` + `_recovery_in_follow_mode.exe` pair from your own official copy.

4. **Reproducible EC Modding Blueprints with Built-In NDS32 Emulation (`ec_research/patch_ec_fan_range.py`, `ec_research/patch_ec_v2_power_fan.py`)**
   - Complete, live-verified reference examples showing how to unlock `0–5500 RPM` manual fan clamps, customize per-mode CPU `PL1`/`PL2` wattages, unlock `90 W` GPU `CTGP` inside Custom Mode, bump both external BIOS SPI caches cleanly, and verify emitted machine code inside a built-in instruction-level `NDS32` emulator before ever touching hardware.
   - *(Note: Exact ROM offsets in `patch_ec_*.py` are specific to `RZ09-0328 EC v01.00`, SHA-256 verified before patching; use `dis_nds32.py` and `analyze_ec_rom.py` to locate equivalents on other models.)*

5. **Zero-Dependency Synapse-Free Python HID GUI Controller (`gui/`)**
   - Pure-Python (`ctypes` + `tkinter`, zero third-party packages) Windows HID controller & system tray app built for `RZ09-0328` (`1532:0255`) with live `nvidia-smi` power-limit cross-checks, Custom Mode `0x0D07` re-application, independent Windows Turbo Boost toggle, and an experimental `--pid` flag for testing other Razer Blade models that share the standard `0x0D` HID protocol.

---

## 2. Key Technical Discoveries

All findings below were verified against disassembled `IT8390E-256` `NDS32` firmware, live ACPI table disassembly (`DSDT` + 12 `SSDT`s), live `nvidia-smi` captures, and live hardware verification. Full register maps and addresses live in **[FINDINGS.md](FINDINGS.md)**.

### A. EC Modes, Power Ceilings & Manual Fan Clamps
The EC stores **8 thermal/fan/power mode slots** (`0..7`, `0x200` bytes each) across SKU banks (`0x3B000..0x3E000`, selected by `[0x802ED]`). Each slot controls **CPU AC/DC `PL1/PL2`**, **thermal fallback `PL1/PL2`**, **auto fan curve steps**, **manual RPM min/default/max clamps**, **GPU thermal target (`NDTT`)**, and **ACPI GPU power policy (`DGPS` / `TMMD`)**:

| Mode | Name | Stock AC CPU `PL1/PL2` | Stock GPU Cap | Stock Auto Fan Range | Stock Manual RPM Clamp |
| :---: | :--- | :---: | :---: | :--- | :--- |
| **`0`** | Balanced | `35 W / 45 W` | `80 W` (`_QD1`) | `0` or `2500–5300` | `4300–5500` (def `4900`) |
| **`1`** | Gaming | `35 W / 60 W` | **`90 W`** (`_Q30`, `CTGP=1`) | `0` or `2700–5500` | `4900–5500` (def `5200`) |
| **`2`** | Creator / Perf | `45 W / 60 W` | `80 W` (`_QD1`) | `0` or `2700–5500` | `4900–5500` (def `5200`) |
| **`3`** | Legacy / Mode 3 | `35 W / 45 W` | `80 W` (`_QD1`) | `0` or `2700–5500` | `< 4900` drops to `0` quirk (`0x1238c`) |
| **`4`** | Custom (`0x0D07`) | `25/45`, `35/45`, or **`55/80 W`** | `80 W` *(stock)* | `0` or `2700–5500` | `4900–5500` (def `5200`) |
| **`5–7`** | Silent (and clones) | `35 W / 45 W` | **`30 W`** (`_QD5`) | `0` or `2200–3250` | **`2400–5000`** (def `3100`) |

* **Why setting `2000 RPM` in Synapse/HID often "does nothing"**: The EC enforces per-mode manual RPM lower clamps (`4300 RPM` in Balanced, `4900 RPM` in Gaming/Creator/Custom, `2400 RPM` in Silent). Stored target readback (`0x0D81`) echoes your requested setpoint, not the clamped target or live tach (`0x0D88` is unsupported on `RZ09-0328`).
* **Thermal Safety Override**: In manual fan mode, reaching the top thermal step forces fans back to Auto and sets `[0x8034D] bit 7` to lock manual writes until cooled.

### B. The `20%` Hardware PWM Spin Floor & `0 RPM` Hysteresis Rule
* Hardware PWM period is `183` (`0xB7` at `0x00F01841`), driving Fan 1 (`DCR7` `0x00F01807`) and Fan 2 (`DCR3` `0x00F01803`).
* Runtime table `0x2098C` loads **`20%` (`0x14`)** into `0x86010..0x86013`, which firmware converts to `(20 * 183) / 100 = 36` ticks (`0x24`).
* **Kick-Start & Snap-to-Zero (`0x11a0c..0x11a88`)**:
  * Spinning **down** toward `0 RPM`: PWM steps down 1 tick per loop until `< 36`, then **snaps straight to `0` (full fan stop)**.
  * Starting **up** from `< 36`: PWM jumps immediately to **`36` (`~20%`)** before handing off to closed-loop tachometer PID (`150 RPM` deadband).
  * Consequence: Even after unlocking manual clamps to `0–5500 RPM`, `0 RPM` produces a clean full stop and `~2000–5500 RPM` gives smooth closed-loop PID, while `100–~1800 RPM` sits below the `20%` spin floor (and small 5 V radial fans physically stall below ~15–20% duty anyway).

### C. Hidden Engineering Direct-PWM Modes (`0xFE` / `0xFF`) & Shared Bank 0 Map
Via the EC's SMFI/BRAM Shared Bank 0 (`0x00F02290..0x00F022C0`, not exposed over USB HID):
* **Mode `0xFF` (`255`)** (requires `0x80425 bit 0 = 1`, Bank 0 `0xF0`): Maps requested RPM directly through the 50-RPM PWM LUT (`0x2081C`) into hardware `DCR` registers.
* **Mode `0xFE` (`254`)** (requires `0x80425 bit 6 = 1`): Writes raw duty bytes `0..183` (Bank 0 `0xB8`/`0xB9`) straight into `DCR7`/`DCR3`, **bypassing PID and the `20%` (`36`) cutoff**, while inheriting the previous mode's power profile (volatile — resets cleanly on reboot).

### D. `0x0D02` Mode-Write Reset Quirk & Power Arbitration (CPU PECI + GPU ACPI/GPS)
* **Custom Mode `0x0D02` Reset Bug/Quirk**: Writing *any* `0x0D02` HID mode packet makes EC firmware (`0x12cb0`) reset Custom CPU & GPU Boost (`0x808E0`/`0x808E1`) back to Medium (`1`). Software must always re-send `0x0D07` **after** `0x0D02`.
* **CPU Power Arbitration (No Tug-of-War When Lowering)**: The EC writes CPU `PL1`/`PL2` over PECI (`0x1A`/`0x1B`) **only when mode, AC/DC state, thermal throttle flag, or the ADC current stepper changes** — never continuously in steady state. Because Intel silicon enforces `MIN(Windows MSR 0x610, MMIO MCHBAR, EC PECI, DPTF)`, **lowering** `PL1`/`PL2` from Windows works cleanly with zero tug-of-war, whereas **raising** above the active EC mode's ceiling requires putting the EC into a higher-watt mode first (and staying under the adapter/PD cap at `0x86120`, `60 W / 90 W` on barrel AC or `100 W` PD, `15 W / 44 W` on `46–99 W` USB-C PD).
* **GPU Power (`30 W / 80 W / 90 W`) Has No Watt Table in EC ROM**: The EC selects discrete ACPI policy codes, and the NVIDIA VBIOS/driver maps them to Watts:
  * `_QD1.._QD5` (`DGPS`): Silent (`5`) locks `_QD5` → **`30 W`**; Modes `0, 2, 3, 4` set `_QD1` → **`80 W`**.
  * `_Q30` (`TMMD = 1` in Gaming) and `_Q33` (`PSTA bit 3` in Custom): Toggle `PEGP.CTGP = 1` (NVIDIA `GPS` bit 22) → unlocks **`90 W`** on RTX 2070 Max-Q.
  * `NDTT` (`[0x80205]`): Sets GPU thermal throttle target (`75 / 80 / 87 / 97 °C`) read by NVIDIA `GPS` Function `42`.
* **BIOS ACPI Fan Interfaces Are Dead Ends**: `AC0F`/`AC1F`, `H_EC`, DPTF `EC0.TFN1._FSL`, `AMW0.WMBA`, and `TZ00._TMP` route to dummy/absent Intel reference stubs (`H_EC._STA() == 0`, empty `ECWT`/`ECMD`). See [docs/ACPI_FAN_ANALYSIS.md](docs/ACPI_FAN_ANALYSIS.md).

### E. The `0x0008D = 0xFF` External SPI (`GD25Q64`) → Internal eFlash Mirror Discovery
* On `RZ09-0328` (`DA550-MB`), the EC flasher writes the external SPI chip (`GD25Q64`), and `IT8390E-256` **hardware-mirrors** that 256 KiB image into its internal eFlash on boot **only if ITE 16-byte signature byte 13 (`0x0008D`) is `0xFF`**.
* After mirroring completes, the hardware automatically clears `0x0008D` back to `0x00` inside internal eFlash and sets `FLHCTRL3R` (`0x00F01063`) bit 7 (`0x08 -> 0x88`).
* **Crucial Gotcha**: Dumping internal eFlash always yields `0x0008D = 0x00`. If you edit your dump and flash it back without changing `0x0008D` to `0xFF`, the flasher succeeds **but the EC silently boots the untouched old internal eFlash**! The same applies when flashing back to stock — your stock rollback image also needs `0x0008D = 0xFF`.

### F. Dual External BIOS SPI Flash Caches (`0x3FE000` & `0x050000`)
Even after internal eFlash is updated, the EC loads CRC-16-validated cached copies from the external BIOS SPI flash:
1. **Thermal / Fan / Power Slot Cache (`0x3FE000` / `0x3FF000`, `0x120ca -> 0x14678`)**: Caches `0x3D000` mode slots and `0x35000` current stepper slots tagged with **version `2`**. Bumping the version check (`0x1211E`) and slot writer (`0x11314`) forces an automatic rebuild on boot — **watch out for the `r7 = 2` register-reuse trap at `0x1135A`** (the stock code shares `r7 = 2` for both the `0x0200` 512-byte length high byte AND version `2`; use callee-saved `r8` for the bumped version instead!).
2. **Runtime Defaults Cache (`0x050000`, `0x105a2..0x105ec`)**: Caches 4 KiB (`0x86000..0x86FFF`, holding both the `20%` PWM floor and the `0x86900` Custom Mode 4 CPU PL table) tagged with CRC-16 + **struct version `7`**. Bumping `7 -> 8` at `0x1046C`, `0x104FC`, `0x105CC`, `0x10606` triggers automatic rebuild and re-CRC.

### G. Verify-Failure Brick Bug in Official `Razer_EC_Flash_v1.03.exe`
Static `.NET` IL analysis of `Razer_EC_Flash_v1.03.exe` (`Program::Main`, `IL_095B..IL_0AD8`) uncovered a serious bug: if post-flash verification mismatches (`.......Fail`, `verifyfail = 1`), missing braces around `if (verifyfail == 0)` cause it to **still print `Verify .........100%`, unconditionally send `EC_Exit_Idle` (`0xFC`) (booting the corrupt/unverified flash!), and unconditionally force a reboot (`DoExitWin(6)`) if `REBOOT=1`**.
* `ec_research/patch_razer_flasher.py` patches this cleanly offline (19-byte IL diff, `.NET 4.8` JIT-verified) to keep the EC safely parked in its RAM-resident Follow-Mode loop (`0x0259a`) on `0x202/0x206` and exit with code `2`, paired with a `_recovery_in_follow_mode.exe` binary that skips `0xDC`/`0x33` so you can re-flash immediately without rebooting.

---

## 3. Repository Structure

```text
.
├── README.md                        # Overview, how this repo helps & key discoveries (this file)
├── FINDINGS.md                      # Complete technical reference, memory maps & decoded tables
├── docs/
│   ├── ACPI_FAN_ANALYSIS.md         # Why BIOS/ACPI/DPTF fan controls are dead ends
│   └── FLASH_GUIDE.md               # Step-by-step EC dump, patch, flash & rollback guide
├── ec_research/
│   ├── README.md                    # EC toolkit guide + official PawnIO download/SHA-256 setup
│   ├── ec_sio_probe.py              # HVCI-safe read-only Super I/O probe, 256 KiB ROM dumper & live verifier
│   ├── ec_snapshot.py               # Shared PawnIO helpers + historical 0x62/0x66 conflict note
│   ├── EcSioProbe.cmd               # UAC launcher for ec_sio_probe.py --run
│   ├── EcRomDump.cmd                # UAC launcher for ec_sio_probe.py --dump
│   ├── EcVerifyLive.cmd             # UAC launcher for ec_sio_probe.py --verify-live
│   ├── acpi_inspect.py              # Read-only Windows ACPI table dumper
│   ├── dis_nds32.py                 # Pure-Python mixed 16/32-bit AndesCore NDS32 disassembler
│   ├── analyze_ec_rom.py            # Offline EC ROM table decoder & ROM diff tool
│   ├── ite_ec_flash_linux.py        # Linux Live USB IT8390E Follow-Mode verifier (--verify-only) & flasher
│   ├── patch_razer_flasher.py       # Offline .NET IL safety patcher for Razer_EC_Flash_v1.03.exe
│   ├── patch_ec_fan_range.py        # RZ09-0328 v1 EC mod example (0–5500 RPM + cache v3 + mirror 0x8D=0xFF)
│   └── patch_ec_v2_power_fan.py     # RZ09-0328 v2 EC mod example (0–5500 RPM + Custom TDP + 90W GPU + NDS32 emu)
└── gui/
    ├── README.md                    # RZ09-0328 HID GUI controller guide (with --pid support for other Blades)
    ├── blade_fan_gui.py             # Tkinter + Win32 tray GUI (EC modes, Custom 4, nvidia-smi, Turbo Boost)
    ├── blade_inspect.py             # Native Windows HID transport & read-only CLI inspector
    ├── blade_profile.py             # CLI Quiet/Performance HID switcher
    ├── blade_silent_test.py         # Bounded Silent-mode HID tester
    ├── BladeFanTester.cmd           # GUI launcher
    ├── BladeQuiet.ps1               # Quiet / Performance Windows power plan + HID backend
    ├── Quiet.cmd / Performance.cmd  # One-click Quiet / Performance preset launchers
```

---

## 4. Quick Start Workflows

### A. Offline Self-Tests (Touch Zero Hardware)
Requires Python 3.10+ (standard library only — zero `pip install` needed):
```powershell
python ec_research/ec_sio_probe.py --self-test
python ec_research/ite_ec_flash_linux.py --self-test
python gui/blade_fan_gui.py --self-test
```

### B. Dump Your Own `IT8390E` EC ROM on Windows 11 (Read-Only, HVCI-Safe)
1. Download the two official signed PawnIO release assets (see [ec_research/README.md](ec_research/README.md) for exact URLs and pinned SHA-256 hashes):
   - `PawnIO_setup.exe` (`v2.2.0` from `namazso/PawnIO.Setup`)
   - `release_0_2_12.zip` (`v0.2.12` from `namazso/PawnIO.Modules`, containing signed `LpcIO.bin`)
   Place both in `tools/pawnio/` (or pass `--pawnio-dir <folder>`).
2. Verify hashes offline:
   ```powershell
   python ec_research/ec_sio_probe.py --check
   ```
3. Run a read-only Super I/O & BRAM discovery probe first:
   - Double-click `ec_research/EcSioProbe.cmd` (or from an elevated prompt: `python ec_research/ec_sio_probe.py --run`).
4. Dump all 256 KiB of internal eFlash (`~4.5 minutes`, read-only over BRAM0 `0x0302` cmd `0x90`, auto-uninstalls PawnIO afterward):
   - Double-click `ec_research/EcRomDump.cmd` (or `python ec_research/ec_sio_probe.py --dump`).
   - Output is saved under `diagnostics/rom-dump-<timestamp>/ec-rom-00000-3ffff.bin`.

### C. Analyze & Disassemble Your Dumped ROM Offline
```powershell
python ec_research/analyze_ec_rom.py path/to/ec-rom-00000-3ffff.bin --steps
python ec_research/dis_nds32.py --bin path/to/ec-rom-00000-3ffff.bin 12356:123a0
```

### D. Patch & Flash (Advanced — Read [docs/FLASH_GUIDE.md](docs/FLASH_GUIDE.md) First!)
* For `RZ09-0328 EC v01.00`, `patch_ec_fan_range.py` (v1) and `patch_ec_v2_power_fan.py` (v2) validate your stock ROM's SHA-256, apply the exact patches, run disassembly/emulator assertions, and emit both the mirror-ready (`0x0008D = 0xFF`) modded ROM and a mirror-ready stock rollback ROM.
* See [docs/FLASH_GUIDE.md](docs/FLASH_GUIDE.md) for both the **Linux Live USB** (`ite_ec_flash_linux.py`) and **patched Windows flasher** (`patch_razer_flasher.py`) workflows, plus post-flash verification via `EcVerifyLive.cmd`.

### E. Use the Synapse-Free Python GUI (`gui/`)
```powershell
python gui/blade_fan_gui.py                     # RZ09-0328 (default PID 0x0255, Stock defaults)
python gui/blade_fan_gui.py --v2-defaults       # If running the v2 EC mod
python gui/blade_fan_gui.py --ec-rom my_ec.bin  # Auto-load tables directly from any RZ09-0328 EC ROM
python gui/blade_fan_gui.py --pid 0x0255        # Override USB HID PID (hex) for other Razer Blade models
```

---

## 5. Credits & References
- **[PawnIO](https://pawnio.eu/)** (`namazso/PawnIO` & `namazso/PawnIO.Modules`) — WHCP-signed kernel driver & signed `LpcIO.bin` module enabling HVCI-safe read-only hardware research on Windows 11.
- **[ACPICA `iasl`](https://github.com/open-acpica/acpica)** — ACPI table disassembly.
- **[blade-control](https://github.com/jdsgnrinfo/blade-control)** & **[razer-ctl](https://github.com/sqmagellan/razer-ctl)** — Prior open-source Razer HID protocol documentation.
- **Microsoft `GetSystemFirmwareTable` API** — User-mode read-only ACPI table extraction.
