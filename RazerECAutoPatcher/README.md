# Razer EC Auto-Patcher

This folder contains `ec_auto_patcher_core.py`, the Python core for offline EC ROM analysis, patch generation, and verification. The current GUI is distributed as `RazerECAutoPatcher.exe`. [GitHub Releases](https://github.com/GepingY/Blade-EC-Control/releases)

## Run from source

Use Python 3.10 or later. From the repository root, you can view the available options or scan a local EC ROM file.

```powershell
python patcher/ec_auto_patcher_core.py --help
python patcher/ec_auto_patcher_core.py --scan path/to/your_ec.bin
```

The offline self-test suite requires a local stock `RZ09-0328` EC v01.00 ROM at `firmware/rz09-0328-ec-v01.00/ec-rom-00000-3ffff.bin`. If you have that file, run the suite from the repository root.

```powershell
python patcher/ec_auto_patcher_core.py --self-test
```
