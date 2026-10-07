"""C12 - CHOP 진입이 밀어낸 기존 N1 거래 전량 출력."""
import pickle, sys
from pathlib import Path
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 300); pd.set_option("display.max_rows", 200)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "c8.pkl", "rb"))


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    if "cx_on" not in d:
        d["cx_on"] = False
    d["cx_on"] = d.cx_on.fillna(False).astype(bool)
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    return d


B = prep(Z["runs"]["A_BASE"])
for t in ("C1a_tp08_sl08_m15", "C2c_m15", "C1a_tp08_sl08_m15__M1",
          "C1a_tp08_sl08_m15__HYST"):
    if t not in Z["runs"]:
        continue
    d = prep(Z["runs"][t])
    n1 = d[~d.cx_on]
    lost = B[~B.k.isin(set(n1.k))]
    cxp = d[d.cx_on]
    print("=" * 130)
    print("[%s] CHOP %d건 진입 -> BASE 의 N1 거래 %d건이 사라짐" % (t, len(cxp), len(lost)))
    print("  사라진 N1 거래 (BASE 기준 손익):")
    print(lost[["date", "session", "direction", "entry_time", "exit_time", "exit_reason",
                "net_pct", "w1a", "pnl"]].to_string(index=False,
                                                    float_format=lambda v: f"{v:,.4f}"))
    print("  사라진 N1 합 %+.3f (이익거래 %d / 손실거래 %d)"
          % (lost.pnl.sum(), int((lost.net_pct > 0).sum()), int((lost.net_pct <= 0).sum())))
    print("  CHOP 거래 손익 합 %+.3f" % cxp.pnl.sum())
