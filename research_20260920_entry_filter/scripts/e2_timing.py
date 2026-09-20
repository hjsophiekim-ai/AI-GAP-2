"""E2 — solo 반사실 측정 타이밍 확인."""
import sys, pickle, time
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
A._Q3_PATH = A.HERE / "_q3base.pkl"
from app.trading.macd2 import config
import hengine5 as H

c = A.ctx(78)
C1 = {"decide": 99.0, "strong": {}, "weak": {},
      "pp": {"arm": float(config.C1_ARM_MFE_PCT),
             "give": float(config.C1_GIVEBACK_PCT), "cond": "gap_neg"}}
H.D_VARIANTS["N1PROD"] = {}
E1 = pickle.load(open(A.HERE / "e1.pkl", "rb"))
FL = E1["N1C1"]

_pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
try:
    for r in FL[:5]:
        t0 = time.time()
        ts = H.run_chain(c, H.N1_PROD, dates=[r["date"]], h50=True,
                         d_variant="N1PROD", ax=C1, solo_idx=int(r["flag_idx"]))
        dt = time.time() - t0
        got = ts[0] if ts else None
        print(f"{r['date']} flag_idx={r['flag_idx']} appr={r['approved']} "
              f"-> n={len(ts)} net={(got['net_pct'] if got else None)} "
              f"reason={(got['exit_reason'] if got else None)}  {dt:.2f}s")
finally:
    H._MEMO["base"] = _pb
A.save()
