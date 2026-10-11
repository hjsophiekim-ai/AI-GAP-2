"""C11 - CHOP 도입 효과를 (a) CHOP 거래 자체 (b) 기존 N1 거래집합 변화 로 분해."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 300)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "c8.pkl", "rb"))
D = Z["dates"]; SEP = [d for d in D if d >= "20260901"]


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    if "cx_on" not in d:
        d["cx_on"] = False
    d["cx_on"] = d.cx_on.fillna(False).astype(bool)
    return d


B = prep(Z["runs"]["A_BASE"])
key = ["date", "entry_time", "direction"]
bk = set(map(tuple, B[key].astype(str).values))
print("=" * 130)
print("CHOP 도입 효과 분해 — (a) CHOP 자체 손익 (b) 기존 N1 거래집합 변화(부수효과)")
print("=" * 130)
rows = []
for t in Z["bat"]:
    if t == "A_BASE":
        continue
    d = prep(Z["runs"][t])
    cxp = d[d.cx_on]
    n1 = d[~d.cx_on]
    n1k = set(map(tuple, n1[key].astype(str).values))
    same = n1k & bk
    added = n1k - bk          # CHOP 때문에 새로 생긴 N1 거래
    lost = bk - n1k           # CHOP 때문에 사라진 N1 거래
    # 같은 키인데 결과가 달라진 거래
    m = n1.copy(); m["k"] = list(map(tuple, m[key].astype(str).values))
    bb = B.copy(); bb["k"] = list(map(tuple, bb[key].astype(str).values))
    j = m.merge(bb[["k", "net_pct", "w1a"]], on="k", how="inner", suffixes=("", "_b"))
    changed = j[(j.net_pct - j.net_pct_b).abs() > 1e-9]
    rows.append(dict(후보=t,
                     CHOP거래=len(cxp), CHOP합=round(cxp.pnl.sum(), 3),
                     N1동일=len(same), N1추가=len(added), N1소멸=len(lost),
                     N1결과변경=len(changed),
                     N1소멸합=round(bb[bb.k.isin(lost)].pnl.sum(), 3),
                     N1추가합=round(m[m.k.isin(added)].pnl.sum(), 3)))
print(pd.DataFrame(rows).to_string(index=False))
print("\n-> CHOP합 이 0 근처인데 전체 Δ 가 크면, 그 차이는 전략 edge 가 아니라")
print("   슬롯 점유로 N1 진입집합이 바뀐 **부수효과(재배치)** 다.")
