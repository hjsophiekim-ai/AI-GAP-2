"""Enhanced Hynix/0197X0 intraday recorder and replay backtest.

The recorder stores only market snapshots and decision diagnostics. It never
sends broker orders. Replay uses the same fast-trend helper and trend-switch
planner as live Enhanced switching so recorded sessions can be compared
against the previous pullback-gated behavior.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL
from app.trading.hynix_fast_trend import compute_fast_trend_signal
from app.trading.hynix_pullback_entry import detect_pullback
from app.trading.hynix_trend_switch_accelerator import (
    default_confirm_state,
    default_frequency_state,
    plan_entry,
    update_confirm_tracker,
)

ROOT = Path(__file__).resolve().parent.parent.parent
REPLAY_DATA_DIR = ROOT / "data" / "enhanced_replay"
SESSION_START = time(9, 0)
SESSION_END = time(15, 30)

MINUTE_COLUMNS = [
    "datetime",
    "hynix_open", "hynix_high", "hynix_low", "hynix_close", "hynix_volume",
    "inverse_open", "inverse_high", "inverse_low", "inverse_close", "inverse_volume",
]

FAST_COLUMNS = [
    "datetime", "direction", "confirmation_count", "vwap", "above_vwap",
    "ema_slope_pct", "return_1m_pct", "return_3m_pct", "return_5m_pct",
    "final_signal", "target_position_pct", "order_allowed", "blocked_reason",
]


def _session_dir(date_str: str) -> Path:
    path = REPLAY_DATA_DIR / date_str
    path.mkdir(parents=True, exist_ok=True)
    return path


def _date_str(ts: datetime) -> str:
    return ts.strftime("%Y%m%d")


def _in_session(ts: datetime) -> bool:
    return SESSION_START <= ts.time() <= SESSION_END


def _normalize_minute_df(df: Optional[pd.DataFrame], prefix: str) -> Optional[pd.DataFrame]:
    if df is None or getattr(df, "empty", True):
        return None
    work = df.copy()
    if "datetime" not in work.columns:
        return None
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["datetime", "open", "high", "low", "close"])
    if work.empty:
        return None
    out = work[["datetime", "open", "high", "low", "close", "volume"]].copy()
    out = out.rename(columns={c: f"{prefix}_{c}" for c in ("open", "high", "low", "close", "volume")})
    return out


def _read_csv(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns)
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        return pd.DataFrame(columns=columns)


def record_minute_bars(
    hynix_df_1min: Optional[pd.DataFrame],
    inverse_df_1min: Optional[pd.DataFrame],
    now: Optional[datetime] = None,
) -> dict:
    """Persist matched 000660/0197X0 1m bars for replay."""
    now = now or datetime.now()
    if not _in_session(now):
        return {"recorded": 0, "skipped": "outside_session"}

    h = _normalize_minute_df(hynix_df_1min, "hynix")
    inv = _normalize_minute_df(inverse_df_1min, "inverse")
    if h is None or inv is None:
        return {"recorded": 0, "skipped": "missing_hynix_or_inverse_df"}
    merged = pd.merge(h, inv, on="datetime", how="inner")
    if merged.empty:
        return {"recorded": 0, "skipped": "no_matching_timestamps"}
    merged = merged[merged["datetime"].dt.strftime("%Y%m%d") == _date_str(now)]
    merged = merged[merged["datetime"].dt.time.between(SESSION_START, SESSION_END)]
    if merged.empty:
        return {"recorded": 0, "skipped": "no_session_rows"}

    path = _session_dir(_date_str(now)) / "minute_bars.csv"
    existing = _read_csv(path, MINUTE_COLUMNS)
    if not existing.empty:
        existing["datetime"] = pd.to_datetime(existing["datetime"], errors="coerce")
    combined = pd.concat([existing, merged], ignore_index=True)
    combined["datetime"] = pd.to_datetime(combined["datetime"], errors="coerce")
    combined = combined.dropna(subset=["datetime"]).drop_duplicates(subset=["datetime"], keep="last")
    combined = combined.sort_values("datetime")
    combined["datetime"] = combined["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
    combined = combined.reindex(columns=MINUTE_COLUMNS)
    combined.to_csv(path, index=False, encoding="utf-8-sig")
    return {"recorded": int(len(merged)), "path": str(path)}


def record_fast_watcher_result(
    *,
    now: datetime,
    fast_signal: dict,
    confirmation_count: int,
    final_signal: Optional[str],
    target_position_pct: Optional[float],
    order_allowed: bool,
    blocked_reason: Optional[str],
) -> dict:
    if not _in_session(now):
        return {"recorded": False, "skipped": "outside_session"}
    returns = fast_signal.get("returns") or {}
    row = {
        "datetime": now.isoformat(timespec="seconds"),
        "direction": fast_signal.get("direction"),
        "confirmation_count": int(confirmation_count or 0),
        "vwap": fast_signal.get("vwap"),
        "above_vwap": fast_signal.get("above_vwap"),
        "ema_slope_pct": fast_signal.get("ema_slope_pct"),
        "return_1m_pct": returns.get("1m"),
        "return_3m_pct": returns.get("3m"),
        "return_5m_pct": returns.get("5m"),
        "final_signal": final_signal,
        "target_position_pct": target_position_pct,
        "order_allowed": bool(order_allowed),
        "blocked_reason": blocked_reason,
    }
    path = _session_dir(_date_str(now)) / "fast_watcher.csv"
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FAST_COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerow(row)
    return {"recorded": True, "path": str(path)}


@dataclass
class ReplayPosition:
    symbol: Optional[str] = None
    quantity: int = 0
    entry_price: float = 0.0


def _load_minute_bars(date_str: str) -> pd.DataFrame:
    path = REPLAY_DATA_DIR / date_str / "minute_bars.csv"
    if not path.exists():
        raise FileNotFoundError(f"Replay minute data not found: {path}")
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)


def _price_for(row: pd.Series, symbol: str) -> float:
    return float(row["hynix_close"] if symbol == HYNIX_SYMBOL else row["inverse_close"])


def _signal_from_direction(direction: str) -> str:
    return "HYNIX_BUY" if direction == "UP" else "INVERSE_BUY"


def _symbol_from_signal(signal: str) -> str:
    return HYNIX_SYMBOL if signal.startswith("HYNIX") else INVERSE_SYMBOL


def _trade_pnl(symbol: str, entry: float, exit_price: float, qty: int) -> dict:
    try:
        from app.trading.trading_cost_engine import TradeCostEngine

        result = TradeCostEngine().compute_net_pnl(symbol, entry, exit_price, qty)
        return {"gross_pnl": result["gross_pnl"], "net_pnl": result["net_pnl"]}
    except Exception:
        gross = (exit_price - entry) * qty
        return {"gross_pnl": round(gross, 2), "net_pnl": round(gross, 2)}


def _simulate_strategy(df: pd.DataFrame, *, strategy: str, initial_cash: float) -> dict:
    state = {
        "trend_switch_confirm_tracker": default_confirm_state(),
        "trend_switch_frequency_state": default_frequency_state(),
    }
    position = ReplayPosition()
    cash = float(initial_cash)
    trades: list[dict] = []
    blocked: list[dict] = []
    watcher_rows: list[dict] = []

    for idx in range(len(df)):
        row = df.iloc[idx]
        now = row["datetime"].to_pydatetime()
        hist = pd.DataFrame({
            "datetime": df.iloc[: idx + 1]["datetime"],
            "open": df.iloc[: idx + 1]["hynix_open"],
            "high": df.iloc[: idx + 1]["hynix_high"],
            "low": df.iloc[: idx + 1]["hynix_low"],
            "close": df.iloc[: idx + 1]["hynix_close"],
            "volume": df.iloc[: idx + 1]["hynix_volume"],
        })
        fast = compute_fast_trend_signal(hist, now=now)
        direction = fast.get("direction")
        if direction not in ("UP", "DOWN"):
            continue

        signal = _signal_from_direction(direction)
        desired_symbol = _symbol_from_signal(signal)
        held_symbol = position.symbol
        tracker = update_confirm_tracker(
            state.get("trend_switch_confirm_tracker"),
            signal,
            held_symbol,
            desired_symbol,
            now,
        )
        state["trend_switch_confirm_tracker"] = tracker
        same = int(tracker.get("same_direction_streak", 0))

        target_pct = None
        proceed = False
        block_reason = None
        if strategy == "new":
            plan = plan_entry(
                final_action=signal,
                held_symbol=held_symbol,
                desired_symbol=desired_symbol,
                confirm_tracker=tracker,
                frequency_state=state.get("trend_switch_frequency_state") or default_frequency_state(),
                pullback_result=None,
                now=now,
                data_ok=True,
                has_unconfirmed_order=False,
                daily_return_pct=0.0,
                atr_pct=None,
            )
            proceed = bool(plan.get("proceed"))
            target_pct = plan.get("position_pct") or 0.20
            block_reason = plan.get("block_reason")
        else:
            pullback_df = hist if desired_symbol == HYNIX_SYMBOL else pd.DataFrame({
                "datetime": df.iloc[: idx + 1]["datetime"],
                "open": df.iloc[: idx + 1]["inverse_open"],
                "high": df.iloc[: idx + 1]["inverse_high"],
                "low": df.iloc[: idx + 1]["inverse_low"],
                "close": df.iloc[: idx + 1]["inverse_close"],
                "volume": df.iloc[: idx + 1]["inverse_volume"],
            })
            pullback = detect_pullback(pullback_df)
            proceed = bool(pullback.get("is_pullback"))
            target_pct = 0.20
            block_reason = None if proceed else f"legacy pullback gate: {pullback.get('reason')}"
            if same >= 2 and not proceed:
                blocked.append({
                    "datetime": now.isoformat(timespec="seconds"),
                    "strategy": strategy,
                    "signal": signal,
                    "blocked_reason": block_reason,
                    "violation": "confirmed_signal_blocked_by_pullback",
                })

        watcher_rows.append({
            "datetime": now.isoformat(timespec="seconds"),
            "strategy": strategy,
            "direction": direction,
            "confirmation_count": same,
            "final_signal": signal,
            "target_position_pct": target_pct,
            "order_allowed": proceed,
            "blocked_reason": block_reason,
            "vwap": fast.get("vwap"),
            "ema_slope_pct": fast.get("ema_slope_pct"),
            "returns": fast.get("returns"),
        })

        if not proceed:
            if strategy == "new" and block_reason:
                blocked.append({
                    "datetime": now.isoformat(timespec="seconds"),
                    "strategy": strategy,
                    "signal": signal,
                    "blocked_reason": block_reason,
                })
            continue
        if held_symbol == desired_symbol:
            continue
        if held_symbol and position.quantity > 0:
            exit_price = _price_for(row, held_symbol)
            pnl = _trade_pnl(held_symbol, position.entry_price, exit_price, position.quantity)
            cash += exit_price * position.quantity + pnl["net_pnl"] - pnl["gross_pnl"]
            trades.append({
                "datetime": now.isoformat(timespec="seconds"),
                "strategy": strategy,
                "side": "SELL",
                "symbol": held_symbol,
                "quantity": position.quantity,
                "price": exit_price,
                "pnl_krw": pnl["net_pnl"],
                "return_pct": round((exit_price / position.entry_price - 1.0) * 100.0, 4),
            })
            position = ReplayPosition()
        entry_price = _price_for(row, desired_symbol)
        budget = max(0.0, initial_cash * float(target_pct or 0.20))
        qty = int(budget // entry_price)
        if qty <= 0:
            blocked.append({
                "datetime": now.isoformat(timespec="seconds"),
                "strategy": strategy,
                "signal": signal,
                "blocked_reason": "target budget below one share",
            })
            continue
        cash -= qty * entry_price
        position = ReplayPosition(desired_symbol, qty, entry_price)
        trades.append({
            "datetime": now.isoformat(timespec="seconds"),
            "strategy": strategy,
            "side": "BUY",
            "symbol": desired_symbol,
            "quantity": qty,
            "price": entry_price,
            "pnl_krw": None,
            "return_pct": None,
        })

    if position.symbol and position.quantity > 0 and not df.empty:
        row = df.iloc[-1]
        exit_price = _price_for(row, position.symbol)
        pnl = _trade_pnl(position.symbol, position.entry_price, exit_price, position.quantity)
        cash += exit_price * position.quantity + pnl["net_pnl"] - pnl["gross_pnl"]
        trades.append({
            "datetime": row["datetime"].isoformat(timespec="seconds"),
            "strategy": strategy,
            "side": "SELL",
            "symbol": position.symbol,
            "quantity": position.quantity,
            "price": exit_price,
            "pnl_krw": pnl["net_pnl"],
            "return_pct": round((exit_price / position.entry_price - 1.0) * 100.0, 4),
            "reason": "session_end",
        })

    net_pnl = round(sum(float(t.get("pnl_krw") or 0.0) for t in trades if t["side"] == "SELL"), 2)
    return {
        "strategy": strategy,
        "initial_cash": initial_cash,
        "net_pnl_krw": net_pnl,
        "return_pct": round(net_pnl / initial_cash * 100.0, 4) if initial_cash else 0.0,
        "round_trips": sum(1 for t in trades if t["side"] == "SELL"),
        "trades": trades,
        "blocked_entries": blocked,
        "watcher_rows": watcher_rows,
    }


def replay_backtest(date_str: str, initial_cash: float = 10_000_000.0, save: bool = True) -> dict:
    """Replay one recorded session and compare legacy vs new regime switching."""
    df = _load_minute_bars(date_str)
    legacy = _simulate_strategy(df, strategy="legacy", initial_cash=initial_cash)
    new = _simulate_strategy(df, strategy="new", initial_cash=initial_cash)
    result = {
        "date": date_str,
        "rows": int(len(df)),
        "initial_cash": initial_cash,
        "legacy": legacy,
        "new_regime_switch": new,
        "comparison": {
            "net_pnl_delta_krw": round(new["net_pnl_krw"] - legacy["net_pnl_krw"], 2),
            "return_delta_pct": round(new["return_pct"] - legacy["return_pct"], 4),
            "round_trip_delta": int(new["round_trips"] - legacy["round_trips"]),
        },
        "test_failures": [],
    }
    for row in legacy["blocked_entries"]:
        if row.get("violation") == "confirmed_signal_blocked_by_pullback":
            result["test_failures"].append(row)
    if save:
        path = _session_dir(date_str) / "replay_result.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["saved_path"] = str(path)
    return result


def safe_record_minute_bars(*args, **kwargs) -> None:
    try:
        record_minute_bars(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[EnhancedReplay] minute record skipped: %s", exc)


def safe_record_fast_watcher_result(*args, **kwargs) -> None:
    try:
        record_fast_watcher_result(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[EnhancedReplay] fast watcher record skipped: %s", exc)
