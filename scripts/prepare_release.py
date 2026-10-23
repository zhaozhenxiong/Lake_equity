"""Export a reviewed public source subset without touching local runtime state."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import zipfile


ROOT_FILES = (
    ".gitignore", ".gitattributes", "LICENSE", "README.md", "THIRD_PARTY_NOTICES.md", "pyproject.toml",
    "install.py", "run.py", "lake.bat", "requirements-runtime.lock", "requirements-qlib.lock",
)
PUBLIC_DOCS = ("LEARNING.md", "EXECUTION_RULES.md", "MARKET_FEATURES.md", "LEGACY_RETIREMENT.md", "RELEASE.md")
PUBLIC_SCRIPTS = ("prepare_release.py", "verify_learning_runtime.py", "install_chart_assets.py", "preview_market.py")
PRIVATE_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "knowledge_base", "knowledge_base_system", "agent_prompts"}
OMITTED_RESEARCH = ("my_strategy/docs/", "my_strategy/data/", "my_strategy/artifacts/", "my_strategy/kronos_model/")
PRIVATE_MAINTENANCE_TESTS = {"tests/test_fill_lake.py", "tests/test_fill_lake_years.py", "tests/test_repair_calendar.py"}


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_relative(value: str) -> Path:
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts or "\\" in value or ":" in value or pure.as_posix() != value:
        raise ValueError(f"Invalid source path: {value}")
    return Path(*pure.parts)


def public_path(relative: Path) -> bool:
    return not (PRIVATE_PARTS.intersection(relative.parts) or relative.name == ".env"
                or relative.name.startswith(".env.") or relative.name == "AI_CHANGELOG.md"
                or relative.suffix in {".pyc", ".pyo", ".log"})


def select_files(root: Path) -> tuple[dict[str, tuple[Path, str]], dict]:
    selected: dict[str, tuple[Path, str]] = {}

    def add(path: Path, expected: str | None = None) -> None:
        relative = path.relative_to(root)
        if not public_path(relative) or relative.as_posix() in PRIVATE_MAINTENANCE_TESTS:
            return
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(root)):
            raise ValueError(f"Symlink cannot be distributed: {relative}")
        if not path.is_file():
            raise FileNotFoundError(path)
        selected[relative.as_posix()] = (path, expected or sha256(path))

    for name in ROOT_FILES:
        add(root / name)
    for directory in ("src", "frontend", "tests", "third_party"):
        if not (root / directory).is_dir():
            raise FileNotFoundError(f"Required public source directory: {directory}")
        for path in sorted((root / directory).rglob("*")):
            if path.is_file():
                add(path)
    for name in PUBLIC_DOCS:
        add(root / "docs" / name)
    for name in PUBLIC_SCRIPTS:
        add(root / "scripts" / name)
    for name in ("chart_assets.json", "learning_runtime.json"):
        add(root / "docs/provenance" / name)

    public_manifest = root / "release-manifest.json"
    if public_manifest.exists():
        manifest = json.loads(public_manifest.read_text(encoding="utf-8"))
        if manifest.get("schema") != 1 or manifest.get("kind") != "lake-equity-public-source":
            raise ValueError("Unsupported public source manifest")
        for entry in manifest["files"]:
            relative = safe_relative(entry["path"])
            path = root / relative
            if not path.is_file() or path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
                raise ValueError(f"Published input changed: {relative}")
            if relative.parts[0] in {"vendor", "packages"}:
                add(path, entry["sha256"])
        provenance = json.loads((root / "docs/provenance/sources.json").read_text(encoding="utf-8"))
    else:
        sources = []
        for name, base in (("cnequity_source.json", root / "vendor/cnequity"),
                           ("khquant_source.json", root / "packages/khquant_research/app")):
            manifest_path = root / "docs/provenance" / name
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            included = 0
            for entry in manifest["files"]:
                relative = safe_relative(entry["path"])
                path = base / relative
                if name == "khquant_source.json" and not path.is_file():
                    path = base.parent / relative
                if not path.is_file() or sha256(path) != entry["sha256"]:
                    raise ValueError(f"Frozen input changed: {name}: {entry['path']}")
                if name == "khquant_source.json" and entry["path"].startswith(OMITTED_RESEARCH):
                    continue
                before = len(selected)
                add(path, entry["sha256"])
                included += len(selected) - before
            sources.append({
                "source": name.removesuffix("_source.json"),
                "version": manifest.get("version"), "git_head": manifest.get("git_head"),
                "dirty": manifest.get("dirty"), "archive_sha256": manifest.get("archive_sha256"),
                "original_manifest_sha256": sha256(manifest_path),
                "original_file_count": len(manifest["files"]), "distributed_file_count": included,
            })
        provenance = {"schema": 1, "sources": sources,
                      "note": "Reviewed source subset; private migration/audit state and historical research documents are omitted. Original engine and native wheel bytes are unchanged."}
    return selected, provenance


def build_release(root: Path, output: Path, version: str) -> dict:
    root, output = root.resolve(), output.resolve()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Use a numeric semantic version, for example 0.1.0")
    if output.exists():
        raise FileExistsError("Release output must be a new directory")
    selected, provenance = select_files(root)
    repository = output / "repo"
    repository.mkdir(parents=True)
    for relative, (source, expected) in sorted(selected.items()):
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if sha256(destination) != expected or sha256(source) != expected:
            raise ValueError(f"Source changed during export: {relative}")
    write_json(repository / "docs/provenance/sources.json", provenance)
    records = [{"path": path.relative_to(repository).as_posix(), "size": path.stat().st_size, "sha256": sha256(path)}
               for path in sorted(repository.rglob("*")) if path.is_file()]
    write_json(repository / "release-manifest.json", {
        "schema": 1, "kind": "lake-equity-public-source", "version": version,
        "license": "MIT (LakeEquity original code; third-party terms preserved)", "files": records,
    })
    archive = output / f"LakeEquity-v{version}-source.zip"
    prefix = f"LakeEquity-v{version}/"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(repository.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(prefix + path.relative_to(repository).as_posix(), date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, path.read_bytes(), compresslevel=6)
    digest = sha256(archive)
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return {"version": version, "files": len(records) + 1, "repository": str(repository),
            "archive": str(archive), "bytes": archive.stat().st_size, "sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(build_release(Path(__file__).resolve().parents[1], arguments.output, arguments.version), indent=2))


if __name__ == "__main__":
    main()
