## Why

Текущий workflow публикует временные отдельные binaries во вкладке Actions,
не проверяя соответствие версии тегу и не создавая полный пользовательский
дистрибутив. Windows-пользователю приходится искать технический artifact, а
ручные release-шаги не гарантируют наличие конфигурационного шаблона, notes и
проверочных сумм.

## What Changes

- Автоматизировать выпуск двух нативных дистрибутивов: macOS arm64 и Windows
  x64, каждый в отдельном versioned ZIP-архиве.
- Проверять до упаковки SemVer, совпадение тега и `src.__version__`, CHANGELOG,
  тесты и Ruff.
- Добавить reproducible packaging workflow: безопасный шаблон конфигурации,
  VERSION, release notes, SHA-256 и smoke-проверки готовых binaries.
- Создавать или обновлять GitHub Release тега и прикреплять к нему ZIP-архивы,
  checksums и notes.
- Исключить из дистрибутивов `.env`, рабочий `robot.toml`, логи, базы и прочие
  пользовательские runtime-файлы.

## Capabilities

### New Capabilities

Нет.

### Modified Capabilities

- `release`: GitHub Release становится каналом поставки проверенных
  platform-specific ZIP-дистрибутивов с согласованной версией и checksums.

## Impact

- `.github/workflows/build-release.yml` и новые скрипты упаковки/проверок.
- `run.py` получит неинтерактивную проверку версии и bundled-конфигурации, если
  это потребуется для smoke-проверки frozen binary.
- `run.spec`, release-документация, `README.txt`, `AGENTS.md`, `CHANGELOG.md` и
  OpenSpec release-contract.
- GitHub Actions permissions и GitHub Release assets.
