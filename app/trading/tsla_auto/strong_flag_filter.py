"""TSLA_AUTO Hybrid strong-flag filter — pure functions only.

Evaluates already-confirmed MACD crossover flags for order authority when
``strong_filter_enabled`` is ON. Does not create flags, mutate input frames,
or call broker/order code. Uses only completed 3m TSLA bars up to the flag
bar (no future bars, no forming bar, no TSLL/TSLZ price, no live quote
injection — docs §4/§9 "강한 플래그 점수는 TSLA 완성 3분봉만으로 계산한다").

Structure mirrors app/trading/macd2/major_flag_filter.py (docs
TSLA_AUTO_COPY_MAP.md — COPY_WITH_US_MARKET_CHANGE), re-implemented here
independently (never imported from there). The two real differences:

1. ``_session_vwap`` uses America/New_York session boundaries (09:30 ET
   open) instead of MACD2's KST 09:00 — the only component whose formula
   itself had to change (component scoring/thresholds are otherwise
   identical to the values MACD2 actually runs, per docs §9).
2. NORMAL/CHOP regime classification + time-of-day threshold table
   (docs §10/§11) — entirely new, not present in MACD2 at all.
"""
from __future__ import annotations

import math
from datetime import datetime, time as dtime
from typing import Any, Optional, Sequence, Union

import pandas as pd

from app.trading.tsla_auto import config
from app.trading.tsla_auto.models import Direction, MarketRegime, StrongFlagDecision

_REQUIRED_COLS = ("datetime", "open", "high", "low", "close", "volume")
ET = config.ET


def _direction_sign(flag_direction: Union[Direction, str]) -> int:
    value = flag_direction.value if isinstance(flag_direction, Direction) else str(flag_direction)
    if value == Direction.UP_RED.value:
        return 1
    if value == Direction.DOWN_BLUE.value:
        return -1
    raise ValueError(f"unsupported flag_direction: {value!r}")


def _as_direction(value: Union[Direction, str, None]) -> Optional[Direction]:
    if value is None:
        return None
    if isinstance(value, Direction):
        return value if value in (Direction.UP_RED, Direction.DOWN_BLUE) else None
    text = str(value)
    if text in (Direction.UP_RED.value, Direction.DOWN_BLUE.value):
        return Direction(text)
    return None


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _copy_bars(bars_3m: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if bars_3m is None:
        return None
    if not isinstance(bars_3m, pd.DataFrame):
        raise TypeError("bars_3m must be a pandas DataFrame")
    return bars_3m.copy(deep=True)


def _prepare_bars(bars_3m: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    work = _copy_bars(bars_3m)
    if work is None or work.empty:
        return None
    missing = [c for c in _REQUIRED_COLS if c not in work.columns]
    if missing:
        return None
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    if work["datetime"].dt.tz is None:
        return None
    for col in ("open", "high", "low", "close", "volume"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["datetime", "open", "high", "low", "close"]).sort_values("datetime")
    work = work.drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)
    if len(work) < config.STRONG_MIN_COMPLETED_BARS:
        return None
    return work


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1,
    )
    return ranges.max(axis=1)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    tr = _true_range(high, low, close)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _session_vwap(work: pd.DataFrame) -> pd.Series:
    """Per-calendar-day America/New_York regular-session VWAP; never mixes
    prior-day volume/price (docs §강한 플래그 필터 "미국 세션 전용 재작성
    필요 항목"). Uses a generous [09:30, 16:30) ET daily window — wide enough
    to cover both a 16:00 regular close and a 13:00 early close without
    needing per-day early-close lookup here (the filter only needs "today's
    regular-session bars", not the exact close instant)."""
    dt = work["datetime"]
    day = dt.dt.tz_convert(ET).dt.strftime("%Y%m%d")
    minutes = dt.dt.tz_convert(ET).dt.hour * 60 + dt.dt.tz_convert(ET).dt.minute
    open_min = config.SESSION_OPEN.hour * 60 + config.SESSION_OPEN.minute
    close_min = config.REGULAR_CLOSE.hour * 60 + config.REGULAR_CLOSE.minute + 30
    typical = (work["high"] + work["low"] + work["close"]) / 3.0
    session = (minutes >= open_min) & (minutes <= close_min)
    pv = typical * work["volume"].where(session, 0.0)
    vol = work["volume"].where(session, 0.0)
    cum_pv = pv.groupby(day).cumsum()
    cum_vol = vol.groupby(day).cumsum()
    out = cum_pv / cum_vol.replace(0.0, float("nan"))
    return out.astype(float)


def _macd_lines(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = _ema(close, config.EMA_FAST)
    ema_slow = _ema(close, config.EMA_SLOW)
    macd = ema_fast - ema_slow
    signal = _ema(macd, config.EMA_SIGNAL)
    hist = macd - signal
    return macd, signal, hist


def _raw_confirmed_color_direction(hist_0: float, hist_1: float, hist_2: float) -> Optional[Direction]:
    if hist_0 < hist_1 and hist_1 < hist_2:
        return Direction.UP_RED
    if hist_0 > hist_1 and hist_1 > hist_2:
        return Direction.DOWN_BLUE
    return None


def _reject(
    *, decision: str, block_reason: str, reasons: Sequence[str], is_reversal: bool = False,
    fast_reversal: bool = False, score: float = 0.0, required_score: float = 0.0,
    component_scores: Optional[dict[str, float]] = None, metrics: Optional[dict[str, Any]] = None,
    regime: str = MarketRegime.UNKNOWN.value,
) -> StrongFlagDecision:
    return StrongFlagDecision(
        approved=False, score=float(score), required_score=float(required_score), decision=decision,
        reasons=tuple(reasons), component_scores=dict(component_scores or {}), metrics=dict(metrics or {}),
        is_reversal=is_reversal, fast_reversal=fast_reversal, regime=regime, block_reason=block_reason,
    )


def compute_component_scores(work: pd.DataFrame) -> tuple[Optional[dict[str, float]], Optional[dict[str, Any]], Optional[str]]:
    """Score components A-G for the last completed bar. Returns (scores, metrics, error)."""
    close = work["close"].astype(float)
    open_ = work["open"].astype(float)
    high = work["high"].astype(float)
    low = work["low"].astype(float)
    volume = work["volume"].astype(float)

    atr = _atr(high, low, close, config.STRONG_ATR_PERIOD)
    macd, signal, hist = _macd_lines(close)
    ema10 = _ema(close, config.STRONG_EMA_FAST)
    ema20 = _ema(close, config.STRONG_EMA_SLOW)
    vwap = _session_vwap(work)

    i = len(work) - 1
    lookback_break = config.STRONG_RANGE_BREAKOUT_LOOKBACK
    lookback_vol = config.STRONG_VOLUME_LOOKBACK
    lookback_range = config.STRONG_RECENT_RANGE_LOOKBACK
    if i < max(lookback_break + 1, lookback_vol + 1, lookback_range, 3, config.EMA_SLOW - 1):
        return None, None, config.FILTER_DATA_INSUFFICIENT

    atr14 = float(atr.iloc[i])
    prev_atr_window = atr.iloc[i - lookback_vol : i]
    if not _finite(atr14) or atr14 <= 0:
        return None, None, config.FILTER_DATA_INSUFFICIENT
    if prev_atr_window.isna().any() or not all(_finite(v) for v in prev_atr_window):
        return None, None, config.FILTER_DATA_INSUFFICIENT

    cur_hist = float(hist.iloc[i])
    prev_hist = float(hist.iloc[i - 1])
    if not _finite(cur_hist) or not _finite(prev_hist):
        return None, None, config.FILTER_DATA_INSUFFICIENT

    cur_close = float(close.iloc[i])
    cur_open = float(open_.iloc[i])
    close_3 = float(close.iloc[i - 3])
    if not all(_finite(v) for v in (cur_close, cur_open, close_3)) or cur_close <= 0:
        return None, None, config.FILTER_DATA_INSUFFICIENT

    vol_window = volume.iloc[i - lookback_vol : i]
    if vol_window.isna().any() or len(vol_window) < lookback_vol:
        return None, None, config.FILTER_DATA_INSUFFICIENT
    vol_median = float(vol_window.median())
    if not _finite(vol_median) or vol_median <= 0:
        return None, None, config.FILTER_DATA_INSUFFICIENT

    cur_vol = float(volume.iloc[i])
    if not _finite(cur_vol):
        return None, None, config.FILTER_DATA_INSUFFICIENT

    ema10_cur = float(ema10.iloc[i])
    ema10_prev = float(ema10.iloc[i - 1])
    ema20_cur = float(ema20.iloc[i])
    vwap_cur = float(vwap.iloc[i]) if _finite(vwap.iloc[i]) else float("nan")
    if not all(_finite(v) for v in (ema10_cur, ema10_prev, ema20_cur)):
        return None, None, config.FILTER_DATA_INSUFFICIENT

    prior_highs = high.iloc[i - lookback_break : i]
    prior_lows = low.iloc[i - lookback_break : i]
    breakout_up = cur_close > float(prior_highs.max())
    breakout_down = cur_close < float(prior_lows.min())

    recent = work.iloc[i - lookback_range + 1 : i + 1]
    recent_range = float(recent["high"].max() - recent["low"].min()) / cur_close
    atr_median_prev = float(prev_atr_window.median())
    ema_spread = abs(ema10_cur - ema20_cur) / cur_close

    metrics: dict[str, Any] = {
        "atr14": atr14, "macd": float(macd.iloc[i]), "signal": float(signal.iloc[i]),
        "hist": cur_hist, "prev_hist": prev_hist, "hist_impulse_atr": None,
        "breakout_up": bool(breakout_up), "breakout_down": bool(breakout_down), "breakout": False,
        "price_impulse_atr": None, "close_3_bars_ago": close_3, "body_atr": abs(cur_close - cur_open) / atr14,
        "volume_ratio": cur_vol / vol_median, "ema10": ema10_cur, "ema10_prev": ema10_prev, "ema20": ema20_cur,
        "vwap": vwap_cur if _finite(vwap_cur) else None, "ema10_ok": False, "ema20_or_vwap_ok": False,
        "recent_range_ratio": recent_range, "ema_spread_ratio": ema_spread, "atr_median_prev20": atr_median_prev,
        "close": cur_close, "open": cur_open, "volume": cur_vol, "volume_median_prev20": vol_median,
    }
    scores = {
        "hist_impulse": 0.0, "price_strength": 0.0, "body": 0.0, "volume": 0.0,
        "ema10_trend": 0.0, "ema20_or_vwap": 0.0, "volatility": 0.0,
    }
    return scores, metrics, None


def score_for_direction(
    scores_template: dict[str, float], metrics: dict[str, Any], flag_direction: Union[Direction, str],
) -> tuple[dict[str, float], dict[str, Any]]:
    direction = _as_direction(flag_direction)
    assert direction is not None
    sign = _direction_sign(direction)
    scores = dict(scores_template)
    m = dict(metrics)

    atr14 = float(m["atr14"])
    hist_impulse_atr = sign * (float(m["hist"]) - float(m["prev_hist"])) / atr14
    m["hist_impulse_atr"] = hist_impulse_atr
    if hist_impulse_atr >= config.STRONG_HIST_IMPULSE_T3:
        scores["hist_impulse"] = 25.0
    elif hist_impulse_atr >= config.STRONG_HIST_IMPULSE_T2:
        scores["hist_impulse"] = 18.0
    elif hist_impulse_atr >= config.STRONG_HIST_IMPULSE_T1:
        scores["hist_impulse"] = 10.0
    else:
        scores["hist_impulse"] = 0.0

    breakout = bool(m["breakout_up"] if direction == Direction.UP_RED else m["breakout_down"])
    price_impulse_atr = sign * (float(m["close"]) - float(m["close_3_bars_ago"])) / atr14
    m["breakout"] = breakout
    m["price_impulse_atr"] = price_impulse_atr
    if breakout:
        scores["price_strength"] = 25.0
    elif price_impulse_atr >= config.STRONG_PRICE_IMPULSE_T2:
        scores["price_strength"] = 25.0
    elif price_impulse_atr >= config.STRONG_PRICE_IMPULSE_T1:
        scores["price_strength"] = 15.0
    else:
        scores["price_strength"] = 0.0

    body_atr = float(m["body_atr"])
    body_dir_ok = (float(m["close"]) > float(m["open"])) if direction == Direction.UP_RED else (float(m["close"]) < float(m["open"]))
    if body_dir_ok and body_atr >= config.STRONG_BODY_ATR_T2:
        scores["body"] = 10.0
    elif body_dir_ok and body_atr >= config.STRONG_BODY_ATR_T1:
        scores["body"] = 5.0
    else:
        scores["body"] = 0.0

    vol_ratio = float(m["volume_ratio"])
    if vol_ratio >= config.STRONG_VOLUME_RATIO_T3:
        scores["volume"] = 15.0
    elif vol_ratio >= config.STRONG_VOLUME_RATIO_T2:
        scores["volume"] = 10.0
    elif vol_ratio >= config.STRONG_VOLUME_RATIO_T1:
        scores["volume"] = 5.0
    else:
        scores["volume"] = 0.0

    if direction == Direction.UP_RED:
        ema10_ok = float(m["ema10"]) > float(m["ema10_prev"]) and float(m["close"]) > float(m["ema10"])
        ema20_ok = float(m["close"]) > float(m["ema20"])
        vwap_ok = m.get("vwap") is not None and _finite(m["vwap"]) and float(m["close"]) > float(m["vwap"])
    else:
        ema10_ok = float(m["ema10"]) < float(m["ema10_prev"]) and float(m["close"]) < float(m["ema10"])
        ema20_ok = float(m["close"]) < float(m["ema20"])
        vwap_ok = m.get("vwap") is not None and _finite(m["vwap"]) and float(m["close"]) < float(m["vwap"])
    ema20_or_vwap_ok = bool(ema20_ok or vwap_ok)
    m["ema10_ok"] = bool(ema10_ok)
    m["ema20_ok"] = bool(ema20_ok)
    m["vwap_ok"] = bool(vwap_ok)
    m["ema20_or_vwap_ok"] = ema20_or_vwap_ok
    scores["ema10_trend"] = 10.0 if ema10_ok else 0.0
    scores["ema20_or_vwap"] = 10.0 if ema20_or_vwap_ok else 0.0

    vol_ok = (
        float(m["recent_range_ratio"]) >= config.STRONG_SIDEWAYS_RANGE_MAX
        or float(m["atr14"]) >= float(m["atr_median_prev20"])
    )
    scores["volatility"] = 5.0 if vol_ok else 0.0
    m["volatility_expansion"] = bool(vol_ok)
    return scores, m


def classify_regime(metrics: dict[str, Any]) -> str:
    """NORMAL/CHOP 1차 정의(docs §10): 변동성 확장(§변동성 컴포넌트와 동일 조건
    — 최근 8봉 range/close ≥ 문턱 또는 ATR14 ≥ 직전 20봉 중앙값) 이면 NORMAL,
    아니면 CHOP. 이미 계산된 실측 지표만 재사용하며 별도 값을 임의로 만들지
    않는다. 데이터 부족(``metrics`` 없음)이면 UNKNOWN."""
    if not metrics:
        return MarketRegime.UNKNOWN.value
    vol_ok = metrics.get("volatility_expansion")
    if vol_ok is None:
        try:
            vol_ok = (
                float(metrics["recent_range_ratio"]) >= config.STRONG_SIDEWAYS_RANGE_MAX
                or float(metrics["atr14"]) >= float(metrics["atr_median_prev20"])
            )
        except (KeyError, TypeError, ValueError):
            return MarketRegime.UNKNOWN.value
    return MarketRegime.NORMAL.value if vol_ok else MarketRegime.CHOP.value


# ── 시간대별 문턱표 (docs §11, 사용자 확정 사양 — 그대로 구현) ──────────────
_DEFAULT_THRESHOLDS = {
    MarketRegime.NORMAL.value: {"entry": 65.0, "reversal": 75.0, "fast_reversal": 82.0},
    MarketRegime.CHOP.value: {"entry": 65.0, "reversal": 75.0, "fast_reversal": 82.0},
}
_MIDDAY_RELAXED = {  # 12:00-14:00 ET
    MarketRegime.NORMAL.value: {"entry": 65.0, "reversal": 75.0, "fast_reversal": 82.0, "max_filled": 4},
    MarketRegime.CHOP.value: {"entry": 65.0, "reversal": 75.0, "fast_reversal": 82.0, "max_filled": 4},
}
_LATE_RELAXED = {  # 14:00-15:30 ET
    MarketRegime.NORMAL.value: {"entry": 65.0, "reversal": 75.0, "fast_reversal": 82.0, "max_filled": 4},
    MarketRegime.CHOP.value: {"entry": 65.0, "reversal": 75.0, "fast_reversal": 82.0, "max_filled": 4},
}
ABSOLUTE_FLOOR = {
    MarketRegime.NORMAL.value: {"entry": 65.0, "reversal": 75.0},
    MarketRegime.CHOP.value: {"entry": 65.0, "reversal": 75.0},
}
_MIDDAY_START, _MIDDAY_END = dtime(12, 0), dtime(14, 0)
_LATE_START, _LATE_END = dtime(14, 0), dtime(15, 30)


def required_scores_for(
    *, now_et: datetime, regime: str, daily_filled_entry_count: int,
) -> dict[str, float]:
    """entry/reversal/fast_reversal 문턱값 계산 — 시간대별 완화는 그 시간대의
    ``max_filled`` 조건을 만족할 때만 적용하고, 그 외에는 기본 문턱을 쓴다.
    15:30~15:45는 명시적으로 기본 문턱으로 복귀한다(마감 직전 완화 금지).
    절대 하한(entry/reversal)은 어떤 경우에도 하회하지 않는다."""
    regime_key = regime if regime in _DEFAULT_THRESHOLDS else MarketRegime.NORMAL.value
    table = dict(_DEFAULT_THRESHOLDS[regime_key])
    t = now_et.time()
    if _MIDDAY_START <= t < _MIDDAY_END:
        relaxed = _MIDDAY_RELAXED[regime_key]
        if daily_filled_entry_count <= relaxed["max_filled"]:
            table = {"entry": relaxed["entry"], "reversal": relaxed["reversal"], "fast_reversal": relaxed["fast_reversal"]}
    elif _LATE_START <= t < _LATE_END:
        relaxed = _LATE_RELAXED[regime_key]
        if daily_filled_entry_count <= relaxed["max_filled"]:
            table = {"entry": relaxed["entry"], "reversal": relaxed["reversal"], "fast_reversal": relaxed["fast_reversal"]}
    # else: 09:30-12:00, 15:30-15:45(+15:45 이후는 어차피 신규 BUY 금지) -> 기본 문턱 유지

    floor = ABSOLUTE_FLOOR[regime_key]
    table["entry"] = max(table["entry"], floor["entry"])
    table["reversal"] = max(table["reversal"], floor["reversal"])
    return table


def _v6_profile_ok(
    *,
    direction: Direction,
    score: float,
    metrics: dict[str, Any],
    now: datetime,
) -> tuple[bool, str]:
    """MACD2 V6 profile gate, shifted to the US regular-session clock.

    MACD2's V6 profiles are defined from a 09:00 local open. TSLA_AUTO uses a
    09:30 ET open, so the same intraday windows are shifted by +30 minutes.
    """
    t = now.astimezone(ET).time()
    price_impulse = float(metrics.get("price_impulse_atr") or 0.0)
    hist_impulse = float(metrics.get("hist_impulse_atr") or 0.0)
    body_atr = float(metrics.get("body_atr") or 0.0)
    volume_ratio = float(metrics.get("volume_ratio") or 0.0)
    trend_ok = bool(metrics.get("ema20_or_vwap_ok"))

    t_0930 = dtime(9, 30)
    t_0935 = dtime(9, 35)
    t_0940 = dtime(9, 40)
    t_0950 = dtime(9, 50)
    t_1000 = dtime(10, 0)
    t_1030 = dtime(10, 30)
    t_1045 = dtime(10, 45)
    t_1100 = dtime(11, 0)
    t_1115 = dtime(11, 15)
    t_1215 = dtime(12, 15)
    t_1230 = dtime(12, 30)
    t_1300 = dtime(13, 0)
    t_1320 = dtime(13, 20)
    t_1330 = dtime(13, 30)
    t_1345 = dtime(13, 45)
    t_1400 = dtime(14, 0)
    t_1450 = dtime(14, 50)
    t_1500 = dtime(15, 0)
    t_1515 = dtime(15, 15)

    if (
        t_0930 <= t <= t_1000
        and score >= 60.0
        and price_impulse >= 1.00
        and hist_impulse >= 0.08
        and volume_ratio >= 0.85
        and trend_ok
    ):
        if direction == Direction.UP_RED and t_0940 <= t <= t_0950 and volume_ratio >= 2.0:
            return False, "V6 opening red spike blocked"
        return True, "V6 opening impulse"

    if (
        direction == Direction.DOWN_BLUE
        and t_0940 <= t <= t_0950
        and 35.0 <= score <= 60.0
        and 0.45 <= price_impulse <= 0.65
        and 0.06 <= hist_impulse <= 0.16
        and 0.85 <= volume_ratio <= 1.20
        and trend_ok
    ):
        return True, "V6 opening blue soft trend"

    if (
        direction == Direction.UP_RED
        and t_0935 <= t <= t_0950
        and 35.0 <= score <= 70.0
        and 0.45 <= price_impulse <= 2.30
        and hist_impulse >= 0.12
        and 0.75 <= volume_ratio <= 1.05
        and trend_ok
    ):
        return True, "V6 opening red hist reversal"

    if (
        direction == Direction.UP_RED
        and t_1000 <= t <= t_1045
        and score >= 45.0
        and price_impulse >= 1.30
        and hist_impulse >= 0.07
    ):
        return True, "V6 morning red recovery"

    if (
        direction == Direction.DOWN_BLUE
        and t_1100 <= t <= t_1300
        and 30.0 <= score
        and 0.65 <= price_impulse
        and hist_impulse >= 0.04
        and 0.45 <= volume_ratio
    ):
        return True, "V6 morning blue follow"

    if (
        direction == Direction.DOWN_BLUE
        and t_1030 <= t <= t_1115
        and score >= 50.0
        and 0.80 <= price_impulse <= 2.20
        and 0.005 <= hist_impulse <= 0.03
        and 0.90 <= volume_ratio <= 1.20
        and trend_ok
    ):
        return True, "V6 morning blue pullback"

    if (
        direction == Direction.DOWN_BLUE
        and t_1320 <= t <= t_1345
        and 45.0 <= score <= 55.0
        and 0.55 <= price_impulse <= 0.70
        and 0.06 <= hist_impulse <= 0.08
        and body_atr >= 0.50
        and 1.00 <= volume_ratio <= 1.20
        and not trend_ok
    ):
        return True, "V6 early afternoon blue reversal"

    if (
        t_1300 <= t <= t_1500
        and score >= 70.0
        and price_impulse >= 1.00
        and hist_impulse >= 0.06
        and volume_ratio >= 1.00
        and trend_ok
    ):
        return True, "V6 trend continuation"

    if (
        direction == Direction.UP_RED
        and t_1400 <= t <= t_1450
        and score <= 35.0
        and price_impulse <= 0.85
        and 0.00 <= hist_impulse <= 0.05
        and 0.70 <= volume_ratio <= 1.20
    ):
        return True, "V6 late red rebound"

    if (
        direction == Direction.UP_RED
        and t_1215 <= t <= t_1230
        and score <= 20.0
        and -2.10 <= price_impulse <= -0.20
        and 0.00 <= hist_impulse <= 0.09
        and body_atr >= 0.65
        and 0.65 <= volume_ratio <= 0.80
    ):
        return True, "V6 midday red contrarian"

    if (
        direction == Direction.DOWN_BLUE
        and t_1400 <= t <= t_1515
        and score >= 60.0
        and price_impulse >= 1.25
        and hist_impulse >= 0.06
        and volume_ratio >= 1.00
    ):
        return True, "V6 late blue capitulation"

    if (
        direction == Direction.DOWN_BLUE
        and t_1330 <= t <= dtime(14, 30)
        and score <= 30.0
        and 0.10 <= price_impulse <= 0.45
        and 0.04 <= hist_impulse <= 0.09
        and 0.60 <= volume_ratio <= 0.95
        and trend_ok
    ):
        return True, "V6 afternoon blue reversal"

    if (
        direction == Direction.UP_RED
        and dtime(11, 30) <= t <= t_1400
        and score >= 55.0
        and price_impulse >= 0.90
        and hist_impulse >= 0.03
        and 0.55 <= volume_ratio
    ):
        if t <= dtime(12, 0) and price_impulse >= 2.20 and not trend_ok:
            return False, "V6 red overextended blocked"
        return True, "V6 midday red continuation"

    if (
        direction == Direction.UP_RED
        and t_1230 <= t <= t_1400
        and 45.0 <= score <= 65.0
        and 0.70 <= price_impulse <= 1.70
        and 0.025 <= hist_impulse <= 0.08
        and 0.75 <= volume_ratio <= 1.15
        and trend_ok
    ):
        return True, "V6 moderate red trend"

    return False, "no V6 frequency-profit profile matched"


def evaluate_strong_flag(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    position_direction: Union[Direction, str, None],
    last_entry_at: Optional[datetime],
    daily_entry_count: int,
    now: datetime,
) -> StrongFlagDecision:
    """Score + approve a confirmed crossover flag. Pure: same inputs -> same output."""
    direction = _as_direction(flag_direction)
    if direction is None:
        return _reject(
            decision=config.FILTER_INPUT_NOT_CROSSOVER, block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=["flag_direction must be UP_RED or DOWN_BLUE"],
        )

    pos_dir = _as_direction(position_direction)
    is_reversal = pos_dir is not None and pos_dir != direction
    fast_reversal = False
    if is_reversal and last_entry_at is not None:
        delta_min = (now - last_entry_at).total_seconds() / 60.0
        if delta_min <= float(config.STRONG_FAST_REVERSAL_WINDOW_MIN):
            fast_reversal = True

    work = _prepare_bars(bars_3m)
    if work is None:
        return _reject(
            decision=config.FILTER_DATA_INSUFFICIENT, block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=["insufficient or invalid completed 3m bars"], is_reversal=is_reversal, fast_reversal=fast_reversal,
        )

    _macd, _signal, hist = _macd_lines(work["close"].astype(float))
    prev2_hist = float(hist.iloc[-3])
    prev_hist = float(hist.iloc[-2])
    curr_hist = float(hist.iloc[-1])
    if not _finite(prev2_hist) or not _finite(prev_hist) or not _finite(curr_hist):
        return _reject(
            decision=config.FILTER_DATA_INSUFFICIENT, block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=["MACD histogram NaN"], is_reversal=is_reversal, fast_reversal=fast_reversal,
        )
    raw = _raw_confirmed_color_direction(prev2_hist, prev_hist, curr_hist)
    if raw is None:
        return _reject(
            decision=config.FILTER_INPUT_NOT_CROSSOVER, block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=[f"last three bars are not a confirmed color flag for {direction.value}"],
            is_reversal=is_reversal, fast_reversal=fast_reversal,
            metrics={"prev2_hist": prev2_hist, "prev_hist": prev_hist, "hist": curr_hist},
        )

    scores_t, metrics_t, err = compute_component_scores(work)
    if err or scores_t is None or metrics_t is None:
        return _reject(
            decision=config.FILTER_DATA_INSUFFICIENT, block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=[err or config.FILTER_DATA_INSUFFICIENT], is_reversal=is_reversal, fast_reversal=fast_reversal,
        )

    scores, metrics = score_for_direction(scores_t, metrics_t, direction)
    metrics["raw_color_direction"] = raw.value
    regime = classify_regime(metrics)
    thresholds = required_scores_for(now_et=now.astimezone(ET), regime=regime, daily_filled_entry_count=int(daily_entry_count))
    if fast_reversal:
        required_score = thresholds["fast_reversal"]
    elif is_reversal:
        required_score = thresholds["reversal"]
    else:
        required_score = thresholds["entry"]

    total = float(sum(scores.values()))
    reasons: list[str] = []
    if raw != direction:
        return _reject(
            decision=config.FILTER_INPUT_NOT_CROSSOVER, block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=[f"raw color {raw.value} does not match {direction.value}"],
            is_reversal=is_reversal, fast_reversal=fast_reversal, score=total,
            required_score=required_score, component_scores=scores, metrics=metrics, regime=regime,
        )

    if (
        float(metrics["ema_spread_ratio"]) < config.STRONG_SIDEWAYS_EMA_SPREAD_MAX
        and float(metrics["recent_range_ratio"]) < config.STRONG_SIDEWAYS_RANGE_MAX
    ):
        reasons.append("sideways ema_spread and recent_range both tight")
        return _reject(
            decision=config.STRONG_SIDEWAYS_BLOCK, block_reason=config.STRONG_SIDEWAYS_BLOCK, reasons=reasons,
            is_reversal=is_reversal, fast_reversal=fast_reversal, score=total, required_score=required_score,
            component_scores=scores, metrics=metrics, regime=regime,
        )

    price_confirm_ok = (
        bool(metrics.get("breakout"))
        or float(metrics.get("price_impulse_atr") or 0.0) >= float(config.STRONG_PRICE_IMPULSE_T1)
        or bool(metrics.get("ema20_ok"))
        or bool(metrics.get("vwap_ok"))
    )
    profile_ok, profile_reason = _v6_profile_ok(
        direction=direction, score=total, metrics=metrics, now=now,
    )
    metrics["strong_profile_reason"] = profile_reason
    if not price_confirm_ok and not profile_ok:
        reasons.append("price confirmation failed (breakout / impulse>=0.35ATR / EMA20 / VWAP)")
        return _reject(
            decision=config.STRONG_PRICE_CONFIRMATION_FAILED, block_reason=config.STRONG_PRICE_CONFIRMATION_FAILED,
            reasons=reasons, is_reversal=is_reversal, fast_reversal=fast_reversal, score=total,
            required_score=required_score, component_scores=scores, metrics=metrics, regime=regime,
        )

    if not profile_ok:
        reasons.append(profile_reason)
        if total < required_score:
            return _reject(
                decision=config.STRONG_SCORE_BELOW_THRESHOLD, block_reason=config.STRONG_SCORE_BELOW_THRESHOLD,
                reasons=[f"score {total:.0f} < required {required_score:.0f} (regime={regime})", profile_reason],
                is_reversal=is_reversal, fast_reversal=fast_reversal, score=total,
                required_score=required_score, component_scores=scores, metrics=metrics, regime=regime,
            )
        return _reject(
            decision=config.STRONG_PROFILE_FAILED, block_reason=config.STRONG_PROFILE_FAILED,
            reasons=reasons, is_reversal=is_reversal, fast_reversal=fast_reversal, score=total,
            required_score=required_score, component_scores=scores, metrics=metrics, regime=regime,
        )

    metrics["daily_entry_count"] = int(daily_entry_count)
    reasons.append(profile_reason)
    return StrongFlagDecision(
        approved=True, score=total, required_score=required_score, decision=config.STRONG_APPROVED,
        reasons=tuple(reasons), component_scores=scores, metrics=metrics, is_reversal=is_reversal,
        fast_reversal=fast_reversal, regime=regime, block_reason=None,
    )


def apply_trade_gates(
    decision: StrongFlagDecision,
    *,
    flag_direction: Union[Direction, str],
    position_direction: Union[Direction, str, None],
    last_entry_at: Optional[datetime],
    last_same_direction_exit_at: Optional[datetime],
    daily_entry_count: int,
    now: datetime,
    daily_max_entries: int,
) -> StrongFlagDecision:
    """Post-score trade gates (same-dir, daily limit(NORMAL=4/CHOP=2), cooldown, min-hold)."""
    direction = _as_direction(flag_direction)
    assert direction is not None
    pos_dir = _as_direction(position_direction)

    if pos_dir is not None and pos_dir == direction:
        return _reject(
            decision=config.SAME_DIRECTION_POSITION_HELD, block_reason=config.SAME_DIRECTION_POSITION_HELD,
            reasons=["same-direction position already held - no add"], is_reversal=False, fast_reversal=False,
            score=decision.score, required_score=decision.required_score, component_scores=decision.component_scores,
            metrics=decision.metrics, regime=decision.regime,
        )

    if not decision.approved:
        return decision

    if int(daily_entry_count) >= int(daily_max_entries):
        return _reject(
            decision="DAILY_ENTRY_LIMIT", block_reason="DAILY_ENTRY_LIMIT",
            reasons=[f"daily entries reached {daily_max_entries} (regime={decision.regime})"],
            is_reversal=decision.is_reversal, fast_reversal=decision.fast_reversal, score=decision.score,
            required_score=decision.required_score, component_scores=decision.component_scores,
            metrics=decision.metrics, regime=decision.regime,
        )

    if pos_dir is None and last_same_direction_exit_at is not None:
        mins = (now - last_same_direction_exit_at).total_seconds() / 60.0
        if mins < float(config.STRONG_SAME_DIRECTION_REENTRY_MIN):
            return _reject(
                decision=config.STRONG_SAME_DIRECTION_COOLDOWN, block_reason=config.STRONG_SAME_DIRECTION_COOLDOWN,
                reasons=[f"same-direction reentry cooldown {mins:.1f}m < {config.STRONG_SAME_DIRECTION_REENTRY_MIN}m"],
                is_reversal=decision.is_reversal, fast_reversal=decision.fast_reversal, score=decision.score,
                required_score=decision.required_score, component_scores=decision.component_scores,
                metrics=decision.metrics, regime=decision.regime,
            )

    if decision.is_reversal and last_entry_at is not None:
        hold_min = (now - last_entry_at).total_seconds() / 60.0
        fast_floor = required_scores_for(
            now_et=now.astimezone(ET), regime=decision.regime, daily_filled_entry_count=int(daily_entry_count),
        )["fast_reversal"]
        if hold_min < float(config.STRONG_MIN_HOLD_MIN) and decision.score < fast_floor:
            return _reject(
                decision=config.STRONG_MIN_HOLD_BLOCK, block_reason=config.STRONG_MIN_HOLD_BLOCK,
                reasons=[f"min hold {hold_min:.1f}m < {config.STRONG_MIN_HOLD_MIN}m and score {decision.score:.0f} < {fast_floor:.0f}"],
                is_reversal=decision.is_reversal, fast_reversal=decision.fast_reversal, score=decision.score,
                required_score=decision.required_score, component_scores=decision.component_scores,
                metrics=decision.metrics, regime=decision.regime,
            )

    return decision


def daily_max_entries_for(regime: str) -> int:
    """NORMAL 최대 4회 / CHOP 최대 2회(docs §10) — 목표이지 보장이 아니다."""
    if regime == MarketRegime.CHOP.value:
        return config.CHOP_MAX_ENTRIES
    return config.NORMAL_MAX_ENTRIES
