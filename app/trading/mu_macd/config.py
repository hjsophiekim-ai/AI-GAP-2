"""MU_MACD configuration — deliberately mirrors app.trading.macd2.config's
defaults (same strategy family: confirmed 3m MACD(12,26,9) crossover -> ETF
switch, -1.5% stop loss, 15:00 forced liquidation) but every runtime file
path below is its own, distinct filename so this module can NEVER read or
write a MACD2 or TSLA_AUTO state/ledger/cache/lock file.

The two traded ETF symbols (LONG_SYMBOL/INVERSE_SYMBOL) ARE imported from
macd2.config on purpose — they identify the exact same two real-world
tradable instruments (KODEX 레버리지 0193T0 / SOL 인버스2X 0197X0), so this is
sharing a physical-instrument identity constant, not runtime state. Nothing
else is imported from macd2.
"""
from __future__ import annotations

import os
from datetime import time, timezone, timedelta

from app.trading.macd2 import config as _macd2_config

KST = timezone(timedelta(hours=9))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


# ── Signal source (never traded directly) ──────────────────────────────────
WATCH_ASSET = "MU"  # Micron Technology, NASDAQ — signal source only

# ── Traded symbols — SAME two ETFs as MACD2, intentionally shared identity ─
LONG_SYMBOL = _macd2_config.LONG_SYMBOL  # KODEX 레버리지 0193T0 — bought on MU_UP_RED
INVERSE_SYMBOL = _macd2_config.INVERSE_SYMBOL  # SOL 인버스2X 0197X0 — bought on MU_DOWN_BLUE
TRADE_SYMBOLS = (LONG_SYMBOL, INVERSE_SYMBOL)

DEFAULT_BUDGET = 8_500_000.0

STRATEGY_NAME = "MU_MACD"
STRATEGY_VERSION = "20260812_MU_DAYSESSION_WS_V1"
SIGNAL_RULE = "MU_CONFIRMED_3M_MACD_12_26_9_CROSSOVER"

# ── MACD parameters (mirrors macd2.config; own constants, not imported, so
# this module's tuning never silently changes if macd2's ever does) ────────
EMA_FAST = 12
EMA_SLOW = 26
EMA_SIGNAL = 9

# ── Session / order-gate times (KST) ────────────────────────────────────────
SESSION_OPEN = time(9, 0)  # KRX open — the traded ETFs cannot fill before this
# 2026-08-13: briefly added a DAY_SESSION_LIVE_START=10:00 gate here (on top
# of SESSION_OPEN) to stop DNASMU-fed premarket ticks from ever driving a
# real entry before RBAQMU's day session actually opened. Removed the same
# day on explicit request: the user wants entries live from KRX open
# (09:00) itself, having started the WS feed at ~07:30 (90min -> exactly
# WARMUP_MIN_3M_BARS=30 3m bars by 09:00) and confirmed DNASMU is tracking
# real MU prices correctly. SESSION_OPEN=09:00 is therefore once again the
# sole lower bound -- 09:00-10:00 entries now run on DNASMU-fed MACD, same
# as 10:00-16:00 runs on RBAQMU (both feed the same on_tick aggregator, so
# the crossover math doesn't care which subscription produced a given bar).
NEW_ENTRY_CUTOFF = time(14, 55)
FORCE_LIQUIDATE_AT = time(15, 0)

# 2026-08-14: "점심시간 신규진입 휴식" (MIDDAY_ENTRY_PAUSE) -- user-requested
# schedule: entries ON 09:00-11:00, OFF 11:00-14:00, ON again 14:00 through
# the existing NEW_ENTRY_CUTOFF/FORCE_LIQUIDATE_AT close-of-day logic
# (both untouched). Gates NEW entries only, exactly like every other
# _entry_gate_block_reason check -- an opposite flag during the OFF window
# still sells 100% of a held position (existing sell-always/buy-only-if-
# gate-clear reversal logic, unchanged), it just never re-buys until 14:00.
MIDDAY_ENTRY_PAUSE_START = time(11, 0)
MIDDAY_ENTRY_PAUSE_END = time(14, 0)

# ── Risk (mirrors macd2.config defaults) ────────────────────────────────────
STOP_LOSS_NET_PCT = _env_float("MU_MACD_STOP_LOSS_NET_PCT", -1.5)

# ── Quick Profit 익절 (optional, OFF by default -- toggled via
# state.quick_profit_enabled) — when ON, closes the held position the
# instant its net return reaches this threshold, independent of MU flag
# state, checked every tick right alongside Stop Loss/Forced Liquidation. ──
QUICK_PROFIT_TAKE_PROFIT_NET_PCT = _env_float("MU_MACD_QUICK_PROFIT_TAKE_PROFIT_NET_PCT", 2.5)
QUICK_PROFIT_ENABLED_DEFAULT = _env_bool("MU_MACD_QUICK_PROFIT_ENABLED_DEFAULT", False)

# ── KIS WebSocket (official koreainvestment/open-trading-api spec — see
# examples_user/overseas_stock/overseas_stock_functions_ws.py):
#   TR_ID=HDFSCNT0 "해외주식 실시간지연체결가"; tr_key="R"+market+symbol for
#   the US DAY SESSION specifically (BAQ=NASDAQ day session, per that file's
#   own docstring: "미국주간거래(10:00~16:00)... 나스닥: BAQ"). Verified
#   LIVE_DAY_SESSION empirically on 2026-08-12 (continuously updating LAST/
#   TVOL/PBID/PASK for 5+ minutes straight, matching the live KIS app).
#
#   2026-08-13: RBAQMU alone left a hard 09:00-10:00+ KST warm-up gap (no
#   ticks at all before the day session opens) -- explicitly requested by
#   the user to be closed today ("MU 실시간 장외거래 가격 지금 바로"). Now
#   ALSO subscribing to DNASMU ("D"+official rsym "NASMU" from the KIS
#   NASDAQ master file) on the SAME WS connection to cover the
#   pre-day-session morning window. This was previously flagged UNVERIFIED
#   (a live boundary probe was planned but never run/committed) -- the
#   user has since redeployed and confirmed DNASMU prices track real MU
#   prices correctly.
#
#   Same day: explicit product decision to trade on it, not just warm up
#   with it -- the user starts the WS feed ~07:30 KST (90min -> exactly
#   WARMUP_MIN_3M_BARS=30 3m bars by KRX open) specifically so real entries
#   can fire from SESSION_OPEN=09:00 onward, same as RBAQMU drives them from
#   10:00. There is no code-level distinction between the two feeds once
#   ticks reach on_tick() -- both are trusted as real signal input across
#   their respective windows. Watch ws_last_error / warmup_bars_3m_count
#   closely if this is ever revisited. ────────────────────────────────────
WS_URL = "ws://ops.koreainvestment.com:21000/tryitout"
WS_TR_ID = "HDFSCNT0"
WS_TR_KEY = f"RBAQ{WATCH_ASSET}"  # "RBAQMU" -- day session, 10:00-16:00 KST, LIVE (verified 2026-08-12)
WS_TR_KEY_EXTENDED = f"DNAS{WATCH_ASSET}"  # "DNASMU" -- pre/after-hours feed, drives 09:00-10:00 entries too (see note above)
WS_COLUMNS = ("SYMB", "ZDIV", "TYMD", "XYMD", "XHMS", "KYMD", "KHMS", "OPEN", "HIGH", "LOW",
              "LAST", "SIGN", "DIFF", "RATE", "PBID", "PASK", "VBID", "VASK", "EVOL", "TVOL",
              "TAMT", "BIVL", "ASVL", "STRN", "MTYP")

# ── Data-quality gates — NEW entries only; an existing position's stop-loss/
# forced-liquidation exit uses the traded ETF's OWN KRX quote (always polled
# separately, never dependent on the MU WebSocket) and is NEVER blocked by
# these. Only a fresh BUY (flat entry or reversal re-entry) is gated. ───────
WS_STALE_MAX_SEC = _env_float("MU_MACD_WS_STALE_MAX_SEC", 15.0)

# ── Broker reconcile throttling — 2026-08-12 real incident: reconciling
# (a real broker.get_positions() REST call) on every ~2s worker tick
# unconditionally hit KIS's per-second rate limit within seconds of starting
# the live mock-mode smoke test ("초당 거래건수를 초과하였습니다"). A HELD
# position always reconciles (accuracy matters for stop-loss math); a FLAT
# state only reconciles once per this interval — mirrors macd2's own
# FLAT_POSITION_RECONCILE_INTERVAL_SEC throttling philosophy exactly.
RECONCILE_INTERVAL_SEC_WHEN_FLAT = _env_float("MU_MACD_RECONCILE_INTERVAL_SEC_WHEN_FLAT", 20.0)

# 2026-08-14 real incident: a SINGLE broker.get_positions() snapshot that
# disagreed with our own tracked position was trusted immediately and wrote
# RECONCILE_POSITION_VANISHED_UNTRACKED with NO fill price at all -- even
# though nothing in this worker's own order paths (stop loss/quick
# profit/opposite signal/forced liquidation/manual buttons) had ever placed
# a matching sell. This is the exact same "one instant KIS read can be
# stale/settlement-lagged" class of issue app.trading.macd2.order_executor's
# _reconcile_to_zero/_reconcile_buy_fill already retry around for a fresh
# SELL/BUY (see that module's 2026-08-10/11 fix comments) -- _do_reconcile
# never had the same guard. Mirrors those retry defaults exactly (3 @ 1.0s)
# before a mismatch is believed enough to overwrite state.position and write
# an untracked-correction ledger row.
RECONCILE_CONFIRM_RETRIES = _env_int("MU_MACD_RECONCILE_CONFIRM_RETRIES", 3)
RECONCILE_CONFIRM_DELAY_SEC = _env_float("MU_MACD_RECONCILE_CONFIRM_DELAY_SEC", 1.0)
# 2026-08-12 real incident: an EMA seeded cold (mid-session, ~11:38 start, no
# real prior history) produced 5 confirmed flags in ~2h on a manual replay —
# a warm-seeded (real prior-history) recompute of the SAME window was used to
# test whether this was a warm-up artifact (see mu_flag_root_cause_verify.py
# in the research scratchpad). Regardless of that specific finding, ANY
# from-cold WS session (this module always starts cold — WS has no
# historical backfill) needs a minimum number of completed 3m bars before
# its EMA can be trusted; this gate exists for exactly that reason.
WARMUP_MIN_3M_BARS = _env_int("MU_MACD_WARMUP_MIN_3M_BARS", 30)  # ~90 min

# 2026-08-13 real incident: a held leverage position rode a real -190,000원
# loss (well past STOP_LOSS_NET_PCT) and a confirmed BLUE flag with NEITHER
# ever acting -- because the process had restarted (Render idle-sleep or a
# redeploy; MU_MACD's Worker/broker/market-data are plain in-process
# attributes with no persistence) and nobody had clicked "시작" again since,
# so run_once() simply never executed. macd2 hit and fixed this exact class
# of bug on 2026-08-04 (see its WORKER_AUTO_RECOVER_COOLDOWN_SEC/
# _auto_recover_worker) -- MU_MACD never got the same fix until now.
# status() retries start() automatically (MOCK mode only -- REAL mode still
# always requires the human to re-enter confirm text) whenever it finds
# auto_trade_on=True but no live worker, rate-limited by this cooldown so a
# persistently-failing bootstrap can't hammer KIS on every UI auto-refresh.
WORKER_AUTO_RECOVER_COOLDOWN_SEC = _env_float("MU_MACD_WORKER_AUTO_RECOVER_COOLDOWN_SEC", 30.0)

# ── Runtime file paths — ALL distinct from macd2/tsla_auto, same LOGS_DIR/
# STATE_DIR/CACHE_DIR root (app.utils.data_paths — Render-persistent-disk
# aware) so redeploys don't lose history, but never the same filename. ─────
RUNTIME_STATE_FILENAME = "mu_macd_runtime.json"
SIGNAL_LEDGER_FILENAME = "mu_macd_signal_ledger.csv"
EXECUTION_LEDGER_FILENAME = "mu_macd_execution_ledger.csv"
BARS_1M_CACHE_FILENAME = "mu_macd_1m_bars.csv"
LOCK_FILENAME = "mu_macd.lock"

DEFAULT_MODE_DEFAULT = "mock"
AUTO_TRADE_ON_DEFAULT = _env_bool("MU_MACD_AUTO_TRADE_ON_DEFAULT", False)

# ── Exit-reason / block-reason string constants (mirrors macd2's own naming
# style — distinct values so a ledger row can never be mistaken for a macd2
# row even if the two CSVs were ever viewed side by side). ──────────────────
EXIT_STOP_LOSS = "MU_MACD_STOP_LOSS"
EXIT_FORCED_LIQUIDATION = "MU_MACD_FORCED_LIQUIDATION"
EXIT_OPPOSITE_SIGNAL = "MU_MACD_OPPOSITE_SIGNAL"
EXIT_MANUAL_LIQUIDATION = "MU_MACD_MANUAL_LIQUIDATION"
EXIT_QUICK_PROFIT_TAKE_PROFIT = "MU_MACD_QUICK_PROFIT_TAKE_PROFIT"
BLOCK_WS_STALE = "MU_MACD_WS_STALE"
BLOCK_WS_DISCONNECTED = "MU_MACD_WS_DISCONNECTED"
BLOCK_WARMUP_INSUFFICIENT = "MU_MACD_WARMUP_INSUFFICIENT"
BLOCK_ENTRY_WINDOW_CLOSED = "MU_MACD_ENTRY_WINDOW_CLOSED"
# 2026-08-14: scheduled 11:00-14:00 KST no-new-entry window (see
# MIDDAY_ENTRY_PAUSE_START/END above) -- distinct from BLOCK_ENTRY_WINDOW_CLOSED
# so the ledger/dashboard can tell "outside 09:00-14:55 entirely" apart from
# "inside the trading day but in the scheduled midday pause".
BLOCK_MIDDAY_ENTRY_PAUSE = "MU_MACD_MIDDAY_ENTRY_PAUSE"
BLOCK_SAME_DIRECTION_HELD = "MU_MACD_SAME_DIRECTION_HELD"
BLOCK_DUPLICATE_SIGNAL = "MU_MACD_DUPLICATE_SIGNAL"
# 2026-08-14: user-toggled "신규진입 정지" -- MU price collection (WS/1m bars),
# the worker tick loop, MACD flag detection/signal-ledger recording, and
# existing-position management (stop loss/quick profit/forced liquidation/
# reconcile) all keep running exactly as before; only a FRESH entry (flat
# BUY or a reversal's re-buy leg) is blocked. Distinct from stopping the
# service entirely (service.stop(), which also tears down the WS feed and
# ends warmup) -- this is a lighter-weight pause within an already-running
# session.
BLOCK_ENTRY_PAUSED_BY_USER = "MU_MACD_ENTRY_PAUSED_BY_USER"
# 2026-08-14: REAL mode's broker (KisRealBroker) refuses to even CONSTRUCT
# without the confirm phrase (safety gate enforced at __init__, not just
# per-order) -- so after a process restart, a real held position's
# reconcile/stop-loss/quick-profit/forced-liquidation genuinely cannot
# resume until the human re-enters it. MU price collection and MACD flag
# detection do NOT need that broker at all, though, so they keep running
# via worker.run_flags_only() in the meantime (see service.py's
# _auto_recover_flags_only) -- flags recorded during this window carry this
# block_reason instead of a normal entry-gate reason, and state.position is
# never touched by it.
BLOCK_REAL_BROKER_NOT_AUTHENTICATED = "MU_MACD_REAL_BROKER_NOT_AUTHENTICATED"
