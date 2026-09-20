"""E5 — 진입했는데 나빴던 거래의 진입시점 특징. (실제 N1+C1 거래 기준)"""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np
import pandas as pd
import axlib as A

E1 = pickle.load(open(A.HERE / "e1.pkl", "rb"))
FL = pd.DataFrame(E1["N1C1"])
TS = E1["trades_n1c1"]
T = pd.DataFrame([{"date": t["date"], "at": t["entry_time"], "net": float(t["net_pct"]),
                   "peak": float(t["peak_net_pct"]), "mae": float(t["mae_net_pct"]),
                   "reason": t["exit_reason"], "w1a": float(t["w1a"])} for t in TS])
E = FL[FL["approved"]].merge(T, on=["date", "at"], how="inner", suffixes=("_f", "_t"))
print(f"승인 플래그 {int(FL['approved'].sum())} / 실제 거래 {len(TS)} / 매칭 {len(E)}")
E["win"] = E["net"] > 0

def blk(t):
    print("\n" + "=" * 118); print(t); print("=" * 118)

def stat(df, label, w=34):
    if not len(df):
        print(f"{label:{w}s} n=  0"); return
    v = df["net"]
    print(f"{label:{w}s} n={len(df):4d} 합{v.sum():+8.1f} 평균{v.mean():+6.2f} "
          f"중앙{v.median():+6.2f} 승률{100*(v>0).mean():5.1f}% "
          f"peak평균{df['peak'].mean():5.2f} net>=2 {int((v>=2).sum()):3d} net<=-2 {int((v<=-2).sum()):3d}")

blk("1. 실제 진입거래 전체")
stat(E, "N1+C1 진입 전체")
stat(E[E["net"] <= -1], "  net <= -1 인 거래")
stat(E[E["net"] <= -2], "  net <= -2 인 거래")

blk("2. 진입시점 특징: 승자 vs 패자 (실제 거래)")
FEATS = ["tq_score", "chop_score", "gap_pct", "gap_exp", "e20_e50_pct", "slope50",
         "slope20", "close_e20_pct", "vwap_pct", "vol_ratio", "flag_ord"]
print(f"{'특징':14s} {'전체중앙':>9s} {'승자중앙':>9s} {'패자중앙':>9s} {'차이':>9s} {'AUC':>6s}")
for f in FEATS:
    a = E.loc[E["win"], f].astype(float).dropna().to_numpy()
    b = E.loc[~E["win"], f].astype(float).dropna().to_numpy()
    if not len(a) or not len(b):
        continue
    auc = float((a[:, None] > b[None, :]).mean() + 0.5 * (a[:, None] == b[None, :]).mean())
    print(f"{f:14s} {E[f].astype(float).median():9.3f} {np.median(a):9.3f} {np.median(b):9.3f} "
          f"{np.median(a)-np.median(b):+9.3f} {auc:6.3f}")
print()
for f in ["trend_ok", "tq_ok", "teg_ok", "entry_chop"]:
    for val in (True, False):
        stat(E[E[f] == val], f"{f}={val}", 24)

blk("3. 세션/슬롯/순번별")
for c_ in ("session", "slot", "flag_ord", "direction"):
    print(f"-- {c_}")
    for k, g in sorted(E.groupby(c_)):
        stat(g, f"   {c_}={k}", 24)

blk("4. 시간대별 (진입 30분 버킷)")
E["hh"] = E["minute"].str.slice(0, 2) + ":" + (E["minute"].str.slice(3, 5).astype(int) // 30 * 30).astype(str).str.zfill(2)
for k, g in sorted(E.groupby("hh")):
    stat(g, f"   {k}", 24)

blk("5. 특징 구간별 (패자 밀집 구간 찾기) — 단일변수 컷")
for f, cuts in (("vwap_pct", [-99, -2, -1, 0, 1, 2, 99]),
                ("gap_exp", [-1e9, 0, 200, 500, 1000, 1e9]),
                ("e20_e50_pct", [-99, -1, -0.3, 0, 0.3, 1, 99]),
                ("slope50", [-1e9, -500, 0, 500, 1e9]),
                ("close_e20_pct", [-99, -0.5, 0, 0.5, 99]),
                ("vol_ratio", [0, 0.5, 0.8, 1.2, 99]),
                ("chop_score", [-1, 1, 2, 3, 99]),
                ("tq_score", [-1, 2, 3, 4, 99])):
    print(f"-- {f}")
    b = pd.cut(E[f].astype(float), cuts)
    for k, g in E.groupby(b, observed=True):
        stat(g, f"   {k}", 24)

E.to_csv(A.HERE / "entered_trades.csv", index=False, encoding="utf-8-sig")
print("\n저장: entered_trades.csv")
