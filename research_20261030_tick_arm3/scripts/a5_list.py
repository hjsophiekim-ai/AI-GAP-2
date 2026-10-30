"""변형 하나의 전 거래 내역을 사람이 읽는 표로 (진입/청산/수량/가격/사유/틱최고/손익).

    python .../a5_list.py --tag CHOPX_A --label "CHOP + MU Assist + XS"
"""
import argparse
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding="utf-8")
from a2_metrics import episodes  # noqa: E402
import a3_peaks as P  # noqa: E402
from app.trading.macd2 import worker as _w  # noqa: E402

DAYS = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]
NAME = {"0193T0": "레버리지", "0197X0": "인버스2X"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--days", nargs="+", default=DAYS)
    a = ap.parse_args()
    print(f"\n{'='*132}\n{a.label}   ({a.tag})\n{'='*132}")
    print(f"{'날짜':9s}{'진입':9s}{'청산':9s}{'종목':10s}{'수량':>6s}{'진입가':>9s}{'청산가':>9s}"
          f"{'수익%':>7s}{'틱최고%':>8s}{'손익':>11s}  청산사유")
    tot = 0.0
    nday = {}
    rows_all = []
    for d in a.days:
        p = P.OUT / f"{a.tag}_{d}.json"
        if not p.exists():
            print(f"{d:9s}  (산출 없음)")
            continue
        eps = episodes(p, d)
        if not eps:
            print(f"{d[4:6]}/{d[6:]:7s}{'— 거래 없음':<40s}")
            continue
        day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        for e in eps:
            pk = float("nan")
            df = P.ticks(e["sym"], day)
            if df is not None and e["entry_t"] and e["exit_t"]:
                w = df[(df.index >= pd.Timestamp(e["entry_t"]).tz_localize(None))
                       & (df.index <= pd.Timestamp(e["exit_t"]).tz_localize(None))]
                if len(w):
                    pk = _w._net_return_pct(e["sym"], e["entry_px"], float(w["price"].max()), e["qty"])
            tot += e["pnl"]
            nday[d] = nday.get(d, 0.0) + e["pnl"]
            print(f"{d[4:6]+'/'+d[6:]:9s}{(e['entry_t'] or '')[11:19]:9s}{(e['exit_t'] or '')[11:19]:9s}"
                  f"{NAME.get(e['sym'], e['sym']):10s}{e['qty']:>6,}{e['entry_px']:>9,.0f}{e['exit_px']:>9,.0f}"
                  f"{e['ret_pct']:>7.2f}{pk:>8.2f}{e['pnl']:>+11,.0f}  {' → '.join(e['exits'])}")
            rows_all.append(e)
    print("-" * 132)
    win = [e for e in rows_all if e["pnl"] > 0]
    print(f"거래 {len(rows_all)}건 · 승 {len(win)} / 패 {len(rows_all)-len(win)} "
          f"(승률 {len(win)/len(rows_all)*100 if rows_all else 0:.1f}%) · 총손익 {tot:+,.0f}원")
    print("\n[일별]")
    for d in a.days:
        v = nday.get(d)
        print(f"  {d[4:6]}/{d[6:]}  {('거래 없음' if v is None else format(v, '+,.0f')):>14s}")


if __name__ == "__main__":
    main()
