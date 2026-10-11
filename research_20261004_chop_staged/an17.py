"""최종 비교: A 현행 P3 / B EARLY-PASS / C EARLY-UP-FAST / E UP-FAST+RS125(한도준수).
핵심 질문 = '수익은 C 수준, 안정성은 B 수준' 이 되는가 + 단일거래 의존도가 낮아졌는가. READ-ONLY.
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
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
NM = {"A": "A 현행 P3", "B": "B EARLY-PASS", "C": "C EARLY-UP-FAST", "E": "E UP-FAST+RS125"}
VS = ("A", "B", "C", "E")
UPD = set(json.load(open(ROOT + "/upfast_days.json", encoding="utf-8"))["days"])
EJ = json.load(open(ROOT + "/upfastrs2_days.json", encoding="utf-8"))
ED = set(EJ["days"])


def ap(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def pb(d):
    p = f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json"
    return p if os.path.exists(p) else f"{BRKD}/REAL_BRK15_{d}.json"


def pc(d):
    p = f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json"
    return p if os.path.exists(p) else pb(d)


def pe(d):
    p = f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json"
    return p if os.path.exists(p) else pc(d)


miss = [d for d in ED if not os.path.exists(f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json")]
if miss:
    print(f"# 미완료 {len(miss)}일: {' '.join(sorted(miss))}")
print(f"# E: 실제 재생 {len(ED)-len(miss)}일 / 나머지 {85-len(ED)}일은 구조상 C 와 동일")
T, pnl, use, EV = {}, {}, {}, {}
for v, f in (("A", ap), ("B", pb), ("C", pc), ("E", pe)):
    T[v], pnl[v], use[v], EV[v] = [], {}, {}, []
    for d in DAYS:
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
    a = np.array([use[v][d] for d in DAYS])
    print(f"| {NM[v]} | **{int((a > CAP*1.001).sum())}일** | {a.max():,.0f} |")


def tab(days, title):
    print(f"\n## {title} ({len(days)}일)")
    print("| 전략 | 총손익 | 복리% | PF | MDD% | 최대1일손실 | 손실일 | 승률% | 거래수 | 복리/\\|MDD\\| |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    M = {}
    for v in VS:
        m = M[v] = metrics(days, [t for t in T[v] if t["day"] in days])
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['pf']:.2f} | {m['mdd']:.2f} | "
              f"{m['dmin']:+,.0f} | {m['lossd']} | {m['win']:.1f} | {m['n']} | {m['comp']/abs(m['mdd']):.1f} |")
    a = M["A"]
    for v in ("B", "C", "E"):
        print(f"  {v}−A {M[v]['krw']-a['krw']:+,.0f} · 보존율 {M[v]['krw']/a['krw']*100:.1f}%")
    print(f"  **E−C {M['E']['krw']-M['C']['krw']:+,.0f} · E−B {M['E']['krw']-M['B']['krw']:+,.0f}**")
    return M


M = tab(DAYS, "85영업일 전체")
tab(SEPT, "9월")
tab(R39, "최근 39일 (08/01~)")
print("\n## 10/01 · 10/02")
print("| 날짜 | " + " | ".join(NM[v] for v in VS) + " |\n|" + "---|" * 5)
for d in ("20261001", "20261002"):
    row = []
    for v, f in (("A", ap), ("B", pb), ("C", pc), ("E", pe)):
        try:
            row.append(f"{sum(t['krw'] for t in trades(json.load(open(f(d), encoding='utf-8')))):+,.0f}")
        except Exception:
            row.append("—")
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(row) + " |")

print("\n## ★ 단일거래 의존도 — 개선 상위 N일 제외 검정 (A 대비)")
print("| 후보 | 제외없음 | 상위1일 제외 | 상위2일 제외 | 상위3일 제외 |")
print("|---|---|---|---|---|")
for v in ("B", "C", "E"):
    dd = {d: pnl[v][d] - pnl["A"][d] for d in DAYS}
    base = sum(dd.values())
    pos = sorted([d for d in DAYS if dd[d] > 0], key=lambda d: -dd[d])
    row = [f"**{base:+,.0f}**"]
    for k in (1, 2, 3):
        row.append(f"{base - sum(dd[d] for d in pos[:k]):+,.0f}")
    print(f"| {NM[v]} | " + " | ".join(row) + " |")

print("\n## 견고성")
rng = np.random.default_rng(0)
for v in ("B", "C", "E"):
    dd = np.array([pnl[v][d] - pnl["A"][d] for d in DAYS])
    bs = np.array([rng.choice(dd, len(dd)).sum() for _ in range(5000)])
    print(f"- {NM[v]}: 우세 {(dd>0).sum()} / 열세 {(dd<0).sum()} / 동일 {(dd==0).sum()} · "
          f"합 {dd.sum():+,.0f} · 부트스트랩 P(>A) {(bs>0).mean()*100:.1f}%")
for lab, days in (("전반기 ~07/31", [d for d in DAYS if d < "20260801"]),
                  ("후반기 08/01~", R39), ("최근6일 제외", [d for d in DAYS if d not in RECENT])):
    print(f"  {lab}: " + " · ".join(f"{v}−A {sum(pnl[v][d]-pnl['A'][d] for d in days):+,.0f}" for v in ("B", "C", "E")))

NB = lambda z: str(z).replace(":BRK", "")
print()
print("## E−C 가 갈린 날")
dec = [(d, pnl["C"][d], pnl["E"][d]) for d in DAYS if abs(pnl["E"][d] - pnl["C"][d]) > 0.5]
print(f"  차이 {len(dec)}일 · 합 {sum(e-c for _,c,e in dec):+,.0f}")
print("| 일자 | C | E | E−C |")
print("|---|---|---|---|")
for d, c, e in sorted(dec, key=lambda r: -abs(r[2]-r[1])):
    print(f"| {d[4:6]}/{d[6:]} | {c:+,.0f} | {e:+,.0f} | **{e-c:+,.0f}** |")
print()
print("## E 에서 RS 증액이 걸린 거래")
rs = [e for e in EV["E"] if e.get("ev") == "RS_UP"]
Am = {NB(t["base"]): t for t in T["A"]}
Cm = {NB(t["base"]): t for t in T["C"]}
Em = {NB(t["base"]): t for t in T["E"]}
ncap = sum(1 for e in rs if e.get("capped"))
print(f"  RS 증액 {len(rs)}건 · 한도로 깎인 것 {ncap}건 · 발동일 {len({e['day'] for e in rs})}일")
print("| 일자 | 시각 | 적용배수 | 수량 C→E | A | C | E | E−C |")
print("|---|---|---|---|---|---|---|---|")
tot = 0.0
for e in sorted(rs, key=lambda x: x["t"]):
    sid = NB(e["sid"])
    a, c, ee = Am.get(sid), Cm.get(sid), Em.get(sid)
    ak = a["krw"] if a else 0.0
    ck = c["krw"] if c else 0.0
    ek = ee["krw"] if ee else 0.0
    tot += ek - ck
    print(f"| {e['day'][4:6]}/{e['day'][6:]} | {e['t'][11:16]} | {e.get('applied', 0):.3f} | "
          f"{c['q'] if c else 0}→{ee['q'] if ee else 0} | {ak:+,.0f} | {ck:+,.0f} | {ek:+,.0f} | **{ek-ck:+,.0f}** |")
tot_day = sum(e-c for _, c, e in dec)
print()
print(f"  RS 증액 거래 직접효과 **{tot:+,.0f}원** / 일자합계 E−C **{tot_day:+,.0f}원**")
print("  (차액 = 증액이 예산을 먼저 써서 같은 날 뒤 거래 수량이 바뀐 간접효과)")
