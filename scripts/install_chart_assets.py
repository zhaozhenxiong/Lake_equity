from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


def main():
    root = Path(__file__).resolve().parents[1]
    build = root / "var/build-assets"
    build.mkdir(parents=True, exist_ok=True)
    result = subprocess.check_output(["npm.cmd", "pack", "echarts@6.1.0", "--pack-destination", str(build), "--silent"], text=True, cwd=root)
    archive = build / result.strip().splitlines()[-1]
    destination = root / "frontend/vendor"
    manifest = {"package": "echarts", "version": "6.1.0", "files": []}
    with tarfile.open(archive) as package:
        for source, target in (("package/dist/echarts.min.js", "echarts.min.js"), ("package/LICENSE", "echarts.LICENSE"), ("package/NOTICE", "echarts.NOTICE")):
            handle = package.extractfile(source)
            if handle is None:
                raise ValueError(f"Chart archive missing {source}")
            content = handle.read()
            (destination / target).write_bytes(content)
            manifest["files"].append({"path": target, "sha256": hashlib.sha256(content).hexdigest()})
    (root / "docs/provenance/chart_assets.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
