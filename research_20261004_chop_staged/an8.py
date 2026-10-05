"""85영업일: A P3-R0 vs B C1-STAGED-50 vs C C1-STAGED-30. READ-ONLY.

왕복장 판정 = 진입 직전 최근 30분이 PE<0.20 & realized range<1.0% & EMA20 교차 >=3회.
손익 = production 거래원장 net_pnl. 일 수익률 = 원 / 1,000만원.
발동하지 않는 날은 B/C 가 A 와 동일하므로 A 재생 결과를 그대로 쓴다(skip_days8.txt,
그중 3일은 실제 재생해 동일함을 대조한다).
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O8 = ROOT + "/wk/out8"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
LONG = "0193T0"
TAG = {"A": "A", "B": "STG50", "C": "STG30"}
NM = {"A": "A P3-R0", "B": "B C1-STAGED-50", "C": "C C1-STAGED-30"}
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
SKIP = set(x.strip() for x in open(ROOT + "/skip_days8.txt", encoding="utf-8") if x.strip())
RUNNER_NET = 3.0


def apath(d):
    p = f"{O3}/REAL_A_{d}.json"
    return p if os.path.exists(p) else f"{O4}/REAL_A_{d}.json"


def ld(v, d):
    if v == "A":
        return json.load(open(apath(d), encoding="utf-8"))
    p = f"{O8}/REAL_{TAG[v]}_{d}.json"
    if not os.path.exists(p) and d in SKIP:
        p = apath(d)                      # 발동 없는 날 = A 와 동일
    return json.load(open(p, encoding="utf-8"))


def trades(o):
    """BUY->SELL 레그를 거래로 묶는다. 보유 중 추가 BUY(staged 추가진입)는
    새 거래가 아니라 **같은 거래에 가중평균으로 합친다**."""
    sells = [x for x in o["orders"] if x["side"] == "SELL"]
    ex_s = [e for e in o["ex"] if e["side"] == "SELL"][-len(sells):] if sells else []
    assert len(ex_s) == len(sells), (o["D"], o["VAR"])
    exe = {pd.Timestamp(s["detected_at"]).strftime("%H:%M:%S"): s["signal_id"]
           for s in o["sig"] if s["order_result"] == "EXECUTED"}
    ent = {pd.Timestamp(e["t"]).strftime("%H:%M:%S"): e for e in o.get("entries", [])}
    out, cur, si = [], None, 0
    for x in o["orders"]:
        t = pd.Timestamp(x["t"])
        if x["side"] == "BUY":
            if cur is not None and cur["sold"] < cur["q"]:          # 추가진입
                tot = cur["q"] + x["qty"]
                cur["px_in"] = (cur["px_in"] * cur["q"] + x["px"] * x["qty"]) / tot
                cur["q"] = tot
                cur["adds"] += 1
                continue
            sid = exe.get(t.strftime("%H:%M:%S"), "")
            e = ent.get(t.strftime("%H:%M:%S"), {})
            cur = dict(day=o["D"], entry=t, dir=("UP 레버" if x["sym"] == LONG else "DN 인버"),
                       px_in=float(x["px"]), q=int(x["qty"]), sid=sid, base=sid, adds=0,
                       sold=0, legs=[], krw=0.0, reasons=[], exit=None, regime=e.get("regime"))
            out.append(cur)
            continue
        e = ex_s[si]; si += 1
        if cur is None:
            continue
        cur["sold"] += x["qty"]; cur["legs"].append((t, x["qty"], x["px"]))
        cur["krw"] += float(e["net_pnl"] or 0); cur["reasons"].append(e["exit_reason"] or "?"); cur["exit"] = t
        if cur["sold"] >= cur["q"]:
            cur["net"] = cur["krw"] / (cur["q"] * cur["px_in"]) * 100
            cur["hold_min"] = (cur["exit"] - cur["entry"]).total_seconds() / 60
            cur = None
    return [t for t in out if "net" in t]


def metrics(days, tr):
    daily = np.array([sum(t["krw"] for t in tr if t["day"] == d) for d in days], dtype=float)
    r = daily / 10_000_000 * 100
    eq = np.cumprod(1 + r / 100)
    k = np.array([t["krw"] for t in tr], dtype=float) if tr else np.zeros(1)
    return dict(krw=daily.sum(), comp=(eq[-1] - 1) * 100,
                pf=k[k > 0].sum() / max(-k[k < 0].sum(), 1.0),
                mdd=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100,
                win=(k > 0).mean() * 100, lossd=int((daily < 0).sum()), d2=int((r >= 2).sum()),
                n=len(tr), hold=(np.mean([t["hold_min"] for t in tr]) if tr else 0.0),
                dmin=daily.min(), dmax=daily.max())


def rs(t):
    return "+".join(dict.fromkeys(t["reasons"]))


DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
miss = [d for d in DAYS if d not in SKIP and not os.path.exists(f"{O8}/REAL_STG50_{d}.json")]
if miss:
    print(f"# 미완료 {len(miss)}일: {' '.join(miss[:10])} ...")
SEPT = [d for d in DAYS if d.startswith("202609")]
RAW = {v: {d: ld(v, d) for d in DAYS} for v in ("A", "B", "C")}
T = {v: sum((trades(RAW[v][d]) for d in DAYS), []) for v in ("A", "B", "C")}
STG = {v: sum(([dict(e, day=d) for e in (RAW[v][d].get("stg") or [])] for d in DAYS), []) for v in ("B", "C")}

sa = sum(t["krw"] for t in T["A"] if t["day"] in SEPT)
print(f"# 대상 {len(DAYS)}일 ({DAYS[0]}~{DAYS[-1]}) · 실제 재생 {len(DAYS)-len(SKIP)}일 / 동일보장 {len(SKIP)}일")
print(f"# anchor: 9월 A {sa:+,.0f}원 (기준 +1,416,214)")
# skip 주장 검증: 실제 재생한 skip 일이 A 와 거래단위까지 같은가
ver, bad = [], []
for d in sorted(SKIP):
    p = f"{O8}/REAL_STG50_{d}.json"
    if not os.path.exists(p):
        continue
    ver.append(d)
    x, y = json.load(open(p, encoding="utf-8")), RAW["A"][d]
    f = lambda o: [(q["t"], q["side"], q["sym"], q["qty"], q["px"]) for q in o["orders"]]
    if f(x) != f(y):
        bad.append(d)
print(f"# skip 동일성 실측검증 {len(ver)}일 {ver} · 불일치 {bad or '없음'}")
if round(sa) != 1_416_214 or bad:
    print("## => INVALID REPLAY")
    sys.exit(1)


def tab(days, title):
    print(f"\n## {title} ({len(days)}일)")
    print("| 전략 | 총손익 | 복리% | PF | MDD% | 승률% | 거래수 | 손실일 | +2%일 | 평균보유 | 최대1일손실 | 최대1일수익 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    M = {}
    for v in ("A", "B", "C"):
        m = M[v] = metrics(days, [t for t in T[v] if t["day"] in days])
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['pf']:.2f} | {m['mdd']:.2f} | {m['win']:.1f} | "
              f"{m['n']} | {m['lossd']} | {m['d2']} | {m['hold']:.0f}분 | {m['dmin']:+,.0f} | {m['dmax']:+,.0f} |")
    for v in ("B", "C"):
        a, b = M["A"], M[v]
        print(f"  {v}−A: 총손익 {b['krw']-a['krw']:+,.0f} · 복리 {b['comp']-a['comp']:+.2f}%p · PF {b['pf']-a['pf']:+.2f} "
              f"· MDD {b['mdd']-a['mdd']:+.2f}%p · 승률 {b['win']-a['win']:+.1f}%p · 최대1일손실 {b['dmin']-a['dmin']:+,.0f}")
    return M


tab(DAYS, "85영업일 전체")
amap = {t["base"]: t for t in T["A"]}

print("\n## staged entry 발동")
print("| 후보 | 발동(STAGED_FIRST) | 돌파 추가진입(ADDED) | 추가 안 됨 | 발동거래 평균손익 | A 같은진입 평균손익 |")
print("|---|---|---|---|---|---|")
for v in ("B", "C"):
    ev = STG[v]
    first = [e for e in ev if e["ev"] == "STAGED_FIRST"]
    added = [e for e in ev if e["ev"] == "ADDED"]
    noadd = [e for e in ev if e["ev"] in ("EXPIRED", "EXPIRED_EOD", "CANCELLED_FLAT", "CANCELLED_EPOCH", "ADD_FAILED", "NO_TRIGGER")]
    vmap = {t["base"]: t for t in T[v]}
    fk = [vmap[e["sid"]]["krw"] for e in first if e["sid"] in vmap]
    ak = [amap[e["sid"]]["krw"] for e in first if e["sid"] in amap]
    print(f"| {NM[v]} | {len(first)} | {len(added)} | {len(noadd)} | "
          f"{np.mean(fk) if fk else 0:+,.0f} ({len(fk)}건) | {np.mean(ak) if ak else 0:+,.0f} ({len(ak)}건) |")

print("\n## 핵심 검증")
for v in ("B", "C"):
    vmap = {t["base"]: t for t in T[v]}
    first = [e for e in STG[v] if e["ev"] == "STAGED_FIRST"]
    addset = {e["sid"] for e in STG[v] if e["ev"] == "ADDED"}
    pair = [(amap[e["sid"]], vmap[e["sid"]], e["sid"] in addset) for e in first
            if e["sid"] in amap and e["sid"] in vmap]
    print(f"\n### {NM[v]}")
    print(f"- 발동 {len(first)}건 중 A 와 같은 진입 매칭 {len(pair)}건 · A {sum(a['krw'] for a,_,_ in pair):+,.0f} → {v} {sum(b['krw'] for _,b,_ in pair):+,.0f} (Δ {sum(b['krw']-a['krw'] for a,b,_ in pair):+,.0f})")
    avoid = sum(b["krw"] - a["krw"] for a, b, _ in pair if a["krw"] < 0 and b["krw"] > a["krw"])
    cut = sum(b["krw"] - a["krw"] for a, b, _ in pair if a["krw"] > 0 and b["krw"] < a["krw"])
    print(f"  - 피한 손실 {avoid:+,.0f}원 ({sum(1 for a,b,_ in pair if a['krw']<0 and b['krw']>a['krw'])}건)")
    print(f"  - 줄어든 수익 {cut:+,.0f}원 ({sum(1 for a,b,_ in pair if a['krw']>0 and b['krw']<a['krw'])}건)")
    run = [(a, b) for a, b, ad in pair if ad and b["net"] >= RUNNER_NET]
    print(f"  - 돌파 추가진입 후 net >= +{RUNNER_NET}% (runner 보존) {len(run)}건 · {v} {sum(b['krw'] for _,b in run):+,.0f}원 (A 같은진입 {sum(a['krw'] for a,_ in run):+,.0f}원)")
    nadd = [(a, b) for a, b, ad in pair if not ad]
    print(f"  - 추가 안 된 거래 {len(nadd)}건 · {v} {sum(b['krw'] for _,b in nadd):+,.0f}원 (A {sum(a['krw'] for a,_ in nadd):+,.0f}원, Δ {sum(b['krw']-a['krw'] for a,b in nadd):+,.0f})")
    for a, b, ad in sorted(pair, key=lambda p: p[1]["krw"] - p[0]["krw"]):
        print(f"    {a['day']} {a['entry']:%H:%M} {a['dir']} {'추가O' if ad else '추가X'} | A {a['px_in']:,.0f}→{rs(a)} {a['krw']:+,.0f} | {v} {b['px_in']:,.0f}→{rs(b)} {b['krw']:+,.0f} | Δ {b['krw']-a['krw']:+,.0f}")

print("\n## 견고성")
idx = {d: i for i, d in enumerate(DAYS)}
for v in ("B", "C"):
    dd = np.array([sum(t["krw"] for t in T[v] if t["day"] == d) - sum(t["krw"] for t in T["A"] if t["day"] == d) for d in DAYS])
    nr = [d for d in DAYS if d not in RECENT]
    print(f"- {NM[v]}: 일별 우세 {(dd>0).sum()} / 열세 {(dd<0).sum()} / 동일 {(dd==0).sum()} · 합 {dd.sum():+,.0f}")
    print(f"  최근 6일 제외({len(nr)}일): A {sum(t['krw'] for t in T['A'] if t['day'] in nr):+,.0f} / "
          f"{v} {sum(t['krw'] for t in T[v] if t['day'] in nr):+,.0f} / Δ {dd[[idx[d] for d in nr]].sum():+,.0f}")
    rng = np.random.default_rng(0)
    bs = np.array([rng.choice(dd, len(dd)).sum() for _ in range(5000)])
    print(f"  일별 부트스트랩 P({v} > A) = {(bs > 0).mean()*100:.1f}%")

tab(SEPT, "9월 (참고)")
print("\n## 10/01 · 10/02")
print("| 날짜 | A | B C1-STAGED-50 | C C1-STAGED-30 |\n|---|---|---|---|")
for d in ("20261001", "20261002"):
    row = []
    for v in ("A", "B", "C"):
        try:
            row.append(f"{sum(t['krw'] for t in trades(ld(v, d))):+,.0f}")
        except Exception:
            row.append("—")
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(row) + " |")
