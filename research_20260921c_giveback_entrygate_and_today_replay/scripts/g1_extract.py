# -*- coding: utf-8 -*-
"""[G] MFE >= +1.5% 거래의 KEEPER / GIVEBACK 분해 — 최근 30영업일. READ-ONLY.

section 1 : 대상 추출 + 분류
production/config/code 무수정. 미래정보 없음(분류는 사후 라벨일 뿐,
rule 에는 도달 시점까지의 정보만 쓴다 — g2 이후).
"""
from __future__ import annotations
import sys, pickle
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P

THR = 1.50
CTX = pickle.load(open("_ctx_B.pkl", "rb"))
D = CTX["dates"]
W30 = D[-30:]
S30 = set(W30)
TS = K.load()
A = K.size_chain(TS, None)
A30 = [t for t in A if t["date"] in S30]


def touch_profile(t):
    """+1.5% 최초 도달봉과 그 이후 경로 통계. 도달 안 하면 None."""
    i, _ = P.first_touch(t, THR)
    if i is None:
        return None
    pth = P.path(t)
    sym, ep = t["symbol"], t["entry_price"]
    n_hi = [P.net_of(sym, ep, float(h)) for h in pth["high"]]
    n_lo = [P.net_of(sym, ep, float(l)) for l in pth["low"]]
    after_hi, after_lo = n_hi[i:], n_lo[i:]
    mfe = max(n_hi)                       # 전 구간 MFE (high 기준)
    peak_after = max(after_hi)            # 도달 이후 최고
    # 도달 이후 '고점 -> 이후 저점' 최대 낙폭
    run, dd = -1e9, 0.0
    for h, l in zip(after_hi, after_lo):
        run = max(run, h)
        dd = max(dd, run - l)
    return {
        "touch_idx": i,
        "touch_time": str(pth["datetime"].iloc[i]),
        "touch_min": int((pd.Timestamp(pth["datetime"].iloc[i])
                          - pd.Timestamp(t["entry_time"])).total_seconds() // 60),
        "bars_after": len(pth) - 1 - i,
        "mfe_high": mfe,
        "peak_after": peak_after,
        "extra_up": peak_after - THR,
        "max_dd_after": dd,               # 도달 이후 최대 반납폭(고점대비)
        "min_after": min(after_lo),
        "mae_before": min(n_lo[:i + 1]),
    }


ROWS = []
for t in A30:
    tp = touch_profile(t)
    if tp is None:
        continue
    r = dict(t)
    r.update(tp)
    r["runner"] = t["peak_net_pct"] >= 8.0
    r["give_from_mfe"] = r["mfe_high"] - t["net_pct"]
    ROWS.append(r)
ROWS.sort(key=lambda x: (x["date"], x["entry_time"]))

# ── 분류 ────────────────────────────────────────────────────────────────
def cls_def1(r):   # 정의1: 최종 realized < +1.0%
    return r["net_pct"] < 1.0
def cls_def2(r):   # 정의2: MFE 대비 1.0%p 이상 반납
    return r["give_from_mfe"] >= 1.0
def severe(r):
    return r["net_pct"] <= 0.0

if __name__ == "__main__":
    print("=" * 150)
    print(f"[G section1] MFE +{THR:.1f}% 도달 거래 — 30영업일 {W30[0]} ~ {W30[-1]}")
    print("=" * 150)
    print(f"  30일 전체 거래 {len(A30)}건 / +{THR:.1f}% 도달 {len(ROWS)}건 "
          f"({len(ROWS)/len(A30)*100:.1f}%) / 미도달 {len(A30)-len(ROWS)}건")
    print(f"  BASE 30일: {K.krw_pnl(A, W30):,.0f} KRW / PF {K.pf(A, W30):.3f} / "
          f"MDD {K.mdd_krw(A, W30):.2f}%")
    print()
    hdr = (f"  {'날짜':9s} {'방향':9s} {'sl':>2s} {'진입':5s} {'도달':5s} {'+분':>4s} "
           f"{'MFE':>6s} {'최종%':>7s} {'추가상승':>7s} {'반납(고점)':>8s} "
           f"{'MFE-최종':>8s} {'run':>3s} {'청산사유':26s}")
    print(hdr); print("  " + "-" * 146)
    for r in ROWS:
        print(f"  {r['date']:9s} {r['direction']:9s} {r['slot']:2d} "
              f"{r['entry_time'][11:16]:5s} {r['touch_time'][11:16]:5s} {r['touch_min']:4d} "
              f"{r['mfe_high']:6.2f} {r['net_pct']:7.2f} {r['extra_up']:7.2f} "
              f"{r['max_dd_after']:8.2f} {r['give_from_mfe']:8.2f} "
              f"{'Y' if r['runner'] else '-':>3s} {r['exit_reason']:26s}")

    print()
    print("=" * 110)
    print("분류")
    print("=" * 110)
    for tag, fn in (("정의1  최종 realized < +1.0%", cls_def1),
                    ("정의2  MFE 대비 1.0%p 이상 반납", cls_def2)):
        g = [r for r in ROWS if fn(r)]
        k = [r for r in ROWS if not fn(r)]
        sv = [r for r in g if severe(r)]
        print(f"\n[{tag}]")
        print(f"  A. KEEPERS        {len(k):3d}건  평균 최종 {sum(x['net_pct'] for x in k)/max(1,len(k)):+6.2f}%  "
              f"평균 MFE {sum(x['mfe_high'] for x in k)/max(1,len(k)):5.2f}%  "
              f"실현 {sum(x['pnl_krw'] for x in k):+12,.0f} KRW  runner {sum(1 for x in k if x['runner'])}건")
        print(f"  B. GIVEBACK       {len(g):3d}건  평균 최종 {sum(x['net_pct'] for x in g)/max(1,len(g)):+6.2f}%  "
              f"평균 MFE {sum(x['mfe_high'] for x in g)/max(1,len(g)):5.2f}%  "
              f"실현 {sum(x['pnl_krw'] for x in g):+12,.0f} KRW  runner {sum(1 for x in g if x['runner'])}건")
        print(f"     C. SEVERE(<=0) {len(sv):3d}건  평균 최종 {sum(x['net_pct'] for x in sv)/max(1,len(sv)):+6.2f}%  "
              f"평균 MFE {sum(x['mfe_high'] for x in sv)/max(1,len(sv)):5.2f}%  "
              f"실현 {sum(x['pnl_krw'] for x in sv):+12,.0f} KRW")
        print(f"  B 가 놓친 돈(MFE-최종 합, 명목): "
              f"{sum(x['give_from_mfe'] for x in g):.2f}%p")

    both = [r for r in ROWS if cls_def1(r) and cls_def2(r)]
    only1 = [r for r in ROWS if cls_def1(r) and not cls_def2(r)]
    only2 = [r for r in ROWS if cls_def2(r) and not cls_def1(r)]
    print(f"\n  두 정의 교집합 {len(both)}건 / 정의1 단독 {len(only1)}건 / 정의2 단독 {len(only2)}건")
    print(f"  정의2 단독(수익은 크지만 많이 반납): "
          + ", ".join(f"{r['date']} {r['entry_time'][11:16]}({r['net_pct']:+.1f}%,MFE{r['mfe_high']:.1f})" for r in only2))
    print(f"  정의1 단독(반납은 작지만 수익 미미): "
          + ", ".join(f"{r['date']} {r['entry_time'][11:16]}({r['net_pct']:+.1f}%,MFE{r['mfe_high']:.1f})" for r in only1))
