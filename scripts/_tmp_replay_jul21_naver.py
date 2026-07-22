"""Fetch Jul 21 1m bars from Naver fchart and replay weighted RANGE before/after logic."""
from __future__ import annotations

import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import hynix_switch_engine as engine  # noqa: E402
from scripts.replay_today_weighted_range import run_replay  # noqa: E402
from app.trading.range_weighted_optimize import load_optimized_config  # noqa: E402


def fetch_naver_minutes(symbol: str, count: int = 4000) -> pd.DataFrame:
    url = (
        f"https://fchart.stock.naver.com/sise.nhn?symbol={symbol}"
        f"&timeframe=minute&count={count}&requestType=0"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("euc-kr", "replace")
    rows: list[dict] = []
    prev_vol = None
    for match in re.finditer(r'data="([^"]+)"', raw):
        parts = match.group(1).split("|")
        if len(parts) < 6:
            continue
        ts, o, h, l, c, v = parts[:6]
        if not ts.startswith("20260721"):
            continue
        try:
            close = float(c)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        open_ = float(o) if o not in ("", "null", None) else close
        high = float(h) if h not in ("", "null", None) else close
        low = float(l) if l not in ("", "null", None) else close
        try:
            cum_vol = int(float(v)) if v not in ("", "null", None) else 0
        except (TypeError, ValueError):
            cum_vol = 0
        # Naver minute volume is cumulative; convert to per-bar when possible.
        if prev_vol is None:
            bar_vol = cum_vol
        else:
            bar_vol = max(0, cum_vol - prev_vol)
        prev_vol = cum_vol
        dt = datetime.strptime(ts, "%Y%m%d%H%M")
        rows.append(
            {
                "datetime": dt,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": bar_vol,
                "time": dt.strftime("%H%M%S"),
            }
        )
    if not rows:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume", "time"])
    return (
        pd.DataFrame(rows)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )


def summarize(label: str, result: dict) -> None:
    buys = [e for e in result["events"] if e["action"] == "매수"]
    down_buys = [e for e in buys if "12:45" <= e["time"] <= "14:00"]
    rebound_buys = [e for e in buys if "14:10" <= e["time"] <= "15:00"]
    inv = [e for e in buys if e.get("symbol") == "인버스"]
    pf = result["profit_factor_conservative"]
    pf_txt = f"{pf:.2f}" if pf is not None else "n/a"
    print(f"\n=== {label} ===")
    print(f"entries/RT: {result['entries']}/{result['round_trips']}")
    print(f"linear PnL: {result['net_pnl_krw']:+,.0f}")
    print(f"conservative PnL: {result['net_pnl_conservative_krw']:+,.0f}")
    print(f"conservative PF: {pf_txt}")
    print(f"blocked probe/reversal-repeat: {result['blocked_probe']}/{result['blocked_reversal_repeat']}")
    print(f"DOWN-window buys(12:45-14:00): {len(down_buys)}  inv total: {len(inv)}")
    print(f"rebound buys(14:10-15:00): {len(rebound_buys)}")
    for e in buys:
        print(f"  {e['time']} {e.get('symbol')} {e.get('path')} evidence={e.get('evidence')}")


def fill_ohlc_from_closes(df: pd.DataFrame) -> pd.DataFrame:
    """Naver minute feed often nulls OHLC; rebuild from close path for swing/VWAP."""
    out = df.copy()
    prev = out["close"].shift(1)
    out["open"] = prev.fillna(out["close"])
    out["high"] = out[["open", "close"]].max(axis=1)
    out["low"] = out[["open", "close"]].min(axis=1)
    return out


def main() -> int:
    load_optimized_config()
    print("Fetching Naver 1m bars for 2026-07-21...")
    hynix = fill_ohlc_from_closes(fetch_naver_minutes("000660", 4000))
    long_df = fill_ohlc_from_closes(fetch_naver_minutes("0193T0", 4000))
    inv_df = fill_ohlc_from_closes(fetch_naver_minutes("0197X0", 4000))
    for label, df in (("HYNIX", hynix), ("LEV", long_df), ("INV", inv_df)):
        print(f"  {label}: {len(df)} bars", end="")
        if len(df):
            print(f"  {df['datetime'].min()} ~ {df['datetime'].max()}")
        else:
            print()
    if min(len(hynix), len(long_df), len(inv_df)) < 100:
        print("ERROR: insufficient Naver minute data")
        return 1

    # Cache for reproducibility of this validation run.
    cache_dir = ROOT / "data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    hynix.to_csv(cache_dir / "replay_20260721_hynix_1m.csv", index=False)
    long_df.to_csv(cache_dir / "replay_20260721_long_1m.csv", index=False)
    inv_df.to_csv(cache_dir / "replay_20260721_inverse_1m.csv", index=False)

    after = run_replay(hynix, long_df, inv_df)
    summarize("AFTER (swing OR vwap+5/10; PROBE_FAILED=REVERSAL only)", after)

    def old_detect(**kwargs):
        existing_direction = kwargs.get("existing_direction")
        new_direction = kwargs.get("new_direction")
        live_direction_matches = kwargs.get("live_direction_matches")
        confirm_dirs = kwargs.get("confirm_dirs") or {}
        existing_structure_broken = kwargs.get("existing_structure_broken")
        new_etf_vwap_reclaim = kwargs.get("new_etf_vwap_reclaim")
        if not existing_direction:
            return True
        if existing_direction == new_direction:
            return False
        if not live_direction_matches:
            return False
        if confirm_dirs.get(5) != new_direction or confirm_dirs.get(10) != new_direction:
            return False
        return bool(existing_structure_broken or new_etf_vwap_reclaim)

    engine.detect_opposite_episode_transition = old_detect  # type: ignore[assignment]
    before = run_replay(hynix, long_df, inv_df)
    summarize("BEFORE (5/10 required even for swing path)", before)

    print("\nTarget: RT 6-9, Net PnL > 0, PF >= 1.3 (conservative preferred)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
