"""85일 기준선 + 변형 비교 — 월별/전체 지표, 상위 제외, 특정일 의존.

    python .../f1_monthly.py A=L85_A D=L85_D --label "85일"
생략일(변형 재생을 하지 않은 날)은 --fill 로 기준 변형의 산출을 쓴다.
"""
import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")
from a2_metrics import episodes, key, mdd, stats  # noqa: E402

OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))


def load(tag, days, fallback=None):
    eps, miss = {}, []
    for d in days:
        p = OUT / f"{tag}_{d}.json"
        if not p.exists():
            if fallback:
                p = OUT / f"{fallback}_{d}.json"
            if not p.exists():
                miss.append(d)
                continue
        for e in episodes(p, d):
            eps[key(e)] = e
    return eps, miss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="+")
    ap.add_argument("--days", nargs="*", default=None)
    ap.add_argument("--label", default="")
    ap.add_argument("--fill", default=None, help="생략일에 쓸 기준 tag")
    a = ap.parse_args()
    names, tags = [], {}
    for p in a.pairs:
        n, t = p.split("=", 1)
        names.append(n)
        tags[n] = t
    days = a.days
    if not days:
        import r1_run
        days = r1_run.complete_days()[-85:]
    base = names[0]

    V, M = {}, {}
    for n in names:
        V[n], M[n] = load(tags[n], days, fallback=(a.fill if n != base else None))
        if M[n]:
            print(f"  !! {n}({tags[n]}) 결측 {len(M[n])}일: {M[n][:5]}")

    print(f"\n{'='*104}\n{a.label}  {names}  {len(days)}일 {days[0]}~{days[-1]}\n{'='*104}")
    S = {n: stats(list(V[n].values()), days) for n in names}
    rows = [("거래수", "n", "{:,.0f}"), ("총손익", "total", "{:+,.0f}"), ("PF", "pf", "{:.3f}"),
            ("MDD", "mdd", "{:,.0f}"), ("최악일", "worst_day", "{:+,.0f}"), ("승률%", "win_rate", "{:.1f}"),
            ("상위1제외", "ex_top1", "{:+,.0f}"), ("상위2제외", "ex_top2", "{:+,.0f}")]
    print(f"{'':10s}" + "".join(f"{n:>16s}" for n in names))
    for lab, k, fmt in rows:
        print(f"{lab:10s}" + "".join(f"{fmt.format(S[n][k]):>16s}" for n in names))
    print(f"{'최악일자':10s}" + "".join(f"{str(S[n]['worst_day_at']):>16s}" for n in names))
    if len(names) > 1:
        print(f"\n[Δ vs {base}]")
        for n in names[1:]:
            print(f"  {n:6s} 총손익 {S[n]['total']-S[base]['total']:+,.0f}  PF {S[n]['pf']-S[base]['pf']:+.3f}  "
                  f"MDD {S[n]['mdd']-S[base]['mdd']:+,.0f}  최악일 {S[n]['worst_day']-S[base]['worst_day']:+,.0f}  "
                  f"승률 {S[n]['win_rate']-S[base]['win_rate']:+.1f}%p  거래 {S[n]['n']-S[base]['n']:+d}  "
                  f"상위1제외 {S[n]['ex_top1']-S[base]['ex_top1']:+,.0f}")

    print(f"\n[월별]")
    print(f"{'월':8s}{'일수':>5s}" + "".join(f"{n+'손익':>15s}{n+'거래':>7s}" for n in names))
    months = sorted({d[:6] for d in days})
    for m in months:
        md = [d for d in days if d[:6] == m]
        line = f"{m[:4]}-{m[4:]:4s}{len(md):>5d}"
        for n in names:
            es = [e for k_, e in V[n].items() if k_[0][:6] == m]
            line += f"{sum(e['pnl'] for e in es):>+15,.0f}{len(es):>7d}"
        print(line)

    if len(names) > 1:
        print(f"\n[{base} 와 달라진 거래]")
        allk = sorted({k_ for n in names for k_ in V[n]})
        shown = 0
        for k_ in allk:
            vals = [(V[n].get(k_) or {}).get("pnl") for n in names]
            if len(set(vals)) == 1:
                continue
            shown += 1
            line = f"  {k_[0][4:6]}/{k_[0][6:]} {(k_[1][11:19] if k_[1] else '?'):9s}{k_[2]:8s}"
            for n in names:
                e = V[n].get(k_)
                line += (f"{e['pnl']:>+13,.0f}" if e else f"{'-':>13s}")
            line += "   " + " | ".join(",".join(sorted(set((V[n].get(k_) or {}).get("exits", [])))) or "-"
                                       for n in names)
            print(line)
        if not shown:
            print("  (차이 없음)")
        else:
            print(f"  -> 달라진 거래 {shown}건")
            for n in names[1:]:
                dd = defaultdict(float)
                for k_ in allk:
                    e0, e1 = V[base].get(k_), V[n].get(k_)
                    if (e0 or {}).get("pnl") != (e1 or {}).get("pnl"):
                        dd[k_[0]] += (e1 or {}).get("pnl", 0.0) - (e0 or {}).get("pnl", 0.0)
                tot = sum(dd.values())
                top = sorted(dd.items(), key=lambda x: -abs(x[1]))[:3]
                print(f"  [{n}] Δ합 {tot:+,.0f} · 상위기여일 " +
                      ", ".join(f"{k_[4:6]}/{k_[6:]} {v:+,.0f}" for k_, v in top))
                if top and tot:
                    print(f"       최대기여일 제외 시 Δ = {tot - top[0][1]:+,.0f}")


if __name__ == "__main__":
    main()
