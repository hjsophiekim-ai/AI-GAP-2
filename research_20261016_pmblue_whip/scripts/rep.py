"""변형 vs BASE 보고 (2026-10-16). READ-ONLY.

사용: python rep.py <제목> <변형태그...>
  예: python rep.py "연구2 PM-BLUE VWAP VETO" PMRELAX PMOFF
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import anal  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 220)


def main():
    title = sys.argv[1]
    tags = sys.argv[2:]
    dys = anal.days()
    base = anal.load("BASE")
    print(f"### {title}")
    print(f"재생 {len(dys)}일 (05-27~10-08) / BASE = production 현행(E + SLOW-TP2 ON)\n")

    loaded = {"BASE": base}
    for t in tags:
        loaded[t] = anal.load(t)
        n = len([d for d in dys if os.path.exists(f"{anal.OUT}/{t}_{d}.json")])
        print(f"  {t}: 변형 원장 {n}일, 나머지 {len(dys) - n}일은 BASE 재사용(규칙 비해당일)")

    for label, _, sub in anal.halves(base):
        print(f"\n== {label} ({len(sub)}일) ==")
        for name, tr in loaded.items():
            t = tr[tr.day.isin(sub)] if len(tr) else tr
            m = anal.metrics(t, sub)
            d = "" if name == "BASE" else f"  Δ {m['총손익'] - anal.metrics(base[base.day.isin(sub)], sub)['총손익']:+,.0f}"
            print(anal.fmt(name, m) + d)
        print()
        for name, tr in loaded.items():
            t = tr[tr.day.isin(sub)] if len(tr) else tr
            print(anal.fmt_top(name, anal.metrics(t, sub)))

    for t in tags:
        var = loaded[t]
        print(f"\n\n===== {t} 상세 =====")
        db = pd.Series(anal.daily(base, dys), index=dys)
        dv = pd.Series(anal.daily(var, dys), index=dys)
        diff = (dv - db)
        ch = diff[diff.abs() > 1e-6]
        print(f"달라진 날 {len(ch)}일 / 합 {ch.sum():+,.0f} "
              f"(+{(ch > 0).sum()}일 {ch[ch > 0].sum():+,.0f} / -{(ch < 0).sum()}일 {ch[ch < 0].sum():+,.0f})")
        if len(ch):
            print(pd.DataFrame(dict(BASE=db[ch.index].round(0), VAR=dv[ch.index].round(0),
                                    Δ=ch.round(0))).to_string())
        bw = anal.big_winner_check(base, var)
        if len(bw):
            lost = bw[bw.status != "유지"]
            print(f"\n큰 승자(BASE +30만 이상) {len(bw)}건 중 영향받은 것 {len(lost)}건, "
                  f"손익변화 {lost.delta.sum():+,.0f}")
            if len(lost):
                print(lost.to_string(index=False))
        newtr = var.merge(base[["day", "entry", "sym"]], on=["day", "entry", "sym"], how="left", indicator=True)
        newtr = newtr[newtr._merge == "left_only"]
        if len(newtr):
            print(f"\n새로 생긴 거래 {len(newtr)}건 합 {newtr.krw.sum():+,.0f} "
                  f"승률 {(newtr.krw > 0).mean() * 100:.0f}%")
            print(newtr[["day", "entry", "exit", "sym", "krw", "net", "hold", "reason"]].round(2).to_string(index=False))
        gone = base.merge(var[["day", "entry", "sym"]], on=["day", "entry", "sym"], how="left", indicator=True)
        gone = gone[gone._merge == "left_only"]
        if len(gone):
            print(f"\n사라진 거래 {len(gone)}건 합 {gone.krw.sum():+,.0f}")
            print(gone[["day", "entry", "exit", "sym", "krw", "net", "hold", "reason"]].round(2).to_string(index=False))

        # 날짜 bootstrap (일손익 차이)
        rng = np.random.default_rng(0)
        for lab, sub in (("전체", dys), ("8~10월", [d for d in dys if d >= "20260801"])):
            x = diff[sub].to_numpy()
            bs = [rng.choice(x, len(x), replace=True).sum() for _ in range(5000)]
            print(f"  bootstrap {lab}: Δ {x.sum():+,.0f}, 95% [{np.percentile(bs, 2.5):+,.0f}, "
                  f"{np.percentile(bs, 97.5):+,.0f}], P(Δ>0) {np.mean(np.array(bs) > 0) * 100:.0f}%")


if __name__ == "__main__":
    main()
