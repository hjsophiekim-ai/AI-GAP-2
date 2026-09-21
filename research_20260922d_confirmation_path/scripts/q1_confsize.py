# -*- coding: utf-8 -*-
"""[Q1] ETF confirmation quality -> **사이징 전용** 적용. READ-ONLY.

계약
----
* 진입집합 / 거래수 / 슬롯 / 청산 전부 BASE(N1+C1) 와 동일 — 게이트가 아니다.
* weak confirmation 거래도 **반드시 진입**한다. 주문수량만 줄인다.
* **감액분을 뒤 거래에 재배분하지 않는다** — 일예산(30M) 소진과 노출누적은
  BASE 배수로 계산하고, 최종 수량에만 감액배수를 곱한다.
* 청산자금 당일 재사용 없음.

weak 정의 (새 threshold 최적화 없음)
    confirmation 구간(플래그봉 시작 ~ 진입 직전) 보유ETF 수익률 <= 0%
    = "확인 6분 동안 ETF 가 신호 방향으로 전혀 안 따라왔다"
    앞선 연구에서 78일 0.348 / 30일 0.462 / OOS48 0.259 로 **세 창 방향 일치**.
    임계 0 은 자연경계이지 최적화 산물이 아니다.

정확성: net_pct 는 수량무관(비용 전부 수량비례·최소수수료 0·ETF 세금 0),
청산 판정은 가격기준이라 수량에 의존하지 않는다 => 해석적 계산이 정확하다.
"""
from __future__ import annotations
import sys
from collections import defaultdict
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K
import p1_confirm as C

A, TS, D, W30, OOS = C.A, C.TS, C.D, C.W30, C.OOS
FE = C.FE
BASE = K.size_chain(TS, None)          # 현행 N1+C1 사이징
BMAP = {(t["date"], t["entry_time"]): t for t in BASE}
AMAP = {(t["date"], t["entry_time"]): t for t in A}
CAP = K.DAILY_CAPITAL
rng = np.random.default_rng(20260922)


def follow(t):
    return FE.get((t["date"], t["entry_time"]), {}).get("ETF_추종%", np.nan)


def is_weak(t):
    v = follow(t)
    return bool(v == v and v <= 0.0)


def apply(mult, *, rounding="floor", weak_fn=None):
    """BASE 예산배분 고정 + 최종 수량에만 감액. 재배분 없음."""
    wf = weak_fn or is_weak
    out = []
    for b in BASE:
        r = dict(b)
        t = AMAP[(b["date"], b["entry_time"])]
        r["weak"] = wf(t)
        r["mfe"] = t["mfe"]
        q0 = int(b["qty"])
        q = q0 if not r["weak"] else (int(q0 * mult) if rounding == "floor"
                                      else int(round(q0 * mult)))
        r["qty"] = q
        r["notional_krw"] = q * b["entry_price"]
        r["pnl_krw"] = r["notional_krw"] * b["net_pct"] / 100.0
        r["reserved_krw"] = b["actual_krw"]        # 예산에서 차지한 자리(불변)
        out.append(r)
    return out


def util(rows, W):
    SW = set(W)
    by = defaultdict(float)
    for r in rows:
        if r["date"] in SW:
            by[r["date"]] += r["notional_krw"]
    return (np.mean([v / CAP for v in by.values()]) * 100) if by else 0.0


def stats(rows, W):
    SW = set(W)
    g = [r for r in rows if r["date"] in SW]
    return {"pl": K.krw_pnl(rows, W), "pf": K.pf(rows, W), "mdd": K.mdd_krw(rows, W),
            "n": len(g), "util": util(rows, W),
            "t1": K.excl_top_krw(rows, W, 1), "t3": K.excl_top_krw(rows, W, 3),
            "t5": K.excl_top_krw(rows, W, 5)}


MULTS = [("A BASE  x1.00", 1.00), ("B weak x0.75", 0.75),
         ("C weak x0.50", 0.50), ("D weak x0.25", 0.25)]
RUNS = {nm: apply(m) for nm, m in MULTS}

if __name__ == "__main__":
    weak = [t for t in A if is_weak(t)]
    print("=" * 130)
    print("[Q1] ETF confirmation quality -> 사이징 전용 (진입 거절 없음)")
    print("=" * 130)
    print(f"  weak 정의: confirmation 구간 ETF 추종 <= 0%   -> 78일 {len(weak)}/{len(A)}건 "
          f"({len(weak)/len(A)*100:.1f}%)")
    for W, tag in ((W30, "30일 IS"), (OOS, "앞48일 OOS"), (D, "78일")):
        SW = set(W)
        g = [t for t in weak if t["date"] in SW]
        gb = [BMAP[(t["date"], t["entry_time"])] for t in g]
        print(f"    {tag:10s} weak {len(g):3d}건  그 실현합 **{sum(x['pnl_krw'] for x in gb):+12,.0f}** KRW  "
              f"평균 net {np.mean([t['net_pct'] for t in g]):+6.3f}%  "
              f"MFE>=3% {sum(1 for t in g if t['mfe']>=3):2d}건 / >=8% {sum(1 for t in g if t['mfe']>=8):d}건")
    print("\n  => 감액 효과는 구조적으로 `-(1-배수) x (weak 거래 실현합)` 이다.")
    print("     즉 **weak 실현합이 음수인 창에서만** 양수가 된다.")

    for W, tag in ((W30, "30일 IS"), (OOS, "앞48일 OOS"), (D, "78일 전체")):
        base = stats(RUNS["A BASE  x1.00"], W)
        print(f"\n── {tag} ──  BASE {base['pl']:,.0f} KRW / PF {base['pf']:.3f} / "
              f"MDD {base['mdd']:.2f}% / 사용률 {base['util']:.1f}% / 거래 {base['n']}")
        print(f"  {'전략':14s} {'거래':>4s} {'P/L':>12s} {'uplift':>12s} {'PF':>6s} {'MDD%':>7s} "
              f"{'사용률':>6s} {'-Top1Δ':>11s} {'-Top3Δ':>11s} {'-Top5Δ':>11s} {'boot P(>0)':>10s}")
        print("  " + "-" * 122)
        for nm, m in MULTS:
            s = stats(RUNS[nm], W)
            da = K.daily_krw(RUNS["A BASE  x1.00"], W); db = K.daily_krw(RUNS[nm], W)
            v = np.array([db[d] - da[d] for d in W])
            bt = v[rng.integers(0, len(v), size=(10000, len(v)))].sum(axis=1)
            p0 = (bt > 0).mean() * 100 if v.any() else 0.0
            print(f"  {nm:14s} {s['n']:4d} {s['pl']:12,.0f} {s['pl']-base['pl']:+12,.0f} "
                  f"{s['pf']:6.3f} {s['mdd']:7.2f} {s['util']:5.1f}% "
                  f"{s['t1']-base['t1']:+11,.0f} {s['t3']-base['t3']:+11,.0f} "
                  f"{s['t5']-base['t5']:+11,.0f} {p0:9.2f}%")

    # ── 분해: bad-trade 감액효과 vs runner 기회손실 ─────────────────────
    print("\n" + "=" * 120)
    print("분해 — weak 거래를 손익 부호로 나눠 본 감액 효과 (배수별)")
    print("=" * 120)
    for W, tag in ((W30, "30일 IS"), (OOS, "앞48일 OOS"), (D, "78일")):
        SW = set(W)
        g = [BMAP[(t["date"], t["entry_time"])] for t in A if is_weak(t) and t["date"] in SW]
        lose = [x for x in g if x["pnl_krw"] < 0]
        win = [x for x in g if x["pnl_krw"] >= 0]
        gr = [x for x in g if AMAP[(x["date"], x["entry_time"])]["mfe"] >= 3]
        r8 = [x for x in g if AMAP[(x["date"], x["entry_time"])]["mfe"] >= 8]
        print(f"\n  {tag}  weak {len(g)}건")
        print(f"    {'배수':>6s} {'손실weak 감액이익':>16s} {'수익weak 기회손실':>16s} "
              f"{'MFE>=3% 기회손실':>16s} {'MFE>=8% 기회손실':>16s} {'순효과':>13s}")
        for _, m in MULTS[1:]:
            k = 1 - m
            print(f"    {m:6.2f} {-k*sum(x['pnl_krw'] for x in lose):+16,.0f} "
                  f"{-k*sum(x['pnl_krw'] for x in win):+16,.0f} "
                  f"{-k*sum(x['pnl_krw'] for x in gr):+16,.0f} "
                  f"{-k*sum(x['pnl_krw'] for x in r8):+16,.0f} "
                  f"{-k*sum(x['pnl_krw'] for x in g):+13,.0f}")

    # ── 정수주 반올림 / 대체 feature 민감도 ────────────────────────────
    print("\n" + "=" * 112)
    print("민감도")
    print("=" * 112)
    print(f"  {'조건':34s} {'30일':>13s} {'OOS48':>13s} {'78일':>13s}")
    b0 = {W: stats(RUNS["A BASE  x1.00"], W)["pl"] for W in (tuple(W30), tuple(OOS), tuple(D))}
    for tag, kw in (("기준 (x0.50, 정수주 내림)", dict(rounding="floor")),
                    ("x0.50, 정수주 반올림", dict(rounding="round"))):
        r = apply(0.50, **kw)
        print(f"  {tag:34s} " + " ".join(
            f"{K.krw_pnl(r,W)-b0[tuple(W)]:+13,.0f}" for W in (W30, OOS, D)))
    wf2 = lambda t: FE.get((t["date"], t["entry_time"]), {}).get("ETF_역행봉수", 0) >= 3
    r = apply(0.50, weak_fn=wf2)
    print(f"  {'(대조) weak=ETF역행>=3봉, x0.50':34s} " + " ".join(
        f"{K.krw_pnl(r,W)-b0[tuple(W)]:+13,.0f}" for W in (W30, OOS, D)))
