"""READ-ONLY 수집: H50/SMALL_WHIPSAW 연구용 재생 1분봉 (2026-10-05 ~ 10-20).

기존 재생 데이터는 2026-10-02 까지만 있어, 최근 20영업일 비교에 필요한 구간을
KIS 에서 보충한다. 출력 형식은 기존 하네스가 읽는 것과 **완전히 동일**하다:

    datetime,open,high,low,close,volume      (naive KST 문자열)
    hynix   : 08:00~ (NXT 포함 "NX" -- 프리마켓 필수, 빼면 지표 seed 가 달라진다)
    long/inv: 09:00~ (ETF 는 "J")

조회 전용이다 -- 주문/상태/원장 경로는 import 조차 하지 않는다. 이미 있는
파일은 건너뛴다(멱등).
"""
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import pandas as pd
from app.trading.macd2 import config
from app.trading.kis_client import create_kis_client

KST = config.KST
OUT = Path("research_20261108_chop_gate/bars_un")
OUT.mkdir(parents=True, exist_ok=True)

SYMS = (
    ("hynix", config.WATCH_SYMBOL, "UN", "080000"),
    ("long", config.LONG_SYMBOL, "J", "090000"),
    ("inverse", config.INVERSE_SYMBOL, "J", "090000"),
)

client = create_kis_client("real")
if client is None:
    print("!! KIS 클라이언트 없음")
    sys.exit(1)


def fetch_day(symbol: str, date_ymd: str, market_div: str, start_hhmmss: str) -> pd.DataFrame:
    """그 날짜의 1분봉 전체를 역방향 페이징으로 모은다."""
    rows, cursor, seen_oldest = [], "", None
    for page in range(40):
        got = client.get_minute_candles_for_date(
            symbol, date_ymd, period_min=1, count=30,
            hour1=cursor, market_div=market_div,
        ) or []
        if not got:
            break
        rows.extend(got)
        oldest = min(r["time"] for r in got if r.get("time"))
        if seen_oldest is not None and oldest >= seen_oldest:
            break                      # 커서가 더 안 밀린다
        seen_oldest = oldest
        if oldest <= start_hhmmss:
            break
        h, m = int(oldest[:2]), int(oldest[2:4])
        prev = (datetime(2000, 1, 1, h, m) - timedelta(minutes=1))
        cursor = prev.strftime("%H%M%S")
        time.sleep(0.15)               # KIS 배려 -- 레이트리밋 회피
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df[df["date"] == date_ymd]
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["date"] + df["time"], format="%Y%m%d%H%M%S")
    df = (df[["datetime", "open", "high", "low", "close", "volume"]]
          .drop_duplicates(subset=["datetime"], keep="last")
          .sort_values("datetime").reset_index(drop=True))
    return df[df["datetime"].dt.strftime("%H%M%S") >= start_hhmmss].reset_index(drop=True)


def trading_days(start: str, end: str) -> list[str]:
    d = datetime.strptime(start, "%Y%m%d")
    stop = datetime.strptime(end, "%Y%m%d")
    out = []
    while d <= stop:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


if len(sys.argv) > 1:
    days = sys.argv[1:]            # 명시 날짜 재수집 (잘린 기존 파일 보정용)
else:
    days = trading_days("20261005", "20261020")
print(f"대상 후보일 {len(days)}개: {days[0]} ~ {days[-1]}")
print("(휴장일은 조회 결과가 비어 자동 제외된다)\n")

summary = []
for day in days:
    line = [day]
    for tag, sym, mdiv, start in SYMS:
        path = OUT / f"replay_{day}_{tag}_1m.csv"
        if path.exists() and path.stat().st_size > 0:
            line.append(f"{tag}=skip")
            continue
        df = fetch_day(sym, day, mdiv, start)
        if df.empty:
            line.append(f"{tag}=EMPTY")
            continue
        df.to_csv(path, index=False, date_format="%Y-%m-%d %H:%M:%S")
        line.append(f"{tag}={len(df)}")
    print("  ".join(line), flush=True)
    summary.append(line)

print("\n=== 수집 결과 ===")
ok = [s for s in summary if all("EMPTY" not in x for x in s[1:])]
print(f"3종목 모두 확보: {len(ok)}일")
print("휴장/결손:", [s[0] for s in summary if any("EMPTY" in x for x in s[1:])])
