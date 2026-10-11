"""W7 - GRACE/인과봉 리포트: 9/22 앵커 · 러너 복구 · false · 성과 · LOO/bootstrap."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 300)
HERE = Path(__file__).resolve().parent
W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
W6 = pickle.load(open(HERE / "w6.pkl", "rb"))
D = W6["dates"]; SH = W6["shadow"]
SEP = [d for d in D if d >= "20260901"]
RUNS = {"A_BASE": W1["runs"]["A_BASE"]}
RUNS.update(W6["runs"])
ORDER = ["A_BASE", "X0_B3", "X1c_snap0", "G1_grace6", "G2_grace9", "G3_gap_spread6", "G4_consec2_6"]
TAGS = [t for t in ORDER if t in RUNS]


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    d["win"] = d.net_pct > 0
    for c in ("gx_promoted", "gx_grace"):
        if c not in d: d[c] = False
        d[c] = d[c].fillna(False).astype(bool)
    for c in ("gx_prom_at", "gx_grace_at"):
        if c not in d: d[c] = None
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    return d


DF = {t: prep(RUNS[t]) for t in TAGS}
B, T3 = DF["A_BASE"], DF["X0_B3"]


def comp(df, dates=None):
    g = df if dates is None else df[df.date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


print("="*150); print("1. 9/22 앵커 (진입 12:15 DOWN_BLUE · AR1 전용 · BASE +4.026 · max-hold 만료 12:35)"); print("="*150)
rows = []
for t in TAGS:
    d = DF[t]
    s = d[(d.date.astype(str) == "20260922") & (d.entry_time.astype(str).str.contains("12:15"))]
    if not len(s):
        rows.append(dict(후보=t, 존재="없음")); continue
    r = s.iloc[0]
    rows.append(dict(후보=t, grace진입=(str(r.gx_grace_at)[11:19] if r.gx_grace else "-"),
                     승격=bool(r.gx_promoted),
                     승격시각=(str(r.gx_prom_at)[11:19] if r.gx_promoted else "-"),
                     청산=str(r.exit_time)[11:16], 사유=r.exit_reason,
                     net=round(float(r.net_pct),3), MFE=round(float(r.mfe),3),
                     BASE대비=round(float(r.net_pct)-4.0259,3), 보유분=round(float(r.hold_minutes),1)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n"+"="*150); print("2. 9월 CHOP 러너 (BASE MFE>=3%)"); print("="*150)
bsep = B[(B.date.isin(set(SEP))) & B.chop]
runners = bsep[bsep.mfe >= 3].copy()
rows = []
for _, r in runners.iterrows():
    row = dict(date=r.date, dir=r.direction, entry=str(r.entry_time)[11:16],
               MFE=round(float(r.mfe),3), BASE=round(float(r.net_pct),3))
    for t in TAGS:
        if t == "A_BASE": continue
        m = DF[t][DF[t].k == r.k]
        row[t] = ("%.3f%s" % (float(m.iloc[0].net_pct), "P" if m.iloc[0].gx_promoted else "")) if len(m) else "없음"
    rows.append(row)
print(pd.DataFrame(rows).to_string(index=False))

print("\n"+"="*150); print("3. 승격 품질 (9월 CHOP) — P=승격"); print("="*150)
truth = {r.k: float(r.mfe) for _, r in bsep.iterrows()}
tot_run = sum(1 for v in truth.values() if v >= 3)
rows = []
for t in TAGS:
    if t in ("A_BASE","X0_B3"): continue
    d = DF[t]; sd = d[(d.date.isin(set(SEP))) & d.chop]
    gr = sd[sd.gx_grace]; pr = sd[sd.gx_promoted]
    tp = sum(1 for _, r in pr.iterrows() if truth.get(r.k,0) >= 3)
    fp = sum(1 for _, r in pr.iterrows() if truth.get(r.k,99) < 1.5)
    rows.append(dict(후보=t, grace발동=len(gr), 승격=len(pr), true_runner=tp, false_lowMFE=fp,
                     기타=len(pr)-tp-fp,
                     precision=round(100*tp/len(pr),1) if len(pr) else 0.0,
                     recall=round(100*tp/tot_run,1) if tot_run else 0.0))
print(pd.DataFrame(rows).to_string(index=False))
print("  * 9월 CHOP MFE>=3%% 러너 총 %d건" % tot_run)

print("\n"+"="*150); print("4. 성과 (9월 / 최근30 / 80일)"); print("="*150)
W = {"9월(14일)": SEP, "최근30": D[-30:], "80일": D}
rows = []
for t in TAGS:
    r = {"후보": t}
    for wn, wd in W.items():
        g = DF[t][DF[t].date.isin(set(wd))].sort_values("exit_time")
        p = g.pnl.values; eq = np.cumprod(1+p/100)
        w_, l_ = p[g.net_pct>0], p[g.net_pct<=0]
        r[wn] = round(float((eq[-1]-1)*100),2)
        r[wn+"Δ B3"] = round(r[wn] - comp(T3, wd), 2)
        if wn == "9월(14일)":
            r["PF"] = round(float(w_.sum()/-l_.sum()),3) if len(l_) and l_.sum()<0 else np.inf
            r["MDD"] = round(float((eq/np.maximum.accumulate(eq)-1).min()*100),2)
            r["WR"] = round(100*g.win.mean(),1)
            ch = g[g.chop]
            r["run3"] = int((ch.mfe>=3).sum()); r["run5"] = int((ch.mfe>=5).sum()); r["run8"] = int((ch.mfe>=8).sum())
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n"+"="*150); print("5. LOO / bootstrap (80일, B3 대비)"); print("="*150)
DAY = {t: {k: g.pnl.values for k, g in DF[t].groupby("date")} for t in TAGS}
def cd(t, days):
    p = np.concatenate([DAY[t][x] for x in days if x in DAY[t]]) if days else np.array([])
    return float(((1+p/100).prod()-1)*100) if len(p) else 0.0
rng = np.random.default_rng(20260925)
BOOT = [list(rng.choice(D, len(D), replace=True)) for _ in range(10000)]
rows = []
for t in TAGS:
    if t in ("A_BASE","X0_B3"): continue
    loo = np.array([cd(t,[x for x in D if x!=dd]) - cd("X0_B3",[x for x in D if x!=dd]) for dd in D])
    bs = np.array([cd(t,s) - cd("X0_B3",s) for s in BOOT])
    rows.append(dict(후보=t, Δ80일=round(comp(DF[t])-comp(T3),2),
                     LOO최소=round(float(loo.min()),2), LOO중앙=round(float(np.median(loo)),2),
                     LOO음수일=int((loo<=0).sum()),
                     bs평균=round(float(bs.mean()),2),
                     bsCI="[%.1f, %.1f]"%(np.percentile(bs,2.5),np.percentile(bs,97.5)),
                     P_gt0=round(100*float((bs>0).mean()),1)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n[B3 러너 손상 대비 회복]")
dmg = 0.0
for _, r in runners.iterrows():
    m = T3[T3.k == r.k]
    if len(m): dmg += float(m.iloc[0].net_pct) - float(r.net_pct)
print("  B3 러너 손상 (9월 %d건): %+.3f" % (len(runners), dmg))
for t in TAGS:
    if t in ("A_BASE","X0_B3"): continue
    rec = 0.0
    for _, r in runners.iterrows():
        a, bb = T3[T3.k==r.k], DF[t][DF[t].k==r.k]
        if len(a) and len(bb): rec += float(bb.iloc[0].net_pct) - float(a.iloc[0].net_pct)
    print("  %-16s 회복 %+.3f (%.1f%%)" % (t, rec, 100*rec/-dmg if dmg else 0))
