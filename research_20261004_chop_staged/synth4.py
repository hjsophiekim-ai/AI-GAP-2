"""슬롯 순번 구조 정밀 분석 — 하루 N번째 진입의 기대값과 러너 분포. READ-ONLY."""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]

DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
rows = []
for d in DAYS:
    seq = sorted(trades(json.load(open(f"{O3}/REAL_A_{d}.json", encoding="utf-8"))), key=lambda t: t["entry"])
    for i, t in enumerate(seq, 1):
        rs = "+".join(dict.fromkeys(t["reasons"]))
        rows.append(dict(day=d, ord=i, krw=t["krw"], net=t["net"], rs=rs,
                         run=("TP2_FULL" in rs), sl=("STOP_LOSS" in rs or rs == "B3_SL"),
                         entry=t["entry"]))
df = pd.DataFrame(rows)
tot = df.krw.sum()
print(f"# A 85일 {len(df)}거래 {tot:+,.0f}원\n")
print("## 하루 N번째 진입의 구조")
print("| 순번 | 거래 | 손익 | 거래당 | 러너(TP2) | 러너손익 | 손절 | 손절손익 | 기타손익 |")
print("|---|---|---|---|---|---|---|---|---|")
for o in sorted(df["ord"].unique()):
    z = df[df["ord"] == o]
    r, s = z[z.run], z[z.sl]
    other = z[~z.run & ~z.sl]
    print(f"| {o} | {len(z)} | {z.krw.sum():+,.0f} | {z.krw.mean():+,.0f} | {len(r)} | {r.krw.sum():+,.0f} | "
          f"{len(s)} | {s.krw.sum():+,.0f} | {other.krw.sum():+,.0f} |")

print("\n## 3번째 진입을 '차단'했을 때 (1차 근사: 그 거래만 제거)")
z3 = df[df["ord"] >= 3]
print(f"  차단 {len(z3)}건 · 손익 {z3.krw.sum():+,.0f}원 (전체의 {z3.krw.sum()/tot*100:.1f}%)")
print(f"  러너 {int(z3.run.sum())}건 {z3[z3.run].krw.sum():+,.0f} / 손절 {int(z3.sl.sum())}건 {z3[z3.sl].krw.sum():+,.0f}")
print(f"  => 순효과 {-z3.krw.sum():+,.0f}원")
print("\n## 3번째 진입을 '절반 비중'으로 했을 때 (1차 근사)")
print(f"  => 순효과 {-z3.krw.sum()/2:+,.0f}원 · 노출 {len(z3)}건 × 50% 축소")

print("\n## 손실일(25일) 안에서 N번째 진입의 기여")
pnl = df.groupby("day").krw.sum()
ld = set(pnl[pnl < 0].index)
zl = df[df.day.isin(ld)]
print("| 순번 | 손실일 내 거래 | 손익 | 러너 |")
print("|---|---|---|---|")
for o in sorted(zl["ord"].unique()):
    z = zl[zl["ord"] == o]
    print(f"| {o} | {len(z)} | {z.krw.sum():+,.0f} | {int(z.run.sum())} |")

print("\n## 연속 손절 2회 직후 당일 중단 — 상세")
cut = []
for d in DAYS:
    seq = df[df.day == d].sort_values("entry").to_dict("records")
    st = 0
    for i, t in enumerate(seq):
        st = st + 1 if (t["sl"] and t["krw"] < 0) else 0
        if st >= 2:
            cut += seq[i + 1:]
            break
c = pd.DataFrame(cut)
if len(c):
    print(f"  차단 {len(c)}건 {c.krw.sum():+,.0f}원 · 러너 {int(c.run.sum())}건 · 순효과 {-c.krw.sum():+,.0f}원")
    print(f"  발동일 {c.day.nunique()}일: {' '.join(sorted(c.day.astype(str).unique()))}")
    for _, r in c.iterrows():
        print(f"    {r['day']} {pd.Timestamp(r['entry']):%H:%M} 순번{r['ord']} {r['rs']} {r['krw']:+,.0f}")

print("\n## 10/02 (참고, 85일 밖)")
t2 = sorted(trades(json.load(open(REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json/REAL_A_20261002.json", encoding="utf-8"))), key=lambda t: t["entry"])
for i, t in enumerate(t2, 1):
    print(f"  순번{i} {t['entry']:%H:%M} {t['dir']} {'+'.join(dict.fromkeys(t['reasons']))} {t['krw']:+,.0f}")
