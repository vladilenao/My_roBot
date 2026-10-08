"""Собирает безопасный versioned ZIP-дистрибутив робота."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from src.release_metadata import ReleaseMetadataError, read_version, validate_release

ROOT = Path(__file__).resolve().parents[1]


PLATFORMS = {"macos-arm64": "", "windows-x64": ".exe"}


def distribution_name(version: str, platform: str) -> str:
    if platform not in PLATFORMS:
        raise ReleaseMetadataError(f"Неподдерживаемая платформа {platform!r}")
    return f"robot-v{version}-{platform}"


def package_distribution(
    *,
    root: Path,
    dist_dir: Path,
    version: str,
    platform: str,
    binary: Path,
    notes: str,
) -> Path:
    """Собирает ZIP только из разрешённых файлов дистрибутива."""
    name = distribution_name(version, platform)
    suffix = PLATFORMS[platform]
    if not binary.is_file() or binary.name != f"{name}{suffix}":
        raise ReleaseMetadataError(f"Не найден binary {name}{suffix}: {binary}")

    package_dir = dist_dir / name
    archive = dist_dir / f"{name}.zip"
    shutil.rmtree(package_dir, ignore_errors=True)
    archive.unlink(missing_ok=True)
    package_dir.mkdir(parents=True)

    shutil.copy2(binary, package_dir / binary.name)
    (package_dir / "VERSION.txt").write_text(f"{version}\n", newline="\n")
    (package_dir / f"robot-v{version}.txt").write_text(notes, newline="\n")
    shutil.copy2(root / "README.txt", package_dir / "README.txt")
    shutil.copy2(root / "default.toml", package_dir / "robot.toml.example")

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for item in sorted(package_dir.iterdir()):
            bundle.write(item, item.relative_to(dist_dir))
    return archive


def build_distribution(*, root: Path, dist_dir: Path, tag: str, platform: str) -> Path:
    """Валидирует release metadata, запускает PyInstaller и создаёт ZIP."""
    version = read_version(root / "src" / "__init__.py")
    notes = validate_release(version, tag, (root / "CHANGELOG.md").read_text())
    name = distribution_name(version, platform)
    pyinstaller_dist = dist_dir / "pyinstaller"
    shutil.rmtree(pyinstaller_dist, ignore_errors=True)
    env = os.environ | {"PYINSTALLER_NAME": name}
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "run.spec", "--noconfirm", "--clean",
            "--distpath", str(pyinstaller_dist), "--workpath", str(dist_dir / "build"),
        ],
        cwd=root,
        env=env,
        check=True,
    )
    return package_distribution(
        root=root,
        dist_dir=dist_dir,
        version=version,
        platform=platform,
        binary=pyinstaller_dist / f"{name}{PLATFORMS[platform]}",
        notes=notes,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Релизный тег вида vX.Y.Z")
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    try:
        archive = build_distribution(root=ROOT, dist_dir=args.dist_dir, tag=args.tag, platform=args.platform)
    except ReleaseMetadataError as exc:
        parser.error(str(exc))
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
