"""D9: 청산 시점에 전량 나가는 대신 '잔량 트레일링 러너'로 돌렸다면? READ-ONLY.

오라클이 아니다 -- 실제 트레일링 규칙(peak 대비 G%p 반납 시 청산, 15:20 강제)을
보유 ETF 1분봉에 그대로 적용한다. 1분봉 안의 순서는 보수적으로 저가 먼저 본다.
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


def trail(e, i0, px0, give):
    """i0 봉부터 트레일링. peak 대비 give %p 반납하면 그 가격에 청산. 반환 = (수익%, 청산봉)."""
    peak = px0
    for i in range(i0, len(e)):
        lo, hi = float(e["low"].iloc[i]), float(e["high"].iloc[i])
        stop = peak * (1 - give / 100)
        if lo <= stop:                      # 보수적: 같은 봉에선 저가 먼저
            return (stop - px0) / px0 * 100, i
        peak = max(peak, hi)
    j = len(e) - 1
    return (float(e["close"].iloc[j]) - px0) / px0 * 100, j


D = pd.read_csv(OUT + "/days.csv", dtype={"day": str}).set_index("day")
GIVE = (1.0, 1.5, 2.0, 2.5)
FRAC = 0.5          # 잔량 비중: TP2 에서 전량 대신 절반만 남긴다
rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    pair = float(D.loc[d, "pair_min_mfe"]) if d in D.index else 0.0
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        sym = LONG if t["dir"].startswith("UP") else "0197X0"
        e = etf(d, sym)
        if e is None:
            continue
        ts = pd.DatetimeIndex(e["datetime"])
        i0 = int(ts.searchsorted(t["exit"]))
        if i0 >= len(e):
            continue
        # 15:20 이후는 안 본다
        cut = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:20", tz=KST)))
        e2 = e.iloc[:max(cut, i0 + 1)].reset_index(drop=True)
        xpx = float(t["legs"][-1][2])
        r = dict(day=d, pair=pair, rs="+".join(dict.fromkeys(t["reasons"])),
                 krw=t["krw"], q=t["q"], px_in=t["px_in"], xpx=xpx)
        for gv in GIVE:
            add_pct, _ = trail(e2, i0, xpx, gv)
            # 잔량 FRAC 을 청산가에 팔지 않고 트레일링했을 때의 증분(원)
            r[f"g{gv}"] = t["q"] * FRAC * xpx * add_pct / 100
        rows.append(r)

R = pd.DataFrame(rows)
R.to_csv(OUT + "/runner.csv", index=False, encoding="utf-8")
BIG = R.pair >= 1.0
TPL = R.rs.str.contains("TP2_FULL|AFTERNOON_TP|OPPOSITE_SIGNAL|TRAILING_STOP|AFTER_TP1_STOP")
print(f"# 거래 {len(R)}건 · 오늘형 {int(BIG.sum())}")
print("\n## '익절·반대신호로 끝난 거래의 잔량 50% 를 트레일링' 증분(원)")
print("| 반납폭 | 오늘형 날 n | 오늘형 증분 | 그외 n | 그외 증분 | 전체 증분 |")
print("|---|---|---|---|---|---|")
for gv in GIVE:
    a, b = R[BIG & TPL], R[~BIG & TPL]
    print(f"| {gv:.1f}%p | {len(a)} | **{a[f'g{gv}'].sum():+,.0f}** | {len(b)} | {b[f'g{gv}'].sum():+,.0f} | "
          f"{R[TPL][f'g{gv}'].sum():+,.0f} |")

print("\n## 같은 규칙을 '손절로 끝난 거래'에 적용하면 (반증 — 규칙은 사유를 모른다면)")
SL = R.rs.str.contains("STOP_LOSS|B3_SL")
for gv in (1.5,):
    a, b = R[BIG & SL], R[~BIG & SL]
    print(f"| {gv}%p | 오늘형 {len(a)}건 {a[f'g{gv}'].sum():+,.0f} | 그외 {len(b)}건 {b[f'g{gv}'].sum():+,.0f} |")

print("\n## 오늘형 날 · 사유별 증분 (반납 1.5%p)")
g = R[BIG].groupby("rs").agg(n=("krw", "size"), 원손익=("krw", "sum"), 증분=("g1.5", "sum"))
print(g[g.n >= 1].sort_values("증분", ascending=False).round(0).to_string())
