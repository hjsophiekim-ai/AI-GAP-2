"""9월 2차 비교 집계: A P3-R0 / B D-R0 / C D-NOH50 / D P3-TREND-BYPASS (전부 production worker 전체 재생, REAL 체결). READ-ONLY.
손익 = production 거래원장 net_pnl. 일 수익률 = 원 / 1,000만원.
"""
import json, os, sys
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
S = sys.argv[1]
O2, O3 = S + "/wk/out2", S + "/wk/out3"
SEPT = ["20260903", "20260904", "20260907", "20260908", "20260909", "20260910", "20260911", "20260914", "20260915",
        "20260916", "20260917", "20260918", "20260921", "20260922", "20260923", "20260928", "20260929", "20260930"]
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
ALL = SEPT + ["20261001"]
LONG = "0193T0"
SRC = {"A": (O3, "A"), "B": (O2, "D"), "C": (O3, "D0"), "D": (O3, "E")}
NAME = {"A": "A P3-R0", "B": "B D-R0 (SIMPLE-N1-H50-NOSTOP)", "C": "C D-NOH50", "D": "D P3-TREND-BYPASS"}


def load(k, d):
    o, v = SRC[k]
    return json.load(open(f"{o}/REAL_{v}_{d}.json", encoding="utf-8"))


def trades(o):
    sells = [x for x in o["orders"] if x["side"] == "SELL"]
    ex_s = [e for e in o["ex"] if e["side"] == "SELL"][-len(sells):] if sells else []
    assert len(ex_s) == len(sells), (o["D"], o["VAR"])
    h50 = [pd.Timestamp(s["detected_at"]) for s in o["sig"] if (s["order_result"] or "") == "H50_SMALL_WHIPSAW_HOLD"]
    exe = {pd.Timestamp(s["detected_at"]).strftime("%H:%M:%S"): s["signal_id"] for s in o["sig"] if s["order_result"] == "EXECUTED"}
    ent = {pd.Timestamp(e["t"]).strftime("%H:%M:%S"): e for e in o.get("entries", [])}
    out, cur, si = [], None, 0
    for x in o["orders"]:
        t = pd.Timestamp(x["t"])
        if x["side"] == "BUY":
            sid = exe.get(t.strftime("%H:%M:%S"), "")
            e = ent.get(t.strftime("%H:%M:%S"), {})
            cur = dict(day=o["D"], entry=t, dir="UP 레버" if x["sym"] == LONG else "DN 인버", px_in=x["px"], q=x["qty"],
                       flag=(sid.split("_")[1][:4] if sid else "?"), sold=0, legs=[], krw=0.0, reasons=[], exit=None,
                       regime=e.get("regime"), trend_ok=e.get("trend_ok"), bypass=e.get("bypass", False))
            out.append(cur); continue
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
            cur["h50_at"] = next((h for h in h50 if cur["entry"] < h <= cur["exit"]), None)
            cur = None
    return [t for t in out if "net" in t]


def metrics(days, tr):
    daily = np.array([sum(t["krw"] for t in tr if t["day"] == d) for d in days])
    r = daily / 10_000_000 * 100
    eq = np.cumprod(1 + r / 100)
    k = np.array([t["krw"] for t in tr]) if tr else np.zeros(1)
    return dict(krw=daily.sum(), comp=(eq[-1] - 1) * 100, pf=k[k > 0].sum() / max(-k[k < 0].sum(), 1.0),
                mdd=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100, win=(k > 0).mean() * 100,
                lossd=int((daily < 0).sum()), d2=int((r >= 2).sum()), n=len(tr),
                hold=np.mean([t["hold_min"] for t in tr]), dmin=daily.min(), dmax=daily.max())


def key(t):
    return (t["day"], t["entry"], t["dir"])


def rs(t):
    return "+".join(dict.fromkeys(t["reasons"]))


# ── anchor ──
print("## 1. anchor (A P3-R0 재실행 vs 기존 REAL A)")
bad = []
for d in ALL:
    a = json.load(open(f"{O3}/REAL_A_{d}.json", encoding="utf-8")); b = json.load(open(f"{O2}/REAL_A_{d}.json", encoding="utf-8"))
    f = lambda o: [(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in o["orders"]]
    k = sum(1 for x in a["orders"] if x["side"] == "SELL")
    g = lambda o: [(e["exit_reason"], e["net_pnl"]) for e in o["ex"] if e["side"] == "SELL"][-k:] if k else []
    if f(a) != f(b) or g(a) != g(b):
        bad.append(d)
T = {k: sum((trades(load(k, d)) for d in ALL), []) for k in SRC}
M = {k: metrics(SEPT, [t for t in T[k] if t["day"] in SEPT]) for k in SRC}
print(f"  A 9월 총손익 {M['A']['krw']:+,.0f}원 (기존 +1,416,214) · 거래단위(시각/방향/수량/체결가/청산사유/net_pnl) 불일치일: {bad or '없음'}")
if bad or round(M["A"]["krw"]) != 1_416_214:
    print("  => INVALID REPLAY"); sys.exit(0)

print(f"\n## 2. 9월 {len(SEPT)}일 (REAL)")
print("| 전략 | 총손익(원) | 복리% | PF | MDD% | 승률% | 손실일 | +2%일 | 거래수 | 평균보유(분) | 최대1일손실 | 최대1일수익 |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|")
for k in SRC:
    m = M[k]
    print(f"| {NAME[k]} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['pf']:.2f} | {m['mdd']:.2f} | {m['win']:.1f} | {m['lossd']} | {m['d2']} | {m['n']} | {m['hold']:.0f} | {m['dmin']:+,.0f} | {m['dmax']:+,.0f} |")
print("\n| 날짜 | A | B | C | D |\n|---|---|---|---|---|")
for d in SEPT:
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(f"{sum(t['krw'] for t in T[k] if t['day'] == d):+,.0f}" for k in SRC) + " |")

# ── B vs C (H50) ──
print("\n## 4. D 계열: B(H50 있음) vs C(H50 없음)")
cmap = {key(t): t for t in T["C"] if t["day"] in SEPT}
bh = [t for t in T["B"] if t["day"] in SEPT and t["h50"]]
pairs = [(t, cmap.get(key(t))) for t in bh]
m_ = [(t, c, t["krw"] - c["krw"]) for t, c in pairs if c]
won = [z for z in m_ if z[2] > 0]; lost = [z for z in m_ if z[2] < 0]
print(f"  B 에서 반대신호 때 H50 HOLD 된 거래 {len(bh)}건 (같은 진입이 C 에 있는 {len(m_)}건 비교, 진입 자체가 달라진 {len(pairs) - len(m_)}건)")
print(f"  H50 때문에 더 번 원화 {sum(z[2] for z in won):+,.0f} ({len(won)}건) / 더 잃은 원화 {sum(z[2] for z in lost):+,.0f} ({len(lost)}건)")
print(f"  B−C 총손익 차이 {M['B']['krw'] - M['C']['krw']:+,.0f}원 (연쇄효과 포함) · MDD B {M['B']['mdd']:.2f} / C {M['C']['mdd']:.2f}")
m_.sort(key=lambda z: z[2])
for lab, zs in (("성공", m_[::-1][:2]), ("실패", m_[:2])):
    for t, c, dl in zs:
        print(f"    대표 {lab}: {t['day'][4:]} {t['entry']:%H:%M} {t['dir']} B {t['exit']:%H:%M} {rs(t)} {t['krw']:+,.0f} | C {c['exit']:%H:%M} {rs(c)} {c['krw']:+,.0f} | 차이 {dl:+,.0f}")

# ── TREND-BYPASS ──
print("\n## 5. P3-TREND-BYPASS (D) 집중 분석 — 9월")
amap = {key(t): t for t in T["A"]}
dmap = {key(t): t for t in T["D"]}
for lab, days in (("9월", SEPT), ("10/01 (참고)", ["20261001"])):
    bp = [t for t in T["D"] if t["day"] in days and t["bypass"]]
    ae = [t for t in T["A"] if t["day"] in days and t["regime"] == "CHOP" and t["trend_ok"]]
    print(f"  [{lab}] bypass 발동 {len(bp)}건 · A 에서 'trend_at_entry=True + regime=CHOP' 진입 {len(ae)}건 (A 에서는 전부 B3 관리)")
    pr = [(t, amap.get(key(t))) for t in bp]
    mm = [(t, a) for t, a in pr if a]
    imp = sum(1 for t, a in mm if t["krw"] > a["krw"]); wor = sum(1 for t, a in mm if t["krw"] < a["krw"])
    print(f"     같은 진입 매칭 {len(mm)}건: A(B3) 합 {sum(a['krw'] for _, a in mm):+,.0f} / D(BASE) 합 {sum(t['krw'] for t, _ in mm):+,.0f} / 차이 {sum(t['krw'] - a['krw'] for t, a in mm):+,.0f} · 개선 {imp} / 악화 {wor}")
    for t, a in pr:
        print(f"       {t['day'][4:]} {t['entry']:%H:%M} {t['dir']}: A " + (f"{a['exit']:%H:%M} {rs(a)} {a['krw']:+,.0f}" if a else "(A 에 같은 진입 없음)") +
              f" | D {t['exit']:%H:%M} {rs(t)} {t['krw']:+,.0f}")
    da = sum(t["krw"] for t in T["D"] if t["day"] in days) - sum(t["krw"] for t in T["A"] if t["day"] in days)
    print(f"     전체 순효과 (D − A, 연쇄 포함) {da:+,.0f}원")

print("\n## 6. (참고) 최근 6일 0922~1001 — 9월 판정과 무관")
print("| 전략 | 합계 | " + " | ".join(d[4:] for d in RECENT) + " |\n|---|---|" + "---|" * len(RECENT))
for k in SRC:
    tr = [t for t in T[k] if t["day"] in RECENT]
    print(f"| {NAME[k]} | {sum(t['krw'] for t in tr):+,.0f} | " + " | ".join(f"{sum(t['krw'] for t in tr if t['day'] == d):+,.0f}" for d in RECENT) + " |")
print("\n## 7. (참고) 10/01 거래 A vs D")
for k in ("A", "D"):
    for t in [t for t in T[k] if t["day"] == "20261001"]:
        print(f"  {k} {t['entry']:%H:%M:%S} {t['dir']} {t['q']}@{t['px_in']:.0f} regime={t['regime']} trend_at_entry={t['trend_ok']} bypass={t['bypass']} -> {t['exit']:%H:%M:%S}@{t['px_out']:.0f} {rs(t)} {t['net']:+.2f}% {t['krw']:+,.0f}")

with open(S + "/wk/lab/sept3_trades.md", "w", encoding="utf-8") as f:
    f.write("| 전략 | 날짜 | 플래그 | 진입 | 방향 | 체결가 | 청산 | 청산사유 | regime | trend_at_entry | bypass | H50 | net% | 손익 |\n|" + "---|" * 14 + "\n")
    for k in SRC:
        for t in T[k]:
            f.write(f"| {k} | {t['day'][4:]} | {t['flag']} | {t['entry']:%H:%M:%S} | {t['dir']} | {t['px_in']:.0f} | {t['exit']:%H:%M:%S} | {rs(t)} | "
                    f"{t['regime'] or '-'} | {t['trend_ok'] if t['trend_ok'] is not None else '-'} | {'Y' if t['bypass'] else '-'} | {'Y' if t['h50'] else '-'} | {t['net']:+.2f} | {t['krw']:+,.0f} |\n")
