"""85영업일: A 현행 P3 / B EARLY-PASS / C EARLY-UP-FAST. READ-ONLY.
C 는 UP 만 dist<=0.3 (c2·c3 유지), DOWN 불변. 영향 8일만 재생하고 나머지는 B 와 동일.
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
SEPT = [d for d in DAYS if d.startswith("202609")]
R39 = [d for d in DAYS if d >= "20260801"]
NM = {"A": "A 현행 P3", "B": "B EARLY-PASS", "C": "C EARLY-UP-FAST"}
VS = ("A", "B", "C")
UPD = set(json.load(open(ROOT + "/upfast_days.json", encoding="utf-8"))["days"])


def ap(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def pb(d):
    p = f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json"
    return p if os.path.exists(p) else f"{BRKD}/REAL_BRK15_{d}.json"


def P(v, d):
    if v == "A":
        return ap(d)
    if v == "B":
        return pb(d)
    p = f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json"
    return p if os.path.exists(p) else pb(d)


miss = [d for d in UPD if not os.path.exists(f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json")]
if miss:
    print(f"# 미완료 {len(miss)}일: {' '.join(miss)}")
print(f"# C: 실제 재생 {len(UPD)-len(miss)}일 / 나머지 {85-len(UPD)}일은 구조상 B 와 동일")
T, use, pnl, EV = {}, {}, {}, {}
for v in VS:
    T[v], use[v], pnl[v], EV[v] = [], {}, {}, []
    for d in DAYS:
        p = P(v, d)
        if not os.path.exists(p):
            p = ap(d)
        o = json.load(open(p, encoding="utf-8"))
        tt = trades(o)
        T[v] += tt
        use[v][d] = sum(x["qty"] * x["px"] for x in o["orders"] if x["side"] == "BUY")
        pnl[v][d] = sum(t["krw"] for t in tt)
        EV[v] += [dict(e, day=d) for e in (o.get("brk") or [])]
sa = sum(t["krw"] for t in T["A"] if t["day"] in SEPT)
print(f"# anchor 9월 A {sa:+,.0f}원 (기준 +1,416,214)")
if round(sa) != 1_416_214:
    print("## => INVALID REPLAY"); sys.exit(1)
print("\n## 일일 3,000만원 한도")
print("| 전략 | 초과일 | 최대 일매수액 |\n|---|---|---|")
for v in VS:
    a = np.array([use[v][d] for d in DAYS])
    print(f"| {NM[v]} | **{int((a > CAP*1.001).sum())}일** | {a.max():,.0f} |")


def tab(days, title):
    print(f"\n## {title} ({len(days)}일)")
    print("| 전략 | 총손익 | 복리% | PF | MDD% | 최대1일손실 | 손실일 | 승률% | 거래수 |")
    print("|---|---|---|---|---|---|---|---|---|")
    M = {}
    for v in VS:
        m = M[v] = metrics(days, [t for t in T[v] if t["day"] in days])
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['pf']:.2f} | {m['mdd']:.2f} | "
              f"{m['dmin']:+,.0f} | {m['lossd']} | {m['win']:.1f} | {m['n']} |")
    print(f"  C−A {M['C']['krw']-M['A']['krw']:+,.0f} · **C−B {M['C']['krw']-M['B']['krw']:+,.0f}** "
          f"· P3 보존율 {M['C']['krw']/M['A']['krw']*100:.1f}%")
    return M


tab(DAYS, "85영업일 전체")
tab(SEPT, "9월")
tab(R39, "최근 39일 (08/01~)")
print("\n## 10/01 · 10/02")
print("| 날짜 | " + " | ".join(NM[v] for v in VS) + " |\n|" + "---|" * 4)
for d in ("20261001", "20261002"):
    row = []
    for v in VS:
        try:
            row.append(f"{sum(t['krw'] for t in trades(json.load(open(P(v, d), encoding='utf-8')))):+,.0f}")
        except Exception:
            row.append("—")
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(row) + " |")

print("\n## 즉시전환 8건 각각의 A / B / C 손익")
Am = {t["base"]: t for t in T["A"]}
Bm = {t["base"].replace(":BRK", ""): t for t in T["B"]}
Cm = {t["base"].replace(":BRK", ""): t for t in T["C"]}
bfired = {e["sid"] for e in EV["B"] if e["ev"] == "FIRED" and e.get("executed")}
bexp = {e["sid"] for e in EV["B"] if e["ev"].startswith("EXPIRED")}
cimm = {e["sid"] for e in EV["C"] if e["ev"] == "EARLY_PASS"}
hit = [e for e in EV["B"] if e["ev"] == "EARLY_FAIL" and e["dir"] == "UP_RED"
       and e.get("c2") and e.get("c3") and 0.2 < e.get("dist", 9) <= 0.3]
print("| 일자 | 승인 | dist | B 경로 | C 경로 | A | B | C | C−B |")
print("|---|---|---|---|---|---|---|---|---|")
tb = tc = ta = 0.0
for e in sorted(hit, key=lambda x: x["armed_at"]):
    sid = e["sid"]
    a, b, c = Am.get(sid), Bm.get(sid), Cm.get(sid)
    ak = a["krw"] if a else 0.0
    bk = b["krw"] if b else 0.0
    ck = c["krw"] if c else 0.0
    ta += ak; tb += bk; tc += ck
    br = "돌파" if sid in bfired else ("**폐기**" if sid in bexp else "?")
    cr = "**즉시**" if sid in cimm else ("돌파" if c else "폐기")
    print(f"| {e['day'][4:6]}/{e['day'][6:]} | {pd.Timestamp(e['armed_at']):%H:%M} | {e['dist']:.3f}% | {br} | {cr} | "
          f"{ak:+,.0f} | {bk:+,.0f} | {ck:+,.0f} | **{ck-bk:+,.0f}** |")
print(f"\n  합계 · A {ta:+,.0f} / B {tb:+,.0f} / C {tc:+,.0f} · **C−B {tc-tb:+,.0f}**")

print("\n## 폐기 집합 · DOWN 동일성 검증")
cexp = {e["sid"] for e in EV["C"] if e["ev"].startswith("EXPIRED")}
print(f"  B 폐기 {len(bexp)}건 / C 폐기 {len(cexp)}건 · B 에만 폐기 {len(bexp-cexp)}건 · C 에만 폐기 {len(cexp-bexp)}건")
revived = [s for s in bexp - cexp]
for s in revived:
    a = Am.get(s)
    print(f"    되살아난 폐기건: {s} · A {a['krw'] if a else 0:+,.0f}")
bd = [t for t in T["B"] if t["dir"].startswith("DN")]
cd = [t for t in T["C"] if t["dir"].startswith("DN")]
f = lambda z: sorted((t["day"], t["entry"].isoformat(), t["q"], t["px_in"], round(t["krw"], 2)) for t in z)
print(f"  DOWN 거래 B {len(bd)}건 {sum(t['krw'] for t in bd):+,.0f} / C {len(cd)}건 {sum(t['krw'] for t in cd):+,.0f} "
      f"· **완전동일 {f(bd) == f(cd)}**")
bdly = [e["delay_min"] for e in EV["B"] if e["ev"] == "FIRED" and e.get("executed") and e.get("delay_min") is not None]
bimm = [e for e in EV["B"] if e["ev"] == "EARLY_PASS"]
cdly = [e["delay_min"] for e in EV["C"] if e["ev"] == "FIRED" and e.get("executed") and e.get("delay_min") is not None]
cimm2 = [e for e in EV["C"] if e["ev"] == "EARLY_PASS"]
ball = bdly + [0.0] * len(bimm)
call = cdly + [0.0] * len(cimm2)
print(f"\n## 진입지연\n| 전략 | 체결 | 즉시 | 대기체결 | 평균지연 |\n|---|---|---|---|---|")
print(f"| B | {len(ball)} | {len(bimm)} | {len(bdly)} | {np.mean(ball):.2f}분 |")
print(f"| C | {len(call)} | {len(cimm2)} | {len(cdly)} | **{np.mean(call):.2f}분** |")
print(f"  B 대비 평균 지연 감소 **{np.mean(ball)-np.mean(call):.2f}분**")
