"""85영업일: A / RS125 / BRK15 / RS125+BRK15 조합 비교. READ-ONLY.
판정 기준은 사전 고정: ① 복리/|MDD| ② 하위10일 합. 총손익은 참고.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
BRK = REPO + "/research_20261003_breakout_confirm/wk/out6"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades, metrics = ns["trades"], ns["metrics"]
RSD = set(json.load(open(ROOT + "/days9.json", encoding="utf-8"))["rs"])
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
NM = {"A": "A P3-R0", "RS125": "B RS125", "BRK15": "C BREAKOUT15", "COMB": "D RS125+BRK15"}
VS = ("A", "RS125", "BRK15", "COMB")
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]


def ap(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def path(v, d):
    if v == "A":
        return ap(d)
    if v == "RS125":
        p = f"{ROOT}/wk/out9/REAL_RS125_{d}.json"
        return p if os.path.exists(p) else (ap(d) if d not in RSD else p)
    if v == "BRK15":
        return f"{BRK}/REAL_BRK15_{d}.json"
    return f"{ROOT}/wk/out11/REAL_RS125BRK15_{d}.json"


miss = [d for d in DAYS if not os.path.exists(path("COMB", d))]
if miss:
    print(f"# 미완료 {len(miss)}일: {' '.join(miss[:10])}")
T, BRKEV = {}, []
for v in VS:
    acc = []
    for d in DAYS:
        p = path(v, d)
        if not os.path.exists(p):
            p = ap(d)
        o = json.load(open(p, encoding="utf-8"))
        acc += trades(o)
        if v == "COMB":
            BRKEV += [dict(e, day=d) for e in (o.get("brk") or [])]
    T[v] = acc
SEPT = [d for d in DAYS if d.startswith("202609")]
sa = sum(t["krw"] for t in T["A"] if t["day"] in SEPT)
print(f"# 대상 {len(DAYS)}일 · anchor 9월 A {sa:+,.0f}원 (기준 +1,416,214)")
if round(sa) != 1_416_214:
    print("## => INVALID REPLAY"); sys.exit(1)

M = {v: metrics(DAYS, T[v]) for v in VS}
dl = {v: {d: sum(t["krw"] for t in T[v] if t["day"] == d) for d in DAYS} for v in VS}
print("\n## 85영업일 전체 — 사전 고정 기준 굵게")
print("| 전략 | 총손익 | 복리% | MDD% | 최대1일손실 | 손실일 | 승률% | 거래수 | PF | **복리/\\|MDD\\|** | **하위10일 합** |")
print("|---|---|---|---|---|---|---|---|---|---|---|")
for v in VS:
    m = M[v]; s = sorted(dl[v].values())
    print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['mdd']:.2f} | {m['dmin']:+,.0f} | {m['lossd']} | "
          f"{m['win']:.1f} | {m['n']} | {m['pf']:.2f} | **{m['comp']/abs(m['mdd']):.1f}** | **{sum(s[:10]):+,.0f}** |")
a = M["A"]
print("\n  A 대비")
for v in VS[1:]:
    m = M[v]; sa_ = sorted(dl["A"].values()); sv = sorted(dl[v].values())
    print(f"  - {NM[v]}: 총손익 {m['krw']-a['krw']:+,.0f} · 복리 {m['comp']-a['comp']:+.2f}%p · "
          f"MDD {m['mdd']-a['mdd']:+.2f}%p · 최대1일손실 {m['dmin']-a['dmin']:+,.0f} · "
          f"복리/MDD {m['comp']/abs(m['mdd'])-a['comp']/abs(a['mdd']):+.1f} · 하위10일 {sum(sv[:10])-sum(sa_[:10]):+,.0f}")

print("\n## 조합의 핵심 질문 — BRK15 가 RS 상위20% 거래를 취소했는가")
arm = [e for e in BRKEV if e["ev"] == "ARMED_BRK15"]
fired = {e["sid"] for e in BRKEV if e["ev"] == "FIRED" and e.get("executed")}
rsup = [e for e in BRKEV if e["ev"] == "RS_UP"]
exp = [e for e in BRKEV if e["ev"].startswith("EXPIRED")]
print(f"  arm {len(arm)} · 돌파체결 {len(fired)} · 폐기 {len(exp)} · RS 증액 적용 {len(rsup)}건")
amap = {t["base"]: t for t in T["A"]}
rs125 = pd.read_csv(ROOT + "/rs125_trades.csv") if os.path.exists(ROOT + "/rs125_trades.csv") else None
if rs125 is not None:
    rs125["day"] = rs125["day"].astype(str)
    keys = set(rs125["day"] + " " + pd.to_datetime(rs125["t"]).dt.strftime("%H:%M:%S"))
    hit = [e for e in arm if (e["day"] + " " + pd.Timestamp(e["armed_at"]).strftime("%H:%M:%S")) in keys]
    cancelled = [e for e in hit if e["sid"] not in fired]
    print(f"  RS125 가 증액했던 16건 중 BRK15 대상 {len(hit)}건 · 그중 **폐기 {len(cancelled)}건**")
    for e in cancelled:
        a_ = amap.get(e["sid"])
        print(f"    폐기: {e['day'][4:6]}/{e['day'][6:]} {pd.Timestamp(e['armed_at']):%H:%M} "
              f"{e['dir']} · A 손익 {a_['krw'] if a_ else 0:+,.0f} ({'+'.join(dict.fromkeys(a_['reasons'])) if a_ else '?'})")
    kept = [e for e in hit if e["sid"] in fired]
    print(f"    체결 {len(kept)}건 · A 합 {sum(amap[e['sid']]['krw'] for e in kept if e['sid'] in amap):+,.0f}")

print("\n## 최악 손실일 5일")
worst = sorted(DAYS, key=lambda d: dl["A"][d])[:5]
print("| 일자 | " + " | ".join(NM[v] for v in VS) + " |\n|" + "---|" * 5)
for d in worst:
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(f"{dl[v][d]:+,.0f}" for v in VS) + " |")
print(f"| **합계** | " + " | ".join(f"{sum(dl[v][d] for d in worst):+,.0f}" for v in VS) + " |")

print("\n## 월별")
print("| 월 | " + " | ".join(NM[v] for v in VS) + " |\n|" + "---|" * 5)
for mo in sorted({d[:6] for d in DAYS}):
    print(f"| {mo[:4]}-{mo[4:]} | " + " | ".join(
        f"{sum(t['krw'] for t in T[v] if t['day'][:6] == mo):+,.0f}" for v in VS) + " |")

print("\n## 구간별 · 견고성")
for lab, days in (("전반기 ~07/31", [d for d in DAYS if d < "20260801"]),
                  ("후반기 08/01~", [d for d in DAYS if d >= "20260801"]),
                  ("최근6일 제외", [d for d in DAYS if d not in RECENT])):
    print(f"- {lab} ({len(days)}일): " + " | ".join(
        f"{NM[v]} {sum(t['krw'] for t in T[v] if t['day'] in days):+,.0f}" for v in VS))
rng = np.random.default_rng(0)
for v in VS[1:]:
    dd = np.array([dl[v][d] - dl["A"][d] for d in DAYS])
    bs = np.array([rng.choice(dd, len(dd)).sum() for _ in range(5000)])
    print(f"- {NM[v]}: 우세 {(dd>0).sum()} / 열세 {(dd<0).sum()} / 동일 {(dd==0).sum()} · "
          f"합 {dd.sum():+,.0f} · 부트스트랩 P(>A) {(bs>0).mean()*100:.1f}%")

print("\n## 9월 · 10/01 · 10/02")
m9 = {v: metrics(SEPT, [t for t in T[v] if t["day"] in SEPT]) for v in VS}
print("| 구간 | " + " | ".join(NM[v] for v in VS) + " |\n|" + "---|" * 5)
print("| 9월 총손익 | " + " | ".join(f"{m9[v]['krw']:+,.0f}" for v in VS) + " |")
print("| 9월 MDD% | " + " | ".join(f"{m9[v]['mdd']:.2f}" for v in VS) + " |")
for d in ("20261001", "20261002"):
    row = []
    for v in VS:
        p = path(v, d)
        try:
            row.append(f"{sum(t['krw'] for t in trades(json.load(open(p, encoding='utf-8')))):+,.0f}")
        except Exception:
            row.append("—")
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(row) + " |")
