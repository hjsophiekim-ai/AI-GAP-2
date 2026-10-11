"""K3: B3->N1 인계 설계 3종 비교. READ-ONLY.

A  현행 TP/SL 유지 + 20분 Y3 조건만 완화 (= B3_MAXHOLD 로 끝나던 건만 영향)
B  +1% 전량익절 제거 + 20분 판정 (k2, 이미 전부 음수)
C  +1% 도달 즉시 **20% 익절 + 잔량 N1 인계** (6분 조건 없이) -- 잔량 스탑 변형별
현행 = B3 그대로.
"""
import glob, json, os, sys
from datetime import timedelta
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
sys.path.insert(0, REPO)
ROOT = REPO + "/research_20261004_chop_staged"
DATA = ROOT + "/wk/data"; TD = REPO + "/data/cache"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
OUT = os.path.dirname(os.path.abspath(__file__))
KST = "Asia/Seoul"; LONG = "0193T0"; DRAG = 0.05
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})
_c = {}


def one(day, tag):
    k = (day, tag)
    if k not in _c:
        r = None
        for sd in (DATA, TD):
            f = f"{sd}/replay_{day}_{tag}_1m.csv"
            if os.path.exists(f):
                df = pd.read_csv(f); dt = pd.to_datetime(df["datetime"])
                df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
                r = df.sort_values("datetime").reset_index(drop=True); break
        _c[k] = r
    return _c[k]


def run(day, entry, direction, px0, opp_min, stops):
    """설계 C: +1% 최초도달 즉시 20% 익절 + 잔량은 N1 래더(스탑 F). 반환 = {F: 가중 net%}"""
    sym = LONG if direction.startswith("UP") else "0197X0"
    e = one(day, "long" if sym == LONG else "inverse")
    if e is None:
        return None
    ts = pd.DatetimeIndex(e["datetime"]); i0 = int(ts.searchsorted(entry))
    iO = int(ts.searchsorted(pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} "
                                          f"{opp_min//60:02d}:{opp_min%60:02d}", tz=KST)))
    iE = int(ts.searchsorted(pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 15:20", tz=KST)))
    lim = min(iO, iE, len(e))
    n = lambda i, w: (float(e[w].iloc[i]) - px0) / px0 * 100 - DRAG
    j1 = None
    for i in range(i0, max(lim, i0 + 1)):
        if n(i, "low") <= -1.0:
            return {f: -1.0 for f in stops}        # B3 SL 먼저
        if n(i, "high") >= 1.0:
            j1 = i; break
    if j1 is None:
        # +1% 미도달 -> 현행과 동일 경로(20분 maxhold 등). 변형 영향 없음
        return None
    res = {}
    for F in stops:
        tp1_done, peak, part, out = False, 1.0, 0.0, None
        for i in range(j1, max(lim, j1 + 1)):
            lo, hi = n(i, "low"), n(i, "high")
            peak = max(peak, hi)
            if not tp1_done:
                if lo <= F:
                    out = F; break
                if hi >= 4.0:
                    out = 4.0; break
                if hi >= 3.0:
                    tp1_done = True; part = 0.2 * 3.0
                    continue
            else:
                if hi >= 4.0:
                    out = part + 0.8 * 4.0; break
                st = max(1.5 if peak >= 3.5 else 0.3, F)
                if lo <= st:
                    out = part + 0.8 * st; break
        if out is None:
            j = max(lim - 1, j1)
            out = (part + 0.8 * n(j, "close")) if tp1_done else n(j, "close")
        res[F] = 0.2 * 1.0 + 0.8 * out if not tp1_done else 0.2 * 1.0 + 0.8 * out
    return res


STOPS = (-1.0, 0.0, 0.2, 0.5)
rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    g = L[L.day == d].sort_values("mins")
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        if t.get("regime") != "CHOP":
            continue
        m = t["entry"].hour * 60 + t["entry"].minute
        want = "UP_RED" if t["dir"].startswith("DN") else "DOWN_BLUE"
        nx = g[(g.mins > m) & (g["dir"] == want)]
        opp = int(nx.iloc[0]["mins"]) if len(nx) else 15 * 60 + 20
        r = run(d, t["entry"], t["dir"], t["px_in"], opp, STOPS)
        if r is None:
            continue
        rows.append(dict(day=d, at=t["entry"].strftime("%H:%M"), krw=t["krw"],
                         notional=t["q"] * t["px_in"],
                         rs="+".join(dict.fromkeys(t["reasons"])),
                         **{f"s{F}": v for F, v in r.items()}))
T = pd.DataFrame(rows)
T.to_csv(OUT + "/designC.csv", index=False, encoding="utf-8")
print(f"# 설계 C 영향권 = CHOP 진입 중 '+1% 도달 또는 그 전 -1% 손절' {len(T)}건")
print(f"# 현행 실제 손익 {T.krw.sum():+,.0f}원")
print("\n## +1% 즉시 20% 익절 + 잔량 N1 인계 (잔량 스탑 F)")
print("| 잔량 스탑 | 손익 합 | 현행 대비 | 승 | 패 | 1건 최악 |")
print("|---|---|---|---|---|---|")
for F in STOPS:
    v = T[f"s{F}"] / 100 * T.notional
    d = v - T.krw
    print(f"| {F:+.1f}% | {v.sum():+,.0f} | **{d.sum():+,.0f}** | {int((d>0).sum())} | {int((d<0).sum())} | {d.min():+,.0f} |")

# ── 오늘 ──
ent = pd.Timestamp("2026-10-07 10:03", tz=KST)
e7 = one("20261007", "inverse")
px0 = float(e7["open"].iloc[int(pd.DatetimeIndex(e7["datetime"]).searchsorted(ent))])
r = run("20261007", ent, "DN 인버", px0, 11 * 60 + 30, STOPS)
r2 = run("20261007", ent, "DN 인버", px0, 15 * 60 + 20, STOPS)
print(f"\n## 오늘 10/07 (현행 B3 = +1.00%)")
print("| 잔량 스탑 | 반대신호(11:30)까지 | EOD 까지 |")
print("|---|---|---|")
for F in STOPS:
    print(f"| {F:+.1f}% | **{r[F]:+.2f}%** ({r[F]-1.0:+.2f}%p) | **{r2[F]:+.2f}%** ({r2[F]-1.0:+.2f}%p) |")
