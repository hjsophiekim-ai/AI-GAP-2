"""후보 필터 산술 스크리닝 (2026-10-17). READ-ONLY. 재생 아님 — 재생 전 솎아내기용.

스크리닝 2단계에서 살아남은 것만 full replay 로 넘긴다.

후보는 **전부 사전 고정**이고 임계값은 production 상수에서 그대로 가져온다 (탐색 금지):
  CHOP_MC_PE_MAX = 0.20, CHOP_MC_XC_MIN = 3, CHOP_MC_LOOKBACK_MIN = 60
  -> 이미 production 에 있는 `chop_defense._path_is_chop` 와 **같은 식**이다.
     지금은 '오전 구간 no-trade' 판정에만 쓰이는데, 후보는 이걸 **개별 진입 필터**로
     쓰는 것이다 (새 필터 추가가 아니라 기존 조건의 적용 범위 변경).

  P1  PATH-CHOP SKIP (전시간) : pe60 < 0.20 AND xc60 >= 3 이면 진입 안 함
  P2  PATH-CHOP SKIP (CHOP 라벨만)
  P3  PE-ONLY       (전시간)  : pe60 < 0.20 이면 진입 안 함
  E1  EMA-STACK 요구 (전시간) : ema_stack == 0 이면 진입 안 함  (TEG 조건을 오전까지 확대)
  E0  EMA-STACK 금지 (반대방향 확인용) : ema_stack == 1 이면 진입 안 함

산술 근사의 한계: 진입을 빼면 슬롯이 남아 **뒤 거래가 달라진다**. 여기서는 그걸 무시한다
(= 상한/하한 감 잡기용). 부호가 확실한 후보만 재생으로 확인한다.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"
PE_MAX, XC_MIN = 0.20, 3


def main():
    d = pd.read_csv(OUT + "/f1_features.csv", dtype={"day": str})
    days = sorted(d.day.unique())
    r10 = d.day.isin(days[-10:])
    d["half"] = np.where(d.day < "20260801", "5~7월", "8~10월")

    cands = {
        "P1 PATH-CHOP 전시간": (d.pe60 < PE_MAX) & (d.xc60 >= XC_MIN),
        "P2 PATH-CHOP CHOP라벨만": (d.pe60 < PE_MAX) & (d.xc60 >= XC_MIN) & (d.chop == 1),
        "P3 PE<0.20 전시간": (d.pe60 < PE_MAX),
        "E1 EMA정렬 요구(전시간)": (d.ema_stack == 0),
        "E0 EMA정렬 금지(대조)": (d.ema_stack == 1),
    }
    print(f"BASE 162거래 / 합 {d.krw.sum():+,.0f} "
          f"(5~7월 {d[d.half == '5~7월'].krw.sum():+,.0f} / 8~10월 {d[d.half == '8~10월'].krw.sum():+,.0f} "
          f"/ 최근10일 {d[r10].krw.sum():+,.0f})")
    print("\n'막는다' = 그 거래를 안 했다고 가정했을 때 (슬롯 2차효과 무시한 산술 근사)")
    print(f"\n  {'후보':26s} {'막는수':>6s} {'막은금액':>12s} {'막은승률':>7s} "
          f"{'5~7월Δ':>12s} {'8~10월Δ':>12s} {'최근10일Δ':>11s} {'상위3제외Δ':>12s}")
    for name, blk in cands.items():
        b = d[blk]
        if not len(b):
            print(f"  {name:26s} {0:6d}")
            continue
        # 막으면 손익은 -(막은 거래의 합)
        dlt = -b.krw.sum()
        d57 = -b[b.half == "5~7월"].krw.sum()
        d810 = -b[b.half == "8~10월"].krw.sum()
        d10 = -b[r10.loc[b.index]].krw.sum()
        bydaysum = (-b.groupby("day").krw.sum())
        top3 = bydaysum.sort_values(ascending=False).head(3).sum()
        print(f"  {name:26s} {len(b):6d} {b.krw.sum():+12,.0f} {b.win.mean() * 100:6.0f}% "
              f"{d57:+12,.0f} {d810:+12,.0f} {d10:+11,.0f} {dlt - top3:+12,.0f}")

    print("\n\n[P1 이 막는 거래 전부]")
    b = d[cands["P1 PATH-CHOP 전시간"]]
    cols = ["day", "dec", "dir", "krw", "net", "mfe_b", "pe60", "xc60", "ema_stack", "chop", "reason"]
    print(b.sort_values("krw")[cols].round(3).to_string(index=False))

    print("\n\n[pe60 분위별 성적 — 임계값 탐색 아님, 분위수 고정]")
    d["q"] = pd.qcut(d.pe60, 4, labels=["Q1 낮음(휩쏘)", "Q2", "Q3", "Q4 높음(추세)"])
    for lab, g in (("전체", d), ("5~7월", d[d.half == "5~7월"]), ("8~10월", d[d.half == "8~10월"])):
        print(f"  [{lab}]")
        for q, x in g.groupby("q", observed=True):
            print(f"    {q:14s} n {len(x):3d} 승률 {x.win.mean() * 100:5.1f}% 합 {x.krw.sum():+11,.0f} "
                  f"평균 {x.krw.mean():+9,.0f} 즉시역행 {x.immrev.mean() * 100:3.0f}% "
                  f"(pe60 {x.pe60.min():.2f}~{x.pe60.max():.2f})")

    print("\n\n[주의: atr_pct -> notimm 은 동어반복 위험]")
    print("  immrev 는 mfe_b<0.3%로 정의된다. 변동성이 작으면 0.3% 자체를 못 가므로")
    print("  atr_pct 가 높을수록 notimm 이 되는 건 일부 기계적이다. ATR 로 정규화해 다시 보면:")
    d["mfe_b_atr"] = d.mfe_b / (2 * d.atr_pct)          # 2배 ETF
    d["immrev_n"] = (d.mfe_b_atr < 0.5).astype(int)      # ATR 의 절반도 못 간 것
    for lab, g in (("전체", d), ("8~10월", d[d.half == "8~10월"]), ("최근10일", d[r10])):
        print(f"  {lab:8s} 원래 immrev {g.immrev.mean() * 100:4.0f}%  ATR정규화 immrev {g.immrev_n.mean() * 100:4.0f}%")

    def auc(y, s):
        m = ~(np.isnan(s) | np.isnan(y))
        y, s = y[m], s[m]
        pos, neg = s[y == 1], s[y == 0]
        if not len(pos) or not len(neg):
            return np.nan
        r = pd.Series(np.r_[pos, neg]).rank().to_numpy()
        return (r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))
    yn = 1 - d.immrev_n.to_numpy(float)
    print(f"  ATR정규화 결과변수에 대한 atr_pct AUC {auc(yn, d.atr_pct.to_numpy(float)):.3f} "
          f"(원래 0.712) / pe60 {auc(yn, d.pe60.to_numpy(float)):.3f}")


if __name__ == "__main__":
    main()
