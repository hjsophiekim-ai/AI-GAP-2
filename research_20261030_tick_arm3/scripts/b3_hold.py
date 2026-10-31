"""ONE_BAR_HOLD / TWO_BAR_HOLD — 유예 전수 + 청산사유별 효과.

    python .../b3_hold.py --strat CHOPX --variants A H1 H2
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")
from a2_metrics import episodes, key  # noqa: E402

OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))
DAYS = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]
GROUP = {
    "반대플래그": ("OPPOSITE_SIGNAL", "SELL_ONLY"),
    "H50": ("H50_WHIPSAW_RELEASE", "H50_MAX_HOLD_EXIT", "MU_ASSIST_H50_GAP_EXIT"),
    "C1/고점보호": ("C1_PEAK_PROTECTION_EXIT", "PROFIT_LOCK", "TRAILING"),
    "추세이탈/구조": ("B3_MAXHOLD", "BREAKEVEN", "AFTER_TP1"),
}


def grp(reason):
    for g, pats in GROUP.items():
        if any(p in reason for p in pats):
            return g
    return "기타"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strat", default="CHOPX")
    ap.add_argument("--variants", nargs="+", default=["A", "H1", "H2"])
    ap.add_argument("--days", nargs="+", default=DAYS)
    a = ap.parse_args()
    base = a.variants[0]

    eps, logs = {}, {}
    for v in a.variants:
        eps[v], logs[v] = {}, {}
        for d in a.days:
            p = OUT / f"{a.strat}_{v}_{d}.json"
            if not p.exists():
                continue
            j = json.loads(p.read_text(encoding="utf-8"))
            for e in episodes(p, d):
                eps[v][key(e)] = e
            logs[v][d] = [x for x in j.get("hold", []) if x["t"][:10].replace("-", "") == d]

    print(f"\n{'='*140}\n[{a.strat}] 유예 판정 전수 (청산신호가 유예 대상이었던 모든 시점)\n{'='*140}")
    print(f"{'날짜':8s}{'시각':9s}{'종목':9s}{'판정':18s}{'사유군':12s}{'청산사유':30s} 비고")
    cnt = Counter()
    for v in a.variants[1:]:
        for d in a.days:
            for x in logs[v].get(d, []):
                g = grp(x.get("reason", ""))
                note = ""
                if x["k"] == "BREAKDOWN":
                    note = f"강한붕괴={x.get('why')} → 즉시청산"
                elif x["k"] == "DEFER":
                    note = f"유예 시작 (~{x.get('until','')[11:19]}), net0={x.get('net0')}"
                elif x["k"] == "RECOVER":
                    note = f"회복 → 청산취소 (net0 {x.get('net0')} → {x.get('net')})"
                elif x["k"] == "EXIT_AFTER_DEFER":
                    note = f"미회복 → 유예 후 청산 (net0 {x.get('net0')} → {x.get('net')})"
                cnt[(v, x["k"], g)] += 1
                print(f"{d[4:6]+'/'+d[6:]:8s}{x['t'][11:19]:9s}{x['sym']:9s}{v+':'+x['k']:18s}{g:12s}"
                      f"{str(x.get('reason'))[:30]:30s} {note}")
    print(f"\n{'='*140}\n[판정 집계]\n{'='*140}")
    for (v, k, g), n in sorted(cnt.items()):
        print(f"  {v}  {k:18s} {g:12s} {n}회")

    print(f"\n{'='*140}\n[거래 1:1 대조 — {base} 와 달라진 거래만]\n{'='*140}")
    allk = sorted({k for v in a.variants for k in eps[v]})
    tot = {v: 0.0 for v in a.variants}
    byg = {}
    for k in allk:
        e0 = eps[base].get(k)
        diff = any((eps[v].get(k) or {}).get("pnl") != (e0 or {}).get("pnl") for v in a.variants[1:])
        for v in a.variants:
            tot[v] += (eps[v].get(k) or {}).get("pnl", 0.0)
        if not diff:
            continue
        line = f"{k[0][4:6]}/{k[0][6:]} {(k[1][11:19] if k[1] else '?'):9s}{k[2]:9s}"
        for v in a.variants:
            e = eps[v].get(k)
            line += (f"{e['pnl']:>+13,.0f}" if e else f"{'-':>13s}")
        line += "   " + " | ".join(",".join(sorted(set((eps[v].get(k) or {}).get("exits", [])))) or "-"
                                   for v in a.variants)
        print(line)
        if e0:
            g = grp((e0.get("exits") or [""])[-1])
            for v in a.variants[1:]:
                d_ = (eps[v].get(k) or {}).get("pnl", 0.0) - e0["pnl"]
                byg.setdefault(g, {}).setdefault(v, 0.0)
                byg[g][v] += d_
    print(f"\n[총손익] " + "  ".join(f"{v}={tot[v]:+,.0f}" for v in a.variants))
    print(f"[Δ vs {base}] " + "  ".join(f"{v}={tot[v]-tot[base]:+,.0f}" for v in a.variants[1:]))
    if byg:
        print(f"\n[청산사유군별 Δ]")
        for g, dd in byg.items():
            print(f"  {g:14s} " + "  ".join(f"{v}={x:+,.0f}" for v, x in dd.items()))


if __name__ == "__main__":
    main()
