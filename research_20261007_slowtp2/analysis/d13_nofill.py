"""D13: 'E 의 돌파대기는 필터로만 쓰고, 체결은 하지 않는다' 1차근사. READ-ONLY.
   슬롯 해제 효과는 반영 못 한다 -- 재생 전 선별용 수치다.
"""
import os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
OUT = os.path.dirname(os.path.abspath(__file__))
B = pd.read_csv(OUT + "/brk.csv", dtype={"day": str})
F = B[B.fired].copy()
F["seg"] = np.where(F.day < "20260801", "train 5~7월", "test 8~10월")
print(f"# 돌파 체결 {len(F)}건 · E 손익합 {F.e_krw.sum():+,.0f} · 같은신호 A 손익합 {F.a_krw.sum(skipna=True):+,.0f}")
print(f"# E-A = {(F.e_krw - F.a_krw).sum():+,.0f}")

print("\n## 돌파 체결을 '폐기'로 바꾸면 (E 대비 증분 = -e_krw)")
print("| dist 하한 | 해당 체결 | E 손익 | 폐기 시 증분 | train 증분 | test 증분 |")
print("|---|---|---|---|---|---|")
for x in (0.0, 0.3, 0.6, 1.0):
    s = F[F.dist >= x]
    tr, te = s[s.seg.str.startswith("train")], s[s.seg.str.startswith("test")]
    print(f"| >={x:.1f}% | {len(s)} | {s.e_krw.sum():+,.0f} | **{-s.e_krw.sum():+,.0f}** | "
          f"{-tr.e_krw.sum():+,.0f} | {-te.e_krw.sum():+,.0f} |")

print("\n## 지연(delay_min) 기준")
print("| delay 하한 | 체결 n | E 손익 | 폐기 시 증분 | 승 | 패 |")
print("|---|---|---|---|---|---|")
for x in (0, 3, 5, 8, 10):
    s = F[F.delay >= x]
    print(f"| >={x}분 | {len(s)} | {s.e_krw.sum():+,.0f} | **{-s.e_krw.sum():+,.0f}** | "
          f"{int((s.e_krw > 0).sum())} | {int((s.e_krw < 0).sum())} |")

print("\n## 체결 25건 전체 (dist 순)")
print("| 일자 | 승인 | 방향 | dist | 지연 | E 손익 | A 손익 | E-A | 오늘형 |")
print("|---|---|---|---|---|---|---|---|---|")
for _, r in F.sort_values("dist", ascending=False).iterrows():
    print(f"| {r.day} | {r.at} | {r.dir} | {r.dist:.2f}% | {r.delay:.0f}분 | {r.e_krw:+,.0f} | "
          f"{r.a_krw:+,.0f} | {r.e_krw - r.a_krw:+,.0f} | {'O' if r.pair >= 1.0 else ''} |")
