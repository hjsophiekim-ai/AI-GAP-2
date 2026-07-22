"""MACD Hynix execution ledger helpers — daily PnL aggregation for UI."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.trading import macd_hynix_order_manager as om
from app.utils.time_utils import kst_now


def _normalize_trading_date(trading_date: Optional[str] = None) -> str:
    """Return YYYY-MM-DD for the requested KST trading date."""
    if trading_date:
        raw = str(trading_date).strip()
        if len(raw) == 8 and raw.isdigit():
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        if len(raw) >= 10 and raw[4] == "-":
            return raw[:10]
        raise ValueError(f"invalid trading_date: {trading_date}")
    return kst_now().strftime("%Y-%m-%d")


def _timestamp_kst_date(ts: Any) -> Optional[str]:
    if ts is None or str(ts).strip() == "":
        return None
    try:
        return datetime.fromisoformat(str(ts).strip()).strftime("%Y-%m-%d")
    except Exception:
        return None


def _bool_val(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "y")


def _float_val(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _int_val(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _successful_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not _bool_val(row.get("success")):
            continue
        if _int_val(row.get("executed_qty")) <= 0:
            continue
        out.append(row)
    return out


def summarize_daily_trading(
    *,
    trading_date: Optional[str] = None,
    budget: float = 10_000_000.0,
) -> dict[str, Any]:
    """Aggregate today's MACD ledger for the UI daily summary panel.

    Round-trip count = successful SELL fills (realized PnL rows).
    Costs = sum of ledger ``cost`` (fees + tax + slippage via TradeCostEngine).
    Profit / loss split uses per-row ``net_pnl`` (BUY rows are entry-cost negatives).
    Return % = total net_pnl / budget * 100 (budget from MACD state, default 10M).
    """
    date_key = _normalize_trading_date(trading_date)
    rows = om.load_ledger(limit=10_000)
    today_rows = [r for r in rows if _timestamp_kst_date(r.get("timestamp")) == date_key]
    live = _successful_rows(today_rows)

    empty: dict[str, Any] = {
        "trading_date": date_key,
        "has_data": False,
        "round_trip_count": 0,
        "buy_fill_count": 0,
        "sell_fill_count": 0,
        "operating_fill_count": 0,
        "total_cost": 0.0,
        "gross_pnl": 0.0,
        "net_pnl": 0.0,
        "profit_amount": 0.0,
        "loss_amount": 0.0,
        "return_pct": 0.0,
        "budget": float(budget or 10_000_000.0),
        "ledger_path": str(om.get_ledger_path()),
    }
    if not live:
        return empty

    buy_rows = [r for r in live if str(r.get("action") or "").upper() == "BUY"]
    sell_rows = [r for r in live if str(r.get("action") or "").upper() == "SELL"]

    total_cost = sum(_float_val(r.get("cost")) for r in live)
    gross_pnl = sum(_float_val(r.get("gross_pnl")) for r in live)
    net_values = [_float_val(r.get("net_pnl")) for r in live]
    net_pnl = sum(net_values)
    profit_amount = sum(v for v in net_values if v > 0)
    loss_amount = sum(-v for v in net_values if v < 0)

    budget_f = float(budget or 10_000_000.0)
    return_pct = (net_pnl / budget_f * 100.0) if budget_f else 0.0

    return {
        "trading_date": date_key,
        "has_data": True,
        "round_trip_count": len(sell_rows),
        "buy_fill_count": len(buy_rows),
        "sell_fill_count": len(sell_rows),
        "operating_fill_count": len(live),
        "total_cost": round(total_cost, 2),
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "profit_amount": round(profit_amount, 2),
        "loss_amount": round(loss_amount, 2),
        "return_pct": round(return_pct, 4),
        "budget": budget_f,
        "ledger_path": str(om.get_ledger_path()),
    }
