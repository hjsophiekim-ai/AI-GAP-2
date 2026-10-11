"""D14: 오늘 같은 날 손절폭을 넓히면? (현행 오전 -1.7% / TP1 +3.0%) READ-ONLY.
   진입가부터 보유 ETF 1분봉을 따라가며, 넓힌 손절 S 와 TP1(+3.0%) 중 무엇이 먼저인지 센다.
   1분봉 내부 순서는 보수적으로 '저가 먼저'.
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


D = pd.read_csv(OUT + "/days.csv", dtype={"day": str}).set_index("day")
STOPS = (-1.7, -2.2, -2.7, -3.2)
TP1 = 3.0
rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    pair = float(D.loc[d, "pair_min_mfe"]) if d in D.index else 0.0
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        rs = "+".join(dict.fromkeys(t["reasons"]))
        if "STOP_LOSS" not in rs or "TP1" in rs:
            continue
        sym = LONG if t["dir"].startswith("UP") else "0197X0"
        e = etf(d, sym)
        if e is None:
            continue
        ts = pd.DatetimeIndex(e["datetime"])
        i0 = int(ts.searchsorted(t["entry"]))
        cut = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:20", tz=KST)))
        px0 = t["px_in"]
        r = dict(day=d, pair=pair, at=t["entry"].strftime("%H:%M"), krw=t["krw"], q=t["q"], px0=px0)
        for S in STOPS:
            res, k = "미결", None
            for i in range(i0, max(cut, i0 + 1)):
                lo = (float(e["low"].iloc[i]) - px0) / px0 * 100
                hi = (float(e["high"].iloc[i]) - px0) / px0 * 100
                if lo <= S:
                    res, k = "손절", i; break
                if hi >= TP1:
                    res, k = "TP1도달", i; break
            r[f"s{S}"] = res
            r[f"k{S}"] = (k - i0) if k is not None else np.nan
            # 손익 근사: 손절이면 S, TP1 도달이면 TP1 에서 50% + 잔량은 그 뒤 트레일 1.5%p
            if res == "손절":
                r[f"p{S}"] = t["q"] * px0 * S / 100
            elif res == "TP1도달":
                peak = px0 * (1 + TP1 / 100)
                for i in range(k, max(cut, k + 1)):
                    lo = float(e["low"].iloc[i]); hi = float(e["high"].iloc[i])
                    if lo <= peak * (1 - 1.5 / 100):
                        px1 = peak * (1 - 1.5 / 100); break
                    peak = max(peak, hi)
                else:
                    px1 = float(e["close"].iloc[max(cut, k) - 1])
                pct = (TP1 * 0.5) + ((px1 - px0) / px0 * 100) * 0.5
                r[f"p{S}"] = t["q"] * px0 * pct / 100
            else:
                r[f"p{S}"] = t["krw"]
        rows.append(r)

X = pd.DataFrame(rows)
X.to_csv(OUT + "/stop.csv", index=False, encoding="utf-8")
BIG = X.pair >= 1.0
print(f"# 손절로 끝난 거래 {len(X)}건 (오늘형 {int(BIG.sum())} / 그외 {int((~BIG).sum())}) · 실제손익 {X.krw.sum():+,.0f}")
print("\n## 손절폭을 넓혔을 때 — TP1(+3.0%) 선도달 전환 수와 손익 근사")
print("| 손절폭 | 오늘형 TP1전환 | 오늘형 손익 | 실제대비 | 그외 TP1전환 | 그외 손익 | 실제대비 |")
print("|---|---|---|---|---|---|---|")
for S in STOPS:
    a, b = X[BIG], X[~BIG]
    print(f"| {S}% | {int((a[f's{S}']=='TP1도달').sum())}/{len(a)} | {a[f'p{S}'].sum():+,.0f} | "
          f"**{a[f'p{S}'].sum()-a.krw.sum():+,.0f}** | {int((b[f's{S}']=='TP1도달').sum())}/{len(b)} | "
          f"{b[f'p{S}'].sum():+,.0f} | **{b[f'p{S}'].sum()-b.krw.sum():+,.0f}** |")
print("\n## 기간 안정성 (손절폭 -2.7%)")
X["seg"] = np.where(X.day < "20260801", "train 5~7월", "test 8~10월")
for seg, s in X.groupby("seg"):
    a, b = s[s.pair >= 1.0], s[s.pair < 1.0]
    print(f"| {seg} | 오늘형 {len(a)}건 {a['p-2.7'].sum()-a.krw.sum():+,.0f} | 그외 {len(b)}건 {b['p-2.7'].sum()-b.krw.sum():+,.0f} |")
