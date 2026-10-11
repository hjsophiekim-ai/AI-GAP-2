"""진단용: 진입 게이트 전부 해제(확정 플래그 전부 승인, 하루 3회 한도·청산 규칙 유지) 풀. READ-ONLY."""
import os, sys, pickle, dataclasses
os.environ["B3_NDAYS"] = "83"; sys.argv = [sys.argv[0]]
import b3lib as L, b3run
from app.trading.macd2 import config as _C
_C.TW2_3SLOT_DAILY_CAP = 99  # 진단: 한도 해제
from app.trading.macd2 import time_window_filter as twf, time_window_3slot as tw3, teg_gate as TG, config
_o = twf.evaluate_time_window_entry
def _all(*a, **k):
    d = _o(*a, **k)
    return d if d.approved else dataclasses.replace(d, approved=True, block_reason=config.TW_APPROVED)
twf.evaluate_time_window_entry = _all
twf.evaluate_tw2_extra_vetoes = lambda *a, **k: (False, None)
_q = tw3.evaluate_trend_quality
tw3.evaluate_trend_quality = lambda *a, **k: dataclasses.replace(_q(*a, **k), approved=True)
_t = TG.evaluate_teg
TG.evaluate_teg = L.H.teg_gate.evaluate_teg = lambda *a, **k: dataclasses.replace(_t(*a, **k), approved=True)
_rs = tw3.resolve_slot
def _slot(**kw):
    s = _rs(**kw)
    if kw["slots_used_today"] < int(config.TW2_3SLOT_DAILY_CAP) and not s.slot_allowed:
        s = dataclasses.replace(s, slot_allowed=True, reject_reason=None)
    return dataclasses.replace(s, requires_quality_gate=False, requires_teg_gate=False)
tw3.resolve_slot = _slot
base = pickle.load(open(L.HERE / "out_BASE_d83.pkl", "rb"))["trades"]
cfg = dict(sl=1.0, hold=20.0, p3_min=6.0, p3_trig=1.0); cfg.update(b3run.VAR["H30"]); cfg["regime"] = L.make_regime(base, strict=True)
ts = L.run(cfg)
pickle.dump({"trades": ts}, open(L.HERE / "out_ALLFLAGS_NOCAP_d83.pkl", "wb")); print("ALLFLAGS_NOCAP", len(ts))
