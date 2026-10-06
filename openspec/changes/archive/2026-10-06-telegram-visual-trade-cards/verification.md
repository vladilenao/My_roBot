# Проверка telegram-visual-trade-cards — 06.10.2026

## Результат

Штатные visual snapshots, отдельные Telegram-шаблоны, headless-графики,
Bot API transport, обновляемые карточки и durable receipts реализованы.
Подтверждены локальные проверки; задача 7.2 закрыта по явному решению пользователя,
который самостоятельно выполняет commit/push и Windows CI. Результат Windows
build/smoke агентом не подтверждён и не входит в перечень успешно выполненных проверок.

- `.venv/bin/python -m pytest -q`: **1907 passed**, 39.62 с.
- `.venv/bin/python -m ruff check src tests run.py`: **All checks passed**.
- `.venv/bin/python -m ruff check .`: **25 прежних разрешённых ошибок**,
  только `tools/download_snapshot_data.py`, `tools/visualize_signals.py`,
  `openspec/_check_sync.py`; новых ошибок рабочего кода/тестов нет.
- `openspec validate telegram-visual-trade-cards --strict`: **valid**.
- `git diff --check`: без ошибок.
- Изолированный прогон notification/events/lifecycle/runtime + management replay:
  **280 passed**. Эталоны стратегических сигналов и старых management replay не менялись.

## Покрытие требований

| Требование | Проверка |
|---|---|
| Принятая в работу сделка, целые аллокации, цена/единица | `test_visual_plan.py`, admission/runtime unit tests; `approved-plan`, Q=1/3 visual cases |
| Отдельные Telegram-шаблоны/русский словарь | `test_visual_delivery.py`, 14 visual cases; консольные template tests без смены ожидаемого оформления |
| Самодостаточный immutable snapshot/schema/Decimal | `test_visual.py`, `test_visual_plan.py`, `test_event_schemas.py`, schema JSON сравнивается с кодом |
| Каталог 19, девять Telegram типов, explicit override | `test_bus.py`, фабрика/конфигурационные unit tests, неизменённая ветка пользовательских overrides |
| Подтверждённый исход/stop effective ACK | `test_visual_lifecycle.py`: next-bar не публикуется, effective old/new сохраняются, duplicate ACK после рестарта не повторяется |
| Графики plan/stop/final, SHORT, факт/план | `test_telegram_visuals.py`, `test_scene_short_mirrors_levels_and_markers_and_has_true_effective_step` |
| Частичный вход/цель, ADD, REDUCE, отмена/отказ | `test_visual_lifecycle.py`, snapshot cases partial-entry/partial-target/add/partial-reduce |
| Общий результат/комиссии/unknown/RAW | независимая арифметика 98−6=92, реальные fills через Storage/manager, configured/unknown/RAW template checks |
| Неблокирующий транспорт, HTTP ok/message_id, HTML/фото | mocked Bot API lifecycle, HTTP failures/429/uncertain timeout; HTML entity/UTF-16/long-caption проверки |
| Исходный график и связанные кнопки без обсуждений | lifecycle mock: три фото, caption-edit прежнего корня, public/private links, missing root/metadata/root-edit failure |
| Durable correlation, namespace, рестарт, stale/dedup | restart/two trades/same-price new fill/crash-before-receipt tests; namespace зависит от каталога/чата/отправителя |
| Ограничения окна/истории/causality | market/gap/monthly boundary/history grouping unit tests; long-window/gap snapshots, свечи проверяются на close≤as_of |
| Overflow/drain/изоляция | blocked-transport test с управляемым threading.Event, initialization failure, worker continuation после ошибки, console unchanged |

## Визуальная сверка

`test_render_review_artifacts` построил три PNG **1440×960** штатным renderer из
явно синтетического `tests/snapshot/data/SBER_15m/candles.csv`. Plan/stop/final
прочитаны визуально и сопоставлены с одобренными Telegram-постами 720–723:
тёмная тема, крупный жирный русский заголовок, зелёные цели/синий вход/красный
стоп, цены и целые количества, маркеры исполнения, первоначальный серый пунктир,
подтверждённая ступень и отдельный итог **+92 ₽ / +98 ₽ / комиссия 6 ₽**.
Основные подписи и маркеры не перекрывают друг друга, кириллица отображается.
Runtime не показывает будущую полную свечу: последний fill допускается отдельным
маркером правее последней закрытой свечи. Активная линия стопа начинается с
момента действия, а не распространяет новый стоп на прошлую историю.

Текущие review artifacts (временные файлы теста):
`/private/var/folders/zs/5zw60s0d0rnc4d989fq19mpw0000gn/T/pytest-of-vladilenaosipova/pytest-880/test_render_review_artifacts0/telegram-{plan,stop,final}.png`.
Восстановить можно повтором `test_render_review_artifacts`; тесты не отправляют
сообщения в рабочий Telegram-канал.

## Сборка и оставшаяся проверка

Локальная PyInstaller сборка завершилась успешно (6.22.2, Python 3.13.1,
macOS arm64); hook явно выбрал только Agg. Команда:

```bash
PYINSTALLER_NAME=robot-telegram-cards-smoke .venv/bin/python -m PyInstaller run.spec --noconfirm --distpath /var/folders/zs/5zw60s0d0rnc4d989fq19mpw0000gn/T/opencode/telegram-card-dist --workpath /var/folders/zs/5zw60s0d0rnc4d989fq19mpw0000gn/T/opencode/telegram-card-build
```

Построенный бинарник `telegram-card-dist/robot-telegram-cards-smoke` запущен с
`--telegram-chart-smoke` вне рабочей директории. Exit 0, результат:
**«График Telegram: Agg и кириллица доступны, PNG 44760 байт.»**
Флаг не запускает торговый runtime/Telegram; проверены доступность headless
backend и кириллического шрифта в one-file дистрибутиве.

В `.github/workflows/build-release.yml` добавлен тот же smoke для обоих OS jobs.
**Результат Windows job с текущим кодом агентом не подтверждён.** Пользователь
решила самостоятельно выполнить commit/push и запуск CI, затем явно поручила
отметить задачу 7.2 выполненной и продолжить workflow. Задача принята пользователем;
эта отметка не заменяет CI-результат. Попытка прочитать GitHub Actions через `gh`
не удалась: GitHub CLI не авторизован. Версия/тег/релиз не создавались.

## Завершение OpenSpec

По решению пользователя задачи отмечены **31/31**, затем выполнены sync и архив.
В `openspec/specs/notification/spec.md` обновлены шесть требований;
`openspec/specs/telegram-trade-cards/spec.md` создан с семью требованиями и
исходным Purpose. Сравнение JSON-представлений через `openspec show` подтвердило
совпадение всех **13 требований и их сценариев** с delta-спецификациями.
`openspec validate --specs`: **36 passed, 0 failed**.
Change архивирован в `openspec/changes/archive/2026-10-06-telegram-visual-trade-cards/`;
CLI использовал `--skip-specs` только для исключения повторного переноса уже
синхронизированных требований. Метаданные `.openspec.yaml` сохранены.
