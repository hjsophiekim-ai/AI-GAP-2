"""주간연구 2026-10-01: 역추세 플래그 거르기 게이트 (R0=H30 위에 진입 게이트만 추가). READ-ONLY.
python tfgate.py <OPEN|VWAP> <AM|ALL>
  OPEN: 판정시점 하이닉스 종가 vs 당일 09:00 시가 / VWAP: vs 09:00 이후 3분봉 VWAP
  AM: 12:00 이전 판정만 적용 / ALL: 종일
"""
import os, sys, pickle, dataclasses, time
os.environ["B3_NDAYS"] = "83"
REF, WIN = sys.argv[1], sys.argv[2]; sys.argv = [sys.argv[0]]
import pandas as pd
import b3lib as L, b3run
from common import summarize
from app.trading.macd2 import time_window_filter as twf, config
from app.trading.macd2.models import Direction
_o = twf.evaluate_time_window_entry
CNT = {"rej": 0, "seen": 0}
def _gate(bars_3m, flag_direction, flag_bar_dt, decision_at, **k):
    d = _o(bars_3m, flag_direction, flag_bar_dt, decision_at, **k)
    if not d.approved or bars_3m is None or len(bars_3m) == 0:
        return d
    kst = pd.Timestamp(decision_at).tz_convert(config.KST)
    if WIN == "AM" and kst.hour >= 12:
        return d
    b = bars_3m.copy()
    dt = pd.to_datetime(b["datetime"])
    dt = dt.dt.tz_convert(config.KST) if dt.dt.tz is not None else dt.dt.tz_localize(config.KST)
    day = b[(dt.dt.date == kst.date()) & (dt.dt.hour >= 9) & (dt <= kst)]
    if len(day) == 0:
        return d
    last = float(day["close"].iloc[-1])
    if REF == "OPEN":
        ref = float(day["open"].iloc[0])
    else:
        tp = (day["high"] + day["low"] + day["close"]) / 3
        v = day["volume"].astype(float)
        ref = float((tp * v).sum() / v.sum()) if v.sum() > 0 else last
    dirv = flag_direction.value if isinstance(flag_direction, Direction) else str(flag_direction)
    s = 1 if dirv == "UP_RED" else -1
    CNT["seen"] += 1
    if (last - ref) * s < 0:
        CNT["rej"] += 1
        return dataclasses.replace(d, approved=False, block_reason="WK_COUNTER_TREND")
    return d
twf.evaluate_time_window_entry = _gate
t0 = time.time()
base = pickle.load(open(L.HERE / "out_BASE_d83.pkl", "rb"))["trades"]
cfg = dict(sl=1.0, hold=20.0, p3_min=6.0, p3_trig=1.0); cfg.update(b3run.VAR["H30"]); cfg["regime"] = L.make_regime(base, strict=True)
ts = L.run(cfg)
m = summarize(ts, L.DATES)
tag = f"TF_{REF}_{WIN}"
print("%-14s 거래 %3d  복리 %9.4f  PF %.4f  MDD %7.3f  승률 %.2f%%  rej %d/%d (%.0fs)" % (tag, m["trades"], m["compound_pct"], m["pf"], m["mdd_pct"], m["win_rate_pct"], CNT["rej"], CNT["seen"], time.time() - t0), flush=True)
pickle.dump({"trades": ts, "m": m}, open(L.HERE / f"out_{tag}_d83.pkl", "wb"))
