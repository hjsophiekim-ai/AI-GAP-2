"""execution 지연의 '진짜 크기' 재기 (2026-10-17). READ-ONLY.

재생 하네스의 체결모형(WK_FILL=REAL)은 결정 tick 에 바로 체결한다 — 주문 왕복 지연은
모형에 없다. 그래서 재생으로 잴 수 있는 execution 비용은 두 가지뿐이고, 이 스크립트가
그 둘을 분리해서 잰다.

  (1) 호가 스프레드 비용 : 체결가(ask1 = 시세+5원) vs 결정시각 시세. 모든 거래에 붙는 상수.
  (2) T+3 확인 대기 비용 : 원 플래그가 뜬 봉이 끝난 시각 -> 실제 진입 체결 시각 사이에
      ETF 가 진입방향으로 이미 움직여버린 폭. production 이 '확인'을 위해 치르는 값이고,
      이게 이 시스템의 **구조적 지연**이다 (주문 왕복 지연보다 훨씬 크다).

원 플래그 봉 시각은 signal_id(YYYYMMDD_HHMMSS_DIR) 에서 가져온다.
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
LED = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/e202387f-ec1a-41fa-a078-e46b72ece959/scratchpad/wk/out"
BARS = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/bb/sp/data"
OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"


def main():
    d = pd.read_csv(OUT + "/d1_trades_89d.csv", dtype={"day": str})
    days = sorted(d.day.unique())
    cache = {}

    def px(day, sym, ts):
        key = (day, sym)
        if key not in cache:
            tag = "long" if sym == "0193T0" else "inverse"
            p = f"{BARS}/replay_{day}_{tag}_1m.csv"
            if not os.path.exists(p):
                cache[key] = None
            else:
                x = pd.read_csv(p)
                x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
                cache[key] = x.drop_duplicates("datetime", keep="last").sort_values("datetime")
        b = cache[key]
        if b is None:
            return np.nan
        w = b[b.datetime <= ts]
        return float(w.close.iloc[-1]) if len(w) else np.nan

    rows = []
    for r in d.itertuples():
        sid = str(r.sid)
        if "_" not in sid or len(sid.split("_")) < 2:
            rows.append((np.nan, np.nan, np.nan))
            continue
        hhmmss = sid.split("_")[1]
        flag_bar = pd.Timestamp(f"{r.day[:4]}-{r.day[4:6]}-{r.day[6:]} {hhmmss[:2]}:{hhmmss[2:4]}:00")
        flag_close = flag_bar + pd.Timedelta(minutes=3)      # 그 플래그 봉이 '완성'된 시각
        ent = pd.Timestamp(f"{r.day[:4]}-{r.day[4:6]}-{r.day[6:]} {r.entry}:00")
        p_flag = px(r.day, r.sym, flag_close)
        lag_min = (ent - flag_close).total_seconds() / 60
        # 진입방향 기준 손해폭: ETF 를 사는 것이므로 가격이 올라버린 만큼 손해
        cost = (r.px_in / p_flag - 1) * 100 if p_flag == p_flag and p_flag > 0 else np.nan
        rows.append((lag_min, p_flag, -cost))
    d[["lag_min", "px_flagclose", "lag_cost"]] = pd.DataFrame(rows, index=d.index)
    d.to_csv(OUT + "/d2_lag_89d.csv", index=False, encoding="utf-8-sig")

    def sec(title, g):
        print(f"\n{'=' * 86}\n{title} — 거래 {len(g)}건\n{'=' * 86}")
        for lab, x in (("손실거래", g[g.krw < 0]), ("이익거래", g[g.krw > 0]), ("전체", g)):
            if not len(x):
                continue
            print(f"  {lab:8s} n {len(x):3d}  "
                  f"T+3 대기 {x.lag_min.mean():5.1f}분  대기 중 가격손해 {x.lag_cost.mean():+6.3f}%  "
                  f"(중앙 {x.lag_cost.median():+6.3f}, 최악 {x.lag_cost.min():+6.3f})  "
                  f"스프레드 {x.slip.mean():+.3f}%")
        L = g[g.krw < 0]
        if len(L):
            tot = L.krw.sum()
            notional = (L.krw / (L.net / 100).replace(0, np.nan)).abs()
            lagkrw = (L.lag_cost / 100 * notional).sum()
            spkrw = (L.slip / 100 * notional).sum()
            print(f"\n  손실 {tot:+,.0f}원 중")
            print(f"    T+3 확인 대기로 이미 밀린 값   {lagkrw:+,.0f}원 ({lagkrw / abs(tot) * 100:5.1f}%)")
            print(f"    호가 스프레드(ask1 매수)        {spkrw:+,.0f}원 ({spkrw / abs(tot) * 100:5.1f}%)")
            print(f"    나머지(진입판단 자체)           {tot - lagkrw - spkrw:+,.0f}원 "
                  f"({(tot - lagkrw - spkrw) / abs(tot) * 100:5.1f}%)")

    for n in (5, 10):
        sec(f"최근 {n}영업일 ({days[-n]}~{days[-1]})", d[d.day.isin(days[-n:])])
    sec("8~10월 전체 (43일)", d[d.day >= "20260801"])
    sec("89일 전체", d)

    print("\n\n[최근 10일 손실거래 — T+3 대기 비용 개별]")
    L = d[(d.day.isin(days[-10:])) & (d.krw < 0)].sort_values("krw")
    cols = ["day", "sid", "entry", "dir", "krw", "net", "lag_min", "lag_cost", "slip", "mfe_b", "cls", "reason"]
    print(L[cols].round(2).to_string(index=False))


if __name__ == "__main__":
    main()
