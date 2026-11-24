"""Packaged entry point for the controller's read-only EC probe."""

import argparse
import json
from pathlib import Path
import sys

from ec_tools import ec_sio_probe


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "verify-live"))
    args = parser.parse_args()
    payload_dir = Path(ec_sio_probe.__file__).resolve().parent / "third_party" / "pawnio"

    try:
        if args.mode == "check":
            report = ec_sio_probe.offline_check(pawnio_dir=payload_dir)
            print(json.dumps(report))
            return 0 if report["passed"] else 2
        _, report = ec_sio_probe.verify_live_report(
            pawnio_dir=payload_dir,
            diagnostics_dir=Path(sys.executable).resolve().parent / "diagnostics",
        )
        print(json.dumps(report))
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
