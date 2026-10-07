"""G6 - (a) BASE AR1 마킹런(규칙 없음=무해) (b) 델타 기준 단일거래 기여율 정정."""
import pickle, sys, time
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 320)
import rlib as R, axlib as A, hengine5 as H
from common import summarize
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "g3.pkl", "rb"))
D = Z["dates"]; SH = np.load(HERE / "shadow_on.npy")

if "G0_MARK" not in Z["runs"]:
    ctx = R.get_ctx()
    def is_ar1(day, rec_at):
        k = rec_at.astimezone(H.KST)
        return (k.strftime("%Y%m%d"), k.strftime("%H:%M")) in R.Z.RELAXED_KEYS
    R.Z.set_gate(lambda f: True); R.Z.RELAXED_KEYS.clear(); R.MODE["on"] = True
    t0 = time.time()
    ts = A.run("N1", ctx, D, ax=R.AX, gx={"on": SH, "is_ar1": is_ar1, "log": []})
    m = summarize(ts, D)
    print("G0_MARK 거래 %d 복리 %.4f (BASE 453.6036 와 같아야 함) %.0fs"
          % (m["trades"], m["compound_pct"], time.time() - t0))
    Z["runs"]["G0_MARK"] = ts
    pickle.dump(Z, open(HERE / "g3.pkl", "wb"))


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    for c in ("gx_chop", "gx_ar1"):
        if c not in d:
            d[c] = False
        d[c] = d[c].fillna(False).astype(bool)
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    return d


DF = {t: prep(Z["runs"][t]) for t in Z["runs"]}
B = DF["G0_BASE"]
M = DF.get("G0_MARK", B)
print("\n[AR1 보호 확인] BASE(mark) AR1 %d건 (CHOP 내 %d) — 후보별:"
      % (int(M.gx_ar1.sum()), int(M[M.chop].gx_ar1.sum())))
rows = []
for t in Z["bat"]:
    if t == "G0_BASE":
        continue
    d = DF[t]
    rows.append(dict(후보=t, AR1=int(d.gx_ar1.sum()), AR1_CHOP=int(d[d.chop].gx_ar1.sum()),
                     BASE_AR1=int(M.gx_ar1.sum()),
                     손상="없음" if int(d.gx_ar1.sum()) >= int(M.gx_ar1.sum()) else "감소!!"))
print(pd.DataFrame(rows).to_string(index=False))


def comp(df):
    p = df.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


print("\n[정정] 단일거래 델타 기여율 — 그 거래를 **양쪽에서** 빼고 델타를 다시 잰다")
rows = []
for t in Z["bat"]:
    if t == "G0_BASE":
        continue
    d = DF[t]
    tot = comp(d) - comp(B)
    if abs(tot) < 1e-9:
        continue
    keys = set(d[d.chop].k) | set(B[B.chop].k)
    best, bv = None, 0.0
    for k in keys:
        dd, bb = d[d.k != k], B[B.k != k]
        c = tot - (comp(dd) - comp(bb))
        if abs(c) > abs(bv):
            best, bv = k, c
    rows.append(dict(후보=t, Δ전체=round(tot, 2), 최대기여거래=str(best),
                     기여값=round(bv, 2), 기여율=round(100 * bv / tot, 1)))
print(pd.DataFrame(rows).to_string(index=False))
