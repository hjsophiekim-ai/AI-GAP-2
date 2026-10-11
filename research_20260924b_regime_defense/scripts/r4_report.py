"""R4 — 창별 성과 + lock 이벤트 전량 + runner 손상 + 강건성. 엔진 재실행 없음."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 320); pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 300)
HERE = Path(__file__).resolve().parent
Z3 = pickle.load(open(HERE / "r3.pkl", "rb"))
D = Z3["dates"]; RUNS = Z3["runs"]; TAGS = Z3["bat"]

SEP = [d for d in D if d >= "20260901"]
NON = [d for d in D if d < "20260901"]
W = {"9월(%d일)" % len(SEP): SEP, "비9월(%d일)" % len(NON): NON,
     "최근30": D[-30:], "앞50": D[:50], "뒤%d" % (len(D) - 50): D[50:],
     "앞40": D[:40], "뒤%d " % (len(D) - 40): D[40:], "전체%d" % len(D): D}


def df_of(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    return d


def met(d, dates):
    g = d[d.date.isin(set(dates))].sort_values("exit_time")
    if not len(g):
        return dict(n=0, comp=0.0, wr=0.0, pf=0.0, mdd=0.0)
    p = g.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[g.net_pct > 0], p[g.net_pct <= 0]
    return dict(n=len(g), comp=round(float((eq[-1] - 1) * 100), 2),
                wr=round(100 * float((g.net_pct > 0).mean()), 1),
                pf=round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else np.inf,
                mdd=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2))


DF = {t: df_of(RUNS[t]) for t in TAGS}
B = DF["P0_BASE"]

print("=" * 130); print("8. 창별 성과 (복리% / 거래수)"); print("=" * 130)
rows = []
for t in TAGS:
    r = {"후보": t}
    for wn, wd in W.items():
        m = met(DF[t], wd)
        r[wn] = m["comp"]
    r["거래"] = len(DF[t])
    rows.append(r)
T = pd.DataFrame(rows)
base_row = T[T.후보 == "P0_BASE"].iloc[0]
print(T.to_string(index=False))
print("\n[BASE 대비 Δ]")
T2 = T.copy()
for wn in W:
    T2[wn] = (T[wn] - base_row[wn]).round(2)
print(T2[T2.후보 != "P0_BASE"].to_string(index=False))

print("\n" + "=" * 130); print("runner 손상 — MFE 분포와 큰 이익거래 보존"); print("=" * 130)
rows = []
for t in TAGS:
    d = DF[t]
    rows.append(dict(후보=t, 거래=len(d),
                     MFE3이상=int((d.mfe >= 3).sum()), MFE5이상=int((d.mfe >= 5).sum()),
                     MFE8이상=int((d.mfe >= 8).sum()),
                     net3이상=int((d.net_pct >= 3).sum()), net5이상=int((d.net_pct >= 5).sum()),
                     상위5합=round(d.nlargest(5, "pnl").pnl.sum(), 2),
                     평균MFE=round(d.mfe.mean(), 3), 평균net=round(d.net_pct.mean(), 3),
                     일평균자본=round(d.groupby("date").w1a.sum().mean(), 3)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 130); print("3. PROFIT LOCK 이벤트 전량 (BASE 대비 정확 비교)"); print("=" * 130)
key = ["date", "entry_time", "direction"]
for t in [x for x in TAGS if x.startswith("P") and x != "P0_BASE"]:
    d = DF[t]
    fired = d[d.rx_lock_fire_at.notna()]
    print("\n[%s] 발동 %d건 / regime ON %d건 / 전체 %d건"
          % (t, len(fired), int(d.rx_on.sum()), len(d)))
    if not len(fired):
        continue
    j = fired.merge(B[key + ["exit_time", "exit_price", "exit_reason", "net_pct", "peak_net_pct"]],
                    on=key, how="left", suffixes=("", "_base"))
    j["uplift"] = j.net_pct - j.net_pct_base
    cols = ["date", "direction", "entry_time", "rx_lock_arm_at", "rx_lock_fire_at",
            "exit_price", "exit_price_base", "exit_time_base", "exit_reason_base",
            "net_pct", "net_pct_base", "uplift", "peak_net_pct"]
    print(j[cols].to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    print("   uplift 합 %+.3f / 양 %d / 음 %d"
          % (j.uplift.sum(), int((j.uplift > 0).sum()), int((j.uplift < 0).sum())))

print("\n" + "=" * 130); print("월별"); print("=" * 130)
rows = []
for t in TAGS:
    d = DF[t]; d["ym"] = d.date.astype(str).str[:6]
    r = {"후보": t}
    for ym, g in d.groupby("ym"):
        r[ym] = met(d, [x for x in D if str(x)[:6] == ym])["comp"]
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))
