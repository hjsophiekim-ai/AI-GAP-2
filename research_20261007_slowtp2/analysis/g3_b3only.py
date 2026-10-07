"""G3: B3 가 실제로 지배하는 집합(CHOP 진입)에서 '+1%' 이후 정책 비교. READ-ONLY.

현행 B3_TP = +1.0% 전량.
후보     = +1.0% 에서 20% 익절 + 잔량은 peak 대비 G%p 트레일 (하한 +0.3% 본전스톱),
           반대플래그 확정 / 15:20 에서 종료.
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


def etf(day, sym):
    k = (day, sym)
    if k not in _c:
        f = f"{DATA}/replay_{day}_{'long' if sym == LONG else 'inverse'}_1m.csv"
        r = None
        if os.path.exists(f):
            df = pd.read_csv(f)
            dt = pd.to_datetime(df["datetime"])
            df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
            r = df.sort_values("datetime").reset_index(drop=True)
        _c[k] = r
    return _c[k]


L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})
GIVES = (0.3, 0.5, 0.8, 1.2)
FLOOR = 0.3
rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    g = L[L.day == d].sort_values("mins")
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        if t.get("regime") != "CHOP":
            continue
        sym = LONG if t["dir"].startswith("UP") else "0197X0"
        e = etf(d, sym)
        if e is None:
            continue
        ts = pd.DatetimeIndex(e["datetime"]); px0 = t["px_in"]
        i0 = int(ts.searchsorted(t["entry"]))
        m = t["entry"].hour * 60 + t["entry"].minute
        want = "UP_RED" if t["dir"].startswith("DN") else "DOWN_BLUE"
        nx = g[(g.mins > m) & (g["dir"] == want)]
        opp = int(nx.iloc[0]["mins"]) if len(nx) else 15 * 60 + 20
        iO = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} {opp//60:02d}:{opp%60:02d}", tz=KST)))
        iE = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:20", tz=KST)))
        lim = min(iO, iE, len(e))
        n = lambda i, w: (float(e[w].iloc[i]) - px0) / px0 * 100 - DRAG
        j1, mae1 = None, 0.0
        for i in range(i0, lim):
            mae1 = min(mae1, n(i, "low"))
            if n(i, "low") <= -1.0:
                break
            if n(i, "high") >= 1.0:
                j1 = i; break
        if j1 is None:
            continue
        r = dict(day=d, at=t["entry"].strftime("%H:%M"), t1=(ts[j1] - t["entry"]).total_seconds() / 60,
                 mae1=-mae1, rs="+".join(dict.fromkeys(t["reasons"])), q=t["q"], px0=px0, krw=t["krw"])
        for G in GIVES:
            peak, out = 1.0, None
            for i in range(j1, lim):
                stop = max(peak - G, FLOOR)
                if n(i, "low") <= stop:
                    out = stop; break
                peak = max(peak, n(i, "high"))
            if out is None:
                out = n(max(lim - 1, j1), "close")
            r[f"g{G}"] = 1.0 * 0.2 + out * 0.8          # 20% 익절 + 80% 트레일
        rows.append(r)
T = pd.DataFrame(rows)
T.to_csv(OUT + "/b3only.csv", index=False, encoding="utf-8")
T["seg"] = np.where(T.day < "20260801", "train", "test")
T["sc"] = (T.t1 > 6) & (T.t1 <= 20) & (T.mae1 <= 0.5)
print(f"# CHOP 진입 중 '+1.0% 도달' {len(T)}건  (현행 B3_TP = 전량 +1.00%p)")
print("\n## 트레일 폭별 평균 수익(%p) — 현행 +1.00%p 대비")
print("| 트레일 | 전체 n | 평균 | 대비 | SLOW-CLEAN n | 평균 | 대비 | 그외 대비 |")
print("|---|---|---|---|---|---|---|---|")
for G in GIVES:
    c = f"g{G}"; s, o = T[T.sc], T[~T.sc]
    print(f"| {G}%p | {len(T)} | {T[c].mean():+.2f} | **{T[c].mean()-1:+.2f}** | {len(s)} | {s[c].mean():+.2f} | "
          f"**{s[c].mean()-1:+.2f}** | {o[c].mean()-1:+.2f} |")
print("\n## 기간 (트레일 0.5%p)")
for sg, s in T.groupby("seg"):
    print(f"  {sg} n={len(s)} 전체 {s['g0.5'].mean()-1:+.2f}%p · SLOW-CLEAN n={int(s.sc.sum())} "
          f"{(s[s.sc]['g0.5'].mean()-1 if s.sc.any() else 0):+.2f}%p")
print("\n## 건별 (트레일 0.5%p)")
print("| 일자 | 진입 | 도달분 | 역행 | SLOW-CLEAN | 현행 | 후보 | 차이 | 실제사유 |")
print("|---|---|---|---|---|---|---|---|---|")
for _, r in T.sort_values("day").iterrows():
    print(f"| {r.day} | {r['at']} | {r.t1:.0f}분 | {r.mae1:.2f}% | {'O' if r.sc else ''} | +1.00% | "
          f"{r['g0.5']:+.2f}% | **{r['g0.5']-1:+.2f}%p** | {r.rs[:28]} |")
