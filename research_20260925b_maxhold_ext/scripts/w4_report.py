"""W4 — MAX-HOLD 연장 리포트 (9월 중심). 9/22 앵커 · 러너 복구 · false 연장 · 성과표."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 300)
HERE = Path(__file__).resolve().parent
W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
W3 = pickle.load(open(HERE / "w3.pkl", "rb"))
D = W3["dates"]; SH = W3["shadow"]
SEP = [d for d in D if d >= "20260901"]
RUNS = {"A_BASE": W1["runs"]["A_BASE"]}
RUNS.update({k: v for k, v in W3["runs"].items()})
ORDER = ["A_BASE", "X0_B3", "X1_NG_prom", "X2_NGE_prom", "X3_NG_ext1", "X4_NG_ext3", "X5_S3_prom"]
TAGS = [t for t in ORDER if t in RUNS]


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    d["win"] = d.net_pct > 0
    for c in ("gx_chop", "gx_promoted"):
        if c not in d:
            d[c] = False
        d[c] = d[c].fillna(False).astype(bool)
    if "gx_ext_count" not in d:
        d["gx_ext_count"] = 0
    d["gx_ext_count"] = pd.to_numeric(d["gx_ext_count"], errors="coerce").fillna(0).astype(int)
    for c in ("gx_prom_at", "gx_prom_conds", "gx_ext_at"):
        if c not in d:
            d[c] = None
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    return d


DF = {t: prep(RUNS[t]) for t in TAGS}
B, T3 = DF["A_BASE"], DF["X0_B3"]


def comp(df, dates=None):
    g = df if dates is None else df[df.date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


print("=" * 150); print("9/22 앵커 (플래그 12:09 · 진입 12:15 · DOWN_BLUE · AR1 전용 · BASE +4.026)")
print("=" * 150)
rows = []
for t in TAGS:
    d = DF[t]
    s = d[(d.date.astype(str) == "20260922") & (d.entry_time.astype(str).str.contains("12:15"))]
    if not len(s):
        rows.append(dict(후보=t, 존재="없음")); continue
    r = s.iloc[0]
    rows.append(dict(후보=t, 승격=bool(r.gx_promoted), 연장횟수=int(r.gx_ext_count),
                     사건시각=(str(r.gx_prom_at)[11:19] if r.gx_promoted
                             else (str(r.gx_ext_at)[11:19] if r.gx_ext_count else "-")),
                     조건=(str(r.gx_prom_conds) if r.gx_promoted else "-"),
                     청산=str(r.exit_time)[11:16], 사유=r.exit_reason,
                     net=round(float(r.net_pct), 3), MFE=round(float(r.mfe), 3),
                     BASE대비=round(float(r.net_pct) - 4.0259, 3),
                     보유분=round(float(r.hold_minutes), 1)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 150); print("9월 CHOP 러너 전량 (BASE MFE>=3%)"); print("=" * 150)
bsep = B[(B.date.isin(set(SEP))) & B.chop]
runners = bsep[bsep.mfe >= 3].copy()
rows = []
for _, r in runners.iterrows():
    row = dict(date=r.date, dir=r.direction, entry=str(r.entry_time)[11:16],
               MFE=round(float(r.mfe), 3), BASE=round(float(r.net_pct), 3))
    for t in TAGS:
        if t == "A_BASE":
            continue
        m = DF[t][DF[t].k == r.k]
        if len(m):
            x = m.iloc[0]
            mark = "P" if x.gx_promoted else ("E%d" % x.gx_ext_count if x.gx_ext_count else "")
            row[t] = "%.3f%s" % (float(x.net_pct), mark)
        else:
            row[t] = "없음"
    rows.append(row)
print(pd.DataFrame(rows).to_string(index=False))
print("  (P=승격 / E n=연장 n회)")

print("\n" + "=" * 150); print("연장·승격 품질 (9월 CHOP)"); print("=" * 150)
truth = {r.k: float(r.mfe) for _, r in bsep.iterrows()}
tot_run = sum(1 for v in truth.values() if v >= 3)
rows = []
for t in TAGS:
    if t in ("A_BASE", "X0_B3"):
        continue
    d = DF[t]
    sd = d[(d.date.isin(set(SEP))) & d.chop]
    act = sd[(sd.gx_promoted) | (sd.gx_ext_count > 0)]
    tp = sum(1 for _, r in act.iterrows() if truth.get(r.k, 0) >= 3)
    fp = sum(1 for _, r in act.iterrows() if truth.get(r.k, 99) < 1.5)
    rows.append(dict(후보=t, 발동=len(act), true_runner=tp, false_lowMFE=fp,
                     기타=len(act) - tp - fp,
                     precision=round(100 * tp / len(act), 1) if len(act) else 0.0,
                     recall=round(100 * tp / tot_run, 1) if tot_run else 0.0))
print(pd.DataFrame(rows).to_string(index=False))
print("  * 9월 CHOP 중 BASE MFE>=3%% 러너 총 %d건" % tot_run)

print("\n" + "=" * 150); print("9월 성과표 (14영업일)"); print("=" * 150)
bac, b3c = comp(B, SEP), comp(T3, SEP)
rows = []
for t in TAGS:
    g = DF[t][DF[t].date.isin(set(SEP))].sort_values("exit_time")
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
                     승격=int(g.gx_promoted.sum()), 연장=int((g.gx_ext_count > 0).sum()),
                     run3=int((ch.mfe >= 3).sum()), run5=int((ch.mfe >= 5).sum()),
                     평균보유=round(ch.hold_minutes.mean(), 1) if len(ch) else 0))
print(pd.DataFrame(rows).to_string(index=False))

dmg = 0.0
for _, r in runners.iterrows():
    m = T3[T3.k == r.k]
    if len(m):
        dmg += float(m.iloc[0].net_pct) - float(r.net_pct)
print("\nB3 러너 손상 총합 (9월 %d건): %+.3f" % (len(runners), dmg))
for t in TAGS:
    if t in ("A_BASE", "X0_B3"):
        continue
    rec = 0.0
    for _, r in runners.iterrows():
        a, b = T3[T3.k == r.k], DF[t][DF[t].k == r.k]
        if len(a) and len(b):
            rec += float(b.iloc[0].net_pct) - float(a.iloc[0].net_pct)
    print("  %-12s 러너 회복 %+.3f (손상 대비 %.1f%%) · 9월 vsB3 %+.2f · 80일 vsB3 %+.2f"
          % (t, rec, 100 * rec / -dmg if dmg else 0, comp(DF[t], SEP) - b3c,
             comp(DF[t]) - comp(T3)))

print("\n" + "=" * 150); print("80일 전체"); print("=" * 150)
rows = []
for t in TAGS:
    d = DF[t]
    p = d.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[d.net_pct > 0], p[d.net_pct <= 0]
    rows.append(dict(후보=t, 거래=len(d), 복리=round(comp(d), 2),
                     vsBASE=round(comp(d) - comp(B), 2), vsB3=round(comp(d) - comp(T3), 2),
                     PF=round(float(w.sum() / -l.sum()), 3),
                     MDD=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
                     승률=round(100 * d.win.mean(), 1)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n[연장/승격 이벤트 로그 요약]")
for t in TAGS:
    lg = W3.get("logs", {}).get(t) or []
    if lg:
        print("  %-12s %s" % (t, pd.Series([x["kind"] for x in lg]).value_counts().to_dict()))
