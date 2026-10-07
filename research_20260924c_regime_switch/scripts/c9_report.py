"""C9 — CHOP 배터리 리포트: 창별 / regime ON·OFF / 시간대 / 방향 / runner 손상 /
밀어냄 로그 / 강건성(Top1·3·5 · LOO · bootstrap 10,000). 엔진 재실행 없음."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 400)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "c8.pkl", "rb"))
D, RUNS, LOGS = Z["dates"], Z["runs"], Z.get("logs", {})
TAGS = sys.argv[1:] or Z["bat"]
SEP = [d for d in D if d >= "20260901"]
NON = [d for d in D if d < "20260901"]
W = {"9월(12일)": SEP, "비9월(66일)": NON, "최근30": D[-30:], "앞50": D[:50],
     "뒤28": D[50:], "앞39": D[:39], "뒤39": D[39:], "전체78": D}


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    d["mae"] = d.mae_net_pct
    d["win"] = d.net_pct > 0
    if "cx_on" not in d:
        d["cx_on"] = False
    d["cx_on"] = d.cx_on.fillna(False).astype(bool)
    t = pd.to_datetime(d.entry_time, utc=True).dt.tz_convert("Asia/Seoul")
    d["tmin"] = t.dt.hour * 60 + t.dt.minute
    d["ym"] = d.date.astype(str).str[:6]
    return d


DF = {t: prep(RUNS[t]) for t in RUNS}
B = DF["A_BASE"]
DAY = {t: {k: g.pnl.values for k, g in DF[t].groupby("date")} for t in RUNS}


def cd(t, days):
    p = np.concatenate([DAY[t][x] for x in days if x in DAY[t]]) if days else np.array([])
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


def full(t, dates=None):
    g = DF[t] if dates is None else DF[t][DF[t].date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


print("=" * 150); print("8·9. 창별 성과 (복리%)"); print("=" * 150)
rows = []
for t in TAGS:
    r = {"후보": t, "거래": len(DF[t]), "CHOP": int(DF[t].cx_on.sum())}
    for wn, wd in W.items():
        r[wn] = round(full(t, wd), 2)
    rows.append(r)
T = pd.DataFrame(rows); print(T.to_string(index=False))
print("\n[BASE 대비 Δ]")
b0 = T[T.후보 == "A_BASE"].iloc[0]
T2 = T.copy()
for wn in W:
    T2[wn] = (T[wn] - b0[wn]).round(2)
print(T2[T2.후보 != "A_BASE"].to_string(index=False))

print("\n" + "=" * 150); print("CHOP 거래만 따로 — 독립적으로 양의 기대값인가 (Q2)"); print("=" * 150)
rows = []
for t in TAGS:
    d = DF[t]
    c = d[d.cx_on]
    if not len(c):
        rows.append(dict(후보=t, CHOP거래=0)); continue
    p = c.pnl.values
    w, l = p[c.net_pct > 0], p[c.net_pct <= 0]
    rows.append(dict(후보=t, CHOP거래=len(c), 승률=round(100 * c.win.mean(), 1),
                     평균net=round(c.net_pct.mean(), 3), 합=round(p.sum(), 3),
                     PF=round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else np.inf,
                     평균MFE=round(c.mfe.mean(), 3), 평균MAE=round(c.mae.mean(), 3),
                     평균보유분=round(c.hold_minutes.mean(), 1),
                     RED=int((c.direction == "UP_RED").sum()),
                     BLUE=int((c.direction == "DOWN_BLUE").sum()),
                     TP=int((c.exit_reason == "CX_TP").sum()),
                     SL=int((c.exit_reason == "CX_SL").sum()),
                     MAXH=int((c.exit_reason == "CX_MAXHOLD").sum()),
                     VWAP=int((c.exit_reason == "CX_VWAP").sum()),
                     기타=int((~c.exit_reason.isin(["CX_TP", "CX_SL", "CX_MAXHOLD", "CX_VWAP"])).sum()),
                     월9=int((c.date.astype(str) >= "20260901").sum())))
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 150); print("6. CHOP 거래 시간대별 (hard filter 아님, 통계만)"); print("=" * 150)
TE = [0, 600, 690, 780, 840, 1440]
TL = ["09:00~10:00", "10:00~11:30", "11:30~13:00", "13:00~14:00", "14:00~"]
for t in TAGS:
    c = DF[t][DF[t].cx_on]
    if not len(c):
        continue
    c = c.copy(); c["tb"] = pd.cut(c.tmin, TE, labels=TL, right=False)
    g = c.groupby("tb", observed=True).agg(n=("net_pct", "size"), 승률=("win", "mean"),
                                           net=("net_pct", "mean"), MFE=("mfe", "mean"),
                                           합=("pnl", "sum"))
    g["승률"] = (100 * g["승률"]).round(1)
    print("\n[%s]" % t); print(g.round(3).to_string())

print("\n" + "=" * 150); print("7. CHOP 거래 방향별"); print("=" * 150)
for t in TAGS:
    c = DF[t][DF[t].cx_on]
    if not len(c):
        continue
    g = c.groupby("direction").agg(n=("net_pct", "size"), 승률=("win", "mean"),
                                   net=("net_pct", "mean"), MFE=("mfe", "mean"), 합=("pnl", "sum"))
    g["승률"] = (100 * g["승률"]).round(1)
    print("\n[%s]" % t); print(g.round(3).to_string())

print("\n" + "=" * 150); print("Q3. trend regime 에서 불필요하게 거래하지 않는가"); print("=" * 150)
rows = []
for t in TAGS:
    c = DF[t][DF[t].cx_on]
    n_sep = int((c.date.astype(str) >= "20260901").sum()) if len(c) else 0
    rows.append(dict(후보=t, CHOP전체=len(c), CHOP_9월=n_sep, CHOP_비9월=len(c) - n_sep,
                     비9월비율=round(100 * (len(c) - n_sep) / max(1, len(c)), 1)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 150); print("8. 밀어냄 / 억제 로그"); print("=" * 150)
for t in TAGS:
    lg = LOGS.get(t) or []
    if not lg:
        continue
    kinds = pd.Series([x["kind"] for x in lg]).value_counts().to_dict()
    print("\n[%s] %s" % (t, kinds))
    blocked = [x for x in lg if x["kind"] in ("N1_BLOCKED_BY_CX", "N1_SUPPRESSED_M1")]
    if blocked:
        print(pd.DataFrame(blocked).to_string(index=False))

print("\n" + "=" * 150); print("10. 강건성 + runner 손상"); print("=" * 150)
rng = np.random.default_rng(20260924)
BOOT = [list(rng.choice(D, len(D), replace=True)) for _ in range(10000)]
rows = []
for t in TAGS:
    d = DF[t]; p = d.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[d.net_pct > 0], p[d.net_pct <= 0]
    r = {"후보": t, "거래": len(d), "승률": round(100 * d.win.mean(), 1),
         "PF": round(float(w.sum() / -l.sum()), 3),
         "MDD": round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
         "평균MFE": round(d.mfe.mean(), 3), "평균MAE": round(d.mae.mean(), 3),
         "Δ전체": round(full(t) - full("A_BASE"), 2),
         "Δ9월": round(full(t, SEP) - full("A_BASE", SEP), 2)}
    for n in (1, 3, 5):
        cv = float(((1 + d.drop(d.nlargest(n, "pnl").index).sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)
        bv = float(((1 + B.drop(B.nlargest(n, "pnl").index).sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)
        r["Δtop%d제외" % n] = round(cv - bv, 2)
    loo = np.array([cd(t, [x for x in D if x != day]) - cd("A_BASE", [x for x in D if x != day]) for day in D])
    r["LOO최소"] = round(float(loo.min()), 2); r["LOO<=0"] = int((loo <= 0).sum())
    bs = np.array([cd(t, s) - cd("A_BASE", s) for s in BOOT])
    r["bs평균"] = round(float(bs.mean()), 2)
    r["bsCI"] = "[%.1f, %.1f]" % (np.percentile(bs, 2.5), np.percentile(bs, 97.5))
    r["P(>0)%"] = round(100 * float((bs > 0).mean()), 1)
    r["MFE3"] = int((d.mfe >= 3).sum()); r["MFE5"] = int((d.mfe >= 5).sum()); r["MFE8"] = int((d.mfe >= 8).sum())
    r["일자본"] = round(d.groupby("date").w1a.sum().mean(), 3)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 150); print("단일거래 기여율 (>40% 경고 / >60% REJECT 우선)"); print("=" * 150)
key = ["date", "entry_time", "direction"]
rows = []
for t in TAGS:
    if t == "A_BASE":
        continue
    d = DF[t]
    tot = full(t) - full("A_BASE")
    if abs(tot) < 1e-9:
        rows.append(dict(후보=t, Δ전체=0.0, 최대기여="-", 기여율="-")); continue
    best, bestv = None, 0.0
    for _, row in d[d.cx_on].iterrows():
        m = ~((d.date == row.date) & (d.entry_time == row.entry_time))
        cv = float(((1 + d[m].sort_values("exit_time").pnl.values / 100).prod() - 1) * 100)
        contrib = full(t) - cv
        if abs(contrib) > abs(bestv):
            best, bestv = (row.date, row.entry_time, row.net_pct), contrib
    rows.append(dict(후보=t, Δ전체=round(tot, 2), 최대기여=str(best),
                     기여값=round(bestv, 2), 기여율=round(100 * bestv / tot, 1) if tot else 0))
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 150); print("월별"); print("=" * 150)
rows = []
for t in TAGS:
    r = {"후보": t}
    for ym in sorted(set(DF[t].ym)):
        r[ym] = round(full(t, [x for x in D if str(x)[:6] == ym]), 2)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))
