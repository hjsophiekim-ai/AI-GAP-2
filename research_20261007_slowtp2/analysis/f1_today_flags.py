"""F1: 10/07 (및 10/06) 플래그 재구성 — production 함수 그대로 호출. READ-ONLY."""
import sys
from datetime import timedelta
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
sys.path.insert(0, REPO)
from app.trading.macd2.signal_engine import (          # noqa: E402
    resample_completed_3m, calculate_macd, evaluate_macd_crossover,
    exclude_preopen_padding_1m)
from app.trading.macd2.models import Direction          # noqa: E402

KST = "Asia/Seoul"


def bars1(day, tag="hynix"):
    df = pd.read_csv(f"{REPO}/data/cache/replay_{day}_{tag}_1m.csv")
    dt = pd.to_datetime(df["datetime"])
    df["datetime"] = dt.dt.tz_localize(KST) if dt.dt.tz is None else dt.dt.tz_convert(KST)
    return df.sort_values("datetime").reset_index(drop=True)


def flags(day):
    """production 과 같은 계약: 완성 3분봉 + preopen padding 제외 + zero-cross + 반복억제."""
    raw = bars1(day)
    clean, _ = exclude_preopen_padding_1m(raw)
    out, prev = [], None
    # 완성봉이 생기는 시각마다 판정 (bar_start + 3분)
    t = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 08:03", tz=KST)
    end = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 15:30", tz=KST)
    seen = set()
    while t <= end:
        b3 = resample_completed_3m(clean, now=t.to_pydatetime())
        if len(b3):
            key = pd.Timestamp(b3["datetime"].iloc[-1])
            if key not in seen:
                seen.add(key)
                snap = calculate_macd(b3)
                if snap is not None:
                    d = evaluate_macd_crossover(snap, prev)
                    if d in (Direction.UP_RED, Direction.DOWN_BLUE):
                        out.append(dict(bar=key, at=t, dir=d.value,
                                        hist=snap.hist, px=float(b3["close"].iloc[-1])))
                        prev = d
        t += timedelta(minutes=3)
    return out, clean


for day in ("20261006", "20261007"):
    fl, clean = flags(day)
    px = bars1(day)
    ts = pd.DatetimeIndex(px["datetime"])
    hi, lo = px["high"].to_numpy(float), px["low"].to_numpy(float)
    cl = px["close"].to_numpy(float)
    print(f"\n{'='*72}\n## {day} — 플래그 {len(fl)}개 (장중 09:00~15:20만 주문권한)")
    print("| # | 확정시각 | 방향 | 플래그가 | 다음 반대플래그까지 MFE | MAE | 지속 |")
    print("|---|---|---|---|---|---|---|")
    core = [f for f in fl if f["at"].hour >= 9 and (f["at"].hour, f["at"].minute) < (15, 20)]
    alt = []
    for f in core:
        if alt and alt[-1]["dir"] == f["dir"]:
            continue
        alt.append(f)
    for k, f in enumerate(alt):
        i0 = int(ts.searchsorted(f["at"]))
        tend = alt[k + 1]["at"] if k + 1 < len(alt) else pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:]} 15:20", tz=KST)
        i1 = int(ts.searchsorted(tend))
        if i1 <= i0:
            continue
        p0 = cl[i0]
        up = f["dir"] == "UP_RED"
        mfe = ((hi[i0:i1].max() - p0) if up else (p0 - lo[i0:i1].min())) / p0 * 100
        mae = ((p0 - lo[i0:i1].min()) if up else (hi[i0:i1].max() - p0)) / p0 * 100
        print(f"| {k+1} | {f['at'].strftime('%H:%M')} | {f['dir']} | {p0:,.0f} | "
              f"**{mfe:.2f}%** | {mae:.2f}% | {(tend-f['at']).total_seconds()/60:.0f}분 |")
    print(f"\n장중 플래그(반복억제 후 교대) {len(alt)}개 · 전체 확정 {len(core)}개")
