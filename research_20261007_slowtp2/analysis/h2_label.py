"""H2: 10거래 롤링 라벨(P3) vs 당일 판정(n1_adaptive 상위추세). READ-ONLY.
   판정은 production 함수 n1_adaptive.snapshot() 을 그대로 호출한다.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
sys.path.insert(0, REPO)
ROOT = REPO + "/research_20261004_chop_staged"
DATA = ROOT + "/wk/data"
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


def trend_ok(day, entry, direction, sd=DATA):
    h = load(day, "hynix", sd)
    if h is None:
        return None
    clean, _ = exclude_preopen_padding_1m(h)
    b3 = resample_completed_3m(clean, now=entry.to_pydatetime())
    dd = Direction.UP_RED if direction.startswith("UP") else Direction.DOWN_BLUE
    s = n1_adaptive.snapshot(b3, dd)
    return s


def leg(day, entry, direction, px0, sd=DATA):
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
    return max((n(i, "high") for i in range(i0, max(lim, i0 + 1))), default=0.0)


rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        s = trend_ok(d, t["entry"], t["dir"])
        mf = leg(d, t["entry"], t["dir"], t["px_in"])
        if s is None or mf is None:
            continue
        rows.append(dict(day=d, at=t["entry"].strftime("%H:%M"), dir=t["dir"],
                         p3=t.get("regime"), n1=("TREND" if s.ok else ("부족" if s.insufficient else "OFF")),
                         mfe=mf, krw=t["krw"], rs="+".join(dict.fromkeys(t["reasons"]))))
T = pd.DataFrame(rows)
T.to_csv(OUT + "/label.csv", index=False, encoding="utf-8")
print(f"# 거래 {len(T)}건")
print("\n## P3 라벨(10거래 롤링) x n1_adaptive 당일판정")
print(pd.crosstab(T.p3.fillna("?"), T.n1).to_string())
print("\n## 각 판정의 '레그가 2%+ 간다' 판별력")
print("| 판정 | 집합 | n | MFE 평균 | 2%+ | 3%+ |")
print("|---|---|---|---|---|---|")
for lab, col, vals in (("P3 (10거래 롤링)", "p3", ["TREND", "CHOP"]),
                       ("n1_adaptive (당일)", "n1", ["TREND", "OFF"])):
    for v in vals:
        s = T[T[col] == v]
        if len(s):
            print(f"| {lab} | {v} | {len(s)} | +{s.mfe.mean():.2f}% | {(s.mfe>=2).mean()*100:.0f}% | {(s.mfe>=3).mean()*100:.0f}% |")

print("\n## 9·10월 CHOP 라벨 18건 — 당일판정은 뭐라 했나")
S = T[(T.p3 == "CHOP")]
print(pd.crosstab(S.n1, S.mfe >= 2.0).to_string())
print("\n| 일자 | 진입 | 방향 | n1 당일판정 | 레그 MFE | 실제사유 |")
print("|---|---|---|---|---|---|")
for _, r in S.sort_values("day").iterrows():
    print(f"| {r.day} | {r['at']} | {r['dir']} | **{r.n1}** | +{r.mfe:.2f}% | {r.rs[:26]} |")

# 오늘
TD = REPO + "/data/cache"
ent = pd.Timestamp("2026-10-07 10:03", tz=KST)
s = trend_ok("20261007", ent, "DN 인버", TD)
print(f"\n## 오늘 10/07 10:03 (DOWN_BLUE 보유) n1_adaptive 판정")
print(f"   ok={s.ok} reason={s.reason}")
print(f"   close={s.close:,.0f} EMA20={s.ema_fast:,.0f} EMA50={s.ema_slow:,.0f} EMA50기울기={s.ema_slow_slope:+,.1f}")
