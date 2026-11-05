"""08:40~09:00 MU 플래그 -> 09:06 승계 진입 (재생 없음, 날짜별 순차, 요약만).

MU 플래그 : 08:40~08:59 완성 1분봉에서 production mu_assist._macd_hist 부호 전환(BLUE: +→−, RED: −→+). 마지막 것 사용.
HY 조건1  : MU 플래그 시점 HY가 '강하게 반대' 아님 = NOT(HY 3분 diff 가 반대방향 ∧ 반대방향 hy_gap_state == EXPAND)
HY 조건2  : 09:06 재확인 — HY 3분 diff 가 진입 방향 유지(BLUE: diff<0)
진입      : 09:06 ETF 시가+1틱 (BLUE 인버스 / RED 레버리지 참고). ±1.5% 선터치, 15/30분.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import nine_fb as nf  # noqa: E402  (bars/outcome/RT/ALL 재사용 — nine_fb 본문도 실행됨: 짧음)
from app.trading.macd2 import mu_assist, signal_engine as se  # noqa: E402

KST = nf.KST
DAYS = nf.C_DAYS
res = []
for day in DAYS:
    i = nf.ALL.index(day)
    d0 = datetime.strptime(day, "%Y%m%d")
    mu = mu_assist.load_mu_1m(d0.replace(hour=9, minute=0), str(nf.RT.get(day, nf.S46 / "mu_sp/rt")))
    if mu is None:
        print(" ", day, "MU 없음(월요일 등)"); continue
    m = mu[mu.index + pd.Timedelta(minutes=1) <= d0.replace(hour=9, minute=0)]
    if len(m) < 30:
        print(" ", day, "MU 1분봉 부족"); continue
    h = mu_assist._macd_hist(m)
    flags = [("BLUE" if h.iloc[k] < 0 else "RED", h.index[k]) for k in range(1, len(h))
             if (h.iloc[k - 1] >= 0 > h.iloc[k] or h.iloc[k - 1] <= 0 < h.iloc[k]) and h.index[k].strftime("%H:%M") >= "08:40"]
    if not flags:
        continue
    dr, ft = flags[-1]
    sgn = -1 if dr == "BLUE" else 1
    hy = pd.concat([nf.bars(d, "hynix", ("datetime", "open", "high", "low", "close")) for d in (nf.ALL[i - 2], nf.ALL[i - 1], day)]).reset_index(drop=True)
    hy, _ = se.exclude_preopen_padding_1m(hy)
    k0 = d0.replace(tzinfo=KST)

    def hy_state(at):
        hb = se.resample_completed_3m(hy, at)
        ms = se.calculate_macd_series(hb)
        g = (ms["macd"] - ms["signal"]).values
        return g[-3:]
    g = hy_state(k0.replace(hour=ft.hour, minute=ft.minute) + timedelta(minutes=1))
    opp = [-sgn * x for x in g]                                   # 반대방향 부호
    strong_opp = opp[-1] > 0 and mu_assist.hy_gap_state(*opp) == "EXPAND"
    g906 = hy_state(k0.replace(hour=9, minute=6))
    keep = sgn * g906[-1] > 0
    ok = (not strong_opp) and keep
    r15, r30, first = nf.outcome(day, dr, k0.replace(hour=9, minute=6))
    res.append((day, ft.strftime("%H:%M"), dr, strong_opp, keep, ok, r15, r30, first))

print()
for dr in ("BLUE", "RED"):
    for lab, f in (("승계 조건 충족", lambda x: x[5]), ("조건 불충족(참고)", lambda x: not x[5])):
        rs = [x for x in res if x[2] == dr and f(x)]
        n = len(rs)
        s = (f"{n}건 선터치 +1.5:{sum(x[8] == '+1.5' for x in rs)} / -1.5:{sum(x[8] == '-1.5' for x in rs)} "
             f"평균15 {sum(x[6] for x in rs)/n:+.2f}% 평균30 {sum(x[7] for x in rs)/n:+.2f}%") if n else "0건"
        print(f"[{dr}] {lab}: {s}")
print("\n전수 (날짜, MU플래그, 방향, HY강반대, 09:06유지, 충족, 15분, 30분, 선터치)")
for x in res:
    print("  ", x)
