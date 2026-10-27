from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tomllib
import venv


def source_version(root: Path) -> str:
    expected = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    tree = ast.parse((root / "src/lake_equity/__init__.py").read_text(encoding="utf-8"))
    package_version = next((ast.literal_eval(node.value) for node in tree.body
                            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__"
                                                                     for target in node.targets)), None)
    if package_version != expected:
        raise ValueError("Source package version differs from pyproject.toml")
    release = root / "release-manifest.json"
    if release.is_file() and json.loads(release.read_text(encoding="utf-8")).get("version") != expected:
        raise ValueError("Release manifest version differs from pyproject.toml")
    return expected


def verify_runtime_version(python: Path, root: Path, expected: str) -> dict:
    command = ("import sys;sys.path.insert(0,sys.argv[1]);"
               "import importlib.metadata,json,lake_equity;"
               "print(json.dumps({'installed':importlib.metadata.version('lake-equity'),'package':lake_equity.__version__}))")
    result = subprocess.run([str(python), "-c", command, str(root / 'src')], check=True, cwd=root, capture_output=True, text=True)
    observed = json.loads(result.stdout)
    if observed.get("installed") != expected or observed.get("package") != expected:
        raise ValueError(f"Installed runtime version mismatch: expected {expected}, observed {observed}; reinstall this source with the target interpreter")
    return {"expected": expected, **observed}


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
    expected_version = source_version(root)
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
    # PEP 660 can build metadata in a temporary directory, leaving an older
    # source egg-info to shadow dist-info when run.py prepends src to sys.path.
    subprocess.run([str(python), "-c", "from setuptools import setup;setup(script_args=['egg_info'])"],
                   check=True, cwd=root)
    identity = verify_runtime_version(python, root, expected_version)
    print(json.dumps({"runtime_version": identity}))
    config = root / "configs/cnequity.toml"
    if not config.exists():
        subprocess.run([str(python), "-m", "cnequity", "config", "create", "--config", str(config), "--data-root", str(root / "var/lake")], check=True, cwd=root)
        config.write_text(config.read_text(encoding="utf-8").replace('ingest = "all_a"', 'ingest = "all_a_sh_sz"'), encoding="utf-8")
    subprocess.run([str(python), "-m", "pip", "check"], check=True, cwd=root)
    subprocess.run([str(python), "run.py", "verify-sources"], check=True, cwd=root)
    print("Installed. Start: lake.bat serve; inspect: lake.bat doctor")


if __name__ == "__main__":
    main()
