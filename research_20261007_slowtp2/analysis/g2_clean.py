"""G2: '+1.0% 에 닿은 그 순간' 무엇을 보고 들고 갈지 정하는가. READ-ONLY.

결과변수는 **반대 플래그 확정까지** 로 자른다 -- 그 뒤는 어차피 OPPOSITE_SIGNAL 이다.
후보 관측치(전부 그 시점에 이미 알 수 있는 값):
  t1    +1.0% 최초도달 경과분
  mae1  도달 전까지의 최대 역행(%)        <- 오늘은 0 에 가까웠다
  dd1   도달 전 '최대 되돌림'(고점 대비)  <- 깔끔한 상승이면 작다
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


def scan(day, entry, direction, px0, regime=None, rs="", krw=0.0):
    sym = LONG if direction.startswith(("UP", "DN")) and direction.startswith("UP") else "0197X0"
    e = etf(day, sym)
    if e is None:
        return None
    ts = pd.DatetimeIndex(e["datetime"])
    i0 = int(ts.searchsorted(entry))
    g = L[L.day == day].sort_values("mins")
    m = entry.hour * 60 + entry.minute
    want = "UP_RED" if direction.startswith("DN") else "DOWN_BLUE"
    nx = g[(g.mins > m) & (g["dir"] == want)]
    opp = int(nx.iloc[0]["mins"]) if len(nx) else 15 * 60 + 20
    iO = int(ts.searchsorted(pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} {opp//60:02d}:{opp%60:02d}", tz=KST)))
    iE = int(ts.searchsorted(pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 15:20", tz=KST)))
    lim = min(iO, iE, len(e))
    n = lambda i, w: (float(e[w].iloc[i]) - px0) / px0 * 100 - DRAG
    j1, mae1, pk = None, 0.0, -9.9
    for i in range(i0, lim):
        h, l = n(i, "high"), n(i, "low")
        mae1 = min(mae1, l)
        if l <= -1.0:
            return None                       # B3 손절 -- 결정 상황이 아니다
        if h >= 1.0:
            j1 = i; break
        pk = max(pk, h)
    if j1 is None:
        return None
    # 도달 이후: 반대플래그 전까지 최대진행 / 그리고 '최고점 대비 1.0%p 트레일' 결과
    peak, out = 1.0, None
    for i in range(j1, lim):
        h, l = n(i, "high"), n(i, "low")
        if l <= peak - 1.0:
            out = peak - 1.0; break
        peak = max(peak, h)
    if out is None:
        out = n(max(lim - 1, j1), "close")
    post = max(1.0, max((n(i, "high") for i in range(j1, lim)), default=1.0))
    return dict(day=day, at=entry.strftime("%H:%M"), dir=direction, regime=regime, rs=rs, krw=krw,
                t1=(ts[j1] - entry).total_seconds() / 60, mae1=-mae1,
                post=post, trail=out, opp_min=(opp - m))


rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        r = scan(d, t["entry"], t["dir"], t["px_in"], t.get("regime"),
                 "+".join(dict.fromkeys(t["reasons"])), t["krw"])
        if r:
            rows.append(r)
T = pd.DataFrame(rows)
T.to_csv(OUT + "/clean1.csv", index=False, encoding="utf-8")
print(f"# '+1.0% 에 닿았고 그 전에 -1.0% 손절이 없던' 거래 {len(T)}건")
print("  (결과는 전부 반대플래그 확정까지로 자름 = OPPOSITE_SIGNAL 전)")

print("\n## 도달 전 최대역행(mae1) 구간별")
T["mb"] = pd.cut(T.mae1, [-0.01, 0.2, 0.5, 1.0, 9], labels=["<=0.2% (거의 무반락)", "0.2~0.5%", "0.5~1.0%", "1.0%+"])
print(T.groupby("mb", observed=True).agg(
    n=("post", "size"), 도달분=("t1", "mean"), 이후최대=("post", "mean"), 중앙=("post", "median"),
    트레일결과=("trail", "mean"),
    _2=("post", lambda s: f"{(s>=2.0).mean()*100:.0f}%"),
    _3=("post", lambda s: f"{(s>=3.0).mean()*100:.0f}%")).round(2).to_string())

print("\n## 교차: 도달분 x 역행")
T["tb"] = pd.cut(T.t1, [-0.1, 6, 20, 999], labels=["<=6분", "6~20분", "20분+"])
print(pd.crosstab(T.tb, T.mb, values=T.trail, aggfunc="mean").round(2).to_string())
print("\n건수:")
print(pd.crosstab(T.tb, T.mb).to_string())

print("\n## '+1.0% 에서 익절' vs '트레일 1.0%p 로 보유' 비교 (%p, 반대플래그까지)")
print("| 조건 | n | +1% 익절 | 트레일 보유 | 차이 |")
print("|---|---|---|---|---|")
for lab, s in (("전체", T), ("mae1<=0.2%", T[T.mae1 <= 0.2]), ("mae1<=0.5%", T[T.mae1 <= 0.5]),
               ("mae1>0.5%", T[T.mae1 > 0.5]), ("CHOP 진입", T[T.regime == "CHOP"]),
               ("CHOP ∧ mae1<=0.5", T[(T.regime == "CHOP") & (T.mae1 <= 0.5)])):
    print(f"| {lab} | {len(s)} | +1.00%p | {s.trail.mean():+.2f}%p | **{s.trail.mean()-1.0:+.2f}%p** |")
