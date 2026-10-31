"""3번 연구 1단계 — B3 가 관리한 거래 전수 + 조기청산 후 추가상승 측정 (재생 불필요).

목적: CHOP 진입은 그대로 두고, B3 관리 거래 중 **추세가 강한 것만** N1 runner 청산으로
승격할 여지가 있는지. 먼저 "어떤 B3 승자가 조기청산 후 더 올라갔는가"를 실틱으로 전수 측정한다.

    python .../d1_b3runner.py --tag CHOPX_A

측정:
  · 청산 후 **그 날 장중**(15:00 까지) 보유방향으로 얼마나 더 갔는가 (틱 최고, 매수호가 종가)
  · N1 의 추세 가지 TP2(8%) / 비추세 TP2(4%) 에 닿았을까
  · 진입 시점의 production 지표 — n1_adaptive.snapshot(추세 판정) / 진입 regime
    (승격 조건 후보를 **기존 지표만으로** 만들 수 있는지 보기 위함. 임계값은 만들지 않는다.)
"""
import argparse
import json
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
from app.trading.macd2 import config, worker as W  # noqa: E402

DAYS = ["20261016", "20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]
#: B3(CHOP-STACK 구조관리)가 낸 청산 사유.
B3_EXITS = ("B3_TP", "B3_SL", "B3_MAXHOLD")
N1_TREND_TP2 = config.N1_TREND_TP2 * 100.0        # 8.0
N1_OFF_TP2 = config.N1_OFF_TREND_TP2 * 100.0      # 4.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="CHOPX_A")
    ap.add_argument("--days", nargs="+", default=DAYS)
    a = ap.parse_args()

    print(f"\n{'='*150}")
    print(f"[{a.tag}] B3 관리 거래 전수 — 청산 후 당일 추가상승")
    print(f"{'='*150}")
    print(f"{'날짜':8s}{'진입':9s}{'청산':9s}{'종목':9s}{'사유':11s}{'실현%':>7s}{'보유중틱최고':>12s}"
          f"{'청산후틱최고':>12s}{'청산후종가':>11s}{'8%':>4s}{'4%':>4s}{'미실현Δ원':>13s}  진입스냅")
    rows = []
    for d in a.days:
        p = P.OUT / f"{a.tag}_{d}.json"
        if not p.exists():
            continue
        j = json.loads(p.read_text(encoding="utf-8"))
        ent = {e["t"][11:19]: e for e in j.get("entries", [])}
        day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        for e in episodes(p, d):
            last = ";".join(e["exits"])
            if not any(b in last for b in B3_EXITS):
                continue
            df = P.ticks(e["sym"], day)
            if df is None:
                continue
            sym, avg, qty = e["sym"], e["entry_px"], e["qty"]
            t_in = pd.Timestamp(e["entry_t"]).tz_localize(None)
            t_out = pd.Timestamp(e["exit_t"]).tz_localize(None)
            t_end = pd.Timestamp(f"{day} 15:00:00")
            held = df[(df.index >= t_in) & (df.index <= t_out)]
            after = df[(df.index > t_out) & (df.index <= t_end)]
            pk_held = W._net_return_pct(sym, avg, float(held["price"].max()), qty) if len(held) else float("nan")
            if len(after):
                pk_aft = W._net_return_pct(sym, avg, float(after["price"].max()), qty)
                bid_last = after["best_bid"].dropna()
                cl_aft = (W._net_return_pct(sym, avg, float(bid_last.iloc[-1]), qty)
                          if len(bid_last) else float("nan"))
            else:
                pk_aft = cl_aft = float("nan")
            exit_net = W._net_return_pct(sym, avg, e["exit_px"], qty)
            # 승격했다면의 **상한**: 청산하지 않고 당일 틱 최고에서 팔았을 때
            dmax = (pk_aft - exit_net) / 100.0 * avg * qty if pk_aft == pk_aft else float("nan")
            snap = ent.get(e["entry_t"][11:19], {})
            tag = (f"trend_ok={snap.get('trend_ok')} {snap.get('snap_reason')} "
                   f"regime={snap.get('regime')}")
            print(f"{d[4:6]+'/'+d[6:]:8s}{e['entry_t'][11:19]:9s}{e['exit_t'][11:19]:9s}{sym:9s}"
                  f"{last.split(';')[-1][:11]:11s}{exit_net:>7.2f}{pk_held:>12.2f}{pk_aft:>12.2f}"
                  f"{cl_aft:>11.2f}{('O' if pk_aft >= N1_TREND_TP2 else '.'):>4s}"
                  f"{('O' if pk_aft >= N1_OFF_TP2 else '.'):>4s}{dmax:>+13,.0f}  {tag}")
            rows.append((d, e, exit_net, pk_held, pk_aft, cl_aft, dmax, snap))

    if not rows:
        print("  (B3 관리 거래 없음)")
        return
    wins = [r for r in rows if r[1]["pnl"] > 0]
    up = [r for r in rows if r[4] == r[4] and r[4] > r[2]]
    print(f"\n{'='*150}")
    print(f"B3 관리 거래 {len(rows)}건 (승 {len(wins)} / 패 {len(rows)-len(wins)})")
    print(f"청산 후 당일 틱이 청산가보다 더 간 건수: {len(up)}건")
    print(f"  그 합계(틱 최고 기준 **상한**): {sum(r[6] for r in up):+,.0f}원")
    print(f"  N1 추세 TP2 {N1_TREND_TP2:.0f}% 도달: {sum(1 for r in rows if r[4]==r[4] and r[4]>=N1_TREND_TP2)}건"
          f" · 비추세 TP2 {N1_OFF_TP2:.0f}% 도달: {sum(1 for r in rows if r[4]==r[4] and r[4]>=N1_OFF_TP2)}건")
    print("\n주: '미실현Δ원' 은 당일 **틱 최고가**에 팔았다는 상한 가정이다. 실제 N1 runner 는")
    print("    TP2/트레일링/손절/반대신호로 그보다 낮은 값에 나간다 — 승격 이득의 천장으로만 읽는다.")


if __name__ == "__main__":
    main()
