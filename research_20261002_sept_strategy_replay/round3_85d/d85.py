"""85영업일: A P3-R0 vs B P3-CHOP-SIMPLE (production worker 전체 재생, REAL 체결). READ-ONLY."""
import importlib.util, json, os, sys
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
S = sys.argv[1]
spec = importlib.util.spec_from_file_location("s3", S + "/wk/lab/sept3_lib.py")
L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)
O4, O3 = S + "/wk/out4", S + "/wk/out3"
DAYS = sorted({os.path.basename(p)[7:15] for p in __import__("glob").glob(O4 + "/REAL_A_*.json")})
SEPT = [d for d in DAYS if d.startswith("202609")]
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
ld = lambda v, d: json.load(open(f"{O4}/REAL_{v}_{d}.json", encoding="utf-8"))
print(f"대상 {len(DAYS)}일 {DAYS[0]}~{DAYS[-1]}")

# anchor: 9월 A == 기존 REAL A (out3), 거래 단위
bad = []
for d in SEPT + ["20261001"]:
    a, b = ld("A", d), json.load(open(f"{O3}/REAL_A_{d}.json", encoding="utf-8"))
    f = lambda o: [(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in o["orders"]]
    k = sum(1 for x in a["orders"] if x["side"] == "SELL")
    g = lambda o: [(e["exit_reason"], e["net_pnl"]) for e in o["ex"] if e["side"] == "SELL"][-k:] if k else []
    if f(a) != f(b) or g(a) != g(b):
        bad.append(d)
T = {v: sum((L.trades(ld(v, d)) for d in DAYS), []) for v in ("A", "F")}
sa = sum(t["krw"] for t in T["A"] if t["day"] in SEPT)
print(f"## anchor: 9월 A {sa:+,.0f}원 (기존 +1,416,214) · 거래단위 불일치일 {bad or '없음'}")
if bad or round(sa) != 1_416_214:
    print("=> INVALID REPLAY"); sys.exit(0)

NM = {"A": "A P3-R0", "F": "B P3-CHOP-SIMPLE"}
def tab(days, title):
    print(f"\n## {title} ({len(days)}일)")
    print("| 전략 | 총손익 | 복리% | PF | MDD% | 승률% | 손실일 | +2%일 | 거래수 | 평균보유(분) | 최대1일손실 | 최대1일수익 |\n|---|---|---|---|---|---|---|---|---|---|---|---|")
    M = {}
    for v in ("A", "F"):
        m = M[v] = L.metrics(days, [t for t in T[v] if t["day"] in days])
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['pf']:.2f} | {m['mdd']:.2f} | {m['win']:.1f} | {m['lossd']} | {m['d2']} | {m['n']} | {m['hold']:.0f} | {m['dmin']:+,.0f} | {m['dmax']:+,.0f} |")
    a, b = M["A"], M["F"]
    print(f"  B−A: 총손익 {b['krw'] - a['krw']:+,.0f} · PF {b['pf'] - a['pf']:+.2f} · MDD {b['mdd'] - a['mdd']:+.2f}%p · 승률 {b['win'] - a['win']:+.1f}%p · 거래수 {b['n'] - a['n']:+d}")
    return M
M85 = tab(DAYS, "85영업일 전체")
print("\n## 월별\n| 월 | A | B | B−A |\n|---|---|---|---|")
for mo in sorted({d[:6] for d in DAYS}):
    a = sum(t["krw"] for t in T["A"] if t["day"][:6] == mo); b = sum(t["krw"] for t in T["F"] if t["day"][:6] == mo)
    print(f"| {mo[:4]}-{mo[4:]} | {a:+,.0f} | {b:+,.0f} | {b - a:+,.0f} |")
tab(SEPT, "9월 하위표 (sanity)")

# CHOP 거래
print("\n## CHOP 진입 거래만 (85일)")
amap = {L.key(t): t for t in T["A"]}; fmap = {L.key(t): t for t in T["F"]}
fc = [t for t in T["F"] if t["regime"] == "CHOP"]; ac = [t for t in T["A"] if t["regime"] == "CHOP"]
pairs = [(amap[L.key(t)], t) for t in fc if L.key(t) in amap]
print(f"  CHOP 진입: A {len(ac)}건 (A 손익 {sum(t['krw'] for t in ac):+,.0f}) · B {len(fc)}건 (B 손익 {sum(t['krw'] for t in fc):+,.0f}) · 차이 {sum(t['krw'] for t in fc) - sum(t['krw'] for t in ac):+,.0f}")
imp = [p for p in pairs if p[1]["krw"] > p[0]["krw"]]; wor = [p for p in pairs if p[1]["krw"] < p[0]["krw"]]
print(f"  같은 진입 매칭 {len(pairs)}건: A {sum(a['krw'] for a, _ in pairs):+,.0f} / B {sum(b['krw'] for _, b in pairs):+,.0f} / 개선 {len(imp)} · 악화 {len(wor)}")
t1 = [p for p in pairs if "B3_SL" in p[0]["reasons"] and p[1]["krw"] > p[0]["krw"]]
t2 = [p for p in pairs if any(r in ("B3_TP", "P3_PARTIAL_EXIT") for r in p[0]["reasons"]) and p[0]["krw"] > p[1]["krw"]]
t3 = [p for p in pairs if p[1]["net"] >= 3.0]
for lab, z in (("1) A 의 B3 −1% 손절보다 SIMPLE 이 좋았던 거래", t1), ("2) A 의 B3 +1%/Q2 가 SIMPLE 보다 좋았던 거래", t2),
               ("3) SIMPLE 이 큰 runner(net ≥ +3%)를 살린 거래", t3)):
    print(f"  {lab}: {len(z)}건, B−A {sum(b['krw'] - a['krw'] for a, b in z):+,.0f}원")
pairs.sort(key=lambda p: p[1]["krw"] - p[0]["krw"])
for lab, zs in (("성공", pairs[::-1][:5]), ("실패", pairs[:5])):
    for a, b in zs:
        print(f"    대표 {lab}: {a['day']} {a['entry']:%H:%M} {a['dir']} A {a['exit']:%H:%M} {L.rs(a)} {a['krw']:+,.0f} | B {b['exit']:%H:%M} {L.rs(b)} {b['krw']:+,.0f} | {b['krw'] - a['krw']:+,.0f}")

# 견고성
print("\n## 견고성")
dd = np.array([sum(t["krw"] for t in T["F"] if t["day"] == d) - sum(t["krw"] for t in T["A"] if t["day"] == d) for d in DAYS])
tot = dd.sum(); top2 = np.sort(dd)[-2:].sum()
print(f"  일별 B−A: 우세 {(dd > 0).sum()}일 / 열세 {(dd < 0).sum()}일 / 동일 {(dd == 0).sum()}일 · 합 {tot:+,.0f} · 상위2일 {top2:+,.0f} · 상위2일 제외 {tot - top2:+,.0f}")
td = np.array([b["krw"] - a["krw"] for a, b in pairs])
print(f"  CHOP 거래 B−A 상위2거래 {np.sort(td)[-2:].sum():+,.0f} · 상위2거래 제외 CHOP 합 {td.sum() - np.sort(td)[-2:].sum():+,.0f}")
nr = [d for d in DAYS if d not in RECENT]
print(f"  최근 6일 제외({len(nr)}일): A {sum(t['krw'] for t in T['A'] if t['day'] in nr):+,.0f} / B {sum(t['krw'] for t in T['F'] if t['day'] in nr):+,.0f} / B−A {dd[[DAYS.index(d) for d in nr]].sum():+,.0f}")
h1 = [d for d in DAYS if d < "20260801"]; h2 = [d for d in DAYS if d >= "20260801"]
print(f"  전반(~07/31 {len(h1)}일) B−A {dd[[DAYS.index(d) for d in h1]].sum():+,.0f} · 후반(08/01~ {len(h2)}일) B−A {dd[[DAYS.index(d) for d in h2]].sum():+,.0f}")
rng = np.random.default_rng(0)
bs = np.array([rng.choice(dd, len(dd)).sum() for _ in range(5000)])
print(f"  일별 부트스트랩 P(B−A>0) {(bs > 0).mean() * 100:.1f}%")
with open(S + "/wk/lab/d85_trades.md", "w", encoding="utf-8") as f:
    f.write("| 전략 | 날짜 | 플래그 | 진입 | 방향 | 체결가 | 청산 | 청산사유 | regime | net% | 손익 |\n|" + "---|" * 11 + "\n")
    for v in ("A", "F"):
        for t in T[v]:
            f.write(f"| {NM[v][0]} | {t['day']} | {t['flag']} | {t['entry']:%H:%M:%S} | {t['dir']} | {t['px_in']:.0f} | {t['exit']:%H:%M:%S} | {L.rs(t)} | {t['regime'] or '-'} | {t['net']:+.2f} | {t['krw']:+,.0f} |\n")
