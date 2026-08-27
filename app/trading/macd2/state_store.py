"""MACD2 runtime state store — data/state/macd2_runtime.json only.

Atomic write (tmp + os.replace) + a thread lock. This module owns exactly one
file and never reads/writes MACD v1's macd_hynix_runtime.json /
macd_hynix_state.json paths or schema (docs/MACD2_LOGIC.md §13). Tests must
monkeypatch ``STATE_DIR``/``STATE_PATH`` to a tmp_path — never the real path.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.trading.macd2 import config
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState, RuntimeStatus
from app.utils.data_paths import STATE_DIR

SCHEMA_VERSION = 1

STATE_DIR_PATH: Path = STATE_DIR
STATE_PATH: Path = STATE_DIR_PATH / config.RUNTIME_STATE_FILENAME

# Frozen at import time -- see app.trading.macd2.ledger's identical pattern
# and its own docstring for the 2026-08-19 incident this guards against.
_DEFAULT_STATE_PATH: Path = STATE_PATH
LIVE_WORKER_MARKER_ENV = "MACD2_LIVE_WORKER_PID"


def _assert_safe_to_write_state() -> None:
    if STATE_PATH != _DEFAULT_STATE_PATH:
        return  # already redirected elsewhere (pytest conftest, an isolated script) -- safe
    if os.environ.get(LIVE_WORKER_MARKER_ENV) == str(os.getpid()):
        return  # this process IS the genuine live Worker thread -- safe
    raise RuntimeError(
        f"REFUSING to write to the production MACD2 state file ({_DEFAULT_STATE_PATH}). "
        "This looks like an ad-hoc/replay script calling save_state() (directly or via "
        "worker.run_once()) without isolating the state path first. Redirect "
        "state_store.STATE_DIR_PATH and state_store.STATE_PATH to a tmp directory BEFORE "
        "calling run_once() -- mirror tests/macd2/conftest.py's _isolate_macd2_state "
        "fixture exactly. If this genuinely is the live Worker process, set "
        f"os.environ['{LIVE_WORKER_MARKER_ENV}'] = str(os.getpid()) once at startup instead "
        "(see app.trading.macd2.worker.Macd2Worker.start())."
    )


_FILE_LOCK = threading.RLock()

_UI_MODE_VALUES = {s.value for s in RuntimeStatus}
_DIRECTION_VALUES = {d.value for d in Direction}


def default_state() -> RuntimeState:
    state = RuntimeState()
    state.major_filter_enabled = bool(getattr(config, "MAJOR_FILTER_DEFAULT", False))
    state.major_filter_version = config.MAJOR_FILTER_VERSION
    state.sideways_filter_enabled = bool(getattr(config, "SIDEWAYS_FILTER_DEFAULT", False))
    state.sideways_filter_version = config.SIDEWAYS_FILTER_VERSION
    state.trend_persistence_filter_enabled = bool(getattr(config, "TREND_PERSISTENCE_FILTER_DEFAULT", False))
    state.trend_persistence_filter_version = config.TREND_PERSISTENCE_FILTER_VERSION
    state.single_entry_filter_enabled = bool(getattr(config, "SINGLE_ENTRY_FILTER_DEFAULT", False))
    state.single_entry_filter_version = config.SINGLE_ENTRY_FILTER_VERSION
    state.time_window_2_filter_enabled = bool(getattr(config, "TIME_WINDOW_2_FILTER_DEFAULT", False))
    state.time_window_2_filter_version = config.TIME_WINDOW_2_FILTER_VERSION
    state.time_window_teg_filter_enabled = bool(getattr(config, "TIME_WINDOW_TEG_FILTER_DEFAULT", False))
    state.time_window_teg_filter_version = config.TIME_WINDOW_TEG_FILTER_VERSION
    state.down_blue_exception_filter_enabled = bool(getattr(config, "TW_DOWN_BLUE_EXCEPTION_FILTER_DEFAULT", False))
    state.down_blue_exception_filter_version = config.TW_DOWN_BLUE_EXCEPTION_FILTER_VERSION
    state.no_filter_0900_1100_enabled = bool(getattr(config, "NO_FILTER_0900_1100_FILTER_DEFAULT", False))
    state.no_filter_0900_1100_filter_version = config.NO_FILTER_0900_1100_FILTER_VERSION
    state.quick_profit_enabled = bool(getattr(config, "QUICK_PROFIT_FILTER_DEFAULT", False))
    state.profit_lock_enabled = bool(getattr(config, "PROFIT_LOCK_DEFAULT_ENABLED", True))
    return state


def _position_to_dict(pos: Optional[PositionSnapshot]) -> Optional[dict[str, Any]]:
    if pos is None:
        return None
    return {
        "symbol": pos.symbol,
        "quantity": pos.quantity,
        "avg_price": pos.avg_price,
        "entry_at": pos.entry_at.isoformat() if pos.entry_at else None,
    }


def _position_from_dict(raw: Any) -> Optional[PositionSnapshot]:
    if not isinstance(raw, dict):
        return None
    entry_at_raw = raw.get("entry_at")
    return PositionSnapshot(
        symbol=raw.get("symbol"),
        quantity=int(raw.get("quantity") or 0),
        avg_price=float(raw.get("avg_price") or 0.0),
        entry_at=datetime.fromisoformat(entry_at_raw) if entry_at_raw else None,
    )


def serialize(state: RuntimeState) -> dict[str, Any]:
    """RuntimeState -> a plain JSON-serializable dict of only the known schema fields."""
    return {
        "schema_version": SCHEMA_VERSION,
        "ui_mode": state.ui_mode.value,
        "auto_trade_on": bool(state.auto_trade_on),
        "mode": state.mode,
        "budget": float(state.budget),
        "stopped": bool(state.stopped),
        "stopped_reason": state.stopped_reason,
        "session_date": state.session_date,
        "warmup_ready": bool(state.warmup_ready),
        "last_signal_direction": (
            state.last_signal_direction.value if state.last_signal_direction else None
        ),
        "last_detected_direction": (
            state.last_detected_direction.value if state.last_detected_direction else None
        ),
        "last_executed_direction": (
            state.last_executed_direction.value if state.last_executed_direction else None
        ),
        "current_episode_direction": (
            state.current_episode_direction.value if state.current_episode_direction else None
        ),
        "last_signal_bar_ts": state.last_signal_bar_ts,
        "last_evaluated_bar_ts": state.last_evaluated_bar_ts,
        "processed_signal_ids": list(state.processed_signal_ids),
        "pending_signal": dict(state.pending_signal) if state.pending_signal else None,
        "position": _position_to_dict(state.position),
        "peak_net_return": float(state.peak_net_return),
        "profit_lock_active": bool(state.profit_lock_active),
        "order_block_reason": state.order_block_reason,
        "position_reconcile_diag": dict(state.position_reconcile_diag or {}),
        "last_position_reconcile_at": state.last_position_reconcile_at,
        "strategy_name": state.strategy_name,
        "strategy_version": state.strategy_version,
        "signal_rule": state.signal_rule,
        "session_started_at": state.session_started_at,
        "session_baseline_bar_ts": state.session_baseline_bar_ts,
        "baseline_relation": state.baseline_relation,
        "worker_instance_id": state.worker_instance_id,
        "possible_toggle_reset_at": state.possible_toggle_reset_at,
        "last_auto_recover_attempt_at": state.last_auto_recover_attempt_at,
        "primary_previous_diff": state.primary_previous_diff,
        "primary_current_diff": state.primary_current_diff,
        "primary_relation": state.primary_relation,
        "latest_primary_flag": state.latest_primary_flag.value if state.latest_primary_flag else None,
        "latest_primary_signal_id": state.latest_primary_signal_id,
        "provisional_bar_start": state.provisional_bar_start,
        "provisional_bar_end": state.provisional_bar_end,
        "provisional_macd": state.provisional_macd,
        "provisional_signal": state.provisional_signal,
        "provisional_diff": state.provisional_diff,
        "provisional_flag": state.provisional_flag.value if state.provisional_flag else None,
        "provisional_signal_id": state.provisional_signal_id,
        "provisional_evaluated_at": state.provisional_evaluated_at,
        "provisional_input_now": state.provisional_input_now,
        "provisional_quote_price": state.provisional_quote_price,
        "provisional_last_1m_at": state.provisional_last_1m_at,
        "provisional_last_1m_close": state.provisional_last_1m_close,
        "provisional_price_scale_note": state.provisional_price_scale_note,
        "provisional_detected_at": state.provisional_detected_at,
        "provisional_order_requested_at": state.provisional_order_requested_at,
        "provisional_ordered_bar_ts": state.provisional_ordered_bar_ts,
        "signed_b_shadow_direction": (
            state.signed_b_shadow_direction.value if state.signed_b_shadow_direction else None
        ),
        "signed_b_shadow_hist_last3": list(state.signed_b_shadow_hist_last3 or ()),
        "updated_at": state.updated_at,
        "candidate_flag": state.candidate_flag.value if state.candidate_flag else None,
        "candidate_bar_ts": state.candidate_bar_ts,
        "candidate_first_seen_at": state.candidate_first_seen_at,
        "candidate_first_diff": state.candidate_first_diff,
        "candidate_confirmed_at": state.candidate_confirmed_at,
        "candidate_confirmed_diff": state.candidate_confirmed_diff,
        "last_broker_order_id": state.last_broker_order_id,
        "last_broker_order_result": state.last_broker_order_result,
        "last_broker_order_symbol": state.last_broker_order_symbol,
        "last_broker_order_side": state.last_broker_order_side,
        "last_broker_order_at": state.last_broker_order_at,
        "last_duplicate_signal_id": state.last_duplicate_signal_id,
        "last_order_orderable_cash": state.last_order_orderable_cash,
        "last_order_nrcvb_buy_amt": state.last_order_nrcvb_buy_amt,
        "last_order_nrcvb_buy_qty": state.last_order_nrcvb_buy_qty,
        "last_order_psbl_qty_calc_unpr": state.last_order_psbl_qty_calc_unpr,
        "last_order_ask1": state.last_order_ask1,
        "last_order_order_price": state.last_order_order_price,
        "last_order_order_type": state.last_order_order_type,
        "last_order_usable_cash": state.last_order_usable_cash,
        "last_order_limit_buyable_qty": state.last_order_limit_buyable_qty,
        "last_order_budget_qty": state.last_order_budget_qty,
        "last_order_final_qty": state.last_order_final_qty,
        "last_order_sizing_rt_cd": state.last_order_sizing_rt_cd,
        "last_order_sizing_msg_cd": state.last_order_sizing_msg_cd,
        "last_order_sizing_msg1": state.last_order_sizing_msg1,
        "last_order_sizing_price": state.last_order_sizing_price,
        "last_order_requested_qty": state.last_order_requested_qty,
        "last_order_expected_amount": state.last_order_expected_amount,
        "last_order_failure_stage": state.last_order_failure_stage,
        "last_order_filled_qty": state.last_order_filled_qty,
        "last_order_fill_poll_result": state.last_order_fill_poll_result,
        "last_order_balance_qty": state.last_order_balance_qty,
        "last_confirmed_bar_ts": state.last_confirmed_bar_ts,
        "today_1m_bar_count": state.today_1m_bar_count,
        "history_newest_at": state.history_newest_at,
        "last_completed_3m_bar_at": state.last_completed_3m_bar_at,
        "quote_history_mismatch_reason": state.quote_history_mismatch_reason,
        "last_quote_stale_signal_id": state.last_quote_stale_signal_id,
        "last_quote_stale_quote_ages": state.last_quote_stale_quote_ages,
        "last_quote_stale_retry_count": state.last_quote_stale_retry_count,
        "last_quote_stale_result": state.last_quote_stale_result,
        "major_filter_enabled": bool(state.major_filter_enabled),
        "major_filter_enabled_at": state.major_filter_enabled_at,
        "major_filter_enabled_by": state.major_filter_enabled_by,
        "major_filter_version": state.major_filter_version or config.MAJOR_FILTER_VERSION,
        "daily_major_entry_count": int(state.daily_major_entry_count or 0),
        "last_major_entry_at": state.last_major_entry_at,
        "last_major_exit_at": state.last_major_exit_at,
        "last_major_exit_direction": (
            state.last_major_exit_direction.value if state.last_major_exit_direction else None
        ),
        "last_major_score": state.last_major_score,
        "last_major_required_score": state.last_major_required_score,
        "last_major_approved": state.last_major_approved,
        "last_major_decision": state.last_major_decision,
        "last_major_block_reason": state.last_major_block_reason,
        "last_major_is_reversal": state.last_major_is_reversal,
        "last_major_fast_reversal": state.last_major_fast_reversal,
        "last_major_component_scores": dict(state.last_major_component_scores or {}) if state.last_major_component_scores else None,
        "last_major_metrics": dict(state.last_major_metrics or {}) if state.last_major_metrics else None,
        "last_major_signal_id": state.last_major_signal_id,
        "sideways_filter_enabled": bool(state.sideways_filter_enabled),
        "sideways_filter_enabled_at": state.sideways_filter_enabled_at,
        "sideways_filter_enabled_by": state.sideways_filter_enabled_by,
        "sideways_filter_version": state.sideways_filter_version or config.SIDEWAYS_FILTER_VERSION,
        "daily_sideways_entry_count": int(state.daily_sideways_entry_count or 0),
        "last_sideways_entry_at": state.last_sideways_entry_at,
        "last_sideways_score": state.last_sideways_score,
        "last_sideways_required_score": state.last_sideways_required_score,
        "last_sideways_approved": state.last_sideways_approved,
        "last_sideways_decision": state.last_sideways_decision,
        "last_sideways_block_reason": state.last_sideways_block_reason,
        "last_sideways_component_scores": dict(state.last_sideways_component_scores or {}) if state.last_sideways_component_scores else None,
        "last_sideways_metrics": dict(state.last_sideways_metrics or {}) if state.last_sideways_metrics else None,
        "last_sideways_signal_id": state.last_sideways_signal_id,
        "trend_persistence_filter_enabled": bool(state.trend_persistence_filter_enabled),
        "trend_persistence_filter_enabled_at": state.trend_persistence_filter_enabled_at,
        "trend_persistence_filter_enabled_by": state.trend_persistence_filter_enabled_by,
        "trend_persistence_filter_version": state.trend_persistence_filter_version or config.TREND_PERSISTENCE_FILTER_VERSION,
        "daily_trend_persistence_entry_count": int(state.daily_trend_persistence_entry_count or 0),
        "last_trend_persistence_entry_at": state.last_trend_persistence_entry_at,
        "last_trend_persistence_score": state.last_trend_persistence_score,
        "last_trend_persistence_required_score": state.last_trend_persistence_required_score,
        "last_trend_persistence_approved": state.last_trend_persistence_approved,
        "last_trend_persistence_decision": state.last_trend_persistence_decision,
        "last_trend_persistence_block_reason": state.last_trend_persistence_block_reason,
        "last_trend_persistence_component_scores": dict(state.last_trend_persistence_component_scores or {}) if state.last_trend_persistence_component_scores else None,
        "last_trend_persistence_metrics": dict(state.last_trend_persistence_metrics or {}) if state.last_trend_persistence_metrics else None,
        "last_trend_persistence_signal_id": state.last_trend_persistence_signal_id,
        "single_entry_filter_enabled": bool(state.single_entry_filter_enabled),
        "single_entry_filter_enabled_at": state.single_entry_filter_enabled_at,
        "single_entry_filter_enabled_by": state.single_entry_filter_enabled_by,
        "single_entry_filter_version": state.single_entry_filter_version or config.SINGLE_ENTRY_FILTER_VERSION,
        "daily_single_entry_count": int(state.daily_single_entry_count or 0),
        "last_single_entry_at": state.last_single_entry_at,
        "last_single_entry_approved": state.last_single_entry_approved,
        "last_single_entry_decision": state.last_single_entry_decision,
        "last_single_entry_block_reason": state.last_single_entry_block_reason,
        "last_single_entry_signal_id": state.last_single_entry_signal_id,
        "daily_confirmed_flag_count": int(state.daily_confirmed_flag_count or 0),
        "last_single_entry_score": state.last_single_entry_score,
        "last_single_entry_flag_seq": state.last_single_entry_flag_seq,
        "last_single_entry_near_zero_blue": state.last_single_entry_near_zero_blue,
        "quick_profit_enabled": bool(state.quick_profit_enabled),
        "quick_profit_enabled_at": state.quick_profit_enabled_at,
        "quick_profit_enabled_by": state.quick_profit_enabled_by,
        "stop_loss_bar_symbol": state.stop_loss_bar_symbol,
        "stop_loss_entry_bar_ts": state.stop_loss_entry_bar_ts,
        "stop_loss_bar_ts": state.stop_loss_bar_ts,
        "stop_loss_bar_close": state.stop_loss_bar_close,
        "profit_lock_enabled": bool(state.profit_lock_enabled),
        "profit_lock_enabled_at": state.profit_lock_enabled_at,
        "profit_lock_enabled_by": state.profit_lock_enabled_by,
        "profit_lock_symbol": state.profit_lock_symbol,
        "profit_lock_entry_bar_ts": state.profit_lock_entry_bar_ts,
        "profit_lock_last_bar_ts": state.profit_lock_last_bar_ts,
        "profit_lock_bars_since_entry": int(state.profit_lock_bars_since_entry or 0),
        "profit_lock_gap_history": list(state.profit_lock_gap_history or []),
        "profit_lock_peak_return_pct": float(state.profit_lock_peak_return_pct or 0.0),
        "profit_lock_current_support_gap": state.profit_lock_current_support_gap,
        "profit_lock_max_support_gap": state.profit_lock_max_support_gap,
        "profit_lock_gap_ratio": state.profit_lock_gap_ratio,
        "profit_lock_contraction_count": int(state.profit_lock_contraction_count or 0),
        "profit_lock_drawdown_pct": float(state.profit_lock_drawdown_pct or 0.0),
        "scheduled_entry_armed_direction": (
            state.scheduled_entry_armed_direction.value if state.scheduled_entry_armed_direction else None
        ),
        "scheduled_entry_armed_at": state.scheduled_entry_armed_at,
        "scheduled_entry_armed_by": state.scheduled_entry_armed_by,
        "scheduled_entry_executed_at": state.scheduled_entry_executed_at,
        "scheduled_entry_last_result": state.scheduled_entry_last_result,
        "scheduled_entry_protected": bool(state.scheduled_entry_protected),
        "premarket_carry_candidate_direction": (
            state.premarket_carry_candidate_direction.value if state.premarket_carry_candidate_direction else None
        ),
        "premarket_carry_candidate_bar_ts": state.premarket_carry_candidate_bar_ts,
        "premarket_carry_executed_at": state.premarket_carry_executed_at,
        "premarket_carry_last_result": state.premarket_carry_last_result,
        "time_window_filter_version": state.time_window_filter_version or "",
        "time_window_2_filter_enabled": bool(state.time_window_2_filter_enabled),
        "time_window_2_filter_enabled_at": state.time_window_2_filter_enabled_at,
        "time_window_2_filter_enabled_by": state.time_window_2_filter_enabled_by,
        "time_window_2_filter_version": state.time_window_2_filter_version or config.TIME_WINDOW_2_FILTER_VERSION,
        "time_window_teg_filter_enabled": bool(state.time_window_teg_filter_enabled),
        "time_window_teg_filter_enabled_at": state.time_window_teg_filter_enabled_at,
        "time_window_teg_filter_enabled_by": state.time_window_teg_filter_enabled_by,
        "time_window_teg_filter_version": state.time_window_teg_filter_version or config.TIME_WINDOW_TEG_FILTER_VERSION,
        "time_window_teg_count_cap_bypass_used": bool(state.time_window_teg_count_cap_bypass_used),
        "last_time_window_teg_bypass_at": state.last_time_window_teg_bypass_at,
        "last_time_window_teg_candidate_at": state.last_time_window_teg_candidate_at,
        "last_time_window_teg_approved": state.last_time_window_teg_approved,
        "last_time_window_teg_reject_reasons": list(state.last_time_window_teg_reject_reasons or []),
        "last_time_window_teg_metrics": dict(state.last_time_window_teg_metrics or {}) if state.last_time_window_teg_metrics else None,
        "last_time_window_teg_conditions": dict(state.last_time_window_teg_conditions or {}) if state.last_time_window_teg_conditions else None,
        "time_window_active_mode": state.time_window_active_mode,
        "time_window_morning_entry_count": int(state.time_window_morning_entry_count or 0),
        "time_window_afternoon_entry_count": int(state.time_window_afternoon_entry_count or 0),
        "last_time_window_entry_at": state.last_time_window_entry_at,
        "last_time_window_score": state.last_time_window_score,
        "last_time_window_required_score": state.last_time_window_required_score,
        "last_time_window_approved": state.last_time_window_approved,
        "last_time_window_decision": state.last_time_window_decision,
        "last_time_window_block_reason": state.last_time_window_block_reason,
        "last_time_window_component_scores": dict(state.last_time_window_component_scores or {}) if state.last_time_window_component_scores else None,
        "last_time_window_metrics": dict(state.last_time_window_metrics or {}) if state.last_time_window_metrics else None,
        "last_time_window_signal_id": state.last_time_window_signal_id,
        "time_window_pending_flag_direction": (
            state.time_window_pending_flag_direction.value if state.time_window_pending_flag_direction else None
        ),
        "time_window_pending_flag_bar_ts": state.time_window_pending_flag_bar_ts,
        "time_window_position_active": bool(state.time_window_position_active),
        "time_window_entry_session": state.time_window_entry_session,
        "time_window_entry_flag_seq": state.time_window_entry_flag_seq,
        "time_window_entry_session_seq": state.time_window_entry_session_seq,
        "time_window_tp1_done": bool(state.time_window_tp1_done),
        "time_window_initial_quantity": int(state.time_window_initial_quantity or 0),
        "time_window_peak_net_return": float(state.time_window_peak_net_return or 0.0),
        "down_blue_exception_filter_enabled": bool(state.down_blue_exception_filter_enabled),
        "down_blue_exception_filter_enabled_at": state.down_blue_exception_filter_enabled_at,
        "down_blue_exception_filter_enabled_by": state.down_blue_exception_filter_enabled_by,
        "down_blue_exception_filter_version": state.down_blue_exception_filter_version or config.TW_DOWN_BLUE_EXCEPTION_FILTER_VERSION,
        "daily_down_blue_exception_used": bool(state.daily_down_blue_exception_used),
        "last_down_blue_exception_at": state.last_down_blue_exception_at,
        "no_filter_0900_1100_enabled": bool(state.no_filter_0900_1100_enabled),
        "no_filter_0900_1100_enabled_at": state.no_filter_0900_1100_enabled_at,
        "no_filter_0900_1100_enabled_by": state.no_filter_0900_1100_enabled_by,
        "no_filter_0900_1100_filter_version": state.no_filter_0900_1100_filter_version or config.NO_FILTER_0900_1100_FILTER_VERSION,
        "last_no_filter_0900_1100_approved": state.last_no_filter_0900_1100_approved,
        "last_no_filter_0900_1100_block_reason": state.last_no_filter_0900_1100_block_reason,
    }


def deserialize(raw: dict[str, Any]) -> RuntimeState:
    """Known-schema fields only — any unexpected key in ``raw`` is silently discarded."""
    base = default_state()
    ui_mode_raw = raw.get("ui_mode")
    ui_mode = RuntimeStatus(ui_mode_raw) if ui_mode_raw in _UI_MODE_VALUES else base.ui_mode
    last_dir_raw = raw.get("last_signal_direction")
    last_dir = Direction(last_dir_raw) if last_dir_raw in _DIRECTION_VALUES else None
    detected_raw = raw.get("last_detected_direction")
    detected_dir = Direction(detected_raw) if detected_raw in _DIRECTION_VALUES else None
    executed_raw = raw.get("last_executed_direction")
    executed_dir = Direction(executed_raw) if executed_raw in _DIRECTION_VALUES else None
    episode_raw = raw.get("current_episode_direction")
    episode_dir = Direction(episode_raw) if episode_raw in _DIRECTION_VALUES else None
    latest_primary_raw = raw.get("latest_primary_flag")
    latest_primary_flag = Direction(latest_primary_raw) if latest_primary_raw in _DIRECTION_VALUES else None
    provisional_raw = raw.get("provisional_flag")
    provisional_flag = Direction(provisional_raw) if provisional_raw in _DIRECTION_VALUES else None
    signed_b_raw = raw.get("signed_b_shadow_direction")
    signed_b_shadow = Direction(signed_b_raw) if signed_b_raw in _DIRECTION_VALUES else None
    candidate_raw = raw.get("candidate_flag")
    candidate_flag = Direction(candidate_raw) if candidate_raw in _DIRECTION_VALUES else None
    last_major_exit_raw = raw.get("last_major_exit_direction")
    last_major_exit_direction = (
        Direction(last_major_exit_raw) if last_major_exit_raw in _DIRECTION_VALUES else None
    )
    scheduled_entry_raw = raw.get("scheduled_entry_armed_direction")
    scheduled_entry_armed_direction = (
        Direction(scheduled_entry_raw) if scheduled_entry_raw in _DIRECTION_VALUES else None
    )
    premarket_carry_raw = raw.get("premarket_carry_candidate_direction")
    premarket_carry_candidate_direction = (
        Direction(premarket_carry_raw) if premarket_carry_raw in _DIRECTION_VALUES else None
    )
    tw_pending_raw = raw.get("time_window_pending_flag_direction")
    time_window_pending_flag_direction = (
        Direction(tw_pending_raw) if tw_pending_raw in _DIRECTION_VALUES else None
    )
    # NOTE: TW1 (time_window_filter_enabled) was retired 2026-08-27 -- any
    # stale value for it in an old state.json is silently discarded here
    # (deserialize() only ever reads known-schema fields, per its own
    # docstring). time_window_filter_version is still read back below since
    # it remains a SHARED last-active-variant diagnostic field.
    stored_time_window_filter_version = str(raw.get("time_window_filter_version") or "")
    time_window_filter_version = stored_time_window_filter_version
    time_window_2_enabled_default = bool(getattr(config, "TIME_WINDOW_2_FILTER_DEFAULT", False))
    stored_time_window_2_filter_version = str(raw.get("time_window_2_filter_version") or "")
    time_window_2_filter_version = stored_time_window_2_filter_version or config.TIME_WINDOW_2_FILTER_VERSION
    time_window_2_filter_enabled = bool(raw.get("time_window_2_filter_enabled", time_window_2_enabled_default))
    if stored_time_window_2_filter_version and stored_time_window_2_filter_version != config.TIME_WINDOW_2_FILTER_VERSION:
        time_window_2_filter_version = config.TIME_WINDOW_2_FILTER_VERSION
        time_window_2_filter_enabled = time_window_2_enabled_default
    time_window_teg_enabled_default = bool(getattr(config, "TIME_WINDOW_TEG_FILTER_DEFAULT", False))
    stored_time_window_teg_filter_version = str(raw.get("time_window_teg_filter_version") or "")
    time_window_teg_filter_version = stored_time_window_teg_filter_version or config.TIME_WINDOW_TEG_FILTER_VERSION
    time_window_teg_filter_enabled = bool(raw.get("time_window_teg_filter_enabled", time_window_teg_enabled_default))
    if stored_time_window_teg_filter_version and stored_time_window_teg_filter_version != config.TIME_WINDOW_TEG_FILTER_VERSION:
        time_window_teg_filter_version = config.TIME_WINDOW_TEG_FILTER_VERSION
        time_window_teg_filter_enabled = time_window_teg_enabled_default
    if time_window_teg_filter_enabled and not time_window_2_filter_enabled:
        time_window_2_filter_enabled = True
        time_window_2_filter_version = config.TIME_WINDOW_2_FILTER_VERSION
    down_blue_exception_enabled_default = bool(getattr(config, "TW_DOWN_BLUE_EXCEPTION_FILTER_DEFAULT", False))
    stored_down_blue_exception_filter_version = str(raw.get("down_blue_exception_filter_version") or "")
    down_blue_exception_filter_version = stored_down_blue_exception_filter_version or config.TW_DOWN_BLUE_EXCEPTION_FILTER_VERSION
    down_blue_exception_filter_enabled = bool(raw.get("down_blue_exception_filter_enabled", down_blue_exception_enabled_default))
    if stored_down_blue_exception_filter_version and stored_down_blue_exception_filter_version != config.TW_DOWN_BLUE_EXCEPTION_FILTER_VERSION:
        down_blue_exception_filter_version = config.TW_DOWN_BLUE_EXCEPTION_FILTER_VERSION
        down_blue_exception_filter_enabled = down_blue_exception_enabled_default
    no_filter_0900_1100_enabled_default = bool(getattr(config, "NO_FILTER_0900_1100_FILTER_DEFAULT", False))
    stored_no_filter_0900_1100_filter_version = str(raw.get("no_filter_0900_1100_filter_version") or "")
    no_filter_0900_1100_filter_version = stored_no_filter_0900_1100_filter_version or config.NO_FILTER_0900_1100_FILTER_VERSION
    no_filter_0900_1100_enabled = bool(raw.get("no_filter_0900_1100_enabled", no_filter_0900_1100_enabled_default))
    if stored_no_filter_0900_1100_filter_version and stored_no_filter_0900_1100_filter_version != config.NO_FILTER_0900_1100_FILTER_VERSION:
        no_filter_0900_1100_filter_version = config.NO_FILTER_0900_1100_FILTER_VERSION
        no_filter_0900_1100_enabled = no_filter_0900_1100_enabled_default
    major_enabled_default = bool(getattr(config, "MAJOR_FILTER_DEFAULT", False))
    stored_major_filter_version = str(raw.get("major_filter_version") or "")
    major_filter_version = stored_major_filter_version or config.MAJOR_FILTER_VERSION
    major_filter_enabled = bool(raw.get("major_filter_enabled", major_enabled_default))
    if stored_major_filter_version and stored_major_filter_version != config.MAJOR_FILTER_VERSION:
        major_filter_version = config.MAJOR_FILTER_VERSION
        major_filter_enabled = major_enabled_default

    sideways_enabled_default = bool(getattr(config, "SIDEWAYS_FILTER_DEFAULT", False))
    stored_sideways_filter_version = str(raw.get("sideways_filter_version") or "")
    sideways_filter_version = stored_sideways_filter_version or config.SIDEWAYS_FILTER_VERSION
    sideways_filter_enabled = bool(raw.get("sideways_filter_enabled", sideways_enabled_default))
    if stored_sideways_filter_version and stored_sideways_filter_version != config.SIDEWAYS_FILTER_VERSION:
        sideways_filter_version = config.SIDEWAYS_FILTER_VERSION
        sideways_filter_enabled = sideways_enabled_default

    trend_persistence_enabled_default = bool(getattr(config, "TREND_PERSISTENCE_FILTER_DEFAULT", False))
    stored_trend_persistence_filter_version = str(raw.get("trend_persistence_filter_version") or "")
    trend_persistence_filter_version = stored_trend_persistence_filter_version or config.TREND_PERSISTENCE_FILTER_VERSION
    trend_persistence_filter_enabled = bool(raw.get("trend_persistence_filter_enabled", trend_persistence_enabled_default))
    if stored_trend_persistence_filter_version and stored_trend_persistence_filter_version != config.TREND_PERSISTENCE_FILTER_VERSION:
        trend_persistence_filter_version = config.TREND_PERSISTENCE_FILTER_VERSION
        trend_persistence_filter_enabled = trend_persistence_enabled_default

    single_entry_enabled_default = bool(getattr(config, "SINGLE_ENTRY_FILTER_DEFAULT", False))
    stored_single_entry_filter_version = str(raw.get("single_entry_filter_version") or "")
    single_entry_filter_version = stored_single_entry_filter_version or config.SINGLE_ENTRY_FILTER_VERSION
    single_entry_filter_enabled = bool(raw.get("single_entry_filter_enabled", single_entry_enabled_default))
    if stored_single_entry_filter_version and stored_single_entry_filter_version != config.SINGLE_ENTRY_FILTER_VERSION:
        single_entry_filter_version = config.SINGLE_ENTRY_FILTER_VERSION
        single_entry_filter_enabled = single_entry_enabled_default
    return RuntimeState(
        schema_version=SCHEMA_VERSION,
        ui_mode=ui_mode,
        auto_trade_on=bool(raw.get("auto_trade_on", base.auto_trade_on)),
        mode=str(raw.get("mode", base.mode)),
        budget=float(raw.get("budget", base.budget)),
        stopped=bool(raw.get("stopped", base.stopped)),
        stopped_reason=raw.get("stopped_reason"),
        session_date=raw.get("session_date"),
        warmup_ready=bool(raw.get("warmup_ready", False)),
        last_signal_direction=last_dir,
        last_detected_direction=detected_dir,
        last_executed_direction=executed_dir,
        current_episode_direction=episode_dir,
        last_signal_bar_ts=raw.get("last_signal_bar_ts"),
        last_evaluated_bar_ts=raw.get("last_evaluated_bar_ts"),
        processed_signal_ids=list(raw.get("processed_signal_ids") or []),
        pending_signal=raw.get("pending_signal") if isinstance(raw.get("pending_signal"), dict) else None,
        position=_position_from_dict(raw.get("position")),
        peak_net_return=float(raw.get("peak_net_return", 0.0)),
        profit_lock_active=bool(raw.get("profit_lock_active", False)),
        order_block_reason=raw.get("order_block_reason"),
        position_reconcile_diag=raw.get("position_reconcile_diag") if isinstance(raw.get("position_reconcile_diag"), dict) else {},
        last_position_reconcile_at=raw.get("last_position_reconcile_at"),
        strategy_name=str(raw.get("strategy_name") or config.STRATEGY_NAME),
        strategy_version=str(raw.get("strategy_version") or ""),
        signal_rule=str(raw.get("signal_rule") or ""),
        session_started_at=raw.get("session_started_at"),
        session_baseline_bar_ts=raw.get("session_baseline_bar_ts"),
        baseline_relation=raw.get("baseline_relation"),
        worker_instance_id=raw.get("worker_instance_id"),
        possible_toggle_reset_at=raw.get("possible_toggle_reset_at"),
        last_auto_recover_attempt_at=raw.get("last_auto_recover_attempt_at"),
        primary_previous_diff=raw.get("primary_previous_diff"),
        primary_current_diff=raw.get("primary_current_diff"),
        primary_relation=raw.get("primary_relation"),
        latest_primary_flag=latest_primary_flag,
        latest_primary_signal_id=raw.get("latest_primary_signal_id"),
        provisional_bar_start=raw.get("provisional_bar_start"),
        provisional_bar_end=raw.get("provisional_bar_end"),
        provisional_macd=raw.get("provisional_macd"),
        provisional_signal=raw.get("provisional_signal"),
        provisional_diff=raw.get("provisional_diff"),
        provisional_flag=provisional_flag,
        provisional_signal_id=raw.get("provisional_signal_id"),
        provisional_evaluated_at=raw.get("provisional_evaluated_at"),
        provisional_input_now=raw.get("provisional_input_now"),
        provisional_quote_price=raw.get("provisional_quote_price"),
        provisional_last_1m_at=raw.get("provisional_last_1m_at"),
        provisional_last_1m_close=raw.get("provisional_last_1m_close"),
        provisional_price_scale_note=raw.get("provisional_price_scale_note"),
        provisional_detected_at=raw.get("provisional_detected_at"),
        provisional_order_requested_at=raw.get("provisional_order_requested_at"),
        provisional_ordered_bar_ts=raw.get("provisional_ordered_bar_ts"),
        signed_b_shadow_direction=signed_b_shadow,
        signed_b_shadow_hist_last3=tuple(raw.get("signed_b_shadow_hist_last3") or ()),
        updated_at=raw.get("updated_at"),
        candidate_flag=candidate_flag,
        candidate_bar_ts=raw.get("candidate_bar_ts"),
        candidate_first_seen_at=raw.get("candidate_first_seen_at"),
        candidate_first_diff=raw.get("candidate_first_diff"),
        candidate_confirmed_at=raw.get("candidate_confirmed_at"),
        candidate_confirmed_diff=raw.get("candidate_confirmed_diff"),
        last_broker_order_id=raw.get("last_broker_order_id"),
        last_broker_order_result=raw.get("last_broker_order_result"),
        last_broker_order_symbol=raw.get("last_broker_order_symbol"),
        last_broker_order_side=raw.get("last_broker_order_side"),
        last_broker_order_at=raw.get("last_broker_order_at"),
        last_duplicate_signal_id=raw.get("last_duplicate_signal_id"),
        last_order_orderable_cash=raw.get("last_order_orderable_cash"),
        last_order_nrcvb_buy_amt=raw.get("last_order_nrcvb_buy_amt"),
        last_order_nrcvb_buy_qty=raw.get("last_order_nrcvb_buy_qty"),
        last_order_psbl_qty_calc_unpr=raw.get("last_order_psbl_qty_calc_unpr"),
        last_order_ask1=raw.get("last_order_ask1"),
        last_order_order_price=raw.get("last_order_order_price"),
        last_order_order_type=raw.get("last_order_order_type"),
        last_order_usable_cash=raw.get("last_order_usable_cash"),
        last_order_limit_buyable_qty=raw.get("last_order_limit_buyable_qty"),
        last_order_budget_qty=raw.get("last_order_budget_qty"),
        last_order_final_qty=raw.get("last_order_final_qty"),
        last_order_sizing_rt_cd=raw.get("last_order_sizing_rt_cd"),
        last_order_sizing_msg_cd=raw.get("last_order_sizing_msg_cd"),
        last_order_sizing_msg1=raw.get("last_order_sizing_msg1"),
        last_order_sizing_price=raw.get("last_order_sizing_price"),
        last_order_requested_qty=raw.get("last_order_requested_qty"),
        last_order_expected_amount=raw.get("last_order_expected_amount"),
        last_order_failure_stage=raw.get("last_order_failure_stage"),
        last_order_filled_qty=raw.get("last_order_filled_qty"),
        last_order_fill_poll_result=raw.get("last_order_fill_poll_result"),
        last_order_balance_qty=raw.get("last_order_balance_qty"),
        last_confirmed_bar_ts=raw.get("last_confirmed_bar_ts"),
        today_1m_bar_count=raw.get("today_1m_bar_count"),
        history_newest_at=raw.get("history_newest_at"),
        last_completed_3m_bar_at=raw.get("last_completed_3m_bar_at"),
        quote_history_mismatch_reason=raw.get("quote_history_mismatch_reason"),
        last_quote_stale_signal_id=raw.get("last_quote_stale_signal_id"),
        last_quote_stale_quote_ages=raw.get("last_quote_stale_quote_ages"),
        last_quote_stale_retry_count=raw.get("last_quote_stale_retry_count"),
        last_quote_stale_result=raw.get("last_quote_stale_result"),
        major_filter_enabled=major_filter_enabled,
        major_filter_enabled_at=raw.get("major_filter_enabled_at"),
        major_filter_enabled_by=raw.get("major_filter_enabled_by"),
        major_filter_version=major_filter_version,
        daily_major_entry_count=int(raw.get("daily_major_entry_count") or 0),
        last_major_entry_at=raw.get("last_major_entry_at"),
        last_major_exit_at=raw.get("last_major_exit_at"),
        last_major_exit_direction=last_major_exit_direction,
        last_major_score=raw.get("last_major_score"),
        last_major_required_score=raw.get("last_major_required_score"),
        last_major_approved=raw.get("last_major_approved"),
        last_major_decision=raw.get("last_major_decision"),
        last_major_block_reason=raw.get("last_major_block_reason"),
        last_major_is_reversal=raw.get("last_major_is_reversal"),
        last_major_fast_reversal=raw.get("last_major_fast_reversal"),
        last_major_component_scores=(
            dict(raw.get("last_major_component_scores"))
            if isinstance(raw.get("last_major_component_scores"), dict) else None
        ),
        last_major_metrics=(
            dict(raw.get("last_major_metrics"))
            if isinstance(raw.get("last_major_metrics"), dict) else None
        ),
        last_major_signal_id=raw.get("last_major_signal_id"),
        sideways_filter_enabled=sideways_filter_enabled,
        sideways_filter_enabled_at=raw.get("sideways_filter_enabled_at"),
        sideways_filter_enabled_by=raw.get("sideways_filter_enabled_by"),
        sideways_filter_version=sideways_filter_version,
        daily_sideways_entry_count=int(raw.get("daily_sideways_entry_count") or 0),
        last_sideways_entry_at=raw.get("last_sideways_entry_at"),
        last_sideways_score=raw.get("last_sideways_score"),
        last_sideways_required_score=raw.get("last_sideways_required_score"),
        last_sideways_approved=raw.get("last_sideways_approved"),
        last_sideways_decision=raw.get("last_sideways_decision"),
        last_sideways_block_reason=raw.get("last_sideways_block_reason"),
        last_sideways_component_scores=(
            dict(raw.get("last_sideways_component_scores"))
            if isinstance(raw.get("last_sideways_component_scores"), dict) else None
        ),
        last_sideways_metrics=(
            dict(raw.get("last_sideways_metrics"))
            if isinstance(raw.get("last_sideways_metrics"), dict) else None
        ),
        last_sideways_signal_id=raw.get("last_sideways_signal_id"),
        trend_persistence_filter_enabled=trend_persistence_filter_enabled,
        trend_persistence_filter_enabled_at=raw.get("trend_persistence_filter_enabled_at"),
        trend_persistence_filter_enabled_by=raw.get("trend_persistence_filter_enabled_by"),
        trend_persistence_filter_version=trend_persistence_filter_version,
        daily_trend_persistence_entry_count=int(raw.get("daily_trend_persistence_entry_count") or 0),
        last_trend_persistence_entry_at=raw.get("last_trend_persistence_entry_at"),
        last_trend_persistence_score=raw.get("last_trend_persistence_score"),
        last_trend_persistence_required_score=raw.get("last_trend_persistence_required_score"),
        last_trend_persistence_approved=raw.get("last_trend_persistence_approved"),
        last_trend_persistence_decision=raw.get("last_trend_persistence_decision"),
        last_trend_persistence_block_reason=raw.get("last_trend_persistence_block_reason"),
        last_trend_persistence_component_scores=(
            dict(raw.get("last_trend_persistence_component_scores"))
            if isinstance(raw.get("last_trend_persistence_component_scores"), dict) else None
        ),
        last_trend_persistence_metrics=(
            dict(raw.get("last_trend_persistence_metrics"))
            if isinstance(raw.get("last_trend_persistence_metrics"), dict) else None
        ),
        last_trend_persistence_signal_id=raw.get("last_trend_persistence_signal_id"),
        single_entry_filter_enabled=single_entry_filter_enabled,
        single_entry_filter_enabled_at=raw.get("single_entry_filter_enabled_at"),
        single_entry_filter_enabled_by=raw.get("single_entry_filter_enabled_by"),
        single_entry_filter_version=single_entry_filter_version,
        daily_single_entry_count=int(raw.get("daily_single_entry_count") or 0),
        last_single_entry_at=raw.get("last_single_entry_at"),
        last_single_entry_approved=raw.get("last_single_entry_approved"),
        last_single_entry_decision=raw.get("last_single_entry_decision"),
        last_single_entry_block_reason=raw.get("last_single_entry_block_reason"),
        last_single_entry_signal_id=raw.get("last_single_entry_signal_id"),
        daily_confirmed_flag_count=int(raw.get("daily_confirmed_flag_count") or 0),
        last_single_entry_score=raw.get("last_single_entry_score"),
        last_single_entry_flag_seq=raw.get("last_single_entry_flag_seq"),
        last_single_entry_near_zero_blue=raw.get("last_single_entry_near_zero_blue"),
        quick_profit_enabled=bool(raw.get("quick_profit_enabled", bool(getattr(config, "QUICK_PROFIT_FILTER_DEFAULT", False)))),
        quick_profit_enabled_at=raw.get("quick_profit_enabled_at"),
        quick_profit_enabled_by=raw.get("quick_profit_enabled_by"),
        stop_loss_bar_symbol=raw.get("stop_loss_bar_symbol"),
        stop_loss_entry_bar_ts=raw.get("stop_loss_entry_bar_ts"),
        stop_loss_bar_ts=raw.get("stop_loss_bar_ts"),
        stop_loss_bar_close=raw.get("stop_loss_bar_close"),
        profit_lock_enabled=bool(raw.get("profit_lock_enabled", bool(getattr(config, "PROFIT_LOCK_DEFAULT_ENABLED", True)))),
        profit_lock_enabled_at=raw.get("profit_lock_enabled_at"),
        profit_lock_enabled_by=raw.get("profit_lock_enabled_by"),
        profit_lock_symbol=raw.get("profit_lock_symbol"),
        profit_lock_entry_bar_ts=raw.get("profit_lock_entry_bar_ts"),
        profit_lock_last_bar_ts=raw.get("profit_lock_last_bar_ts"),
        profit_lock_bars_since_entry=int(raw.get("profit_lock_bars_since_entry") or 0),
        profit_lock_gap_history=list(raw.get("profit_lock_gap_history") or []),
        profit_lock_peak_return_pct=float(raw.get("profit_lock_peak_return_pct") or 0.0),
        profit_lock_current_support_gap=raw.get("profit_lock_current_support_gap"),
        profit_lock_max_support_gap=raw.get("profit_lock_max_support_gap"),
        profit_lock_gap_ratio=raw.get("profit_lock_gap_ratio"),
        profit_lock_contraction_count=int(raw.get("profit_lock_contraction_count") or 0),
        profit_lock_drawdown_pct=float(raw.get("profit_lock_drawdown_pct") or 0.0),
        scheduled_entry_armed_direction=scheduled_entry_armed_direction,
        scheduled_entry_armed_at=raw.get("scheduled_entry_armed_at"),
        scheduled_entry_armed_by=raw.get("scheduled_entry_armed_by"),
        scheduled_entry_executed_at=raw.get("scheduled_entry_executed_at"),
        scheduled_entry_last_result=raw.get("scheduled_entry_last_result"),
        scheduled_entry_protected=bool(raw.get("scheduled_entry_protected") or False),
        premarket_carry_candidate_direction=premarket_carry_candidate_direction,
        premarket_carry_candidate_bar_ts=raw.get("premarket_carry_candidate_bar_ts"),
        premarket_carry_executed_at=raw.get("premarket_carry_executed_at"),
        premarket_carry_last_result=raw.get("premarket_carry_last_result"),
        time_window_filter_version=time_window_filter_version,
        time_window_2_filter_enabled=time_window_2_filter_enabled,
        time_window_2_filter_enabled_at=raw.get("time_window_2_filter_enabled_at"),
        time_window_2_filter_enabled_by=raw.get("time_window_2_filter_enabled_by"),
        time_window_2_filter_version=time_window_2_filter_version,
        time_window_teg_filter_enabled=time_window_teg_filter_enabled,
        time_window_teg_filter_enabled_at=raw.get("time_window_teg_filter_enabled_at"),
        time_window_teg_filter_enabled_by=raw.get("time_window_teg_filter_enabled_by"),
        time_window_teg_filter_version=time_window_teg_filter_version,
        time_window_teg_count_cap_bypass_used=bool(raw.get("time_window_teg_count_cap_bypass_used") or False),
        last_time_window_teg_bypass_at=raw.get("last_time_window_teg_bypass_at"),
        last_time_window_teg_candidate_at=raw.get("last_time_window_teg_candidate_at"),
        last_time_window_teg_approved=raw.get("last_time_window_teg_approved"),
        last_time_window_teg_reject_reasons=list(raw.get("last_time_window_teg_reject_reasons") or []),
        last_time_window_teg_metrics=(
            dict(raw.get("last_time_window_teg_metrics"))
            if isinstance(raw.get("last_time_window_teg_metrics"), dict) else None
        ),
        last_time_window_teg_conditions=(
            dict(raw.get("last_time_window_teg_conditions"))
            if isinstance(raw.get("last_time_window_teg_conditions"), dict) else None
        ),
        time_window_active_mode=raw.get("time_window_active_mode"),
        time_window_morning_entry_count=int(raw.get("time_window_morning_entry_count") or 0),
        time_window_afternoon_entry_count=int(raw.get("time_window_afternoon_entry_count") or 0),
        last_time_window_entry_at=raw.get("last_time_window_entry_at"),
        last_time_window_score=raw.get("last_time_window_score"),
        last_time_window_required_score=raw.get("last_time_window_required_score"),
        last_time_window_approved=raw.get("last_time_window_approved"),
        last_time_window_decision=raw.get("last_time_window_decision"),
        last_time_window_block_reason=raw.get("last_time_window_block_reason"),
        last_time_window_component_scores=(
            dict(raw.get("last_time_window_component_scores"))
            if isinstance(raw.get("last_time_window_component_scores"), dict) else None
        ),
        last_time_window_metrics=(
            dict(raw.get("last_time_window_metrics"))
            if isinstance(raw.get("last_time_window_metrics"), dict) else None
        ),
        last_time_window_signal_id=raw.get("last_time_window_signal_id"),
        time_window_pending_flag_direction=time_window_pending_flag_direction,
        time_window_pending_flag_bar_ts=raw.get("time_window_pending_flag_bar_ts"),
        time_window_position_active=bool(raw.get("time_window_position_active") or False),
        time_window_entry_session=raw.get("time_window_entry_session"),
        time_window_entry_flag_seq=raw.get("time_window_entry_flag_seq"),
        time_window_entry_session_seq=raw.get("time_window_entry_session_seq"),
        time_window_tp1_done=bool(raw.get("time_window_tp1_done") or False),
        time_window_initial_quantity=int(raw.get("time_window_initial_quantity") or 0),
        time_window_peak_net_return=float(raw.get("time_window_peak_net_return") or 0.0),
        down_blue_exception_filter_enabled=down_blue_exception_filter_enabled,
        down_blue_exception_filter_enabled_at=raw.get("down_blue_exception_filter_enabled_at"),
        down_blue_exception_filter_enabled_by=raw.get("down_blue_exception_filter_enabled_by"),
        down_blue_exception_filter_version=down_blue_exception_filter_version,
        daily_down_blue_exception_used=bool(raw.get("daily_down_blue_exception_used") or False),
        last_down_blue_exception_at=raw.get("last_down_blue_exception_at"),
        no_filter_0900_1100_enabled=no_filter_0900_1100_enabled,
        no_filter_0900_1100_enabled_at=raw.get("no_filter_0900_1100_enabled_at"),
        no_filter_0900_1100_enabled_by=raw.get("no_filter_0900_1100_enabled_by"),
        no_filter_0900_1100_filter_version=no_filter_0900_1100_filter_version,
        last_no_filter_0900_1100_approved=raw.get("last_no_filter_0900_1100_approved"),
        last_no_filter_0900_1100_block_reason=raw.get("last_no_filter_0900_1100_block_reason"),
    )


def ensure_paths() -> None:
    STATE_DIR_PATH.mkdir(parents=True, exist_ok=True)


def load_state() -> RuntimeState:
    """Load MACD2 runtime state; corrupted JSON recovers to a fresh default rather than raising."""
    with _FILE_LOCK:
        ensure_paths()
        if not STATE_PATH.exists():
            return default_state()
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return default_state()
            return deserialize(raw)
        except Exception:
            return default_state()


def save_state(state: RuntimeState) -> RuntimeState:
    """Atomic write: tmp file + os.replace, guarded by a thread lock."""
    _assert_safe_to_write_state()
    with _FILE_LOCK:
        ensure_paths()
        state.updated_at = datetime.now(config.KST).isoformat()
        payload = serialize(state)
        tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
        return state
