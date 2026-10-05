"""85영업일: A P3-R0 vs B BREAKOUT15 vs C BREAKOUT30-1T.

production worker 전체 재생(REAL 체결) 결과만 읽는다. READ-ONLY.
손익 = production 거래원장 net_pnl. 일 수익률 = 원 / 1,000만원.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
O6 = ROOT + "/wk/out6"
O3 = os.path.dirname(ROOT) + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = os.path.dirname(ROOT) + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
LONG = "0193T0"
SRC = {"A": (O3, "A"), "B": (O6, "BRK15"), "C": (O6, "BRK30")}
NM = {"A": "A P3-R0", "B": "B BREAKOUT15", "C": "C BREAKOUT30-1T"}
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]


def ld(v, d):
    o, tag = SRC[v]
    p = f"{o}/REAL_{tag}_{d}.json"
    if v == "A" and not os.path.exists(p):
        p = f"{O4}/REAL_A_{d}.json"
    return json.load(open(p, encoding="utf-8"))


def trades(o):
    """BUY->SELL 레그를 하나의 거래로 묶는다 (sept3_lib.trades 와 같은 규칙 + signal_id)."""
    sells = [x for x in o["orders"] if x["side"] == "SELL"]
    ex_s = [e for e in o["ex"] if e["side"] == "SELL"][-len(sells):] if sells else []
    assert len(ex_s) == len(sells), (o["D"], o["VAR"])
    h50 = [pd.Timestamp(s["detected_at"]) for s in o["sig"] if (s["order_result"] or "") == "H50_SMALL_WHIPSAW_HOLD"]
    exe = {pd.Timestamp(s["detected_at"]).strftime("%H:%M:%S"): s["signal_id"]
           for s in o["sig"] if s["order_result"] == "EXECUTED"}
    ent = {pd.Timestamp(e["t"]).strftime("%H:%M:%S"): e for e in o.get("entries", [])}
    out, cur, si = [], None, 0
    for x in o["orders"]:
        t = pd.Timestamp(x["t"])
        if x["side"] == "BUY":
            sid = exe.get(t.strftime("%H:%M:%S"), "")
            e = ent.get(t.strftime("%H:%M:%S"), {})
            cur = dict(day=o["D"], entry=t, dir=("UP 레버" if x["sym"] == LONG else "DN 인버"),
                       px_in=x["px"], q=x["qty"], sid=sid, base=sid.replace(":BRK", ""),
                       flag=(sid.split("_")[1][:4] if sid else "?"), sold=0, legs=[], krw=0.0,
                       reasons=[], exit=None, regime=e.get("regime"), trend_ok=e.get("trend_ok"))
            out.append(cur)
            continue
        e = ex_s[si]; si += 1
        if cur is None:
            continue
        cur["sold"] += x["qty"]; cur["legs"].append((t, x["qty"], x["px"]))
        cur["krw"] += float(e["net_pnl"] or 0); cur["reasons"].append(e["exit_reason"] or "?"); cur["exit"] = t
        if cur["sold"] >= cur["q"]:
            cur["px_out"] = sum(q * p for _, q, p in cur["legs"]) / cur["q"]
            cur["net"] = cur["krw"] / (cur["q"] * cur["px_in"]) * 100
            cur["hold_min"] = (cur["exit"] - cur["entry"]).total_seconds() / 60
            cur["h50"] = any(cur["entry"] < h <= cur["exit"] for h in h50)
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


# ── 로드 ────────────────────────────────────────────────────────────────
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
HAVE = {v: sorted({os.path.basename(p).split("_")[-1][:8] for p in glob.glob(f"{SRC[v][0]}/REAL_{SRC[v][1]}_*.json")})
        for v in ("B", "C")}
DAYS = [d for d in DAYS if d in HAVE["B"] and d in HAVE["C"]]
SEPT = [d for d in DAYS if d.startswith("202609")]
print(f"# 대상 {len(DAYS)}일 ({DAYS[0]}~{DAYS[-1]}) · 9월 {len(SEPT)}일")

RAW = {v: {d: ld(v, d) for d in DAYS} for v in ("A", "B", "C")}
T = {v: sum((trades(RAW[v][d]) for d in DAYS), []) for v in ("A", "B", "C")}
BRK = {v: sum(([dict(e, day=d) for e in (RAW[v][d].get("brk") or [])] for d in DAYS), []) for v in ("B", "C")}

# ── anchor gate ─────────────────────────────────────────────────────────
sa = sum(t["krw"] for t in T["A"] if t["day"] in SEPT)
bad = []
for d in SEPT + (["20261001"] if "20261001" in DAYS else []):
    p = f"{O6}/REAL_A_{d}.json"
    if not os.path.exists(p):
        continue
    a, b = json.load(open(p, encoding="utf-8")), RAW["A"][d]
    f = lambda o: [(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in o["orders"]]
    k = sum(1 for x in a["orders"] if x["side"] == "SELL")
    g = lambda o: [(e["exit_reason"], e["net_pnl"]) for e in o["ex"] if e["side"] == "SELL"][-k:] if k else []
    if f(a) != f(b) or g(a) != g(b):
        bad.append(d)
print(f"# anchor: 9월 A {sa:+,.0f}원 (기준 +1,416,214) · 독립 재현 대조 불일치일: {bad or '없음'}")
if (round(sa) != 1_416_214 or bad) and not os.environ.get("AN6_DRYRUN"):
    print("## => INVALID REPLAY")
    sys.exit(1)


def tab(days, title):
    print(f"\n## {title} ({len(days)}일)")
    print("| 전략 | 총손익 | 복리% | PF | MDD% | 승률% | 거래수 | 손실일 | +2%일 | 평균보유(분) | 최대1일손실 | 최대1일수익 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    M = {}
    for v in ("A", "B", "C"):
        m = M[v] = metrics(days, [t for t in T[v] if t["day"] in days])
        print(f"| {NM[v]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['pf']:.2f} | {m['mdd']:.2f} | {m['win']:.1f} | "
              f"{m['n']} | {m['lossd']} | {m['d2']} | {m['hold']:.0f} | {m['dmin']:+,.0f} | {m['dmax']:+,.0f} |")
    for v in ("B", "C"):
        a, b = M["A"], M[v]
        print(f"  {v}−A: 총손익 {b['krw'] - a['krw']:+,.0f} · 복리 {b['comp'] - a['comp']:+.2f}%p · PF {b['pf'] - a['pf']:+.2f} "
              f"· MDD {b['mdd'] - a['mdd']:+.2f}%p · 승률 {b['win'] - a['win']:+.1f}%p · 거래수 {b['n'] - a['n']:+d}")
    return M


M85 = tab(DAYS, "85영업일 전체")

# ── breakout 동작 요약 ──────────────────────────────────────────────────
print("\n## trigger 동작 (85일)")
print("| 후보 | ARMED | 체결(FIRED) | 취소(미도달) | 교체(SUPERSEDED) | 체결률 | 평균 진입지연(분) | 평균 체결가 불리함% | wick이면 닿았을 취소 |")
print("|---|---|---|---|---|---|---|---|---|")
amap = {t["base"]: t for t in T["A"]}
FIRED, CANC = {}, {}
for v in ("B", "C"):
    ev = BRK[v]
    armed = [e for e in ev if e["ev"] == "ARMED"]
    fired = [e for e in ev if e["ev"] == "FIRED"]
    exp = [e for e in ev if e["ev"].startswith("EXPIRED")]
    sup = [e for e in ev if e["ev"] == "SUPERSEDED"]
    FIRED[v], CANC[v] = fired, exp + sup
    dly = [e["delay_min"] for e in fired if e.get("delay_min") is not None]
    vmap = {t["base"]: t for t in T[v]}
    dis = []
    for e in fired:
        b, a = vmap.get(e["sid"]), amap.get(e["sid"])
        if b and a:
            dis.append((b["px_in"] - a["px_in"]) / a["px_in"] * 100)
    wick = sum(1 for e in CANC[v] if e.get("wick"))
    print(f"| {NM[v]} | {len(armed)} | {len(fired)} | {len(exp)} | {len(sup)} | {len(fired) / max(len(armed), 1) * 100:.1f}% | "
          f"{np.mean(dly) if dly else 0:.2f} | {np.mean(dis) if dis else 0:+.3f} | {wick} |")

# ── 월별 ────────────────────────────────────────────────────────────────
print("\n## 월별\n| 월 | A | B BREAKOUT15 | C BREAKOUT30-1T | B−A | C−A |\n|---|---|---|---|---|---|")
for mo in sorted({d[:6] for d in DAYS}):
    s = {v: sum(t["krw"] for t in T[v] if t["day"][:6] == mo) for v in ("A", "B", "C")}
    print(f"| {mo[:4]}-{mo[4:]} | {s['A']:+,.0f} | {s['B']:+,.0f} | {s['C']:+,.0f} | {s['B'] - s['A']:+,.0f} | {s['C'] - s['A']:+,.0f} |")

tab(SEPT, "9월 sanity check")

# ── 핵심 검증 ───────────────────────────────────────────────────────────
print("\n## 핵심 검증")
for v in ("B", "C"):
    vmap = {t["base"]: t for t in T[v]}
    print(f"\n### {NM[v]}")
    # 1) 취소된 signal 이 A 에서 얼마였나
    cz = [(e, amap.get(e["sid"])) for e in CANC[v]]
    hit = [(e, a) for e, a in cz if a is not None]
    avoid = sum(a["krw"] for _, a in hit if a["krw"] < 0)
    missed = sum(a["krw"] for _, a in hit if a["krw"] > 0)
    print(f"- 취소 {len(cz)}건 중 A 에 같은 진입이 있던 것 {len(hit)}건 · A 손익 합 {sum(a['krw'] for _, a in hit):+,.0f}원")
    print(f"  - 취소로 피한 손실: {avoid:+,.0f}원 ({sum(1 for _, a in hit if a['krw'] < 0)}건)")
    print(f"  - 취소로 놓친 수익: {missed:+,.0f}원 ({sum(1 for _, a in hit if a['krw'] > 0)}건)")
    print(f"  - A 에 없던 취소(슬롯/포지션 때문에 A 도 진입 안함): {len(cz) - len(hit)}건")
    # 2) 늦은 진입 효과 (같은 signal 이 양쪽에서 체결된 경우)
    pair = [(amap[e["sid"]], vmap[e["sid"]]) for e in FIRED[v] if e["sid"] in amap and e["sid"] in vmap]
    dd = [b["krw"] - a["krw"] for a, b in pair]
    print(f"- 같은 signal 이 양쪽에서 체결 {len(pair)}건 · A {sum(a['krw'] for a, _ in pair):+,.0f} → {v} {sum(b['krw'] for _, b in pair):+,.0f} "
          f"(Δ {sum(dd):+,.0f}, 개선 {sum(1 for x in dd if x > 0)} / 악화 {sum(1 for x in dd if x < 0)} / 동일 {sum(1 for x in dd if x == 0)})")
    worse = sum(x for x in dd if x < 0); better = sum(x for x in dd if x > 0)
    print(f"  - 전체 악화 {worse:+,.0f}원 / 개선 {better:+,.0f}원")
    cut_win = sum(b["krw"] - a["krw"] for a, b in pair if a["krw"] > 0 and b["krw"] < a["krw"])
    add_loss = sum(b["krw"] - a["krw"] for a, b in pair if a["krw"] <= 0 and b["krw"] < a["krw"])
    print(f"  - 늦은 진입으로 '줄어든 수익'(A가 이긴 거래) {cut_win:+,.0f}원 "
          f"/ '늘어난 손실'(A가 진 거래) {add_loss:+,.0f}원")
    # 3) 슬롯이 비어 새로 생긴 후속 진입
    only_v = [t for t in T[v] if t["base"] not in amap]
    only_a = [t for t in T["A"] if t["base"] not in vmap]
    print(f"- {v} 에만 있는 진입(슬롯 여유 등) {len(only_v)}건 {sum(t['krw'] for t in only_v):+,.0f}원 "
          f"/ A 에만 있는 진입 {len(only_a)}건 {sum(t['krw'] for t in only_a):+,.0f}원 "
          f"· 순효과 {sum(t['krw'] for t in only_v) - sum(t['krw'] for t in only_a):+,.0f}원")
    # 4) 청산경로 변화
    chg = [(a, b) for a, b in pair if rs(a) != rs(b)]
    print(f"- 같은 진입인데 청산경로가 달라진 거래 {len(chg)}건 (Δ {sum(b['krw'] - a['krw'] for a, b in chg):+,.0f}원)")
    cnt = {}
    for a, b in chg:
        cnt[(rs(a), rs(b))] = cnt.get((rs(a), rs(b)), [0, 0.0])
        cnt[(rs(a), rs(b))][0] += 1
        cnt[(rs(a), rs(b))][1] += b["krw"] - a["krw"]
    for (x, y), (n, k) in sorted(cnt.items(), key=lambda z: z[1][1]):
        print(f"    {x} -> {y}: {n}건 {k:+,.0f}원")
    # 대표 거래
    pair2 = sorted(pair, key=lambda p: p[1]["krw"] - p[0]["krw"])
    for lab, zs in (("실패", [p for p in pair2 if p[1]["krw"] < p[0]["krw"]][:5]),
                    ("성공", [p for p in pair2[::-1] if p[1]["krw"] > p[0]["krw"]][:5])):
        for a, b in zs:
            print(f"    대표{lab}: {a['day']} {a['entry']:%H:%M} {a['dir']} | A {a['px_in']:,.0f}->{rs(a)} {a['krw']:+,.0f} "
                  f"| {v} {b['entry']:%H:%M} {b['px_in']:,.0f}->{rs(b)} {b['krw']:+,.0f} | Δ {b['krw'] - a['krw']:+,.0f}")
    # 취소 대표
    hit2 = sorted(hit, key=lambda z: z[1]["krw"])
    for lab, zs in (("취소로 피한 손실 Top5", [z for z in hit2 if z[1]["krw"] < 0][:5]),
                    ("취소로 놓친 수익 Top5", [z for z in hit2[::-1] if z[1]["krw"] > 0][:5])):
        for e, a in zs:
            print(f"    {lab}: {a['day']} {a['entry']:%H:%M} {a['dir']} A {rs(a)} {a['krw']:+,.0f} "
                  f"(trigger {e['trigger']:,.0f}, wick {e.get('wick') or '미도달'})")

# ── 견고성 ──────────────────────────────────────────────────────────────
print("\n## 견고성")
idx = {d: i for i, d in enumerate(DAYS)}
for v in ("B", "C"):
    dd = np.array([sum(t["krw"] for t in T[v] if t["day"] == d) - sum(t["krw"] for t in T["A"] if t["day"] == d) for d in DAYS])
    tot = dd.sum(); top2 = np.sort(dd)[-2:].sum()
    print(f"\n### {NM[v]}")
    print(f"- 일별 {v}−A: 우세 {(dd > 0).sum()}일 / 열세 {(dd < 0).sum()}일 / 동일 {(dd == 0).sum()}일 · 합 {tot:+,.0f}")
    print(f"- 상위 개선 2일 {top2:+,.0f} 제외 시 {v}−A = {tot - top2:+,.0f}원")
    vmap = {t["base"]: t for t in T[v]}
    pair = [(amap[k], vmap[k]) for k in vmap if k in amap]
    td = np.array([b["krw"] - a["krw"] for a, b in pair]) if pair else np.zeros(1)
    print(f"- 상위 개선 2거래 {np.sort(td)[-2:].sum():+,.0f} 제외 시 매칭거래 Δ = {td.sum() - np.sort(td)[-2:].sum():+,.0f}원")
    nr = [d for d in DAYS if d not in RECENT]
    print(f"- 최근 6일 제외({len(nr)}일): A {sum(t['krw'] for t in T['A'] if t['day'] in nr):+,.0f} / "
          f"{v} {sum(t['krw'] for t in T[v] if t['day'] in nr):+,.0f} / Δ {dd[[idx[d] for d in nr]].sum():+,.0f}")
    h1 = [d for d in DAYS if d < "20260801"]; h2 = [d for d in DAYS if d >= "20260801"]
    m1 = metrics(h1, [t for t in T[v] if t["day"] in h1]); m1a = metrics(h1, [t for t in T["A"] if t["day"] in h1])
    m2 = metrics(h2, [t for t in T[v] if t["day"] in h2]); m2a = metrics(h2, [t for t in T["A"] if t["day"] in h2])
    print(f"- 전반기(~07/31 {len(h1)}일): A {m1a['krw']:+,.0f} / {v} {m1['krw']:+,.0f} / Δ {m1['krw'] - m1a['krw']:+,.0f}")
    print(f"- 후반기(08/01~ {len(h2)}일): A {m2a['krw']:+,.0f} / {v} {m2['krw']:+,.0f} / Δ {m2['krw'] - m2a['krw']:+,.0f}")
    rng = np.random.default_rng(0)
    bs = np.array([rng.choice(dd, len(dd)).sum() for _ in range(5000)])
    print(f"- 일별 부트스트랩 P({v} > A) = {(bs > 0).mean() * 100:.1f}%")

# ── 참고표: 10/01, 10/02 ────────────────────────────────────────────────
print("\n## 참고표 10/01 · 10/02 (85일 판정과 무관)")
print("| 날짜 | A | B BREAKOUT15 | C BREAKOUT30-1T |\n|---|---|---|---|")
for d in ("20261001", "20261002"):
    row = []
    for v in ("A", "B", "C"):
        try:
            tr = trades(ld(v, d))
            row.append(f"{sum(t['krw'] for t in tr):+,.0f}")
        except Exception:
            row.append("—")
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(row) + " |")

print("\n### 10/02 세 signal 상세 (09:54 UP / 11:24 DOWN / 11:45 UP)")
print("| signal | A | B BREAKOUT15 | C BREAKOUT30-1T |\n|---|---|---|---|")
want = ["20261002_095400_UP_RED", "20261002_112400_DOWN_BLUE", "20261002_114500_UP_RED"]
det = {}
for v in ("A", "B", "C"):
    try:
        o = ld(v, "20261002")
        det[v] = (trades(o), {e["sid"].replace(":TW2_3SLOT_CONFIRM", ""): e for e in (o.get("brk") or [])})
    except Exception:
        det[v] = ([], {})
for w in want:
    cells = []
    for v in ("A", "B", "C"):
        tr, ev = det[v]
        m = [t for t in tr if t["base"].replace(":TW2_3SLOT_CONFIRM", "") == w]
        if m:
            t = m[0]
            cells.append(f"{t['entry']:%H:%M:%S} @{t['px_in']:,.0f} → {t['exit']:%H:%M} {rs(t)} **{t['krw']:+,.0f}**")
        elif w in ev:
            e = ev[w]
            cells.append(f"미체결 (trigger {e['trigger']:,.0f}, wick {e.get('wick') or '미도달'})")
        else:
            cells.append("—")
    print(f"| {w[9:13]} {w[16:]} | " + " | ".join(cells) + " |")
