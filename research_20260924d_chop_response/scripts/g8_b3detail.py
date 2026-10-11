"""G8 - B3 정밀: CHOP 거래 전량 + 제거/대체/밀림 + runner 포기 내역."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 320); pd.set_option("display.max_rows", 200)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "g3.pkl", "rb"))
SH = np.load(HERE / "shadow_on.npy")


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    return d


B = prep(Z["runs"]["G0_BASE"])
T = prep(Z["runs"]["B3_tp10_sl10_m20"])
print("=" * 140); print("B3 CHOP 거래 전량 (BASE 대비)"); print("=" * 140)
c = T[T.chop].merge(B[["k", "net_pct", "exit_time", "exit_reason", "peak_net_pct"]],
                    on="k", how="left", suffixes=("", "_b"))
c["uplift"] = c.net_pct - c.net_pct_b
cols = ["date", "direction", "entry_time", "exit_time", "exit_reason", "net_pct",
        "exit_time_b", "exit_reason_b", "net_pct_b", "peak_net_pct_b", "uplift"]
print(c[cols].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
u = c.uplift.dropna()
print("\n매칭된 %d건 uplift 합 %+.3f (양 %d / 음 %d) · BASE 에 없던 신규 %d건 합 %+.3f"
      % (len(u), u.sum(), int((u > 0).sum()), int((u < 0).sum()),
         int(c.net_pct_b.isna().sum()), c[c.net_pct_b.isna()].pnl.sum()))
print("\n[포기한 러너] BASE CHOP 거래 중 MFE>=3%:")
r = B[B.chop & (B.peak_net_pct >= 3)]
rr = r.merge(T[["k", "net_pct", "exit_reason"]], on="k", how="left", suffixes=("_b", ""))
print(rr[["date", "direction", "entry_time", "peak_net_pct", "net_pct_b", "exit_reason_b",
          "net_pct", "exit_reason"]].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
print("\n[제거/대체]")
lost = B[~B.k.isin(set(T.k))]
add = T[~T.k.isin(set(B.k))]
print("제거 %d건 합 %+.3f" % (len(lost), lost.pnl.sum()))
print(lost[["date", "direction", "entry_time", "exit_reason", "net_pct", "pnl"]].to_string(index=False))
print("\n대체진입 %d건 합 %+.3f" % (len(add), add.pnl.sum()))
print(add[["date", "direction", "entry_time", "exit_reason", "net_pct", "pnl", "chop"]].to_string(index=False))
