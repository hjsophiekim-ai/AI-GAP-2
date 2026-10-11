"""Y2 — X1c 계열 80일 완주검증 + 승격 전량 precision/recall + 러너보호 vs false 비용.

항목1: Top1/3/5/10 제외 · LOO · bootstrap · 월별 · 최근30 · 승격거래 전량
항목3: B3 가 잘라낸 MFE>=3/5/8 중 몇 건을 살렸나 / 손실거래를 몇 건 늘렸나 (건수 기준)
엔진 재실행 없음.
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 400)
HERE = Path(__file__).resolve().parent
W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
Y = pickle.load(open(HERE / "y1.pkl", "rb"))
D = Y["dates"]; SH = Y["shadow"]
SEP = [d for d in D if d >= "20260901"]
RUNS = {"BASE": W1["runs"]["A_BASE"], "B3": Y["runs"]["Y0_B3"]}
for k in ("Y1_gap", "Y2_gap_spread", "Y3_gap_etf"):
    if k in Y["runs"]:
        RUNS[k] = Y["runs"][k]
TAGS = list(RUNS)
CAND = [t for t in TAGS if t not in ("BASE", "B3")]


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    d["win"] = d.net_pct > 0
    if "gx_promoted" not in d:
        d["gx_promoted"] = False
    d["gx_promoted"] = d.gx_promoted.fillna(False).astype(bool)
    if "gx_prom_at" not in d:
        d["gx_prom_at"] = None
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    d["ym"] = d.date.astype(str).str[:6]
    return d


DF = {t: prep(RUNS[t]) for t in TAGS}
B, T3 = DF["BASE"], DF["B3"]


def comp(df, dates=None):
    g = df if dates is None else df[df.date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


print("=" * 155); print("항목1-a. 창별 성과 (기준 = B3)"); print("=" * 155)
W = {"80일": D, "최근30": D[-30:], "9월(14일)": SEP, "비9월": [d for d in D if d < "20260901"],
     "앞40": D[:40], "뒤40": D[40:]}
rows = []
for t in TAGS:
    r = {"후보": t, "거래": len(DF[t])}
    for wn, wd in W.items():
        c = comp(DF[t], wd)
        r[wn] = round(c, 2)
        if t != "B3":
            r[wn + "ΔB3"] = round(c - comp(T3, wd), 2)
    g = DF[t]
    p = g.pnl.values
    eq = np.cumprod(1 + p / 100)
    w_, l_ = p[g.net_pct > 0], p[g.net_pct <= 0]
    r["PF"] = round(float(w_.sum() / -l_.sum()), 3)
    r["MDD"] = round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2)
    r["WR"] = round(100 * g.win.mean(), 1)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 155); print("항목1-b. Top N 제외 (각자 자기 상위거래 제외, B3 기준 delta)"); print("=" * 155)
rows = []
for t in CAND:
    tot = comp(DF[t]) - comp(T3)
    r = {"후보": t, "Δ전체": round(tot, 2)}
    for n in (1, 3, 5, 10):
        bb = T3.drop(T3.nlargest(n, "pnl").index)
        tt = DF[t].drop(DF[t].nlargest(n, "pnl").index)
        dv = comp(tt) - comp(bb)
        r["Top%d" % n] = round(dv, 2)
        r["Top%d유지율" % n] = round(100 * dv / tot, 1) if tot else 0.0
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 155); print("항목1-c. LOO / bootstrap (80일, B3 대비)"); print("=" * 155)
DAY = {t: {k: g.pnl.values for k, g in DF[t].groupby("date")} for t in TAGS}


def cd(t, days):
    p = np.concatenate([DAY[t][x] for x in days if x in DAY[t]]) if days else np.array([])
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


rng = np.random.default_rng(20260925)
BOOT = [list(rng.choice(D, len(D), replace=True)) for _ in range(10000)]
rows = []
for t in CAND:
    loo = np.array([cd(t, [x for x in D if x != dd]) - cd("B3", [x for x in D if x != dd]) for dd in D])
    bs = np.array([cd(t, s) - cd("B3", s) for s in BOOT])
    rows.append(dict(후보=t, Δ80일=round(comp(DF[t]) - comp(T3), 2),
                     LOO최소=round(float(loo.min()), 2), LOO중앙=round(float(np.median(loo)), 2),
                     LOO최대=round(float(loo.max()), 2), LOO음수일=int((loo <= 0).sum()),
                     bs평균=round(float(bs.mean()), 2), bs중앙=round(float(np.median(bs)), 2),
                     bs5=round(float(np.percentile(bs, 5)), 2), bs95=round(float(np.percentile(bs, 95)), 2),
                     P_gt0=round(100 * float((bs > 0).mean()), 1)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 155); print("항목1-d. 월별"); print("=" * 155)
rows = []
for t in TAGS:
    r = {"후보": t}
    for ym in sorted(set(B.ym)):
        dd = [x for x in D if str(x)[:6] == ym]
        r[ym] = round(comp(DF[t], dd), 2)
    rows.append(r)
M = pd.DataFrame(rows)
print(M.to_string(index=False))
print("\n[B3 대비 Δ]")
b3row = M[M.후보 == "B3"].iloc[0]
M2 = M.copy()
for ym in sorted(set(B.ym)):
    M2[ym] = (M[ym] - b3row[ym]).round(2)
print(M2[M2.후보 != "B3"].to_string(index=False))

print("\n" + "=" * 155); print("항목1-e. 승격 거래 전량 (80일)"); print("=" * 155)
for t in CAND:
    d = DF[t]
    pr = d[d.gx_promoted]
    j = pr.merge(T3[["k", "net_pct", "exit_reason"]], on="k", how="left", suffixes=("", "_b3"))
    j = j.merge(B[["k", "net_pct", "peak_net_pct"]], on="k", how="left", suffixes=("", "_base"))
    j["Δ_vs_B3"] = j.net_pct - j.net_pct_b3
    j["BASE_MFE"] = j.peak_net_pct
    print("\n[%s] 승격 %d건" % (t, len(j)))
    cols = ["date", "direction", "entry_time", "gx_prom_at", "BASE_MFE", "net_pct_base",
            "net_pct_b3", "net_pct", "Δ_vs_B3", "exit_reason"]
    cols = [c for c in cols if c in j.columns]
    print(j[cols].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    print("   Δ합 %+.3f (양 %d / 음 %d)" % (j.Δ_vs_B3.sum(), int((j.Δ_vs_B3 > 0).sum()),
                                          int((j.Δ_vs_B3 < 0).sum())))

print("\n" + "=" * 155); print("항목1-f. precision / recall (승격 전량, BASE MFE 기준)"); print("=" * 155)
bchop = B[B.chop]
truth = {r.k: float(r.mfe) for _, r in bchop.iterrows()}
rows = []
for t in CAND:
    pr = DF[t][DF[t].gx_promoted]
    for th in (3, 5, 8):
        tot = sum(1 for v in truth.values() if v >= th)
        tp = sum(1 for _, r in pr.iterrows() if truth.get(r.k, 0) >= th)
        rows.append(dict(후보=t, 기준="MFE>=%d%%" % th, 승격=len(pr), 적중=tp, 모집단=tot,
                         precision=round(100 * tp / len(pr), 1) if len(pr) else 0.0,
                         recall=round(100 * tp / tot, 1) if tot else 0.0))
    fp = sum(1 for _, r in pr.iterrows() if truth.get(r.k, 99) < 1.5)
    rows.append(dict(후보=t, 기준="false(MFE<1.5%)", 승격=len(pr), 적중=fp, 모집단=None,
                     precision=None, recall=None))
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 155); print("항목3. 러너 보호율 vs false promotion — **건수** 기준"); print("=" * 155)
rows = []
for t in CAND:
    d = DF[t]
    cut = []          # B3 가 잘라낸 거래 = BASE MFE>=th 인데 B3 net < BASE net
    for th in (3, 5, 8):
        pop = [k for k, v in truth.items() if v >= th]
        saved = 0
        for k in pop:
            a, bb = T3[T3.k == k], d[d.k == k]
            base = B[B.k == k]
            if len(a) and len(bb) and len(base):
                if float(bb.iloc[0].net_pct) > float(a.iloc[0].net_pct) + 1e-9:
                    saved += 1
        rows.append(dict(후보=t, 구간="MFE>=%d%%" % th, 모집단=len(pop), 살린건수=saved,
                         보호율=round(100 * saved / len(pop), 1) if pop else 0.0))
print(pd.DataFrame(rows).to_string(index=False))

print("\n[손실거래 증가 — 승격이 만든 대가]")
rows = []
for t in CAND:
    d = DF[t]
    pr = d[d.gx_promoted]
    w2l = l2w = 0
    worse = better = 0
    dmg = gain = 0.0
    for _, r in pr.iterrows():
        a = T3[T3.k == r.k]
        if not len(a):
            continue
        b3n, cn = float(a.iloc[0].net_pct), float(r.net_pct)
        if b3n > 0 >= cn:
            w2l += 1
        if b3n <= 0 < cn:
            l2w += 1
        if cn < b3n - 1e-9:
            worse += 1; dmg += cn - b3n
        elif cn > b3n + 1e-9:
            better += 1; gain += cn - b3n
    # 전체 손실거래 수 비교
    rows.append(dict(후보=t, 승격=len(pr), 개선건수=better, 개선합=round(gain, 3),
                     악화건수=worse, 악화합=round(dmg, 3),
                     승__패=w2l, 패__승=l2w,
                     전체손실거래_B3=int((T3.net_pct <= 0).sum()),
                     전체손실거래_후보=int((d.net_pct <= 0).sum()),
                     손실거래증가=int((d.net_pct <= 0).sum() - (T3.net_pct <= 0).sum())))
print(pd.DataFrame(rows).to_string(index=False))
