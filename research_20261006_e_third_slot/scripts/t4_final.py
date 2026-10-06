"""85영업일(+10/06): A E(3회) vs B E-2SLOT vs R1(3번째 CHOP 차단). READ-ONLY.
B·R1 은 영향받는 날만 재생 결과로 바꾸고 나머지 날은 구조상 A 와 동일.
"""
import glob, json, os, sys
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
O = os.path.dirname(os.path.abspath(__file__))
SPD = os.path.dirname(O)
ST = SPD + "/wt_reftp/research_20261004_chop_staged"
src = open(ST + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ST + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
L = lambda p: json.load(open(p, encoding="utf-8"))

A = {os.path.basename(p)[11:19]: p for p in glob.glob(SPD + "/eprod/out_merged/REAL_EPROD_*.json")}
A["20261006"] = SPD + "/today1006/out/REAL_EPROD_20261006.json"
DAYS = sorted(A)
P = {"A": dict(A), "B": dict(A), "R1": dict(A)}
for p in glob.glob(SPD + "/slot2/out/CAP2_*.json"):
    P["B"][os.path.basename(p)[5:13]] = p
for p in glob.glob(O + "/out/R1_*.json"):
    P["R1"][os.path.basename(p)[3:11]] = p
T = {v: {d: sorted(trades(L(P[v][d])), key=lambda t: t["entry"]) for d in DAYS} for v in P}
pnl = {v: {d: sum(t["krw"] for t in T[v][d]) for d in DAYS} for v in P}

third_days = [d for d in DAYS if len(T["A"][d]) >= 3]
missing = [d for d in third_days if not os.path.basename(P["B"][d]).startswith("CAP2_")]
print(f"# {len(DAYS)}일 · A 3번째 거래일 {len(third_days)}일 · B 재생 누락 {missing or '없음'}")
if missing:
    sys.exit(1)
# B 경로 점검: 3번째만 빠졌는가
path = []
for d in third_days:
    dd = pnl["B"][d] - pnl["A"][d] + T["A"][d][2]["krw"]
    if abs(dd) > 0.5:
        path.append((d, round(dd)))
print(f"# B 경로변화(3번째 제거 외) {len(path)}일: {path}")
over = [d for v in P for d in DAYS
        if sum(x['qty'] * x['px'] for x in L(P[v][d])['orders'] if x['side'] == 'BUY') > 30_000_000 * 1.001]
print(f"# 일 3,000만원 초과 {len(over)}일")


def met(days, v):
    dv = np.array([pnl[v][d] for d in days], dtype=float)
    k = np.array([t["krw"] for d in days for t in T[v][d]], dtype=float)
    r = dv / 1e7 * 100
    eq = np.cumprod(1 + r / 100)
    mdd = (eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100
    return dict(krw=dv.sum(), pf=k[k > 0].sum() / max(-k[k < 0].sum(), 1), mdd=mdd, dmin=dv.min(),
                lossd=int((dv < 0).sum()), n=len(k))


NM = {"A": "A E (3회)", "B": "B E-2SLOT", "R1": "R1 3번째 CHOP 차단"}
SPLITS = (("85영업일 + 10/06 전체", DAYS), ("6~7월", [d for d in DAYS if d < "20260801"]),
          ("8~10월", [d for d in DAYS if d >= "20260801"]), ("9월", [d for d in DAYS if d.startswith("202609")]))
for lab, days in SPLITS:
    print(f"\n## {lab} ({len(days)}일)")
    print("| | 총손익 | PF | MDD% | 최대1일손실 | 손실일 | 거래수 |\n|---|---|---|---|---|---|---|")
    for v in P:
        m = met(days, v)
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['pf']:.2f} | {m['mdd']:.2f} | {m['dmin']:+,.0f} | {m['lossd']} | {m['n']} |")
    a = met(days, "A")["krw"]
    print(f"  B−A {met(days, 'B')['krw'] - a:+,.0f} · R1−A {met(days, 'R1')['krw'] - a:+,.0f}")

for v in ("B", "R1"):
    gains = sorted([pnl[v][d] - pnl["A"][d] for d in DAYS], reverse=True)
    tot = sum(gains)
    losses = sorted(gains)
    print(f"\n# {NM[v]}: 순효과 {tot:+,.0f} · 개선 상위1일 제외 {tot - gains[0]:+,.0f} · 상위2일 {tot - sum(gains[:2]):+,.0f}"
          f" · 악화 상위1일 제외 {tot - losses[0]:+,.0f}")
    rng = np.random.default_rng(0)
    diffs = np.array([pnl[v][d] - pnl["A"][d] for d in DAYS])
    bs = np.array([rng.choice(diffs, len(diffs)).sum() for _ in range(20000)])
    print(f"  일 부트스트랩 P({v} > A) {(bs > 0).mean() * 100:.1f}%")
