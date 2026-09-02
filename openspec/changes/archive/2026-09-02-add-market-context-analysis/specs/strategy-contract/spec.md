## MODIFIED Requirements

### Requirement: Тип решения без форматирования
Метод `decide()` ДОЛЖЕН возвращать решение в виде `SignalType` (`BUY`, `SELL`, `HOLD`) и цены; формирование человекочитаемых текстов в стратегии ЗАПРЕЩЕНО. Сама стратегия при вызове `decide()` НЕ ДОЛЖНА заполнять поля SL/TP/trend — они остаются `None` и заполняются внешними компонентами (фильтром тренда и риск-менеджером) после `decide()`. Модель `Decision` ДОЛЖНА содержать перечисленные поля с дефолтами `None` для обратной совместимости: `stop_loss: float | None`, `take_profit: float | None`, `sl_distance_pct: float | None`, `tp_distance_pct: float | None`, `sl_level_label: str | None`, `tp_level_label: str | None`, `trend_direction: str | None`, `trend_confidence: float | None`. Существующие стратегии, не заполняющие эти поля, продолжают работать.

#### Scenario: Решение как перечисление
- **WHEN** все сигнальные суммы стратегии положительны на последней свече
- **THEN** `decide()` возвращает `SignalType.BUY` и текущую цену, а все поля SL/TP/trend имеют значение None без какого-либо текста

#### Scenario: Обратная совместимость
- **WHEN** существующая стратегия возвращает Decision только с signal_type и price
- **THEN** все дополнительные поля имеют значение None, система работает без ошибок

#### Scenario: Обогащённое решение
- **WHEN** фильтр и риск-менеджер применили Decision
- **THEN** Decision содержит заполненные SL/TP/trend поля, которые отображаются в уведомлении
