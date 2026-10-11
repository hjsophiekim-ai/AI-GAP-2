"""pm_run — 프리마켓 bias(09:30 이전 첫 정규장 플래그 전용) 연구 실행기.

PM 판정: production n1_adaptive.snapshot 의 3개 조건을 그대로 쓴다(새 지표 없음)
  ① close vs EMA50   ② EMA20 vs EMA50   ③ EMA50 slope   (하이닉스 3분봉, EWM adjust=False)
  09:00 freeze = bar_start + 3분 <= 09:00 인 마지막 완성봉(08:57봉)까지만.
  3/3 같은 방향 STRONG, 2/3 WEAK, 봉 부족/해당일 프리마켓 봉 없음 MIXED.

python pm_run.py PM1 PM3        (R0 = out_BASE.pkl, PM2 = 해석적)
"""
import pickle
import sys
import time
from datetime import time as dtime

import numpy as np
import pandas as pd

import b3lib as L
from common import summarize
from app.trading.macd2 import config

bars = L.ctx.hynix_bars_3m
starts = pd.to_datetime(bars["datetime"])
close = bars["close"].astype(float)
E20 = close.ewm(span=int(config.H50_TREND_EMA_FAST), adjust=False).mean().to_numpy()
E50 = close.ewm(span=int(config.H50_TREND_EMA_SLOW), adjust=False).mean().to_numpy()
C = close.to_numpy()


def pm_for_day(d):
    """(dir, strength, detail) — 09:00 freeze."""
    day = pd.Timestamp(d).date()
    cand = [i for i in range(len(bars))
            if starts.iloc[i].date() == day and starts.iloc[i].time() < dtime(9, 0)
            and (starts.iloc[i] + pd.Timedelta(minutes=3)).time() <= dtime(9, 0)]
    if not cand:
        return "PM_MIXED", "MIXED", {"reason": "no_premarket_bars"}
    i = max(cand)
    if i < int(config.H50_TREND_EMA_SLOW) + 1:
        return "PM_MIXED", "MIXED", {"reason": "insufficient"}
    blue = [C[i] < E50[i], E20[i] < E50[i], (E50[i] - E50[i - 1]) < 0]
    red = [C[i] > E50[i], E20[i] > E50[i], (E50[i] - E50[i - 1]) > 0]
    nb, nr = sum(blue), sum(red)
    det = {"bar": starts.iloc[i].strftime("%H:%M"), "close": C[i], "ema20": E20[i], "ema50": E50[i],
           "slope": E50[i] - E50[i - 1], "blue_votes": nb, "red_votes": nr}
    if nb == 3:
        return "PM_BLUE", "STRONG", det
    if nr == 3:
        return "PM_RED", "STRONG", det
    if nb == 2 and nr <= 1:
        return "PM_BLUE", "WEAK", det
    if nr == 2 and nb <= 1:
        return "PM_RED", "WEAK", det
    return "PM_MIXED", "MIXED", det


PM = {d: pm_for_day(d) for d in L.DATES}

# 그날 첫 정규장 확정 플래그 (worker 가 pending 으로 받는 범위 = SESSION_OPEN<=t<NEW_ENTRY_CUTOFF)
FIRST = {}
for i, dirn in sorted(L.ctx.flags_by_idx.items()):
    t = starts.iloc[i]
    d = t.strftime("%Y%m%d")
    if d in FIRST or d not in PM:
        continue
    if config.SESSION_OPEN <= t.time() < config.NEW_ENTRY_CUTOFF:
        FIRST[d] = (i, dirn.value, t.strftime("%H:%M"))

CUT = dtime(9, 30)


def targeted(d):
    """PM STRONG ∧ 09:30 이전 첫 플래그 ∧ PM 반대방향 → 첫 플래그 idx, 아니면 None."""
    if d not in FIRST:
        return None
    i, dv, hm = FIRST[d]
    pmd, st, _ = PM[d]
    if st != "STRONG" or starts.iloc[i].time() >= CUT:
        return None
    opposite = (pmd == "PM_BLUE" and dv == "UP_RED") or (pmd == "PM_RED" and dv == "DOWN_BLUE")
    return i if opposite else None


def make_gate(mode):
    used = set()

    def gate(g):
        d = g["date"]
        if d in used:
            return None
        ti = targeted(d)
        if ti is None or g["flag_idx"] != ti:
            return None
        used.add(d)                     # 첫 후보 처리 완료 -> 그날 bias 종료
        return "PM3_REJECT" if mode == "PM3" else "PM1_DEFER"
    return gate


if __name__ == "__main__":
    pickle.dump({"PM": PM, "FIRST": FIRST}, open(L.HERE / "pm_meta.pkl", "wb"))
    for name in [a for a in sys.argv[1:] if not a.startswith("--")]:
        t0 = time.time()
        L.hard_reset()
        L.Z.set_gate(lambda f: True); L.Z.RELAXED_KEYS.clear(); L.MODE["on"] = True
        ts = L.A.run("N1", L.ctx, L.DATES, ax=L.AX, gate=make_gate(name))
        m = summarize(ts, L.DATES)
        print("%-5s 거래 %3d 복리 %9.4f PF %.4f MDD %7.3f 승률 %.2f%% (%.0fs)" % (
            name, m["trades"], m["compound_pct"], m["pf"], m["mdd_pct"], m["win_rate_pct"],
            time.time() - t0), flush=True)
        pickle.dump({"trades": ts, "m": m}, open(L.HERE / f"out_{name}.pkl", "wb"))
