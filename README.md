# Razer Blade Fan, Power & EC Control (`RZ09-0328` & `IT8390E` Blades)

A simple, Synapse-free Windows app to control fans, power modes, and CPU Turbo Boost on the **Razer Blade 15 Base Model (Early 2020, `RZ09-0328`)** — plus step-by-step tools to **dump, mod, and safely flash your own Embedded Controller (EC)** to unlock full **`0–5500 RPM`** manual fan control (including complete `0 RPM` fan stop in any mode).

> **No programming experience required to use the app!** Download the ready-to-run `.exe` below and double-click it — no Python install, no Razer Synapse, and no console window.

---

## 1. Quick Start — Use the GUI App (No Firmware Mod Needed)

If you own a **Razer Blade 15 Base (Early 2020, `RZ09-0328`)**, you can use the controller immediately with zero risk:

1. Go to the **[Releases page](https://github.com/GepingY/Blade-EC-Control/releases)** and download **`BladeFanControl.exe`**.
2. Double-click **`BladeFanControl.exe`** to open the app (runs cleanly with no black console popup).
3. From the window (or right-click the green fan icon in your Windows system tray):
   - **Switch performance modes** with one click: **Silent** (`30 W` GPU, quietest), **Balanced** (`80 W` GPU), **Gaming** (`90 W` max GPU), **Creator**, and **Custom** (**Low / Medium / High** CPU power).
   - **Control fan speed**: Switch between **Auto** (built-in temperature curve) and **Manual** RPM slider.
   - **Toggle Windows CPU Turbo Boost ON/OFF** independently — great for keeping temperatures and fan noise low while staying in Silent or Custom mode.
   - **Live telemetry**: View EC temperatures plus live NVIDIA GPU temperature, power draw, and active power cap.
   - **Minimize to System Tray**: Clicking minimize (`_`) hides the app to the tray and pauses GPU polling so your dGPU can sleep cleanly.

> **Using a different Razer Blade model?** Most 2018–2023 Razer Blades share the same USB HID fan/mode protocol and only differ by their USB Product ID (`PID`). You can test the app on another Blade by running `BladeFanControl.exe --pid 0xXXXX` from a shortcut or terminal (note that the displayed Watt/RPM labels are for `RZ09-0328`).

---

## 2. Unlock `0–5500 RPM` Manual Fan Control on `RZ09-0328` (Optional EC Mod)

On stock `RZ09-0328` firmware, manual fan control is heavily restricted — you cannot go below **4300 RPM** in Balanced mode, **4900 RPM** in Gaming/Creator/Custom, or **2400 RPM** in Silent mode. Even if Synapse lets you drag the slider lower, the EC silently ignores it.

With our offline patching script, you can unlock **full `0–5500 RPM` manual control across every mode** (so you can set **`0 RPM`** for complete silence, or **`2000–3500 RPM`** for quiet gaming/cooling — even in Gaming `90 W` or Custom High mode).

> [!CAUTION]
> **Read before flashing:** Flashing EC firmware always carries hardware risk if power is lost or the wrong file is flashed. **Never flash a ROM from a different laptop model**, keep your original AC adapter plugged in with battery > 50%, and follow the full step-by-step checklist in **[docs/FLASH_GUIDE.md](docs/FLASH_GUIDE.md)**.
> No copyrighted Razer firmware or flasher binaries are hosted here — you dump your own backup and patch it locally.

### Overview of the 4 Steps (Full Instructions in [docs/FLASH_GUIDE.md](docs/FLASH_GUIDE.md))
1. **Back up your own stock EC firmware (100% read-only, works on Windows 11 without disabling security settings)**:
   - Install [Python 3.10+](https://www.python.org/downloads/), download the two official signed [PawnIO](https://pawnio.eu/) files listed in [ec_research/README.md](ec_research/README.md) into `tools/pawnio/`, and double-click **`ec_research/EcRomDump.cmd`**.
   - In ~4.5 minutes it saves your laptop's exact 256 KiB stock EC backup (`ec-rom-00000-3ffff.bin`). Keep a safe copy of this file!
2. **Create your modded `0–5500 RPM` firmware offline**:
   ```powershell
   python ec_research/patch_ec_fan_range.py --stock path/to/ec-rom-00000-3ffff.bin --mirror-flag-ff
   ```
   *(Want custom CPU/GPU power limits too — like 25/35W Silent and 90W GPU in Custom Mode? See `ec_research/patch_ec_v2_power_fan.py`.)*
3. **Safely flash your modded firmware**:
   - Patch your copy of Razer's official `Razer_EC_Flash_v1.03.exe` with `python ec_research/patch_razer_flasher.py --in-exe Razer_EC_Flash_v1.03.exe` (fixes a serious bug in Razer's original flasher so it never reboots into a failed flash), or use `ec_research/ite_ec_flash_linux.py` on Linux for direct-eFlash boards.
   - Follow the exact flashing and rollback instructions in **[docs/FLASH_GUIDE.md](docs/FLASH_GUIDE.md)**.
4. **Verify & enjoy**:
   - Double-click **`ec_research/EcVerifyLive.cmd`** to confirm the mod is active, then open **`BladeFanControl.exe`** — drag the manual slider down to **`0 RPM`** to stop both fans completely, or pick any steady speed from **`~2000` to `5500 RPM`**!

---

## 3. What We Discovered (And How This Helps Other Razer Blade Owners)

Even if you own a **different Razer Blade model**, most Intel-era Blades using an **ITE `IT8390E`** embedded controller share the same dumping, analysis, and flashing mechanisms. Here are the key takeaways in plain English:

- **Hidden EC power & fan ceilings**: Each mode (`Balanced`, `Gaming`, `Creator`, `Custom`, `Silent`) lives in a table inside the EC firmware that sets CPU wattage limits (`PL1/PL2`), GPU power policy (`30 W / 80 W / 90 W`), auto fan curves, and manual RPM floors/ceilings. That is why Windows power settings alone cannot raise CPU power above the active EC mode — though lowering CPU wattage from Windows works cleanly without fighting the EC.
- **The `20%` fan speed rule (`0 RPM` snap)**: The EC enforces a hardware `20%` minimum PWM spin floor (because small 5 V laptop blower fans physically stall below ~15–20% duty). When you set **`0 RPM`**, the fans ramp down below 20% and **snap cleanly to a full stop (`0 RPM`)**. From **`~2000–5500 RPM`**, closed-loop speed control runs smoothly. Speeds between `100` and `~1800 RPM` sit below that 20% floor, so use `0 RPM` for silent stop or `2000+ RPM` for steady spin.
- **Dump your EC cleanly on Windows 11**: Instead of old blocked drivers (`WinRing0`) or unstable `0x62/0x66` polling (which fights Windows `ACPI.sys`), our dumper (`ec_research/ec_sio_probe.py`) uses the Microsoft-signed **PawnIO** driver to read the EC's built-in read-only mailbox cleanly with Memory Integrity still turned ON.
- **Two huge EC modding traps we solved**:
  1. **The mirror flag (`0x0008D = 0xFF`)**: On boards like `RZ09-0328` that mirror EC firmware from an external SPI chip (`GD25Q64`) into the EC's internal flash, a dumped ROM has byte `0x0008D = 0x00`. Unless you flip it to `0xFF` before flashing (including when rolling back to stock!), the EC silently ignores your flash and keeps booting the old copy.
  2. **Cached tables in BIOS flash**: The EC caches fan/power tables inside external BIOS flash. If you edit EC firmware without bumping the internal cache version numbers, the EC keeps loading the old cached settings instead of your changes.
- **Bug fixed in Razer's official flasher**: We discovered that Razer's `Razer_EC_Flash_v1.03.exe` still exits flash mode and forces a reboot even when post-flash verification fails. Our patcher (`patch_razer_flasher.py`) fixes this bug offline and builds a companion recovery binary so you can safely retry without ever rebooting into a bad flash.
- **Pure-Python EC reverse-engineering tools**: Includes an offline `NDS32` disassembler (`dis_nds32.py`) and ROM table analyzer (`analyze_ec_rom.py`) so owners of other `IT8390E` Blades can inspect and compare their own EC dumps.

---

## 4. Deep-Dive Guides & Project Layout

| Path | What's Inside |
| :--- | :--- |
| **[Releases](https://github.com/GepingY/Blade-EC-Control/releases)** | Pre-built standalone **`BladeFanControl.exe`** (no Python, no console window) |
| **[docs/FLASH_GUIDE.md](docs/FLASH_GUIDE.md)** | Step-by-step EC dump, patch, flash, live verification & stock rollback guide |
| **[FINDINGS.md](FINDINGS.md)** | Full technical reference for tinkerers (memory maps, registers, ACPI/PECI/GPU details, hidden `0xFE`/`0xFF` direct-PWM modes) |
| **[docs/ACPI_FAN_ANALYSIS.md](docs/ACPI_FAN_ANALYSIS.md)** | Why BIOS/ACPI/DPTF fan settings don't control the physical fans |
| **[ec_research/](ec_research/README.md)** | EC dumper, live verifier, `NDS32` disassembler, ROM analyzer, v1/v2 patchers, Linux flasher & Razer flasher safety patcher |
| **[gui/](gui/README.md)** | Python source code for `BladeFanControl.exe` + Quiet/Performance preset scripts |

---

## Credits
- **[PawnIO](https://pawnio.eu/)** (`namazso/PawnIO` & `namazso/PawnIO.Modules`) — WHCP-signed driver enabling safe, HVCI-compatible EC ROM dumping on Windows 11.
- **[blade-control](https://github.com/jdsgnrinfo/blade-control)** & **[razer-ctl](https://github.com/sqmagellan/razer-ctl)** — Prior open-source Razer HID protocol references.
- **[ACPICA `iasl`](https://github.com/open-acpica/acpica)** — ACPI table disassembly.
