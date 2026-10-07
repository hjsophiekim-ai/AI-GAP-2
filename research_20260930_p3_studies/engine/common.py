"""공통 유틸 — READ-ONLY 연구. production 무수정."""
from __future__ import annotations
import sys
from collections import defaultdict
import hengine as H

def pnl(t):
    return t["net_pct"] * t["w1a"]

def compound(trades, dates):
    ds = set(dates); eq = 1.0
    for t in sorted((x for x in trades if x["date"] in ds), key=lambda x: x["exit_time"]):
        eq *= (1 + pnl(t) / 100.0)
    return (eq - 1) * 100.0

def daily(trades, dates):
    ds = set(dates); d = defaultdict(float)
    for t in trades:
        if t["date"] in ds:
            d[t["date"]] += pnl(t)
    return d

def monthly_pct(trades, dates, days_per_month=21):
    """창 전체 복리를 21영업일 기준 월수익률로 환산."""
    c = compound(trades, dates)
    mult = 1.0 + c / 100.0
    if mult <= 0:
        return float("nan")
    return (mult ** (days_per_month / len(dates)) - 1.0) * 100.0

def excl_topn(trades, dates, k):
    ds = set(dates)
    ts = sorted((x for x in trades if x["date"] in ds), key=lambda x: x["exit_time"])
    if not ts:
        return 0.0
    top = sorted(ts, key=pnl, reverse=True)[:k]
    tid = {id(x) for x in top}
    rest = [x for x in ts if id(x) not in tid]
    eq = 1.0
    for t in rest:
        eq *= (1 + pnl(t) / 100.0)
    return (eq - 1) * 100.0

def excl_topdays(trades, dates, k):
    d = daily(trades, dates)
    drop = set(sorted(d, key=lambda x: d[x], reverse=True)[:k])
    keep = [x for x in dates if x not in drop]
    return compound(trades, keep)

def summarize(trades, dates):
    m = H.metrics(trades, dates)
    if not m.get("trades"):
        return {"trades": 0}
    m["monthly_pct"] = round(monthly_pct(trades, dates), 2)
    m["excl_top1d"] = round(excl_topdays(trades, dates, 1), 2)
    m["excl_top3d"] = round(excl_topdays(trades, dates, 3), 2)
    return m

def line(tag, m):
    if not m.get("trades"):
        return f"{tag:24s} (no trades)"
    return (f"{tag:24s} n={m['trades']:4d} 월={m['monthly_pct']:7.2f}% "
            f"복리={m['compound_pct']:9.2f} PF={m['pf'] if m['pf'] else 0:6.3f} "
            f"MDD={m['mdd_pct']:7.2f} 승률={m['win_rate_pct']:5.1f} "
            f"일승률={m['day_win_rate_pct']:5.1f} T10x={m['top10_excl_pct']:8.2f} "
            f"-1d={m['excl_top1d']:8.2f} -3d={m['excl_top3d']:8.2f}")

def p(*a):
    print(*a, flush=True)
