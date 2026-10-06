"""85영업일: A 현행 P3 / B BREAKOUT15 / C EARLY-PASS-BRK15. READ-ONLY.
일일 3,000만원 누적 매수예산 준수 여부를 실측 검증한다.
"""
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
trades, metrics = ns["trades"], ns["metrics"]
CAP = 30_000_000
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
NM = {"A": "A 현행 P3", "B": "B BREAKOUT15", "C": "C EARLY-PASS-BRK15"}
VS = ("A", "B", "C")
RECENT39 = [d for d in DAYS if d >= "20260801"]
SEPT = [d for d in DAYS if d.startswith("202609")]


def ap(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def P(v, d):
    if v == "A":
        return ap(d)
    if v == "B":
        return f"{BRKD}/REAL_BRK15_{d}.json"
    p = f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json"
    # early-pass 가 한 건도 없는 날은 C == B 이므로 B 결과를 그대로 쓴다
    # (조건식을 사전 계산해 확정한 집합 — ep_plan.json)
    return p if os.path.exists(p) else f"{BRKD}/REAL_BRK15_{d}.json"


import json as _j
_plan = _j.load(open(ROOT + "/ep_plan.json", encoding="utf-8")) if os.path.exists(ROOT + "/ep_plan.json") else {"need": []}
miss = [d for d in _plan.get("need", []) if not os.path.exists(f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json")]
if miss:
    print(f"# 미완료 {len(miss)}일: {' '.join(miss[:10])}")
_sub = [d for d in DAYS if not os.path.exists(f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json")]
print(f"# C: 실제 재생 {len(DAYS)-len(_sub)}일 / early-pass 무발생으로 B 대체 {len(_sub)}일")
T, use, pnl, EV = {}, {}, {}, []
for v in VS:
    T[v], use[v], pnl[v] = [], {}, {}
    for d in DAYS:
        p = P(v, d)
        if not os.path.exists(p):
            p = ap(d)
        o = json.load(open(p, encoding="utf-8"))
        tt = trades(o)
        T[v] += tt
        use[v][d] = sum(x["qty"] * x["px"] for x in o["orders"] if x["side"] == "BUY")
        pnl[v][d] = sum(t["krw"] for t in tt)
        if v == "C":
            EV += [dict(e, day=d) for e in (o.get("brk") or [])]
BEV = []
for d in DAYS:
    p = P("B", d)
    if os.path.exists(p):
        BEV += [dict(e, day=d) for e in (json.load(open(p, encoding="utf-8")).get("brk") or [])]
sa = sum(t["krw"] for t in T["A"] if t["day"] in SEPT)
print(f"# anchor 9월 A {sa:+,.0f}원 (기준 +1,416,214)")
if round(sa) != 1_416_214:
    print("## => INVALID REPLAY"); sys.exit(1)

print("\n## 일일 3,000만원 누적 매수예산 준수 (청산분 재사용 없음)")
print("| 전략 | 초과일 | 최대 일매수액 | 평균 |")
print("|---|---|---|---|")
for v in VS:
    a = np.array([use[v][d] for d in DAYS])
    print(f"| {NM[v]} | **{int((a > CAP*1.001).sum())}일** | {a.max():,.0f} | {a.mean():,.0f} |")


def tab(days, title):
    print(f"\n## {title} ({len(days)}일)")
    print("| 전략 | 총손익 | 복리% | PF | MDD% | 최대1일손실 | 손실일 | 승률% | 거래수 |")
    print("|---|---|---|---|---|---|---|---|---|")
    M = {}
    for v in VS:
        m = M[v] = metrics(days, [t for t in T[v] if t["day"] in days])
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['pf']:.2f} | {m['mdd']:.2f} | "
              f"{m['dmin']:+,.0f} | {m['lossd']} | {m['win']:.1f} | {m['n']} |")
    print(f"  C−A {M['C']['krw']-M['A']['krw']:+,.0f} · **C−B {M['C']['krw']-M['B']['krw']:+,.0f}**")
    return M


tab(DAYS, "85영업일 전체")

print("\n## C 의 진입 경로")
imm = [e for e in EV if e["ev"] == "EARLY_PASS"]
fail = [e for e in EV if e["ev"] == "EARLY_FAIL"]
fired = [e for e in EV if e["ev"] == "FIRED" and e.get("executed")]
exp = [e for e in EV if e["ev"].startswith("EXPIRED")]
print(f"| 경로 | 건수 |\n|---|---|")
print(f"| **즉시진입(EARLY_PASS)** | **{len(imm)}** |")
print(f"| 조건 미달 -> 대기 | {len(fail)} |")
print(f"| 그중 돌파 체결 | {len(fired)} |")
print(f"| 그중 폐기 | {len(exp)} |")
bimm = [e for e in BEV if e["ev"] == "FIRED" and e.get("executed")]
bexp = [e for e in BEV if e["ev"].startswith("EXPIRED")]
print(f"\n  (대조) B BREAKOUT15: 돌파 체결 {len(bimm)} · 폐기 {len(bexp)}")
cd = [e["delay_min"] for e in fired if e.get("delay_min") is not None]
bd = [e["delay_min"] for e in bimm if e.get("delay_min") is not None]
allc = cd + [0.0] * len(imm)
print(f"\n## 진입지연")
print(f"| 전략 | 체결 건수 | 평균 지연(분) |\n|---|---|---|")
print(f"| B | {len(bd)} | {np.mean(bd) if bd else 0:.2f} |")
print(f"| C 전체 | {len(allc)} | **{np.mean(allc) if allc else 0:.2f}** |")
print(f"| C 대기진입만 | {len(cd)} | {np.mean(cd) if cd else 0:.2f} |")
print(f"  **B 대비 평균 지연 감소 {np.mean(bd)-np.mean(allc):.2f}분**" if bd and allc else "")

print("\n## 즉시진입으로 회복한 수익 (C 즉시진입 건을 B 와 1:1 대조)")
Amap = {t["base"]: t for t in T["A"]}
Bmap = {t["base"].replace(":BRK", ""): t for t in T["B"]}
Cmap = {t["base"].replace(":BRK", ""): t for t in T["C"]}
rec = 0.0
rows = []
for e in imm:
    sid = e["sid"]
    b, c, a = Bmap.get(sid), Cmap.get(sid), Amap.get(sid)
    if c is None:
        continue
    bk = b["krw"] if b else 0.0
    rec += c["krw"] - bk
    rows.append((e["day"], e["t"][11:16], e["dir"][:2], a["krw"] if a else 0, bk, c["krw"], c["krw"] - bk))
print(f"  즉시진입 {len(rows)}건 · B 합 {sum(r[4] for r in rows):+,.0f} -> C 합 {sum(r[5] for r in rows):+,.0f} "
      f"· **회복 {rec:+,.0f}원**")
rows.sort(key=lambda r: -abs(r[6]))
print("\n| 일자 | 승인 | 방향 | A | B | C | C−B |\n|---|---|---|---|---|---|---|")
for r in rows[:12]:
    print(f"| {r[0][4:6]}/{r[0][6:]} | {r[1]} | {r[2]} | {r[3]:+,.0f} | {r[4]:+,.0f} | {r[5]:+,.0f} | **{r[6]:+,.0f}** |")

print("\n## 폐기된 거래의 원래 손익 (현행 P3 기준)")
tot = 0.0
nw = nl = 0
for e in exp:
    a = Amap.get(e["sid"])
    if a:
        tot += a["krw"]
        nw += a["krw"] > 0
        nl += a["krw"] <= 0
print(f"  C 폐기 {len(exp)}건 중 A 에 같은 진입 {nw+nl}건 · A 손익 합 **{tot:+,.0f}원** (수익 {nw} / 손실 {nl})")
btot = sum(Amap[e["sid"]]["krw"] for e in bexp if e["sid"] in Amap)
print(f"  (대조) B 폐기 {len(bexp)}건 · A 손익 합 {btot:+,.0f}원")

for lab, days in (("9월", SEPT), ("최근 39일 (08/01~)", RECENT39)):
    tab(days, lab)
print("\n## 10/01 · 10/02")
print("| 날짜 | " + " | ".join(NM[v] for v in VS) + " |\n|" + "---|" * 4)
for d in ("20261001", "20261002"):
    row = []
    for v in VS:
        p = P(v, d)
        try:
            row.append(f"{sum(t['krw'] for t in trades(json.load(open(p, encoding='utf-8')))):+,.0f}")
        except Exception:
            row.append("—")
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(row) + " |")
