"""hynix_cycle_detector.py — Cycle Detector AI + Momentum Acceleration + Turning Point Probability.

SK하이닉스 장중 사이클(갭상승 실패/패닉셀/바닥/반등/추세/천정/붕괴)을 가격·거래량·
기술지표의 "구조"(고정 가격이 아니라 수익률/VWAP/ATR/고저점 구조/모멘텀 변화율)로
판정하고, 그에 따른 추천 매매 액션(HYNIX/INVERSE 진입·청산 비중)을 계산한다.

특정 하루에 맞춘 하드코딩은 없다 — 모든 임계값은 수익률(%)/ATR 배수/표준화 점수
기준이다. 상태 전환은 최소 2개 연속 1분봉 또는 1개 완성된 3분봉 확인을 거쳐야만
확정된다(한 틱 노이즈로 전환되지 않음) — `state` dict를 호출부가 매 사이클 이어서
넘겨줘야 이 확인 로직이 동작한다(hynix_switch_state.py의 상태 dict 관례와 동일).

SHADOW MODE: decide_cycle_trade_action()의 반환값은 "권장(recommended)" 행동일 뿐,
이 모듈 자체는 어떤 주문도 실행하지 않는다. 실제 주문 연결 여부는 호출부
(app/services/hynix_switch_engine.py)가 별도 플래그로 결정한다 — 명세상 최소
5거래일 Shadow Mode 검증 후에만 실제 주문에 연결하도록 되어 있다.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger

ROOT = Path(__file__).resolve().parent.parent.parent
_LOG_PATH = ROOT / "data" / "logs" / "hynix_cycle_ai_log.csv"

# ── Cycle Phase 목록(명세 2절) ────────────────────────────────────────────────
PHASE_OPENING_GAP = "OPENING_GAP"
PHASE_GAP_FAILURE = "GAP_FAILURE"
PHASE_PANIC_SELL = "PANIC_SELL"
PHASE_CAPITULATION = "CAPITULATION"
PHASE_SELLING_EXHAUSTION = "SELLING_EXHAUSTION"
PHASE_BASE_BUILDING = "BASE_BUILDING"
PHASE_EARLY_REVERSAL_UP = "EARLY_REVERSAL_UP"
PHASE_REVERSAL_CONFIRMED_UP = "REVERSAL_CONFIRMED_UP"
PHASE_TREND_UP = "TREND_UP"
PHASE_CLIMAX_UP = "CLIMAX_UP"
PHASE_DISTRIBUTION = "DISTRIBUTION"
PHASE_BREAKDOWN = "BREAKDOWN"
PHASE_RANGE_NOISE = "RANGE_NOISE"
PHASE_NO_TRADE = "NO_TRADE"

# 이 순서로 평가하며, 조건을 만족하는 첫 phase가 raw_phase가 된다(우선순위).
_PHASE_PRECEDENCE = [
    PHASE_GAP_FAILURE, PHASE_PANIC_SELL, PHASE_CAPITULATION, PHASE_SELLING_EXHAUSTION,
    PHASE_BASE_BUILDING, PHASE_EARLY_REVERSAL_UP, PHASE_REVERSAL_CONFIRMED_UP,
    PHASE_TREND_UP, PHASE_CLIMAX_UP, PHASE_DISTRIBUTION, PHASE_BREAKDOWN,
]

_CONFIRM_BARS_1MIN = 2  # 최소 연속 1분봉 확인 개수
_GAP_WINDOW = ("09:00", "09:30")

# Cycle Phase는 신규진입 Entry Gate가 아니라 최종점수(fusion_score)의 보조 feature로만
# 쓴다 — 작은 가점/감점일 뿐 단독으로 주문을 막지 않는다(app.models.hynix_decision_v2의
# calculate_fusion_score가 이 값을 0.10 가중치로만 반영한다). 사용자가 명시한 7개
# phase 외 나머지는 방향성에 맞춰 합리적으로 근사한 값이다.
_CYCLE_BONUS = {
    PHASE_TREND_UP: 15.0, PHASE_REVERSAL_CONFIRMED_UP: 12.0, PHASE_EARLY_REVERSAL_UP: 10.0,
    PHASE_SELLING_EXHAUSTION: 10.0, PHASE_BASE_BUILDING: 6.0, PHASE_GAP_FAILURE: 8.0,
    PHASE_DISTRIBUTION: -4.0, PHASE_NO_TRADE: -8.0,
    PHASE_PANIC_SELL: -6.0, PHASE_CAPITULATION: -10.0, PHASE_CLIMAX_UP: 4.0,
    PHASE_BREAKDOWN: -10.0, PHASE_RANGE_NOISE: 0.0, PHASE_OPENING_GAP: 0.0,
}


def calculate_cycle_bonus(cycle_phase: Optional[str]) -> float:
    """Cycle Phase를 fusion_score의 작은 가점/감점(feature)으로 변환한다.

    절대 단독 Entry Gate가 아니다 — 호출부가 이 값으로 신규진입을 차단해서는 안 된다.
    """
    return _CYCLE_BONUS.get(cycle_phase, 0.0)

CSV_LOG_FIELDS = [
    "timestamp", "hynix_price", "inverse_price", "cycle_phase", "previous_cycle_phase",
    "phase_duration_seconds", "momentum_velocity", "momentum_acceleration_up",
    "momentum_acceleration_down", "early_reversal_score", "up_turn_3m", "up_turn_5m",
    "up_turn_10m", "down_turn_3m", "down_turn_5m", "down_turn_10m", "cycle_confidence",
    "cycle_entry_score", "enhanced_score", "effective_micron_score", "recommended_symbol",
    "recommended_position_pct", "final_action", "order_sent", "order_executed",
    "reason_top1", "reason_top2", "reason_top3",
]


# =============================================================================
# 공용 수치 헬퍼 (pandas 기반 — micron_proxy_prediction.py와 동일한 하우스 스타일)
# =============================================================================

def _norm01(value: Optional[float], scale: float) -> Optional[float]:
    if value is None:
        return None
    return max(0.0, min(100.0, 50.0 + (value / scale) * 50.0))


def _rsi(closes: pd.Series, period: int = 14) -> Optional[float]:
    if closes is None or len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.rolling(period).mean().iloc[-1]
    avg_loss = loss.rolling(period).mean().iloc[-1]
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def _macd_hist_series(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[pd.Series]:
    if closes is None or len(closes) < slow + signal:
        return None
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - signal_line


def _ema(closes: pd.Series, span: int) -> Optional[pd.Series]:
    if closes is None or len(closes) < span:
        return None
    return closes.ewm(span=span, adjust=False).mean()


def calculate_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """True Range의 rolling 평균(ATR). df: open/high/low/close 컬럼 필요, oldest-first."""
    if df is None or len(df) < period + 1:
        return None
    high, low, close = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low, (high - prev_close).abs(), (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return round(float(atr), 2) if not pd.isna(atr) else None


def _vwap(df: pd.DataFrame) -> Optional[float]:
    if df is None or df.empty:
        return None
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].astype(float)
    total = vol.sum()
    if total <= 0:
        return None
    return float((typical * vol).sum() / total)


def _pct_return(series: pd.Series, n: int) -> Optional[float]:
    if series is None or len(series) < 2:
        return None
    n = min(n, len(series) - 1)
    if n <= 0:
        return None
    start, end = series.iloc[-1 - n], series.iloc[-1]
    if start == 0 or pd.isna(start) or pd.isna(end):
        return None
    return round((end / start - 1.0) * 100.0, 4)


def _volume_ratio(vol: pd.Series, lookback: int = 20) -> Optional[float]:
    if vol is None or len(vol) < lookback + 1:
        return None
    latest = float(vol.iloc[-1])
    avg = float(vol.iloc[-1 - lookback:-1].mean())
    if avg <= 0:
        return None
    return round(latest / avg, 3)


def _lower_wick_ratio(bar: pd.Series) -> float:
    body_low = min(bar["open"], bar["close"])
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return 0.0
    return max(0.0, (body_low - bar["low"]) / rng)


def _upper_wick_ratio(bar: pd.Series) -> float:
    body_high = max(bar["open"], bar["close"])
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return 0.0
    return max(0.0, (bar["high"] - body_high) / rng)


def _bearish_count(df: pd.DataFrame, n: int = 3) -> int:
    if df is None or len(df) < n:
        return 0
    tail = df.tail(n)
    return int((tail["close"] < tail["open"]).sum())


def _bullish_count(df: pd.DataFrame, n: int = 3) -> int:
    if df is None or len(df) < n:
        return 0
    tail = df.tail(n)
    return int((tail["close"] > tail["open"]).sum())


def _macd_hist_improving_streak(hist: Optional[pd.Series], n: int = 3) -> bool:
    if hist is None or len(hist) < n + 1:
        return False
    tail = hist.tail(n + 1).reset_index(drop=True)
    return all(tail.iloc[i + 1] > tail.iloc[i] for i in range(len(tail) - 1))


def _macd_hist_declining_streak(hist: Optional[pd.Series], n: int = 2) -> bool:
    if hist is None or len(hist) < n + 1:
        return False
    tail = hist.tail(n + 1).reset_index(drop=True)
    return all(tail.iloc[i + 1] < tail.iloc[i] for i in range(len(tail) - 1))


def _lower_lows_shrinking(df: pd.DataFrame, n: int = 5) -> bool:
    """최근 n개 저점 갱신폭이 축소되는지(대략적 selling-exhaustion 신호)."""
    if df is None or len(df) < n:
        return False
    lows = df["low"].tail(n).reset_index(drop=True)
    diffs = lows.diff().dropna()
    if len(diffs) < 2:
        return False
    down_diffs = diffs[diffs < 0].abs()
    if len(down_diffs) < 2:
        return True
    return bool(down_diffs.iloc[-1] < down_diffs.iloc[0])


def _higher_lows(df: pd.DataFrame, n: int = 3) -> bool:
    if df is None or len(df) < n:
        return False
    lows = df["low"].tail(n).reset_index(drop=True)
    return bool(lows.iloc[-1] >= lows.iloc[0]) and lows.is_monotonic_increasing


def _higher_highs(df: pd.DataFrame, n: int = 3) -> bool:
    if df is None or len(df) < n:
        return False
    highs = df["high"].tail(n).reset_index(drop=True)
    return bool(highs.iloc[-1] >= highs.iloc[0])


# =============================================================================
# 9. Momentum Acceleration
# =============================================================================

def calculate_momentum_acceleration(df_1min: Optional[pd.DataFrame]) -> dict:
    """가격 방향이 아니라 속도의 변화를 계산한다.

    velocity_3 = 최근 3개 1분 수익률 평균. acceleration = 최근 velocity_3 - 이전 velocity_3.
    """
    if df_1min is None or len(df_1min) < 8:
        return {
            "momentum_velocity_up": 50.0, "momentum_velocity_down": 50.0,
            "momentum_acceleration_up": 50.0, "momentum_acceleration_down": 50.0,
            "acceleration_confirmed_up": False, "acceleration_confirmed_down": False,
            "available": False,
        }

    closes = df_1min["close"].astype(float)
    r1 = closes.pct_change().dropna() * 100.0
    velocity_3 = r1.rolling(3).mean()
    acceleration = velocity_3.diff()

    latest_velocity = velocity_3.iloc[-1] if not velocity_3.empty else None
    latest_accel = acceleration.iloc[-1] if not acceleration.empty else None

    accel_recent = acceleration.tail(20).dropna()
    accel_mean = accel_recent.mean() if len(accel_recent) >= 5 else None
    accel_std = accel_recent.std() if len(accel_recent) >= 5 else None

    accel_confirmed_up = False
    accel_confirmed_down = False
    if latest_accel is not None and accel_mean is not None and accel_std is not None and accel_std > 0:
        accel_confirmed_up = latest_accel > (accel_mean + accel_std)
        accel_confirmed_down = latest_accel < (accel_mean - accel_std)

    r1_last3 = r1.tail(3)
    consecutive_up = len(r1_last3) == 3 and r1_last3.is_monotonic_increasing
    consecutive_down = len(r1_last3) == 3 and r1_last3.is_monotonic_decreasing

    vol_increasing = None
    if "volume" in df_1min.columns:
        vr = _volume_ratio(df_1min["volume"].astype(float), lookback=10)
        vol_increasing = (vr is not None and vr > 1.0)

    broke_prior_high = None
    if len(df_1min) >= 6:
        broke_prior_high = bool(closes.iloc[-1] > df_1min["high"].iloc[-6:-1].max())

    return {
        "momentum_velocity_up": _norm01(latest_velocity, 0.3) or 50.0,
        "momentum_velocity_down": _norm01(-(latest_velocity) if latest_velocity is not None else None, 0.3) or 50.0,
        "momentum_acceleration_up": _norm01(latest_accel, 0.15) or 50.0,
        "momentum_acceleration_down": _norm01(-(latest_accel) if latest_accel is not None else None, 0.15) or 50.0,
        "acceleration_confirmed_up": bool(accel_confirmed_up and consecutive_up and bool(vol_increasing) and bool(broke_prior_high)),
        "acceleration_confirmed_down": bool(accel_confirmed_down and consecutive_down),
        "raw_velocity_3": round(float(latest_velocity), 4) if latest_velocity is not None and not pd.isna(latest_velocity) else None,
        "raw_acceleration": round(float(latest_accel), 4) if latest_accel is not None and not pd.isna(latest_accel) else None,
        "available": True,
    }


# =============================================================================
# 10. Turning Point Probability
# =============================================================================

def calculate_turning_point_probability(
    df_1min: Optional[pd.DataFrame],
    momentum: Optional[dict] = None,
    korea_semiconductor_confirmation_score: Optional[float] = None,
    effective_micron_score: Optional[float] = None,
    order_flow_score: Optional[float] = None,
) -> dict:
    """3분/5분/10분 후 전환확률(상승/하락 turn probability, 0~100).

    규칙기반. 데이터가 부족하면 confidence를 낮추고 50 근처로 수렴시킨다.
    """
    if df_1min is None or len(df_1min) < 10:
        return {
            "up_turn_probability_3m": 50.0, "up_turn_probability_5m": 50.0, "up_turn_probability_10m": 50.0,
            "down_turn_probability_3m": 50.0, "down_turn_probability_5m": 50.0, "down_turn_probability_10m": 50.0,
            "confidence": 20.0, "available": False,
        }

    closes = df_1min["close"].astype(float)
    hist = _macd_hist_series(closes)
    rsi_now = _rsi(closes, 14)

    up_components: dict = {}
    down_components: dict = {}

    up_components["lower_lows_reversing"] = 70.0 if _higher_lows(df_1min, 3) else 40.0
    down_components["higher_highs_reversing"] = 70.0 if not _higher_highs(df_1min, 3) else 40.0

    up_components["macd_hist_improving"] = 65.0 if _macd_hist_improving_streak(hist, 3) else 45.0
    down_components["macd_hist_declining"] = 65.0 if _macd_hist_declining_streak(hist, 2) else 45.0

    if rsi_now is not None:
        up_components["rsi_bullish_zone"] = 65.0 if 30.0 <= rsi_now <= 45.0 else (55.0 if rsi_now < 30.0 else 45.0)
        down_components["rsi_bearish_zone"] = 65.0 if 55.0 <= rsi_now <= 70.0 else (55.0 if rsi_now > 70.0 else 45.0)

    if len(df_1min) >= 6 and "volume" in df_1min.columns:
        recent_down_bars = df_1min.tail(6)
        down_vol = recent_down_bars.loc[recent_down_bars["close"] < recent_down_bars["open"], "volume"]
        up_vol = recent_down_bars.loc[recent_down_bars["close"] > recent_down_bars["open"], "volume"]
        if len(down_vol) >= 2:
            down_vol_trend = down_vol.diff().dropna()
            up_components["down_volume_declining"] = 62.0 if (len(down_vol_trend) and down_vol_trend.iloc[-1] < 0) else 45.0
        if len(up_vol) >= 2:
            up_vol_trend = up_vol.diff().dropna()
            down_components["up_volume_declining"] = 62.0 if (len(up_vol_trend) and up_vol_trend.iloc[-1] < 0) else 45.0

    last_bar = df_1min.iloc[-1]
    up_components["lower_wick"] = _norm01((_lower_wick_ratio(last_bar) - 0.3) * 100, 30.0) or 50.0
    down_components["upper_wick"] = _norm01((_upper_wick_ratio(last_bar) - 0.3) * 100, 30.0) or 50.0

    vwap = _vwap(df_1min)
    if vwap:
        gap_pct = abs(float(closes.iloc[-1]) - vwap) / vwap * 100
        up_components["vwap_gap_narrowing"] = _norm01((1.0 - gap_pct), 1.0) or 50.0
        down_components["vwap_break"] = 65.0 if float(closes.iloc[-1]) < vwap else 45.0

    if len(df_1min) >= 6:
        up_components["prior_high_break"] = 68.0 if float(closes.iloc[-1]) > df_1min["high"].iloc[-6:-1].max() else 42.0
        down_components["prior_low_break"] = 68.0 if float(closes.iloc[-1]) < df_1min["low"].iloc[-6:-1].max() else 42.0

    if order_flow_score is not None:
        up_components["order_flow"] = order_flow_score
        down_components["order_flow"] = 100.0 - order_flow_score
    if korea_semiconductor_confirmation_score is not None:
        up_components["korea_confirmation"] = korea_semiconductor_confirmation_score
        down_components["korea_confirmation"] = 100.0 - korea_semiconductor_confirmation_score
    if effective_micron_score is not None:
        up_components["micron_score"] = effective_micron_score
        down_components["micron_score"] = 100.0 - effective_micron_score

    def _aggregate(components: dict) -> float:
        if not components:
            return 50.0
        return round(sum(components.values()) / len(components), 2)

    base_up = _aggregate(up_components)
    base_down = _aggregate(down_components)

    coverage = len(up_components) / 9.0
    confidence = round(max(20.0, min(90.0, coverage * 90.0)), 1)

    def _horizon_adjust(base: float, minutes: int) -> float:
        # 짧은 horizon일수록 신호를 더 강하게, 긴 horizon일수록 50으로 수렴(불확실성 증가).
        pull = {3: 1.0, 5: 0.85, 10: 0.65}.get(minutes, 0.7)
        return round(50.0 + (base - 50.0) * pull, 1)

    momentum_boost_up = 0.0
    momentum_boost_down = 0.0
    if momentum:
        if momentum.get("acceleration_confirmed_up"):
            momentum_boost_up = 5.0
        if momentum.get("acceleration_confirmed_down"):
            momentum_boost_down = 5.0

    return {
        "up_turn_probability_3m": round(max(0.0, min(100.0, _horizon_adjust(base_up, 3) + momentum_boost_up)), 1),
        "up_turn_probability_5m": round(max(0.0, min(100.0, _horizon_adjust(base_up, 5) + momentum_boost_up)), 1),
        "up_turn_probability_10m": round(max(0.0, min(100.0, _horizon_adjust(base_up, 10) + momentum_boost_up)), 1),
        "down_turn_probability_3m": round(max(0.0, min(100.0, _horizon_adjust(base_down, 3) + momentum_boost_down)), 1),
        "down_turn_probability_5m": round(max(0.0, min(100.0, _horizon_adjust(base_down, 5) + momentum_boost_down)), 1),
        "down_turn_probability_10m": round(max(0.0, min(100.0, _horizon_adjust(base_down, 10) + momentum_boost_down)), 1),
        "confidence": confidence, "available": True,
        "up_components": up_components, "down_components": down_components,
    }


# =============================================================================
# 2-8. Cycle Phase 판정
# =============================================================================

def _raw_phase(
    df_1min: pd.DataFrame, now: datetime, gap_pct: Optional[float], session_high: Optional[float],
    session_low: Optional[float], vwap: Optional[float], atr: Optional[float], prior_close: Optional[float],
    momentum: dict, turning_point: dict,
) -> dict:
    """현재 1분봉 데이터만으로(확인 상태기계 이전) raw phase와 조건 충족 상세를 계산한다."""
    closes = df_1min["close"].astype(float)
    current_price = float(closes.iloc[-1])
    hist = _macd_hist_series(closes)
    rsi_now = _rsi(closes, 14)
    now_hm = now.strftime("%H:%M")

    conditions: dict = {}
    detail: dict = {"current_price": current_price, "vwap": vwap, "atr": atr, "rsi": rsi_now}

    # ── GAP_FAILURE (09:00~09:30) ────────────────────────────────────────────
    if _GAP_WINDOW[0] <= now_hm < _GAP_WINDOW[1] and gap_pct is not None and gap_pct >= 2.0:
        gf_conditions = {
            "gap_ge_2pct": gap_pct >= 2.0,
            "drawdown_from_high_ge_1_2pct": (
                session_high is not None and session_high > 0
                and (session_high - current_price) / session_high * 100 >= 1.2
            ),
            "2_of_3_bearish": _bearish_count(df_1min, 3) >= 2,
            "return_3m_le_neg_0_8pct": (_pct_return(closes, 3) or 0) <= -0.8,
            "below_vwap": vwap is not None and current_price < vwap,
            "rsi_falling_or_below_65": rsi_now is not None and rsi_now < 65.0,
            "macd_hist_declining_2plus": _macd_hist_declining_streak(hist, 2),
        }
        met = sum(1 for v in gf_conditions.values() if v)
        vol_ratio = _volume_ratio(df_1min["volume"].astype(float), lookback=20)
        strong = met >= 6 and vol_ratio is not None and vol_ratio >= 1.5
        conditions["GAP_FAILURE"] = {"met": met, "total": 7, "conditions": gf_conditions, "strong": strong}
        if met >= 5:
            detail["gap_failure_strong"] = strong
            return {"phase": PHASE_GAP_FAILURE, "conditions": conditions, "detail": detail}

    # ── PANIC_SELL ───────────────────────────────────────────────────────────
    ret_5m = _pct_return(closes, 5)
    vol_ratio_20 = _volume_ratio(df_1min["volume"].astype(float), lookback=20)
    ps_conditions = {
        "return_5m_le_neg_2pct": ret_5m is not None and ret_5m <= -2.0,
        "volume_ge_2x": vol_ratio_20 is not None and vol_ratio_20 >= 2.0,
        "rsi_le_25": rsi_now is not None and rsi_now <= 25.0,
        "below_bollinger_lower": _norm01(_pct_return(closes, 20), 3.0) is not None and (_pct_return(closes, 20) or 0) < -3.0,
        "acceleration_down": bool(momentum.get("acceleration_confirmed_down")),
    }
    met = sum(1 for v in ps_conditions.values() if v)
    conditions["PANIC_SELL"] = {"met": met, "total": 5, "conditions": ps_conditions}
    if met >= 4:
        return {"phase": PHASE_PANIC_SELL, "conditions": conditions, "detail": detail}

    # ── CAPITULATION (명세에 명시적 조건 없음 — PANIC_SELL의 극단형으로 근사) ──
    cap_conditions = {
        "rsi_le_18": rsi_now is not None and rsi_now <= 18.0,
        "volume_ge_3x": vol_ratio_20 is not None and vol_ratio_20 >= 3.0,
        "return_5m_le_neg_3_5pct": ret_5m is not None and ret_5m <= -3.5,
    }
    if sum(1 for v in cap_conditions.values() if v) >= 3:
        conditions["CAPITULATION"] = {"met": 3, "total": 3, "conditions": cap_conditions}
        return {"phase": PHASE_CAPITULATION, "conditions": conditions, "detail": detail}

    # ── SELLING_EXHAUSTION ───────────────────────────────────────────────────
    last = df_1min.iloc[-1]
    lower_wick_long = _lower_wick_ratio(last) >= 0.4
    close_recovery = (last["close"] - last["low"]) / last["low"] * 100 >= 0.5 if last["low"] > 0 else False
    se_conditions = {
        "low_update_shrinking": _lower_lows_shrinking(df_1min, 5),
        "down_momentum_declining_3bars": bool(momentum.get("raw_acceleration") is not None and momentum.get("raw_acceleration", 0) > -0.05),
        "macd_hist_neg_but_rising": (hist is not None and len(hist) >= 3 and hist.iloc[-1] < 0 and _macd_hist_improving_streak(hist, 2)),
        "rsi_stabilizing_20_30": rsi_now is not None and 20.0 <= rsi_now <= 30.0,
        "long_lower_wick_or_close_recovery": lower_wick_long or close_recovery,
        "no_further_drop_after_heavy_volume": vol_ratio_20 is not None and vol_ratio_20 >= 1.5 and (ret_5m or 0) > -1.0,
    }
    met = sum(1 for v in se_conditions.values() if v)
    conditions["SELLING_EXHAUSTION"] = {"met": met, "total": 6, "conditions": se_conditions}
    if met >= 4:
        return {"phase": PHASE_SELLING_EXHAUSTION, "conditions": conditions, "detail": detail}

    # ── BASE_BUILDING ────────────────────────────────────────────────────────
    range_pct = None
    if atr is not None and atr > 0 and len(df_1min) >= 15:
        recent_range = df_1min["high"].tail(15).max() - df_1min["low"].tail(15).min()
        range_pct = recent_range / atr
    bb_conditions = {
        "range_contracted_vs_atr": range_pct is not None and range_pct < 2.0,
        "last_low_ge_prev_low": _higher_lows(df_1min, 3),
        "below_vwap_but_gap_shrinking": vwap is not None and current_price < vwap and abs(current_price - vwap) / vwap < 0.005,
        "rsi_ge_30": rsi_now is not None and rsi_now >= 30.0,
        "macd_hist_improving_3": _macd_hist_improving_streak(hist, 3),
        "volume_declining_stable": vol_ratio_20 is not None and 0.5 <= vol_ratio_20 <= 1.1,
    }
    met = sum(1 for v in bb_conditions.values() if v)
    conditions["BASE_BUILDING"] = {"met": met, "total": 6, "conditions": bb_conditions}
    if met >= 4:
        return {"phase": PHASE_BASE_BUILDING, "conditions": conditions, "detail": detail}

    # ── EARLY_REVERSAL_UP (점수화) ───────────────────────────────────────────
    ema5 = _ema(closes, 5)
    ema10 = _ema(closes, 10)
    er_score = 0.0
    er_detail = {}
    er_detail["lows_rising"] = _higher_lows(df_1min, 3)
    if er_detail["lows_rising"]:
        er_score += 20.0
    er_detail["macd_hist_improving_3"] = _macd_hist_improving_streak(hist, 3)
    if er_detail["macd_hist_improving_3"]:
        er_score += 15.0
    rebound_from_low = None
    if len(df_1min) >= 10:
        recent_low = df_1min["low"].tail(10).min()
        rebound_from_low = (current_price - recent_low) / recent_low * 100 if recent_low > 0 else None
    er_detail["rsi_recovered_30_to_38"] = rsi_now is not None and 30.0 <= rsi_now < 38.0 + 15
    if rsi_now is not None and rsi_now >= 38.0 and rebound_from_low is not None and rebound_from_low >= 0.8:
        er_score += 15.0
    er_detail["ema5_or_10_recovered"] = ema5 is not None and current_price > float(ema5.iloc[-1])
    if er_detail["ema5_or_10_recovered"] or (ema10 is not None and current_price > float(ema10.iloc[-1])):
        er_score += 10.0
    er_detail["volume_up_1_2x"] = vol_ratio_20 is not None and vol_ratio_20 >= 1.2
    if er_detail["volume_up_1_2x"]:
        er_score += 10.0
    er_detail["broke_prior_high"] = len(df_1min) >= 6 and current_price > df_1min["high"].iloc[-6:-1].max()
    if er_detail["broke_prior_high"]:
        er_score += 15.0
    vwap_gap_narrow = vwap is not None and abs(current_price - vwap) / vwap * 100 < 0.5
    if vwap_gap_narrow:
        er_score += 10.0
    downtrend_break = hist is not None and len(hist) >= 2 and hist.iloc[-1] > 0 and hist.iloc[-2] <= 0
    if downtrend_break:
        er_score += 15.0
    detail["early_reversal_score"] = round(er_score, 1)
    detail["early_reversal_detail"] = er_detail
    if er_score >= 55.0:
        conditions["EARLY_REVERSAL_UP"] = {"score": er_score, "detail": er_detail}
        return {"phase": PHASE_EARLY_REVERSAL_UP, "conditions": conditions, "detail": detail}

    # ── REVERSAL_CONFIRMED_UP ────────────────────────────────────────────────
    rc_conditions = {
        "above_vwap_or_reclaimed_2bars": vwap is not None and current_price > vwap,
        "broke_recent_3m_high": len(df_1min) >= 6 and current_price > df_1min["high"].iloc[-6:-1].max(),
        "two_3min_lows_rising": _higher_lows(df_1min, 6),
        "macd_golden_or_hist_positive": hist is not None and len(hist) >= 1 and hist.iloc[-1] > 0,
        "rsi_ge_45": rsi_now is not None and rsi_now >= 45.0,
        "volume_increasing": vol_ratio_20 is not None and vol_ratio_20 > 1.0,
    }
    met = sum(1 for v in rc_conditions.values() if v)
    conditions["REVERSAL_CONFIRMED_UP"] = {"met": met, "total": 6, "conditions": rc_conditions}
    if met >= 5:
        return {"phase": PHASE_REVERSAL_CONFIRMED_UP, "conditions": conditions, "detail": detail}

    # ── TREND_UP ─────────────────────────────────────────────────────────────
    ema20 = _ema(closes, 20)
    tu_conditions = {
        "price_above_vwap": vwap is not None and current_price > vwap,
        "ema_stack": (
            ema5 is not None and ema10 is not None and ema20 is not None
            and float(ema5.iloc[-1]) > float(ema10.iloc[-1]) > float(ema20.iloc[-1])
        ),
        "higher_highs": _higher_highs(df_1min, 6),
        "higher_lows": _higher_lows(df_1min, 6),
        "macd_hist_positive": hist is not None and len(hist) >= 1 and hist.iloc[-1] > 0,
        "rsi_50_to_72": rsi_now is not None and 50.0 <= rsi_now <= 72.0,
    }
    met = sum(1 for v in tu_conditions.values() if v)
    conditions["TREND_UP"] = {"met": met, "total": 6, "conditions": tu_conditions}
    if met >= 5:
        return {"phase": PHASE_TREND_UP, "conditions": conditions, "detail": detail}

    # ── CLIMAX_UP (명세에 명시적 조건 없음 — TREND_UP의 과열/둔화형으로 근사) ──
    if rsi_now is not None and rsi_now >= 80.0 and vwap is not None and (current_price - vwap) / vwap * 100 >= 2.0:
        conditions["CLIMAX_UP"] = {"rsi": rsi_now}
        return {"phase": PHASE_CLIMAX_UP, "conditions": conditions, "detail": detail}

    # ── DISTRIBUTION (명세에 명시적 조건 없음 — 고점권 모멘텀 둔화로 근사) ─────
    if (
        len(df_1min) >= 6 and current_price < df_1min["high"].iloc[-6:-1].max()
        and hist is not None and _macd_hist_declining_streak(hist, 2)
        and rsi_now is not None and rsi_now >= 60.0
    ):
        conditions["DISTRIBUTION"] = {"rsi": rsi_now}
        return {"phase": PHASE_DISTRIBUTION, "conditions": conditions, "detail": detail}

    # ── BREAKDOWN (명세에 명시적 조건 없음 — VWAP/EMA 하향이탈 + MACD 약세전환) ─
    if (
        vwap is not None and current_price < vwap
        and hist is not None and len(hist) >= 2 and hist.iloc[-1] < 0 and hist.iloc[-2] >= 0
        and _bearish_count(df_1min, 3) >= 2
    ):
        conditions["BREAKDOWN"] = {}
        return {"phase": PHASE_BREAKDOWN, "conditions": conditions, "detail": detail}

    # ── RANGE_NOISE / NO_TRADE ───────────────────────────────────────────────
    if range_pct is not None and range_pct < 1.5:
        return {"phase": PHASE_RANGE_NOISE, "conditions": conditions, "detail": detail}
    return {"phase": PHASE_NO_TRADE, "conditions": conditions, "detail": detail}


def default_cycle_state() -> dict:
    return {
        "current_phase": PHASE_NO_TRADE, "previous_phase": None, "phase_started_at": None,
        "candidate_phase": None, "candidate_count": 0, "transition_history": [],
        "last_entry_direction": None, "last_entry_time": None, "last_flip_time": None,
        "round_trip_count_today": 0, "consecutive_stop_losses": 0, "position_size_scale": 1.0,
        "halted_for_day": False, "_state_date": None,
    }


def classify_cycle_phase(
    df_1min: Optional[pd.DataFrame], now: datetime, gap_pct: Optional[float] = None,
    session_high: Optional[float] = None, session_low: Optional[float] = None,
    prior_close: Optional[float] = None, momentum: Optional[dict] = None,
    turning_point: Optional[dict] = None, state: Optional[dict] = None, df_3min: Optional[pd.DataFrame] = None,
) -> dict:
    """현재 사이클 phase를 판정한다(최소 2개 연속 1분봉 또는 1개 완성 3분봉 확인 필요).

    Returns
    -------
    dict: cycle_phase(확정된 phase), raw_phase(방금 계산된 미확정 phase), confirmed(bool,
    이번 호출에서 전환이 확정됐는지), previous_cycle_phase, phase_started_at,
    phase_duration_seconds, conditions, state(갱신된 state — 호출부가 다음 사이클에
    그대로 넘겨야 함)
    """
    state = dict(state) if state else default_cycle_state()
    today = now.strftime("%Y%m%d")
    if state.get("_state_date") != today:
        fresh = default_cycle_state()
        fresh["_state_date"] = today
        state = fresh

    if df_1min is None or len(df_1min) < 8:
        state["candidate_phase"] = None
        state["candidate_count"] = 0
        return {
            "cycle_phase": state["current_phase"], "raw_phase": PHASE_NO_TRADE, "confirmed": False,
            "previous_cycle_phase": state.get("previous_phase"), "phase_started_at": state.get("phase_started_at"),
            "phase_duration_seconds": 0, "conditions": {}, "state": state,
        }

    vwap = _vwap(df_1min)
    atr = calculate_atr(df_1min, 14)
    momentum = momentum if momentum is not None else calculate_momentum_acceleration(df_1min)
    turning_point = turning_point if turning_point is not None else calculate_turning_point_probability(df_1min, momentum=momentum)

    raw = _raw_phase(df_1min, now, gap_pct, session_high, session_low, vwap, atr, prior_close, momentum, turning_point)
    raw_phase = raw["phase"]

    confirmed_now = False
    # 1개 완성된 3분봉이 있으면 그 자체로 확인 완료(명세: "1개 완성된 3분봉 확인").
    has_completed_3min = df_3min is not None and not df_3min.empty

    if has_completed_3min or raw_phase == state.get("candidate_phase"):
        if not has_completed_3min:
            state["candidate_count"] = state.get("candidate_count", 0) + 1
        else:
            state["candidate_count"] = _CONFIRM_BARS_1MIN
    else:
        state["candidate_phase"] = raw_phase
        state["candidate_count"] = 1

    if state["candidate_count"] >= _CONFIRM_BARS_1MIN and raw_phase != state.get("current_phase"):
        state["previous_phase"] = state.get("current_phase")
        state["current_phase"] = raw_phase
        state["phase_started_at"] = now.isoformat()
        state.setdefault("transition_history", []).append({
            "at": now.isoformat(), "from": state["previous_phase"], "to": raw_phase,
        })
        state["transition_history"] = state["transition_history"][-50:]
        confirmed_now = True

    phase_duration = 0
    if state.get("phase_started_at"):
        try:
            phase_duration = int((now - datetime.fromisoformat(state["phase_started_at"])).total_seconds())
        except Exception:
            phase_duration = 0

    return {
        "cycle_phase": state["current_phase"], "raw_phase": raw_phase, "confirmed": confirmed_now,
        "previous_cycle_phase": state.get("previous_phase"), "phase_started_at": state.get("phase_started_at"),
        "phase_duration_seconds": phase_duration, "conditions": raw["conditions"], "detail": raw["detail"],
        "state": state,
    }


# =============================================================================
# Cycle Confidence / Entry Score
# =============================================================================

def calculate_cycle_confidence(phase_result: dict, momentum: dict, turning_point: dict) -> float:
    """phase 조건 충족률 + turning point confidence + momentum 데이터 가용성을 결합."""
    phase = phase_result.get("cycle_phase")
    conditions = phase_result.get("conditions", {})
    phase_conf = conditions.get(phase, {})
    if "met" in phase_conf and "total" in phase_conf and phase_conf["total"] > 0:
        condition_ratio = phase_conf["met"] / phase_conf["total"]
    elif "score" in phase_conf:
        condition_ratio = phase_conf["score"] / 100.0
    else:
        condition_ratio = 0.5

    tp_confidence = turning_point.get("confidence", 50.0) if turning_point else 50.0
    momentum_available = 80.0 if (momentum and momentum.get("available")) else 30.0

    confidence = condition_ratio * 100 * 0.5 + tp_confidence * 0.3 + momentum_available * 0.2
    return round(max(0.0, min(100.0, confidence)), 1)


def calculate_cycle_entry_score(
    phase_result: dict, momentum: dict, turning_point: dict, cycle_confidence: float, direction: str,
) -> dict:
    """direction: 'hynix'(상승 방향 진입) 또는 'inverse'(하락 방향 진입)."""
    phase = phase_result.get("cycle_phase")
    if direction == "inverse":
        base = turning_point.get("down_turn_probability_3m", 50.0)
        accel = momentum.get("momentum_acceleration_down", 50.0)
        phase_bonus = {
            PHASE_GAP_FAILURE: 15.0, PHASE_PANIC_SELL: -10.0, PHASE_CAPITULATION: -15.0,
            PHASE_BREAKDOWN: 10.0,
        }.get(phase, 0.0)
    else:
        base = turning_point.get("up_turn_probability_5m", 50.0)
        accel = momentum.get("momentum_acceleration_up", 50.0)
        phase_bonus = {
            PHASE_EARLY_REVERSAL_UP: 10.0, PHASE_REVERSAL_CONFIRMED_UP: 15.0, PHASE_TREND_UP: 10.0,
            PHASE_BASE_BUILDING: -5.0,
        }.get(phase, 0.0)

    score = round(max(0.0, min(100.0, base * 0.5 + accel * 0.3 + cycle_confidence * 0.2 + phase_bonus)), 1)
    return {f"cycle_entry_score_{direction}": score}


# =============================================================================
# 11. 실제 주문 시나리오 → 추천 액션
# =============================================================================

ACTION_BUY_HYNIX = "BUY_HYNIX"
ACTION_BUY_INVERSE = "BUY_INVERSE"
ACTION_ADD_INVERSE = "ADD_INVERSE"
ACTION_PARTIAL_SELL_INVERSE = "PARTIAL_SELL_INVERSE"
ACTION_EXIT_INVERSE = "EXIT_INVERSE"
ACTION_HOLD = "HOLD"

_SAME_DIRECTION_COOLDOWN_SEC = 5 * 60
_FLIP_COOLDOWN_SEC = 3 * 60
_MAX_ROUND_TRIPS_PER_DAY = 4


def _frequency_gate(state: dict, direction: str, now: datetime) -> Optional[str]:
    """명세 12절 거래 빈도 제한. 위반 시 차단 사유 문자열, 통과 시 None."""
    if state.get("halted_for_day"):
        return "3회 연속 손절 — 당일 자동매매 중단"
    if state.get("round_trip_count_today", 0) >= _MAX_ROUND_TRIPS_PER_DAY:
        return f"당일 최대 왕복 {_MAX_ROUND_TRIPS_PER_DAY}회 도달"

    last_dir = state.get("last_entry_direction")
    last_time_raw = state.get("last_entry_time")
    if last_time_raw:
        try:
            last_time = datetime.fromisoformat(last_time_raw)
            elapsed = (now - last_time).total_seconds()
            if last_dir == direction and elapsed < _SAME_DIRECTION_COOLDOWN_SEC:
                return f"동일 방향 재진입 쿨다운({_SAME_DIRECTION_COOLDOWN_SEC//60}분) 중"
            if last_dir is not None and last_dir != direction and elapsed < _FLIP_COOLDOWN_SEC:
                return f"방향 전환 쿨다운({_FLIP_COOLDOWN_SEC//60}분) 중"
        except Exception:
            pass
    return None


def decide_cycle_trade_action(
    phase_result: dict, momentum: dict, turning_point: dict, entry_scores: dict,
    inverse_pressure_score: Optional[float], position_state: dict, state: dict, now: datetime,
) -> dict:
    """명세 3~8절(진입 규칙) + 12절(빈도 제한)을 적용해 추천 행동을 계산한다.

    position_state: {"symbol": "000660"|"0197X0"|None, "position_pct": 0~100}
    state: classify_cycle_phase가 반환한 (그리고 이 함수가 갱신하는) 사이클 상태 dict.
    """
    state = dict(state)
    phase = phase_result.get("cycle_phase")
    reasons: list = []
    action = ACTION_HOLD
    recommended_symbol = None
    recommended_position_pct = position_state.get("position_pct", 0.0)
    blocking_reason = None

    inv_score = entry_scores.get("cycle_entry_score_inverse", 50.0)
    hy_score = entry_scores.get("cycle_entry_score_hynix", 50.0)
    down_3m = turning_point.get("down_turn_probability_3m", 50.0)
    down_5m = turning_point.get("down_turn_probability_5m", 50.0)
    up_5m = turning_point.get("up_turn_probability_5m", 50.0)
    early_reversal_score = phase_result.get("detail", {}).get("early_reversal_score", 0.0)
    held_symbol = position_state.get("symbol")
    held_pct = position_state.get("position_pct", 0.0)

    if phase == PHASE_GAP_FAILURE:
        gate = _frequency_gate(state, "inverse", now)
        if gate:
            blocking_reason = gate
        elif (inverse_pressure_score or 0) >= 55 and inv_score >= 62 and down_3m >= 65 and down_5m >= 60 and held_symbol != "0197X0":
            action = ACTION_BUY_INVERSE
            recommended_symbol = "0197X0"
            recommended_position_pct = 40.0
            reasons.append("GAP_FAILURE + inverse_pressure/entry_score/down_turn 조건 충족 — 인버스 40% 1차 진입")
        else:
            reasons.append("GAP_FAILURE이나 인버스 진입 조건 미충족 — 하이닉스 신규매수는 금지")

    elif phase in (PHASE_BREAKDOWN,) and held_symbol == "0197X0" and momentum.get("acceleration_confirmed_down") and held_pct < 70:
        gate = _frequency_gate(state, "inverse", now)
        if not gate:
            action = ACTION_ADD_INVERSE
            recommended_symbol = "0197X0"
            recommended_position_pct = 70.0
            reasons.append("하락 가속 확인 + 직전 저점 재이탈 — 인버스 비중 70%까지 확대")
        else:
            blocking_reason = gate

    elif phase == PHASE_SELLING_EXHAUSTION:
        reasons.append("Selling Exhaustion — 인버스 신규매수 금지, 하이닉스는 아직 매수하지 않음")
        if held_symbol == "0197X0" and 55.0 <= early_reversal_score <= 64.0:
            action = ACTION_PARTIAL_SELL_INVERSE
            recommended_symbol = "0197X0"
            recommended_position_pct = max(0.0, held_pct * 0.6)
            reasons.append("early_reversal_score 55~64 — 인버스 30~50% 부분익절")

    elif phase == PHASE_BASE_BUILDING:
        reasons.append("Base Building — 기본 HOLD, 인버스 추가매수 금지")
        if turning_point.get("up_turn_probability_5m", 0) >= 72 and held_symbol != "000660":
            gate = _frequency_gate(state, "hynix", now)
            if not gate:
                action = ACTION_BUY_HYNIX
                recommended_symbol = "000660"
                recommended_position_pct = 20.0
                reasons.append("turning_up_probability_5m>=72 — 하이닉스 시험매수 최대 20%")
            else:
                blocking_reason = gate

    elif phase == PHASE_EARLY_REVERSAL_UP:
        vwap_ok = phase_result.get("detail", {}).get("vwap") is not None and (
            phase_result.get("detail", {}).get("current_price", 0) >= phase_result.get("detail", {}).get("vwap", 0)
        )
        if early_reversal_score >= 75:
            action = ACTION_EXIT_INVERSE if held_symbol == "0197X0" else ACTION_BUY_HYNIX
            recommended_symbol = "000660"
            recommended_position_pct = 30.0 if not vwap_ok else 25.0
            reasons.append("early_reversal_score>=75 — 인버스 전량청산 + 하이닉스 20~30% 시험매수(VWAP 회복 전 최대 30%)")
        elif early_reversal_score >= 65 and held_symbol == "0197X0":
            action = ACTION_PARTIAL_SELL_INVERSE
            recommended_symbol = "0197X0"
            recommended_position_pct = held_pct * 0.5
            reasons.append("early_reversal_score>=65 — 인버스 50% 청산")

    elif phase == PHASE_REVERSAL_CONFIRMED_UP:
        gate = _frequency_gate(state, "hynix", now)
        trade_confidence = calculate_cycle_confidence(phase_result, momentum, turning_point)
        if gate:
            blocking_reason = gate
        else:
            action = ACTION_BUY_HYNIX
            recommended_symbol = "000660"
            if held_symbol == "000660" and held_pct > 0:
                recommended_position_pct = min(70.0 if trade_confidence >= 75 else 70.0, max(60.0, held_pct))
                reasons.append("Reversal Confirmed — 기존 시험매수를 60~70%까지 확대")
            else:
                recommended_position_pct = 50.0
                reasons.append("Reversal Confirmed — 미보유 상태에서 50% 진입")

    elif phase == PHASE_TREND_UP:
        reasons.append("Trend Up — 눌림목에서만 추가매수, 최대 80%(14:30 이후 신규 40%)")
        if held_symbol == "000660":
            action = ACTION_HOLD
            recommended_symbol = "000660"
            recommended_position_pct = held_pct

    else:
        reasons.append(f"{phase} — 명확한 진입 조건 없음, HOLD")

    if action == ACTION_HOLD:
        recommended_position_pct = held_pct

    if blocking_reason:
        action = ACTION_HOLD
        reasons.insert(0, blocking_reason)

    if action in (ACTION_BUY_INVERSE, ACTION_ADD_INVERSE):
        state["last_entry_direction"] = "inverse"
        state["last_entry_time"] = now.isoformat()
    elif action == ACTION_BUY_HYNIX:
        state["last_entry_direction"] = "hynix"
        state["last_entry_time"] = now.isoformat()

    return {
        "action": action, "recommended_symbol": recommended_symbol,
        "recommended_position_pct": round(recommended_position_pct, 1),
        "reasons": reasons, "blocking_reason": blocking_reason, "state": state,
    }


def register_stop_loss_outcome(state: dict, was_stop_loss: bool) -> dict:
    """손절 발생 시 연속 손절 카운터를 갱신하고 포지션 축소/당일중단을 적용한다(명세 12절)."""
    state = dict(state)
    if was_stop_loss:
        state["consecutive_stop_losses"] = state.get("consecutive_stop_losses", 0) + 1
    else:
        state["consecutive_stop_losses"] = 0

    n = state["consecutive_stop_losses"]
    state["position_size_scale"] = 0.5 if n == 2 else (1.0 if n < 2 else state.get("position_size_scale", 1.0))
    state["halted_for_day"] = bool(n >= 3)
    return state


def register_round_trip(state: dict) -> dict:
    state = dict(state)
    state["round_trip_count_today"] = state.get("round_trip_count_today", 0) + 1
    return state


# =============================================================================
# 16. 로그
# =============================================================================

def log_cycle_ai_prediction(record: dict, log_path: Optional[Path] = None) -> None:
    path = log_path or _LOG_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        row = {field: record.get(field) for field in CSV_LOG_FIELDS}
        row["timestamp"] = record.get("timestamp") or datetime.now().isoformat(timespec="seconds")
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_LOG_FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:
        logger.debug("[HynixCycleDetector] CSV 로그 기록 실패(무해): %s", exc)


# =============================================================================
# HynixCycleDetector — 오케스트레이션 클래스
# =============================================================================

class HynixCycleDetector:
    """명세 1절에 명시된 메서드명을 그대로 노출하는 얇은 오케스트레이터.
    각 메서드는 테스트 편의를 위해 모듈 레벨 순수 함수를 그대로 위임한다."""

    def classify_cycle_phase(self, *args, **kwargs) -> dict:
        return classify_cycle_phase(*args, **kwargs)

    def calculate_momentum_acceleration(self, *args, **kwargs) -> dict:
        return calculate_momentum_acceleration(*args, **kwargs)

    def calculate_turning_point_probability(self, *args, **kwargs) -> dict:
        return calculate_turning_point_probability(*args, **kwargs)

    def calculate_cycle_confidence(self, *args, **kwargs) -> float:
        return calculate_cycle_confidence(*args, **kwargs)

    def calculate_cycle_entry_score(self, *args, **kwargs) -> dict:
        return calculate_cycle_entry_score(*args, **kwargs)

    def decide_cycle_trade_action(self, *args, **kwargs) -> dict:
        return decide_cycle_trade_action(*args, **kwargs)

    def run(
        self, df_1min: Optional[pd.DataFrame], now: datetime, position_state: dict, state: dict,
        gap_pct: Optional[float] = None, session_high: Optional[float] = None, session_low: Optional[float] = None,
        prior_close: Optional[float] = None, inverse_pressure_score: Optional[float] = None,
        korea_semiconductor_confirmation_score: Optional[float] = None, effective_micron_score: Optional[float] = None,
        df_3min: Optional[pd.DataFrame] = None,
    ) -> dict:
        """전체 파이프라인 1회 실행(phase → momentum → turning point → confidence/entry → action)."""
        momentum = calculate_momentum_acceleration(df_1min)
        turning_point = calculate_turning_point_probability(
            df_1min, momentum=momentum, korea_semiconductor_confirmation_score=korea_semiconductor_confirmation_score,
            effective_micron_score=effective_micron_score,
        )
        phase_result = classify_cycle_phase(
            df_1min, now, gap_pct=gap_pct, session_high=session_high, session_low=session_low,
            prior_close=prior_close, momentum=momentum, turning_point=turning_point, state=state, df_3min=df_3min,
        )
        cycle_state = phase_result["state"]
        confidence = calculate_cycle_confidence(phase_result, momentum, turning_point)
        entry_scores = {
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, confidence, "inverse"),
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, confidence, "hynix"),
        }
        decision = decide_cycle_trade_action(
            phase_result, momentum, turning_point, entry_scores, inverse_pressure_score,
            position_state, cycle_state, now,
        )
        result = {
            "cycle_phase": phase_result["cycle_phase"], "previous_cycle_phase": phase_result["previous_cycle_phase"],
            "phase_started_at": phase_result["phase_started_at"], "phase_duration_seconds": phase_result["phase_duration_seconds"],
            "momentum": momentum, "turning_point": turning_point, "cycle_confidence": confidence,
            "entry_scores": entry_scores, "action": decision["action"], "recommended_symbol": decision["recommended_symbol"],
            "recommended_position_pct": decision["recommended_position_pct"], "reasons": decision["reasons"],
            "blocking_reason": decision["blocking_reason"], "state": decision["state"],
            "transition_history": cycle_state.get("transition_history", []),
        }
        return result
