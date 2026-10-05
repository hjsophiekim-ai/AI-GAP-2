"""85영업일: A P3-R0 vs B RS125 vs F RSBRK (RS125 + 오전chop 돌파확인 지연). READ-ONLY."""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades, metrics = ns["trades"], ns["metrics"]
D9 = json.load(open(ROOT + "/days9.json", encoding="utf-8"))
D10 = json.load(open(ROOT + "/days10.json", encoding="utf-8"))
SET = {"B": set(D9["rs"]), "F": set(D10["days"])}
SRC = {"B": (ROOT + "/wk/out9", "RS125"), "F": (ROOT + "/wk/out10", "RSBRK")}
NM = {"A": "A P3-R0", "B": "B RS125", "F": "F RSBRK (RS125 + 오전chop 지연)"}
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
VS = ("A", "B", "F")


def apath(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def ld(v, d):
    if v == "A":
        return json.load(open(apath(d), encoding="utf-8"))
    o, tag = SRC[v]
    p = f"{o}/REAL_{tag}_{d}.json"
    if not os.path.exists(p) and d not in SET[v]:
        p = apath(d)
    return json.load(open(p, encoding="utf-8"))


DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
miss = [d for d in DAYS if d in SET["F"] and not os.path.exists(f"{ROOT}/wk/out10/REAL_RSBRK_{d}.json")]
if miss:
    print(f"# 미완료 {len(miss)}일: {' '.join(miss)}")
RAW = {v: {d: ld(v, d) for d in DAYS} for v in VS}
T = {v: sum((trades(RAW[v][d]) for d in DAYS), []) for v in VS}
BRKEV = sum(([dict(e, day=d) for e in (RAW["F"][d].get("brk") or [])] for d in DAYS), [])
SEPT = [d for d in DAYS if d.startswith("202609")]
sa = sum(t["krw"] for t in T["A"] if t["day"] in SEPT)
print(f"# 대상 {len(DAYS)}일 · anchor 9월 A {sa:+,.0f}원 (기준 +1,416,214)")
if round(sa) != 1_416_214:
    print("## => INVALID REPLAY"); sys.exit(1)


def tab(days, title):
    print(f"\n## {title} ({len(days)}일)")
    print("| 전략 | 총손익 | 복리% | PF | MDD% | 승률% | 거래수 | 손실일 | +2%일 | 최대1일손실 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    M = {}
    for v in VS:
        m = M[v] = metrics(days, [t for t in T[v] if t["day"] in days])
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['pf']:.2f} | {m['mdd']:.2f} | {m['win']:.1f} | "
              f"{m['n']} | {m['lossd']} | {m['d2']} | {m['dmin']:+,.0f} |")
    a = M["A"]
    for v in ("B", "F"):
        b = M[v]
        print(f"  - {NM[v]} − A: 총손익 {b['krw']-a['krw']:+,.0f} · 복리 {b['comp']-a['comp']:+.2f}%p · "
              f"PF {b['pf']-a['pf']:+.2f} · MDD {b['mdd']-a['mdd']:+.2f}%p · 거래수 {b['n']-a['n']:+d}")
    print(f"  - **F − B: {M['F']['krw']-M['B']['krw']:+,.0f}원**")
    return M


tab(DAYS, "85영업일 전체")

print("\n## 오전chop 돌파확인 동작")
arm = [e for e in BRKEV if e["ev"] == "ARMED_AMCHOP"]
fired = [e for e in BRKEV if e["ev"] == "FIRED"]
exp = [e for e in BRKEV if e["ev"].startswith("EXPIRED")]
rsup = [e for e in BRKEV if e["ev"] == "RS_UP"]
print(f"  arm {len(arm)}건 · 돌파진입 {len(fired)}건 · 폐기 {len(exp)}건 · RS 증액 {len(rsup)}건")
amap = {t["base"]: t for t in T["A"]}
fmap = {t["base"].replace(":BRK", ""): t for t in T["F"]}
print("\n| 일자 | 시각 | 방향 | 결과 | A 손익 | F 손익 | Δ |")
print("|---|---|---|---|---|---|---|")
tot = 0.0
for e in sorted(arm, key=lambda x: x["armed_at"]):
    sid = e["sid"]
    a = amap.get(sid)
    f = fmap.get(sid)
    hit = any(x["sid"] == sid and x["ev"] == "FIRED" for x in BRKEV)
    ak = a["krw"] if a else 0.0
    fk = f["krw"] if f else 0.0
    tot += fk - ak
    print(f"| {e['day']} | {pd.Timestamp(e['armed_at']):%H:%M} | {e['dir'][:2]} | "
          f"{'돌파진입' if hit else '**폐기**'} | {ak:+,.0f} | {fk:+,.0f} | {fk-ak:+,.0f} |")
print(f"\n  **오전chop 대상 순효과 {tot:+,.0f}원**")

print("\n## 월별")
print("| 월 | A | B RS125 | F RSBRK | F−B |\n|---|---|---|---|---|")
for mo in sorted({d[:6] for d in DAYS}):
    s = {v: sum(t["krw"] for t in T[v] if t["day"][:6] == mo) for v in VS}
    print(f"| {mo[:4]}-{mo[4:]} | {s['A']:+,.0f} | {s['B']:+,.0f} | {s['F']:+,.0f} | {s['F']-s['B']:+,.0f} |")

print("\n## 구간별")
for lab, days in (("전반기 ~07/31", [d for d in DAYS if d < "20260801"]),
                  ("후반기 08/01~", [d for d in DAYS if d >= "20260801"]),
                  ("최근6일 제외", [d for d in DAYS if d not in RECENT])):
    s = {v: sum(t["krw"] for t in T[v] if t["day"] in days) for v in VS}
    print(f"- {lab} ({len(days)}일): A {s['A']:+,.0f} | B {s['B']:+,.0f} ({s['B']-s['A']:+,.0f}) | "
          f"F {s['F']:+,.0f} ({s['F']-s['A']:+,.0f}) | F−B {s['F']-s['B']:+,.0f}")

print("\n## 견고성")
for v in ("B", "F"):
    dd = np.array([sum(t["krw"] for t in T[v] if t["day"] == d) - sum(t["krw"] for t in T["A"] if t["day"] == d) for d in DAYS])
    rng = np.random.default_rng(0)
    bs = np.array([rng.choice(dd, len(dd)).sum() for _ in range(5000)])
    print(f"- {NM[v]}: 우세 {(dd>0).sum()} / 열세 {(dd<0).sum()} / 동일 {(dd==0).sum()} · 합 {dd.sum():+,.0f} · "
          f"상위2일 제외 {dd.sum()-np.sort(dd)[-2:].sum():+,.0f} · 부트스트랩 P(>A) {(bs>0).mean()*100:.1f}%")

tab(SEPT, "9월 (참고)")
print("\n## 10/01 · 10/02")
print("| 날짜 | A | B RS125 | F RSBRK |\n|---|---|---|---|")
for d in ("20261001", "20261002"):
    row = []
    for v in VS:
        try:
            row.append(f"{sum(t['krw'] for t in trades(ld(v, d))):+,.0f}")
        except Exception:
            row.append("—")
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(row) + " |")
