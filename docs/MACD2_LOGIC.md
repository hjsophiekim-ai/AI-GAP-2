# MACD 자동매매2 요구사항 및 매매 로직

> 본 문서는 신규 독립 모듈 **MACD2**의 요구사항과 매매 로직을 확정하기 위한 설계 문서다.
> 이번 단계에서는 실행 코드·UI·테스트·설정 파일을 작성하지 않으며, 본 문서만 작성한다.
> 기존 MACD 자동매매 v1(`app/trading/macd_hynix_*`, `app/trading/macd_pipeline/*`,
> `app/ui/pages/10_MACD_하이닉스_자동매매.py`)은 이 작업으로 수정·삭제되지 않는다.

---

## 목차

1. [저장 위치](#1-저장-위치)
2. [시스템 목적](#2-시스템-목적)
3. [거래 대상](#3-거래-대상)
4. [시장 데이터와 봉 생성](#4-시장-데이터와-봉-생성)
5. [MACD 계산 방식](#5-macd-계산-방식)
6. [signed B 플래그 정의](#6-signed-b-플래그-정의)
7. [장 시작 및 초기 상태](#7-장-시작-및-초기-상태)
8. [진입 및 방향 전환](#8-진입-및-방향-전환)
9. [투자예산과 주문수량](#9-투자예산과-주문수량)
10. [청산 규칙](#10-청산-규칙)
11. [5초 실행 주기](#11-5초-실행-주기)
12. [시장 데이터 유효성](#12-시장-데이터-유효성)
13. [상태 및 생명주기](#13-상태-및-생명주기)
14. [MOCK / REAL](#14-mock--real)
15. [기존 모듈과 상호배타](#15-기존-모듈과-상호배타)
16. [UI 요구사항](#16-ui-요구사항)
17. [원장 필드](#17-원장-필드)
18. [장애 처리](#18-장애-처리)
19. [검증 기준](#19-검증-기준)
20. [비기능 요구사항](#20-비기능-요구사항)
21. [확정 사항 / 구현 전 확인 사항 / 구현 완료 체크리스트](#21-문서-마지막-섹션)

---

## 1. 저장 위치

- 문서 경로: `docs/MACD2_LOGIC.md` (본 파일)
- 저장소 루트에 `docs/` 폴더가 이미 존재하므로 신규 생성 없이 이 파일만 추가한다.

---

## 2. 시스템 목적

MACD2는 SK하이닉스(`000660`)의 완성된 3분봉 MACD Histogram 방향만으로
KODEX 레버리지(`0193T0`)와 SOL 하이닉스 인버스2X(`0197X0`)를 자동매매하는 독립 모듈이다.

핵심 목표:

- 구조를 단순하게 유지한다.
- UI·신호·주문 판단을 하나의 기준으로 통일한다.
- 플래그 확정 후 5초 이내 주문 요청을 시작한다.
- 동일 신호의 중복주문을 방지한다.
- 기존 Enhanced 및 MACD v1과 완전히 분리한다.
- MOCK과 REAL의 전략 판단(신호·방향·청산·수량 계산)은 동일하게 유지하고,
  브로커 계좌와 안전 게이트만 다르게 적용한다.

---

## 3. 거래 대상

**기준 종목**

- SK하이닉스: `000660`
- 역할: MACD 계산과 매매 방향 판단에만 사용한다.
- 직접 매수하지 않는다.

**상승 방향 거래 종목**

- KODEX 레버리지: `0193T0`
- `UP_RED` 신호에서 매수한다.

**하락 방향 거래 종목**

- SOL 하이닉스 인버스2X: `0197X0`
- `DOWN_BLUE` 신호에서 매수한다.

**ETF 가격의 용도**

ETF(`0193T0`, `0197X0`) 가격은 다음 용도에만 사용한다.

- 주문 수량 계산
- 현재 보유 평가
- 손절 및 Profit Lock 판단
- 손익·수수료·슬리피지 계산

ETF 가격으로 MACD 방향을 변경하거나 보조 확정하지 않는다. 방향 판단은 오직
`000660`의 완성 3분봉 MACD Histogram에서만 나온다.

---

## 4. 시장 데이터와 봉 생성

**시간대**

- 모든 데이터·판단·원장 기록은 Asia/Seoul(KST) 기준이다.
- 거래일(`trading_date`) 구분도 KST 기준이다.

**분봉 생성**

- SK하이닉스 1분봉을 수집한다.
- `09:00`을 기준으로 정렬된 3분봉을 생성한다.
- 미완성 3분봉은 MACD 신호 계산에 절대 사용하지 않는다.
- 새로운 완성 3분봉마다 신호를 정확히 1회 평가한다.

**Warm-up**

- 시작 시 이전 거래일을 포함한 1분봉 최소 300개를 확보한다.
- 완성 3분봉 최소 100개를 생성한다.
- EMA 계산에는 이전 거래일 봉을 포함해서 사용한다.
- 이전 거래일에 발생한 신호를 오늘의 신규 주문 신호로 사용하지 않는다.
- warm-up 완료 전 상태는 `NOT_READY`다.
- 데이터 부족 상태를 `HOLD` 또는 "신호 0건"으로 표시하지 않는다 — 반드시
  `NOT_READY`로 구분해 표시한다.

**증분 갱신**

- bootstrap 이후에는 전체 이력을 반복 조회하지 않는다.
- 최신 1분봉만 추가·갱신한다.
- 중복 `timestamp`는 제거한다.
- 오름차순으로 정렬한다.
- 수정된 동일 1분봉이 재수신되면 최신 값으로 교체한다.

---

## 5. MACD 계산 방식

**기본값**

| 파라미터 | 값 |
|---|---|
| Fast EMA | 12 |
| Slow EMA | 26 |
| Signal EMA | 9 |

**계산식**

```
MACD      = EMA12 − EMA26
Signal    = MACD의 EMA9
Histogram = MACD − Signal
```

EMA 계산은 `pandas.ewm`과 동일한 기준을 사용하며, 라이브 경로와 리플레이(백테스트)
경로가 반드시 동일한 공통 함수를 호출해야 한다(계산 로직의 이중 구현 금지).

**구현 전 확정이 필요한 세부 기준** (아래 항목은 실제 구현 시 코드/테스트로 명시해야
하며, 이 문서에서 임의로 정하지 않는다 — [21장](#21-문서-마지막-섹션) "구현 전 확인이
필요한 사항" 참조)

- `adjust` 파라미터 사용 여부 (`ewm(..., adjust=True/False)`)
- `min_periods` 사용 여부
- 3분봉 resample의 `label`/`closed` 기준(예: `label="left"`/`"right"`, `closed="left"`/`"right"`)
- 완성봉 판정 방식(현재 시각 대비 봉 종료 시각 경과 여부 판단 기준)
- timezone 처리 방식(naive KST로 통일할지, tz-aware로 유지할지)
- NaN 제거 방식(초기 EMA warm-up 구간의 NaN을 어떻게 잘라내는지)

기존 검증된 signed B 전략(MACD v1)과 신호 시각이 반드시 일치해야 한다
([19장 Parity 기준](#19-검증-기준) 참조).

---

## 6. signed B 플래그 정의

최근 완성 3분봉 Histogram을 다음과 같이 정의한다.

- `h0`: 현재 마지막 완성 3분봉 Histogram
- `h1`: 직전 완성 3분봉 Histogram
- `h2`: 그 이전 완성 3분봉 Histogram
- `d0 = h0 − h1`
- `d1 = h1 − h2`

**UP_RED** — 아래 조건을 모두 만족

- `h0 > 0`
- `h1 > 0`
- `d0 > 0`
- `d1 > 0`
- 기존 확정 방향(`last_signal_direction`)이 `UP`이 아님

**DOWN_BLUE** — 아래 조건을 모두 만족

- `h0 < 0`
- `h1 < 0`
- `d0 < 0`
- `d1 < 0`
- 기존 확정 방향이 `DOWN`이 아님

**그 외**: `HOLD`

**중요 제약**

- Histogram이 음수권에서 "덜 음수"로 변하는 경우는 `UP_RED`가 아니다.
- Histogram이 양수권에서 "덜 양수"로 변하는 경우는 `DOWN_BLUE`가 아니다.
- slope(기울기)만으로 신호를 만드는 것(slope-only 신호)은 금지한다.
- 같은 방향이 계속 유지되는 동안에는 반복 신호를 만들지 않는다.
- 방향이 실제로 전환되는 onset(전환 시점)만 신규 플래그로 기록한다.

**signal_id 생성 규칙**

```
signal_id = trading_date + "_" + completed_bar_at + "_" + direction
예: 20260723_102700_DOWN_BLUE
```

동일 `signal_id`의 주문은 시스템 생애 전체에서 1회만 허용한다(재부팅·재시작 후에도
동일 `signal_id` 재주문 금지 — [7장](#7-장-시작-및-초기-상태), [13장](#13-상태-및-생명주기) 참조).

---

## 7. 장 시작 및 초기 상태

**프리마켓 / NXT**

- 기본 전략에서는 프리마켓/NXT 데이터를 사용하지 않는다.
- `OPENING_PROBE_ENABLED=False`
- 프리마켓 신호로 선진입하지 않는다.

**프로그램 시작 시점에 이미 방향 패턴이 유지 중인 경우**

프로그램(또는 Worker) 시작 시점에 `UP_RED` 또는 `DOWN_BLUE` 패턴이 이미 유지
중이라면, 아래 조건을 모두 만족할 때 `INITIAL` 신호로 당일 1회 진입을 허용한다.

- 당일 해당 방향을 아직 주문·처리한 적이 없고
- 포지션이 flat이며
- warm-up 및 시세가 정상이고
- 현재 마지막 완성 당일 3분봉이 signed B 조건을 충족

단, 아래 이벤트만으로는 동일 `INITIAL` 신호를 재주문하지 않는다.

- 페이지 새로고침
- Worker 재시작
- 프로세스 재시작

**원장 구분**

`INITIAL` 신호와 `REVERSAL`(방향 전환) 신호는 원장에 `signal_type` 필드로
구분해서 기록한다([17장](#17-원장-필드) 참조).

---

## 8. 진입 및 방향 전환

**신규 진입**

- flat + `UP_RED` → 설정 예산 범위에서 `0193T0` 매수
- flat + `DOWN_BLUE` → 설정 예산 범위에서 `0197X0` 매수

**방향 전환 절차**

`0197X0` 보유 중 `UP_RED` 발생 시:

1. `0197X0` 전량매도 요청
2. 체결 확인
3. 실제 잔량 0 확인
4. `0193T0` 매수

`0193T0` 보유 중 `DOWN_BLUE` 발생 시:

1. `0193T0` 전량매도 요청
2. 체결 확인
3. 실제 잔량 0 확인
4. `0197X0` 매수

**반대 신호의 우선순위**

- 반대 신호는 현재 손익과 무관하게 적용한다.
- Profit Lock 및 일반 보유 판단보다 우선한다.
- 매도 확인 전에는 반대 ETF를 매수하지 않는다(선매도 확인 → 후매수 순서 엄수).

**동일 방향 보유 중**

같은 방향 ETF를 이미 보유 중이면 추가매수(스케일인)하지 않는다.

---

## 9. 투자예산과 주문수량

**기본 투자예산**

- 기본값: 10,000,000원
- UI에서 사용자가 변경 가능

**수량 계산**

- 설정 예산과 실제 주문가능금액 중 더 작은 금액을 기준으로 한다.
- 현재 ETF 매수가격으로 정수 수량을 계산한다.
- 수수료·호가 변동을 고려한 안전 여유를 반영한다 — **확정(2026-07-24)**:
  `order_executor.compute_order_safety_margin_pct(price, symbol)` =
  (config.yaml `trading_cost`의 해당 종목 매수수수료율, `TradeCostEngine.fee_rate`)
  + (`app.utils.stock_utils.get_tick_size(price)` 1틱을 가격 대비 %로 환산한 값).
  더 이상 고정 placeholder 비율(과거 `ORDER_SAFETY_MARGIN_PCT=0.5`)을 쓰지
  않는다 — §21 미확정 항목 해소.
- 계산된 수량이 1주 미만이면 주문을 차단한다.
- 주문 수량·가격·예산 사용률을 원장에 기록한다.

**초기 기본 진입**

- 설정 예산의 100%를 사용한다.
- 별도의 분할매수·스케일인 로직은 사용하지 않는다.

---

## 10. 청산 규칙

**우선순위** (숫자가 낮을수록 먼저 평가)

1. 15:00 강제청산
2. 안전 손절(Stop Loss)
3. 반대 signed B 신호 스위칭
4. Profit Lock
5. 보유 유지

**손절(Stop Loss)**

- ETF 실제 진입가 대비 순수익률(net) −1.5% 이하일 때 발동한다.
- 수수료 및 추정 비용을 반영한 net 기준으로 계산한다.
- 전량매도한다.
- `exit_reason=STOP_LOSS`

**고정 익절**

- +3% 고정 전량익절 규칙은 사용하지 않는다.

**Profit Lock**

- 현재 순수익률이 +1.5% 이상이면 활성화한다.
- 보유 중 최고 순수익률(`peak_net_return`)을 지속 갱신한다.
- 최고 순수익률 대비 0.8%p 이상 반납(giveback)하면 전량청산한다.
- `exit_reason=PROFIT_LOCK`

예시: `peak=+4.2%`, `current=+3.4%`, `giveback=0.8%p` → 전량청산

**반대 플래그(OPPOSITE_SIGNAL)**

- Profit Lock 조건에 도달하지 않았더라도, 반대 signed B 신호가 발생하면
  즉시 전량청산 후 반대 ETF로 전환한다.
- `exit_reason=OPPOSITE_SIGNAL`

**15:00 강제청산**

- 보유 중인 모든 ETF를 전량청산한다.
- 브로커 잔량 0을 확인한다.
- `exit_reason=FORCED_LIQUIDATION`

**신규진입 종료 시각**

- 14:55 이후에는 신규진입 및 방향 스위칭을 금지한다.
- 14:55 이후에는 청산만 허용한다.

**재진입 금지**

- `CONTINUATION_REENTRY_ENABLED=False`
- TP·SL·Profit Lock으로 청산된 이후, 같은 MACD 방향에서는 재진입하지 않는다.
- 새로운 반대 signed B 신호가 발생해야 새 episode를 시작할 수 있다.

---

## 11. 5초 실행 주기

Worker는 신호 계산과 주문 실행을 소유하는 단일 실행 주체이며, 5초마다 동작한다.

**5초 루프에서 수행하는 작업**

- 캐시된 최신 시세 읽기
- 새 완성 3분봉 확인
- 새 봉일 때만 signed B 계산
- 신규 `signal_id`이면 즉시 주문 intent 생성
- 보유 포지션 손절·Profit Lock 감시
- 15:00 강제청산 확인

**Worker가 하지 않는 작업**

- 과거 전체 분봉 재조회
- UI 렌더링
- 일일 통계 집계
- 별도 신호 queue 대기
- module reload
- 중첩 executor 생성

**성능 목표**

| 지표 | 목표 |
|---|---|
| Worker tick mean | ≤ 5.5초 |
| Worker tick p95 | ≤ 7초 |
| Worker tick max | ≤ 10초 |
| `signal_detected_at` → `order_requested_at` | ≤ 5초 |

KIS API 접수·체결 완료까지 5초 이내를 보장한다고 표현하지 않는다 — 위 목표는
Worker 내부 처리 시간에 대한 목표이며, KIS 측 처리·네트워크 지연은 별도다.

---

## 12. 시장 데이터 유효성

**종목별 캐시 구조**

각 종목(`000660`, `0193T0`, `0197X0`) 캐시는 다음 필드를 갖는다.

- `symbol`
- `price`
- `fetched_at`
- `age_sec`
- `source`
- `error`

**주문 시 필수 조건**

- `000660`, `0193T0`, `0197X0` 가격이 모두 0보다 커야 한다.
- 주문 대상 ETF의 시세 `age_sec` ≤ 10초여야 한다.
- stale이거나 누락된 경우 `ORDER_DATA_INVALID`로 처리한다.

**시세 오류 시 처리**

- 신규 주문을 차단한다.
- 기존 MACD 이력과 신호 원장은 그대로 유지한다.
- 이 상태를 "신호 없음"이 아니라 `DATA_ERROR` 또는 `ORDER_BLOCKED`로 명확히 표시한다.
- 시세가 복구된 이후, 그 시점부터의 새로운 유효 신호부터 주문을 재개한다.

---

## 13. 상태 및 생명주기

**전용 파일 경로**

| 용도 | 경로 |
|---|---|
| 런타임 상태 | `data/state/macd2_runtime.json` |
| 실행(체결) 원장 | `data/logs/macd2_execution_ledger.csv` |
| 신호 원장 | `data/logs/macd2_signal_ledger.csv` |

기존 MACD v1의 state·ledger 파일(`data/state/macd_hynix_runtime.json`,
`data/state/macd_hynix_state.json`, `data/logs/macd_hynix_execution_ledger.csv`,
`data/logs/macd_hynix_signal_ledger.csv`)과 **공유하지 않는다.**

**상태값(`ui_mode`)**

- `STOPPED`
- `BOOTSTRAPPING`
- `READY`
- `RUNNING`
- `DATA_ERROR`
- `SIGNAL_ERROR`
- `ORDER_BLOCKED`
- `WORKER_STALLED`

**단일 Worker 원칙**

- active Worker는 항상 1개만 존재해야 한다.
- Worker 재시작 시 기존 Worker의 종료를 확인한 뒤에만 새 Worker를 시작한다.
- shutdown된 executor는 재사용하지 않는다.
- Streamlit rerun만으로 Worker가 생성·종료되지 않는다.
- Worker가 로드한 코드 SHA와 애플리케이션(디스크) SHA가 불일치하면 시작을 금지한다.

---

## 14. MOCK / REAL

**MOCK**

- 기본값이다.
- KIS 모의투자 계좌를 사용한다.
- REAL 확인문구·REAL 안전 게이트 검사를 하지 않는다.

**REAL**

- 별도의 명시적 확인문구 입력이 필요하다.
- `safety.enable_real_trading=True`가 필요하다.
- 계좌 및 주문 게이트를 통과해야 한다.

**공통 원칙**

MOCK과 REAL은 아래 항목이 동일해야 한다.

- MACD 계산
- 진입 방향 판단
- 청산 판단
- 주문 수량 계산

차이는 오직 브로커 계좌와 REAL 안전 게이트뿐이다.

---

## 15. 기존 모듈과 상호배타

동시에 아래 중 **하나만** 주문 권한(`auto_trade_on`에 준하는 실행 권한)을
가질 수 있다.

- Enhanced
- MACD v1
- MACD2

**MACD2 시작 시**

- 다른 전략(Enhanced, MACD v1)의 실제 `auto_trade_on` 상태를 확인한다.
- 다른 전략이 ON 상태이면 MACD2 시작을 차단한다.
- 단순히 mutex 파일이 존재한다는 사실만으로는 차단하지 않는다(실제 상태 진실
  소스를 확인 — MACD v1이 `hynix_switch_state.load_state()`를 단일 진실
  소스로 사용하는 것과 동일한 원칙).

**역방향 상호배타**

MACD2가 실행 중이면, 기존 전략(Enhanced, MACD v1)도 MACD2의 실행권 상태를
읽고 자신의 시작을 차단해야 한다(상호 배타는 양방향이다).

**확정·구현 완료(2026-07-24)**: 공용 read-only 어댑터
`app.trading.strategy_ownership`가 세 전략 모두의 진실 소스다 —
`other_owner_active(claimant)`가 나머지 두 전략의 실제 `auto_trade_on` +
heartbeat 신선도(각 전략 자체 tick 주기의 배수 — 모듈 docstring 참조)를
확인한다. `macd_hynix_order_manager.can_start_macd()`(MACD v1),
`app/ui/pages/9_SK하이닉스_자동매매.py`의 시작 게이트(Enhanced),
`macd2/service.py`의 `other_strategy_active()`(MACD2) 세 곳 모두 이 모듈을
거친다. stale 파일 존재만으로는 차단하지 않으며, heartbeat가 임계치보다
오래되면(크래시로 추정) 더 이상 차단하지 않는다 — 단, heartbeat가 아예
없으면(방금 플래그만 켜지고 아직 첫 tick 전) fail-safe로 계속 차단한다.

---

## 16. UI 요구사항

**신규 페이지 이름**: `MACD 자동매매2`

**상단 영역**

- MOCK / REAL 선택
- 계좌 마스킹 표시
- 투자예산 입력(기본값 10,000,000원)
- 자동매매 시작 버튼
- 자동매매 중지 버튼

**상태 표시 영역**

- 전략 상태
- Worker 상태 및 SHA
- bootstrap 상태
- 세 종목(`000660`, `0193T0`, `0197X0`) 현재가와 age
- MACD / Signal / Histogram 값
- 최근 Histogram 5개와 변화량
- 현재 플래그
- 최신 `signal_id`
- 현재 보유 종목·수량·평단
- 현재 순손익률
- Profit Lock 상태
- peak/current/giveback
- 다음 예상 행동
- 정확한 block/error reason

**통계 영역**

- 오늘 빨간 플래그(`UP_RED`) 수
- 오늘 파란 플래그(`DOWN_BLUE`) 수
- 미주문 신호와 사유
- 매수/매도 체결 수
- 완료 왕복 수
- Gross/비용/Net PnL
- 수익률
- 승률 / Profit Factor / MDD
- 거래 원장

**UI 원칙**

UI는 상태 읽기 및 command 기록(시작/중지/강제청산 요청)만 수행한다. UI 자체가
MACD를 계산하거나 Worker를 직접 reload하지 않는다(MACD v1과 동일한 원칙).

---

## 17. 원장 필드

**신호 원장** (`data/logs/macd2_signal_ledger.csv`)

- `trading_date`
- `completed_bar_at`
- `signal_id`
- `signal_type` (`INITIAL` / `REVERSAL`)
- `direction`
- `MACD`
- `Signal`
- `histogram` 최근 값
- `detected_at`
- `order_requested_at`
- `order_result`
- `block_reason`

**실행 원장** (`data/logs/macd2_execution_ledger.csv`)

- `order_id`
- `signal_id`
- `timestamp`
- `mode`
- `symbol`
- `side`
- `requested_qty`
- `executed_qty`
- `requested_price`
- `executed_price`
- `position_before`
- `position_after`
- `gross_pnl`
- `fee`
- `slippage`
- `net_pnl`
- `exit_reason`
- `broker_response`

**원칙**: 체결 확인 전에는 성공으로 원장에 기록하지 않는다.

---

## 18. 장애 처리

오류는 숨기지 않고 아래 단계별 코드로 명시 기록한다.

- `DATA_FETCH_ERROR`
- `WARMUP_ERROR`
- `SIGNAL_ERROR`
- `ORDER_REQUEST_ERROR`
- `KIS_REJECTED`
- `EXECUTION_TIMEOUT`
- `POSITION_MISMATCH`
- `WORKER_STALLED`
- `DUPLICATE_SIGNAL_BLOCKED`

신호 계산 불가 상태를 `HOLD` 또는 "신호 0건"으로 표시하지 않는다(4장 warm-up
원칙과 동일).

**Worker 예외 처리**

- traceback을 영속적으로 기록한다.
- `last_tick` age가 15초를 초과하면 `WORKER_STALLED`로 표시한다.
- 자동 복구는 기존 Worker의 종료를 확인한 뒤 1회만 수행한다.
- 반복 재시작 루프를 금지한다.

---

## 19. 검증 기준

문서 차원에서 아래를 구현 완료 조건으로 명시한다(실제 구현 단계에서 테스트
코드로 옮긴다).

**단위 테스트**

- MACD 수식
- 3분봉 경계 판정
- signed B 신호(`UP_RED`/`DOWN_BLUE`/`HOLD`)
- 동일 방향 반복 신호 차단
- `signal_id` 생성 규칙
- SL / Profit Lock 조건
- 15:00 청산

**Parity(정합성) 검증**

- 기존 검증된 7/21·7/22 signed B 타임라인과 완전히 일치해야 한다.
- 7/22 기준 `10:42 DOWN → 12:33 UP` 전환을 포함한다.
- diff 0건을 목표로 한다.

**E2E 시나리오**

- flat + `UP_RED` → `0193T0` BUY
- flat + `DOWN_BLUE` → `0197X0` BUY
- 반대 신호 → SELL → 잔량 0 확인 → 반대 BUY
- 동일 `signal_id` 20회 반복 호출 → 추가 주문 0건
- Worker 재시작 후 이미 완료된 신호의 재주문 0건
- 미처리 신호 상태 복구 시나리오

**운영 검증**

- Render 환경에서 bootstrap 완료
- 세 종목 시세 정상 수신
- Worker 성능 기준 통과(11장 참조)
- signal → order request 5초 이내
- MOCK 모드로 하루 전체 운영
- 15:00 시점 잔량 0 확인

**테스트 격리 원칙**

- 테스트는 `tmp_path`와 fake broker만 사용한다.
- 실제 `data/` 파일 및 실제 KIS API를 절대 호출하지 않는다.
- KIS mock smoke test는 별도의 명시적 진단 모드에서만 수행한다.
- TEST 거래는 운영 통계에서 제외한다.

---

## 20. 비기능 요구사항

- 기존 MACD v1 파일을 수정·삭제하지 않는다.
- 기존 `data/cache`, `data/state`, `data/logs`를 수정·삭제하지 않는다.
- MACD2 전용 파일만 신규 생성한다.
- 상태 저장은 atomic write로 수행한다.
- 민감정보·계좌번호·API key는 로그에 남기지 않는다.
- 모든 종목코드는 문자열로 처리한다(숫자형 변환 금지 — 예: `"0193T0"`을 정수로
  취급하지 않음).
- KST/UTC를 혼용하지 않는다.
- 같은 계산 함수를 라이브·리플레이·테스트 경로에서 공동으로 사용한다.
- 복잡한 queue/pending/reload 구조를 금지한다.
- 외부 네트워크 호출은 MarketData 계층과 Broker 계층에서만 수행한다.

---

## 21. 문서 마지막 섹션

### 확정 사항

| 구분 | 확정 내용 |
|---|---|
| 종목 | 기준 `000660`(신호 전용), 매수 대상 `0193T0`(UP_RED) / `0197X0`(DOWN_BLUE) |
| MACD 설정 | Fast EMA 12 / Slow EMA 26 / Signal EMA 9, 완성 3분봉 기준 |
| signed B 정의 | `h0,h1>0`&`d0,d1>0` → `UP_RED`, `h0,h1<0`&`d0,d1<0` → `DOWN_BLUE`, 그 외 `HOLD`, onset만 신규 플래그 |
| 손절 | 진입가 대비 net −1.5% 이하, 전량매도, `exit_reason=STOP_LOSS` |
| Profit Lock | 활성화 +1.5%, giveback 0.8%p 초과 시 전량청산, `exit_reason=PROFIT_LOCK` |
| 반대신호 처리 | 손익 무관 최우선 적용, 매도 확인 후 반대 매수, `exit_reason=OPPOSITE_SIGNAL` |
| 거래시간 | 09:00 정렬 3분봉, 14:55 이후 신규진입/전환 금지, 15:00 강제청산 |
| 재진입 | `CONTINUATION_REENTRY_ENABLED=False`, 같은 방향 재진입 금지 |
| MOCK/REAL | 전략 판단 동일, 브로커·안전게이트만 차등 적용 |

### 구현 전 확인이 필요한 사항

아래 항목은 이번 문서에서 임의로 확정하지 않았다. 실제 구현 착수 전 반드시
확인·확정이 필요하다.

- [ ] EMA 계산의 `adjust` 파라미터 값(`True`/`False`)
- [ ] EMA 계산의 `min_periods` 값
- [ ] 3분봉 resample의 `label`/`closed` 파라미터 값
- [ ] "완성봉" 판정의 정확한 시각 비교 기준(현재 시각과 봉 종료 시각의 비교 방식)
- [ ] timezone을 naive KST로 통일할지, tz-aware로 유지할지
- [ ] EMA warm-up 구간 NaN 제거 시 정확한 컷오프 기준
- [x] 수량 계산 시 "안전 여유"의 구체적 비율/방식(수수료·호가단위 마진) —
      2026-07-24 확정, §9 참조(`order_executor.compute_order_safety_margin_pct`)
- [ ] 초당 거래건수 제한(KIS rate limit) 대응 방식 — MarketData 계층에서의
      재시도/대기 정책
- [ ] `signal_detected_at` 기록 시점의 정확한 코드 위치(Worker의 신호 확인
      직후로 한정할지, 추가 세분화가 필요한지)
- [x] MACD2와 MACD v1/Enhanced 간 상호배타 확인에 사용할 정확한 "진실 소스"
      함수/모듈(MACD v1의 `legacy_auto_trade_truth`에 대응하는 MACD2/타 전략
      조회 방식) — 2026-07-24 확정, §15 참조(`app.trading.strategy_ownership`)
- [ ] `data/state/macd2_runtime.json` 등 신규 상태 파일 경로에 대한
      테스트 격리 방식(기존 저장소에서 `LEGACY_STATE_PATH` 격리 누락으로
      인한 사고가 있었으므로, MACD2는 설계 단계에서 격리 대상 경로 목록을
      명확히 정의해야 한다)

### 구현 완료 체크리스트

- [ ] `docs/MACD2_LOGIC.md` 검토 및 승인
- [ ] MACD 계산 공통 함수(라이브/리플레이/테스트 공용) 구현
- [ ] 3분봉 resample 및 완성봉 판정 구현
- [ ] signed B 플래그(`UP_RED`/`DOWN_BLUE`/`HOLD`) 구현
- [ ] `signal_id` 생성 및 중복 차단 구현
- [ ] Warm-up(1분봉 300개, 3분봉 100개) 및 `NOT_READY` 상태 구현
- [ ] 진입/방향전환(선매도-확인-후매수) 구현
- [ ] 손절(STOP_LOSS) 구현
- [ ] Profit Lock 구현
- [ ] 반대신호(OPPOSITE_SIGNAL) 우선순위 구현
- [ ] 15:00 강제청산 구현
- [ ] 14:55 이후 신규진입 차단 구현
- [ ] 재진입 금지(`CONTINUATION_REENTRY_ENABLED=False`) 구현
- [ ] 5초 Worker 루프 및 성능 목표(mean/p95/max) 구현
- [ ] 시세 유효성(`age_sec≤10초`) 및 `ORDER_DATA_INVALID` 처리 구현
- [ ] `data/state/macd2_runtime.json` 상태 관리 구현
- [ ] `data/logs/macd2_execution_ledger.csv`, `macd2_signal_ledger.csv` 구현
- [ ] 단일 Worker 원칙 및 SHA 불일치 시작 차단 구현
- [ ] MOCK/REAL 게이트 구현
- [ ] Enhanced/MACD v1/MACD2 상호배타(양방향) 구현
- [ ] `MACD 자동매매2` UI 페이지 구현
- [ ] 장애 코드 전체(`DATA_FETCH_ERROR` 등) 구현
- [ ] `WORKER_STALLED` 감지 및 1회 자동복구 구현
- [ ] 단위 테스트 전체 통과
- [ ] Parity 테스트(7/21·7/22 타임라인) diff 0건 확인
- [ ] E2E 시나리오 전체 통과
- [ ] 테스트 격리(`tmp_path`+fake broker) 검증, 실제 KIS API 호출 0건 확인
- [ ] Render 운영 검증(bootstrap, 시세, 성능, 5초 이내 주문요청, 하루 MOCK 운영, 15:00 잔량 0)

### 푸쉬 위치
https://github.com/hjsophiekim-ai/AI-GAP-2 
main으로 푸쉬하지 말고, MACD2 브랜치로 푸쉬하세요. 
