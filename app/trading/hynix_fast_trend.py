"""Fast live Hynix trend helpers for Enhanced regime switching.

All scores and directions in this module use the same polarity as the
Enhanced strategy: UP/HYNIX means 000660 strength, DOWN/INVERSE means
0197X0 strength.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.utils.time_utils import kst_now


def _float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _close_series(df_1min) -> list[float]:
    if df_1min is None or getattr(df_1min, "empty", True):
        return []
    for col in ("close", "stck_prpr", "price", "현재가"):
        if col in df_1min.columns:
            vals = [_float(v) for v in df_1min[col].tolist()]
            return [v for v in vals if v is not None and v > 0]
    return []


def _volume_series(df_1min) -> list[float]:
    if df_1min is None or getattr(df_1min, "empty", True):
        return []
    for col in ("volume", "cntg_vol", "거래량"):
        if col in df_1min.columns:
            vals = [_float(v) for v in df_1min[col].tolist()]
            return [v for v in vals if v is not None and v >= 0]
    return []


def compute_fast_trend_signal(df_1min, now: Optional[datetime] = None) -> dict:
    """Return a compact 1/3/5 minute live trend signal.

    The function is intentionally deterministic and side-effect free so both
    the 30s watcher and tests can use the same logic.
    """
    now = now or kst_now()
    closes = _close_series(df_1min)
    volumes = _volume_series(df_1min)
    if len(closes) < 6:
        return {
            "direction": "FLAT",
            "confirmable": False,
            "reason": "not enough 1m bars",
            "computed_at": now.isoformat(timespec="seconds"),
            "returns": {"1m": None, "3m": None, "5m": None},
            "above_vwap": False,
            "ema_slope_pct": None,
            "higher_high_low": False,
            "lower_high_low": False,
            "volume_ratio": None,
            "top_factors": ["not enough 1m bars"],
        }

    def ret(minutes: int) -> float:
        base = closes[-(minutes + 1)]
        return (closes[-1] / base - 1.0) * 100.0 if base else 0.0

    ret_1m = ret(1)
    ret_3m = ret(3)
    ret_5m = ret(5)
    recent = closes[-6:]
    prev = closes[-11:-5] if len(closes) >= 11 else closes[:-5]
    recent_high, recent_low = max(recent), min(recent)
    prev_high = max(prev) if prev else recent_high
    prev_low = min(prev) if prev else recent_low
    higher_high_low = recent_high >= prev_high and recent_low >= prev_low
    lower_high_low = recent_high <= prev_high and recent_low <= prev_low

    ema_short = sum(closes[-3:]) / 3.0
    ema_long = sum(closes[-6:]) / 6.0
    ema_slope_pct = (ema_short / ema_long - 1.0) * 100.0 if ema_long else 0.0

    if volumes and len(volumes) >= 6:
        vol_recent = sum(volumes[-3:]) / 3.0
        vol_base = sum(volumes[-10:]) / min(10, len(volumes))
        volume_ratio = vol_recent / vol_base if vol_base else None
    else:
        volume_ratio = None

    if volumes and len(volumes) >= len(closes):
        window_closes = closes[-min(10, len(closes)):]
        window_vols = volumes[-len(window_closes):]
        denom = sum(window_vols)
        vwap = sum(p * v for p, v in zip(window_closes, window_vols)) / denom if denom else sum(window_closes) / len(window_closes)
    else:
        window_closes = closes[-min(10, len(closes)):]
        vwap = sum(window_closes) / len(window_closes)
    above_vwap = closes[-1] >= vwap

    up_votes = 0
    down_votes = 0
    factors: list[str] = []
    if ret_1m > 0:
        up_votes += 1
        factors.append(f"1m up {ret_1m:.2f}%")
    elif ret_1m < 0:
        down_votes += 1
        factors.append(f"1m down {ret_1m:.2f}%")
    if ret_3m > 0:
        up_votes += 1
        factors.append(f"3m up {ret_3m:.2f}%")
    elif ret_3m < 0:
        down_votes += 1
        factors.append(f"3m down {ret_3m:.2f}%")
    if ret_5m > 0:
        up_votes += 1
        factors.append(f"5m up {ret_5m:.2f}%")
    elif ret_5m < 0:
        down_votes += 1
        factors.append(f"5m down {ret_5m:.2f}%")
    if above_vwap:
        up_votes += 1
        factors.append("above VWAP")
    else:
        down_votes += 1
        factors.append("below VWAP")
    if ema_slope_pct > 0:
        up_votes += 1
        factors.append(f"EMA rising {ema_slope_pct:.2f}%")
    elif ema_slope_pct < 0:
        down_votes += 1
        factors.append(f"EMA falling {ema_slope_pct:.2f}%")
    if higher_high_low:
        up_votes += 1
        factors.append("higher high/low")
    if lower_high_low:
        down_votes += 1
        factors.append("lower high/low")

    if up_votes >= 4 and up_votes > down_votes:
        direction = "UP"
    elif down_votes >= 4 and down_votes > up_votes:
        direction = "DOWN"
    else:
        direction = "FLAT"

    return {
        "direction": direction,
        "confirmable": direction in ("UP", "DOWN"),
        "computed_at": now.isoformat(timespec="seconds"),
        "returns": {"1m": round(ret_1m, 4), "3m": round(ret_3m, 4), "5m": round(ret_5m, 4)},
        "above_vwap": above_vwap,
        "vwap": round(vwap, 4),
        "ema_slope_pct": round(ema_slope_pct, 4),
        "higher_high_low": higher_high_low,
        "lower_high_low": lower_high_low,
        "volume_ratio": None if volume_ratio is None else round(volume_ratio, 4),
        "up_votes": up_votes,
        "down_votes": down_votes,
        "top_factors": factors[:6],
    }


def is_live_hynix_uptrend(signal: Optional[dict]) -> bool:
    signal = signal or {}
    returns = signal.get("returns") or {}
    return bool(
        signal.get("above_vwap")
        and (returns.get("3m") or 0) > 0
        and (returns.get("5m") or 0) > 0
        and signal.get("ema_slope_pct", 0) >= 0
    )
