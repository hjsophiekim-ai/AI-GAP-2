"""E production parity — 85영업일. READ-ONLY 집계.

EPROD = feature/macd2-e-strategy 의 production MODE_E 를 그대로 재생한 결과.
A     = 같은 브랜치의 MODE_P3 재생 (OFF parity: 기존 P3 연구결과와 바이트 동일해야 한다).
기준  = 연구 E(wk17/out17 → out15 → out14 → BRK15) / 연구 A(round3_85d).
"""
import json, os, sys
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
ST = os.environ["ST"]
OUT = os.environ["EOUT"]
exec(open(ST + "/an20_decomp.py", encoding="utf-8").read().split("NB = lambda")[0]
     .replace("os.path.dirname(os.path.abspath(__file__))", repr(ST)))
D85 = [d for d in DAYS if d <= "20261001"]
SEPT = [d for d in D85 if d.startswith("202609")]
R39 = [d for d in D85 if d >= "20260801"]


def key(o):
    return [(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in o["orders"]]


def load(var, d):
    p = f"{OUT}/REAL_{var}_{d}.json"
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


R = {}
miss = {"EPROD": [], "A": []}
for var, ref in (("EPROD", pe), ("A", ap)):
    R[var] = {}
    for d in DAYS:
        o = load(var, d)
        if o is None:
            miss[var].append(d)
            continue
        r = json.load(open(ref(d), encoding="utf-8"))
        R[var][d] = dict(new=o, ref=r, same=key(o) == key(r),
                         pn=sum(t["krw"] for t in trades(o)), pr=sum(t["krw"] for t in trades(r)))
for v in miss:
    if miss[v]:
        print(f"# {v} 미완료 {len(miss[v])}일: {' '.join(miss[v][:12])}{' ...' if len(miss[v]) > 12 else ''}")

for v, nm in (("A", "P3 OFF parity (브랜치 P3 vs 기존 P3)"), ("EPROD", "E production vs 연구 E")):
    rows = R[v]
    bad = [d for d in sorted(rows) if not rows[d]["same"]]
    print(f"\n## {nm}: 주문 완전일치 {len(rows) - len(bad)}/{len(rows)}일")
    for d in bad:
        x = rows[d]
        print(f"  ✗ {d} new {x['pn']:+,.0f} ref {x['pr']:+,.0f} (Δ {x['pn'] - x['pr']:+,.0f})")
        a, b = key(x["new"]), key(x["ref"])
        for i in range(max(len(a), len(b))):
            p = a[i] if i < len(a) else None
            q = b[i] if i < len(b) else None
            if p != q:
                print(f"      new {p}\n      ref {q}")
                break


def met(days, var, which):
    tr, dv = [], []
    for d in days:
        o = R[var][d][which]
        t = trades(o)
        tr += t
        dv.append(sum(x["krw"] for x in t))
    dv = np.array(dv, dtype=float)
    k = np.array([x["krw"] for x in tr], dtype=float)
    r = dv / 1e7 * 100
    eq = np.cumprod(1 + r / 100)
    mdd = (eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100
    return dict(krw=dv.sum(), pf=k[k > 0].sum() / max(-k[k < 0].sum(), 1), mdd=mdd, dmin=dv.min(),
                lossd=int((dv < 0).sum()), n=len(tr), win=(k > 0).mean() * 100)


if not miss["EPROD"] and not miss["A"]:
    for lab, days in (("85영업일", D85), ("9월", SEPT), ("최근39일", R39)):
        print(f"\n## {lab} ({len(days)}일)")
        print("| | 총손익 | PF | MDD% | 최대1일손실 | 손실일 | 승률% | 거래수 |\n|---|---|---|---|---|---|---|---|")
        for var, which, nm in (("A", "ref", "연구 P3"), ("A", "new", "브랜치 P3"),
                               ("EPROD", "ref", "연구 E (목표)"), ("EPROD", "new", "**production E**")):
            m = met(days, var, which)
            print(f"| {nm} | {m['krw']:+,.0f} | {m['pf']:.2f} | {m['mdd']:.2f} | {m['dmin']:+,.0f} | "
                  f"{m['lossd']} | {m['win']:.1f} | {m['n']} |")
    for d in ("20261001", "20261002"):
        print(f"  {d}: P3 {R['A'][d]['pn']:+,.0f} · E {R['EPROD'][d]['pn']:+,.0f} (연구 E {R['EPROD'][d]['pr']:+,.0f})")
    sa = sum(R["A"][d]["pn"] for d in SEPT)
    print(f"\n# anchor 9월 P3 {sa:+,.0f} (기준 +1,416,214) -> {'PASS' if round(sa) == 1_416_214 else 'FAIL'}")
    cap = 30_000_000
    for var in ("A", "EPROD"):
        use = [sum(x["qty"] * x["px"] for x in R[var][d]["new"]["orders"] if x["side"] == "BUY") for d in DAYS]
        print(f"# 일 3,000만원 한도 {var}: 초과 {sum(u > cap * 1.001 for u in use)}일 · 최대 {max(use):,.0f}")
