# Сборка и распространение релиза

Этот документ — единый обязательный регламент выпуска робота. Он определяет
состав дистрибутива, проверки и публикацию для macOS и Windows. `AGENTS.md`,
README и OpenSpec release-contract не должны ему противоречить.

## Поддерживаемые дистрибутивы

Каждый релиз публикуется на странице GitHub Release двумя отдельными ZIP-
архивами:

```text
robot-vX.Y.Z-macos-arm64.zip
robot-vX.Y.Z-windows-x64.zip
checksums.txt
```

Каждый ZIP содержит одну папку с тем же именем без `.zip`:

```text
robot-vX.Y.Z-windows-x64/
├── robot-vX.Y.Z-windows-x64.exe
├── VERSION.txt
├── README.txt
├── robot.toml.example
└── robot-vX.Y.Z.txt
```

Для macOS имя исполняемого файла не имеет расширения `.exe`. Метка платформы
обязательна: PyInstaller не выполняет кросс-компиляцию, поэтому binary работает
только на указанной ОС и архитектуре.

## Версия и тег

1. Единственный источник версии — `src/__init__.py::__version__` в формате
   SemVer `MAJOR.MINOR.PATCH`.
2. Правила выбора MAJOR, MINOR и PATCH находятся рядом с этим атрибутом.
3. Релизный тег обязан быть строго равен `v<__version__>`. Например, для
   `__version__ = "4.2.0"` допустим только тег `v4.2.0`.
4. Имя архивов, исполняемых файлов, `VERSION.txt`, release notes и заголовок
   секции CHANGELOG используют ту же версию.
5. Ручной запуск workflow обязан получать явный тег или версию. Имя ветки
   никогда не используется как номер версии или часть имени артефакта.

## Зависимость t-invest (подложенный wheel)

T-Invest Python SDK `t-tech-investments` публикуется только на индексе
`opensource.tbank.ru`, а он недоступен с раннеров GitHub Actions. Поэтому SDK
подкладывается в репозиторий:

- wheel лежит в `vendor/wheels/t_tech_investments-<версия>-py3-none-any.whl`;
- в `requirements.txt` SDK закреплён ровно той же версией
  `t-tech-investments==<версия>`;
- CI командой `python -m scripts.offline_requirements` убирает из
  `requirements.txt` внешний индекс, проверяет наличие wheel закреплённой
  версии и ставит зависимости с `--find-links vendor/wheels`. Остальные
  зависимости SDK берутся с PyPI.

Если версия в `requirements.txt` и wheel расходятся, CI останавливается до
установки зависимостей. При обновлении SDK:

1. Установить новую версию локально и прогнать тесты.
2. Положить её wheel в `vendor/wheels/` и удалить старый. Wheel берётся из
   индекса tbank (`pip download t-tech-investments==X.Y.Z --no-deps
   --only-binary=:all: -d vendor/wheels --index-url
   https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple`) либо,
   если индекс недоступен, из кэша pip (`~/Library/Caches/pip/http-v2`), после
   сверки RECORD с установленным пакетом.
3. Обновить пин в `requirements.txt` и закоммитить всё одним коммитом.

## Безопасность и состав

- В дистрибутив включаются только binary, документация, `VERSION.txt`,
  `robot.toml.example` и notes этой версии.
- `.env`, API-токены, рабочий `robot.toml`, базы SQLite, данные, логи и файлы
  из домашнего каталога пользователя включать запрещено.
- `robot.toml.example` создаётся из безопасных публичных значений `default.toml`.
  Пользователь копирует его в `robot.toml` и настраивает локально.
- `VERSION.txt` содержит только `X.Y.Z`; notes — понятные пользователю тезисы
  из соответствующей секции `CHANGELOG.md`.

## Обязательная последовательность релиза

1. Выбрать SemVer-номер и изменить только `src/__init__.py::__version__`.
2. В `CHANGELOG.md` переименовать `## Unreleased` в
   `## X.Y.Z — DD.MM.YYYY`, добавить новый пустой `## Unreleased`.
3. Убедиться, что wheel `t-tech-investments` в `vendor/wheels/` совпадает с
   пином в `requirements.txt` и с версией в `.venv`
   (`.venv/bin/python -m scripts.offline_requirements --output /dev/null`
   завершается без ошибки). Иначе — обновить по разделу
   «Зависимость t-invest».
4. Выполнить проверки:

   ```bash
   .venv/bin/python -m pytest -q
   .venv/bin/python -m ruff check .
   ```

   Заранее признанные замечания в `tools/` и `openspec/_check_sync.py`
   фиксируются отдельно; новые замечания недопустимы.
5. Собрать локальный macOS-дистрибутив нативным PyInstaller и проверить
   исполняемый файл. Из корня репозитория:

   ```bash
   .venv/bin/python -m pip install -r requirements-build.txt
   .venv/bin/python -m scripts.build_distribution --tag vX.Y.Z --platform macos-arm64
   ```

   Минимальные smoke-проверки: запуск `--version`, загрузка bundled
   `default.toml` и `--telegram-chart-smoke`.
6. Создать коммит релиза, тег `vX.Y.Z` и отправить ветку с тегом.
7. CI обязан нативно собрать и проверить macOS arm64 и Windows x64. Windows
   binary получает GitHub Actions на `windows-latest`; собирать его на macOS
   вручную не требуется.
8. Для каждой платформы CI формирует ZIP требуемой структуры, вычисляет
   SHA-256 и прикрепляет ZIP и `checksums.txt` к GitHub Release тега.
9. Релиз завершён только после проверки содержимого GitHub Release: оба
   архива, notes и checksums доступны для скачивания.

## Получение Windows-сборки

Windows-пользователь открывает страницу **Releases**, выбирает нужный тег и
скачивает `robot-vX.Y.Z-windows-x64.zip`. После распаковки он копирует
`robot.toml.example` в `robot.toml`, создаёт свой `.env` с токенами и запускает
`robot-vX.Y.Z-windows-x64.exe` из папки дистрибутива.

Artifacts во вкладке Actions используются только для диагностики неуспешной
сборки. Они не являются каналом распространения релиза.

## Обязанности автоматизации

CI должен технически проверять, а не только описывать следующие условия:

- тег соответствует `__version__` и SemVer;
- `CHANGELOG.md` содержит секцию версии и новый `Unreleased`;
- до упаковки проходят pytest и Ruff;
- Python, PyInstaller и вспомогательные build tools закреплены
  воспроизводимыми версиями;
- `t-tech-investments` ставится из `vendor/wheels/` той версии, что закреплена
  в `requirements.txt`, без обращения к tbank-индексу;
- binary создан под заявленную ОС и архитектуру;
- smoke-проверки выполняются для готового binary;
- ZIP не содержит секретов или пользовательских runtime-файлов;
- SHA-256 вычислены и опубликованы вместе с GitHub Release.

Workflow `build-release.yml` создаёт GitHub Release только после успешной
сборки и smoke-проверок обеих платформ. Временные artifacts остаются только
техническим входом publish job и не используются для распространения.
