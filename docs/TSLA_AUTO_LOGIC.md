# TSLA_AUTO Logic

## US Market Session Policy

This section supersedes older fixed-time notes in this document, including
fixed 15:45/15:50 ET cutoffs and fixed KST open/close windows.

TSLA_AUTO uses `app.trading.tsla_auto.market_session.get_us_market_state()` as
the single source of truth for US market hours, holidays, daylight saving time,
entry cutoff, and forced liquidation timing. Worker, order executor, service,
and UI must consume the same `USMarketSessionState` object and must not
recalculate market phases independently.

- Market timezone: `America/New_York`
- Korean UI timezone: `Asia/Seoul`
- Time conversion: Python `zoneinfo.ZoneInfo`
- Trading day, holiday, and early close source: `pandas_market_calendars` NYSE calendar
- New entry cutoff: actual `session_close - 15 minutes`
- Forced liquidation: actual `session_close - 10 minutes`
- Calendar/API failure: fail closed; new BUY is blocked

KST 22:30/23:30 examples are display references only. They are never used as
trading logic. See `docs/US_MARKET_SESSION_POLICY.md`.

본 문서는 신규 모듈 `app/trading/tsla_auto/`(예정)의 기술 로직 기준이다. `docs/MACD2_LOGIC.md`의
구조·원칙을 최대한 유지하되, 미국 정규장 시간대와 KIS 해외주식 API에 맞게 재작성했다.
MACD2, MACD v1, Enhanced와 파일·상태·원장·Worker·Service를 공유하지 않는다(§11).

**이 문서 작성 시점 기준으로 코드는 아직 없다.** 아래는 구현 시 따라야 할 설계 명세이며, 확인
안 된 KIS 해외 API 세부사항은 전부 `KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`로 명시했다.

---

## 목적

TSLA_AUTO는 TSLA(나스닥) 미국 정규장 3분봉 MACD(12,26,9) crossover를 신호로 삼아, 방향에 따라
`TSLL`(상승 레버리지) 또는 `TSLZ`(하락 인버스 레버리지)를 매매한다. TSLA 자체는 매수하지 않는다.

MACD2와 동일하게: **신호 계산의 단일 원본은 KIS가 제공하는 당일 1분봉이다.** 실시간 quote만으로
가상의 1분봉 이력을 만들지 않는다. 진행 중(미완성) 3분봉은 UI shadow 표시 전용이며, 주문·통계·
`last_direction` 판단에는 어떤 경우에도 사용하지 않는다.

---

## 종목과 방향

| 역할 | 종목 | 설명 |
|---|---|---|
| 신호 원천 | `TSLA` | 직접 매매하지 않음 — MACD 계산 전용 |
| UP_RED 매수 대상 | `TSLL` | TSLA 상승 방향 레버리지 ETF |
| DOWN_BLUE 매수 대상 | `TSLZ` | TSLA 하락 방향 인버스 레버리지 ETF |

- 반대 ETF 보유 중 반대 신호: 기존 ETF 전량 SELL → 체결 확인 → 실제 잔고 0 확인 → 반대 ETF
  BUY 순서를 강제한다. 매도 잔량이 남아있으면 반대매수를 절대 하지 않는다(MACD2와 동일 원칙).
- `TSLL`과 `TSLZ` 동시 보유 금지, 미국 정규장만 거래, 오버나이트 금지, Stop Loss·Profit Lock·
  15:40/15:45 ET 신규진입 금지·15:50 ET 강제청산·MOCK/REAL 게이트는 본 문서의 고정 규칙이다.

---

## 데이터 — KIS 당일 1분봉이 단일 원본

- `TSLA` 1분봉은 KIS 해외주식분봉조회(TR `HHDFS76950200`, §KIS 해외주식 API)로 주기적으로
  갱신하며, 기존 warm-up 이력에 **append → datetime 기준 dedup(keep last) → sort**한 뒤
  저장한다(MACD2 `MarketDataService.merge_incremental_1m`과 동일 패턴). Worker 자신은 KIS를
  직접 호출하지 않고 이 캐시만 읽는다.
- 완성 3분봉은 **America/New_York 09:30 기준** 3분 경계로 이 누적 1분봉 이력에서 리샘플한다:
  09:30~09:32, 09:33~09:35, ... (`label="left", closed="left"` — 봉 이름/`signal_id`는 항상
  그 봉의 **시작시각(ET)**이다).
- 전일 데이터는 EMA warm-up에만 사용한다. 미국 전일 거래일 계산은 미국 공휴일 캘린더를
  따른다(§미국시장 캘린더 — `KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`: KIS API가 미국 휴장일을
  직접 알려주는지, 아니면 이 저장소가 별도 캘린더를 유지해야 하는지 확인 필요).
- **1분봉 완전성 게이트(MACD2 2026-07-31 수정과 동일 원칙)**: 3개 완성 1분봉이 모두 유효한
  `open`/`close`와 함께 존재할 때만 그 3분봉을 confirmed로 취급한다. API 오류·중간 빈 페이지로
  1분봉 한 개라도 비면 해당 3분봉은 confirmed 목록에서 제외하고(임의 보정·보간 금지),
  `order_block_reason="HISTORY_GAP"`으로 그 시점의 신호 평가·MAJOR 필터·주문을 전부 차단한다.
  `last_confirmed_bar_ts`는 이때 전진하지 않으므로, 이후 incremental merge로 누락 1분봉이
  채워지면 같은 봉이 정상 재평가된다.
- 진행봉과 현재가(quote)를 confirmed MACD 계산에 사용하지 않는다.
- 주문에 필요한 quote는 `price > 0`이고 `age_sec <= QUOTE_MAX_AGE_SEC`(초기값 10초, MACD2와
  동일)이어야 한다. flat `UP_RED`: `TSLA`, `TSLL` quote 필요. flat `DOWN_BLUE`: `TSLA`, `TSLZ`
  quote 필요. 스위칭: `TSLA`, 현재 보유 ETF, 신규 매수 ETF quote 필요. 관계없는 ETF stale만으로
  주문을 차단하지 않는다(MACD2와 동일).

---

## MACD 계산

- MACD: EMA 12 − EMA 26. Signal: MACD의 EMA 9. `adjust=False`.
- **주문권한이 있는 계산은 완성된 3분봉만 사용한다.** 진행 중 3분봉 값은 UI shadow/candidate
  표시에만 쓰이며 이 계산에 절대 섞이지 않는다.
- MACD2와 완전히 동일한 순수 함수 로직(`calculate_macd`)을 이 모듈 전용으로 복제한다 — import로
  공유하지 않는다(§11 MACD2와 완전 분리, `TSLA_AUTO_COPY_MAP.md` §signal_engine.py).

---

## Primary 신호 — 완성봉 MACD crossover만이 주문권한을 가진다

실제 주문권한이 있는 Primary 신호는 **새로 완성된 3분봉의 MACD(12,26,9) crossover** 하나뿐이다.

- 이전 완성봉 diff `<= 0`이고 새 완성봉 diff `> 0`: `UP_RED`
- 이전 완성봉 diff `>= 0`이고 새 완성봉 diff `< 0`: `DOWN_BLUE`
- 그 외: `HOLD`

**새 completed_bar timestamp가 생길 때마다 정확히 1회만 평가한다.** 같은 완성봉을 가리키는
반복 tick은 재평가하지 않고 항상 `HOLD`를 반환해 동일 봉 중복주문을 원천 차단한다.

**미국 정규장 시작 후 이 상태가 평가한 첫 완성 3분봉(또는 직전 평가 봉과 거래일이 다른 최초
봉)은 baseline만 설정하고 주문하지 않는다.** 그 봉의 이전 diff는 전일(또는 그 이전) 마지막
완성봉에서 온 값이라 갭만으로 교차가 발생할 수 있기 때문이다(MACD2와 동일 원칙 — 조기폐장일
다음 거래일의 첫 봉도 동일하게 처리된다).

선택형 **강한 플래그 필터(MAJOR_FLAG)**가 OFF이면 이 confirmed crossover가 곧 주문권한이다.
ON이면 confirmed crossover는 여전히 전부 생성·기록되지만, 주문권한은 Hybrid 점수 승인
(`MAJOR_APPROVED`)된 신호에만 부여된다(§강한 플래그 필터).

## Candidate/Shadow — 진행봉은 주문권한이 없다

- 진행 중(forming) 3분봉의 provisional MACD/diff는 최신 유효 `TSLA` quote로 매 tick 갱신되는
  진단값이며, UI candidate 표시에만 쓰인다.
- **MACD2가 2026-07-31에 겪은 회귀를 처음부터 방지한다**: TSLA_AUTO의 진행봉/candidate 경로는
  설계 단계부터 broker/order_executor/MAJOR 필터/`processed_signal_ids`/signal ledger 중
  어느 것도 호출하지 못하도록 만든다. 오직 완성봉 Primary crossover만 주문권한을 가진다 —
  진행봉 fast-path("candidate가 실제 주문까지 한다")를 나중에 다시 추가하지 않는다.
- 진행봉 provisional 값은 오늘 빨강/파랑 통계, Primary 플래그 수, `last_direction`, signal
  ledger 어디에도 절대 포함하지 않는다.

---

## signal_id와 중복 차단

Primary(확정) `signal_id` 형식:

```text
YYYYMMDD_HHMMSS_DIRECTION
```

`YYYYMMDD_HHMMSS`는 **ET 기준** 새로 완성된 3분봉의 시작시각이다(예: `20260730_104200_UP_RED`).
KST가 아니라 ET를 기준으로 하는 것이 MACD2와의 유일한 시각 관련 차이다 — 두 문서 모두 "봉
시작시각 기준"이라는 규칙 자체는 동일하다.

같은 완성봉에서 몇 번을 반복 평가해도 플래그와 주문은 최초 onset 1회만 생성한다: 봉-once
게이트, `processed_signal_ids`, signal ledger `signal_id` dedup이 3중으로 중복주문을 막는다
(MACD2와 동일 구조). 동일 `signal_id`는 Worker 재시작 후에도 재주문하지 않는다.

시간 필드는 다음을 모두 별도로 기록하고 서로 혼용하지 않는다: `bar_start_at`(=`flag_time`),
`bar_end_at`, `evaluated_at`, `detected_at`, `order_requested_at`. 각 필드는 ET·KST를 함께
표시한다(예: `bar_start_at_et`/`bar_start_at_kst`).

---

## Worker 흐름

Worker는 5초 tick으로 동작한다(MACD2와 동일 주기 — 필요 시 조정 가능하나 초기값은 동일하게
둔다).

1. state 로드
2. position reconcile(실제 계좌와 항상 비교)
3. quote cache 읽기(TSLA/TSLL/TSLZ)
4. history cache 읽기(KIS 당일 1분봉 — history-updater 스레드가 별도로 갱신)
5. 완성 3분봉 resample 및 confirmed MACD 계산(1분봉 완전성 게이트 통과분만)
6. 당일 1분봉 수/history 최신시각/quote-history 단위·시각 불일치 진단 갱신
7. 진행봉 provisional MACD 갱신(shadow 표시 전용)
8. 새 완성봉 여부 판정 → Primary direction 산출(봉당 정확히 1회)
9. 신규 Primary 방향이면:
   1. 신호 원장에 confirmed 플래그 기록(필터 ON/OFF와 무관 — 원본 플래그 수·시각·방향 유지)
   2. **강한 플래그 필터가 ON이면** Hybrid 채점 → 거래 게이트 적용 → 승인일 때만 아래 주문
      단계로 진행. 탈락이면 `FILTERED_OUT`으로 종료하고 broker 호출 0
   3. **[신규] 손절 후 재진입 쿨다운 게이트**(§손절·Profit Lock·전환) — 필터 ON/OFF와
      무관하게 항상 평가
   4. 필터 OFF/승인/쿨다운 예외 통과 시: 주문 직전 KIS 실제 USD 주문가능금액 재조회
   5. `min(UI 예산, 실제 주문가능금액)`에서 수수료·1틱 안전여유를 뺀 안전 수량 계산
   6. (반대신호인 경우) 기존 ETF SELL → 체결 확인 → 실제 잔고 0 확인
   7. 신규/반대 ETF BUY 요청(일반 지정가)
   8. 주문번호로 체결내역 조회 — 주문 접수 응답만으로 체결 확정하지 않음. 최대
      `ORDER_FILL_POLL_MAX_SEC`(초기값 60초) 동안 폴링하며 부분체결도 실제 체결수량 그대로
      반영
   9. 체결 확인 후 실제 잔고를 다시 조회해 종목·수량·평균단가를 state에 반영
10. quote 또는 position 일시 오류면 같은 `signal_id`를 `pending_signal`로 유지하고 재시도
11. 주문 요청이 실제 생성되면(또는 필터/쿨다운 탈락으로 소비되면) `processed_signal_ids`에 등록
12. state 저장

진행봉 crossover는 8-9의 Primary 판단에 전혀 관여하지 않는다.

---

## 손절·Profit Lock·전환

> 이 절은 사용자 확정 지시에 따른 최종 사양이다. MACD2 대비 **신규 추가된 규칙**은 (신규)로
> 표시했다.

- 실제 TSLL/TSLZ 평균체결가 대비 **-1.5% 손절** 유지.
- 실제 ETF 실시간 가격으로 감시(quote 캐시가 아니라 판단 시점의 fresh 가격).
- 기존 Profit Lock 활성화 기준과 giveback을 코드에서 읽어 그대로 유지.
- 손절·Profit Lock·강제청산은 강한 플래그 필터보다 우선한다.
- **(신규) 손절 후 같은 방향 재진입 15분 금지.**
- **(신규) 그 이후 새 confirmed 플래그이며 점수 `>= max(85, 현재 문턱)`일 때, 하루 1회만 같은
  방향 재진입을 허용한다.**
- 반대 전환은 기존 ETF 매도 체결 → 잔고 0 확인 → 반대 ETF 매수 순서를 유지한다.
- **(신규) 15:45 ET 이후에는 신규진입과 반대 ETF 신규매수를 전부 금지한다.** 기존 보유 ETF의
  매도/청산은 계속 허용된다.

### 값 표(MACD2 실제값 → TSLA_AUTO 초기 복제값)

| 항목 | MACD2 상수(`app/trading/macd2/config.py`) | 실제값 | TSLA_AUTO 초기값 |
|---|---|---|---|
| Stop Loss | `STOP_LOSS_NET_PCT` | `-1.5` | `-1.5` (동일) |
| Profit Lock 활성화 | `PROFIT_LOCK_ACTIVATE_NET_PCT` | `+1.5` | `+1.5` (동일) |
| Profit Lock giveback | `PROFIT_LOCK_GIVEBACK_PP` | `0.8` | `0.8` (동일) |

### 신규 게이트 설계(코드 미작성, 명세만)

- 신규 state 필드: `last_stop_loss_exit_at`(ISO, direction 포함), `stop_loss_cooldown_direction`,
  `stop_loss_reentry_override_used_today`(bool, 거래일 롤오버 시 리셋 — 다른 daily 카운터와
  동일 패턴).
- 상수(가칭): `STOP_LOSS_REENTRY_COOLDOWN_MIN = 15`,
  `STOP_LOSS_REENTRY_OVERRIDE_SCORE_MIN = 85`(고정 85가 아니라 그 시점 신규진입/반대전환/
  빠른반전 문턱 중 적용되는 값과 `max()`로 비교 — 현재 MACD2 기본값 기준 65/75/82 모두 85보다
  작으므로 실질적으로 항상 85가 적용되지만, 문턱값이 나중에 바뀌면 자동으로 따라간다).
- 이 게이트는 "강한 플래그만 거래" 토글이 OFF여도 **손절 직후 15분 동안만** 독립적으로 항상
  평가한다(major_flag_filter의 순수 채점 함수는 재사용하되, 승인 여부 자체는 이 별도 쿨다운
  로직이 최종 판단한다). 필터가 이미 ON이어서 정상 흐름으로 채점되는 신호는 이 쿨다운 게이트가
  이중으로 막지 않도록, 손절 직후 15분 이내인지 여부만 추가로 검사한다.
- 새 block_reason(가칭): `STOP_LOSS_REENTRY_COOLDOWN`, `STOP_LOSS_REENTRY_OVERRIDE_USED_TODAY`
  (당일 예외 소진 후 재시도 시), 새 order_result 표시: `STOP_LOSS_REENTRY_OVERRIDE_APPROVED`.
- 새 시간 상수(가칭): `LATE_NEW_BUY_CUTOFF_ET = 15:45` — `NEW_ENTRY_CUTOFF_ET`(15:40, §미국시장
  캘린더)와 별개로 관리한다. 15:40은 신규 flat 진입의 1차 마감이고, 15:45는 반대신호의 매수
  레그까지 포함해 **모든 신규 매수**를 막는 최종 마감이다 — 15:40~15:45 사이에는 반대신호의
  매도(SELL) 레그만 발생할 수 있고 매수(BUY) 레그는 그 시점의 `entry_window_open` 판정에 따라
  이미 막혀 있을 수도 있다(실 구현 시 두 값의 관계를 정확히 확정해야 하며, 최소한 15:45부터는
  예외 없이 전부 차단되어야 한다).

TSLL/TSLZ는 TSLA의 일별 수익률을 레버리지/역방향으로 추종하도록 설계된 상품이지만, 실제 장중
수익률은 정확한 배수와 일치하지 않을 수 있다 — Stop Loss/Profit Lock 문턱값을 그대로 복제한
초기값은 운영 데이터 축적 후 재검토가 필요하다(`docs/TSLA_AUTO_REQUIREMENTS.md` §10 참조).

---

## 주문 로직

### 신규 BUY

1. TSLA confirmed 플래그 생성
2. 강한 플래그 필터 ON이면 승인 여부 확인(손절 재진입 쿨다운 게이트도 항상 확인)
3. `UP_RED`이면 `TSLL`, `DOWN_BLUE`이면 `TSLZ` 선택
4. 대상 ETF fresh 매도 1호가(ask1) 조회
5. 실제 USD 주문가능금액·주문가능수량 조회
6. UI USD 예산과 실제 가능금액 중 작은 값 사용
7. 수수료와 가격변동 여유 반영
8. 정수 수량 계산(소수점 주식 거래 금지)
9. 즉시체결 가능성이 높은 일반 지정가 주문(`ask1 + 1틱`, MACD2와 동일한 "즉시체결형 지정가"
   철학 — 미국 ETF의 실제 최소 호가단위(tick size)는 KRX와 다르므로
   `KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`: 종목별 tick size 규칙을 공식 자료로 확인)
10. 주문번호 확인
11. 체결 polling
12. 부분체결 시 실제 체결수량만 포지션 반영
13. 미체결 잔량 취소
14. 실제 해외주식 잔고 재확인

### 반대신호

- `TSLL` 보유 중 `DOWN_BLUE`: `TSLL` 전량 매도 → 체결 확인 → 실제 `TSLL` 잔고 0 확인 →
  `TSLZ` 매수
- `TSLZ` 보유 중 `UP_RED`: `TSLZ` 전량 매도 → 체결 확인 → 실제 `TSLZ` 잔고 0 확인 →
  `TSLL` 매수

### 금지

- `TSLL`·`TSLZ` 동시 보유
- 기존 ETF 잔고 0 확인 전 반대 ETF 매수
- 같은 `signal_id` 재주문
- 같은 방향 추가매수
- 주문번호 없이 position 생성
- 주문 거절을 성공으로 처리
- stale 호가에서 임의 시장가 전환

---

## 주문수량·통화

기준 통화: USD. 설정: `TSLA_AUTO_BUDGET_USD`, `TSLA_AUTO_ORDER_USAGE_RATIO`(기본 `0.995`,
MACD2의 `usable_cash * 0.995` 안전여유와 동일 개념), 자동환전 기본 OFF, 소수점 주식 거래 금지.

```text
usable_usd = min(UI USD 예산, KIS 실제 해외주식 USD 주문가능금액)

budget_qty = floor(usable_usd × order_usage_ratio ÷ 대상 ETF 지정가)

final_qty = min(budget_qty, KIS 대상 ETF 주문가능수량)
```

기록 필드(원장/state): `target_symbol`, `available_usd`, `usable_usd`, `bid1`, `ask1`,
`order_price`, `budget_qty`, `available_qty`, `final_qty`, `expected_notional_usd`,
`expected_fee_usd`.

`expected_notional_usd`는 항상 `usable_usd`(안전여유 반영 후) 이하로 재검증하며, 과도한 수량
차감은 하지 않고 필요 시 최대 1주만 줄인다(MACD2 `compute_limit_buy_quantity`와 동일 원칙).
호가조회 실패/stale 또는 `final_qty=0`이면 시장가로 자동 전환하지 않고 주문을 차단한다.

---

## 미국시장 캘린더

내부 시간대: `America/New_York`(Python `zoneinfo.ZoneInfo("America/New_York")` — DST 자동
적용). 정규장 09:30~16:00 ET.

| 이벤트 | 기본값(ET) |
|---|---|
| 프리마켓/애프터마켓 신규진입 | 금지 |
| 신규진입 마감(1차, flat 진입) | 15:40 |
| 신규진입·반대매수 전부 금지(최종) | **15:45**(§손절·Profit Lock·전환) |
| 강제청산 시작 | 15:50 |
| 최종 잔고 0 확인 | 15:58 |

필수 처리:

- **DST**: `zoneinfo`가 자동 처리하므로 한국시간 하드코딩을 절대 하지 않는다(예:
  "KST 22:30에 미국장이 열린다" 같은 고정 오프셋 계산 금지 — 서머타임 여부에 따라 KST 환산
  값이 매일 달라질 수 있음을 UI/로직 전부에서 전제한다).
- **미국 휴장일**(신정, MLK, 워싱턴 탄생일, Good Friday, 현충일, 준틴스, 독립기념일, 노동절,
  추수감사절, 크리스마스 등)과 **조기폐장일**(추수감사절 다음날, 크리스마스이브 등, 통상
  13:00 ET 폐장) 처리.
- 조기폐장일에는 위 표의 세 시각(신규진입 마감/강제청산 시작/최종 잔고 확인)을 폐장시각 기준
  으로 자동 축소한다(예: 13:00 조기폐장이면 강제청산 시작을 13:00 이전으로 당김 — 정확한
  축소 비율/여유시간은 구현 단계에서 확정).
- `KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`: **미국 휴장일·조기폐장일을 KIS API 자체에서
  조회할 수 있는지, 아니면 이 저장소가 별도 캘린더(예: `pandas_market_calendars`류 라이브러리
  또는 자체 유지보수 목록)를 가져야 하는지** 미확인. 필요 자료: KIS 해외주식 휴장일 조회 TR
  존재 여부(공식 문서), 또는 대체 캘린더 소스 채택 결정. 구현 차단 여부: **차단 아님** —
  당장은 자체 캘린더(미국 연방 공휴일 규칙 + 알려진 조기폐장일 하드코딩 목록)로 시작하고,
  KIS가 공식 TR을 제공하면 교체 가능. 대체 검증 방법: 과거 실제 거래일 데이터(1분봉 유무)로
  캘린더 판정이 실제 시장 상태와 일치하는지 리플레이 테스트.
- UI에는 ET·KST 시간을 항상 함께 표시한다.

---

## KIS 해외주식 API

이 절의 목적은 실제 endpoint·TR_ID·파라미터·응답 필드를 **이 저장소에서 실제로 확인된 것과
아직 확인되지 않은 것**으로 명확히 나누는 것이다. 확인 안 된 항목은 임의로 확정하지 않고
`KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`로 표시한다.

### 이미 이 저장소에 존재하고 실제 운용 중인 것 (재사용 가능, `COPY_AS_IS` 수준 신뢰도)

출처: `app/data_sources/kis_overseas_minute.py`(MU 종목 대상으로 이미 사용 중).

| 기능 | TR_ID | Endpoint | 비고 |
|---|---|---|---|
| 해외주식 현재가상세 | `HHDFS00000300` | `{base_url}/uapi/overseas-price/v1/quotations/price` | 파라미터: `AUTH=""`, `EXCD`(거래소코드, 예: `NAS`), `SYMB`(종목코드). 응답 `output.last/open/high/low/tvol` |
| 해외주식분봉조회 | `HHDFS76950200` | `{base_url}/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice` | 파라미터: `AUTH/EXCD/SYMB/NMIN/PINC/NEXT/NREC(최대 120)/FILL/KEYB`. 응답 `output2[].kymd(YYYYMMDD)/khms(HHMMSS)/last/open/high/low/evol` |

**주의 — 경로 불일치 발견**: `app/data_sources/auto_market_collector.py`(NVDA/AMD/AVGO 수집
경로)는 **같은 TR_ID(`HHDFS00000300`)**를 호출하면서 경로를
`/uapi/overseas-stock/v1/quotations/price`(`overseas-stock`)로 사용한다 — `kis_overseas_minute.py`
의 `/uapi/overseas-price/v1/quotations/price`(`overseas-price`)와 다르다. 이 저장소 안에서만도
두 경로가 공존한다 → `KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`: 어느 쪽이 공식 정확한 경로인지
(혹은 둘 다 유효한지) KIS 공식 문서로 재확인 후 TSLA_AUTO 구현 시 하나로 통일한다. 영향: 시세
조회 실패 시 원인 진단이 어려워질 수 있음(차단 아님 — 둘 다 시도해보는 방식으로 우회 검증
가능, 실제 응답 성공 여부로 판별).

인증: `_load_credentials(mode)`가 `KIS_REAL_APP_KEY`/`KIS_REAL_APP_SECRET`/`KIS_MOCK_APP_KEY`/
`KIS_MOCK_APP_SECRET`(`app/config.py:get_kis_account_config`와 동일 이름 패턴)을 사용하고,
토큰은 `/oauth2/tokenP`로 발급해 파일 캐시(`data/cache/kis_token_{mode}.json` 계열)에
저장한다. **국내·해외 TR이 같은 앱키/계좌를 공유**하므로 TSLA_AUTO는 새 KIS 앱 등록이
필요 없다 — 단, 국내 주문 함수(`kis_client.py`의 `buy`/`sell`)는 호출하지 않는다.

### 이 저장소에 선례가 전혀 없는 것 (`KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`)

| 기능 | 상태 | 필요한 공식 자료 | 구현에 미치는 영향 | 구현 차단 여부 | 대체 검증 방법 |
|---|---|---|---|---|---|
| 해외주식 잔고조회 | 미확인 | KIS Open API 공식 포털의 해외주식 잔고조회 TR 문서(모의/실전 TR_ID 쌍) | 보유 `TSLL`/`TSLZ` 수량·평균단가를 실제 계좌와 대조하는 reconcile 로직 전체가 이 응답 스키마에 의존 | **차단** — 잔고 재확인 없이는 MACD2와 동등한 안전성(체결 후 실제 잔고 재조회)을 보장할 수 없음 | KIS 공식 GitHub 샘플(`koreainvestment/open-trading-api`)의 해외주식 잔고조회 예제 실행 결과와 대조 |
| 외화예수금 조회 | 미확인 | 상동(해외증거금 통화별 예수금 TR) | `available_usd` 산출 근거 | 차단 | 상동 |
| USD 주문가능금액 조회 | 미확인 | 해외주식 매수가능금액 TR 문서 | `usable_usd` 계산의 실측 입력값 | 차단 | 상동 |
| TSLL/TSLZ 주문가능수량 조회 | 미확인 | 상동(종목·가격 지정 주문가능수량 TR) | `final_qty` 상한 | 차단 | 상동 |
| 해외주식 지정가 매수·매도 | 미확인 | 해외주식 주문 TR 문서(TR_ID, 거래소코드 파라미터, 주문구분 코드, 통화 처리) | 주문 실행 자체 | 차단(가장 중요) | 모의투자(MOCK) 계좌로 소액 실주문 테스트 — REAL 전환 전 필수 |
| 정정·취소 | 미확인 | 상동(정정취소 TR) | 미체결 잔량 취소 로직 | 차단 | 상동 |
| 미체결 조회 | 미확인 | 해외주식 미체결내역 TR 문서 | 체결 polling의 판정 기준 | 차단 | 상동 |
| 체결내역·부분체결·평균체결가 | 미확인 | 해외주식 체결내역 TR 문서 | 손익 계산·포지션 반영의 정확성 | 차단 | 상동 |
| 거래소 코드 매핑(TSLA/TSLL/TSLZ → 실제 상장 거래소 코드) | 부분 확인 | `EXCD="NAS"`는 `kis_overseas_minute.py`에서 MU/NASDAQ 종목에 실제 사용 확인됨 — TSLA/TSLL/TSLZ가 모두 나스닥(NAS)인지, 혹은 TSLL/TSLZ 중 일부가 다른 거래소(예: BATS/NYSE Arca)에 상장되어 다른 코드가 필요한지는 미확인 | 잘못된 거래소 코드는 시세 조회부터 실패 | 차단 | 실제 종목별 상장 거래소를 공개 정보(예: 브로커/거래소 공시)로 먼저 확인 후 KIS 시세 조회로 검증 |
| 미국 휴장·거래가능시간 조회 | 미확인(§미국시장 캘린더) | KIS 해외 거래시간/휴장일 조회 TR 존재 여부 | 자동 캘린더 정확도 | 차단 아님(자체 캘린더로 시작 가능) | 과거 데이터 리플레이 |
| REAL·MOCK 지원 차이 | 미확인 | 해외주식이 KIS 모의투자(VTS)에서 도메스틱과 동일 수준으로 100% 지원되는지(일부 해외 TR은 모의투자 미지원 사례가 있다고 알려져 있음) | 테스트 전략(MOCK으로 얼마나 검증 가능한지) | **테스트 설계에 영향** — 모의투자가 해외주문을 지원하지 않으면 fake broker 기반 단위테스트로만 검증하고 REAL 전환 전 별도 소액 실계좌 검증 절차가 필요 | KIS 공식 문서의 "모의투자 지원 TR 목록"에서 해외주식 주문 TR 포함 여부 확인 |

### 확정 원칙

- 확인되지 않은 TR_ID/엔드포인트/파라미터/응답 필드는 코드에 절대 하드코딩하지 않는다 —
  구현 착수 전 위 표의 각 항목을 공식 자료로 재확인하고 이 문서를 업데이트한다.
- 국내주식 주문/잔고/분봉 TR(`kis_client.py`)은 TSLA_AUTO에서 호출하지 않는다(테스트로 강제,
  §테스트).

---

## 원장

Signal ledger는 `csv.DictWriter`에 컬럼명 기반 dict만 전달한다(MACD2와 동일 안전장치 —
위치 기반 리스트 결합 금지, 디스크 헤더 재정렬 검증 포함).

주요 필드(MACD2 대비 추가/변경분만 표시, 나머지는 `docs/MACD2_LOGIC.md` §원장과 동일 이름
패턴 유지):

- `trading_date`, `signal_id`, `signal_type`, `direction`
- `bar_start_at_et` / `bar_start_at_kst`, `bar_end_at_et` / `bar_end_at_kst`
- `evaluated_at_et` / `evaluated_at_kst`, `detected_at_et` / `detected_at_kst`
- `order_requested_at_et` / `order_requested_at_kst`
- `order_result`, `block_reason`
- `strategy_name`(`TSLA_AUTO`), `strategy_version`, `signal_rule`, `worker_code_sha`,
  `session_started_at`
- MAJOR 필터 필드: MACD2와 동일 이름 유지(`major_filter_enabled`, `major_score`, ... )
- **(신규)** `stop_loss_reentry_cooldown_active`, `stop_loss_reentry_override_used`
- 주문 사이징 필드: `target_symbol`, `available_usd`, `usable_usd`, `bid1`, `ask1`,
  `order_price`, `budget_qty`, `available_qty`, `final_qty`, `expected_notional_usd`,
  `expected_fee_usd`
- 체결 필드: `broker_order_id`, `broker_rt_cd`, `broker_msg_cd`, `broker_msg1`, `filled_qty`,
  `fill_poll_result`, `balance_qty`

`strategy_name`/`direction` 값이 알려진 도메인을 벗어나면 그 행은 삭제·덮어쓰기하지 않고
`MALFORMED_SCHEMA`로 제외 목록에만 표시한다(MACD2와 동일). 오늘 통계는 **현재 거래일 + 현재
Worker 세션 + 현재 `strategy_version`/`signal_rule`/`worker_code_sha`**의 confirmed 신호만
집계하고, 이전 SHA/버전/세션 이전 행은 별도 제외 사유로 표시한다(MACD2 2026-07-31 수정과
동일 원칙).

Execution ledger는 주문 요청과 체결 결과를 `order_id` 기준으로 dedup한다.

---

## UI

전용 Streamlit 페이지. UI는 Worker state와 ledger summary만 읽으며 별도 판단을 하지 않는다.
상태 변경은 service command만 기록한다.

표시 항목:

- READ_ONLY / MOCK / REAL 모드
- KIS 계좌 마스킹
- USD 예수금·주문가능금액, `TSLA_AUTO_BUDGET_USD`
- `TSLA`/`TSLL`/`TSLZ` 현재가·age
- TSLA 당일 1분봉 수·최신시각, 마지막 완성 3분봉
- MACD·Signal·diff, confirmed flag
- ET·KST 시간(모든 시간 필드)
- 강한 플래그 필터 ON/OFF, major score·승인·탈락 이유
- 현재 보유 ETF(`TSLL` 또는 `TSLZ`)·보유수량·평균가
- 호가·주문가·주문수량
- 주문번호·체결·미체결
- Stop Loss·Profit Lock(§손절·Profit Lock·전환의 신규 쿨다운/예외 상태 포함)
- 강제청산 상태
- Gross·비용·Net USD/KRW
- 정확한 `block_reason`
- KIS 응답코드·메시지
- Worker SHA·Git SHA

---

## 검증 기준

필수 테스트는 모두 fake data, fake broker, tmp_path 격리 경로에서 수행한다. 실제 `data/` 파일과
실제 KIS 주문은 사용하지 않는다.

- TSLA 해외 현재가·분봉 수집(fake fetcher)
- TSLL·TSLZ 현재가·호가(fake fetcher)
- 전 거래일 warm-up(미국 공휴일/조기폐장 반영)
- 09:30 ET 기준 3분봉 리샘플, DST 전환일
- 휴장·조기폐장일 처리 및 시각 자동 축소
- `UP_RED→TSLL` BUY, `DOWN_BLUE→TSLZ` BUY
- **`TSLT` 및 국내 종목코드/국내 주문 함수 호출 0건**(가드 테스트 — §MACD2와 완전 분리)
- strong filter OFF/ON, MAJOR 승인·탈락
- 동일 `signal_id` 주문 1회(반복 tick에도)
- USD 주문가능금액·수량 계산
- 일반 지정가 주문, 주문번호 확인
- 전량·부분·미체결, 잔량 취소
- 실제 잔고 재조회
- `TSLL` 매도 → 잔고 0 → `TSLZ` 매수, 역방향도 동일
- Stop Loss(-1.5%), Profit Lock, 손절 후 15분 쿨다운, 하루 1회 85점 예외 재진입
- 15:40 ET 신규진입 마감, 15:45 ET 반대매수 포함 전부 금지, 15:50 ET 강제청산
- 최종 TSLL/TSLZ 전략잔고 0(강제청산 후)
- MACD2와 state·ledger·Worker·Service·lock file 비공유(경로/인스턴스 격리 테스트)
- TSLA_AUTO STOP이 MACD2에 영향 0, MACD2 STOP이 TSLA_AUTO에 영향 0
- 동일 KIS 토큰 사용 시 동시성(토큰 갱신 경합, rate limit 공유)
- REAL gate(확인 문구 없이 REAL 주문 불가)
- 해외주식 비용·손익 계산(Gross/Net USD/KRW, 세금 미차감 확인)

---

## 강한 플래그 필터 (강한 플래그만 거래)

선택형 주문권한 게이트다. MACD2와 마찬가지로 **전략 버전을 바꾸지 않고** 별도
`major_filter_version`으로 관리한다. TSLA_AUTO는 MACD2의 실제 운영값(코드에서 직접 확인한
값)을 초기값으로 그대로 복제한다.

### 목표와 불변 조건

- 기존 confirmed MACD 플래그는 전부 정확히 생성·기록한다.
- 필터 OFF이면 기존 주문 흐름을 100% 유지한다.
- 필터 ON이면 confirmed 플래그 중 강도가 높은 MAJOR_FLAG만 주문권한을 가진다.
- MACD 공식·플래그 시각, 주문·체결·손절 로직은 이 필터가 변경하지 않는다.
- **강한 플래그 점수는 TSLA 완성 3분봉만으로 계산한다. TSLL·TSLZ의 이후 가격이나 미래 봉을
  필터 판단에 사용하지 않는다.**

### UI 제어

- 표시명: 강한 플래그만 거래. 기본값 OFF.
- UI는 command만 기록한다. 토글 변경은 **다음 신규 confirmed 플래그부터** 적용하며, 이미 보유한
  포지션의 Stop Loss·Profit Lock·강제청산에는 영향이 없다.

### Hybrid 점수 — MACD2 실제 운영값 복제 (`app/trading/macd2/config.py`/`major_flag_filter.py`
직접 확인)

| 항목 | 배점 | 조건(MACD2 실제 상수) |
|---|---|---|
| A. Histogram impulse | 최대 25 | `hist_impulse_atr = direction × (curr_hist − prev_hist) / ATR14` — `≥0.10(T1)→10`, `≥0.15(T2)→18`, `≥0.22(T3)→25` |
| B. 가격 impulse·돌파 | 최대 25 | 4봉 돌파(`MAJOR_RANGE_BREAKOUT_LOOKBACK=4`)면 25. 아니면 `price_impulse_atr` — `≥0.35(T1)→15`, `≥0.55(T2)→25` |
| C. 캔들 몸통 | 최대 10 | 방향 일치 캔들 — `body_atr ≥0.25(T1)→5`, `≥0.40(T2)→10` |
| D. 거래량 | 최대 15 | `volume_ratio`(직전 20봉 중앙값 대비, 현재 봉 제외) — `≥1.00(T1)→5`, `≥1.10(T2)→10`, `≥1.20(T3)→15` |
| E. EMA10 | 10 | UP: EMA10 상승 & close>EMA10 / DOWN: EMA10 하락 & close<EMA10 |
| F. EMA20/VWAP | 10 | UP: close>EMA20 또는 close>당일 VWAP / DOWN: close<EMA20 또는 close<당일 VWAP |
| G. 변동성 | 5 | 최근 8봉(`MAJOR_RECENT_RANGE_LOOKBACK=8`) range/close ≥ `0.006`(`MAJOR_SIDEWAYS_RANGE_MAX`) **또는** 현재 ATR14 ≥ 직전 20봉(`MAJOR_VOLUME_LOOKBACK=20`) ATR14 중앙값 |

승인 기준(MACD2 실제 상수 그대로 복제):

- 신규진입(flat): `required_score = MAJOR_ENTRY_SCORE_MIN = 65`
- 반대 포지션 전환: `required_score = MAJOR_REVERSAL_SCORE_MIN = 75`
- 직전 실제 진입 후 `MAJOR_FAST_REVERSAL_WINDOW_MIN=15`분 이내 반대 전환:
  `required_score = MAJOR_FAST_REVERSAL_SCORE_MIN = 82`
- 필수 가격조건(하나 이상): 4봉 돌파 **또는** price impulse ≥ 0.35 ATR **또는** EMA20 방향
  일치 **또는** VWAP 방향 일치
- 횡보 차단(둘 다 만족 시 거절): `|EMA10−EMA20|/close < MAJOR_SIDEWAYS_EMA_SPREAD_MAX(0.0007)`
  **그리고** 최근 8봉 range/close `< MAJOR_SIDEWAYS_RANGE_MAX(0.006)`
- 최소 보유시간: `MAJOR_MIN_HOLD_MIN = 9`분 — 그 안에는 작은 반대 confirmed로 전환하지 않음
  (반대 점수 `≥82`면 9분 이내에도 강한 반전으로 전환 허용)
- 같은 방향 재진입 제한: `MAJOR_SAME_DIRECTION_REENTRY_MIN = 18`분(마지막 같은 방향 청산 후)
- 하루 최대 진입 횟수: `MAJOR_MAX_DAILY_ENTRIES = 4`(실제 BUY 체결수량 > 0일 때만 증가)

### 미국 세션 전용 재작성 필요 항목 — **VWAP 계산**

`major_flag_filter.py`의 `_session_vwap()`은 `work["datetime"].dt.tz_convert(config.KST)`로
날짜/세션 경계를 계산하고, `config.SESSION_OPEN`(09:00)·`config.FORCE_LIQUIDATE_AT`(15:00)라는
**한국 정규장 시각 상수**로 VWAP 누적 구간을 정한다. TSLA_AUTO는 이 함수를 그대로 재사용할 수
없다 — America/New_York 09:30~16:00 세션 기준으로 다시 작성해야 한다(점수 산식·배점 자체는
바뀌지 않는다). 나머지 지표(ATR14, EMA10/20, histogram impulse, price impulse, 거래량 비율,
캔들 몸통, 횡보 판정)는 OHLCV 값과 config 상수에만 의존하므로 시간대 변경 없이 그대로 이식
가능하다.

지표 계산은 이 모듈 전용 순수 함수로만 수행한다. `signal_engine`(TSLA_AUTO 전용 복제본)에
지표를 끼워 넣지 않는다. 입력 DataFrame을 수정하지 않으며, 동일 입력 → 동일 출력이다.

---

## 비용·손익

2026-08-01 구현 기준:

- 매수 거래수수료: 체결금액의 0.25%
- 매도 거래수수료: 체결금액의 0.25%
- 환전우대: 95%로 계산한다. 기본 1.00% 환전 스프레드 중 우대 후 5%만 비용으로 반영하므로 실효 환전비용은 0.05%다.
- 슬리피지: 요청가와 실제 체결가가 모두 있고 차이가 있으면 실제 차이를 비용으로 쓴다. 실제 차이를 산출할 수 없으면 편도 0.05%를 가정한다.
- Worker와 replay는 모두 `app.trading.tsla_auto.cost_engine.OverseasTradeCostEngine`으로 Gross/비용/Net을 계산한다.
- Execution ledger는 거래별 Gross PnL, 매수수수료, 매도수수료, 슬리피지, 환전비용, 총비용, Net PnL을 저장한다.
- UI 당일 요약은 총 거래수수료, 총 슬리피지, 총 환전비용, 총비용, Gross PnL, Net PnL, Net Return을 표시한다.

KIS 계좌에 실제 적용되는 미국주식 수수료율은 설정값(config.yaml 신규 섹션, 가칭
`trading_cost.overseas_*`)으로 관리하며 확정되지 않은 수수료율은 코드에 고정하지 않는다
(`app/trading/trading_cost_engine.py`가 이미 이 패턴을 국내 수수료에 대해 쓰고 있다 —
`domestic_buy_fee_rate`/`etf_buy_fee_rate` 등. 해외용 키는 아직 없다).

비용 항목: TSLL·TSLZ 매수/매도 수수료, SEC Section 31 fee, FINRA TAF, 기타 KIS 체결 비용,
환전수수료·환전 스프레드, 슬리피지. 실제 KIS 체결내역 비용이 제공되면 추정값보다 우선한다.

```text
Gross USD PnL = 매도 체결금액 - 매수 체결금액
Net USD PnL   = Gross USD PnL - 총비용
```

별도 표시(거래별 Net에서 차감하지 않음): 적용환율, Net KRW, 연간 해외주식 실현손익, 양도소득세
참고 추정.

`KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`: KIS가 실제 부과하는 미국주식 매매수수료율·최소수수료·
환전수수료율은 공식 수수료 안내(또는 계좌 약정) 확인 전까지 코드에 고정하지 않는다. SEC
Section 31 fee/FINRA TAF는 미국 규제기관이 공개 고시하는 요율이나 주기적으로 변경되므로 최신값
확인 필요(구현 차단 아님 — 설정값으로 분리해두면 나중에 값만 갱신 가능).

---

## MACD2와 완전 분리

예정 경로: `app/trading/tsla_auto/`, `tests/tsla_auto/`, `data/state/tsla_auto/`,
`data/ledger/tsla_auto/`, `data/cache/tsla_auto/`, `data/runtime/tsla_auto/`, 전용 UI.

> 참고: MACD2는 신호/체결 원장을 `data/logs/`(공용 `LOGS_DIR`) 아래 `macd2_signal_ledger.csv`
> 처럼 **파일명으로만** 구분하고 별도 `data/ledger/` 최상위 디렉터리를 쓰지 않는다
> (`app/utils/data_paths.py`). 이 요구사항 문서가 명시한 `data/ledger/tsla_auto/`는 MACD2의
> 기존 관례와 다른 새 최상위 카테고리다 — 구현 시 `data_paths.py`에 `LEDGER_DIR` 상수를 새로
> 추가할지, 아니면 MACD2 관례대로 `LOGS_DIR/tsla_auto/`를 쓸지 결정이 필요하다(사용자 확인
> 권장, 구현 차단 사유는 아님).

**절대 공유 금지**: runtime state, position, pending signal, `processed_signal_ids`, signal
ledger, execution ledger, cache, Worker singleton, Service singleton, lock file, 강제청산
상태, 예산, 일일 통계, `strategy_version`.

**공유 가능**: KIS 저수준 인증·토큰 관리자, 순수 MACD 계산 함수(같은 수식을 각자 복제 —
import 공유가 아니라 "동일 로직을 두 곳에 유지"라는 의미), 시장 비종속 로깅·시간 유틸.

계좌가 같은 KIS 계좌여도:

- 국내 MACD2 포지션을 TSLA_AUTO 포지션으로 인식하지 않는다.
- `TSLL`·`TSLZ` 이외 해외주식 보유분을 전략 포지션으로 인식하지 않는다.
- TSLA_AUTO 강제청산은 `TSLL`·`TSLZ`의 **전략 보유수량만** 대상으로 한다.

별도 프로세스 락: `macd2_worker.lock` / `tsla_auto_worker.lock`.

TSLA_AUTO에서 국내주식 주문 함수·국내 종목코드 또는 잘못된 상승 ETF 코드(`TSLT` 등)를 호출하면
테스트가 실패하도록 설계한다(§검증 기준, `TSLA_AUTO_COPY_MAP.md` 위험 열).
`app/trading/strategy_ownership.py`(Enhanced/MACD v1/MACD2 국내 3파전 상호배제)에는 TSLA_AUTO를
참여시키지 않는다 — 국내 주문권한을 두고 경합할 필요가 없기 때문이다.

---

## 동시 실행 안전성

MACD2와 TSLA_AUTO를 동시에 실행할 수 있어야 한다. 필수:

- Worker 인스턴스 각각 1개, command 파일 분리, state·ledger·cache 완전 분리.
- 국내장·미국장 시간 독립.
- 한 전략의 STOP 명령이 다른 전략을 중단하지 않음. 한 전략의 강제청산이 다른 전략 포지션을
  매도하지 않음.
- 계좌조회 캐시는 시장·통화·전략별 분리.
- API rate limit 공통 조정기(`kis_client.py`의 `_rate_limit_lock`/`_throttle`, mode 단위)를
  쓰더라도 요청 전략을 기록.
- 토큰 갱신 경합 방지.
- 국내·해외 주문번호 namespace 혼용 금지.

---

## 금지 사항

- MACD 12/26/9 파라미터 변경 금지
- ETF 방향 변경 금지(`UP_RED→TSLL`, `DOWN_BLUE→TSLZ` 고정)
- Stop Loss, Profit Lock, 15:40/15:45 ET 신규진입 금지, 15:50 ET 강제청산 규칙 임의 변경 금지
- MACD2/MACD v1/Enhanced 수정 금지
- 운영 `data/` 파일 수정 금지
- 실제 KIS 주문 테스트 금지
- 새 프레임워크 도입 또는 대규모 리팩토링 금지
- 확인되지 않은 KIS 해외 API 세부사항을 임의로 확정해 코드에 고정하는 것 금지
  (`KIS_OVERSEAS_API_CONFIRMATION_REQUIRED` 항목은 공식 자료 확인 후에만 확정)
