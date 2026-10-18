"""최근 16일에서 CHOP + ±1.5% 를 이기는 청산 오버레이 찾기 (2026-10-17). READ-ONLY.

**이것은 in-sample 적합이다.** 사용자가 "최근 16일 기준으로 더 좋은 것" 을 요청했고,
16일이 곧 선택 기준이므로 구조적으로 과적합이다. 그래서 모든 후보에 대해
16일 / 20일 / 8~10월 / 89일 을 **같이** 찍어 일반화 손실을 눈으로 보게 한다.

오버레이 규칙 (경로 기반 산술, 재생 아님):
  진입 후 1분봉을 순서대로 보며
    · 저가가 SL 에 닿으면 손절      (같은 봉에서 둘 다 닿으면 손절 우선 — 보수적)
    · 고가가 TP 에 닿으면 익절
    · TRAIL 이 설정돼 있고 TP1 을 한 번 찍었으면, 고점 대비 TRAIL%p 되밀리면 청산
    · HOLD 분을 넘기면 그 시점 종가로 청산
  아무것도 안 걸리면 **원래 청산 그대로**(반대신호·장마감 의미 유지).
  수수료는 각 거래의 실측 왕복비용을 그대로 쓴다.

한계: 청산시각이 바뀌면 자금이 풀리는 시점이 달라져 뒤 거래가 바뀔 수 있다(2차효과 미반영).
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rep2  # noqa: E402

BARS = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/bb/sp/data"
W = rep2.LED
OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"
CAP = 1e7
_B = {}


def bars(day, sym):
    k = (day, sym)
    if k not in _B:
        tag = "long" if sym == "0193T0" else "inverse"
        p = f"{BARS}/replay_{day}_{tag}_1m.csv"
        if not os.path.exists(p):
            _B[k] = None
        else:
            x = pd.read_csv(p)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
            _B[k] = x.drop_duplicates("datetime", keep="last").sort_values("datetime")
    return _B[k]


def trades_full(o):
    """rep2.trades + legs(청산 체결) 보존 — 실측 수수료 역산용."""
    sells = [x for x in o["orders"] if x["side"] == "SELL"]
    ex_s = [e for e in o["ex"] if e["side"] == "SELL"][-len(sells):] if sells else []
    out, cur, si = [], None, 0
    for x in o["orders"]:
        t = pd.Timestamp(x["t"])
        if x["side"] == "BUY":
            if cur is not None and cur["sold"] < cur["q"]:
                tot = cur["q"] + x["qty"]
                cur["px_in"] = (cur["px_in"] * cur["q"] + x["px"] * x["qty"]) / tot
                cur["q"] = tot
                continue
            cur = dict(day=o["D"], entry=t, px_in=float(x["px"]), q=int(x["qty"]), sold=0,
                       krw=0.0, reasons=[], exit=None, sym=x["sym"], legs=[])
            out.append(cur)
            continue
        e = ex_s[si]
        si += 1
        if cur is None:
            continue
        cur["sold"] += x["qty"]
        cur["legs"].append((int(x["qty"]), float(x["px"])))
        cur["krw"] += float(e["net_pnl"] or 0)
        cur["reasons"].append(e["exit_reason"] or "?")
        cur["exit"] = t
        if cur["sold"] >= cur["q"]:
            cur["net"] = cur["krw"] / (cur["q"] * cur["px_in"]) * 100
            wavg = sum(q * p for q, p in cur["legs"]) / max(sum(q for q, _ in cur["legs"]), 1)
            cur["fee_pct"] = cur["net"] - (wavg / cur["px_in"] - 1) * 100
            cur = None
    return [t for t in out if "net" in t]


def apply_rule(t, tp, sl, trail, hold):
    """오버레이 손익(원)과 사유."""
    b = bars(t["day"], t["sym"])
    if b is None:
        return t["krw"], "원본"
    px, q, fee = t["px_in"], t["q"], t["fee_pct"]
    e0 = pd.Timestamp(t["entry"]).tz_localize(None)
    e1 = pd.Timestamp(t["exit"]).tz_localize(None)
    w = b[(b.datetime > e0) & (b.datetime <= e1)]
    peak, armed = -1e9, False
    for r in w.itertuples():
        lo = (r.low / px - 1) * 100
        hi = (r.high / px - 1) * 100
        if sl is not None and lo <= sl:
            return (sl + fee) / 100 * q * px, "SL"
        if tp is not None and hi >= tp:
            if trail is None:
                return (tp + fee) / 100 * q * px, "TP"
            armed = True
        if armed and trail is not None:
            peak = max(peak, hi)
            cl = (r.close / px - 1) * 100
            if peak - cl >= trail:
                return (cl + fee) / 100 * q * px, "TRAIL"
        if hold is not None and (r.datetime - e0).total_seconds() / 60 >= hold:
            cl = (r.close / px - 1) * 100
            return (cl + fee) / 100 * q * px, "HOLD"
    return t["krw"], "원본"


def main():
    days = [x.strip() for x in open(rep2.MY + "/days.txt") if x.strip()]
    T = []
    for d in days:
        p = f"{W}/CHOP_{d}.json"
        if os.path.exists(p):
            T += trades_full(json.load(open(p, encoding="utf-8")))
    have = sorted({t["day"] for t in T})
    print(f"CHOP 거래 {len(T)}건 / 원장 {len(have)}일\n")

    WINS = [("16일", days[-16:]), ("20일", days[-20:]), ("30일", days[-30:]),
            ("8~10월", [d for d in days if d >= "20260803"]), ("전체", days)]

    def evaluate(tp, sl, trail, hold):
        res = {}
        pnl = {t["day"]: 0.0 for t in T}
        ks = {}
        for t in T:
            k, _ = apply_rule(t, tp, sl, trail, hold)
            pnl[t["day"]] = pnl.get(t["day"], 0.0) + k
            ks.setdefault(t["day"], []).append(k)
        for lab, sub in WINS:
            dp = np.array([pnl.get(d, 0.0) for d in sub])
            kk = np.array([v for d in sub for v in ks.get(d, [])])
            eq = np.cumprod(1 + dp / CAP)
            mdd = float((eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100) if len(eq) else 0
            res[lab] = (dp.sum(), mdd, (kk[kk > 0].sum() / max(-kk[kk < 0].sum(), 1)) if len(kk) else 0)
        return res

    # ── 선언한 격자 (사후에 늘리지 않는다) ─────────────────────────────────
    cands = [("현행 CHOP (오버레이 없음)", None, None, None, None),
             ("±1.5% (사용자 기준선)", 1.5, -1.5, None, None)]
    for tp in (1.0, 1.5, 2.0, 2.5, 3.0, None):
        for sl in (-0.75, -1.0, -1.5, -2.0, None):
            if (tp, sl) in ((1.5, -1.5), (None, None)):
                continue
            cands.append((f"TP{tp if tp else '-'} / SL{sl if sl else '-'}", tp, sl, None, None))
    for tr in (0.5, 1.0):
        for tp in (1.0, 1.5):
            cands.append((f"TP{tp} 후 트레일 {tr}%p / SL-1.5", tp, -1.5, tr, None))
    for h in (20, 30):
        cands.append((f"±1.5% + {h}분 시간청산", 1.5, -1.5, None, h))

    rows = []
    for name, tp, sl, tr, h in cands:
        r = evaluate(tp, sl, tr, h)
        rows.append(dict(name=name, **{f"{k}_tot": v[0] for k, v in r.items()},
                         **{f"{k}_mdd": v[1] for k, v in r.items()},
                         **{f"{k}_pf": v[2] for k, v in r.items()}))
    x = pd.DataFrame(rows).sort_values("16일_tot", ascending=False)
    x.to_csv(OUT + "/g2_search.csv", index=False, encoding="utf-8-sig")
    base16 = float(x[x.name == "±1.5% (사용자 기준선)"]["16일_tot"].iloc[0])
    chop16 = float(x[x.name.str.startswith("현행 CHOP")]["16일_tot"].iloc[0])
    chop810 = float(x[x.name.str.startswith("현행 CHOP")]["8~10월_tot"].iloc[0])

    print(f"기준: CHOP 16일 {chop16:+,.0f} / CHOP+±1.5% 16일 {base16:+,.0f} / CHOP 8~10월 {chop810:+,.0f}")
    print("\n[최근 16일 성적순]  ★ = 16일에서 ±1.5% 를 이김 / ◎ = 그러면서 8~10월도 CHOP 이상")
    print(f"  {'규칙':28s} {'16일':>12s} {'20일':>12s} {'30일':>12s} {'8~10월':>12s} {'전체':>12s} "
          f"{'16일MDD':>8s} {'8~10MDD':>8s}")
    for _, r in x.iterrows():
        mark = ("◎" if (r["16일_tot"] > base16 and r["8~10월_tot"] >= chop810)
                else ("★" if r["16일_tot"] > base16 else " "))
        print(f" {mark}{r['name']:28s} {r['16일_tot']:+12,.0f} {r['20일_tot']:+12,.0f} "
              f"{r['30일_tot']:+12,.0f} {r['8~10월_tot']:+12,.0f} {r['전체_tot']:+12,.0f} "
              f"{r['16일_mdd']:8.2f} {r['8~10월_mdd']:8.2f}")


if __name__ == "__main__":
    main()
