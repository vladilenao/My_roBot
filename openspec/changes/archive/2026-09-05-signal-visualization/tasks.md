## 1. Генератор картинок

- [x] 1.1 Создать `tools/visualize_signals.py`: CLI (`--strategy`, `--case`, `--direction`, `--out`), выбор стратегии из реестра, загрузка фикстуры кейса и эталона
- [x] 1.2 Реализовать `PANEL_CONFIG` для `macd_rsi_stoch`: цена (классические бары OHLC), MACD, RSI, Stoch с зонами 70/30 и 80/20 и подписями
- [x] 1.3 Реализовать разметку: жирный серый пунктир на `ei`, тонкий на `ei − STRATEGY_WINDOW`, стрелка `BUY/SELL ВХОД`, горизонтальный текст справа с событиями за окно
- [x] 1.4 Автовыбор первого BUY и SELL из эталона по умолчанию; ошибка, если события направления нет

## 2. Генерация и встраивание

- [x] 2.1 Сгенерировать `openspec/assets/signals/macd_rsi_stoch_BUY.svg` и `_SELL.svg` (кейс BR_1h)
- [x] 2.2 Проверить SVG вручную (в браузере) на соответствие согласованному дизайну

## 3. Зависимость и тест

- [x] 3.1 Добавить `matplotlib` в `requirements-dev.txt`
- [x] 3.2 Создать `tests/snapshot/test_visualization.py`: для стратегий с данными сгенерировать SVG во временную папку и проверить валидность (не пуст, содержит `<svg`, есть хотя бы одна панель)
- [x] 3.3 Прогнать `pytest tests/snapshot/test_visualization.py` (6 passed)

## 4. Валидация и финализация

- [x] 4.1 `openspec validate --changes` — change корректен
- [x] 4.2 Полный `pytest` без регрессий (353 passed)
- [x] 4.3 Синхронизировать дельту в main-спек `openspec/specs/signal-visualization/spec.md` (root-relative ссылки)
- [x] 4.4 Заархивировать change (перенос в `openspec/changes/archive/`)