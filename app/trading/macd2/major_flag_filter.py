"""Optional Hybrid MAJOR_FLAG filter — pure functions only.

Evaluates already-confirmed MACD crossover flags for order authority when
``major_filter_enabled`` is ON. Does not create flags, mutate input frames,
or call broker/order code. Uses only completed 3m bars up to the flag bar
(no future bars, no forming bar, no live quote injection).
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional, Sequence, Union

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2.models import Direction, MajorFlagDecision

_REQUIRED_COLS = ("datetime", "open", "high", "low", "close", "volume")


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
    # Never mutate caller's frame.
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
    if len(work) < config.MAJOR_MIN_COMPLETED_BARS:
        return None
    return work


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    tr = _true_range(high, low, close)
    # Wilder-style smoothing via ewm alpha=1/period (equivalent RMA).
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _session_vwap(work: pd.DataFrame) -> pd.Series:
    """Per-calendar-day regular-session VWAP; never mixes prior-day volume/price."""
    dt = work["datetime"]
    day = dt.dt.tz_convert(config.KST).dt.strftime("%Y%m%d")
    minutes = dt.dt.tz_convert(config.KST).dt.hour * 60 + dt.dt.tz_convert(config.KST).dt.minute
    open_min = config.SESSION_OPEN.hour * 60 + config.SESSION_OPEN.minute
    close_min = config.FORCE_LIQUIDATE_AT.hour * 60 + config.FORCE_LIQUIDATE_AT.minute + 30
    typical = (work["high"] + work["low"] + work["close"]) / 3.0
    session = (minutes >= open_min) & (minutes <= close_min)
    pv = typical * work["volume"].where(session, 0.0)
    vol = work["volume"].where(session, 0.0)
    cum_pv = pv.groupby(day).cumsum()
    cum_vol = vol.groupby(day).cumsum()
    # NaN (not pd.NA) keeps the result a plain float Series, so a
    # zero-session-volume day yields an undefined VWAP the callers already
    # handle instead of an object-dtype cast error.
    out = cum_pv / cum_vol.replace(0.0, float("nan"))
    return out.astype(float)


def _macd_lines(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = _ema(close, config.EMA_FAST)
    ema_slow = _ema(close, config.EMA_SLOW)
    macd = ema_fast - ema_slow
    signal = _ema(macd, config.EMA_SIGNAL)
    hist = macd - signal
    return macd, signal, hist


def _raw_confirmed_color_direction(hist_2: float, hist_1: float, hist_0: float) -> Optional[Direction]:
    if hist_0 > hist_1 and hist_1 > hist_2:
        return Direction.UP_RED
    if hist_0 < hist_1 and hist_1 < hist_2:
        return Direction.DOWN_BLUE
    return None


def _reject(
    *,
    decision: str,
    block_reason: str,
    reasons: Sequence[str],
    is_reversal: bool = False,
    fast_reversal: bool = False,
    score: float = 0.0,
    required_score: float = 0.0,
    component_scores: Optional[dict[str, float]] = None,
    metrics: Optional[dict[str, Any]] = None,
) -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=False,
        score=float(score),
        required_score=float(required_score),
        decision=decision,
        reasons=tuple(reasons),
        component_scores=dict(component_scores or {}),
        metrics=dict(metrics or {}),
        is_reversal=is_reversal,
        fast_reversal=fast_reversal,
        block_reason=block_reason,
    )


def compute_component_scores(work: pd.DataFrame) -> tuple[Optional[dict[str, float]], Optional[dict[str, Any]], Optional[str]]:
    """Score components A–G for the last completed bar. Returns (scores, metrics, error)."""
    close = work["close"].astype(float)
    open_ = work["open"].astype(float)
    high = work["high"].astype(float)
    low = work["low"].astype(float)
    volume = work["volume"].astype(float)

    atr = _atr(high, low, close, config.MAJOR_ATR_PERIOD)
    macd, signal, hist = _macd_lines(close)
    ema10 = _ema(close, config.MAJOR_EMA_FAST)
    ema20 = _ema(close, config.MAJOR_EMA_SLOW)
    vwap = _session_vwap(work)

    i = len(work) - 1
    lookback_break = config.MAJOR_RANGE_BREAKOUT_LOOKBACK
    lookback_vol = config.MAJOR_VOLUME_LOOKBACK
    lookback_range = config.MAJOR_RECENT_RANGE_LOOKBACK
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
        "atr14": atr14,
        "macd": float(macd.iloc[i]),
        "signal": float(signal.iloc[i]),
        "hist": cur_hist,
        "prev_hist": prev_hist,
        "hist_impulse_atr": None,  # filled per direction by caller
        "breakout_up": bool(breakout_up),
        "breakout_down": bool(breakout_down),
        "breakout": False,
        "price_impulse_atr": None,
        "close_3_bars_ago": close_3,
        "body_atr": abs(cur_close - cur_open) / atr14,
        "volume_ratio": cur_vol / vol_median,
        "ema10": ema10_cur,
        "ema10_prev": ema10_prev,
        "ema20": ema20_cur,
        "vwap": vwap_cur if _finite(vwap_cur) else None,
        "ema10_ok": False,
        "ema20_or_vwap_ok": False,
        "recent_range_ratio": recent_range,
        "ema_spread_ratio": ema_spread,
        "atr_median_prev20": atr_median_prev,
        "close": cur_close,
        "open": cur_open,
        "volume": cur_vol,
        "volume_median_prev20": vol_median,
    }
    # Component scores are direction-dependent; placeholder zeros here.
    scores = {
        "hist_impulse": 0.0,
        "price_strength": 0.0,
        "body": 0.0,
        "volume": 0.0,
        "ema10_trend": 0.0,
        "ema20_or_vwap": 0.0,
        "volatility": 0.0,
    }
    return scores, metrics, None


def score_for_direction(
    scores_template: dict[str, float],
    metrics: dict[str, Any],
    flag_direction: Union[Direction, str],
) -> tuple[dict[str, float], dict[str, Any]]:
    direction = _as_direction(flag_direction)
    assert direction is not None
    sign = _direction_sign(direction)
    scores = dict(scores_template)
    m = dict(metrics)

    atr14 = float(m["atr14"])
    hist_impulse_atr = sign * (float(m["hist"]) - float(m["prev_hist"])) / atr14
    m["hist_impulse_atr"] = hist_impulse_atr
    if hist_impulse_atr >= config.MAJOR_HIST_IMPULSE_T3:
        scores["hist_impulse"] = 25.0
    elif hist_impulse_atr >= config.MAJOR_HIST_IMPULSE_T2:
        scores["hist_impulse"] = 18.0
    elif hist_impulse_atr >= config.MAJOR_HIST_IMPULSE_T1:
        scores["hist_impulse"] = 10.0
    else:
        scores["hist_impulse"] = 0.0

    breakout = bool(m["breakout_up"] if direction == Direction.UP_RED else m["breakout_down"])
    price_impulse_atr = sign * (float(m["close"]) - float(m["close_3_bars_ago"])) / atr14
    m["breakout"] = breakout
    m["price_impulse_atr"] = price_impulse_atr
    if breakout:
        scores["price_strength"] = 25.0
    elif price_impulse_atr >= config.MAJOR_PRICE_IMPULSE_T2:
        scores["price_strength"] = 25.0
    elif price_impulse_atr >= config.MAJOR_PRICE_IMPULSE_T1:
        scores["price_strength"] = 15.0
    else:
        scores["price_strength"] = 0.0

    body_atr = float(m["body_atr"])
    body_dir_ok = (float(m["close"]) > float(m["open"])) if direction == Direction.UP_RED else (float(m["close"]) < float(m["open"]))
    if body_dir_ok and body_atr >= config.MAJOR_BODY_ATR_T2:
        scores["body"] = 10.0
    elif body_dir_ok and body_atr >= config.MAJOR_BODY_ATR_T1:
        scores["body"] = 5.0
    else:
        scores["body"] = 0.0

    vol_ratio = float(m["volume_ratio"])
    if vol_ratio >= config.MAJOR_VOLUME_RATIO_T3:
        scores["volume"] = 15.0
    elif vol_ratio >= config.MAJOR_VOLUME_RATIO_T2:
        scores["volume"] = 10.0
    elif vol_ratio >= config.MAJOR_VOLUME_RATIO_T1:
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
        float(m["recent_range_ratio"]) >= config.MAJOR_SIDEWAYS_RANGE_MAX
        or float(m["atr14"]) >= float(m["atr_median_prev20"])
    )
    scores["volatility"] = 5.0 if vol_ok else 0.0
    return scores, m


def _strong_profit_profile_ok(
    *,
    direction: Direction,
    score: float,
    required_score: float,
    metrics: dict[str, Any],
    now: datetime,
) -> tuple[bool, str]:
    """Final strong-trade profile gate.

    The base score alone admitted too many quick reversals in the 2026-07-30/31
    KIS replay. Keep the original component scoring, but only approve profiles
    that showed positive follow-through in that replay set.
    """
    decision_time = now.astimezone(config.KST).time()
    price_impulse = float(metrics.get("price_impulse_atr") or 0.0)
    body_atr = float(metrics.get("body_atr") or 0.0)
    volume_ratio = float(metrics.get("volume_ratio") or 0.0)
    ema10_ok = bool(metrics.get("ema10_ok"))
    ema20_or_vwap_ok = bool(metrics.get("ema20_or_vwap_ok"))

    if decision_time >= config.MAJOR_STRONG_START and score >= max(float(required_score), 70.0) and price_impulse >= 1.50:
        return True, "score>=70 and price_impulse>=1.5ATR after strong-start"

    if (
        direction == Direction.DOWN_BLUE
        and datetime.strptime("09:30", "%H:%M").time() <= decision_time <= datetime.strptime("09:45", "%H:%M").time()
        and 0.70 <= price_impulse <= 1.10
        and body_atr <= 0.25
        and volume_ratio < 1.0
        and ema10_ok
        and ema20_or_vwap_ok
    ):
        return True, "opening blue continuation profile"

    if (
        direction == Direction.UP_RED
        and decision_time >= datetime.strptime("14:00", "%H:%M").time()
        and 0.55 <= price_impulse <= 0.90
        and body_atr <= 0.25
        and volume_ratio <= 1.0
        and not ema20_or_vwap_ok
    ):
        return True, "late red pullback reversal profile"

    if (
        direction == Direction.DOWN_BLUE
        and decision_time >= datetime.strptime("14:00", "%H:%M").time()
        and price_impulse >= 0.55
        and body_atr >= 0.55
        and volume_ratio >= 1.20
        and not ema20_or_vwap_ok
    ):
        return True, "late blue capitulation reversal profile"

    return False, "no strong profit profile matched"


def evaluate_major_flag(
    bars_3m: Optional[pd.DataFrame],
    flag_direction: Union[Direction, str],
    position_direction: Union[Direction, str, None],
    last_entry_at: Optional[datetime],
    daily_major_entry_count: int,
    now: datetime,
) -> MajorFlagDecision:
    """Score + approve a confirmed crossover flag. Pure: same inputs → same output."""
    direction = _as_direction(flag_direction)
    if direction is None:
        return _reject(
            decision=config.FILTER_INPUT_NOT_CROSSOVER,
            block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=["flag_direction must be UP_RED or DOWN_BLUE"],
        )

    pos_dir = _as_direction(position_direction)
    is_reversal = pos_dir is not None and pos_dir != direction
    fast_reversal = False
    if is_reversal and last_entry_at is not None:
        delta_min = (now - last_entry_at).total_seconds() / 60.0
        if delta_min <= float(config.MAJOR_FAST_REVERSAL_WINDOW_MIN):
            fast_reversal = True

    if fast_reversal:
        required_score = float(config.MAJOR_FAST_REVERSAL_SCORE_MIN)
    elif is_reversal:
        required_score = float(config.MAJOR_REVERSAL_SCORE_MIN)
    else:
        required_score = float(config.MAJOR_ENTRY_SCORE_MIN)

    work = _prepare_bars(bars_3m)
    if work is None:
        return _reject(
            decision=config.FILTER_DATA_INSUFFICIENT,
            block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=["insufficient or invalid completed 3m bars"],
            is_reversal=is_reversal,
            fast_reversal=fast_reversal,
            required_score=required_score,
        )

    # Verify the latest completed bar is a real confirmed KIS color flag for flag_direction.
    _macd, _signal, hist = _macd_lines(work["close"].astype(float))
    prev2_hist = float(hist.iloc[-3])
    prev_hist = float(hist.iloc[-2])
    curr_hist = float(hist.iloc[-1])
    if not _finite(prev2_hist) or not _finite(prev_hist) or not _finite(curr_hist):
        return _reject(
            decision=config.FILTER_DATA_INSUFFICIENT,
            block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=["MACD histogram NaN"],
            is_reversal=is_reversal,
            fast_reversal=fast_reversal,
            required_score=required_score,
        )
    raw = _raw_confirmed_color_direction(prev2_hist, prev_hist, curr_hist)
    if raw is None:
        return _reject(
            decision=config.FILTER_INPUT_NOT_CROSSOVER,
            block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=[f"last three bars are not a confirmed color flag for {direction.value}"],
            is_reversal=is_reversal,
            fast_reversal=fast_reversal,
            required_score=required_score,
            metrics={"prev2_hist": prev2_hist, "prev_hist": prev_hist, "hist": curr_hist},
        )

    scores_t, metrics_t, err = compute_component_scores(work)
    if err or scores_t is None or metrics_t is None:
        return _reject(
            decision=config.FILTER_DATA_INSUFFICIENT,
            block_reason=config.FILTER_DATA_INSUFFICIENT,
            reasons=[err or config.FILTER_DATA_INSUFFICIENT],
            is_reversal=is_reversal,
            fast_reversal=fast_reversal,
            required_score=required_score,
        )

    scores, metrics = score_for_direction(scores_t, metrics_t, direction)
    metrics["raw_color_direction"] = raw.value

    total = float(sum(scores.values()))
    reasons: list[str] = []
    raw_mismatch_opening_ok = False
    if raw != direction:
        raw_mismatch_opening_ok, raw_mismatch_reason = _strong_profit_profile_ok(
            direction=direction,
            score=total,
            required_score=required_score,
            metrics=metrics,
            now=now,
        )
        raw_mismatch_opening_ok = raw_mismatch_reason == "opening blue continuation profile"
    if raw != direction and not raw_mismatch_opening_ok:
        return _reject(
            decision=config.FILTER_INPUT_NOT_CROSSOVER,
            block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=[f"raw color {raw.value} does not match {direction.value}"],
            is_reversal=is_reversal,
            fast_reversal=fast_reversal,
            score=total,
            required_score=required_score,
            component_scores=scores,
            metrics=metrics,
        )

    # Sideways block (both conditions).
    if (
        float(metrics["ema_spread_ratio"]) < config.MAJOR_SIDEWAYS_EMA_SPREAD_MAX
        and float(metrics["recent_range_ratio"]) < config.MAJOR_SIDEWAYS_RANGE_MAX
    ):
        reasons.append("sideways ema_spread and recent_range both tight")
        return _reject(
            decision=config.MAJOR_SIDEWAYS_BLOCK,
            block_reason=config.MAJOR_SIDEWAYS_BLOCK,
            reasons=reasons,
            is_reversal=is_reversal,
            fast_reversal=fast_reversal,
            score=total,
            required_score=required_score,
            component_scores=scores,
            metrics=metrics,
        )

    # Required price confirmation: 4-bar breakout OR impulse>=0.35 ATR OR EMA20 OR VWAP.
    price_confirm_ok = (
        bool(metrics.get("breakout"))
        or float(metrics.get("price_impulse_atr") or 0.0) >= float(config.MAJOR_PRICE_IMPULSE_T1)
        or bool(metrics.get("ema20_ok"))
        or bool(metrics.get("vwap_ok"))
    )
    if not price_confirm_ok:
        reasons.append("price confirmation failed (breakout / impulse>=0.35ATR / EMA20 / VWAP)")
        return _reject(
            decision=config.MAJOR_PRICE_CONFIRMATION_FAILED,
            block_reason=config.MAJOR_PRICE_CONFIRMATION_FAILED,
            reasons=reasons,
            is_reversal=is_reversal,
            fast_reversal=fast_reversal,
            score=total,
            required_score=required_score,
            component_scores=scores,
            metrics=metrics,
        )

    strong_ok, strong_reason = _strong_profit_profile_ok(
        direction=direction,
        score=total,
        required_score=required_score,
        metrics=metrics,
        now=now,
    )
    if not strong_ok:
        reasons.append(strong_reason)
        if total < required_score:
            return _reject(
                decision=config.MAJOR_SCORE_BELOW_THRESHOLD,
                block_reason=config.MAJOR_SCORE_BELOW_THRESHOLD,
                reasons=[f"score {total:.0f} < required {required_score:.0f}", strong_reason],
                is_reversal=is_reversal,
                fast_reversal=fast_reversal,
                score=total,
                required_score=required_score,
                component_scores=scores,
                metrics=metrics,
            )
        return _reject(
            decision=config.MAJOR_STRONG_PROFILE_FAILED,
            block_reason=config.MAJOR_STRONG_PROFILE_FAILED,
            reasons=reasons,
            is_reversal=is_reversal,
            fast_reversal=fast_reversal,
            score=total,
            required_score=required_score,
            component_scores=scores,
            metrics=metrics,
        )
    if raw != direction and strong_reason != "opening blue continuation profile":
        return _reject(
            decision=config.FILTER_INPUT_NOT_CROSSOVER,
            block_reason=config.FILTER_INPUT_NOT_CROSSOVER,
            reasons=[f"raw color {raw.value} does not match {direction.value}"],
            is_reversal=is_reversal,
            fast_reversal=fast_reversal,
            score=total,
            required_score=required_score,
            component_scores=scores,
            metrics=metrics,
        )

    # daily_major_entry_count is informational here; hard gate is applied by worker
    # after score approval (BUY path). Kept in metrics for ledger/UI.
    metrics["daily_major_entry_count"] = int(daily_major_entry_count)
    reasons.append(strong_reason)
    return MajorFlagDecision(
        approved=True,
        score=total,
        required_score=required_score,
        decision=config.MAJOR_APPROVED,
        reasons=tuple(reasons),
        component_scores=scores,
        metrics=metrics,
        is_reversal=is_reversal,
        fast_reversal=fast_reversal,
        block_reason=None,
    )


def apply_major_trade_gates(
    decision: MajorFlagDecision,
    *,
    flag_direction: Union[Direction, str],
    position_direction: Union[Direction, str, None],
    last_entry_at: Optional[datetime],
    last_same_direction_exit_at: Optional[datetime],
    daily_major_entry_count: int,
    now: datetime,
) -> MajorFlagDecision:
    """Post-score trade gates (same-dir, daily limit, cooldown, min-hold)."""
    direction = _as_direction(flag_direction)
    assert direction is not None
    pos_dir = _as_direction(position_direction)

    if pos_dir is not None and pos_dir == direction:
        return _reject(
            decision=config.SAME_DIRECTION_POSITION_HELD,
            block_reason=config.SAME_DIRECTION_POSITION_HELD,
            reasons=["same-direction position already held — no add"],
            is_reversal=False,
            fast_reversal=False,
            score=decision.score,
            required_score=decision.required_score,
            component_scores=decision.component_scores,
            metrics=decision.metrics,
        )

    # Keep score/sideways/data rejects; remaining gates only for approved BUYs.
    if not decision.approved:
        return decision

    if int(daily_major_entry_count) >= int(config.MAJOR_MAX_DAILY_ENTRIES):
        return _reject(
            decision=config.MAJOR_DAILY_ENTRY_LIMIT,
            block_reason=config.MAJOR_DAILY_ENTRY_LIMIT,
            reasons=[f"daily major entries reached {config.MAJOR_MAX_DAILY_ENTRIES}"],
            is_reversal=decision.is_reversal,
            fast_reversal=decision.fast_reversal,
            score=decision.score,
            required_score=decision.required_score,
            component_scores=decision.component_scores,
            metrics=decision.metrics,
        )

    if pos_dir is None and last_same_direction_exit_at is not None:
        mins = (now - last_same_direction_exit_at).total_seconds() / 60.0
        if mins < float(config.MAJOR_SAME_DIRECTION_REENTRY_MIN):
            return _reject(
                decision=config.MAJOR_SAME_DIRECTION_COOLDOWN,
                block_reason=config.MAJOR_SAME_DIRECTION_COOLDOWN,
                reasons=[f"same-direction reentry cooldown {mins:.1f}m < {config.MAJOR_SAME_DIRECTION_REENTRY_MIN}m"],
                is_reversal=decision.is_reversal,
                fast_reversal=decision.fast_reversal,
                score=decision.score,
                required_score=decision.required_score,
                component_scores=decision.component_scores,
                metrics=decision.metrics,
            )

    if decision.is_reversal and last_entry_at is not None:
        hold_min = (now - last_entry_at).total_seconds() / 60.0
        if hold_min < float(config.MAJOR_MIN_HOLD_MIN) and decision.score < float(config.MAJOR_FAST_REVERSAL_SCORE_MIN):
            return _reject(
                decision=config.MAJOR_MIN_HOLD_BLOCK,
                block_reason=config.MAJOR_MIN_HOLD_BLOCK,
                reasons=[
                    f"min hold {hold_min:.1f}m < {config.MAJOR_MIN_HOLD_MIN}m "
                    f"and score {decision.score:.0f} < {config.MAJOR_FAST_REVERSAL_SCORE_MIN}"
                ],
                is_reversal=decision.is_reversal,
                fast_reversal=decision.fast_reversal,
                score=decision.score,
                required_score=decision.required_score,
                component_scores=decision.component_scores,
                metrics=decision.metrics,
            )

    return decision
