"""85일 기준선을 **regime 체제별로 분해**한다.

CHOP 모드의 성과는 chop_regime detector 판정(진입 regime)에 좌우된다:
  · 진입 regime = CHOP          -> P3/B3 스택이 관리 (작게 먹고 작게 잃는다)
  · 진입 regime = TREND/WARMUP  -> BASE(N1) 래더가 관리 (TP2 8%/4% 까지 간다)
두 체제를 섞어 평균내면 PF·MDD 가 둘 다 호도된다. 그래서 분리해 보고한다.

    python .../f2_regime.py --tag L85_A
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")
from a2_metrics import episodes, mdd  # noqa: E402
import a3_peaks as P  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="L85_A")
    ap.add_argument("--days", nargs="*", default=None)
    a = ap.parse_args()
    days = a.days or sorted(os.path.basename(f).split("_")[-1][:-5]
                            for f in Path(P.OUT).glob(f"{a.tag}_2026*.json"))
    eps, by_day = [], defaultdict(float)
    for d in days:
        p = P.OUT / f"{a.tag}_{d}.json"
        j = json.loads(p.read_text(encoding="utf-8"))
        reg = {e["t"][11:19]: (e.get("regime") or "?") for e in j.get("entries", [])}
        for e in episodes(p, d):
            e["regime"] = reg.get(e["entry_t"][11:19], "?")
            eps.append(e)
            by_day[d] += e["pnl"]

    def block(name, sel):
        es = [e for e in eps if sel(e)]
        if not es:
            print(f"  {name:14s} 거래 0")
            return
        w = [e["pnl"] for e in es if e["pnl"] > 0]
        l = [e["pnl"] for e in es if e["pnl"] <= 0]
        dd = defaultdict(float)
        for e in es:
            dd[e["day"]] += e["pnl"]
        daily = [dd[d] for d in days if d in dd]
        pf = (sum(w) / abs(sum(l))) if l and sum(l) else float("inf")
        top = sorted(e["pnl"] for e in es)[::-1]
        print(f"  {name:14s} 거래 {len(es):>3d}  총 {sum(e['pnl'] for e in es):>+12,.0f}  "
              f"PF {pf:>7.3f}  MDD {mdd(daily):>10,.0f}  최악 {min(daily):>+11,.0f}  "
              f"승률 {len(w)/len(es)*100:>5.1f}%  상위1제외 {sum(top)-top[0]:>+12,.0f}")

    print(f"\n{'='*132}\n[{a.tag}] regime 체제별 분해 — {len(days)}일 ({days[0]}~{days[-1]})\n{'='*132}")
    block("전체", lambda e: True)
    print()
    for r in ("CHOP", "TREND", "WARMUP", "?"):
        block(f"regime={r}", lambda e, r=r: e["regime"] == r)

    print(f"\n[월 × regime 거래수 / 손익]")
    months = sorted({d[:6] for d in days})
    regs = ["CHOP", "TREND", "WARMUP", "?"]
    print(f"{'월':10s}" + "".join(f"{r:>22s}" for r in regs) + f"{'월합계':>14s}")
    for m in months:
        line = f"{m[:4]}-{m[4:]:5s}"
        tot = 0.0
        for r in regs:
            es = [e for e in eps if e["day"][:6] == m and e["regime"] == r]
            s = sum(e["pnl"] for e in es)
            tot += s
            line += (f"{len(es):>4d}건 {s:>+15,.0f}" if es else f"{'-':>22s}")
        print(line + f"{tot:>+14,.0f}")

    print(f"\n[일별 regime 라벨 추이]")
    cur = None
    for d in days:
        rs = {e["regime"] for e in eps if e["day"] == d}
        lab = "/".join(sorted(rs)) if rs else "-"
        if lab != cur:
            print(f"  {d[:4]}-{d[4:6]}-{d[6:]}  {lab}")
            cur = lab


if __name__ == "__main__":
    main()
