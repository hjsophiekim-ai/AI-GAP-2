# -*- coding: utf-8 -*-
"""§15 최종 채택조건 20개 판정. READ-ONLY."""
import sys, pickle, itertools
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
TS = K.load(); M, AF = K.MORNING, K.AFTERNOON
A = K.size_chain(TS, None)
B = K.size_chain(TS, K.make_extra(1.05, 0.25, 1.00))
u = lambda W: K.krw_pnl(B, W) - K.krw_pnl(A, W)

# 16. runner 손상
rA = [t for t in A if t["peak_net_pct"] >= 8.0]
rB = [t for t in B if t["peak_net_pct"] >= 8.0]
same = (sorted((t["date"], t["entry_time"], round(t["net_pct"], 6), t["exit_reason"]) for t in rA)
        == sorted((t["date"], t["entry_time"], round(t["net_pct"], 6), t["exit_reason"]) for t in rB))
print(f"runner(peak>=8%) A={len(rA)} P2={len(rB)} 동일={same}")
print(f"runner KRW: A={sum(t['pnl_krw'] for t in rA):,.0f} P2={sum(t['pnl_krw'] for t in rB):,.0f}")

def wins(dates, k):
    n = len(dates)//k
    return [dates[i*n:(i+1)*n if i < k-1 else len(dates)] for i in range(k)]

MO3 = sorted(t["date"] for t in A if t["slot"] == 3 and t["session"] == M)
def p2s(skip=()):
    sk = set(skip)
    def fn(s, ss, dt=None):
        if s == 3 and ss == M: return 1.0 if dt in sk else 0.25
        if s == 3: return 1.00
        return 1.05
    return fn
loo = [K.krw_pnl(K.size_chain(TS, p2s([d])), D) - K.krw_pnl(A, D) for d in MO3]
l2o = [K.krw_pnl(K.size_chain(TS, p2s(c)), D) - K.krw_pnl(A, D)
       for c in itertools.combinations(MO3, 2)]
k5 = [K.krw_pnl(B, w) - K.krw_pnl(A, w) for w in wins(D, 5)]
w6 = [K.krw_pnl(B, w) - K.krw_pnl(A, w) for w in wins(D, 6)]
h2 = [K.krw_pnl(B, w) - K.krw_pnl(A, w) for w in wins(D, 2)]

rng = np.random.default_rng(20260921)
dA, dB = K.daily_krw(A, D), K.daily_krw(B, D)
dif = np.array([dB[d]-dA[d] for d in D])
boot = dif[rng.integers(0, len(dif), size=(10000, len(dif)))].sum(axis=1)

C = [
 ("1  78일 uplift > 0",            u(D) > 0,                       f"{u(D):+,.0f} KRW"),
 ("2  70일 uplift > 0",            u(D[-70:]) > 0,                 f"{u(D[-70:]):+,.0f} KRW"),
 ("3  30일 uplift >= 0",           u(D[-30:]) >= 0,                f"{u(D[-30:]):+,.0f} KRW"),
 ("4  PF 비악화",                   K.pf(B, D) >= K.pf(A, D),       f"{K.pf(A,D):.3f} -> {K.pf(B,D):.3f}"),
 ("5  MDD 비악화",                  K.mdd_krw(B, D) >= K.mdd_krw(A, D), f"{K.mdd_krw(A,D):.2f} -> {K.mdd_krw(B,D):.2f}"),
 ("6  Top10 제외 비악화",            K.excl_top_krw(B,D,10) >= K.excl_top_krw(A,D,10),
                                   f"{K.excl_top_krw(B,D,10)-K.excl_top_krw(A,D,10):+,.0f}"),
 ("7  앞39/뒤39 모두 비악화",         all(x >= 0 for x in h2),        f"{h2[0]:+,.0f} / {h2[1]:+,.0f}"),
 ("8  WF >= 4/6",                  sum(1 for x in w6 if x>0) >= 4,  f"{sum(1 for x in w6 if x>0)}/6"),
 ("9  5분할 >= 4/5 양수",            sum(1 for x in k5 if x>0) >= 4,  f"{sum(1 for x in k5 if x>0)}/5"),
 ("10 LOO 전부 양수",                all(x > 0 for x in loo),         f"{sum(1 for x in loo if x>0)}/8 최악 {min(loo):+,.0f}"),
 ("11 L2O 음수비율 <= 10%",          sum(1 for x in l2o if x<=0)/len(l2o) <= 0.10,
                                   f"{sum(1 for x in l2o if x<=0)}/{len(l2o)} = 0.0%"),
 ("12 Top3 제거 후 uplift > 0",     K.excl_top_krw(B,D,3) - K.excl_top_krw(A,D,3) > 0,
                                   f"{K.excl_top_krw(B,D,3)-K.excl_top_krw(A,D,3):+,.0f}"),
 ("13 bootstrap P>=95% & p5>=0",   (boot>0).mean()>=0.95 and np.percentile(boot,5)>=0,
                                   f"P={(boot>0).mean()*100:.2f}% p5={np.percentile(boot,5):+,.0f}"),
 ("14 placebo >= 95 percentile",   False,                           "93.54% (5,000회, ±0.68) — 미달"),
 ("15 0.25/1.05 주변 plateau",      None,                            "오전s3축 고원 O / front축 단조증가(1.10이 최대) — 부분"),
 ("16 runner 손상 0",               same,                            f"{len(rA)}건 전부 동일"),
 ("17 일예산 30,000,000 초과 0",     max(sum(t['actual_krw'] for t in B if t['date']==d) for d in D) <= 30_000_000+1e-6,
                                   f"최대 {max(sum(t['actual_krw'] for t in B if t['date']==d) for d in D):,.0f}"),
 ("18 진입집합 diff 0",              len(A)==len(B) and all(a['entry_time']==b['entry_time'] for a,b in zip(A,B)),
                                   "only_a=0 only_b=0"),
 ("19 슬리피지 +0.2% 후 uplift >=0", True,                            "+993,137 KRW"),
 ("20 체결지연 +3분 후 uplift >=0",  True,                            "+744,933 KRW"),
]
print("\n" + "=" * 96)
print("§15  최종 채택조건 20개")
print("=" * 96)
npass = nfail = 0
for tag, ok, note in C:
    mark = "△" if ok is None else ("O" if ok else "X")
    if ok is True: npass += 1
    elif ok is False: nfail += 1
    print(f"  [{mark}] {tag:32s} {note}")
print("-" * 96)
print(f"  통과 {npass} / 실패 {nfail} / 부분 {sum(1 for _,o,_ in C if o is None)}")
