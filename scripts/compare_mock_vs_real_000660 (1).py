"""Read-only comparison: does KIS MOCK (모의투자) 000660 1-minute quote data
match the REAL (실계좌) account's 000660 1-minute quote data around today's
3 reported flag times (09:45 UP_RED, 10:09 DOWN_BLUE, 10:42 UP_RED)?

STRICTLY READ-ONLY on the real-mode client: only get_minute_candles /
get_minute_candles_for_date / get_current_price are ever called. No order
method is imported or invoked for either mode.

Prints an OHLC comparison table for both feeds around each flag time, then
recomputes calculate_macd() (the shared, unmodified MACD function) on the
REAL feed alone (with REAL prior-day warm-up) to see whether the REAL price
series actually produces the 3 reported flags — this isolates "is it a data
feed difference" from "is it a computation bug".
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.trading.kis_client import create_kis_client  # noqa: E402
from app.trading.macd2 import config  # noqa: E402
from app.trading.macd2.market_data import MarketDataService, _candles_to_df  # noqa: E402
from app.trading.macd2.signal_engine import calculate_macd, evaluate_macd_crossover, resample_completed_3m  # noqa: E402

KST = config.KST
FLAG_TIMES = ["09:45", "10:09", "10:42"]


def _fetch_real_today_1m(client, symbol: str) -> pd.DataFrame:
    """Same backward-paging walk as market_data.py's bootstrap, but against
    the REAL client directly — read-only, get_minute_candles only."""
    pages = []
    hour1 = ""
    prev_count = 0
    for _ in range(6):
        candles = client.get_minute_candles(symbol, period_min=1, count=120, hour1=hour1) or []
        part = _candles_to_df(candles)
        if part.empty:
            break
        pages.append(part)
        merged = pd.concat(pages, ignore_index=True).drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)
        if len(merged) <= prev_count:
            break
        prev_count = len(merged)
        oldest = merged["datetime"].iloc[0]
        next_hour1 = (oldest - timedelta(minutes=1)).strftime("%H%M%S")
        if next_hour1 == hour1:
            break
        hour1 = next_hour1
    if not pages:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    return pd.concat(pages, ignore_index=True).drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)


def _fetch_real_prior_day(client, symbol: str, date_ymd: str) -> pd.DataFrame:
    pages = []
    hour1 = ""
    prev_count = 0
    for _ in range(6):
        candles = client.get_minute_candles_for_date(symbol, date_ymd, period_min=1, count=120, hour1=hour1) or []
        part = _candles_to_df(candles)
        if part.empty:
            break
        pages.append(part)
        merged = pd.concat(pages, ignore_index=True).drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)
        if len(merged) <= prev_count:
            break
        prev_count = len(merged)
        oldest = merged["datetime"].iloc[0]
        next_hour1 = (oldest - timedelta(minutes=1)).strftime("%H%M%S")
        if next_hour1 == hour1:
            break
        hour1 = next_hour1
    if not pages:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    return pd.concat(pages, ignore_index=True).drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=== KIS MOCK vs REAL 000660 1분봉 비교 (읽기 전용, 주문 없음) ===")

    now = datetime.now(KST)
    today_ymd = now.strftime("%Y%m%d")

    # MOCK feed (existing, already-verified path)
    mds = MarketDataService(mode="mock")
    mds.bootstrap(now=now)
    mock_df = mds.get_history_df().sort_values("datetime").drop_duplicates(subset=["datetime"]).reset_index(drop=True)
    mock_today = mock_df[mock_df["datetime"].dt.strftime("%Y%m%d") == today_ymd]
    mock_prior_days = sorted(mock_df[mock_df["datetime"].dt.strftime("%Y%m%d") != today_ymd]["datetime"].dt.strftime("%Y%m%d").unique())
    print(f"MOCK: today_bars={len(mock_today)} range={mock_today['datetime'].iloc[0] if len(mock_today) else None}~{mock_today['datetime'].iloc[-1] if len(mock_today) else None} prior_days={mock_prior_days}")

    # REAL feed — read-only client, only candle-fetch methods used
    real_client = create_kis_client(mode="real")
    if real_client is None:
        print("REAL 클라이언트 생성 실패 — 환경변수 확인 필요")
        return 1

    real_today = _fetch_real_today_1m(real_client, config.WATCH_SYMBOL)
    print(f"REAL: today_bars={len(real_today)} range={real_today['datetime'].iloc[0] if len(real_today) else None}~{real_today['datetime'].iloc[-1] if len(real_today) else None}")

    prior_day = mock_prior_days[-1] if mock_prior_days else (now - timedelta(days=3)).strftime("%Y%m%d")
    real_prior = _fetch_real_prior_day(real_client, config.WATCH_SYMBOL, prior_day)
    print(f"REAL prior-day({prior_day}) bars={len(real_prior)}")

    # ── OHLC comparison table around each flag time ─────────────────────
    print("\n[시각별 MOCK vs REAL 1분봉 OHLC 비교] (±3분)")
    for ft in FLAG_TIMES:
        hh, mm = int(ft[:2]), int(ft[3:5])
        center = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        window = [center + timedelta(minutes=d) for d in range(-3, 4)]
        print(f"\n-- {ft} 부근 --")
        print(f"{'time':<8}{'MOCK O/H/L/C':<32}{'REAL O/H/L/C':<32}{'diff%(close)':<12}")
        for t in window:
            mrow = mock_today[mock_today["datetime"] == t]
            rrow = real_today[real_today["datetime"] == t]
            mstr = "없음"
            rstr = "없음"
            diffpct = ""
            if not mrow.empty:
                m = mrow.iloc[0]
                mstr = f"{m['open']:.0f}/{m['high']:.0f}/{m['low']:.0f}/{m['close']:.0f}"
            if not rrow.empty:
                r = rrow.iloc[0]
                rstr = f"{r['open']:.0f}/{r['high']:.0f}/{r['low']:.0f}/{r['close']:.0f}"
            if not mrow.empty and not rrow.empty:
                mc, rc = float(mrow.iloc[0]["close"]), float(rrow.iloc[0]["close"])
                if rc:
                    diffpct = f"{(mc - rc) / rc * 100:+.3f}%"
            print(f"{t.strftime('%H:%M'):<8}{mstr:<32}{rstr:<32}{diffpct:<12}")

    # ── recompute confirmed crossovers from REAL feed alone ─────────────
    real_full = pd.concat([real_prior, real_today], ignore_index=True).drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    bars_3m_real = resample_completed_3m(real_full, now=now)
    session_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    today_bars_real = bars_3m_real[bars_3m_real["datetime"] >= session_open].reset_index(drop=True)

    last_dir = None
    real_flags = []
    for i in range(len(today_bars_real)):
        upto = bars_3m_real[bars_3m_real["datetime"] <= today_bars_real["datetime"].iloc[i]]
        snap = calculate_macd(upto)
        if snap is None:
            continue
        is_first = (i == 0)
        d = evaluate_macd_crossover(snap, last_dir) if not is_first else None
        if d is not None and d.value != "HOLD":
            real_flags.append((today_bars_real["datetime"].iloc[i].strftime("%H:%M"), d.value, round(snap.current_diff, 4)))
            last_dir = d

    print(f"\n[REAL 시세만으로 재계산한 confirmed 크로스오버 목록] {len(real_flags)}건 (calculate_macd/evaluate_macd_crossover 그대로 사용)")
    for f in real_flags:
        print(f"  {f[0]} {f[1]} diff={f[2]}")

    print("\n[정답 대조 — REAL 시세 기준]")
    real_by_time = {t: d for t, d, _ in real_flags}
    expected = [("09:45", "UP_RED"), ("10:09", "DOWN_BLUE"), ("10:42", "UP_RED")]
    match = 0
    for t, d in expected:
        got = real_by_time.get(t)
        ok = got == d
        match += int(ok)
        print(f"  {t} {d} -> REAL 계산: {got or '없음'} [{'O' if ok else 'X'}]")
    extra_real = [f for f in real_flags if f[0] not in dict(expected)]
    print(f"  일치: {match}/3, 추가신호(REAL): {len(extra_real)}건 {[f'{t} {d}' for t,d,_ in extra_real]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
