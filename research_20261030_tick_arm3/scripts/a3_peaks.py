"""A 변형 각 거래의 **실제 1초 틱** 최고 순수익 / 최저 매수호가 순수익을 오프라인으로 잰다.

보호선이 ARM 되려면 틱 최고 순수익이 +3.0% 에 닿아야 한다. 닿지 않은 날은
B/C 가 A 와 **논리적으로 동일**하므로 재생할 필요가 없다 — 그 선별에 쓴다.

    python .../a3_peaks.py --tag CHOPX_A --days 20261016 ... 20261030
"""
import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")
from a2_metrics import episodes  # noqa: E402

OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))
RT = Path(os.environ.get("ARM3_RT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/rt"))

#: 재생 하네스가 쓰는 것과 같은 수수료 모형 (worker._net_return_pct 와 같은 정의를 쓰기 위해
#: production 을 import 한다 — 읽기만 한다).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.trading.macd2 import worker as _w  # noqa: E402

_C = {}


def ticks(sym, day):
    k = (sym, day)
    if k not in _C:
        p = RT / "snapshots" / day / f"{sym}.csv"
        if not p.exists():
            _C[k] = None
        else:
            df = pd.read_csv(p, usecols=["ts", "price", "best_bid"])
            df["ts"] = pd.to_datetime(df["ts"])
            _C[k] = df.dropna(subset=["price"]).set_index("ts").sort_index()
    return _C[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--arm", type=float, default=3.0)
    a = ap.parse_args()
    hit_days, rows = set(), []
    for d in a.days:
        p = OUT / f"{a.tag}_{d}.json"
        if not p.exists():
            print(f"  {d}: 산출 없음")
            continue
        day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        for e in episodes(p, d):
            if not e["entry_t"] or not e["exit_t"]:
                continue
            t0 = pd.Timestamp(e["entry_t"]).tz_localize(None)
            t1 = pd.Timestamp(e["exit_t"]).tz_localize(None)
            df = ticks(e["sym"], day)
            peak = bid_min = None
            if df is not None:
                w = df[(df.index >= t0) & (df.index <= t1)]
                if len(w):
                    peak = _w._net_return_pct(e["sym"], e["entry_px"], float(w["price"].max()), e["qty"])
                    bid_min = _w._net_return_pct(e["sym"], e["entry_px"], float(w["best_bid"].min()), e["qty"])
            rows.append((d, e["entry_t"][11:19], e["sym"], e["qty"], e["entry_px"], e["ret_pct"],
                         e["pnl"], peak, bid_min, ";".join(sorted(set(e["exits"])))))
            if peak is not None and peak >= a.arm:
                hit_days.add(d)
    print(f"{'날짜':10s}{'진입':9s}{'종목':8s}{'수익%':>8s}{'손익':>12s}{'틱최고%':>9s}{'최저bid%':>10s}  사유")
    for r in rows:
        mark = " ◀ARM" if (r[7] is not None and r[7] >= a.arm) else ""
        print(f"{r[0]:10s}{r[1]:9s}{r[2]:8s}{r[5]:>8.2f}{r[6]:>+12,.0f}"
              f"{(r[7] if r[7] is not None else float('nan')):>9.2f}{(r[8] if r[8] is not None else float('nan')):>10.2f}  {r[9]}{mark}")
    print(f"\n틱 최고 순수익 >= +{a.arm}% 인 거래가 있는 날 ({len(hit_days)}일):")
    print(" ".join(sorted(hit_days)))
    print(f"나머지 {len(a.days) - len(hit_days)}일은 보호선이 ARM 되지 않는다 -> B/C ≡ A (재생 불필요)")


if __name__ == "__main__":
    main()
