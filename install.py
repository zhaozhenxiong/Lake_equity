from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description="Install the independent LakeEquity Python 3.12 runtime")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    arguments = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error("Use Python 3.12: py -3.12 install.py")
    machine = platform.machine().lower()
    suffix = ("win_amd64.whl" if os.name == "nt" and machine in {"amd64", "x86_64"} and sys.maxsize > 2**32
              else "macosx_11_0_arm64.whl" if platform.system() == "Darwin" and machine == "arm64" else None)
    if suffix is None:
        parser.error("No bundled native wheel for this interpreter architecture; this release is validated on Windows x64.")
    root = Path(__file__).resolve().parent
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        venv.create(environment, with_pip=True)
    app = root / "packages/khquant_research/app"
    index = json.loads((app / "vendor/wheels/wheel-index.json").read_text(encoding="utf-8"))
    match = next((row for row in index["wheels"] if suffix and row["filename"].endswith(suffix)), None)
    if match is None:
        parser.error("No verified native wheel is included for this platform; this release is validated on Windows x64.")
    wheel = app / "vendor/wheels" / match["filename"]
    with wheel.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != match["sha256"]:
            raise ValueError("Frozen native wheel SHA mismatch")
    subprocess.run([str(python), "-m", "ensurepip"], check=True, cwd=root)
    subprocess.run([str(python), "-m", "pip", "install", "-r", "requirements-runtime.lock"], check=True, cwd=root)
    backend = "cu130" if arguments.device == "cuda" else "cpu"
    subprocess.run([str(python), "-m", "pip", "install", "torch==2.13.0", "--index-url", "https://download.pytorch.org/whl/" + backend], check=True, cwd=root)
    subprocess.run([str(python), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)], check=True, cwd=root)
    for package in (root / "vendor/cnequity", root):
        subprocess.run([str(python), "-m", "pip", "install", "--no-deps", "-e", str(package)], check=True, cwd=root)
    config = root / "configs/cnequity.toml"
    if not config.exists():
        subprocess.run([str(python), "-m", "cnequity", "config", "create", "--config", str(config), "--data-root", str(root / "var/lake")], check=True, cwd=root)
        config.write_text(config.read_text(encoding="utf-8").replace('ingest = "all_a"', 'ingest = "all_a_sh_sz"'), encoding="utf-8")
    subprocess.run([str(python), "-m", "pip", "check"], check=True, cwd=root)
    subprocess.run([str(python), "run.py", "verify-sources"], check=True, cwd=root)
    print("Installed. Start: lake.bat serve; inspect: lake.bat doctor")


if __name__ == "__main__":
    main()
