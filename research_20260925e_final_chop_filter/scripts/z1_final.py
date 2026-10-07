"""Z1 — 최종 실전형 CHOP 필터: PART A~F. 엔진 재실행 없음(기존 산출물 재집계)."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 70)
pd.set_option("display.max_rows", 500)
HERE = Path(__file__).resolve().parent
W1 = pickle.load(open(HERE / "w1.pkl", "rb"))
Y = pickle.load(open(HERE / "y1.pkl", "rb"))
D = Y["dates"]; SH = Y["shadow"]
SEP = [d for d in D if d >= "20260901"]
import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
import axlib as A
ce.CACHE_DIR = Path(r"G:\다른 컴퓨터\내 노트북 (2)\Desktop\AI-GAP 2\data\cache")
H._CTX_CACHE = HERE / "_ctx81.pkl"
ctx = H.build_ctx(81)
bars = ctx.hynix_bars_3m.reset_index(drop=True)
BDATE = pd.to_datetime(bars["datetime"]).dt.strftime("%Y%m%d").values
BREC = (pd.to_datetime(bars["datetime"]) + pd.Timedelta(minutes=3))

RUNS = {"BASE": W1["runs"]["A_BASE"], "B3": Y["runs"]["Y0_B3"], "B3_Y3": Y["runs"]["Y3_gap_etf"]}


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    d["win"] = d.net_pct > 0
    if "gx_promoted" not in d:
        d["gx_promoted"] = False
    d["gx_promoted"] = d.gx_promoted.fillna(False).astype(bool)
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    d["ym"] = d.date.astype(str).str[:6]
    d["ent"] = pd.to_datetime(d.entry_time, utc=True)
    d["ext"] = pd.to_datetime(d.exit_time, utc=True)
    return d


DF = {t: prep(RUNS[t]) for t in RUNS}
B, T3, TY = DF["BASE"], DF["B3"], DF["B3_Y3"]


def comp(df, dates=None):
    g = df if dates is None else df[df.date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


# ═══ PART A ═══════════════════════════════════════════════════════════════
print("=" * 155); print("PART A. CHOP episode (기존 shadow state 재집계, 새 백테스트 없음)"); print("=" * 155)
bb = B.sort_values("ent").reset_index(drop=True)
h50 = bb.h50_held.astype(float).values
tp1 = bb.tp1_hit.astype(float).values
on = bb.chop.values
eps = []
i = 0
while i < len(bb):
    if not on[i]:
        i += 1; continue
    j = i
    while j + 1 < len(bb) and on[j + 1]:
        j += 1
    seg = bb.iloc[i:j + 1]
    days = sorted(set(seg.date.astype(str)))
    lo, hi = seg.ent.iloc[0], seg.ext.iloc[-1]
    def pl(df):
        m = (df.ent >= lo) & (df.ent <= seg.ent.iloc[-1])
        return round(float(df[m].pnl.sum()), 3)
    hr = float(np.mean([h50[max(0, i - 10):i].mean() if i >= 10 else np.nan]))
    eps.append(dict(ep=len(eps) + 1, 시작=str(lo.tz_convert("Asia/Seoul"))[:16],
                    종료=str(seg.ent.iloc[-1].tz_convert("Asia/Seoul"))[:16],
                    영업일수=len(days), 거래=len(seg), 월=seg.ym.iloc[0],
                    H50율=round(float(seg.h50_held.astype(float).mean()), 3),
                    TP1율=round(float(seg.tp1_hit.astype(float).mean()), 3),
                    평균MFE=round(float(seg.mfe.mean()), 3),
                    BASE=pl(B), B3=pl(T3), B3_Y3=pl(TY),
                    유형=("C 지속형" if len(days) >= 2 else
                        ("B 단기형" if len(seg) >= 3 else "A 단발형"))))
    i = j + 1
E = pd.DataFrame(eps)
print(E.to_string(index=False))
print("\n[월별 episode 수]")
print(E.groupby(["월", "유형"]).size().unstack(fill_value=0).to_string())
print("\n[질문 답]")
print("  · 월별 독립 episode: %s" % E.groupby("월").size().to_dict())
c_eps = E[E.유형 == "C 지속형"]
print("  · 지속형(2영업일 이상) episode: %d개 %s"
      % (len(c_eps), list(zip(c_eps.월, c_eps.영업일수))))
print("  · 9월 외 지속형: %d개" % int((c_eps.월 != "202609").sum()))
print("  · 최장 episode: %d영업일 (%s, %s~%s)"
      % (E.영업일수.max(), E.loc[E.영업일수.idxmax(), "월"],
         E.loc[E.영업일수.idxmax(), "시작"], E.loc[E.영업일수.idxmax(), "종료"]))

# ═══ PART B ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 155); print("PART B. P0(SHADOW-BASE) vs P1(실행거래 기준) detector"); print("=" * 155)


def detector_bars(df):
    """그 전략의 **자기 실행거래** 최근10건으로 봉단위 ON/OFF 산출(지연계산)."""
    d = df.sort_values("ext").reset_index(drop=True)
    extn = d.ext.dt.tz_localize(None).values
    hv = d.h50_held.astype(float).values
    qv = d.tp1_hit.astype(float).values
    rec = BREC.dt.tz_convert("UTC").dt.tz_localize(None) if BREC.dt.tz is not None else BREC
    out = np.zeros(len(bars), bool)
    for i in range(len(bars)):
        n = int(np.searchsorted(extn, np.datetime64(rec.iloc[i]), side="left"))
        if n >= 10:
            out[i] = (hv[n - 10:n].mean() >= 0.40) and (qv[n - 10:n].mean() <= 0.20)
    return out


inD = np.array([d in set(D) for d in BDATE])
P0 = SH
rows = []
for tag in ("BASE", "B3", "B3_Y3"):
    P1 = detector_bars(DF[tag])
    agree = float((P0[inD] == P1[inD]).mean())
    sepm = inD & np.array([d >= "20260901" for d in BDATE])
    nonm = inD & np.array([d < "20260901" for d in BDATE])
    # 하루 toggle
    tg = []
    for day in D:
        m = BDATE == day
        s = P1[m].astype(int)
        tg.append(int(np.sum(np.abs(np.diff(s)))) if len(s) > 1 else 0)
    rows.append(dict(기준=("P0(shadow)" if tag == "BASE" else "P1 on " + tag),
                     P0일치율=round(100 * agree, 1),
                     ON봉_P1=int(P1[inD].sum()), ON봉_P0=int(P0[inD].sum()),
                     P1_9월ON=round(100 * P1[sepm].mean(), 1),
                     P1_비9월ON=round(100 * P1[nonm].mean(), 1),
                     toggle2이상일=int(sum(1 for x in tg if x >= 2)),
                     toggle최대=int(max(tg))))
print(pd.DataFrame(rows).to_string(index=False))
print("  * P1 on BASE 는 정의상 P0 와 같아야 한다(검산).")
P1y = detector_bars(TY)
d0 = sorted({d for d, o in zip(BDATE, P0) if o and d in set(D)})
d1 = sorted({d for d, o in zip(BDATE, P1y) if o and d in set(D)})
print("\n  episode 경계(ON 이 있는 날) P0 %d일 / P1(B3_Y3) %d일" % (len(d0), len(d1)))
print("   P0 전용: %s" % [x for x in d0 if x not in d1])
print("   P1 전용: %s" % [x for x in d1 if x not in d0])

# ═══ PART D ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 155); print("PART D. BASE / B3 / B3+Y3 비교"); print("=" * 155)
W = {"전체80": D, "최근30": D[-30:], "9월": SEP, "비9월": [d for d in D if d < "20260901"]}
rows = []
for t in ("BASE", "B3", "B3_Y3"):
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
    rows.append(r)
P = pd.DataFrame(rows)
print(P.to_string(index=False))
print("\n[BASE 대비 Δ]")
b0 = P[P.후보 == "BASE"].iloc[0]
P2 = P.copy()
for wn in W:
    P2[wn] = (P[wn] - b0[wn]).round(2)
print(P2[P2.후보 != "BASE"].to_string(index=False))
print("\n[월별]")
rows = []
for t in ("BASE", "B3", "B3_Y3"):
    r = {"후보": t}
    for ym in sorted(set(B.ym)):
        r[ym] = round(comp(DF[t], [x for x in D if str(x)[:6] == ym]), 2)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

print("\n[강건성 — BASE 기준]")
DAY = {t: {k: g.pnl.values for k, g in DF[t].groupby("date")} for t in DF}


def cd(t, days):
    p = np.concatenate([DAY[t][x] for x in days if x in DAY[t]]) if days else np.array([])
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


rng = np.random.default_rng(20260925)
BOOT = [list(rng.choice(D, len(D), replace=True)) for _ in range(10000)]
rows = []
for t in ("B3", "B3_Y3"):
    tot = comp(DF[t]) - comp(B)
    r = {"후보": t, "Δ전체": round(tot, 2)}
    for n in (5, 10):
        bbx = B.drop(B.nlargest(n, "pnl").index)
        ttx = DF[t].drop(DF[t].nlargest(n, "pnl").index)
        dv = comp(ttx) - comp(bbx)
        r["Top%d제외" % n] = round(dv, 2)
        r["Top%d유지율" % n] = round(100 * dv / tot, 1)
    loo = np.array([cd(t, [x for x in D if x != dd]) - cd("BASE", [x for x in D if x != dd]) for dd in D])
    bs = np.array([cd(t, s) - cd("BASE", s) for s in BOOT])
    r["LOO최소"] = round(float(loo.min()), 2); r["LOO음수일"] = int((loo <= 0).sum())
    r["bs평균"] = round(float(bs.mean()), 2); r["P_gt0"] = round(100 * float((bs > 0).mean()), 1)
    rows.append(r)
print(pd.DataFrame(rows).to_string(index=False))

# ═══ PART E ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 155); print("PART E. 러너 / 손실 균형"); print("=" * 155)
bchop = B[B.chop]
j3 = T3.merge(B[["k", "net_pct", "peak_net_pct"]], on="k", how="inner", suffixes=("", "_base"))
j3 = j3[j3.chop]
jy = TY.merge(B[["k", "net_pct", "peak_net_pct"]], on="k", how="inner", suffixes=("", "_base"))
jy = jy[jy.chop]
j3["d"] = j3.net_pct - j3.net_pct_base
jy["d"] = jy.net_pct - jy.net_pct_base
run3b = j3[j3.peak_net_pct_base >= 3]
non3b = j3[j3.peak_net_pct_base < 3]
print("1. B3 가 줄인 손실 총액 (러너 제외 CHOP 거래 uplift 합) : %+.3f" % non3b.d.sum())
print("2. B3 가 잘라낸 runner 손실 총액 (MFE>=3%% %d건)        : %+.3f"
      % (len(run3b), run3b.d.sum()))
pr = TY[TY.gx_promoted]
prj = pr.merge(T3[["k", "net_pct"]], on="k", how="left", suffixes=("", "_b3"))
prj = prj.merge(B[["k", "peak_net_pct"]].rename(columns={"peak_net_pct": "mfe_base"}),
                on="k", how="left")
prj["d"] = prj.net_pct - prj.net_pct_b3
rec = prj[prj.mfe_base >= 3]
fal = prj[prj.mfe_base < 3]
print("3. Y3 가 복구한 runner 총액 (%d건)                      : %+.3f" % (len(rec), rec.d.sum()))
print("4. Y3 false promotion 으로 늘어난 손실 (%d건)           : %+.3f" % (len(fal), fal.d.sum()))
print("5. 최종 순효과 (3+4)                                    : %+.3f" % prj.d.sum())
print("   참고: BASE→B3 CHOP 순효과 %+.3f · BASE→B3+Y3 CHOP 순효과 %+.3f"
      % (j3.d.sum(), jy.d.sum()))
print("\n[MFE 구간별 runner 보존]")
rows = []
for th in (3, 5, 8):
    pop = bchop[bchop.mfe >= th]
    keep3 = keepy = 0
    for _, r in pop.iterrows():
        a, b2 = T3[T3.k == r.k], TY[TY.k == r.k]
        if len(a) and abs(float(a.iloc[0].net_pct) - float(r.net_pct)) < 1e-9:
            keep3 += 1
        if len(b2) and abs(float(b2.iloc[0].net_pct) - float(r.net_pct)) < 1e-9:
            keepy += 1
    rows.append(dict(구간="MFE>=%d%%" % th, BASE_runner=len(pop),
                     B3_보존=keep3, Y3_보존=keepy,
                     Y3_보존율=round(100 * keepy / len(pop), 1) if len(pop) else 0.0))
print(pd.DataFrame(rows).to_string(index=False))

# ═══ PART F ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 155); print("PART F. 9/22 앵커"); print("=" * 155)
rows = []
for t in ("BASE", "B3", "B3_Y3"):
    d = DF[t]
    s = d[(d.date.astype(str) == "20260922") & (d.entry_time.astype(str).str.contains("12:15"))]
    if not len(s):
        rows.append(dict(후보=t, 존재="없음")); continue
    r = s.iloc[0]
    rows.append(dict(후보=t, regime=("CHOP" if r.chop else "TREND"),
                     진입=str(r.entry_time)[11:16],
                     승격=bool(r.gx_promoted),
                     청산=str(r.exit_time)[11:16], 사유=r.exit_reason,
                     net=round(float(r.net_pct), 3), MFE=round(float(r.mfe), 3),
                     보유분=round(float(r.hold_minutes), 1)))
print(pd.DataFrame(rows).to_string(index=False))
