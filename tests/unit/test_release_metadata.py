from pathlib import Path
import zipfile

import pytest

from scripts.build_distribution import package_distribution
from src.release_metadata import (
    ReleaseMetadataError,
    offline_requirements,
    release_notes,
    validate_release,
)


CHANGELOG = """# История\n\n## Unreleased\n\n## 4.2.0 — 08.10.2026\n\n- Новая поставка.\n\n## 4.1.0 — 01.10.2026\n"""


def test_validate_release_accepts_matching_tag_and_completed_changelog():
    assert validate_release("4.2.0", "v4.2.0", CHANGELOG) == "- Новая поставка.\n"


@pytest.mark.parametrize(
    ("version", "tag"), [("4.2", "v4.2"), ("4.2.0", "v4.2.1")],
)
def test_validate_release_rejects_invalid_version_or_tag(version, tag):
    with pytest.raises(ReleaseMetadataError):
        validate_release(version, tag, CHANGELOG)


def test_release_notes_requires_unreleased_and_version_section():
    with pytest.raises(ReleaseMetadataError):
        release_notes("## 4.2.0 — today\n", "4.2.0")


def test_package_distribution_contains_only_allowlisted_files(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "README.txt").write_text("Инструкция\n")
    (root / "default.toml").write_text("[robot]\n")
    binary = tmp_path / "robot-v4.2.0-windows-x64.exe"
    binary.write_bytes(b"binary")
    (tmp_path / ".env").write_text("secret")
    (tmp_path / "robot.toml").write_text("private")

    archive = package_distribution(
        root=root,
        dist_dir=tmp_path,
        version="4.2.0",
        platform="windows-x64",
        binary=binary,
        notes="- Новая поставка.\n",
    )

    with zipfile.ZipFile(archive) as bundle:
        assert sorted(bundle.namelist()) == [
            "robot-v4.2.0-windows-x64/README.txt",
            "robot-v4.2.0-windows-x64/VERSION.txt",
            "robot-v4.2.0-windows-x64/robot-v4.2.0-windows-x64.exe",
            "robot-v4.2.0-windows-x64/robot-v4.2.0.txt",
            "robot-v4.2.0-windows-x64/robot.toml.example",
        ]
        assert bundle.read("robot-v4.2.0-windows-x64/VERSION.txt") == b"4.2.0\n"


REQUIREMENTS = (
    "numba==0.67.0\n"
    "--extra-index-url https://example.invalid/simple\n"
    "--trusted-host example.invalid\n"
    "t-tech-investments==1.2.3\n"
    "requests\n"
)


def test_offline_requirements_drops_external_index_when_wheel_is_vendored(tmp_path: Path):
    (tmp_path / "t_tech_investments-1.2.3-py3-none-any.whl").write_bytes(b"wheel")

    assert offline_requirements(REQUIREMENTS, tmp_path) == (
        "numba==0.67.0\nt-tech-investments==1.2.3\nrequests\n"
    )


def test_offline_requirements_rejects_wheel_of_other_version(tmp_path: Path):
    (tmp_path / "t_tech_investments-1.2.2-py3-none-any.whl").write_bytes(b"wheel")

    with pytest.raises(ReleaseMetadataError, match="1.2.3"):
        offline_requirements(REQUIREMENTS, tmp_path)


def test_offline_requirements_requires_exact_pin(tmp_path: Path):
    (tmp_path / "t_tech_investments-1.2.3-py3-none-any.whl").write_bytes(b"wheel")

    with pytest.raises(ReleaseMetadataError, match="закреплён"):
        offline_requirements("t-tech-investments\n", tmp_path)


def test_repository_vendors_pinned_broker_sdk():
    root = Path(__file__).resolve().parents[2]
    text = offline_requirements(
        (root / "requirements.txt").read_text(encoding="utf-8"), root / "vendor" / "wheels"
    )

    assert "index-url" not in text
