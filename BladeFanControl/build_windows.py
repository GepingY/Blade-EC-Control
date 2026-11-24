"""Build a portable 64-bit Windows BladeFanControl.exe."""

import argparse
from hashlib import sha256
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile


def main() -> None:
    app_dir = Path(__file__).resolve().parent
    repo_root = app_dir.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=app_dir / "dist")
    args = parser.parse_args()
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        parser.error("Building the controller requires 64-bit Python on Windows")

    with tempfile.TemporaryDirectory(prefix="BladeFanControl-build-") as scratch:
        work = Path(scratch)
        worker_dist = work / "worker-dist"
        gui_dist = work / "gui-dist"
        environment = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(work / "cache")}
        pyinstaller = [
            sys.executable, "-m", "PyInstaller", "--noconfirm",
            "--specpath", str(work), "--paths", str(repo_root),
        ]
        pawnio = "ec_tools/third_party/pawnio"
        subprocess.run(
            [
                *pyinstaller, "--onedir", "--name", "BladeEcProbe",
                "--distpath", str(worker_dist), "--workpath", str(work / "worker-build"),
                "--add-data", f"{repo_root / pawnio / 'PawnIO_setup.exe'}:{pawnio}",
                "--add-data", f"{repo_root / pawnio / 'release_0_2_12.zip'}:{pawnio}",
                str(app_dir / "probe_worker.py"),
            ],
            cwd=repo_root, env=environment, check=True,
        )
        archive = Path(shutil.make_archive(str(work / "BladeEcProbe"), "zip", worker_dist / "BladeEcProbe"))
        digest = sha256(archive.read_bytes()).hexdigest()
        (work / "_probe_payload.py").write_text(f'SHA256 = "{digest}"\n', encoding="utf-8")

        subprocess.run(
            [
                *pyinstaller, "--onefile", "--windowed", "--name", "BladeFanControl",
                "--distpath", str(gui_dist), "--workpath", str(work / "gui-build"),
                "--paths", str(work), "--add-data", f"{archive}:.",
                str(app_dir / "blade_fan_gui.py"),
            ],
            cwd=repo_root, env=environment, check=True,
        )
        args.dist_dir.mkdir(parents=True, exist_ok=True)
        output = args.dist_dir / "BladeFanControl.exe"
        shutil.copy2(gui_dist / output.name, output)
        print(f"Built {output.resolve()}")


if __name__ == "__main__":
    main()
