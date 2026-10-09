"""Проверяет release tag и извлекает notes из CHANGELOG."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.release_metadata import ReleaseMetadataError, read_version, validate_release

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--notes-file", type=Path)
    args = parser.parse_args(argv)
    try:
        version = read_version(ROOT / "src" / "__init__.py")
        notes = validate_release(version, args.tag, (ROOT / "CHANGELOG.md").read_text())
    except ReleaseMetadataError as exc:
        parser.error(str(exc))
    if args.notes_file:
        args.notes_file.write_text(notes)
    else:
        print(notes, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
