"""9월 + 10/01~10/02 에서 EARLY-PASS 가 현행 P3 대비 왜 졌는지 거래별 분해. READ-ONLY."""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
BRKD = REPO + "/research_20261003_breakout_confirm/wk/out6"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]


def ap(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def pc(d):
    p = f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json"
    return p if os.path.exists(p) else f"{BRKD}/REAL_BRK15_{d}.json"


have = {os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}
DAYS = sorted([d for d in have if d >= "20260901"]) + ["20261002"]
A, C, EV = {}, {}, []
for d in DAYS:
    for t in trades(json.load(open(ap(d), encoding="utf-8"))):
        A[t["base"]] = t
    o = json.load(open(pc(d), encoding="utf-8"))
    EV += [dict(e, day=d) for e in (o.get("brk") or [])]
    for t in trades(o):
        C[t["base"].replace(":BRK", "")] = t
imm = {e["sid"] for e in EV if e["ev"] == "EARLY_PASS"}
exp = {e["sid"] for e in EV if e["ev"].startswith("EXPIRED")}
fired = {e["sid"]: e for e in EV if e["ev"] == "FIRED"}

ak = sum(t["krw"] for t in A.values())
ck = sum(t["krw"] for t in C.values())
print(f"# 9월+10/01+10/02 · A {len(A)}건 {ak:+,.0f} · C {len(C)}건 {ck:+,.0f} · Δ **{ck-ak:+,.0f}**\n")

both = [k for k in C if k in A]
conly = [k for k in C if k not in A]
aonly = [k for k in A if k not in C]
d_both = sum(C[k]["krw"] - A[k]["krw"] for k in both)
d_conly = sum(C[k]["krw"] for k in conly)
d_aonly = -sum(A[k]["krw"] for k in aonly)
print("## 분해 (합계가 Δ 와 일치)")
print("| 구성 | 건수 | 기여 |\n|---|---|---|")
print(f"| ① 양쪽 다 체결 (같은 signal) | {len(both)} | **{d_both:+,.0f}** |")
print(f"| ② A 에만 있던 진입 (폐기 등) | {len(aonly)} | **{d_aonly:+,.0f}** |")
print(f"| ③ C 에만 있던 진입 (슬롯 연쇄) | {len(conly)} | **{d_conly:+,.0f}** |")
print(f"| **합계** | | **{d_both+d_aonly+d_conly:+,.0f}** |")

print("\n## ② A 에만 있던 진입 — 폐기/미진입")
tot = 0
for k in sorted(aonly, key=lambda x: A[x]["entry"]):
    t = A[k]
    why = "폐기" if k in exp else "미진입"
    tot += t["krw"]
    print(f"  {t['day'][4:6]}/{t['day'][6:]} {t['entry']:%H:%M} {t['dir']} [{why}] "
          f"{'+'.join(dict.fromkeys(t['reasons']))} {t['krw']:+,.0f}")
print(f"  합 {tot:+,.0f} → C 가 피한 금액 {-tot:+,.0f}")

print("\n## ③ C 에만 있던 진입 — 슬롯이 비어 새로 생긴 거래")
for k in sorted(conly, key=lambda x: C[x]["entry"]):
    t = C[k]
    print(f"  {t['day'][4:6]}/{t['day'][6:]} {t['entry']:%H:%M} {t['dir']} "
          f"{'+'.join(dict.fromkeys(t['reasons']))} {t['krw']:+,.0f}")

print("\n## ① 양쪽 체결 — 손실 상위 (원인 분해)")
rows = []
for k in both:
    a, c = A[k], C[k]
    f = fired.get(k)
    rows.append(dict(day=a["day"], t=a["entry"], dirn=a["dir"],
                     route=("즉시" if k in imm else ("돌파" if f else "?")),
                     delay=(f["delay_min"] if f else 0.0),
                     aq=a["q"], cq=c["q"], apx=a["px_in"], cpx=c["px_in"],
                     arz="+".join(dict.fromkeys(a["reasons"])), crz="+".join(dict.fromkeys(c["reasons"])),
                     ak=a["krw"], ck=c["krw"], d=c["krw"] - a["krw"]))
R = pd.DataFrame(rows).sort_values("d")
print("| 일자 | 진입 | 경로 | 지연 | 수량 A→C | 진입가 A→C | 청산 A | 청산 C | A | C | Δ |")
print("|---|---|---|---|---|---|---|---|---|---|---|")
for _, r in R.head(10).iterrows():
    print(f"| {r['day'][4:6]}/{r['day'][6:]} | {r['t']:%H:%M} | {r['route']} | {r['delay']:.1f} | "
          f"{int(r['aq']):,}→{int(r['cq']):,} | {r['apx']:,.0f}→{r['cpx']:,.0f} | {r['arz']} | {r['crz']} | "
          f"{r['ak']:+,.0f} | {r['ck']:+,.0f} | **{r['d']:+,.0f}** |")
print("\n  (개선 상위)")
for _, r in R.tail(5).iloc[::-1].iterrows():
    print(f"| {r['day'][4:6]}/{r['day'][6:]} | {r['t']:%H:%M} | {r['route']} | {r['delay']:.1f} | "
          f"{int(r['aq']):,}→{int(r['cq']):,} | {r['apx']:,.0f}→{r['cpx']:,.0f} | {r['arz']} | {r['crz']} | "
          f"{r['ak']:+,.0f} | {r['ck']:+,.0f} | **{r['d']:+,.0f}** |")

print("\n## ① 을 원인별로 다시 나누면")
same_px = R[(R.apx == R.cpx)]
diff_px = R[(R.apx != R.cpx)]
same_q = R[(R.aq == R.cq)]
print(f"  진입가 동일 {len(same_px)}건 Δ {same_px.d.sum():+,.0f} / 진입가 다름 {len(diff_px)}건 Δ {diff_px.d.sum():+,.0f}")
print(f"  수량 동일 {len(same_q)}건 Δ {same_q.d.sum():+,.0f} / 수량 다름 {len(R)-len(same_q)}건 Δ {R[R.aq!=R.cq].d.sum():+,.0f}")
print(f"  즉시진입 {int((R.route=='즉시').sum())}건 Δ {R[R.route=='즉시'].d.sum():+,.0f} / "
      f"돌파진입 {int((R.route=='돌파').sum())}건 Δ {R[R.route=='돌파'].d.sum():+,.0f}")
chg = R[R.arz != R.crz]
print(f"  청산사유 달라진 거래 {len(chg)}건 Δ {chg.d.sum():+,.0f} / 같은 거래 {len(R)-len(chg)}건 Δ {R[R.arz==R.crz].d.sum():+,.0f}")
R.to_csv(ROOT + "/sept_decomp.csv", index=False, encoding="utf-8")
