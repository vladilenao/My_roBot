"""Проверка метаданных и заметок выпуска."""

from __future__ import annotations

import re
from pathlib import Path


SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SECTION_RE = re.compile(r"^## (?P<title>.+)$", re.MULTILINE)


class ReleaseMetadataError(ValueError):
    """Release metadata не соответствует контракту."""


def read_version(path: Path) -> str:
    """Извлекает единственный литерал ``__version__`` из модуля пакета."""
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', path.read_text(), re.MULTILINE)
    if match is None:
        raise ReleaseMetadataError(f"Не найдена __version__ в {path}")
    return match.group(1)


def release_notes(changelog: str, version: str) -> str:
    """Возвращает содержимое завершённой секции версии CHANGELOG."""
    headings = list(_SECTION_RE.finditer(changelog))
    if not any(match.group("title") == "Unreleased" for match in headings):
        raise ReleaseMetadataError("В CHANGELOG.md отсутствует заголовок ## Unreleased")

    prefix = f"{version} — "
    for index, match in enumerate(headings):
        if match.group("title").startswith(prefix):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
            notes = changelog[match.end():end].strip()
            if notes:
                return notes + "\n"
            break
    raise ReleaseMetadataError(f"В CHANGELOG.md отсутствует непустая секция ## {version} — <дата>")


def validate_release(version: str, tag: str, changelog: str) -> str:
    """Проверяет SemVer, tag и CHANGELOG, возвращая release notes."""
    if not SEMVER_RE.fullmatch(version):
        raise ReleaseMetadataError(f"Версия должна быть SemVer MAJOR.MINOR.PATCH, получено {version!r}")
    expected_tag = f"v{version}"
    if tag != expected_tag:
        raise ReleaseMetadataError(f"Тег {tag!r} не совпадает с версией {expected_tag!r}")
    return release_notes(changelog, version)


# Пакеты, которых нет на PyPI: CI ставит их из wheel в vendor/wheels/,
# потому что tbank-индекс с GitHub Actions недоступен.
VENDORED_PACKAGES = ("t-tech-investments",)
_INDEX_OPTIONS = ("--index-url", "--extra-index-url", "--trusted-host", "-i ")


def _wheel_name(package: str) -> str:
    return re.sub(r"[-_.]+", "_", package).lower()


def offline_requirements(requirements: str, wheels_dir: Path) -> str:
    """Убирает внешние индексы и проверяет, что vendored-пакеты есть в wheels_dir.

    Каждый пакет из ``VENDORED_PACKAGES`` должен быть закреплён ``==`` и иметь
    wheel той же версии; иначе сборка остановится до установки зависимостей.
    """
    lines = [
        line for line in requirements.splitlines()
        if not line.strip().startswith(_INDEX_OPTIONS)
    ]
    for package in VENDORED_PACKAGES:
        pins = [
            match.group(1)
            for line in lines
            if (match := re.fullmatch(rf"\s*{re.escape(package)}\s*==\s*(\S+)\s*", line))
        ]
        if len(pins) != 1:
            raise ReleaseMetadataError(
                f"{package} должен быть закреплён в requirements.txt как {package}==X.Y.Z"
            )
        pattern = f"{_wheel_name(package)}-{pins[0]}-*.whl"
        if not any(wheels_dir.glob(pattern)):
            raise ReleaseMetadataError(
                f"Нет wheel {package}=={pins[0]} в {wheels_dir} (ожидается {pattern})"
            )
    return "\n".join(lines) + "\n"
