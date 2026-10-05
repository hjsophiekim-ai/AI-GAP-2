"""BOX BREAKOUT SCORE 검증. READ-ONLY. production 무수정, 매매로직 변경 없음.

BOX   = t 기준 [t-40분, t-10분) 의 30개 완성 1분봉 고가/저가 (확립된 박스)
최근구간 = [t-10분, t] 의 10봉 (탈출/복귀를 재는 구간)
방향  = 거래는 그 거래의 방향, 격자는 박스 중앙 대비 현재가 방향.
미래 데이터 미사용. 가중치는 train(05-27~07-31) 로지스틱 1개로만 결정하고 test 고정.
brute-force 임계 탐색 없음.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
rng = np.random.default_rng(23)
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
DATA = REPO + "/research_20261002_sept_strategy_replay/data"
KST = "Asia/Seoul"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
PREV = {}
for l in open(O3 + "/../jobs4.txt"):
    p = l.split()
    if len(p) >= 5 and p[1] == "A":
        PREV[p[2]] = (p[3], p[4])
PREV.setdefault("20261002", ("20261001", "20260930"))
_c = {}


def hy(day):
    if day not in _c:
        parts = []
        for x in (*PREV.get(day, ()), day):
            try:
                d = pd.read_csv(f"{DATA}/replay_{x}_hynix_1m.csv")
                d["datetime"] = pd.to_datetime(d["datetime"].astype(str).str[:19]).dt.tz_localize(KST)
                parts.append(d)
            except FileNotFoundError:
                pass
        _c[day] = pd.concat(parts).drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
    return _c[day]


def apath(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


FEATS = ["box_w", "pos", "brk_dist", "out_bars", "reentry", "swing_new", "ema_slope", "gap_dir"]


def feats(day, t, up=None):
    d = hy(day)
    done = d[d["datetime"] + pd.Timedelta(minutes=1) <= t]
    today = done[done["datetime"].dt.date == t.date()]
    if len(done) < 120 or len(today) < 40:
        return None
    box = done.iloc[-40:-10]
    rec = done.iloc[-10:]
    hi0, lo0 = float(box["high"].max()), float(box["low"].min())
    px = float(done["close"].iloc[-1])
    if hi0 <= lo0:
        return None
    mid = (hi0 + lo0) / 2
    if up is None:
        up = px >= mid
    s = 1.0 if up else -1.0
    f = {}
    f["box_w"] = (hi0 - lo0) / px * 100                                   # ① 박스 폭
    f["pos"] = ((px - lo0) if up else (hi0 - px)) / (hi0 - lo0)           # ② 박스 내 위치
    f["brk_dist"] = ((px - hi0) if up else (lo0 - px)) / px * 100         # ③ 돌파 거리(음수=내부)
    edge = hi0 if up else lo0
    cl = rec["close"].to_numpy(float)
    f["out_bars"] = float(np.sum(cl > edge) if up else np.sum(cl < edge)) # ④ 박스 밖 유지 봉수
    went = (float(rec["high"].max()) > hi0) if up else (float(rec["low"].min()) < lo0)
    inside = (px <= hi0) if up else (px >= lo0)
    f["reentry"] = 1.0 if (went and inside) else 0.0                      # ⑤ 재진입
    p10 = done.iloc[-20:-10]
    f["swing_new"] = float(1.0 if ((float(rec["high"].max()) > float(p10["high"].max())) if up
                                   else (float(rec["low"].min()) < float(p10["low"].min()))) else 0.0)  # ⑥ 고저 갱신
    c = done["close"].astype(float)
    e20 = c.ewm(span=20, adjust=False).mean().to_numpy()
    e50 = c.ewm(span=50, adjust=False).mean().to_numpy()
    f["ema_slope"] = s * ((e20[-1] - e20[-11]) + (e50[-1] - e50[-11])) / 2 / px * 100   # ⑦ EMA 기울기
    g = done.set_index("datetime").resample("3min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if len(g) < 30:
        return None
    gc = g["close"].astype(float)
    gap = ((gc.ewm(span=12, adjust=False).mean() - gc.ewm(span=26, adjust=False).mean())
           .pipe(lambda m: m - m.ewm(span=9, adjust=False).mean())).to_numpy()
    f["gap_dir"] = s * (gap[-1] - gap[-3]) / px * 10000 if len(gap) > 2 else 0.0        # ⑧ gap 확대
    return f


# ── 거래 단위 ─────────────────────────────────────────────────────────
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
rows = []
for day in DAYS + ["20261002"]:
    for t in trades(json.load(open(apath(day), encoding="utf-8"))):
        f = feats(day, t["entry"], up=t["dir"].startswith("UP"))
        if f is None:
            continue
        r_ = "+".join(dict.fromkeys(t["reasons"]))
        f.update(day=day, entry=t["entry"], krw=t["krw"], net=t["net"], rs=r_,
                 y_sl=int("STOP_LOSS" in r_ or r_ == "B3_SL"), y_run=int("TP2_FULL" in r_))
        rows.append(f)
T = pd.DataFrame(rows)
T["seg"] = np.where(T.day < "20260801", "train", np.where(T.day <= "20261001", "test", "1002"))
tr, te, d02 = T[T.seg == "train"], T[T.seg == "test"], T[T.seg == "1002"]
print(f"# 거래 {len(T)}건 (train {len(tr)} 러너 {int(tr.y_run.sum())} / test {len(te)} 러너 {int(te.y_run.sum())} / 10-02 {len(d02)})")


def fit(X, y, lam=8.0, iters=6000, lr=0.1):
    X = np.c_[np.ones(len(X)), X]; w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) / len(y); g[1:] += lam * w[1:] / len(y); w -= lr * g
    return w


mu, sd = tr[FEATS].mean(), tr[FEATS].std().replace(0, 1)
Z = lambda d: ((d[FEATS] - mu) / sd).to_numpy()
W = fit(Z(tr), tr.y_run.to_numpy(float))          # 라벨 = runner (추세 지속)
sc = lambda d: 100.0 / (1 + np.exp(-(np.c_[np.ones(len(d)), Z(d)] @ W)))
for d in (T,):
    d["score"] = sc(d)
tr, te, d02 = T[T.seg == "train"], T[T.seg == "test"], T[T.seg == "1002"]
print("\n## 모형 (train 전용, 라벨=runner, λ=8 고정)")
for c, w in zip(FEATS, W[1:]):
    print(f"  {c:10s} {w:+.3f}")


def auc(y, s):
    y = np.asarray(y, float); s = np.asarray(s, float)
    if y.sum() in (0, len(y)):
        return np.nan
    r = pd.Series(s).rank().to_numpy(); n1 = y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))


def spear(a, b):
    if len(a) < 5 or np.std(a) == 0:
        return np.nan
    return float(np.corrcoef(pd.Series(a).rank(), pd.Series(b).rank())[0, 1])


print("\n## 1~4. 판별력")
print("| 구간 | runner AUC | 손절 AUC | 순위상관(score, 손익) |")
print("|---|---|---|---|")
for lab, d in (("train", tr), ("test", te)):
    print(f"| {lab} | {auc(d.y_run, d.score):.3f} | {auc(d.y_sl, d.score):.3f} | {spear(d.score, d.krw):+.3f} |")
a = auc(te.y_run, te.score)
null = np.array([auc(rng.permutation(te.y_run.to_numpy(float)), te.score.to_numpy()) for _ in range(3000)])
print(f"  test runner AUC 순열검정 p = {(np.abs(null - .5) >= abs(a - .5)).mean():.4f}")
a2 = auc(te.y_sl, te.score)
null2 = np.array([auc(rng.permutation(te.y_sl.to_numpy(float)), te.score.to_numpy()) for _ in range(3000)])
print(f"  test 손절  AUC 순열검정 p = {(np.abs(null2 - .5) >= abs(a2 - .5)).mean():.4f}")

BINS = [0, 25, 45, 65, 101]; LAB = ["0-24", "25-44", "45-64", "65-100"]
T["bucket"] = pd.cut(T.score, BINS, right=False, labels=LAB)
for lab in ("train", "test"):
    d = T[T.seg == lab]
    print(f"\n### {lab} score 구간별 ({len(d)}건, {d.krw.sum():+,.0f}원)")
    print("| score | 거래수 | 평균손익 | 승률% | 손절률% | runner율% | 총손익 |")
    print("|---|---|---|---|---|---|---|")
    for b in LAB:
        y = d[d.bucket == b]
        if not len(y):
            print(f"| {b} | 0 | — | — | — | — | — |"); continue
        print(f"| {b} | {len(y)} | {y.krw.mean():+,.0f} | {(y.krw>0).mean()*100:.1f} | "
              f"{y.y_sl.mean()*100:.1f} | {y.y_run.mean()*100:.1f} | {y.krw.sum():+,.0f} |")

# ── 격자(3분) ────────────────────────────────────────────────────────
print("\n## 5. 10/02 시간대별 score (격자, 방향은 박스 중앙 대비)")
grid = []
for day in ["20261002", "20261001"]:
    t0 = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 09:00:00", tz=KST)
    for k in range(0, 121):
        t = t0 + pd.Timedelta(minutes=3 * k)
        if t.hour >= 15:
            break
        f = feats(day, t)
        if f:
            f.update(day=day, t=t)
            grid.append(f)
G = pd.DataFrame(grid)
G["score"] = sc(G)
z = G[G.day == "20261002"].sort_values("t")
print("| 시각 | score | pos | brk_dist | out_bars | reentry | swing_new | ema_slope |")
print("|---|---|---|---|---|---|---|---|")
for _, r in z.iterrows():
    if r["t"].minute % 15 == 0:
        print(f"| {r['t']:%H:%M} | **{r['score']:.0f}** | {r['pos']:.2f} | {r['brk_dist']:+.2f} | "
              f"{int(r['out_bars'])} | {int(r['reentry'])} | {int(r['swing_new'])} | {r['ema_slope']:+.3f} |")
print(f"\n  10/02 진입시점 score: " + " · ".join(
    f"{r['entry']:%H:%M} {r['score']:.0f} ({r['krw']:+,.0f})" for _, r in d02.sort_values('entry').iterrows()))
print(f"  10/01 격자 최고 {G[G.day=='20261001'].score.max():.0f} / 10/02 격자 최고 {z.score.max():.0f}")

print("\n## false breakout 사례 (reentry=1 인 진입)")
fb = T[(T.reentry == 1) & (T.seg != "1002")]
print(f"  {len(fb)}건 · 평균 {fb.krw.mean():+,.0f}원 · 손절률 {fb.y_sl.mean()*100:.1f}% · runner율 {fb.y_run.mean()*100:.1f}%")
nb = T[(T.reentry == 0) & (T.seg != "1002")]
print(f"  (대조) reentry=0 {len(nb)}건 · 평균 {nb.krw.mean():+,.0f}원 · 손절률 {nb.y_sl.mean()*100:.1f}% · runner율 {nb.y_run.mean()*100:.1f}%")
for lab in ("train", "test"):
    x = T[(T.reentry == 1) & (T.seg == lab)]
    y = T[(T.reentry == 0) & (T.seg == lab)]
    print(f"    {lab}: reentry {len(x)}건 {x.krw.mean() if len(x) else 0:+,.0f} vs 비재진입 {len(y)}건 {y.krw.mean():+,.0f}")
T.to_csv(ROOT + "/bb_trades.csv", index=False, encoding="utf-8")
G.to_csv(ROOT + "/bb_grid.csv", index=False, encoding="utf-8")
