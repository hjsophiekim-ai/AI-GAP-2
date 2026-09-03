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
import os
import threading
import time
import traceback
import uuid
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

from app.logger import logger
from app.trading.macd2 import (
    config,
    early_take_profit,
    ledger,
    major_flag_filter,
    order_executor,
    risk_exit,
    sideways_filter,
    single_entry_filter,
    state_store,
    teg_gate,
    time_window_3slot,
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
RECOVERED_QTY_INCREASE = "RECOVERED_QTY_INCREASE_UNTRACKED_FILL"
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
    # last_detected_direction is intentionally NOT reset here (2026-08-20 NXT
    # fix). It is the running "last confirmed crossover direction" that
    # evaluate_macd_crossover() uses to suppress a same-direction repeat —
    # now that WATCH_SYMBOL's 1m history is a single continuous NXT-inclusive
    # series with no artificial day-boundary gap (market_data.py market_div=
    # "NX"), a calendar-date change is no longer a real discontinuity in the
    # underlying MACD/Signal relationship. Resetting this to None at midnight
    # used to matter only as a safety net against a stale-gap false crossover
    # at the first bar of a new day (see _advance_confirmed_primary's
    # docstring); with continuous NXT data that gap no longer exists, and
    # resetting it would instead let a genuinely still-held direction (e.g.
    # 08:45 BLUE persisting through 09:00) be re-dispatched as if it were a
    # brand-new flag the moment the date rolls over — exactly the "09:00 must
    # be BLUE-state-maintained, not a new BLUE event" requirement this fix
    # exists for.
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
    # (time_window_teg_filter_enabled / time_window_2_filter_enabled) and an
    # ALREADY-open position's own management state (time_window_position_
    # active/tp1_done/etc.) survive the rollover unchanged -- a position can
    # still be open across midnight only in the sense that FORCED_LIQUIDATION
    # already empties it by 15:00 every day, so this never actually matters
    # in practice, but is not reset here regardless (mirrors how
    # state.position itself is never reset on rollover).
    state.time_window_morning_entry_count = 0
    state.time_window_afternoon_entry_count = 0
    # 2026-08-28 fix: the filter-mode-agnostic daily total (see its own
    # field docstring in models.py) is likewise session-scoped -- reset here
    # exactly once per day, same as the two counts above, and NEVER by any
    # filter toggle (see service.set_time_window_2_filter_enabled/set_time_
    # window_teg_filter_enabled, neither of which touches it).
    state.daily_total_entry_count = 0
    state.time_window_pending_flag_direction = None
    state.time_window_pending_flag_bar_ts = None
    # TEG count-cap bypass(2026-08-27)의 "하루 1회" 소진 플래그도 마찬가지로
    # session-scoped; 토글(time_window_teg_filter_enabled)은 그대로 유지.
    state.time_window_teg_count_cap_bypass_used = False
    # 탈락 DOWN_BLUE 예외진입(2026-08-18)의 "하루 1회" 소진 플래그도 마찬가지로
    # session-scoped; 토글(down_blue_exception_filter_enabled)은 그대로 유지.
    state.daily_down_blue_exception_used = False
    # TW2 3-SLOT (2026-09-01)의 슬롯 예산/pending 후보도 마찬가지로
    # session-scoped; 토글(time_window_3slot_filter_enabled)은 그대로 유지.
    state.tw2_3slot_pending_flag_direction = None
    state.tw2_3slot_pending_flag_bar_ts = None
    state.tw2_3slot_slots_used_today = 0
    state.tw2_3slot_morning_count = 0
    state.tw2_3slot_afternoon_count = 0
    state.tw2_3slot_last_afternoon_direction = None
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
    state.premarket_carry_candidate_direction = None
    state.premarket_carry_candidate_bar_ts = None
    state.premarket_carry_executed_at = None
    state.premarket_carry_last_result = None


def _relation_from_diff(diff: Optional[float]) -> str:
    if diff is None:
        return "EQUAL"
    if diff > 0:
        return "ABOVE"
    if diff < 0:
        return "BELOW"
    return "EQUAL"


def _record_premarket_catchup_flag(state: RuntimeState, snap, direction: Direction, now: datetime) -> None:
    """2026-08-20 fix (사용자 요청 — 신호 원장에 프리마켓 08:00~09:00 크로스오버도
    표시): a confirmed flag that run_once()'s own live tick evaluates BEFORE
    config.SESSION_OPEN is already recorded to the signal ledger via
    _record_confirmed_blocked_signal (block_reason=BEFORE_SESSION_OPEN, no
    order ever placed — entry_window_open stays False regardless). But a flag
    that occurred on an EARLIER bar than the Worker's most recent (re)start —
    which initialize_strategy_session's own catch-up walk below evaluates
    purely for state bookkeeping (latest_primary_flag/last_detected_direction)
    — never went through that live-tick path at all, so it silently never
    appeared in the ledger even though the equivalent live flag would have.
    Recorded here the same way (display-only, BLOCKED, no order attempted)
    only for TODAY's premarket bars — ledger.append_signal's own signal_id
    dedup makes replaying the same bar across multiple restarts a safe no-op.
    """
    bar_kst = snap.bar_dt.astimezone(KST)
    if bar_kst.date() != now.astimezone(KST).date() or bar_kst.time() >= config.SESSION_OPEN:
        return
    signal_id = make_signal_id(snap.bar_dt, direction)
    outcome = order_executor.ExecutionOutcome(
        signal_id=signal_id, direction=direction,
        target_symbol=order_executor.target_symbol_for_direction(direction),
        final_state=SignalState.BLOCKED, block_reason="BEFORE_SESSION_OPEN",
    )
    _record_signal_ledger(state, snap, direction, "INITIAL", signal_id, now, outcome)


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
            # 2026-08-24: a Worker restart landing inside 08:45-09:03 must not
            # silently drop a premarket-carry candidate that this same catch-up
            # walk otherwise fully reconstructs (last_flag_snap/last_direction
            # below) -- this is the exact restart scenario today's own live
            # incident (T+3 pending-candidate clobbering, fixed separately in
            # this same file) showed can happen mid-morning. run_once() only
            # ever advances premarket_carry_* off a LIVE tick's own confirmed
            # bar (worker.py's _advance_premarket_carry_candidate call site);
            # this replay is the only other place a flag becomes "confirmed",
            # so it must feed the exact same bookkeeping function.
            _advance_premarket_carry_candidate(state, snap, direction)
            if direction != Direction.HOLD:
                last_direction = direction
                last_flag_snap = snap
                state.latest_primary_flag = direction
                state.latest_primary_signal_id = make_signal_id(snap.bar_dt, direction)
                _record_premarket_catchup_flag(state, snap, direction, now)
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
                if state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled:
                    # 2026-08-21 fix (real incident: a repeated restart/crash
                    # loop this morning made this branch fire _set_pending_
                    # signal for a stale multi-bar-gap mismatch while the TW
                    # filter was ON -- _execute_or_wait's pending_signal path
                    # NEVER consults _judge_entry_gate/time_window_filter at
                    # all, so it force-entered unconditionally, completely
                    # bypassing the T+3 re-confirm + quality gate the user
                    # explicitly turned on. That position then also never got
                    # a TP1/TP2 take-profit chance, because entries taken
                    # this way don't set time_window_entry_session either).
                    # Route through the SAME TW pending-candidate mechanism a
                    # live-detected flag uses instead (_judge_time_window_flag)
                    # -- evaluate_time_window_entry's own multi-bar-gap check
                    # (see _resolve_time_window_candidate's docstring) then
                    # correctly DROPS a stale candidate as expired rather
                    # than blindly confirming off bars this old, exactly the
                    # safety property a bare pending_signal has no concept of.
                    #
                    # 2026-08-24 fix (real incident: repeated restarts during
                    # today's KIS mock-mode rate-limit contention -- see
                    # market_data.py's WATCH_SYMBOL fix -- kept clobbering a
                    # GENUINE, more-recent pending TW candidate that a live
                    # tick had already set and persisted just before each
                    # restart, with this catch-up walk's own necessarily-OLDER
                    # find (its loop deliberately stops one bar short of the
                    # newest, so it can never see a flag on today's actual
                    # newest bar). Net effect: two real flags (09:48 UP_RED,
                    # 10:33 DOWN_BLUE) each got overwritten by a stale
                    # 08:30-ish candidate before ever reaching their own T+3
                    # resolution -- zero orders all day despite two genuine
                    # confirmed flags. A pending candidate already on state
                    # (reloaded from disk, so it survives the restart) is by
                    # construction never older than what this abbreviated
                    # replay can find, so it always wins -- only fill the slot
                    # here if it's still genuinely empty.
                    if state.time_window_pending_flag_direction is None:
                        state.time_window_pending_flag_direction = last_direction
                        state.time_window_pending_flag_bar_ts = last_flag_snap.bar_dt.isoformat()
                else:
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
        # 2026-08-20 NXT fix: a TRUE cold start (no same-day
        # last_confirmed_bar_ts at all — first-ever launch, or state.json
        # lost) used to blindly seed last_detected_direction=None here,
        # discarding whatever direction the continuous NXT-inclusive history
        # already establishes (e.g. an 08:45 BLUE still in force at restart
        # time). Since day rollover no longer resets this field either
        # (_apply_day_rollover), a cold start must derive the SAME direction
        # a continuously-running Worker would already be holding, or a
        # mid-session restart could re-announce an already-known state as a
        # "new" flag, or (condition 3 regression risk) suppress a genuine one
        # via a wrong seed — replay the full continuous bars_3m the exact
        # same way the resuming_today branch above replays today's bars,
        # just over the whole history instead of only today's slice, so
        # restart-before/after state is identical (condition 4).
        last_flag_snap = None
        last_direction = None
        macd_snap = None
        if not bars_3m.empty:
            for pos in range(len(bars_3m)):
                snap = calculate_macd(bars_3m.iloc[: pos + 1])
                if snap is None:
                    continue
                macd_snap = snap
                direction = evaluate_macd_crossover(snap, last_direction)
                if direction != Direction.HOLD:
                    last_direction = direction
                    last_flag_snap = snap
                    _record_premarket_catchup_flag(state, snap, direction, now)
            state.last_detected_direction = last_direction
            if last_flag_snap is not None:
                state.latest_primary_flag = last_direction
                state.latest_primary_signal_id = make_signal_id(last_flag_snap.bar_dt, last_direction)
            if macd_snap is not None:
                state.last_confirmed_bar_ts = macd_snap.bar_dt.isoformat()
        else:
            state.last_detected_direction = None

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
    chance to evaluate and dispatch on.

    2026-08-20 NXT fix: today's first bar used to be treated as baseline-only
    and skipped (mirroring _advance_confirmed_primary's OLD is-first-of-day
    gate) — that live-path gate was already narrowed on 2026-08-18, and is
    removed entirely here on 2026-08-20 now that ``df_1m`` is a single
    continuous NXT-inclusive series with no artificial day boundary. This
    function now instead REPLAYS every bar strictly before today (same
    continuous ``bars_3m`` frame) purely to seed ``last_direction`` before
    entering today's loop, so today's first bar is judged against the real
    last-known direction (e.g. still BLUE from yesterday evening) exactly
    like the live path now does — it is never treated as a fresh start.
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

    last_direction: Optional[Direction] = None
    for idx in bars_3m.index[bars_3m.index < today_indices[0]]:
        window = bars_3m.iloc[: idx + 1]
        snap = calculate_macd(window)
        if snap is None:
            continue
        direction = evaluate_macd_crossover(snap, last_direction)
        if direction != Direction.HOLD:
            last_direction = direction

    session_start_dt = _parse_iso_dt(session_started_at)
    overview: list[dict[str, Any]] = []
    for idx in today_indices:
        window = bars_3m.iloc[: idx + 1]
        snap = calculate_macd(window)
        if snap is None:
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


def _record_reconcile_discovered_position(state: RuntimeState, pos: PositionSnapshot, now: datetime) -> None:
    """2026-08-20 fix: a position the broker holds that runtime state never
    recorded entering (RECOVERED_FROM_BROKER) used to leave zero trace in
    the signal ledger. This never had access to a macd_snap (reconcile runs
    before bars_3m/macd_snap are computed each tick), so it cannot reuse
    _record_signal_ledger's schema -- writes a minimal, clearly-labeled row
    directly instead. The real entry time/price are genuinely unknown (that
    is the entire problem this discovers); this only records WHEN the gap
    was noticed and what was found, never fabricates the missing history.

    2026-08-25 fix: this signal-ledger row alone left the EXECUTION ledger
    with zero trace of the BUY itself (order_executor._record_leg, entirely
    untouched here, never ran for it) -- see ledger.append_reconcile_
    backfill_buy's own docstring for what it backfills and why it is
    idempotent/never double-counts PnL.
    """
    direction = _direction_for_symbol(pos.symbol)
    signal_id = f"RECONCILE_DISCOVERED_{pos.symbol}_{now.strftime('%Y%m%d%H%M%S')}"
    ledger.append_reconcile_backfill_buy(
        symbol=pos.symbol, quantity=pos.quantity, avg_price=pos.avg_price,
        reconciled_at=now.isoformat(), mode=state.mode or "mock", signal_id=signal_id,
    )
    row = {
        "trading_date": now.astimezone(KST).strftime("%Y%m%d"),
        "completed_bar_at": "",
        "signal_id": signal_id,
        "signal_type": "RECONCILE_DISCOVERED",
        "direction": direction.value if direction else "",
        "macd": "", "signal": "", "hist_last3": "",
        "detected_at": now.isoformat(),
        "order_requested_at": "",
        "order_result": "RECONCILE_DISCOVERED_POSITION",
        "block_reason": f"qty={pos.quantity}_avg_price={pos.avg_price}",
        "signal_bar_at": "", "signal_confirmed_at": "",
        "baseline_completed_bar_at": state.session_baseline_bar_ts or "",
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE,
        "worker_code_sha": _git_sha(),
        "worker_instance_id": state.worker_instance_id or "",
        "session_started_at": state.session_started_at or "",
        **_entry_gate_ledger_fields(state, None, "NONE"),
    }
    ledger.append_signal(row)


def _record_reconcile_discovered_buy_delta(
    state: RuntimeState,
    *,
    symbol: str,
    bought_qty: int,
    avg_price: float,
    position_before: int,
    position_after: int,
    now: datetime,
) -> None:
    if bought_qty <= 0:
        return
    signal_id = f"RECONCILE_DISCOVERED_BUY_DELTA_{symbol}_{now.strftime('%Y%m%d%H%M%S')}"
    ledger.append_reconcile_backfill_buy(
        symbol=symbol,
        quantity=bought_qty,
        avg_price=avg_price,
        reconciled_at=now.isoformat(),
        mode=state.mode or "mock",
        signal_id=signal_id,
        position_before=position_before,
        position_after=position_after,
    )
    direction = _direction_for_symbol(symbol)
    row = {
        "trading_date": now.astimezone(KST).strftime("%Y%m%d"),
        "completed_bar_at": "",
        "signal_id": signal_id,
        "signal_type": "RECONCILE_DISCOVERED_BUY_DELTA",
        "direction": direction.value if direction else "",
        "macd": "", "signal": "", "hist_last3": "",
        "detected_at": now.isoformat(),
        "order_requested_at": "",
        "order_result": RECOVERED_QTY_INCREASE,
        "block_reason": f"qty_bought={bought_qty}_avg_price={avg_price}",
        "signal_bar_at": "", "signal_confirmed_at": "",
        "baseline_completed_bar_at": state.session_baseline_bar_ts or "",
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE,
        "worker_code_sha": _git_sha(),
        "worker_instance_id": state.worker_instance_id or "",
        "session_started_at": state.session_started_at or "",
        **_entry_gate_ledger_fields(state, None, "NONE"),
    }
    ledger.append_signal(row)


def _record_reconcile_discovered_sell(
    broker, state: RuntimeState, *, symbol: str, sold_qty: int, entry_price: float,
    position_before: int, position_after: int, now: datetime, exit_reason: str,
) -> None:
    """2026-08-28 real incident fix: the qty-DECREASE mirror of
    _record_reconcile_discovered_position (which fixed the qty-INCREASE
    case, RECOVERED_FROM_BROKER, on 2026-08-25). reconcile_position_state's
    RECOVERED_TO_FLAT and the decrease sub-case of RECOVERED_QTY_MISMATCH
    used to silently adopt a lower broker-reported quantity with ZERO trace
    in either ledger of whatever SELL happened to get there -- a held
    position could shrink or vanish at the broker with no execution-ledger
    row anywhere recording it, breaking summarize_daily_trading's round-trip
    accounting for that position.

    Writes a minimal signal-ledger row (same reasoning as
    _record_reconcile_discovered_position: no macd_snap is available this
    early in a tick) plus a real execution-ledger row via ledger.
    append_reconcile_backfill_sell -- which, unlike the BUY-side backfill,
    DOES compute a real gross/net PnL, because ``entry_price`` here is the
    position's own tracked avg_price (known to this reconcile step, unlike
    the generic broker-layer BROKER_DIRECT hook in ledger.py that has no
    position context at all).
    """
    exit_price = entry_price
    getter = getattr(broker, "get_quote", None) or getattr(broker, "get_current_price", None)
    if getter is not None:
        try:
            quote = getter(symbol)
        except Exception:
            quote = None
        if quote and quote > 0:
            exit_price = float(quote)

    reconciled_at = now.isoformat()
    signal_id = f"RECONCILE_DISCOVERED_SELL_{symbol}_{now.strftime('%Y%m%d%H%M%S')}"
    ledger.append_reconcile_backfill_sell(
        symbol=symbol, quantity=sold_qty, exit_price=exit_price, entry_price=entry_price,
        position_before=position_before, position_after=position_after,
        reconciled_at=reconciled_at, mode=state.mode or "mock", exit_reason=exit_reason,
        signal_id=signal_id,
    )
    direction = _direction_for_symbol(symbol)
    row = {
        "trading_date": now.astimezone(KST).strftime("%Y%m%d"),
        "completed_bar_at": "",
        "signal_id": signal_id,
        "signal_type": "RECONCILE_DISCOVERED_SELL",
        "direction": direction.value if direction else "",
        "macd": "", "signal": "", "hist_last3": "",
        "detected_at": now.isoformat(),
        "order_requested_at": "",
        "order_result": exit_reason,
        "block_reason": f"qty_sold={sold_qty}_exit_price={exit_price}",
        "signal_bar_at": "", "signal_confirmed_at": "",
        "baseline_completed_bar_at": state.session_baseline_bar_ts or "",
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE,
        "worker_code_sha": _git_sha(),
        "worker_instance_id": state.worker_instance_id or "",
        "session_started_at": state.session_started_at or "",
        **_entry_gate_ledger_fields(state, None, "NONE"),
    }
    ledger.append_signal(row)


def abandon_pending_time_window_candidate_if_any(state: RuntimeState, now: datetime, *, reason: str) -> bool:
    """2026-08-28 real incident fix: turning TW2/TEGv2 OFF via the UI toggle
    (service.set_time_window_2_filter_enabled) never touched an already-
    pending T+3 candidate (state.time_window_pending_flag_direction/
    bar_ts) -- it just left it sitting in state. _resolve_time_window_
    candidate's own gate (``if not (time_window_2_filter_enabled or
    time_window_teg_filter_enabled): return None``) then makes it a
    permanent no-op the instant both toggles are off, so the candidate is
    silently orphaned forever: never approved, never rejected, never
    logged, and re-enabling the toggle later does not help either (by then
    bars_3m has moved many bars past flag_bar_dt, breaking evaluate_time_
    window_entry's one-bar-after contract). Real incident: a 10:09 DOWN_
    BLUE flag confirmed, was correctly recorded as INITIAL/PENDING, and
    then simply vanished -- no approval, no rejection, no order, ever.

    Call this from the TW2 toggle-off path so the candidate is explicitly
    cleared and auditable instead of silently lost. A pure state/ledger
    cleanup -- never touches MACD calculation, the TW2/TEGv2 gate scoring,
    or order dispatch. Returns True if a pending candidate was actually
    abandoned (for the caller's own logging), False if there was none.
    """
    direction = state.time_window_pending_flag_direction
    flag_bar_ts = state.time_window_pending_flag_bar_ts
    if direction is None:
        return False
    state.time_window_pending_flag_direction = None
    state.time_window_pending_flag_bar_ts = None
    signal_id = f"TW_PENDING_ABANDONED_{direction.value}_{now.strftime('%Y%m%d%H%M%S')}"
    row = {
        "trading_date": now.astimezone(KST).strftime("%Y%m%d"),
        "completed_bar_at": "",
        "signal_id": signal_id,
        "signal_type": "TIME_WINDOW_CONFIRM",
        "direction": direction.value,
        "macd": "", "signal": "", "hist_last3": "",
        "detected_at": now.isoformat(),
        "order_requested_at": "",
        "order_result": "ABANDONED",
        "block_reason": f"{reason}_flag_bar_at={flag_bar_ts or ''}",
        "signal_bar_at": flag_bar_ts or "", "signal_confirmed_at": "",
        "baseline_completed_bar_at": state.session_baseline_bar_ts or "",
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE,
        "worker_code_sha": _git_sha(),
        "worker_instance_id": state.worker_instance_id or "",
        "session_started_at": state.session_started_at or "",
        **_entry_gate_ledger_fields(state, None, "NONE"),
    }
    ledger.append_signal(row)
    return True


def abandon_pending_tw2_3slot_candidate_if_any(state: RuntimeState, now: datetime, *, reason: str) -> bool:
    """TW2 3-SLOT's own mirror of abandon_pending_time_window_candidate_if_any
    above, on the fully separate tw2_3slot_pending_flag_* fields — same
    2026-08-28 orphaned-candidate incident, closed the same way for this
    mode too. Call from set_time_window_3slot_filter_enabled's toggle-off
    path (and from TW2/TEG's setters when they force this mode off, and
    vice versa) so a pending candidate is explicitly cleared and auditable
    instead of silently lost. Pure state/ledger cleanup only."""
    direction = state.tw2_3slot_pending_flag_direction
    flag_bar_ts = state.tw2_3slot_pending_flag_bar_ts
    if direction is None:
        return False
    state.tw2_3slot_pending_flag_direction = None
    state.tw2_3slot_pending_flag_bar_ts = None
    signal_id = f"TW2_3SLOT_PENDING_ABANDONED_{direction.value}_{now.strftime('%Y%m%d%H%M%S')}"
    row = {
        "trading_date": now.astimezone(KST).strftime("%Y%m%d"),
        "completed_bar_at": "",
        "signal_id": signal_id,
        "signal_type": "TW2_3SLOT_CONFIRM",
        "direction": direction.value,
        "macd": "", "signal": "", "hist_last3": "",
        "detected_at": now.isoformat(),
        "order_requested_at": "",
        "order_result": "ABANDONED",
        "block_reason": f"{reason}_flag_bar_at={flag_bar_ts or ''}",
        "signal_bar_at": flag_bar_ts or "", "signal_confirmed_at": "",
        "baseline_completed_bar_at": state.session_baseline_bar_ts or "",
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE,
        "worker_code_sha": _git_sha(),
        "worker_instance_id": state.worker_instance_id or "",
        "session_started_at": state.session_started_at or "",
        **_entry_gate_ledger_fields(state, None, "NONE"),
    }
    ledger.append_signal(row)
    return True


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
            if new_qty < old_qty:
                # 2026-08-28 fix: this branch is also reached when the
                # broker's real holding QUIETLY SHRANK (not just settled to
                # a different qty on a slow fill) -- e.g. a partial exit
                # whose order confirmation this process missed. Record the
                # implied sell before adopting the broker's new qty below,
                # same principle as RECOVERED_TO_FLAT just below.
                _record_reconcile_discovered_sell(
                    broker, state, symbol=runtime["symbol"], sold_qty=old_qty - new_qty,
                    entry_price=float(runtime["avg_price"] or 0.0),
                    position_before=old_qty, position_after=new_qty,
                    now=now, exit_reason=RECOVERED_QTY_MISMATCH,
                )
            elif new_qty > old_qty:
                _record_reconcile_discovered_buy_delta(
                    state,
                    symbol=runtime["symbol"],
                    bought_qty=new_qty - old_qty,
                    avg_price=float(broker_row.get("avg_price") or runtime["avg_price"] or 0.0),
                    position_before=old_qty,
                    position_after=new_qty,
                    now=now,
                )
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
            # 2026-08-28 real incident fix: this branch used to adopt "flat"
            # with zero execution-ledger trace of the SELL that must have
            # happened to empty a real held position -- see
            # _record_reconcile_discovered_sell's own docstring for the full
            # incident (a MACD2 TP1 partial exit's real leg landed as an
            # unpriced BROKER_DIRECT stub, and the remaining shares'
            # eventual full exit left NO row at all, silently swallowed
            # right here).
            _record_reconcile_discovered_sell(
                broker, state, symbol=runtime["symbol"], sold_qty=int(runtime["qty"]),
                entry_price=float(runtime["avg_price"] or 0.0),
                position_before=int(runtime["qty"]), position_after=0,
                now=now, exit_reason=RECOVERED_TO_FLAT,
            )
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
        # A broker-discovered position has no verified TP1 sell leg in this
        # process. Never carry a stale ladder stage into the adopted position;
        # the normal TW adoption path will tag it active and seed peak return
        # on the next risk-management pass.
        state.time_window_position_active = False
        state.time_window_tp1_done = False
        state.time_window_peak_net_return = 0.0
        state.time_window_initial_quantity = 0
        # 조기익절 필터의 포지션 종속 상태도 같은 수명으로 초기화한다
        # (early_take_profit.py / models.py의 필드 주석 참고).
        state.time_window_entry_chop = False
        state.early_tp_peak_net_return = 0.0
        # 2026-08-28 fix: a reconcile-discovered position is a genuinely new
        # real entry this process never counted anywhere else -- the OTHER
        # contributor to daily_total_entry_count (worker._apply_switch_
        # outcome's EXECUTED branch) cannot double-count it, because that
        # branch only ever runs for an entry THIS process itself dispatched,
        # and this branch is reached only when state.position was already
        # None (i.e. nothing else already counted it this session).
        state.daily_total_entry_count = int(state.daily_total_entry_count or 0) + 1
        diag.update({"comparison_result": RECOVERED_FROM_BROKER, "mismatch_reason": "runtime_flat_broker_position"})
        state.position_reconcile_diag = diag
        state.last_position_reconcile_at = now.isoformat()
        # 2026-08-20 fix (real incident: the runtime believed it was flat but
        # the broker actually held a position -- this branch silently adopted
        # it into state.position with no signal-ledger row at all, so there
        # was NO record anywhere of when/how this position came to exist.
        # Bypasses _record_signal_ledger entirely since it needs a macd_snap
        # this reconcile step never has -- write a minimal, clearly-labeled
        # discovery row directly instead, at minimum making it visible/
        # auditable going forward.
        _record_reconcile_discovered_position(state, state.position, now)
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
    if outcome.final_state == SignalState.FAILED:
        # 2026-08-25 fix (real incident: a BUY reported FAILED (buy_result.
        # success=False) had actually filled at the broker under KIS
        # mock-mode rate-limit/latency pressure -- state.position stayed
        # None/flat here, so this position had ZERO risk management
        # (no stop-loss/TW2 ladder) until the next PERIODIC
        # reconcile_position_state() call (up to FLAT_POSITION_RECONCILE_
        # INTERVAL_SEC=30s later) discovered the mismatch via
        # RECOVERED_FROM_BROKER. A FAILED order result is exactly what
        # reconcile_position_state exists to catch -- force it immediately
        # instead of waiting for the periodic timer, so a real fill is
        # adopted into state.position (and folded into TW2 management on
        # the very next tick) within one cycle instead of up to 30s later.
        post_failure_reconcile = reconcile_position_state(broker, state, now, force=True)
        result.signal_dispatch_trace["post_failure_reconcile"] = post_failure_reconcile
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


def _advance_confirmed_primary(state: RuntimeState, macd_snap, now: datetime) -> Direction:
    """Primary (order-authoritative) crossover — completed 3m bars ONLY
    (docs 2026-07-27 KIS-parity fix; restored 2026-08-03 to the known-good
    zero-line-crossing rule from commit 6a2fd07 — see docs/MACD2_LOGIC.md
    for the git-archaeology writeup. The 2026-07-31 color+regime/debounce
    rewrite that briefly replaced this was found to under-detect real KIS
    flags by ~85% and is removed; do not reintroduce color-state/regime/
    pending debounce here): previous_diff/current_diff come solely from
    calculate_macd(bars_3m), the same confirmed MACD(12,26,9) KIS itself
    charts a flag on for a completed bar. Evaluated exactly once per new
    completed-bar timestamp — a repeat tick against the same bar_dt is
    always HOLD here, regardless of direction.

    2026-08-18 fix: this used to force HOLD (baseline-only, never dispatch)
    on the first completed bar evaluated on a new CALENDAR DATE relative to
    the PREVIOUSLY EVALUATED bar, on the theory that any such zero-crossing
    is always an overnight-gap artifact rather than a genuine reversal. Real
    KIS has no such "trading day" concept at all — it is one continuous
    EMA/MACD line — so a large genuine overnight-gap crossing DOES show up
    as a real flag on KIS (verified against the user's own KIS chart read on
    2026-08-18: a +5.53% gap produced a real 09:00 UP_RED flag that this
    gate silently swallowed; confirmed against the 2026-08-03 golden day too
    — the narrower replacement check below does not change that day's
    14-flag count, since 08-03 had no bar-1 crossing to begin with).

    That gate conflated two different things and only one of them should
    still block dispatch:
      - genuinely stale data: ``macd_snap`` is still anchored to a PRIOR
        calendar date's last bar because today's own first bar hasn't
        completed yet (e.g. 09:00:00-09:02:59, before any of today's 3m
        bars exist) — dispatching off that would trade on yesterday's
        close, not today's market. This is a real trading-day-boundary
        risk and is still blocked, now checked directly against ``now``
        (the actual current tick time) rather than against whatever bar
        this particular state object last happened to evaluate.
      - a genuine same-day reversal that merely happens to be the first
        bar this state has evaluated today — this is exactly the case the
        old gate wrongly swallowed and is now allowed to dispatch like any
        other bar.
      A defense-in-depth twin of the same idea also blocks a bar that
      technically hasn't closed yet as of ``now`` (bar_dt + 3min > now) —
      this can't happen via the real resample_completed_3m -> calculate_macd
      pipeline (it only ever returns bars already closed by ``now``), but
      costs nothing to guard directly here too.

    2026-08-20 NXT fix: ``state.last_detected_direction`` is now NO LONGER
    reset on day rollover (``_apply_day_rollover``). Once WATCH_SYMBOL's 1m
    history became a single continuous NXT-inclusive series (market_data.py
    market_div="NX", 08:00-20:00 every day, no J-only 09:00-15:30 gap), a
    calendar-date change stopped being a real discontinuity in the MACD/
    Signal relationship — KIS itself has no such boundary, per the note
    above. Resetting this at midnight used to be a harmless-looking safety
    net (it only ever suppressed a stale same-direction repeat), but it
    actively broke the "a still-held direction survives the date change
    without being re-announced as a new flag" requirement: e.g. an 08:45
    BLUE crossover must still read as BLUE at 09:00 with no new event, not
    get treated as directionless just because ``session_date`` ticked over.
    The staleness gate directly above (bar_kst.date() != now_kst.date()) is
    unrelated and unchanged — it blocks dispatch only while today's own
    first bar genuinely hasn't completed yet, not because of any rollover
    reset. (MU_MACD's own worker.py never had the old blanket gate to begin
    with — see app/trading/mu_macd/worker.py's run_once — and is out of
    scope for this NXT fix, since MU_MACD trades US-listed Micron where
    Korean NXT does not apply.)
    """
    bar_key = macd_snap.bar_dt.isoformat()
    if state.last_confirmed_bar_ts == bar_key:
        return Direction.HOLD
    state.last_confirmed_bar_ts = bar_key
    now_kst = now.astimezone(KST)
    bar_kst = macd_snap.bar_dt.astimezone(KST)
    if bar_kst.date() != now_kst.date() or bar_kst + timedelta(minutes=3) > now_kst:
        return Direction.HOLD
    # Order-authoritative FLAG source is fixed to zero-cross onset. KIS
    # color/onset may be displayed as reference only and must not replace
    # this calculation without a fresh production-change decision.
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
    state.time_window_filter_version = (
        config.TIME_WINDOW_TEG_FILTER_VERSION if state.time_window_teg_filter_enabled else config.TIME_WINDOW_2_FILTER_VERSION
    )
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
    that later bar (TW2 and the TEG filter both layer two more veto checks
    on top there — see time_window_filter.evaluate_tw2_extra_vetoes). Never
    called unless the TEG filter (state.time_window_teg_filter_enabled) or
    TW2 (state.time_window_2_filter_enabled) is on; never creates or
    suppresses the confirmed flag itself, and never touches STOP_LOSS/
    OPPOSITE_SIGNAL/FORCED_LIQUIDATION.

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
        (state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled or state.time_window_3slot_filter_enabled)
        and state.position is not None and state.position.symbol == pos.symbol
        and current_price is not None
    ):
        if not state.time_window_position_active:
            # 2026-08-21 fix (real incident: a position bought through the
            # 09:03 예약매수 button sat 3%+ in profit for 20+ minutes with
            # ZERO take-profit/stop-loss management, because
            # _execute_scheduled_entry never tagged it as a time-window
            # position -- this whole block's outer condition used to require
            # time_window_position_active already True, so it was silently
            # skipped entirely for that position on every single tick).
            # Whenever the TW filter is enabled, ANY currently-held position
            # for the traded symbol is adopted into its ladder right here,
            # regardless of which entry path opened it -- the filter's own
            # purpose (§11) is to fully own position management while ON,
            # not just for positions it happens to have opened itself.
            # peak_net_return seeds from THIS tick's return (not 0.0) so an
            # already-elevated position isn't treated as if it just broke
            # even -- see evaluate_morning_position's own peak-tracking use.
            state.time_window_position_active = True
            state.time_window_active_mode = state.time_window_active_mode or (
                "TW2_3SLOT" if state.time_window_3slot_filter_enabled
                else ("TEGv2" if state.time_window_teg_filter_enabled else "TW2")
            )
            session = state.time_window_entry_session or time_window_filter.session_for_window(
                time_window_filter.classify_window(now.astimezone(KST).time())
            )
            state.time_window_entry_session = session
            state.time_window_tp1_done = False
            seed_return = _net_return_pct(pos.symbol, pos.avg_price, current_price, pos.quantity)
            state.time_window_peak_net_return = max(float(state.time_window_peak_net_return or 0.0), seed_return)
            state.time_window_initial_quantity = state.time_window_initial_quantity or pos.quantity
            # 조기익절 필터는 "진입 확정봉이 CHOP이었는가"가 필요한데, 이
            # 입양 경로는 정의상 TW2 3-SLOT의 T+3 확정 진입이 아니다(예약매수
            # 버튼/브로커 발견/BUY_FAILED 오보고 복구). 진입 시점 판정을 소급
            # 계산할 방법이 없으므로 CHOP 아님(=필터 미적용)으로 고정한다.
            state.time_window_entry_chop = False
            state.early_tp_peak_net_return = max(
                float(state.early_tp_peak_net_return or 0.0), seed_return,
            )
            # 2026-08-25 fix (real incident: a BUY that actually filled but
            # was reported BUY_FAILED, later discovered via
            # reconcile_position_state's RECOVERED_FROM_BROKER, reaches this
            # adoption path instead of _resolve_time_window_candidate's own
            # EXECUTED branch -- the ONLY place that normally increments
            # time_window_morning_entry_count/time_window_afternoon_entry_
            # count. _execute_scheduled_entry (09:03 button) has the exact
            # same gap for the same reason: it also relies entirely on this
            # shared adoption path and never increments the counter itself.
            # Both left the session's entry cap (MAX_MORNING_ENTRIES/
            # MAX_AFTERNOON_ENTRIES) silently under-counting a real entry.
            # Safe to increment exactly once here: this whole branch is
            # already gated on `not state.time_window_position_active`, and
            # every path that DOES increment the counter itself
            # (_resolve_time_window_candidate's/_execute_premarket_carry_
            # entry's own EXECUTED branches) sets that same flag True the
            # same tick it increments -- so a position counted there can
            # never re-enter this branch and double-count, and repeated
            # reconcile ticks for the SAME position only ever reach this
            # branch once (the first tick after discovery, before this
            # branch flips the flag to True).
            if state.time_window_active_mode == "TW2_3SLOT":
                # TW2 3-SLOT keeps its own separate slot/session counters
                # (never TW2/TEG's time_window_morning_entry_count/
                # afternoon_entry_count) -- same adoption-path gap this
                # whole branch exists to close, just for this mode's own
                # bookkeeping.
                state.tw2_3slot_slots_used_today = int(state.tw2_3slot_slots_used_today or 0) + 1
                if session == "MORNING":
                    state.tw2_3slot_morning_count = int(state.tw2_3slot_morning_count or 0) + 1
                elif session == "AFTERNOON":
                    state.tw2_3slot_afternoon_count = int(state.tw2_3slot_afternoon_count or 0) + 1
                    state.tw2_3slot_last_afternoon_direction = _position_direction(pos).value if _position_direction(pos) else None
            elif session == "MORNING":
                state.time_window_morning_entry_count = int(state.time_window_morning_entry_count or 0) + 1
                state.time_window_entry_session_seq = state.time_window_morning_entry_count
            elif session == "AFTERNOON":
                state.time_window_afternoon_entry_count = int(state.time_window_afternoon_entry_count or 0) + 1
                state.time_window_entry_session_seq = state.time_window_afternoon_entry_count

        # 2026-08-21 fix (사용자 요청 — 익절판단은 3분봉 완성 시점이 아니라
        # 틱뜨자마자 즉시): TP1/TP2/AFTERNOON_TP alone are checked here on
        # EVERY tick against the live current_price, before the bar-close-
        # gated ladder below ever runs -- see evaluate_take_profit_immediate's
        # own docstring for why this is safe to do for take-profit but
        # deliberately NOT extended to STOP_LOSS/trailing-stop (unchanged,
        # still bar-close-gated below).
        tick_net_return = _net_return_pct(pos.symbol, pos.avg_price, current_price, pos.quantity)
        # 조기익절 필터 전용 MFE(틱 관측). production의 time_window_peak_net_
        # return은 손대지 않는다 -- 그 필드는 완성봉 종가 기준으로만 커밋되고
        # (바로 아래 tick TP 경로는 청산이 실제 발동할 때만 커밋한다), 그
        # 커밋 시점을 바꾸면 필터 OFF 동작이 달라진다. armed 판정에 쓰는 MFE는
        # 60일 검증에서 틱 관측값이었으므로 별도 필드로 같은 방식으로 쌓는다.
        if early_take_profit.is_active(state):
            state.early_tp_peak_net_return = max(
                float(state.early_tp_peak_net_return or 0.0), tick_net_return,
            )
        tp_decision = time_window_position_manager.evaluate_take_profit_immediate(
            session=state.time_window_entry_session or "MORNING",
            net_return_pct=tick_net_return,
            tp1_done=bool(state.time_window_tp1_done),
            tp2_pct_override=(config.TW2_MORNING_TP2 * 100.0) if state.time_window_active_mode in ("TW2", "TEG", "TEGv2", "TW2_3SLOT") else None,
        )
        if tp_decision.exit_reason is not None:
            # 2026-08-27 fix (real incident: a premarket-carry position's
            # partial-exit order FAILED at the broker, but tp1_done had
            # already been committed True just above -- the position was
            # then governed by the tightened post-TP1 ladder
            # (MORNING_AFTER_TP1_STOP=+0.3%) instead of the correct pre-TP1
            # -1.7% stop-loss/3% TP1 threshold, so a nearly-flat +0.157%
            # tick was enough to trigger a full exit minutes later). peak_
            # net_return tracking is harmless/independent of whether an
            # order actually filled (it only ever tracks the best return
            # SEEN, not anything about position state), so it still commits
            # unconditionally -- but tp1_done must only ever flip once the
            # corresponding order is CONFIRMED EXECUTED; on a failed/blocked
            # attempt the position must stay governed by whatever ladder
            # stage it was actually in before this tick, so a retry next
            # tick is judged against the correct threshold again.
            state.time_window_peak_net_return = max(float(state.time_window_peak_net_return or 0.0), tp_decision.peak_net_return)
            sell_fraction = max(0.0, min(1.0, tp_decision.sell_fraction))
            if sell_fraction >= 1.0:
                outcome = order_executor.execute_exit(
                    broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                    exit_reason=tp_decision.exit_reason, entry_price=pos.avg_price,
                    reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                )
                _apply_exit_outcome(state, outcome)
                if outcome.final_state == SignalState.EXECUTED:
                    state.time_window_position_active = False
                    state.time_window_tp1_done = tp_decision.tp1_done
                result.actions.append(f"{tp_decision.exit_reason}:{pos.symbol}")
                return True
            sell_qty = min(pos.quantity - 1, max(1, round(pos.quantity * sell_fraction)))
            remaining_qty = pos.quantity - sell_qty
            outcome = order_executor.execute_partial_exit(
                broker=broker, symbol=pos.symbol, sell_qty=sell_qty, remaining_qty=remaining_qty,
                exit_reason=tp_decision.exit_reason, entry_price=pos.avg_price,
                reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
            )
            if outcome.final_state == SignalState.EXECUTED:
                state.position = dataclasses.replace(state.position, quantity=remaining_qty)
                state.time_window_tp1_done = tp_decision.tp1_done
            result.actions.append(f"{tp_decision.exit_reason}:{pos.symbol}")
            return True

        # This position was opened by (or just adopted into) the time-window
        # filter — its own position-management ladder (§11-14) fully
        # replaces the legacy STOP_LOSS check below for as long as it is
        # held (OPPOSITE_SIGNAL is instead handled by
        # _resolve_time_window_candidate, further down in run_once() once
        # macd_snap is ready). Take-profit was already handled immediately
        # above; only STOP_LOSS/trailing-stop outcomes are still possible
        # from here on, and those remain bar-close-gated on purpose.
        completed_bar_close = _advance_stop_loss_bar(state, pos.symbol, current_price, now)
        if completed_bar_close is not None:
            bar_net_return = _net_return_pct(pos.symbol, pos.avg_price, completed_bar_close, pos.quantity)
            pm_decision = time_window_position_manager.evaluate_position(
                session=state.time_window_entry_session or "MORNING",
                net_return_pct=bar_net_return,
                tp1_done=bool(state.time_window_tp1_done),
                peak_net_return=float(state.time_window_peak_net_return or 0.0),
                tp2_pct_override=(config.TW2_MORNING_TP2 * 100.0) if state.time_window_active_mode in ("TW2", "TEG", "TEGv2", "TW2_3SLOT") else None,
            )
            # 2026-08-27 fix -- same reasoning as the immediate-tick TP path
            # just above: peak_net_return still commits unconditionally
            # (harmless), but tp1_done must only flip once the corresponding
            # order is CONFIRMED EXECUTED, never just because the DECISION
            # said a threshold was crossed.
            state.time_window_peak_net_return = pm_decision.peak_net_return
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
                        state.time_window_tp1_done = pm_decision.tp1_done
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
                    state.time_window_tp1_done = pm_decision.tp1_done
                result.actions.append(f"{pm_decision.exit_reason}:{pos.symbol}")
                return True

            # ── 조기익절 필터 (기본 OFF, TW2 3-SLOT 전용) ──────────────────
            # 여기까지 왔다는 것은 production 래더(TP1/TP2/오후TP는 위 틱
            # 경로에서, 손절/after-TP1-stop/trailing은 바로 위 완성봉 경로에서)가
            # 아무 청산도 내지 않았다는 뜻이다 -- 즉 기존 청산이 항상 우선하고,
            # 이 필터는 production이 HOLD라고 답한 뒤에만 발언한다. 그래서
            # 실효 스탑이 max(production 활성 스탑, EARLY_TP_FLOOR_PCT)가 되고
            # TP1/TP2/trailing은 그대로 살아 있다.
            # 다른 모든 하방 rung과 마찬가지로 완성봉 종가(bar_net_return)
            # 기준이다 -- 노이즈 틱 하나로 스탑을 때리지 않는 기존 설계 유지.
            if early_take_profit.is_active(state):
                early_tp = early_take_profit.evaluate(
                    entry_chop=bool(state.time_window_entry_chop),
                    peak_net_return_pct=float(state.early_tp_peak_net_return or 0.0),
                    net_return_pct=bar_net_return,
                )
                if early_tp.armed and not state.last_early_tp_armed_at:
                    state.last_early_tp_armed_at = now.isoformat()
                if early_tp.exit_reason is not None:
                    outcome = order_executor.execute_exit(
                        broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                        exit_reason=early_tp.exit_reason, entry_price=pos.avg_price,
                        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                    )
                    _apply_exit_outcome(state, outcome)
                    if outcome.final_state == SignalState.EXECUTED:
                        state.time_window_position_active = False
                        state.last_early_tp_fired_at = now.isoformat()
                    result.actions.append(f"{early_tp.exit_reason}:{pos.symbol}")
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


def _handle_resolve_exception(
    *,
    state: RuntimeState,
    macd_snap,
    direction: Direction,
    signal_id: str,
    signal_type: str,
    gate_mode: str,
    exc: Exception,
) -> None:
    """2026-09-03 real incident: a T+3 candidate whose resolution
    (_resolve_time_window_candidate_body / _resolve_tw2_3slot_candidate_
    body) raised partway through used to vanish with ZERO trace -- by the
    point either function reaches its own decision-computing body, the
    pending-candidate fields are already cleared in memory (so the next
    tick never retries it), nothing had been written to the signal ledger
    yet (that only happens once a decision object exists), and the only
    place the exception itself landed -- Worker._last_exception -- gets
    silently wiped clean by the very next successful tick. Four real flags
    (10:06/10:51/11:18/11:21) each showed only their own initial "pending"
    ledger row and nothing after, with "최근 block/skip 사유" stuck on
    TIME_WINDOW_PENDING_CONFIRMATION for hours -- exactly what this
    produces if left uncaught.

    This is the caller's except-clause handler: writes an explicit
    RESOLVE_ERROR row to the signal ledger (so the failure is visible in
    신호원장 itself, not just an ops-only log line), and sets state.
    last_resolve_error/last_resolve_error_at -- persisted to disk like every
    other state field and NEVER auto-cleared by a later successful tick
    (unlike Worker._last_exception) -- so it survives long enough to
    diagnose. The candidate itself is treated as consumed (its signal_id is
    marked processed) rather than retried indefinitely, since if the
    underlying cause is a persistent data condition (e.g. a bar-history
    gap), an unbounded retry would just raise again every single tick
    forever."""
    tb = traceback.format_exc()
    logger.error(f"[MACD2] {gate_mode} T+3 resolve raised for signal_id={signal_id}: {tb}")
    error_text = f"{type(exc).__name__}: {exc}"[:500]
    now_iso = datetime.now(KST).isoformat()
    state.last_resolve_error = f"{gate_mode}:{signal_id}: {error_text}"
    state.last_resolve_error_at = now_iso
    state.order_block_reason = config.TW_RESOLVE_ERROR
    if signal_id not in state.processed_signal_ids:
        state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
    dispatch_trace = {
        "signal_id": signal_id, "direction": direction.value, "signal_type": signal_type,
        "completed_bar_at": macd_snap.bar_dt.isoformat(),
        "order_executor_called": False, "broker_called": False,
        "final_block_reason": error_text,
        "order_result_override": config.TW_RESOLVE_ERROR,
    }
    outcome = order_executor.ExecutionOutcome(
        signal_id=signal_id, direction=direction,
        target_symbol=order_executor.target_symbol_for_direction(direction),
        final_state=SignalState.BLOCKED, block_reason=error_text,
    )
    _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, datetime.now(KST), outcome, dispatch_trace)


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
    Never called unless TW2 is on. TEGv2 is an optional TW2 sub-filter; it
    evaluates TW2 candidates rejected solely for the daily entry-count cap
    or (2026-08-27) solely for the TW_MORNING_ONLY afternoon time-window
    block -- see the bypass block below for the exact scope.
    """
    if not (state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled) or not state.time_window_pending_flag_direction:
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

    try:
        return _resolve_time_window_candidate_body(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
            bars_3m=bars_3m, df_1m=df_1m, position=position, result=result,
            direction=direction, signal_id=signal_id, flag_bar_dt=flag_bar_dt,
        )
    except Exception as exc:
        _handle_resolve_exception(
            state=state, macd_snap=macd_snap, direction=direction, signal_id=signal_id,
            signal_type="TIME_WINDOW_CONFIRM", gate_mode="TIME_WINDOW", exc=exc,
        )
        return None


def _resolve_time_window_candidate_body(
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
    direction: Direction,
    signal_id: str,
    flag_bar_dt: datetime,
):
    """Extracted from _resolve_time_window_candidate (2026-09-03 real
    incident fix) so the caller can wrap this decision-computing portion in
    a try/except -- see _handle_resolve_exception's docstring for the
    incident this fixes (a T+3 candidate silently vanishing with zero
    signal-ledger trace if anything in here ever raised). Pure continuation
    of the parent function; never meant to be called from anywhere else."""
    # bars_3m must end EXACTLY one completed bar after flag_bar_dt for
    # evaluate_time_window_entry to accept it (its own T+3 confirmation
    # contract) -- a multi-bar gap (e.g. the Worker was down) means this
    # candidate has expired; drop it rather than confirm off stale bars.
    decision = time_window_filter.evaluate_time_window_entry(
        bars_3m, direction, flag_bar_dt, now,
        position_direction=_position_direction(position),
        morning_entry_count=int(state.time_window_morning_entry_count or 0),
        afternoon_entry_count=int(state.time_window_afternoon_entry_count or 0),
        # 2026-08-28 fix: the daily-cap check must see every real entry
        # today, not just TW2/TEG-gated ones (see evaluate_time_window_
        # entry's own daily_entry_count docstring and RuntimeState.
        # daily_total_entry_count's docstring for the real incident).
        daily_entry_count=int(state.daily_total_entry_count or 0),
    )
    if decision.approved and (state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled):
        # TW2 (2026-08-21 사용자 요청): two extra vetoes layered on top of the
        # SAME base TW gate above -- see config.py's TIME_WINDOW_2_FILTER_
        # DEFAULT docstring for the 29-day TRAIN/OOS validation. Only ever
        # tightens an approval into a rejection; never overrides a genuine
        # base-gate rejection into an approval. The TEG filter (2026-08-27)
        # reuses these SAME two vetoes -- its entry gating is byte-identical
        # to TW2's in every respect except the count-cap bypass below.
        vetoed, veto_reason = time_window_filter.evaluate_tw2_extra_vetoes(bars_3m, direction, flag_bar_dt, now)
        if vetoed:
            decision = dataclasses.replace(decision, approved=False, decision=veto_reason, block_reason=veto_reason)

    # TEG bypass (2026-08-27 사용자 요청; 일일 진입횟수 초과 케이스는 TRAIN/OOS
    # backtest로 validated -- see config.py's TIME_WINDOW_TEG_FILTER_DEFAULT
    # docstring). 2026-08-27 추가 확장(사용자 요청, 별도 backtest 검증 없음):
    # TW_MORNING_ONLY(config.py)로 오후(13:00-15:00) 신규진입이 시간대 자체에서
    # 막힌 경우도 동일하게 하루 1회 우회 대상에 포함한다 -- decision.metrics
    # ["window"]가 실제 오후 window(W5/W6)로 분류됐고(즉 window=None이나
    # 10:50-13:00 W4처럼 애초에 거래일/윈도우 자체가 무효였던 경우는 제외) 아직
    # TW_AFTERNOON_ENTRY_HARD_CUTOFF(14:57) 전이라 T+3 confirmation을 15:00
    # 전에 마칠 여지가 있는 경우만 해당. 다른 REJECT 사유(DUPLICATE_POSITION/
    # SHORT_FLAG_INTERVAL/NO_RESET/LOW_QUALITY_SCORE/extra veto 등)는 이 우회
    # 대상이 아니다 -- 그대로 유지.
    # 공통 조건: TEG 필터(TW2 아님)가 켜져 있고, extra veto가 이 후보를 걸지
    # 않으며, 오늘 ONE 우회를 아직 안 썼을 것(capped at exactly 1/day).
    window_blocked_by_morning_only = (
        decision.block_reason == config.TW_REJECT_TIME_WINDOW
        and decision.metrics.get("window") in (
            time_window_filter.WINDOW_AFTERNOON_1, time_window_filter.WINDOW_AFTERNOON_2,
        )
        and now.astimezone(config.KST).time() < config.TW_AFTERNOON_ENTRY_HARD_CUTOFF
    )
    if (
        not decision.approved
        and state.time_window_teg_filter_enabled
        and (decision.block_reason == config.TW_REJECT_MAX_ENTRY_COUNT or window_blocked_by_morning_only)
        and not state.time_window_teg_count_cap_bypass_used
    ):
        vetoed, _veto_reason = time_window_filter.evaluate_tw2_extra_vetoes(bars_3m, direction, flag_bar_dt, now)
        if not vetoed:
            teg_decision = teg_gate.evaluate_teg(bars_3m, direction, flag_bar_dt, now)
            state.last_time_window_teg_candidate_at = datetime.now(KST).isoformat()
            state.last_time_window_teg_approved = bool(teg_decision.approved)
            state.last_time_window_teg_reject_reasons = list(teg_decision.reject_reasons or [])
            state.last_time_window_teg_metrics = dict(teg_decision.metrics or {})
            state.last_time_window_teg_conditions = dict(teg_decision.conditions or {})
            if teg_decision.approved:
                decision = dataclasses.replace(
                    decision, approved=True, decision=config.TW_TEG_COUNT_CAP_BYPASS, block_reason=None,
                )
                state.time_window_teg_count_cap_bypass_used = True
                state.last_time_window_teg_bypass_at = datetime.now(KST).isoformat()
    _persist_time_window_decision(state, decision, signal_id)

    # Optional "탈락 DOWN_BLUE 예외진입" (2026-08-18) -- see config.py's
    # TW_DOWN_BLUE_EXCEPTION_FILTER_DEFAULT docstring for the backtest
    # rationale. A DOWN_BLUE candidate the real TW gate above just rejected
    # (for ANY reason) still gets exactly one extra entry per trading day,
    # no other condition -- but never while a position is already open
    # (never overrides/switches an existing TW-managed position; that stays
    # governed by the real gate only).
    down_blue_exception_applied = (
        not decision.approved
        and state.down_blue_exception_filter_enabled
        and direction == Direction.DOWN_BLUE
        and not state.daily_down_blue_exception_used
        and position is None
    )

    if not decision.approved and not down_blue_exception_applied:
        target_symbol = order_executor.target_symbol_for_direction(direction)
        if position is not None and position.symbol != target_symbol:
            # 2026-08-19 real incident fix: a genuine opposite flag the real
            # TW gate just rejected used to leave the held position
            # completely untouched here -- neither switched NOR explicitly
            # liquidated, contradicting this exact function's own docstring
            # ("the held position stays untouched until
            # _resolve_time_window_candidate ... decides to switch or
            # hold") and _judge_time_window_flag's ("must stay untouched
            # UNTIL _resolve_time_window_candidate resolves the candidate at
            # T+3") -- both assume THIS function makes a real decision on
            # reject, not a silent no-op. Every OTHER optional filter
            # (MAJOR/SIDEWAYS/TREND_PERSISTENCE/SINGLE_ENTRY) already always
            # sells the held position on a rejected reversal via
            # _execute_reversal_exit_only_for_filtered_entry.
            #
            # 2026-08-19 "휩쏘-내성" T+3 재확인 (사용자 요청, 56일 TRAIN/VAL/
            # OOS 백테스트로 검증 -- scripts/tw_gate_relaxed_optimization.py
            # 계열): decision.block_reason이 config.TW_WHIPSAW_REJECT_REASONS
            # (MACD/Signal 관계가 T+3에도 유지 안 됨, 또는 gap이 확대 안 됨)에
            # 속하면 -- 즉 원래 방향으로 도로 복귀한 휩쏘로 판단되면 -- 매도하지
            # 않고 보유 포지션을 그대로 둔다(정상 TP1/TP2/-1.7% 손절 래더는
            # _advance_held_position_risk_management에서 이 로직과 무관하게
            # 매 tick 계속 평가됨). 그 외 사유(품질점수/시간대/최대진입횟수/
            # 중복포지션)는 기존과 동일하게 무조건 매도 -- 재진입 여부만 게이트가
            # 계속 판단하고, 매도 자체는 그 사유들에 좌우되지 않는다.
            if decision.block_reason in config.TW_WHIPSAW_REJECT_REASONS:
                state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
                whipsaw_trace = {
                    "signal_id": signal_id, "direction": direction.value, "signal_type": "TIME_WINDOW_CONFIRM",
                    "completed_bar_at": macd_snap.bar_dt.isoformat(),
                    "order_executor_called": False, "broker_called": False,
                    "final_block_reason": decision.block_reason or decision.decision or "",
                    "order_result_override": "TIME_WINDOW_WHIPSAW_HOLD",
                    "major_fields": _entry_gate_ledger_fields(state, decision, "TIME_WINDOW"),
                }
                whipsaw_outcome = order_executor.ExecutionOutcome(
                    signal_id=signal_id, direction=direction, target_symbol=target_symbol,
                    final_state=SignalState.BLOCKED, block_reason=decision.block_reason or decision.decision,
                )
                _record_signal_ledger(
                    state, macd_snap, direction, "TIME_WINDOW_CONFIRM", signal_id, datetime.now(KST),
                    whipsaw_outcome, whipsaw_trace,
                )
                result.actions.append(f"TIME_WINDOW_WHIPSAW_HOLD:{direction.value}")
                # 2026-09-03 real incident fix: this T+3 rejection branch never
                # updated state.order_block_reason, so the UI's "최근 block/skip
                # 사유" quick-look line stayed frozen on whatever the FLAG bar's
                # own _record_major_filtered_signal call set it to (always
                # TW_PENDING_CONFIRMATION) -- the real T+3 outcome was only ever
                # visible in the full signal-ledger CSV's block_reason column,
                # never in this single-line summary. Every other reject/exit
                # path in this file (_record_major_filtered_signal,
                # _execute_or_wait, _apply_exit_outcome, etc.) already updates
                # this field on its own outcome; this call brings the T+3
                # whipsaw-hold branch in line with that existing convention.
                state.order_block_reason = decision.block_reason or decision.decision
                _start_whipsaw_watch(state, mode="TW2", direction=direction, bars_3m=bars_3m, flag_bar_dt=macd_snap.bar_dt, now=now)
                return None
            outcome = _execute_reversal_exit_only_for_filtered_entry(
                broker=broker, state=state, macd_snap=macd_snap, direction=direction,
                position=position, decision=decision, result=result, gate_mode="TIME_WINDOW",
                signal_id_override=signal_id,
            )
            if outcome is not None:
                _apply_exit_outcome(state, outcome)
            return outcome
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
            target_symbol=target_symbol,
            final_state=SignalState.BLOCKED, block_reason=decision.block_reason or decision.decision,
        )
        _record_signal_ledger(
            state, macd_snap, direction, "TIME_WINDOW_CONFIRM", signal_id, datetime.now(KST), outcome, dispatch_trace,
        )
        result.actions.append(f"{config.FILTERED_OUT}:{direction.value}")
        # 2026-09-03 real incident fix: see the whipsaw-hold branch above for
        # why this must be set here too -- without it, a T+3 candidate that
        # gets rejected (quality score/veto/max-entry-count/etc.) with no
        # position to liquidate leaves the UI's "최근 block/skip 사유" line
        # stuck on the flag bar's own TW_PENDING_CONFIRMATION forever.
        state.order_block_reason = decision.block_reason or decision.decision
        return None

    if down_blue_exception_applied:
        state.daily_down_blue_exception_used = True
        state.last_down_blue_exception_at = datetime.now(KST).isoformat()
        result.actions.append(f"{config.TW_EXCEPTION_DOWN_BLUE_ENTRY}:{direction.value}")

    signal_detected_at = datetime.now(KST)
    result.signal_detected_at = signal_detected_at.isoformat()
    signal_type = "REVERSAL" if (position is not None and position.quantity > 0) else "INITIAL"
    outcome = _execute_or_wait(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        direction=direction, signal_id=signal_id, signal_type=signal_type, position=position, result=result,
        signal_detected_at=signal_detected_at,
    )
    result.signal_dispatch_trace["major_fields"] = _entry_gate_ledger_fields(
        state, decision, "TIME_WINDOW", down_blue_exception_applied=down_blue_exception_applied,
    )
    if outcome is None and result.skipped == config.MISSED_SIGNAL_QUOTE_STALE:
        state.time_window_pending_flag_direction = direction
        state.time_window_pending_flag_bar_ts = flag_bar_dt.isoformat()
    _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, signal_detected_at, outcome, result.signal_dispatch_trace)

    if outcome is not None and outcome.final_state == SignalState.EXECUTED:
        # _apply_switch_outcome is the SAME function every other entry/switch
        # path uses to actually set state.position on a fill (docs: no
        # duplicated position-adoption logic) -- it also registers
        # outcome.signal_id in processed_signal_ids, so this candidate's
        # signal_id is not separately appended here.
        _apply_switch_outcome(state, outcome, direction, now)
        window = decision.metrics.get("window") if decision.metrics else None
        if window is None:
            # A rejected decision (down_blue_exception_applied path) may not
            # have classified a window at all -- e.g. an early reject like
            # macd_signal_not_held short-circuits before window lookup.
            window = time_window_filter.classify_window(macd_snap.bar_dt.astimezone(KST).time())
        session = time_window_filter.session_for_window(window)
        state.time_window_position_active = True
        state.time_window_active_mode = "TEGv2" if decision.decision == config.TW_TEG_COUNT_CAP_BYPASS else "TW2"
        state.time_window_entry_session = session
        state.time_window_tp1_done = False
        state.time_window_peak_net_return = 0.0
        # 조기익절 필터의 포지션 종속 상태도 같은 수명으로 초기화한다
        # (early_take_profit.py / models.py의 필드 주석 참고).
        state.time_window_entry_chop = False
        state.early_tp_peak_net_return = 0.0
        state.time_window_initial_quantity = outcome.quantity
        state.last_time_window_entry_at = signal_detected_at.isoformat()
        if session == "MORNING":
            state.time_window_morning_entry_count = int(state.time_window_morning_entry_count or 0) + 1
            state.time_window_entry_session_seq = state.time_window_morning_entry_count
        elif session == "AFTERNOON":
            state.time_window_afternoon_entry_count = int(state.time_window_afternoon_entry_count or 0) + 1
            state.time_window_entry_session_seq = state.time_window_afternoon_entry_count
    return outcome


def _persist_tw2_3slot_decision(state: RuntimeState, decision: MajorFlagDecision, signal_id: str) -> None:
    state.time_window_3slot_filter_version = config.TW2_3SLOT_FILTER_VERSION
    metrics = dict(decision.metrics or {})
    state.last_tw2_3slot_approved = bool(decision.approved)
    state.last_tw2_3slot_decision = decision.decision
    state.last_tw2_3slot_block_reason = decision.block_reason
    state.last_tw2_3slot_slot_number = metrics.get("slot_number")
    state.last_tw2_3slot_session = metrics.get("session")
    state.last_tw2_3slot_quality_passed = metrics.get("quality_passed")
    state.last_tw2_3slot_quality_conditions = dict(metrics.get("quality_conditions") or {}) or None
    state.last_tw2_3slot_teg_approved = metrics.get("teg_approved")
    state.last_tw2_3slot_teg_reject_reasons = list(metrics.get("teg_reject_reasons") or [])
    state.last_tw2_3slot_signal_id = signal_id


def _judge_tw2_3slot_flag(
    *, state: RuntimeState, bars_3m, direction: Direction, signal_id: str,
) -> MajorFlagDecision:
    """TW2 3-SLOT's own T -> T+3 pending registration — exactly mirrors
    _judge_time_window_flag's shape (a flag never has order authority on its
    own bar; the real decision happens one bar later in
    _resolve_tw2_3slot_candidate), but writes to FULLY SEPARATE
    tw2_3slot_pending_flag_* state, never TW2/TEG's own time_window_pending_
    flag_* fields. Never called unless state.time_window_3slot_filter_
    enabled is True; never creates or suppresses the confirmed flag itself.

    IMPORTANT (mirrors _judge_time_window_flag's own docstring): a rejection
    here must NEVER trigger _execute_reversal_exit_only_for_filtered_entry's
    sell-only liquidation — the held position (if any) stays untouched until
    _resolve_tw2_3slot_candidate resolves the candidate at T+3. Callers gate
    that explicitly on gate_mode == "TW2_3SLOT".
    """
    flag_bar_dt = pd.Timestamp(bars_3m["datetime"].iloc[-1]).to_pydatetime()
    state.tw2_3slot_pending_flag_direction = direction
    state.tw2_3slot_pending_flag_bar_ts = flag_bar_dt.isoformat()
    decision = MajorFlagDecision(
        approved=False, score=0.0, required_score=0.0,
        decision=config.TW_PENDING_CONFIRMATION,
        reasons=("awaiting T+3 bar re-confirmation (TW2 3-SLOT)",),
        component_scores={}, metrics={"flag_bar_at": flag_bar_dt.isoformat()},
        is_reversal=False, fast_reversal=False, block_reason=config.TW_PENDING_CONFIRMATION,
    )
    _persist_tw2_3slot_decision(state, decision, signal_id)
    return decision


def _resolve_tw2_3slot_candidate(
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
    """Resolves a pending TW2 3-SLOT candidate (T -> T+3 wait), via the SAME
    shared time_window_filter.evaluate_time_window_entry()/evaluate_tw2_
    extra_vetoes() decision functions TW2 itself uses (no duplicated
    entry-condition logic) — entry-count params are always passed as
    0/0/0 so evaluate_time_window_entry's OWN 3/2/5 caps never fire; this
    function's own tw2_3slot_slots_used_today/morning_count/afternoon_count
    bookkeeping is the only cap enforced for this mode. On top of that base
    TW2 clearance, time_window_3slot.resolve_slot decides which extra gate
    (if any) this candidate's slot requires — morning 3rd slot:
    time_window_3slot.evaluate_trend_quality (>= config.TW2_3SLOT_MORNING_
    3RD_QUALITY_MIN of 5); afternoon slot: teg_gate.evaluate_teg (mandatory
    AND-gate, NOT production's once-daily count-cap-bypass mechanism).

    Whipsaw-tolerant T+3 OPPOSITE_SIGNAL reversal-exit classification
    (config.TW_WHIPSAW_REJECT_REASONS) and dispatch (_execute_or_wait /
    _execute_reversal_exit_only_for_filtered_entry) are reused byte-
    identical to TW2's own handling in _resolve_time_window_candidate.
    Never called unless state.time_window_3slot_filter_enabled is True.
    """
    if not state.time_window_3slot_filter_enabled or not state.tw2_3slot_pending_flag_direction:
        return None
    flag_bar_dt = _parse_iso_dt(state.tw2_3slot_pending_flag_bar_ts)
    if flag_bar_dt is None:
        state.tw2_3slot_pending_flag_direction = None
        state.tw2_3slot_pending_flag_bar_ts = None
        return None
    if macd_snap.bar_dt == flag_bar_dt:
        return None  # still sitting on the flag's own bar T -- wait for T+3

    direction = state.tw2_3slot_pending_flag_direction
    signal_id = f"{make_signal_id(flag_bar_dt, direction)}:TW2_3SLOT_CONFIRM"
    state.tw2_3slot_pending_flag_direction = None
    state.tw2_3slot_pending_flag_bar_ts = None
    if signal_id in state.processed_signal_ids:
        return None

    try:
        return _resolve_tw2_3slot_candidate_body(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
            bars_3m=bars_3m, df_1m=df_1m, position=position, result=result,
            direction=direction, signal_id=signal_id, flag_bar_dt=flag_bar_dt,
        )
    except Exception as exc:
        _handle_resolve_exception(
            state=state, macd_snap=macd_snap, direction=direction, signal_id=signal_id,
            signal_type="TW2_3SLOT_CONFIRM", gate_mode="TW2_3SLOT", exc=exc,
        )
        return None


def _resolve_tw2_3slot_candidate_body(
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
    direction: Direction,
    signal_id: str,
    flag_bar_dt: datetime,
):
    """Extracted from _resolve_tw2_3slot_candidate (2026-09-03 real incident
    fix) -- see _resolve_time_window_candidate_body's identical rationale
    (and _handle_resolve_exception's docstring) for why this decision-
    computing portion is split out so its caller can wrap it in a
    try/except. Pure continuation of the parent function; never meant to be
    called from anywhere else."""
    base_decision = time_window_filter.evaluate_time_window_entry(
        bars_3m, direction, flag_bar_dt, now,
        position_direction=_position_direction(position),
        morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0,
    )
    # TW2 3-SLOT never inherits TW_MORNING_ONLY's blanket afternoon block --
    # an afternoon candidate rejected SOLELY by that toggle is still
    # eligible for this mode's own mandatory TW2+TEGv2 dual-gate (mirrors,
    # byte-for-byte, the window_blocked_by_morning_only condition worker.py
    # already computes for the identical purpose inside
    # _resolve_time_window_candidate's TEG count-cap-bypass block).
    window_blocked_by_morning_only = (
        base_decision.block_reason == config.TW_REJECT_TIME_WINDOW
        and base_decision.metrics.get("window") in (
            time_window_filter.WINDOW_AFTERNOON_1, time_window_filter.WINDOW_AFTERNOON_2,
        )
        and now.astimezone(KST).time() < config.TW_AFTERNOON_ENTRY_HARD_CUTOFF
    )
    tw2_cleared = bool(base_decision.approved or window_blocked_by_morning_only)
    if tw2_cleared:
        vetoed, veto_reason = time_window_filter.evaluate_tw2_extra_vetoes(bars_3m, direction, flag_bar_dt, now)
        if vetoed:
            tw2_cleared = False
            base_decision = dataclasses.replace(base_decision, approved=False, decision=veto_reason, block_reason=veto_reason)

    slot_metrics: dict[str, Any] = {}
    final_approved = False
    final_block_reason = base_decision.block_reason
    final_decision_label = base_decision.decision

    if tw2_cleared:
        slot_decision = time_window_3slot.resolve_slot(
            now=now,
            slots_used_today=int(state.tw2_3slot_slots_used_today or 0),
            morning_count=int(state.tw2_3slot_morning_count or 0),
            afternoon_count=int(state.tw2_3slot_afternoon_count or 0),
            direction=direction,
            is_flat=(position is None),
            last_afternoon_direction=state.tw2_3slot_last_afternoon_direction,
        )
        slot_metrics["slot_number"] = slot_decision.slot_number
        slot_metrics["session"] = slot_decision.session
        if not slot_decision.slot_allowed:
            final_block_reason = slot_decision.reject_reason
            final_decision_label = slot_decision.reject_reason
        elif slot_decision.requires_quality_gate:
            quality = time_window_3slot.evaluate_trend_quality(bars_3m, direction)
            slot_metrics["quality_passed"] = quality.passed_count
            slot_metrics["quality_conditions"] = dict(quality.conditions)
            if quality.approved:
                final_approved = True
                final_decision_label = config.TW_APPROVED
                final_block_reason = None
            else:
                final_block_reason = config.TW2_3SLOT_REJECT_QUALITY
                final_decision_label = config.TW2_3SLOT_REJECT_QUALITY
        elif slot_decision.requires_teg_gate:
            teg_decision = teg_gate.evaluate_teg(bars_3m, direction, flag_bar_dt, now)
            slot_metrics["teg_approved"] = bool(teg_decision.approved)
            slot_metrics["teg_reject_reasons"] = list(teg_decision.reject_reasons or [])
            if teg_decision.approved:
                final_approved = True
                final_decision_label = config.TW_APPROVED
                final_block_reason = None
            else:
                final_block_reason = config.TW2_3SLOT_REJECT_TEG
                final_decision_label = config.TW2_3SLOT_REJECT_TEG
        else:
            final_approved = True
            final_decision_label = config.TW_APPROVED
            final_block_reason = None

    decision = dataclasses.replace(
        base_decision, approved=final_approved, decision=final_decision_label, block_reason=final_block_reason,
        metrics={**(base_decision.metrics or {}), **slot_metrics},
    )
    _persist_tw2_3slot_decision(state, decision, signal_id)

    if not decision.approved:
        target_symbol = order_executor.target_symbol_for_direction(direction)
        if position is not None and position.symbol != target_symbol:
            if decision.block_reason in config.TW_WHIPSAW_REJECT_REASONS:
                state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
                whipsaw_trace = {
                    "signal_id": signal_id, "direction": direction.value, "signal_type": "TW2_3SLOT_CONFIRM",
                    "completed_bar_at": macd_snap.bar_dt.isoformat(),
                    "order_executor_called": False, "broker_called": False,
                    "final_block_reason": decision.block_reason or decision.decision or "",
                    "order_result_override": "TIME_WINDOW_WHIPSAW_HOLD",
                    "major_fields": _entry_gate_ledger_fields(state, decision, "TW2_3SLOT"),
                }
                whipsaw_outcome = order_executor.ExecutionOutcome(
                    signal_id=signal_id, direction=direction, target_symbol=target_symbol,
                    final_state=SignalState.BLOCKED, block_reason=decision.block_reason or decision.decision,
                )
                _record_signal_ledger(
                    state, macd_snap, direction, "TW2_3SLOT_CONFIRM", signal_id, datetime.now(KST),
                    whipsaw_outcome, whipsaw_trace,
                )
                result.actions.append(f"TW2_3SLOT_WHIPSAW_HOLD:{direction.value}")
                # 2026-09-03 real incident fix -- see _resolve_time_window_
                # candidate's own identical fix for the full rationale: this
                # T+3 rejection branch never updated state.order_block_reason,
                # so the UI's "최근 block/skip 사유" line stayed frozen on the
                # flag bar's own TW_PENDING_CONFIRMATION forever, even though
                # the real reason was correctly written to the signal-ledger
                # CSV's block_reason column all along.
                state.order_block_reason = decision.block_reason or decision.decision
                _start_whipsaw_watch(state, mode="TW2_3SLOT", direction=direction, bars_3m=bars_3m, flag_bar_dt=macd_snap.bar_dt, now=now)
                return None
            outcome = _execute_reversal_exit_only_for_filtered_entry(
                broker=broker, state=state, macd_snap=macd_snap, direction=direction,
                position=position, decision=decision, result=result, gate_mode="TW2_3SLOT",
                signal_id_override=signal_id,
            )
            if outcome is not None:
                _apply_exit_outcome(state, outcome)
            return outcome
        state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
        dispatch_trace = {
            "signal_id": signal_id, "direction": direction.value, "signal_type": "TW2_3SLOT_CONFIRM",
            "completed_bar_at": macd_snap.bar_dt.isoformat(),
            "order_executor_called": False, "broker_called": False,
            "final_block_reason": decision.block_reason or decision.decision or "",
            "order_result_override": config.FILTERED_OUT,
            "major_fields": _entry_gate_ledger_fields(state, decision, "TW2_3SLOT"),
        }
        outcome = order_executor.ExecutionOutcome(
            signal_id=signal_id, direction=direction,
            target_symbol=target_symbol,
            final_state=SignalState.BLOCKED, block_reason=decision.block_reason or decision.decision,
        )
        _record_signal_ledger(
            state, macd_snap, direction, "TW2_3SLOT_CONFIRM", signal_id, datetime.now(KST), outcome, dispatch_trace,
        )
        result.actions.append(f"{config.FILTERED_OUT}:{direction.value}")
        # 2026-09-03 real incident fix: see _resolve_time_window_candidate's
        # own identical fix above for the full rationale.
        state.order_block_reason = decision.block_reason or decision.decision
        return None

    signal_detected_at = datetime.now(KST)
    result.signal_detected_at = signal_detected_at.isoformat()
    signal_type = "REVERSAL" if (position is not None and position.quantity > 0) else "INITIAL"
    outcome = _execute_or_wait(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        direction=direction, signal_id=signal_id, signal_type=signal_type, position=position, result=result,
        signal_detected_at=signal_detected_at,
    )
    result.signal_dispatch_trace["major_fields"] = _entry_gate_ledger_fields(state, decision, "TW2_3SLOT")
    if outcome is None and result.skipped == config.MISSED_SIGNAL_QUOTE_STALE:
        state.tw2_3slot_pending_flag_direction = direction
        state.tw2_3slot_pending_flag_bar_ts = flag_bar_dt.isoformat()
    _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, signal_detected_at, outcome, result.signal_dispatch_trace)

    if outcome is not None and outcome.final_state == SignalState.EXECUTED:
        _apply_switch_outcome(state, outcome, direction, now)
        session = slot_metrics.get("session") or time_window_filter.session_for_window(
            time_window_filter.classify_window(macd_snap.bar_dt.astimezone(KST).time())
        )
        state.time_window_position_active = True
        state.time_window_active_mode = "TW2_3SLOT"
        state.time_window_entry_session = session
        state.time_window_tp1_done = False
        state.time_window_peak_net_return = 0.0
        state.time_window_initial_quantity = outcome.quantity
        state.last_time_window_entry_at = signal_detected_at.isoformat()
        # ── 조기익절 필터: 진입 확정봉의 CHOP 판정을 이 포지션에 고정 저장 ──
        # 필터가 켜져 있을 때만 계산한다. OFF일 때 아예 호출하지 않는 것이
        # "OFF면 기존 TW2 3-SLOT과 동작 동일"을 보장하는 방식이다(계산 자체도,
        # 상태 쓰기도 없음 -- tests/macd2/test_early_take_profit_worker.py의
        # 회귀테스트가 필터 OFF에서 이 모듈 함수가 단 한 번도 호출되지 않는지
        # 실제로 검증한다). 진입/슬롯/게이트 판단은 이 위에서 이미 전부 끝났고,
        # 여기서 무엇을 계산하든 그 결과를 바꿀 수 없다.
        state.time_window_entry_chop = False
        state.early_tp_peak_net_return = 0.0
        if early_take_profit.is_enabled(state):
            chop = early_take_profit.evaluate_entry_chop(bars_3m, direction, now)
            state.time_window_entry_chop = bool(chop.is_chop)
            state.last_entry_chop_score = int(chop.score)
            state.last_entry_chop_conditions = dict(chop.conditions)
        state.tw2_3slot_slots_used_today = int(state.tw2_3slot_slots_used_today or 0) + 1
        if session == time_window_3slot.SESSION_MORNING:
            state.tw2_3slot_morning_count = int(state.tw2_3slot_morning_count or 0) + 1
        else:
            state.tw2_3slot_afternoon_count = int(state.tw2_3slot_afternoon_count or 0) + 1
            state.tw2_3slot_last_afternoon_direction = direction.value
    return outcome


def _start_whipsaw_watch(
    state: RuntimeState, *, mode: str, direction: Direction, bars_3m, flag_bar_dt: datetime, now: datetime,
) -> None:
    """Seeds the shared TW2/TW2_3SLOT whipsaw-watch follow-up (2026-09-02)
    the instant either mode's own T+3 reversal candidate is whipsaw-held
    (config.TW_WHIPSAW_REJECT_REASONS) -- called immediately after that
    existing hold branch's own ledger row/action, which this never alters.
    Pure state seeding: no order, no ledger row of its own. ``direction``
    is the WATCHED (opposite-to-held) direction, same one the whipsaw-hold
    itself was just evaluated against."""
    seed = time_window_filter.evaluate_whipsaw_watch(bars_3m, direction, float("-inf"), float("-inf"))
    state.whipsaw_watch_active = True
    state.whipsaw_watch_direction = direction
    state.whipsaw_watch_mode = mode
    state.whipsaw_watch_origin_flag_bar_ts = flag_bar_dt.isoformat()
    state.whipsaw_watch_started_at = now.isoformat()
    state.whipsaw_watch_last_gap = seed.current_gap if not seed.insufficient_data else 0.0
    state.whipsaw_watch_last_ema_spread = seed.current_ema_spread if not seed.insufficient_data else 0.0
    state.whipsaw_watch_last_checked_bar_ts = flag_bar_dt.isoformat()
    state.whipsaw_watch_bars_checked = 0


def _clear_whipsaw_watch(state: RuntimeState) -> None:
    """Ends an active watch with no order -- used for release-on-recovery,
    a fresh opposite flag superseding it, and (via _apply_exit_outcome) the
    held position closing for any other reason. Idempotent no-op if no
    watch is active."""
    state.whipsaw_watch_active = False
    state.whipsaw_watch_direction = None
    state.whipsaw_watch_mode = None
    state.whipsaw_watch_origin_flag_bar_ts = None
    state.whipsaw_watch_started_at = None
    state.whipsaw_watch_last_gap = None
    state.whipsaw_watch_last_ema_spread = None
    state.whipsaw_watch_last_checked_bar_ts = None
    state.whipsaw_watch_bars_checked = 0


def _advance_whipsaw_watch(
    *, broker, state: RuntimeState, now: datetime, macd_snap, bars_3m,
    position: Optional[PositionSnapshot], result: TickResult,
):
    """Advances the shared whipsaw-watch follow-up (2026-09-02 real
    incident) on each NEWLY completed bar while state.whipsaw_watch_active
    is True -- called from the SAME held-position section of run_once()
    that resolves TW2's/TW2 3-SLOT's own pending candidates, so it works
    identically regardless of which mode opened/manages the position.
    Idempotent on whipsaw_watch_last_checked_bar_ts (never re-evaluates a
    bar already checked). Exit-only -- never places a new entry; this is
    called AFTER _advance_held_position_risk_management's own TP/SL/
    trailing check already ran and returned this tick if it fired, so that
    ladder always keeps its existing priority unchanged."""
    if not state.whipsaw_watch_active or position is None:
        return None
    checked_bar_ts = _parse_iso_dt(state.whipsaw_watch_last_checked_bar_ts)
    if checked_bar_ts is not None and macd_snap.bar_dt <= checked_bar_ts:
        return None  # already evaluated this bar (or older) -- wait for the next one
    direction = state.whipsaw_watch_direction
    if direction is None:
        _clear_whipsaw_watch(state)
        return None

    decision = time_window_filter.evaluate_whipsaw_watch(
        bars_3m, direction,
        last_gap=float(state.whipsaw_watch_last_gap or 0.0),
        last_ema_spread=float(state.whipsaw_watch_last_ema_spread or 0.0),
    )
    if decision.insufficient_data:
        return None  # keep waiting -- do not advance the checked-bar marker

    state.whipsaw_watch_last_checked_bar_ts = macd_snap.bar_dt.isoformat()
    state.whipsaw_watch_bars_checked = int(state.whipsaw_watch_bars_checked or 0) + 1

    if decision.should_release:
        _clear_whipsaw_watch(state)
        result.actions.append(f"{config.WHIPSAW_WATCH_RELEASED}:{direction.value}")
        return None

    if not decision.should_sell:
        state.whipsaw_watch_last_gap = decision.current_gap
        state.whipsaw_watch_last_ema_spread = decision.current_ema_spread
        return None

    # Deterioration confirmed on both signals -- full liquidation, exit-only.
    # gate_mode must match _entry_gate_ledger_fields' own mode strings
    # ("TIME_WINDOW"/"TW2_3SLOT", not the diagnostic whipsaw_watch_mode
    # values "TW2"/"TW2_3SLOT") so the right ledger column family populates.
    gate_mode = "TW2_3SLOT" if state.whipsaw_watch_mode == "TW2_3SLOT" else "TIME_WINDOW"
    signal_id = f"{make_signal_id(macd_snap.bar_dt, direction)}:WHIPSAW_WATCH_EXIT"
    fake_decision = MajorFlagDecision(
        approved=False, score=0.0, required_score=0.0,
        decision=config.WHIPSAW_WATCH_DETERIORATION_EXIT,
        reasons=("whipsaw watch: opposite gap/EMA spread re-expanded",),
        component_scores={},
        metrics={
            "whipsaw_watch_gap": decision.current_gap,
            "whipsaw_watch_ema_spread": decision.current_ema_spread,
            "whipsaw_watch_bars_checked": state.whipsaw_watch_bars_checked,
        },
        is_reversal=True, fast_reversal=False, block_reason=config.WHIPSAW_WATCH_DETERIORATION_EXIT,
    )
    outcome = _execute_reversal_exit_only_for_filtered_entry(
        broker=broker, state=state, macd_snap=macd_snap, direction=direction,
        position=position, decision=fake_decision, result=result,
        gate_mode=gate_mode, signal_id_override=signal_id,
    )
    _clear_whipsaw_watch(state)
    if outcome is not None:
        _apply_exit_outcome(state, outcome)
        if outcome.final_state == SignalState.EXECUTED:
            result.actions.append(f"{config.WHIPSAW_WATCH_DETERIORATION_EXIT}:{outcome.target_symbol}")
    return outcome


def _judge_no_filter_flag(*, state: RuntimeState, now: datetime, signal_id: str) -> MajorFlagDecision:
    """"무필터 09:00-11:00" 즉시청산 진입모드 (2026-08-20) -- a single approve/
    reject decision on the ALREADY-confirmed crossover bar itself, no T+3
    pending wait, no quality score: approved iff ``now`` falls in
    [config.NO_FILTER_ENTRY_WINDOW_START, config.NO_FILTER_ENTRY_WINDOW_END).
    Never called when ``state.no_filter_0900_1100_enabled`` is False. Judged
    through the exact same generic path as MAJOR/SIDEWAYS/TREND_PERSISTENCE/
    SINGLE_ENTRY (never TIME_WINDOW's own pending/whipsaw machinery), so a
    rejected reversal under this gate always sells immediately via
    _execute_reversal_exit_only_for_filtered_entry -- no whipsaw-tolerant
    hold for this mode, by construction.

    2026-08-28 fix (real incident): this mode used to have NO entry-count
    cap of its own at all -- unlimited entries inside the 09:00-11:00
    window, none of them visible to TW2/TEG's own daily-cap counters
    either (see RuntimeState.daily_total_entry_count's docstring). Toggling
    between this mode and TW2/TEG could bypass config.MAX_DAILY_ENTRIES from
    BOTH directions. Now also rejects once the TRUE daily total (shared with
    every other entry path) reaches the same config.MAX_DAILY_ENTRIES cap
    TW2/TEG itself respects -- the 09:00-11:00 window restriction and
    immediate-liquidation-on-reject behavior above are completely
    unchanged."""
    now_time = now.astimezone(KST).time()
    in_window = config.NO_FILTER_ENTRY_WINDOW_START <= now_time < config.NO_FILTER_ENTRY_WINDOW_END
    daily_count = int(state.daily_total_entry_count or 0)
    at_daily_cap = daily_count >= config.MAX_DAILY_ENTRIES
    approved = in_window and not at_daily_cap
    if not in_window:
        block_reason = config.NO_FILTER_REJECT_OUTSIDE_WINDOW
        reasons = (f"outside 09:00-11:00 entry window (now={now_time.isoformat()})",)
    elif at_daily_cap:
        block_reason = config.TW_REJECT_MAX_ENTRY_COUNT
        reasons = (f"daily entry count {daily_count} >= {config.MAX_DAILY_ENTRIES}",)
    else:
        block_reason = None
        reasons = ()
    decision = MajorFlagDecision(
        approved=approved,
        score=0.0,
        required_score=0.0,
        decision=("APPROVED" if approved else block_reason),
        reasons=reasons,
        component_scores={},
        metrics={},
        is_reversal=False,
        fast_reversal=False,
        block_reason=block_reason,
    )
    state.no_filter_0900_1100_filter_version = config.NO_FILTER_0900_1100_FILTER_VERSION
    state.last_no_filter_0900_1100_approved = approved
    state.last_no_filter_0900_1100_block_reason = decision.block_reason
    return decision


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

    TW2 (``time_window_2_filter_enabled``) / the TEG filter (``time_window_
    teg_filter_enabled``) take TOP PRIORITY, sharing one tier — the two are
    mutually exclusive by construction (service.set_time_window_2_filter_
    enabled/set_time_window_teg_filter_enabled each force the other off,
    2026-08-21 사용자 요청, same pattern the retired TW1/TW2 pair used), so at
    most one of them is ever True; both route through the SAME
    ``_judge_time_window_flag``/``_resolve_time_window_candidate`` pair,
    which internally branches on ``time_window_2_filter_enabled`` OR
    ``time_window_teg_filter_enabled`` for the shared extra vetoes + raised
    TP2, and additionally on ``time_window_teg_filter_enabled`` alone for
    the TEG count-cap bypass (2026-08-27). Then (2026-08-15 사용자 요청: the
    newest, most complete redesign supersedes the simpler entry-only gates
    when a user opts into it) ``no_filter_0900_1100_enabled`` (2026-08-20
    사용자 요청: 6th peer gate, same priority tier as the other simple
    filters -- see ``_judge_no_filter_flag``), then ``sideways_filter_
    enabled``, then ``major_filter_enabled``, then ``trend_persistence_
    filter_enabled``, then ``single_entry_filter_enabled`` — never more than
    one of these (TW2-or-TEG counting as a single tier) active for the same
    signal (2026-08-04 추세전환장 toggle spec: "위 로직 우선으로 들어가는 거야",
    extended 2026-08-07 to Trend Persistence, 2026-08-08 to Single-Entry,
    2026-08-15 to Time-Window, 2026-08-20 to No-Filter-0900-1100, 2026-08-21
    to TW2, 2026-08-27 TW1 retired and replaced by the TEG filter in this
    tier).
    Returns ``(None, "NONE")`` when no toggle is on — legacy behavior (every
    confirmed flag has order authority) is completely unchanged.
    """
    if state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled:
        return _judge_time_window_flag(state=state, bars_3m=bars_3m, direction=direction, signal_id=signal_id), "TIME_WINDOW"
    if state.time_window_3slot_filter_enabled:
        return _judge_tw2_3slot_flag(state=state, bars_3m=bars_3m, direction=direction, signal_id=signal_id), "TW2_3SLOT"
    if state.no_filter_0900_1100_enabled:
        return _judge_no_filter_flag(state=state, now=now, signal_id=signal_id), "NO_FILTER_0900_1100"
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


def _time_window_ledger_fields(
    state: RuntimeState, decision: Optional[MajorFlagDecision] = None, *, down_blue_exception_applied: bool = False,
) -> dict[str, Any]:
    """time_window_* ledger columns — mirrors the other filters' _*_ledger_
    fields pattern. metrics carries the two-bar gap_flag/gap_now/window/
    session values computed by time_window_filter.evaluate_time_window_entry
    (or the bar-T "pending confirmation" placeholder from
    _judge_time_window_flag)."""
    row: dict[str, Any] = {
        # time_window_filter_enabled: TW1 was retired 2026-08-27 -- this
        # column is kept (never renamed/deleted, per ledger.py's own
        # backward-compat policy for historical rows) but always False from
        # here on, since the field it used to mirror no longer exists.
        "time_window_filter_enabled": False,
        "time_window_filter_version": state.time_window_filter_version or "",
        "time_window_2_filter_enabled": bool(state.time_window_2_filter_enabled),
        "time_window_teg_filter_enabled": bool(state.time_window_teg_filter_enabled),
        "time_window_active_mode": state.time_window_active_mode or "",
        "time_window_down_blue_exception_enabled": bool(state.down_blue_exception_filter_enabled),
        "time_window_down_blue_exception_applied": bool(down_blue_exception_applied),
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


def _tw2_3slot_ledger_fields(state: RuntimeState, decision: Optional[MajorFlagDecision] = None) -> dict[str, Any]:
    """tw2_3slot_* ledger columns — mirrors _time_window_ledger_fields'
    shape for TW2 3-SLOT's own slot/quality/TEG diagnostics. Additive-only:
    a new column family, never touching the existing time_window_* ones."""
    row: dict[str, Any] = {
        "tw2_3slot_filter_enabled": bool(state.time_window_3slot_filter_enabled),
        "tw2_3slot_filter_version": state.time_window_3slot_filter_version or config.TW2_3SLOT_FILTER_VERSION,
        "tw2_3slot_slots_used_today": int(state.tw2_3slot_slots_used_today or 0),
        "tw2_3slot_morning_count": int(state.tw2_3slot_morning_count or 0),
        "tw2_3slot_afternoon_count": int(state.tw2_3slot_afternoon_count or 0),
        "tw2_3slot_approved": "",
        "tw2_3slot_decision": "",
        "tw2_3slot_block_reason": "",
        "tw2_3slot_slot_number": "",
        "tw2_3slot_session": "",
        "tw2_3slot_quality_passed": "",
        "tw2_3slot_quality_conditions": "",
        "tw2_3slot_teg_approved": "",
        "tw2_3slot_teg_reject_reasons": "",
    }
    if decision is None:
        return row
    metrics = dict(decision.metrics or {})
    row.update({
        "tw2_3slot_approved": bool(decision.approved),
        "tw2_3slot_decision": decision.decision or "",
        "tw2_3slot_block_reason": decision.block_reason or "",
        "tw2_3slot_slot_number": metrics.get("slot_number") if metrics.get("slot_number") is not None else "",
        "tw2_3slot_session": metrics.get("session") or "",
        "tw2_3slot_quality_passed": metrics.get("quality_passed") if metrics.get("quality_passed") is not None else "",
        "tw2_3slot_quality_conditions": str(metrics.get("quality_conditions")) if metrics.get("quality_conditions") is not None else "",
        "tw2_3slot_teg_approved": metrics.get("teg_approved") if metrics.get("teg_approved") is not None else "",
        "tw2_3slot_teg_reject_reasons": str(metrics.get("teg_reject_reasons")) if metrics.get("teg_reject_reasons") is not None else "",
    })
    return row


def _no_filter_ledger_fields(state: RuntimeState, decision: Optional[MajorFlagDecision] = None) -> dict[str, Any]:
    """no_filter_0900_1100_* ledger columns -- minimal (no score/component
    breakdown, since this gate is a pure time-window check, not a scored
    filter). Mirrors ``_sideways_ledger_fields``'s shape for the fields that
    do apply."""
    row: dict[str, Any] = {
        "no_filter_0900_1100_enabled": bool(state.no_filter_0900_1100_enabled),
        "no_filter_0900_1100_filter_version": state.no_filter_0900_1100_filter_version or config.NO_FILTER_0900_1100_FILTER_VERSION,
        "no_filter_0900_1100_approved": "",
        "no_filter_0900_1100_block_reason": "",
    }
    if decision is None:
        return row
    row.update({
        "no_filter_0900_1100_approved": bool(decision.approved),
        "no_filter_0900_1100_block_reason": decision.block_reason or "",
    })
    return row


def _entry_gate_ledger_fields(
    state: RuntimeState, decision: Optional[MajorFlagDecision], mode: str, *, down_blue_exception_applied: bool = False,
) -> dict[str, Any]:
    """Merge major_*, sideways_*, trend_persistence_*, single_entry_*,
    time_window_*, and no_filter_0900_1100_* ledger columns for one signal row.

    All six column families are always present (never omitted), so every
    ledger row shows the current state of all toggles — but the shared
    generic metric columns (``_MAJOR_METRIC_LEDGER_KEYS``) are populated
    only by whichever of major/sideways actually judged this signal
    (``mode``), never blanked out afterward by the inactive side.
    """
    major_fields = _major_ledger_fields(state, decision if mode == "MAJOR" else None)
    sideways_fields = _sideways_ledger_fields(state, decision if mode == "SIDEWAYS" else None)
    trend_persistence_fields = _trend_persistence_ledger_fields(state, decision if mode == "TREND_PERSISTENCE" else None)
    single_entry_fields = _single_entry_ledger_fields(state, decision if mode == "SINGLE_ENTRY" else None)
    time_window_fields = _time_window_ledger_fields(
        state, decision if mode == "TIME_WINDOW" else None, down_blue_exception_applied=down_blue_exception_applied,
    )
    no_filter_fields = _no_filter_ledger_fields(state, decision if mode == "NO_FILTER_0900_1100" else None)
    tw2_3slot_fields = _tw2_3slot_ledger_fields(state, decision if mode == "TW2_3SLOT" else None)
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
    merged.update(no_filter_fields)
    merged.update(tw2_3slot_fields)
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


def _advance_premarket_carry_candidate(state: RuntimeState, macd_snap, confirmed_direction: Direction) -> None:
    """PRE15+TW 프리마켓 승계 후보 등록/취소 (2026-08-24, TW2/TEGv2 전용,
    사용자 요청 -- 60영업일 백테스트 검증: scripts/premarket_carryover_
    backtest.py의 run_pre15_tw와 동일 규칙). 매 tick, held/flat 분기와
    무관하게 무조건 호출된다(순수 북키핑, 주문 권한 없음) --
    confirmed_direction은 이 tick의 completed bar에서 새로 확정된
    크로스오버(HOLD면 아무것도 안 함).

    2026-09-01 (사용자 요청): TW2 3-SLOT(``time_window_3slot_filter_
    enabled``)은 이 승계 후보 등록에 절대 참여하지 않는다 -- 08:45-08:59
    확정 플래그는 (필터와 무관하게 항상 기록되는 일반 confirmed-flag 신호
    원장 경로를 통해) 그대로 기록되지만, 09:00 이전에 별도 진입 없이
    소비되지 않고 지나간다. 3-SLOT의 하루 3슬롯은 09:00 이후 새로 확정되는
    첫 플래그부터 정상적으로 카운트를 시작한다(``tw2_3slot_slots_used_
    today``는 승계로 인해 미리 소진되지 않음). TW2/TEGv2의 승계 동작은 이
    변경으로 전혀 바뀌지 않는다.

    - config.PREMARKET_CARRY_WINDOW_START(08:45:00) <= bar_time < SESSION_OPEN
      (09:00:00): 이 bar를 오늘의 승계 후보로 등록(덮어쓰기 -- "마지막" 플래그
      규칙은 재등록만으로 자연히 만족됨).
    - 후보가 이미 있고 아직 발동 전(SCHEDULED_ENTRY_TIME=09:03 이전)인 상태에서
      반대 방향 플래그가 확정되면 후보를 취소한다. 취소 판정 구간은 후보의 자기
      bar 다음부터 09:00-09:03 bar까지(즉 bar_time < SCHEDULED_ENTRY_TIME)
      전부 포함-- 09:00 bar에서의 반대 플래그도 여기서 취소된다.
    - 같은 방향의 새 플래그(예: 09:00 bar에 동일 방향 재확정)는 취소하지 않고,
      그대로 일반 TW2 경로(_judge_time_window_flag)로도 흘러가지만
      order_executor.execute_signal의 BLOCK_ALREADY_HOLDING이 자연히
      중복진입을 막는다(승계가 먼저 09:03에 체결되므로 나중에 도착하는 일반
      T+3 재확인은 항상 이미 보유 중인 상태를 본다).
    """
    if confirmed_direction == Direction.HOLD:
        return
    if not (state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled):
        return
    if state.premarket_carry_executed_at:
        return  # already resolved (entered or expired) today
    bar_time = macd_snap.bar_dt.astimezone(KST).time()
    if config.PREMARKET_CARRY_WINDOW_START <= bar_time < config.SESSION_OPEN:
        state.premarket_carry_candidate_direction = confirmed_direction
        state.premarket_carry_candidate_bar_ts = macd_snap.bar_dt.isoformat()
        return
    if state.premarket_carry_candidate_direction is None:
        return
    if bar_time >= config.SCHEDULED_ENTRY_TIME:
        return  # past the 09:00-09:03 re-confirm window; entry logic owns this from here
    if confirmed_direction != state.premarket_carry_candidate_direction:
        state.premarket_carry_candidate_direction = None
        state.premarket_carry_candidate_bar_ts = None


def _premarket_carry_should_fire(state: RuntimeState, now: datetime) -> bool:
    """Mirrors _scheduled_entry_should_fire's own once-per-day + fire-window
    semantics exactly, on the separate premarket_carry_* fields. 2026-09-01:
    TW2_3SLOT excluded (see _advance_premarket_carry_candidate) -- a
    candidate can never exist while only 3SLOT is enabled, but this check
    stays defensive/explicit rather than relying solely on that."""
    if not (state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled):
        return False  # user turned TW2/TEG off (or only TW2_3SLOT is on) between registration and 09:03 -- do not fire
    if state.premarket_carry_candidate_direction is None or state.premarket_carry_executed_at:
        return False
    if now.time() < config.SCHEDULED_ENTRY_TIME:
        return False
    fire_deadline = datetime.combine(now.date(), config.SCHEDULED_ENTRY_TIME, tzinfo=KST) + timedelta(
        seconds=config.SCHEDULED_ENTRY_FIRE_WINDOW_SEC,
    )
    return now <= fire_deadline


def _record_premarket_carry_signal(state: RuntimeState, direction: Direction, signal_id: str, now: datetime, outcome) -> None:
    """Signal-ledger row for the PRE15+TW premarket-carry entry -- mirrors
    _record_scheduled_entry_signal exactly, tagged signal_type=
    PREMARKET_CARRY_TW so it is distinguishable in the audit trail from both
    a normal TW2 entry and the unrelated manual 09:03 예약매수."""
    block_reason = outcome.block_reason or ""
    row = {
        "trading_date": now.strftime("%Y%m%d"),
        "completed_bar_at": now.strftime("%H%M%S"),
        "signal_id": signal_id,
        "signal_type": "PREMARKET_CARRY_TW",
        "direction": direction.value,
        "detected_at": now.isoformat(),
        "order_requested_at": outcome.timestamps.get("buy_requested_at", ""),
        "order_result": outcome.final_state.value,
        "block_reason": block_reason,
        "signal_bar_at": state.premarket_carry_candidate_bar_ts or now.isoformat(),
        "signal_confirmed_at": now.isoformat(),
        "strategy_name": config.STRATEGY_NAME,
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": "PREMARKET_CARRY_TW_0845_0859_NO_VETO",
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


def _execute_premarket_carry_entry(*, broker, market_data: MarketDataService, state: RuntimeState, now: datetime, macd_snap):
    """Fires the PRE15+TW premarket-carry entry at config.SCHEDULED_ENTRY_TIME
    (09:03) -- deliberately bypasses time_window_filter.evaluate_time_window_
    entry / evaluate_tw2_extra_vetoes entirely (no quality score, no VWAP
    veto, no recent-cross veto), calling order_executor.execute_signal
    directly, exactly reproducing scripts/premarket_carryover_backtest.py's
    run_pre15_tw -- the validated 60-day backtest never applied those checks
    to this entry either (사용자 명시 요청: 검증된 조건 그대로, 추가 quality
    조건 없음). Only ever called from run_once's flat branch, so ``position``
    is always None here by construction (mirrors _execute_scheduled_entry's
    own convention of passing position=None explicitly rather than
    state.position). Once filled, this position is managed by the exact same
    TW2 ladder (STOP_LOSS/TP1/TP2/OPPOSITE_SIGNAL/daily entry count) as any
    other TW2 entry, via the same time_window_position_active bookkeeping the
    normal path sets in _dispatch_confirmed_signal's approved branch."""
    direction = state.premarket_carry_candidate_direction
    if direction is None:
        return None
    if state.position is not None and state.position.quantity > 0:
        # Defense-in-depth: this function always dispatches with
        # position=None (never state.position), which is only safe because
        # run_once's flat branch guarantees no position is held when this is
        # called. If ever invoked otherwise, refuse rather than silently
        # buying on top of an existing holding order_executor never gets told
        # about.
        return None
    if not _pending_direction_still_active(direction, macd_snap):
        # spec: "09:03에도 동일 MACD STATE가 유지되면" -- it didn't, so this
        # is a clean non-entry, not a retryable failure.
        state.premarket_carry_candidate_direction = None
        state.premarket_carry_candidate_bar_ts = None
        state.premarket_carry_executed_at = now.isoformat()
        state.premarket_carry_last_result = "MACD_STATE_NOT_HELD_AT_0903"
        return None
    target_symbol = order_executor.target_symbol_for_direction(direction)
    quote_snap = market_data.get_quote(target_symbol)
    if quote_snap is None or quote_snap.error or quote_snap.price <= 0:
        state.premarket_carry_last_result = "QUOTE_UNAVAILABLE"
        return None  # transient -- next tick retries within the fire window
    signal_id = f"PREMARKET_CARRY_TW_{direction.value}_{now.strftime('%Y%m%d')}"
    outcome = order_executor.execute_signal(
        broker=broker, direction=direction, signal_id=signal_id,
        quotes={target_symbol: quote_snap.price}, position=None, budget=state.budget,
        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
    )
    _record_premarket_carry_signal(state, direction, signal_id, now, outcome)

    if outcome.final_state == SignalState.EXECUTED:
        _apply_switch_outcome(state, outcome, direction, now)
        state.premarket_carry_executed_at = now.isoformat()
        state.premarket_carry_last_result = "EXECUTED"
        state.premarket_carry_candidate_direction = None
        state.premarket_carry_candidate_bar_ts = None
        # Counts toward the SAME daily morning entry cap as every other TW2
        # entry (사용자 요청: "기존 일일 진입횟수 카운트에 정상 포함"), and the
        # SAME session bookkeeping _dispatch_confirmed_signal's approved
        # branch sets, so the held-position TW2 branch recognizes and manages
        # this position identically to a normal TW2 entry from here on.
        # 2026-09-01: TW2 3-SLOT never reaches this function at all (see
        # _advance_premarket_carry_candidate/_premarket_carry_should_fire) --
        # this path is TW2/TEGv2-only, so it always increments TW2's own
        # morning entry count, never the 3-SLOT counters.
        state.time_window_morning_entry_count = int(state.time_window_morning_entry_count or 0) + 1
        state.time_window_entry_session_seq = state.time_window_morning_entry_count
        state.time_window_position_active = True
        state.time_window_active_mode = "TW2"
        state.time_window_entry_session = "MORNING"
        state.time_window_tp1_done = False
        state.time_window_peak_net_return = 0.0
        # 조기익절 필터의 포지션 종속 상태도 같은 수명으로 초기화한다
        # (early_take_profit.py / models.py의 필드 주석 참고).
        state.time_window_entry_chop = False
        state.early_tp_peak_net_return = 0.0
        state.time_window_initial_quantity = outcome.quantity
        state.last_time_window_entry_at = now.isoformat()
        return outcome

    state.order_block_reason = outcome.block_reason
    state.premarket_carry_last_result = f"{outcome.final_state.value}:{outcome.block_reason or ''}"
    if outcome.block_reason not in TEMPORARY_BLOCK_REASONS:
        state.premarket_carry_executed_at = now.isoformat()
        state.premarket_carry_candidate_direction = None
        state.premarket_carry_candidate_bar_ts = None
    return None


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
    if reconcile in (POSITION_DATA_ERROR, POSITION_MISMATCH, RECOVERED_TO_FLAT):
        # 2026-08-07 real incident: a same-symbol qty mismatch (e.g. a
        # partial-fill entry whose broker-side qty later settled to a
        # different number than what was recorded at fill time) used to
        # report as POSITION_MISMATCH forever -- nothing ever corrected it,
        # so this early-return fired on EVERY tick from then on, silently
        # skipping STOP_LOSS/OPPOSITE_SIGNAL/PROFIT_LOCK for the held
        # position indefinitely (no forced liquidation, no dispatch, nothing
        # -- the position just sat unmonitored until a human manually sold
        # it). RECOVERED_TO_FLAT has no position left to evaluate. In
        # contrast, RECOVERED_FROM_BROKER / RECOVERED_QTY_MISMATCH have
        # already adopted a sellable broker position into state.position, so
        # they must continue through this same tick; a TW2 T+3 reversal
        # candidate can otherwise be missed exactly on the recovery tick.
        #
        # 2026-08-31 real incident fix: this early return sits BEFORE
        # _advance_confirmed_primary (the MACD crossover detector) runs, so
        # a reconcile block used to also skip crossover DETECTION itself for
        # this tick's completed bar -- not just order execution. A crossover
        # is a one-shot bar-to-bar zero-cross event that can never be
        # re-derived from a later bar once price has moved past it, so any
        # flag landing on a reconcile-blocked tick was lost forever; worse,
        # state.last_detected_direction was left stale, which then made
        # evaluate_macd_crossover's own repeat-dedup silently swallow the
        # NEXT same-direction flag too (confirmed against real 2026-08-31
        # KIS data: a reconcile block spanning the 12:33 DOWN_BLUE crossover
        # left last_detected_direction stuck at 11:57's UP_RED, which then
        # suppressed the genuine 13:36 UP_RED as a false "repeat"). Detection
        # must never depend on reconcile health -- only order execution
        # (everything below this block) does -- so it is run here too,
        # before returning; the normal unblocked path below still computes
        # and re-advances it exactly as before (a no-op once this bar's
        # bar_key has already been stamped).
        _skip_df_1m = market_data.get_history_df()
        _skip_bars_3m = resample_completed_3m(_skip_df_1m, now=now)
        _skip_bars_3m, _ = filter_complete_3m_bars(_skip_bars_3m, _skip_df_1m)
        _skip_macd_snap = calculate_macd(_skip_bars_3m)
        if _skip_macd_snap is not None:
            _advance_confirmed_primary(state, _skip_macd_snap, now)
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
    elif state.order_block_reason == "HISTORY_GAP":
        # 2026-08-20 fix (real incident: dashboard showed "HISTORY_GAP" /
        # bootstrap_status FAILED indefinitely after a real 1-minute gap in
        # the WATCH_SYMBOL history had already been backfilled by a later
        # incremental merge). This was the only place that ever SET
        # order_block_reason to "HISTORY_GAP", but nothing ever cleared it
        # back once the gap resolved -- clear it here, and only here (never
        # touch any OTHER reason a different code path set later this same
        # tick), the first tick this exact bar_starts check comes back clean.
        state.order_block_reason = None
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
    # timestamp; a bar not actually dated today (per `now`) or not yet
    # closed sets baseline only — see _advance_confirmed_primary's own
    # docstring (2026-08-18 fix) for why a genuine same-day first bar no
    # longer does.
    confirmed_direction = _advance_confirmed_primary(state, macd_snap, now)
    _advance_premarket_carry_candidate(state, macd_snap, confirmed_direction)

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
        # state.time_window_2_filter_enabled is True; a no-op otherwise.
        tw_resolve_outcome = _resolve_time_window_candidate(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
            bars_3m=bars_3m, df_1m=df_1m, position=pos, result=result,
        )
        if tw_resolve_outcome is not None and tw_resolve_outcome.final_state == SignalState.EXECUTED:
            # Distinguish by the ACTUAL resulting state, not just which
            # helper produced the outcome: a rejected reversal's sell-only
            # exit (2026-08-19 fix) leaves state.position None, same
            # final_state=EXECUTED as a real switch -- labeling that
            # "TIME_WINDOW_SWITCH:<sold symbol>" would misleadingly imply a
            # new position was opened when the account is actually flat.
            if state.position is not None:
                result.actions.append(f"TIME_WINDOW_SWITCH:{tw_resolve_outcome.target_symbol}")
            else:
                result.actions.append(f"TIME_WINDOW_SELL_ONLY:{tw_resolve_outcome.target_symbol}")
            return result

        # TW2 3-SLOT (2026-09-01): resolves its own pending T+3 candidate,
        # exactly mirroring the TW2 call just above but via fully separate
        # tw2_3slot_pending_flag_*/tw2_3slot_* bookkeeping. A no-op unless
        # state.time_window_3slot_filter_enabled is True (mutually exclusive
        # with TW2/TEG, so at most one of these two calls ever does anything
        # on a given tick).
        tw2_3slot_resolve_outcome = _resolve_tw2_3slot_candidate(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
            bars_3m=bars_3m, df_1m=df_1m, position=pos, result=result,
        )
        if tw2_3slot_resolve_outcome is not None and tw2_3slot_resolve_outcome.final_state == SignalState.EXECUTED:
            if state.position is not None:
                result.actions.append(f"TW2_3SLOT_SWITCH:{tw2_3slot_resolve_outcome.target_symbol}")
            else:
                result.actions.append(f"TW2_3SLOT_SELL_ONLY:{tw2_3slot_resolve_outcome.target_symbol}")
            return result

        # Whipsaw-watch follow-up (2026-09-02, real incident): shared by TW2
        # and TW2 3-SLOT, a no-op unless a watch is currently active
        # (state.whipsaw_watch_active, only ever set by either mode's own
        # whipsaw-hold branch above). Runs AFTER both resolve calls just
        # above (never delays or overrides a genuine switch/sell-only they
        # already produced this tick) and after _advance_held_position_risk_
        # management's own TP/SL/trailing check earlier this tick (which
        # already returned this tick if it fired) -- so this can only ever
        # act when nothing else already has.
        whipsaw_watch_outcome = _advance_whipsaw_watch(
            broker=broker, state=state, now=now, macd_snap=macd_snap,
            bars_3m=bars_3m, position=pos, result=result,
        )
        if whipsaw_watch_outcome is not None and whipsaw_watch_outcome.final_state == SignalState.EXECUTED:
            return result

        if (
            state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled or state.time_window_3slot_filter_enabled
        ) and state.time_window_position_active:
            # This position is (still) managed by the time-window filter's
            # own ladder, fully replacing PROFIT_LOCK/QUICK_PROFIT below for
            # as long as it is held — its STOP_LOSS/TP1/TP2 checks already
            # ran earlier this tick via _advance_held_position_risk_
            # management; nothing else in this priority chain should touch
            # this position directly (OPPOSITE_SIGNAL is instead handled by
            # the T+3 candidate resolution just above).
            #
            # 2026-08-19 real incident fix: _resolve_time_window_candidate
            # just above only RESOLVES an ALREADY-pending candidate (a no-op
            # if state.time_window_pending_flag_direction is still None) --
            # it never CREATES one from a crossover confirmed on THIS bar.
            # The only code that does that (_judge_entry_gate ->
            # _judge_time_window_flag) lived further down in this function,
            # in the "confirmed_direction != HOLD" reversal branch, which
            # this early `return result` made structurally unreachable
            # whenever a TW-managed position was already open. Net effect: a
            # genuinely fresh opposite (or same-direction) confirmed flag
            # that occurred WHILE a TW position was held was recorded in
            # state.last_detected_direction (via _advance_confirmed_primary,
            # called earlier and unaffected by this branch) but NEVER became
            # a pending candidate -- so it could never reach its own T+3
            # re-confirmation, could never dispatch OPPOSITE_SIGNAL, and the
            # held position could only ever exit via its own TP1/TP2/
            # stop-loss/trailing ladder or 15:00 forced liquidation, no
            # matter how many later opposite flags fired (real-world
            # example: BLUE flag 09:00 -> entered 0197X0 at 09:06; a genuine
            # RED flag at 09:30 updated last_detected_direction but was
            # silently dropped -- position never switched). Registering it
            # here (a pure bookkeeping write, no order placed) lets a LATER
            # tick's _resolve_time_window_candidate pick it up and run the
            # real evaluate_time_window_entry decision at T+3, exactly like
            # a fresh flag while flat already works.
            #
            # 2026-08-28 real incident fix: this branch called _judge_time_
            # window_flag directly and returned, same as the flat path's
            # _dispatch_confirmed_signal does when TIME_WINDOW isn't approved
            # yet -- EXCEPT the flat path always follows a not-approved
            # decision with _record_major_filtered_signal (a signal-ledger
            # row, order_result=FILTERED_OUT/PENDING), while this branch never
            # did. _judge_time_window_flag itself only sets in-memory pending
            # state (state.time_window_pending_flag_direction/bar_ts) and
            # _persist_time_window_decision's own state.last_time_window_*
            # fields -- neither touches the signal-ledger CSV. Net effect: a
            # confirmed flag detected while a TW2/TEGv2 position was already
            # held got ZERO signal-ledger row at its own detection bar (only
            # the later T+3 confirmation, one bar after, ever appeared) --
            # invisible in the UI's 신호원장 at the flag's own timestamp even
            # though the flag genuinely fired and was being tracked. Recording
            # it here now (same _record_major_filtered_signal call the flat
            # path already makes) only adds an audit-trail row; it does not
            # change what gets approved, dispatched, or ordered.
            if confirmed_direction != Direction.HOLD:
                # Whipsaw-watch hand-off (2026-09-02, real incident): a
                # genuinely NEW confirmed opposite flag supersedes any stale
                # watch left over from an EARLIER whipsaw-hold -- the new
                # flag's own T+3 cycle takes over entirely rather than
                # running both mechanisms at once.
                if state.whipsaw_watch_active:
                    _clear_whipsaw_watch(state)
                signal_id = make_signal_id(macd_snap.bar_dt, confirmed_direction)
                # TW2 3-SLOT (2026-09-01): same gap/fix as the comment above,
                # just registered against this mode's own separate pending
                # state when it -- not TW2/TEG -- is the one actually
                # managing this held position.
                if state.time_window_active_mode == "TW2_3SLOT":
                    decision = _judge_tw2_3slot_flag(
                        state=state, bars_3m=bars_3m, direction=confirmed_direction,
                        signal_id=signal_id,
                    )
                    held_gate_mode = "TW2_3SLOT"
                else:
                    decision = _judge_time_window_flag(
                        state=state, bars_3m=bars_3m, direction=confirmed_direction,
                        signal_id=signal_id,
                    )
                    held_gate_mode = "TIME_WINDOW"
                _record_major_filtered_signal(
                    state=state, macd_snap=macd_snap, direction=confirmed_direction,
                    signal_type="REVERSAL", signal_id=signal_id, decision=decision,
                    detected_at=datetime.now(KST), result=result, gate_mode=held_gate_mode,
                )
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
                reversal_signal_id = make_signal_id(macd_snap.bar_dt, confirmed_direction)
                reversal_decision, reversal_gate_mode = _judge_entry_gate(
                    state=state, bars_3m=bars_3m, df_1m=df_1m, direction=confirmed_direction,
                    position=pos, now=now,
                    signal_id=reversal_signal_id,
                )
                if reversal_decision is not None and not reversal_decision.approved:
                    if reversal_gate_mode in ("TIME_WINDOW", "TW2_3SLOT"):
                        # Two-bar (T -> T+3) confirmation model (spec §1/§12,
                        # and identically for TW2_3SLOT's own T+3 wait): a
                        # not-yet-confirmed candidate must NEVER trigger the
                        # sell-only liquidation below — the held position
                        # stays untouched until _resolve_time_window_candidate
                        # / _resolve_tw2_3slot_candidate (checked at T+3)
                        # decides to switch or hold.
                        #
                        # 2026-08-28 real incident fix: the candidate is NOT
                        # "already recorded by _judge_time_window_flag above"
                        # as this comment used to claim -- that function only
                        # sets in-memory pending state
                        # (state.time_window_pending_flag_direction/bar_ts),
                        # never the signal ledger (same gap as the sibling
                        # time_window_position_active branch above, fixed the
                        # same way there). A confirmed REVERSAL flag arriving
                        # while a held position had NOT yet been tagged
                        # time_window_position_active (e.g. right after a
                        # reconcile-discovered position, before the adoption
                        # pass in _advance_held_position_risk_management runs)
                        # took this branch instead of that one and got zero
                        # ledger row at its own detection bar. Recording it
                        # here only adds an audit-trail row; it does not
                        # change what gets approved, dispatched, or ordered.
                        _record_major_filtered_signal(
                            state=state, macd_snap=macd_snap, direction=confirmed_direction,
                            signal_type="REVERSAL", signal_id=reversal_signal_id, decision=reversal_decision,
                            detected_at=datetime.now(KST), result=result, gate_mode=reversal_gate_mode,
                        )
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
                or state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled
                or state.time_window_3slot_filter_enabled
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

    tw2_3slot_resolve_outcome = _resolve_tw2_3slot_candidate(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        bars_3m=bars_3m, df_1m=df_1m, position=None, result=result,
    )
    if tw2_3slot_resolve_outcome is not None and tw2_3slot_resolve_outcome.final_state == SignalState.EXECUTED:
        result.actions.append(f"TW2_3SLOT_ENTRY:{tw2_3slot_resolve_outcome.target_symbol}")
        return result

    if _scheduled_entry_should_fire(state, now):
        scheduled_outcome = _execute_scheduled_entry(broker=broker, market_data=market_data, state=state, now=now)
        if scheduled_outcome is not None:
            result.actions.append(f"SCHEDULED_ENTRY_0903:{scheduled_outcome.target_symbol}")
            state.last_evaluated_bar_ts = bar_ts_str
            return result

    if _premarket_carry_should_fire(state, now):
        carry_outcome = _execute_premarket_carry_entry(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        )
        if carry_outcome is not None:
            result.actions.append(f"PREMARKET_CARRY_TW:{carry_outcome.target_symbol}")
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
        state.time_window_active_mode = None
        state.time_window_entry_session = None
        state.time_window_entry_flag_seq = None
        state.time_window_entry_session_seq = None
        state.time_window_tp1_done = False
        state.time_window_initial_quantity = 0
        state.time_window_peak_net_return = 0.0
        # 조기익절 필터의 포지션 종속 상태도 같은 수명으로 초기화한다
        # (early_take_profit.py / models.py의 필드 주석 참고).
        state.time_window_entry_chop = False
        state.early_tp_peak_net_return = 0.0
        # Safety net (2026-09-02, real incident): a held position can close
        # for ANY reason (stop-loss/profit-lock/forced-liquidation/etc.)
        # while a whipsaw-watch is tracking it -- e.g. today's real incident,
        # where the position exited via an unrelated breakeven-stop while a
        # reversal candidate may have been left dangling. A stale
        # whipsaw_watch_active=True must never survive past the position it
        # described, same rationale as the time_window_* resets just above.
        _clear_whipsaw_watch(state)
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
        # 2026-08-28 fix: the filter-agnostic daily total (models.py's own
        # docstring on the field) increments here UNCONDITIONALLY of which
        # filter (if any) judged this signal -- unlike the elif chain below,
        # which is mutually exclusive per filter type. This is the single
        # choke point every entry/switch path (TW2/TEGv2, no-filter, PRE15
        # premarket-carry, the 09:03 scheduled entry, sideways/major/trend-
        # persistence/single-entry, and the legacy no-toggle path) already
        # converges on for position adoption -- see reconcile_position_
        # state's RECOVERED_FROM_BROKER branch for the other (reconcile-
        # discovered) contributor to this same counter.
        if filled_qty > 0:
            state.daily_total_entry_count = int(state.daily_total_entry_count or 0) + 1
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


WORKER_LEASE_FILENAME = "macd2_worker_lease.json"


def _worker_lease_path():
    return state_store.STATE_DIR_PATH / WORKER_LEASE_FILENAME


def _claim_worker_lease(instance_id: str) -> None:
    """Unconditionally overwrites the shared lease file on the persistent
    disk with THIS instance's id -- the newest start() call always wins the
    claim. See Macd2Worker.start()'s own docstring/comment for the real
    2026-09-03 incident (two concurrently-ticking Worker loops silently
    corrupting shared state/ledger writes) this closes."""
    try:
        path = _worker_lease_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"instance_id": instance_id, "pid": os.getpid(), "claimed_at": datetime.now(KST).isoformat()}),
            encoding="utf-8",
        )
    except Exception:
        # Best-effort only -- a lease-file write failure (disk full, perms)
        # must never prevent the Worker from starting; _holds_worker_lease's
        # own fail-open default (True on any read error) means a persistent
        # write failure here simply falls back to no cross-process
        # protection, not a crash or a false "superseded" self-shutdown.
        logger.warning("[MACD2] failed to write worker lease file", exc_info=True)


def _holds_worker_lease(instance_id: str) -> bool:
    """True unless the lease file exists, is readable, AND names a
    DIFFERENT instance_id -- fail-open on any missing/corrupt/unreadable
    lease (never let a diagnostic file's own absence stop real trading)."""
    try:
        path = _worker_lease_path()
        if not path.exists():
            return True
        raw = json.loads(path.read_text(encoding="utf-8"))
        claimed_by = raw.get("instance_id")
        return claimed_by is None or claimed_by == instance_id
    except Exception:
        return True


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
        self._last_quote_updater_restart_at: Optional[datetime] = None

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
            # 2026-09-03 real incident fix: re-checked every tick, BEFORE any
            # state load/mutation this iteration -- if some OTHER Worker
            # instance (a newer start() call, whether in this same process
            # or, since the lease lives on the shared persistent disk, a
            # different process) has since claimed the lease, THIS instance
            # is stale and must stop ticking permanently rather than keep
            # independently load-mutate-saving the shared state/ledger files
            # with no coordination -- see _claim_worker_lease's docstring
            # for the real incident (two concurrently-ticking loops, one on
            # 24-minutes-stale data, corrupting/losing several T+3
            # candidates' resolutions with zero ledger trace) this closes.
            if not _holds_worker_lease(self._instance_id):
                logger.error(
                    f"[MACD2] Worker instance {self._instance_id} superseded by a newer lease holder -- stopping this loop permanently"
                )
                with self._lock:
                    self._last_exception = f"SUPERSEDED_BY_NEWER_WORKER_INSTANCE at {datetime.now(KST).isoformat()}"
                return
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
                else:
                    # 2026-08-20 fix (real incident: MACD2 held a position
                    # with no fresh quotes and STOP_LOSS never fired -- the
                    # quote-updater thread was still is_alive()=True but had
                    # stopped actually producing fresh quotes, permanently.
                    # quote_updater_alive() cannot see this; check staleness
                    # directly on every traded/watched symbol and force a
                    # stop+restart -- the OLD thread, if genuinely stuck
                    # forever, is simply orphaned (daemon=True, harmless) and
                    # a brand-new one takes over, exactly mirroring how
                    # Macd2Service.start() already recovers a stuck Worker
                    # thread.
                    #
                    # 2026-08-21 fix (real incident: Render OOM after this
                    # fired on every single 5s tick for as long as quotes
                    # stayed stale): a genuinely healthy refresh_quotes() call
                    # can now legitimately take much longer than
                    # QUOTE_UPDATER_STALL_AGE_SEC (30s) under sustained KIS
                    # rate limiting -- 3 symbols x up to 8 retry attempts x 5s
                    # mock-mode delay = up to ~120s in the worst case (see
                    # kis_client._get_with_rate_limit_retry). Without a
                    # cooldown, every tick during that window re-triggered
                    # stop+start, abandoning a thread that was still
                    # legitimately working and spawning a fresh one on top of
                    # it (each old one is orphaned, not actually killed, until
                    # its own current blocked call returns) -- over hours this
                    # accumulated dozens of live orphaned threads, all still
                    # polling KIS (worsening the very rate limiting that
                    # caused this), until the container ran out of memory.
                    # Only force a restart once per QUOTE_UPDATER_STALL_AGE_SEC
                    # so a slow-but-working retry sequence gets a real chance
                    # to finish before being abandoned.
                    stalest_age = 0.0
                    for _sym in (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL):
                        _snap = self._market_data.get_quote(_sym)
                        if _snap is not None and _snap.age_sec is not None:
                            stalest_age = max(stalest_age, _snap.age_sec)
                    _since_last_restart = (
                        (datetime.now(KST) - self._last_quote_updater_restart_at).total_seconds()
                        if self._last_quote_updater_restart_at else None
                    )
                    if stalest_age > config.QUOTE_UPDATER_STALL_AGE_SEC and (
                        _since_last_restart is None or _since_last_restart > config.QUOTE_UPDATER_STALL_AGE_SEC
                    ):
                        # 2026-08-24 fix (real incident: Render memory 20%->60%
                        # over ~2h): the 30s cooldown above only throttles how
                        # OFTEN this fires -- it never confirmed the OLD thread
                        # actually died before starting a new one. A stuck KIS
                        # retry chain can legitimately outlive both the 0.5s
                        # join here AND the 30s cooldown under sustained
                        # contention, so the old thread was still orphaned and
                        # running every single time, and a fresh one piled on
                        # top of it each cycle -- net-positive thread
                        # accumulation for as long as the contention lasted.
                        # Only start a replacement once stop_quote_updater()
                        # confirms the old one is actually gone -- UNLESS
                        # staleness has grown past QUOTE_UPDATER_FORCE_REPLACE_
                        # AGE_SEC, comfortably beyond any plausible legitimate
                        # retry chain, in which case this is very likely the
                        # 2026-08-20 incident (a permanently hung call that
                        # will never confirm-stop) and forcing a replacement
                        # anyway is the lesser evil -- one orphan every 5min,
                        # not one every 30s.
                        self._last_quote_updater_restart_at = datetime.now(KST)
                        confirmed_stopped = self._market_data.stop_quote_updater(join_timeout=0.5)
                        if confirmed_stopped or stalest_age > config.QUOTE_UPDATER_FORCE_REPLACE_AGE_SEC:
                            self._market_data.start_quote_updater(interval_sec=1.0)
                        else:
                            logger.warning(
                                "[MACD2] quote-updater stale but old thread still alive after "
                                "join -- skipping restart this cycle to avoid orphaning another thread"
                            )
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
        # 2026-09-03 real incident fix: this check-then-launch was
        # completely unguarded -- if two threads called start() at nearly
        # the same moment (e.g. Macd2Service._auto_recover_worker firing
        # from one Streamlit session's stall-check while another session's
        # request/rerun thread does the same, both seeing is_alive()==False
        # in the brief window right after a restart), BOTH could pass the
        # is_alive() check and each spawn their OWN daemon Thread, with
        # self._thread ending up pointing at only ONE of them -- the OTHER
        # becomes a permanently orphaned, un-stoppable second ticking loop.
        # Real evidence this actually happened: the 2026-09-03 signal ledger
        # shows two rows written ~7 seconds apart with DIFFERENT worker_
        # instance_id but the SAME session_started_at, one of them using
        # completely stale macd_snap data (a bar 24 minutes behind the
        # other) -- consistent with two concurrently-running Worker loops
        # each independently load-mutate-saving the shared state/ledger
        # files with no coordination, silently corrupting/losing several
        # T+3 candidates' resolutions that day. Acquiring self._lock around
        # the whole check-and-launch makes this atomic -- the loser of the
        # race now correctly observes is_alive()==True and returns.
        with self._lock:
            if self.is_alive():
                return  # never spawn a second Worker thread
            # 2026-08-19: marks THIS process as the genuine live Worker so
            # ledger.append_signal/append_execution and state_store.save_state
            # allow writes to the real production paths -- any OTHER caller
            # (an ad-hoc/replay script invoking run_once() directly, never
            # through this class) is refused unless it has redirected those
            # paths itself first (see ledger.py's own docstring for the
            # 2026-08-19 incident this guards against).
            os.environ[ledger.LIVE_WORKER_MARKER_ENV] = str(os.getpid())
            self._stop_event.clear()
            self._started_at = datetime.now(KST)
            # 2026-09-03 real incident fix: claims exclusive "the live
            # writer" status on the SHARED persistent disk (survives across
            # process boundaries, unlike the in-process self._lock above) --
            # unconditionally overwrites any prior claim, since we always
            # want the NEWEST start() call to win. _run_loop re-checks this
            # every tick; a stale/superseded instance (e.g. an old process
            # that Render's restart didn't fully kill yet, or the losing
            # side of the exact in-process race the lock above now prevents
            # for NEW races, but doesn't retroactively fix if one is somehow
            # already running) detects the claim no longer matches its own
            # instance_id and stops ticking permanently rather than
            # continuing to silently corrupt shared state/ledger writes.
            _claim_worker_lease(self._instance_id)
            self._thread = threading.Thread(target=self._run_loop, name="macd2-worker", daemon=True)
            self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> bool:
        """Returns True only if the tick thread is confirmed dead after the
        join -- same orphan-detection reasoning as MarketDataService.
        stop_quote_updater() (2026-08-24 fix)."""
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            self._thread = None  # can't join ourselves; preserves prior behavior
            return True
        thread.join(timeout=join_timeout)
        if thread.is_alive():
            return False
        self._thread = None  # never reused — start() always creates a fresh Thread object
        return True
