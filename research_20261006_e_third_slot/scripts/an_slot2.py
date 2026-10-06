"""E 3번째 슬롯 연구 — 9월 + 10/01·10/02·10/06. READ-ONLY 집계.
A = production E (하루 3회)
B = E-2SLOT  (MACD2_TW2_3SLOT_DAILY_CAP=2 — 당일 3번째 신규진입만 금지)
C = E-3RD-QUALITY (3번째 신규진입 판정에서만 Trend Quality 통과선 N1 q3 -> config.QUALITY_SCORE_THRESHOLD 4)
"""
import json, os, sys
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
O = os.path.dirname(os.path.abspath(__file__))
SPD = os.path.dirname(O)
ST = SPD + "/wt_reftp/research_20261004_chop_staged"
src = open(ST + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ST + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
DAYS = [l.split()[0] for l in open(O + "/days.txt", encoding="utf-8") if l.strip()]
CAP_KRW = 30_000_000
VS = {"A": "CAP3", "B": "CAP2", "C": "Q3RD"}
NM = {"A": "A E (3회)", "B": "B E-2SLOT", "C": "C E-3RD-QUALITY"}


def load(v, d):
    p = f"{O}/out/{VS[v]}_{d}.json"
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


miss = [(v, d) for v in VS for d in DAYS if load(v, d) is None]
if miss:
    print("## 미완료", miss); sys.exit(1)
R = {v: {d: load(v, d) for d in DAYS} for v in VS}
T = {v: {d: sorted(trades(R[v][d]), key=lambda t: t["entry"]) for d in DAYS} for v in VS}
key = lambda o: [(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in o["orders"]]

bad = [d for d in DAYS if d != "20261006" and key(json.load(open(
    f"{SPD}/eprod/out_merged/REAL_EPROD_{d}.json", encoding="utf-8"))) != key(R["A"][d])]
print(f"# 재생 유효성: A vs 확정 E parity 주문 완전일치 {len(DAYS) - 1 - len(bad)}/{len(DAYS) - 1}일"
      + (f" — 불일치 {bad}" if bad else ""))
if bad:
    print("## => INVALID REPLAY"); sys.exit(1)
# 3번째 진입이 없는 날은 B·C 가 A 와 같아야 한다(분기점이 3번째 판정 하나뿐)
for v in ("B", "C"):
    odd = [d for d in DAYS if len(T["A"][d]) < 3 and key(R[v][d]) != key(R["A"][d])]
    print(f"# {v}: 3번째 진입 없는 날 A 와 주문 동일 {'OK' if not odd else 'X ' + str(odd)}")

pnl = {v: {d: sum(t["krw"] for t in T[v][d]) for d in DAYS} for v in VS}
use = {v: {d: sum(x["qty"] * x["px"] for x in R[v][d]["orders"] if x["side"] == "BUY") for d in DAYS} for v in VS}


def met(days, v):
    dv = np.array([pnl[v][d] for d in days], dtype=float)
    k = np.array([t["krw"] for d in days for t in T[v][d]], dtype=float)
    r = dv / 1e7 * 100
    eq = np.cumprod(1 + r / 100)
    mdd = (eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100
    return dict(krw=dv.sum(), pf=k[k > 0].sum() / max(-k[k < 0].sum(), 1), mdd=mdd, dmin=dv.min(),
                lossd=int((dv < 0).sum()), n=len(k), win=(k > 0).mean() * 100 if len(k) else 0)


SEPT = [d for d in DAYS if d.startswith("202609")]
for lab, days in (("9월 + 10/01·02·06 전체", DAYS), ("9월", SEPT)):
    print(f"\n## {lab} ({len(days)}일)")
    print("| | 총손익 | PF | MDD% | 최대1일손실 | 손실일 | 승률% | 거래수 |\n|---|---|---|---|---|---|---|---|")
    for v in VS:
        m = met(days, v)
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['pf']:.2f} | {m['mdd']:.2f} | {m['dmin']:+,.0f} | "
              f"{m['lossd']} | {m['win']:.1f} | {m['n']} |")
    a = met(days, "A")["krw"]
    print(f"  B−A {met(days, 'B')['krw'] - a:+,.0f} · C−A {met(days, 'C')['krw'] - a:+,.0f}")
for v in VS:
    print(f"# 일 3,000만원 한도 {NM[v]}: 초과 {sum(use[v][d] > CAP_KRW * 1.001 for d in DAYS)}일 · "
          f"최대 {max(use[v].values()):,.0f}")

print("\n## 3번째 신규진입이 있었던 날 (A 기준)")
print("| 일자 | A 3번째 진입 | 청산 | A 3번째 손익 | C 3번째 | A 일손익 | B 일손익 | C 일손익 | B−A | C−A |\n"
      "|---|---|---|---|---|---|---|---|---|---|")
rows = []
for d in DAYS:
    ta = T["A"][d]
    if len(ta) < 3:
        continue
    t3 = ta[2]
    tc = T["C"][d]
    c3 = ("통과" if len(tc) >= 3 and tc[2]["entry"] == t3["entry"]
          else ("다른 3번째" if len(tc) >= 3 else "차단"))
    rows.append(dict(d=d, k=t3["krw"], c3=c3))
    print(f"| {d[4:6]}/{d[6:]} | {t3['entry'].strftime('%H:%M')} {t3['dir']} | "
          f"{'+'.join(dict.fromkeys(t3['reasons']))} | {t3['krw']:+,.0f} | {c3} | {pnl['A'][d]:+,.0f} | "
          f"{pnl['B'][d]:+,.0f} | {pnl['C'][d]:+,.0f} | {pnl['B'][d]-pnl['A'][d]:+,.0f} | "
          f"{pnl['C'][d]-pnl['A'][d]:+,.0f} |")
k3 = np.array([r["k"] for r in rows])
print(f"\n  A 3번째 거래 {len(k3)}건: 승 {(k3 > 0).sum()} / 패 {(k3 < 0).sum()} · 합계 {k3.sum():+,.0f}")
for v in ("B", "C"):
    blocked = [r for r in rows if v == "B" or r["c3"] != "통과"]
    av = sum(-r["k"] for r in blocked if r["k"] < 0)
    ms = sum(r["k"] for r in blocked if r["k"] > 0)
    tot = sum(pnl[v][d] - pnl["A"][d] for d in DAYS)
    path = tot - sum(-r["k"] for r in blocked)
    print(f"  {NM[v]}: 차단 {len(blocked)}건 · 피한 손실 +{av:,.0f} · 놓친 수익 −{ms:,.0f} · "
          f"경로변화 {path:+,.0f} · **E 대비 순효과 {tot:+,.0f}**")
    gains = sorted([pnl[v][d] - pnl["A"][d] for d in DAYS], reverse=True)
    for n in (1, 2):
        print(f"      개선 상위 {n}일 제외 {tot - sum(gains[:n]):+,.0f}")
print(f"  C 의 3번째 판정: 통과 {sum(r['c3'] == '통과' for r in rows)} / 차단 {sum(r['c3'] == '차단' for r in rows)} "
      f"/ 다른 3번째 {sum(r['c3'] == '다른 3번째' for r in rows)}")

print("\n## 10/01 · 10/02 · 10/06")
for d in ("20261001", "20261002", "20261006"):
    print(f"  {d[4:6]}/{d[6:]}: " + " · ".join(f"{v} {pnl[v][d]:+,.0f} ({len(T[v][d])}건)" for v in VS)
          + (" · 14:13 까지" if d == "20261006" else ""))
