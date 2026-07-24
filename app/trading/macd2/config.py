"""MACD2 configuration — docs/MACD2_LOGIC.md confirmed defaults.

Strategy-fixed values (never overridden per-request; changing them is a
requirements change, not a runtime setting) are the module-level constants
below. Values the UI/user may change at runtime (mode, budget) are read from
RuntimeState, not from here — this module only supplies their defaults.
"""
from __future__ import annotations

from datetime import time, timedelta, timezone

# KST is a fixed UTC+9 offset with no DST — safe as a plain timezone constant.
KST = timezone(timedelta(hours=9))

# ── Symbols (strategy-fixed) ────────────────────────────────────────────────
WATCH_SYMBOL = "000660"  # SK하이닉스 — signal source only, never traded directly
LONG_SYMBOL = "0193T0"  # KODEX 레버리지 — bought on UP_RED
INVERSE_SYMBOL = "0197X0"  # SOL 인버스2X — bought on DOWN_BLUE
TRADE_SYMBOLS = (LONG_SYMBOL, INVERSE_SYMBOL)

# ── Budget (UI-overridable; this is only the default) ──────────────────────
DEFAULT_BUDGET = 10_000_000.0

STRATEGY_NAME = "MACD2"
STRATEGY_VERSION = "20260724_MACD_FORMING_CROSSOVER_V1"
SIGNAL_RULE = "MACD_FORMING_CROSSOVER"
CONFIRMED_SIGNAL_RULE = "MACD_CROSSOVER_CONFIRMED"
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

# ── Feature flags (strategy-fixed per docs; not user-configurable) ────────
CONTINUATION_REENTRY_ENABLED = False
OPENING_PROBE_ENABLED = False

# ── Isolated MACD2 runtime/ledger paths (never shared with MACD v1) ───────
# Resolved lazily via app.utils.data_paths inside state_store.py/ledger.py so
# tests can monkeypatch those modules' own path constants, not these names.
RUNTIME_STATE_FILENAME = "macd2_runtime.json"
SIGNAL_LEDGER_FILENAME = "macd2_signal_ledger.csv"
EXECUTION_LEDGER_FILENAME = "macd2_execution_ledger.csv"
