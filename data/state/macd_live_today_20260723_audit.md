# MACD Live Today Audit — 2026-07-23 KST

**Generated:** 2026-07-23T11:03:30.627064+09:00
**Window:** 09:00 → 2026-07-23T11:03:30.048145
**Artifacts:** `data/state/macd_live_today_20260723_audit.json`

## Verdict

### `NO_TRADES` / trading `NO_TRADES`

실거래(real) 체결 **0건**. 레저 전체(모의 포함) 5건 (그중 mock stub 5건).

### 왜 실거래가 없었나

- macd_hynix_state.mode=mock (not real)
- real_confirm_ok=false
- mutex.enabled=false reason=test
- mutex.macd_auto_trade_on=false
- legacy_auto_trade_truth auto_trade_on=false
- real broker gate: broker create failed: 실전투자 확인 문구가 틀립니다. 'I_UNDERSTAND_REAL_TRADING_RISK'를 정확히 입력하세요.
- worker.alive=false last_tick_at=2026-07-23T10:32:37.471490 age_sec=1852.6

## 1. 프로그램 가동 (아침)

| 항목 | 값 |
|---|---|
| mode | `mock` |
| auto_trade_on | `True` |
| warmup | `True (WARMUP_READY)` |
| worker.alive | `False` |
| stale_worker | `False` |
| last_tick_at | `2026-07-23T10:32:37.471490` |
| tick_age_sec | `1852.6` |
| avg_interval | `9.963` |
| scheduler_alive | `True` |
| last_flag / current | `HOLD / HOLD` |
| armed_at | `2026-07-23T10:03:05` |
| position_flat | `True` |
| pipeline_all_null | `True` |
| flag_events_today | `False` |
| decision_trace | `False` |
| prices (state) | `{'hynix': 1901000.0, 'long': 15355.0, 'inverse': 10475.0, 'updated_at': '2026-07-23T10:32:40.697305'}` |
| mutex | `{'owner': 'NONE', 'enabled': False, 'macd_auto_trade_on': False, 'mode': 'mock', 'updated_at': '2026-07-23T10:32:40.626418', 'git_sha': 'abf36c3', 'reason': 'test'}` |
| updated_at | `2026-07-23T10:33:51.279186` |

## 2. 실체결 (fills)

**실거래 체결 0건.** (아래는 레저의 mock E2E stub — 가격 10000 고정, 시그널 시각과 불일치)

| 시각 | mode | 종목 | side | 가격 | 수량 | signal_id | 분류 |
|---|---|---|---|---:|---:|---|---|
| 2026-07-23T09:32:00.178014 | mock | 0193T0 | BUY | 10000.0 | 500 | `MACD3M:UP_RED:2026-07-23T10:00:00` | MOCK_E2E_STUB |
| 2026-07-23T09:32:38.668820 | mock | 0193T0 | BUY | 10000.0 | 500 | `MACD3M:UP_RED:2026-07-23T10:00:00` | MOCK_E2E_STUB |
| 2026-07-23T09:33:43.481902 | mock | 0193T0 | BUY | 10000.0 | 500 | `MACD3M:UP_RED:2026-07-23T10:00:00` | MOCK_E2E_STUB |
| 2026-07-23T09:33:44.176772 | mock | 0193T0 | SELL | 10000.0 | 500 | `MACD3M:DOWN_BLUE:2026-07-23T11:00:00` | MOCK_E2E_STUB |
| 2026-07-23T09:33:44.645722 | mock | 0197X0 | BUY | 10000.0 | 500 | `MACD3M:DOWN_BLUE:2026-07-23T11:00:00` | MOCK_E2E_STUB |

### PnL (실거래)

| 구분 | 값 |
|---|---:|
| Round-trips | 0 |
| Open position | flat |
| Realized net | 0 |
| Return % | 0.00% |

## 3. 재구성 플래그 (signed-B, completed 3m)

오늘 완료 3m 봉 **41**개 (워밍업 포함 전체 141). 첫/마지막: `2026-07-23T09:00:00` → `2026-07-23T11:00:00`.

### Live-arm 시그널 (`evaluate_macd_direction` + direction_state)

| bar_close | flag | reason | hist_last3 | signal_id |
|---|---|---|---|---|
| 2026-07-23T09:06:00 | **UP_RED** | UP_RED_FIRST_TURN | [-1460.345725, 2928.736204, 5306.6288] | `MACD3M:UP_RED:2026-07-23T09:03:00` |
| 2026-07-23T10:27:00 | **DOWN_BLUE** | DOWN_BLUE_FIRST_TURN | [1188.424681, -267.123223, -1350.797818] | `MACD3M:DOWN_BLUE:2026-07-23T10:24:00` |

### Onset edges (`collect_signed_hist_two_turn_signals`)

| bar_close | flag | hist_last3 | signal_id |
|---|---|---|---|
| 2026-07-23T09:06:00 | **UP_RED** | [-1460.345725, 2928.736204, 5306.6288] | `MACD3M:UP_RED:2026-07-23T09:03:00` |
| 2026-07-23T10:27:00 | **DOWN_BLUE** | [1188.424681, -267.123223, -1350.797818] | `MACD3M:DOWN_BLUE:2026-07-23T10:24:00` |

### Flag vs 실제 주문

| 재구성 시그널 | 실제 주문 | 판정 |
|---|---|---|
| `MACD3M:UP_RED:2026-07-23T09:03:00` @ 2026-07-23T09:06:00 | 없음 (실거래 0) | **MISS** — mode/mutex/real_confirm 차단 |
| `MACD3M:DOWN_BLUE:2026-07-23T10:24:00` @ 2026-07-23T10:27:00 | 없음 (실거래 0) | **MISS** — mode/mutex/real_confirm 차단 |

### Worker truncated feed (state와 hist 정합)

State live diag received only ~30 1m bars (10:03–10:32); morning 09:00–10:02 missing → EMA/hist ≠ full-day truth trunc hist@10:32=`[7512.167177, 5894.586843, 4402.404676]` vs state=`[7513.802402, 5896.100968, 4403.806667]` match≈`True`.

| under truncation | value |
|---|---|
| eval@10:30 | `{'flag': 'HOLD', 'reason': 'HOLD_NO_PATTERN', 'hist_last3': [7512.167177, 5894.586843, 4402.404676], 'new_signal': False}` |
| trunc arm | `MACD3M:UP_RED:2026-07-23T10:06:00` @ 2026-07-23T10:09:00 |

State `armed_at=10:03` / `MACD3M:UP_RED:…10:00:00`는 mock E2E 레저·latency 오염과 겹침. Truncated 재구성 arm은 `…10:06:00` (10:09 close).

## 4. Counterfactual (would-have) — 실행되지 않음

가정: budget 1,000,000, fill=`next_1m_open after bar_close; TradeCostEngine round-trip`.

| 지표 | 값 |
|---|---:|
| Closed RT | 1 |
| Closed net | 2,704 |
| Open MTM net | 48,933 |
| Total net (incl open) | 51,636 |
| Return % vs budget | 5.1636% |

| entry→exit | sym | dir | qty | entry | exit/mark | net | ret% |
|---|---|---|---:|---:|---:|---:|---:|
| 2026-07-23T09:06:00→2026-07-23T10:27:00 | 0193T0 | UP_RED | 64 | 15490.0 | 15540.0 | 2,704 | 0.2727% |
| 2026-07-23T10:27:00→MTM 2026-07-23T11:03:30.048145 | 0197X0 | DOWN_BLUE | 96 | 10310.0 | 10825.0 | 48,933 | 4.9439% |

## 5. 데이터 소스

- **000660**: today_1m=122 `{'first': '2026-07-23T09:00:00', 'last': '2026-07-23T11:01:00'}` | prior_tail=300 | latest=1875000.0 @ 2026-07-23T11:01:00
- **0193T0**: today_1m=122 `{'first': '2026-07-23T09:00:00', 'last': '2026-07-23T11:01:00'}` | prior_tail=0 | latest=14875.0 @ 2026-07-23T11:01:00
- **0197X0**: today_1m=122 `{'first': '2026-07-23T09:00:00', 'last': '2026-07-23T11:01:00'}` | prior_tail=0 | latest=10825.0 @ 2026-07-23T11:01:00
