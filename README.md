# Razer Blade Fan, Power & EC Control
This repo contains:
- A GUI app to control the fan speed / Mode for many Razer Blades
- EC Mod tutorial to patch your own EC to unlock all fan speed manual control, Unlock Higher or lower TDP, Change GPU TDP mode
- Documented discovery for those who is interested on how does the EC of Razer Blade work and how does Razer Synapse communicate with it and how does the EC controls the Razer Blade(For this check [FINDINGS.MD](https://github.com/GepingY/Blade-EC-Control/blob/main/FINDINGS.md))

 The process and mechanism is very complicated(it took me a day to just write the MarkDown files), it's advised that you directly ask your trusted AI how to use

Want to share a hardware finding or improve either app? See the [contributing guide](CONTRIBUTING.md).

## 1. The GUI App

- For **Razer Blade 15 Base (Early 2020, `RZ09-0328`)**, you can use the controller immediately:

  Go to the **[Releases page](https://github.com/GepingY/Blade-EC-Control/releases)** and download **`BladeFanControl.exe`**, then you can use that app to control the fan with limited range of rpm.

- For other Razer Blades:

  Given that most razer blades share the same method of fan/mode control(The same method used by Synapse), the app should also work on other razer blades, do the same procedure as given above for `RZ09-0328` and it normally would work. There is chance that it won't work because I haven't tested on other blades yet, but it won't do any damage if doesn't work.

> A not modded Razer Blade will have limitation to how much can you adjust it's fan RPM, every mode other than factory test mode will have a specific range of RPM allowed for manual rpm control. Different razer blade will have different limits with un-modded EC, for example `Rz09-0328` have different limit compared to `Rz09-0330`. According to EC firmware codes/instructions EC will return successful when you set RPM manually outside the RPM range and it will set the RPM to the closest available RPM, for example assuming the `Manual RPM min` is 2000, and you set 1500, EC will return success but set RPM to 2000 instead of 1500. Normally the "Silent" mode give you the biggest range of manual fan control.
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


### 1. How to dump/extract the EC firmware
1. If you already have python installed locally proceed to step 2, if not: install [Python 3.10+](https://www.python.org/downloads/) and tick **"Add python.exe to PATH"**.
2. Download this repository.
3. Enter the folder of this repository that you(Blade-EC-Control) and double-click `ec_research/EcRomDump.cmd` and click **Yes** on the UAC prompt. The script installs PawnIO temporarily, reads the EC, then uninstalls PawnIO.
4. Wait for "Press any key". Your EC backup is saved inside `diagnostics/rom-dump-<timestamp>/ec-rom-00000-3ffff.bin` (the exact folder path is printed in the window).

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
5. After finishing your edits, Go to tab Signature Scan & Diff and check the check box `Force 0xff (SPI mirror)`.
6. run a verification simulation to check if the new patched firmware will work in the EC by clicking "Verify & Preview (NDS32 Emulator)", if verification passes proceed to next step, if not create a issue
7. Press "Save Patched ROM (.bin)..." to save
8. All done! Proceed to flashing the ROM you just patched!

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
2. **Prepare the laptop:**
   - Make sure your laptop won't loss power during the process by plug in charger and make sure battery is full enough
   - Close **Razer Synapse**, **`BladeFanControl.exe`**, **HWiNFO**, **MSI Afterburner**, and any other hardware/sensor monitoring software.
   - Disable auto sleep/hibernation in windows setting in case your blades decides to sleep

---

#### Option A: Flash on Windows (External Mirror SPI Boards like `RZ09-0328`, or Any `IT8390E` Blade)
> [!IMPORTANT]
> **Never run the unpatched `Razer_EC_Flash_v1.03.exe`.** Razer's original flasher has a bug where a verification failure still exits Follow Mode (`0xFC`) and forces a reboot. Always patch it first with `patch_razer_flasher.py`.

1. **Unpack Razer's official EC flasher (`Razer_EC_Flash_v1.03.exe`) and patch its verify-failure bug(that's right, razer's own EC updater have bugs):**
   - Download Razer's official EC updater package [RazerUpdater_v1.1.1.2_CH551-ECv0101.exe](https://rzr.to/iCAmd)
     *(this works for blades of other model as well)*
   - **Extract the installer without running the update** using either method:
     - **Method 1 — 7-Zip (Recommended, never executes the installer):** Right-click `RazerUpdater_v1.1.1.2_CH551-ECv0101.exe` → **7-Zip → Extract to "RazerUpdater_v1.1.1.2_CH551-ECv0101\"** (or open it inside 7-Zip File Manager and drag the payload files out).
     - **Method 2 — From `%TEMP%`:** Launch `RazerUpdater_v1.1.1.2_CH551-ECv0101.exe` but **stop at the welcome screen (do NOT click Update)**. Press `Win + R`, type `%TEMP%`, press Enter, open the newly created temporary Razer folder, copy the payload files out, and close the updater window.
   - Create a clean working folder (e.g., `ec_flash\`) and copy **only these 6 files** into it:
     - `Razer_EC_Flash_v1.03.exe` (`46,592` bytes, SHA-256: `3590f72531078eb9562f8ed864753ea710a1d2723f693923b99b3a981b78f07b`)
     - `ToolConfig.ini`
     - `WinRing0x64.dll` & `WinRing0x64.sys`
     - `WinRing0.dll` & `WinRing0.sys`
   - **Delete `CH551_EC_01_01.bin` immediately** so you can never accidentally flash the `RZ09-0330` ROM onto your board.
   - Run our offline `.NET` safety patcher against `Razer_EC_Flash_v1.03.exe`, change the path as needed:
     ```powershell
     python ec_research/patch_razer_flasher.py --in-exe ec_flash/Razer_EC_Flash_v1.03.exe --out-dir ec_flash
     ```
   - This creates two safe executables inside `ec_flash\`:
     - **`Razer_EC_Flash_v1.03_safe.exe`**: Fixes Razer's verify-failure bug so if verification ever fails, it skips `0xFC` (Exit Idle) and exits with code `2` instead of forcing a reboot into a bad flash.
     - **`Razer_EC_Flash_v1.03_recovery_in_follow_mode.exe`**: Allows immediate re-flashing in the same Command Prompt if `_safe.exe` ever reports a verification mismatch.
2. **Configure `ToolConfig.ini`:**
   - Delete any foreign `.bin` files that came with the Razer updater so you cannot accidentally flash the wrong model.
   - Copy your patched `.bin` (and your stock rollback `.bin`) into the flasher folder and edit `ToolConfig.ini`:
     ```ini
     [ToolConfig]
     FILE=your-patched-ec-8dff.bin
     SSID=2011
     REBOOT=0
     ```
     *(Replace `2011` with your own board's 4-digit hex `SSID` — shown in the `EcRomDump.cmd` console output and inside `diagnostics/rom-dump-<timestamp>/probe.json` under `"ssid"`, without the `0x` prefix.)*
3. **Allow `WinRing0x64.sys` to load temporarily:**
   - Because Razer's flasher uses `WinRing0x64.sys`, Windows 11 blocks it by default. Temporarily turn off **Memory Integrity (HVCI)** and the **Microsoft Vulnerable Driver Blocklist** in *Windows Security → Device Security → Core Isolation* and you must reboot before continue.
4. **Run the flash:**
   - Open **Command Prompt as Administrator**, `cd` into your flasher folder, and run:
     ```cmd
     Razer_EC_Flash_v1.03_safe.exe
     ```
   - **If it prints `Update <your-file>.bin file ok!` (with NO `.......Fail` line):**
     1. Perform a **full Windows shutdown** (do not just click Restart).
     2. Wait **10 seconds**, then press the power button to turn the laptop back on (the EC mirrors the new firmware on cold boot).
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

---

**Enjoy!**



## ⭐ Support This Project

If you find this repository useful, please consider giving it a **star ⭐**!

If you successfully extracted or flashed the EC on your razer blade please create a issue or send me a email with your razer blade model so I can add it to supported device list

Your support helps more people discover this project and motivates further development and improvements.

**Thank you for your support!**


## Deep-Dive Guides & Project Layout

| Path | What's Inside |
| :--- | :--- |
| **[Releases](https://github.com/GepingY/Blade-EC-Control/releases)** | Pre-built standalone **`BladeFanControl.exe`** & **`RazerECAutoPatcher.exe`** |
| **[docs/FLASH_GUIDE.md](docs/FLASH_GUIDE.md)** | Step-by-step EC dump, patch, flash, live verification & stock rollback guide |
| **[FINDINGS.md](FINDINGS.md)** | Full technical reference for tinkerers (memory maps, registers, ACPI/PECI/GPU details, hidden `0xFE`/`0xFF` direct-PWM modes) |
| **[docs/ACPI_FAN_ANALYSIS.md](docs/ACPI_FAN_ANALYSIS.md)** | Why BIOS/ACPI/DPTF fan settings don't control the physical fans |
| **[ec_research/](ec_research/README.md)** | EC dumper, live verifier, `NDS32` disassembler, ROM analyzer, v1/v2 patchers, Linux flasher & Razer flasher safety patcher |
| **[controller/](controller/README.md)** | Python source code for `BladeFanControl.exe` + Quiet/Performance preset scripts |
| **[patcher/](patcher/README.md)** | Python core for the offline EC Auto-Patcher |

---

## Credits
- **[PawnIO](https://pawnio.eu/)** (`namazso/PawnIO` & `namazso/PawnIO.Modules`) — WHCP-signed driver enabling safe, HVCI-compatible EC ROM dumping on Windows 11.
- **[blade-control](https://github.com/jdsgnrinfo/blade-control)** & **[razer-ctl](https://github.com/sqmagellan/razer-ctl)** — Prior open-source Razer HID protocol references.
- **[ACPICA `iasl`](https://github.com/open-acpica/acpica)** — ACPI table disassembly.
