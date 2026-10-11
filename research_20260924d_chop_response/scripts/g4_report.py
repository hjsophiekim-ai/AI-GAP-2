"""G4 — CHOP RESPONSE 리포트: parity / 표1·2·3 / 제거·대체·밀림 / AR1 손상."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 400)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "g3.pkl", "rb"))
D, RUNS, LOGS = Z["dates"], Z["runs"], Z.get("logs", {})
TAGS = sys.argv[1:] or Z["bat"]
SEP = [d for d in D if d >= "20260901"]
NON = [d for d in D if d < "20260901"]
SH = np.load(HERE / "shadow_on.npy")
W = {"9월(12일)": SEP, "비9월(66일)": NON, "최근30": D[-30:], "앞50": D[:50],
     "뒤28": D[50:], "앞39": D[:39], "뒤39": D[39:], "전체78": D}


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    d["mae"] = d.mae_net_pct
    d["win"] = d.net_pct > 0
    for c in ("gx_chop", "gx_ar1"):
        if c not in d:
            d[c] = False
        d[c] = d[c].fillna(False).astype(bool)
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    t = pd.to_datetime(d.entry_time, utc=True).dt.tz_convert("Asia/Seoul")
    d["tmin"] = t.dt.hour * 60 + t.dt.minute
    d["ym"] = d.date.astype(str).str[:6]
    return d


DF = {t: prep(RUNS[t]) for t in RUNS}
B = DF["G0_BASE"]
DAY = {t: {k: g.pnl.values for k, g in DF[t].groupby("date")} for t in RUNS}


def cd(t, days):
    p = np.concatenate([DAY[t][x] for x in days if x in DAY[t]]) if days else np.array([])
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


def full(t, dates=None):
    g = DF[t] if dates is None else DF[t][DF[t].date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


# ── 1. TREND OFF-PARITY ───────────────────────────────────────────────────
print("=" * 150); print("1. TREND OFF-PARITY — CHOP OFF 구간이 BASE 와 diff 0 인가"); print("=" * 150)
bo = B[~B.chop]
rows = []
for t in TAGS:
    if t == "G0_BASE":
        continue
    o = DF[t][~DF[t].chop]
    same_keys = set(o.k) == set(bo.k)
    j = o.merge(bo[["k", "net_pct", "w1a", "exit_time", "exit_reason"]], on="k",
                how="inner", suffixes=("", "_b"))
    dif = int(((j.net_pct - j.net_pct_b).abs() > 1e-9).sum()
              + (j.w1a - j.w1a_b).abs().gt(1e-9).sum()
              + (j.exit_time != j.exit_time_b).sum())
    rows.append(dict(후보=t, OFF거래_후보=len(o), OFF거래_BASE=len(bo),
                     키집합일치=same_keys, 결과차이건수=dif,
                     PARITY="OK" if (same_keys and dif == 0) else "<<< 불일치"))
P = pd.DataFrame(rows)
print(P.to_string(index=False))
if (P.PARITY != "OK").any():
    print("\n!! OFF-parity 위반 — 해당 후보는 판정에서 제외한다")

# ── 표 1. CHOP 구간 대응 비교 ─────────────────────────────────────────────
print("\n" + "=" * 150); print("표1. CHOP 구간 거래만 (CHOP ON 에서 진입한 거래)"); print("=" * 150)
rows = []
for t in TAGS:
    c = DF[t][DF[t].chop]
    if not len(c):
        rows.append(dict(후보=t, 거래=0)); continue
    p = c.pnl.values
    w, l = p[c.net_pct > 0], p[c.net_pct <= 0]
    eq = np.cumprod(1 + p / 100)
    rows.append(dict(후보=t, 거래=len(c), WR=round(100 * c.win.mean(), 1),
                     평균net=round(c.net_pct.mean(), 3), 합=round(p.sum(), 3),
                     복리=round(float((eq[-1] - 1) * 100), 3),
                     PF=round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else np.inf,
                     MDD=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
                     평균MFE=round(c.mfe.mean(), 3), 평균MAE=round(c.mae.mean(), 3),
                     AR1=int(c.gx_ar1.sum()),
                     run3=int((c.mfe >= 3).sum()), run5=int((c.mfe >= 5).sum())))
print(pd.DataFrame(rows).to_string(index=False))

# ── 표 2. 전체 전략 비교 ─────────────────────────────────────────────────
print("\n" + "=" * 150); print("표2. 전체 전략 비교"); print("=" * 150)
rows = []
for t in TAGS:
    d = DF[t]; p = d.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[d.net_pct > 0], p[d.net_pct <= 0]
    r = {"후보": t, "거래": len(d), "승률": round(100 * d.win.mean(), 1),
         "PF": round(float(w.sum() / -l.sum()), 3),
         "MDD": round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2)}
    for wn, wd in W.items():
        r[wn] = round(full(t, wd), 2)
    rows.append(r)
T = pd.DataFrame(rows)
print(T.to_string(index=False))
print("\n[BASE 대비 Δ]")
b0 = T[T.후보 == "G0_BASE"].iloc[0]
T2 = T.copy()
for wn in W:
    T2[wn] = (T[wn] - b0[wn]).round(2)
T2["ΔPF"] = (T.PF - b0.PF).round(3)
T2["Δ승률"] = (T.승률 - b0.승률).round(1)
print(T2[T2.후보 != "G0_BASE"].to_string(index=False))

# ── 제거 / 대체 / 밀림 / AR1 ─────────────────────────────────────────────
print("\n" + "=" * 150); print("11. 제거된 BASE 거래 / 대체진입 / AR1 손상"); print("=" * 150)
rows = []
for t in TAGS:
    if t == "G0_BASE":
        continue
    d = DF[t]
    lost = B[~B.k.isin(set(d.k))]
    added = d[~d.k.isin(set(B.k))]
    rows.append(dict(후보=t, 제거=len(lost), 제거합=round(lost.pnl.sum(), 3),
                     제거_이익=int((lost.net_pct > 0).sum()), 제거_손실=int((lost.net_pct <= 0).sum()),
                     대체진입=len(added), 대체합=round(added.pnl.sum(), 3),
                     AR1_BASE=int(B.gx_ar1.sum()), AR1_후보=int(d.gx_ar1.sum()),
                     스킵로그=len(LOGS.get(t) or [])))
print(pd.DataFrame(rows).to_string(index=False))

# ── 표 3. 강건성 ─────────────────────────────────────────────────────────
print("\n" + "=" * 150); print("표3. 강건성 (bootstrap 10,000 · 일 단위 복원추출)"); print("=" * 150)
rng = np.random.default_rng(20260924)
BOOT = [list(rng.choice(D, len(D), replace=True)) for _ in range(10000)]
rows = []
for t in TAGS:
    d = DF[t]
    r = {"후보": t, "Δ전체": round(full(t) - full("G0_BASE"), 2),
         "Δ9월": round(full(t, SEP) - full("G0_BASE", SEP), 2),
         "Δ최근30": round(full(t, D[-30:]) - full("G0_BASE", D[-30:]), 2)}
    for n in (1, 3, 5):
        cv = float(((1 + d.drop(d.nlargest(n, "pnl").index).sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)
        bv = float(((1 + B.drop(B.nlargest(n, "pnl").index).sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)
        r["Δtop%d제외" % n] = round(cv - bv, 2)
    loo = np.array([cd(t, [x for x in D if x != day]) - cd("G0_BASE", [x for x in D if x != day]) for day in D])
    r["LOO최소"] = round(float(loo.min()), 2); r["LOO≤0"] = int((loo <= 0).sum())
    bs = np.array([cd(t, s) - cd("G0_BASE", s) for s in BOOT])
    r["bs평균"] = round(float(bs.mean()), 2)
    r["bsCI"] = "[%.1f, %.1f]" % (np.percentile(bs, 2.5), np.percentile(bs, 97.5))
    r["P(>0)%"] = round(100 * float((bs > 0).mean()), 1)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n[단일거래 최대기여율]")
rows = []
for t in TAGS:
    if t == "G0_BASE":
        continue
    d = DF[t]
    tot = full(t) - full("G0_BASE")
    if abs(tot) < 1e-9:
        rows.append(dict(후보=t, Δ전체=0.0, 기여율="-")); continue
    best, bv = None, 0.0
    for _, row in d.iterrows():
        m = d.k != row.k
        cv = float(((1 + d[m].sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)
        c = full(t) - cv
        if abs(c) > abs(bv):
            best, bv = (row.date, row.entry_time, round(row.net_pct, 3)), c
    rows.append(dict(후보=t, Δ전체=round(tot, 2), 최대기여거래=str(best),
                     기여값=round(bv, 2), 기여율=round(100 * bv / tot, 1)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n[월별]")
rows = []
for t in TAGS:
    r = {"후보": t}
    for ym in sorted(set(DF[t].ym)):
        r[ym] = round(full(t, [x for x in D if str(x)[:6] == ym]), 2)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n[CHOP 거래 방향/시간대 — best 후보 진단용]")
for t in TAGS:
    c = DF[t][DF[t].chop]
    if not len(c):
        continue
    g1 = c.groupby("direction").agg(n=("net_pct", "size"), WR=("win", "mean"), net=("net_pct", "mean"))
    g1["WR"] = (100 * g1.WR).round(1)
    TE = [0, 600, 690, 780, 840, 1440]
    TL = ["09~10", "10~11:30", "11:30~13", "13~14", "14~"]
    c2 = c.copy(); c2["tb"] = pd.cut(c2.tmin, TE, labels=TL, right=False)
    g2 = c2.groupby("tb", observed=True).agg(n=("net_pct", "size"), WR=("win", "mean"), net=("net_pct", "mean"))
    g2["WR"] = (100 * g2.WR).round(1)
    print("\n[%s] 방향: %s | 시간대: %s"
          % (t, g1.round(3).to_dict("index"), g2.round(3).to_dict("index")))
