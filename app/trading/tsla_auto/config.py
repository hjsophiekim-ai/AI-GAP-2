"""TSLA_AUTO configuration — docs/TSLA_AUTO_LOGIC.md confirmed defaults.

Strategy-fixed values (never overridden per-request) are the module-level
constants below. UI/user-changeable values (mode, budget, strong-filter
toggle) are read from RuntimeState, not from here — this module only
supplies their defaults. Never reuses a MACD2 env var name (docs
§16 — "기존 MACD2 환경변수 이름을 재사용하지 않는다").
"""
from __future__ import annotations

import os
from datetime import time, timedelta, timezone

from app.trading.tsla_auto import market_session

# ET/KST timezones for anything needing a fixed offset display; the
# authoritative session/calendar logic lives in market_session.py (zoneinfo,
# DST-aware) — never hardcode a fixed KST offset for US market timing.
ET = market_session.ET
KST = market_session.KST


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default) -> "float | None":
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip()


def _env_csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name)
    text = default if raw is None or str(raw).strip() == "" else str(raw)
    return tuple(part.strip().upper() for part in text.split(",") if part.strip())


# ── Identity (docs §2/§3 — unique identifiers, never reused from MACD2) ────
STRATEGY_ID = "TSLA_AUTO"
STRATEGY_NAME = "TSLA_AUTO"
STRATEGY_VERSION = "TSLA_AUTO_V1"
SIGNAL_RULE = "TSLA_3M_CONFIRMED_MACD"
STRONG_FILTER_VERSION = "TSLA_STRONG_FLAG_V6_MACD2_PARITY"
WORKER_NAME = "tsla_auto_worker"
SERVICE_NAME = "tsla_auto_service"
WORKER_LOCK_FILENAME = "tsla_auto_worker.lock"

# ── Symbols (strategy-fixed, env-overridable per docs §16 but defaults fixed) ──
SIGNAL_SYMBOL = _env_str("TSLA_AUTO_SIGNAL_SYMBOL", "TSLA")  # signal-only, never traded
LONG_SYMBOL = _env_str("TSLA_AUTO_LONG_SYMBOL", "TSLL")  # bought on UP_RED
INVERSE_SYMBOL = _env_str("TSLA_AUTO_INVERSE_SYMBOL", "TSLZ")  # bought on DOWN_BLUE
TRADE_SYMBOLS = (LONG_SYMBOL, INVERSE_SYMBOL)
MANAGED_LIQUIDATION_SYMBOLS = tuple(dict.fromkeys((
    *TRADE_SYMBOLS,
    *_env_csv("TSLA_AUTO_MANAGED_US_SYMBOLS", "TSLQ,TSLY"),
)))
# Known-wrong legacy ticker guarded against everywhere an order symbol is
# accepted (docs §3/§4 — "TSLT" must never be a valid order target).
FORBIDDEN_SYMBOLS = frozenset({"TSLT"})

# ── Exchange code for KIS overseas TR calls (confirmed usage pattern: "NAS"
# for NASDAQ-listed symbols, verified against app/data_sources/kis_overseas_minute.py
# which already calls EXCD="NAS" for MU). Whether TSLL/TSLZ are also NAS-listed
# is itself flagged KIS_OVERSEAS_API_CONFIRMATION_REQUIRED in the logic doc —
# this default may need per-symbol overrides once confirmed. ─────────────────
EXCHANGE_CODE = _env_str("TSLA_AUTO_EXCHANGE_CODE", "NAS")
QUOTE_EXCHANGE_BY_SYMBOL = {
    SIGNAL_SYMBOL: _env_str("TSLA_AUTO_TSLA_QUOTE_EXCHANGE", "NAS"),
    LONG_SYMBOL: _env_str("TSLA_AUTO_TSLL_QUOTE_EXCHANGE", "NAS"),
    INVERSE_SYMBOL: _env_str("TSLA_AUTO_TSLZ_QUOTE_EXCHANGE", "AMS"),
}
ORDER_EXCHANGE_BY_SYMBOL = {
    LONG_SYMBOL: _env_str("TSLA_AUTO_TSLL_ORDER_EXCHANGE", "NASD"),
    INVERSE_SYMBOL: _env_str("TSLA_AUTO_TSLZ_ORDER_EXCHANGE", "AMEX"),
    "TSLQ": _env_str("TSLA_AUTO_TSLQ_ORDER_EXCHANGE", "NASD"),
    "TSLY": _env_str("TSLA_AUTO_TSLY_ORDER_EXCHANGE", "AMEX"),
}
TSLZ_EXCHANGE_UNRESOLVED = "TSLZ_EXCHANGE_UNRESOLVED"

# ── Budget (UI-overridable; this is only the default) — USD ────────────────
DEFAULT_BUDGET_USD = _env_float("TSLA_AUTO_BUDGET_USD", 10_000.0)
ORDER_USAGE_RATIO = _env_float("TSLA_AUTO_ORDER_USAGE_RATIO", 0.995)

# ── Feature/safety toggles (docs §16) ───────────────────────────────────────
TSLA_AUTO_ENABLED = _env_bool("TSLA_AUTO_ENABLED", False)
TSLA_AUTO_MODE_DEFAULT = _env_str("TSLA_AUTO_MODE", "READ_ONLY")  # READ_ONLY | MOCK | REAL
ALLOW_REAL_ORDER = _env_bool("TSLA_AUTO_ALLOW_REAL_ORDER", False)
ALLOW_PREMARKET = _env_bool("TSLA_AUTO_ALLOW_PREMARKET", False)
ALLOW_AFTERMARKET = _env_bool("TSLA_AUTO_ALLOW_AFTERMARKET", False)
AUTO_FX = _env_bool("TSLA_AUTO_AUTO_FX", False)
STRONG_FILTER_DEFAULT = _env_bool("TSLA_AUTO_STRONG_FILTER_DEFAULT", True)

# ── MACD (strategy-fixed, identical to MACD2) ───────────────────────────────
EMA_FAST = 12
EMA_SLOW = 26
EMA_SIGNAL = 9

# ── Warm-up (strategy-fixed, identical structure to MACD2) ──────────────────
WARMUP_3M_BARS_MIN = 100
WARMUP_1M_BARS_MIN = WARMUP_3M_BARS_MIN * 3

# ── Risk / exit (strategy-fixed — MACD2 actual values, read+copied per docs §12) ──
STOP_LOSS_NET_PCT = -1.5
PROFIT_LOCK_ACTIVATE_NET_PCT = 1.5
PROFIT_LOCK_GIVEBACK_PP = 0.8
PROFIT_LOCK_EXIT_ENABLED = False

EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_PROFIT_LOCK = "PROFIT_LOCK"
EXIT_OPPOSITE_SIGNAL = "OPPOSITE_SIGNAL"
EXIT_FORCED_LIQUIDATION = "FORCED_LIQUIDATION"
EXIT_USER_LIQUIDATION = "USER_LIQUIDATION"

# ── (신규) 손절 후 재진입 쿨다운 (docs §12 — TSLA_AUTO 전용, MACD2에 없음) ──
STOP_LOSS_REENTRY_COOLDOWN_MIN = 15
STOP_LOSS_REENTRY_OVERRIDE_SCORE_FLOOR = 85.0  # max(85, 그 시점 문턱)의 베이스값
STOP_LOSS_REENTRY_COOLDOWN_BLOCK = "STOP_LOSS_REENTRY_COOLDOWN"
STOP_LOSS_REENTRY_OVERRIDE_USED_TODAY = "STOP_LOSS_REENTRY_OVERRIDE_USED_TODAY"
STOP_LOSS_REENTRY_OVERRIDE_APPROVED = "STOP_LOSS_REENTRY_OVERRIDE_APPROVED"

# ── Session timing (strategy-fixed, ET; see market_session.py for the actual
# per-day calendar-aware computation — these are only the default relative
# offsets, docs §6) ──────────────────────────────────────────────────────────
SESSION_OPEN = market_session.REGULAR_OPEN  # 09:30 ET
REGULAR_CLOSE = market_session.REGULAR_CLOSE  # 16:00 ET
NEW_ENTRY_CUTOFF_BEFORE_CLOSE_MIN = market_session.NEW_ENTRY_CUTOFF_BEFORE_CLOSE_MIN  # 15
FORCE_LIQUIDATE_BEFORE_CLOSE_MIN = market_session.FORCED_LIQUIDATION_BEFORE_CLOSE_MIN  # 10
FINAL_BALANCE_CHECK_BEFORE_CLOSE_MIN = market_session.FINAL_BALANCE_CHECK_BEFORE_CLOSE_MIN  # 2
US_LIQUIDATION_MAX_RETRIES = int(_env_float("TSLA_AUTO_US_LIQUIDATION_MAX_RETRIES", 3) or 3)
US_LIQUIDATION_RETRY_SECONDS = _env_float("TSLA_AUTO_US_LIQUIDATION_RETRY_SECONDS", 10.0) or 10.0
# (신규) 반대매수 포함 "모든 신규 매수" 최종 컷오프 — 정상장 기준 15:45 ET
# (= market_close - NEW_ENTRY_CUTOFF_BEFORE_CLOSE_MIN, 조기폐장일엔 자동 축소).
# 이 상수는 계산된 값과 항상 일치해야 하며 절대 시각을 별도로 하드코딩하지 않는다.

# ── Worker (strategy-fixed) ─────────────────────────────────────────────────
WORKER_INTERVAL_SEC = 5.0
SIGNAL_TO_ORDER_REQUEST_MAX_SEC = 5.0
WORKER_STALL_AGE_SEC = 15.0

# ── Market data validity (strategy-fixed, identical structure to MACD2) ────
QUOTE_MAX_AGE_SEC = 10.0
PENDING_SIGNAL_RETRY_SEC = 30.0
FLAT_POSITION_RECONCILE_INTERVAL_SEC = 30.0

QUOTE_STALE_RETRY_MAX_ATTEMPTS = 3
QUOTE_STALE_RETRY_INTERVAL_SEC = 1.0
QUOTE_STALE_MAX_WAIT_SEC = 15.0
MISSED_SIGNAL_QUOTE_STALE = "MISSED_SIGNAL_QUOTE_STALE"

ORDER_FILL_POLL_MAX_SEC = 60.0
ORDER_FILL_POLL_INTERVAL_SEC = 1.0

HISTORY_STALE_MAX_SEC = 180.0
HISTORY_GAP = "HISTORY_GAP"

KIS_PAGE_FETCH_PACING_SEC = 0.4
PRIOR_DAY_FETCH_RETRIES = 5
PRIOR_DAY_FETCH_RETRY_DELAY_SEC = 2.0

# ── Signal origin classification (docs §7 — Worker 재시작 이전/이후 분리) ──
ORIGIN_LIVE_CONFIRMED = "LIVE_CONFIRMED"
ORIGIN_HISTORICAL_REPLAY_ONLY = "HISTORICAL_REPLAY_ONLY"

# ── Strong-flag (Hybrid) filter — TSLA_AUTO 초기값, MACD2 실제 운영값 복제
# (docs/TSLA_AUTO_LOGIC.md §강한 플래그 필터). NORMAL/CHOP 문턱은 strong_flag_filter.py
# 자체 상수(§10/§11 시간대별 문턱표)로 관리한다 — 여기서는 컴포넌트 배점 문턱만.
STRONG_HIST_IMPULSE_T1 = 0.10
STRONG_HIST_IMPULSE_T2 = 0.15
STRONG_HIST_IMPULSE_T3 = 0.22
STRONG_PRICE_IMPULSE_T1 = 0.35
STRONG_PRICE_IMPULSE_T2 = 0.55
STRONG_PRICE_IMPULSE_HARD_MIN = 0.40
STRONG_BODY_ATR_T1 = 0.25
STRONG_BODY_ATR_T2 = 0.40
STRONG_VOLUME_RATIO_T1 = 1.00
STRONG_VOLUME_RATIO_T2 = 1.10
STRONG_VOLUME_RATIO_T3 = 1.20
STRONG_SIDEWAYS_EMA_SPREAD_MAX = 0.0007
STRONG_SIDEWAYS_RANGE_MAX = 0.006
STRONG_RANGE_BREAKOUT_LOOKBACK = 4
STRONG_RECENT_RANGE_LOOKBACK = 8
STRONG_VOLUME_LOOKBACK = 20
STRONG_ATR_PERIOD = 14
STRONG_EMA_FAST = 10
STRONG_EMA_SLOW = 20
STRONG_MIN_COMPLETED_BARS = 26

STRONG_MIN_HOLD_MIN = 9
STRONG_FAST_REVERSAL_WINDOW_MIN = 15
STRONG_SAME_DIRECTION_REENTRY_MIN = 18

# 승인 라벨
STRONG_APPROVED = "STRONG_APPROVED"
STRONG_SCORE_BELOW_THRESHOLD = "STRONG_SCORE_BELOW_THRESHOLD"
STRONG_PRICE_CONFIRMATION_FAILED = "STRONG_PRICE_CONFIRMATION_FAILED"
STRONG_SIDEWAYS_BLOCK = "STRONG_SIDEWAYS_BLOCK"
STRONG_PROFILE_FAILED = "STRONG_PROFILE_FAILED"
FILTER_DATA_INSUFFICIENT = "FILTER_DATA_INSUFFICIENT"
FILTER_INPUT_NOT_CROSSOVER = "FILTER_INPUT_NOT_CROSSOVER"
SAME_DIRECTION_POSITION_HELD = "SAME_DIRECTION_POSITION_HELD"
STRONG_SAME_DIRECTION_COOLDOWN = "STRONG_SAME_DIRECTION_COOLDOWN"
STRONG_MIN_HOLD_BLOCK = "STRONG_MIN_HOLD_BLOCK"
FILTERED_OUT = "FILTERED_OUT"

# ── (신규) NORMAL/CHOP 목표 거래 횟수 — 목표이지 보장이 아님 (docs §10) ─────
NORMAL_TARGET_ENTRIES = 4
NORMAL_MAX_ENTRIES = 4
CHOP_TARGET_ENTRIES = 2
CHOP_MAX_ENTRIES = 4
REGIME_NORMAL = "NORMAL"
REGIME_CHOP = "CHOP"
REGIME_UNKNOWN = "UNKNOWN"

# ── Isolated TSLA_AUTO runtime/ledger/cache paths — never shared with MACD2.
# Resolved via app.utils.data_paths.data_path() (existing helper — no edits to
# that shared module) inside state_store.py/ledger.py/market_data.py so tests
# can monkeypatch those modules' own path constants. ────────────────────────
RUNTIME_STATE_FILENAME = "tsla_auto_runtime.json"
SIGNAL_LEDGER_FILENAME = "tsla_auto_signal_ledger.csv"
EXECUTION_LEDGER_FILENAME = "tsla_auto_execution_ledger.csv"
