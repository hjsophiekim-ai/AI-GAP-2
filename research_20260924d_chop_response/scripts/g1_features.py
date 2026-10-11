"""G1 — CHOP ON 구간 BASE 진입 전량 feature dump (진입시점 정보만) + 사후 라벨.

엔진 재실행 없음: BASE 거래의 decision_idx 로 인과적 특징표를 조인하고,
사후 라벨(+6/9/15/30분 수익)은 ctx.quotes 에서 계산한다.
TEG 조건은 production teg_gate 를 진입 봉까지의 슬라이스로 재계산한다(인과적).
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 320); pd.set_option("display.max_columns", 80)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import hengine5 as H
H._CTX_CACHE = HERE / "_ctx_B.pkl"
H._MEMO_PATH = HERE / "_memo_B.pkl"
from app.trading.macd2 import config, teg_gate as TG
from app.trading.macd2.models import Direction

ctx = H.build_ctx(78)
bars = ctx.hynix_bars_3m.reset_index(drop=True)
dt = pd.to_datetime(bars["datetime"])
date = dt.dt.strftime("%Y%m%d").values
close = bars["close"].astype(float).values
high = bars["high"].astype(float).values
low = bars["low"].astype(float).values
vol = bars["volume"].astype(float).values

# ── 인과 특징 ─────────────────────────────────────────────────────────────
ef = bars["close"].ewm(span=12, adjust=False).mean()
es = bars["close"].ewm(span=26, adjust=False).mean()
macd = ef - es
sig = macd.ewm(span=9, adjust=False).mean()
gap = (macd - sig).values
e10 = bars["close"].ewm(span=10, adjust=False).mean().values
e20 = bars["close"].ewm(span=20, adjust=False).mean().values
e50 = bars["close"].ewm(span=50, adjust=False).mean().values

vwap = np.full(len(bars), np.nan)
day_open = np.full(len(bars), np.nan)
cur, pv, pvv, op = None, 0.0, 0.0, None
tp3 = (high + low + close) / 3.0
for i in range(len(bars)):
    if date[i] != cur:
        cur, pv, pvv, op = date[i], 0.0, 0.0, close[i]
    v = vol[i] if vol[i] > 0 else 1.0
    pv += tp3[i] * v
    pvv += v
    vwap[i] = pv / pvv
    day_open[i] = op

flags = ctx.flags_by_idx
fidx = sorted(flags)


def gap_slope(i, k):
    j = i - k
    return float(gap[i] - gap[j]) if j >= 0 and date[j] == date[i] else np.nan


def recent_flags(i, n_bars=20):
    lo = i - n_bars
    ks = [k for k in fidx if lo <= k <= i and date[k] == date[i]]
    return ks


Z = pickle.load(open(HERE / "c8.pkl", "rb"))
B = pd.DataFrame(Z["runs"]["A_BASE"]).sort_values("exit_time").reset_index(drop=True)
B["ext"] = pd.to_datetime(B.exit_time, utc=True)
B["ent"] = pd.to_datetime(B.entry_time, utc=True)
SHADOW_ON = np.load(HERE / "shadow_on.npy")

h50v = B.h50_held.astype(float).values
tp1v = B.tp1_hit.astype(float).values
extv = B.ext.values


def rates_before(ts, K=10):
    n = int(np.searchsorted(extv, np.datetime64(pd.Timestamp(ts).tz_convert("UTC")), side="left"))
    if n < K:
        return np.nan, np.nan
    return float(h50v[n - K:n].mean()), float(tp1v[n - K:n].mean())


def fwd_ret(sym, t0, px0, mins):
    q = ctx.quotes[sym]
    p = q.at(pd.Timestamp(t0) + pd.Timedelta(minutes=mins))
    if p is None or not px0:
        return np.nan
    s = 1.0
    return float((p - px0) / px0 * 100.0 * s)


rows = []
for _, r in B.iterrows():
    i = int(r.decision_idx)
    if i >= len(bars) or not SHADOW_ON[i]:
        continue
    d = Direction(r.direction)
    s = 1.0 if d == Direction.UP_RED else -1.0
    ks = recent_flags(i)
    # 플래그 봉은 decision_idx-1 이다. 간격은 '이번 플래그'와 '직전 플래그' 사이.
    fbar = i - 1
    prev_f = [k for k in fidx if k < fbar and date[k] == date[i]]
    interval = (fbar - prev_f[-1]) if prev_f else np.nan
    opp = sum(1 for k in ks if flags[k] != d)
    teg = TG.evaluate_teg(bars.iloc[: i + 1], d,
                          pd.Timestamp(bars["datetime"].iloc[i - 1]).to_pydatetime(),
                          pd.Timestamp(r.entry_time).to_pydatetime())
    cond = dict(getattr(teg, "conditions", None) or {})
    hr, qr = rates_before(r.ent)
    etf = fwd_ret(r.entry_symbol, pd.Timestamp(r.entry_time) - pd.Timedelta(minutes=6),
                  ctx.quotes[r.entry_symbol].at(pd.Timestamp(r.entry_time) - pd.Timedelta(minutes=6)), 6)
    t = pd.Timestamp(r.entry_time).tz_convert("Asia/Seoul")
    rows.append(dict(
        date=r.date, entry_time=r.entry_time, direction=r.direction, slot=r.slot_number,
        session=r.session, tmin=t.hour * 60 + t.minute, flag_ord=r.flag_ordinal,
        gap=gap[i], gap_s1=gap_slope(i, 1), gap_s2=gap_slope(i, 2), gap_s3=gap_slope(i, 3),
        gap_dir=s * gap[i], gap_s2_dir=s * gap_slope(i, 2),
        e10_20=(e10[i] - e20[i]) / close[i] * 100, e20_50=(e20[i] - e50[i]) / close[i] * 100,
        e20_50_dir=s * (e20[i] - e50[i]) / close[i] * 100,
        e50_slope=(e50[i] - e50[i - 1]) / close[i] * 100 if i > 0 else np.nan,
        e50_slope_dir=s * ((e50[i] - e50[i - 1]) / close[i] * 100) if i > 0 else np.nan,
        vwap_dist=(close[i] - vwap[i]) / close[i] * 100,
        vwap_dir=s * (close[i] - vwap[i]) / close[i] * 100,
        cum_move=s * (close[i] - day_open[i]) / day_open[i] * 100,
        etf_conf=s * (etf if etf == etf else np.nan),
        flag_interval=interval, opp_flag_cnt=opp, cross_cnt=len(ks),
        teg_ok=bool(getattr(teg, "approved", False)),
        teg_pass=int(sum(1 for c in TG.ALL_CONDITIONS if cond.get(c, False))),
        teg_stack=bool(cond.get(TG.COND_EMA_STACK, False)),
        teg_vwap=bool(cond.get(TG.COND_VWAP, False)),
        quality=r.tq if r.tq == r.tq else np.nan,
        is_ar1=bool(str(r.relax or "")) or ("AR1" in str(r.relax or "")),
        h50_rate=hr, tp1_rate=qr,
        # ── 사후 라벨
        net=r.net_pct, mfe=r.peak_net_pct, mae=r.mae_net_pct, w1a=r.w1a,
        pnl=r.net_pct * r.w1a, tp1_hit=bool(r.tp1_hit), hold=r.hold_minutes,
        exit_reason=r.exit_reason,
        r6=fwd_ret(r.entry_symbol, r.entry_time, r.entry_price, 6),
        r9=fwd_ret(r.entry_symbol, r.entry_time, r.entry_price, 9),
        r15=fwd_ret(r.entry_symbol, r.entry_time, r.entry_price, 15),
        r30=fwd_ret(r.entry_symbol, r.entry_time, r.entry_price, 30),
        run3=bool(r.peak_net_pct >= 3), run5=bool(r.peak_net_pct >= 5),
        run8=bool(r.peak_net_pct >= 8),
    ))

F = pd.DataFrame(rows)
F["loss"] = F.net <= 0
F["is_sep"] = F.date.astype(str) >= "20260901"
F.to_csv(HERE / "g1_chop_features.csv", index=False, encoding="utf-8-sig")
print("CHOP ON 구간 BASE 진입 %d건 (9월 %d / 비9월 %d)"
      % (len(F), int(F.is_sep.sum()), int((~F.is_sep).sum())))
print("손실 %d / 이익 %d · 평균net %.3f · 합 %.3f"
      % (int(F.loss.sum()), int((~F.loss).sum()), F.net.mean(), F.pnl.sum()))
print("\n라벨 요약")
print(F[["net", "mfe", "mae", "r6", "r9", "r15", "r30", "hold"]].describe().round(3).to_string())
print("\n청산사유")
print(F.exit_reason.value_counts().to_string())
print("\nrunner: MFE>=3 %d / >=5 %d / >=8 %d · TP1 %d"
      % (F.run3.sum(), F.run5.sum(), F.run8.sum(), F.tp1_hit.sum()))
print("\nsaved g1_chop_features.csv")
