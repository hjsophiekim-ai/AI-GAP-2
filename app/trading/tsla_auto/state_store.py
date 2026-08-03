"""TSLA_AUTO runtime state store — data/state/tsla_auto/tsla_auto_runtime.json
only. Never reads/writes any MACD2 path (docs §3).

Atomic write (tmp + os.replace) + a thread lock, same pattern as
app/trading/macd2/state_store.py (docs TSLA_AUTO_COPY_MAP.md — COPY_AS_IS,
path only). Tests must monkeypatch STATE_DIR_PATH/STATE_PATH to a tmp_path.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.trading.tsla_auto import config
from app.trading.tsla_auto.models import Direction, PositionSnapshot, RuntimeState, RuntimeStatus
from app.utils.data_paths import data_path

SCHEMA_VERSION = 1

STATE_DIR_PATH: Path = data_path("state", "tsla_auto")
STATE_PATH: Path = STATE_DIR_PATH / config.RUNTIME_STATE_FILENAME

_FILE_LOCK = threading.RLock()

_UI_MODE_VALUES = {s.value for s in RuntimeStatus}
_DIRECTION_VALUES = {d.value for d in Direction}


def default_state() -> RuntimeState:
    state = RuntimeState()
    state.strategy_id = config.STRATEGY_ID
    state.strategy_name = config.STRATEGY_NAME
    state.mode = config.TSLA_AUTO_MODE_DEFAULT
    state.budget_usd = config.DEFAULT_BUDGET_USD
    state.strong_filter_enabled = bool(config.STRONG_FILTER_DEFAULT)
    state.strong_filter_version = config.STRONG_FILTER_VERSION
    return state


def _position_to_dict(pos: Optional[PositionSnapshot]) -> Optional[dict[str, Any]]:
    if pos is None:
        return None
    return {
        "symbol": pos.symbol, "quantity": pos.quantity, "avg_price": pos.avg_price,
        "entry_at": pos.entry_at.isoformat() if pos.entry_at else None,
    }


def _position_from_dict(raw: Any) -> Optional[PositionSnapshot]:
    if not isinstance(raw, dict):
        return None
    entry_at_raw = raw.get("entry_at")
    return PositionSnapshot(
        symbol=raw.get("symbol"), quantity=int(raw.get("quantity") or 0),
        avg_price=float(raw.get("avg_price") or 0.0),
        entry_at=datetime.fromisoformat(entry_at_raw) if entry_at_raw else None,
    )


def _direction_value(d: Optional[Direction]) -> Optional[str]:
    return d.value if d else None


def serialize(state: RuntimeState) -> dict[str, Any]:
    """RuntimeState -> a plain JSON-serializable dict of only the known schema fields."""
    return {
        "schema_version": SCHEMA_VERSION,
        "strategy_id": state.strategy_id,
        "ui_mode": state.ui_mode.value,
        "auto_trade_on": bool(state.auto_trade_on),
        "mode": state.mode,
        "budget_usd": float(state.budget_usd),
        "stopped": bool(state.stopped),
        "stopped_reason": state.stopped_reason,
        "session_date": state.session_date,
        "warmup_ready": bool(state.warmup_ready),
        "last_signal_direction": _direction_value(state.last_signal_direction),
        "last_detected_direction": _direction_value(state.last_detected_direction),
        "last_executed_direction": _direction_value(state.last_executed_direction),
        "current_episode_direction": _direction_value(state.current_episode_direction),
        "last_evaluated_bar_ts": state.last_evaluated_bar_ts,
        "last_confirmed_bar_ts": state.last_confirmed_bar_ts,
        "processed_signal_ids": list(state.processed_signal_ids),
        "pending_signal": dict(state.pending_signal) if state.pending_signal else None,
        "position": _position_to_dict(state.position),
        "account_holding_qty": int(state.account_holding_qty or 0),
        "strategy_owned_qty": int(state.strategy_owned_qty or 0),
        "strategy_average_price": float(state.strategy_average_price or 0.0),
        "strategy_order_ids": list(state.strategy_order_ids or []),
        "peak_net_return": float(state.peak_net_return),
        "profit_lock_active": bool(state.profit_lock_active),
        "order_block_reason": state.order_block_reason,
        "position_reconcile_diag": dict(state.position_reconcile_diag or {}),
        "last_position_reconcile_at": state.last_position_reconcile_at,
        "market_session_state": dict(state.market_session_state or {}),
        "liquidation_status": dict(state.liquidation_status or {}),
        "strategy_name": state.strategy_name,
        "strategy_version": state.strategy_version,
        "signal_rule": state.signal_rule,
        "worker_code_sha": state.worker_code_sha,
        "session_started_at": state.session_started_at,
        "session_baseline_bar_ts": state.session_baseline_bar_ts,
        "worker_instance_id": state.worker_instance_id,
        "primary_previous_diff": state.primary_previous_diff,
        "primary_current_diff": state.primary_current_diff,
        "primary_relation": state.primary_relation,
        "latest_primary_flag": _direction_value(state.latest_primary_flag),
        "latest_primary_signal_id": state.latest_primary_signal_id,
        "provisional_bar_start": state.provisional_bar_start,
        "provisional_bar_end": state.provisional_bar_end,
        "provisional_macd": state.provisional_macd,
        "provisional_signal": state.provisional_signal,
        "provisional_diff": state.provisional_diff,
        "provisional_flag": _direction_value(state.provisional_flag),
        "provisional_signal_id": state.provisional_signal_id,
        "updated_at": state.updated_at,
        "last_broker_order_id": state.last_broker_order_id,
        "last_broker_order_result": state.last_broker_order_result,
        "last_broker_order_symbol": state.last_broker_order_symbol,
        "last_broker_order_side": state.last_broker_order_side,
        "last_broker_order_at": state.last_broker_order_at,
        "last_duplicate_signal_id": state.last_duplicate_signal_id,
        "last_order_available_usd": state.last_order_available_usd,
        "last_order_usable_usd": state.last_order_usable_usd,
        "last_order_bid1": state.last_order_bid1,
        "last_order_ask1": state.last_order_ask1,
        "last_order_order_price": state.last_order_order_price,
        "last_order_budget_qty": state.last_order_budget_qty,
        "last_order_available_qty": state.last_order_available_qty,
        "last_order_final_qty": state.last_order_final_qty,
        "last_order_expected_notional_usd": state.last_order_expected_notional_usd,
        "last_order_expected_fee_usd": state.last_order_expected_fee_usd,
        "last_order_rt_cd": state.last_order_rt_cd,
        "last_order_msg_cd": state.last_order_msg_cd,
        "last_order_msg1": state.last_order_msg1,
        "last_order_failure_stage": state.last_order_failure_stage,
        "last_order_filled_qty": state.last_order_filled_qty,
        "last_order_fill_poll_result": state.last_order_fill_poll_result,
        "last_order_balance_qty": state.last_order_balance_qty,
        "today_1m_bar_count": state.today_1m_bar_count,
        "history_newest_at": state.history_newest_at,
        "last_completed_3m_bar_at": state.last_completed_3m_bar_at,
        "last_quote_stale_signal_id": state.last_quote_stale_signal_id,
        "last_quote_stale_retry_count": state.last_quote_stale_retry_count,
        "last_quote_stale_result": state.last_quote_stale_result,
        "strong_filter_enabled": bool(state.strong_filter_enabled),
        "strong_filter_enabled_at": state.strong_filter_enabled_at,
        "strong_filter_enabled_by": state.strong_filter_enabled_by,
        "strong_filter_version": state.strong_filter_version or config.STRONG_FILTER_VERSION,
        "daily_entry_count": int(state.daily_entry_count or 0),
        "last_entry_at": state.last_entry_at,
        "last_exit_at": state.last_exit_at,
        "last_exit_direction": _direction_value(state.last_exit_direction),
        "last_score": state.last_score,
        "last_required_score": state.last_required_score,
        "last_approved": state.last_approved,
        "last_decision": state.last_decision,
        "last_block_reason": state.last_block_reason,
        "last_is_reversal": state.last_is_reversal,
        "last_fast_reversal": state.last_fast_reversal,
        "last_component_scores": dict(state.last_component_scores or {}) if state.last_component_scores else None,
        "last_metrics": dict(state.last_metrics or {}) if state.last_metrics else None,
        "last_signal_id": state.last_signal_id,
        "market_regime": state.market_regime,
        "last_stop_loss_exit_at": state.last_stop_loss_exit_at,
        "stop_loss_cooldown_direction": _direction_value(state.stop_loss_cooldown_direction),
        "stop_loss_reentry_override_used_today": bool(state.stop_loss_reentry_override_used_today),
    }


def _direction_from_raw(raw: Any) -> Optional[Direction]:
    return Direction(raw) if raw in _DIRECTION_VALUES else None


def deserialize(raw: dict[str, Any]) -> RuntimeState:
    """Known-schema fields only — any unexpected key in ``raw`` is silently discarded."""
    base = default_state()
    ui_mode_raw = raw.get("ui_mode")
    ui_mode = RuntimeStatus(ui_mode_raw) if ui_mode_raw in _UI_MODE_VALUES else base.ui_mode
    strong_enabled_default = bool(config.STRONG_FILTER_DEFAULT)
    stored_strong_filter_version = str(raw.get("strong_filter_version") or "")
    strong_filter_version = stored_strong_filter_version or config.STRONG_FILTER_VERSION
    strong_filter_enabled = bool(raw.get("strong_filter_enabled", strong_enabled_default))
    if stored_strong_filter_version and stored_strong_filter_version != config.STRONG_FILTER_VERSION:
        strong_filter_version = config.STRONG_FILTER_VERSION
        strong_filter_enabled = strong_enabled_default
    mode = str(raw.get("mode", base.mode))
    if mode == "READ_ONLY" and not bool(raw.get("auto_trade_on", base.auto_trade_on)) and not raw.get("session_started_at"):
        mode = base.mode
    return RuntimeState(
        schema_version=SCHEMA_VERSION,
        strategy_id=str(raw.get("strategy_id") or config.STRATEGY_ID),
        ui_mode=ui_mode,
        auto_trade_on=bool(raw.get("auto_trade_on", base.auto_trade_on)),
        mode=mode,
        budget_usd=float(raw.get("budget_usd", base.budget_usd)),
        stopped=bool(raw.get("stopped", base.stopped)),
        stopped_reason=raw.get("stopped_reason"),
        session_date=raw.get("session_date"),
        warmup_ready=bool(raw.get("warmup_ready", False)),
        last_signal_direction=_direction_from_raw(raw.get("last_signal_direction")),
        last_detected_direction=_direction_from_raw(raw.get("last_detected_direction")),
        last_executed_direction=_direction_from_raw(raw.get("last_executed_direction")),
        current_episode_direction=_direction_from_raw(raw.get("current_episode_direction")),
        last_evaluated_bar_ts=raw.get("last_evaluated_bar_ts"),
        last_confirmed_bar_ts=raw.get("last_confirmed_bar_ts"),
        processed_signal_ids=list(raw.get("processed_signal_ids") or []),
        pending_signal=raw.get("pending_signal") if isinstance(raw.get("pending_signal"), dict) else None,
        position=_position_from_dict(raw.get("position")),
        account_holding_qty=int(raw.get("account_holding_qty") or 0),
        strategy_owned_qty=int(raw.get("strategy_owned_qty") or 0),
        strategy_average_price=float(raw.get("strategy_average_price") or 0.0),
        strategy_order_ids=list(raw.get("strategy_order_ids") or []),
        peak_net_return=float(raw.get("peak_net_return", 0.0)),
        profit_lock_active=bool(raw.get("profit_lock_active", False)),
        order_block_reason=raw.get("order_block_reason"),
        position_reconcile_diag=raw.get("position_reconcile_diag") if isinstance(raw.get("position_reconcile_diag"), dict) else {},
        last_position_reconcile_at=raw.get("last_position_reconcile_at"),
        market_session_state=raw.get("market_session_state") if isinstance(raw.get("market_session_state"), dict) else {},
        liquidation_status=raw.get("liquidation_status") if isinstance(raw.get("liquidation_status"), dict) else {},
        strategy_name=str(raw.get("strategy_name") or config.STRATEGY_NAME),
        strategy_version=str(raw.get("strategy_version") or ""),
        signal_rule=str(raw.get("signal_rule") or ""),
        worker_code_sha=raw.get("worker_code_sha"),
        session_started_at=raw.get("session_started_at"),
        session_baseline_bar_ts=raw.get("session_baseline_bar_ts"),
        worker_instance_id=raw.get("worker_instance_id"),
        primary_previous_diff=raw.get("primary_previous_diff"),
        primary_current_diff=raw.get("primary_current_diff"),
        primary_relation=raw.get("primary_relation"),
        latest_primary_flag=_direction_from_raw(raw.get("latest_primary_flag")),
        latest_primary_signal_id=raw.get("latest_primary_signal_id"),
        provisional_bar_start=raw.get("provisional_bar_start"),
        provisional_bar_end=raw.get("provisional_bar_end"),
        provisional_macd=raw.get("provisional_macd"),
        provisional_signal=raw.get("provisional_signal"),
        provisional_diff=raw.get("provisional_diff"),
        provisional_flag=_direction_from_raw(raw.get("provisional_flag")),
        provisional_signal_id=raw.get("provisional_signal_id"),
        updated_at=raw.get("updated_at"),
        last_broker_order_id=raw.get("last_broker_order_id"),
        last_broker_order_result=raw.get("last_broker_order_result"),
        last_broker_order_symbol=raw.get("last_broker_order_symbol"),
        last_broker_order_side=raw.get("last_broker_order_side"),
        last_broker_order_at=raw.get("last_broker_order_at"),
        last_duplicate_signal_id=raw.get("last_duplicate_signal_id"),
        last_order_available_usd=raw.get("last_order_available_usd"),
        last_order_usable_usd=raw.get("last_order_usable_usd"),
        last_order_bid1=raw.get("last_order_bid1"),
        last_order_ask1=raw.get("last_order_ask1"),
        last_order_order_price=raw.get("last_order_order_price"),
        last_order_budget_qty=raw.get("last_order_budget_qty"),
        last_order_available_qty=raw.get("last_order_available_qty"),
        last_order_final_qty=raw.get("last_order_final_qty"),
        last_order_expected_notional_usd=raw.get("last_order_expected_notional_usd"),
        last_order_expected_fee_usd=raw.get("last_order_expected_fee_usd"),
        last_order_rt_cd=raw.get("last_order_rt_cd"),
        last_order_msg_cd=raw.get("last_order_msg_cd"),
        last_order_msg1=raw.get("last_order_msg1"),
        last_order_failure_stage=raw.get("last_order_failure_stage"),
        last_order_filled_qty=raw.get("last_order_filled_qty"),
        last_order_fill_poll_result=raw.get("last_order_fill_poll_result"),
        last_order_balance_qty=raw.get("last_order_balance_qty"),
        today_1m_bar_count=raw.get("today_1m_bar_count"),
        history_newest_at=raw.get("history_newest_at"),
        last_completed_3m_bar_at=raw.get("last_completed_3m_bar_at"),
        last_quote_stale_signal_id=raw.get("last_quote_stale_signal_id"),
        last_quote_stale_retry_count=raw.get("last_quote_stale_retry_count"),
        last_quote_stale_result=raw.get("last_quote_stale_result"),
        strong_filter_enabled=strong_filter_enabled,
        strong_filter_enabled_at=raw.get("strong_filter_enabled_at"),
        strong_filter_enabled_by=raw.get("strong_filter_enabled_by"),
        strong_filter_version=strong_filter_version,
        daily_entry_count=int(raw.get("daily_entry_count") or 0),
        last_entry_at=raw.get("last_entry_at"),
        last_exit_at=raw.get("last_exit_at"),
        last_exit_direction=_direction_from_raw(raw.get("last_exit_direction")),
        last_score=raw.get("last_score"),
        last_required_score=raw.get("last_required_score"),
        last_approved=raw.get("last_approved"),
        last_decision=raw.get("last_decision"),
        last_block_reason=raw.get("last_block_reason"),
        last_is_reversal=raw.get("last_is_reversal"),
        last_fast_reversal=raw.get("last_fast_reversal"),
        last_component_scores=dict(raw.get("last_component_scores")) if isinstance(raw.get("last_component_scores"), dict) else None,
        last_metrics=dict(raw.get("last_metrics")) if isinstance(raw.get("last_metrics"), dict) else None,
        last_signal_id=raw.get("last_signal_id"),
        market_regime=str(raw.get("market_regime") or "UNKNOWN"),
        last_stop_loss_exit_at=raw.get("last_stop_loss_exit_at"),
        stop_loss_cooldown_direction=_direction_from_raw(raw.get("stop_loss_cooldown_direction")),
        stop_loss_reentry_override_used_today=bool(raw.get("stop_loss_reentry_override_used_today", False)),
    )


def ensure_paths() -> None:
    STATE_DIR_PATH.mkdir(parents=True, exist_ok=True)


def load_state() -> RuntimeState:
    """Load TSLA_AUTO runtime state; corrupted JSON recovers to a fresh
    default rather than raising."""
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
    with _FILE_LOCK:
        ensure_paths()
        state.updated_at = datetime.now(config.ET).isoformat()
        payload = serialize(state)
        tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
        return state
