"""D10: 트레일링 러너의 두 반증 — ① 다음 진입 슬롯을 막는가 ② 기간 안정성. READ-ONLY."""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
ROOT = REPO + "/research_20261004_chop_staged"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
OUT = os.path.dirname(os.path.abspath(__file__))
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")

R = pd.read_csv(OUT + "/runner.csv", dtype={"day": str})
D = pd.read_csv(OUT + "/days.csv", dtype={"day": str}).set_index("day")

# ① 청산 후 다음 진입까지의 간격
gaps = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    tt = trades(json.load(open(ap(d), encoding="utf-8")))
    pair = float(D.loc[d, "pair_min_mfe"]) if d in D.index else 0.0
    for i in range(len(tt) - 1):
        gaps.append(dict(day=d, pair=pair,
                         gap=(tt[i + 1]["entry"] - tt[i]["exit"]).total_seconds() / 60,
                         nxt=tt[i + 1]["krw"], rs="+".join(dict.fromkeys(tt[i]["reasons"]))))
G = pd.DataFrame(gaps)
print("## ① 청산 -> 다음 진입 간격 (슬롯 재사용 속도)")
print("| 구간 | n | 중앙값(분) | <=30분 비율 | <=30분 다음거래 손익합 | <=60분 손익합 |")
print("|---|---|---|---|---|---|")
for lab, s in (("오늘형 날", G[G.pair >= 1.0]), ("그외", G[G.pair < 1.0])):
    print(f"| {lab} | {len(s)} | {s.gap.median():.0f} | {(s.gap <= 30).mean()*100:.0f}% | "
          f"{s[s.gap <= 30].nxt.sum():+,.0f} | {s[s.gap <= 60].nxt.sum():+,.0f} |")

print("\n## ② 트레일링 증분의 기간 안정성 (반납 1.5%p, 익절·반대신호 거래 잔량 50%)")
TPL = R.rs.str.contains("TP2_FULL|AFTERNOON_TP|OPPOSITE_SIGNAL|TRAILING_STOP|AFTER_TP1_STOP")
R["seg"] = np.where(R.day < "20260801", "train 5~7월", "test 8~10월")
print("| 구간 | 오늘형 n | 오늘형 증분 | 그외 n | 그외 증분 |")
print("|---|---|---|---|---|")
for seg, s in R[TPL].groupby("seg"):
    a, b = s[s.pair >= 1.0], s[s.pair < 1.0]
    print(f"| {seg} | {len(a)} | {a['g1.5'].sum():+,.0f} | {len(b)} | {b['g1.5'].sum():+,.0f} |")

print("\n## ③ 증분의 집중도 (전체, 반납 1.5%p)")
s = R[TPL].sort_values("g1.5", ascending=False)
tot = s["g1.5"].sum()
print(f"전체 {len(s)}건 증분 {tot:+,.0f}원 · 양수 {int((s['g1.5']>0).sum())}건 / 음수 {int((s['g1.5']<0).sum())}건")
for k in (1, 2, 3, 5):
    print(f"  상위 {k}건 제외 -> {tot - s['g1.5'].head(k).sum():+,.0f}원")
print("\n상위 5건:")
print(s.head(5)[["day", "pair", "rs", "krw", "g1.5"]].round(0).to_string(index=False))
