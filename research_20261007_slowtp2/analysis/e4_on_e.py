"""E4: SLOW-TP2 를 E 전략 위에서 계산 + 포트폴리오 영향. READ-ONLY.
   e2 와 같은 시뮬레이션 규칙(C1 제외 = 보수적).
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
BRKD = REPO + "/research_20261003_breakout_confirm/wk/out6"
OUT = os.path.dirname(os.path.abspath(__file__))
KST = "Asia/Seoul"; LONG = "0193T0"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades, metrics = ns["trades"], ns["metrics"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
pb = lambda d: (f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json" if os.path.exists(f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json") else f"{BRKD}/REAL_BRK15_{d}.json")
pc = lambda d: (f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json" if os.path.exists(f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json") else pb(d))
pe = lambda d: (f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json" if os.path.exists(f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json") else pc(d))
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
TH, TP2_NEW, TRAIL = 25.0, 8.0, 1.5
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})


def run(loader, tag):
    rows, daily = [], {}
    for d in DAYS:
        tt = trades(json.load(open(loader(d), encoding="utf-8")))
        daily[d] = sum(t["krw"] for t in tt)
        g = L[L.day == d].sort_values("mins")
        for t in tt:
            rs = "+".join(dict.fromkeys(t["reasons"]))
            if "TP1_PARTIAL" not in rs or "TP2_FULL" not in rs:
                continue
            ratio = t["legs"][0][1] / t["q"]
            if ratio >= 0.35 or ratio < 0.05 or t["hold_min"] < TH:
                continue
            sym = LONG if t["dir"].startswith("UP") else "0197X0"
            e = etf(d, sym)
            if e is None:
                continue
            ts = pd.DatetimeIndex(e["datetime"]); px0 = t["px_in"]
            xt, xq, xpx = t["legs"][-1]
            i0 = int(ts.searchsorted(xt))
            drag = (xpx - px0) / px0 * 100 - t["net"]
            m = t["entry"].hour * 60 + t["entry"].minute
            want = "UP_RED" if t["dir"].startswith("DN") else "DOWN_BLUE"
            nxt = g[(g.mins > m) & (g["dir"] == want)]
            opp = int(nxt.iloc[0]["mins"]) if len(nxt) else 24 * 60
            iF = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:00", tz=KST)))
            iO = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} {opp//60:02d}:{opp%60:02d}", tz=KST)))
            end = min(len(e), max(i0 + 1, min(iF, iO)))
            why, out_px = None, None
            for i in range(i0, end):
                lo = (float(e["low"].iloc[i]) - px0) / px0 * 100 - drag
                hi = (float(e["high"].iloc[i]) - px0) / px0 * 100 - drag
                if lo <= TRAIL:
                    why, out_px = "트레일", px0 * (1 + (TRAIL + drag) / 100); break
                if hi >= TP2_NEW:
                    why, out_px = "TP2(8.0)", px0 * (1 + (TP2_NEW + drag) / 100); break
            if out_px is None:
                j = max(min(end, len(e)) - 1, i0)
                why = "반대신호" if iO < iF else "강제청산"
                out_px = float(e["close"].iloc[j])
            dl = xq * (out_px - xpx)
            daily[d] += dl
            rows.append(dict(day=d, at=t["entry"].strftime("%H:%M"), hold=t["hold_min"],
                             net=t["net"], why=why, delta=dl))
    return pd.DataFrame(rows), daily


print("## SLOW-TP2 (비추세 가지 · TP2 4.0 도달까지 >=25분 · 천장 8.0)")
for loader, tag in ((ap, "A 현행 N1/P3"), (pe, "E UP-FAST+RS125")):
    R, daily = run(loader, tag)
    base = {d: sum(t["krw"] for t in trades(json.load(open(loader(d), encoding="utf-8")))) for d in DAYS}
    b = np.array([base[d] for d in DAYS]); n = np.array([daily[d] for d in DAYS])
    def mk(a):
        r = a / 10_000_000 * 100; eq = np.cumprod(1 + r / 100)
        return (eq[-1] - 1) * 100, (eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100, a.min(), int((a < 0).sum())
    cb, mb, lb, db = mk(b); cn, mn, ln, dn = mk(n)
    print(f"\n### {tag} — 발동 {len(R)}건 · 증분 {R.delta.sum():+,.0f}원")
    print("| 지표 | 현행 | SLOW-TP2 | 차이 |")
    print("|---|---|---|---|")
    print(f"| 총손익 | {b.sum():+,.0f} | {n.sum():+,.0f} | **{n.sum()-b.sum():+,.0f}** |")
    print(f"| 복리% | {cb:+.2f} | {cn:+.2f} | **{cn-cb:+.2f}** |")
    print(f"| MDD% | {mb:.2f} | {mn:.2f} | {mn-mb:+.2f} |")
    print(f"| 최대1일손실 | {lb:+,.0f} | {ln:+,.0f} | {ln-lb:+,.0f} |")
    print(f"| 손실일 | {db} | {dn} | {dn-db:+d} |")
    if len(R):
        print("\n" + R.to_string(index=False))
