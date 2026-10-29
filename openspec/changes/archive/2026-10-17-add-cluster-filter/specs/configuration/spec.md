## ADDED Requirements

### Requirement: Настройка кластерного фильтра
Нормативный уровень: MUST.
Привязка стратегии ДОЛЖНА принимать необязательный ключ `clusters` со значением `off`, `shadow` или `enforce` (по умолчанию `off`). Параметры фильтра ДОЛЖНЫ читаться из необязательной секции `[clusters]` файла `robot.toml` (или вшитого `default.toml`):

| Ключ | Тип | По умолчанию |
|---|---|---|
| `stream` | строка, не пустая | `cluster.events` |
| `consumer_group` | строка `^[a-z0-9][a-z0-9-]{0,62}$` | `robot-v1` |
| `pre_move_veto_sigma` | число > 0 | 1.5 |
| `initiative_max_sigma` | число > 0, меньше `pre_move_veto_sigma` | 0.5 |
| `extreme_position` | число в (0, 1] | 0.8 |
| `climax_window_min` | целое ≥ 1, не больше `initiative_window_min` | 30 |
| `initiative_window_min` | целое ≥ 1 | 60 |
| `sigma_days` | целое ≥ 1 | 10 |
| `max_consumer_lag_sec` | целое ≥ 1 | 60 |
| `enforce_level` | `veto` или `veto_strong` | `veto` |

Подключение к Redis ДОЛЖНО браться из секции `[market_structure]` и переменной окружения `MARKET_STRUCTURE_REDIS_PASSWORD`; секция `[clusters]` НЕ ДОЛЖНА принимать хост, порт или пароль. Незнакомые ключи секции, недопустимое значение `clusters` у привязки и нарушение ограничений ДОЛЖНЫ завершать загрузку `ConfigError` с указанием файла и ключа.

#### Scenario: Дефолты
- **WHEN** секции `[clusters]` нет, а у привязки `clusters = "shadow"`
- **THEN** фильтр работает с порогами 1,5σ / 0,5σ / 0,8, окнами 30 и 60 минут, σ за 10 дней, потоком `cluster.events` и группой `robot-v1`

#### Scenario: Привязка без ключа
- **WHEN** у привязки нет ключа `clusters`
- **THEN** для неё фильтр выключен

#### Scenario: Неверный режим
- **WHEN** у привязки `clusters = "on"`
- **THEN** загрузка завершается `ConfigError` с указанием привязки и допустимых значений

#### Scenario: Пароль в секции
- **WHEN** секция `[clusters]` содержит `redis_password`
- **THEN** загрузка завершается `ConfigError` о незнакомом ключе; значение ключа в сообщение не попадает

#### Scenario: Окно кульминации длиннее окна инициативы
- **WHEN** `climax_window_min = 90`, `initiative_window_min = 60`
- **THEN** загрузка завершается `ConfigError`
