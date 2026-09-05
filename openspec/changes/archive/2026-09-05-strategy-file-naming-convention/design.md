## Context

См. proposal.md — Why. В `src/strategies/` 3 файла стратегий (`macd_rsi_stoch.py`, `flat_triangle.py`, `harmonic_abcd.py`) соседствуют с инфраструктурой (`registry.py`, `strategy.py`, `signals.py`, `names.py`, `contracts.py`, `indicators/`), и имена файлов не передают роль модуля. Имена регистрации (`NAME`/`StrategyName`/привязки) зафиксированы контрактом и не должны меняться. Реестр использует `pkgutil.iter_modules` для автообнаружения и содержит skip-список в `registry.py`.

## Goals / Non-Goals

**Goals**
- Единое правило имени файла стратегии: `src/strategies/<имя>_strategy.py`.
- Инфраструктура в `src/strategies/` без суффикса `_strategy`; `strategy.py` → `base_strategy.py`.
- Сохранение всех идентификаторов стратегий (имён регистрации) без изменений.

**Non-Goals**
- Не переименовываются `NAME`/`StrategyName`/привязки в `config.py` и эталоны snapshot.
- Не реорганизуется структура пакета (индикаторы, signals) — меняются только имена файлов.
- Не меняются контракты (`Decision`, `Strategy`, `StrategyConfig`) и их поведение.

## Decisions

- **D1. Переименование через `git mv`.** Файлы стратегий и `strategy.py` перемещаются `git mv`, сохраняя историю и без изменения содержимого (кроме импортов). Альтернатива «создать новый файл, удалить старый» потеряла бы git-историю.
- **D2. Суффикс `<имя>_strategy.py`, не подпапки.** Предложение о группировании стратегий в подпапки `src/strategies/<имя>/` отклонено: текущая конвенция пакета — плоская структура, и она же выбранная (см. strategy-contract); задача — устранить путаницу имён, а не менять архитектуру.
- **D3. Имя файла не влияет на `NAME`.** Идентификатор стратегии остаётся основанием без суффикса (`NAME = "macd_rsi_stoch"`), суффикс `_strategy` — только у имени модуля. Это отделяет физическое имя файла от логического идентификатора, на который опираются реестр, конфигурация и эталоны.
- **D4. Skip-список реестра.** В `_discover_strategies` исключение `"strategy"` заменяется на `"base_strategy"`; заодно убирается устаревшее исключение `"base"` (модуля `base.py` давно нет). Новые имена `*_strategy.py` не попадают в исключения и по-прежнему обнаруживаются — проверяется `test_discover_strategies*` и полным pytest.
- **D5. Лакмусовая проверка «импорт лёгкий».** Тест `test_import_names_module_is_light` в `test_registry.py` проверяет, что импорт `src.strategies.names` НЕ подтягивает `src.strategies.macd_rsi_stoch`; строка проверки обновляется на новое имя модуля. Реестр по-прежнему не импортирует стратегии на уровне модуля.

## Risks / Trade-offs

- [Пропущенный импорт старого имени файла] → после `git mv` и замены импортов полный `pytest` ловит искры; дополнительно `grep -rn` по старым именам модулей = 0.
- [Лёгкость импорта `names.py` сломается из-за переименования] → обновляется assert в `test_import_names_module_is_light` на новое имя модуля; ленивость регистрации (флаг `_packages_loaded`) не трогается.
- [Эталоны/снапшоты ссылаются на идентификатор] → идентификаторы не меняются, эталоны `*_expected_signals.csv` и значения `NAME` остаются на месте; snapshot-раннер и helper не правят тестовых данных.

## Migration Plan

1. `git mv` 4 файлов в `src/strategies/` с новыми именами.
2. Обновить импорты во `src/strategies/registry.py`, `run.py`, `tools/download_snapshot_data.py`, `tests/snapshot/{helper,test_strategies}.py`, `tests/unit/strategies/{test_registry,test_signals,test_strategy_builder,test_harmonic_abcd}.py`.
3. Обновить skip-список реестра и строку лёгкости импорта.
4. Полный `pytest` — все зелёные.
5. Обновить `openspec/config.yaml` (контекст) и при архивации — дельту `strategy-contract`.

Откат: `git mv` обратно + обратная замена импортов; конвенция откатывается вместе со спекой.

## Open Questions

Нет.