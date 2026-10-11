"""① EARLY-UP-FAST 의 우위가 개선 8건을 빼도 유지되는가
   ② 9월 약점(−541,885)을 따로 고칠 여지가 있는가
재생 불필요 — 기존 결과만 사용. READ-ONLY.
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
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
SEPT = [d for d in DAYS if d.startswith("202609")]
UPD = sorted(json.load(open(ROOT + "/upfast_days.json", encoding="utf-8"))["days"])
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]


def ap(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def pb(d):
    p = f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json"
    return p if os.path.exists(p) else f"{BRKD}/REAL_BRK15_{d}.json"


def pc(d):
    p = f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json"
    return p if os.path.exists(p) else pb(d)


T, pnl, EV = {}, {}, {}
for v, f in (("A", ap), ("B", pb), ("C", pc)):
    T[v], pnl[v], EV[v] = [], {}, []
    for d in DAYS:
        o = json.load(open(f(d), encoding="utf-8"))
        tt = trades(o)
        T[v] += tt
        pnl[v][d] = sum(t["krw"] for t in tt)
        EV[v] += [dict(e, day=d) for e in (o.get("brk") or [])]

dCA = {d: pnl["C"][d] - pnl["A"][d] for d in DAYS}
dCB = {d: pnl["C"][d] - pnl["B"][d] for d in DAYS}
print(f"# 85일 · A {sum(pnl['A'].values()):+,.0f} / B {sum(pnl['B'].values()):+,.0f} / C {sum(pnl['C'].values()):+,.0f}")
print(f"# C−A {sum(dCA.values()):+,.0f} · C−B {sum(dCB.values()):+,.0f}\n")

print("## ① 개선 8일의 기여와 제외 검정")
print("| 일자 | C−B | C−A(그 날) |\n|---|---|---|")
for d in UPD:
    print(f"| {d[4:6]}/{d[6:]} | {dCB[d]:+,.0f} | {dCA[d]:+,.0f} |")
print(f"| **합** | **{sum(dCB[d] for d in UPD):+,.0f}** | {sum(dCA[d] for d in UPD):+,.0f} |")

order = sorted(UPD, key=lambda d: -dCB[d])
print("\n| 제외 기준 | 남은 C−A | 남은 C−B | A 대비 우위 |")
print("|---|---|---|---|")
base_ca, base_cb = sum(dCA.values()), sum(dCB.values())
print(f"| 제외 없음 | **{base_ca:+,.0f}** | {base_cb:+,.0f} | {'O' if base_ca>0 else 'X'} |")
for k in (1, 2, 3):
    ex = order[:k]
    ca = base_ca - sum(dCB[d] for d in ex)   # 그 날을 B 수준으로 되돌림
    cb = base_cb - sum(dCB[d] for d in ex)
    print(f"| 개선 상위 {k}일 제외({'·'.join(d[4:] for d in ex)}) | **{ca:+,.0f}** | {cb:+,.0f} | {'O' if ca>0 else '**X**'} |")
ca_all = base_ca - base_cb
print(f"| 개선 8일 전부 제외 (=B) | **{ca_all:+,.0f}** | +0 | **X** |")

print("\n## 구간/견고성")
rng = np.random.default_rng(0)
for lab, dd in (("C−A", dCA), ("C−B", dCB)):
    a = np.array([dd[d] for d in DAYS])
    bs = np.array([rng.choice(a, len(a)).sum() for _ in range(5000)])
    print(f"  {lab}: 우세 {(a>0).sum()}일 / 열세 {(a<0).sum()}일 / 동일 {(a==0).sum()}일 · "
          f"부트스트랩 P(>0) {(bs>0).mean()*100:.1f}%")
for lab, days in (("전반기 ~07/31", [d for d in DAYS if d < "20260801"]),
                  ("후반기 08/01~", [d for d in DAYS if d >= "20260801"]),
                  ("최근6일 제외", [d for d in DAYS if d not in RECENT])):
    print(f"  {lab} ({len(days)}일): C−A {sum(dCA[d] for d in days):+,.0f} · C−B {sum(dCB[d] for d in days):+,.0f}")

print("\n## ② 9월 약점 — 돌파대기 5건의 승인시점 조건")
Am = {t["base"]: t for t in T["A"]}
Cm = {t["base"].replace(":BRK", ""): t for t in T["C"]}
fail = {e["sid"]: e for e in EV["C"] if e["ev"] == "EARLY_FAIL"}
fired = {e["sid"]: e for e in EV["C"] if e["ev"] == "FIRED" and e.get("executed")}
rows = []
for sid, e in fired.items():
    if not e["day"].startswith("202609"):
        continue
    f = fail.get(sid, {})
    a, c = Am.get(sid), Cm.get(sid)
    if c is None:
        continue
    rows.append(dict(day=e["day"], t=c["entry"], dirn=c["dir"], dist=f.get("dist"), c2=f.get("c2"), c3=f.get("c3"),
                     delay=e.get("delay_min"), apx=(a["px_in"] if a else np.nan), cpx=c["px_in"],
                     runup=((c["px_in"] - a["px_in"]) / a["px_in"] * 100 if a else np.nan),
                     ak=(a["krw"] if a else 0), ck=c["krw"], d=(c["krw"] - (a["krw"] if a else 0))))
R = pd.DataFrame(rows).sort_values("d")
print("| 일자 | 진입 | dist | c2 | c3 | 지연 | 승인가→체결가 | 상승률 | A | C | Δ |")
print("|---|---|---|---|---|---|---|---|---|---|---|")
for _, r in R.iterrows():
    print(f"| {r['day'][4:6]}/{r['day'][6:]} | {r['t']:%H:%M} | {r['dist']:.3f}% | {r['c2']} | {r['c3']} | "
          f"{r['delay']:.1f}분 | {r['apx']:,.0f}→{r['cpx']:,.0f} | **{r['runup']:+.2f}%** | "
          f"{r['ak']:+,.0f} | {r['ck']:+,.0f} | **{r['d']:+,.0f}** |")
print(f"\n  5건 Δ 합 {R.d.sum():+,.0f}")
print(f"  dist 가 0.3% 이내인 건: {int((R.dist<=0.3).sum())}건 -> UP-FAST 로 이미 커버됨")
print(f"  dist 0.3~0.5%: {int(((R.dist>0.3)&(R.dist<=0.5)).sum())}건 · 0.5% 초과: {int((R.dist>0.5).sum())}건")

print("\n## ② 9월 — C 에 없고 A 에만 있던 진입")
for k in sorted([k for k in Am if k not in Cm], key=lambda x: Am[x]["entry"]):
    t = Am[k]
    if not t["day"].startswith("202609"):
        continue
    ex = any(e["sid"] == k and e["ev"].startswith("EXPIRED") for e in EV["C"])
    print(f"  {t['day'][4:6]}/{t['day'][6:]} {t['entry']:%H:%M} {t['dir']} "
          f"[{'폐기' if ex else '미진입'}] {'+'.join(dict.fromkeys(t['reasons']))} {t['krw']:+,.0f}")
R.to_csv(ROOT + "/sept_wait5.csv", index=False, encoding="utf-8")
