"""MACD2 worker — single 5-second tick loop (docs §11/§13).

``run_once()`` is one tick, fully testable without a background thread.
``start()``/``stop()`` wrap it in exactly one daemon thread. Never calls KIS
directly, and never triggers MarketDataService's own incremental merge
either — MarketDataService's own history-updater/quote-updater background
threads refresh those caches; this module only reads them via
``get_history_df()``/``get_quote()`` (docs §8/§11).
Never renders UI, never re-walks full history, never reloads modules, never
uses a pending-signal timer or a signal queue, never runs more than one
Worker thread, never reuses a stopped thread object.

Every tick also reconciles the real account position against
``state.position`` (one ``broker.get_positions()`` call) before evaluating
any signal — a mismatch blocks every order this tick (entry/switch/exit)
until it clears (docs: 실제 계좌와 state는 항상 reconcile). A new trading
date resets only the session-scoped runtime fields (last_signal_direction,
last_evaluated_bar_ts, today's Profit Lock/processed_signal_ids) — the
permanent signal ledger (ledger.append_signal, dedup by signal_id) is never
cleared.

Priority order for a held position, per docs §10 (this is docs' own stated
order, not a re-derivation of MACD v1's runtime behavior — docs is the sole
source of truth per the 2026-07-23 design decision):
  1) 15:00 FORCED_LIQUIDATION
  2) STOP_LOSS
  3) OPPOSITE_SIGNAL (a new, confirmed opposite signed-B direction)
  4) PROFIT_LOCK (MACD convergence early exit, 2026-08-05 spec)
  5) QUICK_PROFIT (optional take-profit filter, EXIT LOGIC ONLY)
  6) HOLD
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
import traceback
import uuid
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

from app.trading.macd2 import (
    config,
    ledger,
    major_flag_filter,
    order_executor,
    risk_exit,
    sideways_filter,
    single_entry_filter,
    time_window_filter,
    time_window_position_manager,
    trend_persistence_filter,
)
from app.trading.macd2.market_data import MarketDataService, filter_complete_3m_bars
from app.trading.macd2.models import (
    Direction,
    MajorFlagDecision,
    PositionSnapshot,
    RuntimeState,
    RuntimeStatus,
    SignalState,
)
from app.trading.macd2.signal_engine import (
    calculate_macd,
    evaluate_macd_crossover,
    evaluate_primary_forming_crossover,
    forming_bar_window,
    make_provisional_signal_id,
    make_signal_id,
    resample_completed_3m,
    signed_b_condition,
)
from app.trading.trading_cost_engine import TradeCostEngine

KST = config.KST

# 주문 성공 응답만으로 체결로 간주하지 않고 실제 체결/잔고 재조회로 확인하는
# 최대 대기시간(docs 2026-07-27 체결확인 fix) — order_executor의 reconcile
# 재시도 파라미터로 Worker의 모든 프로덕션 호출에 적용된다.
ORDER_FILL_RECONCILE_RETRIES = max(1, int(config.ORDER_FILL_POLL_MAX_SEC / config.ORDER_FILL_POLL_INTERVAL_SEC))
ORDER_FILL_RECONCILE_DELAY_SEC = config.ORDER_FILL_POLL_INTERVAL_SEC

POSITION_MISMATCH = "POSITION_MISMATCH"
POSITION_DATA_ERROR = "POSITION_DATA_ERROR"
QUOTE_STALE = "QUOTE_STALE"
MATCH_FLAT = "MATCH_FLAT"
MATCH_POSITION = "MATCH_POSITION"
RECOVERED_FROM_BROKER = "RECOVERED_FROM_BROKER"
RECOVERED_TO_FLAT = "RECOVERED_TO_FLAT"
RECOVERED_QTY_MISMATCH = "RECOVERED_QTY_MISMATCH"
SIGNAL_NOT_DISPATCHED = "SIGNAL_NOT_DISPATCHED"
# Marker key inside ExecutionOutcome.timestamps for a signal the optional
# Hybrid MAJOR_FLAG gate rejected — no broker/order_executor call ever
# happened, so run_once must not treat it as an entry/switch attempt.
MAJOR_FILTERED_TS_KEY = "major_filtered_at"
TEMPORARY_BLOCK_REASONS = {
    QUOTE_STALE,
    order_executor.BLOCK_ORDER_DATA_INVALID,
    order_executor.BLOCK_KIS_BUYABLE_QUERY_FAILED,
    order_executor.BLOCK_INSUFFICIENT_QTY,
    POSITION_DATA_ERROR,
}


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).strip()
    except Exception:
        return ""


def git_sha() -> str:
    """Public wrapper — the SAME short SHA written into every signal-ledger
    row's ``worker_code_sha`` column, so callers (UI stats filtering) compare
    against the identical value/format instead of a differently-formatted SHA
    from another source (e.g. app.utils.runtime_info's full-length SHA)."""
    return _git_sha()


@dataclass
class TickResult:
    ok: bool = True
    actions: list[str] = field(default_factory=list)
    error: Optional[str] = None
    skipped: Optional[str] = None
    signal_detected_at: Optional[str] = None
    order_requested_at: Optional[str] = None
    signal_dispatch_trace: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, float] = field(default_factory=dict)


def _net_return_pct(symbol: str, entry_price: float, current_price: float, quantity: int) -> float:
    if entry_price <= 0 or quantity <= 0 or current_price <= 0:
        return 0.0
    cost = TradeCostEngine().compute_net_pnl(
        symbol, entry_price, current_price, quantity, buy_order_type="market", sell_order_type="market",
    )
    return float(cost["net_pnl"]) / (entry_price * quantity) * 100.0


def _advance_stop_loss_bar(state: RuntimeState, symbol: str, current_price: float, now: datetime) -> Optional[float]:
    """Track the held ETF's own completed 3-minute bar close for Stop Loss
    (docs 2026-08-02 Exit Rule: 3-Minute Confirmed Bars) -- no real ETF 1분봉
    feed exists (market_data.py only tracks WATCH_SYMBOL history), so this
    approximates the traded ETF's completed 3-minute close from the live
    quote stream, tick-sampled the same way. Returns the just-completed bar's close
    the first tick after that bar rolls over, but ONLY once it is strictly
    after the entry execution bar (the 3-minute bar containing the entry
    fill is never eligible for Stop Loss) -- otherwise None (still mid-bar,
    or the bar that just completed is the execution bar itself).
    """
    bar_start, _bar_end = forming_bar_window(now)
    bar_key = bar_start.isoformat()

    if state.stop_loss_bar_symbol != symbol or state.stop_loss_entry_bar_ts is None:
        # Normally already seeded at entry (see _apply_switch_outcome) --
        # this is a defensive fallback for a held position that appeared
        # without going through that path (e.g. broker-reconciled recovery).
        # Treat the CURRENT bar as the (pseudo-)execution bar so it is
        # excluded, same as a real entry.
        state.stop_loss_bar_symbol = symbol
        state.stop_loss_entry_bar_ts = bar_key
        state.stop_loss_bar_ts = bar_key
        state.stop_loss_bar_close = current_price
        return None

    if bar_key == state.stop_loss_bar_ts:
        state.stop_loss_bar_close = current_price
        return None

    completed_bar_ts = state.stop_loss_bar_ts
    completed_close = state.stop_loss_bar_close
    state.stop_loss_bar_ts = bar_key
    state.stop_loss_bar_close = current_price
    if completed_bar_ts is None or completed_bar_ts <= state.stop_loss_entry_bar_ts:
        return None
    return completed_close


def _held_direction_support_gap(direction: Optional[Direction], macd_snap) -> Optional[float]:
    """docs 2026-08-05 Profit Lock spec: 0193T0(UP_RED) held -> MACD-Signal;
    0197X0(DOWN_BLUE) held -> Signal-MACD. Positive while the held direction's
    trend is still supported by the confirmed MACD; <=0 means the trend has
    already reversed (OPPOSITE_SIGNAL owns that case, checked first)."""
    if direction == Direction.UP_RED:
        return float(macd_snap.macd) - float(macd_snap.signal)
    if direction == Direction.DOWN_BLUE:
        return float(macd_snap.signal) - float(macd_snap.macd)
    return None


def _advance_profit_lock(
    state: RuntimeState, *, symbol: str, direction: Direction, macd_snap,
    current_price: float, entry_price: float, quantity: int,
) -> bool:
    """Profit Lock — MACD convergence early exit (docs §10 priority 4,
    2026-08-05 spec; replaces the old net-return-giveback Profit Lock
    entirely). Evaluated once per newly-completed WATCH_SYMBOL(000660)
    3-minute bar while a position is held, off the SAME confirmed
    MACD(12,26,9)/Signal already computed for flag generation (``macd_snap``)
    — never a second MACD calculation, never the forming bar (진행봉으로 청산
    금지: a repeat call against the same ``macd_snap.bar_dt`` is always a
    no-op here). Returns True the one tick all 5 conditions (config.py's
    PROFIT_LOCK_*) are met; the caller executes the exit and
    ``_apply_exit_outcome`` resets all profit_lock_* fields for the next
    holding period.

    Lazily (re-)seeds its own baseline the first time it runs for a symbol it
    hasn't tracked yet (fresh entry, or a held position that appeared without
    going through the normal entry path e.g. broker-reconciled recovery) —
    same convention as ``_advance_stop_loss_bar``'s own defensive fallback.
    ``_apply_exit_outcome`` clears ``profit_lock_symbol``/``profit_lock_entry_
    bar_ts`` on every exit, so a later same-symbol re-entry always re-seeds
    fresh rather than inheriting a previous holding period's history.
    """
    bar_key = macd_snap.bar_dt.isoformat()

    if state.profit_lock_symbol != symbol or state.profit_lock_entry_bar_ts is None:
        state.profit_lock_symbol = symbol
        state.profit_lock_entry_bar_ts = bar_key
        state.profit_lock_last_bar_ts = bar_key
        state.profit_lock_bars_since_entry = 0
        state.profit_lock_gap_history = []
        state.profit_lock_peak_return_pct = 0.0
        state.profit_lock_current_support_gap = None
        state.profit_lock_max_support_gap = None
        state.profit_lock_gap_ratio = None
        state.profit_lock_contraction_count = 0
        state.profit_lock_drawdown_pct = 0.0
        return False

    if bar_key == state.profit_lock_last_bar_ts:
        return False  # same completed bar as last time -- no new bar-close data yet

    state.profit_lock_last_bar_ts = bar_key
    if bar_key <= state.profit_lock_entry_bar_ts:
        return False  # still (at or before) the entry bar -- not eligible yet

    state.profit_lock_bars_since_entry = int(state.profit_lock_bars_since_entry or 0) + 1

    support_gap = _held_direction_support_gap(direction, macd_snap)
    if support_gap is None:
        return False
    support_gap = float(support_gap)
    state.profit_lock_current_support_gap = round(support_gap, 6)

    gap_history = list(state.profit_lock_gap_history or [])
    gap_history.append(support_gap)
    state.profit_lock_gap_history = gap_history[-3:]

    prior_max = state.profit_lock_max_support_gap
    max_gap = support_gap if prior_max is None else max(float(prior_max), support_gap)
    state.profit_lock_max_support_gap = round(max_gap, 6)

    current_return = _net_return_pct(symbol, entry_price, current_price, quantity)
    prior_peak = float(state.profit_lock_peak_return_pct or 0.0)
    peak_return = max(prior_peak, current_return)
    state.profit_lock_peak_return_pct = round(peak_return, 6)
    drawdown = max(0.0, peak_return - current_return)
    state.profit_lock_drawdown_pct = round(drawdown, 6)

    hist = state.profit_lock_gap_history
    contraction_count = 0
    if len(hist) >= 3 and hist[-3] > hist[-2] > hist[-1]:
        contraction_count = 2
    elif len(hist) >= 2 and hist[-2] > hist[-1]:
        contraction_count = 1
    state.profit_lock_contraction_count = contraction_count

    gap_ratio = (support_gap / max_gap) if max_gap > 0 else None
    state.profit_lock_gap_ratio = round(gap_ratio, 6) if gap_ratio is not None else None

    if support_gap <= 0:
        # 반대 플래그 청산이 우선 적용되는 영역 -- Profit Lock은 관여하지 않는다.
        return False

    return bool(
        current_return >= config.PROFIT_LOCK_MIN_NET_RETURN_PCT
        and state.profit_lock_bars_since_entry >= config.PROFIT_LOCK_MIN_BARS_SINCE_ENTRY
        and contraction_count >= config.PROFIT_LOCK_MIN_CONSECUTIVE_CONTRACTIONS
        and gap_ratio is not None and gap_ratio <= config.PROFIT_LOCK_MAX_GAP_RATIO
        and drawdown >= config.PROFIT_LOCK_MIN_DRAWDOWN_PP
    )


def _fresh_quote_prices(market_data: MarketDataService, symbols: tuple[str, ...]) -> dict[str, float]:
    """Only symbols whose cached quote age <= QUOTE_MAX_AGE_SEC are considered
    valid for order sizing/exit decisions (docs §12) — stale/missing quotes
    are simply absent from the returned dict, letting order_executor's own
    ORDER_DATA_INVALID gate fire naturally.
    """
    prices: dict[str, float] = {}
    for symbol in symbols:
        snap = market_data.get_quote(symbol)
        if snap is None or snap.error or snap.price <= 0:
            continue
        if snap.age_sec is not None and snap.age_sec > config.QUOTE_MAX_AGE_SEC:
            continue
        prices[symbol] = snap.price
    return prices


def _apply_day_rollover(state: RuntimeState, now: datetime) -> None:
    """New trading date -> reset only session-scoped runtime fields (docs:
    거래일 변경 초기화). The permanent signal ledger (ledger.append_signal's
    CSV, deduped by signal_id) is untouched here — ``processed_signal_ids``
    is only the in-state, same-day dedup list, safe to clear on rollover."""
    today_str = now.strftime("%Y%m%d")
    if state.session_date is None:
        # First tick ever for this state (e.g. brand-new RuntimeState) — there
        # is nothing to roll over yet, so just record today without wiping
        # fields a caller may have already set for the current session.
        state.session_date = today_str
        return
    if state.session_date == today_str:
        return
    state.session_date = today_str
    state.last_signal_direction = None
    state.last_detected_direction = None
    state.last_executed_direction = None
    state.current_episode_direction = None
    state.last_evaluated_bar_ts = None
    state.last_confirmed_bar_ts = None
    state.processed_signal_ids = []
    state.pending_signal = None
    state.peak_net_return = 0.0
    state.profit_lock_active = False
    state.possible_toggle_reset_at = None
    # MAJOR_FLAG daily entry budget is session-scoped; the toggle itself
    # (major_filter_enabled) is user state and survives the rollover.
    state.daily_major_entry_count = 0
    state.last_major_entry_at = None
    # 추세전환장 filter's daily entry count is likewise session-scoped; its
    # toggle (sideways_filter_enabled) also survives the rollover.
    state.daily_sideways_entry_count = 0
    state.last_sideways_entry_at = None
    # Trend Persistence filter's daily entry count is likewise session-scoped;
    # its toggle (trend_persistence_filter_enabled) also survives the rollover.
    state.daily_trend_persistence_entry_count = 0
    state.last_trend_persistence_entry_at = None
    # Single-Entry filter's daily fill count is likewise session-scoped; its
    # toggle (single_entry_filter_enabled) also survives the rollover.
    state.daily_single_entry_count = 0
    state.last_single_entry_at = None
    # v3's "which confirmed flag number is this" ordinal is a SEPARATE
    # session-scoped counter from the fill count above (incremented on
    # every confirmed flag regardless of approval/fill).
    state.daily_confirmed_flag_count = 0
    # Time-window filter's morning/afternoon entry counts and any pending
    # (unresolved) T+3 candidate are session-scoped; its toggle
    # (time_window_filter_enabled) and an ALREADY-open position's own
    # management state (time_window_position_active/tp1_done/etc.) survive
    # the rollover unchanged -- a position can still be open across
    # midnight only in the sense that FORCED_LIQUIDATION already empties it
    # by 15:00 every day, so this never actually matters in practice, but is
    # not reset here regardless (mirrors how state.position itself is never
    # reset on rollover).
    state.time_window_morning_entry_count = 0
    state.time_window_afternoon_entry_count = 0
    state.time_window_pending_flag_direction = None
    state.time_window_pending_flag_bar_ts = None
    # 09:03 예약 매수(2026-08-06)는 하루 1회짜리 원샷 액션이라, 다른 토글들과
    # 달리 armed 상태 자체가 매일 초기화된다 -- 매일 아침 다시 눌러야 한다.
    #
    # 2026-08-07 fix (real incident: armed 09:03 예약매수가 전혀 체결되지
    # 않고 09:20 실제 플래그로만 체결됨) -- arm_scheduled_entry (service.py)
    # writes armed_direction/armed_at straight to disk OUTSIDE run_once,
    # with NO coordination with session_date. A very normal morning order
    # of operations (1. 예약매수 버튼 누르기, 2. 자동매매 시작 버튼 누르기)
    # means the FIRST tick of the new day -- which is exactly when THIS
    # rollover fires -- happens AFTER the arm, not before it. Unconditionally
    # wiping the armed fields here silently discarded an arm made only
    # seconds earlier for TODAY, before 09:03 ever arrived. Only a STALE arm
    # (armed_at from a PRIOR calendar day, i.e. left over because it never
    # fired and the user never re-armed) should be cleared; an arm already
    # made for today must survive this same-day-rollover race.
    armed_at = _parse_iso_dt(state.scheduled_entry_armed_at)
    armed_today = armed_at is not None and armed_at.astimezone(KST).strftime("%Y%m%d") == today_str
    if not armed_today:
        state.scheduled_entry_armed_direction = None
        state.scheduled_entry_armed_at = None
        state.scheduled_entry_armed_by = None
    state.scheduled_entry_executed_at = None
    state.scheduled_entry_last_result = None
    state.scheduled_entry_protected = False


def _relation_from_diff(diff: Optional[float]) -> str:
    if diff is None:
        return "EQUAL"
    if diff > 0:
        return "ABOVE"
    if diff < 0:
        return "BELOW"
    return "EQUAL"


def initialize_strategy_session(
    state: RuntimeState,
    market_data: MarketDataService,
    *,
    now: Optional[datetime] = None,
    worker_instance_id: Optional[str] = None,
) -> RuntimeState:
    now = now or datetime.now(KST)
    prior_session_started_at = state.session_started_at
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.session_started_at = now.isoformat()
    state.worker_instance_id = worker_instance_id
    state.last_executed_direction = None
    state.current_episode_direction = None
    state.processed_signal_ids = []

    df_1m = market_data.get_history_df()
    bars_3m = resample_completed_3m(df_1m, now=now)
    today_str = now.astimezone(KST).strftime("%Y%m%d")
    today_indices = (
        list(bars_3m.index[bars_3m["datetime"].dt.strftime("%Y%m%d") == today_str])
        if not bars_3m.empty else []
    )

    # 2026-08-04 fix: this used to always jump straight to "whichever bar is
    # newest right now" as the new baseline, silently absorbing any
    # crossover that happened on an EARLIER today bar while the Worker
    # process was not actually ticking (Render idle-sleep/redeploy/crash —
    # see the auto-recovery added in service.py the same day). evaluate_
    # macd_crossover's sign-flip check is inherently bar-local (this bar's
    # own previous/current diff vs the immediately prior bar), so once a
    # bar is skipped that crossover can never be recovered from a later
    # bar — a real flag (and its order) was silently lost this way.
    #
    # If this state already has a same-day last_confirmed_bar_ts (a
    # mid-session RESTART, not a true first start today), replay every bar
    # after it in order — same pattern as the read-only
    # compute_today_signal_overview() — deliberately stopping ONE bar short
    # of the newest so the Worker's very first live run_once() tick still
    # evaluates+dispatches that final bar itself through the normal path
    # (never duplicated here). A true first start today (no matching prior
    # bar) keeps the original single-newest-bar baseline — bars before a
    # Worker's first-ever session start are legitimately never live-actable
    # (same HISTORICAL_REPLAY_ONLY distinction compute_today_signal_overview
    # already makes for display).
    resuming_today = False
    resume_from = 0
    last_direction: Optional[Direction] = None
    prior_confirmed = _parse_iso_dt(state.last_confirmed_bar_ts)
    if prior_confirmed is not None and today_indices:
        prior_confirmed_kst = prior_confirmed.astimezone(KST)
        if prior_confirmed_kst.strftime("%Y%m%d") == today_str:
            for pos, idx in enumerate(today_indices):
                if bars_3m["datetime"].iloc[idx] == prior_confirmed_kst:
                    resume_from = pos + 1
                    last_direction = state.last_detected_direction
                    resuming_today = True
                    break

    if not resuming_today and len(today_indices) > 1:
        # 2026-08-05 fix: a same-day restart whose PERSISTED state was lost
        # (e.g. a Render redeploy/disk hiccup wiping data/state/
        # macd2_runtime.json, not a genuine brand-new trading day) used to be
        # indistinguishable from a true first-ever start today, because
        # last_confirmed_bar_ts is simply absent either way -- the cold-start
        # branch below then silently swallowed whichever bar was newest AT
        # THAT MOMENT as a no-dispatch baseline, discarding a real intraday
        # crossover with zero record (2026-08-05 real incident: a confirmed
        # UP_RED mid-afternoon never dispatched a SELL/BUY switch for an
        # already-held position). A trading day already more than one
        # completed bar past its own open (len(today_indices) > 1, i.e. at
        # least 6 minutes into the session) can never be a genuine first bar
        # of the day, so replay every one of today's own bars the same way an
        # ordinary same-day resume already does (resume_from=0) -- this
        # reuses the exact same multi-bar-gap correction machinery below
        # (RESTART_CATCH_UP_MULTI_BAR_GAP pending_signal) instead of
        # inventing new recovery logic.
        resuming_today = True
        resume_from = 0
        last_direction = None
        # 2026-08-05 fix: a toggle the user set earlier today (major_filter_
        # enabled/sideways_filter_enabled/quick_profit_enabled/profit_lock_
        # enabled) may have silently reverted to its config default the
        # moment state.json was lost -- unlike signal history, a toggle
        # preference can never be reconstructed from market data, so this can
        # only be surfaced, not auto-corrected. The UI shows a prominent
        # warning while this is set (cleared on the next day's rollover).
        state.possible_toggle_reset_at = now.isoformat()

    if resuming_today and prior_session_started_at:
        # 2026-08-04 fix: a mid-day restart used to always bump
        # session_started_at to "now", which retroactively reclassified
        # every already-LIVE_CONFIRMED flag from earlier today as
        # HISTORICAL_REPLAY_ONLY in compute_today_signal_overview's display
        # the moment a later restart happened — even though a live Worker
        # genuinely was running and (should have) traded them at the time.
        # Preserving the ORIGINAL same-day session start keeps that display
        # accurate across restarts; a true first start today still gets a
        # fresh session_started_at (set above) as before.
        state.session_started_at = prior_session_started_at

    macd_snap = None
    if resuming_today:
        last_flag_snap = None
        for pos in range(resume_from, len(today_indices) - 1):
            snap = calculate_macd(bars_3m.iloc[: today_indices[pos] + 1])
            if snap is None:
                continue
            direction = evaluate_macd_crossover(snap, last_direction)
            state.last_confirmed_bar_ts = snap.bar_dt.isoformat()
            if direction != Direction.HOLD:
                last_direction = direction
                last_flag_snap = snap
                state.latest_primary_flag = direction
                state.latest_primary_signal_id = make_signal_id(snap.bar_dt, direction)
        state.last_detected_direction = last_direction
        if today_indices:
            macd_snap = calculate_macd(bars_3m.iloc[: today_indices[-1] + 1])

        # 2026-08-04 fix: an outage spanning MULTIPLE missed bars (not just
        # one) used to lose a reversal that happened on an EARLIER bar
        # within the catch-up walk, not the newest one — the walk correctly
        # recorded latest_primary_flag/last_detected_direction for it, but
        # never gave it an actual dispatch chance, and the crossover itself
        # is bar-local so it can never re-fire later. The live tick that
        # follows only evaluates the NEWEST bar, which has no reason to
        # show a fresh crossover of its own, so the held position was left
        # silently pointing at the wrong side of the market indefinitely.
        # If the fully-resolved direction from the walk doesn't match what
        # is actually held, hand it to the SAME pending_signal retry path
        # already used for quote-stale delayed entries (worker.py's held/
        # flat branches both consult it every tick) so the very next tick
        # corrects the position immediately — without replaying every
        # intermediate historical switch, which cannot be executed
        # retroactively anyway.
        if last_flag_snap is not None:
            held_symbol = state.position.symbol if state.position and state.position.quantity > 0 else None
            target_symbol = order_executor.target_symbol_for_direction(last_direction)
            if target_symbol != held_symbol:
                _set_pending_signal(
                    state,
                    signal_id=make_signal_id(last_flag_snap.bar_dt, last_direction),
                    direction=last_direction,
                    signal_type="REVERSAL" if held_symbol is not None else "INITIAL",
                    macd_snap=last_flag_snap,
                    detected_at=now,
                    reason="RESTART_CATCH_UP_MULTI_BAR_GAP",
                )
    else:
        state.last_detected_direction = None
        macd_snap = calculate_macd(bars_3m)
        if macd_snap is not None:
            state.last_confirmed_bar_ts = macd_snap.bar_dt.isoformat()

    # 2026-08-06 fix: a same-day restart used to unconditionally wipe
    # state.pending_signal to None (regardless of resuming_today) before any
    # of the above catch-up logic ran. Under an unstable host that restarts
    # the whole process every minute or two (2026-08-06 real incident: 6+
    # distinct worker_instance_id values inside 30 minutes), a genuine
    # RESTART_CATCH_UP_MULTI_BAR_GAP pending_signal set by restart N could be
    # silently discarded by restart N+1 before the live tick that follows
    # restart N ever got a chance to act on it -- and because the catch-up
    # walk above also marks every bar it visits as already-evaluated
    # (state.last_confirmed_bar_ts advances past it), that bar's flag could
    # never be re-detected either: the opportunity vanished with zero record
    # (a confirmed, filter-APPROVED 12:03 DOWN_BLUE entry never even reached
    # order_executor). A true first start today (not resuming_today) still
    # clears it -- there is no same-day continuity to preserve. Otherwise the
    # existing pending_signal (whether just freshly set by the walk above, or
    # carried over untouched from an earlier restart) survives, with its
    # detected_at refreshed to THIS restart's `now` so config.PENDING_SIGNAL_
    # RETRY_SEC's short retry window (30s, sized for a live QUOTE_STALE
    # retry within one running session) is judged against this restart's own
    # live tick, not against wall-clock time that piled up across however
    # many prior restarts happened before this one got a fair chance.
    if not resuming_today:
        state.pending_signal = None
    elif state.pending_signal and not state.pending_signal.get("order_requested"):
        state.pending_signal["detected_at"] = now.isoformat()

    if macd_snap is not None:
        state.session_baseline_bar_ts = macd_snap.bar_dt.isoformat()
        state.last_evaluated_bar_ts = macd_snap.bar_dt.isoformat()
        state.baseline_relation = macd_snap.relation or _relation_from_diff(macd_snap.current_diff)
        state.primary_previous_diff = macd_snap.previous_diff
        state.primary_current_diff = macd_snap.current_diff
        state.primary_relation = state.baseline_relation
        state.signed_b_shadow_direction = signed_b_condition(macd_snap)
        state.signed_b_shadow_hist_last3 = macd_snap.hist_last3
    else:
        state.session_baseline_bar_ts = None
        state.last_evaluated_bar_ts = None
        state.baseline_relation = None
    return state


ORIGIN_LIVE_CONFIRMED = "LIVE_CONFIRMED"
ORIGIN_HISTORICAL_REPLAY_ONLY = "HISTORICAL_REPLAY_ONLY"


def compute_today_signal_overview(
    df_1m: pd.DataFrame, *, now: datetime, session_started_at: Optional[str],
) -> list[dict[str, Any]]:
    """Recompute every one of TODAY's confirmed completed-3m-bar MACD flags
    from raw 1-minute history, for the 신호 통계 panel ONLY — never called by
    run_once, never touches order_executor/major_flag_filter/processed_signal_ids
    (docs §3/§5). Uses the exact same pure function as the live Worker
    (resample_completed_3m / filter_complete_3m_bars / calculate_macd /
    evaluate_macd_crossover) so a bar's classification here always agrees
    with what run_once would have decided had it been running at that moment.

    A bar whose window closed strictly before ``session_started_at`` (this
    Worker session was never running yet) is classified
    ``HISTORICAL_REPLAY_ONLY`` — display only, no order authority ever
    existed for it. A bar closing at/after ``session_started_at`` is
    ``LIVE_CONFIRMED`` — the same bar the live run_once() loop had a genuine
    chance to evaluate and dispatch on. The very first bar of the day is
    baseline-only (mirrors ``_advance_confirmed_primary``'s own
    is-first-of-day gate) and never produces a signal.
    """
    bars_3m = resample_completed_3m(df_1m, now=now)
    bars_3m, _dropped = filter_complete_3m_bars(bars_3m, df_1m)
    if bars_3m.empty:
        return []

    today_str = now.astimezone(KST).strftime("%Y%m%d")
    today_mask = bars_3m["datetime"].dt.strftime("%Y%m%d") == today_str
    today_indices = list(bars_3m.index[today_mask])
    if not today_indices:
        return []

    session_start_dt = _parse_iso_dt(session_started_at)
    overview: list[dict[str, Any]] = []
    last_direction: Optional[Direction] = None
    for pos, idx in enumerate(today_indices):
        window = bars_3m.iloc[: idx + 1]
        snap = calculate_macd(window)
        if snap is None:
            continue
        if pos == 0:
            # Baseline only — mirrors _advance_confirmed_primary's own
            # is-first-of-day gate (previous_diff here can span yesterday's
            # last bar into today's first, so any zero-crossing would be an
            # overnight-gap artifact, not an intraday reversal).
            continue
        bar_end = snap.bar_dt + timedelta(minutes=3)
        direction = evaluate_macd_crossover(snap, last_direction)
        if direction == Direction.HOLD:
            continue
        last_direction = direction
        origin = (
            ORIGIN_HISTORICAL_REPLAY_ONLY
            if session_start_dt is not None and bar_end <= session_start_dt
            else ORIGIN_LIVE_CONFIRMED
        )
        overview.append({
            "signal_id": make_signal_id(snap.bar_dt, direction),
            "bar_start_at": snap.bar_dt.isoformat(),
            "bar_end_at": bar_end.isoformat(),
            "direction": direction.value,
            "origin": origin,
        })
    return overview


def _normalize_broker_positions(raw_positions) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    broker_positions: dict[str, dict[str, Any]] = {}
    all_positions: list[dict[str, Any]] = []
    for p in raw_positions or []:
        symbol = str(getattr(p, "symbol", "") or "").strip()
        try:
            qty = int(float(getattr(p, "quantity", 0) or 0))
        except (TypeError, ValueError):
            qty = 0
        try:
            avg_price = float(getattr(p, "avg_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            avg_price = 0.0
        row = {"symbol": symbol, "qty": qty, "avg_price": avg_price}
        all_positions.append(row)
        if symbol in config.TRADE_SYMBOLS and qty > 0:
            broker_positions[symbol] = row
    return broker_positions, all_positions


def _runtime_position_dict(state: RuntimeState) -> dict[str, Any]:
    pos = state.position
    if pos is None or not pos.symbol or int(pos.quantity or 0) <= 0:
        return {"symbol": None, "qty": 0, "avg_price": 0.0}
    return {"symbol": pos.symbol, "qty": int(pos.quantity), "avg_price": float(pos.avg_price or 0.0)}


def _should_reconcile_position(state: RuntimeState, now: datetime, *, force: bool = False) -> bool:
    if force or state.position is not None:
        return True
    if not state.last_position_reconcile_at:
        return True
    try:
        last = datetime.fromisoformat(state.last_position_reconcile_at)
    except ValueError:
        return True
    return (now - last).total_seconds() >= config.FLAT_POSITION_RECONCILE_INTERVAL_SEC


def reconcile_position_state(broker, state: RuntimeState, now: datetime, *, force: bool = False) -> str:
    if not _should_reconcile_position(state, now, force=force):
        return str((state.position_reconcile_diag or {}).get("comparison_result") or MATCH_FLAT)
    try:
        broker_positions, all_positions = _normalize_broker_positions(broker.get_positions())
        broker_error = None
    except Exception as exc:
        broker_positions, all_positions = {}, []
        broker_error = repr(exc)

    runtime = _runtime_position_dict(state)
    diag = {
        "runtime_position": runtime,
        "broker_positions": all_positions,
        f"{config.LONG_SYMBOL}_broker_qty": int((broker_positions.get(config.LONG_SYMBOL) or {}).get("qty") or 0),
        f"{config.INVERSE_SYMBOL}_broker_qty": int((broker_positions.get(config.INVERSE_SYMBOL) or {}).get("qty") or 0),
        "reconciled_at": now.isoformat(),
        "broker_response_error": broker_error,
    }

    if broker_error:
        diag.update({"comparison_result": POSITION_DATA_ERROR, "mismatch_reason": broker_error})
        state.position_reconcile_diag = diag
        state.last_position_reconcile_at = now.isoformat()
        return POSITION_DATA_ERROR

    broker_owned = [row for row in broker_positions.values() if int(row["qty"]) > 0]
    if runtime["qty"] <= 0 and not broker_owned:
        diag.update({"comparison_result": MATCH_FLAT, "mismatch_reason": ""})
        state.position = None
        state.position_reconcile_diag = diag
        state.last_position_reconcile_at = now.isoformat()
        return MATCH_FLAT

    if runtime["qty"] > 0:
        broker_row = broker_positions.get(str(runtime["symbol"]))
        if broker_row and int(broker_row["qty"]) == int(runtime["qty"]):
            diag.update({"comparison_result": MATCH_POSITION, "mismatch_reason": ""})
            state.position_reconcile_diag = diag
            state.last_position_reconcile_at = now.isoformat()
            return MATCH_POSITION
        if broker_row and int(broker_row["qty"]) > 0:
            # 2026-08-07 real incident: runtime recorded qty from a partial
            # fill (e.g. 528/1269 requested) but the broker's own reported
            # qty for the SAME symbol later settled to a different number --
            # this used to fall straight into the POSITION_MISMATCH catch-all
            # below, which blocks every order (entry/switch/exit) and never
            # self-heals (nothing else in this function ever revisits a
            # same-symbol qty difference), so a genuine opposite-flag/STOP_LOSS
            # exit could stay silently blocked tick after tick forever. The
            # broker is always the authority on live holdings (same principle
            # as RECOVERED_FROM_BROKER/RECOVERED_TO_FLAT below) -- adopt its
            # qty/avg_price immediately so this tick's own exit/switch
            # evaluation (the caller re-reads state.position right after this
            # call) already sees the corrected, sellable quantity.
            old_qty = int(runtime["qty"])
            new_qty = int(broker_row["qty"])
            prior_entry_at = state.position.entry_at if state.position else now
            state.position = PositionSnapshot(
                symbol=runtime["symbol"], quantity=new_qty,
                avg_price=float(broker_row.get("avg_price") or runtime["avg_price"] or 0.0),
                entry_at=prior_entry_at,
            )
            diag.update({
                "comparison_result": RECOVERED_QTY_MISMATCH,
                "mismatch_reason": f"runtime_qty={old_qty}_broker_qty={new_qty}",
            })
            state.position_reconcile_diag = diag
            state.last_position_reconcile_at = now.isoformat()
            return RECOVERED_QTY_MISMATCH
        if not broker_owned:
            state.position = None
            state.peak_net_return = 0.0
            state.profit_lock_active = False
            diag.update({"comparison_result": RECOVERED_TO_FLAT, "mismatch_reason": "runtime_position_broker_flat"})
            state.position_reconcile_diag = diag
            state.last_position_reconcile_at = now.isoformat()
            return RECOVERED_TO_FLAT

    if runtime["qty"] <= 0 and broker_owned:
        recovered = broker_owned[0]
        state.position = PositionSnapshot(
            symbol=recovered["symbol"], quantity=int(recovered["qty"]),
            avg_price=float(recovered["avg_price"] or 0.0), entry_at=now,
        )
        diag.update({"comparison_result": RECOVERED_FROM_BROKER, "mismatch_reason": "runtime_flat_broker_position"})
        state.position_reconcile_diag = diag
        state.last_position_reconcile_at = now.isoformat()
        return RECOVERED_FROM_BROKER

    diag.update({"comparison_result": POSITION_MISMATCH, "mismatch_reason": "runtime_broker_position_diff"})
    state.position_reconcile_diag = diag
    state.last_position_reconcile_at = now.isoformat()
    return POSITION_MISMATCH


def _quote_status_for_order(market_data: MarketDataService, symbols: tuple[str, ...]) -> tuple[str, dict[str, float]]:
    statuses = market_data.quote_statuses(symbols)
    valid_prices = _fresh_quote_prices(market_data, symbols)
    vals = set(statuses.values())
    if vals == {"VALID"}:
        return "READY", valid_prices
    if "STALE" in vals:
        return QUOTE_STALE, valid_prices
    return order_executor.BLOCK_ORDER_DATA_INVALID, valid_prices


def _required_quote_symbols(direction: Direction, position: Optional[PositionSnapshot]) -> tuple[str, ...]:
    """Only the symbols an actual order touches (the currently-held ETF, if
    any, and the new target ETF) -- never WATCH_SYMBOL(000660).

    2026-08-12 real incident: WATCH_SYMBOL is signal-source-only (never
    priced/sized/ordered anywhere in order_executor.py) but used to be
    unconditionally required fresh here too. market_data.refresh_quotes()
    fetches its 3 symbols sequentially over one real KIS call each (single
    io_lock, no concurrent KIS calls by design), and WATCH_SYMBOL is fetched
    first in that sequence -- so by the time the cycle comes back around, its
    quote is consistently the stalest of the three (observed 13-21s old vs
    ~2-8s for the traded ETFs on this exact incident date). That alone kept
    tripping the >QUOTE_MAX_AGE_SEC(10s) check and produced
    MISSED_SIGNAL_QUOTE_STALE on every single confirmed flag that day (4/4),
    even though the actually-traded ETF's own quote was fresh enough every
    time. Dropping the never-traded symbol from this requirement removes a
    check that was never protecting anything real.
    """
    symbols: list[str] = []
    if position is not None and position.quantity > 0 and position.symbol:
        symbols.append(position.symbol)
    target = order_executor.target_symbol_for_direction(direction)
    if target:
        symbols.append(target)
    return tuple(dict.fromkeys(symbols))


def _quote_ages(market_data: MarketDataService, symbols: tuple[str, ...]) -> dict[str, Optional[float]]:
    ages: dict[str, Optional[float]] = {}
    for symbol in symbols:
        snap = market_data.get_quote(symbol)
        ages[symbol] = snap.age_sec if snap is not None else None
    return ages


def _pending_detected_at(pending: dict[str, Any], now: datetime) -> datetime:
    """The ORIGINAL detection time of a retried pending signal — the
    QUOTE_STALE 15s window (config.QUOTE_STALE_MAX_WAIT_SEC) is anchored to
    when the signal was first confirmed, not to this retry tick's ``now``."""
    raw = pending.get("detected_at")
    if raw:
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            pass
    return now


def _pending_age_sec(pending: dict[str, Any], now: datetime) -> Optional[float]:
    raw = pending.get("detected_at")
    if not raw:
        return None
    try:
        return (now - datetime.fromisoformat(str(raw))).total_seconds()
    except ValueError:
        return None


def _pending_direction_still_active(pending_dir: Optional[Direction], macd_snap) -> bool:
    if pending_dir == Direction.UP_RED:
        return (macd_snap.current_diff if macd_snap.current_diff is not None else macd_snap.macd - macd_snap.signal) > 0
    if pending_dir == Direction.DOWN_BLUE:
        return (macd_snap.current_diff if macd_snap.current_diff is not None else macd_snap.macd - macd_snap.signal) < 0
    return False


def _quote_valid_for_provisional(market_data: MarketDataService, symbol: str) -> bool:
    snap = market_data.get_quote(symbol)
    return bool(
        snap is not None
        and not snap.error
        and snap.price > 0
        and (snap.age_sec is None or snap.age_sec <= config.QUOTE_MAX_AGE_SEC)
    )


def _update_provisional_diagnostics(state: RuntimeState, macd_snap) -> None:
    """Raw, unconfirmed live diff/MACD/signal display — updated every tick
    regardless of candidate/confirmation status (diagnostic only, never order
    authority)."""
    state.provisional_bar_start = macd_snap.bar_dt.astimezone(KST).isoformat()
    state.provisional_bar_end = (macd_snap.bar_dt + timedelta(minutes=3)).astimezone(KST).isoformat()
    state.provisional_macd = macd_snap.macd
    state.provisional_signal = macd_snap.signal
    state.provisional_diff = macd_snap.current_diff


def _update_provisional_shadow_flag(state: RuntimeState, macd_snap, pattern: Direction, signal_id: Optional[str]) -> None:
    """Shadow/candidate display only (2026-07-27 KIS-parity fix) — the
    forming/provisional bar never carries order, stats, or last_direction
    authority any more. See _advance_confirmed_primary() for the actual
    order-authoritative Primary flag (completed 3m bars only)."""
    _update_provisional_diagnostics(state, macd_snap)
    state.provisional_flag = pattern if pattern != Direction.HOLD else None
    state.provisional_signal_id = signal_id


def _reset_candidate(state: RuntimeState) -> None:
    state.candidate_flag = None
    state.candidate_bar_ts = None
    state.candidate_first_seen_at = None
    state.candidate_first_diff = None


def _advance_provisional_candidate(
    state: RuntimeState,
    provisional_snap,
    provisional_condition: Direction,
    now: datetime,
    *,
    today_has_completed_bar: bool,
) -> tuple[Optional[Any], Direction]:
    """Two-tick shadow/candidate gate (2026-07-27 momentary-crossing fix,
    demoted to display-only by the 2026-07-27 KIS-parity fix — this NEVER
    drives orders/stats/last_direction any more; see
    _advance_confirmed_primary() for that).

    A single-tick provisional forming-bar crossing from
    evaluate_primary_forming_crossover() only becomes a "confirmed candidate"
    (for UI display) once the SAME direction is still present on a LATER,
    fresh quote tick at least config.PROVISIONAL_CONFIRM_MIN_GAP_SEC apart.
    Any other outcome this tick — HOLD, the opposite direction, or the
    forming bar rolling over — cancels the candidate immediately; a fresh
    candidate may start right after.

    ``today_has_completed_bar`` suppresses the very first forming bar of a
    new trading day the same way _advance_confirmed_primary() suppresses the
    first completed bar — previous_diff there still refers to yesterday.
    """
    if provisional_snap is None or provisional_condition == Direction.HOLD or not today_has_completed_bar:
        _reset_candidate(state)
        return None, Direction.HOLD

    bar_key = provisional_snap.bar_dt.isoformat()
    same_candidate = state.candidate_flag == provisional_condition and state.candidate_bar_ts == bar_key
    if not same_candidate:
        # First sighting of this direction on this forming bar — arm the
        # candidate only, never a dispatch signal.
        state.candidate_flag = provisional_condition
        state.candidate_bar_ts = bar_key
        state.candidate_first_seen_at = now.isoformat()
        state.candidate_first_diff = provisional_snap.current_diff
        if config.PROVISIONAL_CONFIRM_MIN_GAP_SEC <= 0:
            state.candidate_confirmed_at = now.isoformat()
            state.candidate_confirmed_diff = provisional_snap.current_diff
            return provisional_snap, provisional_condition
        return None, Direction.HOLD

    gap_sec = None
    if state.candidate_first_seen_at:
        try:
            gap_sec = (now - datetime.fromisoformat(state.candidate_first_seen_at)).total_seconds()
        except ValueError:
            gap_sec = None
    if gap_sec is None or gap_sec < config.PROVISIONAL_CONFIRM_MIN_GAP_SEC:
        return None, Direction.HOLD

    state.candidate_confirmed_at = now.isoformat()
    state.candidate_confirmed_diff = provisional_snap.current_diff
    return provisional_snap, provisional_condition


def _last_1m_diag(df_1m) -> tuple[Optional[str], Optional[float]]:
    if df_1m is None or getattr(df_1m, "empty", True) or "datetime" not in df_1m.columns or "close" not in df_1m.columns:
        return None, None
    work = df_1m.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    closes = pd.to_numeric(work["close"], errors="coerce")
    work = work.loc[work["datetime"].notna() & closes.notna()].copy()
    if work.empty:
        return None, None
    row = work.sort_values("datetime").iloc[-1]
    return pd.Timestamp(row["datetime"]).to_pydatetime().isoformat(), float(row["close"])


def _update_forming_input_diag(
    state: RuntimeState,
    *,
    now: datetime,
    df_1m,
    watch_price: Optional[float],
    market_data: MarketDataService,
) -> None:
    forming_start, forming_end = forming_bar_window(now)
    last_1m_at, last_1m_close = _last_1m_diag(df_1m)
    state.provisional_bar_start = forming_start.isoformat()
    state.provisional_bar_end = forming_end.isoformat()
    state.provisional_evaluated_at = datetime.now(KST).isoformat()
    state.provisional_input_now = now.astimezone(KST).isoformat()
    state.provisional_quote_price = watch_price
    state.provisional_last_1m_at = last_1m_at
    state.provisional_last_1m_close = last_1m_close
    diag = market_data.quote_normalization_diag() if hasattr(market_data, "quote_normalization_diag") else {}
    note = str(diag.get("reason") or "")
    if diag:
        note = note or "NO_SCALE_CHANGE"
    state.provisional_price_scale_note = note


def _update_history_freshness_diag(
    state: RuntimeState, *, df_1m, macd_snap, watch_price: Optional[float], now: datetime,
) -> None:
    """docs 2026-07-27 §1: 당일 추가 1분봉 수/history newest/마지막 완성 3분봉
    시각을 runtime/UI에 표시하고, KIS 1분봉(history)과 실시간 quote의 단위·
    시각이 설명되지 않게 어긋나면 주문을 차단한다 (quote_history_mismatch_reason).
    market_data._normalize_quote_price()가 이미 10배/0.1배 스케일 오차는
    보정하므로, 여기서는 그 보정 이후에도 남는 큰 괴리(단위 불일치)와 1분봉
    history 자체가 갱신되지 않는 시각 불일치만 잡아낸다.
    """
    today_str = now.astimezone(KST).strftime("%Y%m%d")
    today_count = 0
    newest_at: Optional[str] = None
    newest_close: Optional[float] = None
    newest_dt: Optional[datetime] = None
    if df_1m is not None and not df_1m.empty and "datetime" in df_1m.columns:
        dates = df_1m["datetime"].dt.strftime("%Y%m%d")
        today_count = int((dates == today_str).sum())
        last_row = df_1m.sort_values("datetime").iloc[-1]
        newest_dt = pd.Timestamp(last_row["datetime"]).to_pydatetime()
        newest_at = newest_dt.isoformat()
        if "close" in df_1m.columns:
            try:
                newest_close = float(last_row["close"])
            except (TypeError, ValueError):
                newest_close = None
    state.today_1m_bar_count = today_count
    state.history_newest_at = newest_at
    state.last_completed_3m_bar_at = macd_snap.bar_dt.isoformat() if macd_snap is not None else None

    reason: Optional[str] = None
    if now.time() >= config.SESSION_OPEN and not _within_open_grace_window(now):
        if newest_dt is None:
            reason = "HISTORY_EMPTY"
        elif (now - newest_dt).total_seconds() > config.HISTORY_STALE_MAX_SEC:
            reason = "HISTORY_STALE"
    if reason is None and watch_price is not None and newest_close is not None and newest_close > 0:
        ratio = float(watch_price) / float(newest_close)
        if not (config.QUOTE_HISTORY_PRICE_RATIO_MIN <= ratio <= config.QUOTE_HISTORY_PRICE_RATIO_MAX):
            reason = "QUOTE_HISTORY_PRICE_MISMATCH"
    state.quote_history_mismatch_reason = reason


def _within_open_grace_window(now: datetime, *, grace_sec: float = 60.0) -> bool:
    """A brief grace window right at SESSION_OPEN — the history-updater
    thread may not have pulled today's very first 1m bar yet even though the
    session clock has already ticked over; avoid a false HISTORY_STALE/EMPTY
    block in that narrow window."""
    open_dt = now.astimezone(KST).replace(
        hour=config.SESSION_OPEN.hour, minute=config.SESSION_OPEN.minute, second=0, microsecond=0,
    )
    return bool(now.astimezone(KST) < open_dt + timedelta(seconds=grace_sec))


def _expire_pending_if_needed(state: RuntimeState, macd_snap, now: datetime) -> bool:
    pending = state.pending_signal
    if not pending:
        return False
    pending_dir = Direction(pending.get("direction")) if pending.get("direction") in {d.value for d in Direction} else None
    age = _pending_age_sec(pending, now)
    inactive = macd_snap is not None and not _pending_direction_still_active(pending_dir, macd_snap)
    if inactive or (age is not None and age > config.PENDING_SIGNAL_RETRY_SEC):
        pending["status"] = SignalState.EXPIRED.value
        state.pending_signal = None
        return True
    return False


def _set_pending_signal(
    state: RuntimeState,
    *,
    signal_id: str,
    direction: Direction,
    signal_type: str,
    macd_snap,
    detected_at: datetime,
    reason: str,
) -> None:
    existing = state.pending_signal if state.pending_signal and state.pending_signal.get("signal_id") == signal_id else {}
    state.pending_signal = {
        "signal_id": signal_id,
        "direction": direction.value,
        "signal_type": signal_type,
        "bar_ts": macd_snap.bar_dt.isoformat(),
        "detected_at": existing.get("detected_at") or detected_at.isoformat(),
        "status": SignalState.WAITING.value,
        "reason": reason,
        "order_requested": False,
    }


def _has_order_request(outcome) -> bool:
    return bool(outcome.timestamps.get("buy_requested_at") or outcome.timestamps.get("sell_requested_at"))


def _mark_processed_after_request(state: RuntimeState, outcome) -> None:
    if (
        _has_order_request(outcome)
        and not _sell_cleared_but_buy_not_requested(outcome)
        and outcome.signal_id
        and outcome.signal_id not in state.processed_signal_ids
    ):
        state.processed_signal_ids = list(state.processed_signal_ids) + [outcome.signal_id]


def _sell_cleared_but_buy_not_requested(outcome) -> bool:
    return bool(
        outcome is not None
        and outcome.sell_result is not None
        and outcome.sell_result.success
        and outcome.sell_qty_after == 0
        and not outcome.timestamps.get("buy_requested_at")
    )


def _execute_or_wait(
    *,
    broker,
    market_data: MarketDataService,
    state: RuntimeState,
    now: datetime,
    macd_snap,
    direction: Direction,
    signal_id: str,
    signal_type: str,
    position: Optional[PositionSnapshot],
    result: TickResult,
    signal_detected_at: Optional[datetime] = None,
):
    order_started = time.monotonic()
    result.signal_dispatch_trace = {
        "signal_id": signal_id,
        "direction": direction.value,
        "signal_type": signal_type,
        "completed_bar_at": macd_snap.bar_dt.isoformat(),
        "forming_bar_start": "",
        "forming_bar_end": "",
        "position_reconcile_result": None,
        "quote_status": None,
        "required_quote_symbols": [],
        "quote_ages": {},
        "target_quote_valid": False,
        "order_executor_called": False,
        "executor_called_at": None,
        "broker_called": False,
        "broker_order_id": "",
        "broker_raw": {},
        "final_block_reason": None,
    }
    reconcile = reconcile_position_state(broker, state, now, force=True)
    result.signal_dispatch_trace["position_reconcile_result"] = reconcile
    if reconcile == RECOVERED_FROM_BROKER:
        state.order_block_reason = RECOVERED_FROM_BROKER
        result.signal_dispatch_trace["final_block_reason"] = RECOVERED_FROM_BROKER
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=RECOVERED_FROM_BROKER,
        )
        result.skipped = RECOVERED_FROM_BROKER
        result.timing["order_execution"] = time.monotonic() - order_started
        return None
    if reconcile == RECOVERED_QTY_MISMATCH:
        # Broker-corrected qty for the SAME held symbol (see
        # reconcile_position_state) -- not a hard block. Use the freshly
        # corrected snapshot for this order (the caller's ``position``
        # argument is a snapshot captured before this reconcile ran and
        # would otherwise still carry the stale quantity into
        # order_executor.execute_signal's SELL leg).
        position = state.position
    elif reconcile in (POSITION_DATA_ERROR, POSITION_MISMATCH):
        state.order_block_reason = reconcile
        result.signal_dispatch_trace["final_block_reason"] = reconcile
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=reconcile,
        )
        result.skipped = reconcile
        result.timing["order_execution"] = time.monotonic() - order_started
        return None

    required_symbols = _required_quote_symbols(direction, position)
    quote_status, quotes = _quote_status_for_order(market_data, required_symbols)
    target = order_executor.target_symbol_for_direction(direction)
    detected_at = signal_detected_at or now
    quote_ages_at_detection = _quote_ages(market_data, required_symbols)
    result.signal_dispatch_trace["required_quote_symbols"] = list(required_symbols)
    result.signal_dispatch_trace["quote_ages"] = quote_ages_at_detection
    result.signal_dispatch_trace["quote_status"] = quote_status
    result.signal_dispatch_trace["target_quote_valid"] = bool(target and target in quotes and quotes[target] > 0)
    state.last_quote_stale_signal_id = signal_id
    state.last_quote_stale_quote_ages = str(quote_ages_at_detection)

    retry_count = 0
    while quote_status != "READY":
        elapsed = (datetime.now(KST) - detected_at).total_seconds()
        if elapsed >= config.QUOTE_STALE_MAX_WAIT_SEC or retry_count >= config.QUOTE_STALE_RETRY_MAX_ATTEMPTS:
            break
        market_data.refresh_quotes(symbols=required_symbols)
        time.sleep(config.QUOTE_STALE_RETRY_INTERVAL_SEC)
        retry_count += 1
        quote_status, quotes = _quote_status_for_order(market_data, required_symbols)

    result.signal_dispatch_trace["quote_stale_retry_count"] = retry_count
    state.last_quote_stale_retry_count = retry_count

    if quote_status != "READY":
        state.order_block_reason = config.MISSED_SIGNAL_QUOTE_STALE
        state.last_quote_stale_result = config.MISSED_SIGNAL_QUOTE_STALE
        result.signal_dispatch_trace["final_block_reason"] = config.MISSED_SIGNAL_QUOTE_STALE
        result.signal_dispatch_trace["quote_ages"] = _quote_ages(market_data, required_symbols)
        # Resolved synchronously within this one call/tick — never left as a
        # cross-tick pending retry (docs 2026-07-27 QUOTE_STALE fix), so no
        # later tick can mistakenly dispatch this signal_id late.
        state.pending_signal = None
        result.skipped = config.MISSED_SIGNAL_QUOTE_STALE
        result.timing["order_execution"] = time.monotonic() - order_started
        return None

    state.last_quote_stale_result = "RECOVERED" if retry_count > 0 else None
    result.signal_dispatch_trace["quote_ages"] = _quote_ages(market_data, required_symbols)

    result.signal_dispatch_trace["order_executor_called"] = True
    result.signal_dispatch_trace["executor_called_at"] = datetime.now(KST).isoformat()
    outcome = order_executor.execute_signal(
        broker=broker, direction=direction, signal_id=signal_id, quotes=quotes,
        position=position, budget=state.budget,
        processed_signal_ids=frozenset(state.processed_signal_ids),
        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
    )
    if outcome is None:
        state.order_block_reason = SIGNAL_NOT_DISPATCHED
        result.skipped = SIGNAL_NOT_DISPATCHED
        result.signal_dispatch_trace["final_block_reason"] = SIGNAL_NOT_DISPATCHED
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=SIGNAL_NOT_DISPATCHED,
        )
        result.timing["order_execution"] = time.monotonic() - order_started
        return None
    result.order_requested_at = outcome.timestamps.get("sell_requested_at") or outcome.timestamps.get("buy_requested_at")
    result.signal_dispatch_trace["order_requested_at"] = result.order_requested_at or ""
    if (
        outcome.orderable_cash_at_sizing is not None
        or outcome.ask1 is not None
        or outcome.order_type is not None
    ):
        state.last_order_orderable_cash = outcome.orderable_cash_at_sizing
        state.last_order_nrcvb_buy_amt = outcome.nrcvb_buy_amt
        state.last_order_nrcvb_buy_qty = outcome.nrcvb_buy_qty
        state.last_order_psbl_qty_calc_unpr = outcome.psbl_qty_calc_unpr
        state.last_order_ask1 = outcome.ask1
        state.last_order_order_price = outcome.order_price
        state.last_order_order_type = outcome.order_type
        state.last_order_usable_cash = outcome.usable_cash
        state.last_order_limit_buyable_qty = outcome.limit_buyable_qty
        state.last_order_budget_qty = outcome.budget_qty
        state.last_order_final_qty = outcome.final_qty
        state.last_order_sizing_rt_cd = outcome.sizing_rt_cd
        state.last_order_sizing_msg_cd = outcome.sizing_msg_cd
        state.last_order_sizing_msg1 = outcome.sizing_msg1
        state.last_order_sizing_price = outcome.sizing_price
        state.last_order_requested_qty = outcome.buy_result.requested_qty if outcome.buy_result else outcome.quantity
        state.last_order_expected_amount = outcome.expected_amount
    state.last_order_failure_stage = outcome.order_failure_stage
    state.last_order_filled_qty = outcome.filled_qty
    state.last_order_fill_poll_result = outcome.fill_poll_result
    state.last_order_balance_qty = outcome.balance_qty
    _record_broker_order_result(state, outcome)
    broker_result = outcome.buy_result or outcome.sell_result
    if broker_result is not None:
        result.signal_dispatch_trace["broker_called"] = True
        result.signal_dispatch_trace["broker_order_id"] = broker_result.order_id
        result.signal_dispatch_trace["broker_raw"] = dict(broker_result.raw or {})
    result.signal_dispatch_trace["orderable_cash"] = outcome.orderable_cash_at_sizing
    result.signal_dispatch_trace["nrcvb_buy_amt"] = outcome.nrcvb_buy_amt
    result.signal_dispatch_trace["nrcvb_buy_qty"] = outcome.nrcvb_buy_qty
    result.signal_dispatch_trace["psbl_qty_calc_unpr"] = outcome.psbl_qty_calc_unpr
    result.signal_dispatch_trace["ask1"] = outcome.ask1
    result.signal_dispatch_trace["order_price"] = outcome.order_price
    result.signal_dispatch_trace["order_type"] = outcome.order_type
    result.signal_dispatch_trace["usable_cash"] = outcome.usable_cash
    result.signal_dispatch_trace["limit_buyable_qty"] = outcome.limit_buyable_qty
    result.signal_dispatch_trace["budget_qty"] = outcome.budget_qty
    result.signal_dispatch_trace["final_qty"] = outcome.final_qty
    result.signal_dispatch_trace["sizing_price"] = outcome.sizing_price
    result.signal_dispatch_trace["requested_qty"] = outcome.buy_result.requested_qty if outcome.buy_result else outcome.quantity
    result.signal_dispatch_trace["expected_amount"] = outcome.expected_amount
    result.signal_dispatch_trace["sizing_rt_cd"] = outcome.sizing_rt_cd
    result.signal_dispatch_trace["sizing_msg_cd"] = outcome.sizing_msg_cd
    result.signal_dispatch_trace["sizing_msg1"] = outcome.sizing_msg1
    result.signal_dispatch_trace["filled_qty"] = outcome.filled_qty
    result.signal_dispatch_trace["fill_poll_result"] = outcome.fill_poll_result
    result.signal_dispatch_trace["balance_qty"] = outcome.balance_qty
    result.signal_dispatch_trace["failure_stage"] = outcome.order_failure_stage or ""
    sell_only_switch_needs_buy_retry = _sell_cleared_but_buy_not_requested(outcome)
    if _has_order_request(outcome) and not sell_only_switch_needs_buy_retry:
        if state.pending_signal and state.pending_signal.get("signal_id") == signal_id:
            state.pending_signal["status"] = SignalState.ORDER_REQUESTED.value
            state.pending_signal["order_requested"] = True
        _mark_processed_after_request(state, outcome)
    if outcome.final_state == SignalState.BLOCKED and outcome.block_reason in TEMPORARY_BLOCK_REASONS:
        state.order_block_reason = outcome.block_reason
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=outcome.block_reason or "BLOCKED",
        )
    else:
        state.pending_signal = None
    result.signal_dispatch_trace["final_block_reason"] = outcome.block_reason or ""
    result.timing["order_execution"] = time.monotonic() - order_started
    return outcome


def _advance_confirmed_primary(state: RuntimeState, macd_snap) -> Direction:
    """Primary (order-authoritative) crossover — completed 3m bars ONLY
    (docs 2026-07-27 KIS-parity fix; restored 2026-08-03 to the exact
    known-good rule from commit 6a2fd07, which ran unmodified 2026-07-28
    through 2026-07-30 and reproduces the 2026-08-03 KIS chart's 14
    confirmed flags exactly — see docs/MACD2_LOGIC.md for the git-archaeology
    writeup. The 2026-07-31 color+regime/debounce rewrite that briefly
    replaced this was found to under-detect real KIS flags by ~85% and is
    removed; do not reintroduce color-state/regime/pending debounce here):
    previous_diff/current_diff come solely from calculate_macd(bars_3m), the
    same confirmed MACD(12,26,9) KIS itself charts a flag on for a completed
    bar. Evaluated exactly once per new completed-bar timestamp — a repeat
    tick against the same bar_dt is always HOLD here, regardless of
    direction.

    The very first completed bar this state has ever evaluated (or the
    first one on a NEW calendar date relative to the previously evaluated
    bar) sets direction baseline only, never dispatches: previous_diff there
    can span across a trading-day gap (yesterday's last bar vs. today's
    first), so any zero-crossing is an overnight-gap artifact, not an
    intraday reversal. It is also never counted toward the repeat-direction
    suppression state, so a genuine later same-direction crossing still
    fires normally.
    """
    bar_key = macd_snap.bar_dt.isoformat()
    if state.last_confirmed_bar_ts == bar_key:
        return Direction.HOLD
    prior_bar_ts = state.last_confirmed_bar_ts
    state.last_confirmed_bar_ts = bar_key
    is_first_of_day = True
    if prior_bar_ts:
        prior_dt = _parse_iso_dt(prior_bar_ts)
        is_first_of_day = prior_dt is None or prior_dt.astimezone(KST).date() != macd_snap.bar_dt.astimezone(KST).date()
    if is_first_of_day:
        return Direction.HOLD
    direction = evaluate_macd_crossover(macd_snap, state.last_detected_direction)
    if direction != Direction.HOLD:
        state.last_detected_direction = direction
        state.latest_primary_flag = direction
        state.latest_primary_signal_id = make_signal_id(macd_snap.bar_dt, direction)
    return direction


def _direction_for_symbol(symbol: Optional[str]) -> Optional[Direction]:
    """UP_RED position == holding LONG_SYMBOL, DOWN_BLUE == INVERSE_SYMBOL."""
    if not symbol:
        return None
    if symbol == config.LONG_SYMBOL:
        return Direction.UP_RED
    if symbol == config.INVERSE_SYMBOL:
        return Direction.DOWN_BLUE
    return None


def _position_direction(position: Optional[PositionSnapshot]) -> Optional[Direction]:
    if position is None or int(position.quantity or 0) <= 0:
        return None
    return _direction_for_symbol(position.symbol)


def _parse_iso_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _major_last_entry_at(state: RuntimeState, position: Optional[PositionSnapshot]) -> Optional[datetime]:
    if position is not None and int(position.quantity or 0) > 0 and position.entry_at is not None:
        return position.entry_at
    return _parse_iso_dt(state.last_major_entry_at)


def _major_same_direction_exit_at(state: RuntimeState, flag_direction: Direction) -> Optional[datetime]:
    if state.last_major_exit_direction != flag_direction:
        return None
    return _parse_iso_dt(state.last_major_exit_at)


def _persist_major_decision(state: RuntimeState, decision: MajorFlagDecision, signal_id: str) -> None:
    state.major_filter_version = config.MAJOR_FILTER_VERSION
    state.last_major_score = float(decision.score)
    state.last_major_required_score = float(decision.required_score)
    state.last_major_approved = bool(decision.approved)
    state.last_major_decision = decision.decision
    state.last_major_block_reason = decision.block_reason
    state.last_major_is_reversal = bool(decision.is_reversal)
    state.last_major_fast_reversal = bool(decision.fast_reversal)
    state.last_major_component_scores = dict(decision.component_scores or {})
    state.last_major_metrics = dict(decision.metrics or {})
    state.last_major_signal_id = signal_id


def _judge_major_flag(
    *,
    state: RuntimeState,
    bars_3m,
    direction: Direction,
    position: Optional[PositionSnapshot],
    now: datetime,
    signal_id: str,
) -> MajorFlagDecision:
    """Score + gate an ALREADY-confirmed crossover (order authority only).

    Never called when ``state.major_filter_enabled`` is False, never creates or
    suppresses a confirmed flag itself (``_advance_confirmed_primary`` and the
    latest_primary_* stats stay exactly as they were), and never touches
    STOP_LOSS / PROFIT_LOCK / FORCED_LIQUIDATION.
    """
    position_direction = _position_direction(position)
    last_entry_at = _major_last_entry_at(state, position)
    daily_count = int(state.daily_major_entry_count or 0)
    decision = major_flag_filter.evaluate_major_flag(
        bars_3m, direction, position_direction, last_entry_at, daily_count, now,
    )
    decision = major_flag_filter.apply_major_trade_gates(
        decision,
        flag_direction=direction,
        position_direction=position_direction,
        last_entry_at=last_entry_at,
        last_same_direction_exit_at=_major_same_direction_exit_at(state, direction),
        daily_major_entry_count=daily_count,
        now=now,
    )
    _persist_major_decision(state, decision, signal_id)
    return decision


def _persist_sideways_decision(state: RuntimeState, decision: MajorFlagDecision, signal_id: str) -> None:
    state.sideways_filter_version = config.SIDEWAYS_FILTER_VERSION
    state.last_sideways_score = float(decision.score)
    state.last_sideways_required_score = float(decision.required_score)
    state.last_sideways_approved = bool(decision.approved)
    state.last_sideways_decision = decision.decision
    state.last_sideways_block_reason = decision.block_reason
    state.last_sideways_component_scores = dict(decision.component_scores or {})
    state.last_sideways_metrics = dict(decision.metrics or {})
    state.last_sideways_signal_id = signal_id


def _judge_sideways_flag(
    *, state: RuntimeState, bars_3m, df_1m, direction: Direction, now: datetime, signal_id: str,
) -> MajorFlagDecision:
    """Score + gate an ALREADY-confirmed crossover for the 추세전환장 mode
    (order authority only). Never called when
    ``state.sideways_filter_enabled`` is False; never creates or suppresses
    a confirmed flag itself, and never touches STOP_LOSS / PROFIT_LOCK /
    FORCED_LIQUIDATION.

    2026-08-07 v5: sideways_filter.evaluate_sideways_flag now owns the full
    time-window decision itself (09:00-11:00 PRIMARY_TREND-pullback-only vs
    11:00+ score+breakout gate) -- this wrapper only persists the result."""
    decision = sideways_filter.evaluate_sideways_flag(bars_3m, df_1m, direction, now)
    _persist_sideways_decision(state, decision, signal_id)
    return decision


def _persist_trend_persistence_decision(state: RuntimeState, decision: MajorFlagDecision, signal_id: str) -> None:
    state.trend_persistence_filter_version = config.TREND_PERSISTENCE_FILTER_VERSION
    state.last_trend_persistence_score = float(decision.score)
    state.last_trend_persistence_required_score = float(decision.required_score)
    state.last_trend_persistence_approved = bool(decision.approved)
    state.last_trend_persistence_decision = decision.decision
    state.last_trend_persistence_block_reason = decision.block_reason
    state.last_trend_persistence_component_scores = dict(decision.component_scores or {})
    state.last_trend_persistence_metrics = dict(decision.metrics or {})
    state.last_trend_persistence_signal_id = signal_id


def _judge_trend_persistence_flag(
    *, state: RuntimeState, bars_3m, df_1m, direction: Direction, now: datetime, signal_id: str,
) -> MajorFlagDecision:
    """Score + gate an ALREADY-confirmed crossover against
    trend_persistence_filter.evaluate_trend_persistence (order authority
    only). Never called when ``state.trend_persistence_filter_enabled`` is
    False; never creates or suppresses a confirmed flag itself, and never
    touches STOP_LOSS / PROFIT_LOCK / FORCED_LIQUIDATION."""
    decision = trend_persistence_filter.evaluate_trend_persistence(
        bars_3m, df_1m, direction, now, score_min=config.TREND_PERSISTENCE_SCORE_MIN,
    )
    _persist_trend_persistence_decision(state, decision, signal_id)
    return decision


def _persist_single_entry_decision(state: RuntimeState, decision: MajorFlagDecision, signal_id: str) -> None:
    state.single_entry_filter_version = config.SINGLE_ENTRY_FILTER_VERSION
    state.last_single_entry_approved = bool(decision.approved)
    state.last_single_entry_decision = decision.decision
    state.last_single_entry_block_reason = decision.block_reason
    state.last_single_entry_signal_id = signal_id
    state.last_single_entry_score = decision.score
    state.last_single_entry_flag_seq = decision.metrics.get("flag_seq")
    state.last_single_entry_near_zero_blue = decision.metrics.get("near_zero_blue")


def _judge_single_entry_flag(
    *, state: RuntimeState, bars_3m, df_1m, direction: Direction, now: datetime, signal_id: str,
) -> MajorFlagDecision:
    """Gate an ALREADY-confirmed crossover against single_entry_filter.
    evaluate_single_entry (order authority only) — v3: scores EVERY
    confirmed flag of the day (daily_confirmed_flag_count, incremented
    here once per confirmed flag regardless of approval/fill — distinct
    from daily_single_entry_count, which only counts actual fills toward
    the SINGLE_ENTRY_MAX_DAILY_ENTRIES cap), so a 4th+ flag is never
    auto-blocked and a weak 1st-3rd flag is never auto-approved. Never
    called when ``state.single_entry_filter_enabled`` is False; never
    creates or suppresses a confirmed flag itself, and never touches
    STOP_LOSS / PROFIT_LOCK / FORCED_LIQUIDATION."""
    state.daily_confirmed_flag_count = int(state.daily_confirmed_flag_count or 0) + 1
    flag_seq = state.daily_confirmed_flag_count
    decision = single_entry_filter.evaluate_single_entry(
        bars_3m, df_1m, direction, now, flag_seq, state.daily_single_entry_count,
    )
    _persist_single_entry_decision(state, decision, signal_id)
    return decision


def _persist_time_window_decision(state: RuntimeState, decision: MajorFlagDecision, signal_id: str) -> None:
    state.time_window_filter_version = config.TIME_WINDOW_FILTER_VERSION
    state.last_time_window_score = float(decision.score)
    state.last_time_window_required_score = float(decision.required_score)
    state.last_time_window_approved = bool(decision.approved)
    state.last_time_window_decision = decision.decision
    state.last_time_window_block_reason = decision.block_reason
    state.last_time_window_component_scores = dict(decision.component_scores or {})
    state.last_time_window_metrics = dict(decision.metrics or {})
    state.last_time_window_signal_id = signal_id


def _judge_time_window_flag(
    *, state: RuntimeState, bars_3m, direction: Direction, signal_id: str,
) -> MajorFlagDecision:
    """Records this newly-confirmed flag as the pending T+3 candidate and
    returns a not-yet-confirmed rejection (spec §1: a flag never has order
    authority on its own bar). The REAL time_window_filter.
    evaluate_time_window_entry() check happens one bar later, in
    _resolve_time_window_candidate() below, off bars_3m truncated through
    that later bar. Never called when state.time_window_filter_enabled is
    False; never creates or suppresses the confirmed flag itself, and never
    touches STOP_LOSS/OPPOSITE_SIGNAL/FORCED_LIQUIDATION.

    IMPORTANT: a rejection here must NEVER trigger
    _execute_reversal_exit_only_for_filtered_entry's sell-only liquidation
    (unlike every other filter's rejection) — the held position (if any)
    must stay untouched until _resolve_time_window_candidate resolves the
    candidate at T+3. Callers gate that explicitly on gate_mode ==
    "TIME_WINDOW".
    """
    flag_bar_dt = pd.Timestamp(bars_3m["datetime"].iloc[-1]).to_pydatetime()
    state.time_window_pending_flag_direction = direction
    state.time_window_pending_flag_bar_ts = flag_bar_dt.isoformat()
    decision = MajorFlagDecision(
        approved=False, score=0.0, required_score=0.0,
        decision=config.TW_PENDING_CONFIRMATION,
        reasons=("awaiting T+3 bar re-confirmation (spec §1)",),
        component_scores={}, metrics={"flag_bar_at": flag_bar_dt.isoformat()},
        is_reversal=False, fast_reversal=False, block_reason=config.TW_PENDING_CONFIRMATION,
    )
    _persist_time_window_decision(state, decision, signal_id)
    return decision


def _advance_held_position_risk_management(
    *,
    broker,
    state: RuntimeState,
    market_data: MarketDataService,
    now: datetime,
    quotes: dict,
    pos: PositionSnapshot,
    result: TickResult,
) -> bool:
    """docs §10 priorities 1-2 (15:00 FORCED_LIQUIDATION, then STOP_LOSS —
    the time-window filter's own ladder fully replaces STOP_LOSS for a
    position it opened) for an ALREADY-HELD position — extracted to run
    BEFORE bars_3m/macd_snap are computed in run_once() (2026-08-15 fix).

    None of these three actually need macd_snap (FORCED_LIQUIDATION is a
    plain time-of-day check; the legacy STOP_LOSS and the time-window
    ladder are both evaluated off the traded ETF's OWN completed-bar close
    via _advance_stop_loss_bar, never off macd_snap) — so gating them
    behind macd_snap's NOT_READY early return left a held position with
    literally no risk management on any tick where warm-up wasn't ready
    yet. Narrower for MACD2 specifically than for MU_MACD's own version of
    this same fix (MACD2 backfills real prior-day 1-minute history at
    startup, so NOT_READY is normally just a few seconds right after a
    fresh process boot, not ~90 minutes on every restart like MU_MACD's
    intentional cold-start design) — but the same class of gap, and the
    user explicitly asked for it to be closed the same way.

    docs §10 priorities 3-5 (OPPOSITE_SIGNAL/PROFIT_LOCK/QUICK_PROFIT) all
    genuinely need macd_snap and are UNCHANGED, still evaluated later in
    run_once() once it's available — this function only ever returns True
    (an exit fired) or False (nothing fired, continue normal evaluation);
    it never itself decides to skip the rest of the tick just because a
    position happens to be time-window-managed (that would wrongly starve
    _resolve_time_window_candidate — called later, needs macd_snap — of
    ever running for that position).

    _advance_stop_loss_bar's own per-symbol "last completed bar" tracking
    means calling it twice for the same tick/bar would silently swallow the
    second call's result (see its own docstring) — this function is now
    the ONLY caller for a held position; the equivalent checks that used to
    live later in run_once()'s "Held position" chain were removed, not
    duplicated.
    """
    current_price = quotes.get(pos.symbol)
    if current_price is None:
        # 2026-08-04 fix: STOP_LOSS/Quick-Profit are risk-safety checks on an
        # ALREADY-held position, not a decision to take on new risk — fall
        # back to the last known price for this symbol (even if stale) so
        # the checks below still run off a real, recent price instead of
        # none (2026-08-04 real incident: SOL 인버스 -1.5%+ 손실, 손절 미발동).
        stale_snap = market_data.get_quote(pos.symbol)
        if stale_snap is not None and not stale_snap.error and stale_snap.price > 0:
            current_price = stale_snap.price

    if now.time() >= config.FORCE_LIQUIDATE_AT:
        outcome = order_executor.execute_exit(
            broker=broker, symbol=pos.symbol, quantity=pos.quantity,
            exit_reason=config.EXIT_FORCED_LIQUIDATION, entry_price=pos.avg_price,
            reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
        )
        _apply_exit_outcome(state, outcome)
        result.actions.append(f"FORCED_LIQUIDATION:{pos.symbol}")
        return True

    if (
        state.time_window_filter_enabled and state.time_window_position_active
        and state.position is not None and state.position.symbol == pos.symbol
        and current_price is not None
    ):
        # This position was opened by the time-window filter — its own
        # position-management ladder (§11-14) fully replaces the legacy
        # STOP_LOSS check below for as long as it is held (OPPOSITE_SIGNAL
        # is instead handled by _resolve_time_window_candidate, further
        # down in run_once() once macd_snap is ready).
        completed_bar_close = _advance_stop_loss_bar(state, pos.symbol, current_price, now)
        if completed_bar_close is not None:
            bar_net_return = _net_return_pct(pos.symbol, pos.avg_price, completed_bar_close, pos.quantity)
            pm_decision = time_window_position_manager.evaluate_position(
                session=state.time_window_entry_session or "MORNING",
                net_return_pct=bar_net_return,
                tp1_done=bool(state.time_window_tp1_done),
                peak_net_return=float(state.time_window_peak_net_return or 0.0),
            )
            state.time_window_peak_net_return = pm_decision.peak_net_return
            state.time_window_tp1_done = pm_decision.tp1_done
            if pm_decision.exit_reason is not None:
                sell_fraction = max(0.0, min(1.0, pm_decision.sell_fraction))
                full_exit = sell_fraction >= 1.0
                if full_exit:
                    outcome = order_executor.execute_exit(
                        broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                        exit_reason=pm_decision.exit_reason, entry_price=pos.avg_price,
                        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                    )
                    _apply_exit_outcome(state, outcome)
                    if outcome.final_state == SignalState.EXECUTED:
                        state.time_window_position_active = False
                    result.actions.append(f"{pm_decision.exit_reason}:{pos.symbol}")
                    return True
                sell_qty = min(pos.quantity - 1, max(1, round(pos.quantity * sell_fraction)))
                remaining_qty = pos.quantity - sell_qty
                outcome = order_executor.execute_partial_exit(
                    broker=broker, symbol=pos.symbol, sell_qty=sell_qty, remaining_qty=remaining_qty,
                    exit_reason=pm_decision.exit_reason, entry_price=pos.avg_price,
                    reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                )
                if outcome.final_state == SignalState.EXECUTED:
                    state.position = dataclasses.replace(state.position, quantity=remaining_qty)
                result.actions.append(f"{pm_decision.exit_reason}:{pos.symbol}")
                return True
        return False

    if current_price is not None:
        # Stop Loss is evaluated from the completed 3-minute ETF bar close
        # onward, excluding the bar that contains the entry fill (docs
        # 2026-08-02 Exit Rule: 3-Minute Confirmed Bars) -- NOT off this
        # tick's live/instantaneous quote. risk_exit's own -1.5% threshold
        # (check_stop_loss) is reused unchanged, just fed the completed-bar
        # close instead of the live quote.
        completed_bar_close = _advance_stop_loss_bar(state, pos.symbol, current_price, now)
        if completed_bar_close is not None:
            bar_net_return = _net_return_pct(pos.symbol, pos.avg_price, completed_bar_close, pos.quantity)
            if risk_exit.check_stop_loss(bar_net_return):
                outcome = order_executor.execute_exit(
                    broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                    exit_reason=config.EXIT_STOP_LOSS, entry_price=pos.avg_price,
                    reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                )
                _apply_exit_outcome(state, outcome)
                result.actions.append(f"STOP_LOSS:{pos.symbol}")
                return True

    return False


def _resolve_time_window_candidate(
    *,
    broker,
    market_data: MarketDataService,
    state: RuntimeState,
    now: datetime,
    macd_snap,
    bars_3m,
    df_1m,
    position: Optional[PositionSnapshot],
    result: TickResult,
):
    """Resolves a pending time-window candidate (spec §1's T -> T+3 wait) on
    the completed bar immediately after its own flag bar, via the single
    shared time_window_filter.evaluate_time_window_entry() decision function
    (no duplicated entry-condition logic vs the backtest driver). Returns
    the dispatch outcome if an entry/switch was actually placed this tick,
    else ``None`` (still waiting, expired, or rejected — all safe no-ops).
    Never called when ``state.time_window_filter_enabled`` is False.
    """
    if not state.time_window_filter_enabled or not state.time_window_pending_flag_direction:
        return None
    flag_bar_dt = _parse_iso_dt(state.time_window_pending_flag_bar_ts)
    if flag_bar_dt is None:
        state.time_window_pending_flag_direction = None
        state.time_window_pending_flag_bar_ts = None
        return None
    if macd_snap.bar_dt == flag_bar_dt:
        return None  # still sitting on the flag's own bar T -- wait for T+3

    direction = state.time_window_pending_flag_direction
    signal_id = f"{make_signal_id(flag_bar_dt, direction)}:TW_CONFIRM"
    state.time_window_pending_flag_direction = None
    state.time_window_pending_flag_bar_ts = None
    if signal_id in state.processed_signal_ids:
        return None

    # bars_3m must end EXACTLY one completed bar after flag_bar_dt for
    # evaluate_time_window_entry to accept it (its own T+3 confirmation
    # contract) -- a multi-bar gap (e.g. the Worker was down) means this
    # candidate has expired; drop it rather than confirm off stale bars.
    decision = time_window_filter.evaluate_time_window_entry(
        bars_3m, direction, flag_bar_dt, now,
        position_direction=_position_direction(position),
        morning_entry_count=int(state.time_window_morning_entry_count or 0),
        afternoon_entry_count=int(state.time_window_afternoon_entry_count or 0),
    )
    _persist_time_window_decision(state, decision, signal_id)

    if not decision.approved:
        state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
        dispatch_trace = {
            "signal_id": signal_id, "direction": direction.value, "signal_type": "TIME_WINDOW_CONFIRM",
            "completed_bar_at": macd_snap.bar_dt.isoformat(),
            "order_executor_called": False, "broker_called": False,
            "final_block_reason": decision.block_reason or decision.decision or "",
            "order_result_override": config.FILTERED_OUT,
            "major_fields": _entry_gate_ledger_fields(state, decision, "TIME_WINDOW"),
        }
        outcome = order_executor.ExecutionOutcome(
            signal_id=signal_id, direction=direction,
            target_symbol=order_executor.target_symbol_for_direction(direction),
            final_state=SignalState.BLOCKED, block_reason=decision.block_reason or decision.decision,
        )
        _record_signal_ledger(
            state, macd_snap, direction, "TIME_WINDOW_CONFIRM", signal_id, datetime.now(KST), outcome, dispatch_trace,
        )
        result.actions.append(f"{config.FILTERED_OUT}:{direction.value}")
        return None

    signal_detected_at = datetime.now(KST)
    result.signal_detected_at = signal_detected_at.isoformat()
    signal_type = "REVERSAL" if (position is not None and position.quantity > 0) else "INITIAL"
    outcome = _execute_or_wait(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        direction=direction, signal_id=signal_id, signal_type=signal_type, position=position, result=result,
        signal_detected_at=signal_detected_at,
    )
    result.signal_dispatch_trace["major_fields"] = _entry_gate_ledger_fields(state, decision, "TIME_WINDOW")
    _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, signal_detected_at, outcome, result.signal_dispatch_trace)

    if outcome is not None and outcome.final_state == SignalState.EXECUTED:
        # _apply_switch_outcome is the SAME function every other entry/switch
        # path uses to actually set state.position on a fill (docs: no
        # duplicated position-adoption logic) -- it also registers
        # outcome.signal_id in processed_signal_ids, so this candidate's
        # signal_id is not separately appended here.
        _apply_switch_outcome(state, outcome, direction, now)
        window = decision.metrics.get("window") if decision.metrics else None
        session = time_window_filter.session_for_window(window)
        state.time_window_position_active = True
        state.time_window_entry_session = session
        state.time_window_tp1_done = False
        state.time_window_peak_net_return = 0.0
        state.time_window_initial_quantity = outcome.quantity
        state.last_time_window_entry_at = signal_detected_at.isoformat()
        if session == "MORNING":
            state.time_window_morning_entry_count = int(state.time_window_morning_entry_count or 0) + 1
            state.time_window_entry_session_seq = state.time_window_morning_entry_count
        elif session == "AFTERNOON":
            state.time_window_afternoon_entry_count = int(state.time_window_afternoon_entry_count or 0) + 1
            state.time_window_entry_session_seq = state.time_window_afternoon_entry_count
    return outcome


def _judge_entry_gate(
    *,
    state: RuntimeState,
    bars_3m,
    df_1m=None,
    direction: Direction,
    position: Optional[PositionSnapshot],
    now: datetime,
    signal_id: str,
) -> tuple[Optional[MajorFlagDecision], str]:
    """Single order-authority gate dispatcher for a confirmed crossover.

    ``time_window_filter_enabled`` takes TOP PRIORITY (2026-08-15 사용자
    요청: the newest, most complete redesign supersedes the simpler
    entry-only gates when a user opts into it), then ``sideways_filter_enabled``,
    then ``major_filter_enabled``, then ``trend_persistence_filter_enabled``,
    then ``single_entry_filter_enabled`` — the five optional filters are
    never more than one active for the same signal (2026-08-04 추세전환장
    toggle spec: "위 로직 우선으로 들어가는 거야", extended 2026-08-07 to Trend
    Persistence, 2026-08-08 to Single-Entry, 2026-08-15 to Time-Window).
    Returns ``(None, "NONE")`` when no toggle is on — legacy behavior (every
    confirmed flag has order authority) is completely unchanged.
    """
    if state.time_window_filter_enabled:
        return _judge_time_window_flag(state=state, bars_3m=bars_3m, direction=direction, signal_id=signal_id), "TIME_WINDOW"
    if state.sideways_filter_enabled:
        return _judge_sideways_flag(state=state, bars_3m=bars_3m, df_1m=df_1m, direction=direction, now=now, signal_id=signal_id), "SIDEWAYS"
    if state.major_filter_enabled:
        return _judge_major_flag(
            state=state, bars_3m=bars_3m, direction=direction, position=position, now=now, signal_id=signal_id,
        ), "MAJOR"
    if state.trend_persistence_filter_enabled:
        return _judge_trend_persistence_flag(state=state, bars_3m=bars_3m, df_1m=df_1m, direction=direction, now=now, signal_id=signal_id), "TREND_PERSISTENCE"
    if state.single_entry_filter_enabled:
        return _judge_single_entry_flag(state=state, bars_3m=bars_3m, df_1m=df_1m, direction=direction, now=now, signal_id=signal_id), "SINGLE_ENTRY"
    return None, "NONE"


_MAJOR_METRIC_LEDGER_KEYS = (
    "hist_impulse_atr", "breakout", "price_impulse_atr", "body_atr", "volume_ratio",
    "ema10_ok", "ema20_or_vwap_ok", "recent_range_ratio", "ema_spread_ratio",
)


def _major_ledger_fields(state: RuntimeState, decision: Optional[MajorFlagDecision] = None) -> dict[str, Any]:
    """major_* ledger columns: always the toggle/daily-budget context, plus the
    per-signal decision detail whenever the filter actually judged this signal."""
    row: dict[str, Any] = {
        "major_filter_enabled": bool(state.major_filter_enabled),
        "major_filter_version": state.major_filter_version or config.MAJOR_FILTER_VERSION,
        "major_score": "",
        "major_required_score": "",
        "major_approved": "",
        "major_decision": "",
        "major_block_reason": "",
        "major_is_reversal": "",
        "major_fast_reversal": "",
        "major_component_scores": "",
        "daily_major_entry_count": int(state.daily_major_entry_count or 0),
        "last_major_entry_at": state.last_major_entry_at or "",
    }
    for key in _MAJOR_METRIC_LEDGER_KEYS:
        row[key] = ""
    if decision is None:
        return row
    row.update({
        "major_score": float(decision.score),
        "major_required_score": float(decision.required_score),
        "major_approved": bool(decision.approved),
        "major_decision": decision.decision or "",
        "major_block_reason": decision.block_reason or "",
        "major_is_reversal": bool(decision.is_reversal),
        "major_fast_reversal": bool(decision.fast_reversal),
        "major_component_scores": json.dumps(dict(decision.component_scores or {}), sort_keys=True),
    })
    metrics = dict(decision.metrics or {})
    for key in _MAJOR_METRIC_LEDGER_KEYS:
        value = metrics.get(key)
        row[key] = "" if value is None else value
    return row


def _sideways_ledger_fields(state: RuntimeState, decision: Optional[MajorFlagDecision] = None) -> dict[str, Any]:
    """sideways_* ledger columns — mirrors ``_major_ledger_fields`` exactly,
    for the separate 추세전환장 toggle. Shares the same generic
    ``_MAJOR_METRIC_LEDGER_KEYS`` metric columns (hist_impulse_atr, body_atr,
    volume_ratio, ...) since they come from the identical
    major_flag_filter.compute_component_scores computation either way —
    ``_entry_gate_ledger_fields`` decides which side's values actually land
    in those shared columns for a given row."""
    row: dict[str, Any] = {
        "sideways_filter_enabled": bool(state.sideways_filter_enabled),
        "sideways_filter_version": state.sideways_filter_version or config.SIDEWAYS_FILTER_VERSION,
        "sideways_score": "",
        "sideways_required_score": "",
        "sideways_approved": "",
        "sideways_decision": "",
        "sideways_block_reason": "",
        "sideways_component_scores": "",
        "daily_sideways_entry_count": int(state.daily_sideways_entry_count or 0),
        "last_sideways_entry_at": state.last_sideways_entry_at or "",
    }
    for key in _MAJOR_METRIC_LEDGER_KEYS:
        row[key] = ""
    if decision is None:
        return row
    row.update({
        "sideways_score": float(decision.score),
        "sideways_required_score": float(decision.required_score),
        "sideways_approved": bool(decision.approved),
        "sideways_decision": decision.decision or "",
        "sideways_block_reason": decision.block_reason or "",
        "sideways_component_scores": json.dumps(dict(decision.component_scores or {}), sort_keys=True),
    })
    metrics = dict(decision.metrics or {})
    for key in _MAJOR_METRIC_LEDGER_KEYS:
        value = metrics.get(key)
        row[key] = "" if value is None else value
    return row


_TREND_PERSISTENCE_METRIC_LEDGER_KEYS = (
    "ema5", "ema10", "ema20",
    "minutes_above_vwap", "minutes_below_vwap",
    "higher_high_count_last3", "higher_low_count_last3",
    "lower_high_count_last3", "lower_low_count_last3",
)


def _trend_persistence_ledger_fields(state: RuntimeState, decision: Optional[MajorFlagDecision] = None) -> dict[str, Any]:
    """trend_persistence_* ledger columns. Unlike major/sideways, this gate's
    metrics (VWAP dwell/EMA stack/HH-LL structure) are its own dedicated
    columns — never shared with ``_MAJOR_METRIC_LEDGER_KEYS``."""
    row: dict[str, Any] = {
        "trend_persistence_filter_enabled": bool(state.trend_persistence_filter_enabled),
        "trend_persistence_filter_version": state.trend_persistence_filter_version or config.TREND_PERSISTENCE_FILTER_VERSION,
        "trend_persistence_score": "",
        "trend_persistence_required_score": "",
        "trend_persistence_approved": "",
        "trend_persistence_decision": "",
        "trend_persistence_block_reason": "",
        "daily_trend_persistence_entry_count": int(state.daily_trend_persistence_entry_count or 0),
        "last_trend_persistence_entry_at": state.last_trend_persistence_entry_at or "",
    }
    for key in _TREND_PERSISTENCE_METRIC_LEDGER_KEYS:
        row[f"trend_persistence_{key}"] = ""
    if decision is None:
        return row
    row.update({
        "trend_persistence_score": float(decision.score),
        "trend_persistence_required_score": float(decision.required_score),
        "trend_persistence_approved": bool(decision.approved),
        "trend_persistence_decision": decision.decision or "",
        "trend_persistence_block_reason": decision.block_reason or "",
    })
    metrics = dict(decision.metrics or {})
    for key in _TREND_PERSISTENCE_METRIC_LEDGER_KEYS:
        value = metrics.get(key)
        row[f"trend_persistence_{key}"] = "" if value is None else value
    return row


def _single_entry_ledger_fields(state: RuntimeState, decision: Optional[MajorFlagDecision] = None) -> dict[str, Any]:
    """single_entry_* ledger columns — v3: score/flag_seq/near_zero_blue
    diagnostics alongside the daily fill count vs
    config.SINGLE_ENTRY_MAX_DAILY_ENTRIES."""
    row: dict[str, Any] = {
        "single_entry_filter_enabled": bool(state.single_entry_filter_enabled),
        "single_entry_filter_version": state.single_entry_filter_version or config.SINGLE_ENTRY_FILTER_VERSION,
        "single_entry_approved": "",
        "single_entry_decision": "",
        "single_entry_block_reason": "",
        "daily_single_entry_count": int(state.daily_single_entry_count or 0),
        "last_single_entry_at": state.last_single_entry_at or "",
        "single_entry_score": "",
        "single_entry_flag_seq": "",
        "single_entry_near_zero_blue": "",
    }
    if decision is None:
        return row
    row.update({
        "single_entry_approved": bool(decision.approved),
        "single_entry_decision": decision.decision or "",
        "single_entry_block_reason": decision.block_reason or "",
        "single_entry_score": decision.score,
        "single_entry_flag_seq": decision.metrics.get("flag_seq", ""),
        "single_entry_near_zero_blue": decision.metrics.get("near_zero_blue", ""),
    })
    return row


def _time_window_ledger_fields(state: RuntimeState, decision: Optional[MajorFlagDecision] = None) -> dict[str, Any]:
    """time_window_* ledger columns — mirrors the other filters' _*_ledger_
    fields pattern. metrics carries the two-bar gap_flag/gap_now/window/
    session values computed by time_window_filter.evaluate_time_window_entry
    (or the bar-T "pending confirmation" placeholder from
    _judge_time_window_flag)."""
    row: dict[str, Any] = {
        "time_window_filter_enabled": bool(state.time_window_filter_enabled),
        "time_window_filter_version": state.time_window_filter_version or config.TIME_WINDOW_FILTER_VERSION,
        "time_window_score": "",
        "time_window_required_score": "",
        "time_window_approved": "",
        "time_window_decision": "",
        "time_window_block_reason": "",
        "time_window_window": "",
        "time_window_session": "",
        "time_window_flag_bar_at": "",
        "time_window_confirm_bar_at": "",
        "time_window_gap_flag": "",
        "time_window_gap_now": "",
        "time_window_quality_score": "",
        "time_window_morning_entry_count": int(state.time_window_morning_entry_count or 0),
        "time_window_afternoon_entry_count": int(state.time_window_afternoon_entry_count or 0),
    }
    if decision is None:
        return row
    metrics = dict(decision.metrics or {})
    row.update({
        "time_window_score": decision.score,
        "time_window_required_score": decision.required_score,
        "time_window_approved": bool(decision.approved),
        "time_window_decision": decision.decision or "",
        "time_window_block_reason": decision.block_reason or "",
        "time_window_window": metrics.get("window") or "",
        "time_window_session": metrics.get("session") or "",
        "time_window_flag_bar_at": metrics.get("flag_bar_at") or "",
        "time_window_confirm_bar_at": metrics.get("confirm_bar_at") or "",
        "time_window_gap_flag": metrics.get("gap_flag") if metrics.get("gap_flag") is not None else "",
        "time_window_gap_now": metrics.get("gap_now") if metrics.get("gap_now") is not None else "",
        "time_window_quality_score": metrics.get("quality_score") if metrics.get("quality_score") is not None else "",
    })
    return row


def _entry_gate_ledger_fields(
    state: RuntimeState, decision: Optional[MajorFlagDecision], mode: str,
) -> dict[str, Any]:
    """Merge major_*, sideways_*, trend_persistence_*, single_entry_*, and
    time_window_* ledger columns for one signal row.

    All five column families are always present (never omitted), so every
    ledger row shows the current state of all toggles — but the shared
    generic metric columns (``_MAJOR_METRIC_LEDGER_KEYS``) are populated
    only by whichever of major/sideways actually judged this signal
    (``mode``), never blanked out afterward by the inactive side.
    """
    major_fields = _major_ledger_fields(state, decision if mode == "MAJOR" else None)
    sideways_fields = _sideways_ledger_fields(state, decision if mode == "SIDEWAYS" else None)
    trend_persistence_fields = _trend_persistence_ledger_fields(state, decision if mode == "TREND_PERSISTENCE" else None)
    single_entry_fields = _single_entry_ledger_fields(state, decision if mode == "SINGLE_ENTRY" else None)
    time_window_fields = _time_window_ledger_fields(state, decision if mode == "TIME_WINDOW" else None)
    merged = dict(major_fields)
    for key, value in sideways_fields.items():
        if key in _MAJOR_METRIC_LEDGER_KEYS:
            if mode == "SIDEWAYS":
                merged[key] = value
            continue
        merged[key] = value
    merged.update(trend_persistence_fields)
    merged.update(single_entry_fields)
    merged.update(time_window_fields)
    return merged


def _record_major_filtered_signal(
    *,
    state: RuntimeState,
    macd_snap,
    direction: Direction,
    signal_type: str,
    signal_id: str,
    decision: MajorFlagDecision,
    detected_at: datetime,
    result: TickResult,
    gate_mode: str = "MAJOR",
):
    """Entry-gate rejection (MAJOR_FLAG or 추세전환장, per ``gate_mode``):
    ledger only (order_result=FILTERED_OUT), never an order_executor/broker
    call. The signal_id is consumed so the same flag is not re-judged/
    re-dispatched on a later tick."""
    block_reason = decision.block_reason or decision.decision or config.FILTERED_OUT
    state.order_block_reason = block_reason
    if signal_id not in state.processed_signal_ids:
        state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
    result.signal_dispatch_trace = {
        "signal_id": signal_id,
        "direction": direction.value,
        "signal_type": signal_type,
        "completed_bar_at": macd_snap.bar_dt.isoformat(),
        "order_executor_called": False,
        "broker_called": False,
        "final_block_reason": block_reason,
        "order_result_override": config.FILTERED_OUT,
        "major_fields": _entry_gate_ledger_fields(state, decision, gate_mode),
    }
    outcome = order_executor.ExecutionOutcome(
        signal_id=signal_id,
        direction=direction,
        target_symbol=order_executor.target_symbol_for_direction(direction),
        final_state=SignalState.BLOCKED,
        block_reason=block_reason,
        timestamps={MAJOR_FILTERED_TS_KEY: detected_at.isoformat()},
    )
    _record_signal_ledger(
        state, macd_snap, direction, signal_type, signal_id, detected_at, outcome,
        result.signal_dispatch_trace,
    )
    return outcome


def _is_major_filtered(outcome) -> bool:
    return bool(outcome is not None and (outcome.timestamps or {}).get(MAJOR_FILTERED_TS_KEY))


def _execute_reversal_exit_only_for_filtered_entry(
    *,
    broker,
    state: RuntimeState,
    macd_snap,
    direction: Direction,
    position: PositionSnapshot,
    decision: MajorFlagDecision,
    result: TickResult,
    gate_mode: str = "MAJOR",
    signal_id_override: Optional[str] = None,
):
    """Opposite confirmed flag with the active entry gate (MAJOR_FLAG or
    추세전환장) rejected: exit the old ETF, but do not enter the opposite ETF."""
    signal_id = signal_id_override or make_signal_id(macd_snap.bar_dt, direction)
    if signal_id in state.processed_signal_ids:
        state.order_block_reason = order_executor.BLOCK_DUPLICATE_SIGNAL
        return None
    signal_detected_at = datetime.now(KST)
    result.signal_detected_at = signal_detected_at.isoformat()
    block_reason = decision.block_reason or decision.decision or config.FILTERED_OUT
    outcome = order_executor.execute_exit(
        broker=broker,
        symbol=position.symbol,
        quantity=position.quantity,
        exit_reason=config.EXIT_OPPOSITE_SIGNAL,
        entry_price=position.avg_price,
        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES,
        reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
    )
    result.order_requested_at = outcome.timestamps.get("sell_requested_at")
    broker_result = outcome.sell_result
    result.signal_dispatch_trace = {
        "signal_id": signal_id,
        "direction": direction.value,
        "signal_type": "REVERSAL",
        "completed_bar_at": macd_snap.bar_dt.isoformat(),
        "order_executor_called": True,
        "order_requested_at": result.order_requested_at or "",
        "broker_called": bool(broker_result is not None),
        "broker_order_id": broker_result.order_id if broker_result else "",
        "broker_raw": dict(broker_result.raw or {}) if broker_result else {},
        "final_block_reason": block_reason,
        "order_result_override": (
            "SELL_EXECUTED_ENTRY_FILTERED"
            if outcome.final_state == SignalState.EXECUTED
            else outcome.final_state.value
        ),
        "major_fields": _entry_gate_ledger_fields(state, decision, gate_mode),
        "failure_stage": outcome.order_failure_stage or "",
    }
    outcome.signal_id = signal_id
    outcome.direction = direction
    outcome.block_reason = block_reason
    _record_signal_ledger(
        state, macd_snap, direction, "REVERSAL", signal_id, signal_detected_at,
        outcome, result.signal_dispatch_trace,
    )
    if signal_id not in state.processed_signal_ids:
        state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
    return outcome


def _dispatch_confirmed_signal(
    *,
    broker,
    market_data: MarketDataService,
    state: RuntimeState,
    now: datetime,
    macd_snap,
    direction: Direction,
    signal_type: str,
    position: Optional[PositionSnapshot],
    result: TickResult,
    signal_id_override: Optional[str] = None,
    major_decision_override: Optional[MajorFlagDecision] = None,
    major_gate_mode_override: str = "MAJOR",
    bars_3m=None,
    df_1m=None,
):
    signal_id = signal_id_override or make_signal_id(macd_snap.bar_dt, direction)
    if signal_id in state.processed_signal_ids:
        state.order_block_reason = order_executor.BLOCK_DUPLICATE_SIGNAL
        return None
    if state.pending_signal and state.pending_signal.get("signal_id") == signal_id:
        return None

    state.current_episode_direction = direction
    signal_detected_at = datetime.now(KST)
    result.signal_detected_at = signal_detected_at.isoformat()

    # Optional entry gate (MAJOR_FLAG or 추세전환장, sideways takes priority
    # — see _judge_entry_gate) — the ONLY filter judgment point, and only for
    # a brand-new confirmed signal (pending retries already cleared this gate
    # when they were first approved).
    decision: Optional[MajorFlagDecision] = major_decision_override
    gate_mode = major_gate_mode_override
    # REVERSAL is judged by the held-position branch: weak opposite flags
    # must still liquidate the old ETF but must not enter the opposite ETF.
    if signal_type != "REVERSAL":
        decision, gate_mode = _judge_entry_gate(
            state=state, bars_3m=bars_3m, df_1m=df_1m, direction=direction, position=position,
            now=now, signal_id=signal_id,
        )
        if decision is not None and not decision.approved:
            return _record_major_filtered_signal(
                state=state, macd_snap=macd_snap, direction=direction, signal_type=signal_type,
                signal_id=signal_id, decision=decision, detected_at=signal_detected_at, result=result,
                gate_mode=gate_mode,
            )

    outcome = _execute_or_wait(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        direction=direction, signal_id=signal_id, signal_type=signal_type, position=position, result=result,
        signal_detected_at=signal_detected_at,
    )
    result.signal_dispatch_trace["major_fields"] = _entry_gate_ledger_fields(state, decision, gate_mode)
    _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, signal_detected_at, outcome, result.signal_dispatch_trace)
    return outcome


def _record_confirmed_blocked_signal(
    *,
    state: RuntimeState,
    macd_snap,
    direction: Direction,
    signal_type: str,
    reason: str,
    result: TickResult,
) -> None:
    signal_id = make_signal_id(macd_snap.bar_dt, direction)
    if signal_id in state.processed_signal_ids:
        return
    state.order_block_reason = reason
    signal_detected_at = datetime.now(KST)
    result.signal_detected_at = signal_detected_at.isoformat()
    result.signal_dispatch_trace = {
        "signal_id": signal_id,
        "direction": direction.value,
        "signal_type": signal_type,
        "completed_bar_at": macd_snap.bar_dt.isoformat(),
        "order_executor_called": False,
        "broker_called": False,
        "final_block_reason": reason,
    }
    outcome = order_executor.ExecutionOutcome(
        signal_id=signal_id,
        direction=direction,
        target_symbol=order_executor.target_symbol_for_direction(direction),
        final_state=SignalState.BLOCKED,
        block_reason=reason,
    )
    _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, signal_detected_at, outcome, result.signal_dispatch_trace)


def _confirmed_signal_order_gate_block_reason(state: RuntimeState, now: datetime) -> str:
    if now.time() < config.SESSION_OPEN:
        return "BEFORE_SESSION_OPEN"
    if now.time() >= config.NEW_ENTRY_CUTOFF:
        return "NEW_ENTRY_CUTOFF"
    return state.quote_history_mismatch_reason or "ENTRY_WINDOW_CLOSED"


def _record_scheduled_entry_signal(state: RuntimeState, direction: Direction, signal_id: str, now: datetime, outcome) -> None:
    """Signal-ledger row for the 09:03 예약 매수 (2026-08-06) — mirrors
    service.py's _record_manual_entry_signal so a scheduled auto-buy shows up
    in the same audit trail as manual/MACD-confirmed signals, tagged
    signal_type=SCHEDULED_ENTRY_0903 (no macd_snap backs it, same convention
    as MANUAL_ENTRY). Execution-ledger recording already happens inside
    order_executor.execute_signal itself (_record_leg)."""
    block_reason = outcome.block_reason or ""
    row = {
        "trading_date": now.strftime("%Y%m%d"),
        "completed_bar_at": now.strftime("%H%M%S"),
        "signal_id": signal_id,
        "signal_type": "SCHEDULED_ENTRY_0903",
        "direction": direction.value,
        "detected_at": now.isoformat(),
        "order_requested_at": outcome.timestamps.get("buy_requested_at", ""),
        "order_result": outcome.final_state.value,
        "block_reason": block_reason,
        "signal_bar_at": now.isoformat(),
        "signal_confirmed_at": now.isoformat(),
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": "SCHEDULED_ENTRY_0903_UI_BUTTON",
        "worker_code_sha": git_sha(),
        "worker_instance_id": state.worker_instance_id or "",
        "session_started_at": state.session_started_at or "",
        "confirmed_direction": direction.value,
        "executor_called": True,
        "broker_called": bool(outcome.broker_called),
        "broker_order_id": outcome.buy_result.order_id if outcome.buy_result else "",
        "order_price": outcome.order_price,
        "order_type": outcome.order_type or "",
        "requested_qty": outcome.final_qty,
        "final_qty": outcome.quantity,
        "filled_qty": outcome.filled_qty,
        "fill_poll_result": outcome.fill_poll_result or "",
        "balance_qty": outcome.balance_qty,
        "failure_stage": outcome.order_failure_stage or "",
        "final_result": f"{outcome.final_state.value}:{block_reason}" if block_reason else outcome.final_state.value,
    }
    ledger.append_signal(row)


def _scheduled_entry_should_fire(state: RuntimeState, now: datetime) -> bool:
    """09:03 예약 매수(2026-08-06) 발동 여부 — 오늘 이미 체결/포기됐으면
    (scheduled_entry_executed_at) 다시 발동하지 않고(하루 1회), 예약된 게
    없어도 당연히 발동하지 않는다. 발동 시각 이후 SCHEDULED_ENTRY_FIRE_
    WINDOW_SEC 안에서만 유효 -- 그 창을 넘기면 오늘은 놓친 것으로 조용히
    끝난다(사용자가 다음날 다시 눌러야 함)."""
    if state.scheduled_entry_armed_direction is None or state.scheduled_entry_executed_at:
        return False
    if now.time() < config.SCHEDULED_ENTRY_TIME:
        return False
    fire_deadline = datetime.combine(now.date(), config.SCHEDULED_ENTRY_TIME, tzinfo=KST) + timedelta(
        seconds=config.SCHEDULED_ENTRY_FIRE_WINDOW_SEC,
    )
    return now <= fire_deadline


def _scheduled_entry_protection_active(state: RuntimeState, now: datetime) -> bool:
    """2026-08-07 (사용자 요청): True only while the CURRENTLY held position
    came from the scheduled entry AND ``now`` is still before
    config.SCHEDULED_ENTRY_PROTECTION_UNTIL (09:10 KST) -- see
    scheduled_entry_protected's own docstring for what this gates
    (OPPOSITE_SIGNAL sell/switch only; every other exit is unaffected)."""
    if not state.scheduled_entry_protected:
        return False
    return now.astimezone(KST).time() < config.SCHEDULED_ENTRY_PROTECTION_UNTIL


def _execute_scheduled_entry(*, broker, market_data: MarketDataService, state: RuntimeState, now: datetime):
    """Fires the armed 09:03 예약 매수 -- reuses order_executor.execute_signal
    exactly like service.py's manual_entry (no separate buy logic), then
    records it in both the execution ledger (already inside execute_signal)
    and a SCHEDULED_ENTRY_0903 signal-ledger row. Once fired (position taken),
    it is managed by the same held-position priority chain as any other entry
    (손절/프로핏락/퀵프로핏/강제청산 identical) EXCEPT a confirmed OPPOSITE flag
    is protected (caught/logged but not acted on) until
    config.SCHEDULED_ENTRY_PROTECTION_UNTIL (2026-08-07 사용자 요청 -- 개장
    직후 MACD가 아직 불안정해 진짜 반전이 아닌 노이즈성 반대 플래그로 방금 넣은
    포지션이 바로 뒤집히는 것을 막기 위함); see scheduled_entry_protected.

    Marks scheduled_entry_executed_at (stopping further attempts today) on a
    real EXECUTED fill, OR on a non-transient block reason (the executor
    itself structurally refused the order) -- but NOT on a merely transient
    one (state's own TEMPORARY_BLOCK_REASONS, e.g. a stale/missing quote),
    which instead retries on the next tick still inside the same fire window.
    """
    direction = state.scheduled_entry_armed_direction
    target_symbol = order_executor.target_symbol_for_direction(direction)
    quote_snap = market_data.get_quote(target_symbol)
    if quote_snap is None or quote_snap.error or quote_snap.price <= 0:
        state.order_block_reason = "SCHEDULED_ENTRY_QUOTE_UNAVAILABLE"
        return None

    signal_id = f"SCHEDULED_0903_{direction.value}_{now.strftime('%Y%m%d')}"
    outcome = order_executor.execute_signal(
        broker=broker, direction=direction, signal_id=signal_id,
        quotes={target_symbol: quote_snap.price}, position=None, budget=state.budget,
        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
    )
    _record_scheduled_entry_signal(state, direction, signal_id, now, outcome)

    if outcome.final_state == SignalState.EXECUTED:
        _apply_switch_outcome(state, outcome, direction, now)
        state.scheduled_entry_executed_at = now.isoformat()
        state.scheduled_entry_last_result = "EXECUTED"
        state.scheduled_entry_protected = True
        return outcome

    state.order_block_reason = outcome.block_reason
    state.scheduled_entry_last_result = f"{outcome.final_state.value}:{outcome.block_reason or ''}"
    if outcome.block_reason not in TEMPORARY_BLOCK_REASONS:
        state.scheduled_entry_executed_at = now.isoformat()
    return None


def run_once(
    *,
    broker,
    market_data: MarketDataService,
    state: RuntimeState,
    now: Optional[datetime] = None,
) -> TickResult:
    """One Worker cycle — no pending timers, no queues: same-tick signal->order."""
    now = now or datetime.now(KST)
    result = TickResult()
    tick_started = time.monotonic()
    result.timing["state_load"] = 0.0

    if not state.auto_trade_on:
        result.skipped = "auto_trade_off"
        result.timing["total"] = time.monotonic() - tick_started
        return result

    _apply_day_rollover(state, now)
    if state.strategy_version != config.STRATEGY_VERSION or state.signal_rule != config.SIGNAL_RULE:
        state.strategy_name = config.STRATEGY_NAME
        state.strategy_version = config.STRATEGY_VERSION
        state.signal_rule = config.SIGNAL_RULE
        state.pending_signal = None
        state.last_detected_direction = None
        state.last_evaluated_bar_ts = None
        state.last_confirmed_bar_ts = None

    t0 = time.monotonic()
    reconcile = reconcile_position_state(broker, state, now)
    result.timing["position_reconcile"] = time.monotonic() - t0
    if reconcile in (
        POSITION_DATA_ERROR, POSITION_MISMATCH, RECOVERED_FROM_BROKER, RECOVERED_TO_FLAT,
        RECOVERED_QTY_MISMATCH,
    ):
        # 2026-08-07 real incident: a same-symbol qty mismatch (e.g. a
        # partial-fill entry whose broker-side qty later settled to a
        # different number than what was recorded at fill time) used to
        # report as POSITION_MISMATCH forever -- nothing ever corrected it,
        # so this early-return fired on EVERY tick from then on, silently
        # skipping STOP_LOSS/OPPOSITE_SIGNAL/PROFIT_LOCK for the held
        # position indefinitely (no forced liquidation, no dispatch, nothing
        # -- the position just sat unmonitored until a human manually sold
        # it). RECOVERED_QTY_MISMATCH (see reconcile_position_state) already
        # adopted the broker's true qty into state.position this call, so
        # skipping only THIS tick (same as RECOVERED_FROM_BROKER/
        # RECOVERED_TO_FLAT already do) is safe -- the very next tick sees
        # MATCH_POSITION and resumes full evaluation with the corrected,
        # sellable quantity.
        state.order_block_reason = reconcile
        result.skipped = reconcile
        result.timing["total"] = time.monotonic() - tick_started
        return result

    t0 = time.monotonic()
    quotes = _fresh_quote_prices(market_data, (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL))
    result.timing["quote_cache_read"] = time.monotonic() - t0

    # 2026-08-15 fix: FORCED_LIQUIDATION/STOP_LOSS/the time-window filter's
    # own ladder for an already-held position must never depend on
    # macd_snap readiness — checked here, ahead of the NOT_READY warm-up
    # gate below, so a real held position is never left unmonitored during
    # any tick where warm-up isn't ready yet (see
    # _advance_held_position_risk_management's own docstring for why, and
    # for the analogous same-day MU_MACD fix this mirrors).
    _held_pos = state.position
    if _held_pos is not None and _held_pos.quantity > 0:
        if _advance_held_position_risk_management(
            broker=broker, state=state, market_data=market_data, now=now,
            quotes=quotes, pos=_held_pos, result=result,
        ):
            result.timing["total"] = time.monotonic() - tick_started
            return result

    # Worker never calls KIS itself and never triggers the incremental merge —
    # MarketDataService's own history-updater thread refreshes this cache in
    # the background (docs §8/§11); this only reads the cached snapshot.
    t0 = time.monotonic()
    df_1m = market_data.get_history_df()
    result.timing["history_cache_read"] = time.monotonic() - t0
    t0 = time.monotonic()
    bars_3m = resample_completed_3m(df_1m, now=now)
    # docs §4: a completed 3m bar only ever counts as "confirmed" when its own
    # 3 constituent 1-minute bars are ALL present — an API error/dropped page
    # must never silently masquerade as a real bar. Any bin missing one or
    # more of its minutes is dropped here (never filled/interpolated), which
    # also blocks that specific bar's crossover/MAJOR-filter/order evaluation
    # (HISTORY_GAP) until the gap is backfilled by a later incremental merge.
    bars_3m, _history_gap_bar_starts = filter_complete_3m_bars(bars_3m, df_1m)
    if _history_gap_bar_starts:
        state.order_block_reason = "HISTORY_GAP"
    macd_snap = calculate_macd(bars_3m)
    result.timing["macd_calculation"] = time.monotonic() - t0
    if macd_snap is None:
        state.warmup_ready = False
        result.skipped = "NOT_READY"
        result.timing["total"] = time.monotonic() - tick_started
        return result
    state.warmup_ready = True
    state.primary_previous_diff = macd_snap.previous_diff
    state.primary_current_diff = macd_snap.current_diff
    state.primary_relation = macd_snap.relation or _relation_from_diff(macd_snap.current_diff)
    state.signed_b_shadow_direction = signed_b_condition(macd_snap)
    state.signed_b_shadow_hist_last3 = macd_snap.hist_last3

    # ── Shadow/candidate only: forming-bar provisional + Signed-B NEVER carry
    # order/stat/last_direction authority (docs 2026-07-27 KIS-parity fix) ──
    raw_provisional_snap = None
    raw_provisional_condition = Direction.HOLD
    watch_quote_ready = _quote_valid_for_provisional(market_data, config.WATCH_SYMBOL)
    watch_price = quotes.get(config.WATCH_SYMBOL)
    _update_forming_input_diag(
        state, now=now, df_1m=df_1m, watch_price=watch_price, market_data=market_data,
    )
    _update_history_freshness_diag(state, df_1m=df_1m, macd_snap=macd_snap, watch_price=watch_price, now=now)
    if watch_quote_ready and watch_price is not None:
        primary_result = evaluate_primary_forming_crossover(
            bars_3m, df_1m, now=now, current_price=watch_price,
            previous_direction=state.last_detected_direction,
        )
        raw_provisional_snap = primary_result.snapshot
        raw_provisional_condition = primary_result.direction

    if raw_provisional_snap is not None:
        _update_provisional_diagnostics(state, raw_provisional_snap)
    else:
        state.provisional_macd = None
        state.provisional_signal = None
        state.provisional_diff = None

    today_str = now.astimezone(KST).strftime("%Y%m%d")
    today_has_completed_bar = bool(
        not bars_3m.empty and (bars_3m["datetime"].dt.strftime("%Y%m%d") == today_str).any()
    )
    provisional_ready = today_has_completed_bar and bool(state.last_confirmed_bar_ts)
    candidate_snap, candidate_condition = _advance_provisional_candidate(
        state, raw_provisional_snap, raw_provisional_condition, now,
        today_has_completed_bar=provisional_ready,
    )
    if candidate_snap is not None:
        candidate_signal_id = make_provisional_signal_id(candidate_snap.bar_dt, candidate_condition)
        _update_provisional_shadow_flag(state, candidate_snap, candidate_condition, candidate_signal_id)
    else:
        state.provisional_flag = None
        state.provisional_signal_id = None

    # ── Primary (order-authoritative): completed 3m bars ONLY — same
    # confirmed MACD(12,26,9) KIS itself charts a flag on (docs 2026-07-27
    # KIS-parity fix). Evaluated exactly once per new completed-bar
    # timestamp; the first completed bar this state has ever evaluated (or
    # the first on a new calendar date) sets baseline only.
    confirmed_direction = _advance_confirmed_primary(state, macd_snap)

    bar_ts_str = macd_snap.bar_dt.isoformat()

    before_open = now.time() < config.SESSION_OPEN
    entry_cutoff_passed = now.time() >= config.NEW_ENTRY_CUTOFF
    entry_window_open = (not before_open) and (not entry_cutoff_passed) and not state.quote_history_mismatch_reason
    t0 = time.monotonic()
    # A pending (blocked-on-retry) signal is always confirmed-bar-sourced now
    # — its own direction stays "active" as long as macd_snap (the completed
    # bar) hasn't rolled over to a new one yet.
    _expire_pending_if_needed(state, macd_snap, now)
    result.timing["signal_evaluation"] = time.monotonic() - t0

    pos = state.position

    # ── Held position: priority chain (docs §10) ───────────────────────
    # Priorities 1-2 (FORCED_LIQUIDATION, STOP_LOSS/time-window ladder) were
    # already evaluated earlier in this function, before macd_snap was even
    # computed (see _advance_held_position_risk_management above) — if
    # either had fired, run_once() would already have returned. Only
    # priorities 3-5 (OPPOSITE_SIGNAL/PROFIT_LOCK/QUICK_PROFIT), which
    # genuinely need macd_snap, remain below.
    if pos is not None and pos.quantity > 0:
        scheduled_protected = _scheduled_entry_protection_active(state, now)
        current_price = quotes.get(pos.symbol)
        if current_price is None:
            # See _advance_held_position_risk_management's own stale-quote
            # fallback comment (2026-08-04 fix) — same reasoning, just
            # re-fetched here since that call's own current_price is local
            # to it, for PROFIT_LOCK/QUICK_PROFIT's use below.
            stale_snap = market_data.get_quote(pos.symbol)
            if stale_snap is not None and not stale_snap.error and stale_snap.price > 0:
                current_price = stale_snap.price

        # Time-window filter: resolve any pending T+3 candidate (may switch
        # this held position — sell current, buy the new direction — or
        # simply clear a rejected/expired candidate). Only ever acts when
        # state.time_window_filter_enabled is True; a no-op otherwise.
        tw_resolve_outcome = _resolve_time_window_candidate(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
            bars_3m=bars_3m, df_1m=df_1m, position=pos, result=result,
        )
        if tw_resolve_outcome is not None and tw_resolve_outcome.final_state == SignalState.EXECUTED:
            result.actions.append(f"TIME_WINDOW_SWITCH:{tw_resolve_outcome.target_symbol}")
            return result

        if state.time_window_filter_enabled and state.time_window_position_active:
            # This position is (still) managed by the time-window filter's
            # own ladder, fully replacing PROFIT_LOCK/QUICK_PROFIT below for
            # as long as it is held — its STOP_LOSS/TP1/TP2 checks already
            # ran earlier this tick via _advance_held_position_risk_
            # management; nothing else in this priority chain should touch
            # this position (OPPOSITE_SIGNAL is instead handled by the T+3
            # candidate resolution just above).
            return result

        if state.pending_signal and not state.pending_signal.get("order_requested"):
            pending_dir = Direction(state.pending_signal["direction"])
            pending_opposes_held = order_executor.target_symbol_for_direction(pending_dir) != pos.symbol
            if scheduled_protected and pending_opposes_held:
                pass  # 예약매수 보호 구간 -- 반대 방향 pending signal은 이 tick엔 무시(자연 만료/재시도에 맡김)
            elif _pending_direction_still_active(pending_dir, macd_snap):
                outcome = _execute_or_wait(
                    broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
                    direction=pending_dir, signal_id=str(state.pending_signal["signal_id"]),
                    signal_type=str(state.pending_signal.get("signal_type") or "REVERSAL"), position=pos, result=result,
                    signal_detected_at=_pending_detected_at(state.pending_signal, now),
                )
                if outcome is not None:
                    _apply_switch_outcome(state, outcome, pending_dir, now)
                    result.actions.append(f"OPPOSITE_SIGNAL:{pending_dir.value}")
                    state.last_evaluated_bar_ts = bar_ts_str
                    return result

        # NOTE: the forming-bar candidate (candidate_snap/candidate_condition)
        # is shadow/display data ONLY (docs §5 MACD single-path fix) — it must
        # never call order_executor, the MAJOR_FLAG filter, or mutate
        # confirmed state/processed_signal_ids/the signal ledger. Only the
        # confirmed, completed-3m-bar crossover below has order authority.
        if (
            confirmed_direction != Direction.HOLD
            and scheduled_protected
            and order_executor.target_symbol_for_direction(confirmed_direction) != pos.symbol
        ):
            # 2026-08-07 (사용자 요청): 예약매수 보호 구간(09:03~09:10) -- 반대
            # 방향 확정 플래그는 캐치/기록만 하고 청산/스위치는 하지 않는다.
            # STOP_LOSS/PROFIT_LOCK/QUICK_PROFIT/강제청산은 이 위 코드에서 이미
            # 먼저 평가되므로 이 보호와 무관하게 그대로 작동한다.
            _record_confirmed_blocked_signal(
                state=state, macd_snap=macd_snap, direction=confirmed_direction,
                signal_type="REVERSAL", reason=config.SCHEDULED_ENTRY_PROTECTION_ACTIVE, result=result,
            )
        elif confirmed_direction != Direction.HOLD and not entry_window_open:
            target = order_executor.target_symbol_for_direction(confirmed_direction)
            gate_reason = _confirmed_signal_order_gate_block_reason(state, now)
            if target == pos.symbol:
                _record_confirmed_blocked_signal(
                    state=state, macd_snap=macd_snap, direction=confirmed_direction,
                    signal_type="HELD_SAME", reason=gate_reason, result=result,
                )
            else:
                # 2026-08-06 fix: entry_window_open being False (NEW_ENTRY_
                # CUTOFF or quote_history_mismatch_reason -- a WATCH_SYMBOL
                # 000660 data-quality doubt, unrelated to the traded ETF's own
                # quote) used to block EVERYTHING for a confirmed REVERSAL,
                # including selling the already-held, now-wrong-direction
                # position -- leaving it completely unmonitored for the rest
                # of the day (2026-08-06 real incident: a confirmed DOWN_BLUE
                # while holding 0193T0 produced zero order attempts at all;
                # the position sat losing money until a manual sell). Still
                # never re-enters the new direction under the same doubt --
                # same sell-only/no-re-entry semantics already used for a
                # MAJOR/추세전환장-filtered reversal (_execute_reversal_exit_
                # only_for_filtered_entry), just reused with this reason.
                window_closed_decision = MajorFlagDecision(
                    approved=False, score=0.0, required_score=0.0, decision=gate_reason,
                    reasons=(f"entry window closed: {gate_reason}",),
                    component_scores={}, metrics={}, is_reversal=True, fast_reversal=False,
                    block_reason=gate_reason,
                )
                outcome = _execute_reversal_exit_only_for_filtered_entry(
                    broker=broker, state=state, macd_snap=macd_snap,
                    direction=confirmed_direction, position=pos,
                    decision=window_closed_decision, result=result, gate_mode="NONE",
                )
                if outcome is not None:
                    _apply_exit_outcome(state, outcome)
                    result.actions.append(f"OPPOSITE_SIGNAL_SELL_ONLY:{confirmed_direction.value}")
                    return result
        elif entry_window_open and confirmed_direction != Direction.HOLD:
            target = order_executor.target_symbol_for_direction(confirmed_direction)
            if target != pos.symbol:
                reversal_decision, reversal_gate_mode = _judge_entry_gate(
                    state=state, bars_3m=bars_3m, df_1m=df_1m, direction=confirmed_direction,
                    position=pos, now=now,
                    signal_id=make_signal_id(macd_snap.bar_dt, confirmed_direction),
                )
                if reversal_decision is not None and not reversal_decision.approved:
                    if reversal_gate_mode == "TIME_WINDOW":
                        # Two-bar (T -> T+3) confirmation model (spec §1/§12):
                        # a not-yet-confirmed TW candidate must NEVER trigger
                        # the sell-only liquidation below — the held position
                        # stays untouched until _resolve_time_window_candidate
                        # (checked at T+3) decides to switch or hold. The
                        # candidate itself is already recorded by
                        # _judge_time_window_flag above; nothing else happens
                        # on this bar.
                        pass
                    else:
                        outcome = _execute_reversal_exit_only_for_filtered_entry(
                            broker=broker, state=state, macd_snap=macd_snap,
                            direction=confirmed_direction, position=pos,
                            decision=reversal_decision, result=result,
                            gate_mode=reversal_gate_mode,
                        )
                        if outcome is not None:
                            _apply_exit_outcome(state, outcome)
                            result.actions.append(f"OPPOSITE_SIGNAL_SELL_ONLY:{confirmed_direction.value}")
                            return result
                else:
                    outcome = _dispatch_confirmed_signal(
                        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
                        direction=confirmed_direction, signal_type="REVERSAL", position=pos, result=result,
                        major_decision_override=reversal_decision,
                        major_gate_mode_override=reversal_gate_mode,
                        bars_3m=bars_3m, df_1m=df_1m,
                    )
                    if _is_major_filtered(outcome):
                        result.actions.append(f"{config.FILTERED_OUT}:{confirmed_direction.value}")
                    elif outcome is not None:
                        _apply_switch_outcome(state, outcome, confirmed_direction, now)
                        result.actions.append(f"OPPOSITE_SIGNAL:{confirmed_direction.value}")
                        return result
                    elif result.skipped == config.MISSED_SIGNAL_QUOTE_STALE and state.position is not None and state.position.symbol == pos.symbol:
                        # 2026-08-04 fix: a stale/unavailable quote for the NEW
                        # target must never also block exiting the ALREADY-held
                        # position -- entering late into a possibly-reversed
                        # move is riskier than staying in cash, but leaving
                        # real exposure unmonitored is the opposite of what a
                        # risk system should do. Liquidate the held ETF right
                        # now (reusing the filtered-entry sell-only path with a
                        # distinct signal_id so it never collides with the
                        # MISSED_SIGNAL_QUOTE_STALE row _dispatch_confirmed_
                        # signal already recorded for the original signal_id
                        # above) and leave a pending signal so the Flat
                        # branch's existing retry mechanism completes the new
                        # BUY the moment its own quote recovers.
                        base_signal_id = make_signal_id(macd_snap.bar_dt, confirmed_direction)
                        stale_recovery_decision = MajorFlagDecision(
                            approved=False, score=0.0, required_score=0.0,
                            decision=config.MISSED_SIGNAL_QUOTE_STALE,
                            reasons=("target quote unavailable/stale after retries",),
                            component_scores={}, metrics={}, is_reversal=True, fast_reversal=False,
                            block_reason=config.MISSED_SIGNAL_QUOTE_STALE,
                        )
                        sell_outcome = _execute_reversal_exit_only_for_filtered_entry(
                            broker=broker, state=state, macd_snap=macd_snap,
                            direction=confirmed_direction, position=pos,
                            decision=stale_recovery_decision, result=result,
                            gate_mode="NONE",
                            signal_id_override=f"{base_signal_id}:QUOTE_STALE_RECOVERY_SELL",
                        )
                        if sell_outcome is not None:
                            _apply_exit_outcome(state, sell_outcome)
                            _set_pending_signal(
                                state, signal_id=f"{base_signal_id}:QUOTE_STALE_RECOVERY_BUY",
                                direction=confirmed_direction, signal_type="INITIAL",
                                macd_snap=macd_snap, detected_at=now, reason=config.MISSED_SIGNAL_QUOTE_STALE,
                            )
                            result.actions.append(f"OPPOSITE_SIGNAL_SELL_ONLY:{confirmed_direction.value}")
                            return result
            elif (
                state.major_filter_enabled or state.sideways_filter_enabled
                or state.trend_persistence_filter_enabled or state.single_entry_filter_enabled
                or state.time_window_filter_enabled
            ):
                _dispatch_confirmed_signal(
                    broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
                    direction=confirmed_direction, signal_type="HELD_SAME", position=pos, result=result,
                    bars_3m=bars_3m, df_1m=df_1m,
                )
            else:
                _record_confirmed_blocked_signal(
                    state=state, macd_snap=macd_snap, direction=confirmed_direction,
                    signal_type="HELD_SAME",
                    reason=order_executor.BLOCK_ALREADY_HOLDING,
                    result=result,
                )

        # Profit Lock — MACD convergence early exit (docs §10 priority 4,
        # 2026-08-05 spec; replaces the old net-return-giveback Profit Lock
        # entirely). Only reached once the opposite-signal branch above had
        # first refusal and did not switch/exit the position this tick.
        # Mutually exclusive with Quick-Profit below (UI/service enforce
        # never both ON) — evaluated off the SAME confirmed WATCH_SYMBOL
        # MACD/Signal already computed for flag generation above (macd_snap),
        # never a second MACD calculation and never the forming bar.
        if (
            state.profit_lock_enabled and current_price is not None
            and state.position is not None and state.position.symbol == pos.symbol
            and state.position.quantity > 0
        ):
            profit_lock_direction = _direction_for_symbol(pos.symbol)
            if profit_lock_direction is not None:
                should_profit_lock_exit = _advance_profit_lock(
                    state, symbol=pos.symbol, direction=profit_lock_direction, macd_snap=macd_snap,
                    current_price=current_price, entry_price=pos.avg_price, quantity=pos.quantity,
                )
                if should_profit_lock_exit:
                    # Snapshot BEFORE _apply_exit_outcome resets these fields
                    # for the next holding period — record_profit_lock_
                    # convergence_fields() patches the ledger row execute_exit
                    # is about to write via order_executor's own (unmodified)
                    # _record_leg, purely additive columns (docs §10 "상태·
                    # 원장 기록"), never touching order/fill/balance fields.
                    profit_lock_ledger_fields = {
                        "profit_lock_enabled": True,
                        "profit_lock_peak_return_pct": state.profit_lock_peak_return_pct,
                        "profit_lock_max_support_gap": state.profit_lock_max_support_gap,
                        "profit_lock_current_support_gap": state.profit_lock_current_support_gap,
                        "profit_lock_gap_ratio": state.profit_lock_gap_ratio,
                        "profit_lock_contraction_count": state.profit_lock_contraction_count,
                        "profit_lock_drawdown_pct": state.profit_lock_drawdown_pct,
                    }
                    outcome = order_executor.execute_exit(
                        broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                        exit_reason=config.EXIT_PROFIT_LOCK_MACD_CONVERGENCE, entry_price=pos.avg_price,
                        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                    )
                    _apply_exit_outcome(state, outcome)
                    if outcome.final_state == SignalState.EXECUTED and outcome.sell_result is not None:
                        ledger.record_profit_lock_convergence_fields(
                            str(outcome.sell_result.order_id or ""), profit_lock_ledger_fields,
                        )
                    result.actions.append(f"PROFIT_LOCK_MACD_CONVERGENCE:{pos.symbol}")
                    return result

        # Quick-Profit take-profit filter (2026-08-04 user spec, priority 5 —
        # below Profit Lock above) — EXIT LOGIC ONLY, completely independent
        # of major_filter_enabled/sideways_filter_enabled (entry gating is
        # untouched — see _judge_entry_gate). Never touches risk_exit.py's
        # own STOP_LOSS function and always yields to STOP_LOSS/OPPOSITE_
        # SIGNAL/PROFIT_LOCK (checked first, already returned by now if any
        # of them fired this tick).
        #
        # 2026-08-05 redesign (사용자 요청): 더 이상 "1분 고점 기억"으로 판정하지
        # 않는다 — 매 tick의 실시간 quote(``current_price``, 아직 확정되지 않은
        # 진행 중인 1분봉이라도 상관없이)만으로 그 자리에서 즉시 순수익률을 계산해
        # 문턱(기본 +2.0%) 이상이면 바로 전량 매도한다. 기억된 값이 없으므로
        # "이미 반전된 옛 고점 기준으로 팔리는" 문제 자체가 구조적으로 없다(2026
        # -08-04에 고쳤던 문제의 근본 원인 제거). 이 토글은 진입 경로(수동매수 포함
        # — manual_entry도 동일한 state.position/run_once 경로를 타므로 자동으로
        # 적용됨)나 이력과 무관하게, ON으로 바뀐 바로 다음 tick부터 즉시 이 조건으로
        # 판정한다 — 이미 보유 중인 포지션이 이미 조건을 만족한 상태라면 그 tick에
        # 바로 매도된다. OFF면 이 블록 전체가 스킵되어 기존처럼 다음 플래그까지
        # 그대로 보유한다.
        if current_price is not None and state.quick_profit_enabled:
            current_net_return = _net_return_pct(pos.symbol, pos.avg_price, current_price, pos.quantity)
            if current_net_return >= config.QUICK_PROFIT_TAKE_PROFIT_NET_PCT:
                outcome = order_executor.execute_exit(
                    broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                    exit_reason=config.EXIT_QUICK_PROFIT_TAKE_PROFIT, entry_price=pos.avg_price,
                    reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                )
                _apply_exit_outcome(state, outcome)
                result.actions.append(f"QUICK_PROFIT_TAKE_PROFIT:{pos.symbol}")
                return result

        state.last_evaluated_bar_ts = bar_ts_str
        return result

    # ── Flat: new-entry evaluation ──────────────────────────────────────
    tw_resolve_outcome = _resolve_time_window_candidate(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        bars_3m=bars_3m, df_1m=df_1m, position=None, result=result,
    )
    if tw_resolve_outcome is not None and tw_resolve_outcome.final_state == SignalState.EXECUTED:
        result.actions.append(f"TIME_WINDOW_ENTRY:{tw_resolve_outcome.target_symbol}")
        return result

    if _scheduled_entry_should_fire(state, now):
        scheduled_outcome = _execute_scheduled_entry(broker=broker, market_data=market_data, state=state, now=now)
        if scheduled_outcome is not None:
            result.actions.append(f"SCHEDULED_ENTRY_0903:{scheduled_outcome.target_symbol}")
            state.last_evaluated_bar_ts = bar_ts_str
            return result

    if state.pending_signal and not state.pending_signal.get("order_requested"):
        pending_dir = Direction(state.pending_signal["direction"])
        if _pending_direction_still_active(pending_dir, macd_snap):
            outcome = _execute_or_wait(
                broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
                direction=pending_dir, signal_id=str(state.pending_signal["signal_id"]),
                signal_type=str(state.pending_signal.get("signal_type") or "INITIAL"), position=None, result=result,
                signal_detected_at=_pending_detected_at(state.pending_signal, now),
            )
            if outcome is not None:
                _apply_switch_outcome(state, outcome, pending_dir, now)
                result.actions.append(f"ENTRY:{pending_dir.value}")
                state.last_evaluated_bar_ts = bar_ts_str
                return result

    # NOTE: see the held-position branch above — the forming-bar candidate
    # never dispatches an entry order either; only the confirmed crossover
    # below (order authority stays exclusively with the completed 3m bar).
    if confirmed_direction != Direction.HOLD and not entry_window_open:
        _record_confirmed_blocked_signal(
            state=state, macd_snap=macd_snap, direction=confirmed_direction,
            signal_type="INITIAL",
            reason=_confirmed_signal_order_gate_block_reason(state, now),
            result=result,
        )
    elif entry_window_open and confirmed_direction != Direction.HOLD:
        outcome = _dispatch_confirmed_signal(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
            direction=confirmed_direction, signal_type="INITIAL", position=None, result=result,
            bars_3m=bars_3m, df_1m=df_1m,
        )
        if _is_major_filtered(outcome):
            result.actions.append(f"{config.FILTERED_OUT}:{confirmed_direction.value}")
        elif outcome is not None:
            _apply_switch_outcome(state, outcome, confirmed_direction, now)
            result.actions.append(f"ENTRY:{confirmed_direction.value}")
            return result

    state.last_evaluated_bar_ts = bar_ts_str
    return result


def _record_broker_order_result(state: RuntimeState, outcome) -> None:
    """Most recent broker call result (any leg: entry/switch/exit), so the UI
    can show it independent of the ephemeral per-tick TickResult."""
    result = outcome.buy_result or outcome.sell_result
    if result is None:
        return
    state.last_broker_order_id = result.order_id
    if result.success:
        state.last_broker_order_result = "OK"
    else:
        state.last_broker_order_result = outcome.order_failure_stage or outcome.block_reason or "ORDER_FAILED"
    state.last_broker_order_symbol = result.symbol
    state.last_broker_order_side = result.side
    state.last_broker_order_at = datetime.now(KST).isoformat()


def _record_major_exit(state: RuntimeState, symbol: Optional[str]) -> None:
    """Exit bookkeeping for the MAJOR_FLAG same-direction reentry cooldown."""
    direction = _direction_for_symbol(symbol)
    if direction is None:
        return
    state.last_major_exit_at = datetime.now(KST).isoformat()
    state.last_major_exit_direction = direction


def _apply_exit_outcome(state: RuntimeState, outcome) -> None:
    _record_broker_order_result(state, outcome)
    if outcome.final_state == SignalState.EXECUTED:
        exited_symbol = outcome.target_symbol or (outcome.sell_result.symbol if outcome.sell_result else None)
        state.position = None
        state.peak_net_return = 0.0
        state.profit_lock_active = False
        state.stop_loss_bar_symbol = None
        state.stop_loss_entry_bar_ts = None
        state.stop_loss_bar_ts = None
        state.stop_loss_bar_close = None
        state.profit_lock_symbol = None
        state.profit_lock_entry_bar_ts = None
        state.profit_lock_last_bar_ts = None
        state.profit_lock_bars_since_entry = 0
        state.profit_lock_gap_history = []
        state.profit_lock_peak_return_pct = 0.0
        state.profit_lock_current_support_gap = None
        state.profit_lock_max_support_gap = None
        state.profit_lock_gap_ratio = None
        state.profit_lock_contraction_count = 0
        state.profit_lock_drawdown_pct = 0.0
        state.scheduled_entry_protected = False
        # Any full exit (STOP_LOSS/FORCED_LIQUIDATION/PROFIT_LOCK/QUICK_PROFIT/
        # OPPOSITE_SIGNAL switch/the time-window filter's own ladder) clears
        # the time-window filter's position-management state the same way —
        # a stale time_window_position_active=True must never survive past
        # the position it described.
        state.time_window_position_active = False
        state.time_window_entry_session = None
        state.time_window_entry_flag_seq = None
        state.time_window_entry_session_seq = None
        state.time_window_tp1_done = False
        state.time_window_initial_quantity = 0
        state.time_window_peak_net_return = 0.0
        _record_major_exit(state, exited_symbol)
    state.order_block_reason = outcome.block_reason


def _apply_switch_outcome(state: RuntimeState, outcome, pattern: Direction, now: datetime) -> None:
    """Retry policy (docs §2): every signal_id is single-shot regardless of
    outcome — success, failure, or block — so it is never automatically
    retried; a later, genuinely new signal_id (a different bar) is still
    free to fire. A switch whose SELL leg cleared to 0 but whose BUY leg then
    failed/was blocked leaves the account really flat, so state.position must
    reflect that immediately rather than keep pointing at the already-sold
    symbol (docs: 스위칭 부분실패 상태 처리) — this also prevents a duplicate
    SELL next tick, since the held-position branch will no longer see a
    stale position for that symbol.

    Always clears scheduled_entry_protected first -- a switch always forms
    either a brand-new (non-scheduled) position or ends up flat, neither of
    which should inherit a stale scheduled-entry protection window.
    _execute_scheduled_entry re-sets it True right after calling this, for
    its own fill specifically.
    """
    state.scheduled_entry_protected = False
    if outcome.final_state == SignalState.EXECUTED:
        state.position = PositionSnapshot(
            symbol=outcome.target_symbol, quantity=outcome.quantity,
            avg_price=(outcome.filled_avg_price or (outcome.buy_result.executed_price if outcome.buy_result else 0.0)),
            entry_at=datetime.now(KST),
        )
        state.last_signal_direction = pattern
        state.last_executed_direction = pattern
        state.last_signal_bar_ts = outcome.timestamps.get("evaluated_at")
        state.peak_net_return = 0.0
        state.profit_lock_active = False
        # Stop Loss 3-minute bar gating starts fresh at this new position's
        # entry fill -- the bar containing the fill is the execution bar
        # (excluded from Stop Loss, docs 2026-08-02 Exit Rule: 3-Minute
        # Confirmed Bars); see _advance_stop_loss_bar. Uses this tick's
        # logical ``now`` (not state.position.entry_at, which is real
        # wall-clock time used elsewhere e.g. MAJOR_FLAG cooldowns and left
        # unchanged) so it lines up with every later tick's own ``now``.
        entry_bar_start, _entry_bar_end = forming_bar_window(now)
        state.stop_loss_bar_symbol = state.position.symbol
        state.stop_loss_entry_bar_ts = entry_bar_start.isoformat()
        state.stop_loss_bar_ts = entry_bar_start.isoformat()
        state.stop_loss_bar_close = state.position.avg_price
        # MAJOR_FLAG/추세전환장 daily budget counts only a really-filled BUY
        # leg, never a mere filter approval or a rejected/unfilled order. The
        # two toggles are mutually exclusive (same precedence as
        # _judge_entry_gate), so at most one counter increments per fill.
        filled_qty = int(outcome.quantity or 0) or int(
            (outcome.buy_result.executed_qty if outcome.buy_result else 0) or 0
        )
        if filled_qty > 0 and state.sideways_filter_enabled:
            state.daily_sideways_entry_count = int(state.daily_sideways_entry_count or 0) + 1
            state.last_sideways_entry_at = datetime.now(KST).isoformat()
        elif filled_qty > 0 and state.major_filter_enabled:
            state.daily_major_entry_count = int(state.daily_major_entry_count or 0) + 1
            state.last_major_entry_at = datetime.now(KST).isoformat()
        elif filled_qty > 0 and state.trend_persistence_filter_enabled:
            state.daily_trend_persistence_entry_count = int(state.daily_trend_persistence_entry_count or 0) + 1
            state.last_trend_persistence_entry_at = datetime.now(KST).isoformat()
        elif filled_qty > 0 and state.single_entry_filter_enabled:
            state.daily_single_entry_count = int(state.daily_single_entry_count or 0) + 1
            state.last_single_entry_at = datetime.now(KST).isoformat()
    elif outcome.sell_result is not None and outcome.sell_result.success and outcome.sell_qty_after == 0:
        exited_symbol = outcome.sell_result.symbol
        state.position = None
        state.peak_net_return = 0.0
        state.profit_lock_active = False
        _record_major_exit(state, exited_symbol)
    if (
        _has_order_request(outcome)
        and not _sell_cleared_but_buy_not_requested(outcome)
        and outcome.signal_id
        and outcome.signal_id not in state.processed_signal_ids
    ):
        state.processed_signal_ids = list(state.processed_signal_ids) + [outcome.signal_id]
    state.order_block_reason = outcome.block_reason


def _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, detected_at, outcome, dispatch_trace=None) -> None:
    order_result = outcome.final_state.value if outcome is not None else SignalState.WAITING.value
    block_reason = outcome.block_reason or "" if outcome is not None else (state.order_block_reason or "WAITING")
    trading_date = macd_snap.bar_dt.astimezone(KST).strftime("%Y%m%d")
    trace = dict(dispatch_trace or {})
    raw = dict(trace.get("broker_raw") or {})
    # MAJOR_FLAG-rejected signals never reach order_executor, so their
    # order_result comes from the dispatch trace (FILTERED_OUT) instead.
    order_result = str(trace.get("order_result_override") or order_result)
    major_fields = dict(trace.get("major_fields") or {}) or _major_ledger_fields(state)
    # All ledger-recorded signals are confirmed (completed-bar) since the
    # 2026-07-27 KIS-parity fix — the forming/provisional candidate never
    # reaches this function any more (shadow display only).
    row = {
        "trading_date": trading_date,
        "completed_bar_at": macd_snap.bar_dt.astimezone(KST).strftime("%H%M%S"),
        "signal_id": signal_id,
        "signal_type": signal_type,
        "direction": direction.value,
        "macd": macd_snap.macd,
        "signal": macd_snap.signal,
        "hist_last3": str(macd_snap.hist_last3),
        "detected_at": detected_at.isoformat(),
        "order_requested_at": (
            outcome.timestamps.get("buy_requested_at") or outcome.timestamps.get("sell_requested_at") or ""
            if outcome is not None else ""
        ),
        "order_result": order_result,
        "block_reason": block_reason,
        "signal_bar_at": macd_snap.bar_dt.astimezone(KST).isoformat(),
        "signal_confirmed_at": (macd_snap.bar_dt + timedelta(minutes=3)).astimezone(KST).isoformat(),
        "baseline_completed_bar_at": state.session_baseline_bar_ts or "",
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE,
        "worker_code_sha": _git_sha(),
        "worker_instance_id": state.worker_instance_id or "",
        "session_started_at": state.session_started_at or "",
        "forming_bar_start": trace.get("forming_bar_start") or "",
        "forming_bar_end": trace.get("forming_bar_end") or "",
        "previous_macd": macd_snap.previous_macd if macd_snap.previous_macd is not None else "",
        "previous_signal": macd_snap.previous_signal if macd_snap.previous_signal is not None else "",
        "previous_diff": macd_snap.previous_diff if macd_snap.previous_diff is not None else "",
        "provisional_macd": "",
        "provisional_signal": "",
        "provisional_diff": "",
        "confirmed_macd": macd_snap.macd,
        "confirmed_signal": macd_snap.signal,
        "confirmed_diff": macd_snap.current_diff,
        "provisional_direction": "",
        "confirmed_direction": direction.value,
        "quote_ages": str(trace.get("quote_ages") or {}),
        "position_reconcile": trace.get("position_reconcile_result") or "",
        "executor_called": trace.get("order_executor_called"),
        "order_requested_at_trace": trace.get("order_requested_at") or "",
        "broker_called": trace.get("broker_called"),
        "broker_order_id": trace.get("broker_order_id") or "",
        "broker_rt_cd": raw.get("rt_cd") or "",
        "broker_msg_cd": raw.get("msg_cd") or "",
        "broker_msg1": raw.get("msg1") or "",
        "orderable_cash": trace.get("orderable_cash") if trace.get("orderable_cash") is not None else "",
        "nrcvb_buy_amt": trace.get("nrcvb_buy_amt") if trace.get("nrcvb_buy_amt") is not None else "",
        "nrcvb_buy_qty": trace.get("nrcvb_buy_qty") if trace.get("nrcvb_buy_qty") is not None else "",
        "psbl_qty_calc_unpr": trace.get("psbl_qty_calc_unpr") if trace.get("psbl_qty_calc_unpr") is not None else "",
        "ask1": trace.get("ask1") if trace.get("ask1") is not None else "",
        "order_price": trace.get("order_price") if trace.get("order_price") is not None else "",
        "order_type": trace.get("order_type") or "",
        "usable_cash": trace.get("usable_cash") if trace.get("usable_cash") is not None else "",
        "limit_buyable_qty": trace.get("limit_buyable_qty") if trace.get("limit_buyable_qty") is not None else "",
        "budget_qty": trace.get("budget_qty") if trace.get("budget_qty") is not None else "",
        "final_qty": trace.get("final_qty") if trace.get("final_qty") is not None else "",
        "sizing_price": trace.get("sizing_price") if trace.get("sizing_price") is not None else "",
        "requested_qty": trace.get("requested_qty") if trace.get("requested_qty") is not None else "",
        "expected_amount": trace.get("expected_amount") if trace.get("expected_amount") is not None else "",
        "sizing_rt_cd": trace.get("sizing_rt_cd") or "",
        "sizing_msg_cd": trace.get("sizing_msg_cd") or "",
        "sizing_msg1": trace.get("sizing_msg1") or "",
        "filled_qty": trace.get("filled_qty") if trace.get("filled_qty") is not None else "",
        "fill_poll_result": trace.get("fill_poll_result") or "",
        "balance_qty": trace.get("balance_qty") if trace.get("balance_qty") is not None else "",
        "failure_stage": trace.get("failure_stage") or "",
        "final_result": order_result if not block_reason else f"{order_result}:{block_reason}",
    }
    row.update(major_fields)
    written = ledger.append_signal(row)
    state.last_duplicate_signal_id = None if written else signal_id


class Macd2Worker:
    """Owns exactly one background tick thread (docs §13 single-Worker principle)."""

    def __init__(
        self, *, broker, market_data: MarketDataService, get_state, save_state,
        tick_interval_sec: float = config.WORKER_INTERVAL_SEC,
    ) -> None:
        self._broker = broker
        self._market_data = market_data
        self._get_state = get_state
        self._save_state = save_state
        self._tick_interval_sec = tick_interval_sec
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._tick_intervals: list[float] = []
        self._tick_n = 0
        self._last_tick_at: Optional[datetime] = None
        self._last_exception: Optional[str] = None
        self._last_stage_timing: dict[str, float] = {}
        self._lock = threading.RLock()
        self._instance_id = uuid.uuid4().hex[:12]
        self._started_at: Optional[datetime] = None

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def tick_stats(self) -> dict[str, Any]:
        with self._lock:
            intervals = list(self._tick_intervals[-20:])
            mean = sum(intervals) / len(intervals) if intervals else None
            p95 = sorted(intervals)[int(len(intervals) * 0.95) - 1] if intervals else None
            age = (datetime.now(KST) - self._last_tick_at).total_seconds() if self._last_tick_at else None
            next_tick_at = (
                (self._last_tick_at + timedelta(seconds=self._tick_interval_sec)).isoformat()
                if self._last_tick_at else None
            )
            return {
                "tick_n": self._tick_n, "mean_interval_sec": mean, "p95_interval_sec": p95,
                "max_interval_sec": max(intervals) if intervals else None,
                "last_tick_age_sec": age, "last_exception": self._last_exception,
                "stalled": bool(age is not None and age > config.WORKER_STALL_AGE_SEC),
                "instance_id": self._instance_id,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
                "next_tick_at": next_tick_at,
                "recent_tick_sample_count": len(self._tick_intervals),
                "stage_timing_sec": dict(self._last_stage_timing),
            }

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            t0 = time.monotonic()
            stage_timing: dict[str, float] = {}
            try:
                t_stage = time.monotonic()
                state = self._get_state()
                state.worker_instance_id = self._instance_id
                stage_timing["state_load"] = time.monotonic() - t_stage
                # Unlike this tick loop, the quote-updater background thread
                # (market_data.py) has no supervisor of its own — if it ever
                # dies, quotes freeze permanently and every confirmed signal
                # fails order dispatch with MISSED_SIGNAL_QUOTE_STALE forever
                # after (2026-08-05 real incident: quote_updater_status=
                # STOPPED for ~48min, zero auto trades all day). start_quote_
                # updater() is itself a no-op while already alive, so this is
                # safe to check every tick.
                if not self._market_data.quote_updater_alive():
                    self._market_data.start_quote_updater(interval_sec=1.0)
                tick_result = run_once(broker=self._broker, market_data=self._market_data, state=state, now=datetime.now(KST))
                stage_timing.update(tick_result.timing)
                t_stage = time.monotonic()
                self._save_state(state)
                stage_timing["state_save"] = time.monotonic() - t_stage
                with self._lock:
                    self._last_exception = None
            except Exception as exc:
                with self._lock:
                    self._last_exception = f"{exc}\n{traceback.format_exc()}"
            elapsed = time.monotonic() - t0
            with self._lock:
                self._tick_n += 1
                self._last_tick_at = datetime.now(KST)
                self._tick_intervals.append(elapsed)
                self._tick_intervals = self._tick_intervals[-50:]
                stage_timing["total"] = elapsed
                self._last_stage_timing = stage_timing
            self._stop_event.wait(max(0.0, self._tick_interval_sec - elapsed))

    def start(self) -> None:
        if self.is_alive():
            return  # never spawn a second Worker thread
        self._stop_event.clear()
        self._started_at = datetime.now(KST)
        self._thread = threading.Thread(target=self._run_loop, name="macd2-worker", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)
        self._thread = None  # never reused — start() always creates a fresh Thread object
