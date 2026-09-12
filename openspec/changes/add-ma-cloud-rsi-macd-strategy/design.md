## Context

Новая стратегия MA Cloud RSI MACD добавляется в проект с тремя существующими стратегиями: `macd_rsi_stoch`, `flat_triangle`, `harmonic_abcd`. Архитектура проекта: изолированные пакеты стратегий с единым контрактом (`Strategy`), реестр для динамической диспетчеризации, индикаторы как отдельные frozen dataclass'ы наследующие `Indicator`.

Текущая стратегия MA Cloud RSI MACD отличается от существующих сложностью: она первая поддерживает добор (scale-in), ступенчатый выход и внутреннее состояние (отслеживание количества контрактов и истории срабатываний индикаторов). Существующие стратегии просты: все три индикатора срабатывают в окне → BUY/SELL/HOLD. Новая стратегия требует архитектурного расширения контракта `Decision`.

## Goals / Non-Goals

**Goals:**
- Реализовать двустороннюю стратегию MA Cloud RSI MACD с логикой «третий сигнал»
- Расширить контракт `Decision` полями `action`, `exit_reason`, `exit_contracts`
- Сохранить обратную совместимость: существующие стратегии продолжают работать без изменений
- Следовать паттернам проекта: `@register`, frozen dataclass'ы, `StrategyConfig`, `StrategyName` Literal
- Добавить два новых индикатора: MA (облако) и MACD Zero Cross

**Non-Goals:**
- Многопозиционный менеджер (position manager) — вне scope этого изменения; стратегия хранит состояние позиций внутри себя
- Автоматическое управление объёмом добора — логика 50% от текущей позиции упрощена до константы в `DEFAULT_CONFIG`

## Decisions

### D1: Расширение `Decision` — новые поля
Контракт `Decision` расширяется тремя новыми необязательными полями:
- `action: str | None` — `"entry"`, `"scale_in"` или `None`
- `exit_reason: str | None` — причина выхода
- `exit_contracts: int | None` — количество контрактов для частичного выхода; `None` = полный

Все поля имеют дефолт `None` → обратная совместимость. Существующие стратегии (`macd_rsi_stoch`, `flat_triangle`, `harmonic_abcd`) не затронуты.

### D2: Внутреннее состояние стратегии
Стратегия хранит состояние:
- `_pending_indicators: dict[str, list[str]]` — словарь направленийLong/Short, содержащий список сработавших индикаторов (`"ma_cloud"`, `"rsi"`, `"macd_zero"`), и время первого срабатывания (свеча)
- `_positions: dict[str, int]` — количество контрактовLong иShort (0 = нет позиции)

Это отходит от паттерна stateless, но оправдано: логика добора и ступенчатого выхода невозможна без знания текущего размера позиции. Состояние сбрасывается при полном выходе.

### D3: Алгоритм «третий сигнал» —伪代码
```python
# _pending_indicators хранит индикаторы, сработавшие за последние 6 свечей
# При каждой новой свече: удаляем индикаторы старше 6 свечей

def decide(self, ta, timeframe=None):
    row = ta.iloc[-1]
    prev = ta.iloc[-2]

    ma_signal = row["ma_cloud_signal"]  # +1 / -1 / 0
    rsi_signal = row["rsi_signal"]
    macd_signal = row["macd_zero_signal"]

    for direction, check_signals in [("long", [+1,+1,+1]), ("short", [-1,-1,-1])]:
        needed = check_signals  # [ma, rsi, macd] direction values
        actual = [ma_signal, rsi_signal, macd_signal]

        # Определяем какие индикаторы сработали на этой свече
        fired = []
        for ind_name, ind_val in zip(["ma_cloud","rsi","macd_zero"], actual):
            if ind_val == needed[0] if ind_name=="ma_cloud" else \
               ind_val == needed[1] if ind_name=="rsi" else \
               ind_val == needed[2]:
                fired.append(ind_name)

        # Добавляем к pending, удаляем старше 6 свечей
        # Если 3 из 3 сработали → entry signal
        if len(self._pending_indicators[direction]) + len(fired) >= 3:
            return entry_signal(direction)

    # Проверка условий выхода (если позиция открыта)
    # Проверка добора (если позиция открыта и цена в облаке)
    return HOLD
```

### D4: Новые индикаторы — `MaCloudIndicator` и `MacdZeroCrossIndicator`
Оба — frozen dataclass'ы, наследующие `Indicator`, с `compute()` вычисляющим сигнальные колонки.

**MaCloudIndicator**: вычисляет SMA 10 и SMA 40, определяет кроссовер. Warmup = 40. Сигнальная колонка: `ma_cloud_signal`.

**MacdZeroCrossIndicator**: вычисляет MACD (12,26,9) через `pandas_ta_classic`, определяет пересечение сигнальной линией нуля. Warmup = 35 (26+9). Сигнальная колонка: `macd_zero_signal`.

RsiIndicator переиспользуется существующий (`RsiIndicator(period=14)`).

### D5: Пакет стратегии
Файл: `src/strategies/ma_cloud_rsi_macd_strategy.py`. Имя: `ma_cloud_rsi_macd`. Маппинг в `run.py` через `DEFAULT_CONFIG`. `StrategyName` в `names.py` расширяется на `"ma_cloud_rsi_macd"`. `DEFAULT_CONFIG` прописывается в TOML-конфиге.

### D6: Warmup и STRATEGY_WINDOW
`STRATEGY_WINDOW = 1` (решение на текущей свече). Warmup = max(40, 35, 14) = 40 баров. `required_history() = strategy_window + warmup = 41`.

## Risks / Trade-offs

### R1: Внутреннее состояние стратегии
**Риск**: стратегия перестаёт быть stateless; при краше/рестарте теряется информация о позиции.
**Mitigation**: состояние хранится только в RAM; при рестарте бота позиции пересчитываются через API-запрос (существующая логика `_load_positions` в `run.py`). Это нормальное поведение для текущей архитектуры.

### R2: Расширение `Decision` — риск «раздутия» контракта
**Риск**: добавление полей в `Decision` увеличивает количество комбинаций, усложняет понимание.
**Mitigation**: все новые поля необязательны с дефолтом `None`; существующие стратегии их не используют. Контракт остаётся простым для потребителей.

### R3: Стоимость вычислений
**Риск**: новые индикаторы (SMA 10/40, MACD) добавляют ~2% к времени обработки.
**Mitigation**: индикаторы вычисляются локально через `pandas_ta_classic`, который оптимизирован для batch-вычислений. Прогрев 40 баров покрывает оба индикатора. Стоимость пренебрежимо мала по сравнению с API-запросами.
