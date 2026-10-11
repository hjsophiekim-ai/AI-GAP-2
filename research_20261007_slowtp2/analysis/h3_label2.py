"""H3: 전일 봉까지 붙여 n1_adaptive 를 제대로 판정 + 10거래 롤링과 비교. READ-ONLY."""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
sys.path.insert(0, REPO)
ROOT = REPO + "/research_20261004_chop_staged"
DATA = ROOT + "/wk/data"
TD = REPO + "/data/cache"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
OUT = os.path.dirname(os.path.abspath(__file__))
KST = "Asia/Seoul"; LONG = "0193T0"; DRAG = 0.05
from app.trading.macd2.signal_engine import resample_completed_3m, exclude_preopen_padding_1m
from app.trading.macd2 import n1_adaptive
from app.trading.macd2.models import Direction
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
ALLD = sorted({os.path.basename(p)[7:15] for p in glob.glob(DATA + "/replay_*_hynix_1m.csv")}
              | {os.path.basename(p)[7:15] for p in glob.glob(TD + "/replay_*_hynix_1m.csv")})
_c = {}


def one(day, tag):
    k = (day, tag)
    if k not in _c:
        r = None
        for sd in (DATA, TD):
            f = f"{sd}/replay_{day}_{tag}_1m.csv"
            if os.path.exists(f):
                df = pd.read_csv(f)
                dt = pd.to_datetime(df["datetime"])
                df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
                r = df.sort_values("datetime").reset_index(drop=True)
                break
        _c[k] = r
    return _c[k]


def hist(day, nprev=3):
    """전일 nprev 일 + 당일 1분봉 이어붙이기 (production 은 롤링 히스토리를 쓴다)."""
    i = ALLD.index(day)
    parts = [one(d, "hynix") for d in ALLD[max(0, i - nprev):i + 1]]
    parts = [p for p in parts if p is not None]
    return pd.concat(parts, ignore_index=True) if parts else None


L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})


def judge(day, entry, direction):
    h = hist(day)
    if h is None:
        return None
    clean, _ = exclude_preopen_padding_1m(h)
    b3 = resample_completed_3m(clean, now=entry.to_pydatetime())
    dd = Direction.UP_RED if direction.startswith("UP") else Direction.DOWN_BLUE
    return n1_adaptive.snapshot(b3, dd)


def legmfe(day, entry, direction, px0):
    sym = LONG if direction.startswith("UP") else "0197X0"
    e = one(day, "long" if sym == LONG else "inverse")
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
    return max((n(i, "high") for i in range(i0, max(lim, i0 + 1))), default=0.0)


rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        s = judge(d, t["entry"], t["dir"]); mf = legmfe(d, t["entry"], t["dir"], t["px_in"])
        if s is None or mf is None:
            continue
        rows.append(dict(day=d, at=t["entry"].strftime("%H:%M"), dir=t["dir"], p3=t.get("regime"),
                         n1=("부족" if s.insufficient else ("TREND" if s.ok else "OFF")),
                         mfe=mf, krw=t["krw"], rs="+".join(dict.fromkeys(t["reasons"]))))
T = pd.DataFrame(rows)
T.to_csv(OUT + "/label2.csv", index=False, encoding="utf-8")
print(f"# 거래 {len(T)}건 · 판정불가 {int((T.n1=='부족').sum())}건")
V = T[T.n1 != "부족"]
print("\n## 두 판정의 '레그가 2%+ 간다' 판별력")
print("| 판정 | 집합 | n | MFE 평균 | 2%+ | 3%+ | 거래손익 합 |")
print("|---|---|---|---|---|---|---|")
for lab, col, vals in (("**P3 (최근 10거래 롤링)**", "p3", ["TREND", "CHOP"]),
                       ("**n1_adaptive (그 봉, 당일)**", "n1", ["TREND", "OFF"])):
    for v in vals:
        s = V[V[col] == v]
        if len(s):
            print(f"| {lab} | {v} | {len(s)} | +{s.mfe.mean():.2f}% | {(s.mfe>=2).mean()*100:.0f}% | "
                  f"{(s.mfe>=3).mean()*100:.0f}% | {s.krw.sum():+,.0f} |")
print("\n## 교차 (판정가능 건만)")
print(pd.crosstab(V.p3.fillna("?"), V.n1).to_string())
print("\n## 교차별 평균 MFE")
print(V.pivot_table(index="p3", columns="n1", values="mfe", aggfunc=["mean", "size"]).round(2).to_string())
# 오늘
ent = pd.Timestamp("2026-10-07 10:03", tz=KST)
s = judge("20261007", ent, "DN 인버")
print(f"\n## 오늘 10/07 10:03 DOWN_BLUE — n1_adaptive")
print(f"   ok={s.ok} reason={s.reason} close={s.close:,.0f} EMA20={s.ema_fast:,.0f} "
      f"EMA50={s.ema_slow:,.0f} slope={s.ema_slow_slope:+,.1f}")
