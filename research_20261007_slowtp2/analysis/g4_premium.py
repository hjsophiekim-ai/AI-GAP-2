"""G4: B3 +1% 전량익절 대신 '20% 익절 + 잔량 트레일(하한 F)' 의 보험료와 수혜. READ-ONLY.
   보험료 = 9·10월 CHOP 18건(추세 없던 국면)에서의 손해
   수혜   = 오늘(10/07) 09:57 레그에서의 이득
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
ROOT = REPO + "/research_20261004_chop_staged"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
OUT = os.path.dirname(os.path.abspath(__file__))
KST = "Asia/Seoul"; LONG = "0193T0"; DRAG = 0.05
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})


def load(day, sym, src_dir):
    f = f"{src_dir}/replay_{day}_{'long' if sym == LONG else 'inverse'}_1m.csv"
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f)
    dt = pd.to_datetime(df["datetime"])
    df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
    return df.sort_values("datetime").reset_index(drop=True)


def policy(e, i0, px0, lim, G, F):
    """+1.0% 도달 후 20% 익절 + 잔량은 stop = max(peak-G, F). 반환 = 가중 net(%p)."""
    n = lambda i, w: (float(e[w].iloc[i]) - px0) / px0 * 100 - DRAG
    j1 = None
    for i in range(i0, lim):
        if n(i, "low") <= -1.0:
            return None
        if n(i, "high") >= 1.0:
            j1 = i; break
    if j1 is None:
        return None
    peak, out = 1.0, None
    for i in range(j1, lim):
        stop = max(peak - G, F)
        if n(i, "low") <= stop:
            out = stop; break
        peak = max(peak, n(i, "high"))
    if out is None:
        out = n(max(lim - 1, j1), "close")
    return 1.0 * 0.2 + out * 0.8


# ── 보험료: 9·10월 CHOP 18건 ───────────────────────────────────────────
DATA = ROOT + "/wk/data"
prem = {}
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    g = L[L.day == d].sort_values("mins")
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        if t.get("regime") != "CHOP":
            continue
        sym = LONG if t["dir"].startswith("UP") else "0197X0"
        e = load(d, sym, DATA)
        if e is None:
            continue
        ts = pd.DatetimeIndex(e["datetime"]); i0 = int(ts.searchsorted(t["entry"]))
        m = t["entry"].hour * 60 + t["entry"].minute
        want = "UP_RED" if t["dir"].startswith("DN") else "DOWN_BLUE"
        nx = g[(g.mins > m) & (g["dir"] == want)]
        opp = int(nx.iloc[0]["mins"]) if len(nx) else 15 * 60 + 20
        iO = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} {opp//60:02d}:{opp%60:02d}", tz=KST)))
        iE = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:20", tz=KST)))
        lim = min(iO, iE, len(e))
        for G in (0.5, 1.0):
            for F in (0.3, 0.5, 0.7, 0.9):
                v = policy(e, i0, t["px_in"], lim, G, F)
                if v is not None:
                    prem.setdefault((G, F), []).append(v - 1.0)

# ── 수혜: 오늘 10/07 09:57 레그 (인버스, 진입 10:03, 반대플래그 11:30) ──
TD = REPO + "/data/cache"
e = load("20261007", "0197X0", TD)
ts = pd.DatetimeIndex(e["datetime"])
ent = pd.Timestamp("2026-10-07 10:03", tz=KST)
i0 = int(ts.searchsorted(ent)); px0 = float(e["open"].iloc[i0])
limOPP = int(ts.searchsorted(pd.Timestamp("2026-10-07 11:30", tz=KST)))
limEOD = int(ts.searchsorted(pd.Timestamp("2026-10-07 15:20", tz=KST)))

print("## 'B3 +1% 전량' -> '20% 익절 + 잔량 트레일(하한 F)' (%p, 현행 +1.00 대비)")
print("| 트레일 G | 하한 F | 9·10월 18건 평균(보험료) | 최악 | 승 | 패 | 오늘 10/07 (반대신호까지) | 오늘 (EOD까지) |")
print("|---|---|---|---|---|---|---|---|")
for G in (0.5, 1.0):
    for F in (0.3, 0.5, 0.7, 0.9):
        a = np.array(prem[(G, F)])
        t1 = policy(e, i0, px0, limOPP, G, F)
        t2 = policy(e, i0, px0, limEOD, G, F)
        print(f"| {G}%p | +{F}% | **{a.mean():+.3f}%p** | {a.min():+.2f} | {int((a>0).sum())} | {int((a<0).sum())} | "
              f"**{t1-1.0:+.2f}%p** | **{t2-1.0:+.2f}%p** |")
print(f"\n(9·10월 표본 {len(prem[(0.5,0.5)])}건 — B3 가 지배한 적 있는 전부)")
print("오늘 레그: 진입 10:03 @ {:,.0f} · 최고 +3.93%(15:13) · 11:30 +2.42% · 15:20 +3.68%".format(px0))
