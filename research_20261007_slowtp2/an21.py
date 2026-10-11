"""AN21: 부품 B (SLOW-TP2) 하네스 재생 검증. READ-ONLY.
   E 베이스라인(UPFASTRS) vs E+SLOW-TP2 를 같은 하네스 파일로 A/B 재생한 결과.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
ROOT = REPO + "/research_20261004_chop_staged"
BRKD = REPO + "/research_20261003_breakout_confirm/wk/out6"
W = os.path.dirname(os.path.abspath(__file__))
O = W + "/wk21"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]

DAYS = sorted({os.path.basename(p)[len("REAL_UPFASTRS_"):-5]
               for p in glob.glob(O + "/REAL_UPFASTRS_*.json")})
miss = [d for d in DAYS if not os.path.exists(f"{O}/REAL_SLOWTP2_{d}.json")]
print(f"# 재생 완료 {len(DAYS)}일 · SLOWTP2 미완 {miss or '없음'}")
DAYS = [d for d in DAYS if d not in miss]

# ── 무결성 ① 베이스라인이 기존 E 결과와 주문단위 동일한가 ─────────────
def stored(d):
    for p in (f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json", f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json",
              f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json", f"{BRKD}/REAL_BRK15_{d}.json"):
        if os.path.exists(p):
            return p
    return None


key = lambda o: [(q["t"], q["side"], q["sym"], q["qty"], q["px"]) for q in o["orders"]]
bad = []
for d in DAYS:
    sp = stored(d)
    if sp is None:
        continue
    if key(json.load(open(sp, encoding="utf-8"))) != key(json.load(open(f"{O}/REAL_UPFASTRS_{d}.json", encoding="utf-8"))):
        bad.append(d)
print(f"# 무결성① 베이스라인 vs 기존 E 저장본 주문 동일: {'PASS' if not bad else 'FAIL ' + str(bad)}")

# ── 무결성 ② 미발동일은 두 변형이 완전히 같아야 한다 ────────────────
rows, nochange, changed = [], [], []
for d in DAYS:
    b = json.load(open(f"{O}/REAL_UPFASTRS_{d}.json", encoding="utf-8"))
    s = json.load(open(f"{O}/REAL_SLOWTP2_{d}.json", encoding="utf-8"))
    tb, tsl = trades(b), trades(s)
    kb, ks = sum(t["krw"] for t in tb), sum(t["krw"] for t in tsl)
    armed = s.get("slow") or []
    same = key(b) == key(s)
    (nochange if same else changed).append(d)
    rows.append(dict(day=d, base=kb, slow=ks, d=ks - kb, nb=len(tb), ns=len(tsl),
                     armed=len(armed), same=same))
R = pd.DataFrame(rows)
print(f"# 무결성② 발동(SLOW_TP2_ARM) 없는 날은 주문 동일: "
      f"{'PASS' if all(R[R.armed == 0].same) else 'FAIL'}")
print(f"  발동일 {int((R.armed > 0).sum())} / 미발동일 {int((R.armed == 0).sum())} · "
      f"주문 바뀐 날 {len(changed)}")

print(f"\n## 결과 ({len(R)}일)")
print(f"  베이스라인 E  {R.base.sum():+,.0f}원")
print(f"  E + SLOW-TP2 {R.slow.sum():+,.0f}원")
print(f"  **차이        {R.d.sum():+,.0f}원**  ({int((R.d>0).sum())}일 개선 / {int((R.d<0).sum())}일 악화)")

print("\n| 일자 | 발동 | 베이스 E | SLOW-TP2 | 차이 | 거래수 |")
print("|---|---|---|---|---|---|")
for _, r in R.sort_values("day").iterrows():
    print(f"| {r.day} | {'O' if r.armed else '-'} | {r.base:+,.0f} | {r.slow:+,.0f} | "
          f"**{r.d:+,.0f}** | {int(r.nb)}->{int(r.ns)} |")

print("\n## 발동일 거래 상세")
for d in R[R.armed > 0].day:
    b = json.load(open(f"{O}/REAL_UPFASTRS_{d}.json", encoding="utf-8"))
    s = json.load(open(f"{O}/REAL_SLOWTP2_{d}.json", encoding="utf-8"))
    print(f"\n### {d}")
    for e in (s.get("slow") or []):
        print(f"   ARM {e['t'][11:16]} (진입 {e['entry'][11:16]} + {e['elapsed']:.0f}분) -> TP2 {e['tp2']:.1f}%")
    print("| 변형 | 진입 | 방향 | 청산 | 보유 | net% | 손익 | 사유 |")
    print("|---|---|---|---|---|---|---|---|")
    for lab, o in (("E", b), ("SLOW", s)):
        for t in trades(o):
            print(f"| {lab} | {t['entry'].strftime('%H:%M')} | {t['dir']} | {t['exit'].strftime('%H:%M')} | "
                  f"{t['hold_min']:.0f}분 | {t['net']:+.2f} | {t['krw']:+,.0f} | "
                  f"{'+'.join(dict.fromkeys(t['reasons']))} |")
R.to_csv(W + "/an21.csv", index=False, encoding="utf-8")
