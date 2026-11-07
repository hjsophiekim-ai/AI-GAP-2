"""안정성 — 판정 시각을 T+3 에서 ±3분 흔들었을 때 MATCH/MISMATCH 가 몇 번 뒤집히는지.
지표를 바꾸면 '언제 찍느냐'에 얼마나 휘둘리는지를 본다(새 임계값 없음, 기준 100 그대로)."""
import csv
import sys
from datetime import datetime, timedelta

import cx_lib as X
from app.trading.macd2.models import Direction

sys.stdout.reconfigure(encoding="utf-8")
OFFS = (-3, -2, -1, 0, 1, 2, 3)
D = Direction.DOWN_BLUE

rows = list(csv.DictReader(open("out_blue.csv", encoding="utf-8-sig")))
by_day = {}
for r in rows:
    by_day.setdefault(r["day"], []).append(r)

tally = {k: [0, 0] for k in ("A", "B60", "F60", "F180")}   # [뒤집힌 후보수, 전체]
print(f"{'날짜':10s} {'T+3':5s} | {'A':14s} {'B60(prod)':14s} {'F60':14s} {'F180':14s}")
for day, rs in by_day.items():
    snap = X.DaySnap(day)
    for r in rs:
        t3 = datetime.strptime(f"{day} {r['t3_at']}", "%Y-%m-%d %H:%M")
        cells = []
        for key in ("A", "B60", "F60", "F180"):
            verdicts = []
            for o in OFFS:
                t = t3 + timedelta(minutes=o)
                v = (snap.cum(t) if key == "A" else
                     snap.mean_cttr(t, 60) if key == "B60" else
                     snap.flow(t, 60) if key == "F60" else snap.flow(t, 180))
                verdicts.append(X.match(D, v))
            n_mis = sum(1 for v in verdicts if v == "MISMATCH")
            flipped = 0 < n_mis < len(verdicts)
            tally[key][0] += flipped
            tally[key][1] += 1
            cells.append(f"{'FLIP' if flipped else '안정':4s}({len(verdicts)-n_mis}일치/{n_mis}불일치)")
        print(f"{day:10s} {r['t3_at']:5s} | " + " ".join(f"{c:14s}" for c in cells))
    del snap

print()
print("±3분 창에서 판정이 뒤집힌 후보 비율")
for k, (f, n) in tally.items():
    print(f"  {k:5s} {f}/{n} = {100*f/n:.1f}%")
