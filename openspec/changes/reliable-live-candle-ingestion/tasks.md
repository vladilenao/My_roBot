Все пункты ниже — **будущая реализация MVP после отдельного запроса apply**. Текущая согласованная ревизия изменяет только девять артефактов этого change: исходники/рабочие конфиги/тесты/БД не изменяются, apply/тесты/API-probe/миграции/торговые процессы не запускаются. Группы идут по зависимости; каждый пункт проверяется отдельным fake/offline сценарием до отметки выполнения. Числовые профили design — настраиваемые инженерные гипотезы; проверки используют effective config. Исправление `_manage`/`_calculate` не является задачей этого change.

### Статус отдельного apply 2026-10-12

Оговорка выше описывает завершённую документационную ревизию. Последующий отдельно запрошенный apply реализовал отмеченные ниже primitives/контракты; проверка затронутых API/data/clock областей: **294 unit-теста прошли**. Stream ещё не подключён в run.py, rollout и broker probe не выполнялись. Пункты 2.5/2.6 не отмечены: раздельные store-границы и pin primitives готовы, но вычисление всего coverage-prefix и финансовый resume ещё не интегрированы.

**Расширение границ задачи 6.3 согласовано владельцем:** общий outbox получает минимальный eligibility **до claim/delivery**, зависящий от типа/причины/подтверждённых фактов и актуальности, не только UID hold. Новые задачи 6.8–6.11 и design9a фиксируют классификацию, атомарность, сохранение защитных/подтверждённых путей и восемь обязательных тестов. Схема БД, расчёты риска/исполнения и `_manage/_calculate` не меняются. Выявлены существующее отсутствие pre-ACK TTL и особый revision/REJECTED переход slippage guard: не вводить новые бизнес-правила для их «исправления»; при невозможности доказать безопасный допуск — удержание с причиной или отдельное решение владельца.

**Последующий apply расширения:** добавлены финансовые guard/eligibility primitives в dispatch/claim без изменения схемы; восемь сценариев и дополнительные race/restart/fairness/evidence проверки находятся в `tests/unit/trade_management/test_dispatch_eligibility.py`. После отдельного разрешения владельца реализован узкий recovery: только доказанный pending `gap-entry` CloseTrade N для REJECTED snapshot N+1 с живым остатком получает one-shot marker JournalBroker; payload/command_id/TTL/схема не изменяются, а общий active recovery REJECTED не расширен. Runtime binding к будущему computed market evidence остаётся частью дальнейшей ingestion-интеграции, но обязательный port/guard и recovery-проверки финансового контура завершены.

### Статус apply 2026-10-12, завершение MVP

Реализованы все задачи, кроме 8.5 (read-only проба брокера — только после отдельного разрешения владельца). Live-кэш стал единственным владельцем входа для обоих транспортов: торговый поток только читает, сеть выполняют фоновые полосы `IngestionWorker` (unary) и `StreamSupervisor` (stream) под `LiveMarketDataPoller`. Проверка: весь набор `tests/` (unit, snapshot, integration) — 2422 теста прошли; эталоны не обновлялись.

Исправлены дефекты ранее отмеченных пунктов, найденные при интеграции: исторический клиент явно помечает свечи эмулятора завершёнными (иначе ломался исторический режим после 2.2); пакетное принятие страницы одной транзакцией и инкрементальный префикс вместо O(n) SQL на каждую свечу; префикс учитывает календарные перерывы (иначе барьер не открывался после первой ночи); нижняя граница покрытия не поднимается через недоказанный участок.

Ограничения: модель RAM буфера — фиксированная оценка на событие; реальные объём событий, пики коррекций, задержка публикации и квоты измеряются в 8.5, после чего подбирается провизорный профиль. Порядок `_calculate`/`_manage` не менялся (отдельная проблема).

## 1. MVP: контракт текущего SDK и совместимости

- [x] 1.1 Добавить offline contract tests установленного SDK: именованные SubscriptionInterval для всех нужных TIMEFRAMES, Candle без is_complete, HistoricCandle с is_complete, source-поля разных имён, ACK/oneof/presence и сериализация SubscribeCandlesRequest.
- [x] 1.2 Закрепить явную RPC↔stream таблицу нативных интервалов без неполного SDK helper; проверить 1m/15m/1h и зависимости 30m/4h/1d/1w, неподдерживаемый interval не агрегируется молча.
- [x] 1.3 Зафиксировать одинаковый совместимый source-profile UNSPECIFIED, expected/actual source и нативные UTC/session/month границы; при несовместимости SDK/сервера потребовать update change до расширения scope.
- [x] 1.4 Сохранить baseline polling/history fixtures и определить contract acceptance для optional stream-port, не обязывающего HistoricalClient к stream и не создающего exchange-executor.
- [x] 1.5 Проверить расчёт roster/нагрузки из design: cascade tf, dedup binding pairs, base1m/HTF/сохранённые обязательства; all-config fixture без дополнительных UID даёт81. Разделить число пар и control requests, проверить предпосылки оценок16,2RPC/мин и690RPC, не использовать их как гарантию quota/времени загрузки.

## 2. MVP: единственный owner и рыночные факты

- [x] 2.1 Расширить существующие data/cache/poller одним сериализованным accept/normalize/reconcile/publish путём для stream и unary; network/индикаторы вне lock, никакого вызова trade_manager/reducer из reader.
- [x] 2.2 Добавить stream-normalizer закрытия по ACK epoch/waiting_close/UID/TF/profile/grid, historical-normalizer по is_complete; проверить ранний/пустой пакет, UTC+03 vs Z, last_trade_ts не close и запрет default-True/closed downgrade.
- [x] 2.3 Переиспользовать MarketDataStore версии и PK; добавить минимальные market-only provenance/epoch/range/conflict/incident факты, не менять trades.sqlite3/CSV/outbox и не ломать producer-id/export version contract.
- [x] 2.4 Реализовать canonical dedup и deterministic overlap/conflict/recheck: одинаковый ключ не вызывает второй тик, старый history snapshot не понижает новую согласованную версию буфера.
- [x] 2.5 Разделить received high-water, computed contiguous coverage, analysis и external processed frontiers; meaningful lower учитывает existing исполнение/прогрев, не только latest timestamp.
- [x] 2.6 Добавить freeze/pin revision refs начатого пакета и сохранение consumed-input reference в market-state; проверить correction во время обработки и crash между financial commit и market-marker без replay новой версии.

- [x] 2.7 По итогам пробы 8.5: запрос TradingSchedules не раньше текущей даты; решение владельца «б» — для пары без сохранённых обязательств непрерывность от cutover запуска, неподтверждённые пробелы прогрева до него видны в диагностике и не блокируют расчёт; строгий 1m-префикс от границы исполнения сохранённых обязательств (design 7b).

## 3. MVP: stream-session и жизненный цикл

- [x] 3.1 Добавить узкую API stream-session factory на текущем Client/Services с bounded request iterator, owned lifetime/cancel/close и explicit model family; новые модули обосновывать отсутствующим контрактом и преимущественно расширять существующие. Не вводить новую версию SDK без результатов пункта1.1/согласования.
- [x] 3.2 Расширить существующий ingestion supervisor reader/maintenance lanes, desired/sent/acknowledged/failed states и generation tag; подтвердить каждую пару, частичный ACK и invalid interval/source/UID.
- [x] 3.3 Добавить server-ping/last-response health и GetMySubscriptions consistency с настраиваемой cadence/проверкой по сомнению; предварительные ping15с/health45с/ACK30с не фиксировать в assertions как неизменные. Quiet session не является network outage, короткий unary deadline не ограничивает stream lifetime.
- [x] 3.4 Реализовать настраиваемый transient exponential reconnect (предварительно1→30с) с jitter/reset, resubscribe и generation fence; auth/permanent errors, quota и exhausted roster не повторяются бесконечно быстро, batching сохраняет per-pair ACK.
- [x] 3.5 Добавить настраиваемые control/input/startup event/byte caps из design, overflow loss-range/hold и независимый control/stop путь; проверить заполнение истории/буфера и принудительную отмену blocking reader без будущей сделки. Проверить модель объектного RAM/correction bursts и cold-start backlog; 50000/64MiB — предварительный профиль, не достаточность по умолчанию.

## 4. MVP: запуск и handoff без пропущенного окна

- [x] 4.1 В run.py::_launch собрать owner, UID roster/roles и stream-session в lifecycle start/stop; network startup принадлежит ingestion, TradingBot получает только read/readiness интерфейсы.
- [x] 4.2 Выполнить последовательность subscribe/ACK → bounded buffer/durable observations → historical overlap до фиксированного H → reconciliation → per-pair READY; проверить свечу на history/stream стыке и событие до ACK.
- [x] 4.3 Обеспечить warmup/read-only обзор без trading replay pre-cutover, с отдельной recovery lower для сохранённых позиций/pending-orders и зависимыми HTF по binding_requirements.
- [x] 4.4 Проверить ошибку истории, reconnect/overflow во время старта, задержанный ACK и рестарт: READY не возникает по непустому cached frame, durable chunks/checkpoints сохраняются.

## 5. MVP: полная последовательность и GetCandles recovery

- [x] 5.1 Расширить reconciliation expected sequence/lifecycle/calendar проверкой всего необходимого префикса и trailing gap без следующей свечи; новый максимальный timestamp не скрывает внутреннюю/хвостовую дыру.
- [x] 5.2 Реализовать persistent outage ranges и fair recovery от contiguous/исполнительной lower до fixed H, объединяя соседние gaps; тесты5мин и3часа подтверждают восстановление полного разрыва без now−1h отсечения.
- [x] 5.3 Добавить явные overlap chunks/limit/profile/native grid и локальный [start,end) filter, conservative SDK/API span table; проверить обе включённости границ, limit-обрезку и 30-дневную1m по допустимым RPC.
- [x] 5.4 Учесть в shared service/account/IP budget каждую страницу, включая pace=False, shared retry-after и частные quota; чекпоинт каждого успешного чанка переживает сбой третьего.
- [x] 5.5 Добавить настраиваемые bounded retries transient/RPC reset, gap states/durable cooldown/parked unresolved; пустота не становится no-trade или окончательной потерей, быстрый бесконечный цикл не допускается. Publication deadline первого необходимого missing интервала не двигается новым high-water/опросом; delayed не означает no-trade/loss, known disconnect/overflow начинает range recovery сразу.
- [x] 5.6 Проверить применимый calendar/no-trade evidence/unknown schedule/последний интервал завершённой сессии; scheduled_closed/доказанный no-trade не требуют OHLCV, неподтверждённая пустота остаётся hold.
- [x] 5.7 Добавить недорогую periodic recent reconciliation с настраиваемыми cadence/window (предварительно300с/2 native закрытых бара) по budget; распределить запросы без burst всего roster. Late corrections/свежие события продолжают приниматься независимо от старого recovery.
- [x] 5.8 Добавить настраиваемый конечный активный recovery-раунд (предварительно900с), отдельный предел попыток и parked recheck cadence: от начала раунда с учётом quota/backoff без скользящего продления; после срока/попыток прекращаются новые интенсивные запросы, gap остаётся unresolved. Сохранить round start/attempts/checkpoints/next_retry при restart; проверить исчерпание срока до числа попыток, shared reset, одну bounded parked проверку и позднее реальное восстановление, не обещать максимальный возраст gap.

## 6. MVP: readiness и причинная торговая обработка

- [x] 6.1 Подключить data-ready notify/condition и calendar/deadline wakeup к MultiTimeframeScheduler без изменения UTC grids/virtual-clock; heartbeat/диагностика не создают тик отсутствующего бара.
- [x] 6.2 Сделать live frame_for/HTF-read на торговом тике read-only; недостающие кадры регистрируются ingestion и удерживают readiness, не инициируют network/warmup синхронно из compute/filter/manage.
- [x] 6.3 Установить critical1m per-UID barrier для зависимых минут/новых решений/индикаторного сопровождения, не используя общий post_tick=True как доказательство продвижения; сохраняются sequential on_bar/commit, order activation, STOP/TP и общий risk unknown-state. Проверить сохранность позиций/ордеров/защиты, existing независимый идемпотентный путь подтверждений/pinned pending commit и healthy UID без глобальной остановки; не создавать новый bypass, synthetic OHLC или emergency-close по timeout.
- [x] 6.4 Сохранить baseline действующих consumer phases при интеграции транспорта: не переставлять `_manage`/`_calculate`, не менять формулы/priority/управление/риск. Проверить применение пригодной защиты до допуска входов и отсутствие transport-driven reordering; явно оставить несовпадение с ARCH/OpenSpec отдельной проблемой, не объявлять его исправленным.
- [x] 6.5 Закрепить bar_time/receipt/publication/input_ready_at/effective_available_at и raw origin; поздняя15m не исполняется до пригодного входа, старый pre-activation сигнал не становится новым при позднем receipt.
- [x] 6.6 Проверить exactly-once локального ключа своевременной1m: один logical processing и одна защитная передача; duplicate/out-of-order не удваивают сигналы, стопы, тейки или расходы.
- [x] 6.7 Реализовать policy market corrections: unconsumed conflict recheck, будущий пересчёт15m/1h без старого действия, сохраняемый integrity-hold для исполненной1m без rollback/компенсирующего ордера/автоматического снятия reconnect/restart. Первое late поступление отличается от correction; для рабочих ТФ сохраняются existing current/latest/session/CATCH_UP_BARS/activation/risk, слишком старый бар — только исторический контекст.
- [x] 6.8 Закрепить классификацию design9a для Open/Add, обычного сопровождения, защитных Close/Reduce, Cancel и technical/recovery: тип+причина+durable origin, не blanket UID/строковый whitelist. Подтвердить независимый guard-close по финансовому факту и консервативное удержание немедленных cancel/target-reduce при неизвестном execution-prefix.
- [x] 6.9 Добавить минимальный обязательный eligibility-порт dispatch/claim_outbox: атомарная проверка до CLAIMED, согласованный data guard/fence до отправки, здоровые команды за удержанными, отсутствие обхода через прямой dispatch/lifecycle. Сохранить PENDING/резервы/command_id, CLAIMED recovery и обработку подтверждённых событий; не менять финансовую схему/исполнение.
- [x] 6.10 Перед доставкой после recovery проверять существующие revision/phase/order status/применимый TTL и исходную актуальность; учесть доказанный revision/REJECTED переход существующего gap-entry guard. Непроверяемое старое намерение не отправлять и не выдавать новый TTL от enqueue за existing правило; диагностировать/удерживать либо остановиться для согласования бизнес-изменения.
- [x] 6.11 Добавить восемь отдельных offline-тестов eligibility: A hold/B allowed, A остаётся PENDING, direct dispatch не обходит, независимая защита по доказанной политике, stale после recovery не отправлен, duplicate dispatch без повторного исполнения, cancel не меняет применимость входа к прошлым минутам, подтверждённые events проходят при hold. Добавить race/restart/limit fairness cases и релевантные existing финансовые регрессии, без реальных ордеров/рабочих миграций.

## 7. MVP: режимы, членство и откат

- [x] 7.1 Добавить optional live_transport/polling-default, stream_fallback_polling и самостоятельные настраиваемые caps/квоты/временные параметры из design с валидацией; старые конфиги/стратегии/профили/денежные настройки сохраняются. Значения гипотез не выдавать за измеренные defaults/SLA; effective profile диагностируется.
- [x] 7.2 Реализовать per-pair roster по UID, selected work/HTF/always1m и immutable lifecycle/ledger потребностям main-thread; reader не запрашивает финансовую SQLite из другой thread.
- [x] 7.3 Проверить old UID DRAINING с позицией/pending и new UID WAITING_DATA: старая1m остаётся до terminal obligations, новый warmup не двигает исполнение/activation/cutoff и не вводит auto-live rollover.
- [x] 7.4 Проверить одинаковые ticker разныхUID и legacy financial alias: data не смешиваются, неоднозначность даёт hold, старые trade IDs/CSV не переписываются.
- [x] 7.5 Реализовать announced fallback/rollback через того же owner и handoff; unavailable stream/unsupported pair/over-capacity не обнуляют gaps/cursors/incidents, возвращение stream снова подтверждается.
- [x] 7.6 Проверить live simulator и NotifyOnly, polling disable-stream и history/virtual/snapshot режимы; исторический запуск и 30-дневная загрузка не требуют live socket/токена stream и не меняют результаты backtest.
- [x] 7.7 Разделить существующие использования tick.timeout_secs согласно design7a: отдельный market_data.candle_publication_timeout_secs, round/recheck/backoff/reconciliation/subscription/status настройки без автоматического наследования от tick; legacy scheduler timeout/virtual-clock не менять. Проверить изменение только tick, отдельного publication и parked cadence; 65/300с не окончательные publication defaults, deadline не расширяет актуальность торгового решения.

## 8. MVP: приёмка, диагностика и будущий rollout

- [x] 8.1 Добавить независимо проверяемые десять сценариев MVP Acceptance Matrix design: normal stream, late candle, unary-only recovery, 5мин/3часа disconnect, duplicate, correction after decision/commit, no-trade, intraday restart, multiple UID/TF. Сквозной startup/overlap/internal/trailing-gap сценарий дополняет их, не заменяет; assertions на effective config/версии/frontiers, сохранность existing state/независимых подтверждений и отсутствие лишних торговых действий.
- [x] 8.2 Добавить fault tests buffer overflow, forgotten subscription, invalid source/interval/auth/quota, stale generation, blocked read-stop, correction during commit и restart after financial commit; проверить все обязательные scenarios delta-specs.
- [x] 8.3 Публиковать existing log/market_data_status/error/heartbeat diagnostics из design: ACK/generation, profile, timestamps, frontier/gap/retry, buffer/loss, correction/incident/fallback; короткие имена контрактов и отсутствие секретов.
- [x] 8.4 Запустить при реализации релевантные unit/integration и existing snapshot/backtest регрессии без обновления эталонов ради транспортной фичи; tests только в разрешённых подкаталогах, текущий этап propose их не запускает.
- [ ] 8.5 (заход 1 выполнен 2026-10-12: ACK, границы, источники, холодный старт, расписание — см. research.md; осталось: заход 2 в основную сессию) После отдельного разрешения владельца перед stream-rollout выполнить контролируемую read-only broker compatibility проверку ACK нужных UID/ТФ, source-equivalence/edge semantics и доступных квот; измерить publication/health/ACK latency, cold-start/RAM/correction peak и сверить provisional профиль с допустимой актуальностью решений. Зафиксировать наблюдения/ограничения без реальных ордеров. При несовместимости обновить план, не обещать исправленную причину сегодняшних пропусков.
- [x] 8.6 Подготовить рабочую архитектурную/операционную документацию на этапе реализации: различие live-data/simulation/real-executor, per-dependency hold/сохранение состояния, opt-in/fallback/rollback, separate publication/recovery/status и provisional numeric profile, fixed snapshot для replay; `_manage`/`_calculate` явно остаётся отдельной проблемой. Не менять рабочую документацию в данной ревизии.

## 9. Отложенные улучшения — отдельные будущие changes, не критерии MVP

Следующие пункты не являются незавершёнными обязательными задачами этого change и не разрешают расширять scope apply молча:

- Единая агрегация всех ТФ из1m — только после отдельного доказательства эквивалентности native/calendar/no-trade/correction семантики.
- Новый полный raw archive/version-selection продукт и автоматический контрфактический перерасчёт денежных live-результатов — отдельно от сохранения существующих candle_versions/input refs.
- Дополнительные вендоры, Finam/MOEX ISS, реальные брокерские ордера и автоматический live-rollover — отдельные архитектурные изменения.
- UI/автоматическая разблокировка integrity-hold, sharding множества streams и адаптивные quota/buffer политики — после эксплуатационных измерений; MVP не снимает hold молча.
- Исправление предсуществующего порядка `_manage`/`_calculate` и независимая реорганизация календарного/TTL сопровождения — отдельные изменения торгового consumer flow, не транспортный MVP.
