# Razer Blade Python HID Fan & Power Controller (`controller/`)

Zero-dependency pure-Python (`tkinter` + Win32 `ctypes`) Windows HID controller and Quiet/Performance preset toolkit.

> [!NOTE]
> **Model Scope:** Mode wattage labels, auto fan curve tables, and manual RPM clamp hints are decoded specifically for the **Razer Blade 15 Base Model (Early 2020, `RZ09-0328`, `EC v01.00`, USB HID `1532:0255`)**.
>
> Because most Razer Blade models from ~2018–2023+ share the same Synapse USB HID `0x0D` packet framing (`0x0D01`/`0x0D81` fan RPM, `0x0D02`/`0x0D82` power/fan mode, `0x0D07`/`0x0D87` Custom CPU/GPU boost, `0x0D85` EC temps), you can pass **`--pid 0xXXXX`** to test `blade_inspect.py` or `blade_fan_gui.py` on another Blade PID (treat the displayed `RZ09-0328` Watt/RPM labels as reference-only on other models).

---

## Features

- **No Razer Synapse & No `pip install` Required**: Uses Windows' built-in `hid.dll` / `setupapi.dll` / `shell32.dll` directly via `ctypes`.
- **EC Mode Switching + Custom Mode (`4`) Fix**: Switches `Balanced (0)`, `Gaming (1)`, `Creator (2)`, `Legacy (3)`, `Custom (4)` (Low/Med/High CPU + GPU `NDTT` levels), and `Silent (5)`, automatically re-sending `0x0D07` after every `0x0D02` write (working around the EC firmware reset quirk at `0x12cb0`).
- **Auto-Detecting EC Profiles**: Defaults to stock `RZ09-0328 EC v01.00` tables, or automatically loads exact tables from your own live silicon probe (`--probe` / one-click **Probe Live EC**), a dumped 256 KiB EC ROM (`--ec-rom`), or built-in `--v2-defaults`.
- **Live `nvidia-smi` Cross-Check**: Shows live GPU temperature, power draw, and enforced power cap alongside the active EC profile expectation.
- **Independent Windows CPU Turbo Boost Toggle**: Toggles Windows `PERFBOOSTMODE` (`powercfg`) without resetting EC `Silent (5)` or `Custom (4)`.
- **Minimize to System Tray**: Pure Win32 tray icon with quick mode/boost menu; pauses `nvidia-smi` polling while hidden to let the dGPU sleep.

---

## Usage

Run these commands from the repository root.

```powershell
# Offline self-test (no hardware touched)
python controller/blade_fan_gui.py --self-test

# Launch GUI (RZ09-0328 stock EC defaults, USB HID 1532:0255)
python controller/blade_fan_gui.py
# Or zero-console window:
pythonw controller/blade_fan_gui.py

# Launch GUI with v2 modded EC tables or directly from a 256 KiB EC ROM
python controller/blade_fan_gui.py --v2-defaults
python controller/blade_fan_gui.py --ec-rom path/to/your_ec.bin

# Read-only HID inspection (works with --pid for other Razer Blades)
python controller/blade_inspect.py
python controller/blade_inspect.py --pid 0x0255
python controller/blade_fan_gui.py --pid 0x0255
```

### One-Click Quiet / Performance Presets (`Quiet.cmd` / `Performance.cmd` / `BladeQuiet.ps1`)
- `Quiet.cmd`: Exports a backup of your current Windows power plan into `diagnostics/presets/`, clones a **Blade Quiet (boost disabled)** plan (AC min 5%, max 100%, `PERFBOOSTMODE = 0`), and switches EC to `Silent (5) + Auto`.
- `Performance.cmd`: Restores your original Windows power plan and switches EC back to `Balanced (0) + Auto`.
- `.\controller\BladeQuiet.ps1 -Action Status`: Read-only status check.
