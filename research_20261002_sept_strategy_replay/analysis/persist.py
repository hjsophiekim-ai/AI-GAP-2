"""'추세는 오전까지 유지된다' 전제 측정 + DAY-TREND 천장/단순형. READ-ONLY.
P1: 판정시각 T(09:30/10:00/10:30)의 '시가 대비 방향'이 T -> 12:00 / T -> 15:00 에도 이어지는 비율과 평균 크기.
DT-C: 시가 대비 방향만으로 추세 판정, 09:15~11:30 첫 정렬 확인플래그 진입, 15:00 까지 보유(안전손절 -3%).
ORACLE: 그날 종가방향(15:00 vs 09:00)을 미리 안다고 가정, 첫 정렬 확인플래그 진입 -> 15:00 (천장).
"""
import sys
import numpy as np
import pandas as pd
sys.argv = [sys.argv[0], sys.argv[1]]
import importlib.util
spec = importlib.util.spec_from_file_location("dt", __file__.replace("persist.py", "daytrend_core.py"))
dt = importlib.util.module_from_spec(spec); spec.loader.exec_module(dt)

rows = []
for d in dt.days:
    x = dt.day_frame(d)
    if x is None:
        continue
    reg = x[x["datetime"].dt.hour >= 9]
    def px(hh, mm):
        t = pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} {hh:02d}:{mm:02d}")
        s = reg[reg["done"] <= t]
        return float(s["close"].iloc[-1]) if len(s) else np.nan
    op = float(reg["open"].iloc[0])
    r = dict(day=d, op=op)
    for hh, mm in ((9, 30), (10, 0), (10, 30), (12, 0), (15, 0)):
        r[f"p{hh:02d}{mm:02d}"] = px(hh, mm)
    rows.append(r)
df = pd.DataFrame(rows)
print("=== P1 '오전 추세 지속' 측정 (하이닉스, 판정 이후 구간 수익의 부호가 판정방향과 같은 비율)")
for scope, m in (("전체", df["day"] >= "0"), ("9월~", df["day"] >= "20260901"), ("주간", df["day"] >= "20260922")):
    s = df[m]
    for T in ("p0930", "p1000", "p1030"):
        dirn = np.sign(s[T] - s["op"])
        to12 = (s["p1200"] / s[T] - 1) * 100 * dirn
        to15 = (s["p1500"] / s[T] - 1) * 100 * dirn
        print(f"  [{scope:4s} n={len(s):2d}] T={T[1:3]}:{T[3:]}  ->12:00 지속 {(to12 > 0).mean():.0%} 평균 {to12.mean():+.2f}%  |  ->15:00 지속 {(to15 > 0).mean():.0%} 평균 {to15.mean():+.2f}%")
wk = df[df["day"] >= "20260922"].copy()
for T in ("p0930", "p1000", "p1200", "p1500"):
    wk[T] = ((wk[T] / wk["op"] - 1) * 100).round(2)
print(wk[["day", "p0930", "p1000", "p1200", "p1500"]].to_string(index=False))


def run_simple(d, oracle):
    if d not in dt.FL:
        x = dt.day_frame(d); dt.FL[d] = (x, dt.confirmed_flags(x) if x is not None else [])
    x, fl = dt.FL[d]
    if x is None:
        return None
    reg = x[x["datetime"].dt.hour >= 9]
    end = pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]} 15:00")
    day_dir = np.sign(float(reg[reg["done"] <= end]["close"].iloc[-1]) - float(reg["open"].iloc[0]))
    for ct, f, fbt, j in fl:
        hhmm = ct.hour * 60 + ct.minute
        if not (9 * 60 + 15 <= hhmm <= 11 * 60 + 30):
            continue
        want = day_dir if oracle else np.sign(x.at[j, "close"] - x.at[j, "op"])
        if want != f:
            continue
        e = dt.etf(d, f)
        seen = e[e["datetime"] + pd.Timedelta(minutes=1) <= ct]
        if not len(seen):
            continue
        p_in = float(seen["close"].iloc[-1]) + dt.TICK
        for r in e[e["datetime"] + pd.Timedelta(minutes=1) > ct].itertuples():
            t = r.datetime + pd.Timedelta(minutes=1)
            ret = (r.close / p_in - 1) * 100
            if ret <= -dt.SL or t >= end:
                return dict(day=d, dir=f, flag=fbt.strftime("%H:%M"), entry=ct.strftime("%H:%M"), p_in=p_in,
                            exit=t.strftime("%H:%M"), p_out=r.close - dt.TICK, net=((r.close - dt.TICK) / p_in - 1) * 100 - dt.FEE)
        r = e.iloc[-1]
        return dict(day=d, dir=f, flag=fbt.strftime("%H:%M"), entry=ct.strftime("%H:%M"), p_in=p_in,
                    exit="EOD" + r["datetime"].strftime("%H:%M"), p_out=r["close"] - dt.TICK, net=((r["close"] - dt.TICK) / p_in - 1) * 100 - dt.FEE)
    return None


for name, orc in (("DT-C 시가방향만", False), ("ORACLE 종가방향 앎", True)):
    res = [run_simple(d, orc) for d in dt.days]
    daily = {r["day"]: r["net"] for r in res if r}
    print(f"\n=== {name}")
    for scope, ds in (("전체", dt.days), ("9월~", [d for d in dt.days if d >= "20260901"]), ("주간", dt.WEEK)):
        print(f"  [{scope}] R0 " + dt.stats([dt.r0.get(d, 0.0) for d in ds]))
        print(f"  [{scope}] {name[:6]} " + dt.stats([daily.get(d, 0.0) for d in ds]) + f"  거래 {sum(d in daily for d in ds)}")
    for r in res:
        if r and r["day"] in dt.WEEK:
            print(f"    {r['day']} {'UP 레버' if r['dir'] == 1 else 'DN 인버'} 플래그{r['flag']} 진입{r['entry']}@{r['p_in']:.0f} -> {r['exit']}@{r['p_out']:.0f} {r['net']:+.2f}%")
