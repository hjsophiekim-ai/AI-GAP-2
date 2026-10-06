"""85영업일 사이징 후보 비교 — A / RS125 / RS150 / SLOT3 / RSCUT. READ-ONLY.

손익 = production 거래원장 net_pnl. 일 수익률 = 원 / 1,000만원.
조건이 걸리지 않는 날은 후보가 A 와 동일하므로 A 재생 결과를 쓴다(무발동 3일은 실제 대조).
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O9 = ROOT + "/wk/out9"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades, metrics = ns["trades"], ns["metrics"]
D9 = json.load(open(ROOT + "/days9.json", encoding="utf-8"))
SET = {"B": set(D9["rs"]), "C": set(D9["rs"]), "D": set(D9["s3"]), "E": set(D9["cut"])}
TAG = {"B": "RS125", "C": "RS150", "D": "SLOT3", "E": "RSCUT"}
NM = {"A": "A P3-R0", "B": "B RS125 (러너 ×1.25)", "C": "C RS150 (러너 ×1.5)",
      "D": "D SLOT3 (3슬롯 ×0.5)", "E": "E RSCUT (러너 ×1.5 + 왕복장 ×0.5)"}
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
VS = ("A", "B", "C", "D", "E")


def apath(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def ld(v, d):
    if v == "A":
        return json.load(open(apath(d), encoding="utf-8"))
    p = f"{O9}/REAL_{TAG[v]}_{d}.json"
    if not os.path.exists(p) and d not in SET[v]:
        p = apath(d)
    return json.load(open(p, encoding="utf-8"))


DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
miss = {v: [d for d in DAYS if d in SET[v] and not os.path.exists(f"{O9}/REAL_{TAG[v]}_{d}.json")]
        for v in ("B", "C", "D", "E")}
for v, m in miss.items():
    if m:
        print(f"# 미완료 {TAG[v]} {len(m)}일: {' '.join(m[:8])} ...")
RAW = {v: {d: ld(v, d) for d in DAYS} for v in VS}
T = {v: sum((trades(RAW[v][d]) for d in DAYS), []) for v in VS}
SZ = {v: sum(([dict(e, day=d) for e in (RAW[v][d].get("size") or [])] for d in DAYS), []) for v in ("B", "C", "D", "E")}
SEPT = [d for d in DAYS if d.startswith("202609")]

sa = sum(t["krw"] for t in T["A"] if t["day"] in SEPT)
print(f"# 대상 {len(DAYS)}일 ({DAYS[0]}~{DAYS[-1]})")
print(f"# anchor: 9월 A {sa:+,.0f}원 (기준 +1,416,214)")
bad = []
for d in D9["untouched"]:
    p = f"{O9}/REAL_RS150_{d}.json"
    if not os.path.exists(p):
        continue
    x, y = json.load(open(p, encoding="utf-8")), RAW["A"][d]
    f = lambda o: [(q["t"], q["side"], q["sym"], q["qty"], q["px"]) for q in o["orders"]]
    if f(x) != f(y):
        bad.append(d)
print(f"# 무발동 동일성 실측검증 {D9['untouched']} · 불일치 {bad or '없음'}")
if round(sa) != 1_416_214 or bad:
    print("## => INVALID REPLAY"); sys.exit(1)


def tab(days, title):
    print(f"\n## {title} ({len(days)}일)")
    print("| 전략 | 총손익 | 복리% | PF | MDD% | 승률% | 거래수 | 손실일 | +2%일 | 최대1일손실 | 최대1일수익 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    M = {}
    for v in VS:
        m = M[v] = metrics(days, [t for t in T[v] if t["day"] in days])
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['pf']:.2f} | {m['mdd']:.2f} | {m['win']:.1f} | "
              f"{m['n']} | {m['lossd']} | {m['d2']} | {m['dmin']:+,.0f} | {m['dmax']:+,.0f} |")
    a = M["A"]
    print("\n  A 대비")
    for v in ("B", "C", "D", "E"):
        b = M[v]
        print(f"  - {NM[v]}: 총손익 {b['krw']-a['krw']:+,.0f} · 복리 {b['comp']-a['comp']:+.2f}%p · "
              f"PF {b['pf']-a['pf']:+.2f} · MDD {b['mdd']-a['mdd']:+.2f}%p · "
              f"최대1일손실 {b['dmin']-a['dmin']:+,.0f} · 손실일 {b['lossd']-a['lossd']:+d}")
    return M


M = tab(DAYS, "85영업일 전체")

print("\n## 조건 발동")
print("| 후보 | 발동 | 증액 | 감액 | 발동거래 A손익 | 발동거래 후보손익 | Δ |")
print("|---|---|---|---|---|---|---|")
amap = {(t["day"], t["entry"]): t for t in T["A"]}
for v in ("B", "C", "D", "E"):
    ev = SZ[v]
    up = [e for e in ev if e["mult"] > 1]
    dn = [e for e in ev if e["mult"] < 1]
    vmap = {(t["day"], t["entry"]): t for t in T[v]}
    ka, kb = 0.0, 0.0
    for e in ev:
        key = None
        for (d, en) in vmap:
            if d == e["day"] and abs((en - pd.Timestamp(e["t"])).total_seconds()) < 90:
                key = (d, en); break
        if key and key in vmap:
            kb += vmap[key]["krw"]
        ak = [t for t in T["A"] if t["day"] == e["day"] and abs((t["entry"] - pd.Timestamp(e["t"])).total_seconds()) < 90]
        if ak:
            ka += ak[0]["krw"]
    print(f"| {NM[v]} | {len(ev)} | {len(up)} | {len(dn)} | {ka:+,.0f} | {kb:+,.0f} | {kb-ka:+,.0f} |")

print("\n## 월별")
print("| 월 | A | B RS125 | C RS150 | D SLOT3 | E RSCUT |\n|---|---|---|---|---|---|")
for mo in sorted({d[:6] for d in DAYS}):
    s = {v: sum(t["krw"] for t in T[v] if t["day"][:6] == mo) for v in VS}
    print(f"| {mo[:4]}-{mo[4:]} | " + " | ".join(f"{s[v]:+,.0f}" for v in VS) + " |")
print("\n  (A 대비 차이)")
print("| 월 | B−A | C−A | D−A | E−A |\n|---|---|---|---|---|")
for mo in sorted({d[:6] for d in DAYS}):
    s = {v: sum(t["krw"] for t in T[v] if t["day"][:6] == mo) for v in VS}
    print(f"| {mo[:4]}-{mo[4:]} | " + " | ".join(f"{s[v]-s['A']:+,.0f}" for v in ("B", "C", "D", "E")) + " |")

print("\n## 구간별")
H1 = [d for d in DAYS if d < "20260801"]
H2 = [d for d in DAYS if d >= "20260801"]
NR = [d for d in DAYS if d not in RECENT]
ACT = [d for d in DAYS if d >= "20260618"]        # RS 규칙 활성 구간
for lab, days in (("전반기 ~07/31", H1), ("후반기 08/01~", H2), ("최근6일 제외", NR), ("RS 활성구간 06/18~", ACT)):
    s = {v: sum(t["krw"] for t in T[v] if t["day"] in days) for v in VS}
    print(f"- {lab} ({len(days)}일): A {s['A']:+,.0f} | " +
          " | ".join(f"{TAG[v]} {s[v]:+,.0f} ({s[v]-s['A']:+,.0f})" for v in ("B", "C", "D", "E")))

print("\n## 견고성 (일별 Δ)")
for v in ("B", "C", "D", "E"):
    dd = np.array([sum(t["krw"] for t in T[v] if t["day"] == d) - sum(t["krw"] for t in T["A"] if t["day"] == d) for d in DAYS])
    rng = np.random.default_rng(0)
    bs = np.array([rng.choice(dd, len(dd)).sum() for _ in range(5000)])
    top2 = np.sort(dd)[-2:].sum()
    print(f"- {NM[v]}: 우세 {(dd>0).sum()} / 열세 {(dd<0).sum()} / 동일 {(dd==0).sum()} · 합 {dd.sum():+,.0f} · "
          f"상위2일 제외 {dd.sum()-top2:+,.0f} · 부트스트랩 P(>A) {(bs>0).mean()*100:.1f}%")

tab(SEPT, "9월 (참고)")
print("\n## 10/01 · 10/02")
print("| 날짜 | " + " | ".join(NM[v] for v in VS) + " |\n|" + "---|" * 6)
for d in ("20261001", "20261002"):
    row = []
    for v in VS:
        try:
            row.append(f"{sum(t['krw'] for t in trades(ld(v, d))):+,.0f}")
        except Exception:
            row.append("—")
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(row) + " |")
