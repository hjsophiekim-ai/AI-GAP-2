"""E3 — 551개 플래그 전부에 대해 '그 플래그 하나만 진입했다면' 을 측정한다.

청산은 N1 래더 + C1(pp) 그대로. 하루에 그 플래그만 존재하므로 슬롯/보유
간섭이 없는 **독립 반사실**이다. 미래값을 쓰지 않는다 -- 엔진이 그 시점부터
정상 청산로직으로 전개할 뿐이다.
"""
import sys, pickle, time
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
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
FH = {(r["date"], r["flag_idx"]): r for r in E1["H50"]}

OUTP = A.HERE / "e3.pkl"
DONE = pickle.load(open(OUTP, "rb")) if OUTP.exists() else {}
rows = []
t0 = time.time()
_pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
try:
    for n, r in enumerate(FL, 1):
        k = (r["date"], int(r["flag_idx"]))
        if k in DONE:
            rows.append(DONE[k]); continue
        ts = H.run_chain(c, H.N1_PROD, dates=[r["date"]], h50=True,
                         d_variant="N1PROD", ax=C1, solo_idx=int(r["flag_idx"]))
        t = ts[0] if ts else None
        row = dict(r)
        row.pop("h50_handled", None); row.pop("regime_handled", None)
        row["approved_h50"] = bool(FH[k]["approved"]) if k in FH else None
        row["reason_h50"] = FH[k]["reason"] if k in FH else None
        row["solo_n"] = len(ts)
        row["solo_net"] = (float(t["net_pct"]) if t else None)
        row["solo_peak"] = (float(t["peak_net_pct"]) if t else None)
        row["solo_mae"] = (float(t["mae_net_pct"]) if t else None)
        row["solo_reason"] = (t["exit_reason"] if t else None)
        row["solo_hold_min"] = (float(t["hold_minutes"]) if t else None)
        row["solo_entry"] = (t["entry_time"] if t else None)
        row["solo_exit"] = (t["exit_time"] if t else None)
        row["solo_pp_fired"] = (bool(t["pp_fired"]) if t else None)
        row["solo_h50_held"] = (bool(t["h50_held"]) if t else None)
        DONE[k] = row
        rows.append(row)
        if n % 40 == 0:
            pickle.dump(DONE, open(OUTP, "wb"))
            print(f"  {n}/{len(FL)}  {time.time()-t0:5.0f}s", flush=True)
finally:
    H._MEMO["base"] = _pb
pickle.dump(DONE, open(OUTP, "wb"))
A.save()
S = pd.DataFrame(rows)
S.to_csv(A.HERE / "solo_flags.csv", index=False, encoding="utf-8-sig")
print(f"완료 {len(S)}개 / {time.time()-t0:.0f}s / 측정실패(진입 불가) {int((S['solo_n']==0).sum())}")
print("저장: e3.pkl / solo_flags.csv")
