"""Готовит requirements для CI без tbank-индекса: SDK ставится из vendor/wheels."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.release_metadata import ReleaseMetadataError, offline_requirements

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        text = offline_requirements(
            (ROOT / "requirements.txt").read_text(encoding="utf-8"),
            ROOT / "vendor" / "wheels",
        )
    except ReleaseMetadataError as exc:
        parser.error(str(exc))
    args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
