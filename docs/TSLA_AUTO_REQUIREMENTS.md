# TSLA_AUTO Requirements

Current US market-session policy: TSLA_AUTO uses
`app.trading.tsla_auto.market_session.get_us_market_state()` as the single
source of truth. Any older fixed-time examples below are superseded by:
America/New_York regular session open/actual exchange close, `zoneinfo` DST,
entry block at actual close minus 15 minutes, and forced liquidation at actual
close minus 10 minutes. KST values are display conversions only.

표시명: **TSLA_AUTO** · strategy_id: `TSLA_AUTO` · 예정 모듈 경로: `app/trading/tsla_auto/`

이 문서는 요구사항(무엇을/왜)을 정의한다. 실제 계산식·필드·상태 흐름 등 기술 세부사항은
[`docs/TSLA_AUTO_LOGIC.md`](./TSLA_AUTO_LOGIC.md)를, 기존 MACD2 파일별 재사용 가능
여부는 [`docs/TSLA_AUTO_COPY_MAP.md`](./TSLA_AUTO_COPY_MAP.md)를 참조한다.

**이 문서는 설계 문서이며, 이 시점까지 코드/환경변수/운영 데이터는 전혀 수정하지 않았다.**

---

## 0. 근거 자료

이 문서를 작성하기 위해 실제로 읽고 분석한 파일:

- `app/trading/macd2/` 전체 (`config.py`, `models.py`, `signal_engine.py`, `market_data.py`,
  `major_flag_filter.py`, `order_executor.py`, `broker_adapter.py`, `worker.py`, `service.py`,
  `state_store.py`, `ledger.py`, `risk_exit.py`, `__init__.py`)
- `app/ui/pages/11_MACD_자동매매2.py`
- `docs/MACD2_LOGIC.md`
- `tests/macd2/` (파일 목록·주요 테스트 시나리오)
- `app/trading/kis_client.py` (국내주식 전용, 1475줄)
- `app/data_sources/kis_overseas_minute.py` (해외 시세/분봉, MU 전용으로 이미 운영 중)
- `app/data_sources/auto_market_collector.py` (해외 현재가 호출 경로 중복 사례)
- `app/config.py` (`get_kis_account_config`)
- `app/trading/broker_base.py`, `app/trading/broker_factory.py`
- `app/trading/trading_cost_engine.py`
- `app/trading/strategy_ownership.py`
- `app/utils/data_paths.py`
- 리포지토리 전체에서 `TSLT` 문자열 검색 (결과 없음 — §11 참조)

---

## 1. 프로그램 기본 정의

| 항목 | 값 |
|---|---|
| 표시명 | TSLA_AUTO |
| strategy_id | `TSLA_AUTO` |
| 모듈 경로(예정) | `app/trading/tsla_auto/` |
| 브로커 | 한국투자증권(KIS) 해외주식 Open API |
| 신호 기준 종목 | `TSLA` (나스닥) |
| UP_RED 매수 대상 | `TSLL` |
| DOWN_BLUE 매수 대상 | `TSLZ` |

원칙:

1. `TSLA` 자체는 어떤 경우에도 매수하지 않는다 — MACD 신호 계산 전용 입력이다(MACD2의 `000660`과
   동일한 역할).
2. `TSLL`/`TSLZ` 가격만 주문·체결·잔고·손절·Profit Lock·손익 계산에 사용한다.
3. `TSLL`과 `TSLZ`를 동시에 보유하지 않는다.
4. 미국 정규장(09:30~16:00 ET)에서만 거래한다. 프리마켓·애프터마켓 신규진입은 금지한다(§5).
5. 오버나이트 포지션을 절대 만들지 않는다 — 정규장 마감 전 강제청산으로 항상 그 날 안에 flat이
   되어야 한다.
6. `TSLL`/`TSLZ`는 TSLA 일별 수익률을 레버리지/역방향으로 추종하도록 설계된 상품이며, 실제
   장중 수익률은 정확한 배수와 일치하지 않을 수 있다(추적오차) — 이 사실을 UI·문서 어디에도
   숨기지 않는다.

---

## 2. 기존 MACD2 분석 요약

전수 분석 결과는 `docs/TSLA_AUTO_COPY_MAP.md`에 파일별 표로 정리했다. 요약:

- **그대로 유지되는 로직 구조**: 완성 3분봉 MACD(12,26,9, `adjust=False`) crossover, 진행봉
  shadow 전용, 동일 completed bar 1회 평가, `signal_id` dedup, quote stale 재조회, 주문가능금액·
  수량 조회 후 일반 지정가 주문, 주문번호 확인 → 체결 polling → 부분체결 반영 → 실제 잔고
  재확인, 반대신호 전환(매도→체결확인→잔고0→매수), Stop Loss/Profit Lock/강제청산의 우선순위
  구조, 원장·state·UI 진단 패턴, Worker 단일 인스턴스, 선택형 강한 플래그 필터.
- **그대로 옮길 수 없는 부분**: 시간대(KST→America/New_York), 세션 경계(09:00 KST→09:30 ET),
  브로커 계층(국내 KRX 주문/시세 TR → 해외주식 TR — 이 저장소에는 해외 주문/잔고 TR 호출 코드가
  아직 없음), 통화(KRW→USD, 환전 개념 추가), 휴장일 계산(한국 공휴일→미국 공휴일+DST+조기폐장),
  `major_flag_filter.py`의 VWAP 계산(한국 정규장 시각(`config.KST`/`SESSION_OPEN`/
  `FORCE_LIQUIDATE_AT`)에 하드코딩되어 있어 미국 세션 기준으로 다시 작성해야 함).
- **재작성이 필요한 부분**: 해외주식 주문/잔고/주문가능금액·수량 조회는 이 저장소에 선례가
  없다 — `app/data_sources/kis_overseas_minute.py`가 제공하는 것은 시세·분봉뿐이다. 이 부분은
  전부 신규 작성 대상이며, 공식 KIS 문서 확인 전까지는 `KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`로
  표시한다(§6, `TSLA_AUTO_LOGIC.md` §KIS 해외 API).

---

## 3. MACD 신호 로직 (요구사항 레벨)

- 신호 원본: TSLA 미국 정규장 1분봉 단일 원본(진행봉·현재가로 confirmed MACD를 계산하지 않음).
- 3분봉: America/New_York 09:30 기준 왼쪽 라벨(09:30~09:32 → `bar_start_at=09:30`).
- 완성된 1분봉 3개가 모두 있어야 3분봉을 생성한다(MACD2의 2026-07-31 수정과 동일 원칙 —
  `docs/TSLA_AUTO_LOGIC.md` §데이터 참조).
- MACD(12,26,9), `adjust=False`, 완성 3분봉 close만 사용.
- `previous_diff ≤ 0, current_diff > 0` → `UP_RED` / `previous_diff ≥ 0, current_diff < 0` →
  `DOWN_BLUE`.
- 신호별 주문 대상: `UP_RED→TSLL`, `DOWN_BLUE→TSLZ`.
- 모든 시간 필드(`flag_time`/`bar_end_at`/`evaluated_at`/`detected_at`/`order_requested_at`)는
  ET와 KST를 함께 표시한다.
- `signal_id`는 **ET 기준** `bar_start_at`으로 만든다: `YYYYMMDD_HHMMSS_DIRECTION`
  (예: `20260730_104200_UP_RED`).

세부 알고리즘·의사코드는 `docs/TSLA_AUTO_LOGIC.md` §1~§5에 있다.

---

## 4. 강한 플래그 옵션 (요구사항 레벨)

- UI 토글 "강한 플래그만 거래", 기본값 OFF.
- OFF: 모든 LIVE_CONFIRMED 플래그가 주문권한을 가진다.
- ON: Hybrid MAJOR_FLAG 승인 신호만 주문권한을 가진다. 원본 confirmed 플래그는 필터와 무관하게
  전부 통계·원장에 기록된다. 탈락 신호는 `FILTERED_OUT`으로 종료되며, 탈락한 `signal_id`는
  재평가·재주문하지 않는다.
- 초기값은 MACD2의 실제 운영값을 그대로 복제한다(계산은 TSLA 완성 3분봉만 사용 — TSLL/TSLZ의
  이후 가격이나 미래 봉은 어떤 경우에도 필터 판단에 넣지 않는다). 실제 값과 복제 근거는
  `docs/TSLA_AUTO_LOGIC.md` §강한 플래그 필터에 표로 정리했다.

---

## 5. 미국시장 시간

| 항목 | 기본값 |
|---|---|
| 내부 시간대 | `America/New_York` (DST 자동 적용, `zoneinfo` 사용 — 한국시간 하드코딩 금지) |
| 정규장 | 09:30~16:00 ET |
| 프리마켓/애프터마켓 신규진입 | 금지 |
| 신규진입 마감(일반) | 15:40 ET |
| 신규진입·반대 ETF 신규매수 전부 금지(최종) | **15:45 ET** — §8 참조. 15:40 이후 이미
  차단되는 신규 flat 진입에 더해, 15:45부터는 반대신호로 인한 반대 ETF 신규매수까지 예외 없이
  전부 금지한다. 기존 보유 ETF의 매도/청산(반대신호 SELL 레그, Stop Loss, Profit Lock,
  강제청산)은 15:45 이후에도 계속 허용된다. |
| 강제청산 시작 | 15:50 ET |
| 최종 잔고 0 확인 | 15:58 ET |

필수: 미국 휴장일·조기폐장일 처리, 조기폐장 시 위 세 시각(신규진입 마감/강제청산/최종잔고
확인) 자동 축소, UI에는 ET·KST 동시 표시. 세부 계산 규칙과 확인 필요 항목은
`docs/TSLA_AUTO_LOGIC.md` §미국시장 캘린더에 정리했다.

---

## 6. KIS 해외주식 API

**결론(중요)**: 이 저장소에는 해외 **시세·분봉** 호출 코드(`app/data_sources/kis_overseas_minute.py`,
TR `HHDFS00000300`/`HHDFS76950200`)는 이미 존재하고 실제 운용 중(MU 종목)이지만, 해외 **주문·
잔고·주문가능금액/수량·정정취소·미체결·체결내역**을 호출하는 코드는 이 저장소 어디에도 없다.
따라서 주문/잔고 관련 TR_ID·엔드포인트·파라미터·응답 필드는 전부
`KIS_OVERSEAS_API_CONFIRMATION_REQUIRED`로 표시했다 — 실제 값은 KIS Open API 공식 포털/GitHub
샘플로 재확인한 뒤 구현 단계에서 확정해야 한다. 상세 표와 미확인 항목별 영향/차단 여부/대체
검증 방법은 `docs/TSLA_AUTO_LOGIC.md` §KIS 해외주식 API에 정리했다.

기존 KIS 인증 공통부 재사용 가능 여부(확인됨):

- `app/config.py:get_kis_account_config()`와 `kis_overseas_minute.py:_load_credentials()`가
  **동일한** 환경변수 이름(`KIS_REAL_APP_KEY`/`KIS_REAL_APP_SECRET`/`KIS_REAL_ACCOUNT_NO` 등,
  mock도 동일 패턴)을 사용한다 — KIS는 계좌 단위로 앱키/시크릿을 발급하며 국내·해외 TR을
  같은 키로 호출하므로, TSLA_AUTO는 **새 앱키를 발급받을 필요가 없다**.
  국내주식 주문 TR 함수(`app/trading/kis_client.py`의 `buy`/`sell`/`get_balance` 등)는
  호출하지 않되, OAuth 토큰 발급·캐시·재시도·rate-limit throttle 계층은 재사용 가능하다.

---

## 7. 주문 로직 (요구사항 레벨)

신규 BUY 13단계, 반대신호 절차, 금지사항은 사용자 원문 그대로 유지한다(세부 의사코드는
`docs/TSLA_AUTO_LOGIC.md` §주문 로직). 핵심 요약:

1. TSLA confirmed 플래그 생성 → (필터 ON이면) 승인 확인 → 대상 ETF 선택 → fresh 매도 1호가
   조회 → 실제 USD 주문가능금액·수량 조회 → `min(UI 예산, 실제 가능금액)` → 수수료/변동 여유
   반영 → 정수 수량 → 일반 지정가 주문 → 주문번호 확인 → 체결 polling → 부분체결 반영 →
   미체결 잔량 취소 → 실제 잔고 재확인.
2. 반대신호는 기존 ETF 전량매도 → 체결확인 → 잔고 0 확인 → 반대 ETF 매수 순서를 강제한다.
3. 금지: TSLL·TSLZ 동시보유, 잔고 0 확인 전 반대매수, 같은 `signal_id` 재주문, 같은 방향
   추가매수, 주문번호 없는 position 생성, 주문거절을 성공 처리, stale 호가에서 임의 시장가 전환.

---

## 8. 손절·Profit Lock·전환

> 아래 내용은 최종 확정 사양이다(작성 중 사용자 지시로 교체·보강됨). MACD2 대비 **새로 추가된
> 규칙**은 명시적으로 표시했다.

- 실제 TSLL/TSLZ 평균체결가 대비 **-1.5% 손절** 유지(MACD2 `STOP_LOSS_NET_PCT` 그대로 복제).
- 실제 ETF 실시간 가격으로 감시(호가/quote 캐시가 아니라 손절 판단 시점의 fresh 가격).
- 기존 Profit Lock **활성화 기준과 giveback을 코드에서 읽어 그대로 유지**(MACD2
  `PROFIT_LOCK_ACTIVATE_NET_PCT`/`PROFIT_LOCK_GIVEBACK_PP`를 초기값으로 복제 — 실제 수치는
  `docs/TSLA_AUTO_LOGIC.md` §손절·Profit Lock·전환 표 참조).
- Stop Loss·Profit Lock·강제청산은 **강한 플래그 필터보다 우선**한다(MACD2와 동일 우선순위).
- **[신규]** 손절 후 **같은 방향** 재진입은 15분간 금지한다.
- **[신규]** 그 이후 새로운 confirmed 플래그가 발생했고, 그 플래그의 Hybrid 점수가
  `max(85, 그 시점에 적용되는 문턱값)` 이상이면, **하루에 단 1회만** 같은 방향 재진입을
  예외적으로 허용한다. 이 예외 평가는 "강한 플래그만 거래" 토글의 ON/OFF와 무관하게 항상
  수행한다(=꺼져 있어도 손절 직후 15분 동안은 이 안전장치를 적용한다).
- 반대 전환은 기존과 동일한 순서를 유지한다: 기존 ETF 매도 체결 → 잔고 0 확인 → 반대 ETF 매수.
- **[신규]** **15:45 ET 이후에는 신규진입과 반대 ETF 신규매수를 예외 없이 전부 금지**한다.
  기존 보유 ETF의 매도/청산(반대신호의 SELL 레그, Stop Loss, Profit Lock, 강제청산)은 계속
  허용된다 — 즉 15:45 이후는 "팔 수는 있어도 새로 살 수는 없는" 상태다.

이 규칙은 MACD2에는 없는 TSLA_AUTO 전용 로직이며, `risk_exit.py`/`worker.py` 대응 파일에서
`COPY_WITH_US_MARKET_CHANGE`가 아니라 **신규 로직 추가**로 분류한다(`TSLA_AUTO_COPY_MAP.md`
참조).

---

## 9. 주문수량·통화 (요구사항 레벨)

기준 통화 USD. `TSLA_AUTO_BUDGET_USD`, `TSLA_AUTO_ORDER_USAGE_RATIO`(기본 0.995), 자동환전
기본 OFF, 소수점 주식 거래 금지. 계산식:

```text
usable_usd = min(UI USD 예산, KIS 실제 해외주식 USD 주문가능금액)
budget_qty = floor(usable_usd × order_usage_ratio ÷ 대상 ETF 지정가)
final_qty  = min(budget_qty, KIS 대상 ETF 주문가능수량)
```

기록 필드: `target_symbol`, `available_usd`, `usable_usd`, `bid1`, `ask1`, `order_price`,
`budget_qty`, `available_qty`, `final_qty`, `expected_notional_usd`, `expected_fee_usd`.
세부는 `docs/TSLA_AUTO_LOGIC.md` §주문수량·통화.

---

## 10. 위험관리 초기값 (MACD2 실제값 복제 대상)

전부 코드(`app/trading/macd2/config.py`, `risk_exit.py`)에서 직접 읽은 실제 값이다:

| 항목 | MACD2 실제값 | TSLA_AUTO 초기 복제값 | TSLL/TSLZ 재검토 필요성 |
|---|---|---|---|
| Stop Loss | `STOP_LOSS_NET_PCT = -1.5%` | -1.5% (동일) | 레버리지/인버스 ETF는 트래킹 오차·변동성이 커서 -1.5%가 너무 타이트할 수 있음 — 운영 데이터 축적 후 재검토 |
| Profit Lock 활성화 | `PROFIT_LOCK_ACTIVATE_NET_PCT = +1.5%` | +1.5% (동일) | 상동 |
| Profit Lock giveback | `PROFIT_LOCK_GIVEBACK_PP = 0.8%p` | 0.8%p (동일) | 상동 |
| quote stale 기준 | `QUOTE_MAX_AGE_SEC = 10.0` | 10.0초 (동일, 초기값) | 해외 시세 API 지연 특성 확인 필요 |
| history stale 기준 | `HISTORY_STALE_MAX_SEC = 180.0` | 180.0초 (동일, 초기값) | 동일 |
| 체결 polling 주기·최대시간 | `ORDER_FILL_POLL_INTERVAL_SEC=1.0` / `ORDER_FILL_POLL_MAX_SEC=60.0` | 동일 (초기값) | 해외 체결 확인 API 응답속도 확인 필요(§KIS_OVERSEAS_API_CONFIRMATION_REQUIRED) |
| 신규진입 마감 | `NEW_ENTRY_CUTOFF = 14:55 KST` (MACD2, 국내장 마감 5분 전) | `15:40 ET` (§5) | 시간대만 치환, 개념은 동일 |
| 강제청산 | `FORCE_LIQUIDATE_AT = 15:00 KST` | `15:50 ET` 시작, `15:58 ET` 최종 확인(§5) | 미국 조기폐장일 자동 축소 필요 |
| 최대 일일 손실 | MACD2에 명시적 설정 없음(개별 Stop Loss만 존재) | **미정 — 신규 정의 필요** | TSLA_AUTO 도입 시 결정 필요 항목으로 별도 표시 |
| 최대 거래 횟수 | `MAJOR_MAX_DAILY_ENTRIES = 4` (강한 필터 ON일 때만) | 4회 (동일, 초기값) | 필터 OFF일 때의 일일 최대 거래 횟수는 MACD2에도 없음 — TSLA_AUTO도 미정 |

"최대 일일 손실"과 "필터 OFF 상태의 최대 거래 횟수"는 MACD2에 대응 값이 없어 그대로 복제할
수 없다 — 이 두 항목은 **결정이 필요한 미정 항목**으로 별도 표시했다(구현 차단 사유는 아니며,
비워두거나 매우 큰 기본값으로 시작해도 되지만 사용자 확인 후 확정 권장).

---

## 11. 비용·손익 (요구사항 레벨)

KIS 계좌 실제 미국주식 수수료율은 설정값(config.yaml 신규 섹션)으로 관리하고 코드에 고정하지
않는다. 비용 항목: TSLL/TSLZ 매수·매도 수수료, SEC Section 31 fee, FINRA TAF, 기타 KIS 체결
비용, 환전수수료·스프레드, 슬리피지. 실제 KIS 체결내역 비용이 제공되면 추정값보다 우선한다.

```text
Gross USD PnL = 매도 체결금액 - 매수 체결금액
Net USD PnL   = Gross USD PnL - 총비용
```

Net KRW, 적용환율, 연간 실현손익, 양도소득세 참고 추정은 별도 항목으로 표시하고 **거래별
Net에서 차감하지 않는다**. 세부는 `docs/TSLA_AUTO_LOGIC.md` §비용·손익.

---

## 12. MACD2와 완전 분리

예정 경로: `app/trading/tsla_auto/`, `tests/tsla_auto/`, `data/state/tsla_auto/`,
`data/ledger/tsla_auto/`, `data/cache/tsla_auto/`, `data/runtime/tsla_auto/`, 전용 UI 페이지.

**절대 공유 금지**: runtime state, position, pending signal, `processed_signal_ids`, signal
ledger, execution ledger, cache, Worker singleton, Service singleton, lock file, 강제청산
상태, 예산, 일일 통계, `strategy_version`.

**공유 가능**: KIS 저수준 인증·토큰 관리자(§6), 순수 MACD 계산 함수(같은 수식을 각자 모듈에
복제 — import 공유 아님, §COPY_MAP 참조), 시장 비종속 로깅·시간 유틸.

같은 KIS 계좌를 쓰더라도: 국내 MACD2 포지션을 TSLA_AUTO 포지션으로 인식하지 않고, `TSLL`/
`TSLZ` 이외 해외주식 보유분을 전략 포지션으로 인식하지 않으며, TSLA_AUTO 강제청산은 `TSLL`/
`TSLZ`의 **전략 보유수량만** 대상으로 한다(계좌 전체 잔고를 청산하지 않음).

별도 프로세스 락: `macd2_worker.lock` / `tsla_auto_worker.lock`.

**TSLT 검색 결과**: 리포지토리 전체(`*.py`, `*.md`)에서 `TSLT` 문자열은 **0건** 검색됐다 —
현재 코드에 이 오기(誤記)가 실존하지는 않는다. 다만 요구사항에 명시된 대로, TSLA_AUTO 구현
단계에서 국내 종목코드·국내 주문 함수·`TSLT`(존재할 경우 잘못된 상승 ETF 코드)를 호출하면
테스트가 실패하도록 가드 테스트를 설계한다(`docs/TSLA_AUTO_LOGIC.md` §테스트,
`TSLA_AUTO_COPY_MAP.md` 위험 열 참조).

---

## 13. 동시 실행 안전성

MACD2와 TSLA_AUTO 동시 실행 필수 요구사항(그대로 채택):

- Worker 인스턴스 각각 1개, command 파일 분리, state·ledger·cache 완전 분리.
- 국내장·미국장 시간 독립(서로 다른 시간대/휴장 캘린더를 각자 평가).
- 한 전략의 STOP이 다른 전략을 중단하지 않고, 한 전략의 강제청산이 다른 전략 포지션을 매도하지
  않는다.
- 계좌조회 캐시는 시장·통화·전략별로 분리한다.
- API rate limit 공통 조정기(`kis_client.py`의 `_rate_limit_lock`/`_throttle`, mode 단위)를
  재사용하더라도 요청을 보낸 전략을 로그에 남긴다.
- 토큰 갱신 경합 방지, 국내·해외 주문번호 namespace 혼용 금지.
- `app/trading/strategy_ownership.py`의 국내 3파전(Enhanced/MACD v1/MACD2) 상호배제 로직에는
  TSLA_AUTO를 **참여시키지 않는다** — TSLA_AUTO는 국내 종목을 전혀 건드리지 않으므로 국내
  주문권한 경합 대상이 아니다(§KIS_OVERSEAS_API_CONFIRMATION_REQUIRED 아님 — 이 부분은 이미
  코드 분석으로 확인된 설계 결정이다).

---

## 14. UI (요구사항 레벨)

TSLA_AUTO 전용 Streamlit 페이지. 표시 항목 전체 목록은 `docs/TSLA_AUTO_LOGIC.md` §UI에
정리했다(READ_ONLY/MOCK/REAL, 계좌 마스킹, USD 예수금·주문가능금액, 현재가·age, 1분봉
수·최신시각, MACD/Signal/diff, confirmed flag, ET·KST 시간, 강한 필터 ON/OFF·score·승인·탈락
이유, 보유 ETF·수량·평균가, 호가·주문가·주문수량, 주문번호·체결·미체결, Stop Loss·Profit
Lock·강제청산, Gross·비용·Net USD/KRW, block_reason, KIS 응답코드·메시지, Worker/Git SHA).
UI는 snapshot 표시와 command 기록만 수행하고 Streamlit rerun이 Worker/주문을 직접 실행하지
않는다(MACD2와 동일 원칙).

---

## 15. 테스트 (요구사항 레벨)

필수 테스트 전체 목록은 `docs/TSLA_AUTO_LOGIC.md` §테스트에 정리했다. 핵심 카테고리:
시세/분봉 수집, warm-up, 09:30 기준 3분봉·DST·휴장/조기폐장, `UP_RED→TSLL`/`DOWN_BLUE→TSLZ`,
`TSLT` 주문 호출 0건(가드), strong filter OFF/ON, 동일 `signal_id` 1회, USD 사이징, 지정가
주문·주문번호·부분체결·잔량취소·잔고재조회, 반대전환 순서, Stop Loss/Profit Lock/강제청산(§8
신규 규칙 포함), MACD2와 state/ledger/Worker 비공유, 국내 주문 함수 호출 0건, 상호 STOP
무간섭, 동일 KIS 토큰 동시성, REAL gate, 해외주식 비용·손익.

---

## 16. 완료 조건(이번 단계)

- [x] `docs/TSLA_AUTO_REQUIREMENTS.md` (본 문서)
- [x] `docs/TSLA_AUTO_LOGIC.md`
- [x] `docs/TSLA_AUTO_COPY_MAP.md`
- [ ] 코드 작성 — **다음 단계에서 사용자 확인 후 진행**
# TSLA_AUTO Requirements Addendum: US Market Session

아래 정책은 본 문서의 과거 고정 시각 설명(예: 15:45 ET, 15:50 ET,
KST 22:30/23:30)을 대체한다. 고정 시각은 예시일 뿐이며 실제 운영
판단에는 사용하지 않는다.

미국주식 자동매매 시간 판단은 `app.trading.tsla_auto.market_session`의
`USMarketSessionState`를 단일 소스로 사용한다. 정규장 시간은
`America/New_York` 기준 실제 거래소 캘린더의 open/close이며, 한국 UI는
`Asia/Seoul` 변환값을 표시한다. 서머타임은 `zoneinfo`가 자동 적용한다.

- 거래일/휴장/조기폐장: `pandas_market_calendars` NYSE 캘린더
- 신규진입 차단: 실제 폐장 15분 전
- 강제청산: 실제 폐장 10분 전
- 프리마켓/애프터마켓/휴장/주말/캘린더 장애: 신규 BUY 차단
- ENTRY_BLOCKED 이후 방향전환은 기존 보유 SELL은 허용, 반대 ETF BUY는 차단
- 세부 정책: `docs/US_MARKET_SESSION_POLICY.md`
