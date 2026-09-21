# -*- coding: utf-8 -*-
"""§5 과최적화 검증 — LOO / L2O / 월제외 / 앞뒤 / 5분할 / WF6. 전부 KRW. READ-ONLY."""
import sys, pickle, itertools, statistics as st
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
TS = K.load()
M = K.MORNING
A = K.size_chain(TS, None)
MO3 = sorted(t["date"] for t in A if t["slot"] == 3 and t["session"] == M)
print(f"오전 slot3 대상 {len(MO3)}건: {', '.join(MO3)}\n")


AFT3 = float(sys.argv[1]) if len(sys.argv) > 1 else 1.00   # 1.00=사용자정의, 1.05=연구원안


def p2(skip=()):
    """skip 에 든 날짜는 오전 slot3 감액을 적용하지 않는다(현행 그대로)."""
    sk = set(skip)
    def fn(s, ss, dt=None):
        if s == 3 and ss == M:
            return 1.0 if dt in sk else 0.25
        if s == 3:
            return AFT3          # 오후 slot3
        return 1.05
    return fn


def upl(run, dates):
    return K.krw_pnl(run, dates) - K.krw_pnl(A, dates)


B = K.size_chain(TS, p2())


def wins(dates, k):
    n = len(dates) // k
    return [dates[i * n: (i + 1) * n if i < k - 1 else len(dates)] for i in range(k)]


print("=" * 100)
print("§5-A  Leave-One-Out  (8건 각각 하나씩 감액대상에서 제외)")
print("=" * 100)
print(f"{'제외한 날':11s} {'78d Δ KRW':>13s} {'30d Δ KRW':>13s} {'70d Δ':>13s} "
      f"{'PF':>7s} {'MDD%':>7s} {'복리Δ':>8s}")
print("-" * 100)
pfA, mddA = K.pf(A, D), K.mdd_krw(A, D)
rows = []
for d in MO3:
    r = K.size_chain(TS, p2(skip=[d]))
    rows.append((d, upl(r, D), upl(r, D[-30:]), upl(r, D[-70:]),
                 K.pf(r, D), K.mdd_krw(r, D),
                 K.compound_pct(r, D) - K.compound_pct(A, D)))
    print(f"{d:11s} {rows[-1][1]:+13,.0f} {rows[-1][2]:+13,.0f} {rows[-1][3]:+13,.0f} "
          f"{rows[-1][4]:7.3f} {rows[-1][5]:7.2f} {rows[-1][6]:+8.2f}")
print("-" * 100)
print(f"{'(전체 P2)':11s} {upl(B, D):+13,.0f} {upl(B, D[-30:]):+13,.0f} {upl(B, D[-70:]):+13,.0f} "
      f"{K.pf(B, D):7.3f} {K.mdd_krw(B, D):7.2f} {K.compound_pct(B, D)-K.compound_pct(A, D):+8.2f}")
print(f"  기준 A: PF={pfA:.3f} MDD={mddA:.2f}")
neg78 = [r for r in rows if r[1] <= 0]
neg30 = [r for r in rows if r[2] <= 0]
print(f"  LOO 78d 양수 {len(rows)-len(neg78)}/{len(rows)}   30d 양수 {len(rows)-len(neg30)}/{len(rows)}")
print(f"  LOO 78d 최악 {min(r[1] for r in rows):+,.0f} KRW   최선 {max(r[1] for r in rows):+,.0f} KRW")

print()
print("=" * 100)
print("§5-B  Leave-Two-Out  (28조합)")
print("=" * 100)
l2 = []
for a_, b_ in itertools.combinations(MO3, 2):
    r = K.size_chain(TS, p2(skip=[a_, b_]))
    l2.append(upl(r, D))
l2s = sorted(l2)
q = lambda p: l2s[min(len(l2s) - 1, int(p * len(l2s)))]
print(f"  조합 {len(l2)}개")
print(f"  최소 {l2s[0]:+,.0f} / p25 {q(0.25):+,.0f} / 중앙 {st.median(l2s):+,.0f} / "
      f"p75 {q(0.75):+,.0f} / 최대 {l2s[-1]:+,.0f}")
print(f"  음수 조합 {sum(1 for x in l2 if x <= 0)}/{len(l2)} = "
      f"{sum(1 for x in l2 if x <= 0)/len(l2)*100:.1f}%")

print()
print("=" * 100)
print("§5-C  월 제외")
print("=" * 100)
print(f"{'제외 월':10s} {'남은일수':>7s} {'Δ KRW':>13s} {'A KRW':>13s} {'P2 KRW':>13s}")
print("-" * 100)
for mm in ("202606", "202607", "202608", "202609"):
    W = [d for d in D if not d.startswith(mm)]
    print(f"{mm[:4]}-{mm[4:]:5s} {len(W):7d} {upl(B, W):+13,.0f} "
          f"{K.krw_pnl(A, W):13,.0f} {K.krw_pnl(B, W):13,.0f}")

print()
print("=" * 100)
print("§5-D/E/F  앞39/뒤39 · 5분할 · WF6")
print("=" * 100)
h = wins(D, 2)
print(f"  앞39일 Δ = {upl(B, h[0]):+,.0f} KRW    뒤39일 Δ = {upl(B, h[1]):+,.0f} KRW")
k5 = [upl(B, w) for w in wins(D, 5)]
print(f"  5분할 Δ  = {', '.join(f'{x:+,.0f}' for x in k5)}")
print(f"           양수 {sum(1 for x in k5 if x > 0)}/5")
w6 = [upl(B, w) for w in wins(D, 6)]
print(f"  WF6 Δ    = {', '.join(f'{x:+,.0f}' for x in w6)}")
print(f"           양수 {sum(1 for x in w6 if x > 0)}/6")
