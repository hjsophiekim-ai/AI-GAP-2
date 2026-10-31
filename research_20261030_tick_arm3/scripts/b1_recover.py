"""청산 후 1~2개 3분봉 안의 '원래 방향 회복' 전수 측정 (재생 불필요, 실틱 직접 측정).

ONE_BAR_HOLD / TWO_BAR_HOLD 를 만들 가치가 있는지 먼저 본다.
각 청산 거래에 대해 **청산 직후 1봉 / 2봉 창**에서 보유 ETF 가 어디까지 갔는지를
실제 1초 스냅샷으로 재고, 계속 들고 있었다면 얼마가 됐을지를 계산한다.

    python .../b1_recover.py --tag CHOPX_A --label "CHOP + MU Assist + XS"

창 정의 (엔진이 완성 3분봉에서 판정하는 것과 맞춘다):
    1봉 창 = (청산시각, ceil_3m(청산시각) + 3분]
    2봉 창 = (청산시각, ceil_3m(청산시각) + 6분]
"""
import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from a2_metrics import episodes  # noqa: E402
import a3_peaks as P  # noqa: E402
from app.trading.macd2 import worker as W  # noqa: E402

DAYS = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]

#: 사용자가 지정한 '너무 빨리 끊었을 수 있는' 청산 사유군.
GROUP = {
    "반대플래그": ("OPPOSITE_SIGNAL", "TW2_3SLOT_SELL_ONLY", "SMALL_WHIPSAW"),
    "H50": ("H50_WHIPSAW_RELEASE", "H50_MAX_HOLD_EXIT", "MU_ASSIST_H50_GAP_EXIT"),
    "C1/고점보호": ("C1_PEAK_PROTECTION_EXIT", "TIME_WINDOW_PROFIT_LOCK_STOP",
                "TIME_WINDOW_TRAILING_STOP", "TICK_ARM3_STOP"),
    "추세이탈/구조": ("B3_SL", "B3_MAXHOLD", "TIME_WINDOW_BREAKEVEN_STOP",
                 "TIME_WINDOW_AFTER_TP1_STOP"),
}
#: 아래는 대상이 아니다 — 익절 완료·하드손절·장마감.
SKIP = ("TP2_FULL", "AFTERNOON_TP", "B3_TP", "FORCED_LIQUIDATION", "STOP_LOSS",
        "TP1_PARTIAL", "P3_PARTIAL_EXIT")


def group_of(reasons):
    for r in reasons:
        for g, pats in GROUP.items():
            if any(p in r for p in pats):
                return g, r
    return None, (reasons[-1] if reasons else "")


def ceil3(ts):
    m = (ts.minute // 3 + (1 if (ts.minute % 3 or ts.second or ts.microsecond) else 0)) * 3
    base = ts.replace(minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=m)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="CHOPX_A")
    ap.add_argument("--label", default="")
    ap.add_argument("--days", nargs="+", default=DAYS)
    a = ap.parse_args()

    rows = []
    for d in a.days:
        p = P.OUT / f"{a.tag}_{d}.json"
        if not p.exists():
            continue
        day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        tk = P.ticks(None, day) if False else None
        for e in episodes(p, d):
            if not e["exit_t"]:
                continue
            last = e["exits"][-1] if e["exits"] else ""
            g, r = group_of(e["exits"])
            if g is None:
                continue
            if any(s in last for s in SKIP) and g is None:
                continue
            df = P.ticks(e["sym"], day)
            if df is None:
                continue
            tx = pd.Timestamp(e["exit_t"]).tz_localize(None)
            w1_end = ceil3(tx) + timedelta(minutes=3)
            w2_end = ceil3(tx) + timedelta(minutes=6)
            sym, avg, qty = e["sym"], e["entry_px"], e["qty"]

            def stat(t_end):
                w = df[(df.index > tx) & (df.index <= t_end)]
                if not len(w):
                    return None
                hi = float(w["price"].max())
                cl = float(w["price"].iloc[-1])
                bid_cl = float(w["best_bid"].iloc[-1]) if pd.notna(w["best_bid"].iloc[-1]) else cl
                return dict(
                    hi_net=W._net_return_pct(sym, avg, hi, qty),
                    close_net=W._net_return_pct(sym, avg, cl, qty),
                    close_bid_net=W._net_return_pct(sym, avg, bid_cl, qty),
                )
            s1, s2 = stat(w1_end), stat(w2_end)
            exit_net = W._net_return_pct(sym, avg, e["exit_px"], qty)
            rows.append(dict(day=d, t_in=e["entry_t"][11:19], t_out=e["exit_t"][11:19],
                             sym=e["sym"], grp=g, reason=r, qty=qty, entry=avg,
                             exit_px=e["exit_px"], exit_net=exit_net, pnl=e["pnl"],
                             s1=s1, s2=s2))

    print(f"\n{'='*150}\n{a.label or a.tag} — 청산 후 1봉/2봉 회복 전수 ({len(rows)}건)\n{'='*150}")
    print(f"{'날짜':8s}{'진입':9s}{'청산':9s}{'종목':9s}{'사유군':12s}{'청산%':>7s}"
          f"{'1봉최고':>8s}{'1봉종가':>8s}{'2봉최고':>8s}{'2봉종가':>8s}{'1봉Δ원':>11s}{'2봉Δ원':>11s}  사유")
    agg = {}
    for r in rows:
        def f(s, k):
            return s[k] if s else float("nan")
        # 1봉/2봉 끝까지 들고 있었다면의 손익차 (매도는 최우선 매수호가 기준)
        d1 = (f(r["s1"], "close_bid_net") - r["exit_net"]) / 100.0 * r["entry"] * r["qty"] if r["s1"] else float("nan")
        d2 = (f(r["s2"], "close_bid_net") - r["exit_net"]) / 100.0 * r["entry"] * r["qty"] if r["s2"] else float("nan")
        print(f"{r['day'][4:6]+'/'+r['day'][6:]:8s}{r['t_in']:9s}{r['t_out']:9s}{r['sym']:9s}{r['grp']:12s}"
              f"{r['exit_net']:>7.2f}{f(r['s1'],'hi_net'):>8.2f}{f(r['s1'],'close_bid_net'):>8.2f}"
              f"{f(r['s2'],'hi_net'):>8.2f}{f(r['s2'],'close_bid_net'):>8.2f}{d1:>+11,.0f}{d2:>+11,.0f}  {r['reason']}")
        A = agg.setdefault(r["grp"], dict(n=0, r15_1=0, r30_1=0, r15_2=0, r30_2=0,
                                          better1=0, better2=0, d1=0.0, d2=0.0))
        A["n"] += 1
        if r["s1"]:
            A["r15_1"] += r["s1"]["hi_net"] >= 1.5
            A["r30_1"] += r["s1"]["hi_net"] >= 3.0
            A["better1"] += r["s1"]["close_bid_net"] > r["exit_net"]
            A["d1"] += d1
        if r["s2"]:
            A["r15_2"] += r["s2"]["hi_net"] >= 1.5
            A["r30_2"] += r["s2"]["hi_net"] >= 3.0
            A["better2"] += r["s2"]["close_bid_net"] > r["exit_net"]
            A["d2"] += d2

    print(f"\n{'='*150}\n[청산사유군별 요약]  (1봉/2봉 창에서 '들고 있었다면'의 결과)\n{'='*150}")
    print(f"{'사유군':14s}{'건수':>5s}{'1봉 +1.5%':>10s}{'1봉 +3%':>9s}{'2봉 +1.5%':>10s}{'2봉 +3%':>9s}"
          f"{'1봉 개선건':>11s}{'2봉 개선건':>11s}{'1봉 Δ합':>13s}{'2봉 Δ합':>13s}")
    tot = dict(n=0, d1=0.0, d2=0.0)
    for g, A in agg.items():
        print(f"{g:14s}{A['n']:>5d}{A['r15_1']:>10d}{A['r30_1']:>9d}{A['r15_2']:>10d}{A['r30_2']:>9d}"
              f"{A['better1']:>11d}{A['better2']:>11d}{A['d1']:>+13,.0f}{A['d2']:>+13,.0f}")
        tot["n"] += A["n"]
        tot["d1"] += A["d1"]
        tot["d2"] += A["d2"]
    print(f"{'합계':14s}{tot['n']:>5d}{'':>10s}{'':>9s}{'':>10s}{'':>9s}{'':>11s}{'':>11s}"
          f"{tot['d1']:>+13,.0f}{tot['d2']:>+13,.0f}")
    print("\n주: 'Δ원' = 그 창 끝까지 들고 있다가 최우선 매수호가에 팔았을 때의 손익 − 실제 청산 손익.")
    print("    기존 청산을 유예했을 때의 **상한**이 아니라, 유예 자체의 직접 효과다")
    print("    (유예 중 다른 청산규칙이 먼저 걸릴 수 있고, 반대 ETF 신규진입 기회도 사라진다).")


if __name__ == "__main__":
    main()
