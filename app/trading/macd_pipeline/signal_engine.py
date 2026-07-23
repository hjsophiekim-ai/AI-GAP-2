"""MacdSignalEngine — pure functions ONLY. No I/O, no orders, no threads."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pandas as pd

from app.trading.macd_hynix_strategy import (
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    WARMUP_3M_BARS,
    evaluate_macd_direction,
    evaluate_position_exits,
    resample_completed_3m,
    target_symbol_for_direction,
)


def calculate_signed_b_signal(
    df_1m: Optional[pd.DataFrame],
    *,
    now: Optional[datetime] = None,
    last_signal_direction: Optional[str] = None,
    last_signal_bar_ts: Optional[str] = None,
    session_date: Optional[str] = None,
    warmup_ready: bool = True,
) -> dict[str, Any]:
    """Single signed-B evaluation used by Worker + tests.

    When warmup_ready is False → NOT_READY (no trading MACD numbers as ready).
    """
    if not warmup_ready:
        return {
            "ok": False,
            "display_direction": DIR_HOLD,
            "flag": "NOT_READY",
            "new_signal": False,
            "signal_direction": None,
            "signal_id": None,
            "macd": None,
            "signal": None,
            "hist": None,
            "hist_last3": [],
            "hist_deltas": [],
            "bar_ts": None,
            "bar_close_ts": None,
            "completed_3m_count": 0,
            "reason": "NOT_READY",
            "signal_calculation_active": False,
        }
    ev = evaluate_macd_direction(
        df_1m,
        now=now,
        last_signal_direction=last_signal_direction,
        last_signal_bar_ts=last_signal_bar_ts,
        session_date=session_date,
    )
    flag = ev.get("display_direction") or DIR_HOLD
    return {
        **ev,
        "flag": flag,
        "signal_calculation_active": bool(ev.get("ok")),
    }


def completed_3m_bar_key(ev: dict[str, Any]) -> Optional[str]:
    """Identity of the completed 3m bar under evaluation."""
    return ev.get("bar_close_ts") or ev.get("bar_ts")


def is_new_completed_bar(ev: dict[str, Any], last_seen_bar_close: Optional[str]) -> bool:
    key = completed_3m_bar_key(ev)
    if not key:
        return False
    return str(key) != str(last_seen_bar_close or "")


def build_completed_signal_snapshot(ev: dict[str, Any], *, at: Optional[datetime] = None) -> dict[str, Any]:
    at = at or datetime.now()
    return {
        "flag": ev.get("flag") or ev.get("display_direction") or DIR_HOLD,
        "signal_id": ev.get("signal_id"),
        "bar_ts": ev.get("bar_ts"),
        "completed_bar_at": ev.get("bar_close_ts"),
        "new_signal": bool(ev.get("new_signal")),
        "reason": ev.get("reason"),
        "hist_last3": ev.get("hist_last3") or [],
        "macd": ev.get("macd"),
        "signal": ev.get("signal"),
        "hist": ev.get("hist"),
        "at": at.isoformat(),
        "signal_calculation_active": bool(ev.get("signal_calculation_active", ev.get("ok"))),
    }


def count_completed_3m(df_1m: Optional[pd.DataFrame], now: Optional[datetime] = None) -> int:
    return int(len(resample_completed_3m(df_1m, now=now)))


def warmup_ok(df_1m: Optional[pd.DataFrame], now: Optional[datetime] = None) -> bool:
    return count_completed_3m(df_1m, now=now) >= WARMUP_3M_BARS


def target_for_flag(flag: Optional[str]) -> Optional[str]:
    return target_symbol_for_direction(flag)


def evaluate_exits(
    symbol: str,
    entry_price: float,
    current_price: float,
    quantity: int,
    *,
    peak_net_return: float = 0.0,
    profit_lock_active: bool = False,
) -> dict[str, Any]:
    """Pure exit evaluation (SL / Profit Lock). No orders."""
    return evaluate_position_exits(
        symbol,
        entry_price,
        current_price,
        quantity,
        peak_net_return=peak_net_return,
        profit_lock_active=profit_lock_active,
    )


__all__ = [
    "DIR_UP",
    "DIR_DOWN",
    "DIR_HOLD",
    "calculate_signed_b_signal",
    "completed_3m_bar_key",
    "is_new_completed_bar",
    "build_completed_signal_snapshot",
    "count_completed_3m",
    "warmup_ok",
    "target_for_flag",
    "evaluate_exits",
]
