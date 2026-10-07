"""F3: 10/07 09:57 DOWN_BLUE 진입에 각 청산 래더를 적용. READ-ONLY.
   n1_adaptive 상위추세 판정은 production 함수를 그대로 호출한다.
"""
import sys
from datetime import timedelta
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
sys.path.insert(0, REPO)
from app.trading.macd2.signal_engine import resample_completed_3m, exclude_preopen_padding_1m
from app.trading.macd2 import n1_adaptive
from app.trading.macd2.models import Direction
KST = "Asia/Seoul"; DAY = "20261007"
ENTRY = pd.Timestamp("2026-10-07 10:03", tz=KST)      # 09:57 플래그 + T+3 확정 + 체결
OPP = pd.Timestamp("2026-10-07 11:30", tz=KST)        # 다음 반대(UP_RED) 확정
EOD = pd.Timestamp("2026-10-07 15:20", tz=KST)


def b1(tag):
    df = pd.read_csv(f"{REPO}/data/cache/replay_{DAY}_{tag}_1m.csv")
    dt = pd.to_datetime(df["datetime"])
    df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
    return df.sort_values("datetime").reset_index(drop=True)


clean, _ = exclude_preopen_padding_1m(b1("hynix"))
b3 = resample_completed_3m(clean, now=ENTRY.to_pydatetime())
snap = n1_adaptive.evaluate_regime(b3, direction=Direction.DOWN_BLUE) \
    if hasattr(n1_adaptive, "evaluate_regime") else None
print("## n1_adaptive 상위추세 판정 (10:03, DOWN_BLUE 보유 기준)")
if snap is None:
    fns = [f for f in dir(n1_adaptive) if not f.startswith("_")]
    print("  함수:", fns)
else:
    print(" ", snap)

inv = b1("inverse"); ts = pd.DatetimeIndex(inv["datetime"])
i0 = int(ts.searchsorted(ENTRY)); px0 = float(inv["open"].iloc[i0])
iOPP = int(ts.searchsorted(OPP)); iEOD = int(ts.searchsorted(EOD))
print(f"\n## 진입 10:03 @ {px0:,.0f} (인버스 0197X0)")


def net(i, which):
    return (float(inv[which].iloc[i]) - px0) / px0 * 100


def sim(name, tp=None, sl=None, maxhold=None, tp1=None, ratio=None, tp2=None,
        after=None, trail=None, trailtrig=None, opp=True):
    """봉 내부는 보수적으로 불리한 쪽(손절/스탑) 먼저."""
    tp1_done, peak, got = False, 0.0, []
    lim = iOPP if opp else iEOD
    for i in range(i0, min(lim, iEOD) + 1):
        lo, hi, c = net(i, "low"), net(i, "high"), net(i, "close")
        tmin = (ts[i] - ENTRY).total_seconds() / 60
        peak = max(peak, hi)
        if sl is not None and lo <= -sl:
            return name, ts[i], -sl, "SL", got
        if not tp1_done:
            if tp is not None and hi >= tp:
                return name, ts[i], tp, "B3_TP", got
            if maxhold is not None and tmin >= maxhold:
                return name, ts[i], c, "B3_MAXHOLD", got
            if tp2 is not None and hi >= tp2:
                return name, ts[i], tp2, "TP2_DIRECT", got
            if tp1 is not None and hi >= tp1:
                tp1_done = True; got.append((ts[i], tp1, ratio))
                continue
        else:
            if tp2 is not None and hi >= tp2:
                return name, ts[i], tp2, "TP2_FULL", got
            stop = trail if peak >= trailtrig else after
            if lo <= stop:
                return name, ts[i], stop, "TRAIL" if peak >= trailtrig else "AFTER_TP1", got
    j = min(lim, iEOD)
    return name, ts[j], net(j, "close"), ("OPPOSITE_SIGNAL" if opp else "EOD"), got


rows = [
    sim("B3 (P3 · CHOP 진입)", tp=1.0, sl=1.0, maxhold=20.0),
    sim("N1 비추세 가지", tp1=3.0, ratio=0.2, tp2=4.0, sl=1.7, after=0.3, trail=1.5, trailtrig=3.5),
    sim("N1 추세 가지", tp1=3.5, ratio=0.0, tp2=8.0, sl=1.7, after=0.3, trail=1.5, trailtrig=3.5),
    sim("N1 비추세 · 반대신호 무시", tp1=3.0, ratio=0.2, tp2=4.0, sl=1.7, after=0.3,
        trail=1.5, trailtrig=3.5, opp=False),
]
print("\n| 래더 | 청산시각 | 보유 | 최종 net | 사유 | TP1 부분익절 | 가중 net |")
print("|---|---|---|---|---|---|---|")
for name, t, n, why, got in rows:
    hold = (t - ENTRY).total_seconds() / 60
    if got:
        _, p1, r1 = got[0]
        w = p1 * r1 + n * (1 - r1)
        g = f"{p1:+.2f}% x {r1:.0%} @ {got[0][0].strftime('%H:%M')}"
    else:
        w, g = n, "-"
    print(f"| {name} | {t.strftime('%H:%M')} | {hold:.0f}분 | {n:+.2f}% | {why} | {g} | **{w:+.2f}%** |")

print(f"\n## 참고 — 이 레그가 실제로 간 거리")
seg = inv.iloc[i0:iEOD + 1]
print(f"  최고 +{(float(seg['high'].max())-px0)/px0*100:.2f}% "
      f"({pd.Timestamp(seg.loc[seg['high'].idxmax(),'datetime']).strftime('%H:%M')}) · "
      f"11:30(반대확정) 시점 {net(iOPP,'close'):+.2f}% · 15:20 {net(iEOD,'close'):+.2f}%")
for lv in (1.0, 1.5, 2.0, 2.5, 3.0, 3.5):
    hit = [j for j in range(i0, iEOD + 1) if net(j, "high") >= lv]
    if hit:
        print(f"  +{lv:.1f}% 최초도달 {ts[hit[0]].strftime('%H:%M')} (진입 후 {(ts[hit[0]]-ENTRY).total_seconds()/60:.0f}분)")
