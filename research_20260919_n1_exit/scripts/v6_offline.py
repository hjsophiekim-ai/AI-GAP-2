"""V6 — 경로 기반 오프라인 재현으로 placebo 10,000회 x3 + bootstrap 10,000회 x2
       + LOO / Top-N 제거.

full-chain 1회가 ~35초라 10,000회는 불가능하다. 대신 N1 각 포지션의 **완성봉 경로**를
덤프해 두고 오프라인에서 청산시점만 갈아끼운다. 이 근사가 타당한지는 먼저
**실제 C1 을 오프라인으로 재현해 full-chain 결과와 대조**해서 확인한다
(C1 은 진입집합을 바꾸지 않으므로 피드백이 없다 — parity diff 0 으로 이미 확인).
"""
import pickle, sys
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
from common import summarize

RNG = np.random.default_rng(20260919)
c = A.ctx(78); D = c.dates
cfg, po, q3, h50 = A.SPEC["N1"]
V1 = pickle.load(open(A.HERE / "v1.pkl", "rb"))
base, c1 = V1["base"], V1["c1"]
key = lambda t: (t["date"], t["entry_time"])
B = {key(t): t for t in base}; K = {key(t): t for t in c1}

paths = []
_ = A.run("N1", c, D, paths=paths); A.save()
P = {}
for r in paths:
    P.setdefault((r["date"], r["entry_time"]), []).append(r)
print(f"경로 덤프: {len(P)} 포지션 / {len(paths)} 완성봉 레코드")

ORDER = sorted(B, key=lambda k: B[k]["exit_time"])
W30 = set(D[-30:])
ARM, GIVE = 5.0, 1.5


def net_of(rec):
    return rec["realized"] + rec["qty"] * rec["net"]


def replay(fire_at):
    """fire_at: {key: path_index} — 그 봉에서 청산. 없으면 N1 그대로."""
    out = {}
    for k in ORDER:
        if k in fire_at:
            out[k] = net_of(P[k][fire_at[k]]) * B[k]["w1a"]
        else:
            out[k] = B[k]["net_pct"] * B[k]["w1a"]
    return out


def comp(pnls, keys=None):
    eq = 1.0
    for k in (keys if keys is not None else ORDER):
        eq *= (1 + pnls[k] / 100.0)
    return (eq - 1) * 100.0


BASE_P = {k: B[k]["net_pct"] * B[k]["w1a"] for k in ORDER}
BASE78 = comp(BASE_P)
BASE30 = comp(BASE_P, [k for k in ORDER if k[0] in W30])

# ── 실제 C1 오프라인 재현 ────────────────────────────────────────────────
real_fire = {}
armed_bars = {}          # key -> [path idx] (peak>=ARM 인 봉)
for k, pl in P.items():
    ab = [i for i, r in enumerate(pl) if r["peak"] >= ARM]
    if ab:
        armed_bars[k] = ab
    for i in ab:
        r = pl[i]
        if r["gap"] <= 0 and r["net"] <= r["peak"] - GIVE:
            real_fire[k] = i
            break
RP = replay(real_fire)
off78, off30 = comp(RP), comp(RP, [k for k in ORDER if k[0] in W30])
true78 = summarize(c1, D)["compound_pct"]
true30 = summarize([t for t in c1 if t["date"] in W30], D[-30:])["compound_pct"]
print(f"\n오프라인 재현 검증")
print(f"  발동 {len(real_fire)}건 (full-chain {sum(1 for t in c1 if t.get('pp_fired'))}건), "
      f"동일 거래: {set(real_fire) == {k for k in K if K[k].get('pp_fired')}}")
print(f"  78일 오프라인 {off78:.4f} vs full-chain {true78:.4f}  차이 {off78-true78:+.4f}")
print(f"  30일 오프라인 {off30:.4f} vs full-chain {true30:.4f}  차이 {off30-true30:+.4f}")
REAL_U78, REAL_U30 = off78 - BASE78, off30 - BASE30
print(f"  uplift 78일 {REAL_U78:+.4f} / 30일 {REAL_U30:+.4f}")

# ── 6. Top-N 제거 / LOO ──────────────────────────────────────────────────
print(f"\n=== 6. Top-event 의존성 (발동 {len(real_fire)}건) ===")
deltas = sorted(((k, RP[k] - BASE_P[k]) for k in real_fire), key=lambda x: -x[1])
print(f"  {'일자':10s} {'진입':6s} {'Δ(w1a)':>9s} {'이 1건 제거시 78일 uplift':>26s} {'30일':>9s}")
for k, d in deltas:
    f2 = {x: v for x, v in real_fire.items() if x != k}
    rp = replay(f2)
    print(f"  {k[0]:10s} {str(k[1])[11:16]:6s} {d:+9.3f} "
          f"{comp(rp)-BASE78:+26.2f} {comp(rp, [x for x in ORDER if x[0] in W30])-BASE30:+9.2f}")
for n in (1, 2, 3):
    f2 = {x: v for x, v in real_fire.items() if x not in {k for k, _ in deltas[:n]}}
    rp = replay(f2)
    print(f"  Top{n} 제거 → 78일 uplift {comp(rp)-BASE78:+8.2f}  "
          f"30일 {comp(rp, [x for x in ORDER if x[0] in W30])-BASE30:+7.2f}  "
          f"(단순합 {sum(d for _, d in deltas[n:]):+.2f}%p)")

# ── 9. 부트스트랩 ────────────────────────────────────────────────────────
print("\n=== 9. 부트스트랩 10,000회 ===")
pb = np.array([BASE_P[k] for k in ORDER]); pc = np.array([RP[k] for k in ORDER])
n = len(ORDER)
idx = RNG.integers(0, n, size=(10000, n))
cb = np.expm1(np.log1p(pb[idx] / 100).sum(axis=1)) * 100
cc = np.expm1(np.log1p(pc[idx] / 100).sum(axis=1)) * 100
u = cc - cb
print(f"  거래단위: C1>N1 확률 {100*(u>0).mean():.2f}%   uplift<=0 확률 {100*(u<=0).mean():.2f}%")
print(f"            p5 {np.percentile(u,5):+.2f}  중앙값 {np.percentile(u,50):+.2f}  "
      f"p95 {np.percentile(u,95):+.2f}")
days = sorted({k[0] for k in ORDER})
byday = {d_: [k for k in ORDER if k[0] == d_] for d_ in days}
di = RNG.integers(0, len(days), size=(10000, len(days)))
ub = []
for row in di:
    eb = ec = 0.0
    for j in row:
        for k in byday[days[j]]:
            eb += np.log1p(BASE_P[k] / 100); ec += np.log1p(RP[k] / 100)
    ub.append((np.expm1(ec) - np.expm1(eb)) * 100)
ub = np.array(ub)
print(f"  일단위  : C1>N1 확률 {100*(ub>0).mean():.2f}%   uplift<=0 확률 {100*(ub<=0).mean():.2f}%")
print(f"            p5 {np.percentile(ub,5):+.2f}  중앙값 {np.percentile(ub,50):+.2f}  "
      f"p95 {np.percentile(ub,95):+.2f}")

# ── 10. Placebo 1,000회 x3 ───────────────────────────────────────────────
print("\n=== 10. Placebo (각 1,000회, 발동빈도 동일 조건) ===")
all_armed = sorted(armed_bars)
p_gapneg = float(np.mean([P[k][i]["gap"] <= 0 for k in all_armed for i in armed_bars[k]]))
print(f"  arm 이후 완성봉 {sum(len(v) for v in armed_bars.values())}개 중 "
      f"gap<=0 비율 p={p_gapneg:.4f}, 무장 포지션 {len(all_armed)}개")


def pctile(name, arr):
    arr = np.array(arr)
    pr = 100.0 * (arr < REAL_U78).mean()
    print(f"  {name:28s} 평균 {arr.mean():+8.2f}  p50 {np.percentile(arr,50):+8.2f}  "
          f"p95 {np.percentile(arr,95):+8.2f}  최대 {arr.max():+8.2f}  "
          f"실제 백분위 {pr:6.2f}%  위약>=실제 {int((arr>=REAL_U78).sum())}/{len(arr)}")


# (a) gap 부호 랜덤
res = []
for _ in range(1000):
    f = {}
    for k in all_armed:
        for i in armed_bars[k]:
            r = P[k][i]
            if RNG.random() < p_gapneg and r["net"] <= r["peak"] - GIVE:
                f[k] = i; break
    res.append(comp(replay(f)) - BASE78)
pctile("(a) gap 부호 랜덤", res)

# (b) 발동시각 랜덤 (무장 포지션 중 7개 무작위 x 무작위 봉)
NF = len(real_fire)
res = []
for _ in range(1000):
    ks = RNG.choice(len(all_armed), size=NF, replace=False)
    f = {all_armed[j]: int(RNG.choice(armed_bars[all_armed[j]])) for j in ks}
    res.append(comp(replay(f)) - BASE78)
pctile("(b) 발동시각 랜덤(7건)", res)

# (c) 실제 발동 거래 내 임의 완성봉
res = []
for _ in range(1000):
    f = {k: int(RNG.choice(armed_bars[k])) for k in real_fire}
    res.append(comp(replay(f)) - BASE78)
pctile("(c) 동일거래 내 임의봉", res)

pickle.dump({"P": P, "real_fire": real_fire, "BASE_P": BASE_P, "RP": RP,
             "ORDER": ORDER, "armed": armed_bars}, open(A.HERE / "v6.pkl", "wb"))
print("\n저장: v6.pkl")
