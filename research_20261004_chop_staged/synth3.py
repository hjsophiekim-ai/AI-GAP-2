"""진입 시점 정보만으로 '손절거래'와 '러너'를 구분할 수 있는가 — 직접 검정. READ-ONLY.

질문: 10/02 의 첫 두 거래(둘 다 손절)를 진입 전에 걸러내면서, TP2 러너는 그대로 들어가는
임계가 존재하는가?
"""
import os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
P = pd.read_csv(ROOT + "/synth2_paths.csv")
E = pd.read_csv(ROOT + "/calib2_entries.csv")
P["key"] = P["day"].astype(str) + " " + pd.to_datetime(P["entry"]).dt.strftime("%H:%M:%S")
E["key"] = E["day"].astype(str) + " " + pd.to_datetime(E["t"]).dt.strftime("%H:%M:%S")
df = P.merge(E[["key", "pe", "rng", "xc"]], on="key", how="inner")
df["hhmm"] = pd.to_datetime(df["entry"]).dt.strftime("%H:%M")
df["min"] = pd.to_datetime(df["entry"]).dt.hour * 60 + pd.to_datetime(df["entry"]).dt.minute
print(f"# 진입지표 + 경로가 모두 있는 거래 {len(df)}건 / 경로 {len(P)}건")
print(f"  버킷: {df.bucket.value_counts().to_dict()}")

RUN = df.bucket == "RUNNER(TP2)"
SL = df.bucket.isin(["N1손절", "B3손절"])
print(f"\n## 진입시점 지표 — 러너({int(RUN.sum())}건) vs 손절({int(SL.sum())}건)")
print(f"{'지표':8s} {'러너 평균':>10s} {'러너 중앙':>10s} {'손절 평균':>10s} {'손절 중앙':>10s} {'차이':>8s}")
for c in ("pe", "rng", "xc", "min"):
    a, b = df.loc[RUN, c], df.loc[SL, c]
    print(f"{c:8s} {a.mean():>10.3f} {a.median():>10.3f} {b.mean():>10.3f} {b.median():>10.3f} {a.mean()-b.mean():>8.3f}")

print("\n## 10/02 첫 두 거래의 진입지표 (걸러내야 할 대상)")
t2 = df[df.day == 20261002]
for _, r in t2.iterrows():
    print(f"  {r['hhmm']}  PE {r['pe']:.3f}  RNG {r['rng']:.3f}  XC {int(r['xc'])}  -> {r['bucket']} {r['krw']:+,.0f}")

print("\n## 그 두 거래와 '같은 구간'에 들어오는 러너는 몇 건인가")
print("| 조건(진입 차단) | 차단건수 | 차단손익 | 그중 손절 | 그중 러너 | 러너손익 | 10/02 2건 차단? |")
print("|---|---|---|---|---|---|---|")
l1 = t2[t2.hhmm == "10:00"].iloc[0] if len(t2[t2.hhmm == "10:00"]) else None
l2 = t2[t2.hhmm == "11:30"].iloc[0] if len(t2[t2.hhmm == "11:30"]) else None
conds = [
    ("PE < 0.30", df.pe < 0.30),
    ("PE < 0.30 & RNG < 1.0", (df.pe < 0.30) & (df.rng < 1.0)),
    ("PE < 0.30 & RNG < 1.0 & XC >= 3", (df.pe < 0.30) & (df.rng < 1.0) & (df.xc >= 3)),
    ("RNG < 1.0", df.rng < 1.0),
    ("XC >= 3", df.xc >= 3),
    ("XC >= 5", df.xc >= 5),
]
for name, m in conds:
    z = df[m]
    hit = []
    for lab, r in (("09:54", l1), ("11:24", l2)):
        if r is not None:
            ok = bool(m.loc[df.index[df.key == r["key"]]].iloc[0])
            hit.append(lab if ok else "-")
    print(f"| {name} | {int(m.sum())} | {z.krw.sum():+,.0f} | {int(z.bucket.isin(['N1손절','B3손절']).sum())} | "
          f"{int((z.bucket=='RUNNER(TP2)').sum())} | {z[z.bucket=='RUNNER(TP2)'].krw.sum():+,.0f} | {' '.join(hit)} |")

print("\n## 판별력 검정 — 어떤 단일 임계가 손절을 러너보다 많이 거르는가")
print("| 지표 | 임계 | 차단 손절 | 차단 러너 | 손절:러너 | 차단 총손익 |")
print("|---|---|---|---|---|---|")
for c, ths in (("pe", [0.10, 0.15, 0.20, 0.25, 0.30]),
               ("rng", [0.8, 1.0, 1.2, 1.5]),
               ("xc", [3, 4, 5, 6])):
    for th in ths:
        m = (df[c] < th) if c != "xc" else (df[c] >= th)
        z = df[m]
        ns_, nr = int(z.bucket.isin(["N1손절", "B3손절"]).sum()), int((z.bucket == "RUNNER(TP2)").sum())
        ratio = f"{ns_/nr:.1f}:1" if nr else f"{ns_}:0"
        op = "<" if c != "xc" else ">="
        print(f"| {c} | {op}{th} | {ns_} | {nr} | {ratio} | {z.krw.sum():+,.0f} |")
base_r = int(SL.sum()) / max(int(RUN.sum()), 1)
print(f"\n  무작위 기준선(전체 손절:러너) = {base_r:.1f}:1 — 위 비율이 이보다 크게 높아야 판별력이 있다.")
