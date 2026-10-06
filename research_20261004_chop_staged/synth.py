"""종합 분석: 85일 A(P3-R0) 거래 구조 — 손실일은 무엇으로 만들어지고 러너는 어디 있는가.
재생 불필요(기존 A 재생 결과만 사용). READ-ONLY.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
LONG = "0193T0"
sys.path.insert(0, ROOT)
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades, metrics = ns["trades"], ns["metrics"]

DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
T = []
for d in DAYS:
    T += trades(json.load(open(f"{O3}/REAL_A_{d}.json", encoding="utf-8")))
for t in T:
    t["rs"] = "+".join(dict.fromkeys(t["reasons"]))
print(f"# A 85일 {len(T)}거래 {sum(t['krw'] for t in T):+,.0f}원")

print("\n## 1. 청산사유별 (손익 오름차순)")
df = pd.DataFrame([{"rs": t["rs"], "krw": t["krw"], "net": t["net"]} for t in T])
g = df.groupby("rs").agg(건수=("krw", "size"), 합계=("krw", "sum"), 평균=("krw", "mean")).sort_values("합계")
print(g.round(0).to_string())

print("\n## 2. 일별 구조")
daily = {d: [t for t in T if t["day"] == d] for d in DAYS}
pnl = {d: sum(t["krw"] for t in daily[d]) for d in DAYS}
loss_days = [d for d in DAYS if pnl[d] < 0]
print(f"  손실일 {len(loss_days)}일 합계 {sum(pnl[d] for d in loss_days):+,.0f}원 "
      f"/ 수익일 {len(DAYS)-len(loss_days)}일 {sum(pnl[d] for d in DAYS if pnl[d]>=0):+,.0f}원")
nl = [sum(1 for t in daily[d] if t["krw"] < 0) for d in DAYS]
print(f"  하루 손실거래 수 분포: {pd.Series(nl).value_counts().sort_index().to_dict()}")

print("\n## 3. 'N번째 손실거래 이후' 당일 잔여 거래의 손익 (서킷브레이커 상한)")
print("| 기준 | 차단거래 | 차단손익 | 그중 러너(net>=3%) | 러너손익 | 순효과 |")
print("|---|---|---|---|---|---|")
for N in (1, 2, 3):
    cut = []
    for d in DAYS:
        seq = sorted(daily[d], key=lambda t: t["entry"])
        k = 0
        for i, t in enumerate(seq):
            if t["krw"] < 0:
                k += 1
                if k >= N:
                    cut += seq[i + 1:]
                    break
    run = [t for t in cut if t["net"] >= 3.0]
    print(f"| 손실 {N}회 후 중단 | {len(cut)}건 | {sum(t['krw'] for t in cut):+,.0f} | {len(run)}건 | "
          f"{sum(t['krw'] for t in run):+,.0f} | {-sum(t['krw'] for t in cut):+,.0f} |")

print("\n## 4. '당일 누적손실 X% 이하면 중단'")
print("| 기준 | 차단거래 | 차단손익 | 그중 러너 | 순효과 |")
print("|---|---|---|---|---|")
for X in (-0.5, -1.0, -1.5, -2.0):
    cut = []
    for d in DAYS:
        seq = sorted(daily[d], key=lambda t: t["entry"])
        acc = 0.0
        for i, t in enumerate(seq):
            acc += t["krw"]
            if acc / 10_000_000 * 100 <= X:
                cut += seq[i + 1:]
                break
    run = [t for t in cut if t["net"] >= 3.0]
    print(f"| 누적 {X}% | {len(cut)}건 | {sum(t['krw'] for t in cut):+,.0f} | {len(run)}건 | {-sum(t['krw'] for t in cut):+,.0f} |")

print("\n## 5. 연속 손절 2회(B3_SL/STOP_LOSS) 직후 중단")
SL = ("B3_SL", "TIME_WINDOW_STOP_LOSS")
cut = []
for d in DAYS:
    seq = sorted(daily[d], key=lambda t: t["entry"])
    streak = 0
    for i, t in enumerate(seq):
        if any(r in SL for r in t["reasons"]) and t["krw"] < 0:
            streak += 1
        else:
            streak = 0
        if streak >= 2:
            cut += seq[i + 1:]
            break
run = [t for t in cut if t["net"] >= 3.0]
print(f"  차단 {len(cut)}건 {sum(t['krw'] for t in cut):+,.0f}원 · 러너 {len(run)}건 {sum(t['krw'] for t in run):+,.0f}원 "
      f"· 순효과 {-sum(t['krw'] for t in cut):+,.0f}원")

print("\n## 6. 러너(net>=3%)는 하루 중 몇 번째 거래인가")
rows = []
for d in DAYS:
    seq = sorted(daily[d], key=lambda t: t["entry"])
    for i, t in enumerate(seq, 1):
        rows.append({"ord": i, "run": t["net"] >= 3.0, "krw": t["krw"], "loss_before": sum(1 for x in seq[:i-1] if x["krw"] < 0)})
r = pd.DataFrame(rows)
print(r.groupby("ord").agg(거래=("krw", "size"), 러너=("run", "sum"), 합계=("krw", "sum")).to_string())
print("\n  러너 기준 '그 전에 난 손실거래 수':")
print(r[r.run].groupby("loss_before").agg(러너수=("krw", "size"), 러너손익=("krw", "sum")).to_string())
print("\n  전체 거래의 '그 전 손실거래 수'별 손익:")
print(r.groupby("loss_before").agg(거래=("krw", "size"), 러너=("run", "sum"), 합계=("krw", "sum"), 평균=("krw", "mean")).round(0).to_string())
