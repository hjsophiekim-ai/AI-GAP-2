"""일 한도 3,000만원 준수판 비교 — A / RS125 / BRK15 / 조합 / 조합CAP. READ-ONLY.

조합CAP 은 RS 가 발동한 날만 재생했고, 나머지 날은 조합(out11) 과 동일하다
(RS 분기 밖에서는 코드가 같으므로 결과가 같다 — 아래에서 실측 대조한다).
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
RSD = set(json.load(open(ROOT + "/days9.json", encoding="utf-8"))["rs"])
HIT = set(json.load(open(ROOT + "/rs_days.json", encoding="utf-8"))["hitdays"])
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
NM = {"A": "A 현행 P3", "RS125": "B RS125 (한도초과)", "BRK15": "C BREAKOUT15",
      "COMB": "D RS125+BRK15 (한도초과)", "CAP": "E RS125CAP+BRK15 (한도준수)"}
VS = ("A", "RS125", "BRK15", "COMB", "CAP")


def ap(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def P(v, d):
    if v == "A":
        return ap(d)
    if v == "RS125":
        p = f"{ROOT}/wk/out9/REAL_RS125_{d}.json"
        return p if os.path.exists(p) else ap(d)
    if v == "BRK15":
        return f"{BRKD}/REAL_BRK15_{d}.json"
    if v == "COMB":
        return f"{ROOT}/wk/out11/REAL_RS125BRK15_{d}.json"
    p = f"{ROOT}/wk/out12/REAL_RS125CAPBRK15_{d}.json"
    return p if os.path.exists(p) else f"{ROOT}/wk/out11/REAL_RS125BRK15_{d}.json"


T, use, pnl = {}, {}, {}
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
SEPT = [d for d in DAYS if d >= "20260901"]
sa = sum(t["krw"] for t in T["A"] if t["day"].startswith("202609"))
print(f"# anchor 9월 A {sa:+,.0f}원 (기준 +1,416,214)")
if round(sa) != 1_416_214:
    print("## => INVALID REPLAY"); sys.exit(1)
# RS 비발동일에서 CAP == COMB 인지 실측 대조
bad = [d for d in DAYS if d not in HIT and os.path.exists(f"{ROOT}/wk/out12/REAL_RS125CAPBRK15_{d}.json")
       and json.load(open(f"{ROOT}/wk/out12/REAL_RS125CAPBRK15_{d}.json", encoding="utf-8"))["orders"]
       != json.load(open(f"{ROOT}/wk/out11/REAL_RS125BRK15_{d}.json", encoding="utf-8"))["orders"]]
print(f"# RS 비발동일 CAP==COMB 대조 불일치: {bad or '없음'}")

print("\n## 일 한도 3,000만원 준수")
print("| 전략 | 초과일 | 최대 일매수액 | 평균 | 85일 총손익 |")
print("|---|---|---|---|---|")
for v in VS:
    a = np.array([use[v][d] for d in DAYS])
    print(f"| {NM[v]} | **{int((a > CAP*1.001).sum())}일** | {a.max():,.0f} | {a.mean():,.0f} | "
          f"{sum(pnl[v].values()):+,.0f} |")


def tab(days, title):
    print(f"\n## {title} ({len(days)}일)")
    print("| 전략 | 총손익 | 복리% | MDD% | 최대1일손실 | 손실일 | 승률% | 거래수 | PF | 복리/\\|MDD\\| | 하위5일 합 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    M = {}
    for v in VS:
        m = M[v] = metrics(days, [t for t in T[v] if t["day"] in days])
        s = sorted(pnl[v][d] for d in days)
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['mdd']:.2f} | {m['dmin']:+,.0f} | "
              f"{m['lossd']} | {m['win']:.1f} | {m['n']} | {m['pf']:.2f} | {m['comp']/abs(m['mdd']):.1f} | {sum(s[:5]):+,.0f} |")
    return M


tab(SEPT, "**9월 + 10월(10/01)** — 요청 구간")
tab(DAYS, "85영업일 전체 (참고)")

print("\n## 한도 클립이 실제로 걸린 거래")
rows = []
for d in sorted(HIT):
    p = f"{ROOT}/wk/out12/REAL_RS125CAPBRK15_{d}.json"
    if not os.path.exists(p):
        continue
    for e in (json.load(open(p, encoding="utf-8")).get("brk") or []):
        if e.get("ev") == "RS_UP":
            rows.append(dict(day=d, t=e["t"][11:19], want=e.get("want"), room=e.get("room"),
                             applied=e.get("applied"), capped=e.get("capped")))
R = pd.DataFrame(rows)
if len(R):
    print(f"  RS 증액 {len(R)}건 중 **한도로 깎인 것 {int(R.capped.sum())}건**")
    print("| 일자 | 시각 | 원하는 배수 | 남은 room | 실제 적용 | 클립 |")
    print("|---|---|---|---|---|---|")
    for _, r in R.iterrows():
        print(f"| {r['day'][4:6]}/{r['day'][6:]} | {r['t']} | {r['want']:.3f} | {r['room']:.3f} | "
              f"**{r['applied']:.3f}** | {'**O**' if r['capped'] else '-'} |")

print("\n## 9월+10월 일별")
print("| 일자 | A | BRK15 | 조합CAP | CAP−A |\n|---|---|---|---|---|")
for d in SEPT:
    print(f"| {d[4:6]}/{d[6:]} | {pnl['A'][d]:+,.0f} | {pnl['BRK15'][d]:+,.0f} | {pnl['CAP'][d]:+,.0f} | {pnl['CAP'][d]-pnl['A'][d]:+,.0f} |")
print("\n## 10/02 (참고)")
r = []
for v in VS:
    p = P(v, "20261002")
    try:
        r.append(f"{sum(t['krw'] for t in trades(json.load(open(p, encoding='utf-8')))):+,.0f}")
    except Exception:
        r.append("—")
print("| " + " | ".join(NM[v] for v in VS) + " |\n|" + "---|" * 5)
print("| " + " | ".join(r) + " |")
