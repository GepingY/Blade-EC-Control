# Live ACPI Fan Analysis — Razer Blade 15 Base (`RZ09-0328`)

Analysis performed via read-only Windows `GetSystemFirmwareTable` + registry SSDT dumps disassembled with ACPICA `iasl` `20260408` (`iasl.exe -dl -d <table.aml>`). No ACPI methods were evaluated, no EC registers were written, and no firmware was flashed during this phase.

## Summary Finding

The BIOS `Active Trip Point 0/1 Fan Speed` percentages feed an **unused Intel reference-controller interface (`H_EC`)**. On this laptop, `H_EC` is explicitly absent and its write methods have empty bodies. Changing BIOS fan percentages or hidden DPTF options cannot operate the physical fans through the inspected ACPI path.

### Evidence From Live ACPI Tables (`DSDT` + 12 `SSDT`s)

- `DSDT` (`AC0F` / `AC1F`): Fields in the global ACPI NVS area.
- `SSD5_Ther_Rvp` (`FNCL`): Combines virtual `FAN0`–`FAN4` states; hardware updates are gated on `H_EC.ECAV` and `ETMD`, calling `H_EC.ECWT(...PENV)` and `H_EC.ECMD(0x1A)`.
- `DSDT` (`H_EC`):
  - `_STA()` returns `0` (absent), and `ECAV` is initialized to `0` with no assignment enabling it anywhere in the DSDT or SSDTs.
  - `H_EC.ECRD` returns `0` for all cases.
  - `H_EC.ECWT` contains empty switch cases and performs no hardware writes.
  - `H_EC.ECMD` has an empty body.

### DPTF Routes Through the Same Unused Interface

`SSD1_DptfTabl` defines fan participant `EC0.TFN1` (`INT3404`), whose `_FSL` and `_FST` methods also depend on the absent `H_EC.ECAV` and call the same empty `H_EC.ECWT`/`ECMD` stubs. The presence of `_FPS` levels including `0%` is therefore not a usable physical fan-off control.

### The Actual Embedded Controller (`\_SB.PCI0.LPCB.EC0`)

- `LPCB.EC0._STA()` returns `0x0F` (present).
- Its `EmbeddedControl` operation region (`ERAM`, mapped to EC SRAM `0x00080200 + offset`) exposes:
  - Thermal/power coordination fields: `NDTT` (`0x05`), `CPUT` (`0x56`), `GPUT` (`0x58`), `PCHT` (`0x5E`), `TMMD` (`0x5F`), `PSTA` (`0xAA`), `ECE1` (`0xE1`), `ECE2` (`0xE5`), `DGPS` (`0xE9`), `ACTP` (`0xEB`), `CORP` (`0xEC`), `SKID` (`0xED`).
  - Generic indirect mailbox at `0xF8`–`0xFA` (`ECMD`, `ECAD`, `ECDT`) accessed via `RECR`/`RECW` (`[0x80700 + ECAD]`).
  - GPU power & thermal Q-events: `_Q30` (`TMMD -> CTGP`), `_Q33` (`PSTA bit 3 -> CTGP`, `NDTT`), `_QD1.._QD5` (`DGPS` → `Notify(PEG0.PEGP, 0xD1..0xD5)`).
- No named fan duty, fan-stop, or tachometer field exists in `EC0`. Do not reuse Intel reference `PENV`/`CFAN` or older Razer Blade NBFC register offsets as though they belonged to this controller.
- `AMW0.WMBA` dispatches Thunderbolt commands only, and `TZ00._TMP` returns a hardcoded `0x0BC2` (`3010` dK) fallback.

## Reproducing on Your Own Machine

1. Dump your own ACPI tables read-only with `python ec_tools/acpi_inspect.py`.
2. Extract `iasl` from the bundled ACPICA archive (SHA-256 `121f5e4f30b1df63d09052294e4a605d4dee2dfb9599fa24af4ac6015df02b70`). [Bundled archive](../ec_tools/third_party/acpica/iasl-win-20260408.zip) · [Upstream release](https://github.com/open-acpica/acpica/releases/tag/20260408)
3. Disassemble each table individually (`iasl -dl -d <table>.aml`) and inspect `H_EC`, `LPCB.EC0`, `Opt1Tabl` (`GPS` / `CTGP` / `NDTT`), and `Ther_Rvp`.

Reference Intel template: [Comet Lake `Thermal.asl` in Slim Bootloader](https://github.com/slimbootloader/slimbootloader/blob/master/Platform/CometlakeBoardPkg/AcpiTables/Ssdt/Thermal.asl).
