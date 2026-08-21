## 1. Миграция фикстур

- [x] 1.1 Перенести кейс: `mv tests/data/macd_rsi_stoch/NG_1h tests/data/NG_1h`; переименовать `expected_signals.csv` → `macd_rsi_stoch_expected_signals.csv`; удалить опустевшую папку `tests/data/macd_rsi_stoch/`
- [x] 1.2 Убедиться, что `candles.csv` не изменился байт-в-байт (md5 до/после)

## 2. Тест стратегии

- [x] 2.1 В `tests/strategies/test_macd_rsi_stoch.py` заменить скан подпапок на `glob("*/macd_rsi_stoch_expected_signals.csv")` по точному имени; id кейса = имя родительской папки
- [x] 2.2 Обновить пути загрузки: `tests/data/<case>/candles.csv` и `tests/data/<case>/<strategy>_expected_signals.csv`; вынести имя эталона в константу стратегии
- [x] 2.3 Проверить прогон `pytest tests/strategies/ -v`: кейс NG_1h проходит, эталон не потребовал регенерации

## 3. Скрипт скачивания

- [x] 3.1 В `tools/download_snapshot_data.py` писать свечи в `tests/data/<case>/candles.csv`, а эталон — в `tests/data/<case>/<strategy>_expected_signals.csv` (стратегия только в имени файла, не в пути)
- [x] 3.2 Проверить идемпотентность: повторный запуск той же команды перезаписывает candles.csv и свой эталон, чужие файлы кейса не трогает

## 4. Фиксация правила и проверка

- [x] 4.1 Добавить блок `context:` в `openspec/config.yaml`: правило наименования (кейс = `<ИНСТРУМЕНТ>_<ТАЙМФРЕЙМ>/` с одним `candles.csv`; эталон = `<стратегия>_expected_signals.csv`; стратегия = `tests/strategies/test_<стратегия>.py`)
- [x] 4.2 Прогнать весь набор `pytest` — ничего не сломано, структура плоская
