"""
early_trend_live_feed.py — Early Trend Detector 전용 실시간(5초 샘플) 가격
히스토리. 1분봉 기반 vote 신호는 새 분봉이 확정돼야만 바뀔 수 있어 태생적으로
30~90초 이상 반응이 늦다(2026-07-20 실측: 10:27 인버스 반전을 놓치고 10:25에
레버리지를 매수, 인버스가 이미 오른 10:34에야 뒤늦게 매수).

이 모듈은 별도의 틱데이터/체결 피드 없이, 5초 주기로 반복 조회하는 현재가
(collect_long_current/collect_inverse_current — 이미 존재하는 가벼운 함수)만으로
종목별 (시각, 가격) 샘플을 누적해 진짜 5/10/20/30초 기울기를 만든다. 1분봉으로는
구분 불가능한 "1분 안에서 방향이 바뀌었는지"를 이 히스토리로만 판단할 수 있다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

MAX_HISTORY_SECONDS = 90.0
LOOKBACK_WINDOWS_SECONDS: tuple[float, ...] = (5.0, 10.0, 20.0, 30.0)
# 호가 흔들림(노이즈)을 방향전환으로 오판하지 않기 위한 최소 변동폭.
MIN_SLOPE_PCT_FOR_DIRECTION = 0.02
REVERSAL_MIN_FACTORS = 3
REVERSAL_HOLD_SECONDS = 15.0


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
    """Detect short reversals without waiting for the 3-minute/main cycle.

    A REVERSAL_CANDIDATE requires at least 3 active factors to persist for 15s.
    Single rebound bars therefore create only an observing candidate, not a
    confirmed live direction change.
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
