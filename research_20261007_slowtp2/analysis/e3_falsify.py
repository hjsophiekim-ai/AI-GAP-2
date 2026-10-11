"""E3: SLOW-TP2 후보 반증 — ① 슬롯 점유 ② 임계 민감도 ③ 집중도 ④ 반대 적용. READ-ONLY."""
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
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
R = pd.read_csv(OUT + "/tp2.csv", dtype={"day": str})
TH = 25.0
S = R[R.hold >= TH].copy()
print(f"# SLOW-TP2 (TP2 4.0% 도달까지 >={TH:.0f}분) 대상 {len(S)}건 · 증분 {S.delta.sum():+,.0f}원")

print("\n## 반증① 슬롯 점유 — 연장이 다음 진입을 먹는가")
print("| 일자 | 기존 청산 | 새 청산(추정) | 같은날 다음 진입 | 충돌 | 그 거래 손익 |")
print("|---|---|---|---|---|---|")
bad = 0.0
for _, r in S.sort_values("day").iterrows():
    tt = trades(json.load(open(ap(r.day), encoding="utf-8")))
    cur = next((t for t in tt if t["entry"].strftime("%H:%M") == r["at"]), None)
    if cur is None:
        continue
    old_x = cur["exit"]
    nxt = [t for t in tt if t["entry"] > old_x]
    # 새 청산시각: 시뮬레이션에서 why 로 역산 -- 보수적으로 '다음 진입 전까지는 못 끝난다' 가정 불가하므로
    # 실제 경로상 도달 시각을 다시 계산하지 않고, 연장 하한(기존 청산)과 다음 진입 간격만 본다.
    gap = (nxt[0]["entry"] - old_x).total_seconds() / 60 if nxt else np.nan
    clash = "확인필요" if (nxt and gap < 90) else "없음"
    if nxt and gap < 90:
        bad += nxt[0]["krw"]
    print(f"| {r.day} | {old_x.strftime('%H:%M')} | {r.why} | "
          f"{nxt[0]['entry'].strftime('%H:%M') if nxt else '-'} | {clash} | "
          f"{nxt[0]['krw']:+,.0f} |" if nxt else
          f"| {r.day} | {old_x.strftime('%H:%M')} | {r.why} | - | 없음 | - |")
print(f"\n→ 90분 내 다음 진입이 있는 건들의 그 다음 거래 손익합: {bad:+,.0f}원 (이만큼이 위험에 노출)")

print("\n## 반증② 임계 민감도")
print("| 임계 | n | 증분 | 승-패 | train | test |")
print("|---|---|---|---|---|---|")
R["seg"] = np.where(R.day < "20260801", "train", "test")
for x in (15, 20, 22, 25, 28, 31, 35, 45):
    s = R[R.hold >= x]
    print(f"| >={x}분 | {len(s)} | {s.delta.sum():+,.0f} | {int((s.delta>0).sum())}-{int((s.delta<0).sum())} | "
          f"{s[s.seg=='train'].delta.sum():+,.0f} | {s[s.seg=='test'].delta.sum():+,.0f} |")

print("\n## 반증③ 집중도 (임계 25분)")
s = S.sort_values("delta", ascending=False)
for k in (1, 2, 3):
    print(f"  상위 {k}건 제외 → {S.delta.sum() - s.delta.head(k).sum():+,.0f}")
print(f"  최악 1건 {s.delta.min():+,.0f} ({s.iloc[-1].day})")

print("\n## 반증④ 거꾸로 — 빠르게 도달한 건(<25분)을 연장하면")
f = R[R.hold < TH]
print(f"  {len(f)}건 {f.delta.sum():+,.0f}원 ({int((f.delta>0).sum())}승 {int((f.delta<0).sum())}패)"
      f" → 느린 건만 고르는 것이 방향을 가른다")

print("\n## 반증⑤ 새 청산사유별")
print(S.groupby("why").agg(n=("delta", "size"), 증분=("delta", "sum")).round(0).to_string())
