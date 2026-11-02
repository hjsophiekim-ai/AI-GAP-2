"""E 돌파대기 regime 별 진단 + A/B 비교 (2026-11-02, REGIME_E 연구, research only).

  python d3_e_regime.py diag    --tag L85_D
  python d3_e_regime.py compare --a L85_D --b REGE_B [--bout <B 출력 폴더>]

진단 정의
  T+3 기준가 ref = T+3 직전 완성 1분봉 종가 (d1_diag 와 동일)
  E 추가비용%    = (E 체결가 / ref − 1) × 100   (+ 면 E 때문에 비싸게 샀다)
  regime         = 진입 시 p3 각인 (entries[].regime)
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import d1_diag as D        # noqa: E402
import d2_preflag as P     # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def load(tag, out_dir):
    rows = []
    files = [f for f in sorted(Path(out_dir).glob(f"{tag}_2026*.json")) if len(f.name) == len(tag) + 14 and f.stem.split("_")[-1] >= D.REAL_FROM]
    for f in files:
        day = f.stem.split("_")[-1]
        js = json.loads(f.read_text(encoding="utf-8"))
        cache = {}
        for ep in D.episodes(js, day):
            cache.setdefault(ep["sym"], D.bars(day, ep["sym"]))
            r = P.analyse(day, ep, cache[ep["sym"]])
            if r:
                r["kind"] = "E_BRK" if r["e_brk"] else "IMMEDIATE"
                r["e_cost"] = (r["entry_px"] / r["ref"] - 1) * 100
                rows.append(r)
    return pd.DataFrame(rows), [f.stem.split("_")[-1] for f in files]


def pf(x):
    g, l = x[x > 0].sum(), -x[x < 0].sum()
    return g / l if l else np.inf


def diag(a):
    d, days = load(a.tag, a.out)
    print(f"[{a.tag}] {len(days)}일 {days[0]}~{days[-1]}  {len(d)}거래  총손익 {d.pnl.sum():+,.0f}")
    print("\n[1] 진입 방식 × regime")
    for (reg, kind), g in d.groupby(["regime", "kind"]):
        print(f"  {str(reg):6s} {kind:9s} {len(g):3d}건  승률 {g.win.mean()*100:5.1f}%  PF {pf(g.pnl):5.2f}  손익 {g.pnl.sum():+12,.0f}"
              f"  E추가비용 평균 {g.e_cost.mean():+.2f}% 중앙 {g.e_cost.median():+.2f}%  mae3 중앙 {g.mae3.median():+.2f}  손절 {int(g.stop.sum())}")
    print("\n[2] E 돌파 진입 전수")
    print(f"  {'날짜':8s} {'플래그':5s} {'T+3':5s} {'진입':8s} {'방향':2s} {'regime':6s} {'ref':>7s} {'E체결':>7s} {'추가%':>6s}"
          f" {'mae1':>6s} {'mae3':>6s} {'mae6':>6s} {'mae9':>6s} {'mfe3':>6s} {'mfe9':>6s} {'MFE':>6s} BURST {'청산':24s} {'손익':>11s}")
    for r in d[d.e_brk].sort_values(["day", "entry"]).itertuples():
        print(f"  {r.day} {r.flag} {r.t3} {r.entry} {r.sym:2s} {str(r.regime):6s} {r.ref:7,.0f} {r.entry_px:7,.0f} {r.e_cost:+6.2f}"
              f" {r.mae1:+6.2f} {r.mae3:+6.2f} {r.mae6:+6.2f} {r.mae9:+6.2f} {r.mfe3:+6.2f} {r.mfe9:+6.2f} {r.mfe:+6.2f}"
              f" {'  O  ' if r.burst else ('  -  ' if r.pre_ok else ' n/a ')} {r.reason[:24]:24s} {r.pnl:+11,.0f}")
    print("\n[3] 월별 E 진입 (regime 별)")
    e = d[d.e_brk]
    for (m, reg), g in e.groupby(["month", "regime"]):
        print(f"  {m} {str(reg):6s} {len(g):2d}건  승 {g.win.mean()*100:5.1f}%  손익 {g.pnl.sum():+11,.0f}  추가비용 평균 {g.e_cost.mean():+.2f}%")
    d.to_csv(Path(__file__).resolve().parents[1] / "out" / f"e_diag_{a.tag}.csv", index=False, encoding="utf-8-sig")


def _mdd(daily):
    c = daily.cumsum()
    return float((c.cummax() - c).max())


def summary(d, days):
    daily = d.groupby("day").pnl.sum().reindex(days, fill_value=0.0)
    top = d.pnl.sort_values(ascending=False)
    return dict(n=len(d), total=d.pnl.sum(), pf=pf(d.pnl), mdd=_mdd(daily), worst=daily.min(),
                worst_day=daily.idxmin(), win=d.win.mean() * 100,
                ex1=d.pnl.sum() - top.iloc[:1].sum(), ex2=d.pnl.sum() - top.iloc[:2].sum())


def compare(a):
    A, days = load(a.a, a.out)
    B, days_b = load(a.b, a.bout or a.out)
    common = sorted(set(days) & set(days_b))
    A, B = A[A.day.isin(common)], B[B.day.isin(common)]
    print(f"A={a.a}  B={a.b}  공통 {len(common)}일 {common[0]}~{common[-1]}")
    sa, sb = summary(A, common), summary(B, common)
    print(f"\n{'':10s} {'A':>14s} {'B':>14s} {'Δ':>12s}")
    for k, lab in (("n", "거래수"), ("total", "총손익"), ("pf", "PF"), ("mdd", "MDD"), ("worst", "최악일"),
                   ("win", "승률%"), ("ex1", "상위1제외"), ("ex2", "상위2제외")):
        fa, fb = sa[k], sb[k]
        fmt = "{:14.3f}" if k == "pf" else ("{:14.1f}" if k == "win" else "{:+14,.0f}")
        print(f"{lab:10s} {fmt.format(fa)} {fmt.format(fb)} {fb - fa:+12,.2f}")
    print(f"  최악일자 A {sa['worst_day']}  B {sb['worst_day']}")
    print("\n[regime 별]")
    for reg in sorted(set(A.regime.astype(str)) | set(B.regime.astype(str))):
        ga, gb = A[A.regime.astype(str) == reg], B[B.regime.astype(str) == reg]
        print(f"  {reg:7s} A {len(ga):3d}건 {ga.pnl.sum():+12,.0f} 승 {ga.win.mean()*100 if len(ga) else 0:5.1f}%"
              f"  | B {len(gb):3d}건 {gb.pnl.sum():+12,.0f} 승 {gb.win.mean()*100 if len(gb) else 0:5.1f}%  | Δ {gb.pnl.sum() - ga.pnl.sum():+11,.0f}")
    print("\n[월별]")
    for m in sorted(set(A.month) | set(B.month)):
        ga, gb = A[A.month == m], B[B.month == m]
        print(f"  {m}  A {ga.pnl.sum():+12,.0f} ({len(ga)})  B {gb.pnl.sum():+12,.0f} ({len(gb)})  Δ {gb.pnl.sum() - ga.pnl.sum():+11,.0f}"
              f"   TREND Δ {gb[gb.regime=='TREND'].pnl.sum() - ga[ga.regime=='TREND'].pnl.sum():+11,.0f}"
              f"   CHOP Δ {gb[gb.regime=='CHOP'].pnl.sum() - ga[ga.regime=='CHOP'].pnl.sum():+11,.0f}")
    # 거래 단위 대조: (day, flag) 로 매칭
    ka = A.set_index(["day", "flag", "sym"]); kb = B.set_index(["day", "flag", "sym"])
    keys = sorted(set(ka.index) | set(kb.index))
    print("\n[달라진 거래 전수]  (day, 플래그, 종목)")
    n_chg = 0
    for k in keys:
        ra = ka.loc[[k]].iloc[0] if k in ka.index else None
        rb = kb.loc[[k]].iloc[0] if k in kb.index else None
        same = (ra is not None and rb is not None and ra.entry == rb.entry and ra.entry_px == rb.entry_px
                and ra.exit == rb.exit and abs(ra.pnl - rb.pnl) < 1)
        if same:
            continue
        n_chg += 1
        fa = f"{ra.entry} {ra.kind[:5]} @{ra.entry_px:,.0f} → {ra.exit[:5]} {ra.reason[:22]:22s} {ra.pnl:+10,.0f}" if ra is not None else "-" * 20
        fb = f"{rb.entry} {rb.kind[:5]} @{rb.entry_px:,.0f} → {rb.exit[:5]} {rb.reason[:22]:22s} {rb.pnl:+10,.0f}" if rb is not None else "-" * 20
        reg = (ra.regime if ra is not None else rb.regime)
        dlt = (rb.pnl if rb is not None else 0) - (ra.pnl if ra is not None else 0)
        print(f"  {k[0]} {k[1]} {k[2]} {str(reg):6s} | A {fa} | B {fb} | Δ {dlt:+10,.0f}")
    print(f"  → {n_chg}건 달라짐")
    chop_a = A[A.regime == "CHOP"].set_index(["day", "flag", "sym"])[["entry", "entry_px", "exit", "pnl"]]
    chop_b = B[B.regime == "CHOP"].set_index(["day", "flag", "sym"])[["entry", "entry_px", "exit", "pnl"]]
    ok = chop_a.round(0).sort_index().equals(chop_b.round(0).sort_index())
    print(f"\n[CHOP 동일성] CHOP 거래 A {len(chop_a)}건 vs B {len(chop_b)}건 — {'완전 동일' if ok else '차이 있음 (위 전수 참조)'}")
    adopt = (sb["total"] > sa["total"]) and (B[B.regime == 'TREND'].pnl.sum() > A[A.regime == 'TREND'].pnl.sum()) \
        and (sb["mdd"] <= sa["mdd"]) and (sb["worst"] >= sa["worst"])
    print(f"[채택 기준] TREND 수익 개선 + 전체 MDD/최악일 악화 없음 → {'충족' if adopt else '미충족'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["diag", "compare"])
    ap.add_argument("--tag", default="L85_D")
    ap.add_argument("--a", default="L85_D")
    ap.add_argument("--b", default="REGE_B")
    ap.add_argument("--out", default=os.environ.get("ARM3_OUT"))
    ap.add_argument("--bout", default=None)
    a = ap.parse_args()
    diag(a) if a.mode == "diag" else compare(a)


if __name__ == "__main__":
    main()
