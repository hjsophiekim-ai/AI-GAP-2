"""E5: 연장 후 실제 청산시각 -> 슬롯 충돌 최종 확인 + 플래그수 조건의 기여도. READ-ONLY."""
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
        df = pd.read_csv(f)
        dt = pd.to_datetime(df["datetime"])
        df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
        _c[k] = df.sort_values("datetime").reset_index(drop=True)
    return _c[k]


L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})
R = pd.read_csv(OUT + "/tp2.csv", dtype={"day": str})
S = R[R.hold >= 25].sort_values("day")
print("## 연장 후 청산시각 vs 같은 날 다음 진입")
print("| 일자 | 진입 | 기존청산(TP2 4.0) | **연장 청산** | 사유 | 다음 진입 | 충돌 |")
print("|---|---|---|---|---|---|---|")
clash = []
for _, r in S.iterrows():
    d = r.day
    tt = trades(json.load(open(ap(d), encoding="utf-8")))
    cur = next(t for t in tt if t["entry"].strftime("%H:%M") == r["at"])
    xt, xq, xpx = cur["legs"][-1]
    sym = LONG if cur["dir"].startswith("UP") else "0197X0"
    e = etf(d, sym); ts = pd.DatetimeIndex(e["datetime"]); px0 = cur["px_in"]
    drag = (xpx - px0) / px0 * 100 - cur["net"]
    g = L[L.day == d].sort_values("mins")
    m = cur["entry"].hour * 60 + cur["entry"].minute
    want = "UP_RED" if cur["dir"].startswith("DN") else "DOWN_BLUE"
    nx = g[(g.mins > m) & (g["dir"] == want)]
    opp = int(nx.iloc[0]["mins"]) if len(nx) else 24 * 60
    i0 = int(ts.searchsorted(xt))
    iF = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:00", tz=KST)))
    iO = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} {opp//60:02d}:{opp%60:02d}", tz=KST)))
    end = min(len(e), max(i0 + 1, min(iF, iO)))
    newt, why = None, None
    for i in range(i0, end):
        lo = (float(e["low"].iloc[i]) - px0) / px0 * 100 - drag
        hi = (float(e["high"].iloc[i]) - px0) / px0 * 100 - drag
        if lo <= 1.5:
            newt, why = ts[i], "트레일"; break
        if hi >= 8.0:
            newt, why = ts[i], "TP2(8.0)"; break
    if newt is None:
        newt = ts[max(min(end, len(e)) - 1, i0)]
        why = "반대신호" if iO < iF else "강제청산"
    nxt = [t for t in tt if t["entry"] > xt]
    ne = nxt[0]["entry"] if nxt else None
    bad = bool(ne is not None and newt > ne)
    if bad:
        clash.append((d, nxt[0]["krw"]))
    print(f"| {d} | {r['at']} | {xt.strftime('%H:%M')} | **{newt.strftime('%H:%M')}** | {why} | "
          f"{ne.strftime('%H:%M') if ne is not None else '-'} | {'**충돌**' if bad else '없음'} |")
print(f"\n→ 실제 충돌 {len(clash)}건 {clash}")

print("\n## '당일 플래그 수' 조건을 추가하면 기여가 있는가 (임계 25분 기준)")
print("| 조건 | n | 증분 |")
print("|---|---|---|")
print(f"| 플래그수 무관(현 후보) | {len(S)} | **{S.delta.sum():+,.0f}** |")
for k in (1, 2):
    s = S[S.nflag <= k]
    print(f"| 당일 플래그 <= {k} | {len(s)} | {s.delta.sum():+,.0f} |")
