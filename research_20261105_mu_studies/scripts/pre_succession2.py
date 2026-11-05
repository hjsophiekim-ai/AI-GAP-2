"""08:40~09:00 MU BLUE -> 09:06 승계 v2 (재생 없음, 요약만).

MU BLUE 발생 : 08:40~08:59 1분 MU hist(+→−) 전환이 한 번이라도 있음 (그 뒤 재전환 여부는 기록만).
09:00 봉     : 방향 판정 제외. 판단은 09:03 봉(09:06 완성)에서.
후보(하나라도): (a) HY 3분 diff(09:03봉) < 0  (b) diff(09:03봉) < diff(09:00봉) = RED gap 축소/BLUE 전환
               (c) 체결강도 09:05~09:06 평균 < 100 (production MATCH 기준)
폐기(우선)   : diff(09:03봉) > 0 ∧ diff(09:03봉) > diff(09:00봉) = HY 상승 유지 ∧ RED gap 확대
진입         : 09:06 인버스 시가+1틱, ±1.5% 선터치, 15/30분.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import io
_o = sys.stdout


class _N(io.StringIO):
    def reconfigure(self, **k):
        pass


sys.stdout = _N()
import nine_fb as nf  # noqa: E402
sys.stdout = _o
from app.trading.macd2 import mu_assist, signal_engine as se  # noqa: E402

KST = nf.KST
res = []
for day in nf.C_DAYS:
    i = nf.ALL.index(day)
    d0 = datetime.strptime(day, "%Y%m%d")
    mu = mu_assist.load_mu_1m(d0.replace(hour=9), str(nf.RT.get(day, nf.S46 / "mu_sp/rt")))
    if mu is None:
        continue
    m = mu[mu.index + pd.Timedelta(minutes=1) <= d0.replace(hour=9)]
    if len(m) < 30:
        continue
    h = mu_assist._macd_hist(m)
    blue = [h.index[k] for k in range(1, len(h)) if h.iloc[k - 1] >= 0 > h.iloc[k] and h.index[k].strftime("%H:%M") >= "08:40"]
    if not blue:
        continue
    reflip = bool(h.iloc[-1] > 0)
    hy = pd.concat([nf.bars(d, "hynix", ("datetime", "open", "high", "low", "close")) for d in (nf.ALL[i - 2], nf.ALL[i - 1], day)]).reset_index(drop=True)
    hy, _ = se.exclude_preopen_padding_1m(hy)
    k0 = d0.replace(tzinfo=KST)
    ms = se.calculate_macd_series(se.resample_completed_3m(hy, k0.replace(hour=9, minute=6)))
    ms["d"] = ms["macd"] - ms["signal"]
    d903 = float(ms.loc[ms["datetime"] == k0.replace(hour=9, minute=3), "d"].iloc[0])
    d900 = float(ms.loc[ms["datetime"] == k0.replace(hour=9, minute=0), "d"].iloc[0])
    cm = nf.cttr_match(day, d0.replace(hour=9, minute=6), "BLUE")
    a, b, c = d903 < 0, d903 < d900, bool(cm)
    discard = d903 > 0 and d903 > d900
    ok = (a or b or c) and not discard
    r15, r30, first = nf.outcome(day, "BLUE", k0.replace(hour=9, minute=6))
    res.append(dict(day=day, mu_blue=blue[-1].strftime("%H:%M"), mu_reflip_red_by_0900=reflip, a_hy_blue=a,
                    b_gap_shrink=b, c_cttr_blue=c, discard=discard, ok=ok, d900=round(d900), d903=round(d903),
                    ret15=r15, ret30=r30, first=first))


def s(rs):
    n = len(rs)
    return (f"{n}건 선터치 +1.5:{sum(x['first'] == '+1.5' for x in rs)} / -1.5:{sum(x['first'] == '-1.5' for x in rs)} "
            f"평균15 {sum(x['ret15'] for x in rs)/n:+.2f}% 평균30 {sum(x['ret30'] for x in rs)/n:+.2f}%") if n else "0건"


print("[승계 진입 후보]", s([x for x in res if x["ok"]]))
print("[폐기/불충족]  ", s([x for x in res if not x["ok"]]))
for x in res:
    print("  ", x)
