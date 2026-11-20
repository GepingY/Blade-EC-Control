# EC, ACPI & Super I/O Research Toolkit (`ec_research/`)

Read-only hardware probes, Windows 11 HVCI-safe `IT8390E-256` ROM dumper, offline AndesCore `NDS32` disassembler/analyzer, Linux Follow-Mode flasher, and `.NET IL` flasher safety patcher.

For full hardware discoveries and decoded register maps, see [../FINDINGS.md](../FINDINGS.md). For flashing procedures and safety warnings, see [../docs/FLASH_GUIDE.md](../docs/FLASH_GUIDE.md).

---

## Offline Analysis & Patching Tools (Zero Hardware Access)

| File | Purpose | Example |
| :--- | :--- | :--- |
| `analyze_ec_rom.py` | Decodes `0x3D000` thermal/fan/power slots, `0x35000` ADC current stepper, `0x2098C` `20%` PWM spin floor defaults, `0x2081C` 50-RPM PWM LUT, and diffs two ROMs (`--compare`) | `python ec_research/analyze_ec_rom.py my_ec.bin --steps` |
| `dis_nds32.py` | Pure-Python mixed 16/32-bit AndesCore `NDS32` disassembler | `python ec_research/dis_nds32.py --bin my_ec.bin 12356:123a0` |
| `patch_razer_flasher.py` | Offline `.NET IL` patcher for `Razer_EC_Flash_v1.03.exe` (fixes verify-failure brick bug; emits `_safe.exe` and `_recovery_in_follow_mode.exe`) | `python ec_research/patch_razer_flasher.py --in-exe path/to/Razer_EC_Flash_v1.03.exe` |
| `ite_ec_flash_linux.py` | Linux Live USB `IT8390E-256` direct eFlash (`FF FF FE`) PM1 `0xDC` Follow-Mode verifier (`--verify-only`) and flasher (`--flash --i-accept-brick-risk`), plus offline mock-EC `--self-test` | `python ec_research/ite_ec_flash_linux.py --self-test` |
| `patch_ec_fan_range.py` | Example `RZ09-0328 EC v01.00` v1 patcher: unlocks manual fan clamps `0–5500 RPM`, bumps `0x3FE000` cache `2 -> 3`, optional `--mirror-flag-ff` (`0x0008D = 0xFF`) | `python ec_research/patch_ec_fan_range.py --stock my_ec.bin --mirror-flag-ff` |
| `patch_ec_v2_power_fan.py` | Example `RZ09-0328 EC v01.00` v2 patcher: `0–5500 RPM`, Custom TDP `35/45·60/80·60/90 W`, Custom Low/Med `90 W` GPU (`_Q33` / `CTGP=1`), Silent `25/35 W`, dual cache bump `v4`/`v8`, `0x0008D = 0xFF`, built-in `NDS32` emulator | `python ec_research/patch_ec_v2_power_fan.py --stock my_ec.bin` |

---

## Read-Only Hardware Probes & EC ROM Dumper

| File | Purpose | Notes |
| :--- | :--- | :--- |
| `ec_sio_probe.py` | Super I/O (`0x2E/0x2F`), BRAM0 (`0x0300`/`0x0302`), safe I2EC MMIO sampler, fast `--verify-live` reader, and 256 KiB EC ROM dumper (`--dump`) via WHCP-signed `PawnIO` `LpcIO.bin` | Run `--self-test` offline with zero external files. `--run` / `--dump` / `--verify-live` require Windows Admin (UAC), work with HVCI ON, never touch `0x62/0x66` or I2EC `0x103F`, and auto-uninstall PawnIO afterward unless `--keep-driver` is passed. |
| `EcSioProbe.cmd` / `EcRomDump.cmd` / `EcVerifyLive.cmd` | Windows UAC launchers for `--run`, `--dump`, and `--verify-live` | Require official PawnIO assets below. |
| `acpi_inspect.py` | Read-only Windows `GetSystemFirmwareTable` + registry SSDT dumper | No driver required; dumps `.aml` tables locally into `diagnostics/acpi/`. |
| `ec_snapshot.py` | Shared PawnIO verification helpers (and historical ACPI EC `0x62/0x66` read-only probe) | Direct `0x62/0x66` access collides with Windows `ACPI.sys` (Event 15); use `ec_sio_probe.py` instead. |

---

## Setting Up Official PawnIO (Required for Live Hardware Dumping / Probing)

No third-party binaries are distributed in this repository. To run `ec_sio_probe.py --run`, `--dump`, or `--verify-live`, download the two official signed releases from upstream and place them in `tools/pawnio/` (or pass `--pawnio-dir <folder>`):

1. **`PawnIO_setup.exe`** (`v2.2.0`):
   - Release: <https://github.com/namazso/PawnIO.Setup/releases/tag/2.2.0>
   - Pinned SHA-256: `1f519a22e47187f70a1379a48ca604981c4fcf694f4e65b734aaa74a9fba3032`
   - *(Optional if you already installed official PawnIO 2.2.0+ system-wide.)*
2. **`release_0_2_12.zip`** (`v0.2.12`, contains signed `LpcIO.bin`):
   - Release: <https://github.com/namazso/PawnIO.Modules/releases/tag/0.2.12>
   - Pinned Archive SHA-256: `09698897ce19b92f1043a2e322cfcf0b1fe823e0b576971b178a65244fb0af9d`
   - Pinned `LpcIO.bin` SHA-256: `b3896a1cab0d808fca31fe2ebcae045d59dac690da87b17c858bb8da357eb45e`

Verify your downloaded files offline before touching hardware:
```powershell
python ec_research/ec_sio_probe.py --self-test
python ec_research/ec_sio_probe.py --check
```
