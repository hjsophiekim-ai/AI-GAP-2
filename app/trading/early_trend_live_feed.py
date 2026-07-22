"""
early_trend_live_feed.py — Early Trend Detector 전용 실시간(5초 샘플) 가격
히스토리. 1분봉 기반 vote 신호는 새 분봉이 확정돼야만 바뀔 수 있어 태생적으로
30~90초 이상 반응이 늦다(2026-07-20 실측: 10:27 인버스 반전을 놓치고 10:25에
레버리지를 매수, 인버스가 이미 오른 10:34에야 뒤늦게 매수).

이 모듈은 별도의 틱데이터/체결 피드 없이, 5초 주기로 반복 조회하는 현재가
(collect_long_current/collect_inverse_current — 이미 존재하는 가벼운 함수)만으로
종목별 (시각, 가격) 샘플을 누적해 진짜 5/10/20/30초 기울기를 만든다. 1분봉으로는
구분 불가능한 "1분 안에서 방향이 바뀌었는지"를 이 히스토리로만 판단할 수 있다.

2026-07-22: Day Bias(당일 등락·Micron·예측점수)와 Live Trade Direction을 분리한다.
Live direction은 최근 3/5/10/15/30분 가격 구조 + ETF 초단위 창으로만 확정하며,
Enhanced 점수는 방향을 덮어쓰지 못한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

MAX_HISTORY_SECONDS = 90.0
LOOKBACK_WINDOWS_SECONDS: tuple[float, ...] = (5.0, 10.0, 20.0, 30.0)
# 호가 흔들림(노이즈)을 방향전환으로 오판하지 않기 위한 최소 변동폭.
MIN_SLOPE_PCT_FOR_DIRECTION = 0.02
REVERSAL_MIN_FACTORS = 3
REVERSAL_HOLD_SECONDS = 15.0

# Structural live direction: ≥3 of 4 factors confirm UP/DOWN (Enhanced-agnostic).
STRUCTURAL_FACTOR_MIN = 3
ETF_WINDOW_SECONDS_FOR_STRUCTURAL: tuple[int, ...] = (5, 10, 20)
DRAWDOWN_FORBID_BUY_PCT = 1.0
DRAWDOWN_EPISODE_CANDIDATE_PCT = 1.5
# Event bonuses (RSI/Bollinger recovery): valid for 2 completed 3-min bars, hard-stop ~10m.
EVENT_BONUS_MAX_3M_BARS = 2
EVENT_BONUS_HARD_DECAY_SECONDS = 10 * 60.0


def record_price_sample(history: Optional[dict], symbol: str, price: Optional[float], now: datetime) -> dict:
    """symbol의 샘플 리스트에 (시각, 가격)을 추가하고 MAX_HISTORY_SECONDS보다
    오래된 샘플은 버린다. price가 없으면(조회 실패) 기존 히스토리를 그대로 반환한다."""
    history = {k: list(v) for k, v in (history or {}).items()}
    if price is None:
        return history
    samples = list(history.get(symbol, []))
    samples.append({"t": now.isoformat(), "p": float(price)})
    cutoff = now - timedelta(seconds=MAX_HISTORY_SECONDS)
    kept = []
    for s in samples:
        try:
            if datetime.fromisoformat(s["t"]) >= cutoff:
                kept.append(s)
        except Exception:
            continue
    history[symbol] = kept
    return history


def _sample_at_or_before(samples: list, target: datetime) -> Optional[dict]:
    candidate = None
    candidate_t: Optional[datetime] = None
    for s in samples:
        try:
            t = datetime.fromisoformat(s["t"])
        except Exception:
            continue
        if t <= target and (candidate_t is None or t > candidate_t):
            candidate, candidate_t = s, t
    return candidate


def slope_pct_at(history: dict, symbol: str, now: datetime, lookback_seconds: float) -> Optional[float]:
    """lookback_seconds 전 샘플 대비 최신 샘플의 변동률(%). 요청한 만큼의 과거
    히스토리가 아직 쌓이지 않았으면(수집 시작 직후) None을 반환한다 — 짧은
    히스토리를 가장 오래된 샘플로 대체해 성급하게 방향을 판단하지 않는다."""
    samples = (history or {}).get(symbol) or []
    if len(samples) < 2:
        return None
    latest = samples[-1]
    target = now - timedelta(seconds=lookback_seconds)
    base = _sample_at_or_before(samples, target)
    if base is None:
        try:
            oldest_age = (now - datetime.fromisoformat(samples[0]["t"])).total_seconds()
        except Exception:
            return None
        if oldest_age < lookback_seconds * 0.6:
            return None
        base = samples[0]
    try:
        base_price = float(base["p"])
        latest_price = float(latest["p"])
    except Exception:
        return None
    if base_price <= 0:
        return None
    return round((latest_price / base_price - 1.0) * 100.0, 4)


def multi_window_slopes(history: dict, symbol: str, now: datetime) -> dict:
    return {int(w): slope_pct_at(history, symbol, now, w) for w in LOOKBACK_WINDOWS_SECONDS}


def compute_live_direction(history: dict, symbol: str, now: datetime) -> dict:
    """요구사항1 — ETF 자체 5/10/20/30초 기울기. 사용 가능한 구간이 2개 이상이고
    전부 같은 방향(노이즈 임계 이상)으로 일치할 때만 신뢰할 수 있는 방향으로
    본다(일부만 일치하면 아직 확정된 반전이 아니라 혼조로 취급해 None)."""
    slopes = multi_window_slopes(history, symbol, now)
    available = {w: v for w, v in slopes.items() if v is not None}
    direction = None
    window_directions = {}
    for w, value in available.items():
        if value >= MIN_SLOPE_PCT_FOR_DIRECTION:
            window_directions[w] = "UP"
        elif value <= -MIN_SLOPE_PCT_FOR_DIRECTION:
            window_directions[w] = "DOWN"
    for candidate in ("UP", "DOWN"):
        if window_directions.get(5) == candidate and window_directions.get(10) == candidate:
            if sum(1 for d in window_directions.values() if d == candidate) >= 3:
                direction = candidate
                break
    return {
        "slopes": slopes,
        "window_directions": window_directions,
        "direction": direction,
        "windows_available": len(available),
    }


def _implied_trade_direction(symbol: str, raw_direction: Optional[str], *, signal_symbol: str, long_symbol: str, inverse_symbol: str) -> Optional[str]:
    if raw_direction not in ("UP", "DOWN"):
        return None
    if symbol in (signal_symbol, long_symbol):
        return raw_direction
    if symbol == inverse_symbol:
        return {"UP": "DOWN", "DOWN": "UP"}.get(raw_direction)
    return None


def compute_live_trade_direction(
    history: dict, now: datetime, *, signal_symbol: str, long_symbol: str, inverse_symbol: str,
) -> dict:
    """Return the fast trade direction decoupled from 15/30m structural trend.

    UP means favor the leveraged long ETF, DOWN means favor the inverse ETF. The
    inverse ETF's own price direction is normalized back to the underlying trade
    direction, so long DOWN + inverse UP both vote DOWN.
    """
    per_symbol = {
        symbol: compute_live_direction(history, symbol, now)
        for symbol in (signal_symbol, long_symbol, inverse_symbol)
    }
    votes: list[str] = []
    for symbol, result in per_symbol.items():
        implied = _implied_trade_direction(
            symbol, result.get("direction"),
            signal_symbol=signal_symbol, long_symbol=long_symbol, inverse_symbol=inverse_symbol,
        )
        if implied:
            votes.append(implied)
    up_votes = votes.count("UP")
    down_votes = votes.count("DOWN")
    direction = None
    if max(up_votes, down_votes) >= 2 and up_votes != down_votes:
        direction = "UP" if up_votes > down_votes else "DOWN"
    return {
        "direction": direction,
        "up_votes": up_votes,
        "down_votes": down_votes,
        "per_symbol": per_symbol,
        "updated_at": now.isoformat(),
    }


def update_reversal_candidate_state(
    state: Optional[dict], *, live_direction: Optional[str], previous_direction: Optional[str],
    factors: dict, now: datetime,
) -> dict:
    """Detect short reversals for SHADOW/diagnostics only (2026-07-22).

    LIVE broker orders must not use REVERSAL_CANDIDATE as an entry path.
    Price-action early entry (strategy D) is isolated to SHADOW; real orders
    go only through weighted RANGE (strategy A). A REVERSAL_CANDIDATE still
    requires at least 3 active factors to persist for 15s for UI/diagnostics.
    """
    state = dict(state or {})
    active = [name for name, ok in (factors or {}).items() if ok]
    factor_count = len(active)
    opposite = bool(previous_direction and live_direction and previous_direction != live_direction)
    candidate_direction = live_direction if (live_direction in ("UP", "DOWN") and (opposite or not previous_direction)) else None

    if not candidate_direction or factor_count < REVERSAL_MIN_FACTORS:
        state.update({
            "status": "NONE", "candidate_direction": candidate_direction,
            "factor_count": factor_count, "active_factors": active,
            "existing_direction_blocked": False,
        })
        return state

    if state.get("candidate_direction") != candidate_direction or state.get("status") not in ("OBSERVING", "REVERSAL_CANDIDATE"):
        state.update({
            "status": "OBSERVING",
            "candidate_direction": candidate_direction,
            "first_detected_at": now.isoformat(),
            "confirmed_at": None,
        })

    try:
        first_dt = datetime.fromisoformat(state["first_detected_at"])
    except Exception:
        first_dt = now
        state["first_detected_at"] = now.isoformat()
    delay = max(0.0, (now - first_dt).total_seconds())
    if delay >= REVERSAL_HOLD_SECONDS:
        state["status"] = "REVERSAL_CANDIDATE"
        state["confirmed_at"] = state.get("confirmed_at") or now.isoformat()
        state["existing_direction_blocked"] = True
    state.update({
        "factor_count": factor_count,
        "active_factors": active,
        "detection_to_confirmation_delay_seconds": delay if state.get("status") == "REVERSAL_CANDIDATE" else None,
        "updated_at": now.isoformat(),
    })
    return state


def _float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _normalize_ohlcv(df_1min) -> Optional[Any]:
    """Return oldest-first OHLCV frame with datetime/open/high/low/close/volume, or None."""
    if df_1min is None or getattr(df_1min, "empty", True):
        return None
    try:
        import pandas as pd

        work = df_1min.copy()
        colmap = {}
        for src, dst in (
            ("datetime", "datetime"), ("time", "datetime"),
            ("open", "open"), ("high", "high"), ("low", "low"),
            ("close", "close"), ("stck_prpr", "close"), ("price", "close"),
            ("volume", "volume"), ("cntg_vol", "volume"),
        ):
            if src in work.columns and dst not in colmap.values():
                colmap[src] = dst
        if "close" not in colmap.values() and "close" not in work.columns:
            return None
        work = work.rename(columns=colmap)
        if "datetime" in work.columns:
            work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
            work = work.dropna(subset=["datetime"]).sort_values("datetime")
        else:
            work = work.reset_index(drop=True)
        for col in ("open", "high", "low", "close", "volume"):
            if col not in work.columns:
                if col == "volume":
                    work[col] = 0.0
                elif col in ("open", "high", "low"):
                    work[col] = work["close"]
                else:
                    return None
            work[col] = pd.to_numeric(work[col], errors="coerce")
        work = work.dropna(subset=["close"])
        return work if len(work) >= 6 else None
    except Exception:
        return None


def _pct_return_at(closes: list[float], minutes: int) -> Optional[float]:
    if len(closes) < minutes + 1:
        return None
    base = closes[-(minutes + 1)]
    last = closes[-1]
    if not base or base <= 0:
        return None
    return (last / base - 1.0) * 100.0


def _resample_3m_bars(work) -> Optional[Any]:
    if work is None or len(work) < 3:
        return None
    try:
        import pandas as pd

        if "datetime" not in work.columns:
            # Synthetic index every 1 minute from a fixed origin.
            idx = pd.date_range("2000-01-01", periods=len(work), freq="1min")
            frame = work.copy()
            frame.index = idx
        else:
            frame = work.set_index("datetime")
        agg = frame.resample("3min", label="right", closed="right").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
        }).dropna(subset=["close"])
        return agg if len(agg) >= 3 else None
    except Exception:
        return None


def _lh_ll_from_3m(bars_3m) -> dict:
    """Recent 3m bars: LH/LL and HH/HL structure."""
    empty = {
        "lower_highs": False, "lower_lows": False, "higher_highs": False, "higher_lows": False,
        "lh_ll": False, "hh_hl": False,
    }
    if bars_3m is None or len(bars_3m) < 3:
        return empty
    highs = [float(x) for x in bars_3m["high"].iloc[-3:].tolist()]
    lows = [float(x) for x in bars_3m["low"].iloc[-3:].tolist()]
    lower_highs = highs[-1] < highs[-2] <= highs[-3] or (highs[-1] <= highs[-2] and highs[-2] <= highs[-3])
    lower_lows = lows[-1] < lows[-2] <= lows[-3] or (lows[-1] <= lows[-2] and lows[-2] <= lows[-3])
    higher_highs = highs[-1] > highs[-2] >= highs[-3] or (highs[-1] >= highs[-2] and highs[-2] >= highs[-3])
    higher_lows = lows[-1] > lows[-2] >= lows[-3] or (lows[-1] >= lows[-2] and lows[-2] >= lows[-3])
    return {
        "lower_highs": bool(lower_highs),
        "lower_lows": bool(lower_lows),
        "higher_highs": bool(higher_highs),
        "higher_lows": bool(higher_lows),
        "lh_ll": bool(lower_highs and lower_lows),
        "hh_hl": bool(higher_highs and higher_lows),
    }


def _vwap_and_ema(work) -> dict:
    closes = [float(x) for x in work["close"].tolist()]
    volumes = [float(x) if x == x else 0.0 for x in work["volume"].tolist()]
    price = closes[-1]
    window = min(20, len(closes))
    w_closes = closes[-window:]
    w_vols = volumes[-window:]
    denom = sum(w_vols)
    if denom > 0:
        vwap = sum(p * v for p, v in zip(w_closes, w_vols)) / denom
    else:
        vwap = sum(w_closes) / len(w_closes)
    ema_span = min(9, len(closes))
    ema = sum(closes[-ema_span:]) / ema_span
    return {
        "price": price,
        "vwap": vwap,
        "ema_short": ema,
        "below_vwap": price < vwap,
        "below_ema": price < ema,
        "above_vwap": price > vwap,
        "above_ema": price > ema,
        "below_vwap_or_ema": price < vwap or price < ema,
        "above_vwap_or_ema": price > vwap or price > ema,
    }


def _etf_window_direction_count(window_directions: Optional[dict], direction: str) -> int:
    dirs = window_directions or {}
    return sum(1 for w in ETF_WINDOW_SECONDS_FOR_STRUCTURAL if dirs.get(w) == direction)


def compute_structural_live_direction(
    df_1min,
    *,
    etf_window_directions: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Resolve Live Trade Direction from minute structure + ETF second windows.

    DOWN when ≥3 of 4 hold (Enhanced score ignored):
      1) recent 3m bars LH/LL
      2) 10m and 15m returns negative
      3) price below VWAP or short EMA
      4) ≥2 of ETF 5/10/20s windows DOWN
    UP is fully symmetric. Day-bias scores must not call this path.
    """
    now = now or datetime.now()
    work = _normalize_ohlcv(df_1min)
    if work is None:
        return {
            "direction": None,
            "status": "DATA_INSUFFICIENT",
            "down_factors": {},
            "up_factors": {},
            "down_count": 0,
            "up_count": 0,
            "returns": {"5m": None, "10m": None, "15m": None, "30m": None},
            "updated_at": now.isoformat(),
        }
    closes = [float(x) for x in work["close"].tolist()]
    bars_3m = _resample_3m_bars(work)
    structure = _lh_ll_from_3m(bars_3m)
    levels = _vwap_and_ema(work)
    ret_5 = _pct_return_at(closes, 5)
    ret_10 = _pct_return_at(closes, 10)
    ret_15 = _pct_return_at(closes, 15)
    ret_30 = _pct_return_at(closes, 30)
    etf_down = _etf_window_direction_count(etf_window_directions, "DOWN")
    etf_up = _etf_window_direction_count(etf_window_directions, "UP")

    down_factors = {
        "lh_ll_3m": bool(structure["lh_ll"]),
        "returns_10m_15m_neg": bool(
            ret_10 is not None and ret_15 is not None and ret_10 < 0 and ret_15 < 0
        ),
        "below_vwap_or_ema": bool(levels["below_vwap_or_ema"]),
        "etf_windows_down_ge2": etf_down >= 2,
    }
    up_factors = {
        "hh_hl_3m": bool(structure["hh_hl"]),
        "returns_10m_15m_pos": bool(
            ret_10 is not None and ret_15 is not None and ret_10 > 0 and ret_15 > 0
        ),
        "above_vwap_or_ema": bool(levels["above_vwap_or_ema"]),
        "etf_windows_up_ge2": etf_up >= 2,
    }
    down_count = sum(1 for v in down_factors.values() if v)
    up_count = sum(1 for v in up_factors.values() if v)
    direction = None
    status = "MIXED"
    if down_count >= STRUCTURAL_FACTOR_MIN and down_count > up_count:
        direction = "DOWN"
        status = "STRUCTURAL_DOWN"
    elif up_count >= STRUCTURAL_FACTOR_MIN and up_count > down_count:
        direction = "UP"
        status = "STRUCTURAL_UP"
    return {
        "direction": direction,
        "status": status,
        "down_factors": down_factors,
        "up_factors": up_factors,
        "down_count": down_count,
        "up_count": up_count,
        "structure_3m": structure,
        "levels": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in levels.items()},
        "returns": {
            "5m": None if ret_5 is None else round(ret_5, 4),
            "10m": None if ret_10 is None else round(ret_10, 4),
            "15m": None if ret_15 is None else round(ret_15, 4),
            "30m": None if ret_30 is None else round(ret_30, 4),
        },
        "etf_window_directions": dict(etf_window_directions or {}),
        "etf_down_windows": etf_down,
        "etf_up_windows": etf_up,
        "updated_at": now.isoformat(),
    }


def merge_live_trade_direction(
    etf_seconds_result: Optional[dict],
    structural_result: Optional[dict],
) -> dict:
    """Combine ETF-second votes with structural minute confirmation.

    Structural ≥3/4 confirmation owns live direction regardless of Enhanced /
    day-bias scores. ETF-second direction is the fallback when structure is mixed.
    One or two rebound bars alone cannot flip a confirmed structural direction.
    """
    etf = dict(etf_seconds_result or {})
    structural = dict(structural_result or {})
    structural_dir = structural.get("direction")
    etf_dir = etf.get("direction")
    if structural_dir in ("UP", "DOWN"):
        direction = structural_dir
        source = "structural_minute"
    elif etf_dir in ("UP", "DOWN"):
        direction = etf_dir
        source = "etf_seconds"
    else:
        direction = None
        source = "none"
    return {
        **etf,
        "direction": direction,
        "direction_source": source,
        "structural": structural,
        "etf_seconds_direction": etf_dir,
        "day_bias_excluded": True,
    }


def compute_session_drawdown_gates(df_1min, *, now: Optional[datetime] = None) -> dict:
    """Drawdown from recent session high/low → buy forbid + episode candidates.

    ≥ -1.0% from high + LH/LL → forbid HYNIX_BUY (UP entries)
    ≥ -1.5% from high → immediate DOWN episode candidate
    Symmetric for UP / inverse from session low.
    """
    now = now or datetime.now()
    work = _normalize_ohlcv(df_1min)
    empty = {
        "drawdown_from_high_pct": None,
        "rally_from_low_pct": None,
        "lh_ll": False,
        "hh_hl": False,
        "forbid_hynix_buy": False,
        "forbid_inverse_buy": False,
        "down_episode_candidate": False,
        "up_episode_candidate": False,
        "updated_at": now.isoformat(),
    }
    if work is None:
        return empty
    closes = [float(x) for x in work["close"].tolist()]
    highs = [float(x) for x in work["high"].tolist()]
    lows = [float(x) for x in work["low"].tolist()]
    price = closes[-1]
    session_high = max(highs)
    session_low = min(lows)
    dd = ((price / session_high) - 1.0) * 100.0 if session_high > 0 else None
    rally = ((price / session_low) - 1.0) * 100.0 if session_low > 0 else None
    structure = _lh_ll_from_3m(_resample_3m_bars(work))
    forbid_hynix = bool(
        dd is not None and dd <= -DRAWDOWN_FORBID_BUY_PCT and structure["lh_ll"]
    )
    forbid_inverse = bool(
        rally is not None and rally >= DRAWDOWN_FORBID_BUY_PCT and structure["hh_hl"]
    )
    down_ep = bool(dd is not None and dd <= -DRAWDOWN_EPISODE_CANDIDATE_PCT)
    up_ep = bool(rally is not None and rally >= DRAWDOWN_EPISODE_CANDIDATE_PCT)
    return {
        "drawdown_from_high_pct": None if dd is None else round(dd, 4),
        "rally_from_low_pct": None if rally is None else round(rally, 4),
        "session_high": session_high,
        "session_low": session_low,
        "lh_ll": structure["lh_ll"],
        "hh_hl": structure["hh_hl"],
        "forbid_hynix_buy": forbid_hynix,
        "forbid_inverse_buy": forbid_inverse,
        "down_episode_candidate": down_ep,
        "up_episode_candidate": up_ep,
        "updated_at": now.isoformat(),
    }


def completed_3m_bar_index(df_1min, now: Optional[datetime] = None) -> Optional[int]:
    """Monotonic index of the last completed 3-minute bar (for event-bonus TTL)."""
    work = _normalize_ohlcv(df_1min)
    if work is None:
        return None
    bars = _resample_3m_bars(work)
    if bars is None or bars.empty:
        return None
    # Exclude the currently forming 3m bucket when possible.
    try:
        now = now or datetime.now()
        last_ts = bars.index[-1].to_pydatetime() if hasattr(bars.index[-1], "to_pydatetime") else None
        if last_ts is not None and (now - last_ts).total_seconds() < 180:
            return max(0, len(bars) - 2)
    except Exception:
        pass
    return len(bars) - 1


def update_event_bonus_state(
    state: Optional[dict],
    *,
    active_event_keys: list[str],
    now: datetime,
    completed_3m_index: Optional[int],
) -> dict:
    """Track RSI/Bollinger-style event bonuses; expire after 2×3m bars or 10 minutes.

    Once expired, the same recovery signal cannot re-accumulate until it goes
    inactive (breach again) and then reappears as a fresh event.
    """
    state = dict(state or {})
    events = dict(state.get("events") or {})
    active = set(active_event_keys or [])
    for key in active:
        meta = events.get(key)
        if meta is None:
            events[key] = {
                "first_seen_at": now.isoformat(),
                "first_3m_index": completed_3m_index,
                "active": True,
            }
        elif meta.get("expired"):
            # Still detecting the same recovery — do not re-arm until it clears.
            meta["active"] = True
        else:
            meta["active"] = True
    for key, meta in list(events.items()):
        if key not in active:
            meta["active"] = False
        try:
            first_dt = datetime.fromisoformat(meta["first_seen_at"])
            age_sec = (now - first_dt).total_seconds()
        except Exception:
            age_sec = EVENT_BONUS_HARD_DECAY_SECONDS + 1
        bars_elapsed = None
        if completed_3m_index is not None and meta.get("first_3m_index") is not None:
            try:
                bars_elapsed = int(completed_3m_index) - int(meta["first_3m_index"])
            except Exception:
                bars_elapsed = None
        expired = age_sec >= EVENT_BONUS_HARD_DECAY_SECONDS or (
            bars_elapsed is not None and bars_elapsed >= EVENT_BONUS_MAX_3M_BARS
        )
        meta["bars_elapsed"] = bars_elapsed
        meta["age_seconds"] = round(age_sec, 2)
        meta["expired"] = bool(expired)
        meta["scale"] = 0.0 if expired else 1.0
        # Drop only after the underlying recovery signal has cleared.
        if expired and key not in active:
            events.pop(key, None)
    state["events"] = events
    state["updated_at"] = now.isoformat()
    return state


def event_bonus_scale(state: Optional[dict], event_key: str) -> float:
    meta = ((state or {}).get("events") or {}).get(event_key) or {}
    try:
        return float(meta.get("scale", 0.0))
    except Exception:
        return 0.0


def scale_event_bonus_points(
    points: list[tuple[float, str]],
    state: Optional[dict],
    *,
    event_labels: Optional[set[str]] = None,
) -> tuple[list[tuple[float, str]], list[str]]:
    """Zero out aged recovery event points; leave non-event points untouched."""
    labels = event_labels or {
        "RSI(14) 30 이하 이탈 후 재돌파",
        "볼린저 하단 이탈 후 회복",
        "Williams %R -80 이하 이탈 후 회복",
    }
    scaled: list[tuple[float, str]] = []
    active_keys: list[str] = []
    for pts, desc in points:
        if desc in labels:
            active_keys.append(desc)
            scale = event_bonus_scale(state, desc)
            if scale > 0:
                scaled.append((pts * scale, desc))
            # else: expired — drop (no accumulation past TTL)
        else:
            scaled.append((pts, desc))
    return scaled, active_keys


def collect_event_bonus_keys(
    points: list[tuple[float, str]],
    *,
    event_labels: Optional[set[str]] = None,
) -> list[str]:
    labels = event_labels or {
        "RSI(14) 30 이하 이탈 후 재돌파",
        "볼린저 하단 이탈 후 회복",
        "Williams %R -80 이하 이탈 후 회복",
    }
    return [desc for _, desc in points if desc in labels]