"""E4 — '진입하지 않았지만 수익이 좋았던 플래그' 의 정체 분석.

전부 진입시점 특징(완성봉)만 쓴다. 하루 3회 슬롯 한도는 하드 제약이므로
DAILY_SLOT_CAP 거절은 '정책상 제외' 로 따로 표시한다.
"""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np
import pandas as pd
import axlib as A

S = pd.DataFrame(list(pickle.load(open(A.HERE / "e3.pkl", "rb")).values()))
S = S[S["solo_n"] > 0].copy()
HARD = {"TW2_3SLOT_REJECT_DAILY_SLOT_CAP"}
S["policy_locked"] = S["reason"].isin(HARD)
S["miss"] = (~S["approved"]) & (~S["approved_h50"].fillna(False))
S["win"] = S["solo_net"] > 0

def blk(t):
    print("\n" + "=" * 118); print(t); print("=" * 118)

def stat(df, label, w=34):
    if not len(df):
        print(f"{label:{w}s} n=  0"); return
    v = df["solo_net"]
    print(f"{label:{w}s} n={len(df):4d} 합{v.sum():+9.1f} 평균{v.mean():+6.2f} "
          f"중앙{v.median():+6.2f} 승률{100*(v>0).mean():5.1f}% "
          f"peak평균{df['solo_peak'].mean():5.2f} peak>=5 {int((df['solo_peak']>=5).sum()):3d} "
          f"net>=2 {int((v>=2).sum()):3d} net<=-2 {int((v<=-2).sum()):3d}")

blk("1. 진입/미진입 solo 성과 대조 (독립 반사실, 청산 = N1+C1)")
stat(S[S["approved"]], "N1 승인 플래그")
stat(S[S["approved_h50"].fillna(False)], "H50 승인 플래그")
stat(S[S["miss"]], "N1·H50 둘 다 미진입")
stat(S[S["miss"] & ~S["policy_locked"]], "  그중 정책제외(3회한도) 제외")
stat(S[S["miss"] & S["policy_locked"]], "  그중 3회한도로 막힌 것")
stat(S[S["miss"] & ~S["policy_locked"] & S["flat"]], "  미진입·완화가능·flat")
stat(S[S["miss"] & ~S["policy_locked"] & ~S["flat"]], "  미진입·완화가능·보유중")

blk("2. 거절사유별 solo 성과 (flat, 미진입만)")
P = S[S["miss"] & S["flat"]]
for reason, g in sorted(P.groupby("reason"), key=lambda kv: -kv[1]["solo_net"].sum()):
    tag = " [정책제외]" if reason in HARD else ""
    stat(g, reason[:30] + tag, 42)

blk("3. 완화가능 미진입 풀에서 '좋았던 플래그' 의 특징 (winners vs losers)")
C = P[~P["policy_locked"]].copy()
FEATS = ["tq_score", "chop_score", "gap_pct", "gap_exp", "e20_e50_pct", "slope50",
         "slope20", "close_e20_pct", "vwap_pct", "vol_ratio", "flag_ord"]
BOOL = ["trend_ok", "tq_ok", "teg_ok", "entry_chop"]
print(f"  풀 n={len(C)} / net>=+2 {int((C['solo_net']>=2).sum())} / net<=-2 {int((C['solo_net']<=-2).sum())}")
print(f"\n{'특징':14s} {'전체중앙':>9s} {'승자중앙':>9s} {'패자중앙':>9s} {'차이':>8s} {'AUC':>6s}")
big = C[C["solo_net"] >= 2]; bad = C[C["solo_net"] <= -2]
for f in FEATS:
    v = C[f].astype(float)
    a = C.loc[C["win"], f].astype(float); b = C.loc[~C["win"], f].astype(float)
    if a.isna().all() or b.isna().all():
        continue
    # AUC = P(승자 특징 > 패자 특징)
    aa, bb = a.dropna().to_numpy(), b.dropna().to_numpy()
    auc = float((aa[:, None] > bb[None, :]).mean() + 0.5 * (aa[:, None] == bb[None, :]).mean()) \
        if len(aa) and len(bb) else float("nan")
    print(f"{f:14s} {v.median():9.3f} {a.median():9.3f} {b.median():9.3f} "
          f"{a.median()-b.median():+8.3f} {auc:6.3f}")
print()
for f in BOOL:
    for val in (True, False):
        g = C[C[f] == val]
        stat(g, f"{f}={val}", 24)

blk("4. 상위추세 정렬(trend_ok) x 품질점수 교차표 — 완화가능 미진입 풀")
print(f"{'':10s} " + " ".join(f"{'tq='+str(q):>26s}" for q in sorted(C['tq_score'].unique())))
for t in (True, False):
    cells = []
    for q in sorted(C["tq_score"].unique()):
        g = C[(C["trend_ok"] == t) & (C["tq_score"] == q)]
        cells.append(f"n{len(g):3d} 합{g['solo_net'].sum():+7.1f} 승{100*(g['solo_net']>0).mean() if len(g) else 0:4.0f}%"
                     if len(g) else " " * 26)
    print(f"trend={str(t):5s} " + " ".join(f"{x:>26s}" for x in cells))

blk("5. 좋았던 미진입 플래그 전량 (solo_net >= +2, 완화가능)")
cols = ["date", "minute", "direction", "flag_ord", "reason", "tq_score", "trend_ok",
        "teg_ok", "entry_chop", "chop_score", "gap_pct", "gap_exp", "e20_e50_pct",
        "slope50", "vwap_pct", "vol_ratio", "solo_net", "solo_peak", "solo_mae", "solo_reason"]
B = C[C["solo_net"] >= 2].sort_values("solo_net", ascending=False)
pd.set_option("display.width", 250, "display.max_columns", 40)
print(B[cols].to_string(index=False, float_format=lambda x: f"{x:.2f}"))

blk("6. 나빴던 미진입 플래그 (solo_net <= -2, 완화가능) — 완화하면 같이 들어온다")
W = C[C["solo_net"] <= -2].sort_values("solo_net")
print(W[cols].to_string(index=False, float_format=lambda x: f"{x:.2f}"))

C.to_csv(A.HERE / "relaxable_pool.csv", index=False, encoding="utf-8-sig")
print("\n저장: relaxable_pool.csv")
