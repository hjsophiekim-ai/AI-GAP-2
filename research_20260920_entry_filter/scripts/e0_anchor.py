"""E0 — 훅 미지정 = 완전한 no-op 임을 앵커로 확인한다."""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
A._Q3_PATH = A.HERE / "_q3base.pkl"
from common import summarize
from app.trading.macd2 import config
import hengine5 as H

c = A.ctx(78); D = c.dates
C1 = {"decide": 99.0, "strong": {}, "weak": {},
      "pp": {"arm": float(config.C1_ARM_MFE_PCT),
             "give": float(config.C1_GIVEBACK_PCT), "cond": "gap_neg"}}
H.D_VARIANTS["N1PROD"] = {}

_pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
try:
    for tag, kw in (("N1", dict(ax=None)), ("N1+C1", dict(ax=C1))):
        ts = H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD", **kw)
        m = summarize(ts, D)
        print(f"{tag:8s} n={m['trades']} 78d={m['compound_pct']:.4f}")
        assert not any(t.get("forced") for t in ts), "forced 태그가 새는지 확인"
    # H50 도 같이 (사용자 요청: H50/N1 모두 진입하지 않은 플래그)
    for tag, spec in (("H50", "H50"), ("X2lite", "X2lite_W1")):
        cfg, po, q3, h50 = A.SPEC[spec]
        ts = A.run(spec, c, D)
        m = summarize(ts, D)
        print(f"{tag:8s} n={m['trades']} 78d={m['compound_pct']:.4f}")
finally:
    H._MEMO["base"] = _pb
A.save()
