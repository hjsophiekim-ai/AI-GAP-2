"""D4: 직전레그 크기로 나눈 거래 성적 + 청산 후 남은 진행. READ-ONLY."""
import os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
OUT = os.path.dirname(os.path.abspath(__file__))
T = pd.read_csv(OUT + "/trades.csv", dtype={"day": str})
S = T.dropna(subset=["prev_mfe"]).copy()
print(f"# 직전레그 관측된 거래 {len(S)}건 / 전체 {len(T)}건  (나머지는 당일 첫 진입 = 직전레그 없음)")

print("\n## 1. 직전레그 MFE 4분위별 거래성적")
S["q"] = pd.qcut(S.prev_mfe, 4, labels=["Q1 작음", "Q2", "Q3", "Q4 큼"])
print(S.groupby("q", observed=True).agg(n=("krw", "size"), 손익=("krw", "sum"), 거래당=("krw", "mean"),
      평균net=("net", "mean"), 보유분=("hold", "mean"),
      손절=("rs", lambda r: int(r.str.contains("STOP_LOSS|B3_SL").sum())),
      완주=("rs", lambda r: int(r.str.contains("TP2_FULL").sum()))).round(1).to_string())

print("\n## 2. 임계별 (직전레그 MFE >= x) — 발동/비발동")
print("| 임계 | 발동 n | 발동 손익 | 거래당 | 완주 | 손절 | 비발동 n | 비발동 거래당 |")
print("|---|---|---|---|---|---|---|---|")
for x in (0.8, 1.0, 1.2, 1.5, 2.0):
    a, b = S[S.prev_mfe >= x], S[S.prev_mfe < x]
    print(f"| >={x:.1f}% | {len(a)} | {a.krw.sum():+,.0f} | {a.krw.mean():+,.0f} | "
          f"{int(a.rs.str.contains('TP2_FULL').sum())} | {int(a.rs.str.contains('STOP_LOSS|B3_SL').sum())} | "
          f"{len(b)} | {b.krw.mean():+,.0f} |")

print("\n## 3. 보유시간이 짧게 끝난 거래에서 20/30/40/60분까지의 ETF 최대진행(MFE %)")
for lab, sub in (("직전레그 >=1.2%", S[S.prev_mfe >= 1.2]), ("직전레그 <1.2%", S[S.prev_mfe < 1.2])):
    r = sub[["mfe20", "mfe30", "mfe40", "mfe60", "mae20", "mae30", "mae40", "mae60"]].mean()
    print(f"\n### {lab}  (n={len(sub)})")
    print("| 경과 | 최대진행 평균 | 최대역행 평균 |")
    print("|---|---|---|")
    for n in (20, 30, 40, 60):
        print(f"| {n}분 | {r[f'mfe{n}']:+.2f}% | {r[f'mae{n}']:+.2f}% |")

print("\n## 4. 청산사유별 x 직전레그")
S["big"] = np.where(S.prev_mfe >= 1.2, "큼", "작음")
piv = S.pivot_table(index="rs", columns="big", values="krw", aggfunc=["size", "sum"]).fillna(0)
piv.columns = [f"{a}_{b}" for a, b in piv.columns]
piv = piv.sort_values("size_큼", ascending=False).head(12)
print(piv.round(0).to_string())
