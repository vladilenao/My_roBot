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
