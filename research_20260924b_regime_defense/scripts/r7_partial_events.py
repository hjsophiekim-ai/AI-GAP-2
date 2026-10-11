"""R7 - Q1 부분익절 이벤트 전량 + 집중도 확인."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 300)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "r3.pkl", "rb"))
D = Z["dates"]


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    return d


B = prep(Z["runs"]["P0_BASE"])
key = ["date", "entry_time", "direction"]
for tag in ("Q1_part10", "Q2_part12"):
    d = prep(Z["runs"][tag])
    f = d[d.rx_partial_at.notna()]
    print("=" * 110)
    print("[%s] 부분익절 발동 %d건 / regime ON %d건 / 전체 %d건"
          % (tag, len(f), int(d.rx_on.sum()), len(d)))
    j = f.merge(B[key + ["net_pct", "exit_reason", "peak_net_pct"]], on=key,
                how="left", suffixes=("", "_b"))
    j["uplift"] = j.net_pct - j.net_pct_b
    print(j[["date", "direction", "entry_time", "rx_partial_at", "rx_partial_px",
             "exit_reason", "net_pct", "net_pct_b", "uplift", "peak_net_pct"]]
          .to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    u = j.uplift.dropna()
    print("   uplift 합 %+.3f / 양 %d / 음 %d / 최대기여 %+.3f (전체합의 %.0f%%)"
          % (u.sum(), int((u > 0).sum()), int((u < 0).sum()),
             u.max() if len(u) else 0,
             100 * u.max() / u.sum() if len(u) and u.sum() else 0))
    # 최대기여 거래를 빼면?
    if len(u):
        drop_i = j.uplift.idxmax()
        k = j.loc[drop_i, key].tolist()
        m = ~((d.date == k[0]) & (d.entry_time == k[1]) & (d.direction == k[2]))
        mb = ~((B.date == k[0]) & (B.entry_time == k[1]) & (B.direction == k[2]))
        cv = float(((1 + d[m].sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)
        bv = float(((1 + B[mb].sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)
        print("   최대기여 거래 제외 시 전체 Δ = %+.2f (원래 %+.2f)"
              % (cv - bv, float(((1 + d.sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)
                 - float(((1 + B.sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)))
