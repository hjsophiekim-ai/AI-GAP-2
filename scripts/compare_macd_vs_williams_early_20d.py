"""Read-only ≥20-day compare: MACD B only vs Williams 20% early + MACD B confirm.

Does NOT place broker orders. Does NOT modify production auto-trade modules.
Writes incremental JSON under data/state/ then a final JSON + markdown report.

Data priority:
  1) Local KIS replay caches (data/cache/replay_YYYYMMDD_*.csv)
  2) Naver fchart 1m (cached under data/cache/naver_multi_1m/)
  3) Daily-anchored synthetic 1m for remaining dates to reach ≥20 sessions
     (documented in JSON date_sources)

Usage:
    python scripts/compare_macd_vs_williams_early_20d.py
    python scripts/compare_macd_vs_williams_early_20d.py --days 20 --refetch-naver
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.trading.macd_hynix_strategy import (  # noqa: E402
    DIR_DOWN,
    DIR_UP,
    EXIT_SL,
    EXIT_TP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SYMBOL,
    evaluate_macd_direction,
    macd_components,
    resample_completed_3m,
)
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"
NAVER_CACHE = CACHE / "naver_multi_1m"
PARTIAL = STATE / "macd_vs_williams_early_20d_partial.json"
OUT_JSON = STATE / "macd_vs_williams_early_20d_compare.json"
OUT_MD = STATE / "macd_vs_williams_early_20d_compare.md"

INITIAL_CASH = 10_000_000.0
ENTRY_CUTOFF = (14, 55)
FORCE_HM = (15, 0)
WR_PERIOD = 14
WR_SIGNAL_SPAN = 9
EXPLORE_PCT = 0.20
EXPLORE_CONFIRM_SEC = 120  # wall-clock minutes*60 after explore fill
MIN_BARS = 300

# Stress fills: (label, delay_min after signal, adverse_pct)
SCENARIOS = (
    ("baseline", 1, 0.05),
    ("plus_1m_delay", 2, 0.05),
    ("plus_2m_slip10", 3, 0.10),
)

SYMBOLS = (SIGNAL_SYMBOL, LONG_SYMBOL, INVERSE_SYMBOL)
SYM_TAG = {
    SIGNAL_SYMBOL: "hynix",
    LONG_SYMBOL: "long",
    INVERSE_SYMBOL: "inverse",
}


# ── indicators ──────────────────────────────────────────────────────────────
def williams_r(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = WR_PERIOD) -> pd.Series:
    """%R = (HH - Close) / (HH - LL) * -100  (repo / jul22 convention)."""
    hh = highs.rolling(period).max()
    ll = lows.rolling(period).min()
    span = (hh - ll).replace(0.0, np.nan)
    return (hh - closes) / span * -100.0


def _macd_hist_series(closes: pd.Series) -> Optional[pd.Series]:
    comps = macd_components(closes)
    return comps.get("hist")


# ── data load / cache ───────────────────────────────────────────────────────
def _fetch_naver_raw(symbol: str, count: int = 9000) -> pd.DataFrame:
    url = (
        f"https://fchart.stock.naver.com/sise.nhn?symbol={symbol}"
        f"&timeframe=minute&count={count}&requestType=0"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("euc-kr", "replace")
    rows: list[dict] = []
    prev_vol = None
    prev_day = None
    for match in re.finditer(r'data="([^"]+)"', raw):
        parts = match.group(1).split("|")
        if len(parts) < 6:
            continue
        ts, o, h, l, c, v = parts[:6]
        if len(ts) < 12:
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
        day = ts[:8]
        if prev_day != day:
            prev_vol = None
            prev_day = day
        bar_vol = cum_vol if prev_vol is None else max(0, cum_vol - prev_vol)
        prev_vol = cum_vol
        dt = datetime.strptime(ts[:12], "%Y%m%d%H%M")
        rows.append(
            {
                "datetime": dt,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": bar_vol,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    return (
        pd.DataFrame(rows)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )


def _ensure_naver_cache(refetch: bool = False) -> dict[str, pd.DataFrame]:
    NAVER_CACHE.mkdir(parents=True, exist_ok=True)
    out: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        path = NAVER_CACHE / f"{sym}_1m.csv"
        if path.exists() and not refetch:
            df = pd.read_csv(path)
            df["datetime"] = pd.to_datetime(df["datetime"])
            out[sym] = df
            print(f"  naver cache {sym}: {len(df)} bars")
            continue
        print(f"  fetching naver {sym}…")
        df = _fetch_naver_raw(sym)
        df.to_csv(path, index=False)
        out[sym] = df
        print(f"  wrote {path.name}: {len(df)} bars")
    return out


def _split_by_day(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if df.empty:
        return {}
    work = df.copy()
    work["datetime"] = pd.to_datetime(work["datetime"])
    work["_day"] = work["datetime"].dt.strftime("%Y-%m-%d")
    return {
        day: g.drop(columns=["_day"]).sort_values("datetime").reset_index(drop=True)
        for day, g in work.groupby("_day")
    }


def _load_kis_day(day: str) -> Optional[dict[str, pd.DataFrame]]:
    tag = day.replace("-", "")
    files = {sym: CACHE / f"replay_{tag}_{SYM_TAG[sym]}_1m.csv" for sym in SYMBOLS}
    if not all(p.exists() for p in files.values()):
        return None
    out = {}
    for sym, path in files.items():
        df = pd.read_csv(path)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        if len(df) < MIN_BARS:
            return None
        out[sym] = df
    return out


def _session_minutes(day: str) -> list[datetime]:
    start = datetime.strptime(f"{day} 09:00:00", "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(f"{day} 15:20:00", "%Y-%m-%d %H:%M:%S")
    out = []
    t = start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=1)
    return out


def _synthetic_day_from_daily(row: pd.Series, day: str, rng: np.random.Generator) -> dict[str, pd.DataFrame]:
    """Build 1m paths anchored to daily OHLC; ETF ≈ ±2x intraday return."""
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])
    times = _session_minutes(day)
    n = len(times)
    # piecewise: open → high/low excursion → close
    up_first = c >= o
    mid = int(n * 0.45)
    mid2 = int(n * 0.70)
    path = np.empty(n)
    if up_first:
        path[:mid] = np.linspace(o, h, mid)
        path[mid:mid2] = np.linspace(h, l, mid2 - mid)
        path[mid2:] = np.linspace(l, c, n - mid2)
    else:
        path[:mid] = np.linspace(o, l, mid)
        path[mid:mid2] = np.linspace(l, h, mid2 - mid)
        path[mid2:] = np.linspace(h, c, n - mid2)
    noise = rng.normal(0.0, max(abs(h - l) * 0.0015, o * 1e-5), n)
    closes = np.maximum(path + noise, 1.0)

    def ohlc(closes_arr: np.ndarray, tick: float) -> pd.DataFrame:
        opens = np.empty(n)
        opens[0] = closes_arr[0]
        opens[1:] = closes_arr[:-1]
        highs = np.maximum(opens, closes_arr) * (1 + np.abs(rng.normal(0, 0.0008, n)))
        lows = np.minimum(opens, closes_arr) * (1 - np.abs(rng.normal(0, 0.0008, n)))
        return pd.DataFrame(
            {
                "datetime": times,
                "open": np.round(opens / tick) * tick,
                "high": np.round(highs / tick) * tick,
                "low": np.round(lows / tick) * tick,
                "close": np.round(closes_arr / tick) * tick,
                "volume": rng.integers(5_000, 30_000, n),
            }
        )

    und = ohlc(closes, 1000.0)
    ret = closes / closes[0] - 1.0
    # ETF anchors near recent observed levels
    lev0 = 13_300.0
    inv0 = 12_250.0
    lev = ohlc(lev0 * (1 + 2.0 * ret + np.cumsum(rng.normal(0, 0.0004, n))), 5.0)
    inv = ohlc(inv0 * (1 - 2.0 * ret + np.cumsum(rng.normal(0, 0.0004, n))), 5.0)
    return {SIGNAL_SYMBOL: und, LONG_SYMBOL: lev, INVERSE_SYMBOL: inv}


def build_day_universe(n_days: int, refetch_naver: bool) -> tuple[list[str], dict[str, str], dict[str, dict[str, pd.DataFrame]]]:
    """Return (dates, date_sources, day_data).

    Priority per day:
      1) Naver fchart overlapping sessions (≥MIN_BARS) — preferred real multi-day source
      2) Local replay_YYYYMMDD_*.csv if present (may be KIS or previously cached)
      3) Daily-anchored synthetic fill to reach n_days
    """
    naver = _ensure_naver_cache(refetch=refetch_naver)
    by_sym = {sym: _split_by_day(df) for sym, df in naver.items()}
    overlap = set(by_sym[SIGNAL_SYMBOL])
    for sym in SYMBOLS[1:]:
        overlap &= set(by_sym[sym])
    real_days = sorted(
        d
        for d in overlap
        if all(len(by_sym[s][d]) >= MIN_BARS for s in SYMBOLS)
    )
    daily_path = ROOT / "data" / "hynix" / "hynix_daily.csv"
    daily = pd.read_csv(daily_path)
    daily["date_iso"] = pd.to_datetime(daily["datetime"]).dt.strftime("%Y-%m-%d")
    daily = daily.sort_values("date_iso")
    calendar = list(daily["date_iso"].tolist())
    target = calendar[-n_days:] if len(calendar) >= n_days else list(calendar)
    for d in real_days[-n_days:]:
        if d not in target:
            target.append(d)
    target = sorted(set(target))[-n_days:]

    rng = np.random.default_rng(20260722)
    date_sources: dict[str, str] = {}
    day_data: dict[str, dict[str, pd.DataFrame]] = {}

    for day in target:
        if day in real_days:
            day_data[day] = {sym: by_sym[sym][day].copy() for sym in SYMBOLS}
            # Also refresh per-day cache files for reuse by sibling agents
            tag = day.replace("-", "")
            for sym, df in day_data[day].items():
                outp = CACHE / f"replay_{tag}_{SYM_TAG[sym]}_1m.csv"
                if not outp.exists():
                    df.to_csv(outp, index=False)
            date_sources[day] = "naver_fchart"
            continue
        kis = _load_kis_day(day)
        if kis is not None:
            day_data[day] = kis
            # Without Naver overlap these are usually daily-anchored synthetics written earlier
            date_sources[day] = "synthetic_cached_1m"
            continue
        row = daily[daily["date_iso"] == day]
        if row.empty:
            continue
        day_data[day] = _synthetic_day_from_daily(row.iloc[0], day, rng)
        date_sources[day] = "synthetic_daily_anchor"
        tag = day.replace("-", "")
        for sym, df in day_data[day].items():
            outp = CACHE / f"replay_{tag}_{SYM_TAG[sym]}_1m.csv"
            df.to_csv(outp, index=False)

    dates = sorted(day_data.keys())
    if len(dates) < n_days:
        for day in reversed(calendar):
            if day in day_data:
                continue
            row = daily[daily["date_iso"] == day]
            if row.empty:
                continue
            day_data[day] = _synthetic_day_from_daily(row.iloc[0], day, rng)
            date_sources[day] = "synthetic_daily_anchor"
            tag = day.replace("-", "")
            for sym, df in day_data[day].items():
                df.to_csv(CACHE / f"replay_{tag}_{SYM_TAG[sym]}_1m.csv", index=False)
            if len(day_data) >= n_days:
                break
        dates = sorted(day_data.keys())[-n_days:]
        day_data = {d: day_data[d] for d in dates}
        date_sources = {d: date_sources[d] for d in dates}
    return dates, date_sources, day_data


# ── fill / trade helpers ────────────────────────────────────────────────────
@dataclass
class Trade:
    strategy: str
    day: str
    direction: str
    symbol: str
    signal_time: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    qty: int
    gross_pnl: float
    cost: float
    net_pnl: float
    exit_reason: str
    size_pct: float
    entry_kind: str
    signal_to_fill_sec: float
    wrong_direction: bool = False


def _fill(
    df: pd.DataFrame,
    signal_ts: datetime,
    side: str,
    delay_min: int,
    adverse_pct: float,
) -> tuple[Optional[datetime], Optional[float]]:
    sig = signal_ts.replace(second=0, microsecond=0)
    target = sig + timedelta(minutes=max(1, delay_min))
    sub = df[df["datetime"] >= target]
    if sub.empty:
        return None, None
    row = sub.iloc[0]
    ts = pd.Timestamp(row["datetime"]).to_pydatetime()
    px = float(row["open"])
    if side == "BUY":
        px *= 1.0 + adverse_pct / 100.0
    else:
        px *= 1.0 - adverse_pct / 100.0
    return ts, float(px)


def _px_at(df: pd.DataFrame, ts: datetime) -> Optional[float]:
    sub = df[df["datetime"] <= ts]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


def _day_range_pct(hynix: pd.DataFrame) -> float:
    if hynix.empty:
        return 0.0
    hi = float(hynix["high"].max())
    lo = float(hynix["low"].min())
    op = float(hynix.iloc[0]["open"])
    if op <= 0:
        return 0.0
    return (hi - lo) / op * 100.0


def _true_day_dir(hynix: pd.DataFrame) -> str:
    if len(hynix) < 2:
        return "FLAT"
    a = float(hynix.iloc[0]["open"])
    b = float(hynix.iloc[-1]["close"])
    if b > a * 1.002:
        return "UP"
    if b < a * 0.998:
        return "DOWN"
    return "FLAT"


# ── PRE / Williams early detection ──────────────────────────────────────────
def _precompute_day_features(hynix: pd.DataFrame) -> dict[str, Any]:
    """One-pass indicators for a session (avoids O(n²) recompute in the minute loop)."""
    df = hynix.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    closes = pd.to_numeric(df["close"], errors="coerce")
    highs = pd.to_numeric(df["high"], errors="coerce")
    lows = pd.to_numeric(df["low"], errors="coerce")
    wr = williams_r(highs, lows, closes)
    wr_sig = wr.ewm(span=WR_SIGNAL_SPAN, adjust=False).mean()
    gap = wr - wr_sig
    hist_1m = _macd_hist_series(closes)

    bars3 = resample_completed_3m(df, now=df["datetime"].iloc[-1] + timedelta(minutes=3))
    hist3_series: list[tuple[datetime, float]] = []
    if len(bars3) >= 26:
        hser = _macd_hist_series(pd.to_numeric(bars3["close"], errors="coerce"))
        if hser is not None:
            for i in range(len(bars3)):
                ct = pd.Timestamp(bars3.iloc[i]["datetime"]).to_pydatetime() + timedelta(minutes=3)
                hv = float(hser.iloc[i])
                if not math.isnan(hv):
                    hist3_series.append((ct, hv))

    pre: dict[datetime, str] = {}
    wr_by_close: dict[datetime, float] = {}
    for i in range(len(df)):
        bar_ts = pd.Timestamp(df.iloc[i]["datetime"]).to_pydatetime()
        close_ts = bar_ts + timedelta(minutes=1)
        w = float(wr.iloc[i]) if i < len(wr) and wr.iloc[i] == wr.iloc[i] else float("nan")
        if not math.isnan(w):
            wr_by_close[close_ts] = w

    start_i = max(WR_PERIOD + WR_SIGNAL_SPAN, 30)
    h3_end = 0
    for i in range(start_i, len(df)):
        bar_ts = pd.Timestamp(df.iloc[i]["datetime"]).to_pydatetime()
        sig_ts = bar_ts + timedelta(minutes=1)
        w0, w1, w2 = float(wr.iloc[i - 2]), float(wr.iloc[i - 1]), float(wr.iloc[i])
        g0, g1, g2 = float(gap.iloc[i - 2]), float(gap.iloc[i - 1]), float(gap.iloc[i])
        if any(math.isnan(x) for x in (w0, w1, w2, g0, g1, g2)):
            continue
        if hist_1m is None or i >= len(hist_1m) or math.isnan(float(hist_1m.iloc[i])):
            continue
        h1m = float(hist_1m.iloc[i])
        while h3_end < len(hist3_series) and hist3_series[h3_end][0] <= sig_ts:
            h3_end += 1
        if h3_end < 4:
            continue
        h_a = hist3_series[h3_end - 4][1]
        h_b = hist3_series[h3_end - 3][1]
        h_c = hist3_series[h3_end - 2][1]
        h_d = hist3_series[h3_end - 1][1]
        d1, d2, d3 = h_b - h_a, h_c - h_b, h_d - h_c
        if (
            w2 > w1 > w0
            and g2 > g1 > g0
            and d1 < 0
            and d2 < 0
            and d3 < 0
            and abs(d3) < abs(d2) < abs(d1)
            and h1m > 0
        ):
            pre[sig_ts] = "UP"
        elif (
            w2 < w1 < w0
            and g2 < g1 < g0
            and d1 > 0
            and d2 > 0
            and d3 > 0
            and abs(d3) < abs(d2) < abs(d1)
            and h1m < 0
        ):
            pre[sig_ts] = "DOWN"

    macd_events: list[tuple[datetime, str, Optional[str]]] = []
    last_dir = None
    last_bar = None
    for ct, _ in hist3_series:
        hist_slice = df[df["datetime"] < ct]
        ev = evaluate_macd_direction(
            hist_slice, now=ct, last_signal_direction=last_dir, last_signal_bar_ts=last_bar
        )
        if not ev.get("ok"):
            continue
        if ev.get("new_signal"):
            direction = ev["signal_direction"]
            last_dir = direction
            last_bar = ev.get("bar_ts")
            macd_events.append((ct, direction, ev.get("bar_ts")))
        else:
            macd_events.append((ct, "HOLD", ev.get("bar_ts")))

    return {"pre": pre, "wr_by_close": wr_by_close, "macd_events": macd_events}


def _wr_invalidated_fast(wr_by_close: dict[datetime, float], direction: str, now: datetime) -> bool:
    times = [t for t in wr_by_close if t <= now]
    if len(times) < 2:
        return False
    w1, w2 = wr_by_close[times[-2]], wr_by_close[times[-1]]
    if direction in (DIR_UP, "UP"):
        return w2 < w1
    return w2 > w1


# ── day replay ──────────────────────────────────────────────────────────────
def replay_day_strategy(
    day: str,
    data: dict[str, pd.DataFrame],
    strategy: str,
    *,
    delay_min: int,
    adverse_pct: float,
    features: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    cost_engine = TradeCostEngine()
    hynix = data[SIGNAL_SYMBOL]
    long_df = data[LONG_SYMBOL]
    inv_df = data[INVERSE_SYMBOL]
    true_dir = _true_day_dir(hynix)
    feat = features or _precompute_day_features(hynix)
    pre_map: dict[datetime, str] = feat["pre"]
    wr_by_close: dict[datetime, float] = feat["wr_by_close"]
    wr_times = sorted(wr_by_close.keys())
    macd_events = feat["macd_events"]

    trades: list[Trade] = []
    realized = 0.0
    last_dir: Optional[str] = None
    position: Optional[dict] = None
    used_pre: set[str] = set()
    explore_stats = {
        "explore_starts": 0,
        "explore_scaled": 0,
        "explore_invalidated": 0,
        "explore_timeout": 0,
        "explore_pnl": 0.0,
        "minutes_earlier_vs_a": [],
        "wrong_explores": [],
    }
    first_macd_up: Optional[datetime] = None
    first_macd_dn: Optional[datetime] = None
    delays: list[float] = []

    minutes = [pd.Timestamp(t).to_pydatetime() for t in hynix["datetime"].tolist()]
    force_ts = datetime.strptime(f"{day} 15:00:00", "%Y-%m-%d %H:%M:%S")
    pre_times = sorted(pre_map.keys())
    pre_i = 0
    macd_i = 0
    wr_i = 0  # index of last wr close <= ts

    # ETF close arrays for fast mark
    long_close = {pd.Timestamp(r["datetime"]).to_pydatetime(): float(r["close"]) for _, r in long_df.iterrows()}
    inv_close = {pd.Timestamp(r["datetime"]).to_pydatetime(): float(r["close"]) for _, r in inv_df.iterrows()}
    long_times = sorted(long_close.keys())
    inv_times = sorted(inv_close.keys())
    li = ii = 0

    def equity() -> float:
        return INITIAL_CASH + realized

    def mark_px(symbol: str, ts: datetime) -> Optional[float]:
        nonlocal li, ii
        if symbol == LONG_SYMBOL:
            while li + 1 < len(long_times) and long_times[li + 1] <= ts:
                li += 1
            if not long_times or long_times[li] > ts:
                return None
            return long_close[long_times[li]]
        while ii + 1 < len(inv_times) and inv_times[ii + 1] <= ts:
            ii += 1
        if not inv_times or inv_times[ii] > ts:
            return None
        return inv_close[inv_times[ii]]

    def hit_tp_sl(symbol: str, entry: float, cur: float, qty: int) -> Optional[str]:
        notional = entry * qty
        if notional <= 0:
            return None
        bd = cost_engine.compute_net_pnl(
            symbol, entry, cur, qty, buy_order_type="market", sell_order_type="market"
        )
        pct = float(bd["net_pnl"]) / notional * 100.0
        if pct <= -1.5:
            return EXIT_SL
        if pct >= 3.0:
            return EXIT_TP
        return None

    def close_pos(reason: str, signal_ts: datetime) -> None:
        nonlocal position, realized
        if position is None:
            return
        etf = long_df if position["symbol"] == LONG_SYMBOL else inv_df
        xts, xpx = _fill(etf, signal_ts, "SELL", delay_min, adverse_pct)
        if xpx is None:
            xpx = float(position["entry_price"])
            xts = signal_ts
        bd = cost_engine.compute_net_pnl(
            position["symbol"], position["entry_price"], xpx, position["qty"],
            buy_order_type="market", sell_order_type="market",
        )
        wrong = False
        if true_dir in ("UP", "DOWN"):
            want = LONG_SYMBOL if true_dir == "UP" else INVERSE_SYMBOL
            wrong = position["symbol"] != want and bd["net_pnl"] < 0
        t = Trade(
            strategy=strategy,
            day=day,
            direction=position["direction"],
            symbol=position["symbol"],
            signal_time=position["signal_time"],
            entry_time=str(position["entry_time"]),
            entry_price=float(position["entry_price"]),
            exit_time=str(xts),
            exit_price=float(xpx),
            qty=int(position["qty"]),
            gross_pnl=float(bd["gross_pnl"]),
            cost=float(bd["total_cost"]),
            net_pnl=float(bd["net_pnl"]),
            exit_reason=reason,
            size_pct=float(position.get("size_pct", 1.0)),
            entry_kind=str(position.get("entry_kind", "FULL")),
            signal_to_fill_sec=float(position.get("delay_sec", 0.0)),
            wrong_direction=wrong,
        )
        trades.append(t)
        realized += t.net_pnl
        if position.get("entry_kind") == "EXPLORE" and reason not in (
            "SCALE_TO_FULL",
            "SCALE_REPLACE",
        ):
            explore_stats["explore_pnl"] += t.net_pnl
        position = None

    def open_pos(direction: str, signal_ts: datetime, size_pct: float, kind: str) -> bool:
        nonlocal position
        target = LONG_SYMBOL if direction in (DIR_UP, "UP") else INVERSE_SYMBOL
        etf = long_df if target == LONG_SYMBOL else inv_df
        ets, epx = _fill(etf, signal_ts, "BUY", delay_min, adverse_pct)
        if epx is None or epx <= 0:
            return False
        qty = int((equity() * size_pct) // epx)
        if qty < 1:
            return False
        delay_sec = max(0.0, (ets - signal_ts).total_seconds()) if ets else float(delay_min * 60)
        delays.append(delay_sec)
        dir_key = DIR_UP if direction in (DIR_UP, "UP") else DIR_DOWN
        position = {
            "symbol": target,
            "direction": dir_key,
            "qty": qty,
            "entry_price": epx,
            "entry_time": ets,
            "signal_time": signal_ts.isoformat(),
            "size_pct": size_pct,
            "entry_kind": kind,
            "delay_sec": delay_sec,
            "explore_deadline": (ets + timedelta(seconds=EXPLORE_CONFIRM_SEC)) if kind == "EXPLORE" else None,
        }
        return True

    for ts in minutes:
        hm = (ts.hour, ts.minute)
        while wr_i + 1 < len(wr_times) and wr_times[wr_i + 1] <= ts:
            wr_i += 1

        if hm >= FORCE_HM and position is not None:
            close_pos("15:00_FORCE", force_ts)
            continue

        if position is not None:
            cur = mark_px(position["symbol"], ts)
            if cur is not None:
                hit = hit_tp_sl(position["symbol"], position["entry_price"], cur, position["qty"])
                if hit:
                    close_pos(hit, ts)

        if strategy == "B" and position is not None and position.get("entry_kind") == "EXPLORE":
            dead = position.get("explore_deadline")
            if dead is not None and ts >= dead:
                explore_stats["explore_timeout"] += 1
                close_pos("EXPLORE_TIMEOUT", ts)
            elif wr_i >= 1 and _wr_invalidated_fast(
                {wr_times[wr_i - 1]: wr_by_close[wr_times[wr_i - 1]], wr_times[wr_i]: wr_by_close[wr_times[wr_i]]},
                position["direction"],
                ts,
            ):
                # use last two wr points
                w1 = wr_by_close[wr_times[wr_i - 1]]
                w2 = wr_by_close[wr_times[wr_i]]
                bad = (w2 < w1) if position["direction"] == DIR_UP else (w2 > w1)
                if bad:
                    explore_stats["explore_invalidated"] += 1
                    close_pos("WR_INVALIDATE", ts)

        if strategy == "B" and hm < ENTRY_CUTOFF:
            while pre_i < len(pre_times) and pre_times[pre_i] <= ts:
                pre_ts = pre_times[pre_i]
                pre_dir = pre_map[pre_ts]
                pre_i += 1
                pre_key = f"{pre_dir}:{pre_ts.isoformat()}"
                if pre_key in used_pre:
                    continue
                if position is not None:
                    same = (position["direction"] == DIR_UP and pre_dir == "UP") or (
                        position["direction"] == DIR_DOWN and pre_dir == "DOWN"
                    )
                    if same:
                        used_pre.add(pre_key)
                        continue
                    close_pos("PRE_OPPOSITE", pre_ts)
                used_pre.add(pre_key)
                if open_pos(pre_dir, pre_ts, EXPLORE_PCT, "EXPLORE"):
                    explore_stats["explore_starts"] += 1
                    if true_dir in ("UP", "DOWN") and pre_dir != true_dir:
                        explore_stats["wrong_explores"].append(
                            {"time": pre_ts.isoformat(), "direction": pre_dir, "true_dir": true_dir}
                        )

        # MACD events at this timestamp
        while macd_i < len(macd_events) and macd_events[macd_i][0] <= ts:
            ct, direction, _bar = macd_events[macd_i]
            macd_i += 1
            if direction == "HOLD":
                continue
            last_dir = direction
            if direction == DIR_UP and first_macd_up is None:
                first_macd_up = ct
            if direction == DIR_DOWN and first_macd_dn is None:
                first_macd_dn = ct

            if (
                strategy == "B"
                and position is not None
                and position.get("entry_kind") == "EXPLORE"
                and position["direction"] == direction
                and (ct.hour, ct.minute) < ENTRY_CUTOFF
            ):
                target = position["symbol"]
                etf = long_df if target == LONG_SYMBOL else inv_df
                ets, epx = _fill(etf, ct, "BUY", delay_min, adverse_pct)
                if epx and epx > 0:
                    target_qty = max(position["qty"], int(equity() // epx))
                    add = target_qty - position["qty"]
                    if add > 0:
                        new_cost = position["entry_price"] * position["qty"] + epx * add
                        position["qty"] = target_qty
                        position["entry_price"] = new_cost / target_qty
                    position["entry_kind"] = "FULL_CONFIRM"
                    position["size_pct"] = 1.0
                    position["explore_deadline"] = None
                    explore_stats["explore_scaled"] += 1
                    delays.append(max(0.0, (ets - ct).total_seconds()) if ets else float(delay_min * 60))
                continue

            if (ct.hour, ct.minute) >= ENTRY_CUTOFF:
                if position is not None and position["direction"] != direction:
                    close_pos(f"OPPOSITE_{direction}", ct)
                continue

            if strategy == "A":
                if position is not None and position["direction"] != direction:
                    close_pos(f"OPPOSITE_{direction}", ct)
                    open_pos(direction, ct, 1.0, "FULL")
                elif position is None:
                    open_pos(direction, ct, 1.0, "FULL")
            else:
                if position is not None and position["direction"] != direction:
                    close_pos(f"OPPOSITE_{direction}", ct)
                    open_pos(direction, ct, 1.0, "FULL_OPPOSITE")

    if position is not None:
        close_pos("EOD_FLAT", force_ts)

    if strategy == "B":
        for pre_ts, d in sorted(pre_map.items()):
            macd_t = first_macd_up if d == "UP" else first_macd_dn
            if macd_t is not None and pre_ts < macd_t:
                explore_stats["minutes_earlier_vs_a"].append(
                    round((macd_t - pre_ts).total_seconds() / 60.0, 2)
                )

    nets = [t.net_pnl for t in trades]
    wins = [n for n in nets if n > 0]
    gp = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
    gl = abs(sum(t.gross_pnl for t in trades if t.gross_pnl < 0))
    pf = (gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0)
    eq = INITIAL_CASH
    peak = eq
    mdd = 0.0
    for n in nets:
        eq += n
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100.0 if peak else 0.0)

    exit_counts = {
        "TP": sum(1 for t in trades if t.exit_reason == EXIT_TP),
        "SL": sum(1 for t in trades if t.exit_reason == EXIT_SL),
        "OPPOSITE": sum(1 for t in trades if t.exit_reason.startswith("OPPOSITE") or t.exit_reason == "PRE_OPPOSITE"),
        "FORCE_1500": sum(1 for t in trades if "15:00" in t.exit_reason or t.exit_reason == "EOD_FLAT"),
        "EXPLORE_EXIT": sum(
            1 for t in trades if t.exit_reason in ("EXPLORE_TIMEOUT", "WR_INVALIDATE", "SCALE_REPLACE")
        ),
    }
    cost = sum(t.cost for t in trades)
    return {
        "day": day,
        "strategy": strategy,
        "net_pnl": round(sum(nets), 2),
        "ret_pct": round(sum(nets) / INITIAL_CASH * 100.0, 4),
        "round_trips": len(trades),
        "win_rate_pct": round(len(wins) / len(nets) * 100.0, 2) if nets else 0.0,
        "pf": round(pf, 3) if pf < 900 else None,
        "mdd_pct": round(mdd, 3),
        "total_cost": round(cost, 2),
        "cost_gross_ratio_pct": round(cost / gp * 100.0, 2) if gp > 0 else None,
        "lev_pnl": round(sum(t.net_pnl for t in trades if t.symbol == LONG_SYMBOL), 2),
        "inv_pnl": round(sum(t.net_pnl for t in trades if t.symbol == INVERSE_SYMBOL), 2),
        "lev_trades": sum(1 for t in trades if t.symbol == LONG_SYMBOL),
        "inv_trades": sum(1 for t in trades if t.symbol == INVERSE_SYMBOL),
        "exit_counts": exit_counts,
        "day_range_pct": round(_day_range_pct(hynix), 3),
        "true_dir": true_dir,
        "avg_signal_to_fill_sec": round(statistics.mean(delays), 1) if delays else None,
        "explore": explore_stats if strategy == "B" else None,
        "trades": [asdict(t) for t in trades],
    }


# ── aggregate / verdict ─────────────────────────────────────────────────────
def aggregate(day_results: list[dict], strategy: str) -> dict[str, Any]:
    rows = [r for r in day_results if r["strategy"] == strategy]
    if not rows:
        return {}
    nets = [r["net_pnl"] for r in rows]
    rets = [r["ret_pct"] for r in rows]
    all_trades = [t for r in rows for t in r["trades"]]
    wins = [t for t in all_trades if t["net_pnl"] > 0]
    losses = [t for t in all_trades if t["net_pnl"] < 0]
    gp = sum(t["gross_pnl"] for t in all_trades if t["gross_pnl"] > 0)
    gl = abs(sum(t["gross_pnl"] for t in all_trades if t["gross_pnl"] < 0))
    pf = (gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0)
    eq = INITIAL_CASH
    peak = eq
    mdd = 0.0
    for t in sorted(all_trades, key=lambda x: (x["day"], x["exit_time"])):
        eq += t["net_pnl"]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100.0 if peak else 0.0)
    cost = sum(t["cost"] for t in all_trades)
    best = max(all_trades, key=lambda t: t["net_pnl"]) if all_trades else None
    ex_best_net = sum(t["net_pnl"] for t in all_trades if best is None or t is not best)
    # fix ex-best: compare by identity via max net
    if all_trades:
        best_net = max(t["net_pnl"] for t in all_trades)
        removed = False
        ex_best_net = 0.0
        for t in all_trades:
            if not removed and t["net_pnl"] == best_net:
                removed = True
                continue
            ex_best_net += t["net_pnl"]

    ranges = [r["day_range_pct"] for r in rows]
    med_range = statistics.median(ranges) if ranges else 0.0
    for r in rows:
        r["regime"] = "TREND" if r["day_range_pct"] >= med_range else "RANGE"

    def _split(reg: str) -> dict:
        sub = [r for r in rows if r["regime"] == reg]
        return {
            "days": len(sub),
            "net_pnl": round(sum(r["net_pnl"] for r in sub), 2),
            "mean_ret_pct": round(statistics.mean([r["ret_pct"] for r in sub]), 4) if sub else 0.0,
        }

    exit_tot = {"TP": 0, "SL": 0, "OPPOSITE": 0, "FORCE_1500": 0, "EXPLORE_EXIT": 0}
    for r in rows:
        for k, v in r["exit_counts"].items():
            exit_tot[k] = exit_tot.get(k, 0) + v

    explore = None
    if strategy == "B":
        starts = sum((r["explore"] or {}).get("explore_starts", 0) for r in rows)
        scaled = sum((r["explore"] or {}).get("explore_scaled", 0) for r in rows)
        inv = sum((r["explore"] or {}).get("explore_invalidated", 0) for r in rows)
        to = sum((r["explore"] or {}).get("explore_timeout", 0) for r in rows)
        epnl = sum((r["explore"] or {}).get("explore_pnl", 0.0) for r in rows)
        earlier = [
            m
            for r in rows
            for m in ((r["explore"] or {}).get("minutes_earlier_vs_a") or [])
        ]
        wrong = [w for r in rows for w in ((r["explore"] or {}).get("wrong_explores") or [])]
        explore = {
            "explore_starts": starts,
            "explore_scaled": scaled,
            "explore_success_rate_pct": round(scaled / starts * 100.0, 2) if starts else None,
            "explore_invalidated": inv,
            "explore_timeout": to,
            "explore_pnl": round(epnl, 2),
            "avg_minutes_earlier_than_A": round(statistics.mean(earlier), 2) if earlier else None,
            "wrong_explores_count": len(wrong),
            "wrong_explores": wrong[:40],
        }

    delays = [r["avg_signal_to_fill_sec"] for r in rows if r.get("avg_signal_to_fill_sec") is not None]

    return {
        "strategy": strategy,
        "n_days": len(rows),
        "daily_pnl": [{"day": r["day"], "net_pnl": r["net_pnl"], "ret_pct": r["ret_pct"], "regime": r["regime"]} for r in rows],
        "cum_net_pnl": round(sum(nets), 2),
        "cum_ret_pct": round(sum(nets) / INITIAL_CASH * 100.0, 4),
        "mean_daily_ret_pct": round(statistics.mean(rets), 4) if rets else 0.0,
        "median_daily_ret_pct": round(statistics.median(rets), 4) if rets else 0.0,
        "win_days": sum(1 for n in nets if n > 0),
        "loss_days": sum(1 for n in nets if n < 0),
        "flat_days": sum(1 for n in nets if n == 0),
        "round_trips": len(all_trades),
        "avg_trades_per_day": round(len(all_trades) / len(rows), 2) if rows else 0.0,
        "win_rate_pct": round(len(wins) / len(all_trades) * 100.0, 2) if all_trades else 0.0,
        "pf": round(pf, 3) if pf < 900 else None,
        "mdd_pct": round(mdd, 3),
        "total_cost": round(cost, 2),
        "cost_gross_ratio_pct": round(cost / gp * 100.0, 2) if gp > 0 else None,
        "lev_pnl": round(sum(t["net_pnl"] for t in all_trades if t["symbol"] == LONG_SYMBOL), 2),
        "inv_pnl": round(sum(t["net_pnl"] for t in all_trades if t["symbol"] == INVERSE_SYMBOL), 2),
        "exit_counts": exit_tot,
        "worst_day": min(rows, key=lambda r: r["net_pnl"])["day"] if rows else None,
        "worst_day_pnl": min(nets) if nets else 0.0,
        "ex_best_trade_net": round(ex_best_net, 2),
        "range_vs_trend": {"RANGE": _split("RANGE"), "TREND": _split("TREND"), "median_day_range_pct": round(med_range, 3)},
        "avg_signal_to_fill_sec": round(statistics.mean(delays), 1) if delays else None,
        "explore": explore,
        "regime_definition": "TREND if day (high-low)/open% >= sample median else RANGE",
    }


def decide_verdict(a: dict, b: dict, stress: dict) -> dict[str, Any]:
    reasons: list[str] = []
    winner = "NO_CLEAR_WINNER"

    def better_net(x, y):
        return (x.get("cum_net_pnl") or 0) - (y.get("cum_net_pnl") or 0)

    d_net = better_net(b, a)
    a_pf = a.get("pf") or 0
    b_pf = b.get("pf") or 0
    a_mdd = a.get("mdd_pct") or 0
    b_mdd = b.get("mdd_pct") or 0
    a_loss = a.get("loss_days") or 0
    b_loss = b.get("loss_days") or 0
    a_worst = a.get("worst_day_pnl") or 0
    b_worst = b.get("worst_day_pnl") or 0
    a_cost = a.get("total_cost") or 0
    b_cost = b.get("total_cost") or 0
    a_rt = a.get("round_trips") or 0
    b_rt = b.get("round_trips") or 0

    # Delay sensitivity: net drop from baseline → plus_2m
    a_sens = (stress.get("A", {}).get("baseline", {}).get("cum_net_pnl") or 0) - (
        stress.get("A", {}).get("plus_2m_slip10", {}).get("cum_net_pnl") or 0
    )
    b_sens = (stress.get("B", {}).get("baseline", {}).get("cum_net_pnl") or 0) - (
        stress.get("B", {}).get("plus_2m_slip10", {}).get("cum_net_pnl") or 0
    )

    score_a = 0
    score_b = 0
    # 1 Net
    if d_net > 50_000:
        score_b += 2
        reasons.append(f"Net favors B by {d_net:,.0f}")
    elif d_net < -50_000:
        score_a += 2
        reasons.append(f"Net favors A by {-d_net:,.0f}")
    else:
        reasons.append(f"Net similar (Δ={d_net:,.0f})")

    # 2 PF
    if b_pf > a_pf * 1.05:
        score_b += 1
        reasons.append(f"PF B {b_pf} > A {a_pf}")
    elif a_pf > b_pf * 1.05:
        score_a += 1
        reasons.append(f"PF A {a_pf} > B {b_pf}")

    # 3 MDD (lower better)
    if b_mdd > a_mdd * 1.15 + 0.1:
        score_a += 2
        reasons.append(f"MDD worse for B ({b_mdd}% vs {a_mdd}%)")
    elif a_mdd > b_mdd * 1.15 + 0.1:
        score_b += 1
        reasons.append(f"MDD better for B ({b_mdd}% vs {a_mdd}%)")

    # 4 loss days / max day loss
    if b_loss > a_loss + 1 or b_worst < a_worst * 1.15:
        score_a += 1
        reasons.append(f"Loss days/worst: A={a_loss}/{a_worst:,.0f} B={b_loss}/{b_worst:,.0f}")
    elif a_loss > b_loss + 1 or a_worst < b_worst * 1.15:
        score_b += 1
        reasons.append(f"Loss days/worst favor B")

    # 5 overtrading / costs
    if b_rt > a_rt * 1.5 or b_cost > a_cost * 1.4:
        score_a += 1
        reasons.append(f"B overtrades/costs (RT {b_rt} vs {a_rt}, cost {b_cost:,.0f} vs {a_cost:,.0f})")
    elif a_rt > b_rt * 1.5 or a_cost > b_cost * 1.4:
        score_b += 1
        reasons.append("A higher costs/trades")

    # 6 delay sensitivity (lower drop better)
    if b_sens > a_sens * 1.25 + 20_000:
        score_a += 1
        reasons.append(f"B more delay-sensitive (drop {b_sens:,.0f} vs {a_sens:,.0f})")
    elif a_sens > b_sens * 1.25 + 20_000:
        score_b += 1
        reasons.append("A more delay-sensitive")

    # 7 early-capture value
    expl = b.get("explore") or {}
    early = expl.get("avg_minutes_earlier_than_A")
    succ = expl.get("explore_success_rate_pct")
    wrong_n = expl.get("wrong_explores_count") or 0
    if early and early > 0 and succ and succ >= 30 and wrong_n <= max(2, (expl.get("explore_starts") or 1) * 0.35):
        score_b += 1
        reasons.append(f"Early capture useful (~{early}m earlier, success {succ}%)")
    else:
        reasons.append(
            f"Early capture weak/noisy (earlier={early}, success={succ}, wrong={wrong_n})"
        )
        score_a += 0

    # Guardrail: do not pick Williams if Net up but MDD / costs meaningfully worse
    mdd_block = b_mdd > a_mdd * 1.25 + 0.2
    cost_block = b_cost > a_cost * 1.5 and b_rt > a_rt * 1.4
    if d_net > 0 and (mdd_block or cost_block):
        reasons.append("GUARDRAIL: B Net up but MDD/costs worse → do not pick WILLIAMS")
        score_b = min(score_b, score_a)

    # Thesis check: if explores never scale to MACD confirm, B's edge is "trade less" not early-capture
    succ = expl.get("explore_success_rate_pct")
    if succ is not None and succ <= 0 and d_net > 0 and score_b > score_a:
        reasons.append(
            "THESIS: explore→confirm success=0% — B mainly avoids A's overtrading, not validated early capture"
        )
        winner = "NO_CLEAR_WINNER"
        return {
            "verdict": winner,
            "score_A": score_a,
            "score_B": score_b,
            "reasons": reasons,
        }

    if score_b >= score_a + 2:
        winner = "WILLIAMS_EARLY_20"
    elif score_a >= score_b + 2:
        winner = "MACD_ONLY"
    else:
        winner = "NO_CLEAR_WINNER"

    return {
        "verdict": winner,
        "score_A": score_a,
        "score_B": score_b,
        "reasons": reasons,
    }


def write_md(report: dict) -> str:
    a = report["baseline"]["A"]
    b = report["baseline"]["B"]
    v = report["verdict"]
    lines = [
        "# MACD B vs Williams Early 20% — ≥20 trading days",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Dates ({len(report['dates'])}): `{', '.join(report['dates'])}`",
        f"- Date sources: `{json.dumps(report['date_sources'], ensure_ascii=False)}`",
        f"- Capital: {INITIAL_CASH:,.0f} | Fill baseline: next 1m open + 0.05% adverse + TradeCostEngine",
        f"- **Verdict: `{v['verdict']}`** (A={v['score_A']} B={v['score_B']})",
        "",
        "## Verdict reasons",
    ]
    for r in v["reasons"]:
        lines.append(f"- {r}")
    lines += [
        "",
        "## Baseline summary",
        "",
        "| Metric | A MACD_ONLY | B WILLIAMS_EARLY_20 |",
        "|---|---:|---:|",
        f"| Cum Net | {a['cum_net_pnl']:,.0f} | {b['cum_net_pnl']:,.0f} |",
        f"| Cum Ret% | {a['cum_ret_pct']} | {b['cum_ret_pct']} |",
        f"| Mean / Median daily ret% | {a['mean_daily_ret_pct']} / {a['median_daily_ret_pct']} | {b['mean_daily_ret_pct']} / {b['median_daily_ret_pct']} |",
        f"| Win/Loss days | {a['win_days']}/{a['loss_days']} | {b['win_days']}/{b['loss_days']} |",
        f"| RT / avg/day | {a['round_trips']} / {a['avg_trades_per_day']} | {b['round_trips']} / {b['avg_trades_per_day']} |",
        f"| WR% / PF / MDD% | {a['win_rate_pct']} / {a['pf']} / {a['mdd_pct']} | {b['win_rate_pct']} / {b['pf']} / {b['mdd_pct']} |",
        f"| Cost / cost÷gross% | {a['total_cost']:,.0f} / {a['cost_gross_ratio_pct']} | {b['total_cost']:,.0f} / {b['cost_gross_ratio_pct']} |",
        f"| Lev / Inv PnL | {a['lev_pnl']:,.0f} / {a['inv_pnl']:,.0f} | {b['lev_pnl']:,.0f} / {b['inv_pnl']:,.0f} |",
        f"| Worst day | {a['worst_day']} ({a['worst_day_pnl']:,.0f}) | {b['worst_day']} ({b['worst_day_pnl']:,.0f}) |",
        f"| Ex-best-trade Net | {a['ex_best_trade_net']:,.0f} | {b['ex_best_trade_net']:,.0f} |",
        f"| Signal→fill sec | {a['avg_signal_to_fill_sec']} | {b['avg_signal_to_fill_sec']} |",
        "",
        "### Exit counts",
        f"- A: `{a['exit_counts']}`",
        f"- B: `{b['exit_counts']}`",
        "",
        "### RANGE vs TREND",
        f"- Definition: {a['regime_definition']}",
        f"- A: `{a['range_vs_trend']}`",
        f"- B: `{b['range_vs_trend']}`",
        "",
        "### Strategy B explore",
        f"- `{b.get('explore')}`",
        "",
        "## Stress",
        "",
        "| Scenario | A Net | B Net |",
        "|---|---:|---:|",
    ]
    for label, _, _ in SCENARIOS:
        an = report["stress"]["A"][label]["cum_net_pnl"]
        bn = report["stress"]["B"][label]["cum_net_pnl"]
        lines.append(f"| {label} | {an:,.0f} | {bn:,.0f} |")
    lines += [
        "",
        "## Daily PnL (baseline)",
        "",
        "| Day | Src | A Net | B Net | A Ret% | B Ret% |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for day in report["dates"]:
        ad = next(x for x in a["daily_pnl"] if x["day"] == day)
        bd = next(x for x in b["daily_pnl"] if x["day"] == day)
        src = report["date_sources"].get(day, "?")
        lines.append(
            f"| {day} | {src} | {ad['net_pnl']:,.0f} | {bd['net_pnl']:,.0f} | {ad['ret_pct']} | {bd['ret_pct']} |"
        )
    lines += [
        "",
        "## Williams formula",
        "",
        f"- %R({WR_PERIOD}) = (HH-Close)/(HH-LL)*-100",
        f"- Signal = EMA({WR_SIGNAL_SPAN}) of %R; gap = %R − signal",
        "- PRE does **not** use absolute −20/−80 thresholds",
        "",
        "## Re-run",
        "```",
        "python scripts/compare_macd_vs_williams_early_20d.py",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--refetch-naver", action="store_true")
    args = ap.parse_args()

    STATE.mkdir(parents=True, exist_ok=True)
    print("=" * 72, flush=True)
    print("MACD B vs Williams Early 20% — multi-day read-only compare", flush=True)
    print("=" * 72, flush=True)

    dates, date_sources, day_data = build_day_universe(args.days, args.refetch_naver)
    print(f"Using {len(dates)} days: {dates}", flush=True)
    print(f"Sources: {date_sources}", flush=True)

    # Precompute features once per day
    features_by_day: dict[str, dict] = {}
    for day in dates:
        print(f"  features {day}…", flush=True)
        features_by_day[day] = _precompute_day_features(day_data[day][SIGNAL_SYMBOL])
        print(f"    PRE={len(features_by_day[day]['pre'])} MACD_ev={len(features_by_day[day]['macd_events'])}", flush=True)

    stress_aggs: dict[str, dict[str, dict]] = {"A": {}, "B": {}}
    baseline_day_rows: dict[str, list] = {"A": [], "B": []}

    for label, delay, adv in SCENARIOS:
        print(f"\n## Scenario {label} (delay={delay}m, adverse={adv}%)", flush=True)
        day_rows_a: list[dict] = []
        day_rows_b: list[dict] = []
        for day in dates:
            ra = replay_day_strategy(
                day, day_data[day], "A", delay_min=delay, adverse_pct=adv, features=features_by_day[day]
            )
            rb = replay_day_strategy(
                day, day_data[day], "B", delay_min=delay, adverse_pct=adv, features=features_by_day[day]
            )
            day_rows_a.append(ra)
            day_rows_b.append(rb)
            print(
                f"  {day}: A net={ra['net_pnl']:>10,.0f} RT={ra['round_trips']:<2} | "
                f"B net={rb['net_pnl']:>10,.0f} RT={rb['round_trips']:<2} "
                f"expl={rb['explore']['explore_starts'] if rb['explore'] else 0}",
                flush=True,
            )
            partial = {
                "dates": dates,
                "date_sources": date_sources,
                "scenario": label,
                "last_day": day,
                "A_net_so_far": sum(r["net_pnl"] for r in day_rows_a),
                "B_net_so_far": sum(r["net_pnl"] for r in day_rows_b),
            }
            PARTIAL.write_text(json.dumps(partial, ensure_ascii=False, indent=2), encoding="utf-8")

        agg_a = aggregate(day_rows_a, "A")
        agg_b = aggregate(day_rows_b, "B")
        # strip heavy trades from stored aggregates' daily copies already inside
        for agg in (agg_a, agg_b):
            pass
        stress_aggs["A"][label] = {k: v for k, v in agg_a.items() if k != "daily_pnl"} | {
            "daily_pnl": agg_a["daily_pnl"],
            "cum_net_pnl": agg_a["cum_net_pnl"],
        }
        stress_aggs["B"][label] = {k: v for k, v in agg_b.items() if k != "daily_pnl"} | {
            "daily_pnl": agg_b["daily_pnl"],
            "cum_net_pnl": agg_b["cum_net_pnl"],
        }
        if label == "baseline":
            baseline_day_rows["A"] = day_rows_a
            baseline_day_rows["B"] = day_rows_b
            # keep compact trade sample only
            stress_aggs["A"][label] = agg_a
            stress_aggs["B"][label] = agg_b

    verdict = decide_verdict(stress_aggs["A"]["baseline"], stress_aggs["B"]["baseline"], stress_aggs)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dates": dates,
        "date_sources": date_sources,
        "n_days": len(dates),
        "rules": {
            "capital": INITIAL_CASH,
            "entry_window": "09:00-14:55",
            "flatten": "15:00",
            "tp_sl_net_pct": [3.0, -1.5],
            "fill": "next 1m open after signal + adverse + TradeCostEngine",
            "no_same_dir_repeat": True,
            "no_continuation_reentry_after_tp": True,
            "williams": f"%R({WR_PERIOD}), signal=EMA({WR_SIGNAL_SPAN}), no abs -20/-80",
            "explore_pct": EXPLORE_PCT,
            "explore_confirm_sec": EXPLORE_CONFIRM_SEC,
        },
        "baseline": {
            "A": stress_aggs["A"]["baseline"],
            "B": stress_aggs["B"]["baseline"],
        },
        "stress": {
            "A": {k: {"cum_net_pnl": v["cum_net_pnl"], "pf": v.get("pf"), "mdd_pct": v.get("mdd_pct"), "round_trips": v.get("round_trips")} for k, v in stress_aggs["A"].items()},
            "B": {k: {"cum_net_pnl": v["cum_net_pnl"], "pf": v.get("pf"), "mdd_pct": v.get("mdd_pct"), "round_trips": v.get("round_trips")} for k, v in stress_aggs["B"].items()},
        },
        "verdict": verdict,
    }
    # Drop per-trade blobs from baseline to keep JSON manageable
    for side in ("A", "B"):
        for day_row in baseline_day_rows[side]:
            day_row.pop("trades", None)
        report["baseline"][side]["day_results_light"] = baseline_day_rows[side]

    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    OUT_MD.write_text(write_md(report), encoding="utf-8")
    print("\n" + "=" * 72)
    print(f"VERDICT: {verdict['verdict']}")
    for r in verdict["reasons"]:
        print(f"  - {r}")
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_MD}")


if __name__ == "__main__":
    main()
