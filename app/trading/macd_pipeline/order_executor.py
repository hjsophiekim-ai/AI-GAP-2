"""MacdOrderExecutor — order execution ONLY (broker / switch / liquidate)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.trading import macd_hynix_order_manager as om
from app.trading.macd_hynix_strategy import (
    DIR_DOWN,
    DIR_UP,
    ENTRY_INITIAL,
    EXIT_OPPOSITE,
    EXIT_SESSION,
    opposite_symbol,
    target_symbol_for_direction,
)
from app.trading.macd_hynix_order_manager import SIGNAL_SOURCE


def create_broker(mode: str, *, real_confirm_ok: bool = False):
    mode = "real" if mode == "real" else "mock"
    if mode == "mock":
        return om.create_macd_broker("mock")
    confirm = ""
    if real_confirm_ok:
        from app.config import get_config
        confirm = str(get_config().real_confirm_text() or "")
    return om.create_macd_broker(mode, real_confirm_text=confirm, real_ready=bool(real_confirm_ok))


def execute_switch(
    broker,
    direction: str,
    *,
    mode: str,
    budget: float,
    quotes: dict[str, Any],
    signal_id: str,
    state: dict[str, Any],
    entry_kind: str = ENTRY_INITIAL,
    sell_reason: Optional[str] = None,
) -> dict[str, Any]:
    """Immediate same-tick order path — Worker calls on new signal_id (no pending timer)."""
    pos = state.get("position") or {}
    held = pos.get("symbol")
    target = target_symbol_for_direction(direction)
    if held and target and held != target and not sell_reason:
        sell_reason = EXIT_OPPOSITE
    is_reversal = bool(
        sell_reason == EXIT_OPPOSITE
        or (held and target and held != target and int(pos.get("quantity") or 0) > 0)
    )
    # Latency clock starts here; order_requested stamped before any broker I/O.
    om.begin_order_latency(
        state,
        signal_id=signal_id,
        completed_3m_bar_at=state.get("completed_3m_bar_at")
        or (state.get("completed_signal_snapshot") or {}).get("completed_bar_at"),
        signal_detected_at=datetime.now().isoformat(),
    )
    om.stamp_order_latency(state, "order_requested_at", overwrite=True)
    state["signal_lifecycle"] = "ORDER_INTENT"
    state["current_flag"] = direction
    state["armed_at"] = state.get("armed_at") or datetime.now().isoformat()
    res = om.switch_to_direction(
        broker,
        direction,
        mode=mode,
        budget=budget,
        quotes=quotes,
        signal_id=signal_id,
        state=state,
        entry_kind=entry_kind,
        signal_source=SIGNAL_SOURCE,
        sell_reason=sell_reason,
    )
    if res.get("success"):
        state["signal_lifecycle"] = "LEDGER_RECORDED"
        state["last_signal_id"] = signal_id
        state["last_signal_direction"] = direction
        state["signal_type"] = "REVERSAL" if is_reversal else "INITIAL"
        state["current_flag"] = direction
        state["duplicate_block_reason"] = None
        bar = (state.get("completed_signal_snapshot") or {}).get("bar_ts")
        if bar:
            state["last_signal_bar_ts"] = bar
    else:
        if res.get("duplicate"):
            state["signal_lifecycle"] = "LEDGER_RECORDED"
            state["duplicate_block_reason"] = f"SIGNAL_ID_ALREADY_PROCESSED:{signal_id}"
        else:
            state["signal_lifecycle"] = "ORDER_BLOCKED" if state.get("order_block_reason") else "IDLE"
    return res


def execute_exit(
    broker,
    *,
    mode: str,
    quotes: dict[str, Any],
    state: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return om.exit_position_full(broker, mode=mode, quotes=quotes, state=state, reason=reason)


def force_liquidate(
    broker,
    *,
    mode: str,
    quotes: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    return om.force_liquidate_all(broker, mode=mode, quotes=quotes, state=state)


def quotes_for_order(cache: dict[str, Any]) -> dict[str, Any]:
    """Strip meta; keep hynix/long/inverse slots for OM validators."""
    return {k: v for k, v in cache.items() if k in ("hynix", "long", "inverse")}
