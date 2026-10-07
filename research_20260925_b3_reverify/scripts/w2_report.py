"""W2 — RUNNER PROMOTION 리포트 (9월). 9/22 앵커 · runner 전량 · false promotion · 성과표."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 300)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "w1.pkl", "rb"))
D = Z["dates"]
SH = Z["shadow"]
SEP = [d for d in D if d >= "20260901"]
TAGS = ["A_BASE", "B_B3", "C_R1", "D_R2", "E_R3", "F_R4", "G_S3", "H_S4"]
TAGS = [t for t in TAGS if t in Z["runs"]]


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    d["mae"] = d.mae_net_pct
    d["win"] = d.net_pct > 0
    for c in ("gx_chop", "gx_promoted"):
        if c not in d:
            d[c] = False
        d[c] = d[c].fillna(False).astype(bool)
    for c in ("gx_prom_at", "gx_prom_score", "gx_prom_conds"):
        if c not in d:
            d[c] = None
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    return d


DF = {t: prep(Z["runs"][t]) for t in TAGS}
B, T3 = DF["A_BASE"], DF["B_B3"]


def comp(df, dates=None):
    g = df if dates is None else df[df.date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


# ── 9. 9/22 앵커 ──────────────────────────────────────────────────────────
print("=" * 150); print("9. 9/22 앵커 (플래그 12:09 · 확정 12:12 · 진입 12:15 · DOWN_BLUE · AR1 전용)")
print("=" * 150)
rows = []
for t in TAGS:
    d = DF[t]
    s = d[(d.date.astype(str) == "20260922") & (d.entry_time.astype(str).str.contains("12:15"))]
    if not len(s):
        rows.append(dict(후보=t, 존재="없음")); continue
    r = s.iloc[0]
    rows.append(dict(후보=t, 진입=str(r.entry_time)[11:16], 방향=r.direction,
                     승격=bool(r.gx_promoted), 승격시각=(str(r.gx_prom_at)[11:19] if r.gx_promoted else "-"),
                     점수=(int(r.gx_prom_score) if r.gx_promoted and r.gx_prom_score == r.gx_prom_score else None),
                     조건=(str(r.gx_prom_conds) if r.gx_promoted else "-"),
                     청산시각=str(r.exit_time)[11:16], 청산사유=r.exit_reason,
                     net=round(float(r.net_pct), 3), MFE=round(float(r.mfe), 3),
                     BASE대비=round(float(r.net_pct) - 4.0259, 3)))
print(pd.DataFrame(rows).to_string(index=False))
print("\n9/22 전체 거래 (후보별)")
for t in TAGS:
    d = DF[t]
    s = d[d.date.astype(str) == "20260922"]
    print("  [%s] %s" % (t, [(str(r.entry_time)[11:16], r.direction, round(float(r.net_pct), 3),
                             r.exit_reason, ("PROM" if r.gx_promoted else ""))
                            for _, r in s.iterrows()]))

# ── 4. 9월 runner 전량 ───────────────────────────────────────────────────
print("\n" + "=" * 150); print("4. 9월 CHOP 거래 중 BASE MFE>=3% — B3 가 잘라먹는 러너 전량")
print("=" * 150)
bsep = B[(B.date.isin(set(SEP))) & B.chop]
runners = bsep[bsep.mfe >= 3].copy()
rows = []
for _, r in runners.iterrows():
    row = dict(date=r.date, dir=r.direction, entry=str(r.entry_time)[11:16],
               BASE_MFE=round(float(r.mfe), 3), BASE_net=round(float(r.net_pct), 3),
               BASE_exit=r.exit_reason)
    for t in TAGS:
        if t == "A_BASE":
            continue
        m = DF[t][DF[t].k == r.k]
        if len(m):
            x = m.iloc[0]
            row[t] = ("%.3f%s" % (float(x.net_pct), "*" if x.gx_promoted else ""))
        else:
            row[t] = "없음"
    rows.append(row)
print(pd.DataFrame(rows).to_string(index=False))
print("  (* = RUNNER_PROMOTE 발동)")
for th in (3, 5, 8):
    n = int((bsep.mfe >= th).sum())
    print("  9월 CHOP MFE>=%d%%: BASE %d건" % (th, n))

# ── 5. false promotion ───────────────────────────────────────────────────
print("\n" + "=" * 150); print("5. promotion precision / recall (9월 CHOP 거래 기준)")
print("=" * 150)
truth = {r.k: float(r.mfe) for _, r in bsep.iterrows()}
rows = []
for t in TAGS:
    if t in ("A_BASE", "B_B3"):
        continue
    d = DF[t]
    sep_d = d[(d.date.isin(set(SEP))) & d.chop]
    prom = sep_d[sep_d.gx_promoted]
    tp = sum(1 for _, r in prom.iterrows() if truth.get(r.k, 0) >= 3)
    fp_low = sum(1 for _, r in prom.iterrows() if truth.get(r.k, 99) < 1.5)
    tot_run = sum(1 for v in truth.values() if v >= 3)
    rows.append(dict(후보=t, 승격=len(prom), true_runner=tp,
                     false_lowMFE=fp_low, 기타=len(prom) - tp - fp_low,
                     precision=round(100 * tp / len(prom), 1) if len(prom) else 0.0,
                     recall=round(100 * tp / tot_run, 1) if tot_run else 0.0))
print(pd.DataFrame(rows).to_string(index=False))
print("  * true_runner = BASE MFE>=3%% 거래를 승격 / false_lowMFE = BASE MFE<1.5%% 인데 승격")
print("  * 9월 CHOP 중 BASE MFE>=3%% 러너 총 %d건" % sum(1 for v in truth.values() if v >= 3))

# ── 8. 9월 성과표 ────────────────────────────────────────────────────────
print("\n" + "=" * 150); print("8. 9월 성과표"); print("=" * 150)
b3c = comp(T3, SEP); bac = comp(B, SEP)
rows = []
for t in TAGS:
    d = DF[t]
    g = d[d.date.isin(set(SEP))].sort_values("exit_time")
    p = g.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[g.net_pct > 0], p[g.net_pct <= 0]
    c = float((eq[-1] - 1) * 100)
    ch = g[g.chop]
    rows.append(dict(후보=t, 거래=len(g), 승률=round(100 * g.win.mean(), 1),
                     복리=round(c, 2), vsBASE=round(c - bac, 2), vsB3=round(c - b3c, 2),
                     PF=round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else np.inf,
                     MDD=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
                     평균net=round(g.net_pct.mean(), 3),
                     승격=int(g.gx_promoted.sum()),
                     run3=int((ch.mfe >= 3).sum()), run5=int((ch.mfe >= 5).sum()),
                     run8=int((ch.mfe >= 8).sum())))
print(pd.DataFrame(rows).to_string(index=False))

print("\n[B3 러너 손상 vs promotion 회복]")
dmg = 0.0
for _, r in runners.iterrows():
    m = T3[T3.k == r.k]
    if len(m):
        dmg += float(m.iloc[0].net_pct) - float(r.net_pct)
print("  B3 러너 손상 총합 (9월, BASE MFE>=3%% %d건): %+.3f" % (len(runners), dmg))
for t in TAGS:
    if t in ("A_BASE", "B_B3"):
        continue
    rec = 0.0
    for _, r in runners.iterrows():
        a = T3[T3.k == r.k]
        b = DF[t][DF[t].k == r.k]
        if len(a) and len(b):
            rec += float(b.iloc[0].net_pct) - float(a.iloc[0].net_pct)
    print("  %-6s 러너 회복 %+.3f  (손상 대비 %.1f%%) · 9월 복리 vsB3 %+.2f"
          % (t, rec, 100 * rec / -dmg if dmg else 0.0, comp(DF[t], SEP) - b3c))

# ── 전체 80일 참고 ────────────────────────────────────────────────────────
print("\n[참고] 80일 전체")
rows = []
for t in TAGS:
    rows.append(dict(후보=t, 거래=len(DF[t]), 복리=round(comp(DF[t]), 2),
                     vsBASE=round(comp(DF[t]) - comp(B), 2),
                     vsB3=round(comp(DF[t]) - comp(T3), 2)))
print(pd.DataFrame(rows).to_string(index=False))
