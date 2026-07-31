"""Read-only ≥20-day compare: MACD signed-B exit variants A/B/C.

Entry and symbol selection identical to signed B (NEW_TURN_ONLY / macd_hynix_strategy helpers).
Does NOT modify production code or place broker orders.

Exit variants:
  A FIXED_TP                  — +3.0% net TP (current), SL -1.5%, opposite switch, 15:00 flat
  B OPPOSITE_SIGNAL_ONLY      — no fixed TP; winners exit on opposite B or 15:00; SL -1.5%
  C TREND_EXIT_WITH_PROFIT_LOCK — no +3% TP; lock at +1.5% net; 0.8pp giveback from peak exits

Shared: no continuation re-entry; next 1m open + adverse slip + TradeCostEngine costs.

Usage:
    python scripts/compare_macd_exit_abc_20d.py
    python scripts/compare_macd_exit_abc_20d.py --days 20
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.trading.macd_hynix_strategy import (  # noqa: E402
    DIR_DOWN,
    DIR_UP,
    ENTRY_INITIAL,
    EXIT_OPPOSITE,
    EXIT_SESSION,
    EXIT_SL,
    EXIT_TP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SYMBOL,
    SL_NET_PCT,
    TP_NET_PCT,
    WARMUP_1M_BARS,
    check_tp_sl,
    evaluate_macd_direction,
    net_pnl_pct_vs_entry,
    resample_completed_3m,
    tail_prior_day_1m,
)
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402
from scripts.compare_macd_vs_williams_early_20d import SYM_TAG, build_day_universe  # noqa: E402

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"
SIBLING_JSONS = (
    STATE / "macd_opening_abc_20d_compare.json",
    STATE / "macd_vs_williams_early_20d_compare.json",
    STATE / "macd_vs_williams_early_20d_partial.json",
)
OUT_JSON = STATE / "macd_exit_abc_20d_compare.json"
OUT_MD = STATE / "macd_exit_abc_20d_compare.md"

INITIAL_CASH = 10_000_000.0
ENTRY_CUTOFF_HM = (14, 55)
FORCE_HM = (15, 0)
ADVERSE_PCT = 0.05
DELAY_MIN = 1
MIN_DAYS = 20

# Variant C profit-lock parameters
PROFIT_LOCK_ACTIVATE_PCT = 1.5
PROFIT_LOCK_GIVEBACK_PP = 0.8
FIXED_TP_REF_PCT = 3.0

STRATEGIES = (
    "FIXED_TP",
    "OPPOSITE_SIGNAL_ONLY",
    "TREND_EXIT_WITH_PROFIT_LOCK",
)
STRAT_KEYS = ("A", "B", "C")

SCENARIOS = (
    ("baseline", DELAY_MIN, ADVERSE_PCT),
    ("plus_1m_delay", DELAY_MIN + 1, ADVERSE_PCT),
    ("plus_2m_slip10", DELAY_MIN + 2, 0.10),
)


@dataclass
class Trade:
    strategy: str
    day: str
    direction: str
    symbol: str
    signal_time: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    qty: int
    gross_pnl: float
    cost: float
    net_pnl: float
    exit_reason: str
    entry_kind: str
    hold_minutes: float = 0.0
    peak_unrealized_pct: float = 0.0
    peak_unrealized_net: float = 0.0
    capture_ratio: Optional[float] = None
    exceeded_3pct: bool = False
    extra_vs_fixed_tp_net: float = 0.0
    giveback_while_waiting_pct: float = 0.0


@dataclass
class DayResult:
    strategy: str
    day: str
    trades: list[Trade] = field(default_factory=list)
    net_pnl: float = 0.0
    ret_pct: float = 0.0


def _iso(day: str) -> str:
    day = str(day)
    if "-" in day:
        return day
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}"


def _load_sibling_dates() -> Optional[dict[str, Any]]:
    for path in SIBLING_JSONS:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        days = payload.get("days") or payload.get("dates") or payload.get("trading_days")
        sources = payload.get("day_sources") or payload.get("date_sources") or {}
        if days and len(days) >= MIN_DAYS:
            return {
                "days": [_iso(d) for d in days],
                "date_sources": {_iso(k): str(v) for k, v in sources.items()},
                "from": str(path),
            }
    return None


def _write_replay_cache(day: str, frames: dict[str, pd.DataFrame]) -> None:
    tag = day.replace("-", "")
    CACHE.mkdir(parents=True, exist_ok=True)
    for sym, df in frames.items():
        out = df.copy()
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        path = CACHE / f"replay_{tag}_{SYM_TAG[sym]}_1m.csv"
        out.to_csv(path, index=False)


def ensure_dataset(n_days: int) -> dict[str, Any]:
    notes: list[str] = []
    sibling = _load_sibling_dates()
    dates, date_sources, day_data = build_day_universe(n_days, refetch_naver=False)
    if sibling:
        notes.append(f"Reused sibling date list from {sibling['from']}.")
        preferred = sibling["days"]
        for d, src in sibling.get("date_sources", {}).items():
            date_sources[d] = src
        missing = [d for d in preferred if d not in day_data]
        if not missing and len(preferred) >= n_days:
            dates = preferred
            day_data = {d: day_data[d] for d in dates}
            date_sources = {d: date_sources.get(d, sibling["date_sources"].get(d, "unknown")) for d in dates}
        else:
            notes.append(f"Sibling dates incomplete (missing={missing}); using build_day_universe.")
    else:
        notes.append("No sibling JSON — using build_day_universe.")
    for day in dates:
        _write_replay_cache(day, day_data[day])
    return {"days": dates, "day_sources": date_sources, "day_data": day_data, "notes": notes}


def _session_slice(df: pd.DataFrame, day: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    start = datetime.strptime(f"{day} 09:00:00", "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(f"{day} 15:30:00", "%Y-%m-%d %H:%M:%S")
    return work[(work["datetime"] >= start) & (work["datetime"] <= end)].reset_index(drop=True)


def _fill(
    df: pd.DataFrame,
    signal_ts: datetime,
    side: str,
    delay_min: int,
    adverse_pct: float,
) -> tuple[Optional[datetime], Optional[float]]:
    target = signal_ts.replace(second=0, microsecond=0) + timedelta(minutes=max(1, delay_min))
    sub = df[df["datetime"] >= target]
    if sub.empty:
        return None, None
    row = sub.iloc[0]
    ts = pd.Timestamp(row["datetime"]).to_pydatetime()
    px = float(row["open"])
    if side == "BUY":
        px *= 1.0 + adverse_pct / 100.0
    else:
        px *= 1.0 - adverse_pct / 100.0
    return ts, float(px)


def _precompute_macd_events(
    hynix_df: pd.DataFrame,
    day: str,
    warmup_1m: pd.DataFrame,
) -> list[tuple[datetime, dict[str, Any]]]:
    bars3 = resample_completed_3m(
        hynix_df, now=datetime.strptime(f"{day} 15:30:00", "%Y-%m-%d %H:%M:%S")
    )
    events: list[tuple[datetime, dict[str, Any]]] = []
    last_dir: Optional[str] = None
    last_bar: Optional[str] = None
    for i in range(len(bars3)):
        bar_start = pd.Timestamp(bars3.iloc[i]["datetime"]).to_pydatetime()
        close_ts = bar_start + timedelta(minutes=3)
        today_1m = hynix_df[hynix_df["datetime"] <= close_ts]
        if not warmup_1m.empty:
            sub_1m = (
                pd.concat([warmup_1m, today_1m], ignore_index=True)
                .drop_duplicates("datetime")
                .sort_values("datetime")
            )
        else:
            sub_1m = today_1m
        ev = evaluate_macd_direction(
            sub_1m,
            now=close_ts,
            last_signal_direction=last_dir,
            last_signal_bar_ts=last_bar,
        )
        events.append((close_ts, ev))
        if ev.get("new_signal"):
            last_dir = ev["signal_direction"]
            last_bar = ev.get("bar_ts")
    return events


def _net_pct(symbol: str, entry: float, px: float, qty: int) -> float:
    return net_pnl_pct_vs_entry(symbol, entry, px, qty)


def _net_pnl(symbol: str, entry: float, px: float, qty: int, cost_engine: TradeCostEngine) -> float:
    bd = cost_engine.compute_net_pnl(
        symbol, entry, px, qty, buy_order_type="market", sell_order_type="market"
    )
    return float(bd["net_pnl"])


def _bar_extremes(row: pd.Series, symbol: str) -> tuple[float, float]:
    """Return (favorable_px, adverse_px) for peak / SL checks within a 1m bar."""
    hi = float(row["high"])
    lo = float(row["low"])
    if symbol == LONG_SYMBOL:
        return hi, lo
    return lo, hi


def replay_day(
    strategy: str,
    day: str,
    day_data: dict[str, pd.DataFrame],
    days: list[str],
    *,
    delay_min: int = DELAY_MIN,
    adverse_pct: float = ADVERSE_PCT,
) -> DayResult:
    cost_engine = TradeCostEngine()
    hynix_df = day_data[day][SIGNAL_SYMBOL]
    long_df = day_data[day][LONG_SYMBOL]
    inv_df = day_data[day][INVERSE_SYMBOL]

    idx = days.index(day)
    warmup_1m = pd.DataFrame()
    if idx > 0:
        prev_day = days[idx - 1]
        prev = day_data.get(prev_day, {}).get(SIGNAL_SYMBOL)
        warmup_1m = tail_prior_day_1m(
            _session_slice(prev, prev_day) if prev is not None else pd.DataFrame(),
            min_bars=WARMUP_1M_BARS,
        )

    macd_events = _precompute_macd_events(hynix_df, day, warmup_1m)
    event_by_ts = {ts: ev for ts, ev in macd_events}

    minutes = sorted(
        {
            pd.Timestamp(t).to_pydatetime()
            for t in hynix_df["datetime"].tolist()
            if pd.Timestamp(t).hour < 15 or (pd.Timestamp(t).hour == 15 and pd.Timestamp(t).minute == 0)
        }
    )
    day_force = datetime.strptime(f"{day} 15:00:00", "%Y-%m-%d %H:%M:%S")

    position: Optional[dict[str, Any]] = None
    realized = 0.0
    trades: list[Trade] = []

    def equity() -> float:
        return INITIAL_CASH + realized

    def close_position(reason: str, signal_ts: datetime, track: dict[str, Any]) -> None:
        nonlocal position, realized
        if position is None:
            return
        etf_df = long_df if position["symbol"] == LONG_SYMBOL else inv_df
        xts, xpx = _fill(etf_df, signal_ts, "SELL", delay_min, adverse_pct)
        if xpx is None:
            xpx = float(position["entry_price"])
            xts = signal_ts
        bd = cost_engine.compute_net_pnl(
            position["symbol"], position["entry_price"], xpx, position["qty"],
            buy_order_type="market", sell_order_type="market",
        )
        entry_dt = datetime.fromisoformat(str(position["entry_time"]).replace(" ", "T"))
        exit_dt = xts if isinstance(xts, datetime) else signal_ts
        hold_min = max(0.0, (exit_dt - entry_dt).total_seconds() / 60.0)
        peak_pct = float(track.get("peak_pct") or 0.0)
        peak_net = float(track.get("peak_net") or 0.0)
        exit_pct = _net_pct(position["symbol"], position["entry_price"], xpx, position["qty"])
        # Capture = realized / peak unrealized; 0 if never in profit or peak ≤ 0;
        # clamp to [0, 1.5] so tiny-peak losers do not dominate averages.
        if peak_net > 0:
            raw_cap = float(bd["net_pnl"]) / peak_net
            capture = round(max(0.0, min(1.5, raw_cap)), 4)
        else:
            capture = None
        exceeded = peak_pct >= FIXED_TP_REF_PCT
        notional = position["entry_price"] * position["qty"]
        # Extra vs fixed +3% TP: only meaningful when peak ≥ 3% (what leaving TP on table / capturing beyond)
        fixed_tp_net = notional * FIXED_TP_REF_PCT / 100.0 if exceeded else 0.0
        extra = round(bd["net_pnl"] - fixed_tp_net, 2) if exceeded else 0.0
        # Giveback while waiting for opposite / session / lock exit (not SL/TP)
        if peak_pct > 0 and reason in (
            EXIT_OPPOSITE, EXIT_SESSION, "EOD_FLAT", "PROFIT_LOCK_GIVEBACK"
        ):
            giveback = round(max(0.0, peak_pct - exit_pct), 4)
        else:
            giveback = 0.0
        trades.append(Trade(
            strategy=strategy,
            day=day,
            direction=position["direction"],
            symbol=position["symbol"],
            signal_time=position["signal_time"],
            entry_time=str(position["entry_time"]),
            entry_price=float(position["entry_price"]),
            exit_time=str(xts),
            exit_price=float(xpx),
            qty=int(position["qty"]),
            gross_pnl=float(bd["gross_pnl"]),
            cost=float(bd["total_cost"]),
            net_pnl=float(bd["net_pnl"]),
            exit_reason=reason,
            entry_kind=position.get("entry_kind", ENTRY_INITIAL),
            hold_minutes=round(hold_min, 2),
            peak_unrealized_pct=round(peak_pct, 4),
            peak_unrealized_net=round(peak_net, 2),
            capture_ratio=capture,
            exceeded_3pct=exceeded,
            extra_vs_fixed_tp_net=extra,
            giveback_while_waiting_pct=giveback,
        ))
        realized += float(bd["net_pnl"])
        position = None

    def open_position(direction: str, signal_ts: datetime) -> None:
        nonlocal position
        target = LONG_SYMBOL if direction == DIR_UP else INVERSE_SYMBOL
        etf_df = long_df if target == LONG_SYMBOL else inv_df
        ets, epx = _fill(etf_df, signal_ts, "BUY", delay_min, adverse_pct)
        if epx is None or epx <= 0:
            return
        qty = int(equity() // epx)
        if qty < 1:
            return
        position = {
            "symbol": target,
            "direction": direction,
            "qty": qty,
            "entry_price": epx,
            "entry_time": str(ets),
            "signal_time": signal_ts.isoformat(),
            "entry_kind": ENTRY_INITIAL,
        }

    track: dict[str, Any] = {"peak_pct": 0.0, "peak_net": 0.0, "lock_active": False}

    for ts in minutes:
        hm = (ts.hour, ts.minute)

        if hm >= FORCE_HM and position is not None:
            close_position(EXIT_SESSION, day_force, track)
            track = {"peak_pct": 0.0, "peak_net": 0.0, "lock_active": False}
            continue

        if position is not None:
            etf_df = long_df if position["symbol"] == LONG_SYMBOL else inv_df
            sub = etf_df[etf_df["datetime"] == ts]
            if sub.empty:
                sub = etf_df[etf_df["datetime"] <= ts]
            if not sub.empty:
                row = sub.iloc[-1]
                fav_px, adv_px = _bar_extremes(row, position["symbol"])
                close_px = float(row["close"])

                # Peak tracking (favorable extreme — 5s proxy within 1m bar)
                fav_pct = _net_pct(position["symbol"], position["entry_price"], fav_px, position["qty"])
                if fav_pct > track["peak_pct"]:
                    track["peak_pct"] = fav_pct
                    track["peak_net"] = _net_pnl(
                        position["symbol"], position["entry_price"], fav_px, position["qty"], cost_engine
                    )

                if strategy == "TREND_EXIT_WITH_PROFIT_LOCK":
                    close_pct = _net_pct(position["symbol"], position["entry_price"], close_px, position["qty"])
                    if close_pct >= PROFIT_LOCK_ACTIVATE_PCT:
                        track["lock_active"] = True
                    if track["lock_active"] and track["peak_pct"] - close_pct >= PROFIT_LOCK_GIVEBACK_PP:
                        close_position("PROFIT_LOCK_GIVEBACK", ts, track)
                        track = {"peak_pct": 0.0, "peak_net": 0.0, "lock_active": False}
                        continue

                # SL always; TP only for variant A
                sl_hit = check_tp_sl(
                    position["symbol"], position["entry_price"], adv_px, position["qty"],
                    tp_pct=999.0, sl_pct=SL_NET_PCT,
                )
                if sl_hit == EXIT_SL:
                    close_position(EXIT_SL, ts, track)
                    track = {"peak_pct": 0.0, "peak_net": 0.0, "lock_active": False}
                    continue

                if strategy == "FIXED_TP":
                    tp_hit = check_tp_sl(
                        position["symbol"], position["entry_price"], fav_px, position["qty"],
                        tp_pct=TP_NET_PCT, sl_pct=SL_NET_PCT - 1.0,
                    )
                    if tp_hit == EXIT_TP:
                        close_position(EXIT_TP, ts, track)
                        track = {"peak_pct": 0.0, "peak_net": 0.0, "lock_active": False}
                        continue

        if ts in event_by_ts:
            ev = event_by_ts[ts]
            if ev.get("new_signal"):
                direction = ev["signal_direction"]
                if hm >= ENTRY_CUTOFF_HM:
                    if position is not None and position["direction"] != direction:
                        close_position(EXIT_OPPOSITE, ts, track)
                        track = {"peak_pct": 0.0, "peak_net": 0.0, "lock_active": False}
                    continue

                target = LONG_SYMBOL if direction == DIR_UP else INVERSE_SYMBOL
                if position is not None and position["symbol"] != target:
                    close_position(EXIT_OPPOSITE, ts, track)
                    track = {"peak_pct": 0.0, "peak_net": 0.0, "lock_active": False}
                    open_position(direction, ts)
                elif position is None:
                    open_position(direction, ts)
                    track = {"peak_pct": 0.0, "peak_net": 0.0, "lock_active": False}

    if position is not None:
        close_position("EOD_FLAT", day_force, track)

    nets = [t.net_pnl for t in trades]
    return DayResult(
        strategy=strategy,
        day=day,
        trades=trades,
        net_pnl=round(sum(nets), 2),
        ret_pct=round(sum(nets) / INITIAL_CASH * 100.0, 4),
    )


def _metrics_from_trades(trades: list[Trade], cash0: float = INITIAL_CASH) -> dict[str, float]:
    if not trades:
        return {"net": 0.0, "ret": 0.0, "wr": 0.0, "pf": 0.0, "mdd": 0.0}
    nets = [t.net_pnl for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    equity = cash0
    peak = cash0
    mdd = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0 if peak else 0.0
        mdd = max(mdd, dd)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    return {
        "net": round(sum(nets), 2),
        "ret": round(sum(nets) / cash0 * 100.0, 3),
        "wr": round(len(wins) / len(nets) * 100.0, 2),
        "pf": round(pf, 3),
        "mdd": round(mdd, 3),
    }


def summarize(day_results: list[DayResult], strategy: str) -> dict[str, Any]:
    trades = [t for d in day_results for t in d.trades]
    m = _metrics_from_trades(trades)
    rets = [d.ret_pct for d in day_results]
    holds = [t.hold_minutes for t in trades if t.hold_minutes > 0]
    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl < 0]
    captures = [t.capture_ratio for t in trades if t.capture_ratio is not None]
    exceeded = [t for t in trades if t.exceeded_3pct]
    givebacks = [t.giveback_while_waiting_pct for t in trades if t.giveback_while_waiting_pct > 0]

    exit_counts: dict[str, int] = {}
    for t in trades:
        exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1

    return {
        "strategy": strategy,
        "cum_net_pnl": m["net"],
        "cum_ret_pct": m["ret"],
        "mean_daily_ret_pct": round(statistics.mean(rets), 4) if rets else 0.0,
        "median_daily_ret_pct": round(statistics.median(rets), 4) if rets else 0.0,
        "profit_factor": m["pf"],
        "mdd_pct": m["mdd"],
        "win_rate_pct": m["wr"],
        "round_trips": len(trades),
        "avg_hold_minutes": round(statistics.mean(holds), 2) if holds else 0.0,
        "max_hold_minutes": round(max(holds), 2) if holds else 0.0,
        "avg_win_net": round(statistics.mean(wins), 2) if wins else 0.0,
        "avg_loss_net": round(statistics.mean(losses), 2) if losses else 0.0,
        "avg_capture_ratio": round(statistics.mean(captures), 4) if captures else None,
        "median_capture_ratio": round(statistics.median(captures), 4) if captures else None,
        "trades_exceeded_3pct": len(exceeded),
        "extra_profit_on_exceeded_3pct": round(sum(t.extra_vs_fixed_tp_net for t in exceeded), 2),
        "avg_giveback_while_waiting_pct": round(statistics.mean(givebacks), 4) if givebacks else 0.0,
        "total_giveback_while_waiting_pct": round(sum(givebacks), 4),
        "exit_counts": exit_counts,
        "daily": [{"day": d.day, "net_pnl": d.net_pnl, "ret_pct": d.ret_pct} for d in day_results],
        "trades": [asdict(t) for t in trades],
    }


def evaluate_adoption(a: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    mdd_delta = float(c["mdd_pct"]) - float(a["mdd_pct"])
    net_better = float(c["cum_net_pnl"]) > float(a["cum_net_pnl"])
    pf_ok = float(c["profit_factor"]) >= float(a["profit_factor"])
    mdd_ok = mdd_delta <= 0.5
    gates = {
        "net_c_gt_a": {
            "pass": net_better,
            "detail": f"C net {c['cum_net_pnl']:,.0f} > A net {a['cum_net_pnl']:,.0f}",
        },
        "pf_c_not_worse": {
            "pass": pf_ok,
            "detail": f"C PF {c['profit_factor']} ≥ A PF {a['profit_factor']}",
        },
        "mdd_delta_le_0_5pp": {
            "pass": mdd_ok,
            "detail": f"MDD Δ={mdd_delta:.3f}pp ≤ 0.5",
        },
    }
    all_pass = all(g["pass"] for g in gates.values())
    if all_pass:
        verdict = "ADOPT_C"
    elif net_better and (not pf_ok or not mdd_ok):
        verdict = "NO_CLEAR_WINNER"
    else:
        verdict = "DO_NOT_ADOPT"
    return {
        "gates": gates,
        "all_pass": all_pass,
        "verdict": verdict,
        "mdd_delta_pp": round(mdd_delta, 3),
        "live_enable": False,
    }


def render_md(report: dict[str, Any]) -> str:
    a, b, c = report["A"], report["B"], report["C"]
    adopt = report["adoption"]
    lines = [
        "# MACD Signed-B Exit A/B/C (≥20d)",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Days ({len(report['days'])}): {', '.join(report['days'])}",
        f"- Entry: signed B NEW_TURN_ONLY (`evaluate_macd_direction`)",
        f"- Fill: next 1m open + {report['fill_model']['adverse_pct']}% adverse + costs",
        "",
        "## Profit-lock proxy (variant C)",
        "",
        report["profit_lock_proxy_note"],
        "",
        "## Summary",
        "",
        "| Variant | Cum Net | Mean daily% | Median daily% | PF | MDD% | WR% | Trades | Avg hold (m) | Max hold (m) |",
        "|---------|---------|-------------|---------------|----|------|-----|--------|--------------|--------------|",
        f"| A FIXED_TP | {a['cum_net_pnl']:,.0f} | {a['mean_daily_ret_pct']} | {a['median_daily_ret_pct']} | {a['profit_factor']} | {a['mdd_pct']} | {a['win_rate_pct']} | {a['round_trips']} | {a['avg_hold_minutes']} | {a['max_hold_minutes']} |",
        f"| B OPPOSITE_ONLY | {b['cum_net_pnl']:,.0f} | {b['mean_daily_ret_pct']} | {b['median_daily_ret_pct']} | {b['profit_factor']} | {b['mdd_pct']} | {b['win_rate_pct']} | {b['round_trips']} | {b['avg_hold_minutes']} | {b['max_hold_minutes']} |",
        f"| C PROFIT_LOCK | {c['cum_net_pnl']:,.0f} | {c['mean_daily_ret_pct']} | {c['median_daily_ret_pct']} | {c['profit_factor']} | {c['mdd_pct']} | {c['win_rate_pct']} | {c['round_trips']} | {c['avg_hold_minutes']} | {c['max_hold_minutes']} |",
        "",
        "## Win / loss & capture",
        "",
        "| Variant | Avg win | Avg loss | Avg capture | Med capture | >3% trades | Extra vs +3% TP | Avg giveback wait |",
        "|---------|---------|----------|-------------|-------------|------------|-----------------|-------------------|",
    ]
    for key, label in (("A", "FIXED_TP"), ("B", "OPPOSITE_ONLY"), ("C", "PROFIT_LOCK")):
        s = report[key]
        lines.append(
            f"| {label} | {s['avg_win_net']:,.0f} | {s['avg_loss_net']:,.0f} | "
            f"{s.get('avg_capture_ratio')} | {s.get('median_capture_ratio')} | "
            f"{s['trades_exceeded_3pct']} | {s['extra_profit_on_exceeded_3pct']:,.0f} | "
            f"{s['avg_giveback_while_waiting_pct']} |"
        )
    lines += [
        "",
        "## Exit reason counts",
        "",
        f"- A: {a['exit_counts']}",
        f"- B: {b['exit_counts']}",
        f"- C: {c['exit_counts']}",
        "",
        "## Adoption gates (C vs A)",
        "",
    ]
    for name, g in adopt["gates"].items():
        lines.append(f"- **{name}**: {'PASS' if g['pass'] else 'FAIL'} — {g['detail']}")
    lines += [
        "",
        f"**Verdict: `{adopt['verdict']}`** (live enable: {adopt['live_enable']})",
        "",
        "## Stress",
        "",
        "| Scenario | A Net | A PF | A MDD | B Net | C Net |",
        "|----------|-------|------|-------|-------|-------|",
    ]
    for scen, vals in report.get("stress", {}).items():
        lines.append(
            f"| {scen} | {vals['A']['net']:,.0f} | {vals['A']['pf']} | {vals['A']['mdd']} | "
            f"{vals['B']['net']:,.0f} | {vals['C']['net']:,.0f} |"
        )
    lines += ["", f"- JSON: `{OUT_JSON.as_posix()}`", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=MIN_DAYS)
    args = parser.parse_args()

    ds = ensure_dataset(args.days)
    dates = ds["days"]
    day_data = ds["day_data"]
    print(f"Replaying {len(dates)} days …")

    results: dict[str, dict[str, Any]] = {}
    for key, strat in zip(STRAT_KEYS, STRATEGIES):
        day_results: list[DayResult] = []
        for day in dates:
            day_results.append(
                replay_day(strat, day, day_data, dates, delay_min=DELAY_MIN, adverse_pct=ADVERSE_PCT)
            )
        results[key] = summarize(day_results, strat)
        print(
            f"  {key} {strat}: Net={results[key]['cum_net_pnl']:,.0f} "
            f"PF={results[key]['profit_factor']} MDD={results[key]['mdd_pct']}"
        )

    stress: dict[str, dict[str, dict[str, float]]] = {}
    for scen_name, delay, adverse in SCENARIOS:
        stress[scen_name] = {}
        for key, strat in zip(STRAT_KEYS, STRATEGIES):
            trades: list[Trade] = []
            for day in dates:
                dr = replay_day(strat, day, day_data, dates, delay_min=delay, adverse_pct=adverse)
                trades.extend(dr.trades)
            stress[scen_name][key] = _metrics_from_trades(trades)

    adoption = evaluate_adoption(results["A"], results["C"])
    proxy_note = (
        "Peak net PnL% uses intra-bar favorable extreme (1m high for long ETF, low for inverse) "
        "at each minute — approximating 5s polling within 1m resolution. "
        "Profit-lock giveback is evaluated on bar close vs running peak; "
        f"lock activates at +{PROFIT_LOCK_ACTIVATE_PCT}% net, exits when giveback ≥ {PROFIT_LOCK_GIVEBACK_PP}pp."
    )
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": dates,
        "day_sources": ds["day_sources"],
        "notes": ds["notes"],
        "entry_model": "signed_B_NEW_TURN_ONLY",
        "shared_rules": {
            "sl_net_pct": SL_NET_PCT,
            "tp_net_pct_A_only": TP_NET_PCT,
            "opposite_switch": True,
            "force_flat": "15:00",
            "continuation_reentry": False,
        },
        "exit_variants": {
            "A": "FIXED_TP",
            "B": "OPPOSITE_SIGNAL_ONLY",
            "C": "TREND_EXIT_WITH_PROFIT_LOCK",
        },
        "profit_lock_C": {
            "activate_net_pct": PROFIT_LOCK_ACTIVATE_PCT,
            "giveback_pp": PROFIT_LOCK_GIVEBACK_PP,
        },
        "profit_lock_proxy_note": proxy_note,
        "fill_model": {
            "delay_min": DELAY_MIN,
            "adverse_pct": ADVERSE_PCT,
        },
        "A": results["A"],
        "B": results["B"],
        "C": results["C"],
        "adoption": adoption,
        "stress": stress,
    }
    STATE.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(render_md(report), encoding="utf-8")
    print(f"\nAdoption: {adoption['verdict']}")
    print(f"Wrote {OUT_JSON} and {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
