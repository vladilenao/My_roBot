## MODIFIED Requirements

### Requirement: Профили привязки в ответах контуров
Нормативный уровень: MUST. Каждая привязка в ответах API управления ДОЛЖНА нести профиль фильтра `filter` и профиль управления `management` — значения из текущей конфигурации `[strategies.*]`, без перевода (`raw` — без фильтра). Это касается:

- участников песочницы — `bindings` в `GET /api/v1/sandbox` и `members` в `GET /api/v1/sandbox/bindings`; если привязки уже нет в конфигурации, значения ДОЛЖНЫ браться из карточки состава, а без них — `null`;
- каталога — `available` в `GET /api/v1/sandbox/bindings`;
- участников эмуляции — новое поле `bindings` у `Member`: `id`, `strategy`, `timeframe`, `filter`, `management` каждой привязки, по которой торгует инструмент. Подбор ДОЛЖЕН совпадать с торговлей: акция — по тикеру, фьючерс — по семейству, а если по семейству привязок нет — по его коду. Привязок нет — пустой список.

Привязка участника эмуляции (`bindings` у `Member`) и привязка состава песочницы (`BindingMember`) ДОЛЖНЫ нести `_links.indicators` — адрес `GET /api/v1/candles/{instrument}/{timeframe}/indicators?binding=<id>` для короткого имени инструмента участника и таймфрейма привязки из текущей конфигурации, если привязка в ней есть, а инструмент участника — контракт или акция; иначе ссылки нет. У привязки песочницы на фьючерс, для которой ядро ещё не выбрало контракт, ссылки нет: в карточке только код семейства. У привязки каталога (`available`) ссылки НЕ ДОЛЖНО быть: там инструмент — код семейства, а не контракт.

Новые поля НЕ ДОЛЖНЫ менять прежние поля ответов.

#### Scenario: Фильтр и управление у привязки песочницы
- **GIVEN** в составе песочницы привязка `future-ng-macd` с `filter = "triple_screen"` и `management = "levels_rr"`
- **WHEN** приходит `GET /api/v1/sandbox`
- **THEN** у привязки `future-ng-macd` в `bindings` есть `filter: "triple_screen"` и `management: "levels_rr"`

#### Scenario: Привязка без фильтра в каталоге
- **GIVEN** у привязки `share-sber-ma` в конфигурации `filter = "raw"`
- **WHEN** приходит `GET /api/v1/sandbox/bindings`
- **THEN** у `share-sber-ma` в `available` — `filter: "raw"`, а не пропуск поля

#### Scenario: Привязки участника эмуляции
- **GIVEN** эмуляция торгует NG-12.26, в конфигурации у семейства NG одна привязка `future-ng-macd`
- **WHEN** приходит `GET /api/v1/emulation/instruments`
- **THEN** у NG-12.26 `bindings` — один элемент с `id`, `strategy`, `timeframe`, `filter` и `management` привязки `future-ng-macd`

#### Scenario: Инструмент без привязок
- **GIVEN** в составе эмуляции акция, для которой в конфигурации нет привязок
- **WHEN** приходит `GET /api/v1/emulation`
- **THEN** у этого участника `bindings` — пустой список

#### Scenario: Ссылка на индикаторы привязки
- **GIVEN** эмуляция торгует NG-12.26 по привязке `future-ng-macd` на `15m`
- **WHEN** приходит `GET /api/v1/emulation/instruments`
- **THEN** у привязки `future-ng-macd` участника NG-12.26 `_links.indicators.href` равен `/api/v1/candles/NG-12.26/15m/indicators?binding=future-ng-macd`

#### Scenario: Привязки уже нет в конфигурации
- **GIVEN** в составе песочницы привязка, которой больше нет в `[strategies.*]`
- **WHEN** приходит `GET /api/v1/sandbox`
- **THEN** у этой привязки нет `_links.indicators`, прочие поля как раньше
