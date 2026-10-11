"""F2: 10/07 전체 플래그(장전 포함) + 09:57 DOWN_BLUE 진입의 ETF 경로. READ-ONLY."""
import sys
from datetime import timedelta
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
sys.path.insert(0, REPO)
from app.trading.macd2.signal_engine import (resample_completed_3m, calculate_macd,
                                             evaluate_macd_crossover, exclude_preopen_padding_1m)
from app.trading.macd2.models import Direction
KST = "Asia/Seoul"
DAY = "20261007"


def b1(tag):
    df = pd.read_csv(f"{REPO}/data/cache/replay_{DAY}_{tag}_1m.csv")
    dt = pd.to_datetime(df["datetime"])
    df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
    return df.sort_values("datetime").reset_index(drop=True)


raw = b1("hynix")
clean, _ = exclude_preopen_padding_1m(raw)
t = pd.Timestamp(f"2026-10-07 08:03", tz=KST)
end = pd.Timestamp(f"2026-10-07 15:30", tz=KST)
prev, seen, fl = None, set(), []
while t <= end:
    b3 = resample_completed_3m(clean, now=t.to_pydatetime())
    if len(b3):
        key = pd.Timestamp(b3["datetime"].iloc[-1])
        if key not in seen:
            seen.add(key)
            s = calculate_macd(b3)
            if s is not None:
                d = evaluate_macd_crossover(s, prev)
                if d in (Direction.UP_RED, Direction.DOWN_BLUE):
                    fl.append((t, d.value, float(b3["close"].iloc[-1])))
                    prev = d
    t += timedelta(minutes=3)
print("## 10/07 전체 플래그 (장전 포함)")
for a, d, p in fl:
    mark = "" if a.hour >= 9 else "  (장전 — 주문권한 없음)"
    print(f"  {a.strftime('%H:%M')}  {d:10s} {p:>11,.0f}{mark}")

print("\n## 09:00~10:00 하이닉스 흐름 (3분봉)")
b3 = resample_completed_3m(clean, now=pd.Timestamp("2026-10-07 11:00", tz=KST).to_pydatetime())
s = b3[b3["datetime"] >= pd.Timestamp("2026-10-07 09:00", tz=KST)]
for _, r in s.iterrows():
    print(f"  {pd.Timestamp(r['datetime']).strftime('%H:%M')}  O{r['open']:>10,.0f} H{r['high']:>10,.0f} "
          f"L{r['low']:>10,.0f} C{r['close']:>10,.0f}")

print("\n## 인버스 ETF(0197X0) 경로 — 09:57 DOWN_BLUE 이후")
inv = b1("inverse")
ts = pd.DatetimeIndex(inv["datetime"])
for ent_s in ("10:00", "10:03", "10:06"):
    ent = pd.Timestamp(f"2026-10-07 {ent_s}", tz=KST)
    i = int(ts.searchsorted(ent))
    px0 = float(inv["open"].iloc[i])
    seg = inv.iloc[i:]
    peak = (float(seg["high"].max()) - px0) / px0 * 100
    pt = seg.loc[seg["high"].idxmax(), "datetime"]
    print(f"  진입 {ent_s} @ {px0:,.0f} → 최고 +{peak:.2f}% ({pd.Timestamp(pt).strftime('%H:%M')}) "
          f"· 15:20 종가 {(float(inv['close'].iloc[-1])-px0)/px0*100:+.2f}%")
print("\n  ETF 분단위 (진입 10:03 기준 net%)")
ent = pd.Timestamp("2026-10-07 10:03", tz=KST)
i0 = int(ts.searchsorted(ent)); px0 = float(inv["open"].iloc[i0])
prevp = None
for j in range(i0, min(i0 + 130, len(inv))):
    h = (float(inv["high"].iloc[j]) - px0) / px0 * 100
    l = (float(inv["low"].iloc[j]) - px0) / px0 * 100
    c = (float(inv["close"].iloc[j]) - px0) / px0 * 100
    tm = pd.Timestamp(inv["datetime"].iloc[j]).strftime("%H:%M")
    if j == i0 or tm.endswith(("0", "5")) and (prevp is None or True):
        if int(tm[-1]) % 5 == 0:
            print(f"    {tm}  H{h:+6.2f}  L{l:+6.2f}  C{c:+6.2f}")
