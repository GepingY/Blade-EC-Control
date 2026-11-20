# `IT8390E-256` EC Dump, Patch, Flash & Rollback Guide

> [!CAUTION]
> **Read every warning before touching any EC flasher.**
> - Never flash a firmware image dumped from a different laptop model or Board `SSID`.
> - `IT8390E-256` runs from a **single 256 KiB internal eFlash bank** with **no A/B fallback bootloader**.
> - On boards without external SPI mirroring (or if internal eFlash / Follow Mode is corrupted), recovery requires a dedicated ITE EDI programmer (e.g., SVOD / Vertyanov / RT809H) via keyboard FPC lines or chip replacement.
> - **Never run the unpatched `Razer_EC_Flash_v1.03.exe`** — it has a verified verify-failure bug that sends `0xFC` (Exit Idle) and forces a reboot even after a verification mismatch.
> - Always stay on original barrel AC power with battery **> 50%**, close all sensor/HID tools (Synapse, HWiNFO, Afterburner, `blade_fan_gui.py`), and keep the laptop awake.

---

## Step 1 — Dump Your Own Golden Stock EC ROM (Read-Only, Windows 11 HVCI-Safe)

1. Download official signed PawnIO assets (no need to disable Windows 11 Memory Integrity / Secure Boot):
   - **`PawnIO_setup.exe`** (`v2.2.0`): `https://github.com/namazso/PawnIO.Setup/releases/tag/2.2.0`
     - Expected SHA-256: `1f519a22e47187f70a1379a48ca604981c4fcf694f4e65b734aaa74a9fba3032`
   - **`release_0_2_12.zip`** (`v0.2.12`, contains signed `LpcIO.bin`): `https://github.com/namazso/PawnIO.Modules/releases/tag/0.2.12`
     - Expected SHA-256: `09698897ce19b92f1043a2e322cfcf0b1fe823e0b576971b178a65244fb0af9d`
   - Place both files inside `tools/pawnio/` in this repository (or pass `--pawnio-dir <folder>`).
2. Run offline verification:
   ```powershell
   python ec_research/ec_sio_probe.py --check
   ```
3. Run read-only Super I/O & BRAM discovery (`EcSioProbe.cmd` or elevated):
   ```powershell
   python ec_research/ec_sio_probe.py --run
   ```
   Confirm `chip_id = 0x8390`, `reg22 = 0x82` (256 KiB), active BAR `0x0302`, and record your Board `SSID` and EC version from mailbox `0x300`.
4. Dump all 256 KiB (`~4.5 minutes`, read-only via BRAM0 `0x0302` cmd `0x90`; PawnIO auto-uninstalls afterward):
   ```powershell
   python ec_research/ec_sio_probe.py --dump
   ```
   Back up the dumped `ec-rom-00000-3ffff.bin` safely off-machine (USB drive / cloud backup) before doing anything else!

---

## Step 2 — Analyze & Patch Offline

Inspect your stock dump offline:
```powershell
python ec_research/analyze_ec_rom.py path/to/ec-rom-00000-3ffff.bin --steps
python ec_research/dis_nds32.py --bin path/to/ec-rom-00000-3ffff.bin 12356:123a0
```

### Critical Check: External SPI Mirror Flag (`0x0008D = 0xFF`)
On `RZ09-0328` (`DA550-MB`) — and potentially other `IT8390E` Blades wired with an external `GD25Q64` SPI chip — the flasher writes external SPI, and `IT8390E` mirrors it into internal eFlash on boot **only when ITE signature byte 13 (`0x0008D`) is `0xFF`** (after mirroring, the hardware clears `0x0008D` back to `0x00` in internal eFlash and sets `FLHCTRL3R` `0x00F01063` bit 7 = `1`).
- Always check whether your official vendor EC updater image for your Blade generation has `0x0008D == 0xFF`.
- If your board mirrors from external SPI, **both your modded image AND your stock rollback image must set `0x0008D = 0xFF`**.

### Generating `RZ09-0328 EC v01.00` Example Mods
Both patchers verify the exact stock `RZ09-0328 v01.00` SHA-256 (`c676ff55da486c07cb8897c2e4bcb891e3140463864dbde7f615cef4c5a4b95d`) before modifying anything:
```powershell
# v1: 0–5500 RPM manual clamps + 0x3FE000 cache v2->v3 + 0x0008D=0xFF mirror flag
python ec_research/patch_ec_fan_range.py --stock path/to/ec-rom-00000-3ffff.bin --mirror-flag-ff

# v2: 0–5500 RPM + Custom TDP (35/45, 60/80, 60/90W) + 90W GPU Custom Low/Med + Silent 25/35W + cache v4/v8 + mirror-ready stock rollback
python ec_research/patch_ec_v2_power_fan.py --stock path/to/ec-rom-00000-3ffff.bin
```

---

## Step 3 — Choosing the Right Flashing Path

### Path A: Direct Internal eFlash (`SPI ID == FF FF FE`) — Linux Live USB (`ite_ec_flash_linux.py`)
Use this path on `IT8390E-256` boards whose Follow-Mode SPI ID is **`FF FF FE`** (direct internal eFlash, `0xD7` 1 KiB erase + `0xAD` AAI-word program). It provides a **read-only `--verify-only` dry run**, automatic in-Follow-Mode per-sector retry, and a fail-safe guard refusing `0xFC` on verify failure — without `WinRing0` or disabling Windows HVCI.

1. Disable Secure Boot temporarily in BIOS (Linux kernel lockdown blocks `/dev/port` when Secure Boot is enabled) and boot a Linux Live USB.
2. **Board check (read-only)**:
   ```bash
   sudo python3 ec_research/ite_ec_flash_linux.py --check-only
   ```
3. **Mandatory dry run (ZERO erase / ZERO write)**:
   ```bash
   sudo python3 ec_research/ite_ec_flash_linux.py --verify-only --stock path/to/ec-rom-00000-3ffff.bin
   ```
   *(If your board uses an external mirror SPI chip such as `GD25Q64`, `--verify-only` will cleanly report the non-`FF FF FE` SPI ID and abort without touching flash — use Path B instead!)*
4. **Flash (only after `--verify-only` passes 100%)**:
   ```bash
   sudo python3 ec_research/ite_ec_flash_linux.py --flash path/to/patched.bin --i-accept-brick-risk
   ```
5. Wait for `FLASH & 256 KiB VERIFY SUCCEEDED 100%`, shut down (`sudo poweroff`), wait 10 s, and power back on.

---

### Path B: External Mirror SPI (`GD25Q64`, e.g., `RZ09-0328`) or Windows — Patched `Razer_EC_Flash_v1.03_safe.exe`

Obtain `Razer_EC_Flash_v1.03.exe` (`46,592` bytes, SHA-256 `3590f72531078eb9562f8ed864753ea710a1d2723f693923b99b3a981b78f07b`) and its accompanying `WinRing0*` files from an official Razer EC updater package for your laptop family, then **patch it offline before running**:

```powershell
python ec_research/patch_razer_flasher.py --orig path/to/Razer_EC_Flash_v1.03.exe
```

This emits two `.NET 4.8`-verified executables beside the original:
1. **`Razer_EC_Flash_v1.03_safe.exe`**: Fixes the verify-failure bug — on any mismatch it prints `.......Fail`, skips `Verify 100%`, skips `EC_Exit_Idle (0xFC)`, skips `DoExitWin` even if `REBOOT=1`, and exits with `%ERRORLEVEL% == 2`, keeping the EC safely parked in its RAM-resident Follow-Mode loop on `0x202/0x206`.
2. **`Razer_EC_Flash_v1.03_recovery_in_follow_mode.exe`**: Keeps all safety fixes AND skips `Send_EC_Update_Qevent` + `EC_Write_Enable` (`0xDC`/`0x33`), because the EC's Follow-Mode loop ignores a second `0xDC`.

#### Running Safely
1. Place your target `.bin` (with `0x0008D = 0xFF` for mirror boards) and your mirror-ready stock rollback `.bin` in the flash folder, and configure `ToolConfig.ini`:
   ```ini
   [ToolConfig]
   FILE=your-patched-rom-8dff.bin
   SSID=2011
   REBOOT=0
   ```
   *(Replace `2011` with your own board's `SSID` from Step 1! Remove any foreign model `.bin` files from the folder.)*
2. Because `WinRing0x64.sys` is blocked by Windows 11 Memory Integrity (HVCI) and the Vulnerable Driver Blocklist, temporarily disable them (or boot WinPE without HVCI/blocklist), and plug in an external USB keyboard as an extra precaution.
3. From an **Administrator** Command Prompt in the flash folder, run:
   ```cmd
   Razer_EC_Flash_v1.03_safe.exe
   ```
4. **If it prints `Update <file> file ok!` (with NO `.......Fail` line)**: Perform a full shutdown (not just Restart), wait 10 seconds, power back on, and re-enable Windows Security / HVCI.
5. **If it EVER prints `.......Fail` (exit code `2`)**: **DO NOT REBOOT OR POWER OFF!** Immediately run `Razer_EC_Flash_v1.03_recovery_in_follow_mode.exe` in the same Administrator Command Prompt (either retrying the patched `.bin`, or replacing `ToolConfig.ini` to point to your stock rollback `.bin`) until it prints `Update <file> file ok!`.

---

## Step 4 — Post-Flash Live Verification & Clean Rollback

- **Live read-only verification** (re-enable HVCI first — PawnIO works with HVCI ON!):
  ```powershell
  python ec_research/ec_sio_probe.py --verify-live
  ```
  On `RZ09-0328`, confirm:
  - `mmio_flhctrl3r_f01063 = "0x88"` (bit 7 = mirror completed)
  - Patched ROM bytes and SRAM cache version (`v3` for v1 mod, `v4` / defaults `v8` for v2 mod) match
  - `sram_active_manual_min_rpm = 0`, `sram_active_manual_max_rpm = 5500`
- **Clean Rollback to Stock**: Flash your mirror-ready stock ROM (`0x0008D = 0xFF`). Because stock firmware checks `cache_version != 2` (and defaults `!= 7`), it automatically rebuilds both external BIOS SPI caches back to stock on first boot.
