"""9월 전략 비교 v2 집계 (production worker 전체 재생 결과만 사용). READ-ONLY.
손익(원) = production 거래원장 net_pnl (TradeCostEngine 실현 수수료). 일 수익률 = 원 / 1,000만원 예산.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
S = sys.argv[1]
O = S + "/wk/out2"
SEPT = sorted({os.path.basename(p).split("_")[2][:8] for p in glob.glob(O + "/REAL_A_2026*.json")})
SEPT = [d for d in SEPT if d.startswith("202609")]
RECENT = ["20260922", "20260923", "20260928", "20260929", "20260930", "20261001"]
LONG = "0193T0"
BUDGET = 10_000_000


def load(fill, var, d):
    p = f"{O}/{fill}_{var}_{d}.json"
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def trades(o):
    """BUY -> SELL(들) 로 묶고, 그날 SELL 원장행(마지막 k개)의 exit_reason/net_pnl 을 순서대로 붙인다."""
    sells = [x for x in o["orders"] if x["side"] == "SELL"]
    ex_sells = [e for e in o["ex"] if e["side"] == "SELL"][-len(sells):] if sells else []
    if len(ex_sells) != len(sells):
        raise RuntimeError(f"{o['D']} {o['VAR']} sell rows mismatch {len(ex_sells)} vs {len(sells)}")
    h50_times = [pd.Timestamp(s["detected_at"]) for s in o["sig"] if (s["order_result"] or "") == "H50_SMALL_WHIPSAW_HOLD"]
    exe = {pd.Timestamp(s["detected_at"]).strftime("%H:%M:%S"): s["signal_id"] for s in o["sig"] if s["order_result"] == "EXECUTED"}
    out, cur, si = [], None, 0
    for x in o["orders"]:
        t = pd.Timestamp(x["t"])
        if x["side"] == "BUY":
            sid = exe.get(t.strftime("%H:%M:%S"), "")
            cur = dict(day=o["D"], entry=t, dir="UP 레버" if x["sym"] == LONG else "DN 인버", px_in=x["px"], q=x["qty"],
                       flag=(sid.split("_")[1][:4] if sid else "?"), sold=0, legs=[], krw=0.0, reasons=[], exit=None)
            out.append(cur)
            continue
        e = ex_sells[si]; si += 1
        if cur is None:
            continue
        cur["sold"] += x["qty"]; cur["legs"].append((t, x["qty"], x["px"]))
        cur["krw"] += float(e["net_pnl"] or 0); cur["reasons"].append(e["exit_reason"] or "?"); cur["exit"] = t
        if cur["sold"] >= cur["q"]:
            avg = sum(q * p for _, q, p in cur["legs"]) / cur["q"]
            cur["px_out"] = avg
            cur["net"] = cur["krw"] / (cur["q"] * cur["px_in"]) * 100
            cur["hold_min"] = (cur["exit"] - cur["entry"]).total_seconds() / 60
            cur["h50"] = any(cur["entry"] < h <= cur["exit"] for h in h50_times)
            cur = None
    return [t for t in out if "net" in t]


def old_formula(o):
    """기존 anchor 계산식 (수수료 왕복 0.03% 근사) — anchor 대조용."""
    tot, cur = 0.0, None
    for r in o["orders"]:
        if r["side"] == "BUY":
            cur = dict(q=r["qty"], px=r["px"], sold=0, pr=0.0)
        elif cur:
            cur["sold"] += r["qty"]; cur["pr"] += r["qty"] * r["px"]
        if cur and cur["sold"] >= cur["q"]:
            tot += cur["pr"] - cur["q"] * cur["px"] - (cur["pr"] + cur["q"] * cur["px"]) * 0.03 / 200
            cur = None
    return tot


def metrics(days, tr):
    daily = np.array([sum(t["krw"] for t in tr if t["day"] == d) for d in days])
    r = daily / BUDGET * 100
    eq = np.cumprod(1 + r / 100)
    k = np.array([t["krw"] for t in tr]) if tr else np.zeros(1)
    return dict(krw=daily.sum(), comp=(eq[-1] - 1) * 100, pf=k[k > 0].sum() / max(-k[k < 0].sum(), 1.0),
                mdd=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100,
                win=(k > 0).mean() * 100 if tr else 0.0, lossd=int((daily < 0).sum()), d2=int((r >= 2).sum()),
                n=len(tr), hold=np.mean([t["hold_min"] for t in tr]) if tr else 0.0,
                dmin=daily.min(), dmax=daily.max())


# ── 8. 신뢰성 (anchor) ────────────────────────────────────────────────
ANCHOR_BAD = []
print("## 8. 신뢰성 확인 (anchor: 시각/방향/수량/체결가/청산사유 전부)")
for var, sfx, target in (("A", "", 554_926), ("B", "_N1", 259_194)):
    tot, same, n = 0.0, 0, 0
    diffs = []
    for d in SEPT:
        o = load("OLD", var, d)
        prev = json.load(open(f"{S}/wk/out/wk_{d}{sfx}.json", encoding="utf-8"))
        if o is None:
            diffs.append(d + ":없음"); continue
        tot += old_formula(o); n += 1
        a = [(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in o["orders"]]
        b = [(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in prev["orders"]]
        ks = sum(1 for x in o["orders"] if x["side"] == "SELL")
        ra = [e["exit_reason"] for e in o["ex"] if e["side"] == "SELL"][-ks:] if ks else []
        rb = [e["exit_reason"] for e in prev["ex"] if e["side"] == "SELL"][-ks:] if ks else []
        ok = a == b and ra == rb
        same += ok
        if not ok:
            diffs.append(d)
            ANCHOR_BAD.append(d)
    print(f"  {'A P3-R0' if var == 'A' else 'B N1-R0'}: OLD 재현 {tot:+,.0f}원 (기존 {target:+,} / 차이 {tot - target:+,.0f}) · 거래단위 동일 {same}/{n}일 {('불일치:' + ','.join(diffs)) if diffs else ''}")

# ── 4. 9월 18일 REAL ──────────────────────────────────────────────────
VARS = [("A", "A P3-R0"), ("B", "B N1-R0"), ("C", "C SIMPLE-N1-H50"), ("D", "D SIMPLE-N1-H50-NOSTOP"),
        ]
T = {}
for v, _ in VARS:
    T[v] = []
    for d in SEPT + ["20261001"]:
        o = load("REAL", v, d)
        if o is None:
            print(f"  !! REAL {v} {d} 결과 없음"); continue
        T[v] += trades(o)
print(f"\n## 4. 9월 {len(SEPT)}일 (REAL 체결)  대상일: {' '.join(d[4:] for d in SEPT)}")
print("| 전략 | 총손익(원) | 복리% | PF | MDD% | 승률% | 손실일 | +2%일 | 거래수 | 평균보유(분) | 최대1일손실 | 최대1일수익 |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|")
M = {}
for v, name in VARS:
    tr = [t for t in T[v] if t["day"] in SEPT]
    m = M[v] = metrics(SEPT, tr)
    print(f"| {name} | {m['krw']:+,.0f} | {m['comp']:+.2f} | {m['pf']:.2f} | {m['mdd']:.2f} | {m['win']:.1f} | {m['lossd']} | {m['d2']} | {m['n']} | {m['hold']:.0f} | {m['dmin']:+,.0f} | {m['dmax']:+,.0f} |")
print("\n### 날짜별 하루손익(원)")
print("| 날짜 | A | B | C | D |")
print("|---|---|---|---|---|")
for d in SEPT:
    print(f"| {d[4:6]}/{d[6:]} | " + " | ".join(f"{sum(t['krw'] for t in T[v] if t['day'] == d):+,.0f}" for v in "ABCD") + " |")

with open(S + "/wk/lab/sept2_trades.md", "w", encoding="utf-8") as f:
    f.write("| 전략 | 날짜 | 플래그 | 진입 | 방향 | 체결가 | 청산 | 청산사유 | H50 HOLD | net% | 손익(원) |\n|---|---|---|---|---|---|---|---|---|---|---|\n")
    for v in ("A", "B", "C", "D"):
        for t in T[v]:
            f.write(f"| {v} | {t['day'][4:]} | {t['flag']} | {t['entry']:%H:%M:%S} | {t['dir']} | {t['px_in']:.0f} | {t['exit']:%H:%M:%S} | "
                    f"{'+'.join(dict.fromkeys(t['reasons']))} | {'Y' if t['h50'] else '-'} | {t['net']:+.2f} | {t['krw']:+,.0f} |\n")

# ── 5. H50 (HOLD 시점 즉시청산 반사실) ───────────────────────────────
def etf_frame(d, sym):
    x = pd.read_csv(f"{S}/wk/data/replay_{d}_{'long' if sym == LONG else 'inverse'}_1m.csv")
    x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
    return x.set_index("datetime")


def real_px(ix, ts):
    m = ts.floor("min")
    if m in ix.index:
        r = ix.loc[m]
        if isinstance(r, pd.DataFrame):
            r = r.iloc[-1]
        p = float(r["open"]) + (float(r["close"]) - float(r["open"])) * (ts - m).total_seconds() / 60.0
        return round(p / 5.0) * 5.0
    done = ix[ix.index + pd.Timedelta(minutes=1) <= ts]
    return float(done["close"].iloc[-1])


FEE_LEG = 0.000036396
print("\n## 5. H50 검증 (9월) — HOLD 발생 시점에 즉시 전량청산(bid=그 순간 시세)했을 경우 대비")
for v in ("C", "D"):
    rows5 = []
    for d in SEPT:
        o = load("REAL", v, d)
        holds = [pd.Timestamp(s_["detected_at"]).tz_localize(None) if pd.Timestamp(s_["detected_at"]).tzinfo is None
                 else pd.Timestamp(s_["detected_at"]).tz_convert("Asia/Seoul").tz_localize(None)
                 for s_ in o["sig"] if (s_["order_result"] or "") == "H50_SMALL_WHIPSAW_HOLD"]
        for t in [t for t in T[v] if t["day"] == d]:
            en = t["entry"].tz_convert("Asia/Seoul").tz_localize(None); xt = t["exit"].tz_convert("Asia/Seoul").tz_localize(None)
            hs = [h for h in holds if en < h <= xt]
            if not hs:
                continue
            sym = LONG if t["dir"].startswith("UP") else "0197X0"
            p = real_px(etf_frame(d, sym), hs[0])
            cf = t["q"] * (p - t["px_in"]) - (t["q"] * (p + t["px_in"])) * FEE_LEG
            rows5.append((t["krw"] - cf, t, hs[0], p, cf))
    gain = [r for r in rows5 if r[0] > 0]; loss = [r for r in rows5 if r[0] < 0]
    print(f"  {v}: H50 HOLD 거래 {len(rows5)}건 | HOLD 로 이익 커짐(손실 줄어듦 포함) {len(gain)} / HOLD 로 손실 커짐(이익 줄어듦 포함) {len(loss)} / 동일 {len(rows5) - len(gain) - len(loss)}")
    print(f"     H50 순효과(실제 − 즉시청산) 합 {sum(r[0] for r in rows5):+,.0f}원  (이득 {sum(r[0] for r in gain):+,.0f} / 손해 {sum(r[0] for r in loss):+,.0f})")
    rows5.sort(key=lambda z: z[0])
    for lab, z in (("대표 성공", rows5[-1] if rows5 else None), ("대표 실패", rows5[0] if rows5 else None)):
        if z:
            dl, t, h, p, cf = z
            print(f"     {lab}: {t['day'][4:]} {t['entry']:%H:%M} {t['dir']} HOLD {h:%H:%M} 즉시청산가 {p:.0f} ({cf:+,.0f}) -> 실제 {t['exit']:%H:%M} {'+'.join(dict.fromkeys(t['reasons']))} ({t['krw']:+,.0f}) 차이 {dl:+,.0f}")
    ww = [t for t in T[v] if t["day"] in SEPT and any("WHIPSAW_WATCH" in r for r in t["reasons"])]
    print(f"     (whipsaw-watch 후속청산 발생 {len(ww)}건 — production 반대신호 경로의 일부라 유지)")

# ── P3 개입 분리 (A vs B, 같은 진입 매칭) ────────────────────────────
print("\n## (추가) P3 개입 거래 vs BASE와 동일 거래 — A(P3) 거래를 같은 날·진입시각·방향의 B(N1) 거래와 매칭")
bmap = {(t["day"], t["entry"], t["dir"]): t for t in T["B"] if t["day"] in SEPT}
grp = {"BASE와 동일(청산시각·사유·체결 같음)": [], "P3 개입(같은 진입, 청산이 달라짐)": [], "진입 자체가 B에 없음(슬롯/포지션 연쇄)": []}
for t in [t for t in T["A"] if t["day"] in SEPT]:
    b = bmap.get((t["day"], t["entry"], t["dir"]))
    if b is None:
        grp["진입 자체가 B에 없음(슬롯/포지션 연쇄)"].append((t, None))
    elif b["exit"] == t["exit"] and b["reasons"] == t["reasons"] and abs(b["px_out"] - t["px_out"]) < 1e-9:
        grp["BASE와 동일(청산시각·사유·체결 같음)"].append((t, b))
    else:
        grp["P3 개입(같은 진입, 청산이 달라짐)"].append((t, b))
bonly = [t for k, t in bmap.items() if k not in {(a["day"], a["entry"], a["dir"]) for a in T["A"]}]
for g, lst in grp.items():
    a_sum = sum(t["krw"] for t, _ in lst); b_sum = sum(b["krw"] for _, b in lst if b)
    print(f"  {g}: {len(lst)}건  A 손익 {a_sum:+,.0f}원" + (f" / 같은 진입의 B 손익 {b_sum:+,.0f}원 / 차이(A−B) {a_sum - b_sum:+,.0f}" if any(b for _, b in lst) else ""))
print(f"  B에만 있는 진입: {len(bonly)}건  B 손익 {sum(t['krw'] for t in bonly):+,.0f}원")
for t, b in grp["P3 개입(같은 진입, 청산이 달라짐)"]:
    print(f"    {t['day'][4:]} {t['entry']:%H:%M} {t['dir']}: A {t['exit']:%H:%M} {'+'.join(dict.fromkeys(t['reasons']))} {t['krw']:+,.0f} | B {b['exit']:%H:%M} {'+'.join(dict.fromkeys(b['reasons']))} {b['krw']:+,.0f}")

# ── 6. 손절 (C vs D) ──────────────────────────────────────────────────
print("\n## 6. 손절 검증 (C vs D, 9월)")
cmap = {(t["day"], t["entry"], t["dir"]): t for t in T["C"] if t["day"] in SEPT}
dmap = {(t["day"], t["entry"], t["dir"]): t for t in T["D"] if t["day"] in SEPT}
sl = [t for t in cmap.values() if "TIME_WINDOW_STOP_LOSS" in t["reasons"] or "STOP_LOSS" in t["reasons"]]
keep_good = cut_then_ran = enlarged = 0
rows = []
for t in sl:
    k = (t["day"], t["entry"], t["dir"])
    if k not in dmap:
        rows.append((t, None)); continue
    dd = dmap[k]
    diff = t["net"] - dd["net"]   # + = 손절 유지가 유리
    rows.append((t, dd))
    if diff > 0: keep_good += 1
    if dd["net"] - t["net"] >= 1.0: cut_then_ran += 1
    if t["net"] - dd["net"] >= 1.0: enlarged += 1
print(f"  C 의 손절 청산 {len(sl)}건 중 같은 진입이 D 에 있는 {sum(1 for _, d in rows if d)}건:")
print(f"    손절 유지가 유리 {keep_good} / 손절 뒤 원래방향으로 1%p 이상 더 간 거래 {cut_then_ran} / 손절 없애서 손실 1%p 이상 확대 {enlarged}")
for t, dd in rows:
    print(f"    {t['day'][4:]} {t['entry']:%H:%M} {t['dir']}: C {t['net']:+.2f}% ({t['exit']:%H:%M}) vs D " +
          (f"{dd['net']:+.2f}% ({dd['exit']:%H:%M} {'+'.join(dict.fromkeys(dd['reasons']))})" if dd else "진입 달라짐"))
print(f"  총 순효과(C−D) {M['C']['krw'] - M['D']['krw']:+,.0f}원 · MDD C {M['C']['mdd']:.2f} / D {M['D']['mdd']:.2f}")

# ── 7. 최근 6일 (별도 참고) ───────────────────────────────────────────
print("\n## 7. (참고) 최근 6일 0922~1001 — 9월 판정과 무관")
print("| 전략 | 총손익(원) | 거래수 | 손실일 | " + " | ".join(d[4:] for d in RECENT) + " |")
print("|---|---|---|---|" + "---|" * len(RECENT))
for v, name in VARS[:4]:
    tr = [t for t in T[v] if t["day"] in RECENT]
    m = metrics(RECENT, tr)
    print(f"| {name} | {m['krw']:+,.0f} | {m['n']} | {m['lossd']} | " + " | ".join(f"{sum(t['krw'] for t in tr if t['day'] == d):+,.0f}" for d in RECENT) + " |")

print("\nANCHOR_BAD", ANCHOR_BAD)
