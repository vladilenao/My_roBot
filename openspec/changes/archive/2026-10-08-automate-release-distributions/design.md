## Context

Текущий tag-triggered workflow собирает два отдельных PyInstaller binary и
загружает их как временные Actions artifacts. Он не создаёт ZIP-дистрибутив,
GitHub Release, checksums или sidecar-файлы. См. мотивацию в `proposal.md`.

## Goals / Non-Goals

**Goals:**
- Сделать GitHub Release единой точкой скачивания проверенных macOS arm64 и
  Windows x64 дистрибутивов.
- Формировать одинаковую безопасную структуру ZIP на обеих платформах.
- Не допускать публикации при несовпадении тега, версии, CHANGELOG или quality
  gates.
- Сделать локальную сборку macOS воспроизводимой тем же packaging-скриптом.

**Non-Goals:**
- Подпись Windows binary, macOS notarization и публикация в package registry.
- Кросс-компиляция PyInstaller либо поставка Intel macOS и Linux сборок.
- Включение пользовательских секретов, данных или рабочего конфига.

## Decisions

### Один Python-скрипт создаёт дистрибутив

Добавить `scripts/build_distribution.py`, который получает версию и target
platform, вызывает PyInstaller, собирает staging-папку и создаёт ZIP. Workflow
и локальная macOS-проверка используют этот же скрипт.

Это исключает различие между ручным `dist/` и CI. Альтернатива с shell-логикой
в workflow дублирует Windows/macOS команды и хуже тестируется.

### Нативная GitHub Actions matrix и отдельная публикация

Матрица использует явные runner и label для `macos-arm64` и `windows-x64`.
Каждая job запускает quality gates, собирает ZIP, проверяет binary и загружает
его как промежуточный artifact. Отдельная publish job скачивает оба ZIP,
создаёт checksums и создаёт/обновляет GitHub Release.

Разделение build и publish не даёт частично собранной платформе попасть в
Release. Альтернатива публиковать из каждой матричной job может создать
неполный релиз при ошибке второй платформы.

### Валидация release metadata до упаковки

Небольшой Python validator читает `src.__version__`, GitHub tag и CHANGELOG.
Он проверяет строгий SemVer, равенство tag/version, завершённую changelog
секцию и новый `Unreleased`. `workflow_dispatch` принимает явный release tag,
а не использует имя ветки.

### Безопасные sidecar-файлы

Скрипт создаёт `VERSION.txt`, извлекает release notes из CHANGELOG и копирует
безопасный `robot.toml.example` из `default.toml`. Он использует allowlist
файлов, поэтому `.env`, рабочий `robot.toml`, базы и логи физически не могут
оказаться в ZIP.

### Проверка готового binary

Добавить неинтерактивные флаги `--version` и `--config-smoke`; вместе с
существующим `--telegram-chart-smoke` они проверяют frozen executable без
запуска торгового цикла. Tests покрывают парсинг release metadata и структуру
созданного ZIP.

## Risks / Trade-offs

- [macOS arm64 runner может быть недоступен для текущего GitHub плана] → до
  включения release job проверить доступность runner и fail-fast задокументировать.
- [PyInstaller binary различается по окружению] → закрепить Python, PyInstaller
  и зависимости, явно маркировать target platform.
- [Неполный релиз при падении матрицы] → publish job зависит от успеха обеих
  build jobs.
- [Непреднамеренная упаковка секрета] → allowlist sidecar-файлов и отдельный
  тест содержимого ZIP.

## Migration Plan

1. Добавить локальный packaging/validation скрипт и его unit tests.
2. Перевести workflow на matrix build и publish job, сначала проверить на
   pre-release tag.
3. Убедиться, что GitHub Release содержит два ZIP и checksums.
4. При сбое удалить draft release или повторно запустить workflow для того же
   тега после исправления; существующие production binaries не изменяются.
