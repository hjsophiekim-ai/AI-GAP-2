"""V3 — 정확한 80영업일(20260527~20260922) B3 재검증. 엔진 재실행 없음."""
import pickle, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 340); pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 300)
HERE = Path(__file__).resolve().parent
Z = pickle.load(open(HERE / "v2_80.pkl", "rb"))
D = Z["dates"]
SH = Z["shadow"]
import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
import axlib as A
ce.CACHE_DIR = Path(r"G:\다른 컴퓨터\내 노트북 (2)\Desktop\AI-GAP 2\data\cache")
H._CTX_CACHE = HERE / "_ctx81.pkl"
ctx = H.build_ctx(81)
bars = ctx.hynix_bars_3m.reset_index(drop=True)
BDATE = pd.to_datetime(bars["datetime"]).dt.strftime("%Y%m%d").values
BREC = (pd.to_datetime(bars["datetime"]) + pd.Timedelta(minutes=3)).dt.strftime("%H:%M").values

SEP = [d for d in D if d >= "20260901"]
NON = [d for d in D if d < "20260901"]
W = {"전체80": D, "최근30": D[-30:], "9월(%d일)" % len(SEP): SEP, "비9월": NON,
     "앞40": D[:40], "뒤40": D[40:], "앞50": D[:50], "뒤30": D[50:]}


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


B, T, M = prep(Z["base"]), prep(Z["b3"]), prep(Z["mark"])


def comp(df, dates=None):
    g = df if dates is None else df[df.date.isin(set(dates))]
    p = g.sort_values("exit_time").pnl.values
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


def stats(df, dates=None):
    g = (df if dates is None else df[df.date.isin(set(dates))]).sort_values("exit_time")
    if not len(g):
        return dict(n=0, 복리=0, PF=0, MDD=0, WR=0, 평균net=0, 일자본=0)
    p = g.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[g.net_pct > 0], p[g.net_pct <= 0]
    return dict(n=len(g), 복리=round(float((eq[-1] - 1) * 100), 2),
                PF=round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else np.inf,
                MDD=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
                WR=round(100 * float((g.net_pct > 0).mean()), 1),
                평균net=round(float(g.net_pct.mean()), 3),
                일자본=round(float(g.groupby("date").w1a.sum().mean()), 3))


print("=" * 150); print("80일 창: %s ~ %s (%d일) · 9월 %d일" % (D[0], D[-1], len(D), len(SEP)))
print("=" * 150)
print("\n2. PARITY")
print("  MARK vs BASE : 거래 %d/%d · 키집합 동일 %s" % (len(M), len(B), set(M.k) == set(B.k)))
on_days = sorted({d for d, o in zip(BDATE, SH) if o and d in set(D)})
pure_off = [d for d in D if d not in set(on_days)]
bo, to = B[B.date.astype(str).isin(set(pure_off))], T[T.date.astype(str).isin(set(pure_off))]
j = to.merge(bo[["k", "net_pct", "w1a", "exit_time"]], on="k", how="inner", suffixes=("", "_b"))
dif = int(((j.net_pct - j.net_pct_b).abs() > 1e-9).sum() + (j.w1a - j.w1a_b).abs().gt(1e-9).sum()
          + (j.exit_time != j.exit_time_b).sum())
print("  완전 TREND %d일 : BASE %d / B3 %d · 키집합 동일 %s · 차이 %d -> %s"
      % (len(pure_off), len(bo), len(to), set(bo.k) == set(to.k), dif,
         "diff 0" if (set(bo.k) == set(to.k) and dif == 0) else "위반"))
print("  SHADOW ON 일수 %d · 9월 ON 비율 %.1f%%" % (len(on_days),
      100 * np.mean([d in set(on_days) for d in SEP])))

print("\n3. 창별 성과")
rows = []
for wn, wd in W.items():
    sb, st = stats(B, wd), stats(T, wd)
    rows.append(dict(창=wn, BASE=sb["복리"], B3=st["복리"], Δ=round(st["복리"] - sb["복리"], 2),
                     BPF=sb["PF"], TPF=st["PF"], BMDD=sb["MDD"], TMDD=st["MDD"],
                     BWR=sb["WR"], TWR=st["WR"], Bn=sb["n"], Tn=st["n"],
                     Bnet=sb["평균net"], Tnet=st["평균net"], B자본=sb["일자본"], T자본=st["일자본"]))
print(pd.DataFrame(rows).to_string(index=False))

print("\n4. TOP N 제외")
tot = comp(T) - comp(B); b0 = comp(B)
rows = []
for n in (0, 1, 3, 5, 10, 15):
    bb = B.drop(B.nlargest(n, "pnl").index) if n else B
    tt = T.drop(T.nlargest(n, "pnl").index) if n else T
    dv = comp(tt) - comp(bb)
    rows.append(dict(제외=("없음" if n == 0 else "Top%d" % n), BASE=round(comp(bb), 2),
                     B3=round(comp(tt), 2), delta=round(dv, 2),
                     BASE유지율=round(100 * comp(bb) / b0, 1),
                     delta유지율=round(100 * dv / tot, 1),
                     상대=round((dv / tot) / (comp(bb) / b0), 2)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n5. 거래별 delta 기여도")
keys = list(dict.fromkeys(list(B.k) + list(T.k)))
rec = []
for k in keys:
    c = tot - (comp(T[T.k != k]) - comp(B[B.k != k]))
    rb, rt = B[B.k == k], T[T.k == k]
    rec.append(dict(date=k[0], time=str(k[1])[11:16], dir=k[2],
                    BASEnet=round(float(rb.net_pct.iloc[0]), 3) if len(rb) else None,
                    B3net=round(float(rt.net_pct.iloc[0]), 3) if len(rt) else None,
                    chop=bool(rt.chop.iloc[0] if len(rt) else rb.chop.iloc[0]),
                    B3exit=(str(rt.exit_reason.iloc[0]) if len(rt) else "제거됨"),
                    기여=round(c, 3)))
C = pd.DataFrame(rec).sort_values("기여", key=abs, ascending=False).reset_index(drop=True)
print(C.head(12).to_string(index=False))
a = C.기여.abs()
print("  전체 delta %+.2f · 절대기여합 %.2f" % (tot, a.sum()))
for n in (1, 3, 5, 10):
    print("   Top%-2d 절대비중 %.1f%% · 순합 %+.2f" % (n, 100 * a.head(n).sum() / a.sum(),
                                                  C.기여.head(n).sum()))
print("   Herfindahl %.4f (균등 %.4f) · 양 %d건 %+.2f / 음 %d건 %+.2f"
      % (float(((a / a.sum()) ** 2).sum()), 1 / len(a), int((C.기여 > 0).sum()),
         C[C.기여 > 0].기여.sum(), int((C.기여 < 0).sum()), C[C.기여 < 0].기여.sum()))
C.to_csv(HERE / "v3_delta_contrib80.csv", index=False, encoding="utf-8-sig")

print("\n6. LOO / bootstrap")
DB = {k: g.pnl.values for k, g in B.groupby("date")}
DT = {k: g.pnl.values for k, g in T.groupby("date")}


def cd(dd, days):
    p = np.concatenate([dd[x] for x in days if x in dd]) if days else np.array([])
    return float(((1 + p / 100).prod() - 1) * 100) if len(p) else 0.0


loo = np.array([cd(DT, [x for x in D if x != d]) - cd(DB, [x for x in D if x != d]) for d in D])
print("  LOO(%d일): 최소 %+.2f / 중앙 %+.2f / 최대 %+.2f / 음수일 %d"
      % (len(D), loo.min(), np.median(loo), loo.max(), int((loo <= 0).sum())))
rng = np.random.default_rng(20260924)
bs = np.array([cd(DT, list(rng.choice(D, len(D), replace=True)))
               - cd(DB, list(rng.choice(D, len(D), replace=True))) for _ in range(0)])
bs = np.empty(10000)
for i in range(10000):
    s = list(rng.choice(D, len(D), replace=True))
    bs[i] = cd(DT, s) - cd(DB, s)
print("  bootstrap: 평균 %+.2f / 중앙 %+.2f / 5%% %+.2f / 95%% %+.2f / P(>0) %.1f%%"
      % (bs.mean(), np.median(bs), np.percentile(bs, 5), np.percentile(bs, 95),
         100 * (bs > 0).mean()))

print("\n7. 월별")
rows = []
for ym in sorted(set(B.ym)):
    dd = [x for x in D if str(x)[:6] == ym]
    rows.append(dict(월=ym, BASE=round(comp(B, dd), 2), B3=round(comp(T, dd), 2),
                     Δ=round(comp(T, dd) - comp(B, dd), 2),
                     CHOP거래=int(T[(T.ym == ym) & T.chop].shape[0])))
print(pd.DataFrame(rows).to_string(index=False))

print("\n8. CHOP ON 진입 거래만")
rows = []
for lab, d in (("BASE", B[B.chop]), ("B3", T[T.chop])):
    if not len(d):
        continue
    p = d.pnl.values
    eq = np.cumprod(1 + p / 100)
    w, l = p[d.net_pct > 0], p[d.net_pct <= 0]
    er = d.exit_reason.value_counts().to_dict()
    rows.append(dict(구분=lab, 거래=len(d), WR=round(100 * d.win.mean(), 1),
                     평균net=round(d.net_pct.mean(), 3), 합=round(p.sum(), 3),
                     PF=round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else np.inf,
                     MDD=round(float((eq / np.maximum.accumulate(eq) - 1).min() * 100), 2),
                     평균MFE=round(d.mfe.mean(), 3), 평균MAE=round(d.mae.mean(), 3),
                     run3=int((d.mfe >= 3).sum()), run5=int((d.mfe >= 5).sum()),
                     run8=int((d.mfe >= 8).sum()), TP=er.get("GX_TP", 0),
                     SL=er.get("GX_SL", 0), MAXH=er.get("GX_MAXHOLD", 0)))
print(pd.DataFrame(rows).to_string(index=False))

print("\n9~10. 효과 4분해")
mt = T[T.chop & T.k.isin(set(B.k))].merge(
    B[["k", "net_pct", "peak_net_pct", "exit_reason"]], on="k", how="left", suffixes=("", "_b"))
mt["uplift"] = mt.net_pct - mt.net_pct_b
run = mt[mt.peak_net_pct_b >= 3]
non = mt[mt.peak_net_pct_b < 3]
add = T[~T.k.isin(set(B.k))]
lost = B[~B.k.isin(set(T.k))]
print("  A 기존 exit 개선(러너 제외 %d건) %+.3f | D 러너 손상(%d건) %+.3f | A+D %+.3f"
      % (len(non), non.uplift.sum(), len(run), run.uplift.sum(), mt.uplift.sum()))
print("  B 대체진입(%d건) %+.3f | C 제거(%d건) %+.3f | 단순합 %+.3f"
      % (len(add), add.pnl.sum(), len(lost), -lost.pnl.sum(),
         non.uplift.sum() + run.uplift.sum() + add.pnl.sum() - lost.pnl.sum()))
print("\n  [러너 손상 전량]")
print(run[["date", "direction", "entry_time", "peak_net_pct_b", "net_pct_b", "exit_reason_b",
           "net_pct", "exit_reason", "uplift"]].to_string(index=False,
                                                          float_format=lambda v: f"{v:,.3f}"))
if len(add):
    p = add.pnl.values
    w, l = p[add.net_pct > 0], p[add.net_pct <= 0]
    print("\n  [대체진입 %d건] 승률 %.1f%% · 평균net %.3f · PF %s · 월별 %s"
          % (len(add), 100 * add.win.mean(), add.net_pct.mean(),
             round(float(w.sum() / -l.sum()), 3) if len(l) and l.sum() < 0 else "inf",
             add.groupby("ym").pnl.sum().round(3).to_dict()))

print("\n11. regime 전환 안정성")
rows = []
for day in sorted(set(on_days)):
    m = BDATE == day
    seq = SH[m].astype(int)
    tm = BREC[m]
    flips = int(np.sum(np.abs(np.diff(seq)))) if len(seq) > 1 else 0
    trans = [(tm[i + 1], "ON" if seq[i + 1] else "OFF") for i in range(len(seq) - 1)
             if seq[i] != seq[i + 1]]
    ent = T[T.date.astype(str) == day]
    rows.append(dict(날짜=day, ON비율=round(100 * seq.mean(), 1), 전환=flips,
                     전환시각=str(trans[:4]), 진입=len(ent), CHOP=int(ent.chop.sum())))
R = pd.DataFrame(rows)
print(R.to_string(index=False))
print("  전환 2회 이상: %d일" % int((R.전환 >= 2).sum()))
