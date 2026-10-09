## 1. Release metadata and package assembly

- [x] 1.1 Добавить тестируемый модуль проверки release metadata: строгий SemVer, равенство release tag и `src.__version__`, выпущенная секция CHANGELOG и новый `Unreleased`.
- [x] 1.2 Добавить `scripts/build_distribution.py`, который нативно запускает PyInstaller, собирает allowlist sidecar-файлов (`VERSION.txt`, README, `robot.toml.example`, notes), проверяет содержимое и создаёт platform-specific ZIP без пользовательских данных.
- [x] 1.3 Добавить unit-тесты metadata validator и сборщика ZIP: имена, содержимое, release notes и отсутствие `.env`, `robot.toml`, баз и логов.

## 2. Frozen binary smoke checks

- [x] 2.1 Добавить в `run.py` неинтерактивные флаги `--version` и `--config-smoke`, не запускающие торговый цикл и не требующие секретов.
- [x] 2.2 Добавить unit-тесты для новых флагов и сохранить существующий `--telegram-chart-smoke` как третью smoke-проверку bundled binary.

## 3. GitHub Release workflow

- [x] 3.1 Переписать `build-release.yml`: явный release tag для tag push и workflow dispatch, проверка metadata, pytest и Ruff до упаковки, нативная матрица `macos-arm64`/`windows-x64`.
- [x] 3.2 Выполнять packaging script и три smoke-проверки на каждой платформе; загружать ZIP как промежуточный artifact только после успешной проверки.
- [x] 3.3 Добавить publish job, которая ждёт обе platform jobs, создаёт SHA-256 checksums и создаёт/обновляет GitHub Release с ZIP, checksums и release notes; предоставить workflow минимальные permissions для записи release.

## 4. Release contract and verification

- [x] 4.1 Согласовать `run.spec`, зависимости сборки и документацию с фактическим packaging script, зафиксировать версии build toolchain и безопасный `robot.toml.example`.
- [x] 4.2 Обновить `docs/release/build-and-distribution.md`, `README.txt`, `AGENTS.md` и OpenSpec release spec так, чтобы они описывали реализованный workflow без временных ручных исключений.
- [x] 4.3 Проверить change через `openspec validate automate-release-distributions --strict`, полный pytest, Ruff и локальную macOS-сборку ZIP с проверкой структуры и smoke-команд.
