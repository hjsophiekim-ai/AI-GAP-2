"""연구1 MORNING-PEAK -> PM-BLUE (2026-10-16). READ-ONLY, production 무수정.

가설: 09:00~12:00 사이 하이닉스 고점이 시가 대비 +1.5%/+2.0% 이상 형성되고
그 뒤 고점 대비 되밀림이 생긴 날의 13:30~14:30 DOWN_BLUE 확정플래그는
성공률/평균수익이 높다.

플래그 모집단 : research_20261015d_exit_synthesis/flag_select.csv
  (N1 재생 원장의 :TW2_3SLOT_CONFIRM 확정플래그 692개 / 92일 / 05-27~10-16,
   결정시각 = detected_at, 사후수익 ret = 그 방향 ETF 를 결정시각 1분봉 종가에
   사서 '다음 반대방향 확정' 또는 15:00 종가에 판 %)
오전 특징     : bb/sp/data 의 하이닉스 1분봉(05-27~10-08, 89일).
  10/12·10/13·10/16 은 이 PC 에 1분봉이 없어 특징을 못 만든다 -> 집계에서 제외하고
  몇 건이 빠졌는지 같이 보고한다.

임계값은 전부 사전 고정이다. 사용자 지시대로 threshold 세밀탐색을 하지 않는다.
  MP   : am_peak_pct >= 1.5 / 2.0  (09:00 시가 대비 09:00~11:59 최고가)
  PB   : 플래그 결정시각 종가가 그 고점 대비 >= 1.0% 아래 (민감도로 0.5% 도 같이)
  창   : 13:30 <= 결정시각 < 14:30, 방향 DOWN_BLUE
"""
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = "G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
BARS = "C:/Users/KIMHYU~1/AppData/Local/Temp/claude/G----------------2--Desktop-AI-GAP-2/4745b385-5441-4c15-97e7-446b1c980929/scratchpad/bb/sp/data"
FLAGS = ROOT + "/research_20261015d_exit_synthesis/flag_select.csv"
OUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/out"
os.makedirs(OUT, exist_ok=True)


def bars(day):
    p = f"{BARS}/replay_{day}_hynix_1m.csv"
    if not os.path.exists(p):
        return None
    x = pd.read_csv(p)
    x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
    return x.drop_duplicates("datetime", keep="last").set_index("datetime").sort_index()


def day_features(day):
    hy = bars(day)
    if hy is None:
        return None
    s = hy[(hy.index.time >= pd.Timestamp("09:00").time()) & (hy.index.time < pd.Timestamp("12:00").time())]
    if len(s) < 60:
        return None
    op = float(s["open"].iloc[0])
    hi = float(s["high"].max())
    lo = float(s["low"].min())
    t_hi = s["high"].idxmax()
    return dict(day=day, op=op, am_high=hi, am_low=lo, t_high=t_hi,
                am_peak_pct=(hi / op - 1) * 100, am_low_pct=(lo / op - 1) * 100,
                am_close_pct=(float(s["close"].iloc[-1]) / op - 1) * 100)


def main():
    df = pd.read_csv(FLAGS, dtype={"day": str}, parse_dates=["t"])
    df["hm"] = df.t.dt.hour * 60 + df.t.dt.minute
    feats = [f for f in (day_features(d) for d in sorted(df.day.unique())) if f]
    fd = pd.DataFrame(feats).set_index("day")
    print(f"플래그 {len(df)}개 / {df.day.nunique()}일 (05-27~10-16)")
    print(f"오전특징 계산 가능 {len(fd)}일 — 1분봉 없는 날 "
          f"{sorted(set(df.day.unique()) - set(fd.index))}\n")

    pm = df[(df.d == -1) & (df.hm >= 810) & (df.hm < 870)].copy()
    print(f"13:30~14:30 DOWN_BLUE 확정플래그 {len(pm)}건 / {pm.day.nunique()}일")
    miss = pm[~pm.day.isin(fd.index)]
    print(f"  그중 1분봉 없는 날 {len(miss)}건 ({sorted(miss.day.unique())}) -> 제외\n")

    pm = pm[pm.day.isin(fd.index)].copy()
    for c in ("op", "am_high", "am_peak_pct", "am_low_pct", "am_close_pct"):
        pm[c] = pm.day.map(fd[c])
    pm["t_high"] = pm.day.map(fd["t_high"])

    # 플래그 결정시각의 하이닉스 종가 -> 고점 대비 되밀림
    px = {}
    for day in pm.day.unique():
        hy = bars(day)
        for t in pm.loc[pm.day == day, "t"].unique():
            s = hy["close"][hy.index <= pd.Timestamp(t)]
            px[(day, pd.Timestamp(t))] = float(s.iloc[-1]) if len(s) else np.nan
    pm["px_flag"] = [px[(r.day, r.t)] for r in pm.itertuples()]
    pm["pullback_pct"] = (pm.am_high - pm.px_flag) / pm.am_high * 100
    pm["half"] = np.where(pm.day < "20260801", "5-7월", "8-10월")
    pm.to_csv(OUT + "/s1_pmblue.csv", index=False, encoding="utf-8-sig")

    def row(name, g):
        if not len(g):
            return f"  {name:32s}    0      -      -      -        -"
        return (f"  {name:32s} {len(g):4d} {(g.ret > 0).mean() * 100:5.1f}% "
                f"{g.ret.mean():+6.2f} {g.ret.median():+6.2f} {g.ret.sum():+8.1f}  {g.day.nunique():3d}일")

    def table(title, sub):
        print(f"\n=== {title} ===")
        print(f"  {'조건':32s} {'n':>4s} {'승률':>6s} {'평균':>6s} {'중앙':>6s} {'합계':>8s}  일수")
        for name, g in sub:
            print(row(name, g))

    def conds(base):
        yield "PM-BLUE 전체 (조건 없음)", base
        for th in (1.5, 2.0):
            m = base[base.am_peak_pct >= th]
            yield f"MP{th:.1f} (오전고점 >= +{th}%)", m
            for pb in (0.5, 1.0):
                yield f"  +되밀림 >= {pb}%", m[m.pullback_pct >= pb]
        yield "대조: 오전고점 < +1.5%", base[base.am_peak_pct < 1.5]

    for title, g in (("92일 전체 (실제 89일 중 특징가능일)", pm),
                     ("5~7월", pm[pm.half == "5-7월"]),
                     ("8~10월", pm[pm.half == "8-10월"])):
        table(title, list(conds(g)))

    print("\n=== MP1.5 + 되밀림>=1.0% 의 개별 플래그 ===")
    sel = pm[(pm.am_peak_pct >= 1.5) & (pm.pullback_pct >= 1.0)].sort_values("t")
    cols = ["day", "t", "am_peak_pct", "pullback_pct", "ret", "result", "reason"]
    s = sel[cols].copy()
    s["t"] = s["t"].dt.strftime("%H:%M")
    print(s.round(2).to_string(index=False))

    print("\n=== 날짜 bootstrap: MP1.5+PB1.0 평균 - 나머지 PM-BLUE 평균 ===")
    rng = np.random.default_rng(0)
    days = pm.day.unique()
    for label, sub in (("전체", pm), ("8~10월", pm[pm.half == "8-10월"])):
        dd = sub.day.unique()
        hit = (sub.am_peak_pct >= 1.5) & (sub.pullback_pct >= 1.0)
        if hit.sum() == 0:
            print(f"  {label}: 해당 없음")
            continue
        obs = sub[hit].ret.mean() - sub[~hit].ret.mean()
        boots = []
        for _ in range(3000):
            x = pd.concat([sub[sub.day == k] for k in rng.choice(dd, len(dd))])
            h = (x.am_peak_pct >= 1.5) & (x.pullback_pct >= 1.0)
            if h.sum() and (~h).sum():
                boots.append(x[h].ret.mean() - x[~h].ret.mean())
        lo, hi = np.percentile(boots, [2.5, 97.5])
        print(f"  {label}: 관측 {obs:+.2f}%p, 95% CI [{lo:+.2f}, {hi:+.2f}], n={int(hit.sum())}")
    _ = days


if __name__ == "__main__":
    main()
