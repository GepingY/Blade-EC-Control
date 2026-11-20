# Razer Blade Fan, Power & EC Control
This repo contains:
- A GUI app to control the fan speed / Mode for many Razer Blades
- EC Mod tutorial to patch your own EC to unlock all fan speed manual control, Unlock Higher or lower TDP, Change GPU TDP mode
- Documented discovery for those who is interested on how does the EC of Razer Blade work and how does Razer Synapse communicate with it and how does the EC controls the Razer Blade(For this check [FINDINGS.MD](https://github.com/GepingY/Blade-EC-Control/blob/main/FINDINGS.md))
---

## 1. The GUI App

- For **Razer Blade 15 Base (Early 2020, `RZ09-0328`)**, you can use the controller immediately with no risk:

  Go to the **[Releases page](https://github.com/GepingY/Blade-EC-Control/releases)** and download **`BladeFanControl.exe`**, then you can use that app to control the fan with limited range of rpm.

- For other Razer Blades:

  Given that most razer blades share the same method of fan/mode control(The same method used by Synapse), the app should also work on other razer blades, do the same procedure as given above for `RZ09-0328` and it normally would work. There is chance that it won't work because I haven't tested on other blades yet, but it won't do any damage if doesn't work.

> A not modded Razer Blade will have limitation to how much can you adjust it's fan RPM, every mode other than factory test mode will have a specific range of RPM allowed for manual rpm control. Different razer blade will have different limits with un-modded EC, for example `Rz09-0328` have different limit compared to `Rz09-0330`. According to EC firmware codes/instructions EC will return successful when you set RPM manually outside the RPM range and it will set the RPM to the closest available RPM, for example assuming the `Manual RPM min` is 2000, and you set 1500, EC will return success but set RPM to 2000 instead of 1500
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


### 1. How to dump the EC firmware
1. If you already have python installed locally proceed to step 2, if not: install [Python 3.10+](https://www.python.org/downloads/) and tick **"Add python.exe to PATH"**.
2. Download this repository.
3. Enter the folder of this repository that you(Blade-EC-Control) and double-click `ec_research/EcRomDump.cmd` and click **Yes** on the UAC prompt. The script installs PawnIO temporarily, reads the EC, then uninstalls PawnIO.
4. Wait for "Press any key". Your EC is then saved as `ec-rom-00000-3ffff.bin` in the run folder. 
  
### 2. How to modify the EC you extracted
After extracting the EC in step 1, you might want to modify it to change settings such as fan RPM restriction or CPU TDP or GPU TDP or other stuff, for this read below:

**You have 3 method of patching/modding the EC(The first and third is the most recommended):**
1. Use the auto-patcher program provided in release page
2. Manually read through the structure of the EC and manually reprogram it
3. Ask a AI agent to do it for you

- **Method 1**: Auto-Patcher
1. Download RazerECAutoPatcher.exe
2. Double click it to run it and in the user interface load the `ec-rom-00000-3ffff.bin` you extracted earlier
3. 2 thing might happen after you load the binary, either your bin matches the signature and the program is able to recognize the bytes of your binary, then you can proceed to the next step. If the program fails to recognize the signature pattern of your binary then you will have to try Method 2 or 3, RazerECAutoPatcher.exe can't help you.
4. If the program is able to recognize your `.bin` then congrats! You can now edit your binary inside the app.
5. After finishing your edits, run a verification simulation to check if the new patched firmware will work in the EC by clicking "Verify & Preview (NDS32 Emulator)", if verification passes, then click "Save Patched ROM(.bin)"
6. All done! Proceed to flashing the ROM you just patched!

- **Method 2**: Manual programming
Read all the MD of this repo including FINDINGS.MD and then hopefully you will understand how to do it :)

- **Method 3**: Ask for the help of a AI Agent
Just send the URL of this repository to a AI you think is the smartest and send it the binary of the EC you extracted and ask it to parse it and give you options to mod it. Best if you do it with a Agent instead of a web AI model, for reference ChatGPT Sol 6+ MAX/ Astra 6.2 Max in codex can definitely do it, Gemini Neon/Carbon/Hydrogen/Argon in google antigravity has no problem as well.

### 3. How to Flash the binary of the EC
> [!CAUTION]
> **Read before flashing:** Flashing EC firmware always carries hardware risk if power is lost or the wrong file is flashed. **Never flash a ROM from a different laptop model**, follow the full step-by-step checklist in **[docs/FLASH_GUIDE.md](docs/FLASH_GUIDE.md)**.
> No copyrighted Razer firmware or flasher binaries are hosted here — you dump your own backup and patch it locally.
> 
> **Disclaimer:** Use at your own risk. Flashing or modifying EC firmware can permanently brick your device, and this repository or the owner of this repository is not responsible for any damage.

#### Before You Flash (Mandatory Checklist)
1. **Verify your backup & patched binary:**
   - Make sure your dumped stock backup (`ec-rom-00000-3ffff.bin`) is saved off-machine (e.g., on a USB drive).
   - **Mirror Flag (`0x0008D = 0xFF`):** On Razer Blades that mirror EC firmware from an external SPI flash chip (`GD25Q64`) into the `IT8390E`'s internal 256 KiB eFlash on boot (such as `RZ09-0328`), a raw dump from internal eFlash has byte `0x0008D = 0x00`. **Both your patched `.bin` and your stock rollback `.bin` must have `0x0008D` set to `0xFF`**, or the EC will silently ignore the flash on reboot.
   - **External SPI Cache Versions:** If you modified mode/fan/power tables (`0x3D000`) or runtime defaults (`0x2098C` / `0x10070`), make sure the internal SPI cache version checks (`0x3FE000` and `0x050000`) were bumped so the EC rebuilds its cached tables on first boot.
2. **Prepare the laptop:**
   - Plug in the **original Razer barrel AC adapter** and make sure battery charge is **> 50%**.
   - Close **Razer Synapse**, **`BladeFanControl.exe`**, **HWiNFO**, **MSI Afterburner**, and any other hardware/sensor monitoring software.
   - Disable sleep/hibernation and keep an external USB keyboard plugged in as a precaution.
---
#### Option A: Flash on Windows (External Mirror SPI Boards like `RZ09-0328`, or Any `IT8390E` Blade)
> [!IMPORTANT]
> **Never run the unpatched `Razer_EC_Flash_v1.03.exe`.** Razer's original flasher has a bug where a verification failure still exits Follow Mode (`0xFC`) and forces a reboot. Always patch it first with `patch_razer_flasher.py`.
1. **Get & patch Razer's official EC flasher:**
   - Extract `Razer_EC_Flash_v1.03.exe` and its accompanying `WinRing0x64.sys` / `WinRing0x64.dll` files from an official Razer EC updater package into a working folder.
   - Run our safety patcher on it:
     ```powershell
     python ec_research/patch_razer_flasher.py --in-exe path/to/Razer_EC_Flash_v1.03.exe
     ```
   - This generates two safe executables in the same folder:
     - **`Razer_EC_Flash_v1.03_safe.exe`**: Aborts before `0xFC` (Exit Idle) and skips rebooting (`exit code 2`) if post-flash verification ever fails, keeping the EC safely parked in RAM Follow Mode.
     - **`Razer_EC_Flash_v1.03_recovery_in_follow_mode.exe`**: Used to immediately re-flash without rebooting if `_safe.exe` ever reports a verify mismatch.
2. **Configure `ToolConfig.ini`:**
   - Delete any foreign `.bin` files that came with the Razer updater so you cannot accidentally flash the wrong model.
   - Copy your patched `.bin` (and your stock rollback `.bin`) into the flasher folder and edit `ToolConfig.ini`:
     ```ini
     [ToolConfig]
     FILE=your-patched-ec-8dff.bin
     SSID=2011
     REBOOT=0
     ```
     *(Replace `2011` with your own board's `SSID` reported by `EcSioProbe.cmd`!)*
3. **Allow `WinRing0x64.sys` to load temporarily:**
   - Because Razer's flasher uses `WinRing0x64.sys`, Windows 11 blocks it by default. Temporarily turn off **Memory Integrity (HVCI)** and the **Microsoft Vulnerable Driver Blocklist** in *Windows Security → Device Security → Core Isolation* and reboot (or boot into a WinPE USB).
4. **Run the flash:**
   - Open **Command Prompt as Administrator**, `cd` into your flasher folder, and run:
     ```cmd
     Razer_EC_Flash_v1.03_safe.exe
     ```
   - **If it prints `Update <your-file>.bin file ok!` (with NO `.......Fail` line):**
     1. Perform a **full Windows shutdown** (do not just click Restart).
     2. Wait **10 seconds**, then press the power button to turn the laptop back on (the EC mirrors the new firmware on cold boot).
     3. Turn **Memory Integrity (HVCI)** and the **Driver Blocklist** back ON in Windows Security.
   - **If it EVER prints `.......Fail` (`%ERRORLEVEL% == 2`):**
     - **DO NOT reboot or power off!**
     - Immediately run `Razer_EC_Flash_v1.03_recovery_in_follow_mode.exe` in the same Administrator Command Prompt (or point `ToolConfig.ini` to your stock rollback `.bin` first and run `_recovery_in_follow_mode.exe`) until it prints `Update <file> file ok!`.
---
#### Option B: Flash on Linux Live USB (Direct Internal eFlash Boards where `SPI ID == FF FF FE`)
If your Blade's `IT8390E-256` flashes internal eFlash directly (`SPI ID == FF FF FE` rather than an external `GD25Q64`), you can use our pure-Python Linux flasher (`ec_research/ite_ec_flash_linux.py`), which includes a **100% read-only `--verify-only` dry run** and automatic per-sector retries without needing `WinRing0`:
1. Temporarily disable **Secure Boot** in BIOS (Linux blocks `/dev/port` under Secure Boot lockdown) and boot a **Linux Live USB** (e.g., Ubuntu).
2. **Read-only board check:**
   ```bash
   sudo python3 ec_research/ite_ec_flash_linux.py --check-only
   ```
3. **Mandatory read-only dry run (zero erase / zero write):**
   ```bash
   sudo python3 ec_research/ite_ec_flash_linux.py --verify-only --stock path/to/ec-rom-00000-3ffff.bin
   ```
   *(If this reports an external SPI chip ID instead of `FF FF FE`, it will safely abort without touching flash — use **Option A** instead.)*
4. **Flash the patched ROM (only after `--verify-only` passes 100%):**
   ```bash
   sudo python3 ec_research/ite_ec_flash_linux.py --flash path/to/your-patched-ec.bin --i-accept-brick-risk
   ```
5. Wait for `FLASH & 256 KiB VERIFY SUCCEEDED 100%`, shut down completely (`sudo poweroff`), wait 10 seconds, and power back on.
---
#### Post-Flash Verification & Rolling Back to Stock
- **Verify live:** Back in Windows (with HVCI re-enabled), double-click **`ec_research/EcVerifyLive.cmd`** (or run `python ec_research/ec_sio_probe.py --verify-live`) to confirm your patched bytes, cache version, and new RPM/power limits are active in live EC RAM.
- **Rollback to stock anytime:** Flash your stock backup `.bin` (with `0x0008D = 0xFF` on mirror boards) using the exact same steps above. On first boot, the stock firmware detects the bumped cache version and automatically rebuilds the external BIOS SPI caches back to factory defaults.


















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

## ⭐ Support This Project

If you find this repository useful, please consider giving it a **star ⭐**!

Your support helps more people discover this project and motivates further development and improvements.

**Thank you for your support!**


## Deep-Dive Guides & Project Layout

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
