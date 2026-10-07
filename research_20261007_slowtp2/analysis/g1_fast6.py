"""G1: P3 rescue 의 '+1% 를 6분 안에' 조건은 부호가 맞는가. READ-ONLY.

가설(사용자): 진짜 긴 추세는 천천히 올라온다 -> 빠른 도달을 고르는 조건은 거꾸로다.
검정: 모든 거래에 대해 보유 ETF 1분봉으로 '+1.0% 최초도달 경과분' 을 재고,
      그 뒤 어디까지 갔는지(이후 최대 MFE)를 본다. 미래참조 없음 -- 도달시각은
      그 시점에 관측되는 값이고, MFE 는 결과변수다.
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
KST = "Asia/Seoul"; LONG = "0193T0"
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
rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    o = json.load(open(ap(d), encoding="utf-8"))
    g = L[L.day == d].sort_values("mins")
    for t in trades(o):
        sym = LONG if t["dir"].startswith("UP") else "0197X0"
        e = etf(d, sym)
        if e is None:
            continue
        ts = pd.DatetimeIndex(e["datetime"])
        i0 = int(ts.searchsorted(t["entry"])); px0 = t["px_in"]
        # 반대 플래그 확정시각 (이 뒤는 어차피 OPPOSITE_SIGNAL 로 나간다)
        m = t["entry"].hour * 60 + t["entry"].minute
        want = "UP_RED" if t["dir"].startswith("DN") else "DOWN_BLUE"
        nx = g[(g.mins > m) & (g["dir"] == want)]
        opp = int(nx.iloc[0]["mins"]) if len(nx) else 15 * 60 + 20
        iO = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} {opp//60:02d}:{opp%60:02d}", tz=KST)))
        iE = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:20", tz=KST)))
        end = min(iE, len(e))
        t1, mfe = None, 0.0
        drag = 0.05                                     # 왕복비용 근사(%p)
        for i in range(i0, end):
            h = (float(e["high"].iloc[i]) - px0) / px0 * 100 - drag
            l = (float(e["low"].iloc[i]) - px0) / px0 * 100 - drag
            mfe = max(mfe, h)
            if t1 is None and h >= 1.0:
                t1 = (ts[i] - t["entry"]).total_seconds() / 60
            if t1 is None and l <= -1.0:
                break                                    # B3 는 -1% 에서 손절
        if t1 is None:
            continue
        # 도달 이후 구간
        j1 = int(ts.searchsorted(t["entry"] + pd.Timedelta(minutes=t1)))
        post = e.iloc[j1:min(iO, end)]
        post_mfe = ((float(post["high"].max()) - px0) / px0 * 100 - drag) if len(post) else 1.0
        rows.append(dict(day=d, at=t["entry"].strftime("%H:%M"), dir=t["dir"],
                         regime=t.get("regime"), rs="+".join(dict.fromkeys(t["reasons"])),
                         krw=t["krw"], net=t["net"], t1=t1, mfe=mfe, post_mfe=post_mfe))
T = pd.DataFrame(rows)
T.to_csv(OUT + "/fast1.csv", index=False, encoding="utf-8")
print(f"# +1.0% 에 도달한 거래 {len(T)}건 (손절 전 도달분만)")
print("\n## +1.0% 도달 경과분 구간별 — 그 뒤 어디까지 갔나")
T["b"] = pd.cut(T.t1, [-0.1, 6, 12, 20, 30, 999],
                labels=["<=6분 (P3 rescue 조건)", "6~12분", "12~20분", "20~30분", "30분+"])
print(T.groupby("b", observed=True).agg(
    n=("mfe", "size"), 최종MFE=("mfe", "mean"), MFE중앙=("mfe", "median"),
    도달후추가=("post_mfe", "mean"),
    _2pct=("mfe", lambda s: f"{(s>=2.0).mean()*100:.0f}%"),
    _3pct=("mfe", lambda s: f"{(s>=3.0).mean()*100:.0f}%")).round(2).to_string())

print("\n## CHOP 진입(=B3 가 지배)만")
C = T[T.regime == "CHOP"]
print(f"  {len(C)}건")
print(C.groupby("b", observed=True).agg(
    n=("mfe", "size"), 최종MFE=("mfe", "mean"), MFE중앙=("mfe", "median"),
    _2pct=("mfe", lambda s: f"{(s>=2.0).mean()*100:.0f}%"),
    _3pct=("mfe", lambda s: f"{(s>=3.0).mean()*100:.0f}%")).round(2).to_string())

print("\n## 상관")
print(f"  전체  도달분 vs 최종MFE : 피어슨 {T.t1.corr(T.mfe):+.3f} / 순위 {T.t1.rank().corr(T.mfe.rank()):+.3f}")
print(f"  CHOP  도달분 vs 최종MFE : 피어슨 {C.t1.corr(C.mfe):+.3f} / 순위 {C.t1.rank().corr(C.mfe.rank()):+.3f}")
