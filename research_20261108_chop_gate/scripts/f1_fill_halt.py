"""장중 거래중단 구간을 평탄봉으로 메운다 — 재생 1분봉 충실도 보정.

2026-11-06 09:04~09:32 은 1초 스냅샷에서 가격·누적거래량이 **전혀 변하지 않았다**
(000660 / 0193T0 / 0197X0 모두). 실제 거래가 없던 시장 중단이다. KIS 분봉 API 는
체결이 없으면 봉 자체를 주지 않지만, 라이브 production 은 직전가를 이어받는다.
그래서 그 구간만 OHLC=직전종가, volume=0 으로 채운다.

15:20~15:29 결손은 **메우지 않는다** — 종가 단일가 구간이고, 기존 10월 기준선도
그대로 둔 채 재현됐다.
"""
import shutil
import sys
from pathlib import Path

import pandas as pd

SRC = Path("research_20261020_h50_hold/data")
DST = Path("research_20261108_chop_gate/bars_fix")
FILL = {"20261106": ("09:04", "09:32")}          # 날짜 -> 메울 구간(양끝 포함)
DAYS = ["20261102", "20261103", "20261104", "20261105", "20261106"]
sys.stdout.reconfigure(encoding="utf-8")
DST.mkdir(parents=True, exist_ok=True)

for d in DAYS:
    day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    for tag in ("hynix", "long", "inverse"):
        src = SRC / f"replay_{d}_{tag}_1m.csv"
        dst = DST / f"replay_{d}_{tag}_1m.csv"
        if not src.exists():
            print(f"{d} {tag}: 원본 없음"); continue
        if d not in FILL:
            shutil.copy2(src, dst); print(f"{d} {tag}: 그대로 복사"); continue
        df = pd.read_csv(src)
        df["datetime"] = pd.to_datetime(df["datetime"])
        a, b = (pd.Timestamp(f"{day} {t}") for t in FILL[d])
        want = pd.date_range(a, b, freq="1min")
        have = set(df["datetime"])
        miss = [t for t in want if t not in have]
        if miss:
            prev = df[df["datetime"] < a].sort_values("datetime")
            if prev.empty:
                print(f"{d} {tag}: 직전봉 없음 — 메우지 않음"); shutil.copy2(src, dst); continue
            c = float(prev["close"].iloc[-1])
            add = pd.DataFrame({"datetime": miss, "open": c, "high": c, "low": c, "close": c, "volume": 0})
            df = pd.concat([df, add], ignore_index=True).sort_values("datetime").reset_index(drop=True)
        df.to_csv(dst, index=False, date_format="%Y-%m-%d %H:%M:%S")
        print(f"{d} {tag}: {len(miss)}분 평탄봉 보충 -> {len(df)}행")
print(f"\n출력 {DST}")
