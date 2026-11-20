"""Explicit persistent Blade profile actions using the reviewed fixed HID transport.

Status is read-only. Quiet/Performance require --apply, use only Silent5 or
Balanced0 with automatic fan control, and never write an RPM target.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path

from blade_silent_test import ExperimentHid, Journal, exact_modes, restore_balanced


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("status", "quiet", "performance"), default="status")
    parser.add_argument("--apply", action="store_true", help="Allow the explicitly selected mode change")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action != "status" and not args.apply:
        parser.error("A changing action requires --apply")
    root = Path(__file__).resolve().parent.parent / "diagnostics" / "presets"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    journal = Journal(root / f"native-{args.action}-{stamp}.jsonl")
    result = {"action": args.action, "success": False, "log": str(journal.path)}
    hid = None
    try:
        hid = ExperimentHid(journal)
        modes = hid.modes()
        result["before"] = modes
        if args.action == "quiet":
            if not exact_modes(modes, 5):
                for zone in (1, 2):
                    hid.set_zone(zone, 5)
            modes = hid.modes()
            if not exact_modes(modes, 5):
                raise RuntimeError("Both zones did not hold Silent5 with automatic fan control")
        elif args.action == "performance":
            modes = restore_balanced(hid, journal)
            if modes is None:
                raise RuntimeError("Balanced/Auto restoration was not verified")
        result["modes"] = modes
        try:
            result["temperatures"] = hid.temperatures()
        except Exception as error:
            # Temperature telemetry is informational for a firmware Auto preset.
            result["temperature_error"] = str(error)
        result["success"] = True
    except Exception as error:
        result["error"] = str(error)
        if args.action == "quiet" and hid is not None:
            recovered = restore_balanced(hid, journal)
            result["recovery_modes"] = recovered
            result["balanced_auto_recovered"] = recovered is not None
    finally:
        if hid is not None:
            hid.close()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
