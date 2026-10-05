"""실시간 WHIPSAW SCORE 검증 2단계 — 예측력이 실재하는가. READ-ONLY.
train 05-27~07-31 / test 08-01~10-01 시간분할. 10/02 는 별도 관찰용.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
rng = np.random.default_rng(3)
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]

F = pd.read_csv(ROOT + "/ws_scores.csv")
F["day"] = F["day"].astype(str)
F["t"] = pd.to_datetime(F["t"])
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})


def apath(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


TR = []
for d in DAYS + ["20261002"]:
    for t in trades(json.load(open(apath(d), encoding="utf-8"))):
        rs = "+".join(dict.fromkeys(t["reasons"]))
        TR.append(dict(day=d, entry=t["entry"], krw=t["krw"], net=t["net"], rs=rs,
                       sl=int("STOP_LOSS" in rs or rs == "B3_SL"), run=int("TP2_FULL" in rs)))
T = pd.DataFrame(TR)
T["t"] = pd.to_datetime(T["entry"])
print(f"# 격자 {len(F)}개 · A 거래 {len(T)}건")

# ── 1. 격자 수준: score vs 이후 30분 실현손익 ──────────────────────────
fwd = []
for _, g in F.groupby("day"):
    d = g["day"].iloc[0]
    tt = T[T.day == d]
    for _, r in g.iterrows():
        m = tt[(tt.t > r["t"]) & (tt.t <= r["t"] + pd.Timedelta(minutes=30))]
        fwd.append(dict(day=d, t=r["t"], score=r["score"], n=len(m), krw=m.krw.sum()))
FW = pd.DataFrame(fwd)
FW["seg"] = np.where(FW.day < "20260801", "train", np.where(FW.day <= "20261001", "test", "1002"))


def spear(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 5 or np.std(a) == 0 or np.std(b) == 0:
        return np.nan
    ra, rb = pd.Series(a).rank().to_numpy(), pd.Series(b).rank().to_numpy()
    return float(np.corrcoef(ra, rb)[0, 1])


print("\n## 1. 격자 수준 — score 가 높을수록 이후 30분 손익이 나빠지는가")
print("| 구간 | 격자수 | 진입 있는 격자 | 순위상관(전체) | 순위상관(진입격자만) |")
print("|---|---|---|---|---|")
for seg in ("train", "test"):
    z = FW[FW.seg == seg]
    zz = z[z.n > 0]
    print(f"| {seg} | {len(z)} | {len(zz)} | {spear(z.score, z.krw):+.3f} | {spear(zz.score, zz.krw):+.3f} |")
print("  (음수여야 '점수 높을수록 손익 나쁨'을 뜻한다)")

# ── 2. 거래 수준: 진입 시점 score ─────────────────────────────────────
S = F.set_index(["day", "t"])["score"]
sc = []
for _, r in T.iterrows():
    g = F[(F.day == r["day"]) & (F.t <= r["t"])]
    sc.append(g["score"].iloc[-1] if len(g) else np.nan)
T["score"] = sc
T = T.dropna(subset=["score"])
T["seg"] = np.where(T.day < "20260801", "train", np.where(T.day <= "20261001", "test", "1002"))
BINS = [0, 40, 60, 75, 101]
LAB = ["0-39", "40-59", "60-74", "75-100"]
T["bucket"] = pd.cut(T.score, BINS, right=False, labels=LAB)
print(f"\n## 2. 거래 수준 — 진입 시점 score 구간별 (A 거래 {len(T)}건 중 점수 산출 가능분)")
for seg in ("train", "test"):
    z = T[T.seg == seg]
    print(f"\n### {seg} ({len(z)}건, {z.krw.sum():+,.0f}원)")
    print("| score | 거래수 | 평균손익 | 승률% | 손절률% | runner율% |")
    print("|---|---|---|---|---|---|")
    for b in LAB:
        y = z[z.bucket == b]
        if not len(y):
            print(f"| {b} | 0 | — | — | — | — |")
            continue
        print(f"| {b} | {len(y)} | {y.krw.mean():+,.0f} | {(y.krw>0).mean()*100:.1f} | "
              f"{y.sl.mean()*100:.1f} | {y.run.mean()*100:.1f} |")


def auc(y, s):
    y = np.asarray(y, float); s = np.asarray(s, float)
    if y.sum() == 0 or y.sum() == len(y):
        return np.nan
    r = pd.Series(s).rank().to_numpy()
    n1, n0 = y.sum(), len(y) - y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


print("\n## 3. 판별력 (AUC) · 순위상관")
print("| 구간 | AUC(손절) | AUC(runner) | 순위상관(score, 거래손익) |")
print("|---|---|---|---|")
for seg in ("train", "test"):
    z = T[T.seg == seg]
    print(f"| {seg} | {auc(z.sl, z.score):.3f} | {auc(z.run, z.score):.3f} | {spear(z.score, z.krw):+.3f} |")
zt = T[T.seg == "test"]
a = auc(zt.sl, zt.score)
null = np.array([auc(rng.permutation(zt.sl.to_numpy(float)), zt.score.to_numpy()) for _ in range(2000)])
print(f"  test 손절 AUC 순열검정 p = {(np.abs(null-.5) >= abs(a-.5)).mean():.4f}")

# ── 4. 10/02 시간대별 ────────────────────────────────────────────────
print("\n## 4. 10/02 장중 score")
z = F[F.day == "20261002"].sort_values("t")
print("| 시각 | score | PE30 | RNG30 | XC | flip | swing |")
print("|---|---|---|---|---|---|---|")
for _, r in z.iterrows():
    if r["t"].minute % 15 == 0:
        print(f"| {r['t']:%H:%M} | **{r['score']:.0f}** | {r['pe30']:.2f} | {r['rng30']:.2f} | "
              f"{int(r['xc30'])} | {int(r['flip'])} | {r['swing']:.2f} |")
e = T[(T.day == "20261002")]
print("\n  10/02 진입 시점 score:")
for _, r in e.iterrows():
    print(f"    {r['t']:%H:%M} score {r['score']:.0f} ({r['bucket']}) · {r['rs']} {r['krw']:+,.0f}")
print(f"  10/02 일중 최고 score {z.score.max():.0f} · HIGH(>=75) 격자 {int((z.score>=75).sum())}개 / {len(z)}")

# ── 5. false alert ──────────────────────────────────────────────────
print("\n## 5. false alert — '정상일'에 HIGH 가 뜨는가")
dd = T[T.seg != "1002"].groupby("day").agg(krw=("krw", "sum"), sl=("sl", "sum"), run=("run", "sum"))
hi = F[F.day.isin(dd.index)].groupby("day")["score"].agg(mx="max", n75=lambda s: int((s >= 75).sum()))
J = dd.join(hi)
norm = J[(J.krw > 0) & (J.sl == 0)]
chop = J[(J.krw < 0) & (J.sl >= 2)]
print(f"  정상 추세일(일손익>0 ∧ 손절 0건) {len(norm)}일 중 HIGH(>=75) 발생 **{int((norm.n75>0).sum())}일** "
      f"({(norm.n75>0).mean()*100:.0f}%) · 평균 HIGH 격자 {norm.n75.mean():.1f}개")
print(f"  왕복 손실일(일손익<0 ∧ 손절>=2건) {len(chop)}일 중 HIGH 발생 **{int((chop.n75>0).sum())}일** "
      f"({(chop.n75>0).mean()*100:.0f}%) · 평균 HIGH 격자 {chop.n75.mean():.1f}개")
print(f"  러너일(runner>=1) {int((J.run>=1).sum())}일 중 HIGH 발생 {int((J[J.run>=1].n75>0).sum())}일")
print(f"\n  일 최고 score 평균: 정상일 {norm.mx.mean():.1f} · 왕복손실일 {chop.mx.mean():.1f}")
T.to_csv(ROOT + "/ws_trades_scored.csv", index=False, encoding="utf-8")
FW.to_csv(ROOT + "/ws_grid_fwd.csv", index=False, encoding="utf-8")
