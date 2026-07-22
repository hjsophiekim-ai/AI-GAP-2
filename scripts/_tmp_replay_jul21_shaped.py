"""Replay Jul21 with Naver closes + yfinance intrabar shape for swing/VWAP."""
from __future__ import annotations

import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import hynix_switch_engine as engine  # noqa: E402
from app.trading.range_weighted_optimize import load_optimized_config  # noqa: E402
from scripts.replay_today_weighted_range import run_replay  # noqa: E402


def fetch_naver_closes(symbol: str, count: int = 4000) -> pd.DataFrame:
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
        ts, _o, _h, _l, c, v = parts[:6]
        if not ts.startswith("20260721"):
            continue
        try:
            close = float(c)
            cum_vol = int(float(v)) if v not in ("", "null", None) else 0
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        bar_vol = cum_vol if prev_vol is None else max(0, cum_vol - prev_vol)
        prev_vol = cum_vol
        dt = datetime.strptime(ts, "%Y%m%d%H%M")
        rows.append({"datetime": dt, "close": close, "volume": bar_vol, "time": dt.strftime("%H%M%S")})
    if not rows:
        return pd.DataFrame(columns=["datetime", "close", "volume", "time"])
    return pd.DataFrame(rows).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)


def fetch_yf_hynix() -> pd.DataFrame:
    raw = yf.download("000660.KS", start="2026-07-21", end="2026-07-22", interval="1m", progress=False)
    raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
    raw = raw.reset_index()
    col = "Datetime" if "Datetime" in raw.columns else raw.columns[0]
    out = pd.DataFrame(
        {
            "datetime": pd.to_datetime(raw[col]).dt.tz_localize(None),
            "open": raw["Open"].astype(float),
            "high": raw["High"].astype(float),
            "low": raw["Low"].astype(float),
            "close": raw["Close"].astype(float),
            "volume": raw["Volume"].fillna(0).astype(int),
        }
    )
    out["time"] = out["datetime"].dt.strftime("%H%M%S")
    return out.sort_values("datetime").reset_index(drop=True)


def apply_hynix_shape(etf_closes: pd.DataFrame, hynix: pd.DataFrame, leverage: float) -> pd.DataFrame:
    merged = etf_closes.merge(
        hynix[["datetime", "open", "high", "low", "close"]].rename(
            columns={"open": "h_o", "high": "h_h", "low": "h_l", "close": "h_c"}
        ),
        on="datetime",
        how="left",
    )
    rows = []
    for _, r in merged.iterrows():
        c = float(r["close"])
        if pd.isna(r.get("h_c")) or float(r["h_c"]) <= 0:
            rows.append({"datetime": r["datetime"], "open": c, "high": c, "low": c, "close": c, "volume": int(r["volume"]), "time": r["time"]})
            continue
        hc = float(r["h_c"])
        o_ret = leverage * (float(r["h_o"]) / hc - 1.0)
        h_ret = leverage * (float(r["h_h"]) / hc - 1.0)
        l_ret = leverage * (float(r["h_l"]) / hc - 1.0)
        o = c * (1.0 + o_ret)
        h = max(c * (1.0 + h_ret), o, c)
        l = min(c * (1.0 + l_ret), o, c)
        rows.append({"datetime": r["datetime"], "open": o, "high": h, "low": l, "close": c, "volume": int(r["volume"]), "time": r["time"]})
    return pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)


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


def main() -> int:
    load_optimized_config()
    print("Building Jul21 bars (Naver closes + yfinance Hynix OHLC shape)...")
    hynix = fetch_yf_hynix()
    # yfinance often ends ~14:59; extend with Naver closes shaped flat if needed
    naver_h = fetch_naver_closes("000660")
    if len(naver_h) > len(hynix):
        extra = naver_h[~naver_h["datetime"].isin(set(hynix["datetime"]))].copy()
        if len(extra):
            extra["open"] = extra["close"]
            extra["high"] = extra["close"]
            extra["low"] = extra["close"]
            hynix = pd.concat([hynix, extra], ignore_index=True).sort_values("datetime").reset_index(drop=True)

    long_df = apply_hynix_shape(fetch_naver_closes("0193T0"), hynix, leverage=2.0)
    inv_df = apply_hynix_shape(fetch_naver_closes("0197X0"), hynix, leverage=-2.0)
    for label, df in (("HYNIX", hynix), ("LEV", long_df), ("INV", inv_df)):
        print(f"  {label}: {len(df)}  {df['datetime'].min()} ~ {df['datetime'].max()}")

    after = run_replay(hynix, long_df, inv_df)
    summarize("AFTER", after)

    def old_detect(**kwargs):
        existing_direction = kwargs.get("existing_direction")
        new_direction = kwargs.get("new_direction")
        live_direction_matches = kwargs.get("live_direction_matches")
        confirm_dirs = kwargs.get("confirm_dirs") or {}
        if not existing_direction:
            return True
        if existing_direction == new_direction:
            return False
        if not live_direction_matches:
            return False
        if confirm_dirs.get(5) != new_direction or confirm_dirs.get(10) != new_direction:
            return False
        return bool(kwargs.get("existing_structure_broken") or kwargs.get("new_etf_vwap_reclaim"))

    orig = engine.detect_opposite_episode_transition
    engine.detect_opposite_episode_transition = old_detect  # type: ignore[assignment]
    before = run_replay(hynix, long_df, inv_df)
    engine.detect_opposite_episode_transition = orig
    summarize("BEFORE (5/10 always required)", before)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
