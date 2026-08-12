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
NEW_ENTRY_CUTOFF = time(14, 55)
FORCE_LIQUIDATE_AT = time(15, 0)

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
#   NOT YET RESOLVED (2026-08-12): this module only subscribes to RBAQMU
#   (day session, 10:00-16:00 KST). The official aftermarket feed DNASMU
#   ("D"+official rsym from the KIS NASDAQ master file, confirmed "NASMU")
#   nominally covers 05:00-09:00 KST per THIS PROJECT's own
#   kis_overseas_minute.classify_session() boundaries -- but that boundary
#   is this project's own labeling, not something confirmed in KIS's own
#   docs/master data. Whether DNASMU is actually still LIVE through
#   09:00-10:00 KST (the gap between that boundary and RBAQMU's 10:00 start)
#   is UNVERIFIED as of this commit -- a live 3-channel probe
#   (DNASMU/RBAQMU/the non-official RNASMU, tested only, never for
#   production) covering 08:55-10:05 KST is planned for the next live
#   trading day. Do not assume the gap is real, and do not assume DNASMU
#   integration for pre-09:00 warm-up until that test confirms it. ────────
WS_URL = "ws://ops.koreainvestment.com:21000/tryitout"
WS_TR_ID = "HDFSCNT0"
WS_TR_KEY = f"RBAQ{WATCH_ASSET}"  # "RBAQMU" -- DNASMU (aftermarket) integration pending the live boundary test above
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
# 2026-08-12 real incident: an EMA seeded cold (mid-session, ~11:38 start, no
# real prior history) produced 5 confirmed flags in ~2h on a manual replay —
# a warm-seeded (real prior-history) recompute of the SAME window was used to
# test whether this was a warm-up artifact (see mu_flag_root_cause_verify.py
# in the research scratchpad). Regardless of that specific finding, ANY
# from-cold WS session (this module always starts cold — WS has no
# historical backfill) needs a minimum number of completed 3m bars before
# its EMA can be trusted; this gate exists for exactly that reason.
WARMUP_MIN_3M_BARS = _env_int("MU_MACD_WARMUP_MIN_3M_BARS", 30)  # ~90 min

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
BLOCK_SAME_DIRECTION_HELD = "MU_MACD_SAME_DIRECTION_HELD"
BLOCK_DUPLICATE_SIGNAL = "MU_MACD_DUPLICATE_SIGNAL"
