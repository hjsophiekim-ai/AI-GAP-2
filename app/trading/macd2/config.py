"""MACD2 configuration — docs/MACD2_LOGIC.md confirmed defaults.

Strategy-fixed values (never overridden per-request; changing them is a
requirements change, not a runtime setting) are the module-level constants
below. Values the UI/user may change at runtime (mode, budget) are read from
RuntimeState, not from here — this module only supplies their defaults.
"""
from __future__ import annotations

import os
from datetime import time, timedelta, timezone

# KST is a fixed UTC+9 offset with no DST — safe as a plain timezone constant.
KST = timezone(timedelta(hours=9))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default

# ── Symbols (strategy-fixed) ────────────────────────────────────────────────
WATCH_SYMBOL = "000660"  # SK하이닉스 — signal source only, never traded directly
LONG_SYMBOL = "0193T0"  # KODEX 레버리지 — bought on UP_RED
INVERSE_SYMBOL = "0197X0"  # SOL 인버스2X — bought on DOWN_BLUE
TRADE_SYMBOLS = (LONG_SYMBOL, INVERSE_SYMBOL)

# ── Budget (UI-overridable; this is only the default) ──────────────────────
DEFAULT_BUDGET = 10_000_000.0

STRATEGY_NAME = "MACD2"
# 2026-07-27 KIS-parity fix: order authority moved OFF the forming/provisional
# bar and onto the confirmed, completed-3m-bar MACD(12,26,9) crossover — the
# same thing KIS itself charts a flag on. SIGNAL_RULE is now that confirmed
# rule; CONFIRMED_SIGNAL_RULE is kept as an alias (same value) since it is
# still referenced by the UI/tests under its original name. The forming bar
# and Signed-B remain shadow/candidate-only display (PROVISIONAL_SHADOW_RULE),
# never written to the signal ledger and never given order/stat authority.
STRATEGY_VERSION = "20260731_KIS_MACD_COLOR_FLAG_V1"
SIGNAL_RULE = "KIS_MACD_COLOR_FLAG_CONFIRMED"
CONFIRMED_SIGNAL_RULE = SIGNAL_RULE
PROVISIONAL_SHADOW_RULE = "MACD_FORMING_CANDIDATE_SHADOW"
LEGACY_SIGNAL_RULE = "SIGNED_B_LEGACY"

# Order-sizing safety margin (docs §9: "수수료·호가 변동을 고려한 안전 여유") is no
# longer a fixed placeholder ratio here — docs/MACD2_LOGIC.md §21 flagged the old
# ORDER_SAFETY_MARGIN_PCT=0.5 constant as an unconfirmed placeholder. It is now
# computed per-order from real inputs (buy fee rate from config.yaml
# trading_cost + KRX tick size for the order price) by
# order_executor.compute_order_safety_margin_pct(); see that function's
# docstring and docs/MACD2_LOGIC.md §9/§21 for the rationale.

# ── MACD (strategy-fixed) ───────────────────────────────────────────────────
EMA_FAST = 12
EMA_SLOW = 26
EMA_SIGNAL = 9
# Old A-F `signals_B`: first eligible bar index is 26 → len(bars) must be > 26.
SIGNAL_MIN_BAR_INDEX = 26

# ── Warm-up (strategy-fixed) ────────────────────────────────────────────────
WARMUP_3M_BARS_MIN = 100
WARMUP_1M_BARS_MIN = WARMUP_3M_BARS_MIN * 3  # >=300

# ── Risk / exit (strategy-fixed) ────────────────────────────────────────────
STOP_LOSS_NET_PCT = -1.5
PROFIT_LOCK_ACTIVATE_NET_PCT = 1.5
PROFIT_LOCK_GIVEBACK_PP = 0.8

EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_PROFIT_LOCK = "PROFIT_LOCK"
EXIT_OPPOSITE_SIGNAL = "OPPOSITE_SIGNAL"
EXIT_FORCED_LIQUIDATION = "FORCED_LIQUIDATION"
EXIT_USER_LIQUIDATION = "USER_LIQUIDATION"  # UI "자동매매 중지 및 일괄매도" 버튼

# ── Session timing (strategy-fixed, KST) ───────────────────────────────────
SESSION_OPEN = time(9, 0)
NEW_ENTRY_CUTOFF = time(14, 55)
FORCE_LIQUIDATE_AT = time(15, 0)

# ── Worker (strategy-fixed) ─────────────────────────────────────────────────
WORKER_INTERVAL_SEC = 5.0
WORKER_TICK_MEAN_MAX_SEC = 5.5
WORKER_TICK_P95_MAX_SEC = 7.0
WORKER_TICK_MAX_SEC = 10.0
SIGNAL_TO_ORDER_REQUEST_MAX_SEC = 5.0
WORKER_STALL_AGE_SEC = 15.0

# ── Market data validity (strategy-fixed) ──────────────────────────────────
QUOTE_MAX_AGE_SEC = 10.0
PENDING_SIGNAL_RETRY_SEC = 30.0
FLAT_POSITION_RECONCILE_INTERVAL_SEC = 30.0

# 2026-07-27 QUOTE_STALE 처리 수정: confirmed 신호가 quote stale로 막히면
# 그 자리에서(같은 tick 안에서) 강제 재조회 후 최대 이 횟수만큼, 이 간격으로
# 재검증한다. 신호 확정(detected_at) 후 이 시간을 넘기면 더 이상 뒤늦게
# 주문하지 않고 MISSED_SIGNAL_QUOTE_STALE로 종료·기록한다.
QUOTE_STALE_RETRY_MAX_ATTEMPTS = 3
QUOTE_STALE_RETRY_INTERVAL_SEC = 1.0
QUOTE_STALE_MAX_WAIT_SEC = 15.0
MISSED_SIGNAL_QUOTE_STALE = "MISSED_SIGNAL_QUOTE_STALE"

# 2026-07-27 momentary-crossing fix: a single-tick provisional forming-bar
# crossover is only a CANDIDATE, never an order — it is confirmed as a
# Primary onset only once the SAME direction is still present on a LATER
# fresh quote tick at least this many seconds after the first sighting.
# (candidate/shadow display only since the 2026-07-27 KIS-parity fix — never
# order/stat authority any more.)
PROVISIONAL_CONFIRM_MIN_GAP_SEC = 0.0

# 주문 성공 응답만으로 체결로 간주하지 않고, 주문번호로 실제 체결/잔고를
# 재조회해 확인하는 최대 대기시간·간격 (docs 2026-07-27 체결확인 fix).
ORDER_FILL_POLL_MAX_SEC = 60.0
ORDER_FILL_POLL_INTERVAL_SEC = 1.0

# KIS 1분봉(history)과 실시간 quote의 단위·시각 불일치 감지 허용범위 (docs
# 2026-07-27 fix) — 정상 범위를 벗어나면 주문을 차단한다. 10배/0.1배 스케일
# 오차는 market_data._normalize_quote_price()가 이미 보정하므로, 여기서는
# 그 보정 이후에도 설명되지 않는 큰 괴리만 잡아낸다.
QUOTE_HISTORY_PRICE_RATIO_MIN = 0.5
QUOTE_HISTORY_PRICE_RATIO_MAX = 2.0
# 정규장 중 1분봉 history의 최신 시각이 이보다 오래되면(당일 데이터가 갱신되지
# 않는 상태) 시각 불일치로 간주한다.
HISTORY_STALE_MAX_SEC = 180.0

# 전일 warm-up 조회(주식일별분봉조회) 중 KIS 서버 일시 오류(500 등)를 "해당
# 날짜에 데이터 없음(휴장일)"으로 오인해 더 이전 날짜로 잘못 넘어가면 EMA
# seed가 실제 KIS 차트와 달라진다 (2026-07-27 3플래그 재현 검증에서 발견 —
# 정상 거래일 20260724 조회가 500으로 실패해 20260723으로 잘못 대체됨). 응답이
# 명시적 오류를 동반한 빈 결과일 때만 재시도하고, 오류 없는 빈 결과(진짜 휴장일)
# 는 즉시 다음 날짜로 넘어간다.
PRIOR_DAY_FETCH_RETRIES = 5
PRIOR_DAY_FETCH_RETRY_DELAY_SEC = 2.0

# 백워드 페이징(주식일별분봉조회/inquire-time-itemchartprice)에서 연속 요청을
# 텀 없이 쏘면 KIS 초당 거래건수 제한에 걸려 일부 페이지가 오류 없이 조용히
# 빈 결과로 돌아온다 (2026-07-27 발견 — page당 실수신 30건인데 지연 없이
# 여러 페이지를 연속 요청하면 중간 페이지가 누락됨). 페이지 사이에 짧은
# 페이싱을 둔다.
KIS_PAGE_FETCH_PACING_SEC = 0.4

# ── Feature flags (strategy-fixed per docs; not user-configurable) ────────
CONTINUATION_REENTRY_ENABLED = False
OPENING_PROBE_ENABLED = False

# ── Optional Hybrid MAJOR_FLAG filter (order gate only; confirmed flags unchanged) ──
# UI toggle defaults OFF. Env MACD2_MAJOR_FILTER_DEFAULT may override the
# cold-start default; runtime state / UI command still wins after start.
MAJOR_FILTER_VERSION = "MAJOR_FILTER_HYBRID_V2_STRONG_PROFILE"
MAJOR_FILTER_DEFAULT = _env_bool("MACD2_MAJOR_FILTER_DEFAULT", False)

MAJOR_ENTRY_SCORE_MIN = _env_float("MACD2_MAJOR_ENTRY_SCORE_MIN", 65.0)
MAJOR_REVERSAL_SCORE_MIN = _env_float("MACD2_MAJOR_REVERSAL_SCORE_MIN", 75.0)
MAJOR_FAST_REVERSAL_SCORE_MIN = _env_float("MACD2_MAJOR_FAST_REVERSAL_SCORE_MIN", 82.0)
MAJOR_STRONG_START = time(10, 30)

# Hybrid component tiers (Hybrid V1 — order gate only)
MAJOR_HIST_IMPULSE_T1 = 0.10  # 10 pts
MAJOR_HIST_IMPULSE_T2 = 0.15  # 18 pts
MAJOR_HIST_IMPULSE_T3 = 0.22  # 25 pts
MAJOR_PRICE_IMPULSE_T1 = 0.35  # 15 pts (also price-confirm floor)
MAJOR_PRICE_IMPULSE_T2 = 0.55  # 25 pts
MAJOR_BODY_ATR_T1 = 0.25  # 5 pts
MAJOR_BODY_ATR_T2 = 0.40  # 10 pts
MAJOR_VOLUME_RATIO_T1 = 1.00  # 5 pts
MAJOR_VOLUME_RATIO_T2 = 1.10  # 10 pts
MAJOR_VOLUME_RATIO_T3 = 1.20  # 15 pts
# Legacy single-threshold aliases (tests / older docs may still reference)
MAJOR_HIST_IMPULSE_ATR_MIN = MAJOR_HIST_IMPULSE_T3
MAJOR_PRICE_IMPULSE_ATR_MIN = MAJOR_PRICE_IMPULSE_T1
MAJOR_BODY_ATR_MIN = MAJOR_BODY_ATR_T2
MAJOR_VOLUME_RATIO_MIN = MAJOR_VOLUME_RATIO_T3
MAJOR_SIDEWAYS_EMA_SPREAD_MAX = 0.0007
MAJOR_SIDEWAYS_RANGE_MAX = 0.006
MAJOR_RANGE_BREAKOUT_LOOKBACK = 4
MAJOR_RECENT_RANGE_LOOKBACK = 8
MAJOR_VOLUME_LOOKBACK = 20
MAJOR_ATR_PERIOD = 14
MAJOR_EMA_FAST = 10
MAJOR_EMA_SLOW = 20
MAJOR_MIN_COMPLETED_BARS = 26

MAJOR_MAX_DAILY_ENTRIES = _env_int("MACD2_MAJOR_MAX_DAILY_ENTRIES", 4)
MAJOR_MIN_HOLD_MIN = _env_int("MACD2_MAJOR_MIN_HOLD_MIN", 9)
MAJOR_FAST_REVERSAL_WINDOW_MIN = _env_int("MACD2_MAJOR_FAST_REVERSAL_WINDOW_MIN", 15)
MAJOR_SAME_DIRECTION_REENTRY_MIN = _env_int("MACD2_MAJOR_SAME_DIRECTION_REENTRY_MIN", 18)

# Ledger / UI decision labels (filter gate only — not strategy_version)
MAJOR_APPROVED = "MAJOR_APPROVED"
MAJOR_SCORE_BELOW_THRESHOLD = "MAJOR_SCORE_BELOW_THRESHOLD"
MAJOR_PRICE_CONFIRMATION_FAILED = "MAJOR_PRICE_CONFIRMATION_FAILED"
MAJOR_SIDEWAYS_BLOCK = "MAJOR_SIDEWAYS_BLOCK"
MAJOR_STRONG_PROFILE_FAILED = "MAJOR_STRONG_PROFILE_FAILED"
FILTER_DATA_INSUFFICIENT = "FILTER_DATA_INSUFFICIENT"
FILTER_INPUT_NOT_CROSSOVER = "FILTER_INPUT_NOT_CROSSOVER"
SAME_DIRECTION_POSITION_HELD = "SAME_DIRECTION_POSITION_HELD"
MAJOR_DAILY_ENTRY_LIMIT = "MAJOR_DAILY_ENTRY_LIMIT"
MAJOR_SAME_DIRECTION_COOLDOWN = "MAJOR_SAME_DIRECTION_COOLDOWN"
MAJOR_MIN_HOLD_BLOCK = "MAJOR_MIN_HOLD_BLOCK"
FILTERED_OUT = "FILTERED_OUT"

# ── Isolated MACD2 runtime/ledger paths (never shared with MACD v1) ───────
# Resolved lazily via app.utils.data_paths inside state_store.py/ledger.py so
# tests can monkeypatch those modules' own path constants, not these names.
RUNTIME_STATE_FILENAME = "macd2_runtime.json"
SIGNAL_LEDGER_FILENAME = "macd2_signal_ledger.csv"
EXECUTION_LEDGER_FILENAME = "macd2_execution_ledger.csv"
