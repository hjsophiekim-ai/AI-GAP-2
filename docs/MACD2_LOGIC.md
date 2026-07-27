# MACD2 Logic

본 문서는 독립 모듈 `app/trading/macd2/`의 현재 운용 기준이다(2026-07-27 KIS-parity 개정). MACD v1, Enhanced 전략과 파일·상태·원장을 공유하지 않는다.

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
- 완성 3분봉은 09:00 기준 3분 경계로 이 누적 1분봉 이력에서 리샘플한다: 09:00~09:02, 09:03~09:05, ...
- 전일 데이터는 EMA warm-up에만 사용한다.
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

직전 완성 3분봉의 diff와 새로 완성된 3분봉의 diff를 비교한다.

- 이전 완성봉 diff `<= 0`이고 새 완성봉 diff `> 0`: `UP_RED`
- 이전 완성봉 diff `>= 0`이고 새 완성봉 diff `< 0`: `DOWN_BLUE`
- 그 외: `HOLD`

**새 completed_bar timestamp가 생길 때마다 정확히 1회만 평가한다** (`worker._advance_confirmed_primary`). 같은 완성봉을 가리키는 반복 tick은 재평가하지 않고 항상 `HOLD`를 반환해 동일 봉 중복주문을 원천 차단한다.

**장 시작 후 이 상태가 평가한 첫 완성 3분봉(또는 직전 평가 봉과 날짜가 다른 최초 봉)은 baseline만 설정하고 주문하지 않는다.** 그 봉의 이전 diff는 전일(또는 그 이전) 마지막 완성봉에서 온 값이라 갭만으로 교차가 발생할 수 있기 때문이다. 이 baseline 평가는 방향 억제 상태(`last_detected_direction`)에도 반영하지 않으므로, 이후 실제 당일 교차는 정상적으로 신호가 된다.

Primary 계산은 `app.trading.macd2.signal_engine.evaluate_macd_crossover()` + `calculate_macd()`를 공통 함수로 사용한다. Worker, UI 상태, 리플레이, 테스트는 반드시 이 결과에서 나온 MACD, Signal, diff, direction, `signal_id`를 같은 의미로 해석해야 한다.

## Candidate/Shadow — 진행봉과 Signed-B는 주문권한이 없다

- **진행 중(forming) 3분봉의 provisional MACD/diff**: 최신 유효 `000660` quote로 매 tick 갱신되는 진단값이다. `evaluate_primary_forming_crossover()`로 계산하되, 그 결과는 UI candidate 표시(`state.candidate_flag`, `state.provisional_flag`)에만 쓰인다. 같은 방향이 서로 다른 fresh quote tick에서 최소 `PROVISIONAL_CONFIRM_MIN_GAP_SEC`(3초) 이상 간격을 두고 2회 유지되면 "confirmed candidate"로 표시하지만, 이 확정도 여전히 shadow일 뿐 주문·원장·통계·`last_direction`에는 절대 반영하지 않는다.
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
9. 신규 Primary 방향이면 즉시:
   1. 주문 직전 KIS 실제 주문가능금액 재조회
   2. `min(UI 예산, 실제 주문가능금액)`에서 수수료·1틱 안전여유를 뺀 안전 수량 계산
   3. (반대신호인 경우) 기존 ETF SELL → 체결 확인 → 실제 잔고 0 확인
   4. 신규/반대 ETF BUY 요청
   5. 주문번호로 체결내역 조회 — 주문 접수 응답만으로 체결 확정하지 않는다. 최대 `ORDER_FILL_POLL_MAX_SEC`(60초) 동안 폴링하며 부분체결도 실제 체결수량 그대로 반영한다.
   6. 체결 확인 후 실제 잔고를 다시 조회해 종목·수량·평균단가를 state에 반영
10. quote 또는 position 일시 오류면 같은 `signal_id`를 `pending_signal`로 유지하고 최대 30초 재시도
11. 주문 요청이 실제 생성되면 `processed_signal_ids`에 등록
12. state 저장

진행봉 crossover는 8-9의 Primary 판단에 전혀 관여하지 않는다 — 오직 완성봉만 주문을 만든다.

## 주문 및 위험관리

- `UP_RED` + flat: `0193T0` BUY
- `DOWN_BLUE` + flat: `0197X0` BUY
- 반대 신호 + 보유: 기존 ETF SELL → 체결 확인 → 잔량 0 확인 → 반대 ETF BUY
- 14:55 이후 신규 진입 금지
- 15:00 이후 강제청산 우선
- Stop Loss(-1.5%)와 Profit Lock(+1.5% 활성화, 0.8%p 반납 청산)은 기존 규칙을 유지한다.
- 주문 수량은 `min(UI 예산, KIS 실제 주문가능금액)`을 기준으로, 수수료·1틱 안전여유를 뺀 뒤 정수로 계산한다(`order_executor.compute_order_quantity`). 실제 주문가능금액은 대상 종목(`0193T0`/`0197X0`) 기준으로 매 주문 직전 재조회한다(계좌 전체 기준 조회가 아님).
- 주문가능금액 부족 시 주문하지 않고 KIS의 실제 코드·메시지를 신호 원장에 그대로 기록한다. 같은 `signal_id`로 무한 재시도하지 않는다(signal_id 단발성 원칙).
- 체결은 주문 성공 응답만으로 확정하지 않고, 주문번호 기준 실제 체결/잔고 재조회로 확인한다(최대 60초 폴링, 부분체결 반영).
- MOCK/REAL 게이트는 broker adapter와 기존 service 경로를 따른다. REAL 주문, 신용, 미수는 사용하지 않는다.
- 실제 KIS 주문은 명시된 운영 모드에서만 허용한다. 테스트는 fake broker만 사용한다.

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

`strategy_name`/`direction` 값이 알려진 도메인을 벗어나면(예: 컬럼 밀림으로 다른 값이 들어온 경우) 그 행은 삭제·덮어쓰기하지 않고 `MALFORMED_SCHEMA`로 제외 목록에만 표시한다. 오늘 빨강/파랑 통계는 **현재 거래일 + 현재 Worker 세션(`session_started_at` 이후) + 현재 `strategy_version`/`signal_rule`**의 confirmed 신호만 집계한다. candidate(shadow), 취소된 후보, malformed 행, 이전 세션·구버전 행은 모두 별도 제외 사유(`OLD_STRATEGY`/`LEGACY_INVALID`/`PRE_SESSION_ROW`/`PRE_SESSION_SIGNAL`/`MALFORMED_SCHEMA`)로 표시하며 통계에 하드코딩된 값(예: 고정 건수)을 사용하지 않는다.

Execution ledger는 주문 요청과 체결 결과를 `order_id` 기준으로 dedup한다.

## UI

UI는 Worker state와 ledger summary만 읽는다. UI가 별도 MACD 주문 판단을 하지 않는다.

다음을 각각 분리 표시한다:

- **current candidate** (`state.candidate_flag`, `CANDIDATE_UP_RED`/`CANDIDATE_DOWN_BLUE`) — 진행봉 shadow, 주문권한 없음
- **current confirmed flag** (`state.provisional_flag`) — 이번 tick의 candidate 확정 표시(여전히 shadow)
- **last confirmed onset** (`state.latest_primary_flag` / `state.latest_primary_signal_id`) — 실제 주문권한을 가졌던 마지막 완성봉 Primary
- **order result** (`state.last_broker_order_result` 등) — 가장 최근 브로커 응답. 과거 실패(BUY_FAILED 등)와 현재 `order_block_reason`은 서로 다른 필드로 분리해 혼동을 막는다.
- **주문 sizing**: 실제 주문가능금액, sizing에 사용한 가격, `requested_qty`, 예상 주문금액
- **1분봉 history 진단**: 당일 추가 1분봉 수, history 최신시각, 마지막 완성 3분봉 시각, quote-history 불일치 사유

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

## 금지 사항

- MACD 12/26/9 파라미터 변경 금지
- ETF 방향 변경 금지
- SL, Profit Lock, 14:55, 15:00 청산 변경 금지
- MACD v1 수정 금지
- 운영 `data/` 파일 수정 금지
- 실제 KIS 주문 테스트 금지
- 새 프레임워크 도입 또는 대규모 리팩토링 금지
- `main` 브랜치 푸시 금지. `main-MACD2`에만 커밋·푸시한다.
