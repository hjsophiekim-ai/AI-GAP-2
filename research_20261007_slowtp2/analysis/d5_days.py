"""D5: '오전에 큰 레드 + 큰 블루 2개' 날 분류 -> 그 날의 성적. READ-ONLY.

사용자 기술: 오전에 레드 크게, 블루 크게 2시간 동안 2번.
= 오전(09:00~11:30)에 서로 반대방향 플래그 2개, 간격 <=120분, 두 레그 모두 큼.
판정시점은 '2번째 플래그' -- 그때 1번째 레그 크기는 이미 관측 가능하다.
"""
import os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
OUT = os.path.dirname(os.path.abspath(__file__))
L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})
T = pd.read_csv(OUT + "/trades.csv", dtype={"day": str})

AM_END = 11 * 60 + 30
rows = []
for day, g in L.groupby("day"):
    g = g.sort_values("mins").reset_index(drop=True)
    am = g[g.mins < AM_END]
    # 오전 최대 레그, 그리고 '반대방향 2연속 큰 레그' 쌍
    best_pair, pair_at = 0.0, None
    for i in range(len(am) - 1):
        a, b = am.iloc[i], am.iloc[i + 1]
        if a["dir"] == b["dir"]:
            continue
        if b["mins"] - a["mins"] > 120:
            continue
        m = min(a["mfe"], b["mfe"])          # 둘 다 커야 하므로 작은 쪽
        if m > best_pair:
            best_pair, pair_at = m, f"{a['at']}->{b['at']}"
    rows.append(dict(day=day, am_legs=len(am), pair_min_mfe=best_pair, pair=pair_at,
                     am_max_mfe=float(am.mfe.max()) if len(am) else 0.0,
                     day_max_mfe=float(g.mfe.max())))
D = pd.DataFrame(rows)
P = T.groupby("day").agg(krw=("krw", "sum"), n=("krw", "size")).reset_index()
D = D.merge(P, on="day", how="left").fillna({"krw": 0, "n": 0})
D.to_csv(OUT + "/days.csv", index=False, encoding="utf-8")

print(f"# {len(D)}일 · 거래일 {(D.n>0).sum()}일 · 총 {D.krw.sum():+,.0f}원")
print("\n## '오전 반대방향 큰 레그 2연속' 임계별 (두 레그 중 작은 쪽 MFE >= x)")
print("| 임계 | 해당일 | 그 날 손익합 | 일평균 | 거래수 | 비해당 일평균 |")
print("|---|---|---|---|---|---|")
for x in (0.5, 0.8, 1.0, 1.2, 1.5, 2.0):
    a, b = D[D.pair_min_mfe >= x], D[D.pair_min_mfe < x]
    print(f"| >={x:.1f}% | {len(a)}일 | {a.krw.sum():+,.0f} | {a.krw.mean():+,.0f} | {int(a.n.sum())} | {b.krw.mean():+,.0f} |")

print("\n## 해당일 목록 (임계 1.0%) — 9·10월 표시")
sel = D[D.pair_min_mfe >= 1.0].sort_values("day")
print("| 일자 | 쌍 | 작은쪽MFE | 거래수 | 그 날 손익 |")
print("|---|---|---|---|---|")
for _, r in sel.iterrows():
    mk = " **9/10월**" if r.day >= "20260901" else ""
    print(f"| {r.day}{mk} | {r.pair} | {r.pair_min_mfe:.2f}% | {int(r.n)} | {r.krw:+,.0f} |")
