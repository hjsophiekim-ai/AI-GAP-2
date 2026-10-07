"""E1 — 20260909 를 빼고도 P3 가 전 후보를 이기는가. 엔진 재실행 없음.

제외 방식 2가지
  (b) 해당 거래 1건만 제외 — 20260909 09:15 UP_RED (MFE 5.637)
  (c) 20260909 하루 전체 제외 — 같은 날 다른 거래의 재배치 효과까지 제거
두 방식 모두 **모든 후보에서 동일하게** 제외한다.
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 60)
HERE = Path(__file__).resolve().parent
W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
Z2 = pickle.load(open(HERE / "z2.pkl", "rb"))
D2 = pickle.load(open(HERE / "d2.pkl", "rb"))
D = D2["dates"]; SH = D2["shadow"]
SEP = [d for d in D if d >= "20260901"]
RUNS = {"BASE": W1["runs"]["A_BASE"], "B3": D2["runs"]["A_B3"], "B_Y3": D2["runs"]["B_Y3"],
        "P1_fast12": D2["runs"]["P1_fast12"], "P2_fast12_ez2": D2["runs"]["P2_fast12_ez2"],
        "P3_fast6": D2["runs"]["P3_fast6"]}
TAGS = list(RUNS)
KEY909 = ("20260909", "2026-09-09T09:15:00+09:00", "UP_RED")


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["win"] = d.net_pct > 0
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    return d


DF = {t: prep(RUNS[t]) for t in TAGS}


def comp(df, dates=None):
    g = df if dates is None else df[df.date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


def stats(df, dates=None):
    g = (df if dates is None else df[df.date.isin(set(dates))]).sort_values("exit_time")
    if not len(g):
        return dict(n=0, 복리=0.0, PF=0.0, MDD=0.0, WR=0.0)
    p = g.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[g.net_pct > 0], p[g.net_pct <= 0]
    return dict(n=len(g), 복리=round(float((eq[-1] - 1) * 100), 2),
                PF=round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else np.inf,
                MDD=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
                WR=round(100 * float((g.net_pct > 0).mean()), 1))


VIEWS = {
    "(a) 전체": lambda d: d,
    "(b) 20260909 09:15 거래만 제외": lambda d: d[d.k != KEY909],
    "(c) 20260909 하루 전체 제외": lambda d: d[d.date.astype(str) != "20260909"],
}
DAYS = {"(a) 전체": D, "(b) 20260909 09:15 거래만 제외": D,
        "(c) 20260909 하루 전체 제외": [x for x in D if x != "20260909"]}
SEPV = {"(a) 전체": SEP, "(b) 20260909 09:15 거래만 제외": SEP,
        "(c) 20260909 하루 전체 제외": [x for x in SEP if x != "20260909"]}

for vn, f in VIEWS.items():
    print("=" * 150); print(vn); print("=" * 150)
    sub = {t: f(DF[t]) for t in TAGS}
    rows = []
    for t in TAGS:
        s80 = stats(sub[t], DAYS[vn])
        s30 = stats(sub[t], DAYS[vn][-30:])
        ssep = stats(sub[t], SEPV[vn])
        rows.append(dict(후보=t, 거래=s80["n"], 전체80=s80["복리"], 최근30=s30["복리"],
                         월9=ssep["복리"], PF=s80["PF"], MDD=s80["MDD"], WR=s80["WR"]))
    T = pd.DataFrame(rows)
    b3 = T[T.후보 == "B_Y3"].iloc[0]
    ba = T[T.후보 == "BASE"].iloc[0]
    T["Δ_B_Y3"] = (T.전체80 - b3.전체80).round(2)
    T["Δ9월_B_Y3"] = (T.월9 - b3.월9).round(2)
    T["Δ30_B_Y3"] = (T.최근30 - b3.최근30).round(2)
    T["Δ_BASE"] = (T.전체80 - ba.전체80).round(2)
    print(T.to_string(index=False))
    p3 = T[T.후보 == "P3_fast6"].iloc[0]
    others = T[~T.후보.isin(["P3_fast6"])]
    print("\n  P3 가 전 후보를 이기는가:")
    for _, o in others.iterrows():
        print("   vs %-14s 80일 %+8.2f | 최근30 %+7.2f | 9월 %+7.2f | PF %+.3f  -> %s"
              % (o.후보, p3.전체80 - o.전체80, p3.최근30 - o.최근30, p3.월9 - o.월9,
                 p3.PF - o.PF,
                 "승" if (p3.전체80 > o.전체80 and p3.최근30 > o.최근30 and p3.월9 > o.월9) else "패/혼재"))
    print()

# ── (c) 기준 LOO / bootstrap ────────────────────────────────────────────────
print("=" * 150); print("(c) 20260909 제외 기준 강건성 (B_Y3 대비)"); print("=" * 150)
Dx = [x for x in D if x != "20260909"]
DAY = {t: {k: g.pnl.values for k, g in DF[t][DF[t].date.astype(str) != "20260909"].groupby("date")}
       for t in TAGS}


def cd(t, days):
    p = np.concatenate([DAY[t][x] for x in days if x in DAY[t]]) if days else np.array([])
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


rng = np.random.default_rng(20260925)
BOOT = [list(rng.choice(Dx, len(Dx), replace=True)) for _ in range(10000)]
rows = []
for t in TAGS:
    if t == "B_Y3":
        continue
    loo = np.array([cd(t, [x for x in Dx if x != dd]) - cd("B_Y3", [x for x in Dx if x != dd])
                    for dd in Dx])
    bs = np.array([cd(t, s) - cd("B_Y3", s) for s in BOOT])
    rows.append(dict(후보=t, Δ80일=round(cd(t, Dx) - cd("B_Y3", Dx), 2),
                     LOO최소=round(float(loo.min()), 2), LOO중앙=round(float(np.median(loo)), 2),
                     LOO음수일=int((loo <= 0).sum()),
                     bs평균=round(float(bs.mean()), 2),
                     bs5=round(float(np.percentile(bs, 5)), 2),
                     P_gt0=round(100 * float((bs > 0).mean()), 1)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n" + "=" * 150); print("(c) 기준 Top N 제외 (B_Y3 대비)"); print("=" * 150)
rows = []
for t in TAGS:
    if t == "B_Y3":
        continue
    d = DF[t][DF[t].date.astype(str) != "20260909"]
    bb0 = DF["B_Y3"][DF["B_Y3"].date.astype(str) != "20260909"]
    tot = comp(d) - comp(bb0)
    r = {"후보": t, "Δ전체": round(tot, 2)}
    for n in (1, 3, 5, 10):
        r["Top%d" % n] = round(comp(d.drop(d.nlargest(n, "pnl").index))
                               - comp(bb0.drop(bb0.nlargest(n, "pnl").index)), 2)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))
