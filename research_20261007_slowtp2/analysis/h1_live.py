"""H1: '당일 관측만으로' chop/trend 를 가르는 신호 탐색. READ-ONLY.

P3 detector 는 최근 완료 shadow 거래 10건으로 판정한다 -> 라벨이 끈적하다.
여기서는 **진입 시점에 그날 장중에서만 관측되는 값**으로 대체신호를 찾는다.
라벨(=detector 출력)이 아니라 **결과**(그 포지션의 레그가 실제로 달렸는가)로 검정한다.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
ROOT = REPO + "/research_20261004_chop_staged"
DATA = ROOT + "/wk/data"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
OUT = os.path.dirname(os.path.abspath(__file__))
KST = "Asia/Seoul"; LONG = "0193T0"; DRAG = 0.05
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
_c = {}


def load(day, tag, sd):
    k = (day, tag, sd)
    if k not in _c:
        f = f"{sd}/replay_{day}_{tag}_1m.csv"
        r = None
        if os.path.exists(f):
            df = pd.read_csv(f)
            dt = pd.to_datetime(df["datetime"])
            df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
            r = df.sort_values("datetime").reset_index(drop=True)
        _c[k] = r
    return _c[k]


L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})


def feats(day, entry, sd=DATA):
    """진입 시점까지의 당일 장중 관측치만."""
    h = load(day, "hynix", sd)
    if h is None:
        return None
    ts = pd.DatetimeIndex(h["datetime"])
    o9 = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 09:00", tz=KST)
    i9, ie = int(ts.searchsorted(o9)), int(ts.searchsorted(entry))
    if ie - i9 < 10:
        return None
    s = h.iloc[i9:ie]
    el = (entry - o9).total_seconds() / 60                      # 09:00 이후 경과분
    g = L[L.day == day].sort_values("mins")
    m = entry.hour * 60 + entry.minute
    pre = g[g.mins <= m]
    nf = len(pre)
    since = (m - int(pre.iloc[-1]["mins"])) if len(pre) else el
    op, cl = float(s["open"].iloc[0]), float(s["close"].iloc[-1])
    rng = (float(s["high"].max()) - float(s["low"].min())) / op * 100
    # 3분봉 기준 경로효율 / 변동
    b3 = (s.set_index("datetime").resample("3min", label="left", closed="left")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna())
    c = b3["close"].astype(float)
    moves = c.diff().abs().sum()
    eff = abs(cl - op) / moves * 100 if moves else 0.0           # 방향효율(0~100)
    atr = ((b3["high"] - b3["low"]).mean()) / op * 100 if len(b3) else 0.0
    return dict(elapsed=el, nflag=nf, rate=nf / max(el / 60, 0.25), since=since,
                rng=rng, eff=eff, atr3=atr, nbar3=len(b3))


def outcome(day, entry, direction, px0, sd=DATA):
    """그 포지션의 레그가 반대플래그 확정 전까지 얼마나 갔나(보유 ETF net %)."""
    sym = LONG if direction.startswith("UP") else "0197X0"
    e = load(day, "long" if sym == LONG else "inverse", sd)
    if e is None:
        return None
    ts = pd.DatetimeIndex(e["datetime"]); i0 = int(ts.searchsorted(entry))
    g = L[L.day == day].sort_values("mins")
    m = entry.hour * 60 + entry.minute
    want = "UP_RED" if direction.startswith("DN") else "DOWN_BLUE"
    nx = g[(g.mins > m) & (g["dir"] == want)]
    opp = int(nx.iloc[0]["mins"]) if len(nx) else 15 * 60 + 20
    iO = int(ts.searchsorted(pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} {opp//60:02d}:{opp%60:02d}", tz=KST)))
    iE = int(ts.searchsorted(pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 15:20", tz=KST)))
    lim = min(iO, iE, len(e))
    n = lambda i, w: (float(e[w].iloc[i]) - px0) / px0 * 100 - DRAG
    mfe, mae = 0.0, 0.0
    for i in range(i0, max(lim, i0 + 1)):
        mfe = max(mfe, n(i, "high")); mae = min(mae, n(i, "low"))
    return mfe, -mae, (opp - m)


rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        f = feats(d, t["entry"])
        o = outcome(d, t["entry"], t["dir"], t["px_in"])
        if f is None or o is None:
            continue
        rows.append(dict(day=d, at=t["entry"].strftime("%H:%M"), dir=t["dir"],
                         regime=t.get("regime"), krw=t["krw"],
                         rs="+".join(dict.fromkeys(t["reasons"])),
                         mfe=o[0], mae=o[1], legmin=o[2], **f))
T = pd.DataFrame(rows)
# 오늘
TD = REPO + "/data/cache"
ent = pd.Timestamp("2026-10-07 10:03", tz=KST)
f = feats("20261007", ent, TD)
e7 = load("20261007", "inverse", TD)
px0 = float(e7["open"].iloc[int(pd.DatetimeIndex(e7["datetime"]).searchsorted(ent))])
print("## 오늘(10/07) 09:57 DOWN_BLUE 진입 시점 당일 관측치")
for k, v in f.items():
    print(f"   {k:8s} {v:.2f}")
T.to_csv(OUT + "/live.csv", index=False, encoding="utf-8")
print(f"\n# 85일 거래 {len(T)}건 · 결과=반대플래그 확정 전까지의 ETF MFE")
T["run2"] = (T.mfe >= 2.0).astype(int)
T["run3"] = (T.mfe >= 3.0).astype(int)
T["seg"] = np.where(T.day < "20260801", "train 5~7월", "test 8~10월")
print("\n## 후보 신호별 — '레그가 2%+ 간다' 판별력 (AUC, train/test)")


def auc(y, x):
    y = np.asarray(y); x = np.asarray(x)
    p, n = x[y == 1], x[y == 0]
    if len(p) == 0 or len(n) == 0:
        return np.nan
    return float((p[:, None] > n[None, :]).mean() + 0.5 * (p[:, None] == n[None, :]).mean())


print("| 신호 | 전체 AUC(2%) | train | test | 전체 AUC(3%) |")
print("|---|---|---|---|---|")
for c in ("rate", "nflag", "since", "rng", "eff", "atr3", "elapsed"):
    tr, te = T[T.seg.str.startswith("train")], T[T.seg.str.startswith("test")]
    print(f"| {c} | {auc(T.run2, T[c]):.3f} | {auc(tr.run2, tr[c]):.3f} | {auc(te.run2, te[c]):.3f} | "
          f"{auc(T.run3, T[c]):.3f} |")
print("\n## 플래그 빈도(rate = 장중 플래그수 / 경과시간h) 구간별")
T["rb"] = pd.cut(T.rate, [-0.01, 1.0, 2.0, 3.5, 99], labels=["<=1.0 (희소)", "1.0~2.0", "2.0~3.5", "3.5+ (잦음)"])
print(T.groupby("rb", observed=True).agg(n=("mfe", "size"), MFE=("mfe", "mean"), 중앙=("mfe", "median"),
      _2=("run2", lambda s: f"{s.mean()*100:.0f}%"), _3=("run3", lambda s: f"{s.mean()*100:.0f}%"),
      레그분=("legmin", "mean"), 손익=("krw", "sum")).round(2).to_string())
print(f"\n오늘 rate = {f['rate']:.2f} (플래그 {f['nflag']}개 / 경과 {f['elapsed']:.0f}분)")
