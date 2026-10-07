"""Z3 — TP-RUNNER-RESCUE 리포트: 러너 5건 절단지점 구분 · 성과 · 강건성 · 균형."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 70)
pd.set_option("display.max_rows", 300)
HERE = Path(__file__).resolve().parent
W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
Z = pickle.load(open(HERE / "d2.pkl", "rb"))
D = Z["dates"]; SH = Z["shadow"]
SEP = [d for d in D if d >= "20260901"]
RUNS = {"BASE": W1["runs"]["A_BASE"]}
for k in ("A_B3", "B_Y3", "P1_fast12", "P2_fast12_ez2", "P3_fast6"):
    if k in Z["runs"]:
        RUNS[k] = Z["runs"][k]
TAGS = list(RUNS)
CAND = [t for t in TAGS if t not in ("BASE", "A_B3")]


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    d["win"] = d.net_pct > 0
    for c in ("gx_promoted", "gx_rescue"):
        if c not in d:
            d[c] = False
        d[c] = d[c].fillna(False).astype(bool)
    for c in ("gx_rescue_at", "gx_rescue_conds", "gx_prom_at"):
        if c not in d:
            d[c] = None
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    d["ym"] = d.date.astype(str).str[:6]
    return d


DF = {t: prep(RUNS[t]) for t in TAGS}
B, T3 = DF["BASE"], DF["A_B3"]


def comp(df, dates=None):
    g = df if dates is None else df[df.date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


print("=" * 155); print("0. B3 가 잘라먹은 러너 5건 — **절단 지점 구분**"); print("=" * 155)
bchop = B[B.chop]
runners = bchop[bchop.mfe >= 3].copy()
rows = []
for _, r in runners.iterrows():
    m = T3[T3.k == r.k]
    cut = m.iloc[0].exit_reason if len(m) else "없음"
    row = dict(date=r.date, dir=r.direction, entry=str(r.entry_time)[11:16],
               BASE_MFE=round(float(r.mfe), 3), BASE_net=round(float(r.net_pct), 3),
               B3_net=round(float(m.iloc[0].net_pct), 3) if len(m) else None,
               B3_절단=("TP(+1.0%)" if cut == "GX_TP" else
                      ("max-hold(20분)" if cut == "GX_MAXHOLD" else cut)),
               손상=round(float(m.iloc[0].net_pct) - float(r.net_pct), 3) if len(m) else None)
    for t in CAND:
        x = DF[t][DF[t].k == r.k]
        if len(x):
            mk = "R" if x.iloc[0].gx_rescue else ("P" if x.iloc[0].gx_promoted else "")
            row[t] = "%.3f%s" % (float(x.iloc[0].net_pct), mk)
        else:
            row[t] = "없음"
    rows.append(row)
RN = pd.DataFrame(rows)
print(RN.to_string(index=False))
print("  (R = TP-RESCUE 부분익절+승격 / P = max-hold 승격)")
print("\n  절단 지점 분포: %s" % RN.B3_절단.value_counts().to_dict())

print("\n" + "=" * 155); print("1. 성과 비교"); print("=" * 155)
W = {"80일": D, "최근30": D[-30:], "9월": SEP, "비9월": [d for d in D if d < "20260901"]}
rows = []
for t in TAGS:
    d = DF[t]
    p = d.pnl.values
    eq = np.cumprod(1 + p / 100)
    w_, l_ = p[d.net_pct > 0], p[d.net_pct <= 0]
    r = {"후보": t, "거래": len(d)}
    for wn, wd in W.items():
        r[wn] = round(comp(d, wd), 2)
    r["PF"] = round(float(w_.sum() / -l_.sum()), 3)
    r["MDD"] = round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2)
    r["승률"] = round(100 * d.win.mean(), 1)
    r["승격"] = int(d.gx_promoted.sum()); r["구조"] = int(d.gx_rescue.sum())
    rows.append(r)
P = pd.DataFrame(rows)
print(P.to_string(index=False))
print("\n[B_Y3 대비 Δ]")
by = P[P.후보 == "B_Y3"].iloc[0]
P2 = P.copy()
for wn in W:
    P2[wn] = (P[wn] - by[wn]).round(2)
P2["ΔPF"] = (P.PF - by.PF).round(3)
print(P2[P2.후보.isin(["C1_R1", "C2_R2", "C3_R3", "A_B3", "BASE"])].to_string(index=False))

print("\n[월별]")
rows = []
for t in TAGS:
    r = {"후보": t}
    for ym in sorted(set(B.ym)):
        r[ym] = round(comp(DF[t], [x for x in D if str(x)[:6] == ym]), 2)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 155); print("2. 강건성 (B_Y3 기준)"); print("=" * 155)
DAY = {t: {k: g.pnl.values for k, g in DF[t].groupby("date")} for t in TAGS}


def cd(t, days):
    p = np.concatenate([DAY[t][x] for x in days if x in DAY[t]]) if days else np.array([])
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


rng = np.random.default_rng(20260925)
BOOT = [list(rng.choice(D, len(D), replace=True)) for _ in range(10000)]
rows = []
for t in ["A_B3"] + [c for c in CAND if c != "B_Y3"]:
    tot = comp(DF[t]) - comp(DF["B_Y3"])
    r = {"후보": t, "ΔB_Y3": round(tot, 2)}
    for n in (5, 10):
        bb = DF["B_Y3"].drop(DF["B_Y3"].nlargest(n, "pnl").index)
        tt = DF[t].drop(DF[t].nlargest(n, "pnl").index)
        r["Top%d제외" % n] = round(comp(tt) - comp(bb), 2)
    loo = np.array([cd(t, [x for x in D if x != dd]) - cd("B_Y3", [x for x in D if x != dd]) for dd in D])
    bs = np.array([cd(t, s) - cd("B_Y3", s) for s in BOOT])
    r["LOO최소"] = round(float(loo.min()), 2); r["LOO중앙"] = round(float(np.median(loo)), 2)
    r["LOO음수일"] = int((loo <= 0).sum())
    r["bs평균"] = round(float(bs.mean()), 2); r["P_gt0"] = round(100 * float((bs > 0).mean()), 1)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n[BASE 기준 강건성 — 최종 후보 확인용]")
rows = []
for t in ["A_B3", "B_Y3"] + [c for c in CAND if c != "B_Y3"]:
    tot = comp(DF[t]) - comp(B)
    r = {"후보": t, "ΔBASE": round(tot, 2)}
    for n in (5, 10):
        bb = B.drop(B.nlargest(n, "pnl").index)
        tt = DF[t].drop(DF[t].nlargest(n, "pnl").index)
        r["Top%d제외" % n] = round(comp(tt) - comp(bb), 2)
    loo = np.array([cd(t, [x for x in D if x != dd]) - cd("BASE", [x for x in D if x != dd]) for dd in D])
    bs = np.array([cd(t, s) - cd("BASE", s) for s in BOOT])
    r["LOO최소"] = round(float(loo.min()), 2); r["LOO음수일"] = int((loo <= 0).sum())
    r["P_gt0"] = round(100 * float((bs > 0).mean()), 1)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 155); print("3. runner 보존율 / false promotion / 균형"); print("=" * 155)
rows = []
for t in ["A_B3"] + CAND:
    d = DF[t]
    r = {"후보": t}
    for th in (3, 5, 8):
        pop = bchop[bchop.mfe >= th]
        keep = 0
        for _, x in pop.iterrows():
            m = d[d.k == x.k]
            if len(m) and float(m.iloc[0].net_pct) >= float(x.net_pct) - 1e-9:
                keep += 1
        r["MFE>=%d" % th] = "%d/%d" % (keep, len(pop))
        r["보존율%d" % th] = round(100 * keep / len(pop), 1) if len(pop) else 0.0
    # false promotion (BASE MFE<1.5 인데 승격/구조)
    act = d[(d.gx_promoted) | (d.gx_rescue)]
    j = act.merge(B[["k", "peak_net_pct"]].rename(columns={"peak_net_pct": "mfe_b"}),
                  on="k", how="left")
    r["개입"] = len(act)
    r["false(MFE<1.5)"] = int((j.mfe_b < 1.5).sum())
    # B3 대비
    jj = d.merge(T3[["k", "net_pct"]], on="k", how="inner", suffixes=("", "_b3"))
    jj["dd"] = jj.net_pct - jj.net_pct_b3
    r["B3대비_회복"] = round(float(jj[jj.dd > 0].dd.sum()), 3)
    r["B3대비_추가손실"] = round(float(jj[jj.dd < 0].dd.sum()), 3)
    r["순"] = round(float(jj.dd.sum()), 3)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n[개입 거래 전량]")
for t in CAND:
    d = DF[t]
    act = d[(d.gx_promoted) | (d.gx_rescue)]
    j = act.merge(T3[["k", "net_pct"]], on="k", how="left", suffixes=("", "_b3"))
    j = j.merge(B[["k", "peak_net_pct"]].rename(columns={"peak_net_pct": "MFE_base"}),
                on="k", how="left")
    j["ΔB3"] = j.net_pct - j.net_pct_b3
    print("\n[%s] 개입 %d건 (구조 %d / max-hold승격 %d)"
          % (t, len(j), int(act.gx_rescue.sum()), int((act.gx_promoted & ~act.gx_rescue).sum())))
    if len(j):
        cols = ["date", "direction", "entry_time", "MFE_base", "net_pct_b3", "net_pct",
                "ΔB3", "gx_rescue", "gx_rescue_conds", "exit_reason"]
        cols = [c for c in cols if c in j.columns]
        print(j[cols].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
        print("   ΔB3 합 %+.3f (양 %d / 음 %d)"
              % (j.ΔB3.sum(), int((j.ΔB3 > 0).sum()), int((j.ΔB3 < 0).sum())))

print("\n[단일거래 의존 — ΔB_Y3 기여율]")
rows = []
for t in [c for c in CAND if c != "B_Y3"]:
    d = DF[t]
    tot = comp(d) - comp(DF["B_Y3"])
    if abs(tot) < 1e-9:
        rows.append(dict(후보=t, Δ=0.0, 최대기여="-", 기여율="-")); continue
    keys = set(d[d.chop].k) | set(DF["B_Y3"][DF["B_Y3"].chop].k)
    best, bv = None, 0.0
    for k in keys:
        c = tot - (comp(d[d.k != k]) - comp(DF["B_Y3"][DF["B_Y3"].k != k]))
        if abs(c) > abs(bv):
            best, bv = k, c
    rows.append(dict(후보=t, Δ=round(tot, 2), 최대기여=str(best), 기여값=round(bv, 2),
                     기여율=round(100 * bv / tot, 1)))
print(pd.DataFrame(rows).to_string(index=False))
