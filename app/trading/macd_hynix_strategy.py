"""Isolated Strategy B: completed 3-minute MACD histogram direction for 000660.

Does not call Enhanced / WOC / Early / Active / Fusion / Regime / Prediction.
Orders are never placed from this module — direction only.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

SIGNAL_SYMBOL = "000660"
LONG_SYMBOL = "0193T0"  # KODEX SK하이닉스 레버리지
INVERSE_SYMBOL = "0197X0"  # SOL SK하이닉스 인버스2X
LONG_NAME = "KODEX SK하이닉스단일종목레버리지"
INVERSE_NAME = "SOL SK하이닉스선물단일종목인버스2X"
SIGNAL_NAME = "SK하이닉스"

TRADE_SYMBOLS = (LONG_SYMBOL, INVERSE_SYMBOL)
SYMBOL_NAME = {
    SIGNAL_SYMBOL: SIGNAL_NAME,
    LONG_SYMBOL: LONG_NAME,
    INVERSE_SYMBOL: INVERSE_NAME,
}

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

DIR_UP = "UP_RED"
DIR_DOWN = "DOWN_BLUE"
DIR_HOLD = "HOLD"


def resample_completed_3m(
    df_1m: Optional[pd.DataFrame],
    now: Optional[datetime] = None,
) -> pd.DataFrame:
    """1m bars → completed 3m bars only. Incomplete current 3m window is excluded."""
    if df_1m is None or getattr(df_1m, "empty", True):
        return pd.DataFrame()
    work = df_1m.copy()
    if "datetime" not in work.columns:
        return pd.DataFrame()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    work = work.dropna(subset=["datetime"]).sort_values("datetime")
    if work.empty:
        return pd.DataFrame()
    indexed = work.set_index("datetime")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in indexed.columns:
        agg["volume"] = "sum"
    bars = indexed.resample("3min").agg(agg).dropna(subset=["close"]).reset_index()
    if bars.empty:
        return bars
    if now is None:
        return bars
    cutoff = now.replace(second=0, microsecond=0) if hasattr(now, "replace") else now
    return bars[bars["datetime"] + timedelta(minutes=3) <= cutoff].reset_index(drop=True)


def macd_components(closes: pd.Series) -> dict[str, Optional[pd.Series]]:
    """Return full MACD / Signal / Histogram series (or empty if insufficient)."""
    closes = pd.to_numeric(closes, errors="coerce").dropna()
    if len(closes) < MACD_SLOW:
        return {"macd": None, "signal": None, "hist": None}
    ema12 = closes.ewm(span=MACD_FAST, adjust=False).mean()
    ema26 = closes.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    hist = macd - signal
    return {"macd": macd, "signal": signal, "hist": hist}


def _pattern_direction(h1: float, h2: float, h3: float) -> str:
    """Classify the last three completed histogram values (newest=h1)."""
    d1 = h1 - h2
    d2 = h2 - h3
    if h1 > h2 > h3 and d1 > 0 and d2 > 0:
        return DIR_UP
    if h1 < h2 < h3 and d1 < 0 and d2 < 0:
        return DIR_DOWN
    return DIR_HOLD


def evaluate_macd_direction(
    df_1m: Optional[pd.DataFrame],
    *,
    now: Optional[datetime] = None,
    last_signal_direction: Optional[str] = None,
    last_signal_bar_ts: Optional[str] = None,
) -> dict[str, Any]:
    """Evaluate Strategy B on completed 3m MACD histogram of 000660.

    Returns light color (display_direction) and whether a *new* armed signal fired.
    New signals only on first turn (DOWN→UP or UP→DOWN) for a newly completed 3m bar.
    """
    empty = {
        "ok": False,
        "display_direction": DIR_HOLD,
        "new_signal": False,
        "signal_direction": None,
        "macd": None,
        "signal": None,
        "hist": None,
        "hist_last3": [],
        "hist_deltas": [],
        "completed_3m_count": 0,
        "bar_ts": None,
        "bar_close_ts": None,
        "reason": "DATA_INSUFFICIENT",
        "signal_id": None,
    }
    bars = resample_completed_3m(df_1m, now=now)
    if len(bars) < MACD_SLOW:
        return {**empty, "completed_3m_count": int(len(bars))}

    closes = pd.to_numeric(bars["close"], errors="coerce").dropna()
    comps = macd_components(closes)
    if comps["hist"] is None or len(comps["hist"]) < 3:
        return {**empty, "completed_3m_count": int(len(bars)), "reason": "MACD_INSUFFICIENT"}

    hist = comps["hist"]
    macd = comps["macd"]
    signal = comps["signal"]
    h1, h2, h3 = float(hist.iloc[-1]), float(hist.iloc[-2]), float(hist.iloc[-3])
    pattern = _pattern_direction(h1, h2, h3)
    bar_ts = bars["datetime"].iloc[-1]
    bar_ts_iso = pd.Timestamp(bar_ts).isoformat()
    bar_close_ts = pd.Timestamp(bar_ts) + timedelta(minutes=3)

    last_dir = str(last_signal_direction or "").upper() or None
    if last_dir in ("UP", "DOWN"):
        last_dir = DIR_UP if last_dir == "UP" else DIR_DOWN
    if last_dir not in (DIR_UP, DIR_DOWN, DIR_HOLD, None):
        last_dir = None

    new_signal = False
    signal_direction = None
    reason = "HOLD"
    if pattern == DIR_UP:
        reason = "UP_RED_PATTERN"
        if last_dir != DIR_UP and bar_ts_iso != str(last_signal_bar_ts or ""):
            new_signal = True
            signal_direction = DIR_UP
            reason = "UP_RED_FIRST_TURN"
    elif pattern == DIR_DOWN:
        reason = "DOWN_BLUE_PATTERN"
        if last_dir != DIR_DOWN and bar_ts_iso != str(last_signal_bar_ts or ""):
            new_signal = True
            signal_direction = DIR_DOWN
            reason = "DOWN_BLUE_FIRST_TURN"
    else:
        reason = "HOLD_NO_PATTERN"

    signal_id = None
    if new_signal and signal_direction:
        signal_id = f"MACD3M:{signal_direction}:{bar_ts_iso}"

    return {
        "ok": True,
        "display_direction": pattern,
        "new_signal": bool(new_signal),
        "signal_direction": signal_direction,
        "macd": round(float(macd.iloc[-1]), 6),
        "signal": round(float(signal.iloc[-1]), 6),
        "hist": round(h1, 6),
        "hist_last3": [round(h3, 6), round(h2, 6), round(h1, 6)],
        "hist_deltas": [round(h2 - h3, 6), round(h1 - h2, 6)],
        "completed_3m_count": int(len(bars)),
        "bar_ts": bar_ts_iso,
        "bar_close_ts": bar_close_ts.isoformat(),
        "reason": reason,
        "signal_id": signal_id,
    }


def target_symbol_for_direction(direction: Optional[str]) -> Optional[str]:
    text = str(direction or "").upper()
    if text in (DIR_UP, "UP", "UP_RED", LONG_SYMBOL):
        return LONG_SYMBOL
    if text in (DIR_DOWN, "DOWN", "DOWN_BLUE", INVERSE_SYMBOL):
        return INVERSE_SYMBOL
    return None


def opposite_symbol(symbol: Optional[str]) -> Optional[str]:
    if symbol == LONG_SYMBOL:
        return INVERSE_SYMBOL
    if symbol == INVERSE_SYMBOL:
        return LONG_SYMBOL
    return None
