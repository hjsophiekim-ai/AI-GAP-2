"""E1 — 78일 전체 플래그 원장. N1(+C1) 과 H50 각각의 판정을 전부 기록한다.
미래값 없음: 특징은 판정시점까지의 완성봉으로만 계산된다.
"""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
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

LOG = {}
_pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
try:
    fl = []
    TS = H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD",
                     ax=C1, flag_log=fl)
    LOG["N1C1"] = fl
finally:
    H._MEMO["base"] = _pb
fl2 = []
TH = A.run("H50", c, D, flag_log=fl2)
LOG["H50"] = fl2
A.save()

print(f"N1+C1 진입 {len(TS)}건 / 플래그 {len(fl)}개")
print(f"H50   진입 {len(TH)}건 / 플래그 {len(fl2)}개")

F = pd.DataFrame(fl)
F["approved_h50"] = pd.Series([r["approved"] for r in fl2]).values if len(fl2) == len(fl) else None
print("\n== 플래그 원장 요약 (N1 기준) ==")
print(f"  총 플래그            {len(F)}")
print(f"  N1 승인              {int(F['approved'].sum())}")
print(f"  H50 승인             {sum(1 for r in fl2 if r['approved'])}")
print(f"  둘 다 미진입          {int((~F['approved'] & ~F['approved_h50']).sum())}")
print(f"  flat 상태 플래그       {int(F['flat'].sum())}")
print(f"  보유중 플래그         {int((~F['flat']).sum())}")

print("\n== 거절사유 분포 (N1) ==")
r = F[~F["approved"]].groupby(["flat", "reason"]).size().sort_values(ascending=False)
for (flat, reason), n in r.items():
    print(f"  {'flat ' if flat else '보유중'} {reason:44s} {n:4d}")

print("\n== 거절사유 분포 (H50) ==")
H50 = pd.DataFrame(fl2)
r2 = H50[~H50["approved"]].groupby(["flat", "reason"]).size().sort_values(ascending=False)
for (flat, reason), n in r2.items():
    print(f"  {'flat ' if flat else '보유중'} {reason:44s} {n:4d}")

pickle.dump({"N1C1": fl, "H50": fl2, "trades_n1c1": TS, "trades_h50": TH, "dates": D},
            open(A.HERE / "e1.pkl", "wb"))
F.to_csv(A.HERE / "flag_ledger_n1.csv", index=False, encoding="utf-8-sig")
print("\n저장: e1.pkl / flag_ledger_n1.csv")
