"""프리마켓 HY BLUE 플래그(장전이라 미진입) + MU BLUE (같은 때 또는 그 이전) -> 09:06 인버스 승계. 재생 없음."""
import sys, io
from datetime import datetime, timedelta
import pandas as pd
class _N(io.StringIO):
    def reconfigure(self, **k): pass
_o = sys.stdout; sys.stdout = _N()
sys.path.insert(0, "."); import nine_fb as nf
sys.stdout = _o
from app.trading.macd2 import mu_assist, signal_engine as se
out = []
for day in nf.C_DAYS:
    i = nf.ALL.index(day); d0 = datetime.strptime(day, "%Y%m%d"); k0 = d0.replace(tzinfo=nf.KST)
    hy = pd.concat([nf.bars(d, "hynix", ("datetime", "open", "high", "low", "close")) for d in (nf.ALL[i-2], nf.ALL[i-1], day)]).reset_index(drop=True)
    hy, _ = se.exclude_preopen_padding_1m(hy)
    ms = se.calculate_macd_series(se.resample_completed_3m(hy, k0.replace(hour=9))); g = (ms["macd"] - ms["signal"]).values
    hb = [ms["datetime"].iloc[k] for k in range(1, len(g)) if g[k-1] >= 0 > g[k] and k0.replace(hour=8, minute=30) <= ms["datetime"].iloc[k] <= k0.replace(hour=8, minute=48)]
    if not hb: continue
    hflag = hb[-1]; hconf = hflag + timedelta(minutes=3)
    hy_red_after = any(g[k-1] <= 0 < g[k] and ms["datetime"].iloc[k] > hflag for k in range(1, len(g)))
    mu = mu_assist.load_mu_1m(d0.replace(hour=9), str(nf.RT.get(day, nf.S46 / "mu_sp/rt")))
    m = None if mu is None else mu[mu.index + pd.Timedelta(minutes=1) <= d0.replace(hour=9)]
    if m is None or len(m) < 30:
        mu_info, mu_blue_at_hy = "MU 없음", None
    else:
        h = mu_assist._macd_hist(m)
        flips = [(("BLUE" if h.iloc[k] < 0 else "RED"), h.index[k].strftime("%H:%M")) for k in range(1, len(h))
                 if (h.iloc[k-1] >= 0 > h.iloc[k] or h.iloc[k-1] <= 0 < h.iloc[k]) and h.index[k].strftime("%H:%M") >= "08:20"]
        at = h[h.index + pd.Timedelta(minutes=1) <= hconf.replace(tzinfo=None)]
        mu_blue_at_hy = bool(at.iloc[-1] < 0)
        mu_info = f"HY확정({hconf:%H:%M}) 시점 MU {'BLUE' if mu_blue_at_hy else 'RED'}, 08:59 MU {'BLUE' if h.iloc[-1] < 0 else 'RED'}, 전환 {flips}"
    t = k0.replace(hour=9, minute=6)
    e = nf.bars(day, "inverse"); seg = e[e["datetime"] >= t].reset_index(drop=True)
    ent = float(seg["open"].iloc[0]) + 5; q = int(10_000_000 // ent)
    r15, r30, first = nf.outcome(day, "BLUE", t)
    px = ent * 1.015 if first == "+1.5" else ent * 0.985 if first == "-1.5" else float(seg["close"].iloc[-1])
    close_px = float(seg[seg["datetime"] <= k0.replace(hour=15, minute=19)]["close"].iloc[-1])
    out.append((day, hflag.strftime("%H:%M"), hy_red_after, mu_blue_at_hy, mu_info, first, r15, r30,
                round(q * (px - ent) - 0.000036 * q * (px + ent)), round((close_px / ent - 1) * 100, 2)))
for x in out:
    print(f"{x[0][4:]} HY BLUE {x[1]}봉 | 이후 HY RED 재전환(~09:00) {x[2]} | {x[4]}\n      -> 09:06 인버스: {x[5]} 선터치, 15분 {x[6]:+.2f}% 30분 {x[7]:+.2f}% ±1.5손익 {x[8]:+,} | 장마감 보유 {x[9]:+.2f}%")
sel = [x for x in out if x[3] is True]
n = len(sel)
print(f"\n[승계 대상: HY BLUE ∧ HY 확정 시점 MU BLUE] {n}건 선터치 +1.5:{sum(x[5]=='+1.5' for x in sel)} / -1.5:{sum(x[5]=='-1.5' for x in sel)} "
      f"30분 평균 {sum(x[7] for x in sel)/max(n,1):+.2f}% ±1.5손익 합 {sum(x[8] for x in sel):+,}")
