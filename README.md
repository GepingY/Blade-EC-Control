# Razer Blade Fan, Power & EC Control
This repo contains:
- A GUI app to control the fan speed / Mode for many Razer Blades
- EC Mod tutorial to patch your own EC to unlock all fan speed manual control, Unlock Higher or lower TDP, Change GPU TDP mode
- Documented discovery for those who is interested on how does the EC of Razer Blade work and how does Razer Synapse communicate with it and how does the EC controls the Razer Blade(For this check FINDINGS.MD)
---

## 1. The GUI App

- For **Razer Blade 15 Base (Early 2020, `RZ09-0328`)**, you can use the controller immediately with no risk:

  Go to the **[Releases page](https://github.com/GepingY/Blade-EC-Control/releases)** and download **`BladeFanControl.exe`**, then you can use that app to control the fan with limited range of rpm.

- For other Razer Blades:

  Given that most razer blades share the same method of fan/mode control(The same method used by Synapse), the app should also work on other razer blades, do the same procedure as given above for `RZ09-0328` and it normally would work. There is chance that it won't work because I haven't tested on other blades yet, but it won't do any damage if doesn't work.

> A not modded Razer Blade will have limitation to how much can you adjust it's fan RPM, every mode other than factory test mode will have a specific range of RPM allowed for manual rpm control. Different razer blade will have different limits with un-modded EC, for example `Rz09-0328` have different limit compared to `Rz09-0330`.
>
> The limit of `Rz09-0328` is given as below:
>
> | Mode | Name | Auto RPM range | Auto steps | Manual RPM min | Manual RPM max | Manual default |
>| :---: | :--- | :--- | :---: | :---: | :---: | :---: |
>| 0 | Balanced | 0 (stop) or 2500–5300 | 0–9 | 4300 | 5500 | 4900 |
>| 1 | Gaming | 0 (stop) or 2700–5500 | 0–9 | 4900 | 5500 | 5200 |
>| 2 | Creator / Perf | 0 (stop) or 2700–5500 | 0–9 | 4900 | 5500 | 5200 |
>| 3 | Legacy | 0 (stop) or 2700–5500 | 0–9 | 4900 (requests below this give 0 RPM, a firmware quirk) | 5500 | – |
>| 4 | Custom | 0 (stop) or 2700–5500 | 0–9 | 4900 | 5500 | 5200 |
>| 5 | Silent | 0 (stop) or 2200–3250 | 0–7 | 2400 | 5000 | 3100 |
>| 6, 7 | Silent clones | 0 (stop) or 2200–3250 | 0–7 | 2400 | 5000 | 3100 |
---

>[!WARNING]
>Due to the electronic physical limit of a laptop fan, it can't operate reliably at low RPM, for my razer blade it would appear that the fan might come to stop unexpectedly or not starting at all for RPM of range `1-1000`, though there is probably no harm of setting such RPM other than it might not always run. RPM of `1500` and above seems to be able to keep the fan running reliably. If you want the fan to keep running but as silent as possible, a good range I would recommend is `1500-2200` RPM.

## 2. EC MOD

**On stock firmware, manual fan control is heavily restricted which has been show above. The way to remove the limit is to reprogram/patch the EC ourself by rewriting the firmware partially**

There isn't a limit to how much you can change the EC, as far as I am concerned, you can reprogram the EC to send morse code if you want, but that is pointless, normally you would rewrite the EC to change the fan limit or TDP limit.
With my offline patching script, you can unlock **full `0–5500 RPM` manual control across every mode**

> [!CAUTION]
> **Read before flashing:** Flashing EC firmware always carries hardware risk if power is lost or the wrong file is flashed. **Never flash a ROM from a different laptop model**, follow the full step-by-step checklist in **[docs/FLASH_GUIDE.md](docs/FLASH_GUIDE.md)**.
> No copyrighted Razer firmware or flasher binaries are hosted here — you dump your own backup and patch it locally.


### 1. How to extract/dump the EC firmware binary of a Razer Blade
   - Install [Python 3.10+](https://www.python.org/downloads/) if you don't have python installed already, download the two official signed [PawnIO](https://pawnio.eu/) files listed in [ec_research/README.md](ec_research/README.md) into `tools/pawnio/`, and double-click **`ec_research/EcRomDump.cmd`**.
   - In ~4.5 minutes it saves your laptop's exact 256 KiB stock EC backup (`ec-rom-00000-3ffff.bin`). Keep a safe copy of this file!
  

3. **Create your modded `0–5500 RPM` firmware offline**:
   ```powershell
   python ec_research/patch_ec_fan_range.py --stock path/to/ec-rom-00000-3ffff.bin --mirror-flag-ff
   ```
   *(Want custom CPU/GPU power limits too — like 25/35W Silent and 90W GPU in Custom Mode? See `ec_research/patch_ec_v2_power_fan.py`.)*
4. **Safely flash your modded firmware**:
   - Patch your copy of Razer's official `Razer_EC_Flash_v1.03.exe` with `python ec_research/patch_razer_flasher.py --in-exe Razer_EC_Flash_v1.03.exe` (fixes a serious bug in Razer's original flasher so it never reboots into a failed flash), or use `ec_research/ite_ec_flash_linux.py` on Linux for direct-eFlash boards.
   - Follow the exact flashing and rollback instructions in **[docs/FLASH_GUIDE.md](docs/FLASH_GUIDE.md)**.
5. **Verify & enjoy**:
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
