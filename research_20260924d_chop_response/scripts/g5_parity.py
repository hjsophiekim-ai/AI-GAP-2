"""G5 - OFF-parity 정밀검사. regime 이 장중 토글되므로 'ON 이 하나도 없는 날'로 가른다."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 320)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "g3.pkl", "rb"))
D = Z["dates"]
SH = np.load(HERE / "shadow_on.npy")
import hengine5 as H
H._CTX_CACHE = HERE / "_ctx_B.pkl"
ctx = H.build_ctx(78)
bd = pd.to_datetime(ctx.hynix_bars_3m["datetime"]).dt.strftime("%Y%m%d").values
on_days = sorted({d for d, o in zip(bd, SH) if o})
pure_off = [d for d in D if d not in set(on_days)]
print("ON 이 한 번이라도 있는 날 %d개: %s" % (len(on_days), on_days))
print("완전 OFF 인 날 %d개\n" % len(pure_off))


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    return d


DF = {t: prep(Z["runs"][t]) for t in Z["runs"]}
B = DF["G0_BASE"]
po = set(pure_off)
bo = B[B.date.astype(str).isin(po)]
rows = []
for t in Z["bat"]:
    if t == "G0_BASE":
        continue
    o = DF[t][DF[t].date.astype(str).isin(po)]
    same = set(o.k) == set(bo.k)
    j = o.merge(bo[["k", "net_pct", "w1a", "exit_time", "exit_reason"]], on="k", how="inner",
                suffixes=("", "_b"))
    dif = int(((j.net_pct - j.net_pct_b).abs() > 1e-9).sum()
              + (j.w1a - j.w1a_b).abs().gt(1e-9).sum()
              + (j.exit_time != j.exit_time_b).sum())
    rows.append(dict(후보=t, 완전OFF일_거래=len(o), BASE=len(bo), 키일치=same, 차이=dif,
                     PARITY="OK" if (same and dif == 0) else "위반"))
print(pd.DataFrame(rows).to_string(index=False))

print("\n[혼합일(ON+OFF 공존) 에서의 파급 — 설계상 불가피, 규모만 기록]")
mixed = [d for d in D if d in set(on_days)]
rows = []
for t in Z["bat"]:
    if t == "G0_BASE":
        continue
    o = DF[t][DF[t].date.astype(str).isin(set(mixed))]
    b2 = B[B.date.astype(str).isin(set(mixed))]
    rows.append(dict(후보=t, 혼합일_후보거래=len(o), 혼합일_BASE거래=len(b2),
                     추가=len(set(o.k) - set(b2.k)), 소멸=len(set(b2.k) - set(o.k))))
print(pd.DataFrame(rows).to_string(index=False))
