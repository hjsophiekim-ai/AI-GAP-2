"""MACD Hynix execution ledger helpers — daily PnL aggregation for UI."""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Optional

from app.trading import macd_hynix_order_manager as om
from app.utils.time_utils import kst_now

# Order-latency segment keys (seconds between ISO timestamps).
LATENCY_SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("bar_complete_to_signal_detect", "completed_3m_bar_at", "signal_detected_at"),
    ("signal_detect_to_order_request", "signal_detected_at", "order_requested_at"),
    ("order_request_to_kis_accept", "order_requested_at", "kis_order_accepted_at"),
    ("kis_accept_to_fill_confirm", "kis_order_accepted_at", "broker_executed_at"),
    ("signal_detect_to_final_fill", "signal_detected_at", "position_confirmed_at"),
)

LATENCY_TS_KEYS: tuple[str, ...] = (
    "completed_3m_bar_at",
    "signal_detected_at",
    "order_requested_at",
    "kis_order_accepted_at",
    "broker_executed_at",
    "position_confirmed_at",
)

# Pass gates (only evaluated when real signal samples exist).
# Hard requirement: signal→order_requested ≤5s (same-tick preferred; target 0–1s).
GATE_SIGNAL_TO_REQUEST_MEDIAN = 5.0
GATE_SIGNAL_TO_REQUEST_P95 = 5.0
GATE_SIGNAL_TO_KIS_MEDIAN = 5.0  # same-tick path: detect→request≈0 + request→KIS
GATE_SIGNAL_TO_KIS_P95 = 6.0
GATE_REQUEST_TO_KIS_MEDIAN = 4.0
GATE_REQUEST_TO_KIS_P95 = 4.5
GATE_TICK_MEAN = 5.5
GATE_TICK_P95 = 7.0
OVER_THRESHOLD_SEC = 10.0


def parse_iso_timestamp(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    raw = str(ts).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def seconds_between(start: Any, end: Any) -> Optional[float]:
    """Elapsed seconds from start→end ISO timestamps; None if either missing/invalid."""
    a = parse_iso_timestamp(start)
    b = parse_iso_timestamp(end)
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 3)


def percentile(values: list[float], p: float) -> Optional[float]:
    """Nearest-rank percentile on a non-empty list (p in 0..1)."""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def aggregate_latency_values(
    values: list[Any],
    *,
    threshold_sec: float = OVER_THRESHOLD_SEC,
) -> dict[str, Any]:
    """n / median / p95 / maximum / count>threshold for a numeric series."""
    clean: list[float] = []
    for v in values:
        if v is None or v == "":
            continue
        try:
            clean.append(float(v))
        except Exception:
            continue
    if not clean:
        return {
            "n": 0,
            "median": None,
            "p95": None,
            "maximum": None,
            "over_10s_count": 0,
        }
    ordered = sorted(clean)
    return {
        "n": len(ordered),
        "median": round(statistics.median(ordered), 3),
        "p95": percentile(ordered, 0.95),
        "maximum": round(ordered[-1], 3),
        "over_10s_count": sum(1 for v in ordered if v > threshold_sec),
    }


def compute_latency_segments(event: dict[str, Any]) -> dict[str, Optional[float]]:
    """Compute the five segment latencies (seconds) from a timestamp event dict."""
    out: dict[str, Optional[float]] = {}
    for key, start_k, end_k in LATENCY_SEGMENTS:
        out[key] = seconds_between(event.get(start_k), event.get(end_k))
    return out


def _events_from_ledger(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One latency event per signal_id from successful BUY fills with timing cols."""
    by_sid: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("action") or "").upper() != "BUY":
            continue
        if not _bool_val(row.get("success")):
            continue
        if not _bool_val(row.get("position_confirmed")) and str(row.get("position_confirmed")).strip() not in ("",):
            # allow missing position_confirmed on older rows if success
            if str(row.get("position_confirmed")).strip().lower() in ("false", "0", "no"):
                continue
        sid = str(row.get("signal_id") or "").strip()
        if not sid:
            continue
        if not any(str(row.get(k) or "").strip() for k in ("signal_detected_at", "order_requested_at")):
            continue
        event = {k: (row.get(k) or None) for k in LATENCY_TS_KEYS}
        event["signal_id"] = sid
        event["timestamp"] = row.get("timestamp")
        event["segments_sec"] = compute_latency_segments(event)
        by_sid[sid] = event
    return list(by_sid.values())


def _events_from_history(history: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in history or []:
        if not isinstance(raw, dict):
            continue
        if not str(raw.get("signal_id") or "").strip():
            continue
        if not raw.get("signal_detected_at"):
            continue
        event = {k: raw.get(k) for k in LATENCY_TS_KEYS}
        event["signal_id"] = raw.get("signal_id")
        segs = raw.get("segments_sec")
        if not isinstance(segs, dict):
            segs = compute_latency_segments(event)
        event["segments_sec"] = segs
        out.append(event)
    return out


def summarize_order_latency(
    *,
    state: Optional[dict[str, Any]] = None,
    ledger_rows: Optional[list[dict[str, Any]]] = None,
    tick_intervals: Optional[list[float]] = None,
    main_cycle_3m_wait_count: Optional[int] = None,
) -> dict[str, Any]:
    """Aggregate order-latency + worker-tick metrics for UI / gate verdict."""
    state = state or {}
    worker = state.get("worker") or {}
    history = list(state.get("order_latency_history") or [])
    rows = ledger_rows if ledger_rows is not None else om.load_ledger(limit=10_000)

    events = _events_from_history(history)
    ledger_events = _events_from_ledger(rows)
    # Prefer history; fill gaps from ledger by signal_id
    seen = {str(e.get("signal_id")) for e in events}
    for e in ledger_events:
        sid = str(e.get("signal_id"))
        if sid not in seen:
            events.append(e)
            seen.add(sid)

    segment_stats: dict[str, Any] = {}
    for seg_key, _, _ in LATENCY_SEGMENTS:
        vals = [(e.get("segments_sec") or {}).get(seg_key) for e in events]
        segment_stats[seg_key] = aggregate_latency_values(vals)

    intervals = tick_intervals
    if intervals is None:
        intervals = list(worker.get("tick_intervals") or [])
    tick_stats = aggregate_latency_values(intervals, threshold_sec=OVER_THRESHOLD_SEC)
    # Worker tick uses mean (not median) for the gate.
    tick_clean = [float(v) for v in intervals if v is not None and v != ""]
    tick_mean = round(sum(tick_clean) / len(tick_clean), 3) if tick_clean else None

    wait_count = main_cycle_3m_wait_count
    if wait_count is None:
        wait_count = int(worker.get("main_cycle_3m_wait_count") or 0)

    sample_count = int(segment_stats["signal_detect_to_order_request"]["n"])
    # End-to-end samples may be fewer; report max n across core segments.
    sample_count = max(
        sample_count,
        int(segment_stats["signal_detect_to_final_fill"]["n"]),
        len(events),
    )

    # signal→KIS = signal_detect → kis_order_accepted (composed)
    sig_to_kis_vals: list[float] = []
    for e in events:
        v = seconds_between(e.get("signal_detected_at"), e.get("kis_order_accepted_at"))
        if v is not None:
            sig_to_kis_vals.append(v)
    signal_to_kis = aggregate_latency_values(sig_to_kis_vals)

    summary = {
        "sample_count": sample_count,
        "events": events[-50:],
        "segments": segment_stats,
        "signal_detect_to_kis_accept": signal_to_kis,
        "worker_tick": {
            **tick_stats,
            "mean": tick_mean,
            "p95": tick_stats.get("p95"),
            "n": tick_stats.get("n") or 0,
        },
        "main_cycle_3m_wait_count": int(wait_count),
        "ledger_path": str(om.get_ledger_path()),
    }
    summary["verdict"] = evaluate_latency_verdict(summary)
    return summary


def evaluate_latency_verdict(summary: dict[str, Any]) -> str:
    """PASS / FAIL / NOT_MEASURED against instrumentation gates."""
    n = int(summary.get("sample_count") or 0)
    if n <= 0:
        return "NOT_MEASURED"

    seg = (summary.get("segments") or {}).get("signal_detect_to_order_request") or {}
    sig_kis = summary.get("signal_detect_to_kis_accept") or {}
    tick = summary.get("worker_tick") or {}
    waits = int(summary.get("main_cycle_3m_wait_count") or 0)

    checks: list[bool] = []
    # signal→order_request
    if seg.get("n", 0) > 0:
        med = seg.get("median")
        p95 = seg.get("p95")
        if med is not None:
            checks.append(float(med) <= GATE_SIGNAL_TO_REQUEST_MEDIAN)
        if p95 is not None:
            checks.append(float(p95) <= GATE_SIGNAL_TO_REQUEST_P95)
    # signal→KIS accept
    if sig_kis.get("n", 0) > 0:
        med = sig_kis.get("median")
        p95 = sig_kis.get("p95")
        if med is not None:
            checks.append(float(med) <= GATE_SIGNAL_TO_KIS_MEDIAN)
        if p95 is not None:
            checks.append(float(p95) <= GATE_SIGNAL_TO_KIS_P95)
    # worker tick
    if (tick.get("n") or 0) > 0:
        mean = tick.get("mean")
        p95 = tick.get("p95")
        if mean is not None:
            checks.append(float(mean) <= GATE_TICK_MEAN)
        if p95 is not None:
            checks.append(float(p95) <= GATE_TICK_P95)
    checks.append(waits == 0)

    if not checks:
        return "NOT_MEASURED"
    return "PASS" if all(checks) else "FAIL"


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
