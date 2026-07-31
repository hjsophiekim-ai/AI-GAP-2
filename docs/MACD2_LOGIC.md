# MACD2 Logic

## 2026-07-31 MACD2 Primary Rule

- `strategy_version`: `20260731_KIS_MACD_COLOR_FLAG_V1`
- `signal_rule`: `KIS_MACD_COLOR_FLAG_CONFIRMED`
- Primary order authority uses completed 3-minute bars only.
- MACD uses EMA 12/26/9 with `adjust=False`.
- A completed bar is `UP_RED` when MACD histogram values rise for two consecutive completed bars: `h0 > h1 > h2`.
- A completed bar is `DOWN_BLUE` when MACD histogram values fall for two consecutive completed bars: `h0 < h1 < h2`.
- Histogram sign is not part of the KIS color rule. A less-negative histogram can be `UP_RED`; a less-positive histogram can be `DOWN_BLUE`.
- `flag_time` and `signal_id` use the completed bar's start timestamp (`bar_start_at`), while tradeable confirmation happens at `bar_start_at + 3 minutes`.

본 문서는 독립 모듈 `app/trading/macd2/`의 현재 운용 기준이다(2026-07-27 KIS-parity 개정, 2026-07-30 Optional Hybrid MAJOR_FLAG 필터 추가, 2026-07-31 플래그 정합성 수정 — 진행봉 candidate 주문권한 재제거·1분봉 완전성 게이트·Worker 세션/SHA 기준 통계 분리). MACD v1, Enhanced 전략과 파일·상태·원장을 공유하지 않는다.

## 목적

MACD2는 SK하이닉스(`000660`) KIS 3분봉 MACD(12,26,9) 차트에 빨간색·파란색 플래그가 표시되는 **완성된 3분봉**과 정확히 같은 방향·같은 봉의 Primary 플래그를 만들고, 확정 즉시 `signal_id`를 생성해 원장에 기록하고 주문을 요청한다.

**신호 계산의 단일 원본은 KIS가 제공하는 당일 1분봉 API다.** 실시간 quote만으로 별도의 가상 1분봉 이력을 만들지 않는다. 진행 중(아직 완성되지 않은) 3분봉의 값과 Signed-B는 UI shadow/candidate 표시 전용이며, 주문·통계·`last_direction` 판단에는 어떤 경우에도 사용하지 않는다.

## 종목과 방향

- 신호 원천: `000660` (직접 매매하지 않음)
- `UP_RED`: `0193T0` 매수
- `DOWN_BLUE`: `0197X0` 매수
- 반대 ETF 보유 중 반대 신호: 기존 ETF 전량 SELL → 체결 확인 → 실제 잔고 0 확인 → 반대 ETF BUY 순서를 강제한다. 매도 잔량이 남아있으면 반대매수를 절대 하지 않는다.

종목 방향, 손절, Profit Lock, 14:55 신규진입 금지, 15:00 강제청산, MOCK/REAL 게이트는 본 문서의 고정 규칙이다.

## 데이터 — KIS 당일 1분봉이 단일 원본

- `000660` 1분봉은 KIS 당일 1분봉 API(`MarketDataService.merge_incremental_1m`)로 주기적으로(history-updater 스레드, Worker tick과 같은 주기) 갱신하며, 기존 warm-up 이력에 **append → datetime 기준 dedup(keep last) → sort**한 뒤 저장한다. Worker 자신은 KIS를 직접 호출하지 않고 이 캐시만 읽는다.
- 완성 3분봉은 09:00 기준 3분 경계로 이 누적 1분봉 이력에서 리샘플한다: 09:00~09:02, 09:03~09:05, ... (`label="left", closed="left"` — 봉의 이름/`signal_id`는 항상 그 봉의 **시작시각**이다. 예: 13:42~13:44 봉은 `completed_bar_at`/`signal_id`에 `13:42:00`으로 기록되고, `detected_at`/`order_requested_at`만 그 봉이 실제로 마감된 13:45:00 이후에 찍힌다 — 봉 시작/종료/평가 시각을 서로 섞어 쓰지 않는다.)
- 전일 데이터는 EMA warm-up에만 사용한다.
- **2026-07-31 1분봉 완전성 게이트**: 리샘플된 3분봉은 그 3개 구성 1분봉이 **모두** `open/close` 유효값과 함께 존재할 때만 confirmed 취급한다(`market_data.filter_complete_3m_bars`). API 오류·중간 빈 페이지로 1분봉 한 개라도 비면 그 3분봉은 confirmed 목록에서 제외되며(임의 보정·보간 금지), `state.order_block_reason="HISTORY_GAP"`으로 그 시점의 신호 평가·MAJOR 필터·주문을 모두 차단한다. `last_confirmed_bar_ts`는 이때 전진하지 않으므로, 이후 incremental merge로 누락 1분봉이 채워지면 같은 봉이 정상적으로 재평가된다.
- **2026-07-27 조회 신뢰성 수정**: `주식일별분봉조회`/`inquire-time-itemchartprice`는 실제로 요청 1회당 약 30건만 반환한다(`count` 파라미터와 무관). 페이지 예산(`KIS_MAX_PAGES`)을 06→20으로 늘려 하루 세션(09:00~15:30, 390분)을 모두 커버하도록 했다(이전에는 장 시작 후 3시간이 지나면 당일 데이터 앞부분이 누락됐다). 또한 연속 페이지 요청 사이에 짧은 페이싱(`KIS_PAGE_FETCH_PACING_SEC`)을 두어 KIS 초당 거래건수 제한으로 중간 페이지가 조용히 빈 결과로 오는 문제를 줄였다. 전일 warm-up 날짜 조회가 KIS 서버 오류(500 등)로 실패하면 이를 "해당 날짜 데이터 없음(휴장일)"로 오인해 더 이전 날짜로 잘못 대체하던 버그도 수정했다(`PRIOR_DAY_FETCH_RETRIES`/`PRIOR_DAY_FETCH_RETRY_DELAY_SEC` — 명시적 오류를 동반한 빈 응답만 재시도, 오류 없는 진짜 휴장일은 즉시 다음 날짜로 진행). 전일 warm-up 조회(`_fetch_minute_candles_for_date`)는 MOCK 환경의 이 엔드포인트가 유독 불안정해 읽기 전용 REAL 계좌 client로 수행한다(`000660`은 신호 입력 전용, 직접 매매 대상이 아니므로 REAL/MOCK 시세 데이터가 동일함을 실측 검증했다 — 매매·잔고·주문 경로는 여전히 MOCK만 사용). 당일 라이브 페이징은 REAL 전환 시 오히려 REAL 계좌 rate limit에 더 취약해져 MOCK 그대로 유지한다.
- 당일 추가된 1분봉 수(`today_1m_bar_count`), 1분봉 이력의 최신 시각(`history_newest_at`), 마지막으로 완성된 3분봉 시각(`last_completed_3m_bar_at`)을 매 tick runtime/UI에 표시한다.
- `000660` quote와 최근 1분봉 close가 10배/0.1배 스케일 차이를 보이면 MarketData 계층에서 quote를 1회 정상화한다(신호 계산부의 임의 보정은 금지). 이 보정 이후에도 quote와 1분봉 history의 가격 비율이 정상 범위(`QUOTE_HISTORY_PRICE_RATIO_MIN`~`MAX`)를 벗어나거나, 정규장 중인데 1분봉 history의 최신 시각이 `HISTORY_STALE_MAX_SEC` 이상 갱신되지 않으면 **단위·시각 불일치**로 보고 신규 진입을 차단하며 `state.quote_history_mismatch_reason`에 원인을 남긴다.
- 주문에 필요한 quote는 `price > 0`이고 `age_sec <= 10`이어야 한다.
- flat `UP_RED`: `000660`, `0193T0` quote 필요
- flat `DOWN_BLUE`: `000660`, `0197X0` quote 필요
- 스위칭: `000660`, 현재 보유 ETF, 신규 매수 ETF quote 필요
- 관계없는 ETF stale만으로 주문을 차단하지 않는다.

## MACD 계산

- MACD: EMA 12 - EMA 26
- Signal: MACD의 EMA 9
- EMA는 `adjust=False`를 사용한다.
- **주문권한이 있는 계산은 완성된 3분봉만 사용한다.** 진행 중 3분봉(아직 마감되지 않은 봉)의 값은 UI shadow/candidate 표시에만 쓰이며 이 계산에 절대 섞이지 않는다.

## Primary 신호 — 완성봉 MACD crossover만이 주문권한을 가진다

실제 주문권한이 있는 Primary 신호는 **새로 완성된 3분봉의 MACD(12,26,9) crossover** 하나뿐이다(2026-07-27 KIS-parity 개정 — 진행봉/Signed-B는 아래 "Candidate/Shadow" 절 참조).

선택형 **강한 플래그 필터(MAJOR_FLAG)** 가 OFF이면 이 confirmed crossover가 곧 주문권한이다. ON이면 confirmed crossover는 여전히 전부 생성·기록되지만, 주문권한은 Hybrid 점수 승인(`MAJOR_APPROVED`)된 신호에만 부여된다(아래 “Optional Hybrid MAJOR_FLAG filter” 절).

직전 완성 3분봉의 diff와 새로 완성된 3분봉의 diff를 비교한다.

- 이전 완성봉 diff `<= 0`이고 새 완성봉 diff `> 0`: `UP_RED`
- 이전 완성봉 diff `>= 0`이고 새 완성봉 diff `< 0`: `DOWN_BLUE`
- 그 외: `HOLD`

**새 completed_bar timestamp가 생길 때마다 정확히 1회만 평가한다** (`worker._advance_confirmed_primary`). 같은 완성봉을 가리키는 반복 tick은 재평가하지 않고 항상 `HOLD`를 반환해 동일 봉 중복주문을 원천 차단한다.

**장 시작 후 이 상태가 평가한 첫 완성 3분봉(또는 직전 평가 봉과 날짜가 다른 최초 봉)은 baseline만 설정하고 주문하지 않는다.** 그 봉의 이전 diff는 전일(또는 그 이전) 마지막 완성봉에서 온 값이라 갭만으로 교차가 발생할 수 있기 때문이다. 이 baseline 평가는 방향 억제 상태(`last_detected_direction`)에도 반영하지 않으므로, 이후 실제 당일 교차는 정상적으로 신호가 된다.

Primary 계산은 `app.trading.macd2.signal_engine.evaluate_macd_crossover()` + `calculate_macd()`를 공통 함수로 사용한다. Worker, UI 상태, 리플레이, 테스트는 반드시 이 결과에서 나온 MACD, Signal, diff, direction, `signal_id`를 같은 의미로 해석해야 한다.

## Candidate/Shadow — 진행봉과 Signed-B는 주문권한이 없다

- **진행 중(forming) 3분봉의 provisional MACD/diff**: 최신 유효 `000660` quote로 매 tick 갱신되는 진단값이다. `evaluate_primary_forming_crossover()`로 계산하되, 그 결과는 UI candidate 표시(`state.candidate_flag`, `state.provisional_flag`)에만 쓰인다. 같은 방향이 서로 다른 fresh quote tick에서 `config.PROVISIONAL_CONFIRM_MIN_GAP_SEC` 이상 간격을 두고 2회 유지되면 "confirmed candidate"로 표시하지만, 이 확정도 여전히 shadow일 뿐이다.
- **2026-07-31 재확인/수정**: 한때 이 candidate가 `_dispatch_confirmed_signal`을 직접 호출해(진행 중 3분봉·live quote 기반으로) 실제 BUY/스위칭 주문과 MAJOR_FLAG 채점까지 수행하던 회귀가 있었다(봉이 마감되기 전에 평가되어 `detected_at`이 봉 종료시각보다 앞서는 등 §플래그 시각 규칙을 위반). `run_once()`에서 candidate/provisional 경로의 주문 호출을 완전히 제거해, candidate가 broker/`order_executor`/MAJOR 필터/`processed_signal_ids`/signal ledger 중 어느 것도 절대 건드리지 못하도록 되돌렸다 — 오직 완성봉 Primary crossover(`_advance_confirmed_primary`)만 주문권한을 가진다.
- **Signed-B**: `evaluate_signed_b`, histogram slope, signed-B 조건은 주문권한이 없다. UI에는 shadow 진단값으로만 표시한다(최근 histogram 3개, signed-B shadow direction, `order_authority=NONE`).
- 진행봉 provisional 값과 Signed-B는 오늘 빨강/파랑 통계, Primary 플래그 수, `last_direction`, signal ledger 어디에도 절대 포함하지 않는다.

## signal_id와 중복 차단

Primary(확정) `signal_id` 형식:

```text
YYYYMMDD_HHMMSS_DIRECTION
```

`HHMMSS`는 새로 완성된 3분봉의 시작시각이다(`make_signal_id`, `_PROVISIONAL` 접미사 없음 — 그 접미사가 붙은 형식은 candidate/shadow 표시 전용 라벨이며 원장에 기록되지 않는다).

같은 완성봉에서 20회 반복 평가해도 플래그와 주문은 최초 onset 1회만 생성한다: `_advance_confirmed_primary`의 봉-once 게이트, `processed_signal_ids`, signal ledger `signal_id` dedup이 3중으로 중복주문을 막는다.

동일 `signal_id`는 재시작 후에도 재주문하지 않는다. 신호 원장은 `signal_id` 기준 append-only dedup을 수행한다.

## Worker 흐름

Worker는 5초 tick으로 동작한다.

1. state 로드
2. position reconcile (실제 계좌와 항상 비교)
3. quote cache 읽기
4. history cache 읽기 (KIS 당일 1분봉 — history-updater 스레드가 별도로 갱신)
5. 완성 3분봉 resample 및 confirmed MACD 계산, Signed-B shadow 갱신
6. 당일 1분봉 수/history 최신시각/quote-history 단위·시각 불일치 진단 갱신
7. 진행봉 provisional MACD/candidate 갱신 (shadow 표시 전용, 아래 8-9와 독립)
8. `_advance_confirmed_primary()`로 새 완성봉 여부 판정 → Primary direction 산출 (봉당 정확히 1회)
9. 신규 Primary 방향이면:
   1. 신호 원장에 confirmed 플래그 기록(필터 ON/OFF와 무관 — 원본 플래그 수·시각·방향은 유지)
   2. **강한 플래그 필터가 ON이면** `evaluate_major_flag()` → 거래 게이트 적용 → 승인(`MAJOR_APPROVED`)일 때만 아래 주문 단계로 진행. 탈락이면 `FILTERED_OUT`으로 종료하고 broker 호출 0 (아래 “Optional Hybrid MAJOR_FLAG filter” 절)
   3. 필터 OFF이거나 MAJOR 승인 시: 주문 직전 KIS 실제 주문가능금액 재조회
   4. `min(UI 예산, 실제 주문가능금액)`에서 수수료·1틱 안전여유를 뺀 안전 수량 계산
   5. (반대신호인 경우) 기존 ETF SELL → 체결 확인 → 실제 잔고 0 확인
   6. 신규/반대 ETF BUY 요청
   7. 주문번호로 체결내역 조회 — 주문 접수 응답만으로 체결 확정하지 않는다. 최대 `ORDER_FILL_POLL_MAX_SEC`(60초) 동안 폴링하며 부분체결도 실제 체결수량 그대로 반영한다.
   8. 체결 확인 후 실제 잔고를 다시 조회해 종목·수량·평균단가를 state에 반영
10. quote 또는 position 일시 오류면 같은 `signal_id`를 `pending_signal`로 유지하고 최대 30초 재시도
11. 주문 요청이 실제 생성되면(또는 MAJOR 필터 탈락으로 소비되면) `processed_signal_ids`에 등록
12. state 저장

진행봉 crossover는 8-9의 Primary 판단에 전혀 관여하지 않는다 — 오직 완성봉만 주문을 만든다. MAJOR 필터는 완성봉 confirmed 신호의 **주문권한 게이트**일 뿐, confirmed 플래그 생성 자체는 바꾸지 않는다.

## Worker 시작/재시작 — LIVE_CONFIRMED vs HISTORICAL_REPLAY_ONLY

- `initialize_strategy_session()`(Worker `start()`마다 실행)은 그 시점의 마지막 완성봉을 `session_baseline_bar_ts`/`last_confirmed_bar_ts`로 기록한다. `_advance_confirmed_primary`는 이 baseline 봉 자체를 "이미 평가됨"으로 취급해 재주문하지 않고, baseline **이후에 새로 완성되는 봉만** 정상적으로 신호가 된다 — 즉 Worker가 내려가 있던 동안(또는 장 시작 전) 이미 완성된 과거 봉은 재시작 시 절대 뒤늦게 주문되지 않으며, 재시작 이후 첫 신규 교차는 정상적으로 포착된다.
- **오늘 전체 신호 통계 재계산**: Worker가 실제로 살아있지 않았던 시간대(장 시작 전 포함)에 완성된 봉은 라이브 실행 중에는 신호 원장에 전혀 기록되지 않는다(위 baseline 스킵 때문). UI 통계 패널에는 이를 보완하기 위해 `worker.compute_today_signal_overview(df_1m, now=..., session_started_at=state.session_started_at)`가 오늘 하루 전체의 confirmed crossover를 **읽기 전용으로 재계산**해 보여준다 — `resample_completed_3m`/`filter_complete_3m_bars`/`calculate_macd`/`evaluate_macd_crossover`와 완전히 동일한 순수 함수만 재사용하며, `order_executor`/`major_flag_filter`/`processed_signal_ids`/signal ledger에는 어떤 영향도 주지 않는다(참고용 표시 전용).
  - 봉 마감 시각(`bar_end`)이 `session_started_at`보다 **이전**이면 `HISTORICAL_REPLAY_ONLY`(주문권한 없었음, 표시 전용).
  - 봉 마감 시각이 `session_started_at` **이상**이면 `LIVE_CONFIRMED`(실제 Worker가 주문 기회를 가졌던 봉과 동일).
  - `HISTORICAL_REPLAY_ONLY`/`HISTORY_GAP`(위 1분봉 완전성 절 참조) 봉은 MAJOR 필터·주문 평가 대상이 될 수 없다 — 애초에 broker/필터 호출 경로 자체에 들어오지 않는다.

## 원장 신호의 원산지(SHA) 격리

- 신호 원장의 각 행은 그 신호가 기록된 시점의 배포 코드 SHA를 `worker_code_sha`에 남긴다(`worker.git_sha()` — `git rev-parse --short HEAD`).
- "오늘 빨강/파랑 플래그" 통계·"현재 confirmed" 표는 **현재 실행 중인 코드의 SHA와 일치하는 행만** 집계한다(`ledger.summarize_signals(..., worker_code_sha=...)`). 배포 도중 SHA가 바뀌면 그 이전 SHA로 기록된 행은 자동으로 `OLD_WORKER_SHA` 사유로 "과거/제외 신호" 패널에만 표시되고, 기존 원장 행 자체는 삭제·수정하지 않는다(조회 필터만 변경).
- 같은 (날짜, `completed_bar_at`, `direction`) 조합은 `signal_id`가 항상 동일하므로(SHA/버전을 포함하지 않음) 원장 저장 단계(`append_signal`의 `signal_id` dedup)에서 이미 최초 1건만 남는다 — 서로 다른 SHA가 같은 봉을 동시에 기록하는 중복 행은 애초에 생길 수 없다.

## 주문 및 위험관리

- `UP_RED` + flat: `0193T0` BUY
- `DOWN_BLUE` + flat: `0197X0` BUY
- 반대 신호 + 보유: 기존 ETF SELL → 체결 확인 → 잔량 0 확인 → 반대 ETF BUY
- 14:55 이후 신규 진입 금지
- 15:00 이후 강제청산 우선
- Stop Loss(-1.5%)와 Profit Lock(+1.5% 활성화, 0.8%p 반납 청산)은 기존 규칙을 유지한다.
- 신규 BUY 수량은 시장가가 아니라 fresh 매도 1호가 기반 일반 지정가(`ORD_DVSN=00`, `order_type=limit`)로 계산한다. IOC(`ORD_DVSN=11`)는 신규 BUY 경로에서 사용하지 않는다. 주문 직전 KIS 호가조회에서 `ask1`을 받고 `order_price=ask1+1틱`(KRX 호가단위 정규화)으로 정한다. ask1이 0/stale/조회실패면 시장가 전환 없이 차단한다. 같은 계좌·종목·`ORD_DVSN=00`·`order_price`로 KIS 매수가능조회 후 `usable_cash = min(UI 예산, KIS 실제 주문가능금액)`, `budget_qty = floor((usable_cash * 0.995) / order_price)`, `final_qty = min(budget_qty, limit_buyable_qty)`를 사용한다. `expected_amount`는 항상 `usable_cash * 0.995` 이하로 재검증하며, 과도한 수량 차감은 하지 않고 필요 시 최대 1주만 줄인다. 호가조회 실패/stale 또는 `final_qty=0`이면 시장가로 자동 전환하지 않고 주문을 차단해 원장/UI에 `ask1`, `order_price`, `order_type`, `usable_cash`, `limit_buyable_qty`, `budget_qty`, `final_qty`, `expected_amount`, `filled_qty`를 기록한다.
- 주문가능금액 부족 시 주문하지 않고 KIS의 실제 코드·메시지를 신호 원장에 그대로 기록한다. 같은 `signal_id`로 무한 재시도하지 않는다(signal_id 단발성 원칙).
- 체결은 주문 성공 응답만으로 확정하지 않고, 주문번호 기준 실제 체결/잔고 재조회로 확인한다(최대 60초 폴링, 부분체결 반영).
- MOCK/REAL 게이트는 broker adapter와 기존 service 경로를 따른다. REAL 주문, 신용, 미수는 사용하지 않는다.
- 실제 KIS 주문은 명시된 운영 모드에서만 허용한다. 테스트는 fake broker만 사용한다.
- **QUOTE_STALE 처리 (2026-07-27 수정)**: confirmed 신호가 000660/주문 대상 ETF quote stale로 막히면, 여러 tick에 걸쳐 대기하지 않고 **같은 tick 안에서 동기적으로** 해당 종목 quote를 강제 재조회(`market_data.refresh_quotes`)해 최대 `QUOTE_STALE_RETRY_MAX_ATTEMPTS`(3)회, `QUOTE_STALE_RETRY_INTERVAL_SEC`(1초) 간격으로 재검증한다. 신호 확정(`detected_at`) 후 `QUOTE_STALE_MAX_WAIT_SEC`(15초)를 넘기면 더 이상 뒤늦게 주문하지 않고 `MISSED_SIGNAL_QUOTE_STALE`로 신호 원장에 종료·기록하며 `pending_signal`을 남기지 않는다(이후 어떤 tick도 이 signal_id를 뒤늦게 주문하지 않는다). 재시도 도중 fresh quote가 확보되면 그 자리에서 실제 MOCK 주문가능금액 재조회 → 안전수량 계산 → 주문 순서를 그대로 진행한다. 신호 발생 사실(방향)은 주문 성공 여부와 무관하게 오늘 red/blue 통계에 정확히 집계된다.

## 원장

Signal ledger는 `csv.DictWriter`에 컬럼명 기반 dict만 전달한다(위치 기반 list 결합 금지). 새 행을 쓸 때는 항상 실제 디스크 헤더를 다시 읽어 그 순서로 기록해 열 밀림을 방지한다(`ledger._append_row`).

주요 필드:

- `trading_date`
- `completed_bar_at`: 완성된 3분봉 시작시각
- `signal_id`
- `signal_type`
- `direction`
- `detected_at`
- `order_requested_at`
- `order_result`
- `block_reason`
- `strategy_name` (`MACD2`)
- `strategy_version` (현재: `20260727_MACD_CONFIRMED_CROSSOVER_V1`)
- `signal_rule` (완성봉 crossover 규칙, 현재: `MACD_CROSSOVER_CONFIRMED` — `config.SIGNAL_RULE`과 `config.CONFIRMED_SIGNAL_RULE`은 이제 같은 값이다)
- `worker_code_sha`
- `session_started_at`
- `previous_macd` / `previous_signal` / `previous_diff` (2026-07-27 수정: `previous_macd`/`previous_signal`이 항상 빈 값으로 기록되던 버그를 고쳤다 — `MacdSnapshot`에 `previous_macd`/`previous_signal` 필드를 추가하고 `calculate_macd()`가 직전 완성봉의 MACD선/Signal선 값을 채운다)
- `confirmed_macd` / `confirmed_signal` / `confirmed_diff` / `confirmed_direction`
- `quote_ages`
- `position_reconcile`
- `executor_called`
- `broker_called`
- `broker_rt_cd` / `broker_msg_cd` / `broker_msg1`
- `final_result`
- (뒤에 추가) MAJOR 필터 필드: `major_filter_enabled`, `major_filter_version`, `major_score`, `major_required_score`, `major_approved`, `major_decision`, `major_block_reason`, `major_is_reversal`, `major_fast_reversal`, `major_component_scores`, `hist_impulse_atr`, `breakout`, `price_impulse_atr`, `body_atr`, `volume_ratio`, `ema10_ok`, `ema20_or_vwap_ok`, `recent_range_ratio`, `ema_spread_ratio`, `daily_major_entry_count`, `last_major_entry_at` — **기존 컬럼은 삭제·이름변경하지 않고 뒤에만 추가**한다. 과거 행 파싱이 깨지지 않도록 기본값을 둔다.

`strategy_name`/`direction` 값이 알려진 도메인을 벗어나면(예: 컬럼 밀림으로 다른 값이 들어온 경우) 그 행은 삭제·덮어쓰기하지 않고 `MALFORMED_SCHEMA`로 제외 목록에만 표시한다. 오늘 빨강/파랑 통계는 **현재 거래일 + 현재 Worker 세션(`session_started_at` 이후) + 현재 `strategy_version`/`signal_rule`/`worker_code_sha`**의 confirmed 신호만 집계한다. candidate(shadow), 취소된 후보, malformed 행, 이전 세션·구버전·이전 배포 SHA 행은 모두 별도 제외 사유(`OLD_STRATEGY`/`LEGACY_INVALID`/`PRE_SESSION_ROW`/`PRE_SESSION_SIGNAL`/`MALFORMED_SCHEMA`/`OLD_WORKER_SHA`)로 표시하며 통계에 하드코딩된 값(예: 고정 건수)을 사용하지 않는다. **원본 confirmed 플래그 수와 MAJOR 승인·필터 탈락 수는 분리 집계**한다(필터 ON이어도 원본 red/blue 통계는 사라지지 않는다).

Execution ledger는 주문 요청과 체결 결과를 `order_id` 기준으로 dedup한다.

## UI

UI는 Worker state와 ledger summary만 읽는다. UI가 별도 MACD 주문 판단을 하지 않는다. 상태 변경은 service command만 기록한다(Streamlit이 Worker 상태를 직접 수정하거나 주문 함수를 호출하지 않는다).

다음을 각각 분리 표시한다:

- **current candidate** (`state.candidate_flag`, `CANDIDATE_UP_RED`/`CANDIDATE_DOWN_BLUE`) — 진행봉 shadow, 주문권한 없음
- **current confirmed flag** (`state.provisional_flag`) — 이번 tick의 candidate 확정 표시(여전히 shadow)
- **last confirmed onset** (`state.latest_primary_flag` / `state.latest_primary_signal_id`) — 실제 주문권한을 가졌던 마지막 완성봉 Primary
- **order result** (`state.last_broker_order_result` 등) — 가장 최근 브로커 응답. 과거 실패(BUY_FAILED 등)와 현재 `order_block_reason`은 서로 다른 필드로 분리해 혼동을 막는다.
- **주문 sizing**: 실제 주문가능금액, sizing에 사용한 가격, `requested_qty`, 예상 주문금액
- **1분봉 history 진단**: 당일 추가 1분봉 수, history 최신시각, 마지막 완성 3분봉 시각, quote-history 불일치 사유
- **강한 플래그만 거래** 토글 (계좌/제어): OFF/ON. 기본 OFF. `service.set_major_filter_enabled()` command만 기록
- **Major filter 상태**: ON/OFF, `filter_version`, 오늘 MAJOR 승인 진입 수 / 최대 4회, 마지막 MAJOR 승인 시각, `filter_enabled_at`·변경 주체
- **현재 confirmed / MAJOR 판정**: 원본 flag, major score/required, APPROVED 또는 FILTERED_OUT, 핵심 통과·탈락 이유
- **오늘 통계 분리**: 원본 빨간/파란 플래그 수, MAJOR 승인 빨강/파랑 수, 필터 탈락 수, 실제 체결 진입 수
- **필터 탈락 신호 표**: 시간, 방향, score, required, block reason, component scores
- **오늘 전체 신호 개요 (재계산, 참고용)**: `LIVE_CONFIRMED`/`HISTORICAL_REPLAY_ONLY` 건수와 목록 — `service.get_snapshot()["today_signal_overview"]`(`worker.compute_today_signal_overview`)를 그대로 표시만 한다. 주문·필터 평가에는 절대 사용하지 않는다.

`"KIS manual arrows"`처럼 실제 데이터 없이 고정 건수를 표시하는 하드코딩은 금지한다 — 실제 값이 없으면 `-`로 표시한다.

## 검증 기준

필수 테스트는 모두 fake data, fake broker, tmp_path 격리 경로에서 수행한다. 실제 `data/` 파일과 실제 KIS 주문은 사용하지 않는다.

- 전일 마지막 diff 음수, 오늘 첫 완성봉 diff 양수: baseline만 설정, 신호·주문 0건
- 새로 완성된 3분봉 상승 교차 → `UP_RED` 플래그 및 `0193T0` BUY (5초 이내 주문 요청)
- 새로 완성된 3분봉 하락 교차 → `DOWN_BLUE` 플래그 및 `0197X0` BUY
- 진행봉(미완성) 순간 교차는 candidate 확정 여부와 무관하게 주문 0건
- 동일 completed_bar 20회 반복 평가해도 신호·주문 1건
- 반대 교차 시 기존 ETF SELL → 체결 확인 → 잔고 0 → 반대 ETF BUY 순서, 잔고 0 확인 전 BUY 0건
- 관계없는 ETF stale은 차단하지 않고 대상 ETF stale은 차단
- quote 복구 시 같은 `signal_id`로 주문 1회
- `signal_detected_at -> executor_called_at` 5초 이내
- 주문 후 실제 잔고(브로커)와 state.position이 일치
- signal CSV 각 필드가 정확한 헤더 열에 저장됨(디스크 헤더 재정렬 검증 포함)
- 이전 malformed 행은 오늘 통계에서 제외되고 삭제되지 않음
- 세션 이전(구버전/이전 세션) 신호는 오늘 통계에서 제외되고 정상 오늘 신호는 포함됨
- KIS manual arrows 하드코딩 0건
- 주문가능금액이 예산보다 작으면 수량이 안전하게 축소되고 예상 주문금액이 실제 주문가능금액을 넘지 않음
- UI/Worker/리플레이가 같은 Primary 공통 계산 결과 사용
- 본 문서와 `app/trading/macd2/config.py`의 `SIGNAL_RULE`/`STRATEGY_VERSION`이 항상 일치 (`tests/macd2/test_docs_consistency.py`)
- `tests/macd2`와 `compileall` 통과
- MAJOR 필터 OFF이면 기존 `_execute_or_wait` 주문 경로·테스트 결과 불변
- MAJOR 필터 ON이면 승인 신호만 주문, 탈락 신호 broker 호출 0, 동일 `signal_id` 재심사·재주문 0
- Stop Loss / Profit Lock / 강제청산은 필터와 무관하게 기존 규칙 유지
- `tests/macd2/test_major_flag_filter.py` 통과
- read-only 검증 스크립트 `scripts/macd2_validate_major_filter.py`는 운영 state/ledger/cache·broker를 변경하지 않는다
- 13:42~13:44 완성봉의 `completed_bar_at`/`signal_id`는 `13:42:00`/`134200`을 포함하고, `detected_at`/`order_requested_at`은 13:45 이후
- 진행봉(forming) candidate는 몇 번을 재확인(shadow confirm)하든 broker/`order_executor`/MAJOR 필터/원장 호출 0건 — 오직 완성봉 Primary만 주문
- 1분봉 3개 중 하나라도 빠진 3분봉은 confirmed 목록에서 제외되고(`HISTORY_GAP`) 그 봉에 대한 신호·MAJOR 평가·주문 0건이며, 이후 해당 1분봉이 채워지면 같은 봉이 정상 재평가됨
- Worker 재시작 시 재시작 이전에 이미 완성된 봉은 재주문되지 않고, 재시작 이후 첫 신규 완성봉은 정상 신호가 됨
- 오늘 전체 신호 재계산(`compute_today_signal_overview`)이 세션 시작 이전 봉을 `HISTORICAL_REPLAY_ONLY`, 이후 봉을 `LIVE_CONFIRMED`로 정확히 분리하며 order_executor/MAJOR 필터를 호출하지 않음
- 다른 `worker_code_sha`로 기록된 신호는 오늘 current 통계에서 제외되고(`OLD_WORKER_SHA`) "과거/제외 신호" 패널에만 표시되며, 원장 원본 행은 변경되지 않음

## Optional Hybrid MAJOR_FLAG filter (강한 플래그만 거래)

선택형 주문권한 게이트다. **전략 버전(`STRATEGY_VERSION`)을 바꾸지 않고** 별도 `major_filter_version = MAJOR_FILTER_HYBRID_V1`로 관리한다. 필터 ON/OFF가 과거 원장의 기존 confirmed 신호를 변경하지 않는다.

### 목표와 불변 조건

- 기존 confirmed MACD 플래그는 지금처럼 **전부 정확히 생성·기록**한다.
- UI에서 강한 플래그 필터가 **OFF**이면 기존 주문 흐름을 **100% 유지**한다.
- 필터가 **ON**이면 confirmed 플래그 중 강도가 높은 MAJOR_FLAG만 주문권한을 가진다.
- `signal_engine`의 MACD 공식·플래그 시각, 주문·체결·손절 로직은 변경하지 않는다.
- 특정 날짜·시각·방향을 코드에 하드코딩하지 않는다.
- 짧은 왕복 교차를 줄이고 큰 흐름을 남기는 Hybrid 점수 필터를 사용한다.

### UI 제어

- 표시명: **강한 플래그만 거래**
- OFF: 기존 confirmed 신호가 모두 주문권한 보유
- ON: MAJOR_FLAG 승인 신호만 주문권한 보유
- 기본값 OFF. 환경변수 `MACD2_MAJOR_FILTER_DEFAULT=false`
- UI는 `Macd2Service.set_major_filter_enabled()` command만 기록한다. Streamlit이 Worker 상태를 직접 수정하거나 주문 함수를 호출하지 않는다.
- 전략 실행 중 토글 변경: **다음 신규 confirmed 플래그부터** 적용. 이미 보유한 포지션의 Stop Loss·Profit Lock·강제청산에는 영향 없음. 토글 변경 시 기존 position을 즉시 청산하거나 신규 매수하지 않는다. `major_filter_enabled_at`과 변경 주체(`major_filter_enabled_by`)를 state에 기록한다.

### 입력 데이터와 미래 데이터 금지

필터는 confirmed 플래그가 발생한 **완성 3분봉까지의 데이터만** 사용한다.

허용 입력:

- 현재 플래그가 발생한 완성 3분봉과 그 이전 완성 3분봉
- 현재 실제 position 상태, 당일 승인 진입 횟수, 마지막 실제 진입시각

금지:

- 플래그 이후 봉, 이후 고가·저가·수익률, ETF 미래 가격, 차트 사후 모양
- 현재 진행봉, live quote를 과거 완성봉 지표에 삽입
- 선택된 시각 하드코딩

필요 최소 데이터: `open/high/low/close/volume`, 최소 26개 이상 완성 3분봉, ATR14, MACD/Signal/Histogram, EMA10, EMA20, 당일 정규장 VWAP(전일 거래량·가격 혼합 금지).

데이터 부족 또는 NaN/ATR 0/volume median 0이면:

- 기존 confirmed 플래그는 그대로 기록
- MAJOR_FLAG는 거절 (`FILTER_DATA_INSUFFICIENT`)
- 필터 OFF이면 기존 주문은 정상 진행

지표 계산은 `app/trading/macd2/major_flag_filter.py` 안의 **순수 함수**로만 수행한다. `signal_engine.py`에 지표를 끼워 넣지 않는다. 입력 DataFrame을 수정하지 않으며, 동일 입력 → 동일 출력이다.

### Hybrid 점수 (최대 100)

`evaluate_major_flag(bars_3m, flag_direction, position_direction, last_entry_at, daily_major_entry_count, now) -> MajorFlagDecision`

반환: `approved`, `score`, `required_score`, `decision`, `reasons`, `component_scores`, `metrics`, `is_reversal`, `fast_reversal`, `block_reason`.

방향: `UP_RED` = +1, `DOWN_BLUE` = -1.

기본 확인: 마지막 두 완성봉이 실제 confirmed crossover 조건을 만족하는지 검증. 불만족 시 `FILTER_INPUT_NOT_CROSSOVER`. 이 함수는 새 플래그를 생성하지 않고 기존 confirmed 신호만 평가한다.

| 항목 | 배점 | 조건 |
|------|------|------|
| A. Histogram impulse | 25 | `hist_impulse_atr = direction × (curr_hist − prev_hist) / ATR14` — `≥0.10→10`, `≥0.15→18`, `≥0.22→25` |
| B. 가격 강도 | 25 | 4봉 돌파이면 25. 아니면 impulse — `≥0.35 ATR→15`, `≥0.55 ATR→25` |
| C. 캔들 몸통 | 10 | 방향 일치 캔들 — `body_atr ≥0.25→5`, `≥0.40→10` |
| D. 거래량 | 15 | `volume_ratio` — `≥1.00→5`, `≥1.10→10`, `≥1.20→15` (현재 봉은 중앙값 제외) |
| E. EMA10 추세 | 10 | UP: EMA10↑ & close>EMA10 / DOWN: EMA10↓ & close<EMA10 |
| F. EMA20 또는 VWAP | 10 | UP: close>EMA20 또는 close>당일 VWAP / DOWN: close<EMA20 또는 close<당일 VWAP |
| G. 변동성 | 5 | 최근 8봉 range/close ≥ `0.006` **또는** 현재 ATR14 ≥ 직전 20개 ATR14 중앙값 |

설정 기본값(`config.py`, 하드코딩 금지 — 필터 함수는 config 사용):

- `MAJOR_ENTRY_SCORE_MIN=65`, `MAJOR_REVERSAL_SCORE_MIN=75`, `MAJOR_FAST_REVERSAL_SCORE_MIN=82`
- hist/price/body/volume 구간은 `MAJOR_*_T1/T2/T3` 상수
- `MAJOR_SIDEWAYS_EMA_SPREAD_MAX=0.0007`, `MAJOR_SIDEWAYS_RANGE_MAX=0.006`
- `MAJOR_RANGE_BREAKOUT_LOOKBACK=4`, `MAJOR_RECENT_RANGE_LOOKBACK=8`, `MAJOR_VOLUME_LOOKBACK=20`

### 승인 기준

- 신규 진입(flat): `required_score = 65`
- 반대 포지션 전환: `required_score = 75`
- 직전 실제 진입 후 15분 이내 반대 전환: `required_score = 82`, `fast_reversal=True`
- 필수 가격조건(하나 이상): 4봉 돌파 **또는** price impulse ≥ 0.35 ATR **또는** EMA20 방향 일치 **또는** VWAP 방향 일치
- 횡보 차단(동시 만족 시 거절): `ema_spread = |EMA10−EMA20|/close < 0.0007` **그리고** 최근 8봉 range/close < `0.006`

결과 라벨:

- 승인: `MAJOR_APPROVED`
- 점수 미달: `MAJOR_SCORE_BELOW_THRESHOLD`
- 필수 가격조건 미달: `MAJOR_PRICE_CONFIRMATION_FAILED`
- 횡보: `MAJOR_SIDEWAYS_BLOCK`
- 데이터 부족: `FILTER_DATA_INSUFFICIENT`
- 입력 비교차: `FILTER_INPUT_NOT_CROSSOVER`

### 거래 횟수와 추가매수 방지

설정: `MAJOR_MAX_DAILY_ENTRIES=4`, `MAJOR_MIN_HOLD_MIN=9`, `MAJOR_FAST_REVERSAL_WINDOW_MIN=15`, `MAJOR_SAME_DIRECTION_REENTRY_MIN=18` (환경변수 `MACD2_MAJOR_*`로 기본값 덮어쓰기 가능).

1. 같은 방향 포지션 보유 중 같은 방향 플래그: 추가매수 금지 → `SAME_DIRECTION_POSITION_HELD`
2. 하루 승인 신규진입 4회 도달 후 신규 BUY 금지 → `MAJOR_DAILY_ENTRY_LIMIT` (Stop Loss·Profit Lock·강제청산은 계속 작동)
3. 같은 방향 재진입: 마지막 같은 방향 청산 후 18분 이내 금지 → `MAJOR_SAME_DIRECTION_COOLDOWN`
4. 최소 보유 9분: 작은 반대 confirmed로는 전환하지 않음. 반대 점수 ≥ 82이면 9분 이내에도 강한 반전 전환 허용. Stop Loss는 시간과 무관 즉시. Profit Lock·강제청산은 기존 규칙 우선
5. `daily_major_entry_count`는 **실제 BUY 체결수량 > 0일 때만** 증가. 플래그 승인만·주문 거절·미체결로는 증가 금지

점수 승인 후 거래 게이트는 `apply_major_trade_gates()`로 적용한다.

### Worker 연결 위치

필터 판단은 주문 전 **한 곳**에서만 수행한다(`_dispatch_confirmed_signal` 내부, `_execute_or_wait` 직전). 주문·체결 함수 내부에 필터를 넣지 않는다.

```text
confirmed crossover 생성
→ 기존 signal ledger 기록(원본 플래그 유지)
→ [필터 ON] evaluate_major_flag + trade gates → 원장/state 기록
→ approved=True 일 때만 _execute_or_wait
→ approved=False 이면 broker 호출 없이 FILTERED_OUT + processed_signal_ids 등록
```

필터 OFF:

```text
confirmed crossover 생성 → 기존과 동일하게 _execute_or_wait
```

반대신호:

- 필터 OFF: 기존 반대신호 전환 유지
- 필터 ON: 반대 confirmed도 major 승인 시에만 기존 SELL→잔고0→BUY. 탈락 반대신호는 포지션 유지. Stop Loss·Profit Lock 조건이 발생하면 필터와 무관하게 청산

pending 재시도는 최초 승인(또는 필터 OFF) 당시 게이트를 이미 통과한 것으로 보고 재필터하지 않는다.

`HISTORY_GAP`(1분봉 공백)으로 차단된 봉과 `HISTORICAL_REPLAY_ONLY`로 분류되는 Worker 세션 시작 이전 봉은 애초에 `_advance_confirmed_primary`/`_dispatch_confirmed_signal` 호출 경로에 들어오지 않으므로 MAJOR 필터 평가·주문 호출이 구조적으로 0건이다 — 별도의 필터 예외 처리가 필요 없다.

### 모델·state

`MajorFlagDecision` dataclass를 `models.py`에 둔다. runtime state에 최소 다음을 저장한다:

`major_filter_enabled`, `major_filter_enabled_at`, `major_filter_enabled_by`, `major_filter_version`, `daily_major_entry_count`, `last_major_entry_at`, `last_major_exit_at`, `last_major_exit_direction`, 및 직전 판정 필드(`last_major_score` 등).

거래일 롤오버 시 `daily_major_entry_count`는 0으로 리셋한다. 토글(`major_filter_enabled`)은 유지한다.

### 검증 스크립트

`scripts/macd2_validate_major_filter.py` — 지정 일자의 000660 1분봉에 대해 confirmed 신호에 필터를 적용하는 **read-only** 검증.

- 입력 예: `data/validation/macd2_parity`
- 출력: `data/validation/major_filter/all_flags_scored.csv`, `approved_flags.csv`, `summary.json`
- 주문·broker 호출 금지, 운영 state·ledger·cache 변경 금지
- 참고용 목표 거래 시각(라벨일 뿐 코드 정답 하드코딩 아님)과의 승인·탈락 차이를 숨기지 않고 보고한다. 파라미터를 반복 조정해 과최적화하지 않는다.

### 수정 범위 / 금지

허용(최소 수정): `major_flag_filter.py`(신규), `config.py`, `models.py`, `worker.py`, `state_store.py`, `ledger.py`, `service.py`, UI, 본 문서, `tests/macd2/test_major_flag_filter.py`, 검증 스크립트.

수정 금지: `signal_engine.py`의 MACD·confirmed crossover, `market_data.py` 기존 수집·봉 생성, `broker_adapter.py`, `order_executor.py`, `kis_client.py` 주문 함수, BUY/SELL 수량 산식, 체결 polling·잔고 동기화, STOP_LOSS, PROFIT_LOCK, 강제청산, REAL gate, 다른 전략 모듈, 광범위 리팩토링.

## 금지 사항

- MACD 12/26/9 파라미터 변경 금지
- ETF 방향 변경 금지
- SL, Profit Lock, 14:55, 15:00 청산 변경 금지
- MACD v1 수정 금지
- 운영 `data/` 파일 수정 금지
- 실제 KIS 주문 테스트 금지
- 새 프레임워크 도입 또는 대규모 리팩토링 금지
- `main` 브랜치 푸시 금지. `main-MACD2`에만 커밋·푸시한다.
- MAJOR 필터용 날짜·시각·방향 하드코딩 금지
- 필터 ON이 confirmed 플래그 생성 수·시각·방향을 바꾸게 하는 변경 금지
- 필터를 주문·체결 함수 내부에 넣는 변경 금지
- Stop Loss / Profit Lock / 강제청산을 필터에 종속시키는 변경 금지
