"""9월 + 10/01~10/02: A 현행 P3 / B EARLY-PASS / C EARLY-PASS+REFERENCE-TP.
C 는 돌파대기 후 체결된 거래만 익절 기준가격을 승인시점 참조가로 바꾼다. READ-ONLY.
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
ALL = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
SEPT = [d for d in ALL if d.startswith("202609")]
TGT = SEPT + ["20261001", "20261002"]
J = json.load(open(ROOT + "/reftp_days.json", encoding="utf-8"))
WAIT = list(J["days"])
NM = {"A": "A 현행 P3", "B": "B EARLY-PASS", "C": "C EARLY-PASS+REF-TP"}
VS = ("A", "B", "C")


def ap(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def pb(d):
    p = f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json"
    return p if os.path.exists(p) else f"{BRKD}/REAL_BRK15_{d}.json"


def pc(d):
    p = f"{ROOT}/wk/out18/REAL_REFTP_{d}.json"
    return p if os.path.exists(p) else pb(d)


miss = [d for d in WAIT if not os.path.exists(f"{ROOT}/wk/out18/REAL_REFTP_{d}.json")]
if miss:
    print(f"# 미완료 {len(miss)}일: {' '.join(miss)}")
print(f"# 대상 {len(TGT)}일 (9월 {len(SEPT)} + 10/01 + 10/02)")
print(f"# C 실제 재생 {len(WAIT) - len(miss)}일 — 돌파대기 체결이 있는 날만. "
      f"나머지 {len(TGT) - len(WAIT)}일은 대기체결이 없어 구조상 B 와 동일")

T, pnl, use, EV = {}, {}, {}, {}
for v, f in (("A", ap), ("B", pb), ("C", pc)):
    T[v], pnl[v], use[v], EV[v] = [], {}, {}, []
    for d in TGT:
        o = json.load(open(f(d), encoding="utf-8"))
        tt = trades(o)
        T[v] += tt
        pnl[v][d] = sum(t["krw"] for t in tt)
        use[v][d] = sum(x["qty"] * x["px"] for x in o["orders"] if x["side"] == "BUY")
        EV[v] += [dict(e, day=d) for e in (o.get("brk") or [])]

sa = sum(t["krw"] for t in T["A"] if t["day"] in SEPT)
print(f"# anchor 9월 A {sa:+,.0f}원 (기준 +1,416,214)")
if round(sa) != 1_416_214:
    print("## => INVALID REPLAY"); sys.exit(1)

print("\n## 일일 3,000만원 한도")
print("| 전략 | 초과일 | 최대 일매수액 |\n|---|---|---|")
for v in VS:
    a = np.array([use[v][d] for d in TGT])
    print(f"| {NM[v]} | **{int((a > CAP*1.001).sum())}일** | {a.max():,.0f} |")


def tab(days, title):
    print(f"\n## {title} ({len(days)}일)")
    print("| 전략 | 총손익 | PF | MDD% | 최대1일손실 | 손실일 | 승률% | 거래수 |")
    print("|---|---|---|---|---|---|---|---|")
    M = {}
    for v in VS:
        m = M[v] = metrics(days, [t for t in T[v] if t["day"] in days])
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['pf']:.2f} | {m['mdd']:.2f} | "
              f"{m['dmin']:+,.0f} | {m['lossd']} | {m['win']:.1f} | {m['n']} |")
    a = M["A"]
    for v in ("B", "C"):
        print(f"  {v}−A {M[v]['krw']-a['krw']:+,.0f} · 보존율 {M[v]['krw']/a['krw']*100:.1f}%")
    print(f"  **C−B {M['C']['krw']-M['B']['krw']:+,.0f}**")
    return M


tab(SEPT, "9월")
tab(TGT, "9월 + 10/01~10/02")

print("\n## 10/01 · 10/02")
print("| 날짜 | " + " | ".join(NM[v] for v in VS) + " |\n|" + "---|" * 4)
for d in ("20261001", "20261002"):
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(f"{pnl[v][d]:+,.0f}" for v in VS) + " |")

print("\n## 일자별 (C 가 B 와 갈린 날은 ★)")
print("| 일자 | A | B | C | C−B | C−A |\n|---|---|---|---|---|---|")
for d in TGT:
    mark = " ★" if abs(pnl["C"][d] - pnl["B"][d]) > 0.5 else ""
    print(f"| {d[4:6]}/{d[6:]}{mark} | {pnl['A'][d]:+,.0f} | {pnl['B'][d]:+,.0f} | "
          f"{pnl['C'][d]:+,.0f} | **{pnl['C'][d]-pnl['B'][d]:+,.0f}** | {pnl['C'][d]-pnl['A'][d]:+,.0f} |")

print("\n## ★ 돌파대기 체결 거래별 A / B / C")
NB = lambda z: str(z).replace(":BRK", "")
Am = {NB(t["base"]): t for t in T["A"]}
Bm = {NB(t["base"]): t for t in T["B"]}
Cm = {NB(t["base"]): t for t in T["C"]}
fired = [e for e in EV["C"] if e["ev"] == "FIRED" and e.get("executed")]
print("| 일자 | 승인 | 체결 | 방향 | 지연 | 참조가 | 체결가 | 괴리 | A | B | C | C−B |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|")
tot_b = tot_c = tot_a = 0.0
for e in sorted(fired, key=lambda x: x["armed_at"]):
    sid = NB(e["sid"])
    a, b, c = Am.get(sid), Bm.get(sid), Cm.get(sid)
    ak = a["krw"] if a else 0.0
    bk = b["krw"] if b else 0.0
    ck = c["krw"] if c else 0.0
    tot_a += ak; tot_b += bk; tot_c += ck
    rp = e.get("ref_px")
    fp = c["px_in"] if c else None
    gap = ((fp - rp) / rp * 100.0) if (rp and fp) else float("nan")
    print(f"| {e['day'][4:6]}/{e['day'][6:]} | {str(e['armed_at'])[11:16]} | {str(e['at'])[11:16]} | "
          f"{'UP' if e['dir']=='UP_RED' else 'DN'} | {float(e.get('delay_min') or 0):.1f}분 | "
          f"{(rp or 0):,.0f} | {(fp or 0):,.0f} | {gap:+.2f}% | "
          f"{ak:+,.0f} | {bk:+,.0f} | {ck:+,.0f} | **{ck-bk:+,.0f}** |")
print(f"\n  합계 · A {tot_a:+,.0f} / B {tot_b:+,.0f} / C {tot_c:+,.0f} · **C−B {tot_c-tot_b:+,.0f}**")

print("\n## 청산 경로 변화 (돌파대기 체결 거래)")
print("| 일자 | 승인 | B 청산사유 | B 손익 | C 청산사유 | C 손익 |")
print("|---|---|---|---|---|---|")
for e in sorted(fired, key=lambda x: x["armed_at"]):
    sid = NB(e["sid"])
    b, c = Bm.get(sid), Cm.get(sid)
    br = "+".join(dict.fromkeys(b["reasons"])) if b else "미진입"
    cr = "+".join(dict.fromkeys(c["reasons"])) if c else "미진입"
    print(f"| {e['day'][4:6]}/{e['day'][6:]} | {str(e['armed_at'])[11:16]} | {br} | "
          f"{(b['krw'] if b else 0):+,.0f} | {cr} | {(c['krw'] if c else 0):+,.0f} |")

print("\n## 청산사유 분포 (대상 20일 전체)")
rs = sorted({r for v in VS for t in T[v] for r in t["reasons"]})
print("| 청산사유 | " + " | ".join(f"{NM[v]} 건/손익" for v in VS) + " |")
print("|---|" + "---|" * 3)
for r in rs:
    cells = []
    for v in VS:
        sel = [t for t in T[v] if r in t["reasons"]]
        cells.append(f"{len(sel)} / {sum(t['krw'] for t in sel):+,.0f}")
    print(f"| {r} | " + " | ".join(cells) + " |")

print("\n## 폐기 집합 — 기존 폐기 손실거래가 되살아났는가")
for v in ("B", "C"):
    exp = {e["sid"] for e in EV[v] if e["ev"].startswith("EXPIRED")}
    print(f"  {NM[v]} 폐기 {len(exp)}건")
bexp = {e["sid"] for e in EV["B"] if e["ev"].startswith("EXPIRED")}
cexp = {e["sid"] for e in EV["C"] if e["ev"].startswith("EXPIRED")}
print(f"  B 에만 폐기(=C 에서 되살아남) {len(bexp-cexp)}건 · C 에만 폐기 {len(cexp-bexp)}건")
for s in sorted(bexp - cexp):
    t = Am.get(NB(s))
    print(f"    되살아남: {s} · A 손익 {(t['krw'] if t else 0):+,.0f}")
if not (bexp - cexp):
    print("  => 되살아난 폐기건 **없음** (C 는 진입/폐기 로직을 건드리지 않는다)")
