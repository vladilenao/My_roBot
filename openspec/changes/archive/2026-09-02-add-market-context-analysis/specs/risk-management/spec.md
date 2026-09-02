## Purpose

Рассчитывает уровни стоп-лосс и тейк-профит для каждого торгового сигнала на основе ближайших уровней поддержки/сопротивления. Является чистым трансформером Decision, не зависит от ExecutionPort.

## ADDED Requirements

### Requirement: Расчёт стоп-лосс по уровням S/R
Система ДОЛЖНА рассчитывать стоп-лосс на основе ближайшего уровня S/R. Для BUY: стоп размещается ниже ближайшего уровня поддержки. Для SELL: стоп размещается выше ближайшего уровня сопротивления. Если уровни S/R отсутствуют — используется fallback (процент от текущей цены).

#### Scenario: BUY с ближайшим support
- **WHEN** сигнал BUY и есть уровень support ниже текущей цены
- **THEN** `stop_loss` = цена этого уровня support (ближайший к цене в направлении вниз), `sl_level_label` = метка уровня

#### Scenario: SELL с ближайшим resistance
- **WHEN** сигнал SELL и есть уровень resistance выше текущей цены
- **THEN** `stop_loss` = цена этого уровня resistance (ближайший к цене в направлении вверх), `sl_level_label` = метка уровня

#### Scenario: Нет уровней S/R или нет уровня в нужном направлении
- **WHEN** ближайший уровень в нужном направлении (support ниже цены для BUY, resistance выше цены для SELL) отсутствует, включая пустой список уровней
- **THEN** `stop_loss` = цена ± default_sl_pct (2%), `sl_level_label` = None

### Requirement: Расчёт тейк-профита по соотношению risk/reward
Система ДОЛЖНА рассчитывать тейк-профит на основе расстояния до стоп-лосс и заданного соотношения risk/reward. Для BUY: тейк = цена + (цена - стоп) × ratio. Для SELL: тейк = цена - (стоп - цена) × ratio. Если расчётный тейк попадает на уровень S/R — используется цена этого уровня.

#### Scenario: Тейк по risk/reward
- **WHEN** стоп-лосс рассчитан и risk/reward ratio = 2.0
- **THEN** `take_profit` рассчитывается как цена + 2 × (цена - стоп) для BUY

#### Scenario: Тейк на уровне S/R
- **WHEN** расчётный тейк-профит близко к уровню S/R (в пределах 1%)
- **THEN** `take_profit` = цена этого уровня, `tp_level_label` = метка уровня

### Requirement: Обогащение Decision уровнями
Риск-менеджер ДОЛЖЕН добавлять к Decision поля `stop_loss`, `take_profit`, `sl_level_label`, `tp_level_label`, `sl_distance_pct`, `tp_distance_pct`. Процентные расстояния рассчитываются от текущей цены.

#### Scenario: BUY с полным набором полей
- **WHEN** сигнал BUY, есть support и resistance
- **THEN** Decision содержит stop_loss, take_profit, sl_level_label, tp_level_label, sl_distance_pct, tp_distance_pct

#### Scenario: HOLD пропускается
- **WHEN** входной Decision имеет тип HOLD
- **THEN** риск-менеджер возвращает Decision без изменений
