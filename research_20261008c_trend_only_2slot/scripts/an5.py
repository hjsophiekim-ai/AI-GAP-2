"""TREND-ONLY-2SLOT 손익 분해 (공통진입 / 차단 / 신규). READ-ONLY."""
import json, os, sys
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
PK = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2/research_20261008_peaklock/out"
sys.path.insert(0, W)
from feat import trades  # noqa

DAYS = [l.split()[1] for l in open(W + "/jobs_TREND2.txt", encoding="utf-8")]


def ld(v, d):
    ps = ([f"{W}/out/REAL_A1008_{d}.json", f"{W}/out/REAL_UPFASTRS_{d}.json",
           f"{PK}/REAL_UPFASTRS_{d}.json"] if v == "A" else [f"{W}/out/REAL_{v}_{d}.json"])
    for p in ps:
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))


def base(s):
    return s.replace(":BRK", "").split(":TW2")[0]


TA, TB = {}, {}
for d in DAYS:
    for t in trades(ld("A", d)):
        TA[(d, base(t["sid"]))] = t
    for t in trades(ld("TREND2", d)):
        TB[(d, base(t["sid"]))] = t
sh, oA, oB = set(TA) & set(TB), set(TA) - set(TB), set(TB) - set(TA)
dsh = sum(TB[k]["krw"] - TA[k]["krw"] for k in sh)
print(f"① 양쪽 다 진입한 같은 신호 {len(sh)}건 : A {sum(TA[k]['krw'] for k in sh):+,.0f}"
      f" -> C {sum(TB[k]['krw'] for k in sh):+,.0f}  = {dsh:+,.0f}")
lo = [k for k in oA if TA[k]["krw"] < 0]
wi = [k for k in oA if TA[k]["krw"] > 0]
print(f"② C 가 진입하지 않은 A 진입 {len(oA)}건 : 회피 {-sum(TA[k]['krw'] for k in oA):+,.0f}"
      f"  (손실 {len(lo)}건 {sum(TA[k]['krw'] for k in lo):+,.0f} / 수익 {len(wi)}건 {sum(TA[k]['krw'] for k in wi):+,.0f})")
print(f"③ C 에만 있던 진입 {len(oB)}건 : {sum(TB[k]['krw'] for k in oB):+,.0f}")
print(f"합계 C-A = {dsh - sum(TA[k]['krw'] for k in oA) + sum(TB[k]['krw'] for k in oB):+,.0f}")

imp = [k for k in sh if TB[k]["krw"] > TA[k]["krw"]]
wor = [k for k in sh if TB[k]["krw"] < TA[k]["krw"]]
print(f"\n① 내역 : 개선 {len(imp)}건 {sum(TB[k]['krw'] - TA[k]['krw'] for k in imp):+,.0f}"
      f" / 악화 {len(wor)}건 {sum(TB[k]['krw'] - TA[k]['krw'] for k in wor):+,.0f}"
      f" / 동일 {len(sh) - len(imp) - len(wor)}건")
pin = [(TB[k]["px_in"] / TA[k]["px_in"] - 1) * 100 for k in sh]
print(f"① 진입가(ETF 매수가) 불리폭 : 평균 {np.mean(pin):+.3f}% / 중앙 {np.median(pin):+.3f}%"
      f" / 불리 {sum(1 for x in pin if x > 0)}건 유리 {sum(1 for x in pin if x < 0)}건")

print("\n② 차단된 A 진입 목록")
for k in sorted(oA):
    t = TA[k]
    print(f"   {k[0]} {str(t['entry'])[11:16]} net {t['net']:+.2f}% {t['krw']:+,.0f}")

ev = {}
dly = []
for d in DAYS:
    for e in (ld("TREND2", d).get("brk") or []):
        ev[e["ev"]] = ev.get(e["ev"], 0) + 1
        if e["ev"] == "FIRED" and e.get("executed"):
            dly.append(float(e["delay_min"]))
print("\nTREND2 candidate 이벤트:", ev)
print(f"발사 {len(dly)}건 / 진입지연 평균 {np.mean(dly):.2f}분 중앙 {np.median(dly):.2f}분 최대 {max(dly):.1f}분")
