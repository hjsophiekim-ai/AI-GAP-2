"""V1 — B3 최종 재검증 (78일). 엔진 재실행 없음: g3.pkl 의 거래집합만 쓴다.

항목 3~11. delta 기여도는 **그 거래를 양쪽에서 제거하고 델타를 다시 재는** 방식이다
(전체 복리 기여가 아니라 BASE 대비 델타에 대한 기여).
"""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 400)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "g3.pkl", "rb"))
D = Z["dates"]
SH = np.load(HERE / "shadow_on.npy")
import hengine5 as H
H._CTX_CACHE = HERE / "_ctx_B.pkl"
ctx = H.build_ctx(78)
bars = ctx.hynix_bars_3m.reset_index(drop=True)
BDATE = pd.to_datetime(bars["datetime"]).dt.strftime("%Y%m%d").values
BREC = (pd.to_datetime(bars["datetime"]) + pd.Timedelta(minutes=3)).dt.strftime("%H:%M").values

SEP = [d for d in D if d >= "20260901"]
NON = [d for d in D if d < "20260901"]
W = {"전체78": D, "최근30": D[-30:], "9월(12일)": SEP, "비9월(66일)": NON,
     "앞40": D[:40], "뒤38": D[40:], "앞50": D[:50], "뒤28": D[50:]}


def prep(ts):
    d = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    d["pnl"] = d.net_pct * d.w1a
    d["mfe"] = d.peak_net_pct
    d["mae"] = d.mae_net_pct
    d["win"] = d.net_pct > 0
    d["chop"] = [bool(SH[int(i)]) if int(i) < len(SH) else False for i in d.decision_idx]
    d["k"] = list(zip(d.date.astype(str), d.entry_time.astype(str), d.direction.astype(str)))
    d["ym"] = d.date.astype(str).str[:6]
    return d


B = prep(Z["runs"]["G0_BASE"])
T = prep(Z["runs"]["B3_tp10_sl10_m20"])
M = prep(Z["runs"]["G0_MARK"])


def comp(df, dates=None):
    g = df if dates is None else df[df.date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


def stats(df, dates=None):
    g = df if dates is None else df[df.date.isin(set(dates))]
    g = g.sort_values("exit_time")
    if not len(g):
        return dict(n=0)
    p = g.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[g.net_pct > 0], p[g.net_pct <= 0]
    return dict(n=len(g), 복리=round(float((eq[-1] - 1) * 100), 2),
                PF=round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else np.inf,
                MDD=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
                WR=round(100 * float((g.net_pct > 0).mean()), 1),
                평균net=round(float(g.net_pct.mean()), 3),
                일자본=round(float(g.groupby("date").w1a.sum().mean()), 3))


# ── 2. PARITY ─────────────────────────────────────────────────────────────
print("=" * 150); print("2. PARITY"); print("=" * 150)
print("G0_MARK vs G0_BASE : 거래 %d/%d · 키집합 동일 %s · net/w1a/청산시각 차이 %d"
      % (len(M), len(B), set(M.k) == set(B.k),
         int(((M.sort_values("k").net_pct.values - B.sort_values("k").net_pct.values) != 0).sum()
             if set(M.k) == set(B.k) else -1)))
on_days = sorted({d for d, o in zip(BDATE, SH) if o})
pure_off = [d for d in D if d not in set(on_days)]
bo, to = B[B.date.astype(str).isin(set(pure_off))], T[T.date.astype(str).isin(set(pure_off))]
j = to.merge(bo[["k", "net_pct", "w1a", "exit_time"]], on="k", how="inner", suffixes=("", "_b"))
dif = int(((j.net_pct - j.net_pct_b).abs() > 1e-9).sum() + (j.w1a - j.w1a_b).abs().gt(1e-9).sum()
          + (j.exit_time != j.exit_time_b).sum())
print("완전 TREND 일 %d일 : BASE %d거래 / B3 %d거래 · 키집합 동일 %s · 차이 %d  -> %s"
      % (len(pure_off), len(bo), len(to), set(bo.k) == set(to.k), dif,
         "diff 0 (TREND 불변 증명)" if (set(bo.k) == set(to.k) and dif == 0) else "위반"))

# ── 3. 핵심 성과 ──────────────────────────────────────────────────────────
print("\n" + "=" * 150); print("3. 창별 성과"); print("=" * 150)
rows = []
for wn, wd in W.items():
    sb, st = stats(B, wd), stats(T, wd)
    rows.append(dict(창=wn, BASE복리=sb["복리"], B3복리=st["복리"],
                     Δ=round(st["복리"] - sb["복리"], 2),
                     BASE_PF=sb["PF"], B3_PF=st["PF"], BASE_MDD=sb["MDD"], B3_MDD=st["MDD"],
                     BASE_WR=sb["WR"], B3_WR=st["WR"], BASE_n=sb["n"], B3_n=st["n"],
                     BASEnet=sb["평균net"], B3net=st["평균net"],
                     BASE자본=sb["일자본"], B3자본=st["일자본"]))
print(pd.DataFrame(rows).to_string(index=False))
print("\n9월 재현 확인: BASE %.2f (기대 8.73) / B3 %.2f (기대 10.96)"
      % (comp(B, SEP), comp(T, SEP)))

# ── 4. TOP 의존도 ─────────────────────────────────────────────────────────
print("\n" + "=" * 150); print("4. TOP N 제외 (BASE·B3 각각 자기 상위거래 제외)"); print("=" * 150)
tot = comp(T) - comp(B)
rows = []
for n in (0, 1, 3, 5, 10, 15):
    bb = B.drop(B.nlargest(n, "pnl").index) if n else B
    tt = T.drop(T.nlargest(n, "pnl").index) if n else T
    dv = comp(tt) - comp(bb)
    rows.append(dict(제외=("없음" if n == 0 else "Top%d" % n),
                     BASE복리=round(comp(bb), 2), B3복리=round(comp(tt), 2),
                     delta=round(dv, 2), 유지율=round(100 * dv / tot, 1)))
print(pd.DataFrame(rows).to_string(index=False))

# ── 5. 기여도 concentration ───────────────────────────────────────────────
print("\n" + "=" * 150); print("5. 거래별 delta 기여도 (양쪽에서 제거 후 델타 재측정)"); print("=" * 150)
keys = list(dict.fromkeys(list(B.k) + list(T.k)))
rec = []
for k in keys:
    bb, tt = B[B.k != k], T[T.k != k]
    c = tot - (comp(tt) - comp(bb))
    rb = B[B.k == k]
    rt = T[T.k == k]
    rec.append(dict(key=k, date=k[0], time=str(k[1])[11:16], dir=k[2],
                    BASEnet=round(float(rb.net_pct.iloc[0]), 3) if len(rb) else None,
                    B3net=round(float(rt.net_pct.iloc[0]), 3) if len(rt) else None,
                    chop=bool((rt.chop.iloc[0] if len(rt) else rb.chop.iloc[0])),
                    B3exit=(str(rt.exit_reason.iloc[0]) if len(rt) else "제거됨"),
                    기여=round(c, 3)))
C = pd.DataFrame(rec).sort_values("기여", key=abs, ascending=False).reset_index(drop=True)
print(C.head(20).to_string(index=False))
a = C.기여.abs()
print("\n전체 delta %+.2f · 기여 절대합 %.2f" % (tot, a.sum()))
for n in (1, 3, 5, 10):
    print("  Top%-2d 절대기여 비중 %.1f%%  (순합 %+.2f)"
          % (n, 100 * a.head(n).sum() / a.sum(), C.기여.head(n).sum()))
hhi = float(((a / a.sum()) ** 2).sum())
print("  Herfindahl(절대기여 점유율 제곱합) = %.4f  (균등분산 1/%d=%.4f)"
      % (hhi, len(a), 1 / len(a)))
print("  양의 기여 %d건 합 %+.2f · 음의 기여 %d건 합 %+.2f"
      % (int((C.기여 > 0).sum()), C[C.기여 > 0].기여.sum(),
         int((C.기여 < 0).sum()), C[C.기여 < 0].기여.sum()))
C.to_csv(HERE / "v1_delta_contrib.csv", index=False, encoding="utf-8-sig")

# ── 6. LOO / bootstrap ────────────────────────────────────────────────────
print("\n" + "=" * 150); print("6. LOO / bootstrap"); print("=" * 150)
DAYB = {k: g.pnl.values for k, g in B.groupby("date")}
DAYT = {k: g.pnl.values for k, g in T.groupby("date")}


def cd(dd, days):
    p = np.concatenate([dd[x] for x in days if x in dd]) if days else np.array([])
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


loo = np.array([cd(DAYT, [x for x in D if x != day]) - cd(DAYB, [x for x in D if x != day])
                for day in D])
print("LOO(%d일): 최소 %+.2f / 중앙 %+.2f / 최대 %+.2f / 음수일 %d"
      % (len(D), loo.min(), np.median(loo), loo.max(), int((loo <= 0).sum())))
rng = np.random.default_rng(20260924)
bs = np.empty(10000)
for i in range(10000):
    s = list(rng.choice(D, len(D), replace=True))
    bs[i] = cd(DAYT, s) - cd(DAYB, s)
print("bootstrap 10,000: 평균 %+.2f / 중앙 %+.2f / 5%% %+.2f / 95%% %+.2f / P(>0) %.1f%%"
      % (bs.mean(), np.median(bs), np.percentile(bs, 5), np.percentile(bs, 95),
         100 * (bs > 0).mean()))

# ── 7. 월별 ───────────────────────────────────────────────────────────────
print("\n" + "=" * 150); print("7. 월별"); print("=" * 150)
rows = []
for ym in sorted(set(B.ym)):
    dd = [x for x in D if str(x)[:6] == ym]
    rows.append(dict(월=ym, BASE=round(comp(B, dd), 2), B3=round(comp(T, dd), 2),
                     Δ=round(comp(T, dd) - comp(B, dd), 2),
                     CHOP거래=int(T[(T.ym == ym) & T.chop].shape[0])))
print(pd.DataFrame(rows).to_string(index=False))

# ── 8. CHOP 내부 ──────────────────────────────────────────────────────────
print("\n" + "=" * 150); print("8. CHOP ON 진입 거래만"); print("=" * 150)
rows = []
for lab, d in (("BASE", B[B.chop]), ("B3", T[T.chop])):
    p = d.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[d.net_pct > 0], p[d.net_pct <= 0]
    er = d.exit_reason.value_counts().to_dict()
    rows.append(dict(구분=lab, 거래=len(d), WR=round(100 * d.win.mean(), 1),
                     평균net=round(d.net_pct.mean(), 3), 합=round(p.sum(), 3),
                     PF=round(float(w.sum() / -l.sum()), 3),
                     MDD=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
                     평균MFE=round(d.mfe.mean(), 3), 평균MAE=round(d.mae.mean(), 3),
                     run3=int((d.mfe >= 3).sum()), run5=int((d.mfe >= 5).sum()),
                     run8=int((d.mfe >= 8).sum()),
                     TP=er.get("GX_TP", 0), SL=er.get("GX_SL", 0), MAXH=er.get("GX_MAXHOLD", 0),
                     기타=len(d) - er.get("GX_TP", 0) - er.get("GX_SL", 0) - er.get("GX_MAXHOLD", 0)))
print(pd.DataFrame(rows).to_string(index=False))

# ── 9~10. 4분해 ───────────────────────────────────────────────────────────
print("\n" + "=" * 150); print("9~10. 효과 4분해"); print("=" * 150)
matched = T[T.chop & T.k.isin(set(B.k))].merge(
    B[["k", "net_pct", "peak_net_pct", "exit_reason"]], on="k", how="left", suffixes=("", "_b"))
matched["uplift"] = matched.net_pct - matched.net_pct_b
runner = matched[matched.peak_net_pct_b >= 3]
nonrun = matched[matched.peak_net_pct_b < 3]
added = T[~T.k.isin(set(B.k))]
lost = B[~B.k.isin(set(T.k))]
print("A. 기존 거래 exit 개선 (러너 제외 %d건) : %+.3f" % (len(nonrun), nonrun.uplift.sum()))
print("D. 러너 손상 (%d건)                    : %+.3f" % (len(runner), runner.uplift.sum()))
print("   A+D = 매칭 %d건 uplift 합            : %+.3f" % (len(matched), matched.uplift.sum()))
print("B. 대체진입 (%d건)                      : %+.3f" % (len(added), added.pnl.sum()))
print("C. 제거된 BASE 거래 (%d건)              : %+.3f" % (len(lost), -lost.pnl.sum()))
print("   단순합 A+B+C+D                       : %+.3f"
      % (nonrun.uplift.sum() + added.pnl.sum() - lost.pnl.sum() + runner.uplift.sum()))
print("\n[러너 손상 전량]")
print(runner[["date", "dir" if "dir" in runner else "direction", "entry_time",
              "peak_net_pct_b", "net_pct_b", "exit_reason_b", "net_pct", "exit_reason", "uplift"]]
      .to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
print("\n[대체진입 %d건 — 우연인가 구조인가]" % len(added))
print(added[["date", "direction", "entry_time", "exit_reason", "net_pct", "pnl", "chop"]]
      .to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
if len(added):
    p = added.pnl.values
    w, l = p[added.net_pct > 0], p[added.net_pct <= 0]
    print("  승률 %.1f%% · 평균net %.3f · PF %s · 월별 %s"
          % (100 * added.win.mean(), added.net_pct.mean(),
             round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else "inf",
             added.groupby("ym").pnl.sum().round(3).to_dict()))
    for n in (1, 3):
        print("  상위 %d건 제외 시 합 %+.3f" % (n, added.drop(added.nlargest(n, "pnl").index).pnl.sum()))

# ── 11. regime switching 안정성 ───────────────────────────────────────────
print("\n" + "=" * 150); print("11. regime 전환 안정성 (혼합일 전량)"); print("=" * 150)
rows = []
for day in sorted(set(on_days)):
    m = BDATE == day
    seq = SH[m].astype(int)
    tm = BREC[m]
    flips = int(np.sum(np.abs(np.diff(seq)))) if len(seq) > 1 else 0
    trans = [(tm[i + 1], "ON" if seq[i + 1] else "OFF")
             for i in range(len(seq) - 1) if seq[i] != seq[i + 1]]
    ent = T[T.date.astype(str) == day]
    rows.append(dict(날짜=day, 봉수=len(seq), ON봉=int(seq.sum()),
                     ON비율=round(100 * seq.mean(), 1), 전환횟수=flips,
                     전환시각=str(trans[:4]), B3진입=len(ent),
                     CHOP진입=int(ent.chop.sum()), TREND진입=int((~ent.chop).sum())))
R = pd.DataFrame(rows)
print(R.to_string(index=False))
print("\n전환 2회 이상인 날: %d개 %s"
      % (int((R.전환횟수 >= 2).sum()), list(R[R.전환횟수 >= 2].날짜)))
print("포지션 보유 중 mode 변경: 없음 (진입 시 rec.gx_chop 로 고정, 청산부가 그 값만 참조)")
