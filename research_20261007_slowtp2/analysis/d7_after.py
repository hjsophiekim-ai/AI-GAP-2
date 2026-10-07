"""D7: 청산 '이후' 가격이 얼마나 더 갔나 = 더 길게 들고 갔으면 벌었을 금액의 상한.
   오늘 같은 날(오전 반대 큰레그 2연속) vs 나머지. READ-ONLY.
   상한이다 -- 손절/반대신호/세션종료가 먼저 걸리면 실제로는 못 얻는다.
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


def ap(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


_c = {}


def etf(day, sym):
    k = (day, sym)
    if k in _c:
        return _c[k]
    f = f"{DATA}/replay_{day}_{'long' if sym == LONG else 'inverse'}_1m.csv"
    r = None
    if os.path.exists(f):
        df = pd.read_csv(f)
        dt = pd.to_datetime(df["datetime"])
        df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
        r = df.sort_values("datetime").reset_index(drop=True)
    _c[k] = r
    return r


D = pd.read_csv(OUT + "/days.csv", dtype={"day": str}).set_index("day")
rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    pair = float(D.loc[d, "pair_min_mfe"]) if d in D.index else 0.0
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        sym = LONG if t["dir"].startswith("UP") else "0197X0"
        e = etf(d, sym)
        if e is None:
            continue
        ts = pd.DatetimeIndex(e["datetime"])
        xt = t["exit"]
        i0 = int(ts.searchsorted(xt))
        if i0 >= len(e):
            continue
        xpx = float(t["legs"][-1][2])
        r = dict(day=d, pair=pair, at=t["entry"].strftime("%H:%M"), xat=xt.strftime("%H:%M"),
                 rs="+".join(dict.fromkeys(t["reasons"])), krw=t["krw"], net=t["net"],
                 hold=t["hold_min"], q=t["q"], xpx=xpx)
        for n in (15, 30, 60):
            i1 = int(ts.searchsorted(xt + pd.Timedelta(minutes=n)))
            seg = e.iloc[i0:max(i1, i0 + 1)]
            r[f"up{n}"] = (float(seg["high"].max()) - xpx) / xpx * 100
            r[f"dn{n}"] = (float(seg["low"].min()) - xpx) / xpx * 100
        # 세션 끝(15:20)까지
        i1 = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:20", tz=KST)))
        seg = e.iloc[i0:max(i1, i0 + 1)]
        r["upEOD"] = (float(seg["high"].max()) - xpx) / xpx * 100
        rows.append(r)

X = pd.DataFrame(rows)
X.to_csv(OUT + "/after.csv", index=False, encoding="utf-8")
BIG = X.pair >= 1.0
print(f"# 거래 {len(X)}건 (오늘형 {int(BIG.sum())} / 그외 {int((~BIG).sum())})")

print("\n## 청산 이후 ETF 추가 상승 (보유 계속했다면 닿았을 최대, % — 상한)")
print("| 구간 | n | +15분 | +30분 | +60분 | 장끝까지 |")
print("|---|---|---|---|---|---|")
for lab, s in (("오늘형 날", X[BIG]), ("그외", X[~BIG])):
    print(f"| {lab} | {len(s)} | {s.up15.mean():+.2f}% | {s.up30.mean():+.2f}% | "
          f"{s.up60.mean():+.2f}% | {s.upEOD.mean():+.2f}% |")

print("\n## 조기청산 의심 사유별 (오늘형 날)")
for lab, s in (("오늘형 날", X[BIG]), ("그외", X[~BIG])):
    print(f"\n### {lab}")
    g = s.groupby("rs").agg(n=("krw", "size"), 손익=("krw", "sum"), 보유=("hold", "mean"),
                            후30=("up30", "mean"), 후60=("up60", "mean"), 후EOD=("upEOD", "mean"))
    print(g[g.n >= 2].sort_values("n", ascending=False).round(2).to_string())
