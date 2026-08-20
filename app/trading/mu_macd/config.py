"""MU_MACD configuration — deliberately mirrors app.trading.macd2.config's
defaults (same strategy family: confirmed 3m MACD(12,26,9) crossover -> ETF
switch, -1.5% stop loss, 15:00 forced liquidation) but every runtime file
path below is its own, distinct filename so this module can NEVER read or
write a MACD2 or TSLA_AUTO state/ledger/cache/lock file.

The two traded ETF symbols (LONG_SYMBOL/INVERSE_SYMBOL) ARE imported from
macd2.config on purpose — they identify the exact same two real-world
tradable instruments (KODEX 레버리지 0193T0 / SOL 인버스2X 0197X0), so this is
sharing a physical-instrument identity constant, not runtime state.
TW_WHIPSAW_REJECT_REASONS (2026-08-19) is likewise imported directly rather
than redefined — it classifies block_reason values produced by macd2's own
time_window_filter.evaluate_time_window_entry, which this module calls BY
IMPORT (see the time-window filter section below), so a second, independently
maintained copy of the same two string constants would risk silently
drifting apart from what that shared function actually returns. Nothing else
is imported from macd2.
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

# ── Optional "시간대별 최적거래 필터" (Time-Window Optimal Trading Filter,
# 2026-08-15 사용자 요청) — SAME entry logic and SAME stop-loss/take-profit
# ladder as app.trading.macd2's own time-window filter, reused directly by
# import (app.trading.macd2.time_window_filter.evaluate_time_window_entry /
# app.trading.macd2.time_window_position_manager.evaluate_morning_position),
# exactly the same way this module already reuses macd2's signal_engine/
# order_executor. NEVER modifies any file under app/trading/macd2/ -- pure
# import only, mirroring this module's own existing house rule.
#
# Default OFF: MU_MACD is the currently-live auto-trading module (2026-08-14
# user decision), so this new gate must never silently change already-
# running behavior. Turning it ON replaces the plain "any confirmed MACD
# crossover enters immediately" flat-entry/reversal logic with macd2's own
# two-bar (T -> T+3) delayed confirmation + per-window quality-score gate,
# and replaces the plain STOP_LOSS(-1.5%)/QUICK_PROFIT(optional) exit check
# with macd2's own morning position-management ladder (STOP_LOSS -1.7%,
# TP1 +3.0%/50% partial, ratcheted stops, TP2 +5.0% full) for any position
# this filter itself opened. MIDDAY_ENTRY_PAUSE/NEW_ENTRY_CUTOFF/
# FORCE_LIQUIDATE_AT above are UNCHANGED and still apply on top.
TIME_WINDOW_FILTER_ENABLED_DEFAULT = _env_bool("MU_MACD_TIME_WINDOW_FILTER_ENABLED_DEFAULT", False)

EXIT_TW_STOP_LOSS = "MU_MACD_TW_STOP_LOSS"
EXIT_TW_TP1_PARTIAL = "MU_MACD_TW_TP1_PARTIAL"
EXIT_TW_TP2_FULL = "MU_MACD_TW_TP2_FULL"
EXIT_TW_AFTER_TP1_STOP = "MU_MACD_TW_AFTER_TP1_STOP"
EXIT_TW_TRAILING_STOP = "MU_MACD_TW_TRAILING_STOP"
BLOCK_TW_PENDING_CONFIRMATION = "MU_MACD_TW_PENDING_CONFIRMATION"

# ── 반대신호 청산 T+3 재확인("휩쏘-내성", 2026-08-19 사용자 요청) ──────────
# app.trading.macd2와 완전히 동일한 조건/로직을 MU_MACD에도 그대로 적용한다
# (사용자 요청: "MACD2 모듈과 MU-MACD모듈의 반대플래그 청산 로직에 전부 똑같이
# 반영해줘"). evaluate_time_window_entry가 반대방향 재진입 후보를 거절했을 때,
# 그 사유가 이 두 가지("MACD-Signal 관계가 T+3에도 유지 안 됨" / "gap이 확대
# 안 됨")면 원래 방향으로 복귀한 휩쏘로 보고 보유 포지션을 그대로 둔다(그 외
# 사유 -- 품질점수/시간대/최대진입횟수/중복포지션 -- 는 기존과 동일하게
# 무조건 매도). macd2.config에서 직접 가져온 값 그 자체(별도 정의 아님) --
# time_window_filter.evaluate_time_window_entry가 그 두 reject 문자열을
# 만들어내는 바로 그 함수이므로, 복제하면 나중에 서로 어긋날 위험이 있다.
# gap 절대값 임계값 같은 추가 조건은 없음(단순 버전, macd2와 동일).
# -1.7% 하드 손절/TP1/TP2/trailing stop은 이 로직과 완전히 무관하게 매 tick
# 즉시 평가되므로 영향받지 않는다.
TW_WHIPSAW_REJECT_REASONS = _macd2_config.TW_WHIPSAW_REJECT_REASONS

# ── Optional "TW 1 blue" 예외진입 (하루 최대 1회) — 2026-08-19 사용자 요청,
# app.trading.macd2의 동일한 기능(config.TW_DOWN_BLUE_EXCEPTION_FILTER_DEFAULT,
# 2026-08-18, 56거래일 TRAIN/VAL/OOS 백테스트로 검증됨 -- 그 모듈의 주석 참고)을
# MU_MACD에 동일한 조건/로직으로 그대로 이식한 것 -- 이 필터(time_window_filter_
# enabled)가 거절한 DOWN_BLUE 플래그만, 다른 조건 없이 하루 최대 1회 추가로
# 진입을 허용한다. TW 필터 자체가 꺼져 있으면 이 토글은 아무 효과가 없다.
# OFF가 기본값.
TW_DOWN_BLUE_EXCEPTION_FILTER_DEFAULT = _env_bool("MU_MACD_TW_DOWN_BLUE_EXCEPTION_FILTER_DEFAULT", False)
TW_DOWN_BLUE_EXCEPTION_FILTER_VERSION = "TW_1_BLUE_V1_20260819"

# ── "무필터 09:00-11:00" 즉시청산 진입모드 (2026-08-20 사용자 요청) ─────────
# TW필터(time_window_filter_enabled)가 꺼져있을 때 이미 존재하던 "legacy"
# 즉시진입/즉시청산 경로(worker.py의 confirmed_direction 처리, T+3 대기도
# quality gate도 없이 확정 flag 즉시 진입 + 반대신호 무조건 즉시매도)에
# 09:00-11:00 진입창 제한 한 줄만 추가한다(worker._entry_gate_block_reason).
# 그 legacy 경로 자체(반대신호 즉시매도, STOP_LOSS_NET_PCT/QUICK_PROFIT/
# FORCED_LIQUIDATION 리스크관리)는 전혀 안 건드림 -- TW필터의 휩쏘-내성
# 코드와도 완전히 무관. NO_FILTER_ENTRY_WINDOW_START/END는 macd2.config에서
# 직접 가져온 값 그 자체(별도 정의 아님, TW_WHIPSAW_REJECT_REASONS와 동일
# 관례). OFF가 기본값.
NO_FILTER_0900_1100_FILTER_DEFAULT = _env_bool("MU_MACD_NO_FILTER_0900_1100_FILTER_DEFAULT", False)
NO_FILTER_0900_1100_FILTER_VERSION = "NO_FILTER_0900_1100_V1_20260820"
NO_FILTER_ENTRY_WINDOW_START = _macd2_config.NO_FILTER_ENTRY_WINDOW_START
NO_FILTER_ENTRY_WINDOW_END = _macd2_config.NO_FILTER_ENTRY_WINDOW_END
NO_FILTER_REJECT_OUTSIDE_WINDOW = _macd2_config.NO_FILTER_REJECT_OUTSIDE_WINDOW
