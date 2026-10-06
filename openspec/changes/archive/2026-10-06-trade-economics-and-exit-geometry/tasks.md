## 1. Baseline и загрузка политики

- [x] 1.1 Зафиксировать исходные проверки и audit 8.1–8.3 `stop-geometry-and-plan-economics`, сверить его синхронизированный baseline и доступность `.venv/bin/python -m ruff` (восстановить Ruff из dev-окружения, если отсутствует). Исправления старого risk runtime выполнять в target 5.3–5.6; предшественник архивировать до target sync, не требуя предварительного внедрения групп. До интеграционного replay закрыть 6.1–6.3 `history-data-gaps`
- [x] 1.2 В `src/config_loader.py` реализовать единый portfolio_pct с дефолтом 2 и диапазоном [0,100], ConfigError с миграцией trade_pct/instrument_pct/groups, min_trade_risk_pct≤portfolio_pct, единицы commission/slippage, пороги экономики/BE и списки/legacy-пару atr_trend. Выбирать явную форму до merge дефолтов; проверить нечисловые/boolean/нефинитные, неизвестные ключи и нули
- [x] 1.3 Обновить `default.toml`: один portfolio_pct=2 без trade_pct/instrument_pct/groups, max_qty как предел количества, пороги экономики 2/1.5/0.25, BE 1.5, лестница atr_trend [1,2]/[0.25,0.25]. Сохранить levels_rr [1,2], документировать единицы расходов и явную миграцию без обещания прибыльности

## 2. Модель, версии и миграция

- [x] 2.1 В `src/trade_management/models.py`, `pipeline.py` и payload-сериализации добавить algorithm_version, frozen cost/admission snapshot, price_basis целей, целые аллокации и известную фиксированную часть; None для неизвестного общего reward/expected_r/payoff. Различить аудит процента прежнего допуска и текущую глобальную PortfolioBudgetPolicy; проверить round-trip без переписывания старого плана
- [x] 2.2 В `src/trade_journal/schema.py` реализовать атомарную additive миграцию v9→v10: provenance/опорные поля fills, cost adjustments с уникальным ID, observations и фактический D0; сохранить counts/FK/счёт/цели/защиту/резервы, проверить откат при ошибке
- [x] 2.3 В `storage.py` и восстановлении менеджера/симулятора реализовать legacy-v1 versus economics-v2 для профилей/фактов с сохранением планов/уровней/fees. Единый текущий бюджет учитывает оба поколения и старые pending без переключения профильных правил; старый reader отклоняет новую схему

## 3. Исполнение и однократный финансовый учёт

- [x] 3.1 Расширить `src/broker/port.py` известностью суммы комиссии и provenance; реализовать канонизацию broker/configured/unknown из сохранённого trade snapshot. Независимо проверить приоритет известного broker нуля
- [x] 3.2 В `journal_broker.py` начислять configured fee на incremental quantity OPEN/ADD/TARGET/STOP/CLOSE/REDUCE; ACK/REJECT/CANCEL без исполнения не начисляют комиссию. Пример 10 вход + 5 + 5 выходов должен давать 15+7.5+7.5=30 ₽
- [x] 3.3 Сохранять benchmark входа/добора, активный стоп/цель и цену решения рыночного выхода вместе с execution; считать signed cash deviation с направлением ордера. Проверить gap и favorable deviation, отсутствие опоры, отсутствие добавочного списания s или фактического deviation
- [x] 3.4 В `reducer.py`/`storage.py` сохранить атомарность и execution-id идемпотентность при новых полях; проверить позицию/счёт/net и связанные резервы/цели/фазу на входе, частичном fill, доборе и выходе
- [x] 3.5 Реализовать адресное позднее уточнение broker-комиссии через cost adjustment: абсолютная новая сумма, delta к прежней комиссии, устойчивый ID, atomic account/position/provenance update без изменения gross/quantity. Проверить повтор, конфликтующий payload, возврат оценки и рестарт
- [x] 3.6 Реализовать явные LIMIT новых входов/доборов с собственным пределом в плане/action/outbox и восстановлением ACK-pending: касание на следующем доступном баре, более выгодный open, продолжение ожидания/срок/отмена, полное OHLC-исполнение без очереди. Проверить open versus intrabar причинность целей/стопа, BUY/SELL, legacy market и отсутствие покупки за D

## 4. Геометрия, цели и общая аллокация

- [x] 4.1 В общей геометрии добавить stop-bounds-conflict при floor>cap либо tick>cap; сохранить независимые границы без ATR, разрешённое округление <tick и числовую трассу. Legacy цены не переписывать
- [x] 4.2 Пересчитывать все R-цели с `rD+c/V`, включая неизменённый structural stop; округлять LONG вверх/SHORT вниз без накопления компенсации при rebase, непригодную цель возвращать как target-not-ahead
- [x] 4.3 В rebase/профилях различать R и absolute price_basis: pattern midpoint/D остаются абсолютными после гэпа/добора, план неизменяем, исполненные цели сохраняют факт, активный стоп не ослабляется
- [x] 4.4 Применить единый allocator в plan economics, менеджере, симуляторе, target state reducer и восстановлении: final remainder у полного выхода, floor каждой ступени и retained remainder у atr_trend; проверить Q=1/3/8 и partial entry
- [x] 4.5 Фиксировать D0 по первому подтверждённому входу/стопу отдельно от текущей защиты; накапливать Qmax и использовать D0×V×Qmax в денежной R-мере. Проверить гэп с/без rebase, добор, TP1 и рестарт

## 5. Экономический допуск и бюджет

- [x] 5.1 В `economics.py` считать неокруглённые R,C,S, budget risk и gross по целым аллокациям; полный versus partial reward/payoff согласно specs/trade-management. Форматирование округлять отдельно
- [x] 5.2 После проверки портфеля и максимального положительного sizing до reserve применить порядок cost-exceeds-reward, risk-below-floor, risk-cost-ratio, slippage-risk-ratio, payoff-below-floor. Добавить risk-state-unknown и русские описания/audit без фиктивного trade FK; успешный меньший Q не возвращает rejection, окончательный отказ не переписывает уже записанный план
- [x] 5.3 В `src/portfolio/risk.py` и адаптере менеджера реализовать единый B/portfolio budget и текущий риск от фактической средней до confirmed_stop на оставшемся q, с clamp zero на trade и будущими расходами. Не подставлять первоначальный стоп при прибыльной защите, не добавлять оплаченные fees или повторно вычитать realized PnL; проверить актуальную B/equity и различие с Initial Risk
- [x] 5.4 Согласовать ledger открытого риска/ГО и незаполненных резервов с атомарным sizing/reserve: priority DESC, устойчивый tie-break, максимальный целый Q по риску/ГО/запросу/max_qty/профилю, корректный добор и перенос partial fill. Проверить race повторной бюджетной проверки, успешный resize по ГО и отсутствие нулевых ордеров
- [x] 5.5 Устранить бюджетные ReduceTrade/CloseTrade из post-fill/over_risk/corrective веток: превышение блокирует новые входы/доборы и диагностируется, ранее принятый fill применяется. Защитные/профильные выходы, экспирация и собственный gap-entry сохраняют основания; прибыльная и убыточная позиция не вытесняются сигналом
- [x] 5.6 Проверить восстановление общего риска legacy+v2: сохранённые факторы/стопы, старые pending, unknown risk-state блокирует только новые допуски. Если нет legacy cost snapshot, явно маркировать оценку только будущих затрат в risk trace без подстановки её в старые fees/BE; проверить изменение глобального процента и отсутствие бюджетной ликвидации

## 6. BE и трендовое сопровождение

- [x] 6.1 В `profiles/rules.py` реализовать чистый BE всей сделки по G,F,q,A,V и expected remaining exit costs, с направленным округлением. Проверить независимые LONG/SHORT примеры 102.5 и BE ниже средней за счёт прибыли
- [x] 6.2 Подключить новый BE к levels_rr/pattern_targets: confirmed TP1, текущий close ≥ min_be_r×D0 по направлению, известные расходы/рынок, улучшение active stop; audit skip при неизвестности. Проверить отсутствие ретроактивной защиты, legacy routing и разрешённые выходы при невыполненном пороге
- [x] 6.3 Реализовать лестницу atr_trend и устойчивую активацию trailing/ban-adds/extreme: после первой фиксации либо причинного threshold при нулевой аллокации. Следующие цели не забирают trailing remainder; проверить малый/нечётный объём, stop priority и восстановление после активации без fill

## 7. Наблюдения, экспорт и уведомления

- [x] 7.1 Подключить durable observations в runtime on_bar/менеджере/журнале: только интервал владения, bar-id идемпотентность, bars without fill, отдельные известные fill prices, исключение экстремума после выхода; сохранить raw fill-bar данные отдельно
- [x] 7.2 Исправить направленные формулы MAE/MFE и clamp zero в `export.py`; holding-bars-v2 отличается от legacy fills-bounded. Проверить среднюю всех входов, Qmax/D0, отсутствующие factors/наблюдения и неприменимые RUB/RAW отношения
- [x] 7.3 Сохранить/вывести complete/partial/unavailable и определение экскурсии; gap и неизвестная intrabar последовательность не выдаются за complete. Проверить согласованность с отчётом/кэшем history-data-gaps без интерполяции
- [x] 7.4 Обновить `trade_summary.csv` header contract и отображение provenance/version: ₽ для рисков, R для Result/MAE/MFE, отрицательная пользовательская комиссия, статус частично закрыта, без внутренних ID и legacy-копий; оба CSV из одного read-only snapshot
- [x] 7.5 Обновить события/шаблоны: requested/selected quantity и limiting constraint, общий процент/B/лимит/open risk/pending/free/excess, gross/net и оценка versus факт, unknown payoff/risk-state и русские причины. Синхронизировать event-schemas/json-schemas, документацию профилей/журнала, `docs/trade-management/portfolio-risk.md` и CHANGELOG, включая миграцию старых ключей
- [x] 7.6 Мигрировать `tools/show_state.py` и `tests/unit/test_show_state.py` на единый portfolio_pct и B=max(0,min(balance,equity)): один согласованный read-only снимок, риск фактических остатков только по confirmed_stop с направленным clamp zero и будущими расходами, ACTIVE-резервы отдельно, свободный риск и превышение без бюджетной ликвидации. Pending-stop показывать как намерение, неизвестные стопы/факторы/суммы/ГО явно маркировать; legacy-оценку только будущих затрат отличать от фактических fees. Проверить LONG/SHORT, прибыльную защиту, partial fill, legacy+v2, уменьшение B/процента, unknown-state, короткие имена, отсутствие старых процентных ключей и побайтовую неизменность базы

## 8. Независимая проверка и фиксированный replay

- [x] 8.1 Подготовить локальные cases GMKN/VTBR/TATN/PLZL вокруг 02.10.2026: реальные 15m-свечи из Invest API (20.09–06.10.2026, охватывают рабочие факты 01–05.10) в `tests/snapshot/data/<TICKER>_15m/` с `candles.csv` и замороженными provenance-метаданными `candles_metadata.json`: источник, таймфрейм/часовой пояс, диапазон/прогрев, cutoff, sha256, шаг/стоимость шага, лот, конфигурацию расходов/порогов и версию алгоритма; метаданные не восстанавливаются текущим API во время тестов. Данные валидны: окна реальных исторических фактов, лоты/шаги совпадают с API и журналом. Whitelist `.gitignore` распространён на новые корпуса.
- [x] 8.2 В `tests/unit/` покрыть независимыми Decimal-ожиданиями нормативные сценарии: единый 2%, приоритеты 400/150→2/1, ГО 20000/8000→2, подтверждённый стоп/остаток, запрет бюджетных сокращений, B/equity, миграция ключей, unknown-state; также геометрия/цели/аллокации/экономика/BE/fees/adjustments/MAE. Свечные корпуса в unit не читать
- [x] 8.3 Добавить data-driven lifecycle replay в `tests/snapshot/`: боевой путь plan/sizing/outbox/simulator/reducer/export, partial entry/add/targets/stop, legacy/v2 migration, рестарт/replay/откат; проверить идентичность account/position/CSV и независимое правило 30 ₽ комиссии
- [x] 8.4 Через replay проверить gap через активный стоп и fill с превышением бюджета без отдельной бюджетной продажи, удержанный бар без fill, выход на open до экстремума, разрыв в пределах семи суток/ограниченный поиск. Проверить общий риск/приоритеты/resize, честную полноту и неизменность старого плана, не рост прибыли
- [x] 8.5 Обновить эталоны планов/исполнений явно через --update-snapshots и проверить diff по независимым инвариантам; стратегические эталоны/раннер выбора кейсов не меняются. Измерить размер observations и время replay на зафиксированном корпусе

## 9. Итоговая проверка и завершение change

- [x] 9.1 Выполнить `.venv/bin/python -m pytest -q` и `.venv/bin/python -m ruff check .`; сопоставить все requirement/scenario с проверкой и записать результаты. Допустимые прежние lint-ошибки в tools/ и openspec/_check_sync.py обозначить отдельно
- [x] 9.2 Выполнить `openspec validate trade-economics-and-exit-geometry --strict`, `openspec validate --specs` и диагностическую `openspec validate --specs --strict`; повторить семантическую сверку активных changes/baseline и сохранение сценариев. Старые MUST/SHALL WARNING из design зафиксировать отдельно; новые ERROR/WARNING в delta и изменённых блоках не допускаются
- [x] 9.3 После собственных проверок архивировать predecessor до target sync; после подтверждённой реализации синхронизировать целевые delta-specs и архивировать target. Согласовать Purpose основных risk/config specs и пользовательскую документацию с единым бюджетом; старую delta повторно не накладывать. Planning-complete не является выполнением checklist
