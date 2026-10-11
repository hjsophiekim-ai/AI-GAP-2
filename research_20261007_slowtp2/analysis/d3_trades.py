"""D3: 거래 x 직전레그 크기. READ-ONLY.

진입 시점에 관측 가능한 '직전 반대레그 MFE' 로 거래를 나눠, 20분 max-hold 가
어디서 돈을 남기고 끊는지 본다. 가격경로는 실제 보유 ETF 1분봉으로 본다.
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
KST = "Asia/Seoul"
LONG = "0193T0"

src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]


def apath(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


_cache = {}


def etf(day, sym):
    """보유 ETF 1분봉 (long/inverse)."""
    key = (day, sym)
    if key in _cache:
        return _cache[key]
    kind = "long" if sym == LONG else "inverse"
    f = f"{DATA}/replay_{day}_{kind}_1m.csv"
    r = None
    if os.path.exists(f):
        df = pd.read_csv(f)
        dt = pd.to_datetime(df["datetime"])
        df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
        r = df.sort_values("datetime").reset_index(drop=True)
    _cache[key] = r
    return r


L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})
# 진입 시점에 쓸 수 있는 직전레그 = 진입시각 '이전'에 이미 끝난 마지막 레그
legs_by_day = {d: g.reset_index(drop=True) for d, g in L.groupby("day")}

rows = []
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
for day in DAYS:
    o = json.load(open(apath(day), encoding="utf-8"))
    g = legs_by_day.get(day)
    for t in trades(o):
        ein = t["entry"]
        mins = ein.hour * 60 + ein.minute
        prev = np.nan
        if g is not None:
            # 진입시각 이전에 시작했고, 그 전에 '끝난' 레그 = 시작 + 지속 <= 진입시각
            done = g[(g["mins"] + g["dur"]) <= mins]
            if len(done):
                prev = float(done.iloc[-1]["mfe"])
        sym = "0193T0" if t["dir"].startswith("UP") else "0197X0"
        e = etf(day, sym)
        up = True   # ETF 는 방향상품 -- 보유 ETF 기준으로는 항상 상승이 이익
        path = {}
        if e is not None:
            ts = pd.DatetimeIndex(e["datetime"])
            i0 = int(ts.searchsorted(ein))
            px0 = t["px_in"]
            for n in (20, 25, 30, 35, 40, 50, 60):
                i1 = int(ts.searchsorted(ein + pd.Timedelta(minutes=n)))
                if i1 <= i0 or i1 > len(e):
                    continue
                seg = e.iloc[i0:i1]
                path[f"mfe{n}"] = (float(seg["high"].max()) - px0) / px0 * 100
                path[f"mae{n}"] = (float(seg["low"].min()) - px0) / px0 * 100
                j = min(i1, len(e) - 1)
                path[f"cl{n}"] = (float(e["close"].iloc[j]) - px0) / px0 * 100
        rows.append(dict(day=day, at=ein.strftime("%H:%M"), mins=mins, dir=t["dir"],
                         sym=sym, krw=t["krw"], net=t["net"], hold=t["hold_min"],
                         rs="+".join(dict.fromkeys(t["reasons"])), prev_mfe=prev,
                         regime=t.get("regime"), **path))

T = pd.DataFrame(rows)
T.to_csv(OUT + "/trades.csv", index=False, encoding="utf-8")
print(f"# A 거래 {len(T)}건 / {T.day.nunique()}일 · 직전레그 있는 건 {T.prev_mfe.notna().sum()}")
print(f"# 총손익 {T.krw.sum():+,.0f}원 (기준 A 85일 +17,227,606)")
print("\n## 청산사유 분포")
print(T.groupby("rs").agg(n=("krw", "size"), 손익=("krw", "sum"), 평균net=("net", "mean"),
                          보유분=("hold", "mean")).sort_values("n", ascending=False).head(12).round(2).to_string())
