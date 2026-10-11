"""E2: 비추세 가지(TP2 4.0) 로 잘린 거래에 TP2 8.0 을 적용하면? READ-ONLY.

최소 변경 = TP1(3.0 / 20%) 과 손절·after-TP1 스탑은 그대로 두고 **TP2 천장만 4.0 -> 8.0**.
TP2 4.0 체결 시점부터 보유 ETF 1분봉을 따라가며 다음 중 가장 이른 것에서 청산:
   (a) net >= 8.0%           TP2_FULL
   (b) net <= 1.5%           N1 trailing stop (peak>=3.5 이므로 이미 활성)
   (c) 반대방향 플래그 확정   OPPOSITE_SIGNAL (신호원장 실제 시각)
   (d) 15:00                 강제청산
C1 peak protection(MFE>=5% + gap반전 + 1.5%p 반납)은 **일부러 뺐다** -- 넣으면
더 좋은 가격에 나가므로, 빼는 쪽이 보수적이다.
봉 내부 순서는 보수적으로 저가(=손절쪽) 먼저 본다.
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
L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})
TP2_NEW, TRAIL, FORCE = 8.0, 1.5, "15:00"
rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    o = json.load(open(ap(d), encoding="utf-8"))
    pair = float(D.loc[d, "pair_min_mfe"]) if d in D.index else 0.0
    g = L[L.day == d].sort_values("mins")
    for t in trades(o):
        rs = "+".join(dict.fromkeys(t["reasons"]))
        if "TP1_PARTIAL" not in rs or "TP2_FULL" not in rs:
            continue
        ratio = t["legs"][0][1] / t["q"]
        if ratio >= 0.35 or ratio < 0.05:
            continue                       # 비추세 가지(0.2)만 대상
        sym = LONG if t["dir"].startswith("UP") else "0197X0"
        e = etf(d, sym)
        if e is None:
            continue
        ts = pd.DatetimeIndex(e["datetime"])
        px0 = t["px_in"]
        xt, xqty, xpx = t["legs"][-1]
        i0 = int(ts.searchsorted(xt))
        drag = (xpx - px0) / px0 * 100 - t["net"]     # 이 거래의 비용 drag(%p)
        m = t["entry"].hour * 60 + t["entry"].minute
        prior = g[g.mins <= m]
        # 보유 중 '다음 반대방향 플래그' 시각
        want = "UP_RED" if t["dir"].startswith("DN") else "DOWN_BLUE"
        nxt = g[(g.mins > m) & (g["dir"] == want)]
        opp_m = int(nxt.iloc[0]["mins"]) if len(nxt) else 24 * 60
        force = pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} {FORCE}", tz=KST)
        iF = int(ts.searchsorted(force))
        iO = int(ts.searchsorted(pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} "
                                              f"{opp_m//60:02d}:{opp_m%60:02d}", tz=KST)))
        end = min(len(e), max(i0 + 1, min(iF, iO)))
        why, out_px = "시간/반대", None
        for i in range(i0, end):
            lo = (float(e["low"].iloc[i]) - px0) / px0 * 100 - drag
            hi = (float(e["high"].iloc[i]) - px0) / px0 * 100 - drag
            if lo <= TRAIL:
                why, out_px = "트레일(+1.5)", px0 * (1 + (TRAIL + drag) / 100); break
            if hi >= TP2_NEW:
                why, out_px = "TP2(8.0)", px0 * (1 + (TP2_NEW + drag) / 100); break
        if out_px is None:
            j = min(end, len(e)) - 1
            why = "반대신호" if iO < iF else "강제청산"
            out_px = float(e["close"].iloc[max(j, i0)])
        new_net = (out_px - px0) / px0 * 100 - drag
        qrem = xqty                                   # TP2 에서 팔린 잔량 = 80%
        rows.append(dict(day=d, pair=pair, at=t["entry"].strftime("%H:%M"), dir=t["dir"],
                         regime=t.get("regime"), nflag=len(prior), krw=t["krw"], net=t["net"],
                         q=t["q"], qrem=qrem, px0=px0, xpx=xpx, new_px=out_px,
                         new_net=new_net, why=why,
                         delta=qrem * (out_px - xpx) * (1 - 0.0 / 100),
                         hold=t["hold_min"]))
R = pd.DataFrame(rows)
R.to_csv(OUT + "/tp2.csv", index=False, encoding="utf-8")
R["seg"] = np.where(R.day < "20260801", "train 5~7월", "test 8~10월")
print(f"# 대상(비추세 가지 + TP2 4.0 체결) {len(R)}건")
print(f"# 증분 합계 **{R.delta.sum():+,.0f}원**  (양수 {int((R.delta>0).sum())} / 음수 {int((R.delta<0).sum())})")
print("\n## 새 청산사유 분포")
print(R.groupby("why").agg(n=("delta", "size"), 증분=("delta", "sum"), 새net=("new_net", "mean")).round(1).to_string())
print("\n## 기간")
print(R.groupby("seg").agg(n=("delta", "size"), 증분=("delta", "sum")).round(0).to_string())
print("\n## 집중도")
s = R.sort_values("delta", ascending=False)
for k in (1, 2, 3, 5):
    print(f"  상위 {k}건 제외 → {R.delta.sum() - s.delta.head(k).sum():+,.0f}")
print("\n## 전체 16건")
print("| 일자 | 진입 | 방향 | regime | 당일플래그수 | 기존net | 새net | 새사유 | 증분 |")
print("|---|---|---|---|---|---|---|---|---|")
for _, r in R.sort_values("day").iterrows():
    print(f"| {r.day} | {r.at} | {r.dir} | {r.regime} | {int(r.nflag)} | {r.net:+.2f}% | "
          f"{r.new_net:+.2f}% | {r.why} | **{r.delta:+,.0f}** |")
