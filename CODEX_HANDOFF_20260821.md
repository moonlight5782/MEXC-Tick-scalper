# MEXC Tick Scalper — полный handoff для Codex

Дата среза: 2026-08-21

## 0. Ссылки и источники истины

- Репозиторий: https://github.com/moonlight5782/MEXC-Tick-scalper
- Git clone: https://github.com/moonlight5782/MEXC-Tick-scalper.git
- Активная ветка: https://github.com/moonlight5782/MEXC-Tick-scalper/tree/persistent-end2end-latency-v1
- Текущий рабочий handoff: `CODEX_HANDOFF_20260821.md`
- Предыдущий подробный технический handoff: `DEVELOPER_HANDOFF_20260816.md`
- История исходного ChatGPT-чата: `<PASTE ORIGINAL CHAT URL HERE>`

Важно: оригинальный URL старого ChatGPT-чата владелец проекта ранее передавал, но в текущем доступном контексте точная ссылка не восстановилась. Не удалять этот пункт: после вставки ссылки чат является дополнительным источником истории решений. Код репозитория и этот handoff остаются основным source of truth по текущему состоянию.

---

## 1. Главная идея проекта

Это низколатентный направленный lead-lag скальпер Binance -> MEXC Futures.

Идея НЕ является классическим двухсторонним безрисковым арбитражем. Binance используется как более быстрый источник информации/лидирующего движения. На MEXC открывается одна направленная позиция в сторону ожидаемого догоняющего движения MEXC после Binance.

В текущем рабочем контуре:

1. Binance LIVE public market data — источник лидирующего импульса/состояния.
2. MEXC LIVE public market data — источник фактического состояния MEXC, residual/microspread, исполнимого стакана и spread.
3. MEXC Futures Demo/Testnet — единственное место, куда разрешено отправлять реальные ордера.
4. PRIVATE LIVE MEXC writes запрещены.

Цель текущего этапа НЕ менять стратегию. Цель — сохранить достигнутую торговую механику и результаты, одновременно разрезав монолитный код на независимые блоки, чтобы изменение одной функции не требовало подтягивать и переписывать весь бот.

Формулировка владельца проекта, которую нужно считать архитектурным требованием:

> Не переписывать бота. Разделить существующего бота на независимые блоки так, чтобы ради одной функции не тянуло всё остальное, сохранив 1:1 текущее поведение и тот же способ запуска.

---

## 2. Что нельзя менять во время текущего рефакторинга

Ниже frozen behavior. Любое изменение этих пунктов — уже изменение стратегии/продукта, а не рефакторинг. Без отдельного явного решения владельца НЕ менять.

### Запуск и конфигурация

- Бот запускается локально, как раньше.
- Используется локальный `.env`.
- Не переносить Testnet execution в GitHub Actions.
- Не требовать новых secrets/workflow для обычного запуска.
- Тот же launcher/CLI должен остаться рабочим.
- `.env` не коммитить.

### Safety boundary

- MEXC LIVE private writes запрещены.
- Testnet/Demo writes разрешены только через Demo конфигурацию.
- Demo host должен оставаться жёстко проверяемым.
- Для Testnet используется `MEXC_DEMO_WEB_TOKEN`.
- `MEXC_DEMO_WRITE=YES` является явным локальным unlock для Demo writes.
- LIVE private auth не должен требоваться для Testnet execution.

### Strategy / entry invariants

- Target notional: 10,000 USDT.
- Requested leverage intent: 200x, но фактическое leverage ограничивается возможностями LIVE/Testnet контракта.
- Строгий trading entry:
  - absolute residual >= 8 bps;
  - signal strength >= 3x.
- Discovery может быть шире (примерно 2 bps / 1.5x), но discovery никогда не имеет права ослаблять реальный trading entry.
- Residual retention reference: 60%.
- Impulse retention reference: 75%.
- IOC cross <= 1 bp в текущем structured Testnet контуре.
- Max actual entry slippage <= 1 bp.
- Min actual filled notional = 50 USDT.
- Executable edge должен быть >= 2 bps и >= 1.5x roundtrip cost.
- Одна IOC попытка.
- Partial fill принимается.
- Остаток не догоняется.
- Нет top-up.
- Нет chase.
- Нет pyramiding.
- Нет martingale.
- Нет averaging down.
- Rearm behavior не менять.

### Risk invariants

- Logical start bank = 100 USDT.
- Equity reserve >= 20%.
- Max session drawdown = 60% от стартового logical bank.
- Одна позиция одновременно.

### Latency invariants

- В Trading Mode нет искусственной задержки.
- Не добавлять `sleep()` в critical entry/exit path.
- Подтверждённый fill сразу начинает position management.
- После fill не требуется обязательный дополнительный `get_positions()` перед началом управления.
- Hard adverse exit должен приниматься по LIVE состоянию ДО потенциально медленного Demo REST price lookup.
- Close сначала отправляется, затем выполняется reconciliation остаточной позиции.

### Post-fill guard

После фактического fill позиция немедленно перепроверяется по свежему LIVE состоянию. Если alpha уже исчезла, reversed, snapshot invalid/stale, actual fill слишком мал или slippage недопустим — позиция немедленно закрывается.

Не ослаблять этот guard и не откладывать его ради «дать сделке шанс».

### Profit Hold

- Profit Hold активируется на первом положительном executable PnL.
- После arm обычные thesis exits подавляются.
- Hard emergency/safety exit остаётся активным всегда.
- Positive trailing stop продолжает управлять прибыльной позицией.
- Не возвращать старый `profit-runner-arm-bps` threshold.

Текущая ratchet-механика `ProfitHoldPolicy`:

- first positive executable PnL -> armed;
- начальный positive floor = `min(0.10 bps, move * 0.5)`;
- peak >= 3 bps -> stop минимум +0.5 bps;
- peak >= 5 bps -> stop минимум +2.0 bps;
- peak >= 6 bps -> stop = max(previous stop, peak - max(0.1, distance_bps)).

### Exit priority / semantics

Исторически и в текущей policy должны сохраняться причины:

1. `mid_adverse_cut`
2. `leader_retrace`
3. `residual_reversal`
4. `mexc_catchup_convergence`
5. `no_progress`
6. `positive_trailing_stop`
7. `timeout`

Текущая structured Testnet safety policy использует очень быстрый `mid_adverse_cut` с `emergency_mid_adverse_bps = 0.01` и без искусственного minimum hold.

---

## 3. История идеи и эволюция бота

### 3.1 Реконструкция старого торгового бота

Проект начинался с восстановления поведения старого MEXC futures бота по истории ордеров.

Из фактической истории наиболее вероятно восстановлена execution-механика:

1. отправить относительно большой IOC LIMIT;
2. принять доступный partial fill;
3. IOC автоматически отменяет незаполненный остаток;
4. не докупать остаток market order;
5. управлять только реально заполненным qty;
6. закрывать reduce-only.

Исторические ориентиры из предыдущего handoff:

- UNIUSDT: около 685 входов, около 200x, requested notional около 20k USDT, median fill ~7.65%, PF ~1.45, итог ~+121 USDT.
- BCHUSDT: около 456 входов, около 195x, requested notional около 10k USDT, median fill ~42.6%, PF ~1.89, итог ~+108 USDT.
- Победители обычно держались дольше проигравших: примерно 9.5–10 sec против 2–5 sec.

Эта история хорошо подтверждает execution pattern/sizing, но НЕ доказывает точный старый сигнал.

### 3.2 Ранний Binance impulse кандидат

Был построен кандидат `binance-impulse-zero-fee-gross-v1`, где вход определялся быстрым движением Binance, а MEXC использовался как место исполнения и экономической проверки.

Этот этап дал важные доказательства существования краткоживущего edge, но позже архитектура/сигнал эволюционировали в более явную Binance/MEXC lead-lag microspread/residual модель.

Не путать ранний impulse-кандидат с текущей structured Testnet стратегией.

### 3.3 Текущий lead-lag / microspread контур

Текущий structured Testnet engine использует:

- `MicroSpreadModel` для Binance/MEXC basis/residual;
- `LeadLagGate` для signal readiness/threshold/noise/strength;
- строгие реальные entry filters 8 bps / 3x;
- LIVE Binance + LIVE MEXC public data;
- Demo/Testnet execution;
- фактическую Demo best price и IOC;
- immediate post-fill guard;
- `PositionManager` для lifecycle после fill;
- `ProfitHoldPolicy` + `TestnetExitPolicy` для выхода;
- `TradeReporter` для timing/PnL/fees telemetry.

---

## 4. Достигнутые результаты — НЕ потерять

Результаты ниже относятся к разным поколениям контура. Их нельзя смешивать в один статистический вывод, но нельзя и забывать: они объясняют, почему проект продолжался.

### 4.1 Demo/Testnet gross candidate

На раннем замороженном кандидате (91 штатный exit + 1 reconciled emergency exit):

- 92 позиции;
- zero-fee Demo gross counterfactual: +232.3418 USDT;
- Testnet fees: 367.9385 USDT;
- фактический Demo net: -135.5967 USDT;
- gross W/L/F: 68/20/4;
- non-flat gross WR: ~77.27%;
- gross PF: ~4.89;
- median / p95 hold: ~5.37s / 60.00s;
- median / p95 IOC confirmation: ~642.9ms / 1766.6ms;
- median / p95 signal -> position visible: ~1041.6ms / 5049.5ms.

Вывод: edge был положительный gross, но Testnet fee schedule делал Demo net отрицательным. Это НЕ доказательство live profitability; это доказательство того, что стратегия имеет смысл исследовать на подходящих fee conditions и что latency критична.

### 4.2 LIVE read-only zero-delay shadow — 100 сделок

Файл исследования: `binance_impulse_live_shadow_instant_100_20260815.csv` (generated artifact, мог не быть закоммичен).

Результат:

- 100 закрытых shadow сделок;
- PnL при fee=0: +173.2098 USDT;
- W/L/F: 79/21/0;
- WR: 79.00%;
- PF: 4.126;
- median / p95 hold: 1.130s / 27.698s;
- median / p95 event-loop signal -> fill: 8ms / 15ms;
- LINK дал 96/100 сделок и около +169.7840 USDT.

Вывод: краткоживущий edge реально наблюдался в LIVE публичном market data сразу после сигнала. Главная проблема — успевает ли private order gateway.

### 4.3 Почему latency стала центральной темой

Demo private execution измерялась значительно медленнее, чем event-loop/public data. Поэтому проект ушёл от наивного «сигнал положительный -> значит будет прибыль» к измерению полного end-to-end пути:

- signal time;
- IOC submit time;
- IOC confirmation;
- management start;
- exit decision;
- exit submit;
- exit fill confirmation;
- residual reconciliation.

Текущий reporting обязан различать:

- GROSS;
- DEMO_FEES;
- DEMO_NET;
- signal-to-submit;
- submit-to-fill;
- signal-to-fill;
- fill-to-management;
- exit-decision-to-submit;
- exit-submit-to-fill;
- exit reconciliation.

---

## 5. Важные исторические коммиты

Не обязательно checkout каждого, но знать назначение.

### Shadow / baseline

- `372c3b286eb82aa4b87d806999f8db47173a2b3e` — `Test exact 100-trade paper wrapper`.
  Исторически известный good 100-trade paper/shadow checkpoint.

- `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5` — frozen successful shadow baseline referenced during current refactor. Не заменять его смысл новым кодом без проверки.

### Прямой Testnet baseline / Demo execution evolution

История содержит этапы:

- `24f0415...` — Run frozen baseline v1 directly against MEXC Testnet
- `e9a4ecf...` — Launch frozen baseline v1 with direct Testnet execution
- `0f8791b...` — Test direct Testnet baseline v1 adapter helpers
- `3a2b126...` — Restore same-symbol Binance/MEXC Testnet Demo trading flow
- `d190af8...` — Launch same-symbol automatic Testnet trading

Эти коммиты полезны для forensic comparison, если текущая модульная версия начинает вести себя иначе.

### Current modularization sequence

Ключевые последние изменения текущей ветки:

- `e2d984f811fb1ff5b1f6be7deed8659dabf943c7` — Switch TradingSession to modular Testnet engine
- `100061f36928f6990fade0a3fa631a30a2ec7051` — Inject explicit Testnet read and trading dependencies
- `cbdf44652da4e8bdbecff4b9c7dbcb2ae5e3f150` — Launch modular Testnet engine without obsolete Profit Hold threshold
- `4a8aca3635441b84e71d3a255b5e226ea28227cb` — Strengthen modular Testnet architecture guards
- `85ef91f5700185055e73d3498b75e4e418a5a2da` — Delegate Testnet position lifecycle to PositionManager
- `0e8e36b6e4fa214bab3248383b396aae01b65661` — Guard PositionManager architecture boundary
- `627062147036dd20e70552a6b9137069f34955dd` — Remove out-of-scope Testnet workflow
- `6dac76cec87ea5c6d922d12a9d6f62eeca1d4875` — Characterize Testnet position lifecycle behavior

На момент создания этого handoff рабочая ветка должна быть не старее `6dac76c`.

---

## 6. Текущая архитектура

Целевая структура:

`Configuration`
  -> `UniverseService`
  -> `LeadLagScanner`
  -> `PairSelector`
  -> `TradingSession`
      -> `SignalEngine`
      -> `EntryPolicy`
      -> `ExecutionAdapter`
      -> `PositionManager`
          -> `ExitPolicy`
          -> `ProfitHoldPolicy`
          -> `RiskManager`/state where appropriate
          -> `Reporter`

Не обязательно слепо следовать названиям классов; важны dependency boundaries.

### Уже разделено

`src/mexc_tick_scalper/testnet/config.py`
- единственная bootstrap-точка `.env` для structured Testnet app;
- создаёт readonly и trading execution dependencies;
- жёстко валидирует Demo write environment.

`testnet/universe.py`
- строит public cross-listed universe;
- получает execution config через dependency injection;
- не должен сам грузить `.env`;
- не должен знать LIVE private auth.

`testnet/scanner.py`
- discovery / lead-lag scanning;
- не должен отправлять ордера.

`testnet/selector.py`
- выбор fee scope / пары;
- console-only selection;
- без сетевых/торговых side effects.

`testnet/session.py`
- orchestration boundary;
- создаёт structured `TestnetTradingEngine`;
- legacy monkeypatch bridge удалён.

`testnet/execution.py`
- Testnet-specific execution adapter;
- Demo only;
- fast order-result polling без software sleep;
- `position_from_fill()` начинает management сразу из confirmed fill;
- close-first, then reconcile residual state.

`testnet/risk.py`
- logical bank, leverage, sizing, Demo IOC price rounding.

`testnet/exit_policy.py`
- pure-ish exit decision based on `ExitContext` + args + ProfitHold state.

`testnet/profit_hold.py`
- winner-state / positive trailing ratchet.

`testnet/reporting.py`
- stats + CSV telemetry + gross/fees/net.

`testnet/position_manager.py`
- НОВЫЙ вынесенный lifecycle block;
- `ActivePosition` теперь живёт здесь;
- owns management from confirmed fill to terminal close;
- НЕ должен заниматься discovery, pair selection, entry sizing или `open_ioc`.

### Осталось слишком связанным

`testnet/trading_engine.py` всё ещё содержит большой `_try_open()` и orchestration loop.

В `_try_open()` сейчас смешаны:

1. snapshot validation;
2. LeadLagGate decision;
3. signal strength/absolute residual gate;
4. TradeSignal creation;
5. arrival economics;
6. risk sizing;
7. virtual IOC / depth economics;
8. executable edge/cost checks;
9. Demo best-price lookup;
10. Demo limit normalization;
11. actual `open_ioc()`;
12. confirmed-fill -> PositionSnapshot;
13. timing/stat updates;
14. `ActivePosition` construction;
15. immediate post-fill guard;
16. emergency close via PositionManager if guard fails.

Это следующий главный кандидат на разделение.

---

## 7. PositionManager — что уже сделано и зафиксировано

Коммит `85ef91f...` механически вынес из `TestnetTradingEngine`:

- `ActivePosition`;
- `_manage_position()`;
- `_close_position()`.

Вместо старых методов engine вызывает:

- `self.position_manager.manage_position(...)`
- `self.position_manager.close_position(...)`

Важно: перенос задумывался как behavior-preserving. Порядок операций не менять.

### Architecture guards

`tests/test_testnet_architecture.py` теперь проверяет:

- structured Testnet modules не импортируют legacy execution bridges;
- TestnetApp получает readonly и trading execution отдельно;
- TradingSession не делает legacy global monkeypatch;
- universe не загружает env сам;
- scanner не имеет execution/open_ioc;
- TradingEngine делегирует lifecycle в PositionManager;
- PositionManager не содержит discovery/entry submission;
- obsolete `--profit-runner-arm-bps` отсутствует.

### Position lifecycle regression tests

Коммит `6dac76c...` добавил `tests/test_position_manager_regression.py`.

Он фиксирует минимум три critical invariants:

1. `mid_adverse_cut` закрывает позицию до Demo quote lookup (`best_price_calls == 0`).
2. Первый positive executable PnL включает Profit Hold, но сам по себе не форсирует close.
3. `close_position()` передаёт reporter точный reason, fill confirmation time, reconciliation time и close attempts.

Эти тесты — защита следующего рефакторинга, а не новая стратегия.

---

## 8. Текущий локальный запуск

НЕ заменять новым способом.

Пользователь запускает бота локально на Windows с существующим `.env`.

Проект имеет console entry point:

```text
mexc-testnet = mexc_tick_scalper.testnet_app:main
```

Есть локальный launcher `start_auto_testnet.bat` (проверить актуальное имя/содержимое перед изменениями).

Ожидаемый flow для пользователя должен оставаться прежним:

1. локальный `.env` содержит Testnet token/config;
2. запускается существующий launcher/CLI;
3. app строит universe;
4. discovery;
5. selection;
6. trading session;
7. LIVE Binance/MEXC alpha + MEXC Demo execution;
8. CSV telemetry.

Не добавлять обязательные GitHub Actions, cloud secret store или новый UI в рамках текущего refactor.

---

## 9. Тестирование

Перед каждым extraction:

1. Сначала characterise существующее поведение тестами.
2. Вынести ровно одну ответственность.
3. Прогнать regression suite.
4. Только если behavior unchanged — переходить к следующему блоку.

Рекомендуемый локальный тест:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .codex-test-tmp -p no:cacheprovider
```

или при активированном venv:

```powershell
pytest -q
```

После unit/regression тестов пользователь сам делает реальный локальный Testnet run своим `.env` и обычным launcher. Это финальная проверка поведения сети/exchange после архитектурного изменения.

Не заявлять «всё работает», если реально не запускались тесты/локальный Testnet.

---

## 10. Следующая задача для Codex

### Приоритет 1 — Entry block extraction, без изменения поведения

Не переписывать `_try_open()` сразу «красиво».

Правильный порядок:

#### A. Characterization tests BEFORE move

Добавить точечные tests для текущего `_try_open()` поведения, особенно:

- invalid/stale snapshot -> no signal/no order;
- gate not ready -> no signal/no order;
- ready but strength < 3x -> reject;
- residual < 8 bps -> reject;
- arrival residual reversed -> expired/reject;
- remaining edge too small -> expired/reject;
- requested sizing остаётся текущим;
- virtual IOC qty / min notional check;
- slippage > max -> reject;
- executable edge/cost failure -> reject;
- Demo best price only после всех дешёвых LIVE/economic gates;
- one `open_ioc()` attempt;
- no fill -> `nofill`, no ActivePosition;
- confirmed partial fill accepted;
- management begins immediately from fill without mandatory position GET;
- post-fill invalid snapshot -> immediate close;
- post-fill stale book -> immediate close;
- post-fill actual fill < min -> immediate close;
- post-fill actual slippage too high -> immediate close;
- post-fill residual reversed/edge collapsed -> immediate close;
- success -> ActivePosition fields exactly as before.

#### B. Extract with minimal semantic movement

Рекомендуемый boundary:

- `SignalEngine` / signal evaluator: snapshot + gate -> `TradeSignal | None`;
- `EntryPolicy`: pure/pre-submit checks and possibly an `EntryPlan` data object;
- `EntryExecutor` or existing execution adapter remains owner of network order submission;
- `TradingEngine` orchestrates these blocks;
- `PositionManager` принимает только confirmed successful position lifecycle.

Не обязательно создавать все три сразу. Лучше один extraction per commit.

#### C. Keep network ordering identical

Особенно не менять порядок:

LIVE snapshot/gate/economics
-> sizing/virtual liquidity checks
-> Demo best price
-> Demo IOC submit
-> fill confirmation
-> immediate management state
-> post-fill guard

Нельзя добавлять extra private GET между fill и management.

### Приоритет 2 — уменьшить coupling TradingEngine

После Entry extraction `TradingEngine` должен стать orchestration layer, а не хранилищем стратегии.

### Приоритет 3 — только после стабильности обсуждать новые strategy improvements

Не оптимизировать thresholds, fees, latency policy, Profit Hold, leverage или exit logic одновременно с архитектурным refactor.

---

## 11. Антипаттерны, которые уже были проблемой

Не возвращать:

- wrapper цепочки, где один launcher импортирует другой старый launcher;
- global mutable active symbol/config state;
- monkeypatch `legacy_engine.SYMBOL`, `LeadLagGate`, etc.;
- deep `.env` loading внутри universe/scanner/engine;
- discovery, который сам запускает execution;
- LIVE private auth как неявная зависимость Testnet;
- дублирование Profit Hold в нескольких launchers;
- изменение thresholds через разные wrappers;
- обязательный polling sleep на critical path;
- обязательный `get_positions()` после confirmed entry fill до начала management;
- top-up/chase после partial IOC.

---

## 12. Источники при конфликте информации

Если Codex видит конфликт между разными эпохами проекта, использовать следующий приоритет:

1. Текущий код активной ветки `persistent-end2end-latency-v1`.
2. Regression/architecture tests на этой ветке.
3. Этот `CODEX_HANDOFF_20260821.md` для намерений и frozen invariants.
4. `DEVELOPER_HANDOFF_20260816.md` для измеренных исторических результатов и более ранней эволюции.
5. История ChatGPT по ссылке выше — для контекста решений, но не как замена текущему коду.
6. Старые commits — forensic reference, а не автоматически правильная версия.

Если документ говорит о желаемом поведении, а текущий тест явно фиксирует другое — сначала установить, не является ли это намеренным более поздним изменением. Не «чинить» молча.

---

## 13. Definition of Done текущего рефакторинга

Рефакторинг считается успешным, когда:

- пользователь запускает бота так же, как до рефакторинга;
- тот же `.env` работает;
- discovery/trading flow внешне не изменён;
- strict signal/entry/risk/exit semantics не изменены;
- real Demo/Testnet execution path остался тем же;
- все regression tests проходят;
- локальный Testnet run не показывает поведенческой регрессии;
- `TradingEngine` стал orchestration layer;
- discovery, signal, entry, execution, position management, exits, risk и reporting имеют понятные boundaries;
- изменение одной policy больше не требует импортировать/запускать весь старый монолит.

Главное: НЕ получить «более красивого другого бота». Нужно получить ТОГО ЖЕ бота, но разделённого на независимые блоки.
