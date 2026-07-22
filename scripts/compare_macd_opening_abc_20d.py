"""Compare MACD Strategy B opening variants (≥20 trading days).

A NEW_TURN_ONLY              — current signed-B new-turn only (baseline)
B FIRST_COMPLETED_BAR_ENTRY  — first regular 3m bar (09:03) B confirm → full entry
C IMMEDIATE_50_THEN_CONFIRM  — 09:00 opening probe 50% → 09:03 scale or flatten

Fills: immediate = first 1m open + 0.05% adverse; scale = next 1m open; all costs.
Live enable gated on adoption: Net(C)>Net(A), PF(C)≥PF(A), MDD Δ≤0.5pp.
"""
from __future__ import annotations

import argparse
import json
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
    ENTRY_OPEN_IMMEDIATE,
    ENTRY_OPEN_SCALE,
    EXIT_OPPOSITE,
    EXIT_OPEN_UNCONFIRMED,
    EXIT_SL,
    EXIT_TP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    OPEN_IMMEDIATE_BUDGET_FRACTION,
    OPEN_IMMEDIATE_MIN_RETURN_PCT,
    OPENING_PROBE_ENABLED,
    SIGNAL_SYMBOL,
    SL_NET_PCT,
    TP_NET_PCT,
    WARMUP_1M_BARS,
    check_tp_sl,
    compute_warmup_macd,
    evaluate_macd_direction,
    evaluate_opening_probe,
    macd_components,
    opening_probe_b_confirms,
    resample_completed_3m,
    tail_prior_day_1m,
)
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402
from scripts.compare_macd_vs_williams_early_20d import build_day_universe  # noqa: E402

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"
OUT_JSON = STATE / "macd_opening_abc_20d_compare.json"
OUT_MD = STATE / "macd_opening_abc_20d_compare.md"

INITIAL_CASH = 10_000_000.0
ENTRY_CUTOFF_HM = (14, 55)
FORCE_HM = (15, 0)
ADVERSE_PCT = 0.05
DELAY_MIN = 1
MIN_DAYS = 20
OPEN_WINDOW_TIMES = [
    datetime.strptime(f"2000-01-01 09:00:{s:02d}", "%Y-%m-%d %H:%M:%S").time()
    for s in (5, 10, 15)
]

STRATEGIES = (
    "NEW_TURN_ONLY",
    "FIRST_COMPLETED_BAR_ENTRY",
    "IMMEDIATE_50_THEN_CONFIRM",
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
    size_pct: float = 1.0


@dataclass
class DayStats:
    strategy: str
    day: str
    first_entry_time: Optional[str] = None
    open_probe_fired: bool = False
    open_probe_success: bool = False
    unconfirmed_exit_pnl: float = 0.0
    gap_reversal_loss: float = 0.0
    first_30m_pnl: float = 0.0
    trades: list[Trade] = field(default_factory=list)


def _fill(
    df: pd.DataFrame,
    signal_ts: datetime,
    side: str,
    delay_min: int = DELAY_MIN,
    adverse_pct: float = ADVERSE_PCT,
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


def _metrics(trades: list[Trade], cash0: float = INITIAL_CASH) -> dict[str, float]:
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


def _session_slice(df: pd.DataFrame, day: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    start = datetime.strptime(f"{day} 09:00:00", "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(f"{day} 15:30:00", "%Y-%m-%d %H:%M:%S")
    return work[(work["datetime"] >= start) & (work["datetime"] <= end)].reset_index(drop=True)


def _warmup_hist(day_data: dict[str, pd.DataFrame], day: str, days: list[str]) -> dict[str, Any]:
    idx = days.index(day)
    if idx <= 0:
        return {"ok": False, "reason": "NO_PRIOR_DAY"}
    prev_day = days[idx - 1]
    prev = day_data.get(prev_day, {}).get(SIGNAL_SYMBOL)
    prev_sess = _session_slice(prev, prev_day) if prev is not None else pd.DataFrame()
    warmup_1m = tail_prior_day_1m(prev_sess, min_bars=WARMUP_1M_BARS)
    return compute_warmup_macd(warmup_1m, now=None)


def _simulate_open_probe_at_900(
    warm: dict[str, Any],
    hynix_df: pd.DataFrame,
    day: str,
    long_df: pd.DataFrame,
    inv_df: pd.DataFrame,
) -> Optional[dict[str, Any]]:
    if not warm.get("ok"):
        return None
    sess = _session_slice(hynix_df, day)
    if sess.empty:
        return None
    day_open = float(sess.iloc[0]["open"])
    # Walk 09:00:05/10/15 — use close at that minute as proxy (no lookahead beyond bar)
    samples: list[tuple[Any, float]] = []
    for t in OPEN_WINDOW_TIMES:
        ts = datetime.strptime(f"{day} 09:00:00", "%Y-%m-%d %H:%M:%S").replace(
            hour=9, minute=0, second=t.second
        )
        sub = sess[sess["datetime"] <= ts]
        if sub.empty:
            continue
        px = float(sub.iloc[-1]["close"])
        samples.append((ts, px))
    if len(samples) < 2:
        return None
    last_ts, last_px = samples[-1]
    probe = evaluate_opening_probe(
        warm,
        hynix_price=last_px,
        day_open_price=day_open,
        long_quote={"ok": True, "price": float(long_df.iloc[0]["open"]) if not long_df.empty else 1.0},
        inverse_quote={"ok": True, "price": float(inv_df.iloc[0]["open"]) if not inv_df.empty else 1.0},
        price_samples_5s=samples,
        now=last_ts,
    )
    if probe.get("ok_to_trade"):
        return {"probe": probe, "signal_ts": last_ts, "hynix_px": last_px, "day_open": day_open}
    return None


def replay_day(
    strategy: str,
    day: str,
    day_data: dict[str, pd.DataFrame],
    days: list[str],
    *,
    delay_min: int = DELAY_MIN,
) -> DayStats:
    hynix_df = day_data[day][SIGNAL_SYMBOL]
    long_df = day_data[day][LONG_SYMBOL]
    inv_df = day_data[day][INVERSE_SYMBOL]
    cost_engine = TradeCostEngine()
    stats = DayStats(strategy=strategy, day=day)

    bars3 = resample_completed_3m(hynix_df, now=datetime.strptime(f"{day} 15:30:00", "%Y-%m-%d %H:%M:%S"))
    if bars3.empty:
        return stats

    closes = pd.to_numeric(bars3["close"], errors="coerce")
    comps = macd_components(closes)
    hist = comps.get("hist")
    if hist is None:
        return stats

    events: list[tuple[datetime, dict[str, Any]]] = []
    last_dir: Optional[str] = None
    last_bar: Optional[str] = None
    first_bar_done = False

    warm = _warmup_hist(day_data, day, days)
    warmup_1m = pd.DataFrame()
    idx = days.index(day)
    if idx > 0:
        prev_day = days[idx - 1]
        prev = day_data.get(prev_day, {}).get(SIGNAL_SYMBOL)
        warmup_1m = tail_prior_day_1m(_session_slice(prev, prev_day) if prev is not None else pd.DataFrame())

    for i in range(len(bars3)):
        bar_start = pd.Timestamp(bars3.iloc[i]["datetime"]).to_pydatetime()
        close_ts = bar_start + timedelta(minutes=3)
        today_1m = hynix_df[hynix_df["datetime"] <= close_ts]
        if not warmup_1m.empty:
            sub_1m = pd.concat([warmup_1m, today_1m], ignore_index=True).drop_duplicates("datetime").sort_values("datetime")
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
        if not first_bar_done and close_ts.hour == 9 and close_ts.minute == 3:
            first_bar_done = True
            first_bar_ev = ev

    position: Optional[dict[str, Any]] = None
    realized = 0.0
    partial_await_confirm = False
    probe_dir: Optional[str] = None

    # Strategy C: 09:00 immediate half
    if strategy == "IMMEDIATE_50_THEN_CONFIRM":
        hit = _simulate_open_probe_at_900(warm, hynix_df, day, long_df, inv_df)
        if hit:
            stats.open_probe_fired = True
            probe = hit["probe"]
            probe_dir = probe["direction"]
            target = LONG_SYMBOL if probe_dir == DIR_UP else INVERSE_SYMBOL
            etf_df = long_df if target == LONG_SYMBOL else inv_df
            sig_ts = hit["signal_ts"]
            ets, epx = _fill(etf_df, sig_ts, "BUY", delay_min=delay_min)
            if epx and epx > 0:
                equity = INITIAL_CASH + realized
                half_budget = equity * OPEN_IMMEDIATE_BUDGET_FRACTION
                qty = int(half_budget // epx)
                if qty >= 1:
                    stats.open_probe_success = True
                    stats.first_entry_time = str(ets)
                    position = {
                        "symbol": target,
                        "direction": probe_dir,
                        "qty": qty,
                        "entry_price": epx,
                        "entry_time": str(ets),
                        "signal_time": sig_ts.isoformat(),
                        "entry_kind": ENTRY_OPEN_IMMEDIATE,
                        "size_pct": OPEN_IMMEDIATE_BUDGET_FRACTION,
                    }
                    partial_await_confirm = True
                    last_dir = probe_dir

    day_force = datetime.strptime(f"{day} 15:00:00", "%Y-%m-%d %H:%M:%S")

    for close_ts, ev in events:
        hm = (close_ts.hour, close_ts.minute)
        if hm >= FORCE_HM:
            break

        # TP/SL on open partial/full
        if position is not None:
            etf_df = long_df if position["symbol"] == LONG_SYMBOL else inv_df
            sub = etf_df[etf_df["datetime"] <= close_ts]
            if not sub.empty:
                cur = float(sub.iloc[-1]["close"])
                exit_hit = check_tp_sl(
                    position["symbol"], position["entry_price"], cur, position["qty"]
                )
                if exit_hit:
                    xts, xpx = _fill(etf_df, close_ts, "SELL", delay_min=delay_min)
                    if xpx is None:
                        xpx = cur
                        xts = close_ts
                    bd = cost_engine.compute_net_pnl(
                        position["symbol"], position["entry_price"], xpx, position["qty"],
                        buy_order_type="market", sell_order_type="market",
                    )
                    stats.trades.append(Trade(
                        strategy=strategy, day=day, direction=position["direction"],
                        symbol=position["symbol"], signal_time=position["signal_time"],
                        entry_time=position["entry_time"], entry_price=position["entry_price"],
                        exit_time=str(xts), exit_price=xpx, qty=position["qty"],
                        gross_pnl=bd["gross_pnl"], cost=bd["total_cost"], net_pnl=bd["net_pnl"],
                        exit_reason=exit_hit, entry_kind=position["entry_kind"],
                        size_pct=position.get("size_pct", 1.0),
                    ))
                    realized += bd["net_pnl"]
                    position = None
                    partial_await_confirm = False

        # 09:03 confirm for C
        if (
            strategy == "IMMEDIATE_50_THEN_CONFIRM"
            and partial_await_confirm
            and position is not None
            and close_ts.hour == 9
            and close_ts.minute == 3
        ):
            if probe_dir and opening_probe_b_confirms(ev, probe_dir):
                target = position["symbol"]
                etf_df = long_df if target == LONG_SYMBOL else inv_df
                equity = INITIAL_CASH + realized
                ets, epx = _fill(etf_df, close_ts, "BUY", delay_min=delay_min)
                if epx and epx > 0:
                    add_budget = equity * OPEN_IMMEDIATE_BUDGET_FRACTION
                    add_qty = int(add_budget // epx)
                    if add_qty >= 1:
                        old_q = position["qty"]
                        old_p = position["entry_price"]
                        new_q = old_q + add_qty
                        avg = (old_p * old_q + epx * add_qty) / new_q
                        position["qty"] = new_q
                        position["entry_price"] = avg
                        position["entry_kind"] = ENTRY_OPEN_SCALE
                        position["size_pct"] = 1.0
            else:
                etf_df = long_df if position["symbol"] == LONG_SYMBOL else inv_df
                xts, xpx = _fill(etf_df, close_ts, "SELL", delay_min=delay_min)
                if xpx is None:
                    xpx = position["entry_price"]
                    xts = close_ts
                bd = cost_engine.compute_net_pnl(
                    position["symbol"], position["entry_price"], xpx, position["qty"],
                    buy_order_type="market", sell_order_type="market",
                )
                stats.trades.append(Trade(
                    strategy=strategy, day=day, direction=position["direction"],
                    symbol=position["symbol"], signal_time=position["signal_time"],
                    entry_time=position["entry_time"], entry_price=position["entry_price"],
                    exit_time=str(xts), exit_price=xpx, qty=position["qty"],
                    gross_pnl=bd["gross_pnl"], cost=bd["total_cost"], net_pnl=bd["net_pnl"],
                    exit_reason=EXIT_OPEN_UNCONFIRMED, entry_kind=position["entry_kind"],
                    size_pct=position.get("size_pct", 0.5),
                ))
                stats.unconfirmed_exit_pnl += bd["net_pnl"]
                realized += bd["net_pnl"]
                position = None
            partial_await_confirm = False

        direction: Optional[str] = None
        if strategy == "FIRST_COMPLETED_BAR_ENTRY":
            if close_ts.hour == 9 and close_ts.minute == 3 and position is None:
                display = ev.get("display_direction")
                if display in (DIR_UP, DIR_DOWN) and opening_probe_b_confirms(ev, display):
                    direction = display
        elif strategy == "IMMEDIATE_50_THEN_CONFIRM":
            if ev.get("new_signal") and not (close_ts.hour == 9 and close_ts.minute == 3):
                direction = ev["signal_direction"]
        elif strategy == "NEW_TURN_ONLY":
            if ev.get("new_signal"):
                direction = ev["signal_direction"]

        if direction is None:
            continue
        if hm >= ENTRY_CUTOFF_HM:
            continue

        target = LONG_SYMBOL if direction == DIR_UP else INVERSE_SYMBOL
        etf_df = long_df if target == LONG_SYMBOL else inv_df

        if position is not None and position["symbol"] != target:
            xts, xpx = _fill(
                long_df if position["symbol"] == LONG_SYMBOL else inv_df,
                close_ts, "SELL", delay_min=delay_min,
            )
            if xpx is None:
                xpx = position["entry_price"]
                xts = close_ts
            bd = cost_engine.compute_net_pnl(
                position["symbol"], position["entry_price"], xpx, position["qty"],
                buy_order_type="market", sell_order_type="market",
            )
            stats.trades.append(Trade(
                strategy=strategy, day=day, direction=position["direction"],
                symbol=position["symbol"], signal_time=position["signal_time"],
                entry_time=position["entry_time"], entry_price=position["entry_price"],
                exit_time=str(xts), exit_price=xpx, qty=position["qty"],
                gross_pnl=bd["gross_pnl"], cost=bd["total_cost"], net_pnl=bd["net_pnl"],
                exit_reason=EXIT_OPPOSITE, entry_kind=position["entry_kind"],
                size_pct=position.get("size_pct", 1.0),
            ))
            realized += bd["net_pnl"]
            position = None

        if position is not None and position["symbol"] == target:
            continue

        equity = INITIAL_CASH + realized
        ets, epx = _fill(etf_df, close_ts, "BUY", delay_min=delay_min)
        if epx is None or epx <= 0:
            continue
        qty = int(equity // epx)
        if qty < 1:
            continue
        if stats.first_entry_time is None:
            stats.first_entry_time = str(ets)
        position = {
            "symbol": target,
            "direction": direction,
            "qty": qty,
            "entry_price": epx,
            "entry_time": str(ets),
            "signal_time": close_ts.isoformat(),
            "entry_kind": ENTRY_INITIAL,
            "size_pct": 1.0,
        }
        last_dir = direction
        last_bar = ev.get("bar_ts")

    if position is not None:
        etf_df = long_df if position["symbol"] == LONG_SYMBOL else inv_df
        xts, xpx = _fill(etf_df, day_force, "SELL", delay_min=delay_min)
        if xpx is None:
            xpx = position["entry_price"]
            xts = day_force
        bd = cost_engine.compute_net_pnl(
            position["symbol"], position["entry_price"], xpx, position["qty"],
            buy_order_type="market", sell_order_type="market",
        )
        stats.trades.append(Trade(
            strategy=strategy, day=day, direction=position["direction"],
            symbol=position["symbol"], signal_time=position["signal_time"],
            entry_time=position["entry_time"], entry_price=position["entry_price"],
            exit_time=str(xts), exit_price=xpx, qty=position["qty"],
            gross_pnl=bd["gross_pnl"], cost=bd["total_cost"], net_pnl=bd["net_pnl"],
            exit_reason="EOD_FLAT", entry_kind=position["entry_kind"],
            size_pct=position.get("size_pct", 1.0),
        ))

    # first 30m PnL
    if stats.trades:
        t0 = datetime.strptime(f"{day} 09:00:00", "%Y-%m-%d %H:%M:%S")
        t30 = t0 + timedelta(minutes=30)
        stats.first_30m_pnl = round(
            sum(
                t.net_pnl for t in stats.trades
                if t.entry_time and datetime.fromisoformat(str(t.entry_time)) <= t30
            ),
            2,
        )
    # gap reversal: probe UP but day net negative on first trade
    if stats.open_probe_fired and stats.trades:
        first = stats.trades[0]
        if first.entry_kind == ENTRY_OPEN_IMMEDIATE and first.net_pnl < 0:
            stats.gap_reversal_loss += first.net_pnl

    return stats


def summarize(all_trades: list[Trade], day_stats: list[DayStats]) -> dict[str, Any]:
    m = _metrics(all_trades)
    entries = [ds.first_entry_time for ds in day_stats if ds.first_entry_time]
    avg_entry = None
    if entries:
        secs = []
        for e in entries:
            try:
                dt = datetime.fromisoformat(str(e))
                open_dt = dt.replace(hour=9, minute=0, second=0, microsecond=0)
                secs.append((dt - open_dt).total_seconds())
            except Exception:
                pass
        if secs:
            avg_entry = round(sum(secs) / len(secs), 1)
    c_days = [ds for ds in day_stats if ds.strategy == "IMMEDIATE_50_THEN_CONFIRM"]
    fired = sum(1 for d in c_days if d.open_probe_fired)
    success = sum(1 for d in c_days if d.open_probe_success)
    return {
        **_metrics(all_trades),
        "round_trips": len(all_trades),
        "avg_first_entry_sec_after_900": avg_entry,
        "open_0900_success_rate_pct": round(success / fired * 100.0, 2) if fired else None,
        "open_probe_attempts": fired,
        "unconfirmed_exit_pnl": round(sum(d.unconfirmed_exit_pnl for d in day_stats), 2),
        "gap_reversal_loss": round(sum(d.gap_reversal_loss for d in day_stats), 2),
        "first_30m_pnl": round(sum(d.first_30m_pnl for d in day_stats), 2),
    }


def evaluate_adoption(a: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    mdd_delta = float(c.get("mdd") or 0) - float(a.get("mdd") or 0)
    gates = {
        "net_c_gt_a": {
            "pass": float(c["net"]) > float(a["net"]),
            "detail": f"C net {c['net']} > A net {a['net']}",
        },
        "pf_c_not_worse": {
            "pass": float(c["pf"]) >= float(a["pf"]),
            "detail": f"C PF {c['pf']} ≥ A PF {a['pf']}",
        },
        "mdd_delta_le_0_5pp": {
            "pass": mdd_delta <= 0.5,
            "detail": f"MDD Δ={mdd_delta:.3f}pp ≤ 0.5",
        },
    }
    all_pass = all(g["pass"] for g in gates.values())
    return {
        "gates": gates,
        "all_pass": all_pass,
        "verdict": "ADOPT" if all_pass else "DO_NOT_ADOPT",
        "live_flag_recommendation": all_pass,
    }


def render_md(report: dict[str, Any]) -> str:
    a, b, c = report["A"], report["B"], report["C"]
    adopt = report["adoption"]
    lines = [
        "# MACD Opening Probe A/B/C (≥20d)",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Days: {', '.join(report['days'])}",
        f"- Live `OPENING_PROBE_ENABLED`: **{OPENING_PROBE_ENABLED}** → "
        f"replay verdict **`{adopt['verdict']}`**",
        "",
        "## Summary",
        "",
        "| Variant | Net | PF | MDD% | WR% | Avg 1st entry (s) | 09:00 success | Unconf exit PnL | 1st-30m PnL |",
        "|---------|-----|----|------|-----|-------------------|---------------|-----------------|-------------|",
        f"| A NEW_TURN | {a['net']:,.0f} | {a['pf']} | {a['mdd']} | {a['wr']} | {a.get('avg_first_entry_sec_after_900')} | — | — | {a.get('first_30m_pnl')} |",
        f"| B 09:03 BAR | {b['net']:,.0f} | {b['pf']} | {b['mdd']} | {b['wr']} | {b.get('avg_first_entry_sec_after_900')} | — | — | {b.get('first_30m_pnl')} |",
        f"| C IMMEDIATE+CONFIRM | {c['net']:,.0f} | {c['pf']} | {c['mdd']} | {c['wr']} | {c.get('avg_first_entry_sec_after_900')} | {c.get('open_0900_success_rate_pct')}% | {c.get('unconfirmed_exit_pnl')} | {c.get('first_30m_pnl')} |",
        "",
        "## Adoption gates (C vs A)",
        "",
    ]
    for name, g in adopt["gates"].items():
        lines.append(f"- **{name}**: {'PASS' if g['pass'] else 'FAIL'} — {g['detail']}")
    lines += [
        "",
        f"**Verdict: `{adopt['verdict']}`**",
        "",
        "## Stress (+1m delay)",
        "",
    ]
    stress = report.get("stress") or {}
    for k, v in stress.items():
        lines.append(f"- {k}: Net={v.get('net')} PF={v.get('pf')} MDD={v.get('mdd')}")
    lines += ["", f"- JSON: `{OUT_JSON.as_posix()}`", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=MIN_DAYS)
    args = parser.parse_args()

    dates, date_sources, day_data = build_day_universe(args.days, refetch_naver=False)
    print(f"Replaying {len(dates)} days …")

    results: dict[str, Any] = {}
    for strat, key in zip(STRATEGIES, ("A", "B", "C")):
        trades: list[Trade] = []
        day_stats: list[DayStats] = []
        for day in dates:
            ds = replay_day(strat, day, day_data, dates, delay_min=DELAY_MIN)
            day_stats.append(ds)
            trades.extend(ds.trades)
        results[key] = summarize(trades, day_stats)
        results[key]["strategy"] = strat
        results[key]["trades"] = [asdict(t) for t in trades]
        print(f"  {key} {strat}: Net={results[key]['net']:,.0f} PF={results[key]['pf']} MDD={results[key]['mdd']}")

    stress = {}
    for key, strat in zip(("A", "B", "C"), STRATEGIES):
        trades = []
        ds_list = []
        for day in dates:
            ds = replay_day(strat, day, day_data, dates, delay_min=DELAY_MIN + 1)
            ds_list.append(ds)
            trades.extend(ds.trades)
        stress[key] = _metrics(trades)

    adoption = evaluate_adoption(results["A"], results["C"])
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": dates,
        "day_sources": date_sources,
        "A": results["A"],
        "B": results["B"],
        "C": results["C"],
        "adoption": adoption,
        "stress": stress,
        "live_OPENING_PROBE_ENABLED": OPENING_PROBE_ENABLED,
        "fill_model": {
            "immediate_delay_min": DELAY_MIN,
            "adverse_pct": ADVERSE_PCT,
            "open_min_return_pct": OPEN_IMMEDIATE_MIN_RETURN_PCT,
        },
    }
    STATE.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(render_md(report), encoding="utf-8")
    print(f"\nAdoption: {adoption['verdict']}")
    print(f"Wrote {OUT_JSON} and {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
