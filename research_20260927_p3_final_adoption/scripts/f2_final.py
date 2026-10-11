"""F2 — P3 최종 채택 검증 (항목 2~13). 엔진 재실행 없음."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 70)
pd.set_option("display.max_rows", 400)
HERE = Path(__file__).resolve().parent
W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
D2 = pickle.load(open(HERE / "d2.pkl", "rb"))
D = D2["dates"]; SH = D2["shadow"]
SEP = [d for d in D if d >= "20260901"]
import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
import axlib as A
ce.CACHE_DIR = Path(r"G:\다른 컴퓨터\내 노트북 (2)\Desktop\AI-GAP 2\data\cache")
H._CTX_CACHE = HERE / "_ctx81.pkl"
ctx = H.build_ctx(81)
bars = ctx.hynix_bars_3m.reset_index(drop=True)
BDATE = pd.to_datetime(bars["datetime"]).dt.strftime("%Y%m%d").values

RUNS = {"A_BASE": W1["runs"]["A_BASE"], "B_B3": D2["runs"]["A_B3"],
        "C_Y3": D2["runs"]["B_Y3"], "D_P3": D2["runs"]["P3_fast6"]}
TAGS = list(RUNS)


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    d["mae"] = d.mae_net_pct
    d["win"] = d.net_pct > 0
    for c in ("gx_promoted", "gx_rescue"):
        if c not in d:
            d[c] = False
        d[c] = d[c].fillna(False).astype(bool)
    for c in ("gx_rescue_at", "gx_prom_at"):
        if c not in d:
            d[c] = None
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    d["ym"] = d.date.astype(str).str[:6]
    return d


DF = {t: prep(RUNS[t]) for t in TAGS}
B, T3, Y3, P3 = DF["A_BASE"], DF["B_B3"], DF["C_Y3"], DF["D_P3"]
DAY = {t: {k: g.pnl.values for k, g in DF[t].groupby("date")} for t in TAGS}


def cd(t, days):
    p = np.concatenate([DAY[t][x] for x in days if x in DAY[t]]) if days else np.array([])
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


def comp(df, dates=None):
    g = df if dates is None else df[df.date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


def stats(df, dates=None):
    g = (df if dates is None else df[df.date.isin(set(dates))]).sort_values("exit_time")
    if not len(g):
        return dict(n=0, 복리=0.0, PF=0.0, MDD=0.0, WR=0.0, 평균net=0.0)
    p = g.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[g.net_pct > 0], p[g.net_pct <= 0]
    return dict(n=len(g), 복리=round(float((eq[-1] - 1) * 100), 2),
                PF=round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else np.inf,
                MDD=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
                WR=round(100 * float((g.net_pct > 0).mean()), 1),
                평균net=round(float(g.net_pct.mean()), 3))


# ── 2. REGIME episode ─────────────────────────────────────────────────────
print("=" * 155); print("2. REGIME (SHADOW-BASE, 수정 없음)"); print("=" * 155)
bb = B.sort_values("entry_time").reset_index(drop=True)
on = bb.chop.values
eps, i = [], 0
while i < len(bb):
    if not on[i]:
        i += 1; continue
    j = i
    while j + 1 < len(bb) and on[j + 1]:
        j += 1
    seg = bb.iloc[i:j + 1]
    eps.append(dict(ep=len(eps) + 1, 시작=str(seg.entry_time.iloc[0])[:16],
                    종료=str(seg.entry_time.iloc[-1])[:16],
                    영업일=seg.date.nunique(), 거래=len(seg), 월=seg.ym.iloc[0],
                    days=sorted(set(seg.date.astype(str)))))
    i = j + 1
E = pd.DataFrame(eps)
print(E[["ep", "시작", "종료", "영업일", "거래", "월"]].to_string(index=False))
on_days = sorted({d for d, o in zip(BDATE, SH) if o and d in set(D)})
print("  ON 거래 %d / ON 영업일 %d / 전체 %d일" % (int(bb.chop.sum()), len(on_days), len(D)))
t2 = pd.DataFrame({"d": BDATE, "on": SH}).query("d in @D").groupby("d").on.mean()
print("  월별 ON 비율: %s" % {m: round(100 * t2[[x for x in t2.index if x[:6] == m]].mean(), 1)
                          for m in sorted({x[:6] for x in t2.index})})

# ── 3. 성과 ───────────────────────────────────────────────────────────────
print("\n" + "=" * 155); print("3. 성과 비교"); print("=" * 155)
W = {"전체80": D, "최근30": D[-30:], "9월": SEP, "비9월": [d for d in D if d < "20260901"]}
rows = []
for t in TAGS:
    s = stats(DF[t])
    r = {"전략": t, "거래": s["n"], "PF": s["PF"], "MDD": s["MDD"], "WR": s["WR"],
         "평균net": s["평균net"]}
    for wn, wd in W.items():
        r[wn] = round(comp(DF[t], wd), 2)
    for ym in sorted(set(B.ym)):
        r[ym] = round(comp(DF[t], [x for x in D if str(x)[:6] == ym]), 2)
    rows.append(r)
PR = pd.DataFrame(rows)
print(PR.to_string(index=False))
print("\n[핵심 비교]")
for a, b in (("D_P3", "A_BASE"), ("D_P3", "B_B3"), ("D_P3", "C_Y3")):
    print("  %s vs %-7s : 전체80 %+8.2f | 최근30 %+7.2f | 9월 %+7.2f | PF %+.3f | MDD %+.2f"
          % (a, b, comp(DF[a]) - comp(DF[b]),
             comp(DF[a], D[-30:]) - comp(DF[b], D[-30:]),
             comp(DF[a], SEP) - comp(DF[b], SEP),
             stats(DF[a])["PF"] - stats(DF[b])["PF"],
             stats(DF[a])["MDD"] - stats(DF[b])["MDD"]))

# ── 4. TREND 보존 ─────────────────────────────────────────────────────────
print("\n" + "=" * 155); print("4. TREND 보존 (ON 봉이 하나도 없는 날)"); print("=" * 155)
pure_off = [d for d in D if d not in set(on_days)]
bo = B[B.date.astype(str).isin(set(pure_off))]
rows = []
for t in TAGS:
    if t == "A_BASE":
        continue
    o = DF[t][DF[t].date.astype(str).isin(set(pure_off))]
    j = o.merge(bo[["k", "net_pct", "w1a", "exit_time", "exit_reason"]], on="k", how="inner",
                suffixes=("", "_b"))
    dif = int(((j.net_pct - j.net_pct_b).abs() > 1e-9).sum()
              + (j.w1a - j.w1a_b).abs().gt(1e-9).sum()
              + (j.exit_time != j.exit_time_b).sum()
              + (j.exit_reason != j.exit_reason_b).sum())
    rows.append(dict(전략=t, 완전TREND일=len(pure_off), BASE거래=len(bo), 후보거래=len(o),
                     키집합동일=set(o.k) == set(bo.k), 차이=dif,
                     판정="diff 0" if (set(o.k) == set(bo.k) and dif == 0) else "위반"))
print(pd.DataFrame(rows).to_string(index=False))
print("\n[TREND 강한 달(7월·5월) 복리]")
for ym in ("202605", "202607"):
    dd = [x for x in D if str(x)[:6] == ym]
    print("  %s : %s" % (ym, {t: round(comp(DF[t], dd), 2) for t in TAGS}))

# ── 5. CHOP 내부 ──────────────────────────────────────────────────────────
print("\n" + "=" * 155); print("5. CHOP ON 거래만"); print("=" * 155)
rows = []
for t in TAGS:
    d = DF[t][DF[t].chop]
    p = d.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[d.net_pct > 0], p[d.net_pct <= 0]
    er = d.exit_reason.value_counts().to_dict()
    rows.append(dict(전략=t, 거래=len(d), 승률=round(100 * d.win.mean(), 1),
                     평균net=round(d.net_pct.mean(), 3), 합=round(p.sum(), 3),
                     PF=round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else np.inf,
                     MDD기여=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
                     평균MFE=round(d.mfe.mean(), 3), 평균MAE=round(d.mae.mean(), 3),
                     TP=er.get("GX_TP", 0), SL=er.get("GX_SL", 0), MAXH=er.get("GX_MAXHOLD", 0),
                     기타=len(d) - er.get("GX_TP", 0) - er.get("GX_SL", 0) - er.get("GX_MAXHOLD", 0)))
print(pd.DataFrame(rows).to_string(index=False))

# ── 6. runner 보존 ────────────────────────────────────────────────────────
print("\n" + "=" * 155); print("6. runner 보존 (CHOP BASE runner)"); print("=" * 155)
bchop = B[B.chop]
rows = []
for th in (3, 5, 8):
    pop = bchop[bchop.mfe >= th]
    r = {"구간": "MFE>=%d%%" % th, "BASE_n": len(pop),
         "BASE_익": round(float(pop.net_pct.sum()), 3)}
    for t in TAGS:
        if t == "A_BASE":
            continue
        keep = 0; prof = 0.0
        for _, x in pop.iterrows():
            m = DF[t][DF[t].k == x.k]
            if len(m):
                prof += float(m.iloc[0].net_pct)
                if float(m.iloc[0].net_pct) >= float(x.net_pct) - 1e-9:
                    keep += 1
        r[t + "_보존n"] = keep
        r[t + "_익합"] = round(prof, 3)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n[러너 5건 전량 추적]")
rows = []
for _, x in bchop[bchop.mfe >= 3].iterrows():
    m3 = T3[T3.k == x.k]
    cut = m3.iloc[0].exit_reason if len(m3) else "-"
    row = dict(date=x.date, dir=x.direction, entry=str(x.entry_time)[11:16],
               MFE=round(float(x.mfe), 3), BASE=round(float(x.net_pct), 3),
               B3=round(float(m3.iloc[0].net_pct), 3) if len(m3) else None,
               절단=("TP" if cut == "GX_TP" else ("maxhold" if cut == "GX_MAXHOLD" else cut)))
    for t in ("C_Y3", "D_P3"):
        m = DF[t][DF[t].k == x.k]
        if len(m):
            e = m.iloc[0]
            mk = "R" if e.gx_rescue else ("P" if e.gx_promoted else "")
            row[t] = "%.3f%s" % (float(e.net_pct), mk)
            row[t + "_시점"] = (str(e.gx_rescue_at)[11:19] if e.gx_rescue
                              else (str(e.gx_prom_at)[11:19] if e.gx_promoted else "-"))
            row[t + "_청산"] = e.exit_reason
    rows.append(row)
print(pd.DataFrame(rows).to_string(index=False))

print("\n[false promotion 비용 vs rescue 이익 (B3 대비)]")
rows = []
for t in ("C_Y3", "D_P3"):
    d = DF[t]
    act = d[(d.gx_promoted) | (d.gx_rescue)]
    j = act.merge(T3[["k", "net_pct"]], on="k", how="left", suffixes=("", "_b3"))
    j = j.merge(B[["k", "peak_net_pct"]].rename(columns={"peak_net_pct": "mfe_b"}),
                on="k", how="left")
    j["dd"] = j.net_pct - j.net_pct_b3
    run_ = j[j.mfe_b >= 3]
    fal = j[j.mfe_b < 1.5]
    mid = j[(j.mfe_b >= 1.5) & (j.mfe_b < 3)]
    rows.append(dict(전략=t, 개입=len(j),
                     러너rescue_n=len(run_), 러너rescue_익=round(float(run_.dd.sum()), 3),
                     false_n=len(fal), false_비용=round(float(fal.dd.sum()), 3),
                     중간_n=len(mid), 중간_손익=round(float(mid.dd.sum()), 3),
                     순=round(float(j.dd.sum()), 3)))
print(pd.DataFrame(rows).to_string(index=False))

# ── 7. 0909 의존성 ────────────────────────────────────────────────────────
print("\n" + "=" * 155); print("7. 20260909 의존성"); print("=" * 155)
K909 = ("20260909", "2026-09-09T09:15:00+09:00", "UP_RED")
rows = []
for vn, filt, dd, sp in (
        ("A. 전체", lambda d: d, D, SEP),
        ("B. 거래 1건 제외", lambda d: d[d.k != K909], D, SEP),
        ("C. 하루 전체 제외", lambda d: d[d.date.astype(str) != "20260909"],
         [x for x in D if x != "20260909"], [x for x in SEP if x != "20260909"])):
    p3, y3 = filt(P3), filt(Y3)
    rows.append(dict(방식=vn,
                     P3_80=round(comp(p3, dd), 2), Y3_80=round(comp(y3, dd), 2),
                     Δ80=round(comp(p3, dd) - comp(y3, dd), 2),
                     Δ30=round(comp(p3, dd[-30:]) - comp(y3, dd[-30:]), 2),
                     Δ9월=round(comp(p3, sp) - comp(y3, sp), 2),
                     P3_PF=stats(p3, dd)["PF"], Y3_PF=stats(y3, dd)["PF"],
                     P3_MDD=stats(p3, dd)["MDD"], Y3_MDD=stats(y3, dd)["MDD"]))
print(pd.DataFrame(rows).to_string(index=False))

# ── 8. day-level LOO ──────────────────────────────────────────────────────
print("\n" + "=" * 155); print("8. day-level LOO (P3 − Y3)"); print("=" * 155)
loo = {}
for dd in D:
    rest = [x for x in D if x != dd]
    loo[dd] = cd("D_P3", rest) - cd("C_Y3", rest)
L = pd.Series(loo).sort_values()
print("  최소 %+.2f / 중앙 %+.2f / 최대 %+.2f / 음수일 %d/%d"
      % (L.min(), L.median(), L.max(), int((L <= 0).sum()), len(L)))
print("  최악 5일: %s" % L.head(5).round(2).to_dict())
print("  최고 5일: %s" % L.tail(5).round(2).to_dict())

# ── 9. Top-N ──────────────────────────────────────────────────────────────
print("\n" + "=" * 155); print("9. Top-N 제외"); print("=" * 155)
rows = []
for n in (1, 3, 5, 10, 15):
    r = {"제외": "Top%d" % n}
    for ref in ("C_Y3", "A_BASE"):
        bb2 = DF[ref].drop(DF[ref].nlargest(n, "pnl").index)
        tt = P3.drop(P3.nlargest(n, "pnl").index)
        r["거래단위_P3-%s" % ref] = round(comp(tt) - comp(bb2), 2)
    # day-level paired removal: 상위 pnl 거래가 속한 날을 양쪽에서 함께 제거
    for ref in ("C_Y3", "A_BASE"):
        days_rm = set(P3.nlargest(n, "pnl").date.astype(str)) | set(
            DF[ref].nlargest(n, "pnl").date.astype(str))
        keep = [x for x in D if x not in days_rm]
        r["일단위_P3-%s" % ref] = round(cd("D_P3", keep) - cd(ref, keep), 2)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

# ── 10. day-level bootstrap 20,000 ────────────────────────────────────────
print("\n" + "=" * 155); print("10. day-level bootstrap 20,000"); print("=" * 155)
rng = np.random.default_rng(20260927)
BOOT = [list(rng.choice(D, len(D), replace=True)) for _ in range(20000)]
rows = []
for ref in ("C_Y3", "A_BASE"):
    bs = np.array([cd("D_P3", s) - cd(ref, s) for s in BOOT])
    rows.append(dict(비교="P3 - " + ref, 평균=round(float(bs.mean()), 2),
                     중앙=round(float(np.median(bs)), 2),
                     p2_5=round(float(np.percentile(bs, 2.5)), 2),
                     p5=round(float(np.percentile(bs, 5)), 2),
                     p95=round(float(np.percentile(bs, 95)), 2),
                     p97_5=round(float(np.percentile(bs, 97.5)), 2),
                     P_gt0=round(100 * float((bs > 0).mean()), 1)))
print(pd.DataFrame(rows).to_string(index=False))

# ── 11. episode-level ─────────────────────────────────────────────────────
print("\n" + "=" * 155); print("11. episode-level"); print("=" * 155)
rows = []
for _, e in E.iterrows():
    dset = e["days"]
    rows.append(dict(ep=e.ep, 월=e.월, 영업일=e.영업일, 거래=e.거래,
                     **{t: round(cd(t, dset), 3) for t in TAGS}))
print(pd.DataFrame(rows).to_string(index=False))
print("\n[episode 제거 후 P3 − Y3]")
rows = []
for _, e in E.iterrows():
    keep = [x for x in D if x not in set(e["days"])]
    rows.append(dict(제거=("ep%d (%s)" % (e.ep, e.월)),
                     남은일수=len(keep),
                     P3=round(cd("D_P3", keep), 2), Y3=round(cd("C_Y3", keep), 2),
                     Δ=round(cd("D_P3", keep) - cd("C_Y3", keep), 2),
                     남은CHOP거래=int(B[B.chop & B.date.astype(str).isin(set(keep))].shape[0])))
print(pd.DataFrame(rows).to_string(index=False))

# ── 12. 재배치 분리 ───────────────────────────────────────────────────────
print("\n" + "=" * 155); print("12. P3 − Y3 효과 4분해"); print("=" * 155)
common = set(P3.k) & set(Y3.k)
j = P3[P3.k.isin(common)].merge(Y3[["k", "net_pct"]], on="k", how="inner", suffixes=("", "_y3"))
j = j.merge(B[["k", "peak_net_pct"]].rename(columns={"peak_net_pct": "mfe_b"}), on="k", how="left")
j["dd"] = j.net_pct - j.net_pct_y3
resc = j[j.gx_rescue]
runr = resc[resc.mfe_b >= 3]
falr = resc[resc.mfe_b < 3]
othr = j[(~j.gx_rescue) & (j.dd.abs() > 1e-9)]
added = P3[~P3.k.isin(set(Y3.k))]
lost = Y3[~Y3.k.isin(set(P3.k))]
print("A. 기존 거래 exit 개선 (rescue 아닌 공통거래 변화) : %+.3f (%d건)" % (othr.dd.sum(), len(othr)))
print("B. runner rescue 직접효과 (MFE>=3 rescue)          : %+.3f (%d건)" % (runr.dd.sum(), len(runr)))
print("D. false promotion 비용 (MFE<3 rescue)             : %+.3f (%d건)" % (falr.dd.sum(), len(falr)))
print("   A+B+D (직접효과 합)                              : %+.3f" % (othr.dd.sum() + resc.dd.sum()))
print("C. 재배치 — P3 신규 %d건 %+.3f / Y3 소멸 %d건 %+.3f  순 %+.3f"
      % (len(added), added.pnl.sum(), len(lost), -lost.pnl.sum(),
         added.pnl.sum() - lost.pnl.sum()))
print("   직접(A+B+D) %+.3f  vs  재배치(C) %+.3f" %
      (othr.dd.sum() + resc.dd.sum(), added.pnl.sum() - lost.pnl.sum()))

# ── 13. path-shape 규칙 검증 ──────────────────────────────────────────────
print("\n" + "=" * 155); print("13. +1% 도달 6분 이내 vs 초과 (B3 기준 TP 도달 거래)"); print("=" * 155)
tp = T3[(T3.chop) & (T3.exit_reason == "GX_TP")].copy()
tp["spd"] = [(pd.Timestamp(r.exit_time) - pd.Timestamp(r.entry_time)).total_seconds() / 60.0
             for _, r in tp.iterrows()]
tp["mfe_b"] = [mfe if (mfe := B[B.k == r.k].peak_net_pct.iloc[0] if len(B[B.k == r.k]) else np.nan)
               else np.nan for _, r in tp.iterrows()]
tp["p3net"] = [float(P3[P3.k == r.k].net_pct.iloc[0]) if len(P3[P3.k == r.k]) else np.nan
               for _, r in tp.iterrows()]
rows = []
for lab, g in (("6분 이내", tp[tp.spd <= 6]), ("6분 초과", tp[tp.spd > 6])):
    v = g.mfe_b.dropna()
    rows.append(dict(그룹=lab, n=len(g), 라벨있음=len(v),
                     러너비율=round(100 * float((v >= 3).mean()), 1) if len(v) else 0.0,
                     BASE평균MFE=round(float(v.mean()), 3) if len(v) else None,
                     B3평균net=round(float(g.net_pct.mean()), 3),
                     B3평균MAE=round(float(g.mae.mean()), 3),
                     P3평균net=round(float(g.p3net.mean()), 3) if g.p3net.notna().any() else None))
print(pd.DataFrame(rows).to_string(index=False))
