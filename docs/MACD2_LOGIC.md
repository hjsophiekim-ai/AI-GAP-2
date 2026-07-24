# MACD2 Logic

본 문서는 독립 모듈 `app/trading/macd2/`의 현재 운용 기준이다. MACD v1, Enhanced 전략과 파일·상태·원장을 공유하지 않는다.

## 목적

MACD2는 SK하이닉스(`000660`) KIS 3분봉 MACD 차트에 빨간색·파란색 플래그가 표시되는 시점과 최대한 가깝게 같은 방향 Primary 플래그를 만들고, 신규 `signal_id` 확인 즉시 주문 요청을 시작한다.

KIS 앱 내부 계산 규칙이 공개되지 않았으므로 시각을 맞추기 위한 임의 보정은 금지한다. 차이가 있으면 3분봉 경계, 정규장/NXT 포함 여부, EMA 초기화, 현재 진행봉 포함 방식을 진단한다.

## 종목과 방향

- 신호 원천: `000660`
- `UP_RED`: `0193T0` 매수
- `DOWN_BLUE`: `0197X0` 매수
- 반대 ETF 보유 중 반대 신호: 기존 ETF 전량 SELL, 체결 및 잔량 0 확인, 반대 ETF BUY

종목 방향, 손절, Profit Lock, 14:55 신규진입 금지, 15:00 강제청산, MOCK/REAL 게이트는 본 문서의 고정 규칙이다.

## 데이터

- 1분봉은 `000660` 정규장 09:00 이후 데이터를 사용한다.
- 3분봉 경계는 09:00 기준이다: 09:00~09:02, 09:03~09:05, ...
- 전일 데이터는 EMA warm-up에만 사용한다.
- Worker는 현재가·분봉 네트워크 호출을 직접 하지 않고, `MarketDataService`의 캐시만 읽는다.
- 현재 진행 중 3분봉의 close는 최신 유효 `000660` quote로 5초마다 갱신한다.
- 주문에 필요한 quote는 `price > 0`이고 `age_sec <= 10`이어야 한다.
- flat `UP_RED`: `000660`, `0193T0` quote 필요
- flat `DOWN_BLUE`: `000660`, `0197X0` quote 필요
- 스위칭: `000660`, 현재 보유 ETF, 신규 매수 ETF quote 필요
- 관계없는 ETF stale만으로 주문을 차단하지 않는다.

## MACD 계산

- MACD: EMA 12 - EMA 26
- Signal: MACD의 EMA 9
- EMA는 `adjust=False`를 사용한다.
- 전일 및 당일 완성 3분봉으로 warm-up을 만든 뒤, 현재 진행 중 3분봉 한 개를 덧붙여 Primary 판단용 MACD를 계산한다.
- 진행봉은 봉 마감 전 가격 변화로 diff가 다시 되돌아갈 수 있다. 이 경우 사후 confirmed 진단과 차이가 날 수 있으나, 주문권한은 최초 진행봉 Primary onset에만 있다.

## Primary 신호

실제 주문권한이 있는 Primary 신호는 하나뿐이다.

직전 완성 3분봉까지의 `MACD-Signal` diff와 현재 진행 중 3분봉의 최신 quote 반영 diff를 비교한다.

- 이전 diff `<= 0`이고 현재 diff `> 0`: `UP_RED`
- 이전 diff `>= 0`이고 현재 diff `< 0`: `DOWN_BLUE`
- 그 외: `HOLD`

Primary 계산은 `signal_engine.evaluate_primary_forming_crossover()`를 공통 함수로 사용한다. Worker, UI 상태, 리플레이, 테스트는 이 결과에서 나온 MACD, Signal, diff, direction, `signal_id`를 같은 의미로 해석해야 한다.

## signal_id와 중복 차단

진행봉 Primary `signal_id` 형식:

```text
YYYYMMDD_HHMMSS_DIRECTION_PROVISIONAL
```

`HHMMSS`는 현재 진행 중인 3분봉 시작시각이다.

같은 진행 3분봉에서 동일 교차가 20회 반복 계산되어도 플래그와 주문은 최초 onset 1회만 생성한다. 같은 봉 안에서 교차가 취소됐다가 다시 발생하거나 반대 방향으로 흔들려도 해당 3분봉은 `processed_signal_ids`, `pending_signal`, `provisional_ordered_bar_ts`, signal ledger dedup으로 중복주문을 막는다.

동일 `signal_id`는 재시작 후에도 재주문하지 않는다. 신호 원장은 `signal_id` 기준 append-only dedup을 수행한다.

## Signed-B shadow

`evaluate_signed_b`, histogram slope, signed-B 조건은 주문권한이 없다.

UI에는 Signed-B shadow 진단값으로만 표시한다.

- 최근 histogram 3개
- signed-B shadow direction
- `order_authority=NONE`

Signed-B는 Primary 플래그 수, 오늘 거래 통계, 주문 dispatch에 포함하지 않는다.

## Worker 흐름

Worker는 5초 tick으로 동작한다.

1. state 로드
2. position reconcile
3. quote cache 읽기
4. history cache 읽기
5. 완성 3분봉 resample 및 confirmed MACD 진단값 계산
6. 최신 `000660` quote로 현재 진행봉 MACD 계산
7. `evaluate_primary_forming_crossover()`로 Primary direction과 `signal_id` 산출
8. 신규 Primary `signal_id`이면 즉시 `_execute_or_wait()` 호출
9. quote 또는 position 일시 오류면 같은 `signal_id`를 `pending_signal`로 유지하고 최대 30초 재시도
10. 주문 요청이 실제 생성되면 `processed_signal_ids`에 등록
11. state 저장

완성봉 crossover는 사후 정합성 확인용 confirmed 진단값일 뿐 주문을 만들지 않는다.

## 주문 및 위험관리

- `UP_RED` + flat: `0193T0` BUY
- `DOWN_BLUE` + flat: `0197X0` BUY
- 반대 신호 + 보유: 기존 ETF SELL, 잔량 0 확인, 반대 ETF BUY
- 14:55 이후 신규 진입 금지
- 15:00 이후 강제청산 우선
- Stop Loss와 Profit Lock은 기존 규칙을 유지한다.
- MOCK/REAL 게이트는 broker adapter와 기존 service 경로를 따른다.
- 실제 KIS 주문은 명시된 운영 모드에서만 허용한다. 테스트는 fake broker만 사용한다.

## 원장

Signal ledger 주요 필드:

- `trading_date`
- `completed_bar_at`: Primary에서는 진행봉 시작시각
- `signal_id`
- `signal_type`
- `direction`
- `detected_at`
- `order_requested_at`
- `order_result`
- `block_reason`
- `strategy_name`
- `strategy_version`
- `signal_rule=MACD_FORMING_CROSSOVER`
- `forming_bar_start`
- `forming_bar_end`
- `previous_diff`
- `provisional_macd`
- `provisional_signal`
- `provisional_diff`
- `provisional_direction`
- `quote_ages`
- `position_reconcile`
- `executor_called`
- `broker_called`
- `final_result`

Execution ledger는 주문 요청과 체결 결과를 `order_id` 기준으로 dedup한다.

## UI

UI는 Worker state와 ledger summary만 읽는다. UI가 별도 MACD 주문 판단을 하지 않는다.

Primary 표시:

- current forming bar start/end
- provisional MACD
- provisional Signal
- provisional diff
- provisional flag
- provisional `signal_id`
- `signal_detected_at`
- `order_requested_at`

Confirmed 표시:

- 마지막 완성봉 MACD
- Signal
- previous/current diff
- relation
- 주문권한 없음

Signed-B shadow 표시:

- histogram 최근 3개
- signed-B shadow flag
- 주문권한 없음

오늘 통계는 현재 `strategy_version`과 `signal_rule=MACD_FORMING_CROSSOVER`의 고유 Primary onset만 집계한다. OLD_STRATEGY, Signed-B legacy, confirmed-only rows는 제외 영역에만 표시한다.

## 검증 기준

필수 테스트는 모두 fake data, fake broker, tmp_path 격리 경로에서 수행한다. 실제 `data/` 파일과 실제 KIS 주문은 사용하지 않는다.

- 진행 중 3분봉 상승 교차 즉시 `UP_RED` 플래그 및 `0193T0` BUY 1회
- 진행 중 3분봉 하락 교차 즉시 `DOWN_BLUE` 플래그 및 `0197X0` BUY 1회
- 동일 교차 20회 반복 평가 시 주문 1건
- 같은 봉 안 가격 흔들림으로 중복 매수·매도 0건
- Signed-B만 충족하고 Primary 교차가 없으면 주문 0건
- 반대 교차 시 기존 ETF SELL, 잔량 0, 반대 ETF BUY
- 관계없는 ETF stale은 차단하지 않고 대상 ETF stale은 차단
- quote 복구 시 같은 `signal_id`로 주문 1회
- `signal_detected_at -> order_requested_at` 5초 이내
- UI/Worker/리플레이가 같은 Primary 공통 계산 결과 사용
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
