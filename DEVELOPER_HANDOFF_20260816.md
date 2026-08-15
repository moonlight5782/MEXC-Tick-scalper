# MEXC Tick Scalper — технический отчёт для передачи разработки

Дата среза: 2026-08-16 (Europe/Bucharest)

Репозиторий: `moonlight5782/MEXC-Tick-scalper`

Ветка: `main`

HEAD кода до коммита этого отчёта: `e2a57d3 Model depth fills and equity scaling`

## 1. Краткий статус

Проект не перестраивался заново. Продолжена существующая реконструкция старого
MEXC-фьючерсного бота. Текущая рабочая гипотеза — направленная Binance -> MEXC
lead-lag стратегия: Binance является быстрым источником импульса, а на MEXC
открывается только одна позиция в направлении ожидаемого догоняющего движения.
Это не безрисковый двухсторонний арбитраж.

Сейчас есть три раздельных контура:

1. MEXC Demo execution — реальные IOC-заявки только на Testnet для проверки
   механики исполнения и reconciliation.
2. LIVE read-only shadow — реальные публичные Binance/MEXC bid/ask, но только
   виртуальные позиции; ни LIVE-, ни Demo-ордера не создаются.
3. Depth/scaling shadow — расширенный LIVE read-only контур с 20 уровнями
   стакана, IOC limit, частичным заполнением без докупки и виртуальным
   масштабированием позиции от капитала 60 USDT.

Кандидат стратегии показал положительный результат до комиссий на Demo и в
нескольких LIVE-shadow контролях, но прибыльность реального продукта ещё не
доказана. Главная неопределённость — реальная задержка private order gateway и
matching engine, очередь/изменение стакана во время отправки IOC и устойчивость
результата на независимых сессиях.

## 2. Жёсткая граница безопасности

- PRIVATE LIVE MEXC writes запрещены и не включались.
- LIVE Binance/MEXC market data разрешены.
- Приватная LIVE-таблица комиссий читается только read-only.
- Запись ордеров разрешена только в MEXC Demo/Testnet.
- Последний depth/scaling тест полностью read-only: execution adapter вообще не
  создаётся.
- Неизвестная, устаревшая либо ненулевая LIVE maker/taker fee блокирует вход.
- Для целевой стратегии требуется строго `maker=0` и `taker=0` на конкретном
  аккаунте и символе; этот набор динамический.
- Секреты, токены, cookies и `.env` не выводились и не коммитились.

Эту границу нельзя ослаблять без отдельного явного решения владельца проекта и
нового safety review.

## 3. Что реконструировано из истории старого бота

По ранее разобранным экспортам ордеров наиболее вероятная механика исполнения:

1. отправить относительно большой IOC LIMIT;
2. принять фактически доступное частичное заполнение;
3. отменить незаполненный остаток автоматически через IOC;
4. не докупать остаток рыночной заявкой;
5. управлять только реально заполненным количеством;
6. закрыть позицию reduce-only market.

Исторические ориентиры:

- UNIUSDT: около 685 входов, около 200x, requested notional около 20 000 USDT,
  медианное заполнение около 7,65%, PF около 1,45, итог около +121 USDT.
- BCHUSDT: около 456 входов, около 195x, requested notional около 10 000 USDT,
  медианное заполнение около 42,6%, PF около 1,89, итог около +108 USDT.
- Победители обычно удерживались дольше проигравших: примерно 9,5–10 секунд
  против 2–5 секунд.
- В истории встречались лимитные уровни на 4–10 bps от цены. Не доказано, что
  это был тот же режим сигнала.

Точный старый сигнал по одному экспорту ордеров восстановить нельзя. История
подтверждает execution pattern и sizing, но не доказывает причинность Binance ->
MEXC.

## 4. Зафиксированный кандидат стратегии

Профиль в коде: `binance-impulse-zero-fee-gross-v1`.

Основные параметры:

- символы: `XRP_USDT`, `LINK_USDT`, `DOGE_USDT`;
- источник входного сигнала: только Binance USD-M bookTicker;
- горизонт импульса: 100 ms;
- MEXC LIVE не участвует в направлении входа, но его исполнимый spread
  участвует в экономическом gate и цене shadow fill;
- threshold = `max(1.0 bps, MEXC spread + 0.2 bps, MEXC spread * 1.05)`;
- одна позиция одновременно;
- requested notional: 10 000 USDT;
- isolated leverage: до 200x, но не выше лимита контракта;
- IOC cross/limit offset: 5 bps;
- частичный fill принимается без докупки;
- минимальная прибыль для штатного target-caught выхода: 0,5 bps;
- adverse cut зависит от spread и фиксированного порога;
- положительный staged trailing защищает достигнутую прибыль;
- max hold: 60 секунд;
- gross session-loss halt: -50 USDT.

Выход `binance_target_caught` происходит только когда исполнимая цена MEXC
достигла зафиксированной при сигнале Binance target price и текущая доходность
не ниже 0,5 bps. Дополнительно существуют `live_adverse_cut`,
`positive_trailing_stop`, `timeout` и аварийное завершение сессии.

Важно: -50 USDT — фиксированный стоп сессии после закрытых результатов, а не
процент капитала и не гарантированный внутрисделочный лимит убытка. При капитале
60 USDT это слишком грубый контроль для реального использования и требует
переработки до любых LIVE writes.

## 5. Реализованные изменения

### Demo execution и телеметрия

- `start_demo.bat` ведёт в event-driven microspread Demo launcher.
- Реализован быстрый Binance impulse entry в существующем Demo runner.
- IOC entry, фактический fill, isolated leverage и reduce-only exit сохранены.
- Добавлена provisional/persistent reconciliation: подтверждённый IOC может
  появиться в Testnet positions с задержкой; до разрешения состояния повторный
  вход запрещён.
- Structured excursion CSV записывает signal -> book lookup -> IOC POST -> HTTP
  response -> confirmation -> reconciliation -> position visible.
- Demo gross, entry/exit fees, Demo net и LIVE zero-fee counterfactual разделены.

### LIVE read-only shadow

- Добавлен `src/mexc_tick_scalper/live_binance_impulse_shadow.py`.
- Runner использует публичные Binance/MEXC WebSocket данные и read-only LIVE fee
  discovery, но не импортирует order-capable execution path.
- Поддерживается воспроизведение 91 измеренного Demo latency sample.
- Добавлен zero-delay контроль для доказательства наличия edge в момент сигнала.
- Добавлен текущий безопасный latency proxy: `15.5 ms order build + robust
  median(public MEXC REST RTT)/2` отдельно на входе и выходе. Устаревший/missing
  RTT блокирует новые входы.

### Стакан, partial fill и масштабирование

- `EventMexcDepthFeed` сохраняет до 20 уровней bid/ask, а не только top of book.
- Объём контрактов переводится в base quantity через актуальный LIVE
  `contractSize`.
- IOC entry проходит стакан только до limit price с offset 5 bps.
- Partial fill принимается; остаток не добирается.
- Exit проходит актуальный стакан как reduce-only market shadow fill.
- PnL считается по фактическому виртуальному base quantity и VWAP входа/выхода.
- CSV содержит requested/filled notional, fill ratio, использованные уровни,
  видимую ликвидность и equity before/after.
- Requested notional масштабируется как базовые 10 000 USDT * equity/60, затем
  ограничивается `equity * contract_leverage * 0.90` и лимитом контракта.

### Регрессионные тесты

Добавлены тесты на:

- строгий fee gate;
- замороженные параметры профиля;
- latency trace/replay и текущий RTT proxy;
- расчёт executable bid/ask и slippage;
- depth walking и contract-size conversion;
- partial IOC без top-up;
- market exit VWAP и shortfall;
- equity compounding и isolated-margin cap;
- позднюю видимость Demo-позиции и persistent reconciliation.

Проверка на текущем HEAD:

```text
146 passed in 1.11s
```

Команда:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .codex-report-test-tmp -p no:cacheprovider
```

## 6. Данные и условия экспериментов

Использовались четыре различных типа данных; их нельзя смешивать в одном
выводе:

1. Исторический XLSX старого бота — фактические старые MEXC orders/positions;
   применяется для reconstruction execution pattern и sizing.
2. MEXC Demo/Testnet — реальные тестовые IOC/fill/position/fee события; цены и
   комиссии Testnet не считаются надёжной моделью LIVE economics.
3. Текущие Binance USD-M и MEXC LIVE WebSocket bid/ask/depth — реальные
   исполнимые котировки для read-only shadow.
4. LIVE account fee table — read-only подтверждение exact 0/0 на момент сессии.

Ключевые артефакты в корне репозитория:

- `binance_impulse_fast_100_excursions_20260815.csv` — полная Demo excursion и
  latency telemetry;
- `binance_impulse_live_shadow_instant_100_20260815.csv` — завершённый
  zero-delay LIVE shadow;
- `binance_impulse_live_shadow_current_rtt_1bps_100_20260816.csv` — текущий
  fixed-notional shadow с online RTT proxy;
- `depth_scaled_current_rtt_1bps_100_20260816.csv` — текущий depth/scaling
  shadow;
- одноимённые `.log`/`.err.log` — параметры запуска и диагностика.

Эти CSV/log являются generated research artifacts и намеренно не добавлены в
Git.

## 7. Измеренные результаты

### 7.1 Demo: замороженный успешный gross-кандидат

Основные 91 штатно залогированный exit плюс один отдельно reconciled emergency
exit:

- 92 позиции;
- zero-fee Demo gross counterfactual: +232,3418 USDT;
- Testnet fees: 367,9385 USDT;
- фактический Demo net: -135,5967 USDT;
- gross W/L/F: 68/20/4;
- non-flat gross winrate: 77,27%;
- gross PF: около 4,89;
- median/p95 hold: 5,37 s / 60,00 s;
- median/p95 IOC confirmation: 642,9 / 1766,6 ms;
- median/p95 signal -> position visible: 1041,6 / 5049,5 ms.

То есть положительный результат был только до Testnet-комиссий. Это полезный
кандидат исключительно для пар с подтверждёнными LIVE maker=0/taker=0.

### 7.2 LIVE read-only zero-delay контроль — завершённые 100 сделок

Файл: `binance_impulse_live_shadow_instant_100_20260815.csv`.

- PnL при fee=0: +173,2098 USDT;
- W/L/F: 79/21/0;
- non-flat winrate: 79,00%;
- PF: 4,126;
- median/p95 hold: 1,130 / 27,698 s;
- median/p95 event-loop signal -> fill: 8 / 15 ms;
- LINK дал 96 из 100 сделок и +169,7840 USDT.

Контроль подтверждает наличие краткоживущего edge в доступном процессе сразу
после сигнала, но не доказывает, что реальный IOC успеет получить эту цену.

### 7.3 LIVE read-only fixed 10k с текущим RTT proxy — незавершённый срез

Файл: `binance_impulse_live_shadow_current_rtt_1bps_100_20260816.csv`.
На момент формирования отчёта: 74 сделки.

- zero-fee PnL: +38,8116 USDT;
- W/L/F: 46/21/7;
- non-flat winrate: 68,66%;
- PF: 1,516;
- median/p95 hold: 9,633 / 63,016 s;
- median/p95 signal -> fill: 166,5 / 203,4 ms;
- median/p95 exit decision -> fill: 354 / 2003 ms;
- LINK: 52 сделки, +32,3996 USDT;
- DOGE: 9 сделок, +1,4335 USDT;
- XRP: 13 сделок, +4,9785 USDT.

Этот процесс был запущен раньше нового depth/scaling теста и всё ещё является
промежуточным, а не финальным результатом.

### 7.4 LIVE read-only depth + scaling — основной текущий тест

Файл: `depth_scaled_current_rtt_1bps_100_20260816.csv`.

Условия:

- реальные Binance bookTicker и MEXC LIVE depth;
- 20 уровней MEXC;
- IOC limit offset 5 bps;
- partial fill без докупки;
- fee=0/0 gate;
- slippage parameter 0 bps, но VWAP по реальным уровням стакана;
- online LIVE RTT proxy;
- initial equity 60 USDT;
- initial requested notional 10 000 USDT;
- leverage capped by contract, максимум 200x;
- max isolated margin fraction 90%;
- min edge 1 bps и spread-aware threshold;
- fixed gross session stop -50 USDT;
- цель 100 закрытых виртуальных сделок.

Промежуточный срез на момент отчёта: 25/100 сделок.

- zero-fee PnL: +25,8126 USDT;
- virtual equity: 60,0000 -> 85,8126 USDT;
- max closed-equity drawdown: 4,2866 USDT;
- W/L/F: 16/6/3;
- non-flat winrate: 72,73%;
- PF: 2,812;
- median/p95 hold: 9,390 / 62,273 s;
- median/p95 signal -> fill: 173,0 / 204,4 ms;
- median/p95 exit decision -> fill: 363 / 1609 ms;
- median requested/filled notional: 10 584,77 / 10 579,68 USDT;
- p95 requested/filled notional: 13 878,13 / 13 871,20 USDT;
- fill ratio median/p5: 100% / 100%;
- partial/unfilled entries: 0/0;
- entry levels median/p95: 1/2;
- exit levels median/p95: 1/2.

По символам:

- LINK: 17 сделок, +17,1874 USDT, W/L/F 12/4/1;
- DOGE: 5 сделок, +5,6167 USDT, W/L/F 2/1/2;
- XRP: 3 сделки, +3,0086 USDT, W/L/F 2/1/0.

По причинам выхода:

- `binance_target_caught`: 16, +38,6590 USDT;
- `live_adverse_cut`: 5, -10,4009 USDT;
- `timeout`: 4, -2,4455 USDT.

Все первые заявки поместились в видимую глубину в пределах 5 bps, поэтому тест
пока не получил ни одного естественного partial fill. Это не дефект механизма,
но выборка ещё не проверила именно ту низкую fill-ratio картину, которая была в
исторических UNI/BCH данных.

## 8. Что уже ясно из сравнения

- Zero-delay результат существенно лучше режима с реалистичным latency proxy:
  edge быстро стареет, задержка является главным параметром.
- При текущем proxy стратегия пока положительна до комиссии, но PF заметно ниже
  zero-delay контроля.
- LINK доминирует по числу сигналов и PnL; итог нельзя считать трёхсимвольной
  диверсификацией.
- `binance_target_caught` приносит прибыль, а основная утечка идёт через adverse
  cut и часть timeout.
- 10 000–14 000 USDT на текущих XRP/LINK/DOGE обычно помещаются в 1–2 уровня;
  поэтому depth-aware модель пока почти совпадает с full-fill моделью.
- Testnet fees экономически уничтожили Demo gross, но это не опровергает
  zero-fee гипотезу; одновременно это означает, что любая ненулевая или
  изменившаяся комиссия должна немедленно блокировать торговлю.

## 9. Известные проблемы и ограничения

### Критические для доказательства profitability

1. Online RTT proxy не является измерением реального private IOC execution.
   Он не включает private gateway, matching-engine queue, подтверждение fill и
   возможный rate limiting. Его нельзя называть фактической LIVE execution
   latency.
2. Depth snapshot берётся в виртуальный момент fill, но модель не симулирует
   queue position, отмены/появление ликвидности между snapshot и матчингом,
   hidden liquidity, reject и network packet loss.
3. Параметр дополнительного slippage равен нулю. VWAP внутри видимого стакана
   учитывается, но model-risk остаётся оптимистичным.
4. Наблюдений partial fill пока нет. Нельзя считать историческую механику
   воспроизведённой статистически, пока не проверены менее глубокие пары или
   больший requested notional в read-only shadow.
5. Текущая выборка depth/scaling — 25 сделок одной рыночной сессии. Она слишком
   мала для product decision и может зависеть от режима волатильности.
6. Exact 0/0 fee — аккаунт- и времени-зависимое состояние, а не свойство символа
   навсегда.

### Риск и sizing

7. Масштабирование сейчас линейно компаундит весь virtual equity reference и
   допускает до 90% isolated margin. Это реконструкция поведения, а не
   рекомендованный production risk model.
8. Stop -50 USDT при initial equity 60 USDT слишком велик. Он срабатывает после
   realized shadow trade и не предотвращает liquidation/gap внутри сделки.
9. Нет доказанной модели liquidation price, maintenance margin tiers, funding,
   ADL и contract-specific risk limits в shadow PnL.
10. Абсолютный PnL масштабированной симуляции нельзя экстраполировать как
    гарантированный доход от реального депозита.

### Данные и процесс

11. TradingView candles недостаточны для проверки миллисекундного lead-lag;
    проверять надо по сохранённым tick/depth данным и timestamps.
12. В рабочем каталоге много untracked исследовательских CSV/log/tmp файлов.
    Tracked tree до добавления этого отчёта чистый; generated artifacts нельзя
    массово коммитить.
13. Кодовая часть локального `main` опережала `origin/main` на 7 коммитов; после
    добавления этого отчёта ветка опережает origin на 8. Изменения сохранены
    локальными коммитами, но ещё не опубликованы на GitHub.

## 10. Точный следующий эксперимент

### Сначала завершить текущие 100 сделок без изменения параметров

Не подгонять threshold/exit/sizing по промежуточным 25 сделкам. После 100 exits
сохранить:

- zero-fee PnL и final equity;
- max drawdown в USDT и процентах;
- W/L/F, non-flat winrate и PF;
- median/p95 hold, signal-to-fill, exit-decision-to-fill;
- requested/filled notional, fill ratio median/p5;
- partial/unfilled count и levels used;
- результаты по символам, направлениям и exit reason;
- минимум/максимум RTT во время сделки;
- MAE/MFE распределения победителей и проигравших.

Критерий продолжения: PF > 1 после консервативного stress, приемлемая просадка и
результат не должен полностью зависеть от нескольких LINK wins.

### Затем независимый latency/depth stress без LIVE writes

На сохранённых реальных depth/tick данных либо в параллельных read-only shadow
прогонах сравнить один и тот же поток сигналов:

1. текущий RTT proxy;
2. proxy +50 ms;
3. proxy +100 ms;
4. proxy +200 ms;
5. slippage 0 / 0,25 / 0,5 bps на сторону;
6. IOC offset 2 / 5 / 10 bps;
7. fixed 10k против equity scaling;
8. текущие XRP/LINK/DOGE и отдельный liquidity-stress набор из exact 0/0 пар с
   меньшей глубиной, выбранный по свежему edge-after-spread ranking.

Оценивать walk-forward: параметры выбираются на одной сессии, подтверждаются на
другой. Не оптимизировать и не оценивать на одних и тех же 100 сделках.

### До любого LIVE execution

Нужны одновременно:

- несколько независимых 100+ trade read-only сессий в разные часы/режимы;
- положительный PF после latency/slippage/depth stress;
- account-specific exact 0/0 fee gate на каждом входе;
- risk sizing как малый процент equity, а не 90%;
- drawdown halt в процентах и дневной loss limit;
- liquidation/maintenance-margin/risk-tier guard;
- измеренная на минимальном безопасном объёме private order round-trip и fill
  latency только после отдельного явного разрешения;
- canary rollout с минимальным notional, а не сразу исторические 10k/20k.

## 11. Коммиты после `origin/main`

`origin/main` сейчас указывает на `3bc9389 Add fast Binance impulse Demo execution`.
Локально поверх него находятся:

- `723c15c` — frozen zero-fee-gross strategy profile;
- `1bb2818` — persistent confirmed IOC reconciliation;
- `36e838f` — read-only Binance impulse LIVE shadow;
- `e0b4e87` — replay measured Demo latency;
- `1ce7789` — full Demo latency trace recovery;
- `304b2bb` — current MEXC LIVE network-latency model;
- `e2a57d3` — depth fills and equity scaling.

Перед публикацией нужно просмотреть `git diff origin/main..HEAD`, не добавлять
generated logs/CSV/tmp и затем отдельно push/PR.

## 12. Основные файлы для продолжения

- `AGENTS.md` — обязательные safety/strategy/Git правила.
- `PROJECT_STATE.md` — основная история решений и текущее состояние.
- `src/mexc_tick_scalper/demo_microspread_test.py` — LIVE signal -> Demo IOC.
- `src/mexc_tick_scalper/live_binance_impulse_shadow.py` — основной read-only
  shadow, latency, depth, scaling и CSV.
- `src/mexc_tick_scalper/microspread.py` — Binance impulse и microspread models.
- `src/mexc_tick_scalper/microspread_feed.py` — event-driven Binance/MEXC feeds
  и depth representation.
- `src/mexc_tick_scalper/demo_position_manager.py` — Demo lifecycle/reconciliation.
- `src/mexc_tick_scalper/web_execution.py` — Demo execution adapter и safety.
- `src/mexc_tick_scalper/live_zero_fee_universe.py` — exact LIVE 0/0 discovery.
- `tests/test_live_binance_impulse_shadow.py` — shadow/depth/scaling regression.
- `tests/test_microspread.py` — strategy/Demo/fee/reconciliation regression.

## 13. Формулировка текущего вывода

Корректно: «Binance impulse candidate показал положительный gross результат на
одной Demo сессии и положительные zero-fee результаты в LIVE read-only shadow;
промежуточный depth/scaling run также положителен. Edge чувствителен к latency,
а реальная private execution latency, queue/partial-fill distribution и
walk-forward устойчивость ещё не доказаны».

Некорректно: «бот гарантированно прибыльный», «60 USDT превратятся в измеренный
shadow equity на реальном аккаунте» или «текущий RTT proxy равен фактической
скорости исполнения реального ордера».
