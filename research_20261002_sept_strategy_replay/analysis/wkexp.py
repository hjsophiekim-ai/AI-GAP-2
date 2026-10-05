"""주간연구 2026-10-01 추가 변형 (R0=H30 위). READ-ONLY.  python wkexp.py <NAME>
  QW15  : P3 rescue(러너 승격) 창 6분 -> 15분 (20% 익절 후 러너)
  QW20  : 창 20분 (= B3 max-hold 전 +1% 도달이면 언제든 러너)
  QW20H : 창 20분, 익절 비율 50%
  NO930 : 09:30 이전 판정 진입 금지
  NO930_QW20 : NO930 + QW20
"""
import os, sys, pickle, dataclasses, time
os.environ["B3_NDAYS"] = "83"
NAME = sys.argv[1]; sys.argv = [sys.argv[0]]
import pandas as pd
import b3lib as L, b3run
from common import summarize
from app.trading.macd2 import time_window_filter as twf, config

if NAME.startswith("NO930"):
    _o = twf.evaluate_time_window_entry
    def _gate(bars_3m, flag_direction, flag_bar_dt, decision_at, **k):
        d = _o(bars_3m, flag_direction, flag_bar_dt, decision_at, **k)
        kst = pd.Timestamp(decision_at).tz_convert(config.KST)
        if d.approved and kst.hour * 60 + kst.minute < 9 * 60 + 30:
            return dataclasses.replace(d, approved=False, block_reason="WK_NO_ENTRY_BEFORE_0930")
        return d
    twf.evaluate_time_window_entry = _gate

t0 = time.time()
base = pickle.load(open(L.HERE / "out_BASE_d83.pkl", "rb"))["trades"]
cfg = dict(sl=1.0, hold=20.0, p3_min=6.0, p3_trig=1.0); cfg.update(b3run.VAR["H30"])
if "QW15" in NAME: cfg["p3_min"] = 15.0
if "QW20" in NAME: cfg["p3_min"] = 20.0
if NAME.endswith("QW20H"): cfg["resc_frac"] = 0.5
cfg["regime"] = L.make_regime(base, strict=True)
ts = L.run(cfg)
m = summarize(ts, L.DATES)
print("%-14s 거래 %3d  복리 %9.4f  PF %.4f  MDD %7.3f  승률 %.2f%%  (%.0fs)" % (NAME, m["trades"], m["compound_pct"], m["pf"], m["mdd_pct"], m["win_rate_pct"], time.time() - t0), flush=True)
pickle.dump({"trades": ts, "m": m}, open(L.HERE / f"out_WK_{NAME}_d83.pkl", "wb"))
