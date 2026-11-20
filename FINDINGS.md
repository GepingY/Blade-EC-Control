# Razer Blade 15 Base (`RZ09-0328`) — Fan, Power & `IT8390E-256` EC Technical Findings

> Complete technical knowledge base documenting ACPI disassembly, USB HID control, read-only `IT8390E-256` internal eFlash dumping via WHCP-signed `PawnIO`, full AndesCore `NDS32` firmware reverse engineering, the `0x0008D = 0xFF` external-SPI (`GD25Q64`) → internal-eFlash mirror discovery, dual external BIOS SPI cache overrides, and live-verified EC firmware mods.

---

## 1. Reference Platform & Critical Guardrails

### Reference Machine
- **Model / Board**: Razer Blade 15 Base Model (Early 2020), **`RZ09-0328`** (`DA550-MB`), Board **`SSID 0x2011`**
- **CPU / GPU**: Intel Core i7-10750H / NVIDIA RTX 2070 with Max-Q Design
- **BIOS / OS**: BIOS `01.06`, Windows 11 Pro
- **EC**: **ITE `IT8390E-256`** (`Chip ID 0x8390`, `reg 0x22 = 0x82`), firmware **`ITE8390EC-V01.00`** (256 KiB internal eFlash mirrored from external `GD25Q64` SPI flash, 16/32-bit mixed AndesCore `NDS32`)<img width="1949" height="1288" alt="IMG_3520" src="https://github.com/user-attachments/assets/0faae902-c4d1-4efa-af0f-2d6c405b4d93" />

- **USB HID Interface**: `VID 0x1532`, `PID 0x0255` (front-end MCU bridging Synapse HID `0x0D` packets into `IT8390E` Bank 0 shared registers)

### Top 9 Non-Obvious Gotchas
1. **Never cross-flash EC ROMs between Blade models**: Comparing `RZ09-0330` (`CH551_EC_01_01.bin`, `SSID 2009`/`200A`) vs `RZ09-0328` (`SSID 2011`) showed only `34.0%` code-region overlap (`0x00000..0x28000`). Always dump and patch your own board's EC ROM.
2. **External SPI (`GD25Q64`) → Internal eFlash Hardware Mirror (`0x0008D = 0xFF`)**: On `RZ09-0328`, `Razer_EC_Flash_v1.03` flashes the external SPI chip (`GD25Q64`), and `IT8390E-256` hardware-mirrors that image into its internal 256 KiB eFlash on reboot **only when ITE 16-byte signature byte 13 (`0x0008D`) is `0xFF`**. After mirroring completes, the hardware clears `0x0008D` back to `0x00` inside internal eFlash and sets `FLHCTRL3R` (`0x00F01063`) bit 7 (`0x08 → 0x88`). Because a dump from internal eFlash always has `0x0008D = 0x00`, **every flashed image (including stock rollback!) must set `0x0008D = 0xFF`** or the EC will silently keep booting its old internal eFlash.
3. **No A/B Fallback Bank / Brick Risk**: `IT8390E-256` runs from a single 256 KiB internal eFlash bank with **no A/B fallback bootloader**. On non-mirroring ITE boards (or if Follow Mode / internal eFlash is corrupted), a cheap `SOIC-8` clip cannot unbrick internal eFlash — recovery requires an ITE EDI programmer (e.g., SVOD / Vertyanov / RT809H) via keyboard FPC lines or chip replacement. Never run unpatched `Razer_EC_Flash_v1.03.exe` (see §6).
4. **Dual External BIOS SPI Flash Caches Override Static EC ROM Edits**: At boot/mode-switch, the EC loads CRC-16-validated cached copies of `0x3D000`/`0x35000` tables from external BIOS SPI (`0x3FE000`, version `2`) and `0x86000` runtime defaults (including the `20%` PWM floor and `0x86900` Custom 4 CPU PL table) from external BIOS SPI (`0x050000`, CRC-16 + struct version `7`). Static EC ROM edits have zero effect until those cache version checks are bumped so the EC rebuilds and re-CRCs them.
5. **Never touch raw ACPI EC ports `0x62/0x66` directly**: Direct reads collide with Windows `ACPI.sys` (causing Windows ACPI Event 15). Also never read/write I2EC `0x103F` (`ECINDDR`) blindly.
6. **`0x0D02` Mode-Write Reset Quirk**: Writing *any* `0x0D02` HID mode packet causes EC firmware (`0x12cb0`) to reset Custom CPU & GPU Boost (`0x808E0`/`0x808E1`) back to Medium (`1`). Always send `0x0D07` **after** `0x0D02` when entering Custom Mode `4`.
7. **Windows Turbo Boost vs EC `Silent (5)`**: Toggling Windows `PERFBOOSTMODE` (`powercfg`) does **not** change EC mode. In stock `Silent (5)`, the CPU still gets `35 W / 45 W` when Turbo Boost is ON, which heats past `Silent (5)`'s `60 °C` fan-stop threshold quickly (lowered to `25/35 W` AC & DC in the v2 mod).
8. **BIOS ACPI fan percentages & `0x0D88` tach are dead ends**: BIOS `AC0F`/`AC1F`, `H_EC`, DPTF `EC0.TFN1._FSL`, `AMW0.WMBA`, and `TZ00._TMP` (`3010` dK fallback) do not control or measure physical fans. HID `0x0D88` (live tachometer) returns status `5` (unsupported) on `RZ09-0328`.
9. **Locale-Independent `powercfg` Parsing**: Non-English Windows locales output localized labels in `mbcs` encoding; always parse GUIDs and `0x` hex indices rather than English labels.

---

## 2. EC Firmware Architecture & Read-Only ROM Extraction (`IT8390E-256`, `v01.00`)

### How the 256 KiB EC ROM Is Dumped Without `WinRing0` or `0x62/0x66`
- Implemented in `ec_research/ec_sio_probe.py --dump` using official WHCP-signed **`PawnIO` 2.2.0** + signed **`LpcIO.bin`** (auto-uninstalled cleanly afterward unless `--keep-driver` is passed).
- Super I/O `0x2E/0x2F` confirms **`Chip ID = 0x8390`**, `reg 0x22 = 0x82` (256 KiB internal eFlash); `0x300` mailbox confirms `version = "01.00"`, `SSID = 0x2011`; `[0x000802ED] = 0x03` (`SKID = 3`).
- Uses the EC's own read-only **BRAM0 `0x0302` command `0x90`** memory-read protocol (never touching `0x62/0x66` or I2EC `0x103F`), reading all `262,144` bytes in `~280 s` (`~840 B/s`) with canary verification at `0x00000`, `0x00080` (`"ITE8390EC-V01.00"`), and `0x10000`.
- **Reference Stock `RZ09-0328 EC v01.00` SHA-256**: `c676ff55da486c07cb8897c2e4bcb891e3140463864dbde7f615cef4c5a4b95d`.

### CPU & Memory Layout (`256 KiB` Internal eFlash)
- **CPU**: 16/32-bit mixed **AndesCore `NDS32`** (`nds32le`, `gp = 0x00080000`, `sp = 0x00084000` configured at `0x03002..0x0300e`).
- **On-chip SRAM**: `0x00080000..0x0008AFFF` (`0x0008A000` holds active `0x200`-byte thermal slot; `0x00086000` holds runtime config/defaults; `0x00087C00` holds active `0x35000` current stepper).
- **ITE Hardware MMIO**: `0x00F01000..0x00F02FFF` (`PWM`, `TACH`, `ADC`, `SMBus`, `GPIO`, `BRAM`/`SMFI`).

| Flash Range | Contents | Key Addresses / Details |
| :--- | :--- | :--- |
| `0x00000..0x1FFFF` | Code (`NDS32`) | Reset/init `0x03000`, BRAM/SMFI `0x02218..0x0255a`, Follow-Mode loop `0x0259a..0x02842`, EC USB HID (`1532:F003`) `0x0ec14..0x0ee80`, CPU/GPU power `0x0e0f0..0x0ea94`, thermal/fan engine `0x11314..0x12d0e`, PD/ADC stepper `0x1362e..0x13b40` & `0x14c1a`, PM1 `0xDC` entry `0x17192 -> 0x1728c` |
| `0x205E4..0x20B5F` | Control Tables | Charger SMBus regs (`0x205e4`, `0x206a4`), 50-RPM PWM LUT (`0x2081c`), 4× SMBus thermal sensors (`0x2091c`), fan descriptors (`0x2093c`), runtime `{addr, val}` defaults (`0x2098c`, incl. 20% PWM floor) |
| `0x21078..0x25077` | Shared Bank Map | 8 banks × `0x800` B (`256 × 8` B `{perm, ram_ptr}`), pointer list at `0x27904..0x27920` (reverse order: Bank 0 at `0x24878` .. Bank 7 at `0x21078`) |
| `0x251F8..` | USB-C PD `4CC` Strings | TI `TPS6598x` SMBus `0x94` commands (`Gaid`, `GAID`, `SWSk`, `SWSr`, `ADCs`, etc.) |
| `0x35000..0x36FFF` | Dynamic Current / Power | 4 identical SKU banks (`0x35000`, `0x35800`, `0x36000`, `0x36800`), 8 × `0x100` B mode slots (`ID 0xF0..0xF7`); in `01.00` all 8 slots are identical |
| `0x37000..0x3A2AF` | `@gddv` DataVault | 4 identical `687`-byte Intel DTT DataVault copies (`0x37000..0x3a000`, CRC/tag `0x941fe5a0`) |
| `0x3B000..0x3EFFF` | Thermal / Fan / Power Profiles | 4 identical SKU banks (`0x3b000..0x3e000`), indexed by `SKID` (`[0x802ED] = 0x03` selects **`0x3D000`**); each bank holds 8 × `0x200` B mode slots (`0..7`) |

*(Verify all tables offline anytime with `python ec_research/analyze_ec_rom.py <rom.bin> --steps` and disassemble any range with `python ec_research/dis_nds32.py --bin <rom.bin> <start_hex>:<end_hex>`.)*

---

## 3. Complete Mode, Wattage, Thermal & Fan Control Reference (`0x3D000`)

When `[0x8035F]` (`Mode`, `0..7`) changes, `0x120ca(mode)` loads `0x200` (512) bytes from `0x3D000 + mode * 0x200` (or its external SPI cache) into SRAM `0x0008A000` (footer at `+0x1A8` → `0x0008A1A8..0x0008A1DF`).

### Stock firmware Summary Table (`RZ09-0328`, `EC v01.00`, Verified Against ROM + Live `nvidia-smi`)

| Mode | Name | AC CPU `PL1 / PL2` | Thermal Fallback `PL1/PL2` | Battery CPU `PL1 / PL2` | Live GPU Cap (`nvidia-smi` + ACPI) | GPU Target (`NDTT` `+0x32`) | Auto Fan Range | Manual RPM Clamp (`+0x21..+0x26`, Def `+0x23`) |
| :---: | :--- | :---: | :---: | :---: | :--- | :---: | :--- | :--- |
| **`0`** | **Balanced** | `35 W / 45 W` | `30 W / 80 W` | `25 W / 25 W` | **`80 W`** (`_QD1`, `CTGP=0`) | `80 °C` | `0` or `2500–5300` (st `0..9`) | `4300–5500` (def `4900`) |
| **`1`** | **Gaming** | `35 W / 60 W` | `30 W / 80 W` | `30 W / 30 W` | **`90 W` (Max)** (`_QD1`, `TMMD=1` → `_Q30` `CTGP=1`) | `87 °C` | `0` or `2700–5500` (st `0..9`) | `4900–5500` (def `5200`) |
| **`2`** | **Creator / Perf** | `45 W / 60 W` | `35 W / 50 W` | `30 W / 30 W` | **`80 W`** (`_QD1`, `CTGP=0`) | `87 °C` | `0` or `2700–5500` (st `0..9`) | `4900–5500` (def `5200`) |
| **`3`** | **Legacy / Mode 3** | `35 W / 45 W` | `35 W / 80 W` | `25 W / 25 W` | **`80 W`** (`_QD1`, `CTGP=0`) | `87 °C` | `0` or `2700–5500` (st `0..9`) | `< 4900` drops to `0` quirk (`0x1238c`); safety auto-return at step `9` |
| **`4`** | **Custom** *(via `0x0D07`)* | **`25/45`, `35/45`, or `55/80 W`** *(see below)* | `35 W` if CPU therm; `40 W` Med if GPU therm | `20`, `25`, or `30 W` | **`80 W`** (`_QD1`, `CTGP=0` on all GPU sliders) | `75 °C` (Low) / `87 °C` (Med/High) | `0` or `2700–5500` (st `0..9`) | `4900–5500` (def `5200`) |
| **`5`** | **Silent** | `35 W / 45 W` | `35 W / 80 W` | `30 W / 30 W` | **`30 W`** (Footer `+0x0B = 0x05` → **`_QD5`**) | `97 °C` *(v01.00)* | `0` or `2200–3250` (st `0..7`) | **`2400–5000`** (def `3100`) |
| **`6, 7`** | **Silent Clones** | Identical to `5` | Identical to `5` | Identical to `5` | **`30 W`** (`_QD5`) | `97 °C` | `0` or `2200–3250` (st `0..7`) | `2400–5000` (def `3100`) |

| Mode | Name | Auto RPM range | Auto steps | Manual RPM min | Manual RPM max | Manual default |
| :---: | :--- | :--- | :---: | :---: | :---: | :---: |
| 0 | Balanced | 0 (stop) or 2500–5300 | 0–9 | 4300 | 5500 | 4900 |
| 1 | Gaming | 0 (stop) or 2700–5500 | 0–9 | 4900 | 5500 | 5200 |
| 2 | Creator / Perf | 0 (stop) or 2700–5500 | 0–9 | 4900 | 5500 | 5200 |
| 3 | Legacy | 0 (stop) or 2700–5500 | 0–9 | 4900 (requests below this give 0 RPM, a firmware quirk) | 5500 | – |
| 4 | Custom | 0 (stop) or 2700–5500 | 0–9 | 4900 | 5500 | 5200 |
| 5 | Silent | 0 (stop) or 2200–3250 | 0–7 | 2400 | 5000 | 3100 |
| 6, 7 | Silent clones | 0 (stop) or 2200–3250 | 0–7 | 2400 | 5000 | 3100 |

Auto-mode RPM per step, as you can see it uses step based fan control instead of continuous algorithm based control
| Step | Balanced (0) | Gaming / Creator / Legacy / Custom (1–4) | Silent (5–7) |
| :---: | :---: | :---: | :---: |
| 0 | 0 | 0 | 0 |
| 1 | 2500 | 2700 | 2200 |
| 2 | 2800 | 3000 | 2400 |
| 3 | 3100 | 3300 | 2500 |
| 4 | 3400 | 3600 | 2700 |
| 5 | 3700 | 3900 | 2900 |
| 6 | 4100 | 4300 | 3100 |
| 7 | 4500 | 4700 | 3250 |
| 8 | 4900 | 5100 | – |
| 9 | 5300 | 5500 | – |


### Stock Custom Mode (`4`) Sub-Levels (`0x0D07` / `0x0D87`, SRAM `0x808E0` & `0x808E1`)
- **CPU Slider (`0x0D07` zone `1`, SRAM `[0x808E0]` → LUT `0x00086900`, built in code at `0x10070..0x101c8`)**:
  - **`0` (Low)**: AC **`25 W / 45 W`**, Battery `20 W / 20 W` (`19 2d 14 14`)
  - **`1` (Medium — default reset on `0x0D02`)**: AC **`35 W / 45 W`**, Battery `25 W / 25 W` (`23 2d 19 19`, clipped to `40 W` AC if `[0x802E4] bit 1`)
  - **`2` (High — highest stock CPU power)**: AC **`55 W / 80 W`**, Battery `30 W / 30 W` (`37 50 1e 1e`, clipped to `35 W` if `[0x802E4] bit 0`)
- **GPU Slider (`0x0D07` zone `2`, SRAM `[0x808E1]` → LUT `0x000869A0`, handler `0x0e50a..0x0e592`)**:
  - **`0` (Low)**: Sets `ERAM NDTT = 75 °C` (`0x4B`), updates `PSTA bit 3`, fires `_Q33`. Live `nvidia-smi` power cap stays `80 W` in stock firmware.
  - **`1` (Medium) / `2` (High)**: Sets `ERAM NDTT = 87 °C` (`0x57`), fires `_Q33`. Live `nvidia-smi` power cap stays `80 W` in stock firmware.

### Auto Fan Curves (`0x3D000 + 8`, `13` bytes/step, `mask = 0x07`)
- Active sensors in `01.00`: **`S0 = CPU PECI`**, **`S1 = dGPU SMBus 0x9E`**, **`S2 = Chassis SMBus 0x90`**.
- Step layout: `[fan1_x50rpm, fan2_x50rpm, T0_dn, T0_up, T1_dn, T1_up, T2_dn, T2_up, dn_t0, dn_t1, up_t0, up_t1, flags]`.
- Escalates when **any** sensor exceeds `T_up`; steps down when **all** sensors drop below `T_dn`.

| Step | Balanced (Mode `0`) | Gaming / Creator / Legacy / Custom (`1–4`) | Silent (`5–7`, max step `7`) |
| :---: | :--- | :--- | :--- |
| **0 (Stop)** | `0 RPM` (`CPU<55, GPU<54, Ch<39 °C`) | `0 RPM` (`CPU<55, GPU<55, Ch<39 °C`) | `0 RPM` (`CPU<60, GPU<45, Ch<43 °C`) |
| **1** | `2500 RPM` (`50–60 / 50–58 / 36–41 °C`) | `2700 RPM` (`50–59 / 50–59 / 36–41 °C`) | `2200 RPM` (`55–65 / 40–50 / 40–46 °C`) |
| **2** | `2800 RPM` (`55–65 / 54–62 / 38–43 °C`) | `3000 RPM` (`54–63 / 54–63 / 38–43 °C`) | `2400 RPM` (`60–70 / 45–55 / 43–49 °C`) |
| **3** | `3100 RPM` (`60–70 / 58–66 / 40–45 °C`) | `3300 RPM` (`58–67 / 58–67 / 40–45 °C`) | `2500 RPM` (`65–75 / 50–60 / 46–52 °C`) |
| **4** | `3400 RPM` (`65–75 / 62–70 / 42–47 °C`) | `3600 RPM` (`62–71 / 62–71 / 42–47 °C`) | `2700 RPM` (`70–80 / 55–65 / 49–255 °C`) |
| **5** | `3700 RPM` (`70–80 / 66–74 / 44–49 °C`) | `3900 RPM` (`66–75 / 65–74 / 44–49 °C`) | `2900 RPM` (`75–85 / 60–70 / --`) |
| **6** | `4100 RPM` (`75–85 / 70–78 / 46–51 °C`) | `4300 RPM` (`70–79 / 68–77 / 46–51 °C`) | `3100 RPM` (`80–90 / 65–75 / --`) |
| **7** | `4500 RPM` (`80–89 / 74–82 / 48–53 °C`) | `4700 RPM` (`74–83 / 71–80 / 48–53 °C`) | `3250 RPM` (`85–255 / 70–255 / --`) |
| **8** | `4900 RPM` (`84–93 / 78–86 / 50–55 °C`) | `5100 RPM` (`78–87 / 74–83 / 50–55 °C`) | *(unused)* |
| **9** | `5300 RPM` (`88–255 / 82–255 / 52–255 °C`) | `5500 RPM` (`82–255 / 77–255 / 52–255 °C`) | *(unused)* |

### PWM Engine, `20%` Spin Floor, Manual Clamps & Hidden Engineering Modes (`0xFF` / `0xFE`)
1. **Hardware PWM Registers**: Period `[0x00F01841] = 0xB7 (183)` (set at `0x10bd6`). Live duty registers: `[0x00F01807]` (`DCR7`, Fan 1) and `[0x00F01803]` (`DCR3`, Fan 2).
2. **`20%` Minimum Spin Floor**: Runtime table `0x2098C` (`8`-byte `{ram_addr, val}` records, entries `10..13`) loads `20` (`0x14`) into `0x86010..0x86013`. `0x103b4..0x10402` computes `(20 * 183) / 100 = 36` (`0x24`) into `[0x8057A..8057D]`.
3. **Kick-Start & `0 RPM` Hysteresis (`0x11a0c..0x11a88`)**:
   - Spinning down to Step `0` (`0 RPM`, when `Mode != 0xFE`): PWM steps down 1 tick/loop until `< 36`, then snaps straight to **`0`**.
   - Starting up from `< 36`: jumps immediately to **`36`** (`~20%`) before closed-loop tachometer PID (`deadband = 150 RPM` at `[0x86025]`).
4. **Manual Mode Clamping (`0x12356..0x1263a`)**:
   - Closed-loop manual mode is enabled by `[0x8034D] bit 0 = 1` with `x100 RPM` setpoints `[0x80330]` / `[0x80331]`.
   - Clamps against `[0x80555]` (footer `+0x21`: `4300` Balanced, `4900` Modes `1–4`, `2400` Silent) and `[0x8055A]` (footer `+0x26`: `5500` Modes `0–4`, `5000` Silent).
   - **Mode 3 Quirk (`0x1238c`)**: If `Mode == 3` and requested `< 4900`, `0x1238c` skips the lower-clamp load while `r0 == 0` (`[0xF0162E] == 0`), producing `0 RPM`. Also at `0x124ec`/`0x12612`, reaching top thermal step (`9`) forces fans back to Auto and sets `[0x8034D] bit 7` to lock manual writes at `0x024a2`.
5. **Hidden Direct PWM Modes (`0xFF` & `0xFE` — Bank 0 SMFI Only, NOT USB HID)**:
   - **`Mode 0xFF (255)` (`0x12948`)**: Requires `[0x80425] bit 0 = 1` (Bank 0 `reg 0xF0`). Maps `[0x80330..33b]` directly through the 50-RPM PWM LUT at `0x2081C` (`0->0, 1500->35, 2400->59, 4300->122, 5500->170, 5800->181, 5900+->183`) into hardware `DCR` registers.
   - **`Mode 0xFE (254)` (`0x12a08 -> 0x12a30`)**: Requires **`[0x80425] bit 6 = 1`** (Bank 0 `reg 0xF0`); otherwise snaps back to Mode `0`. Writes raw duty bytes `[0x80394/395]` (Bank 0 `0xB8/0xB9`, range `0..183`) directly to `DCR7`/`DCR3`, bypassing PID and the `20%` (`36`) cutoff (`0x11a10`).
   - **Power inheritance in Mode `254`**: Mode `254` has **no power profile of its own**—it inherits the previously active `0x0008A000` slot and `TMMD` (except switching from Custom `4` loses the `0x86900` sliders and falls back to Mode 4's `35/60 W` base table; entering from Mode `1` retains `35/60 W` CPU + `90 W` GPU).

---

## 4. Power Arbitration: CPU PECI, GPU ACPI/GPS, Adapter & ADC Current Stepper

### CPU Power (`0x0e0f0..0x0e472`) & Windows vs EC Arbitration
- **PECI Writes**: `0x0f740(w)` sets CPU `PL1` (`PECI WrPkgConfig`, `addr=0x30`, `index=0x1A`); `0x0f7ec(w)` sets CPU `PL2` (`index=0x1B`). Cached in `[0x804e5]`/`[0x804e6]` (and `[0x805EB]`/`[0x805EC]`) — **the EC does NOT spam PECI in steady state**; it only re-sends when mode, AC/DC state, thermal throttle flag, or ADC current stepper changes (`0x0e40e`, `0x0e436`).
- **Intel Silicon Arbitration**: CPU enforces **`MIN(Windows MSR 0x610, MMIO MCHBAR 0x59A0, EC PECI 0x1A/0x1B, Intel DPTF)`**:
  - **Lowering** CPU `PL1`/`PL2` from Windows (e.g., `25/35 W` while EC allows `55/80 W`): **No tug-of-war**; Windows wins cleanly without EC interference.
  - **Raising** CPU `PL1`/`PL2` from Windows *above* the EC's active PECI cap: **Cannot override** the lower PECI ceiling; EC must be placed in a higher-watt mode first.

### Two Runtime Capping Layers Applied Above Mode Watts (`0x0e390..0x0e472`)
1. **Adapter & USB-C PD Table (`0x00086120`, `10 × 20` B via `0x0d150` / `0x14c1a`)**:
   - TI `TPS6598x` (SMBus `0x94`) reports negotiated PD power/voltage (`20 V`):
     - Razer Barrel AC adapter / `100 W` PD (`idx 9`) / default AC (`idx 4`): **`60 W PL1 / 90 W PL2`** CPU cap. Charger input current limit programmed over SMBus (`5,632 / 6,400 / 8,064 mA`).
     - `46–99 W` USB-C PD (`idx 0..2`): Restricts CPU down to **`15 W PL1 / 44 W` (or `25 W`) `PL2`**.
2. **Dynamic ADC Current Stepper (`0x35000..0x357FF` → `0x00087C00`, `0x1362e..0x13b40`)**:
   - Polls raw 10-bit ADC (`0..1023`): **AC ADC Ch1** (`0x00F01907/908`, thresholds `504, 490, 518, 490` — `~1.46–1.48 V` on charger `PSYS`/`IMON`/`IADP`) and **DC ADC Ch4** (`0x00F01939/93a`, thresholds `231, 207, 254, 207`).
   - Steps `1..8` progressively tighten CPU `PL1/PL2` (`45 → 40 → 35 → 30 → 25 W`), escalate GPU `DGPS` (`0xD1 → 0xD5`), assert `PROCHOT#` (`0xF01607 bit 6`), and assert `ECE1` (`[0x802E1]` → `_Q21`).

### GPU Power & Temperature Control (EC + ACPI + NVIDIA VBIOS/Driver)
There is **no GPU Watt table inside the EC ROM**; the EC selects discrete ACPI policy codes, and the NVIDIA driver/VBIOS maps them to Watts:
1. **`_QD1`..`_QD5` (`DGPS`, EC `0x0e594`/`0x0ea94`)**: EC continuously computes `max(mode_floor, current_stepper, ...)` and fires `Notify(PEG0.PEGP, 0xD1..0xD5)`.
   - `Silent (5)` locks footer `+0x0B = 0x05` → **`_QD5`** → **`30.00 W`** (`nvidia-smi` verified).
   - Modes `0, 2, 3, 4` set `0x01` → **`_QD1`** → **`80.00 W`**.
   - Note: `0x0e594`/`0x0ea94` re-checks every loop, so overriding `DGPS` externally gets overwritten by the EC.
2. **Configurable TGP (`_Q30` & `_Q33`)**:
   - Setting **Mode `1` (Gaming)** sets `TMMD = 1`, firing `_Q30` → `PEGP.CTGP = 1` (exposes NVIDIA `GPS` bit `22`, `0x00400000`) → unlocks **`90.00 W`** on the RTX 2070 Max-Q.
   - In **Mode `4` (Custom)**, setting `PSTA bit 3` (`0x802AA |= 0x08`) and firing **`_Q33`** also sets `PEGP.CTGP = 1` → **`90.00 W`** (used by our v2 EC mod for Custom Low & Custom Medium!).
3. **GPU Target Temperature (`NDTT`)**:
   - EC writes `NDTT` (`[0x80205]`: `75 / 80 / 87 / 97 °C`); NVIDIA `GPS` Function `42` subfunction `2` reads `EC0.NDTT` into `TGPU` and the NVIDIA driver enforces the thermal throttle point. (EC SMBus `bus=1, addr=0x9E` to the GPU is read-only temperature polling.)

---

## 5. Host Interfaces, Sensors & Shared Bank 0 Register Map

### USB HID `1532:0255` vs `IT8390E` Built-In USB `1532:F003`
- `IT8390E`'s own USB descriptor (`0x25a90`) is `1532:F003` (`"WINUSB"`, `"Razer EC"`).
- The **`1532:0255`** USB HID controller translates Synapse packets into `IT8390E` Bank 0 shared registers (`0x8035F`, `0x8034D`, `0x80330/331`, `0x808E0/8E1`):
  - `0x0081`: Read FW version (`01.00`)
  - `0x078F`: Read Max-Fan flag
  - `0x0D02` / `0x0D82`: Set / Get `(prefix, zone, mode, manual_flag)` — **resets `0x0D07` boost to `1`!**
  - `0x0D01` / `0x0D81`: Set / Get `(prefix, zone, rpm_x100)` (`0..70` → `0..7000 RPM`, clamped by EC)
  - `0x0D07` / `0x0D87`: Set / Get `(prefix, zone 1=CPU / 2=GPU, level 0..2)` — effective in Custom Mode `4`
  - `0x0D85`: Read raw EC temps `[count, cpu_ec_c, gpu_ec_c]`
  - `0x0D88`: Live tachometer — **unsupported** (returns status `5`)

### SMFI / BRAM Shared Bank 0 (`0x00F02290..0x00F022C0`, Table `0x24878`)
Protocol (`0x02218..0x022fe`): Bank `0..7` (`0xF02290`), start reg (`0xF02291`), count `<= 0x20` (`0xF02292`), cmd (`0xF02293`: `0xA0` Read / `0xA1` Write), status (`0xF02294 = 0x80` OK), payload (`0xF022A0..BF`).

| Bank 0 Reg | Perm | EC SRAM / MMIO | Meaning |
| :---: | :---: | :--- | :--- |
| `0x10` / `0x11` / `0x12` | RW / RW / RO | `0x80256` / `0x80258` / `0x8025E` | `CPUT`, `GPUT`, `PCHT` |
| `0x1A` | RW | `0x8035F` | Active Thermal / Fan Mode (`0..7`, `0xFE`, `0xFF`) |
| `0x1B` / `0x1C` | RW | `0x80330` / `0x80331` | Fan 1 / Fan 2 Manual RPM (`x100`) |
| `0x2A` / `0x2B` | RW | `0x808E0` / `0x808E1` | Custom Mode 4 CPU / GPU Sliders (`0..2`) |
| `0x36` | RW | `0x8034D` | Manual Fan Enable (`bit 0`), Auto-Lock (`bit 7`) |
| `0x3A` / `0x3B` | RO | `0x805EB` / `0x805EC` | Active PECI `PL1` / `PL2` (W) |
| `0x81` | RW | `0x802E1` | `ECE1` (`_Q21`) |
| `0xB0` / `0xB1` / `0xB2` | RO | `0xF01807` / `0xF01803` / `0xF01841` | Live PWM1, PWM2, Period (`183`) |
| `0xB8` / `0xB9` | RW | `0x80394` / `0x80395` | Direct Raw PWM1 / PWM2 (`0..183` for Mode `0xFE`) |
| `0xDC` / `0xDF` | RO | `0x80555` / `0x8055A` | Active Mode Manual RPM Min / Max (`x100`) |
| `0xF0` | RW | `0x80425` | Diagnostic Flags (`bit 0` = Mode `0xFF`, `bit 5` = freeze sensors, `bit 6` = Mode `0xFE`) |

### 8-Sensor Thermal Array (`0x0008053C..0x00080543`) & ACPI `ERAM`
- `S0` (`0x8053C`): CPU via PECI `GetTemp()` (`Tjmax [0x805E3] - (-raw/64)`) → `ERAM CPUT` (`0x80256`).
- `S1` (`0x8053D`): dGPU via SMBus `bus=1, addr=0x9E` → `ERAM GPUT` (`0x80258`).
- `S2..S5` (`0x8053E..80541`): Round-robin SMBus thermal poll (`0x0eef8`, table `0x2091c`) at `addr = 0x90, 0x92, 0x94, 0x96` `reg 0x00` → `ERAM 0x80259..0x8025C`.
- `S7` (`0x80543`): `(word[0x8035C] - 2731) / 10` (`0.1 K → °C`).
- ACPI `ERAM` maps `0x00080200 + off`: `0x05=NDTT`, `0x56=CPUT`, `0x58=GPUT`, `0x5E=PCHT`, `0x5F=TMMD`, `0xAA=PSTA`, `0xE1=ECE1`, `0xE5=ECE2`, `0xE9=DGPS`, `0xEB=ACTP`, `0xEC=CORP`, `0xED=SKID`, `0xF8..0xFA=ECMD/ECAD/ECDT` (`RECR`/`RECW` → `[0x80700 + ECAD]`).

---

## 6. EC Flashing, Hardware Mirroring, Cache Invalidation & Firmware Mods

### Key Flashing & Architecture Discoveries
1. **Cross-Model Comparison (`RZ09-0330` `CH551 v01.01` vs `RZ09-0328 v01.00`)**:
   - Only `146,285 / 262,144` bytes (`55.8%`) match overall (`34.0%` in code `0x00000..0x28000`).
   - Sensor mask changed from `0x07` (`S0, S1, S2` = SMBus `0x90`) to `0x13` (`S0, S1, S4` = SMBus `0x94`).
   - `Razer_EC_Flash_v1.03.exe` validates only `SSID` from `ToolConfig.ini` vs mailbox `0x300` regs `12:11` (zero RSA/ECDSA or payload CRC checks).
2. **PM1 `0xDC` Follow-Mode Protocol**:
   - EC `0x17192: beqc r0, #0xdc → 0x1728c → 0x0259a` enters the RAM-resident Follow-Mode loop, acknowledging with `0x33`.
   - Remapping Super I/O `LDN 0x11` from `0x62/0x66` → `0x202/0x206` (with Intel PCH `PCI 0:31:0 reg 0x82 |= 0x0200` `GAMEL_LPC_EN`) cleanly isolates flashing from `ACPI.sys`.
3. **Verify-Failure Brick Bug in Official `Razer_EC_Flash_v1.03.exe` (`IL_095B..IL_0AD8`)**:
   - On a verify mismatch the original binary prints `.......Fail` and sets `verifyfail = 1`, but due to missing braces around `if (verifyfail == 0)` it still prints `Verify .........100%`, unconditionally sends `EC_Exit_Idle` (`0xFC`), and unconditionally forces a reboot (`DoExitWin(6)`) if `REBOOT=1`.
   - `ec_research/patch_razer_flasher.py` offline-patches `Program::Main` (19-byte IL diff, `.NET 4.8` JIT-verified) to emit **`Razer_EC_Flash_v1.03_safe.exe`** (skips `Verify 100%`, `0xFC`, and `DoExitWin` on mismatch; exits with code `2` leaving EC parked safely in Follow Mode on `0x202/0x206`) and **`Razer_EC_Flash_v1.03_recovery_in_follow_mode.exe`** (NOPs `Send_EC_Update_Qevent` and `EC_Write_Enable` so you can immediately re-flash without hanging on a second `0xDC`/`0x33`).
4. **Live Hardware Mirror Discovery (`0x0008D = 0xFF` → `GD25Q64` to Internal eFlash)**:
   - On `RZ09-0328`, flashing a patched image whose ITE signature byte `0x0008D` remained `0x00` (as dumped from internal eFlash) updated `GD25Q64` external SPI, but `IT8390E` skipped mirroring on reboot (`FLHCTRL3R` `0x00F01063 = 0x08`, internal ROM stayed stock `v01.00`).
   - Setting **`0x0008D = 0xFF`** (matching Razer's official updater images) triggered hardware mirroring from `GD25Q64` → internal 256 KiB eFlash on reboot (`FLHCTRL3R = 0x88` bit 7 mirror-done, `0x0008D` auto-cleared back to `0x00` in internal eFlash).
   - **Stock rollback images must also set `0x0008D = 0xFF`** so hardware mirroring restores stock `v01.00` into internal eFlash.
5. **Dual External BIOS SPI Flash Caches (Solved)**:
   - **Thermal / Mode Cache (`0x3FE000` / `0x3FF000`, `0x120ca → 0x14678`)**: Caches `0x3D000` (`ID 0x20..0x27`) and `0x35000` (`ID 0xF0..0xF7`) slots with CRC-16 (`0x023a0`) and version byte `[0xF02218] == 2` (`0x1211E`). **Register-reuse trap at `0x1135A`**: `movi55 r7, #2` is shared between `[r6+0x215]` (`0x0200` 512-byte length high byte) AND `[r6+0x218]` (version `2`). Load callee-saved `r8` (`push25 re=2` saves `r6..r10`) with the new version (`3` or `4`) and patch the 8 slot writes (`0x11376..0x114FE`) from `sbi r7,[r6+0x218]` to `sbi r8,[r6+0x218]`.
   - **Runtime Defaults Cache (`0x050000`, `0x105a2..0x105ec`)**: Loads 4 KiB into `0x86000..0x86FFF` (including the `20%` PWM floor at `0x86010..13` and `0x10070` Custom `4` PL table at `0x86900`). Checks CRC-16 **and** `[0x86004] == 7`; bumping `7 → 8` at `0x1046C`, `0x104FC`, `0x105CC`, `0x10606` triggers automatic rebuild and re-CRC.

### Reference EC Mods Implemented in This Repository (`RZ09-0328 EC v01.00`)
1. **v1 Mod (`ec_research/patch_ec_fan_range.py --mirror-flag-ff`, Live-Verified)**:
   - 118-byte diff (`100` table bytes + `17` cache-version code bytes + `1` mirror header byte `0x0008D = 0xFF`).
   - Unlocks manual fan clamps to **`0–5500 RPM`** across all 8 modes × all 4 SKU banks (default seeds preserved) and bumps `0x3FE000` cache version `2 → 3`.
   - Live-verified via `ec_sio_probe.py --verify-live` (`FLHCTRL3R = 0x88`, `sram_cache_version = 3`, `sram_active_manual_min_rpm = 0`, `max = 5500`) and live HID testing (`0 RPM` stop through `5500 RPM` across Gaming / Custom / Silent).
2. **v2 Mod (`ec_research/patch_ec_v2_power_fan.py`, NDS32 Emulator-Verified)**:
   - 571-byte diff verified offline via a built-in instruction-level `NDS32` emulator:
     - Manual fan clamps **`0–5500 RPM`** across all modes.
     - **Silent (`5/6/7`)**: AC, DC, and thermal fallback CPU TDP lowered to **`25 W PL1 / 35 W PL2`**.
     - **Custom Mode (`4`) AC CPU TDP (`0x10070 → 0x86900`)**: Low **`35/45 W`**, Med **`60/80 W`** (with `0x0e162` thermal fallback fix), High **`60/90 W`** (DC unchanged `20/25/30 W`).
     - **Custom Mode (`4`) GPU `90 W` (`0x0e534..0x0e592`)**: Sets `PSTA bit 3` + fires `_Q33` (`CTGP = 1` → **`90 W`**) for Custom Low & Med, clears bit 3 (`80 W`) for Custom High, preserving `75 °C` / `87 °C` `NDTT`.
     - Bumps both caches (`0x3FE000` `2 → 4`, `0x050000` `7 → 8`), sets `0x0008D = 0xFF`, and emits a mirror-ready stock rollback image.

---

## 7. External References
- Microsoft `GetSystemFirmwareTable`: https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-getsystemfirmwaretable
- PawnIO signed kernel driver & modules: https://pawnio.eu/ · https://github.com/namazso/PawnIO.Setup · https://github.com/namazso/PawnIO.Modules
- ACPICA `iasl`: https://github.com/open-acpica/acpica/releases/tag/20260408
- Razer HID protocol references: https://github.com/jdsgnrinfo/blade-control · https://github.com/sqmagellan/razer-ctl
