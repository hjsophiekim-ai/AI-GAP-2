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
    bar_archive,
    bar_ledger,
    chop_regime,
    config,
    e_strategy,
    early_take_profit,
    peak_protection,
    n1_adaptive,
    ledger,
    major_flag_filter,
    order_executor,
    p3_stack,
    position_sizing,
    premarket_shadow,
    shadow_base,
    risk_exit,
    sideways_filter,
    single_entry_filter,
    small_whipsaw_hold,
    smart_sizing,
    state_store,
    teg_gate,
    time_window_3slot,
    time_window_filter,
    time_window_position_manager,
    trend_persistence_filter,
)
from app.trading.macd2.broker_adapter import BrokerOrderResult
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
    exclude_preopen_padding_1m,
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
# 2026-09-11 실사고 (플래그 전파 누락) — 아래 두 값은 block_reason 표시용
# 라벨이며 판정에 쓰이는 파라미터가 아니다.
#   RESTART_CATCH_UP_REPLAY: 재시작 catch-up replay 가 재구성한 장중 플래그.
#     그 시각에 live tick 이 없었으므로 주문은 애초에 불가능했지만, 플래그
#     자체는 원장에 남아야 한다(09:00 이전 프리마켓 플래그가 이미
#     BEFORE_SESSION_OPEN 으로 남는 것과 같은 취급).
#   RECONCILE_DEFERRED_SUFFIX: reconcile 차단 tick 에서 "주문만" 미룬 플래그의
#     감사용 행에 붙이는 signal_id 접미사. 실제 주문 행은 접미사 없는 원래
#     signal_id 로 남아야 하므로(append_signal 의 signal_id dedup 이 나중 행을
#     조용히 버린다) 반드시 분리한다.
RESTART_CATCH_UP_REPLAY = "RESTART_CATCH_UP_REPLAY"
#: 늦게 완성된 완성봉을 live tick 이 뒤늦게 따라잡아 평가한 경우 (2026-09-16).
#: FLAG 복원 전용 -- 이 경로는 절대 주문을 내지 않는다.
LATE_COMPLETED_BAR_REPLAY = "LATE_COMPLETED_BAR_REPLAY"
RECONCILE_DEFERRED_SUFFIX = ":RECONCILE_DEFERRED"
#: 신규 매수 hard gate (2026-09-21). 브로커 보유수량을 **확실히** 아는
#: 상태에서만 새 포지션을 연다. 청산(매도)은 이 게이트의 대상이 아니다 —
#: 매도를 막으면 보유 포지션이 무방비로 남기 때문이다.
ENTRY_BLOCKED_RECONCILE_UNHEALTHY = "ENTRY_BLOCKED_RECONCILE_UNHEALTHY"
#   TICK_ALREADY_EXECUTED: 이 tick 이 이미 주문을 냈기 때문에 "주문만" 다음
#     tick 으로 미룬 플래그. 플래그 자체는 원장/후보에 정상 기록된다.
TICK_ALREADY_EXECUTED = "TICK_ALREADY_EXECUTED"
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

# ── 2026-10-01 hotfix: POSITION_DATA_ERROR SAFE EXIT ─────────────────────
# 실사고: 11:06 RED -> 11:12 T+3 승인 -> 주문 직전 잔고조회 실패(POSITION_DATA_ERROR)
# -> 보유 인버스 청산까지 막혀 pending(30초) 만료 -> 12분 뒤 손절 -193,691원.
# 원칙: "잔고가 불확실하면 신규 BUY 는 막되, 보유가 확실한 기존 포지션의 EXIT 는
# 막지 않는다." 정상 조회 경로(CASE F)는 바이트 단위로 기존과 같다.
POSITION_DATA_ERROR_RETRY_MAX = 3          # 주문 직전 잔고 재조회 횟수
POSITION_DATA_ERROR_RETRY_DELAY_SEC = 0.5  # 재조회 간격 (KIS 초당 호출 제한 고려)
SAFE_EXIT_CONFIRM_RETRIES = 3              # SAFE EXIT 매도 직후 잔고 확인 시도
SAFE_EXIT_SETTLE_SEC = 60.0                # 매도 후 잔고 반영 대기 (이 동안 다른 주문 차단)
SAFE_EXIT_SOURCE = "SAFE_EXIT_POSITION_DATA_ERROR"
SAFE_EXIT_BUY_BLOCKED = "SAFE_EXIT_SELL_DONE_BUY_BLOCKED"
SAFE_EXIT_SUBMITTED_UNCONFIRMED = "SAFE_EXIT_SUBMITTED_UNCONFIRMED"
CRITICAL_POSITION_DATA_ERROR = "CRITICAL_POSITION_DATA_ERROR"


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

    # ── 09:03 예약매수 arm 유효기간 (2026-09-16 실거래 사고) ──────────────
    # "예약은 그것을 건 날에만 유효하다" 는 불변식은 아래 rollover 분기와
    # **무관하게** 성립해야 한다. 원래는 날짜가 바뀌는 분기 안에만 있어서
    # 두 조기반환(session_date is None / == today) 경로에서는 과거 arm 이
    # 영원히 지워지지 않았다 -- 상태 파일이 복구/이관되거나 스키마가 낡아
    # session_date 가 비어 있으면 몇 달 전 예약도 09:03 에 그대로 발동한다.
    _armed_at = _parse_iso_dt(state.scheduled_entry_armed_at)
    _armed_today = (_armed_at is not None
                    and _armed_at.astimezone(KST).strftime("%Y%m%d") == today_str)
    if state.scheduled_entry_armed_direction is not None and not _armed_today:
        state.scheduled_entry_armed_direction = None
        state.scheduled_entry_armed_at = None
        state.scheduled_entry_armed_by = None

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
    # 오늘 평가한 봉 집합도 session-scoped (models.RuntimeState 주석 참고).
    state.evaluated_bar_ts_today = []
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
    # W1a 사이징(2026-09-12)의 누적 exposure / 진입순번 / 첫거래 손절
    # 플래그도 session-scoped -- 토글은 그대로 두고 값만 리셋한다.
    position_sizing.reset_daily(state)
    # E(2026-10-05): 걸려 있던 돌파 대기는 날짜가 바뀌면 폐기한다. RS 표본은
    # 과거 영업일 자료이므로 유지한다(토글/모드도 그대로).
    e_strategy.reset_daily(state)
    # H50(2026-09-15) HOLD 상태도 session-scoped -- 토글은 그대로 두고
    # 보류 상태만 리셋한다. 날짜가 바뀌면 전날 HOLD 를 이어받지 않는다.
    small_whipsaw_hold.clear(state)
    # C1 Peak Protection(2026-09-19)도 session-scoped -- 토글은 그대로 두고
    # arm/MFE/멱등키만 리셋한다. 전날 arm 상태를 다음날로 넘기지 않는다.
    peak_protection.clear(state)
    # N1(2026-09-20) adaptive 캐시도 session-scoped -- 전략 선택 토글은
    # 그대로 두고 판정 캐시만 리셋한다.
    n1_adaptive.clear(state)
    # P3(2026-09-27) 의 포지션 regime 스냅샷도 session-scoped -- 전략 모드
    # 선택은 그대로 두고 B3/rescue/승격 상태만 리셋한다. 섀도우 **완료거래
    # ledger 는 건드리지 않는다**(detector 가 날짜를 넘어 이어져야 하므로).
    p3_stack.clear_position(state)
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
    # (arm 유효기간 검사는 이 함수 맨 위에서 분기와 무관하게 이미 끝났다)
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


def _record_catchup_flag(state: RuntimeState, snap, direction: Direction, now: datetime,
                        *, reason: Optional[str] = None) -> None:
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
    only for TODAY's bars — ledger.append_signal's own signal_id
    dedup makes replaying the same bar across multiple restarts a safe no-op.

    2026-09-11 fix (실사고): this used to return early for any bar at/after
    SESSION_OPEN, so the catch-up walk recorded ONLY premarket flags. An
    INTRADAY flag reconstructed by the same walk therefore updated
    last_detected_direction/latest_primary_flag while leaving zero trace in
    the signal ledger — the UI's own 신호 통계 panel
    (compute_today_signal_overview, a pure recompute) still showed it, so the
    two disagreed and a genuinely detected flag looked like it had never
    happened (2026-09-10: UI last FLAG EVENT 18:54 UP_RED vs signal ledger
    last row 17:21 UP_RED). A detected direction change must always be
    persisted regardless of whether an order was possible, so an intraday
    catch-up bar is now recorded too, tagged RESTART_CATCH_UP_REPLAY to keep
    it distinguishable from a live-tick flag: there was no live tick at that
    moment, so no order was ever possible for it either way. Nothing about
    which flags are DETECTED, or which orders are placed, changes here.
    """
    bar_kst = snap.bar_dt.astimezone(KST)
    if bar_kst.date() != now.astimezone(KST).date():
        return
    block_reason = reason or (
        "BEFORE_SESSION_OPEN" if bar_kst.time() < config.SESSION_OPEN else RESTART_CATCH_UP_REPLAY
    )
    signal_id = make_signal_id(snap.bar_dt, direction)
    outcome = order_executor.ExecutionOutcome(
        signal_id=signal_id, direction=direction,
        target_symbol=order_executor.target_symbol_for_direction(direction),
        final_state=SignalState.BLOCKED, block_reason=block_reason,
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
    # 관측 전용 (2026-09-08): market_data 는 worker 를 import 하지 않으므로
    # 아카이브가 쓸 instance_id 를 이 MarketDataService **인스턴스**에 심는다.
    # 모듈 전역이 아니라 인스턴스 속성이어야 동시 Worker 환경에서 서로의
    # 값을 덮어쓰지 않는다(2026-09-03 dual-Worker 사고, commit 790cea6).
    try:
        market_data.observed_worker_instance_id = str(worker_instance_id or "")
    except Exception:
        pass
    state.last_executed_direction = None
    state.current_episode_direction = None
    state.processed_signal_ids = []

    df_1m = market_data.get_history_df()
    # 2026-10-01 hotfix: 신호 입력에서 08:50~08:59 단일가 padding 제외 (live 와 같은 입력)
    _sig_1m, _ = exclude_preopen_padding_1m(df_1m)
    bars_3m = resample_completed_3m(_sig_1m, now=now)
    # 2026-09-16 수정: restart catch-up 도 live 와 **같은 완성봉 프레임**을 써야
    # 한다. 여기만 filter_complete_3m_bars 가 빠져 있어서(같은 파일의 다른 호출부
    # 는 전부 적용한다) catch-up 은 구성 1분봉이 모자란 불완전 봉까지 EMA 에
    # 넣고 계산했다. EMA 는 누적이라 그 한 봉이 **이후 모든 봉의 macd/signal/gap
    # 을 바꾼다** -- 2026-09-16 실데이터에서 같은 09:00 봉의 prev_gap 이
    # live -475.894 vs catch-up -185.374 로 갈렸다.
    #
    # 이게 위험한 이유: 아래 walk 는 봉마다 state.last_confirmed_bar_ts 에
    # 도장을 찍고 state.last_detected_direction 을 덮어쓴다. 잘못된 프레임으로
    # 낸 판정이 그대로 live 상태가 되고, 도장 때문에 live 경로는 그 봉을
    # **다시 평가할 수 없다**(_advance_confirmed_primary 의 bar_key 중복방지).
    # 부호를 걸치는 날이면 두 경로가 방향 자체를 다르게 판정한다.
    #
    # MACD 계산식도 resample 규칙도 바꾸지 않는다 -- live 가 이미 쓰고 있는
    # 같은 입력을 catch-up 에도 똑같이 주는 것뿐이다.
    bars_3m, _catchup_dropped = filter_complete_3m_bars(bars_3m, _sig_1m)
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
                _record_catchup_flag(state, snap, direction, now)
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
                elif time_window_3slot.is_3slot_enabled(state):
                    # 2026-09-11 fix (사용자 요청): the 2026-08-21 rationale in
                    # the TW2/TEG branch above applies to the 3-SLOT modes
                    # (TW2 3-SLOT / TWF 3-SLOT — the ones actually live since
                    # 2026-09-01) word for word, but they were never added to
                    # it. A restart catch-up under 3-SLOT therefore fell
                    # through to the bare ``pending_signal`` below, and
                    # ``_execute_or_wait``'s pending path never consults
                    # ``_judge_entry_gate`` at all — so that entry went in with
                    # NO T+3 re-confirmation, NO quality score, NO TEG gate and
                    # NO slot accounting, completely bypassing the filter the
                    # user has turned on. Route it through this mode's OWN
                    # pending-candidate slot instead: pure bookkeeping, no
                    # order, and ``_resolve_tw2_3slot_candidate`` then runs the
                    # real T+3 -> quality/TEG -> slot decision on a later tick,
                    # byte-identical to a live-detected flag.
                    #
                    # Same "a candidate already on state always wins" rule as
                    # the TW2/TEG branch (and the same reason — 2026-08-24
                    # incident): the slot is reloaded from disk so it survives
                    # the restart and is by construction never older than what
                    # this abbreviated replay can find, which is also what
                    # keeps a second back-to-back restart from discarding a
                    # still-pending candidate.
                    if state.tw2_3slot_pending_flag_direction is None:
                        state.tw2_3slot_pending_flag_direction = last_direction
                        state.tw2_3slot_pending_flag_bar_ts = last_flag_snap.bar_dt.isoformat()
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
                    _record_catchup_flag(state, snap, direction, now)
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
    # 2026-10-01 hotfix: live 와 같은 신호 입력 (08:50~08:59 padding 제외)
    _sig_1m, _ = exclude_preopen_padding_1m(df_1m)
    bars_3m = resample_completed_3m(_sig_1m, now=now)
    bars_3m, _dropped = filter_complete_3m_bars(bars_3m, _sig_1m)
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


# ── 재계산 플래그 vs 신호원장 대조 (2026-09-22, **표시 전용**) ──────────────
# `compute_today_signal_overview` 는 호출될 때마다 오늘 전체를 처음부터 다시
# 걷는 **순수 재계산**이다. MACD EMA 는 누적이고 `evaluate_macd_crossover` 는
# `last_direction` 기반 dedup 을 하므로, 하루 중 **어느 1분봉 하나만 늦게
# 들어오거나 빠져도** 그 뒤의 모든 플래그가 재배열된다 — 한 번 화면에 떴던
# 과거 플래그가 다른 시각으로 옮겨가며 사라진다(2026-09-22 실사고: 12:09
# DOWN_BLUE 가 표시됐다가 소멸. 재현: 12:05 를 빼면 12:09D -> 12:12D,
# 12:09/12:10/12:11 중 하나를 빼면 12:09D -> 12:15D 로 이동).
#
# 그래서 "마지막 플래그" 를 재계산 결과로 보여주면 **repaint 된다**. 아래
# 함수는 재계산 결과를 **신호원장(= 워커가 실시간으로 확정해 기록한 불변
# 이벤트)** 과 대조해 표시용 origin 을 확정한다.
#
#   LIVE_CONFIRMED   원장 대응행이 있다 -> 워커가 실제로 평가/기록했다
#   RECOMPUTED_ONLY  재계산에만 있다   -> 워커가 본 적 없다(주문 권한 없었음)
#   LEDGER_ONLY      원장에만 있다     -> 재계산에서 사라졌지만 **지우지 않는다**
#
# **표시 전용이다.** 주문/진입/청산/슬롯/N1/C1/H50/사이징 경로는 이 함수를
# 호출하지 않으며, 반환값이 그쪽으로 흘러갈 수 있는 경로가 존재하지 않는다.
ORIGIN_RECOMPUTED_ONLY = "RECOMPUTED_ONLY"
ORIGIN_LEDGER_ONLY = "LEDGER_ONLY"

#: 원장 signal_id 는 T+3 확정/감사행에서 접미사가 붙는다 — 대조는 접미사를
#: 떼어낸 **기본 signal_id**(= make_signal_id(bar_dt, direction)) 로 한다.
def base_signal_id(signal_id: Any) -> str:
    raw = str(signal_id or "")
    return raw.split(":", 1)[0] if ":" in raw else raw


def _ledger_row_bar_at(row: dict[str, Any]) -> str:
    """원장 행의 바 시각 (ISO, KST) -- **실제 신호원장 컬럼**에서만 읽는다.

    2026-09-28 fix (실사고: 재배포 직후 "마지막 FLAG EVENT" 가 08:00 BLUE 로
    되돌아감, 실제 마지막은 14:09 BLUE). 예전 코드는 원장에 **존재하지 않는**
    ``confirmed_bar_at`` / ``bar_start_at`` / ``flag_bar_at`` 을 읽어서
    LEDGER_ONLY 행의 시각이 늘 빈 문자열이 됐고, 그래서 LEDGER_ONLY 는 절대
    마지막 플래그가 될 수 없었다. 재시작 직후 재계산이 repaint 되면 원장의
    최신 플래그들이 전부 LEDGER_ONLY 로 밀려나 오래된 일치 이벤트만 남았다.

    우선순위: ``signal_bar_at`` (ISO) -> ``trading_date`` + ``completed_bar_at``
    (YYYYMMDD + HHMMSS). 둘 다 없으면 빈 문자열(후보 제외 -- 추정하지 않는다).
    """
    raw = str(row.get("signal_bar_at") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw).astimezone(KST).isoformat()
        except ValueError:
            pass
    day = str(row.get("trading_date") or "").strip()
    hms = str(row.get("completed_bar_at") or "").strip()
    if len(day) == 8 and day.isdigit() and len(hms) == 6 and hms.isdigit():
        try:
            return datetime.strptime(day + hms, "%Y%m%d%H%M%S").replace(tzinfo=KST).isoformat()
        except ValueError:
            pass
    return ""


def reconcile_signal_overview_with_ledger(
    overview: list[dict[str, Any]], ledger_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """재계산 overview + 원장 행 -> 표시용 플래그 이벤트 목록 (순수 함수).

    ``ledger_rows`` 는 오늘자 신호원장 행. ``signal_id`` 의 접미사를 떼고
    맞춘다. 반환 행은 원본 필드에 ``origin`` / ``in_ledger`` /
    ``in_recompute`` 를 얹은 것이고, bar 시작시각 오름차순으로 정렬된다.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for row in overview or ():
        key = base_signal_id(row.get("signal_id"))
        if not key:
            continue
        item = dict(row)
        item["in_recompute"] = True
        item["in_ledger"] = False
        by_id[key] = item
    for row in ledger_rows or ():
        key = base_signal_id(row.get("signal_id"))
        if not key:
            continue
        hit = by_id.get(key)
        bar_at = _ledger_row_bar_at(row)
        if hit is not None:
            hit["in_ledger"] = True
            # 같은 플래그의 원장 행이 여러 개(플래그봉 행 + ":TW_CONFIRM" 확정봉 행)
            # 이면 **가장 이른** 시각 = 플래그봉을 쓴다. 재계산 행의 시각은 건드리지 않는다.
            if (not hit["in_recompute"] and bar_at
                    and (not hit["bar_start_at"] or bar_at < hit["bar_start_at"])):
                hit["bar_start_at"] = bar_at
            continue
        by_id[key] = {
            "signal_id": key,
            "bar_start_at": bar_at,
            "bar_end_at": row.get("bar_end_at") or "",
            "direction": row.get("direction") or row.get("confirmed_direction") or "",
            "in_recompute": False,
            "in_ledger": True,
        }
    out: list[dict[str, Any]] = []
    for item in by_id.values():
        if item["in_ledger"] and item["in_recompute"]:
            item["origin"] = ORIGIN_LIVE_CONFIRMED
        elif item["in_ledger"]:
            item["origin"] = ORIGIN_LEDGER_ONLY
        else:
            item["origin"] = ORIGIN_RECOMPUTED_ONLY
        out.append(item)
    out.sort(key=lambda r: str(r.get("bar_start_at") or ""))
    return out


def latest_ledger_backed_flag(events: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """표시용 '마지막 플래그' — **원장이 뒷받침하는 이벤트만** 고른다.

    RECOMPUTED_ONLY 는 워커가 본 적 없는 신호라 authoritative 가 될 수 없다.
    원장 행이 하나도 없으면 None 을 돌려주고, 호출부는 재계산 값을 쓰되
    화면에 '재계산 전용' 이라고 명시한다.
    """
    backed = [e for e in (events or ())
              if e.get("origin") in (ORIGIN_LIVE_CONFIRMED, ORIGIN_LEDGER_ONLY)
              and str(e.get("bar_start_at") or "")]
    if not backed:
        return None
    return max(backed, key=lambda e: str(e.get("bar_start_at")))


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

    # 2026-10-01 hotfix: 마지막 '성공한' 잔고조회 스냅샷 (SAFE EXIT 의 보유 확인 근거).
    state.last_good_broker_positions = {sym: int(row["qty"]) for sym, row in broker_positions.items()}
    state.last_good_broker_epoch = int(state.position_epoch or 0)
    state.last_good_broker_at = now.isoformat()
    _safe_exit_result = _resolve_safe_exit_after_recovery(broker, state, broker_positions, now, diag)
    if _safe_exit_result is not None:
        return _safe_exit_result

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
            # 2026-09-21 실사고: 여기서 position/peak/profit_lock 만 지우고
            # H50/C1/N1/whipsaw-watch 를 남겨둔 것이 원인이었다. 브로커가 flat
            # 이라고 **확인**한 이 시점이 곧 포지션 종료이므로, 시스템이 스스로
            # 청산했을 때와 똑같이 position-scoped 상태를 전부 끝낸다.
            # (수동 KIS 매도처럼 시스템 밖에서 사라져도 다음 거래로 넘어가지 않는다)
            _clear_position_scoped_state(state, reason=RECOVERED_TO_FLAT)
            diag.update({"comparison_result": RECOVERED_TO_FLAT, "mismatch_reason": "runtime_position_broker_flat"})
            state.position_reconcile_diag = diag
            state.last_position_reconcile_at = now.isoformat()
            return RECOVERED_TO_FLAT

    if runtime["qty"] <= 0 and broker_owned:
        recovered = broker_owned[0]
        # 2026-09-21: 새 포지션의 시작 — 이전 포지션의 position-scoped
        # 상태(H50/C1/N1/whipsaw-watch)를 **전부** 끝내고 epoch 을 올린다.
        # 반드시 아래 필드 세팅보다 먼저 와야 한다(이 함수가 초기화한다).
        _begin_position_epoch(state, reason="RECONCILE_ADOPT")
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
        # 2026-10-01 hotfix: 잔고조회 실패로 대기하던 신호가 끝내 만료되면 원장에 WAITING 만
        # 남지 않게 최종 상태로 갱신한다(다른 대기 사유는 기존 그대로).
        if pending.get("reason") == POSITION_DATA_ERROR:
            _finalize_waiting_signal_row(
                str(pending.get("signal_id") or ""), order_result=SignalState.BLOCKED.value,
                block_reason=f"{state.order_block_reason or POSITION_DATA_ERROR}_EXPIRED",
            )
        return True
    return False


def _finalize_waiting_signal_row(signal_id: str, *, order_result: str, block_reason: str) -> bool:
    """원장의 WAITING 행 하나를 다른 컬럼은 보존한 채 최종 결과로 갱신한다."""
    if not signal_id:
        return False
    try:
        rows = [r for r in ledger._load_rows(ledger.SIGNAL_LEDGER_PATH, limit=0)
                if r.get("signal_id") == signal_id]
    except Exception:
        return False
    if not rows or str(rows[-1].get("order_result") or "") != SignalState.WAITING.value:
        return False
    row = dict(rows[-1])
    row.update({"order_result": order_result, "block_reason": block_reason,
                "final_result": f"{order_result}:{block_reason}"})
    return bool(ledger.append_signal(row))


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


def _sell_reconcile_query_failed(outcome) -> bool:
    """매도는 접수됐지만 체결 확인 조회가 전부 실패해 보유수량을 모르는 결과인가."""
    return bool(
        outcome is not None
        and outcome.final_state == SignalState.FAILED
        and outcome.block_reason == order_executor.FAIL_SELL_RECONCILE_QUERY_ERROR
    )


def _position_certain_reasons(state: RuntimeState) -> list[str]:
    """잔고조회 실패 중에도 '보유가 확실하다' 고 볼 수 없는 이유 목록 (빈 목록 = 확실)."""
    pos = state.position
    last = dict(state.last_good_broker_positions or {})
    reasons: list[str] = []
    if pos is None or not pos.symbol or int(pos.quantity or 0) <= 0:
        return ["NO_LOCAL_POSITION"]
    if pos.symbol not in config.TRADE_SYMBOLS:
        reasons.append("UNKNOWN_SYMBOL")
    if int(last.get(pos.symbol, 0)) != int(pos.quantity or 0):
        reasons.append("LAST_BROKER_SNAPSHOT_MISMATCH")
    if state.last_good_broker_epoch is None or int(state.last_good_broker_epoch) != int(state.position_epoch or 0):
        reasons.append("EPOCH_MISMATCH")
    return reasons


class _SafeExitUnconfirmed(Exception):
    """보호 매도는 나갔지만 잔고조회 실패로 체결을 확인할 수 없다 (가짜 청산 기록 금지)."""


class _SafeExitGuardBroker:
    """POSITION_DATA_ERROR tick 전용 브로커 래퍼 (2026-10-01 hotfix).

    기존 청산 판정(_advance_held_position_risk_management)을 그대로 돌리되 주문만 보호한다:
      * SELL 은 현재 보유 종목·**전량**·1회만 통과 (부분매도/두 번째 매도 차단)
      * BUY 는 전부 차단
      * SELL 이 나간 뒤의 잔고 확인은 _SafeExitUnconfirmed 로 끊는다 -> 체결 확인 전에는
        포지션/원장을 바꾸지 않고 state.safe_exit(SUBMITTED) 만 남긴다. 다음 정상 잔고조회에서
        _resolve_safe_exit_after_recovery 가 실제 잔고로 마무리한다.
    """

    def __init__(self, inner, state: RuntimeState, now: datetime) -> None:
        self._inner = inner
        self._state = state
        self._now = now
        self.sold = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _blocked(self, symbol, side, qty, why):
        return BrokerOrderResult(False, "", str(symbol), side, int(qty or 0), 0, 0.0, why)

    def buy_market(self, symbol, qty, client_order_id):
        return self._blocked(symbol, "BUY", qty, "BUY_BLOCKED_POSITION_DATA_ERROR")

    def buy_limit(self, symbol, qty, price, client_order_id):
        return self._blocked(symbol, "BUY", qty, "BUY_BLOCKED_POSITION_DATA_ERROR")

    def buy_ioc_limit(self, symbol, qty, price, client_order_id):
        return self._blocked(symbol, "BUY", qty, "BUY_BLOCKED_POSITION_DATA_ERROR")

    def sell_market(self, symbol, qty, client_order_id):
        st = self._state
        pos = st.position
        se = st.safe_exit or {}
        pending_same_epoch = (se.get("status") == "SUBMITTED"
                              and int(se.get("epoch", -1)) == int(st.position_epoch or 0))
        if (self.sold or pos is None or symbol != pos.symbol or int(qty) != int(pos.quantity or 0)
                or pending_same_epoch):
            return self._blocked(symbol, "SELL", qty, "SELL_BLOCKED_POSITION_DATA_ERROR")
        res = self._inner.sell_market(symbol, qty, client_order_id)
        if res.success:
            self.sold = True
            cid = str(client_order_id or "")
            reason = cid.split(":")[1] if cid.startswith("EXIT:") and cid.count(":") >= 2 else "PROTECTIVE_EXIT"
            epoch = int(st.position_epoch or 0)
            st.safe_exit = {
                "signal_id": f"PROTECTIVE_EXIT:{epoch}:{reason}", "epoch": epoch, "symbol": symbol,
                "qty": int(qty), "avg_price": float(pos.avg_price or 0.0), "order_id": str(res.order_id or ""),
                "executed_price": float(res.executed_price or 0.0), "raw": dict(res.raw or {}),
                "submitted_at": self._now.isoformat(), "status": "SUBMITTED", "exit_reason": reason,
                "kind": "PROTECTIVE",
            }
            try:
                state_store.save_state(st)
            except Exception as exc:  # pragma: no cover
                logger.warning("[MACD2] protective exit state save failed: %s", exc)
        return res

    def reconcile_position(self, symbol):
        if self.sold:
            raise _SafeExitUnconfirmed(symbol)
        return self._inner.reconcile_position(symbol)

    def get_positions(self):
        if self.sold:
            raise _SafeExitUnconfirmed("positions")
        return self._inner.get_positions()


def _protective_exit_during_position_data_error(*, broker, state: RuntimeState, market_data,
                                                now: datetime, result) -> bool:
    """잔고조회 실패 tick 에서도 보유가 확실한 포지션의 청산 판정(강제청산/손절/익절)을 돌린다.

    판정 로직·임계값은 기존 _advance_held_position_risk_management 그대로이고 주문만
    _SafeExitGuardBroker 로 보호한다. 보유가 불확실하거나 이미 보호 매도가 나가 체결 확인
    대기 중이면 아무것도 하지 않는다. 반환: 보호 매도를 냈는가.
    """
    pos = state.position
    if pos is None or int(pos.quantity or 0) <= 0:
        return False
    se = state.safe_exit or {}
    if se.get("status") == "SUBMITTED":
        result.actions.append("PROTECTIVE_EXIT_WAITING_CONFIRMATION")
        return False
    reasons = _position_certain_reasons(state)
    if reasons:
        state.position_reconcile_diag = dict(state.position_reconcile_diag or {},
                                             protective_exit_skipped=",".join(reasons))
        return False
    quotes = _fresh_quote_prices(market_data, (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL))
    guard = _SafeExitGuardBroker(broker, state, now)
    try:
        _advance_held_position_risk_management(
            broker=guard, state=state, market_data=market_data, now=now,
            quotes=quotes, pos=pos, result=result,
        )
    except _SafeExitUnconfirmed:
        pass
    except Exception:
        logger.exception("[MACD2] protective exit during POSITION_DATA_ERROR failed")
    if guard.sold:
        result.actions.append(f"PROTECTIVE_EXIT_SUBMITTED:{(state.safe_exit or {}).get('exit_reason')}")
    return guard.sold


def _safe_exit_fill_price(broker, se: dict) -> float:
    """SAFE EXIT 매도의 실제 체결가: 당일 체결조회(주문번호 일치) > 주문 응답 > 현재가 > 평단."""
    order_id = str(se.get("order_id") or "")
    getter = getattr(broker, "get_today_fills", None)
    if order_id and getter is not None:
        try:
            fills = dict(getter(str(se.get("symbol") or "")) or {}).get("fills") or []
        except Exception:
            fills = []
        for f in fills:
            if str(f.get("order_id") or f.get("odno") or "") == order_id:
                for k in ("price", "avg_price", "avg_prvs"):
                    try:
                        v = float(f.get(k) or 0.0)
                    except (TypeError, ValueError):
                        v = 0.0
                    if v > 0:
                        return v
    if float(se.get("executed_price") or 0.0) > 0:
        return float(se["executed_price"])
    quote = order_executor._fallback_sell_price(broker, str(se.get("symbol") or ""))
    return float(quote or se.get("avg_price") or 0.0)


def _safe_exit_record_leg(broker, state: RuntimeState, se: dict, *, sold_qty: int,
                          position_before: int, position_after: int, now: datetime) -> None:
    """실제 주문번호로 SELL 레그를 기록한다 (추정 RECONCILE 행이 아니다)."""
    price = _safe_exit_fill_price(broker, se)
    order_result = BrokerOrderResult(
        True, str(se.get("order_id") or ""), str(se.get("symbol") or ""), "SELL",
        int(se.get("qty") or 0), int(sold_qty), float(price), "SAFE_EXIT",
        dict(se.get("raw") or {}),
    )
    order_executor._record_leg(
        broker_mode=broker.mode, signal_id=str(se.get("signal_id") or ""),
        symbol=str(se.get("symbol") or ""), side="SELL", qty=int(sold_qty), price=float(price),
        position_before=int(position_before), position_after=int(position_after),
        exit_reason=str(se.get("exit_reason") or config.EXIT_OPPOSITE_SIGNAL), order_result=order_result,
        entry_price=float(se.get("avg_price") or 0.0), confirmed_at=now.isoformat(),
        source=SAFE_EXIT_SOURCE,
    )
    se["recorded_qty"] = int(se.get("recorded_qty") or 0) + int(sold_qty)
    se["fill_price"] = float(price)


def _resolve_safe_exit_after_recovery(broker, state: RuntimeState, broker_positions: dict,
                                      now: datetime, diag: dict) -> Optional[str]:
    """잔고조회가 다시 성공했을 때, 체결 확인 전인 SAFE EXIT 매도를 실제 잔고로 마무리한다.

    잔고 0 -> 실제 주문번호로 SELL 기록 + 포지션 종료 (RECOVERED_TO_FLAT).
    일부만 감소 -> 감소분 기록, 잔량만 계속 관리, 반대 BUY 금지 (RECOVERED_QTY_MISMATCH).
    그대로 -> SAFE_EXIT_SETTLE_SEC 동안은 POSITION_DATA_ERROR 로 tick 을 막아 중복 매도를
    막고, 그 뒤에도 그대로면 미체결로 보고 기존 관리로 돌아간다.
    SAFE EXIT 가 없으면 None (기존 reconcile 그대로).
    """
    se = state.safe_exit
    if not se or se.get("status") != "SUBMITTED":
        return None
    sym = str(se.get("symbol") or "")
    orig = int(se.get("qty") or 0)
    held = int((broker_positions.get(sym) or {}).get("qty") or 0)
    if held <= 0:
        _safe_exit_record_leg(broker, state, se, sold_qty=orig, position_before=orig, position_after=0, now=now)
        se["status"] = "CONFIRMED"
        se["confirmed_at"] = now.isoformat()
        se["confirmed_via"] = "RECONCILE"
        if state.position is not None and state.position.symbol == sym:
            state.position = None
            _clear_position_scoped_state(state, reason="SAFE_EXIT_CONFIRMED")
            _record_major_exit(state, sym)
        diag.update({"comparison_result": RECOVERED_TO_FLAT, "mismatch_reason": "safe_exit_confirmed_flat"})
        state.position_reconcile_diag = diag
        state.last_position_reconcile_at = now.isoformat()
        return RECOVERED_TO_FLAT
    if held < orig:
        _safe_exit_record_leg(broker, state, se, sold_qty=orig - held, position_before=orig,
                              position_after=held, now=now)
        se["status"] = "PARTIAL"
        se["remaining_qty"] = held
        se["confirmed_at"] = now.isoformat()
        if state.position is not None and state.position.symbol == sym:
            state.position = dataclasses.replace(state.position, quantity=held)
        state.pending_signal = None   # 잔량 관리 중 반대 BUY 금지
        diag.update({"comparison_result": RECOVERED_QTY_MISMATCH,
                     "mismatch_reason": f"safe_exit_partial_{orig}_to_{held}"})
        state.position_reconcile_diag = diag
        state.last_position_reconcile_at = now.isoformat()
        return RECOVERED_QTY_MISMATCH
    try:
        age = (now - datetime.fromisoformat(str(se.get("submitted_at")))).total_seconds()
    except (TypeError, ValueError):
        age = SAFE_EXIT_SETTLE_SEC
    if age < SAFE_EXIT_SETTLE_SEC:
        diag.update({"comparison_result": POSITION_DATA_ERROR, "mismatch_reason": "safe_exit_settling"})
        state.position_reconcile_diag = diag
        state.last_position_reconcile_at = now.isoformat()
        return POSITION_DATA_ERROR
    se["status"] = "NOT_FILLED"
    se["resolved_at"] = now.isoformat()
    return None


def _safe_exit_on_position_data_error(broker, state: RuntimeState, *, signal_id: str,
                                      direction: Direction, now: datetime,
                                      trace: dict) -> Optional[order_executor.ExecutionOutcome]:
    """잔고 재조회 3회가 모두 실패했을 때, 보유가 확실한 기존 포지션만 매도한다.

    조건이 하나라도 불확실하면 None (SELL 도 BUY 도 하지 않음 -> 호출부가 CRITICAL 기록).
    같은 포지션/같은 signal 에 SELL 은 최대 1회: position_epoch + state.safe_exit +
    디스크 dispatch claim(재시작 후에도 유지) + signal_id_has_leg 로 막는다.
    """
    pos = state.position
    epoch = int(state.position_epoch or 0)
    last = dict(state.last_good_broker_positions or {})
    reasons: list[str] = _position_certain_reasons(state)
    se_prev = state.safe_exit or {}
    if se_prev and (se_prev.get("status") == "SUBMITTED"
                    or (int(se_prev.get("epoch", -1)) == epoch and se_prev.get("status") in ("CONFIRMED", "PARTIAL"))):
        reasons.append("SAFE_EXIT_ALREADY_ATTEMPTED")
    if signal_id in (state.processed_signal_ids or []):
        reasons.append("SIGNAL_ALREADY_PROCESSED")
    if ledger.signal_id_has_leg(signal_id, "SELL"):
        reasons.append("SELL_LEG_ALREADY_RECORDED")
    info = {
        "original_signal_id": signal_id, "approved_exit_reason": config.EXIT_OPPOSITE_SIGNAL,
        "local_qty": int(pos.quantity or 0) if pos else 0, "symbol": pos.symbol if pos else None,
        "last_known_broker_qty": int(last.get(pos.symbol, 0)) if pos else 0,
        "last_good_broker_at": state.last_good_broker_at, "position_epoch": epoch,
        "buy_blocked_reason": SAFE_EXIT_BUY_BLOCKED,
    }
    if reasons:
        info.update({"attempted": False, "reasons": reasons})
        trace["safe_exit"] = info
        trace["failure_stage"] = f"{CRITICAL_POSITION_DATA_ERROR}:{','.join(reasons)}"
        return None
    if not ledger.try_claim_signal_dispatch(signal_id, "SELL"):
        info.update({"attempted": False, "reasons": ["SELL_CLAIM_EXISTS"]})
        trace["safe_exit"] = info
        trace["failure_stage"] = f"{CRITICAL_POSITION_DATA_ERROR}:SELL_CLAIM_EXISTS"
        return None
    target = order_executor.target_symbol_for_direction(direction)
    timestamps = {"evaluated_at": now.isoformat(), "sell_requested_at": datetime.now(KST).isoformat()}
    sell = broker.sell_market(pos.symbol, int(pos.quantity), f"{signal_id}:SAFE_EXIT:{pos.symbol}")
    state.safe_exit = {
        "signal_id": signal_id, "epoch": epoch, "symbol": pos.symbol, "qty": int(pos.quantity),
        "exit_reason": config.EXIT_OPPOSITE_SIGNAL, "kind": "SWITCH",
        "avg_price": float(pos.avg_price or 0.0), "order_id": str(sell.order_id or ""),
        "executed_price": float(sell.executed_price or 0.0), "raw": dict(sell.raw or {}),
        "submitted_at": now.isoformat(), "status": "SUBMITTED" if sell.success else "SUBMIT_FAILED",
    }
    info.update({"attempted": True, "sell_order_id": str(sell.order_id or ""), "sell_success": bool(sell.success)})
    outcome = order_executor.ExecutionOutcome(signal_id, direction, target, SignalState.FAILED,
                                              timestamps=timestamps)
    outcome.sell_result = sell
    if not sell.success:
        ledger.release_signal_dispatch_claim(signal_id, "SELL")
        outcome.block_reason = order_executor.FAIL_SELL
        trace["safe_exit"] = info
        trace["failure_stage"] = f"SAFE_EXIT:SUBMIT_FAILED:order={sell.order_id}"
        return outcome
    try:   # 재시작해도 이 매도를 다시 내지 않도록 즉시 영속화 (claim 파일과 이중 보호)
        state_store.save_state(state)
    except Exception as exc:  # pragma: no cover - 저장 실패가 이미 나간 매도를 되돌리지 않는다
        logger.warning("[MACD2] SAFE EXIT state save failed: %s", exc)
    timestamps["sell_confirmed_at"] = datetime.now(KST).isoformat()
    qty_after = None
    for i in range(SAFE_EXIT_CONFIRM_RETRIES):
        try:
            qty_after = int(broker.reconcile_position(pos.symbol))
        except Exception:
            qty_after = None
        if qty_after == 0:
            break
        if i < SAFE_EXIT_CONFIRM_RETRIES - 1 and POSITION_DATA_ERROR_RETRY_DELAY_SEC > 0:
            time.sleep(POSITION_DATA_ERROR_RETRY_DELAY_SEC)
    info["confirm_qty_after"] = qty_after
    if qty_after == 0:
        se = state.safe_exit
        _safe_exit_record_leg(broker, state, se, sold_qty=int(se["qty"]), position_before=int(se["qty"]),
                              position_after=0, now=now)
        se["status"] = "CONFIRMED"
        se["confirmed_at"] = now.isoformat()
        se["confirmed_via"] = "IMMEDIATE"
        exited = pos.symbol
        state.position = None
        _clear_position_scoped_state(state, reason="SAFE_EXIT_CONFIRMED")
        _record_major_exit(state, exited)
        outcome.sell_qty_after = 0
        outcome.final_state = SignalState.BLOCKED      # 매도 완료, 반대 BUY 는 차단
        outcome.block_reason = SAFE_EXIT_BUY_BLOCKED
        outcome.balance_qty = 0
        info.update({"sell_fill_qty": int(se["qty"]), "status": "CONFIRMED"})
        trace["safe_exit"] = info
        trace["balance_qty"] = 0
        trace["failure_stage"] = (f"SAFE_EXIT:CONFIRMED:order={sell.order_id}:sold={se['qty']}:"
                                  f"epoch={epoch}:retry={trace.get('position_data_error_retries')}")
        return outcome
    # 매도는 나갔지만 체결/잔고 확인을 못 했다 -> 포지션은 그대로 두고(가짜 청산 기록 금지)
    # 다음 정상 잔고조회에서 _resolve_safe_exit_after_recovery 가 실제 잔고로 마무리한다.
    outcome.sell_qty_after = qty_after if qty_after is not None else -1
    outcome.block_reason = SAFE_EXIT_SUBMITTED_UNCONFIRMED
    info.update({"status": "SUBMITTED_UNCONFIRMED"})
    trace["safe_exit"] = info
    trace["failure_stage"] = f"SAFE_EXIT:SUBMITTED_UNCONFIRMED:order={sell.order_id}:epoch={epoch}"
    return outcome


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
    budget_multiplier: float = 1.0,
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
    # 2026-10-01 hotfix: 잔고조회 실패면 바로 포기하지 않고 짧게 최대 3회 재조회한다.
    # 재조회는 이 함수 안에서 동기로 끝나므로 같은 tick/주문의 중복 실행이 없고, 그 동안
    # 어떤 주문도 나가지 않는다. 하나라도 성공하면 아래 기존 분기를 그대로 탄다.
    _pde_retries: list[str] = []
    if reconcile == POSITION_DATA_ERROR:
        for _ in range(POSITION_DATA_ERROR_RETRY_MAX):
            if POSITION_DATA_ERROR_RETRY_DELAY_SEC > 0:
                time.sleep(POSITION_DATA_ERROR_RETRY_DELAY_SEC)
            reconcile = reconcile_position_state(broker, state, now, force=True)
            _pde_retries.append(reconcile)
            if reconcile != POSITION_DATA_ERROR:
                break
        result.signal_dispatch_trace["position_data_error_retries"] = list(_pde_retries)
    result.signal_dispatch_trace["position_reconcile_result"] = (
        reconcile if not _pde_retries
        else f"{POSITION_DATA_ERROR}|retry={','.join(_pde_retries)}"
    )
    if reconcile == POSITION_DATA_ERROR and state.position is not None:
        # 3회 모두 실패 + 보유 중: 보유가 확실하면 기존 포지션 SELL 만 (반대 BUY 금지).
        _se_outcome = _safe_exit_on_position_data_error(
            broker, state, signal_id=signal_id, direction=direction, now=now,
            trace=result.signal_dispatch_trace,
        )
        if _se_outcome is not None:
            _record_broker_order_result(state, _se_outcome)
            if _se_outcome.sell_result is not None:
                result.signal_dispatch_trace["broker_called"] = True
                result.signal_dispatch_trace["broker_order_id"] = _se_outcome.sell_result.order_id
                result.signal_dispatch_trace["broker_raw"] = dict(_se_outcome.sell_result.raw or {})
            result.order_requested_at = _se_outcome.timestamps.get("sell_requested_at")
            result.signal_dispatch_trace["order_requested_at"] = result.order_requested_at or ""
            result.signal_dispatch_trace["order_executor_called"] = True
            state.pending_signal = None   # 같은 신호의 반대 BUY 재시도 금지
            if signal_id and signal_id not in state.processed_signal_ids:
                state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
            state.order_block_reason = _se_outcome.block_reason
            result.signal_dispatch_trace["final_block_reason"] = _se_outcome.block_reason or ""
            result.timing["order_execution"] = time.monotonic() - order_started
            return _se_outcome
        # 보유가 확실하지 않다 -> SELL 도 BUY 도 하지 않는다 (기존과 같은 pending 대기).
        state.order_block_reason = CRITICAL_POSITION_DATA_ERROR
        result.signal_dispatch_trace["final_block_reason"] = CRITICAL_POSITION_DATA_ERROR
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=POSITION_DATA_ERROR,
        )
        result.skipped = POSITION_DATA_ERROR
        result.timing["order_execution"] = time.monotonic() - order_started
        return None
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
    elif reconcile == RECOVERED_TO_FLAT:
        # 2026-10-06 (10/02 사고 재시도 검증): 브로커가 플랫임을 방금 확인했고 사라진
        # 매도 레그도 기록했다. 호출부가 넘긴 ``position`` 은 이 reconcile **이전**
        # 스냅샷이라, 그대로 쓰면 이미 체결된 매도(10/02 11:51 인버스)를 execute_signal
        # 이 한 번 더 내려다 FAIL_SELL 로 끝나고 누락된 반대 BUY 도 또 빠진다.
        # 확인된 플랫(None)으로 갈아끼워 BUY 만 낸다.
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

    # ── 신규 매수 hard gate (2026-09-21) ────────────────────────────────
    # 위 분기들이 이미 POSITION_DATA_ERROR/POSITION_MISMATCH/
    # RECOVERED_FROM_BROKER 를 막고 있지만, "막는 목록"이 아니라 **"허용
    # 목록"** 으로 뒤집어 못 박는다 — 앞으로 reconcile 결과가 추가돼도
    # 기본값이 '차단' 이 되게 하기 위해서다.
    # 신규 진입(= 지금 플랫)에서 허용되는 것은 브로커 보유수량을 확실히 안
    # 경우뿐이다: MATCH_FLAT(원래 플랫) / RECOVERED_TO_FLAT(브로커가 플랫임을
    # 확인해 방금 정리함). 보유 중(청산/스위치)은 이 게이트를 타지 않는다.
    if state.position is None and reconcile not in (MATCH_FLAT, RECOVERED_TO_FLAT):
        state.order_block_reason = ENTRY_BLOCKED_RECONCILE_UNHEALTHY
        result.signal_dispatch_trace["final_block_reason"] = ENTRY_BLOCKED_RECONCILE_UNHEALTHY
        result.signal_dispatch_trace["entry_gate_reconcile_result"] = reconcile
        logger.warning(
            "[MACD2] new BUY blocked -- reconcile not in allow-list (result=%s)", reconcile)
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=ENTRY_BLOCKED_RECONCILE_UNHEALTHY,
        )
        result.skipped = ENTRY_BLOCKED_RECONCILE_UNHEALTHY
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
    # W1a 사이징(2026-09-12): **budget 에만** 배수를 곱한다. 기본값 1.0 이라
    # X2-lite 외 모든 호출부는 바이트 단위로 동일하다. 수량 산출은
    # order_executor.compute_limit_buy_quantity 가 그대로 하므로
    # min(budget, orderable_cash) / limit_buyable_qty 상한이 자동으로 걸려
    # 주문가능금액을 넘는 주문이 구조적으로 나갈 수 없다.
    _sized_budget = float(state.budget or 0.0) * float(budget_multiplier or 1.0)
    outcome = order_executor.execute_signal(
        broker=broker, direction=direction, signal_id=signal_id, quotes=quotes,
        position=position, budget=_sized_budget,
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
    # 2026-09-29 safety 진단 (사용자 주문금액 한도 cap / 브로커 내부 거절 사유).
    for _safety_key in SAFETY_LEDGER_FIELDS:
        result.signal_dispatch_trace[_safety_key] = getattr(outcome, _safety_key, None)
    sell_only_switch_needs_buy_retry = _sell_cleared_but_buy_not_requested(outcome)
    # 2026-10-02 실사고: 반전 매도는 접수됐는데 체결 확인용 잔고조회가 전부 실패했다
    # (KIS 초당한도 EGW00215). 보유수량을 모르므로 반대 BUY 는 이번에 내지 않았다 --
    # 이 신호를 processed 로 소비하면 반전이 통째로 사라진다(10/02 11:48 RED). 대신
    # pending 으로 남겨 다음 tick 에 잔고를 다시 맞춘 뒤(_execute_or_wait 첫 줄의
    # reconcile -- 플랫이면 RECOVERED_TO_FLAT 로 매도 레그를 기록) 같은 신호를 재시도한다.
    sell_unconfirmed_needs_retry = _sell_reconcile_query_failed(outcome)
    if sell_unconfirmed_needs_retry:
        result.signal_dispatch_trace["sell_reconcile_query_error_retry"] = True
        logger.warning(
            "[MACD2] 반전 매도 체결확인 조회 실패 -- 신호 %s 를 pending 으로 남겨 다음 tick 에 재시도",
            signal_id)
    if (_has_order_request(outcome) and not sell_only_switch_needs_buy_retry
            and not sell_unconfirmed_needs_retry):
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
    elif sell_unconfirmed_needs_retry:
        state.order_block_reason = outcome.block_reason
        _set_pending_signal(
            state, signal_id=signal_id, direction=direction, signal_type=signal_type,
            macd_snap=macd_snap, detected_at=now, reason=outcome.block_reason,
        )
    else:
        state.pending_signal = None
    result.signal_dispatch_trace["final_block_reason"] = outcome.block_reason or ""
    result.timing["order_execution"] = time.monotonic() - order_started
    return outcome


#: 관측 전용 (2026-09-08). run_once 가 이번 tick 의 프레임 진단값(bars_3m /
#: 불완전봉 드롭 목록)을 여기에 둔다. bar_ledger 기록에만 쓰이고 거래 판정은
#: 절대 읽지 않는다. ``_advance_confirmed_primary`` 의 시그니처를 바꾸지 않으려고
#: 이 경로를 쓴다 — 기존 테스트들이 그 함수를 3-인자 stub 으로 monkeypatch 한다.
#:
#: **thread-local 이어야 하는 이유**: 프로세스당 MACD2 Worker 가 1개라는 보장이
#: 없다. 2026-09-03 실사고(commit 790cea6)에서 두 Worker 루프가 같은 프로세스
#: 안에서 동시에 틱을 돌았고, 방어책인 lease 는 (a) 매 틱 *시작 시점*에만
#: 검사하며 (b) 읽기 실패 시 fail-open(``_holds_worker_lease``) 이라 겹치는
#: 구간이 남는다. 모듈 전역이면 Worker B 가 덮어쓴 프레임을 Worker A 가 읽어
#: **진단 컬럼만 조용히 뒤섞인다** — 재현성 추적에 쓰려는 바로 그 값이다.
#: run_once -> _advance_confirmed_primary 는 같은 스레드 안의 동기 호출이고
#: Worker 마다 스레드가 분리되므로 thread-local 이 정확한 격리 단위다.
_OBSERVED_FRAME = threading.local()


def _set_observed_frame(bars_3m=None, dropped_bar_starts=None) -> None:
    """관측 전용. 실패해도 무해하도록 호출부에서 try/except 로 감싼다."""
    _OBSERVED_FRAME.bars_3m = bars_3m
    _OBSERVED_FRAME.dropped_bar_starts = dropped_bar_starts


def _replay_unevaluated_completed_bars(
    *, state: RuntimeState, bars_3m, now: datetime, result: TickResult,
) -> list[tuple[str, str]]:
    """프레임에 있는데 ``evaluated_bar_ts_today`` 에 없는 **미평가 완성봉**을
    시간순으로 따라잡는다.

    왜 필요한가 (2026-09-16 실사고)
    --------------------------------
    ``_advance_confirmed_primary`` 는 언제나 프레임의 **마지막 봉 하나**만
    평가한다. 그런데 KIS 1분봉이 늦게 도착하면 그 순간 해당 3분봉은
    ``filter_complete_3m_bars`` 에서 불완전으로 탈락하고(HISTORY_GAP),
    뒤늦게 분봉이 채워져 완성될 때쯤이면 프레임의 마지막 봉은 이미 **그 다음
    봉**이다. 그래서 그 봉은 두 번 다시 "마지막 봉" 이 되지 못하고 **영구히
    평가되지 않는다** -- 플래그도, 신호원장 행도, T+3 후보도 사라진다.
    2026-09-16 에 KIS 실제 플래그 8건 중 4건이 이렇게 없어졌고, 기록된 4건은
    전부 재시작 catch-up walk 가 복원한 것이었다.

    계약
    ----
      * **실제 완성봉만** 평가한다 -- 호출부가 이미 resample +
        filter_complete_3m_bars 를 거친 프레임을 넘긴다. MACD 계산식 / resample /
        완성봉 필터 규칙은 하나도 바꾸지 않는다.
      * **FLAG RECOVERY 전용.** 여기서 복원되는 과거 봉은 원장 행과 방향 상태만
        되살리고 **주문을 내지 않는다**(``_record_catchup_flag`` 는 BLOCKED 행만
        쓴다). 이미 진입 시각/T+3 유효시간이 지난 플래그로 뒤늦게 주문이 나가는
        일은 구조적으로 불가능하다 -- 이 함수는 order_executor 를 import 조차
        하지 않는 경로만 탄다.
      * **마지막 봉은 건드리지 않는다.** 그 봉은 호출부의 기존 live 경로가
        평가하며, 주문 권한도 거기에만 있다. 따라서 "지금도 정상적으로 T+3
        절차를 밟을 수 있는" 신호만 기존 로직으로 흘러간다.
      * 중복은 ``evaluated_bar_ts_today`` + ``processed_signal_ids`` +
        ``ledger.append_signal`` 의 signal_id 평생 dedup, 3중으로 막힌다.
      * **하한(high-water mark) 을 쓰지 않는다** (2026-09-16 2차 실사고).
        ``last_confirmed_bar_ts`` 하나를 하한으로 삼으면, 구멍 난 봉보다 뒤 봉이
        먼저 평가되는 순간 그 하한이 구멍 너머로 전진해 뒤늦게 채워진 봉을
        영구히 제외해 버린다(2026-09-16 14:33/14:36/14:39 UP_RED/DOWN_BLUE/
        UP_RED 3건 연속 소실). 실제 평가한 봉 **집합**을 기준으로 삼으면 구멍이
        어디서 메워지든 정확히 그 봉만 골라낼 수 있다.
      * 오늘 봉만 대상으로 한다(``_record_catchup_flag`` 자체도 오늘이 아니면
        기록하지 않는다).

    돌려주는 값은 관측/테스트용 ``[(bar_hhmm, direction)]`` 목록이다.
    """
    recovered: list[tuple[str, str]] = []
    if bars_3m is None or len(bars_3m) < 2:
        return recovered
    evaluated = set(state.evaluated_bar_ts_today or [])
    if not evaluated:
        # 집합이 아직 비어 있는 두 경우를 여기서 흡수한다:
        #   (a) 이 수정 이전에 저장된 state 를 그대로 물려받은 첫 tick
        #   (b) 오늘 아직 아무 봉도 평가하지 않은 기동 직후
        # (a) 는 ``last_confirmed_bar_ts`` 가 유일한 평가 증거이므로 그것으로
        # 집합을 시딩한다 -- 이러면 이 함수의 동작이 수정 전과 **정확히 같아진다**
        # (아래 floor 주석 참고). (b) 는 재시작 catch-up walk 의 영역이라
        # 여기서 하루치를 통째로 되감지 않는다(그 경로가 이미 같은 일을 한다).
        seed = _parse_iso_dt(state.last_confirmed_bar_ts)
        if seed is None or seed.astimezone(KST).date() != now.astimezone(KST).date():
            return recovered
        _note_evaluated_bar(state, seed, now)
        evaluated = set(state.evaluated_bar_ts_today or [])
        if not evaluated:
            return recovered
    # high-water mark 는 이제 "하한"이 아니라 **앞/뒤 판정용**으로만 쓴다.
    # 이보다 뒤 봉 = 아직 아무도 지나가지 않은 정상 따라잡기(상태 전진 허용),
    # 이보다 앞 봉 = 구멍이 뒤늦게 메워진 것(원장 복구만, 상태 전진 금지).
    # 문자열이 아니라 **시각**으로 비교한다 -- 저장된 ISO 문자열의 표기가
    # (구분자/오프셋 표기/마이크로초) 조금이라도 다르면 문자열 비교는 조용히
    # 전부 "미평가"로 오판해 하루치를 통째로 되감아 버린다.
    evaluated_ts = set()
    for raw in evaluated:
        parsed = _parse_iso_dt(raw)
        if parsed is not None:
            evaluated_ts.add(pd.Timestamp(parsed))
    if not evaluated_ts:
        return recovered
    hwm = max(evaluated_ts)
    # floor = 우리가 "평가했는지 여부"를 아는 가장 오래된 봉. 그보다 앞 봉은
    # 이 프로세스가 추적을 시작하기 전이라 평가 여부를 알 수 없으므로 절대
    # 건드리지 않는다 -- 안 그러면 기동 전에 이미 원장에 남은 플래그를 다시
    # 써서 중복 행이 생긴다. 집합에 원소가 하나뿐일 때(위 시딩 경로)는
    # floor == hwm == 예전 ``prior`` 라서 수정 전 동작과 완전히 동일해진다.
    floor = min(evaluated_ts)
    today = now.astimezone(KST).strftime("%Y%m%d")
    dt_index = list(bars_3m["datetime"])
    # 마지막 봉은 제외 -- live 경로가 평가하고 주문 권한도 거기에만 있다.
    pending: list[int] = []
    for idx in range(len(dt_index) - 1):
        bar_dt = pd.Timestamp(dt_index[idx])
        bar_kst = bar_dt.astimezone(KST)
        if bar_kst.strftime("%Y%m%d") != today:
            continue
        if bar_dt in evaluated_ts or bar_dt < floor:
            continue
        pending.append(idx)
    if not pending:
        return recovered
    for idx in pending:                      # 시간순 -- bars_3m 는 정렬돼 있다
        snap = calculate_macd(bars_3m.iloc[: idx + 1])
        if snap is None:
            continue
        if pd.Timestamp(snap.bar_dt) > hwm:
            # 정상 따라잡기 -- 기존과 완전히 동일한 경로.
            direction = _advance_confirmed_primary(state, snap, now)
        else:
            # 2026-09-16: hwm 보다 **앞선** 봉이 뒤늦게 완성됐다. 여기서
            # last_detected_direction 을 되돌리면 이미 지나간 더 뒤 봉의 방향
            # 상태를 과거 값으로 덮어써 다음 live 플래그가 통째로 억제된다.
            # 그래서 상태는 한 글자도 건드리지 않고 **원장 복구만** 한다:
            # 크로스 판정은 순수 zero-cross onset(dedup 입력 None)으로 보고,
            # 이 봉을 평가 완료로만 표시한다. 주문은 어느 쪽 경로든 나가지
            # 않는다(_record_catchup_flag 는 BLOCKED 행만 쓴다).
            direction = evaluate_macd_crossover(snap, None)
            _note_evaluated_bar(state, snap.bar_dt, now)
        if direction == Direction.HOLD:
            continue
        _record_catchup_flag(state, snap, direction, now,
                             reason=LATE_COMPLETED_BAR_REPLAY)
        recovered.append((snap.bar_dt.astimezone(KST).strftime("%H:%M"), direction.value))
    if recovered:
        result.actions.append(
            f"{LATE_COMPLETED_BAR_REPLAY}:{len(recovered)}")
    return recovered


def _note_evaluated_bar(state: RuntimeState, bar_dt: datetime, now: datetime) -> None:
    """이 완성봉을 "오늘 평가했다"고 기록한다 (2026-09-16 실사고).

    ``last_confirmed_bar_ts`` 는 그대로 두고(다른 경로들이 이미 그 의미로 쓰고
    있다) **추가로만** 적재한다 — 이 집합은 ``_replay_unevaluated_completed_bars``
    가 "프레임에 있는데 아직 평가 안 한 봉"을 정확히 고르는 데만 쓰인다.
    오늘 날짜 봉만 담고, 중복은 담지 않는다."""
    bar_kst = bar_dt.astimezone(KST)
    if bar_kst.date() != now.astimezone(KST).date():
        return
    key = bar_dt.isoformat()
    current = state.evaluated_bar_ts_today or []
    if key in current:
        return
    state.evaluated_bar_ts_today = list(current) + [key]


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
    now_kst = now.astimezone(KST)
    bar_kst = macd_snap.bar_dt.astimezone(KST)
    if bar_kst.date() != now_kst.date() or bar_kst + timedelta(minutes=3) > now_kst:
        # 2026-09-16 수정: 이 검사에 걸린 봉은 **평가하지 않은** 봉이다.
        # 예전에는 도장(last_confirmed_bar_ts)을 이 검사보다 **먼저** 찍어서,
        # 한 번 걸린 봉은 위 중복방지에 막혀 **영구히 재평가되지 않았다** --
        # 그 봉에 제로크로스가 있었다면 플래그도, 신호원장 행도, T+3 후보도,
        # 따라서 주문까지 통째로 사라진다(봉 원장에도 행이 남지 않아 사후
        # 추적조차 불가능하다). 평가한 봉에만 도장을 찍는다.
        return Direction.HOLD
    state.last_confirmed_bar_ts = bar_key
    _note_evaluated_bar(state, macd_snap.bar_dt, now)
    # Order-authoritative FLAG source is fixed to zero-cross onset. KIS
    # color/onset may be displayed as reference only and must not replace
    # this calculation without a fresh production-change decision.
    # 관측 전용 (2026-09-08): 크로스 판정이 실제로 읽는 직전 상태값. 아래
    # evaluate_macd_crossover 호출에 넘기는 것과 같은 값이며, 이 대입은
    # 판정에 아무 영향도 주지 않는다.
    _observed_prev_direction = state.last_detected_direction
    direction = evaluate_macd_crossover(macd_snap, state.last_detected_direction)
    if direction != Direction.HOLD:
        state.last_detected_direction = direction
        state.latest_primary_flag = direction
        state.latest_primary_signal_id = make_signal_id(macd_snap.bar_dt, direction)
    # ── 3분 확정봉 MACD 원장 (2026-09-08, 관측 전용) ──────────────────────
    # 위에서 이미 계산이 끝난 값만 그대로 넘긴다 — 재계산 없음, MACD 호출 없음.
    # 쓰기 실패는 절대 판정/주문/청산에 영향을 주지 않는다.
    try:
        bar_ledger.record_bar(
            macd_snap=macd_snap,
            direction=direction,
            prev_direction_state=_observed_prev_direction,
            bars_3m=getattr(_OBSERVED_FRAME, "bars_3m", None),
            dropped_bar_starts=getattr(_OBSERVED_FRAME, "dropped_bar_starts", None),
            signal_id=(state.latest_primary_signal_id if direction != Direction.HOLD else ""),
            worker_instance_id=state.worker_instance_id,
        )
    except Exception:
        pass
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


def _n1_ladder_overrides(state: RuntimeState) -> dict:
    """N1 이 활성일 때 오전 래더에 넘길 override 3개. 그 외 모드는 빈 dict.

    판정은 **완성봉에서만** 갱신되고(`_advance_n1_adaptive`), 그 봉 안의 틱은
    같은 값을 재사용한다 — 연구엔진의 `trend_ok_at` 캐시와 같은 계약이다.

    ⚠ 연구엔진과의 의도적 차이: 연구엔진은 틱 판단에도 **그 틱이 속한(아직
    완성되지 않은) 봉**의 판정을 썼다(백테스트라 가능). production 은 마지막
    **완성봉**의 판정을 쓴다 — 미완성 봉을 보고 주문하지 않기 위해서다.
    판정이 아직 없으면(포지션 첫 틱 등) 비추세 래더로 떨어진다 — `snapshot()`
    의 "봉 부족 -> ok=False" 와 같은 보수적 fallback 이다.
    """
    if not n1_adaptive.is_active(state):
        return {}
    if not getattr(state, "time_window_position_active", False):
        return {}
    ld = n1_adaptive.cached_ladder(state)
    if ld is None:
        tp1, ratio, tp2 = n1_adaptive.off_trend_ladder()
    else:
        tp1, ratio, tp2 = ld.tp1_pct, ld.tp1_sell_ratio, ld.tp2_pct
    return {
        "tp2_pct_override": float(tp2),
        "tp1_sell_ratio_override": float(ratio),
        "tp1_pct_override": float(tp1),
    }


def _advance_n1_adaptive(*, state: RuntimeState, macd_snap, bars_3m, position) -> None:
    """N1 adaptive 판정을 완성봉마다 갱신한다 (2026-09-20).

    청산/주문을 전혀 하지 않는다 — 다음 tick 부터 오전 래더가 읽을 값을
    state 에 캐시하는 것이 전부다. `n1_last_eval_bar_ts` 로 멱등(같은 완성봉을
    두 번 계산하지 않는다). N1 이 아니면 완전한 no-op 이다."""
    if not n1_adaptive.is_active(state):
        return
    if position is None or position.quantity <= 0:
        n1_adaptive.clear(state)
        return
    if not getattr(state, "time_window_position_active", False):
        return
    if macd_snap is None:
        return
    checked = _parse_iso_dt(state.n1_last_eval_bar_ts)
    if checked is not None and macd_snap.bar_dt <= checked:
        return
    held = _position_direction(position)
    if held is None:
        return
    decision = n1_adaptive.resolve_ladder(bars_3m, held)
    n1_adaptive.note_eval(state, macd_snap.bar_dt, decision)


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
        (state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled
         or time_window_3slot.is_3slot_enabled(state))
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
                time_window_3slot.active_3slot_mode(state)
                or ("TEGv2" if state.time_window_teg_filter_enabled else "TW2")
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
            if state.time_window_active_mode in time_window_3slot.MODES_3SLOT:
                # TW2 3-SLOT / TWF 3-SLOT keep their own separate slot/session counters
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
        # C1 Peak Protection(2026-09-19): arm 판정에 쓰는 MFE 도 같은 이유로
        # 틱 관측값이며 production 의 time_window_peak_net_return 과 분리한다.
        # C1 이 active 가 아니면 필드를 단 한 번도 쓰지 않는다(OFF parity).
        if peak_protection.is_active(state):
            peak_protection.note_peak(state, tick_net_return)
        # 2026-09-07: TWF 3-SLOT 은 오후 TP 만 override 한다(오전 TP2 는 동일).
        # exit_overrides 는 TWF 가 아니면 전부 None 이라 기존 동작 불변.
        _tw_exit_overrides = time_window_3slot.exit_overrides(state.time_window_active_mode)
        # N1 (2026-09-20): 상위추세 여부로 TP1/TP1비중/TP2 가 봉마다 바뀐다.
        # N1 이 아니면 _n1_over 가 빈 dict 라 기존 인자가 그대로 쓰인다.
        _n1_over = _n1_ladder_overrides(state)

        # ── P3 스택: B3 TP/SL + P3 runner rescue (2026-09-27) ──────────────
        # **CHOP 으로 진입한 포지션에만** 적용된다. TREND/WARMUP 진입은 아래
        # 기존 래더로 그대로 내려간다(p3_position_active 가 False 이므로
        # governs_position 이 False 다) -- OFF/TREND parity 의 근거다.
        #
        # 여기서는 TP/SL 만 본다. max-hold 의 Y3 조건은 완성봉/MACD 가 필요해서
        # 아래 _advance_p3_max_hold (C1·H50 과 같은 자리) 에서 판정한다.
        #
        # 우선순위: 이 블록은 FORCED_LIQUIDATION **뒤**에 있다. 즉 강제청산 /
        # 세션종료는 언제나 먼저다. 반대로 기존 틱 익절 래더보다는 앞에 있고,
        # B3 가 관리하는 동안에는 그 래더를 아예 건너뛴다(연구엔진의
        # exit mode="replace" 와 같은 범위).
        _p3_governs = p3_stack.governs_position(state)
        if _p3_governs:
            _p3_entry_at = pos.entry_at or _parse_iso_dt(state.last_time_window_entry_at)
            if _p3_entry_at is None:
                # 진입시각을 모르면 경과시간을 셀 수 없다 -- 추정하지 않고
                # 기존 래더에 맡긴다(fail-safe = BASE).
                _p3_governs = False
            else:
                _p3_dec = p3_stack.evaluate(
                    net_return_pct=tick_net_return,
                    entry_at=_p3_entry_at, now=now,
                    direction=_position_direction(pos),
                    already_rescued=bool(state.p3_tp_rescued),
                    already_promoted=bool(state.p3_promoted),
                    allow_max_hold=False,
                    # H30 연장 중이면 +1% 를 전량익절이 아니라 Q2 와 같은 비중의
                    # 부분익절 + 승격으로 받는다. 연장이 아니면 False 라 기존과
                    # 한 줄도 다르지 않다(연구엔진 _bs["ext"] 와 같은 자리).
                    h30_active=p3_stack.is_h30(state),
                )
                if _p3_dec.action == p3_stack.ACTION_PARTIAL_PROMOTE:
                    # "+1% 최초 도달" 은 여기서 한 번만 각인된다. 같은 tick 이
                    # 두 번 평가돼도 note_first_tp 가 False 를 돌려주므로
                    # 부분매도는 한 번만 나간다(중복주문 방지). P3 rescue 와
                    # H30 연장중 rescue 는 **같은 각인**을 공유하므로 한 포지션에
                    # 부분매도가 두 번 나가는 경로가 없다.
                    if p3_stack.note_first_tp(state, now):
                        if _p3_dec.reason == "P3_H30_RESCUE":
                            _p3_log("P3_H30_RESCUE", timestamp=now.isoformat(),
                                    trade_id=state.position_epoch,
                                    direction=getattr(_position_direction(pos), "value", None),
                                    regime=state.p3_entry_regime,
                                    entry_time=_p3_entry_at.isoformat(),
                                    elapsed=round(float(_p3_dec.elapsed_min or 0.0), 2),
                                    net=round(float(_p3_dec.net_pct or 0.0), 4),
                                    h50_active=small_whipsaw_hold.is_holding(state),
                                    mode=p3_stack.MODE_H30, reason=_p3_dec.conditions)
                        else:
                            _p3_log("P3_RESCUE", timestamp=now.isoformat(),
                                    direction=getattr(_position_direction(pos), "value", None),
                                    regime=state.p3_entry_regime,
                                    entry_time=_p3_entry_at.isoformat(),
                                    elapsed=round(float(_p3_dec.elapsed_min or 0.0), 2),
                                    net=round(float(_p3_dec.net_pct or 0.0), 4),
                                    mode=p3_stack.MODE_B3, reason=_p3_dec.conditions)
                        _p3_sell_qty = min(pos.quantity - 1,
                                           max(1, round(pos.quantity * _p3_dec.sell_fraction)))
                        if pos.quantity >= 2 and _p3_sell_qty >= 1:
                            _p3_remaining = pos.quantity - _p3_sell_qty
                            _p3_out = order_executor.execute_partial_exit(
                                broker=broker, symbol=pos.symbol, sell_qty=_p3_sell_qty,
                                remaining_qty=_p3_remaining, exit_reason=_p3_dec.exit_reason,
                                entry_price=pos.avg_price,
                                reconcile_retries=ORDER_FILL_RECONCILE_RETRIES,
                                reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                            )
                            if _p3_out.final_state == SignalState.EXECUTED:
                                # 주문이 **확정 체결**된 뒤에만 상태를 옮긴다 --
                                # 실패하면 다음 tick 에 다시 판정된다.
                                state.position = dataclasses.replace(
                                    state.position, quantity=_p3_remaining)
                                p3_stack.note_rescued(state, now)
                                _p3_log(_p3_dec.exit_reason, timestamp=now.isoformat(),
                                        net=round(float(_p3_dec.net_pct or 0.0), 4),
                                        mode=p3_stack.MODE_P3_RUNNER,
                                        reason=f"sold={_p3_sell_qty},left={_p3_remaining}")
                                _p3_log("P3_RUNNER_PROMOTE", timestamp=now.isoformat(),
                                        mode=p3_stack.MODE_P3_RUNNER,
                                        reason="back_to_N1_C1_ladder")
                            else:
                                # 부분매도 실패 -- 최초도달 각인을 되돌려
                                # 다음 tick 에 같은 조건으로 재시도한다.
                                state.p3_first_tp_at = None
                            result.actions.append(f"{_p3_dec.exit_reason}:{pos.symbol}")
                            return True
                        # 1주 이하라 50%를 쪼갤 수 없다 -- 승격만 하고 잔량은
                        # 기존 N1/C1 래더가 맡는다(강제로 전량매도하지 않는다).
                        p3_stack.note_rescued(state, now)
                        _p3_log("P3_RUNNER_PROMOTE", timestamp=now.isoformat(),
                                mode=p3_stack.MODE_P3_RUNNER,
                                reason="qty_too_small_promote_only")
                        _p3_governs = False
                elif _p3_dec.action == p3_stack.ACTION_EXIT:
                    _p3_out = order_executor.execute_exit(
                        broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                        exit_reason=_p3_dec.exit_reason, entry_price=pos.avg_price,
                        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES,
                        reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                    )
                    _apply_exit_outcome(state, _p3_out, exit_reason=_p3_dec.exit_reason)
                    if _p3_out.final_state == SignalState.EXECUTED:
                        state.time_window_position_active = False
                    _p3_log(_p3_dec.exit_reason, timestamp=now.isoformat(),
                            direction=getattr(_position_direction(pos), "value", None),
                            regime=state.p3_entry_regime,
                            elapsed=round(float(_p3_dec.elapsed_min or 0.0), 2),
                            net=round(float(_p3_dec.net_pct or 0.0), 4),
                            mode=p3_stack.MODE_B3, reason=_p3_dec.reason)
                    result.actions.append(f"{_p3_dec.exit_reason}:{pos.symbol}")
                    return True

        if _p3_governs:
            # B3 가 관리하는 동안 기존 **틱 익절 래더**(TP1/TP2/오후TP)는 돌지
            # 않는다. 완성봉 손절/after-TP1-stop/trailing, OPPOSITE_SIGNAL,
            # whipsaw, H50, C1, 강제청산은 아래에서 그대로 살아 있다.
            tp_decision = time_window_position_manager.PositionManagementDecision(
                exit_reason=None, sell_fraction=0.0,
                tp1_done=bool(state.time_window_tp1_done),
                peak_net_return=float(state.time_window_peak_net_return or 0.0),
                label="B3_TICK_TP_SUPPRESSED",
            )
        else:
            tp_decision = time_window_position_manager.evaluate_take_profit_immediate(
            session=state.time_window_entry_session or "MORNING",
            net_return_pct=tick_net_return,
            tp1_done=bool(state.time_window_tp1_done),
            tp2_pct_override=_n1_over.get(
                "tp2_pct_override",
                time_window_3slot.morning_tp2_pct_override(state.time_window_active_mode)),
            afternoon_tp_pct_override=_tw_exit_overrides["afternoon_tp_pct_override"],
            tp1_sell_ratio_override=_n1_over.get(
                "tp1_sell_ratio_override", _tw_exit_overrides["tp1_sell_ratio_override"]),
            tp1_pct_override=_n1_over.get("tp1_pct_override"),
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
            _pm_kw = dict(_tw_exit_overrides)
            _pm_tp2 = time_window_3slot.morning_tp2_pct_override(state.time_window_active_mode)
            if _n1_over:
                _pm_tp2 = _n1_over["tp2_pct_override"]
                _pm_kw["tp1_sell_ratio_override"] = _n1_over["tp1_sell_ratio_override"]
            pm_decision = time_window_position_manager.evaluate_position(
                session=state.time_window_entry_session or "MORNING",
                net_return_pct=bar_net_return,
                tp1_done=bool(state.time_window_tp1_done),
                peak_net_return=float(state.time_window_peak_net_return or 0.0),
                tp2_pct_override=_pm_tp2,
                tp1_pct_override=_n1_over.get("tp1_pct_override"),
                **_pm_kw,
            )
            # 2026-08-27 fix -- same reasoning as the immediate-tick TP path
            # just above: peak_net_return still commits unconditionally
            # (harmless), but tp1_done must only flip once the corresponding
            # order is CONFIRMED EXECUTED, never just because the DECISION
            # said a threshold was crossed.
            state.time_window_peak_net_return = pm_decision.peak_net_return
            # C1 MFE 는 틱 + 완성봉 종가 둘 다 본다(연구 엔진과 동일).
            if peak_protection.is_active(state):
                peak_protection.note_peak(state, bar_net_return)
            if pm_decision.exit_reason is not None:
                sell_fraction = max(0.0, min(1.0, pm_decision.sell_fraction))
                full_exit = sell_fraction >= 1.0
                if full_exit:
                    outcome = order_executor.execute_exit(
                        broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                        exit_reason=pm_decision.exit_reason, entry_price=pos.avg_price,
                        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                    )
                    _apply_exit_outcome(state, outcome, exit_reason=pm_decision.exit_reason)
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
                _etp_trigger, _etp_floor = early_take_profit.thresholds(state)
                early_tp = early_take_profit.evaluate(
                    entry_chop=bool(state.time_window_entry_chop),
                    peak_net_return_pct=float(state.early_tp_peak_net_return or 0.0),
                    net_return_pct=bar_net_return,
                    trigger_pct=_etp_trigger,
                    floor_pct=_etp_floor,
                )
                if early_tp.armed and not state.last_early_tp_armed_at:
                    state.last_early_tp_armed_at = now.isoformat()
                if early_tp.exit_reason is not None:
                    # Snapshot BEFORE the exit dispatch — _apply_exit_outcome
                    # below resets time_window_entry_chop/early_tp_peak_net_
                    # return for the next holding period, so the values that
                    # actually caused this exit would otherwise be gone by the
                    # time the ledger row exists. Captured here (not inside
                    # _apply_exit_outcome) for exactly the same reason and in
                    # exactly the same place the PROFIT_LOCK_MACD_CONVERGENCE
                    # exit already snapshots its own diagnostics: nothing
                    # mutates these fields between here and the reset, and
                    # _apply_exit_outcome is shared by every exit path so it
                    # must stay agnostic to any single filter.
                    early_tp_ledger_fields = {
                        "early_tp_entry_chop_score": (
                            "" if state.last_entry_chop_score is None
                            else int(state.last_entry_chop_score)
                        ),
                        "early_tp_entry_chop_conditions": json.dumps(
                            state.last_entry_chop_conditions or {}, ensure_ascii=False, sort_keys=True,
                        ),
                        "early_tp_armed_at": state.last_early_tp_armed_at or "",
                        "early_tp_peak_net_return_pct": round(
                            float(state.early_tp_peak_net_return or 0.0), 6,
                        ),
                        # 2026-09-12: 모드별 임계값을 그대로 기록한다 —
                        # X2-lite 는 내장 ETP(1.5/1.0) 라 config 의 F 값을
                        # 적으면 원장 진단이 실제 판정과 어긋난다.
                        "early_tp_trigger_pct": float(_etp_trigger),
                        "early_tp_floor_pct": float(_etp_floor),
                    }
                    outcome = order_executor.execute_exit(
                        broker=broker, symbol=pos.symbol, quantity=pos.quantity,
                        exit_reason=early_tp.exit_reason, entry_price=pos.avg_price,
                        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
                    )
                    _apply_exit_outcome(state, outcome)
                    if outcome.final_state == SignalState.EXECUTED:
                        state.time_window_position_active = False
                        state.last_early_tp_fired_at = now.isoformat()
                        # Patch the row execute_exit just wrote via
                        # order_executor's own (unmodified) _record_leg.
                        # Purely additive columns; record_early_tp_fields
                        # itself re-checks that the row's exit_reason really
                        # is EARLY_TAKE_PROFIT and is a no-op if already
                        # recorded, so a retry/duplicate tick cannot corrupt
                        # or duplicate anything.
                        if outcome.sell_result is not None:
                            ledger.record_early_tp_fields(
                                str(outcome.sell_result.order_id or ""), early_tp_ledger_fields,
                            )
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
        # 2026-09-21: 새 포지션의 시작 — 이전 포지션의 position-scoped
        # 상태(H50/C1/N1/whipsaw-watch)를 **전부** 끝내고 epoch 을 올린다.
        # 반드시 아래 필드 세팅보다 먼저 와야 한다(이 함수가 초기화한다).
        _begin_position_epoch(state, reason="NEW_ENTRY")
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


# ── E 전략 보조 (EARLY-UP-FAST + RS125, 2026-10-05) ─────────────────────
# E 가 꺼져 있으면(=N1/P3) 아래 함수의 호출부가 전부 e_strategy.is_active 로
# 먼저 걸러지므로, 두 기존 모드에서는 이 코드가 단 한 줄도 실행되지 않는다.

def _e_watch_price(market_data: MarketDataService, df_1m) -> Optional[float]:
    """지금 기준가(하이닉스). 실시간 호가 우선, 없으면 마지막 완성 1분봉 종가."""
    try:
        q = market_data.get_quote(config.WATCH_SYMBOL)
        if q is not None and q.price and float(q.price) > 0:
            return float(q.price)
    except Exception:
        pass
    try:
        if df_1m is not None and len(df_1m):
            return float(df_1m["close"].astype(float).iloc[-1])
    except Exception:
        pass
    return None


def _e_rs_features(df_1m, now: datetime) -> Optional[dict]:
    """RS 원자료 — **완성봉 종가만** 쓴다(전일 포함, 미래정보 없음)."""
    try:
        if df_1m is None or not len(df_1m):
            return None
        today = None
        if "datetime" in df_1m.columns:
            dts = pd.to_datetime(df_1m["datetime"])
            day = now.astimezone(KST).date()
            if getattr(dts.dt, "tz", None) is not None:
                dts = dts.dt.tz_convert(KST)
            today = int((dts.dt.date == day).sum())
        return e_strategy.rs_features(df_1m["close"].astype(float).to_numpy(), now,
                                      today_bars=today)
    except Exception as exc:                                   # pragma: no cover
        logger.warning("[MACD2][E] RS 원자료 계산 실패 -- RS 미적용: %s", exc)
        return None


def _e_trading_day(now: datetime) -> str:
    return now.astimezone(KST).strftime("%Y%m%d")


def _e_fire_hard_safety(state: RuntimeState, now: datetime, rec: dict) -> Optional[str]:
    """돌파 체결 **직전**에만 다시 보는 hard safety. 통과면 None.

    승인(게이트) 자체는 T+3 에 이미 끝났고 여기서 재평가하지 않는다 --
    여기서 막는 것은 '지금 주문을 내면 안 되는 구조적 이유'뿐이다:
    킬스위치 / 장 시간 / 하루 슬롯 cap / 중복주문 / 일일 예산 소진.
    포지션·잔고 정합성과 호가 신선도는 그 다음 ``_execute_or_wait`` 가
    기존 경로와 **완전히 같은 코드**로 재확인한다.
    """
    if not state.auto_trade_on or state.stopped:
        return "AUTO_TRADE_OFF"
    if now.astimezone(KST).time() >= config.NEW_ENTRY_CUTOFF:
        return "NEW_ENTRY_CUTOFF"
    if int(state.tw2_3slot_slots_used_today or 0) >= int(config.TW2_3SLOT_DAILY_CAP):
        return "TW2_3SLOT_DAILY_CAP_REACHED"
    if e_strategy.breakout_signal_id(rec) in state.processed_signal_ids:
        return "DUPLICATE_SIGNAL_ID"
    if position_sizing.is_active(state) and position_sizing.remaining_daily_budget(state) <= 0:
        return "DAILY_EXPOSURE_CAP_EXHAUSTED"
    return None


def _advance_e_pending(
    *, broker, market_data: MarketDataService, state: RuntimeState, now: datetime,
    macd_snap, bars_3m, df_1m, position: Optional[PositionSnapshot], result: TickResult,
):
    """매 tick: 걸려 있는 돌파 대기를 **폐기하거나 체결**한다.

    체결은 **승인 시점 게이트 스냅샷을 재생**한다 -- 돌파 시점에 게이트를 다시
    평가하지 않는다. 승인은 T+3 에 이미 끝났고, 15분 뒤 재평가하면 그 사이
    바뀐 시장상태가 이미 승인된 신호를 조용히 바꿔 버리기 때문이다. 구조는
    기존 pending retry(``_retry_pending_signal``)와 같다: 승인 때 저장한
    session/slot/chop/사이징을 그대로 쓰고, 지금의 일일 노출 잔여로만 배수를
    다시 자른 뒤 ``_execute_or_wait`` -> 체결되면 ``_finalize_tw2_3slot_entry``
    로 **최초 체결과 동일한 후처리**를 탄다.

    승인 시점과 **다른 signal_id**(``:E_BRK``)를 쓰므로 중복주문이 구조적으로
    불가능하고, 슬롯/예산은 여기서 체결됐을 때 비로소 소비된다.

    E 가 아니면 즉시 None 이다 -- N1/P3 에서는 아무 일도 하지 않는다.
    """
    if not e_strategy.is_active(state):
        return None
    rec = e_strategy.pending_record(state)
    if rec is None:
        return None
    reason = e_strategy.pending_expiry_reason(rec, now)
    if reason is not None:
        e_strategy.clear_pending(state, reason)
        result.actions.append("E_PENDING_EXPIRED:" + str(rec.get("direction")))
        return None
    price = _e_watch_price(market_data, df_1m)
    if not e_strategy.breakout_hit(rec, price):
        return None

    direction = e_strategy.pending_direction(rec)
    flag_bar_dt = _parse_iso_dt(rec.get("flag_bar_ts"))
    ctx = rec.get("gate") if isinstance(rec.get("gate"), dict) else None
    if direction is None or flag_bar_dt is None or ctx is None:
        e_strategy.clear_pending(state, "E_PENDING_INVALID")
        return None
    block = _e_fire_hard_safety(state, now, rec)
    if block is not None:
        e_strategy.clear_pending(state, "E_PENDING_BLOCKED:" + block)
        state.order_block_reason = block
        result.actions.append("E_PENDING_BLOCKED:" + block)
        logger.warning("[MACD2][E] 돌파했지만 체결하지 않는다 -- %s (%s)",
                       block, rec.get("signal_id"))
        return None

    fire_signal_id = e_strategy.breakout_signal_id(rec)
    approved_at = _parse_iso_dt(rec.get("approved_at"))
    delay_min = (now - approved_at).total_seconds() / 60.0 if approved_at else 0.0
    e_strategy.clear_pending(state, e_strategy.PENDING_FIRED)
    logger.info("[MACD2][E] breakout fired %s dir=%s trigger=%.0f price=%s delay=%.1f분",
                fire_signal_id, rec.get("direction"), float(rec.get("trigger") or 0.0),
                price, delay_min)

    # ── 승인 스냅샷 재생 ────────────────────────────────────────────────
    # 사이징: 승인 때의 clip 후 배수를 쓰되 **지금의** 일일 노출 잔여로 다시
    # 자른다(_tw2_3slot_retry_sizing — pending retry 와 같은 함수).
    sizing = _tw2_3slot_retry_sizing(state, ctx)
    rs = e_strategy.evaluate_rs(
        state, features=_e_rs_features(df_1m, now), day=_e_trading_day(now))
    boost = e_strategy.apply_rs_boost(state, sizing.applied, rs)
    result.signal_dispatch_trace["e_breakout"] = {
        "signal_id": fire_signal_id, "trigger": rec.get("trigger"), "price": price,
        "delay_min": round(delay_min, 2), "approved_at": rec.get("approved_at"),
        "gate_replayed": True,
    }
    result.signal_dispatch_trace["e_rs"] = {
        "hit": rs.hit, "score": rs.score, "threshold": rs.threshold,
        "samples": rs.samples, "reason": rs.reason, "base": boost.base,
        "wanted": boost.wanted, "applied": boost.applied, "extra": boost.extra,
        "capped": boost.capped,
    }
    signal_type = "REVERSAL" if (position is not None and position.quantity > 0) else "INITIAL"
    detected_at = _parse_iso_dt(ctx.get("signal_detected_at")) or now
    outcome = _execute_or_wait(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        direction=direction, signal_id=fire_signal_id, signal_type=signal_type,
        position=position, result=result, signal_detected_at=detected_at,
        budget_multiplier=boost.applied,
    )
    _record_signal_ledger(state, macd_snap, direction, signal_type, fire_signal_id,
                          detected_at, outcome, result.signal_dispatch_trace)
    # 돌파 체결이 주문 단계에서 pending 이 됐다(잔고조회 실패 / 2026-10-02 반전 매도
    # 체결확인 조회 실패 등) -> run_once 의 pending retry 가 이어받는다. 승인 시점
    # 게이트 스냅샷을 3-SLOT 문맥으로 실어, 재시도 체결도 _finalize_tw2_3slot_entry
    # (P3 스냅샷/슬롯/epoch/노출)를 똑같이 타게 한다. 없으면 09/29 와 같은 BASE 사고.
    if (state.pending_signal
            and state.pending_signal.get("signal_id") == fire_signal_id):
        state.pending_signal["tw2_3slot_ctx"] = dict(ctx)
    if outcome is None:
        state.e_last_pending_result = "E_PENDING_FIRED_NOT_EXECUTED"
        return None
    if outcome.final_state == SignalState.EXECUTED:
        e_strategy.note_boost(state, boost)
        _apply_switch_outcome(state, outcome, direction, now)
        chop = None
        c = ctx.get("presized_chop")
        if c:
            chop = early_take_profit.EntryChopDecision(
                is_chop=bool(c.get("is_chop")), score=int(c.get("score") or 0),
                required=int(c.get("required") or 0), conditions=dict(c.get("conditions") or {}))
        _finalize_tw2_3slot_entry(
            state, outcome=outcome, direction=direction, now=now,
            session=ctx.get("session"), sizing=sizing, presized_chop=chop,
            bars_3m=bars_3m, signal_detected_at=detected_at, signal_id=fire_signal_id,
        )
        state.e_last_pending_result = e_strategy.PENDING_FIRED
    else:
        state.e_last_pending_result = "E_PENDING_FIRED_NOT_EXECUTED"
    return outcome


def _e_arm_pending_instead_of_entry(
    *, state: RuntimeState, now: datetime, macd_snap, direction: Direction,
    signal_id: str, flag_bar_dt: datetime, price: Optional[float], early: Any,
    slot_metrics: dict, gate: dict, result: TickResult,
    mark_processed: bool = True,
) -> bool:
    """즉시진입 조건 미달 -> 돌파 대기를 건다. 걸었으면 True.

    **슬롯도 예산도 여기서는 소비하지 않는다** -- 체결됐을 때
    ``_finalize_tw2_3slot_entry`` 가 비로소 올린다. 승인 signal_id 는
    processed 로 찍어 같은 T+3 후보가 다시 뜨지 않게 하고, 체결은 별도
    ``:E_BRK`` id 로 나가므로 중복주문이 생길 수 없다.

    ``gate`` 는 승인 시점 게이트 스냅샷(session/slot/chop/사이징)이다 --
    돌파 시점에 게이트를 다시 평가하지 않기 위해 여기서 통째로 저장한다.

    ``mark_processed=False`` 는 호출자가 같은 signal_id 로 **반대 포지션
    청산**을 곧바로 낼 때 쓴다. 청산 함수
    (``_execute_reversal_exit_only_for_filtered_entry``)는 이미 processed 인
    id 를 중복으로 보고 아무것도 하지 않으므로, 여기서 먼저 찍으면 보유 중인
    반대 포지션이 청산되지 않는다(2026-10-06 parity 06/15·06/25 에서 발견).
    그 경우 processed 는 청산 함수가 찍는다.
    """
    if early.trigger is None:
        return False
    e_strategy.arm_pending(
        state, direction=direction, trigger=float(early.trigger), signal_id=signal_id,
        flag_bar_dt=flag_bar_dt, confirm_bar_dt=macd_snap.bar_dt, now=now,
        session=slot_metrics.get("session"), slot_number=slot_metrics.get("slot_number"),
        gate=gate,
    )
    state.e_last_pending_result = None
    if mark_processed and signal_id and signal_id not in state.processed_signal_ids:
        state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
    result.actions.append("E_PENDING_ARMED:" + direction.value)
    logger.info("[MACD2][E] %s 대기 등록 trigger=%.0f price=%s dist=%s%% "
                "c1=%s c2=%s c3=%s (%.0f분 내 미돌파면 폐기)",
                direction.value, float(early.trigger), price, early.dist_pct,
                early.c1, early.c2, early.c3, float(config.E_WAIT_MINUTES))
    return True


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
    Never called unless a 3-SLOT mode (TW2 3-SLOT or TWF 3-SLOT) is enabled.
    """
    if not time_window_3slot.is_3slot_enabled(state) or not state.tw2_3slot_pending_flag_direction:
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
        # N1 (2026-09-20): 이 모드만 창별 quality 기준이 3 이다(연구사양 q3).
        # 다른 모드는 None 이라 config.QUALITY_SCORE_THRESHOLD(4) 그대로다.
        quality_threshold_override=time_window_3slot.quality_score_threshold(
            time_window_3slot.active_3slot_mode(state)),
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
            # ── AR1: 오후 동일방향 재진입 예외 (2026-09-24 내장, 토글 없음) ──
            # **N1 경로 전용**이다. 이 함수는 3-SLOT 계열 다섯 모드가 공용으로
            # 쓰지만 AR1 의 검증 BASE 는 N1 + C1 (+ SMART) 하나뿐이라,
            # afternoon_reentry_exception_enabled 로 X2-lite W1 / H50 /
            # TW2 3-SLOT / TW TEG 3-SLOT 을 잘라낸다(그 모드 동작 불변).
            # 적용대상은 SAME_DIRECTION_AFTERNOON 으로 거절된 후보 **뿐**이다.
            # 현행 파이프라인은 이 분기에서 TEG 를 아예 호출하지 않으므로
            # 여기서 직접 계산해 넘긴다(그러지 않으면 AR1 이 판정 불가다).
            # TEG 탈락이 price_ema_stack_aligned **하나뿐**이고 macd_gap 확대 +
            # ema_spread 확대 + vwap 우호가 전부 참일 때만 그 조건 하나를
            # 면제한다. 오후 TEG 전체를 완화하지 않는다(새 임계값 0개).
            #
            # 통과는 "즉시 주문"이 아니다 — 동일방향 거절만 해제되고, 이후
            # CHOP TEG / 예산 / SMART sizing / order path 는 그대로 탄다.
            if (slot_decision.reject_reason
                    == time_window_3slot.REJECT_SAME_DIRECTION_AFTERNOON
                    and time_window_3slot.afternoon_reentry_exception_enabled(state)):
                ar1_teg = teg_gate.evaluate_teg(bars_3m, direction, flag_bar_dt, now)
                ar1 = time_window_3slot.evaluate_afternoon_reentry(
                    ar1_teg, base_reject_reason=slot_decision.reject_reason)
                slot_metrics["ar1_reason"] = ar1.reason
                slot_metrics["ar1_allowed"] = bool(ar1.allowed)
                slot_metrics["ar1_stack_exempt"] = bool(ar1.stack_exempt)
                if ar1.allowed:
                    final_approved = True
                    final_decision_label = config.TW_APPROVED
                    final_block_reason = None
                    slot_metrics["slot_number"] = int(
                        state.tw2_3slot_slots_used_today or 0) + 1
                    slot_metrics["session"] = time_window_3slot.SESSION_AFTERNOON
                    logger.info("[MACD2][AR1] 오후 동일방향 재진입 허용 (%s, %s)",
                                direction.value, ar1.reason)
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

        # ── TW TEG 3-SLOT 전용: CHOP 후보 TEGv2 추가 요구 (2026-09-08) ────
        # 위 게이트 체인이 전부 끝난 뒤, **TW TEG 3-SLOT 이 선택됐을 때만**
        # 추가로 판정한다. TW2 3-SLOT 을 포함한 다른 모든 모드에서는
        # requires_chop_teg_gate() 가 False 라 이 블록 자체가 실행되지 않는다
        # (MU_MACD 는 이 함수를 아예 타지 않는다).
        #
        #   entry_chop=False -> 아무것도 하지 않는다(기존과 완전히 동일)
        #   entry_chop=True  -> TEGv2 통과해야 진입. 실패하면 거절.
        #
        # 새 점수식/임계값을 만들지 않는다 — CHOP 판정은
        # early_take_profit.evaluate_entry_chop, 게이트는 teg_gate.evaluate_teg
        # 를 그대로 재사용한다. 슬롯 카운트(tw2_3slot_slots_used_today/
        # morning_count/afternoon_count)는 아래 outcome.final_state == EXECUTED
        # 분기에서만 증가하므로 여기서 거절해도 **슬롯은 소비되지 않고**
        # 다음 플래그가 같은 슬롯 후보로 다시 평가된다.
        #
        # 슬롯이 이미 TEGv2 게이트를 요구했다면(오후 슬롯) 그 후보는 방금 위에서
        # TEGv2 를 통과해 final_approved 가 된 것이므로 다시 부르지 않는다 —
        # 같은 프레임/같은 인자라 결과가 동일하고, 중복 호출만 늘 뿐이다.
        # ``state.time_window_active_mode`` 는 **진입 체결 시점**에야 세팅되므로
        # (아래 EXECUTED 분기) 후보 판정 시점에는 비어 있거나 직전 값이 남아
        # 있을 수 있다. 지금 어떤 전략이 켜져 있는지는 토글에서 직접 읽는다 —
        # EXECUTED 분기가 active_3slot_mode(state) 를 쓰는 것과 같은 출처다.
        if final_approved and time_window_3slot.requires_chop_teg_gate(
            time_window_3slot.active_3slot_mode(state)
        ):
            chop_decision = early_take_profit.evaluate_entry_chop(bars_3m, direction, now)
            is_chop = bool(chop_decision.is_chop) and not chop_decision.insufficient_data
            slot_metrics["tw_teg_entry_chop"] = is_chop
            slot_metrics["tw_teg_entry_chop_score"] = (
                0 if chop_decision.insufficient_data else int(chop_decision.score)
            )
            if is_chop and not slot_decision.requires_teg_gate:
                chop_teg = teg_gate.evaluate_teg(bars_3m, direction, flag_bar_dt, now)
                slot_metrics["tw_teg_chop_teg_approved"] = bool(chop_teg.approved)
                slot_metrics["tw_teg_chop_teg_reject_reasons"] = list(
                    chop_teg.reject_reasons or []
                )
                if not chop_teg.approved:
                    final_approved = False
                    final_block_reason = config.TW_TEG_3SLOT_REJECT_CHOP_TEG
                    final_decision_label = config.TW_TEG_3SLOT_REJECT_CHOP_TEG

        # ── Slot1 CHOP veto (2026-09-04 사용자 요청) ─────────────────────
        # 위 게이트 체인이 전부 끝난 뒤, 승인된 **Slot1 신규진입만** 추가로
        # 거절한다. Slot2/Slot3 과 청산/휩쏘/조기익절 경로는 한 줄도 건드리지
        # 않는다. 판정은 early_take_profit.evaluate_entry_chop 재사용(새 점수식
        # 없음). 슬롯 카운트(tw2_3slot_slots_used_today/morning_count/
        # afternoon_count)는 아래 outcome.final_state == EXECUTED 분기에서만
        # 증가하므로 차단해도 소비되지 않고, 다음 플래그가 다시 Slot1 후보로
        # 평가된다.
        #
        # 조기익절 필터가 OFF면 이 veto도 동작하지 않는다. 두 가지 이유:
        #   (1) 판정을 그 필터의 CHOP 평가기에 전적으로 위임하므로 필터가 꺼진
        #       상태에서 그 모듈을 호출하는 것 자체가 기존 계약 위반이다
        #       ("OFF면 early_take_profit 함수가 단 한 번도 호출되지 않는다" --
        #       tests/macd2/test_early_take_profit_worker.py 가 강제).
        #   (2) 이 veto의 60영업일 검증은 "TW2 3-SLOT + 조기익절" 위에서만
        #       측정됐다(data/validation/lossveto_fullchain). 조기익절이 꺼진
        #       조합은 검증된 적이 없으므로 켜지 않는 것이 맞다.
        if final_approved:
            slot1_veto = time_window_3slot.evaluate_slot1_chop_veto(
                bars_3m, direction, now, slot_number=slot_decision.slot_number,
                enabled=(config.TW2_3SLOT_SLOT1_CHOP_VETO
                         and early_take_profit.is_enabled(state)),
            )
            if slot1_veto.applicable:
                slot_metrics["slot1_chop_veto_score"] = slot1_veto.score
                slot_metrics["slot1_chop_veto_conditions"] = dict(slot1_veto.conditions)
            if slot1_veto.vetoed:
                final_approved = False
                final_block_reason = config.TW2_3SLOT_REJECT_SLOT1_ENTRY_CHOP
                final_decision_label = config.TW2_3SLOT_REJECT_SLOT1_ENTRY_CHOP

    decision = dataclasses.replace(
        base_decision, approved=final_approved, decision=final_decision_label, block_reason=final_block_reason,
        metrics={**(base_decision.metrics or {}), **slot_metrics},
    )
    _persist_tw2_3slot_decision(state, decision, signal_id)

    # ── P3 SHADOW: 이 확정 플래그를 가상 BASE 스트림에도 전달한다 ─────────
    # **주문을 내지 않는다.** 섀도우는 자기 슬롯 장부로 진입 여부를 따로
    # 판정하고(knock-on 격리), 보유 중이면 H50 개입 / 반대신호 청산을 BASE
    # 규칙대로 처리한다. 실거래가 이 후보를 승인했는지와 무관하게 호출한다 --
    # 그래야 "실거래는 슬롯을 다 썼지만 BASE 라면 들어갔을" 거래를 놓치지 않는다.
    # AR1 만 실거래 판정을 힌트로 받는다(AR1 게이트/조건을 재구현하지 않는다).
    _p3_note_shadow_flag(
        state=state, now=now, market_data=market_data, bars_3m=bars_3m,
        direction=direction, base_cleared=bool(tw2_cleared),
        flag_bar_dt=flag_bar_dt,
    )

    # ── H50: 작은 휩쏘 HOLD (2026-09-15, X2-lite H50 모드 전용) ──────────
    # 보유 중 반대 플래그가 **정상 확정**됐을 때, 보유방향이 구조적 상위추세와
    # 같고(EMA20/EMA50) 최근 60분 range 가 좁으면 그 반대신호 청산을 보류한다.
    # 승인/거절 분기보다 **앞**에 두는 이유: 승인이면 switch(청산+진입), 거절이면
    # exit-only 인데 H50 은 두 경우 모두 "청산하지 않는다"가 되어야 하므로
    # 분기 하나로 처리하는 것이 유일하게 안전하다.
    #
    # 진입에는 일절 관여하지 않는다 -- 여기서 return 하는 경로는 포지션을
    # 그대로 두는 것뿐이고, 슬롯 카운터(tw2_3slot_slots_used_today 등)는
    # 아래 EXECUTED 분기에서만 증가하므로 소비되지 않는다. 하드스톱/TP/
    # 트레일링/ETP/강제청산은 이 tick 의 앞 단계에서 이미 평가됐고 이 블록이
    # 그것을 막을 수 없다(우선순위 불변).
    if small_whipsaw_hold.is_active(state) and position is not None:
        _h50_target = order_executor.target_symbol_for_direction(direction)
        if position.symbol != _h50_target:
            _held_dir = _direction_for_symbol(position.symbol)
            _h50 = small_whipsaw_hold.evaluate_hold(bars_3m, _held_dir, now)
            if _h50.should_hold:
                state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
                h50_trace = {
                    "signal_id": signal_id, "direction": direction.value,
                    "signal_type": "TW2_3SLOT_CONFIRM",
                    "completed_bar_at": macd_snap.bar_dt.isoformat(),
                    "order_executor_called": False, "broker_called": False,
                    "final_block_reason": config.H50_HOLD_BLOCK_REASON,
                    "order_result_override": config.H50_HOLD_BLOCK_REASON,
                    "h50_trend": _h50.trend, "h50_range_pct": _h50.range_pct,
                    "major_fields": _entry_gate_ledger_fields(state, decision, "TW2_3SLOT"),
                }
                h50_outcome = order_executor.ExecutionOutcome(
                    signal_id=signal_id, direction=direction,
                    target_symbol=_h50_target,
                    final_state=SignalState.BLOCKED,
                    block_reason=config.H50_HOLD_BLOCK_REASON,
                )
                _record_signal_ledger(
                    state, macd_snap, direction, "TW2_3SLOT_CONFIRM", signal_id,
                    datetime.now(KST), h50_outcome, h50_trace,
                )
                result.actions.append(f"{config.H50_HOLD_BLOCK_REASON}:{direction.value}")
                state.order_block_reason = config.H50_HOLD_BLOCK_REASON
                if not small_whipsaw_hold.is_holding(state):
                    small_whipsaw_hold.note_hold_start(
                        state, held_direction=_held_dir, now=now, decision=_h50)
                    # 2026-09-21: 이 HOLD 가 **어느 포지션의 것인지** 각인한다.
                    # 포지션이 바뀌면 epoch 이 달라져 stale 로 판정된다.
                    state.h50_owner_epoch = int(state.position_epoch or 0)
                # H50 은 whipsaw-watch 를 arm 하지 않는다 (2026-09-17 사용자 결정).
                # 2026-09-16 사고 이후 여기에 `_start_whipsaw_watch` 를 붙이는
                # 안을 구현·검증했으나 기존 H50 보다 열위라 **코드에서 제거**했다
                # (70일 복리 +193.51% vs +204.18%, 근거 전량은
                # `data/validation/macd2/h50_whipsaw_watch_20260917/`).
                # H50 의 해제경로는 `_advance_h50_hold` 의 두 가지뿐이다 --
                # 구조추세 2봉 이탈 / 최대 60분. TW2·TW2 3-SLOT 자신의
                # whipsaw-watch 는 아래 분기에서 예전 그대로 동작한다.
                return None

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
    # ── W1a 포지션 사이징 (X2-lite 전용, 2026-09-12) ────────────────────
    # 진입 승인 여부는 위에서 이미 끝났다. 여기서는 **주문수량 배수만**
    # 정한다. CHOP 판정이 주문 전에 필요하므로 X2-lite 일 때만 여기서 미리
    # 계산하고, 아래 진입 성공 블록이 같은 값을 재사용한다(같은 순수함수에
    # 같은 입력이라 값이 달라질 수 없다). X2-lite 가 아니면 이 블록 전체가
    # no-op 이고 배수는 1.0 이라 기존 동작이 조금도 바뀌지 않는다.
    _presized_chop = None
    _sizing = position_sizing.NEUTRAL
    _tox = smart_sizing.UNKNOWN
    if position_sizing.is_active(state):
        _presized_chop = early_take_profit.evaluate_entry_chop(bars_3m, direction, now)
        # ── SMART: toxic 판정 (사이징 전용) ────────────────────────────
        # 미래정보 없음 — bars_3m 은 filter_complete_3m_bars 를 통과한 **완성봉**
        # 이고, ETF trail 은 `now`(진입 시각) **미만** 샘플만 쓴다.
        # confirmation 구간 = 플래그봉 시작(T) ~ 진입 직전. 판정 불가면
        # toxic=False 로 fail-open 하므로 BASE/P2 동작이 그대로 유지된다.
        if position_sizing.smart_active(state):
            _tox = smart_sizing.assess(
                bars_3m=bars_3m, direction=direction,
                samples=smart_sizing.trail_for(
                    state, order_executor.target_symbol_for_direction(direction)),
                confirm_start_at=flag_bar_dt, entry_at=now,
            )
        # slot_number/session 은 위 resolve_slot 이 이미 내린 값을 그대로 넘긴다
        # (새 시간기준을 만들지 않는다). BASE 모드에서는 쓰이지 않는다.
        _sizing = position_sizing.evaluate(
            state, entry_chop=bool(_presized_chop.is_chop),
            slot_number=slot_metrics.get("slot_number"),
            session=slot_metrics.get("session"),
            toxic=_tox.toxic,
        )
        _smart_trace = {
            "date": now.astimezone(KST).strftime("%Y%m%d"),
            "symbol": order_executor.target_symbol_for_direction(direction),
            "direction": direction.value,
            "slot": _sizing.slot_number, "session": _sizing.session,
            "sizing_mode": position_sizing.sizing_mode(state),
            "base_order_budget": float(state.budget or 0.0),
            "confirmation_etf_return_pct": _tox.confirmation_return_pct,
            "confirmation_weak": _tox.confirmation_weak,
            "confirmation_samples": _tox.samples_used,
            "ema20_50_directional_pct": _tox.ema20_50_directional_pct,
            "toxic": bool(_tox.toxic), "toxic_reason": _tox.reason,
            "p2_multiplier": _sizing.p2, "smart_multiplier": _sizing.smart,
            "w1a_rules": _sizing.raw, "clipped": _sizing.clipped,
            "applied": _sizing.applied, "capped": _sizing.capped,
            "pre_cap_budget": float(state.budget or 0.0) * float(_sizing.clipped),
            "remaining_daily_budget": position_sizing.remaining_daily_budget(state),
            "daily_capital": position_sizing.daily_capital(state),
        }
        state.last_smart_sizing_trace = _smart_trace
        result.signal_dispatch_trace["smart_sizing"] = _smart_trace
        logger.info(
            "[MACD2] SMART sizing %s %s slot=%s/%s mode=%s conf_etf=%s weak=%s "
            "ema20_50=%s toxic=%s p2=%.4f smart=%.4f applied=%.4f remaining=%.0f",
            _smart_trace["date"], _smart_trace["symbol"], _sizing.slot_number,
            _sizing.session, _smart_trace["sizing_mode"],
            _tox.confirmation_return_pct, _tox.confirmation_weak,
            _tox.ema20_50_directional_pct, bool(_tox.toxic),
            _sizing.p2, _sizing.smart, _sizing.applied,
            _smart_trace["remaining_daily_budget"],
        )
        result.signal_dispatch_trace["x2lite_sizing"] = {
            "raw": _sizing.raw, "clipped": _sizing.clipped,
            "applied": _sizing.applied, "capped": _sizing.capped,
            "exposure_before": _sizing.exposure_before, "reason": _sizing.reason,
            "p2": _sizing.p2, "slot_number": _sizing.slot_number,
            "session": _sizing.session,
            "toxic": bool(_tox.toxic), "smart": _sizing.smart,
            "sizing_mode": position_sizing.sizing_mode(state),
            "remaining_daily_budget": position_sizing.remaining_daily_budget(state),
            "daily_capital": position_sizing.daily_capital(state),
        }
    # ── E 전략 overlay (2026-10-05) ────────────────────────────────────
    # 진입 **승인**은 위에서 이미 끝났다. 여기서는 (1) 지금 살지 / 돌파를
    # 기다릴지, (2) RS 상위20% 면 얼마나 더 살지 두 가지만 정한다. E 가
    # 아니면 이 블록 전체가 no-op 이고 배수는 _sizing.applied 그대로라
    # N1/P3 동작이 조금도 바뀌지 않는다.
    _e_mult = float(_sizing.applied)
    _e_boost = None
    _e_rs = e_strategy.NEUTRAL_RS
    if e_strategy.is_active(state):
        _e_price = _e_watch_price(market_data, df_1m)
        _e_feat = _e_rs_features(df_1m, now)
        # RS 통계의 원자료는 **승인 시점**에 쌓는다 -- 대기로 가든 즉시 사든
        # 같은 자료가 들어가야 과거분포가 경로에 따라 달라지지 않는다.
        # 오늘 쌓은 표본은 rs_stats_for_day 가 'day < 오늘' 로 잘라내므로
        # 오늘 판정에는 절대 쓰이지 않는다(미래정보 차단).
        e_strategy.note_rs_sample(state, _e_trading_day(now), _e_feat)
        # 돌파 체결(_advance_e_pending)은 이 본문을 타지 않는다 -- 승인 시점
        # 게이트 스냅샷을 재생해 바로 주문하므로, 여기 오는 것은 항상 T+3
        # 승인 직후의 최초 판정이다.
        _e_early = e_strategy.evaluate_early_pass(
            bars_3m=bars_3m, flag_bar_dt=flag_bar_dt,
            confirm_bar_dt=macd_snap.bar_dt, direction=direction, price=_e_price)
        result.signal_dispatch_trace["e_early_pass"] = dataclasses.asdict(_e_early)
        if not _e_early.immediate:
            # 즉시진입 조건 미달 -> 돌파 대기. 승인 시점 게이트 스냅샷을
            # 통째로 저장해 두고(돌파 때 재평가하지 않는다), 보유 중인
            # **반대** 포지션은 지금 청산한다.
            _e_gate = _tw2_3slot_retry_ctx(
                session=slot_metrics.get("session"), sizing=_sizing,
                presized_chop=_presized_chop, signal_detected_at=signal_detected_at,
                flag_bar_dt=flag_bar_dt)
            _e_gate["slot_number"] = slot_metrics.get("slot_number")
            _e_gate["toxic"] = bool(getattr(_tox, "toxic", False))
            _e_gate["decision"] = str(getattr(decision, "decision", "") or "")
            _tgt = order_executor.target_symbol_for_direction(direction)
            _e_opposite = bool(position is not None and position.quantity > 0
                               and position.symbol != _tgt)
            if _e_arm_pending_instead_of_entry(
                    state=state, now=now, macd_snap=macd_snap,
                    direction=direction, signal_id=signal_id, flag_bar_dt=flag_bar_dt,
                    price=_e_price, early=_e_early, slot_metrics=slot_metrics,
                    gate=_e_gate, result=result, mark_processed=not _e_opposite):
                _record_signal_ledger(
                    state, macd_snap, direction, signal_type, signal_id,
                    signal_detected_at, None, result.signal_dispatch_trace)
                if _e_opposite:
                    _e_dec = MajorFlagDecision(
                        approved=False, score=0.0, required_score=0.0,
                        decision="E_PENDING_BREAKOUT", reasons=("e pending breakout",),
                        component_scores={}, metrics={}, is_reversal=True,
                        fast_reversal=False, block_reason="E_PENDING_BREAKOUT")
                    _e_exit = _execute_reversal_exit_only_for_filtered_entry(
                        broker=broker, state=state, macd_snap=macd_snap,
                        direction=direction, position=position, decision=_e_dec,
                        result=result, gate_mode="TW2_3SLOT",
                        signal_id_override=signal_id)
                    # 청산 함수가 processed 를 찍는다. 어떤 이유로든 찍히지 않았으면
                    # 여기서 찍어 같은 T+3 후보가 다음 tick 에 다시 대기로 걸리지 않게 한다.
                    if signal_id and signal_id not in state.processed_signal_ids:
                        state.processed_signal_ids = list(state.processed_signal_ids) + [signal_id]
                    if _e_exit is not None:
                        _apply_exit_outcome(state, _e_exit)
                        return _e_exit
                return None
        # RS125 — 일일 누적한도 안에서만 증액한다.
        _e_rs = e_strategy.evaluate_rs(
            state, features=_e_feat, day=_e_trading_day(now))
        _e_boost = e_strategy.apply_rs_boost(state, _sizing.applied, _e_rs)
        _e_mult = float(_e_boost.applied)
        result.signal_dispatch_trace["e_rs"] = {
            "hit": _e_rs.hit, "score": _e_rs.score, "threshold": _e_rs.threshold,
            "samples": _e_rs.samples, "reason": _e_rs.reason,
            "base": _e_boost.base, "wanted": _e_boost.wanted,
            "applied": _e_boost.applied, "extra": _e_boost.extra,
            "capped": _e_boost.capped,
            "remaining_daily_budget": position_sizing.remaining_daily_budget(state),
        }
        if _e_rs.hit:
            logger.info("[MACD2][E] RS 상위20%% 증액 %s score=%.4f thr=%.4f "
                        "배수 %.4f -> %.4f (한도초과로 깎임=%s)",
                        signal_id, float(_e_rs.score or 0.0), float(_e_rs.threshold or 0.0),
                        _e_boost.base, _e_boost.applied, _e_boost.capped)
    outcome = _execute_or_wait(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        direction=direction, signal_id=signal_id, signal_type=signal_type, position=position, result=result,
        signal_detected_at=signal_detected_at,
        budget_multiplier=_e_mult,
    )
    result.signal_dispatch_trace["major_fields"] = _entry_gate_ledger_fields(state, decision, "TW2_3SLOT")
    if outcome is None and result.skipped == config.MISSED_SIGNAL_QUOTE_STALE:
        state.tw2_3slot_pending_flag_direction = direction
        state.tw2_3slot_pending_flag_bar_ts = flag_bar_dt.isoformat()
    _record_signal_ledger(state, macd_snap, direction, signal_type, signal_id, signal_detected_at, outcome, result.signal_dispatch_trace)

    if outcome is not None and outcome.final_state == SignalState.EXECUTED:
        # E: position_sizing.note_entry 는 '증액 전' 배수만 기록한다. 증액분을
        # 여기서 누계에 직접 더하지 않으면 뒤 거래가 여유를 과대평가해 일일
        # 3,000만원 한도를 넘는다(2026-10-05 연구에서 8일 초과 실측).
        e_strategy.note_boost(state, _e_boost)
        _apply_switch_outcome(state, outcome, direction, now)
        session = slot_metrics.get("session") or time_window_filter.session_for_window(
            time_window_filter.classify_window(macd_snap.bar_dt.astimezone(KST).time())
        )
        _finalize_tw2_3slot_entry(
            state, outcome=outcome, direction=direction, now=now, session=session,
            sizing=_sizing, presized_chop=_presized_chop, bars_3m=bars_3m,
            signal_detected_at=signal_detected_at, signal_id=signal_id,
        )
    elif (state.pending_signal
          and state.pending_signal.get("signal_id") == signal_id):
        # 2026-09-29: 주문 전 단계(POSITION_DATA_ERROR 등)에서 pending 이 됐다.
        # 2026-10-02: 반전 매도 후 체결확인 조회 실패(FAILED + pending)도 같다 --
        # outcome 이 None 이 아니어도 pending 으로 남았으면 문맥을 싣는다.
        # run_once 의 pending retry 가 이 신호를 체결하면 위와 **똑같은** 후처리를
        # 타도록, 이 tick 에서 이미 결정된 진입 문맥을 pending 에 싣는다.
        state.pending_signal["tw2_3slot_ctx"] = _tw2_3slot_retry_ctx(
            session=(slot_metrics.get("session") or time_window_filter.session_for_window(
                time_window_filter.classify_window(macd_snap.bar_dt.astimezone(KST).time()))),
            sizing=_sizing, presized_chop=_presized_chop,
            signal_detected_at=signal_detected_at, flag_bar_dt=flag_bar_dt,
        )
    return outcome


def _finalize_tw2_3slot_entry(
    state: RuntimeState, *, outcome, direction: Direction, now: datetime, session,
    sizing, presized_chop, bars_3m, signal_detected_at: datetime, signal_id: str,
) -> bool:
    """3-SLOT 계열 신규진입의 **체결 후처리 전부** (2026-09-29 분리).

    최초 체결(``_resolve_tw2_3slot_candidate_body``)과 POSITION_DATA_ERROR 등으로
    pending 이 된 같은 signal_id 의 **재시도 체결**(run_once 의 pending retry)이
    이 함수 하나를 공유한다. 2026-09-29 실사고: 재시도 체결은 이 블록을 통째로
    건너뛰어 P3 스냅샷(B3 ownership) / epoch / 슬롯·오전오후 카운트 / W1a 노출이
    전부 빠졌다 -- 오늘 10:36, 12:15 두 거래가 CHOP 인데 BASE 가 된 원인.

    호출 전에 ``_apply_switch_outcome`` 은 호출부가 이미 끝냈어야 한다.
    같은 signal_id 에 두 번 불리면 두 번째는 아무 것도 하지 않는다(멱등 가드,
    state 에 영속되므로 재시작 후에도 유지). 실행했으면 True.
    """
    if signal_id and str(getattr(state, "tw2_3slot_post_entry_signal_id", "") or "") == signal_id:
        logger.warning("[MACD2] 3-SLOT post-entry already applied for %s -- skipped (idempotent)", signal_id)
        return False
    # 2026-09-21: 새 포지션의 시작 — 이전 포지션의 position-scoped
    # 상태(H50/C1/N1/whipsaw-watch)를 **전부** 끝내고 epoch 을 올린다.
    # 반드시 아래 필드 세팅보다 먼저 와야 한다(이 함수가 초기화한다).
    _begin_position_epoch(state, reason="NEW_ENTRY")
    state.time_window_position_active = True
    # 2026-09-07: 어느 3-SLOT 모드가 이 진입을 열었는지 기록한다 --
    # 청산 override 선택이 전적으로 이 값에 달려 있다.
    state.time_window_active_mode = (
        time_window_3slot.active_3slot_mode(state) or time_window_3slot.MODE_TW2_3SLOT
    )
    state.time_window_entry_session = session
    state.time_window_tp1_done = False
    # ── P3: 진입 시점 regime 스냅샷 (2026-09-27) ──────────────────────
    # 반드시 _begin_position_epoch **뒤**에 와야 한다(그 함수가
    # p3_stack.clear_position 으로 이전 포지션의 스냅샷을 지운다).
    # 여기서 찍은 값은 보유 내내 소급 변경되지 않는다 -- 보유 중 detector
    # 가 TREND<->CHOP 으로 바뀌어도 이 포지션의 관리모드는 그대로다.
    # P3 가 꺼져 있으면 regime 은 WARMUP 이고 p3_position_active 는 False
    # 이므로 이 포지션은 기존 래더로만 관리된다(OFF parity).
    if p3_stack.is_active(state):
        _p3_entry_regime = state.p3_last_regime or chop_regime.REGIME_WARMUP
        p3_stack.note_entry_regime(state, _p3_entry_regime, now=now)
        _p3_log("P3_ENTRY_SNAPSHOT", timestamp=now.isoformat(),
                direction=direction.value, regime=_p3_entry_regime,
                H50_rate=state.p3_last_h50_rate, TP1_rate=state.p3_last_tp1_rate,
                entry_time=now.isoformat(),
                mode=(p3_stack.MODE_B3
                      if _p3_entry_regime == chop_regime.REGIME_CHOP
                      else p3_stack.MODE_BASE))
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
        # 2026-09-12: W1a 가 주문 전에 계산해 둔 값이 있으면 그대로 쓴다 --
        # 사이징에 쓴 CHOP 과 포지션에 저장되는 CHOP 이 어긋날 수 없다.
        chop = (presized_chop if presized_chop is not None
                else early_take_profit.evaluate_entry_chop(bars_3m, direction, now))
        state.time_window_entry_chop = bool(chop.is_chop)
        state.last_entry_chop_score = int(chop.score)
        state.last_entry_chop_conditions = dict(chop.conditions)
    # W1a: 체결된 뒤에만 누적 exposure/진입순번을 올린다(진입시점 누적).
    position_sizing.note_entry(state, sizing)
    state.tw2_3slot_slots_used_today = int(state.tw2_3slot_slots_used_today or 0) + 1
    if session == time_window_3slot.SESSION_MORNING:
        state.tw2_3slot_morning_count = int(state.tw2_3slot_morning_count or 0) + 1
    else:
        state.tw2_3slot_afternoon_count = int(state.tw2_3slot_afternoon_count or 0) + 1
        state.tw2_3slot_last_afternoon_direction = direction.value
    state.tw2_3slot_post_entry_signal_id = signal_id
    return True


def _tw2_3slot_retry_ctx(*, session, sizing, presized_chop, signal_detected_at: datetime,
                         flag_bar_dt: datetime) -> dict[str, Any]:
    """pending 에 싣는 3-SLOT 진입 문맥 -- state JSON 에 그대로 저장된다(재시작 유지)."""
    chop = None
    if presized_chop is not None:
        chop = {
            "is_chop": bool(presized_chop.is_chop), "score": int(presized_chop.score),
            "required": int(getattr(presized_chop, "required", 0) or 0),
            "conditions": dict(presized_chop.conditions or {}),
        }
    return {
        "session": session,
        "sizing": dataclasses.asdict(sizing) if sizing is not None else None,
        "presized_chop": chop,
        "signal_detected_at": signal_detected_at.isoformat(),
        "flag_bar_dt": flag_bar_dt.isoformat() if flag_bar_dt is not None else None,
    }


def _tw2_3slot_retry_sizing(state: RuntimeState, ctx: dict[str, Any]):
    """재시도 시점의 사이징. 최초 판정의 배수(clip 후)를 쓰되 **지금의** 일일
    노출 잔여(room)로 다시 자른다 -- 그 사이 노출이 쓰였다면 3.0 을 넘지 않는다."""
    raw = dict(ctx.get("sizing") or {})
    if not raw:
        return position_sizing.NEUTRAL
    decision = position_sizing.SizingDecision(**raw)
    if not decision.active:
        return decision
    used = position_sizing.exposure_used(state)
    room = float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP) - used
    applied = max(0.0, min(float(decision.clipped), room))
    capped = applied < float(decision.clipped)
    reason = decision.reason
    if capped and not reason.endswith("+CAPPED"):
        reason = reason + "+CAPPED"
    return dataclasses.replace(
        decision, applied=applied, capped=capped,
        exposure_before=used, exposure_after=used + applied, reason=reason,
    )


def _retry_pending_signal(
    *, broker, market_data: MarketDataService, state: RuntimeState, now: datetime, macd_snap,
    pending_dir: Direction, position, result: TickResult, bars_3m, default_signal_type: str,
):
    """run_once 의 pending 재시도 (2026-09-29 통합).

    3-SLOT 문맥(``tw2_3slot_ctx``)이 실린 pending 은 최초 판정과 **같은** 사이징
    배수로 주문하고, 체결되면 ``_finalize_tw2_3slot_entry`` 로 최초 체결과 같은
    후처리를 탄다. 최종 결과는 신호원장의 WAITING 행을 갱신한다.
    문맥이 없는 pending(TW2 등 다른 전략)은 예전과 한 줄도 다르지 않다.
    """
    pending = dict(state.pending_signal or {})
    signal_id = str(pending["signal_id"])
    signal_type = str(pending.get("signal_type") or default_signal_type)
    ctx = pending.get("tw2_3slot_ctx") if isinstance(pending.get("tw2_3slot_ctx"), dict) else None
    detected_at = _pending_detected_at(state.pending_signal, now)
    if ctx is None:
        outcome = _execute_or_wait(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
            direction=pending_dir, signal_id=signal_id, signal_type=signal_type,
            position=position, result=result, signal_detected_at=detected_at,
        )
        if outcome is not None:
            _apply_switch_outcome(state, outcome, pending_dir, now)
        return outcome
    # 하루 3회 cap: 최초 판정 뒤 슬롯이 찼다면 재시도하지 않는다.
    if int(state.tw2_3slot_slots_used_today or 0) >= int(config.TW2_3SLOT_DAILY_CAP):
        logger.warning("[MACD2] pending 3-SLOT retry dropped -- daily cap reached (signal_id=%s)", signal_id)
        state.pending_signal = None
        state.order_block_reason = "TW2_3SLOT_DAILY_CAP_REACHED"
        return None
    sizing = _tw2_3slot_retry_sizing(state, ctx)
    outcome = _execute_or_wait(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        direction=pending_dir, signal_id=signal_id, signal_type=signal_type,
        position=position, result=result, signal_detected_at=detected_at,
        budget_multiplier=sizing.applied,
    )
    # 또 pending 이 됐다면 문맥을 다시 싣는다(다음 재시도도 같은 후처리).
    # 2026-10-02: FAILED + pending(반전 매도 체결확인 조회 실패)도 포함한다.
    if state.pending_signal and state.pending_signal.get("signal_id") == signal_id:
        state.pending_signal["tw2_3slot_ctx"] = ctx
    if outcome is None:
        return None
    _record_signal_ledger(state, macd_snap, pending_dir, signal_type, signal_id,
                          detected_at, outcome, result.signal_dispatch_trace)
    _apply_switch_outcome(state, outcome, pending_dir, now)
    if outcome.final_state == SignalState.EXECUTED:
        chop = None
        c = ctx.get("presized_chop")
        if c:
            chop = early_take_profit.EntryChopDecision(
                is_chop=bool(c.get("is_chop")), score=int(c.get("score") or 0),
                required=int(c.get("required") or 0), conditions=dict(c.get("conditions") or {}))
        _finalize_tw2_3slot_entry(
            state, outcome=outcome, direction=pending_dir, now=now, session=ctx.get("session"),
            sizing=sizing, presized_chop=chop, bars_3m=bars_3m,
            signal_detected_at=_parse_iso_dt(ctx.get("signal_detected_at")) or detected_at,
            signal_id=signal_id,
        )
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


def _clear_position_scoped_state(state: RuntimeState, *, reason: str = "") -> None:
    """포지션 **한 건의 수명**과 함께 끝나야 하는 상태를 한 곳에서 전부 정리한다.

    2026-09-21 실사고: 09:57 진입 -> 10:51 H50 HOLD -> 사용자가 KIS 에서 수동
    매도(시스템 밖 청산) -> reconcile 이 flat 을 채택했지만 H50 상태는 아무도
    지우지 않음 -> 12:57 신규 진입 3초 뒤, 126분 전에 시작된 HOLD 가
    MAX_HOLD(60분) 만료로 깨어나 **새 포지션**을 즉시 청산했다.

    그때까지 C1/N1 은 진입·청산·복구 경로마다 개별적으로 clear 되고 있었는데
    H50 과 whipsaw-watch 만 일부 경로에서 빠져 있었다. 개별 추가로는 같은 실수가
    반복되므로, **position-scoped 상태는 반드시 이 함수 하나를 통해서만** 정리한다.
    새 position-scoped 상태가 생기면 여기에 한 줄 추가하는 것이 유일한 등록 방법이다.

    호출해야 하는 lifecycle transition (전부 이 함수를 쓴다):
      - 신규 포지션 생성 직전            (`_begin_position_epoch`)
      - 전량 청산                        (`_apply_exit_outcome`)
      - RECOVERED_TO_FLAT               (브로커가 flat 이라고 확인)
      - 브로커 외부/수동 청산 감지        (위와 같은 경로)
      - reconcile 로 포지션을 새로 입양   (`_begin_position_epoch`)

    토글(전략 선택/필터 on-off)은 건드리지 않는다 — 포지션 수명이 아니라
    세션 수명이기 때문이다.
    """
    # 시간창 필터의 포지션 관리 상태
    state.time_window_position_active = False
    state.time_window_active_mode = None
    state.time_window_entry_session = None
    state.time_window_entry_flag_seq = None
    state.time_window_entry_session_seq = None
    state.time_window_tp1_done = False
    state.time_window_initial_quantity = 0
    state.time_window_peak_net_return = 0.0
    # 조기익절(ETP) 포지션 종속 상태
    state.time_window_entry_chop = False
    state.early_tp_peak_net_return = 0.0
    # 레거시 profit-lock / peak
    state.peak_net_return = 0.0
    state.profit_lock_active = False
    # 독립 모듈들 (각자 자기 필드만 지운다)
    peak_protection.clear(state)        # C1
    n1_adaptive.clear(state)            # N1
    small_whipsaw_hold.clear(state)     # H50
    _clear_whipsaw_watch(state)         # whipsaw-watch
    p3_stack.clear_position(state)      # P3 regime 스냅샷(B3/Y3/rescue)
    # 소유권 키 — 다음 포지션이 이전 포지션의 상태를 물려받지 못하게 한다
    state.h50_owner_epoch = 0
    state.c1_owner_epoch = 0
    if reason:
        logger.info("[MACD2] position-scoped state cleared (reason=%s, epoch=%s)",
                    reason, state.position_epoch)


def _begin_position_epoch(state: RuntimeState, *, reason: str = "") -> int:
    """새 포지션의 시작. 이전 포지션의 흔적을 지우고 epoch 을 1 올린다.

    반드시 **진입 필드를 세팅하기 전에** 호출한다 — 이 함수가 시간창 필드를
    초기화하므로 순서가 뒤집히면 방금 세팅한 값이 지워진다."""
    _clear_position_scoped_state(state, reason=reason or "NEW_POSITION")
    state.position_epoch = int(state.position_epoch or 0) + 1
    return state.position_epoch


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
        exit_reason=config.WHIPSAW_WATCH_DETERIORATION_EXIT,
    )
    _clear_whipsaw_watch(state)
    if outcome is not None:
        _apply_exit_outcome(state, outcome)
        if outcome.final_state == SignalState.EXECUTED:
            result.actions.append(f"{config.WHIPSAW_WATCH_DETERIORATION_EXIT}:{outcome.target_symbol}")
    return outcome


def _advance_h50_hold(
    *, broker, state: RuntimeState, now: datetime, macd_snap, bars_3m,
    position: Optional[PositionSnapshot], result: TickResult,
):
    """H50 HOLD 를 완성봉마다 갱신한다 (2026-09-15).

    해제 조건 (둘 중 하나):
      1) 구조적 상위추세가 보유 반대방향으로 ``H50_TREND_BREAK_BARS`` 봉 연속
      2) HOLD 시작 후 ``H50_MAX_HOLD_MIN`` 분 경과

    ``h50_last_checked_bar_ts`` 로 멱등 -- 같은 완성봉을 두 번 세지 않는다.
    청산 전용이며 신규 진입은 절대 하지 않는다. H50 모드가 아니거나 HOLD 가
    없으면 완전한 no-op 이다(모듈 함수 호출조차 하지 않는다)."""
    if not small_whipsaw_hold.is_active(state):
        return None
    if not small_whipsaw_hold.is_holding(state):
        return None
    if position is None or position.quantity <= 0:
        small_whipsaw_hold.clear(state)
        return None
    # ── 소유권 가드 (2026-09-21 실사고) ──────────────────────────────────
    # 09:57 진입 -> 10:51 HOLD -> 사용자가 KIS 에서 수동매도 -> reconcile flat
    # -> 12:57 신규 진입. 그 순간 126분 전에 시작된 HOLD 가 MAX_HOLD(60분)
    # 만료 상태로 깨어나 **새 포지션**을 3초 만에 청산했다.
    # owner 가 지금 포지션과 다르면 그 HOLD 는 이전 포지션의 잔재다 —
    # **clear + 경고만 하고 청산 신호는 절대 내지 않는다.**
    # 1순위: 소유권 키가 **양쪽 다 알려져 있는데 서로 다르면** 확실한 stale.
    #   (둘 중 하나라도 0 = '모름' 이면 여기서 판단하지 않는다 — 이 필드가
    #    없던 시절에 만들어진 상태/포지션까지 stale 로 몰면 정상 HOLD 가
    #    통째로 사라진다. 그 경우는 아래 started_at fallback 이 잡는다.)
    _owner = int(state.h50_owner_epoch or 0)
    _cur = int(state.position_epoch or 0)
    if _owner and _cur and _owner != _cur:
        logger.warning(
            "[MACD2] stale H50 HOLD discarded (owner_epoch=%s, position_epoch=%s, "
            "started_at=%s) -- no exit order was placed",
            _owner, _cur, state.h50_hold_started_at)
        state.last_h50_stale_discarded_at = now.isoformat()
        small_whipsaw_hold.clear(state)
        return None
    # fallback 안전망: epoch 이 어떤 이유로든 0 이어도, HOLD 시작이 현재
    # 포지션 진입보다 **앞서면** 그 HOLD 는 이 포지션의 것이 아니다.
    _entry_at = getattr(position, "entry_at", None)
    _started = _parse_iso_dt(state.h50_hold_started_at)
    if _entry_at is not None and _started is not None and _started < _entry_at:
        logger.warning(
            "[MACD2] stale H50 HOLD discarded (started_at=%s < position entry_at=%s)"
            " -- no exit order was placed", state.h50_hold_started_at, _entry_at)
        state.last_h50_stale_discarded_at = now.isoformat()
        small_whipsaw_hold.clear(state)
        return None
    held_dir = small_whipsaw_hold.held_direction(state)
    if held_dir is None:
        small_whipsaw_hold.clear(state)
        return None
    checked = _parse_iso_dt(state.h50_last_checked_bar_ts)
    if checked is not None and macd_snap.bar_dt <= checked:
        return None
    small_whipsaw_hold.note_checked_bar(state, macd_snap.bar_dt)
    started_at = _parse_iso_dt(state.h50_hold_started_at)
    decision = small_whipsaw_hold.evaluate_release(
        bars_3m, held_dir, started_at, now,
        trend_break_count=int(state.h50_trend_break_count or 0),
    )
    small_whipsaw_hold.note_trend_break_count(state, decision.trend_break_count)
    if not decision.should_release:
        return None

    exit_direction = (Direction.DOWN_BLUE if held_dir == Direction.UP_RED
                      else Direction.UP_RED)
    signal_id = f"{make_signal_id(macd_snap.bar_dt, exit_direction)}:H50_HOLD_EXIT"
    fake_decision = MajorFlagDecision(
        approved=False, score=0.0, required_score=0.0,
        decision=small_whipsaw_hold.EXIT_SMALL_WHIPSAW_HOLD,
        reasons=(f"H50 hold released: {decision.reason}",),
        component_scores={},
        metrics={
            "h50_release_reason": decision.reason,
            "h50_trend": decision.trend,
            "h50_trend_break_count": decision.trend_break_count,
            "h50_elapsed_min": decision.elapsed_min,
        },
        is_reversal=True, fast_reversal=False,
        block_reason=small_whipsaw_hold.EXIT_SMALL_WHIPSAW_HOLD,
    )
    outcome = _execute_reversal_exit_only_for_filtered_entry(
        broker=broker, state=state, macd_snap=macd_snap, direction=exit_direction,
        position=position, decision=fake_decision, result=result,
        gate_mode="TW2_3SLOT", signal_id_override=signal_id,
        exit_reason=(config.EXIT_H50_MAX_HOLD if decision.reason == "MAX_HOLD"
                     else config.EXIT_H50_TREND_BREAK),
    )
    small_whipsaw_hold.clear(state)
    if outcome is not None:
        _apply_exit_outcome(state, outcome)
        if outcome.final_state == SignalState.EXECUTED:
            result.actions.append(
                f"{small_whipsaw_hold.EXIT_SMALL_WHIPSAW_HOLD}:{outcome.target_symbol}")
    return outcome


def _advance_c1_peak_protection(
    *, broker, state: RuntimeState, now: datetime, macd_snap, position,
    result: TickResult,
):
    """C1 Peak Protection 을 완성봉마다 평가한다 (2026-09-19).

    자리: ``_advance_h50_hold`` 바로 뒤 = whipsaw-watch / H50 과 같은 자리다.
    이 지점까지 왔다는 것은 이 tick 에서

        FORCED_LIQUIDATION / 손절 / TP1 / TP2 / after-TP1 스탑 / trailing /
        조기익절(ETP) / T+3 반대신호 switch·sell-only / whipsaw-watch / H50

    이 **전부 아무 청산도 내지 않았다**는 뜻이다(각 경로는 발동 즉시
    ``run_once`` 에서 return 한다). 그래서 기존 래더 우선순위가 그대로
    유지되고, C1 은 production 이 HOLD 라고 답한 뒤에만 발언한다.

    발동 조건 (둘 다 성립해야 함):
      1) 보유 중 MFE(틱 관측) >= ``config.C1_ARM_MFE_PCT``
      2) 이 완성봉에서 MACD-Signal gap 이 보유방향 반대로 **부호 전환**되고,
         동시에 MFE 대비 ``config.C1_GIVEBACK_PCT`` 이상 반납

    ``c1_last_checked_bar_ts`` 로 멱등 — 같은 완성봉을 두 번 평가하지 않는다
    (같은 봉 재진입 tick 에서 C1 SELL 이 중복 발행될 수 없다).
    청산 전용이며 신규 진입은 절대 하지 않는다. C1 이 active 가 아니면
    완전한 no-op 이다(모듈 함수 호출조차 하지 않는다)."""
    if not peak_protection.is_active(state):
        return None
    if position is None or position.quantity <= 0:
        peak_protection.clear(state)
        return None
    if not state.time_window_position_active:
        # C1 은 시간대필터가 관리 중인 포지션에만 얹힌다(입양/MU_MACD 제외).
        return None
    # ── 소유권 가드 (2026-09-21, H50 과 같은 계약) ──────────────────────
    # arm 상태가 **이전 포지션의 것**이면 이 포지션에 적용하지 않는다.
    # (H50 에서 실제로 터진 사고의 동일 취약점 — 대칭으로 막아 둔다)
    _c1_owner = int(state.c1_owner_epoch or 0)
    _c1_cur = int(state.position_epoch or 0)
    if _c1_owner and _c1_cur and _c1_owner != _c1_cur:
        logger.warning(
            "[MACD2] stale C1 arm discarded (owner_epoch=%s, position_epoch=%s)"
            " -- no exit order was placed", _c1_owner, _c1_cur)
        peak_protection.clear(state)
        return None
    if macd_snap is None:
        return None
    checked = _parse_iso_dt(state.c1_last_checked_bar_ts)
    if checked is not None and macd_snap.bar_dt <= checked:
        return None
    peak_protection.note_checked_bar(state, macd_snap.bar_dt)

    held_dir = _position_direction(position)
    if held_dir is None:
        return None
    # 완성봉 종가 기준 순수익률 — 하방 rung 과 같은 규약.
    bar_close = state.stop_loss_bar_close
    if bar_close is None:
        return None
    bar_net_return = _net_return_pct(
        position.symbol, position.avg_price, float(bar_close), position.quantity,
    )
    arm_pct, give_pct = peak_protection.thresholds(state)
    decision = peak_protection.evaluate(
        held_direction=held_dir,
        peak_net_return_pct=float(state.c1_peak_net_return or 0.0),
        net_return_pct=bar_net_return,
        macd_hist=macd_snap.hist,
        arm_pct=arm_pct,
        giveback_pct=give_pct,
    )
    if decision.armed:
        peak_protection.note_armed(state, now)
        # 이 arm 이 어느 포지션의 것인지 각인 (2026-09-21)
        state.c1_owner_epoch = int(state.position_epoch or 0)
    if decision.exit_reason is None:
        return None

    # 원장 진단 스냅샷 — _apply_exit_outcome 이 C1 상태를 리셋하기 전에 떠 둔다
    # (조기익절이 같은 이유로 같은 자리에서 뜨는 것과 동일).
    c1_ledger_fields = {
        "c1_peak_net_return_pct": round(float(state.c1_peak_net_return or 0.0), 6),
        "c1_current_net_return_pct": round(float(bar_net_return), 6),
        "c1_giveback_pct": round(float(decision.giveback_pct), 6),
        "c1_macd_hist": round(float(macd_snap.hist), 6),
        "c1_arm_threshold_pct": float(arm_pct),
        "c1_giveback_threshold_pct": float(give_pct),
        "c1_armed_at": state.c1_armed_at or "",
        "c1_held_direction": held_dir.value,
    }
    exit_direction = (Direction.DOWN_BLUE if held_dir == Direction.UP_RED
                      else Direction.UP_RED)
    signal_id = f"{make_signal_id(macd_snap.bar_dt, exit_direction)}:C1_PEAK_PROTECTION"
    fake_decision = MajorFlagDecision(
        approved=False, score=0.0, required_score=0.0,
        decision=peak_protection.EXIT_C1_PEAK_PROTECTION,
        reasons=(
            f"C1 peak protection: peak={c1_ledger_fields['c1_peak_net_return_pct']}% "
            f"net={c1_ledger_fields['c1_current_net_return_pct']}% "
            f"giveback={c1_ledger_fields['c1_giveback_pct']}%p "
            f"hist={c1_ledger_fields['c1_macd_hist']}",
        ),
        component_scores={},
        metrics=dict(c1_ledger_fields),
        is_reversal=True, fast_reversal=False,
        block_reason=peak_protection.EXIT_C1_PEAK_PROTECTION,
    )
    outcome = _execute_reversal_exit_only_for_filtered_entry(
        broker=broker, state=state, macd_snap=macd_snap, direction=exit_direction,
        position=position, decision=fake_decision, result=result,
        gate_mode="TW2_3SLOT", signal_id_override=signal_id,
        exit_reason=peak_protection.EXIT_C1_PEAK_PROTECTION,
    )
    if outcome is not None:
        peak_protection.note_triggered(state, now)
        _apply_exit_outcome(state, outcome)
        if outcome.final_state == SignalState.EXECUTED:
            result.actions.append(
                f"{peak_protection.EXIT_C1_PEAK_PROTECTION}:{outcome.target_symbol}")
            if outcome.sell_result is not None:
                ledger.record_c1_fields(
                    str(outcome.sell_result.order_id or ""), c1_ledger_fields,
                )
    return outcome


# ── P3 REGIME STACK (2026-09-27) ──────────────────────────────────────────
def _p3_etf_price_at(state: RuntimeState, symbol: str, when: datetime):
    """ETF 호가 trail 에서 ``when`` **이전** 마지막 표본을 찾는다.

    SMART 사이징이 매 tick 적재하는 ``etf_quote_trail`` 을 읽기만 한다(쓰지
    않는다). 표본이 없으면 ``None`` 이고, 호출부는 조건 불충족으로 처리한다
    -- 없는 값을 추정하지 않는다.
    """
    stamp = when.isoformat()
    best = None
    for raw_at, px in smart_sizing.trail_for(state, symbol):
        if str(raw_at) <= stamp:
            best = px
        else:
            break
    return None if best is None else float(best)


def _advance_p3_max_hold(*, broker, state: RuntimeState, now: datetime,
                         bars_3m, position, quotes: dict, result: TickResult):
    """B3 max-hold(20분) + Y3 승격을 완성봉 기준으로 판정한다 (2026-09-27).

    자리: ``_advance_c1_peak_protection`` 과 같은 자리 = H50/whipsaw-watch 뒤다.
    여기까지 왔다는 것은 이 tick 에서 강제청산 / B3 TP·SL / 완성봉 손절 /
    trailing / ETP / 반대신호 switch / whipsaw / H50 / C1 이 **전부 아무 청산도
    내지 않았다**는 뜻이다 -- 기존 우선순위가 그대로 유지된다.

    Y3 의 두 조건(gap 확대 / ETF 추종)은 **마지막 완성봉**만 본다. 형성 중인
    봉을 쓰면 미래를 보는 것이므로 ``p3_stack.last_completed_bar_index`` 가
    그 경계를 강제한다.
    """
    if not p3_stack.governs_position(state):
        return None
    if position is None or position.quantity <= 0:
        return None
    entry_at = position.entry_at or _parse_iso_dt(state.last_time_window_entry_at)
    if entry_at is None:
        return None
    current_price = quotes.get(position.symbol)
    if current_price is None:
        return None
    net = _net_return_pct(position.symbol, position.avg_price, current_price,
                          position.quantity)
    # ETF 추종은 **보유 ETF 자신의** 3분 전 가격과 비교한다(연구엔진과 동일).
    # bars_3m 은 하이닉스 기초자산이라 여기 쓸 수 없다 -- SMART 사이징이
    # 이미 tick 마다 쌓고 있는 ETF 호가 trail 을 그대로 읽는다.
    etf_prev = _p3_etf_price_at(state, position.symbol, now - timedelta(minutes=3))
    # H30 (2026-09-28): 이 자리는 `_advance_h50_hold` **뒤**다. 즉 이 tick 에서
    # H50 이 해제됐다면 그 해제가 이미 반영된 뒤에 아래 판정이 돈다 -- "H50 기존
    # 규칙 우선" 이 코드 순서로 보장된다. 손절/강제청산/세션종료/trailing/ETP/
    # 반대신호/whipsaw/C1 도 전부 이 앞에서 끝나 있다.
    h50_active = small_whipsaw_hold.is_holding(state)
    was_h30 = p3_stack.is_h30(state)
    decision = p3_stack.evaluate(
        net_return_pct=net, entry_at=entry_at, now=now,
        direction=_position_direction(position),
        already_rescued=bool(state.p3_tp_rescued),
        already_promoted=bool(state.p3_promoted),
        bars_3m=bars_3m, etf_prev_price=etf_prev, etf_current_price=current_price,
        allow_max_hold=True,
        h50_active=h50_active, h30_active=was_h30,
    )
    if decision.action == p3_stack.ACTION_HOLD:
        return None

    def _h30_fields() -> dict:
        return dict(timestamp=now.isoformat(), trade_id=state.position_epoch,
                    entry_time=entry_at.isoformat(),
                    elapsed=round(float(decision.elapsed_min or 0.0), 2),
                    net=round(float(decision.net_pct or 0.0), 4),
                    h50_active=h50_active)

    # ── 연장 시작/지속 -- 주문이 나가지 않는 유일한 분기다 ──────────────────
    if decision.action == p3_stack.ACTION_EXTEND:
        if p3_stack.note_h30_start(state, now, entry_at=entry_at):
            _p3_log("P3_H30_START", **_h30_fields(),
                    direction=getattr(_position_direction(position), "value", None),
                    regime=state.p3_entry_regime,
                    deadline=state.p3_h30_deadline_at,
                    mode=p3_stack.MODE_H30, reason=decision.conditions)
        # 두 번째 tick 부터는 조용히 유예만 한다(로그 최소화).
        return None

    # 연장 중이었는데 deadline 전에 연장이 끊겼다 = H50 이 풀린 것이다.
    # 아래 Y3 판정(기존 경로)으로 그대로 내려간다 -- 유예만 취소된다.
    if (was_h30 and not h50_active
            and float(decision.elapsed_min or 0.0)
            < float(config.P3_H30_EXT_MAX_HOLD_MIN)):
        _p3_log("P3_H30_CANCEL", **_h30_fields(),
                mode=p3_stack.MODE_H30, reason="h50_released")

    _p3_log("Y3_CHECK", timestamp=now.isoformat(),
            direction=getattr(_position_direction(position), "value", None),
            regime=state.p3_entry_regime, entry_time=entry_at.isoformat(),
            elapsed=round(float(decision.elapsed_min or 0.0), 2),
            net=round(float(decision.net_pct or 0.0), 4),
            mode=(p3_stack.MODE_H30 if was_h30 else p3_stack.MODE_B3),
            reason=decision.conditions)
    if decision.action == p3_stack.ACTION_PROMOTE:
        p3_stack.note_y3_promoted(state, now)
        if was_h30:
            _p3_log("P3_H30_Y3_PROMOTE", **_h30_fields(),
                    mode=p3_stack.MODE_Y3_RUNNER, reason=decision.conditions)
        else:
            _p3_log("Y3_PROMOTE", timestamp=now.isoformat(),
                    net=round(float(decision.net_pct or 0.0), 4),
                    mode=p3_stack.MODE_Y3_RUNNER, reason=decision.conditions)
        return None
    if decision.action != p3_stack.ACTION_EXIT:
        return None
    outcome = order_executor.execute_exit(
        broker=broker, symbol=position.symbol, quantity=position.quantity,
        exit_reason=decision.exit_reason, entry_price=position.avg_price,
        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES,
        reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
    )
    _apply_exit_outcome(state, outcome, exit_reason=decision.exit_reason)
    if outcome.final_state == SignalState.EXECUTED:
        state.time_window_position_active = False
    if was_h30:
        _p3_log("P3_H30_EXIT", **_h30_fields(),
                mode=p3_stack.MODE_H30, reason=decision.reason)
    else:
        _p3_log("B3_MAXHOLD", timestamp=now.isoformat(),
                elapsed=round(float(decision.elapsed_min or 0.0), 2),
                net=round(float(decision.net_pct or 0.0), 4),
                mode=p3_stack.MODE_B3, reason=decision.reason)
    result.actions.append(f"{decision.exit_reason}:{position.symbol}")
    return outcome


def _p3_log(event: str, **fields) -> None:
    """P3 스택의 구조화 로그. 한 줄 = 한 이벤트.

    P3 가 꺼져 있으면 호출되지 않으므로 OFF 일 때 로그가 단 한 줄도 늘지 않는다.
    """
    parts = [f"{k}={v}" for k, v in fields.items() if v is not None and v != ""]
    logger.info("[MACD2][P3] %s %s", event, " ".join(parts))


def _p3_regime_as_of(bars_3m, now: datetime) -> Optional[str]:
    """detector 에 넘길 판정기준 시각 = **마지막 완성봉의 완성시각**.

    그 시각 이후에 끝난 섀도우 거래는 아직 관측되지 않은 것으로 본다 --
    연구엔진이 ``bar_start + 3분`` 으로 잘라 세던 것과 같은 계약이다.
    """
    idx = p3_stack.last_completed_bar_index(bars_3m, now)
    if idx is None:
        return None
    try:
        ts = bars_3m["datetime"].iloc[idx]
    except Exception:
        return None
    start = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=KST)
    return (start + timedelta(minutes=3)).isoformat()


def _advance_p3_regime(*, state: RuntimeState, now: datetime, bars_3m) -> str:
    """detector 를 갱신하고 현재 regime 을 돌려준다. 주문/청산을 하지 않는다.

    P3 가 꺼져 있거나 N1 계열이 아니면 아무 것도 하지 않고 WARMUP 을 돌려준다
    -- 호출부는 WARMUP 을 BASE 로 취급하므로 OFF parity 가 유지된다.
    어떤 예외도 밖으로 내보내지 않는다(fail-safe = BASE).
    """
    if not p3_stack.is_active(state):
        return chop_regime.REGIME_WARMUP
    try:
        decision = chop_regime.current_regime(as_of=_p3_regime_as_of(bars_3m, now))
    except Exception:
        logger.exception("[MACD2][P3] regime 갱신 실패 -- BASE 로 처리한다")
        return chop_regime.REGIME_WARMUP
    prev = state.p3_last_regime
    state.p3_last_regime = decision.regime
    state.p3_last_regime_at = now.isoformat()
    state.p3_last_h50_rate = decision.h50_rate
    state.p3_last_tp1_rate = decision.tp1_rate
    state.p3_last_shadow_sample = int(decision.sample)
    if prev != decision.regime:
        _p3_log("REGIME_UPDATE", timestamp=now.isoformat(), regime=decision.regime,
                H50_rate=decision.h50_rate, TP1_rate=decision.tp1_rate,
                sample=decision.sample, reason=decision.reason)
        if decision.regime == chop_regime.REGIME_CHOP:
            _p3_log("CHOP_ENTER", timestamp=now.isoformat(), regime=decision.regime,
                    H50_rate=decision.h50_rate, TP1_rate=decision.tp1_rate)
        elif prev == chop_regime.REGIME_CHOP:
            _p3_log("CHOP_EXIT", timestamp=now.isoformat(), regime=decision.regime,
                    H50_rate=decision.h50_rate, TP1_rate=decision.tp1_rate)
        if decision.regime == chop_regime.REGIME_WARMUP:
            _p3_log("REGIME_WARMUP", timestamp=now.isoformat(), reason=decision.reason)
    return decision.regime


def _advance_shadow_base(*, state: RuntimeState, now: datetime, quotes: dict,
                         bars_3m, macd_snap) -> None:
    """섀도우 BASE 스트림을 한 tick 전진시킨다. **주문을 절대 내지 않는다.**

    실제 P3 포지션이 B3 로 일찍 끝나도 섀도우 포지션은 BASE 규칙대로 독립적으로
    계속 진행한다 -- 그것이 detector 의 피드백 루프를 막는 유일한 근거다.
    어떤 예외도 밖으로 내보내지 않는다(섀도우 실패가 실거래를 막지 않는다).
    """
    if not p3_stack.is_active(state):
        return
    try:
        shadow_base.on_day_rollover(state, now, log=_p3_log)
        idx = p3_stack.last_completed_bar_index(bars_3m, now)
        bar_ts_iso = None
        if idx is not None:
            try:
                _ts = bars_3m["datetime"].iloc[idx]
                bar_ts_iso = _ts.isoformat() if hasattr(_ts, "isoformat") else str(_ts)
            except Exception:
                bar_ts_iso = None
        # 완성봉 **종가는 넘기지 않는다** -- bars_3m 은 기초자산이고, 하방
        # 래더는 보유 ETF 자신의 가격으로 계산해야 한다(shadow_base 주석 참고).
        shadow_base.advance_exits(
            state, now=now, quotes=quotes, bars_3m=bars_3m,
            completed_bar_idx=idx, log=_p3_log)
    except Exception:
        logger.exception("[MACD2][P3] 섀도우 진행 실패 -- 실거래에는 영향 없음")


def _advance_shadow_late(*, state: RuntimeState, now: datetime, quotes: dict,
                         bars_3m) -> None:
    """섀도우의 **늦은** 청산 단계 -- whipsaw-watch / H50 해제 / C1.

    worker 의 자리를 그대로 따른다: 이 셋은 후보 해석(H50 / 반대신호) **뒤**에
    온다. 순서가 결과를 바꾼다 -- 반대 플래그와 C1 청산이 같은 시각에 걸리면
    누가 먼저냐에 따라 그 거래의 ``h50_intervened`` 가 달라지고, 그 값이 곧
    detector 입력이다(2026-09-27 parity, 2026-07-09 건).
    """
    if not p3_stack.is_active(state):
        return
    try:
        idx = p3_stack.last_completed_bar_index(bars_3m, now)
        bar_ts_iso = None
        if idx is not None:
            try:
                _ts = bars_3m["datetime"].iloc[idx]
                bar_ts_iso = _ts.isoformat() if hasattr(_ts, "isoformat") else str(_ts)
            except Exception:
                bar_ts_iso = None
        shadow_base.advance_whipsaw_watch(
            state, now=now, quotes=quotes, bars_3m=bars_3m,
            bar_ts_iso=bar_ts_iso, log=_p3_log)
        shadow_base.advance_h50_release(
            state, now=now, quotes=quotes, bars_3m=bars_3m,
            bar_ts_iso=bar_ts_iso, log=_p3_log)
        shadow_base.advance_c1(
            state, now=now, quotes=quotes, bars_3m=bars_3m,
            completed_bar_idx=idx, log=_p3_log)
    except Exception:
        logger.exception("[MACD2][P3] 섀도우 late 단계 실패 -- 실거래에는 영향 없음")


def _p3_note_shadow_flag(*, state: RuntimeState, now: datetime,
                         market_data: MarketDataService, bars_3m,
                         direction: Direction, base_cleared: bool = True,
                         flag_bar_dt: Optional[datetime] = None) -> None:
    """확정 플래그 한 건을 섀도우에 전달한다(H50 개입 / 반대신호 / 진입 판정).

    호가는 여기서 직접 모은다 -- 이 함수가 불리는 자리(후보 해석)는 run_once 의
    ``quotes`` 가 인자로 넘어오지 않는 곳이다. 실패해도 조용히 넘어간다.
    """
    if not p3_stack.is_active(state):
        return
    try:
        quotes = _fresh_quote_prices(
            market_data, (config.LONG_SYMBOL, config.INVERSE_SYMBOL))
        # 조기익절의 진입 CHOP 판정은 섀도우도 **같은 함수**로 구한다 --
        # 필터가 꺼져 있으면 계산조차 하지 않는다(기존 관례).
        entry_chop = False
        if early_take_profit.is_enabled(state):
            entry_chop = bool(
                early_take_profit.evaluate_entry_chop(bars_3m, direction, now).is_chop)
        shadow_base.on_confirmed_flag(
            state, direction=direction, now=now, quotes=quotes, bars_3m=bars_3m,
            base_cleared=base_cleared, flag_bar_dt=flag_bar_dt,
            entry_chop=entry_chop, log=_p3_log)
    except Exception:
        logger.exception("[MACD2][P3] 섀도우 플래그 처리 실패 -- 실거래에는 영향 없음")


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
    if time_window_3slot.is_3slot_enabled(state):
        # TWF 3-SLOT (2026-09-07) 은 같은 판정 함수를 그대로 쓴다 -- 진입
        # 로직이 TW2 3-SLOT 과 완전히 동일하기 때문. gate_mode 문자열도
        # 공유해 원장 컬럼/신호 타입이 갈라지지 않게 한다.
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
        "twf_3slot_filter_enabled": bool(state.time_window_twf_filter_enabled),
        "twf_3slot_filter_version": state.time_window_twf_filter_version or config.TWF_3SLOT_FILTER_VERSION,
        "tw2_3slot_active_mode": time_window_3slot.active_3slot_mode(state) or "",
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
    exit_reason: Optional[str] = None,
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
        # 2026-09-21: 예전에는 호출자와 무관하게 항상 EXIT_OPPOSITE_SIGNAL 을
        # 기록했다. 그래서 H50 해제 / whipsaw-watch / C1 청산이 거래원장에서
        # 전부 "반대신호"로 보였고, 실제 MACD 반대 크로스오버가 없었던
        # 2026-09-21 사고에서 "블루가 안 떴는데 반대신호"로 오인하게 만들었다.
        # 호출자가 준 진짜 사유를 기록한다(미지정이면 기존과 완전히 동일).
        exit_reason=(exit_reason or config.EXIT_OPPOSITE_SIGNAL),
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


def _propagate_confirmed_flag_without_orders(
    *,
    state: RuntimeState,
    macd_snap,
    bars_3m,
    df_1m,
    direction: Direction,
    now: datetime,
    result: TickResult,
    defer_reason: str,
) -> None:
    """A confirmed crossover detected on a tick that must not place orders:
    persist it and register its candidate, never call order_executor/broker.

    2026-09-11 real incident. ``reconcile_position_state`` returning
    POSITION_DATA_ERROR/POSITION_MISMATCH/RECOVERED_TO_FLAT makes run_once
    return early, and that early return already (2026-08-31 fix) runs
    ``_advance_confirmed_primary`` so DETECTION never depends on reconcile
    health. But it then returned immediately, so everything DOWNSTREAM of
    detection was skipped: no signal-ledger row, no TW/3-SLOT pending
    candidate, no opposite-signal exit candidate. A crossover is a bar-local
    zero-cross event that can never be re-derived from a later bar, and
    ``evaluate_macd_crossover``'s own repeat-dedup then treats the NEXT
    same-direction flag as a stale repeat — so the flag was lost outright
    while ``last_detected_direction`` silently moved on. Observed 2026-09-11:
    a DOWN_BLUE entry at 10:30:26 was followed by a genuine UP_RED crossover
    that produced ZERO ledger rows and ZERO T+3 candidate, so the reversal
    exit never ran and the inverse position was held to a 11:15 stop-loss;
    the 11:33 DOWN_BLUE that fired afterwards proves the UP_RED had in fact
    been detected (it could not have fired otherwise — its own prev-direction
    dedup would have suppressed it).

    Registering the candidate here is pure bookkeeping — the same
    ``_judge_entry_gate`` call the flat/held paths already make, which for
    TW2/TEG and the 3-SLOT modes only records ``*_pending_flag_*`` state and
    returns TW_PENDING_CONFIRMATION. The real decision still happens one bar
    later in ``_resolve_time_window_candidate``/``_resolve_tw2_3slot_
    candidate`` on a healthy tick, through the completely unmodified
    T+3/quality/TEG path. For a mode with no T+3 concept (or one that would
    have approved an order outright) the flag is instead handed to the
    EXISTING ``state.pending_signal`` retry queue that both the flat and held
    branches already consult every tick, so the order happens as soon as
    reconcile is healthy again — with ``_pending_direction_still_active``'s
    own staleness guard unchanged.

    Ledger rows never collide: a candidate registration reuses the plain
    ``signal_id`` exactly like the live flat/held paths do (its T+3
    resolution row carries the ``:TW2_3SLOT_CONFIRM``/``:TW_CONFIRM``
    suffix), while a deferred-order audit row is written under
    ``signal_id + RECONCILE_DEFERRED_SUFFIX`` so the real order row can
    still be appended under the original signal_id later
    (``ledger.append_signal`` dedups by signal_id and would otherwise drop
    it silently).
    """
    signal_id = make_signal_id(macd_snap.bar_dt, direction)
    if signal_id in state.processed_signal_ids:
        return
    position = state.position
    decision, gate_mode = _judge_entry_gate(
        state=state, bars_3m=bars_3m, df_1m=df_1m, direction=direction, position=position,
        now=now, signal_id=signal_id,
    )
    if decision is not None and not decision.approved:
        # Candidate registered (or genuinely rejected) by the gate itself —
        # identical row shape to the live flat/held flag-bar row.
        _record_major_filtered_signal(
            state=state, macd_snap=macd_snap, direction=direction,
            signal_type="REVERSAL" if (position is not None and position.quantity > 0) else "INITIAL",
            signal_id=signal_id, decision=decision, detected_at=datetime.now(KST),
            result=result, gate_mode=gate_mode,
        )
        return
    # No T+3 gate to hold this flag (legacy/no-filter modes, or a gate that
    # approved it): the order itself is what must wait for reconcile, so keep
    # the event on the existing pending-signal queue and leave an audit row.
    _set_pending_signal(
        state, signal_id=signal_id, direction=direction,
        signal_type="REVERSAL" if (position is not None and position.quantity > 0) else "INITIAL",
        macd_snap=macd_snap, detected_at=now, reason=defer_reason,
    )
    audit_signal_id = f"{signal_id}{RECONCILE_DEFERRED_SUFFIX}"
    if audit_signal_id in state.processed_signal_ids:
        return
    outcome = order_executor.ExecutionOutcome(
        signal_id=audit_signal_id, direction=direction,
        target_symbol=order_executor.target_symbol_for_direction(direction),
        final_state=SignalState.BLOCKED, block_reason=defer_reason,
    )
    _record_signal_ledger(
        state, macd_snap, direction,
        "REVERSAL" if (position is not None and position.quantity > 0) else "INITIAL",
        audit_signal_id, datetime.now(KST), outcome,
    )


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
    # ── 일일 누적매수한도 (2026-10-05) ────────────────────────────────
    # 이 경로는 W1a 규칙배수를 쓰지 않지만 **한도는 지켜야 한다**. 규칙배수를
    # 건드리지 않고 한도만 적용하므로, 한도가 남아 있는 한(=이 진입이 그날
    # 첫 주문인 정상 경로) 주문금액은 기존과 바이트 단위로 동일하다.
    # **E 모드에서만** 적용한다 -- N1/P3 는 이 경로의 주문금액·노출 누계가 기존과
    # 바이트 단위로 같아야 한다(OFF parity). N1/P3 의 같은 한도 우회는 별도 hotfix 대상.
    _cap_mult, _cap_hit = (position_sizing.clip_to_daily_cap(state, 1.0)
                           if e_strategy.is_active(state) else (1.0, False))
    if _cap_hit:
        logger.warning("[MACD2] PREMARKET_CARRY_TW -- 일일 누적매수한도로 주문을 깎는다 "
                       "(배수 1.0 -> %.4f, 사용 %.4f/%.2f)", _cap_mult,
                       position_sizing.exposure_used(state),
                       float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP))
    if _cap_mult <= 0:
        state.premarket_carry_last_result = "DAILY_EXPOSURE_CAP_EXHAUSTED"
        return None
    signal_id = f"PREMARKET_CARRY_TW_{direction.value}_{now.strftime('%Y%m%d')}"
    outcome = order_executor.execute_signal(
        broker=broker, direction=direction, signal_id=signal_id,
        quotes={target_symbol: quote_snap.price}, position=None,
        budget=(float(state.budget or 0.0) * _cap_mult if e_strategy.is_active(state)
                else state.budget),
        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
    )
    _record_premarket_carry_signal(state, direction, signal_id, now, outcome)

    if outcome.final_state == SignalState.EXECUTED:
        # 한도 누계에만 반영한다(진입순번/첫거래 손절 플래그는 건드리지 않는다 --
        # 그 둘은 W1a 규칙배수의 입력이라 올리면 기존 동작이 바뀐다).
        if e_strategy.is_active(state):
            position_sizing.note_external_exposure(state, _cap_mult)
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
        # 2026-09-21: 새 포지션의 시작 — 이전 포지션의 position-scoped
        # 상태(H50/C1/N1/whipsaw-watch)를 **전부** 끝내고 epoch 을 올린다.
        # 반드시 아래 필드 세팅보다 먼저 와야 한다(이 함수가 초기화한다).
        _begin_position_epoch(state, reason="NEW_ENTRY")
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
    # 2026-09-16 사고 수정 (1차 방어): 3-SLOT 계열에서는 발동하지 않는다.
    # 프리마켓 승계(_premarket_carry_should_fire)가 이미 같은 형태의 모드
    # 게이트를 갖고 있었는데 예약매수에만 없었다 -- 그 비대칭이 사고의 구조적
    # 원인이다. 손으로 편집된 state 나 과거 잔존 arm 에 대한 방어이기도 하다.
    if not time_window_3slot.scheduled_entry_supported(state):
        return False
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


def _execute_scheduled_entry(*, broker, market_data: MarketDataService, state: RuntimeState, now: datetime, macd_snap):
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
    if direction is None:
        # 정상 경로에서는 _scheduled_entry_should_fire 가 먼저 막는다. 방어적
        # 조기 반환 -- 예약이 없는데 여기까지 오면 아무 것도 하지 않는다.
        return None

    # ── 발동 직전 MACD 상태 재확인 (2026-09-16 실거래 사고 수정) ──────────
    # 사고: 08:00 에 BLUE 가 떠서 09:03 예약(BLUE)을 걸어뒀는데, 09:00 bar 에서
    # RED 로 뒤집혔다. 그런데도 예약은 예약된 방향 그대로 인버스를 매수했다 --
    # 사야 할 것은 레버리지였다. 09:00 플래그의 T+3 확인은 09:06 이라 09:03
    # 시점에는 아직 "확정 플래그"가 없지만, 09:00-09:03 **완성봉의 MACD 상태**는
    # 이미 뒤집혀 있으므로 그것으로 막을 수 있다.
    #
    # 자매 기능인 프리마켓 승계(_execute_premarket_carry_entry)는 처음부터 바로
    # 이 검사를 하고 있었다("09:03에도 동일 MACD STATE가 유지되면"). 수동 예약만
    # 빠져 있어서 생긴 비대칭이고, 여기서 같은 헬퍼로 맞춘다.
    if macd_snap is None:
        # 완성봉이 아직 없어 검증 자체가 불가능하다 -- 검증 없이 쏘지 않는다.
        # 일시적 상황이므로 발동창(FIRE_WINDOW) 안에서 다음 tick 에 재시도한다.
        state.order_block_reason = "SCHEDULED_ENTRY_MACD_SNAP_UNAVAILABLE"
        return None
    if not _pending_direction_still_active(direction, macd_snap):
        flipped_outcome = order_executor.ExecutionOutcome(
            signal_id=f"SCHEDULED_0903_{direction.value}_{now.strftime('%Y%m%d')}",
            direction=direction,
            target_symbol=order_executor.target_symbol_for_direction(direction),
            final_state=SignalState.BLOCKED,
            block_reason=config.SCHEDULED_ENTRY_MACD_STATE_FLIPPED,
        )
        _record_scheduled_entry_signal(
            state, direction, flipped_outcome.signal_id, now, flipped_outcome)
        state.scheduled_entry_armed_direction = None
        state.scheduled_entry_armed_at = None
        state.scheduled_entry_armed_by = None
        state.scheduled_entry_executed_at = now.isoformat()   # 하루 1회 소진
        state.scheduled_entry_last_result = config.SCHEDULED_ENTRY_MACD_STATE_FLIPPED
        state.order_block_reason = config.SCHEDULED_ENTRY_MACD_STATE_FLIPPED
        return None

    target_symbol = order_executor.target_symbol_for_direction(direction)
    quote_snap = market_data.get_quote(target_symbol)
    if quote_snap is None or quote_snap.error or quote_snap.price <= 0:
        state.order_block_reason = "SCHEDULED_ENTRY_QUOTE_UNAVAILABLE"
        return None

    # ── 일일 누적매수한도 (2026-10-05) ────────────────────────────────
    # 이 경로는 W1a 규칙배수를 쓰지 않지만 **한도는 지켜야 한다**. 규칙배수를
    # 건드리지 않고 한도만 적용하므로, 한도가 남아 있는 한(=이 진입이 그날
    # 첫 주문인 정상 경로) 주문금액은 기존과 바이트 단위로 동일하다.
    # **E 모드에서만** 적용한다 -- N1/P3 는 이 경로의 주문금액·노출 누계가 기존과
    # 바이트 단위로 같아야 한다(OFF parity). N1/P3 의 같은 한도 우회는 별도 hotfix 대상.
    _cap_mult, _cap_hit = (position_sizing.clip_to_daily_cap(state, 1.0)
                           if e_strategy.is_active(state) else (1.0, False))
    if _cap_hit:
        logger.warning("[MACD2] SCHEDULED_ENTRY_0903 -- 일일 누적매수한도로 주문을 깎는다 "
                       "(배수 1.0 -> %.4f, 사용 %.4f/%.2f)", _cap_mult,
                       position_sizing.exposure_used(state),
                       float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP))
    if _cap_mult <= 0:
        state.scheduled_entry_last_result = "DAILY_EXPOSURE_CAP_EXHAUSTED"
        state.order_block_reason = "DAILY_EXPOSURE_CAP_EXHAUSTED"
        return None

    signal_id = f"SCHEDULED_0903_{direction.value}_{now.strftime('%Y%m%d')}"
    outcome = order_executor.execute_signal(
        broker=broker, direction=direction, signal_id=signal_id,
        quotes={target_symbol: quote_snap.price}, position=None,
        budget=(float(state.budget or 0.0) * _cap_mult if e_strategy.is_active(state)
                else state.budget),
        reconcile_retries=ORDER_FILL_RECONCILE_RETRIES, reconcile_delay_sec=ORDER_FILL_RECONCILE_DELAY_SEC,
    )
    _record_scheduled_entry_signal(state, direction, signal_id, now, outcome)

    if outcome.final_state == SignalState.EXECUTED:
        # 한도 누계에만 반영한다(진입순번/첫거래 손절 플래그는 건드리지 않는다).
        if e_strategy.is_active(state):
            position_sizing.note_external_exposure(state, _cap_mult)
        _apply_switch_outcome(state, outcome, direction, now)
        state.scheduled_entry_executed_at = now.isoformat()
        state.scheduled_entry_last_result = "EXECUTED"
        state.scheduled_entry_protected = True
        # 2026-09-16: 체결됐으면 예약을 **소진**한다. 이전에는 armed_direction 이
        # 그대로 남아 UI 가 체결 이후에도 "[예약중] 09시03분 인버스(블루)
        # 전량매수 예약" 을 계속 보여줬다 -- 사용자가 "예약을 건 적이 없는데
        # 예약중으로 떠 있다" 고 본 화면이 바로 이것이다. 이미 소비된 예약이
        # state 에 남아 있을 이유가 없고, 남아 있으면 executed_at 이 어떤
        # 이유로든 지워질 때 재발동 소지가 된다.
        state.scheduled_entry_armed_direction = None
        state.scheduled_entry_armed_at = None
        state.scheduled_entry_armed_by = None
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
    # 2026-09-21: 신규 매수 hard gate 의 유일한 입력. 여기 한 줄로만 기록한다.
    state.last_position_reconcile_result = reconcile
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
        _skip_sig_1m, _ = exclude_preopen_padding_1m(_skip_df_1m)  # 2026-10-01 hotfix
        _skip_bars_3m = resample_completed_3m(_skip_sig_1m, now=now)
        _skip_bars_3m, _ = filter_complete_3m_bars(_skip_bars_3m, _skip_sig_1m)
        _skip_macd_snap = calculate_macd(_skip_bars_3m)
        if _skip_macd_snap is not None:
            try:    # 관측 전용
                _set_observed_frame(_skip_bars_3m, None)
            except Exception:
                pass
            # reconcile 블록으로 조기 return 하는 tick 에서도 프리마켓 표본은
            # 남긴다 -- 프리마켓 플래그는 그 봉이 지나가면 다시 만들 수 없고,
            # 이 경로가 하루 중 08:45~09:00 구간을 통째로 삼키면 관측 자체가
            # 사라진다. 여기서도 주문/상태는 건드리지 않는다(기록 전용).
            try:
                premarket_shadow.observe(state=state, macd_snap=_skip_macd_snap,
                                         bars_3m=_skip_bars_3m, now=now, quotes=None)
            except Exception:  # pragma: no cover - 방어적 이중 차단
                pass
            _skip_direction = _advance_confirmed_primary(state, _skip_macd_snap, now)
            if _skip_direction != Direction.HOLD:
                # 2026-09-11 real incident fix: detection alone is not enough
                # — everything downstream of it (signal-ledger row, TW/3-SLOT
                # pending candidate, opposite-signal exit candidate) used to
                # be skipped by this early return, losing the flag outright.
                # See _propagate_confirmed_flag_without_orders' docstring.
                # Never places an order on this tick.
                try:
                    _propagate_confirmed_flag_without_orders(
                        state=state, macd_snap=_skip_macd_snap, bars_3m=_skip_bars_3m,
                        df_1m=_skip_df_1m, direction=_skip_direction, now=now,
                        result=result, defer_reason=reconcile,
                    )
                except Exception:
                    # This early return exists to keep a reconcile-blocked
                    # tick harmless; an audit-trail failure must never turn it
                    # into a raising tick.
                    logger.exception("[MACD2] confirmed-flag propagation failed during reconcile block")
        # 2026-10-01 hotfix: 잔고조회 실패 tick 에도 보유가 확실한 포지션의 청산 보호
        # (강제청산/손절/익절 판정은 기존 그대로, 주문은 전량 SELL 1회만·BUY 금지).
        if reconcile == POSITION_DATA_ERROR:
            try:
                _protective_exit_during_position_data_error(
                    broker=broker, state=state, market_data=market_data, now=now, result=result)
            except Exception:  # pragma: no cover - 보호 경로가 tick 을 깨면 안 된다
                logger.exception("[MACD2] protective exit path raised")
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
            # 2026-09-16 수정: 청산이 실행된 tick 이라도 **이 tick 의 완성봉
            # 크로스오버 탐지/원장 기록은 반드시 수행한다.**
            #
            # 이 조기 return 은 탐지(_advance_confirmed_primary)보다 앞에 있어서,
            # 청산이 발동한 tick 에 마침 새 크로스오버가 확정되면 그 플래그가
            # 통째로 사라졌다 -- 크로스오버는 봉 단위 일회성 사건이라 나중에
            # 다시 만들 수 없고, evaluate_macd_crossover 의 같은-방향 억제가
            # 다음 같은 방향 플래그까지 연쇄로 삼킨다. reconcile 블록 경로는
            # 2026-08-31 에 정확히 같은 이유로 이미 보강됐는데(탐지는 주문
            # 건강도에 의존하면 안 된다) 이 분기만 빠져 있었다.
            #
            # 청산 자체(가격/사유/수량/슬롯)는 위에서 이미 끝났고 여기서는
            # 건드리지 않는다. 기록 전용이며 주문을 내지 않는다 --
            # _propagate_confirmed_flag_without_orders 는 broker 인자조차 받지
            # 않는다. 실패해도 이미 체결된 청산 tick 을 예외로 만들지 않는다.
            try:
                _exit_df_1m = market_data.get_history_df()
                _exit_sig_1m, _ = exclude_preopen_padding_1m(_exit_df_1m)  # 2026-10-01 hotfix
                _exit_bars_3m = resample_completed_3m(_exit_sig_1m, now=now)
                _exit_bars_3m, _exit_dropped = filter_complete_3m_bars(
                    _exit_bars_3m, _exit_sig_1m)
                _exit_snap = calculate_macd(_exit_bars_3m)
                if _exit_snap is not None:
                    try:    # 관측 전용
                        _set_observed_frame(_exit_bars_3m, _exit_dropped)
                    except Exception:
                        pass
                    _replay_unevaluated_completed_bars(
                        state=state, bars_3m=_exit_bars_3m, now=now, result=result)
                    _exit_direction = _advance_confirmed_primary(state, _exit_snap, now)
                    if _exit_direction != Direction.HOLD:
                        _propagate_confirmed_flag_without_orders(
                            state=state, macd_snap=_exit_snap, bars_3m=_exit_bars_3m,
                            df_1m=_exit_df_1m, direction=_exit_direction, now=now,
                            result=result, defer_reason=TICK_ALREADY_EXECUTED,
                        )
            except Exception:
                logger.exception(
                    "[MACD2] confirmed-flag preservation failed on an exit tick")
            result.timing["total"] = time.monotonic() - tick_started
            return result

    # Worker never calls KIS itself and never triggers the incremental merge —
    # MarketDataService's own history-updater thread refreshes this cache in
    # the background (docs §8/§11); this only reads the cached snapshot.
    t0 = time.monotonic()
    df_1m = market_data.get_history_df()
    result.timing["history_cache_read"] = time.monotonic() - t0
    # 2026-09-22 SMART sizing: confirmation 구간 ETF 수익률 계산용 호가 trail.
    # **사이징 전용 관측치**다 — 진입/청산/주문 판정은 이 값을 읽지 않는다.
    # 실패해도 조용히 넘어간다(판정 불가 = toxic 아님, fail-open).
    try:
        for _etf in (config.LONG_SYMBOL, config.INVERSE_SYMBOL):
            _q = market_data.get_quote(_etf)
            if _q is not None and _q.price and float(_q.price) > 0:
                smart_sizing.append_quote_sample(state, _etf, float(_q.price), _q.fetched_at)
    except Exception:
        pass
    t0 = time.monotonic()
    # 2026-10-01 hotfix: 08:50~08:59 단일가 padding 봉을 신호 입력(MACD/플래그/
    # last_direction/게이트 bars_3m)에서 제외한다. df_1m 원본은 아래의 다른 용도
    # (freshness 진단, VWAP 등)에 그대로 쓴다. 진단값만 state 에 남긴다.
    _sig_df_1m, _padding_excluded = exclude_preopen_padding_1m(df_1m)
    if _padding_excluded:
        state.preopen_padding_excluded_count = len(_padding_excluded)
        state.preopen_padding_last_excluded_at = max(_padding_excluded).isoformat()
    bars_3m = resample_completed_3m(_sig_df_1m, now=now)
    # docs §4: a completed 3m bar only ever counts as "confirmed" when its own
    # 3 constituent 1-minute bars are ALL present — an API error/dropped page
    # must never silently masquerade as a real bar. Any bin missing one or
    # more of its minutes is dropped here (never filled/interpolated), which
    # also blocks that specific bar's crossover/MAJOR-filter/order evaluation
    # (HISTORY_GAP) until the gap is backfilled by a later incremental merge.
    bars_3m, _history_gap_bar_starts = filter_complete_3m_bars(bars_3m, _sig_df_1m)
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

    # ── P3 regime + SHADOW-BASE (2026-09-27) — **기록/판정 전용** ────────
    # 주문을 내지 않는다. detector 를 갱신하고 가상 BASE 스트림을 한 tick
    # 전진시킬 뿐이다. P3 가 꺼져 있으면 두 함수 모두 첫 줄에서 빠지므로
    # OFF 일 때 계산도 상태쓰기도 로그도 단 한 줄 늘지 않는다.
    # 반드시 후보 해석(entry)보다 **앞**에 와야 한다 -- 신규 진입이 이 tick 의
    # regime 스냅샷을 읽기 때문이다.
    _advance_p3_regime(state=state, now=now, bars_3m=bars_3m)
    _advance_shadow_base(state=state, now=now, quotes=quotes,
                         bars_3m=bars_3m, macd_snap=macd_snap)

    # ── 프리마켓 carry SHADOW 관측 (2026-09-13) — **기록 전용** ──────────
    # 주문/슬롯/상태를 전혀 건드리지 않는다. premarket_shadow 는 order_executor
    # 를 import 조차 하지 않고 자체 JSON/CSV 에만 쓴다. 이 호출이 어떤 이유로
    # 실패해도 트레이딩 틱이 멈추면 안 되므로 모듈 내부에서 이미 예외를 삼키고,
    # 여기서 한 번 더 감싼다(이중 fail-open).
    try:
        premarket_shadow.observe(state=state, macd_snap=macd_snap,
                                 bars_3m=bars_3m, now=now, quotes=quotes)
    except Exception:  # pragma: no cover - 방어적 이중 차단
        pass

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
    try:    # 관측 전용 — bar_ledger 진단 컬럼용, 판정에는 쓰이지 않는다
        _set_observed_frame(bars_3m, _history_gap_bar_starts)
    except Exception:
        pass
    # 2026-09-16: 늦게 완성된 미평가 봉을 시간순으로 먼저 따라잡는다.
    # FLAG 복원 전용이며 주문은 내지 않는다 -- 마지막 봉만 아래 live
    # 경로가 평가하고 주문 권한을 갖는다.
    _replay_unevaluated_completed_bars(
        state=state, bars_3m=bars_3m, now=now, result=result)
    confirmed_direction = _advance_confirmed_primary(state, macd_snap, now)
    _advance_premarket_carry_candidate(state, macd_snap, confirmed_direction)

    bar_ts_str = macd_snap.bar_dt.isoformat()

    def _preserve_confirmed_flag(reason: str) -> None:
        """2026-09-11 fix (사용자 요청): an EXECUTION on this tick must not
        swallow this tick's OWN newly confirmed flag.

        Everything below that actually places an order returns early
        (a T+3 switch/sell-only, a whipsaw-watch exit, a pending-signal fill,
        the 09:03 scheduled entry, a premarket-carry entry, PROFIT_LOCK/
        QUICK_PROFIT) — and every one of those early returns sits BEFORE the
        code that records a confirmed flag and registers its candidate. When a
        genuinely new crossover landed on that same bar it was therefore
        dropped outright: no signal-ledger row, no pending/T+3 candidate. A
        crossover is a bar-local zero-cross event that can never be re-derived
        from a later bar, and ``evaluate_macd_crossover``'s repeat-dedup then
        swallows the NEXT same-direction flag too (the 2026-08-31 incident
        shape). Reachable on consecutive-bar flags, which is exactly when a
        T+3 resolution and a fresh flag share one bar.

        Persists state -> ledger -> candidate, and NEVER places an order on
        this tick; the next healthy tick resolves it through the completely
        unmodified T+3/quality/TEG/slot path. Same helper, same dedup
        (``processed_signal_ids`` + ``append_signal``'s signal_id dedup) the
        reconcile-blocked path already uses, so the order that just executed
        here is never duplicated.
        """
        if confirmed_direction == Direction.HOLD:
            return
        try:
            _propagate_confirmed_flag_without_orders(
                state=state, macd_snap=macd_snap, bars_3m=bars_3m, df_1m=df_1m,
                direction=confirmed_direction, now=now, result=result, defer_reason=reason,
            )
        except Exception:
            # An audit-trail failure must never turn an already-executed tick
            # into a raising tick.
            logger.exception("[MACD2] confirmed-flag preservation failed on early-return tick")

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
        # N1 adaptive 판정 갱신 (2026-09-20) — 주문/청산 없음, state 캐시만.
        # 다음 tick 의 _advance_held_position_risk_management 가 이 값을 읽는다.
        try:
            _advance_n1_adaptive(state=state, macd_snap=macd_snap,
                                 bars_3m=bars_3m, position=pos)
        except Exception:
            # 진단 캐시 갱신 실패가 이미 열려 있는 포지션의 리스크관리를
            # 예외로 만들면 안 된다 -- 실패하면 직전 판정(또는 비추세
            # fallback)이 그대로 쓰인다.
            logger.exception("[MACD2] N1 adaptive 판정 갱신 실패")
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
            _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
            return result

        # TW2 3-SLOT (2026-09-01): resolves its own pending T+3 candidate,
        # exactly mirroring the TW2 call just above but via fully separate
        # tw2_3slot_pending_flag_*/tw2_3slot_* bookkeeping. A no-op unless
        # state.time_window_3slot_filter_enabled is True (mutually exclusive
        # with TW2/TEG, so at most one of these two calls ever does anything
        # on a given tick).
        # E: 걸려 있는 돌파 대기를 먼저 본다 -- 폐기 또는 체결. E 가 아니면
        # 즉시 None 이라 N1/P3 에서는 호출 자체가 no-op 이다.
        e_pending_outcome = _advance_e_pending(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
            bars_3m=bars_3m, df_1m=df_1m, position=pos, result=result,
        )
        if e_pending_outcome is not None and e_pending_outcome.final_state == SignalState.EXECUTED:
            if state.position is not None:
                result.actions.append(f"E_BREAKOUT_SWITCH:{e_pending_outcome.target_symbol}")
            else:
                result.actions.append(f"E_BREAKOUT_SELL_ONLY:{e_pending_outcome.target_symbol}")
            _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
            return result

        tw2_3slot_resolve_outcome = _resolve_tw2_3slot_candidate(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
            bars_3m=bars_3m, df_1m=df_1m, position=pos, result=result,
        )
        if tw2_3slot_resolve_outcome is not None and tw2_3slot_resolve_outcome.final_state == SignalState.EXECUTED:
            if state.position is not None:
                result.actions.append(f"TW2_3SLOT_SWITCH:{tw2_3slot_resolve_outcome.target_symbol}")
            else:
                result.actions.append(f"TW2_3SLOT_SELL_ONLY:{tw2_3slot_resolve_outcome.target_symbol}")
            _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
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
        # 섀도우의 늦은 청산 단계 -- 실거래 whipsaw-watch 와 같은 자리다.
        _advance_shadow_late(state=state, now=now, quotes=quotes, bars_3m=bars_3m)

        whipsaw_watch_outcome = _advance_whipsaw_watch(
            broker=broker, state=state, now=now, macd_snap=macd_snap,
            bars_3m=bars_3m, position=pos, result=result,
        )
        if whipsaw_watch_outcome is not None and whipsaw_watch_outcome.final_state == SignalState.EXECUTED:
            _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
            return result

        # H50 HOLD 해제 (2026-09-15): whipsaw-watch 와 같은 자리 -- 하드스톱/
        # TP/트레일링/ETP 가 이미 이 tick 앞에서 평가돼 발동했으면 여기까지
        # 오지 않으므로 기존 래더 우선순위가 그대로 유지된다. 청산 전용이며
        # 신규 진입은 절대 하지 않는다.
        h50_outcome = _advance_h50_hold(
            broker=broker, state=state, now=now, macd_snap=macd_snap,
            bars_3m=bars_3m, position=pos, result=result,
        )
        if h50_outcome is not None and h50_outcome.final_state == SignalState.EXECUTED:
            _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
            return result

        # C1 Peak Protection (2026-09-19): H50/whipsaw-watch 와 같은 자리 --
        # 하드스톱/TP1/TP2/트레일링/ETP/강제청산/반대신호 switch 가 이미 이
        # tick 앞에서 평가돼 발동했으면 여기까지 오지 않으므로 기존 청산
        # 우선순위가 그대로 유지된다. 청산 전용이며 신규 진입은 하지 않는다.
        c1_outcome = _advance_c1_peak_protection(
            broker=broker, state=state, now=now, macd_snap=macd_snap,
            position=pos, result=result,
        )
        if c1_outcome is not None and c1_outcome.final_state == SignalState.EXECUTED:
            _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
            return result

        # B3 max-hold(20분) + Y3 승격 (2026-09-27): C1 바로 뒤 = 같은 자리.
        # 여기까지 왔다는 것은 강제청산/B3 TP·SL/손절/trailing/ETP/반대신호/
        # whipsaw/H50/C1 이 전부 아무 청산도 내지 않았다는 뜻이다. CHOP 으로
        # 진입한 포지션이 아니면 함수 첫 줄에서 바로 빠진다(TREND parity).
        p3_maxhold_outcome = _advance_p3_max_hold(
            broker=broker, state=state, now=now, bars_3m=bars_3m,
            position=pos, quotes=quotes, result=result,
        )
        if (p3_maxhold_outcome is not None
                and p3_maxhold_outcome.final_state == SignalState.EXECUTED):
            _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
            return result

        if (
            state.time_window_2_filter_enabled or state.time_window_teg_filter_enabled
            or time_window_3slot.is_3slot_enabled(state)
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
                if state.time_window_active_mode in time_window_3slot.MODES_3SLOT:
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
                outcome = _retry_pending_signal(
                    broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
                    pending_dir=pending_dir, position=pos, result=result, bars_3m=bars_3m,
                    default_signal_type="REVERSAL",
                )
                if outcome is not None:
                    result.actions.append(f"OPPOSITE_SIGNAL:{pending_dir.value}")
                    state.last_evaluated_bar_ts = bar_ts_str
                    _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
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
            #
            # 2026-09-16 수정: 보호는 **주문만** 막아야 하고 플래그 자체를
            # 삼키면 안 된다. 이전에는 _record_confirmed_blocked_signal 로
            # 원장 행 하나만 남겨서, T+3 후보가 등록되지 않았다 -- 보호가
            # 풀리는 09:10 시점에 해소할 후보가 아예 없어 그 플래그는 그대로
            # 소멸했고, evaluate_macd_crossover 의 같은-방향 억제가 다음
            # 같은 방향 플래그까지 연쇄로 삼켰다(2026-08-31 사고와 동형).
            # reconcile 블록 경로가 쓰는 것과 **같은 헬퍼**로 바꿔 상태/원장/
            # T+3 후보를 전부 남긴다. 이 헬퍼는 주문을 절대 내지 않고,
            # 보호구간 동안 pending 반대신호는 바로 아래 분기가 계속 보류한다.
            _propagate_confirmed_flag_without_orders(
                state=state, macd_snap=macd_snap, bars_3m=bars_3m, df_1m=df_1m,
                direction=confirmed_direction, now=now, result=result,
                defer_reason=config.SCHEDULED_ENTRY_PROTECTION_ACTIVE,
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
                or time_window_3slot.is_3slot_enabled(state)
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
                    _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
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
                _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
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
        _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
        return result

    e_pending_outcome = _advance_e_pending(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        bars_3m=bars_3m, df_1m=df_1m, position=None, result=result,
    )
    if e_pending_outcome is not None and e_pending_outcome.final_state == SignalState.EXECUTED:
        result.actions.append(f"E_BREAKOUT_ENTRY:{e_pending_outcome.target_symbol}")
        _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
        return result

    tw2_3slot_resolve_outcome = _resolve_tw2_3slot_candidate(
        broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        bars_3m=bars_3m, df_1m=df_1m, position=None, result=result,
    )
    # 섀도우의 늦은 청산 단계 -- 실거래가 flat 이어도 섀도우는 포지션을 들고
    # 있을 수 있으므로(knock-on) 보유 경로와 별개로 여기서도 돌린다.
    _advance_shadow_late(state=state, now=now, quotes=quotes, bars_3m=bars_3m)

    if tw2_3slot_resolve_outcome is not None and tw2_3slot_resolve_outcome.final_state == SignalState.EXECUTED:
        result.actions.append(f"TW2_3SLOT_ENTRY:{tw2_3slot_resolve_outcome.target_symbol}")
        _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
        return result

    if _scheduled_entry_should_fire(state, now):
        scheduled_outcome = _execute_scheduled_entry(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap)
        if scheduled_outcome is not None:
            result.actions.append(f"SCHEDULED_ENTRY_0903:{scheduled_outcome.target_symbol}")
            state.last_evaluated_bar_ts = bar_ts_str
            _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
            return result

    if _premarket_carry_should_fire(state, now):
        carry_outcome = _execute_premarket_carry_entry(
            broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
        )
        if carry_outcome is not None:
            result.actions.append(f"PREMARKET_CARRY_TW:{carry_outcome.target_symbol}")
            state.last_evaluated_bar_ts = bar_ts_str
            _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
            return result

    if state.pending_signal and not state.pending_signal.get("order_requested"):
        pending_dir = Direction(state.pending_signal["direction"])
        if _pending_direction_still_active(pending_dir, macd_snap):
            outcome = _retry_pending_signal(
                broker=broker, market_data=market_data, state=state, now=now, macd_snap=macd_snap,
                pending_dir=pending_dir, position=None, result=result, bars_3m=bars_3m,
                default_signal_type="INITIAL",
            )
            if outcome is not None:
                result.actions.append(f"ENTRY:{pending_dir.value}")
                state.last_evaluated_bar_ts = bar_ts_str
                _preserve_confirmed_flag(TICK_ALREADY_EXECUTED)
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


def _apply_exit_outcome(state: RuntimeState, outcome,
                        exit_reason: Optional[str] = None) -> None:
    _record_broker_order_result(state, outcome)
    if outcome.final_state == SignalState.EXECUTED:
        # W1a 사이징(2026-09-12): 그날 **첫 거래**가 정확히
        # config.EXIT_TW_STOP_LOSS 로 전량청산됐는지만 기록한다. TP1 이후
        # 잔량 stop / trailing / 반대신호 / whipsaw / 강제청산 / 조기익절은
        # 전부 해당하지 않는다. ExecutionOutcome 에는 exit_reason 필드가
        # 없으므로 호출부가 명시적으로 넘긴다 -- 안 넘기면 None 이라 no-op.
        # 아래에서 time_window_* 상태가 초기화되기 전에 호출해야 한다.
        position_sizing.note_full_exit(state, exit_reason)
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
        # 2026-09-21: position-scoped 상태는 전부 한 함수에서 정리한다.
        # (C1/N1 은 지우면서 H50/whipsaw-watch 만 빠뜨린 경로가 이번 사고의
        #  원인이었다 — 개별 나열을 없애고 등록 지점을 하나로 만든다.)
        _clear_position_scoped_state(state, reason="FULL_EXIT")
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
        # 2026-09-21: 스위치의 매도레그만 성사돼 flat 이 된 경우도 '포지션
        # 종료' 다. 여기서도 position-scoped 상태를 전부 끝낸다 — 그러지
        # 않으면 뒤이어 성공하는 진입이 이전 포지션의 H50/C1/N1 을 물려받는다.
        _clear_position_scoped_state(state, reason="SWITCH_SELL_LEG_FLAT")
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
    for _safety_key in SAFETY_LEDGER_FIELDS:
        _v = trace.get(_safety_key)
        row[_safety_key] = "" if _v is None else _v
    row.update(major_fields)
    written = ledger.append_signal(row)
    state.last_duplicate_signal_id = None if written else signal_id


#: 2026-09-29 신호원장 safety 진단 컬럼 (ledger.SIGNAL_LEDGER_COLUMNS 에도 있다).
SAFETY_LEDGER_FIELDS = (
    "safety_requested_qty", "safety_requested_amount",
    "safety_capped_qty", "safety_capped_amount",
    "safety_limit_type", "safety_reason", "broker_error_type",
    "daily_ordered_released",
)

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
