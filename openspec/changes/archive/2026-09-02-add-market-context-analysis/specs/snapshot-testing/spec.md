## ADDED Requirements

### Requirement: Snapshot-раннер для analysis-слоя
Система ДОЛЖНА предоставлять отдельный data-driven раннер `tests/snapshot/test_analysis.py` для проверки детерминированности analysis-слоя (тренд и уровни S/R) на тех же общих свечах кейсов `tests/snapshot/data/*/<ИНСТРУМЕНТ>_<ТАЙМФРЕЙМ>/candles.csv`. Раннер ДОЛЖЕН параметризоваться по папкам кейсов, а не по стратегиям, и использовать собственные эталоны с суффиксом `_expected_context.csv`. Раннер и его эталоны НЕ ДОЛЖНЫ пересекаться с раннером стратегий `test_strategies.py` и его эталонами `*_expected_signals.csv`.

#### Scenario: Автоподхват кейса для analysis
- **WHEN** в папку кейса добавлен файл `trend_expected_context.csv` (или `sr_levels_expected_context.csv`)
- **THEN** раннер `test_analysis.py` выполняет проверку для этого кейса без правки кода теста

#### Scenario: Отсутствие файлов данных
- **WHEN** в `tests/snapshot/data/` нет ни одного файла `*_expected_context.csv`
- **THEN** параметризация analysis-раннера даёт ноль кейсов; прогон завершается без ошибок

#### Scenario: Изоляция от стратегических эталонов
- **WHEN** в кейсе лежат и стратегические (`*_expected_signals.csv`), и analysis (`*_expected_context.csv`) эталоны
- **THEN** анализ-раннер обрабатывает только `*_expected_context.csv`, а раннер стратегий — только `*_expected_signals.csv`; файлы не смешиваются

### Requirement: Эталон analysis-контекста
Эталон analysis-слоя ДОЛЖЕН храниться рядом с фикстурой свечей и называться по паттерну `<modes>_expected_context.csv`, где `<modes>` кодирует входной модуль анализа (например `trend`, `sr_levels`). Типичные обязательные данные: для тренда — `datetime` и `trend_direction`/`trend_strength`; для S/R — отсортированные уровни с `price`, `type`, `strength`, `label`. Содержимое эталона ДОЛЖНО полностью определяться боевым модулем analysis-слоя, а не дублировать правила стратегий.

#### Scenario: Эталон тренда
- **WHEN** эталон `trend_expected_context.csv` создан для кейса
- **THEN** он содержит столбцы `datetime` и `trend_direction`/`trend_strength`, рассчитанные по последней закрытой свече кейса

#### Scenario: Эталон уровней S/R
- **WHEN** эталон `sr_levels_expected_context.csv` создан для кейса
- **THEN** он содержит отсортированные уровни с `price`, `type` (support/resistance), `strength` и `label`, согласно ограничениям `min_touches` и `max_levels`

### Requirement: Регенерация analysis-эталонов флагом --update-snapshots
Раннер `test_analysis.py` ДОЛЖЕН поддерживать тот же флаг `--update-snapshots`, что и раннер стратегий (определённый в `tests/conftest.py`): при запуске с флагом эталоны analysis-слоя ДОЛЖНЫ пересчитываться боевым модулем и перезаписываться.

#### Scenario: Осознанное обновление analysis-эталона
- **WHEN** разработчик изменил логику тренда/S/R и запустил `pytest tests/snapshot/test_analysis.py --update-snapshots`
- **THEN** `*_expected_context.csv` перезаписывается текущим поведением analysis-слоя; diff ревьюится и коммитится

#### Scenario: Обычный прогон не изменяет analysis-эталоны
- **WHEN** pytest для `test_analysis.py` запущен без флага
- **THEN** файлы `*_expected_context.csv` остаются неизменными

### Requirement: Сравнение analysis-эталона
Сравнение анализа с эталоном ДОЛЖНО выполняться через `pandas.testing.assert_frame_equal` с допуском округления float (`check_exact=False`, малый rtol). При расхождении тест ДОЛЖЕН указывать первую расходящуюся строку.

#### Scenario: Полное совпадение
- **WHEN** фактические trend/S/R равны эталону
- **THEN** тест проходит

#### Scenario: Расхождение
- **WHEN** фактический тренд или уровень S/R отличается от эталона
- **THEN** тест падает с указанием первой расходящейся строки

### Requirement: Детерминизм analysis-слоя
Snapshot-тесты analysis-слоя ДОЛЖНЫ быть детерминированными: результат зависит только от содержимого фикстуры, боевого модуля analysis и зафиксированных версий библиотек.

#### Scenario: Непреднамеренное изменение анализа
- **WHEN** рефакторинг или обновление зависимости меняет расчёт тренда или уровней S/R
- **THEN** snapshot-тест analysis-слоя падает до попадания изменений в продакшн-цикл бота
