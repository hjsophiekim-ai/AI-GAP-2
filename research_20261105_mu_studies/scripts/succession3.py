"""08:4x~09:00 MU BLUE -> 09:06 인버스 가상진입. 창 08:40/08:45/08:50 × MU 유지필터(재전환 없음 ∧ 08:59 BLUE 모멘텀 확대).
MU 유지필터 = production mu_assist 정의: 08:59 hist<0(재전환 없음) ∧ 부호화 hist 마지막 > 직전(EXPANDING). 09:00 이후 MU 는 보지 않음(겨울 공백 가정)."""
import sys, io
from datetime import datetime
from pathlib import Path
import pandas as pd
class _N(io.StringIO):
    def reconfigure(self, **k): pass
_o = sys.stdout; sys.stdout = _N()
sys.path.insert(0, "."); import nine_fb as nf
sys.stdout = _o
from app.trading.macd2 import mu_assist
rows = []
for day in nf.C_DAYS:
    d0 = datetime.strptime(day, "%Y%m%d")
    mu = mu_assist.load_mu_1m(d0.replace(hour=9), str(nf.RT.get(day, nf.S46 / "mu_sp/rt")))
    if mu is None: continue
    m = mu[mu.index + pd.Timedelta(minutes=1) <= d0.replace(hour=9)]
    if len(m) < 30: continue
    h = mu_assist._macd_hist(m)
    flips = [h.index[k] for k in range(1, len(h)) if h.iloc[k-1] >= 0 > h.iloc[k] and h.index[k].strftime("%H:%M") >= "08:40"]
    if not flips: continue
    s = -h
    hold = bool(h.iloc[-1] < 0) and bool(s.iloc[-1] > s.iloc[-2])
    t = datetime.strptime(day + "0906", "%Y%m%d%H%M").replace(tzinfo=nf.KST)
    e = nf.bars(day, "inverse"); seg = e[e["datetime"] >= t].reset_index(drop=True)
    ent = float(seg["open"].iloc[0]) + 5; q = int(10_000_000 // ent)
    r15, r30, first = nf.outcome(day, "BLUE", t)
    px = ent * 1.015 if first == "+1.5" else ent * 0.985 if first == "-1.5" else float(seg["close"].iloc[-1])
    rows.append(dict(day=day, flips=[f.strftime("%H:%M") for f in flips], last_blue=flips[-1].strftime("%H:%M"),
                     hold=hold, h0859=round(float(h.iloc[-1]), 4), first=first, r15=r15, r30=r30,
                     pnl=round(q * (px - ent) - 0.000036 * q * (px + ent))))
for r in rows: print(" ", r)
print()
for w in ("08:40", "08:45", "08:50"):
    for flt in (False, True):
        rs = [r for r in rows if any(f >= w for f in r["flips"]) and (r["hold"] or not flt)]
        n = len(rs)
        if not n: print(f"[{w}~09:00 {'MU유지필터' if flt else '무조건':<7}] 0건"); continue
        print(f"[{w}~09:00 {'MU유지필터' if flt else '무조건':<7}] {n}건 선터치 +1.5:{sum(r['first']=='+1.5' for r in rs)} / -1.5:{sum(r['first']=='-1.5' for r in rs)} "
              f"평균15 {sum(r['r15'] for r in rs)/n:+.2f}% 평균30 {sum(r['r30'] for r in rs)/n:+.2f}% 가상손익 {sum(r['pnl'] for r in rs):+,} "
              f"날짜 {[r['day'][4:] for r in rs]}")
