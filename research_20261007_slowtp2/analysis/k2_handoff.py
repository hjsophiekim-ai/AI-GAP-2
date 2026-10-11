"""K2: 'B3 +1% 전량익절 제거 + 20분에 N1 으로 넘길지 판정' — 판정조건 변형 비교. READ-ONLY.

설계: 진입은 E 그대로(CHOP 라벨 -> B3 지배). 보유 중
  · B3 SL(-1.0%) 과 P3 rescue(<=6분) 는 그대로 둔다
  · B3 TP(+1.0% 전량) 는 **판정 보류**로 바꾼다
  · 진입 +20분에 조건 판정 -> 통과면 N1 래더로 넘기고(= 이후 B3 없음),
    실패면 그 자리에서 청산(현행 B3_MAXHOLD 와 같다)
N1 래더 근사: TP1 3.0%(20% 익절) / after-TP1 +0.3 / peak>=3.5 면 trailing +1.5 /
  TP2 4.0 / SL -1.7 / 반대플래그 확정 / 15:20. (추세가지 8.0 은 쓰지 않는다 = 보수적)
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
from app.trading.macd2 import p3_stack
from app.trading.macd2.signal_engine import resample_completed_3m, exclude_preopen_padding_1m
from app.trading.macd2.models import Direction
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
ALLD = sorted({os.path.basename(p)[7:15] for p in glob.glob(DATA + "/replay_*_hynix_1m.csv")}
              | {os.path.basename(p)[7:15] for p in glob.glob(TD + "/replay_*_hynix_1m.csv")})
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


def hist3(day, nprev=3):
    i = ALLD.index(day)
    parts = [one(d, "hynix") for d in ALLD[max(0, i - nprev):i + 1]]
    return pd.concat([p for p in parts if p is not None], ignore_index=True)


def sim(day, entry, direction, px0, opp_min):
    """20분 판정 + 그 뒤 N1 래더. 반환 = {변형: 최종 가중 net%}"""
    sym = LONG if direction.startswith("UP") else "0197X0"
    e = one(day, "long" if sym == LONG else "inverse")
    if e is None:
        return None
    ts = pd.DatetimeIndex(e["datetime"])
    i0 = int(ts.searchsorted(entry))
    iO = int(ts.searchsorted(pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} "
                                          f"{opp_min//60:02d}:{opp_min%60:02d}", tz=KST)))
    iE = int(ts.searchsorted(pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 15:20", tz=KST)))
    lim = min(iO, iE, len(e))
    n = lambda i, w: (float(e[w].iloc[i]) - px0) / px0 * 100 - DRAG
    t20 = entry + timedelta(minutes=20)
    i20 = int(ts.searchsorted(t20))
    # 20분 전 구간: B3 SL(-1%) 과 P3 rescue(<=6분 +1% 도달) 는 그대로 살아 있다
    mae20, rescued = 0.0, False
    for i in range(i0, min(i20, lim)):
        mae20 = min(mae20, n(i, "low"))
        if n(i, "low") <= -1.0:
            return dict(pre="B3_SL", **{k: -1.0 for k in VAR})
        if not rescued and n(i, "high") >= 1.0 and (ts[i] - entry).total_seconds() / 60 <= 6.0:
            rescued = True
    if i20 >= lim:
        return None
    # 20분 시점 관측치
    now = ts[i20]
    cur = float(e["close"].iloc[max(i20 - 1, i0)])
    prv_i = int(ts.searchsorted(t20 - timedelta(minutes=3))) - 1
    prv = float(e["close"].iloc[max(prv_i, i0)])
    net20 = (cur - px0) / px0 * 100 - DRAG
    clean, _ = exclude_preopen_padding_1m(hist3(day))
    b3 = resample_completed_3m(clean, now=now.to_pydatetime())
    dd = Direction.UP_RED if direction.startswith("UP") else Direction.DOWN_BLUE
    gap_ok, _ = p3_stack.macd_gap_expanding(b3, now, dd)
    etf_ok, _ = p3_stack.etf_following(prv, cur)
    peak20 = max((n(i, "high") for i in range(i0, i20)), default=0.0)
    near_high = cur >= float(e["high"].iloc[i0:i20].max()) * (1 - 0.003)

    conds = dict(
        Y3=(net20 > 0 and gap_ok and etf_ok),                       # 현행
        ETF=(net20 > 0 and etf_ok),                                 # gap 조건 제거
        CLEAN=(net20 > 0 and etf_ok and -mae20 <= 0.5),             # + 무반락
        HIGH=(net20 > 0 and etf_ok and near_high),                  # + 고점 부근
        NET=(net20 > 0),                                            # net 만
    )

    def ladder():
        tp1_done, peak, part = False, 0.0, 0.0
        for i in range(i20, lim):
            lo, hi = n(i, "low"), n(i, "high")
            peak = max(peak, hi)
            if not tp1_done:
                if lo <= -1.7:
                    return -1.7
                if hi >= 4.0:
                    return 4.0
                if hi >= 3.0:
                    tp1_done = True; part = 0.2 * 3.0
                    continue
            else:
                if hi >= 4.0:
                    return part + 0.8 * 4.0
                stop = 1.5 if peak >= 3.5 else 0.3
                if lo <= stop:
                    return part + 0.8 * stop
        j = max(lim - 1, i20)
        return (part + 0.8 * n(j, "close")) if tp1_done else n(j, "close")

    hold_val = ladder()
    out = dict(pre="OK", net20=net20, gap=gap_ok, etf=etf_ok, mae20=-mae20, peak20=peak20)
    for k, ok in conds.items():
        out[k] = hold_val if ok else net20
    out["promote"] = conds
    return out


VAR = ("Y3", "ETF", "CLEAN", "HIGH", "NET")
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
        r = sim(d, t["entry"], t["dir"], t["px_in"], opp)
        if r is None:
            continue
        rows.append(dict(day=d, at=t["entry"].strftime("%H:%M"), dir=t["dir"],
                         rs="+".join(dict.fromkeys(t["reasons"])), krw=t["krw"],
                         notional=t["q"] * t["px_in"], pre=r["pre"],
                         **{k: r[k] for k in VAR},
                         **{f"p_{k}": r["promote"][k] for k in VAR} if r["pre"] == "OK" else {}))
T = pd.DataFrame(rows)
T.to_csv(OUT + "/handoff.csv", index=False, encoding="utf-8")
A = T[T.pre == "OK"]
print(f"# CHOP 진입 {len(T)}건 중 20분까지 생존 {len(A)}건 (나머지 {len(T)-len(A)}건은 그 전 B3_SL)")
print(f"# 현행 실제 손익(해당 {len(T)}건 합) {T.krw.sum():+,.0f}원")
print("\n## 판정조건 변형별 — 20분에 N1 으로 넘긴 건수와 손익(원)")
print("| 조건 | 승격 | 손익 합 | 현행 대비 | 승 | 패 |")
print("|---|---|---|---|---|---|")
base = T.krw.sum()
for k in VAR:
    v = T[k] / 100 * T.notional
    n_pro = int(A[f"p_{k}"].sum()) if f"p_{k}" in A else 0
    d = v - T.krw
    print(f"| {k} | {n_pro} | {v.sum():+,.0f} | **{v.sum()-base:+,.0f}** | {int((d>0).sum())} | {int((d<0).sum())} |")
