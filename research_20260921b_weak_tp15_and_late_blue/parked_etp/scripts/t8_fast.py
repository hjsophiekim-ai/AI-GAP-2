# -*- coding: utf-8 -*-
"""§8/§10/§11/§12/§13 — 사전계산 기반 고속판. READ-ONLY.

threshold 가 고정이면 각 거래의 TP15 결과(도달시각/체결가/새 net)는 regime 과
무관하게 하나로 정해진다. 한 번만 계산해 두고, 이후에는 '어느 거래에 적용할지'
집합만 바꾼다. 결과는 t3_tp15.simulate 와 동일해야 한다(아래에서 assert).
"""
import sys, pickle, itertools, statistics as st
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t2_regime as RG, t3_tp15 as T

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
TS = K.load(); A = K.size_chain(TS, None); A78 = K.krw_pnl(A, D)
rng = np.random.default_rng(20260921)
wins = lambda ds, k: [ds[i*(len(ds)//k):(i+1)*(len(ds)//k) if i < k-1 else len(ds)] for i in range(k)]
KEY = lambda t: (t["date"], t["entry_time"])


def precompute(thr, **kw):
    """모든 거래에 TP15 를 적용한 결과 1회 계산 -> key: 바뀐 필드."""
    full = T.simulate(TS, lambda t: True, thr, **kw)
    return {KEY(x): x for x in full if x["tp15_fired"]}


def apply(sel, pre):
    """sel(키 집합)에 속한 거래만 TP15 결과로 치환."""
    out = []
    for t in TS:
        k = KEY(t)
        if k in sel and k in pre:
            p = pre[k]
            r = dict(t)
            r.update(net_pct=p["net_pct"], exit_time=p["exit_time"],
                     exit_price=p["exit_price"], exit_reason=p["exit_reason"])
            out.append(r)
        else:
            out.append(dict(t))
    return K.size_chain(out, None)


PRE = precompute(1.50)
R3KEYS = {KEY(t) for t in TS if RG.R3(t)}
R3 = apply(R3KEYS, PRE)
ref = K.size_chain(T.simulate(TS, RG.R3, 1.50), None)
assert abs(K.krw_pnl(R3, D) - K.krw_pnl(ref, D)) < 1e-6, "고속판 불일치"
print(f"고속판 검증 OK — R3 78d = {K.krw_pnl(R3, D):,.0f} (느린판과 일치)")
u = lambda W, r=R3: K.krw_pnl(r, W) - K.krw_pnl(A, W)
fired = [k for k in R3KEYS if k in PRE]
fdays = sorted({k[0] for k in fired})
wkdays = sorted({t["date"] for t in TS if KEY(t) in R3KEYS})
print(f"R3: weak 거래 {len(R3KEYS)} / 발동 {len(fired)} / 발동일 {len(fdays)} / weak일 {len(wkdays)}")
print(f"78d uplift {u(D):+,.0f} KRW ({u(D)/A78*100:+.2f}%)\n")

print("=" * 92); print("§8  기간 분할"); print("=" * 92)
h = wins(D, 2); k5 = [u(w) for w in wins(D, 5)]; w6 = [u(w) for w in wins(D, 6)]
print(f"  30d {u(D[-30:]):+,.0f} / 70d {u(D[-70:]):+,.0f} / 78d {u(D):+,.0f}")
print(f"  앞39 {u(h[0]):+,.0f} / 뒤39 {u(h[1]):+,.0f}")
print("  월제외 " + " | ".join(f"{m[4:]}월 {u([d for d in D if not d.startswith(m)]):+,.0f}"
                              for m in ("202606","202607","202608","202609")))
print(f"  5분할 {', '.join(f'{x:+,.0f}' for x in k5)} -> {sum(1 for x in k5 if x>0)}/5")
print(f"  WF6   {', '.join(f'{x:+,.0f}' for x in w6)} -> {sum(1 for x in w6 if x>0)}/6")

print("\n" + "=" * 92); print("§10  의존성"); print("=" * 92)
d = sorted((y["pnl_krw"]-x["pnl_krw"] for x, y in zip(A, R3)
            if abs(y["pnl_krw"]-x["pnl_krw"]) > 1e-6), reverse=True)
tot = sum(d)
print(f"  손익 달라진 거래 {len(d)} / 순 {tot:+,.0f} | Top1 {d[0]/tot*100:.1f}% Top3 {sum(d[:3])/tot*100:.1f}%")
for k in (1,3,5):
    print(f"  -Top{k} 후 uplift {tot-sum(d[:k]):+,.0f}")
loo = [u(D, apply(R3KEYS - {k}, PRE)) for k in fired]
print(f"  LOO(발동 {len(fired)}건) 양수 {sum(1 for x in loo if x>0)}/{len(loo)} "
      f"최소 {min(loo):+,.0f} 중앙 {st.median(loo):+,.0f}")
l2 = [u(D, apply(R3KEYS - set(c), PRE)) for c in itertools.combinations(fired, 2)]
neg = sum(1 for x in l2 if x <= 0)
print(f"  L2O {len(l2)}조합 최소 {min(l2):+,.0f} 중앙 {st.median(l2):+,.0f} "
      f"음수 {neg}/{len(l2)} = {neg/len(l2)*100:.1f}%")

print("\n" + "=" * 92); print("§12  부트스트랩 10,000회"); print("=" * 92)
for tag, v in (("일단위", np.array([K.daily_krw(R3,D)[x]-K.daily_krw(A,D)[x] for x in D])),
               ("거래단위", np.array([y["pnl_krw"]-x["pnl_krw"] for x, y in zip(A, R3)]))):
    bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
    print(f"  [{tag}] P(>0)={(bt>0).mean()*100:6.2f}% p5={np.percentile(bt,5):+,.0f} "
          f"중앙={np.percentile(bt,50):+,.0f} p95={np.percentile(bt,95):+,.0f} "
          f"P(<=0)={(bt<=0).mean()*100:.2f}%")

print("\n" + "=" * 92); print("§11  placebo 5,000회"); print("=" * 92)
real = u(D)
alld = sorted({t["date"] for t in TS})
allk = [KEY(t) for t in TS]
N = 5000
pl = np.empty(N)
for i in range(N):
    days = {alld[j] for j in rng.choice(len(alld), len(wkdays), replace=False)}
    pl[i] = u(D, apply({k for k in allk if k[0] in days}, PRE))
p1 = (pl < real).mean()*100
print(f"  [임의 {len(wkdays)}일을 weak 지정] 실제 {real:+,.0f} | p50 {np.percentile(pl,50):+,.0f} "
      f"p95 {np.percentile(pl,95):+,.0f} | percentile {p1:.2f}% 단측p {(pl>=real).mean():.4f} "
      f"{'통과' if p1>=95 else '미달'}")
skeys = [k for k in allk if k not in R3KEYS]
pl2 = np.empty(N)
for i in range(N):
    pick = {skeys[j] for j in rng.choice(len(skeys), len(R3KEYS), replace=False)}
    pl2[i] = u(D, apply(pick, PRE))
p2 = (pl2 < real).mean()*100
print(f"  [strong 에서 임의 {len(R3KEYS)}거래] p50 {np.percentile(pl2,50):+,.0f} "
      f"p95 {np.percentile(pl2,95):+,.0f} | percentile {p2:.2f}% {'통과' if p2>=95 else '미달'}")

print("\n" + "=" * 92); print("§13  체결 스트레스"); print("=" * 92)
print(f"  {'시나리오':24s} {'78d KRW':>13s} {'uplift':>12s}")
for tag, kw in (("기준(도달봉 종가)", {}), ("도달가 즉시체결(낙관)", dict(fill="touch")),
                ("다음봉 종가(1분지연)", dict(fill="next_close")),
                ("+0.05% 슬립", dict(slip_pct=0.05)), ("+0.10% 슬립", dict(slip_pct=0.10)),
                ("+0.20% 슬립", dict(slip_pct=0.20)), ("3봉 지연", dict(delay_bars=3)),
                ("다음봉+0.10% 슬립", dict(fill="next_close", slip_pct=0.10))):
    r = apply(R3KEYS, precompute(1.50, **kw))
    print(f"  {tag:24s} {K.krw_pnl(r, D):13,.0f} {K.krw_pnl(r, D)-A78:+12,.0f}")
