"""H5: U자 재검정(경계를 반대플래그로 자른 뒤) + 최종 필터 두 부품 합산. READ-ONLY."""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
ROOT = REPO + "/research_20261004_chop_staged"
DATA = ROOT + "/wk/data"; TD = REPO + "/data/cache"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
OUT = os.path.dirname(os.path.abspath(__file__))
KST = "Asia/Seoul"; LONG = "0193T0"; DRAG = 0.05
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
_c = {}


def one(day, tag):
    k = (day, tag)
    if k not in _c:
        r = None
        for sd in (DATA, TD):
            f = f"{sd}/replay_{day}_{tag}_1m.csv"
            if os.path.exists(f):
                df = pd.read_csv(f); dt = pd.to_datetime(df["datetime"])
                df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
                r = df.sort_values("datetime").reset_index(drop=True); break
        _c[k] = r
    return _c[k]


L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})
F = 0.2          # 잔량 고정스탑
rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    g = L[L.day == d].sort_values("mins")
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        sym = LONG if t["dir"].startswith("UP") else "0197X0"
        e = one(d, "long" if sym == LONG else "inverse")
        if e is None:
            continue
        ts = pd.DatetimeIndex(e["datetime"]); i0 = int(ts.searchsorted(t["entry"])); px0 = t["px_in"]
        m = t["entry"].hour * 60 + t["entry"].minute
        want = "UP_RED" if t["dir"].startswith("DN") else "DOWN_BLUE"
        nx = g[(g.mins > m) & (g["dir"] == want)]
        opp = int(nx.iloc[0]["mins"]) if len(nx) else 15 * 60 + 20
        iO = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} {opp//60:02d}:{opp%60:02d}", tz=KST)))
        iE = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:20", tz=KST)))
        lim = min(iO, iE, len(e))
        n = lambda i, w: (float(e[w].iloc[i]) - px0) / px0 * 100 - DRAG
        j1 = None
        for i in range(i0, max(lim, i0 + 1)):
            if n(i, "low") <= -1.0:
                break
            if n(i, "high") >= 1.0:
                j1 = i; break
        if j1 is None:
            continue
        t1 = (ts[j1] - t["entry"]).total_seconds() / 60
        out = None
        for i in range(j1, max(lim, j1 + 1)):
            if n(i, "low") <= F:
                out = F; break
        if out is None:
            out = n(max(lim - 1, j1), "close")
        post = max((n(i, "high") for i in range(j1, max(lim, j1 + 1))), default=1.0)
        rows.append(dict(day=d, at=t["entry"].strftime("%H:%M"), p3=t.get("regime"), t1=t1,
                         post=post, hold=0.2 + 0.8 * out, notional=t["q"] * px0,
                         rs="+".join(dict.fromkeys(t["reasons"]))))
T = pd.DataFrame(rows)
T["seg"] = np.where(T.day < "20260801", "train 5~7월", "test 8~10월")
T["d"] = (T.hold - 1.0) / 100 * T.notional
print(f"# '+1.0% 도달' {len(T)}건 — 결과는 반대플래그 확정 전까지로 잘랐다")
print("\n## U자 재검정 — +1% 도달시간 구간별 (경계=반대플래그)")
T["b"] = pd.cut(T.t1, [-0.1, 3, 6, 12, 20, 35, 999],
                labels=["<=3분", "3~6분", "6~12분", "12~20분", "20~35분", "35분+"])
print(T.groupby("b", observed=True).agg(n=("post", "size"), 이후최대=("post", "mean"), 중앙=("post", "median"),
      _2=("post", lambda s: f"{(s>=2).mean()*100:.0f}%"), _3=("post", lambda s: f"{(s>=3).mean()*100:.0f}%"),
      보유시손익=("d", "sum")).round(2).to_string())
print("\n## 같은 표를 P3 라벨별로")
for lab in ("TREND", "CHOP"):
    s = T[T.p3 == lab]
    print(f"\n### P3={lab} (n={len(s)})")
    print(s.groupby("b", observed=True).agg(n=("post", "size"), 이후최대=("post", "mean"),
          _2=("post", lambda x: f"{(x>=2).mean()*100:.0f}%"), 보유시손익=("d", "sum")).round(2).to_string())

print("\n" + "=" * 70)
print("## 부품 A — B3 +1% 전량 -> 20% 익절 + 잔량 고정스탑 +0.2% (CHOP 라벨만)")
C = T[T.p3 == "CHOP"]
print(f"| 구간 | n | 합(원) | 승 | 패 | 1건 최악 |")
print(f"|---|---|---|---|---|---|")
print(f"| 전체 | {len(C)} | **{C.d.sum():+,.0f}** | {int((C.d>0).sum())} | {int((C.d<0).sum())} | {C.d.min():+,.0f} |")
for b, s in C.groupby("b", observed=True):
    if len(s):
        print(f"| {b} | {len(s)} | {s.d.sum():+,.0f} | {int((s.d>0).sum())} | {int((s.d<0).sum())} | {s.d.min():+,.0f} |")
