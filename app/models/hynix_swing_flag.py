"""
hynix_swing_flag.py — SK하이닉스 스윙 매매 플래그 모듈.

가격예측 결과 + 마이크론 feature + 기술적 지표를 종합하여
단기 저점/고점 확률과 매수·매도·관망 플래그를 생성합니다.

실전 주문 기능과 절대 연결하지 않습니다.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
_SWING_WEIGHTS_PATH = _ROOT / "config" / "hynix_swing_weights.json"

# ── 플래그 상수 ───────────────────────────────────────────────────────────────
STRONG_BUY  = "STRONG_BUY"
BUY         = "BUY"
WAIT_BUY    = "WAIT_BUY"
NEUTRAL     = "NEUTRAL"
TAKE_PROFIT = "TAKE_PROFIT"
SELL        = "SELL"
STRONG_SELL = "STRONG_SELL"

FLAG_LABELS = {
    STRONG_BUY:  "강력매수",
    BUY:         "매수",
    WAIT_BUY:    "눌림목 대기",
    NEUTRAL:     "관망",
    TAKE_PROFIT: "분할매도",
    SELL:        "매도",
    STRONG_SELL: "강력매도",
}

FLAG_COLORS = {
    STRONG_BUY:  "#1a7a1a",
    BUY:         "#2ecc71",
    WAIT_BUY:    "#3498db",
    NEUTRAL:     "#95a5a6",
    TAKE_PROFIT: "#e67e22",
    SELL:        "#e74c3c",
    STRONG_SELL: "#8b0000",
}

# "분할매도"(TAKE_PROFIT)는 전량매도가 아니라 보유물량의 일부만 매도하라는
# 뜻이다 — SELL/STRONG_SELL로 갈수록 매도 비중이 커진다. UI에서 이 구분을
# 명확히 보여주기 위한 권장 매도비중.
SELL_RATIO_LABELS = {
    TAKE_PROFIT: "보유물량의 30~50%",
    SELL:        "보유물량의 50~70%",
    STRONG_SELL: "보유물량 전량(100%)",
}

# 매도 방향 플래그에서 신규 매수구간(buy_zone)이 None인 이유를 설명하는 문구.
BUY_ZONE_UNAVAILABLE_NOTE = (
    "현재 매도 신호이므로 신규 매수 구간을 제시하지 않습니다 "
    "(재매수는 플래그가 매수 방향으로 전환된 뒤 검토하세요)."
)


# ── 가중치 로드 ───────────────────────────────────────────────────────────────

def _load_swing_weights() -> dict:
    """config/hynix_swing_weights.json 로드. 없으면 기본값 반환."""
    defaults = {
        "micron_premarket": 0.30,
        "kospilab":         0.20,
        "tech_position":    0.25,
        "volume_momentum":  0.10,
        "semiconductor":    0.10,
        "currency_risk":    0.05,
    }
    try:
        if _SWING_WEIGHTS_PATH.exists():
            with open(_SWING_WEIGHTS_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("weights", defaults)
    except Exception:
        pass
    return defaults


# ── 기술적 지표 계산 (일봉 OHLCV에서) ────────────────────────────────────────

def compute_hynix_tech_indicators(df_daily: pd.DataFrame) -> dict:
    """
    일봉 OHLCV DataFrame에서 기술적 지표를 계산합니다.

    Parameters
    ----------
    df_daily : DataFrame
        columns: [datetime, open, high, low, close, volume]
        최소 60개 행 권장 (60일선 계산 위해)

    Returns
    -------
    dict
        rsi_14, macd, macd_signal_cross, ma5/20/60_position_pct,
        from_20d_high_pct, from_20d_low_pct, bollinger_pct,
        prev_candle_type, return_3d_pct, return_5d_pct,
        return_10d_pct, volume_change_pct
    """
    result: dict = {k: None for k in [
        "rsi_14", "macd", "macd_signal_cross",
        "ma5_position_pct", "ma20_position_pct", "ma60_position_pct",
        "from_20d_high_pct", "from_20d_low_pct", "bollinger_pct",
        "prev_candle_type", "return_3d_pct", "return_5d_pct",
        "return_10d_pct", "volume_change_pct", "atr_14_pct",
    ]}

    if df_daily is None or len(df_daily) < 5:
        return result

    df = df_daily.copy().sort_values("datetime").reset_index(drop=True)
    closes  = df["close"]
    highs   = df["high"]
    lows    = df["low"]
    opens   = df["open"]
    volumes = df["volume"]
    current = float(closes.iloc[-1])

    # RSI 14
    if len(df) >= 15:
        delta = closes.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0.0, float("inf"))
        rsi   = (100 - 100 / (1 + rs)).iloc[-1]
        result["rsi_14"] = round(float(rsi), 2)

    if len(df) >= 15:
        prev_close_series = closes.shift(1)
        true_range = pd.concat(
            [
                highs - lows,
                (highs - prev_close_series).abs(),
                (lows - prev_close_series).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr14 = float(true_range.rolling(14).mean().iloc[-1])
        if current > 0 and atr14 > 0:
            result["atr_14_pct"] = round(atr14 / current * 100, 4)

    # MACD (12, 26, 9)
    if len(df) >= 27:
        ema12  = closes.ewm(span=12, adjust=False).mean()
        ema26  = closes.ewm(span=26, adjust=False).mean()
        macd   = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        result["macd"] = round(float(macd.iloc[-1]), 4)
        # 골든크로스/데드크로스 (최근 2봉)
        if len(macd) >= 2:
            prev_diff = float(macd.iloc[-2]) - float(signal.iloc[-2])
            curr_diff = float(macd.iloc[-1]) - float(signal.iloc[-1])
            if prev_diff < 0 and curr_diff >= 0:
                result["macd_signal_cross"] = 1   # 골든크로스
            elif prev_diff > 0 and curr_diff <= 0:
                result["macd_signal_cross"] = -1  # 데드크로스
            else:
                result["macd_signal_cross"] = 0

    # 이동평균선 위치 (현재가 대비 %)
    for days, key in [(5, "ma5_position_pct"), (20, "ma20_position_pct"), (60, "ma60_position_pct")]:
        if len(df) >= days:
            ma  = float(closes.rolling(days).mean().iloc[-1])
            pct = (current / ma - 1) * 100 if ma > 0 else 0.0
            result[key] = round(pct, 2)

    # 최근 20일 고점/저점 대비 위치
    if len(df) >= 20:
        high20 = float(highs.tail(20).max())
        low20  = float(lows.tail(20).min())
        if high20 > 0:
            result["from_20d_high_pct"] = round((current / high20 - 1) * 100, 2)
        if low20 > 0 and current > 0:
            result["from_20d_low_pct"] = round((current / low20 - 1) * 100, 2)

    # 볼린저밴드 위치 (0=하단, 100=상단)
    if len(df) >= 20:
        ma20  = float(closes.rolling(20).mean().iloc[-1])
        std20 = float(closes.rolling(20).std().iloc[-1])
        upper = ma20 + 2 * std20
        lower = ma20 - 2 * std20
        if upper != lower:
            bb_pct = (current - lower) / (upper - lower) * 100
            result["bollinger_pct"] = round(float(bb_pct), 2)

    # 전일 캔들 타입 (장대양봉/장대음봉: 몸통이 전일 ATR의 1.5배 이상)
    if len(df) >= 3:
        prev_open  = float(opens.iloc[-2])
        prev_close = float(closes.iloc[-2])
        atr_approx = float((highs - lows).tail(5).mean())
        body = abs(prev_close - prev_open)
        if atr_approx > 0 and body > atr_approx * 1.5:
            result["prev_candle_type"] = 1 if prev_close > prev_open else -1
        else:
            result["prev_candle_type"] = 0

    # 수익률
    for days, key in [(3, "return_3d_pct"), (5, "return_5d_pct"), (10, "return_10d_pct")]:
        if len(df) > days:
            base = float(closes.iloc[-(days + 1)])
            if base > 0:
                result[key] = round((current / base - 1) * 100, 2)

    # 거래량 변화율 (최근 5일 vs 이전 5일)
    if len(df) >= 10:
        vol_recent = float(volumes.tail(5).mean())
        vol_prior  = float(volumes.iloc[-10:-5].mean())
        if vol_prior > 0:
            result["volume_change_pct"] = round((vol_recent / vol_prior - 1) * 100, 2)

    return result


# ── 스윙 플래그 평가 ──────────────────────────────────────────────────────────

def evaluate_swing_flag(
    micron_features: dict,
    kospilab_expected_return_pct: Optional[float] = None,
    tech_indicators: Optional[dict] = None,
    sox_return_pct: Optional[float] = None,
    nvda_return_pct: Optional[float] = None,
    qqq_return_pct: Optional[float] = None,
    usd_krw_change_pct: Optional[float] = None,
    hynix_current_price: Optional[float] = None,
    hynix_prev_close: Optional[float] = None,
    prediction: Optional[dict] = None,
) -> dict:
    """
    스윙 매매 플래그 평가.

    Parameters
    ----------
    micron_features              : compute_micron_features() 결과
    kospilab_expected_return_pct : 코스피랩 예상등락률 (%)
    tech_indicators              : compute_hynix_tech_indicators() 결과 또는 수동 입력 dict
    sox_return_pct               : SOX 등락률 (%)
    nvda_return_pct              : NVDA 등락률 (%)
    qqq_return_pct               : QQQ 등락률 (%)
    usd_krw_change_pct           : USD/KRW 변화율 (%)
    hynix_prev_close             : SK하이닉스 전일 종가 (원)
    prediction                   : predict_hynix() 결과 (선택)

    Returns
    -------
    dict
        swing_score, swing_flag, flag_label, flag_color,
        bottom_probability, top_probability,
        buy_zone_low, buy_zone_high, sell_zone_low, sell_zone_high,
        target_price, stop_loss_price, expected_holding_days,
        confidence_score, component_scores
    """
    weights = _load_swing_weights()
    ti      = tech_indicators or {}

    # ── 각 컴포넌트 신호 계산 (-1 ~ +1) ──────────────────────────────────────
    comp = {
        "micron_premarket": _micron_signal(micron_features),
        "kospilab":         _kospilab_signal(kospilab_expected_return_pct),
        "tech_position":    _tech_signal(ti),
        "volume_momentum":  _volume_signal(ti),
        "semiconductor":    _semi_signal(sox_return_pct, nvda_return_pct, qqq_return_pct),
        "currency_risk":    _currency_signal(usd_krw_change_pct),
    }

    # ── 가중 합산 → swing_score (0~100) ──────────────────────────────────────
    composite = sum(comp[k] * weights.get(k, 0.0) for k in comp)
    weight_sum = sum(weights.get(k, 0.0) for k in comp)
    if weight_sum > 0:
        composite /= weight_sum
    composite = max(-1.0, min(1.0, composite))

    swing_score = round(50.0 + composite * 50.0, 1)
    swing_score = max(0.0, min(100.0, swing_score))

    # ── 플래그 결정 ────────────────────────────────────────────────────────────
    flag = _score_to_flag(swing_score)

    # ── 저점/고점 확률 ─────────────────────────────────────────────────────────
    bottom_prob, top_prob = _compute_bottom_top_prob(swing_score, ti, micron_features)

    # ── 가격 구간 계산 ────────────────────────────────────────────────────────
    prices = _compute_price_zones(
        hynix_current_price=hynix_current_price,
        hynix_prev_close=hynix_prev_close,
        swing_score=swing_score,
        composite=composite,
        ti=ti,
        prediction=prediction,
    )

    # ── 신뢰도 ────────────────────────────────────────────────────────────────
    confidence = _compute_confidence(comp, ti, micron_features)

    # ── 구체적 매매 액션 텍스트 ───────────────────────────────────────────────
    hold_days = _holding_days(flag)
    action_texts = _generate_action_texts(flag, prices, bottom_prob, top_prob, hold_days)

    return {
        "swing_score":           swing_score,
        "swing_flag":            flag,
        "flag_label":            FLAG_LABELS.get(flag, flag),
        "flag_color":            FLAG_COLORS.get(flag, "#95a5a6"),
        "bottom_probability":    bottom_prob,
        "top_probability":       top_prob,
        "buy_zone_low":          prices["buy_zone_low"],
        "buy_zone_high":         prices["buy_zone_high"],
        "sell_zone_low":         prices["sell_zone_low"],
        "sell_zone_high":        prices["sell_zone_high"],
        "target_price":          prices["target_price"],
        "stop_loss_price":       prices["stop_loss_price"],
        "expected_holding_days": hold_days,
        "confidence_score":      confidence,
        "component_scores":      {k: round(v, 4) for k, v in comp.items()},
        "composite_signal":      round(composite, 4),
        "weights_used":          weights,
        # 구체적 액션 텍스트
        "action_text":           action_texts["action_text"],
        "buy_timing_text":       action_texts["buy_timing_text"],
        "sell_timing_text":      action_texts["sell_timing_text"],
        "bottom_window_text":    action_texts["bottom_window_text"],
        "top_window_text":       action_texts["top_window_text"],
        # "분할매도"가 전량매도가 아님을 명시하는 권장 매도비중 (매수 방향 플래그면 None)
        "sell_ratio_text":       action_texts["sell_ratio_text"],
        # 매도 방향 플래그에서 buy_zone이 None인 이유 설명 (해당 없으면 None)
        "buy_zone_note":         action_texts["buy_zone_note"],
    }


# ── 컴포넌트 신호 함수들 ──────────────────────────────────────────────────────

def _norm(val: Optional[float], scale: float) -> float:
    """값을 -1~+1로 정규화."""
    if val is None:
        return 0.0
    return max(-1.0, min(1.0, val / scale))


def _micron_signal(features: dict) -> float:
    """마이크론 프리마켓 방향 신호 (-1 ~ +1). 없는 서브-신호는 가중치에서 제외."""
    pm_ret   = features.get("micron_premarket_return")
    mom30    = features.get("micron_premarket_30m_momentum")
    mom60    = features.get("micron_premarket_60m_momentum")
    strength = features.get("micron_session_strength_score")
    af_ret   = features.get("micron_aftermarket_return")

    num, den = 0.0, 0.0
    for val, scale, w in [
        (pm_ret, 3.0, 0.40), (mom30, 2.0, 0.20),
        (mom60, 2.0, 0.15), (af_ret, 2.0, 0.10),
    ]:
        if val is not None:
            num += _norm(val, scale) * w
            den += w
    if strength is not None:
        num += (strength - 50) / 50 * 0.15
        den += 0.15
    return num / den if den > 0 else 0.0


def _kospilab_signal(kospilab_return: Optional[float]) -> float:
    """코스피랩 예상등락률 신호."""
    return _norm(kospilab_return, 2.0)


def _tech_signal(ti: dict) -> float:
    """기술적 지표 종합 신호 (-1 ~ +1)."""
    signals = []

    # RSI (과매도<30 → 강한 매수, 과매수>70 → 강한 매도)
    rsi = ti.get("rsi_14")
    if rsi is not None:
        if rsi <= 30:
            signals.append(((30 - rsi) / 30) * 0.25)    # 최대 +0.25
        elif rsi >= 70:
            signals.append(-((rsi - 70) / 30) * 0.25)   # 최소 -0.25
        else:
            signals.append(-(rsi - 50) / 20 * 0.10)     # 중간 구간 약한 신호

    # MACD 크로스
    cross = ti.get("macd_signal_cross")
    if cross is not None:
        signals.append(cross * 0.10)

    # MA 위치 (현재가가 이동평균 위면 +, 아래면 -)
    for key, w in [("ma5_position_pct", 0.07), ("ma20_position_pct", 0.08), ("ma60_position_pct", 0.05)]:
        val = ti.get(key)
        if val is not None:
            signals.append(_norm(val, 3.0) * w)

    # 20일 고점 대비 하락률 (많이 떨어졌으면 저점 기회)
    from_high = ti.get("from_20d_high_pct")
    if from_high is not None:
        # -10% 이하면 강한 매수, 0이면 중립
        signals.append(_norm(-from_high, 10.0) * 0.10)

    # 20일 저점 대비 상승률 (많이 올랐으면 고점 우려)
    from_low = ti.get("from_20d_low_pct")
    if from_low is not None:
        # 20% 이상 올랐으면 고점 신호
        signals.append(_norm(-from_low, 15.0) * 0.08)

    # 볼린저밴드 (0=하단매수, 100=상단매도)
    bb = ti.get("bollinger_pct")
    if bb is not None:
        signals.append(_norm(50 - bb, 50.0) * 0.10)

    # 전일 캔들
    candle = ti.get("prev_candle_type")
    if candle is not None:
        signals.append(candle * 0.07)

    return sum(signals) if signals else 0.0


def _volume_signal(ti: dict) -> float:
    """거래량/수급 모멘텀 신호."""
    vol_chg  = ti.get("volume_change_pct")
    return_3d = ti.get("return_3d_pct")

    signal = 0.0
    if vol_chg is not None:
        # 거래량 증가 + 상승은 강한 매수, 거래량 증가 + 하락은 매도
        if return_3d is not None and return_3d < 0 and vol_chg > 20:
            # 하락에 거래량 증가 → 투매 가능성 → 반등 준비
            signal += 0.3
        elif return_3d is not None and return_3d > 0 and vol_chg > 30:
            # 급등에 거래량 급증 → 과열 가능성
            signal -= 0.2
        else:
            signal += _norm(vol_chg, 30.0) * 0.3
    return signal


def _semi_signal(
    sox: Optional[float],
    nvda: Optional[float],
    qqq: Optional[float],
) -> float:
    """반도체 지수/NVDA/QQQ 종합 신호."""
    signals = []
    if sox is not None:
        signals.append(_norm(sox, 2.0) * 0.4)
    if nvda is not None:
        signals.append(_norm(nvda, 3.0) * 0.35)
    if qqq is not None:
        signals.append(_norm(qqq, 2.0) * 0.25)
    return sum(signals) if signals else 0.0


def _currency_signal(usd_krw: Optional[float]) -> float:
    """USD/KRW 환율 리스크 신호 (환율 상승 = 외국인 수급 약화 = 부정)."""
    if usd_krw is None:
        return 0.0
    return _norm(-usd_krw, 1.5)


# ── 저점/고점 확률 ────────────────────────────────────────────────────────────

def _compute_bottom_top_prob(
    swing_score: float,
    ti: dict,
    micron_features: dict,
) -> tuple[float, float]:
    """
    단기 저점/고점 확률 계산 (0~100).

    두 확률은 독립적으로 계산 (합이 100이 아니어도 됨).
    """
    rsi      = ti.get("rsi_14")
    from_high = ti.get("from_20d_high_pct")
    from_low  = ti.get("from_20d_low_pct")
    bb        = ti.get("bollinger_pct")
    strength  = micron_features.get("micron_session_strength_score")

    # 저점 확률: RSI 과매도, 20일 저점 근접, 볼린저 하단, 마이크론 강세
    bottom_signals = []
    if rsi is not None:
        bottom_signals.append(max(0, (30 - rsi) / 30) * 30)
    if from_high is not None:
        bottom_signals.append(max(0, -from_high / 15) * 20)
    if bb is not None:
        bottom_signals.append(max(0, (20 - bb) / 20) * 20)
    if strength is not None:
        bottom_signals.append(max(0, (strength - 50) / 50) * 20)
    if from_low is not None:
        bottom_signals.append(max(0, (50 - from_low) / 50) * 10)
    bottom_prob = round(min(sum(bottom_signals), 95.0), 1)

    # 고점 확률: RSI 과매수, 20일 고점 근접, 볼린저 상단, 마이크론 약세
    top_signals = []
    if rsi is not None:
        top_signals.append(max(0, (rsi - 70) / 30) * 30)
    if from_high is not None:
        top_signals.append(max(0, (-from_high - 0) / 5) * 20)
    if bb is not None:
        top_signals.append(max(0, (bb - 80) / 20) * 20)
    if strength is not None:
        top_signals.append(max(0, (50 - strength) / 50) * 20)
    if from_low is not None:
        top_signals.append(max(0, from_low / 20) * 10)
    top_prob = round(min(sum(top_signals), 95.0), 1)

    return bottom_prob, top_prob


# ── 가격 구간 계산 ────────────────────────────────────────────────────────────

def _compute_price_zones(
    hynix_current_price: Optional[float] = None,
    hynix_prev_close: Optional[float] = None,
    swing_score: float = 50.0,
    composite: float = 0.0,
    ti: Optional[dict] = None,
    prediction: Optional[dict] = None,
) -> dict:
    """목표가, 손절가, 매수/매도 구간 계산.

    매수 방향: target > base > stop_loss (stop_loss < target 보장)
    매도 방향(long-only): target ≥ base > stop_loss (stop_loss < target 보장)
    """
    empty = {
        "buy_zone_low": None, "buy_zone_high": None,
        "sell_zone_low": None, "sell_zone_high": None,
        "target_price": None, "stop_loss_price": None,
    }

    ti = ti or {}
    base = hynix_current_price
    if not base and prediction:
        base = prediction.get("current_price") or prediction.get("base_price") or prediction.get("today_close_expected")
    if not base:
        base = hynix_prev_close
    if not base or base <= 0:
        return empty

    # 변동성 추정 (기술적 지표 기반)
    atr_pct = ti.get("atr_14_pct")
    from_high = abs(ti.get("from_20d_high_pct") or 5.0)
    volatility = max(float(atr_pct) if atr_pct is not None else from_high * 0.3, 1.5)
    volatility = min(volatility, 8.0)

    def _r(p: Optional[float]) -> Optional[int]:
        if p is None or p <= 0:
            return None
        unit = 500 if p < 500_000 else 1_000
        return int(round(p / unit) * unit)

    if composite >= 0:  # 매수 방향: target > base > stop_loss
        risk_pct   = min(max(volatility * 1.2, 1.5), 7.0)
        reward_pct = risk_pct * 1.8
        buy_low    = base * (1 - volatility * 0.005)
        buy_high   = base * (1 + volatility * 0.003)
        target     = base * (1 + reward_pct / 100)   # base 위 → target > base
        stop_loss  = base * (1 - risk_pct / 100)     # base 아래 → stop_loss < base
        sell_low   = target * 0.99
        sell_high  = target * 1.01
    else:  # 매도 방향 (long-only 투자자 기준): target ≥ base > stop_loss
        risk_pct   = min(max(volatility * 1.2, 1.5), 7.0)
        # 목표가: 현재가 부근 또는 소폭 위 (리바운드 시 청산 목표)
        reward_pct = risk_pct * 0.5
        sell_low   = base * (1 - volatility * 0.005)   # 매도 구간 하단 (현재가 아래)
        sell_high  = base * (1 + volatility * 0.003)   # 매도 구간 상단 (현재가 위)
        target     = base * (1 + reward_pct / 100)     # 목표가 = 현재가 위 (리바운드 청산)
        stop_loss  = base * (1 - risk_pct / 100)       # 손절가 = 현재가 아래 (강제 손절)
        # 매도 방향에서 재매수 구간은 제공하지 않음
        buy_low    = None
        buy_high   = None

    # 논리 검증: stop_loss < target 보장
    if target is not None and stop_loss is not None and stop_loss >= target:
        # 계산 오류 안전망: stop_loss를 target 아래로 강제 조정
        stop_loss = target * 0.93

    return {
        "buy_zone_low":    _r(buy_low),
        "buy_zone_high":   _r(buy_high),
        "sell_zone_low":   _r(sell_low),
        "sell_zone_high":  _r(sell_high),
        "target_price":    _r(target),
        "stop_loss_price": _r(stop_loss),
    }


# ── 구체적 매매 액션 텍스트 ──────────────────────────────────────────────────

def _generate_action_texts(
    flag: str,
    prices: dict,
    bottom_prob: float,
    top_prob: float,
    hold_days: Optional[int],
) -> dict:
    """플래그와 가격 구간에서 구체적인 한글 액션 텍스트를 생성합니다."""
    buy_low  = prices.get("buy_zone_low")
    buy_high = prices.get("buy_zone_high")
    target   = prices.get("target_price")
    stop     = prices.get("stop_loss_price")

    def _p(v: Optional[int]) -> str:
        return f"{v:,}원" if v else "—"

    def _days(n: Optional[int]) -> str:
        return f"{n}거래일 이내" if n else "단기"

    if flag == STRONG_BUY:
        action_text        = f"즉시 분할 매수 진입 — {_p(buy_low)}~{_p(buy_high)} 구간"
        buy_timing_text    = f"{_p(buy_low)} ~ {_p(buy_high)} 구간에서 분할 매수 진입"
        sell_timing_text   = f"{_p(target)} 부근에서 분할 매도 (손절: {_p(stop)})"
        bottom_window_text = f"{_days(hold_days)} {_p(buy_low)} 부근에서 단기 저점 가능성 (확률 {bottom_prob:.0f}%)"
        top_window_text    = f"{_days((hold_days or 3) * 2)} {_p(target)} 부근에서 단기 고점 가능성"
    elif flag == BUY:
        action_text        = f"분할 매수 — {_p(buy_low)}~{_p(buy_high)} 구간"
        buy_timing_text    = f"{_p(buy_low)} ~ {_p(buy_high)} 구간에서 분할 매수"
        sell_timing_text   = f"{_p(target)} 부근에서 분할 매도 (손절: {_p(stop)})"
        bottom_window_text = f"{_days(hold_days)} {_p(buy_low)} 부근에서 저점 매수 기회"
        top_window_text    = f"{_days((hold_days or 5) + 3)} {_p(target)} 부근에서 목표가 도달 예상"
    elif flag == WAIT_BUY:
        action_text        = f"눌림목 대기 — {_p(buy_low)} 이하로 하락 시 매수 검토"
        buy_timing_text    = f"{_p(buy_low)} 이하로 추가 하락 시 매수 진입"
        sell_timing_text   = f"{_p(target)} 부근에서 분할 매도"
        bottom_window_text = f"{_days(hold_days)} 추가 눌림목 후 {_p(buy_low)} 부근 저점 가능성"
        top_window_text    = None
    elif flag == NEUTRAL:
        action_text        = "방향성 불확실 — 관망 유지"
        buy_timing_text    = None
        sell_timing_text   = None
        bottom_window_text = None
        top_window_text    = None
    elif flag == TAKE_PROFIT:
        ratio               = SELL_RATIO_LABELS[TAKE_PROFIT]
        action_text         = f"분할 매도 검토 — {ratio} 매도, {_p(target)} 부근 목표가 도달"
        buy_timing_text     = None
        sell_timing_text    = f"{ratio}를 {_p(target)} 부근에서 매도 (전량 매도 아님 — 나머지는 보유)"
        bottom_window_text  = None
        top_window_text     = f"{_days(hold_days)} 내 {_p(target)} 부근에서 단기 고점 가능성 (확률 {top_prob:.0f}%)"
    elif flag == SELL:
        ratio               = SELL_RATIO_LABELS[SELL]
        action_text         = f"{ratio} 매도 후 현금 비중 확대 — 손절라인: {_p(stop)}"
        buy_timing_text     = None
        sell_timing_text    = f"{ratio} 매도, {_p(target)} 이하로 추가 하락 시 나머지도 손절 고려"
        bottom_window_text  = None
        top_window_text     = f"{_days(hold_days)} 내 추가 하락 가능성 (확률 {top_prob:.0f}%)"
    else:  # STRONG_SELL
        ratio               = SELL_RATIO_LABELS[STRONG_SELL]
        action_text         = f"즉시 손절/{ratio} 매도 — 손절라인: {_p(stop)}"
        buy_timing_text     = None
        sell_timing_text    = f"{ratio} 즉시 매도, 손절라인 {_p(stop)} 엄수"
        bottom_window_text  = None
        top_window_text     = f"단기 급락 주의 (확률 {top_prob:.0f}%)"

    sell_ratio_text = SELL_RATIO_LABELS.get(flag)
    buy_zone_note = BUY_ZONE_UNAVAILABLE_NOTE if (buy_low is None and flag in SELL_RATIO_LABELS) else None

    return {
        "action_text":        action_text,
        "buy_timing_text":    buy_timing_text,
        "sell_timing_text":   sell_timing_text,
        "bottom_window_text": bottom_window_text,
        "top_window_text":    top_window_text,
        "sell_ratio_text":    sell_ratio_text,
        "buy_zone_note":      buy_zone_note,
    }


# ── 플래그 / 보유기간 ─────────────────────────────────────────────────────────

def _score_to_flag(score: float) -> str:
    """swing_score → 플래그 문자열."""
    if score >= 85:  return STRONG_BUY
    if score >= 70:  return BUY
    if score >= 55:  return WAIT_BUY
    if score >= 45:  return NEUTRAL
    if score >= 30:  return TAKE_PROFIT
    if score >= 15:  return SELL
    return STRONG_SELL


def _holding_days(flag: str) -> Optional[int]:
    """플래그별 예상 보유기간 (거래일)."""
    mapping = {
        STRONG_BUY:  3,
        BUY:         5,
        WAIT_BUY:    7,
        NEUTRAL:     None,
        TAKE_PROFIT: 2,
        SELL:        3,
        STRONG_SELL: 1,
    }
    return mapping.get(flag)


# ── 신뢰도 ───────────────────────────────────────────────────────────────────

def _compute_confidence(
    component_scores: dict,
    ti: dict,
    micron_features: dict,
) -> float:
    """
    신뢰도 점수 (0~100).
    데이터 가용성 + 신호 방향 일치도 + 마이크론 강도로 계산.
    """
    # 데이터 가용성 (최대 40점)
    available = sum(1 for v in component_scores.values() if v != 0.0)
    data_score = available / max(len(component_scores), 1) * 40

    # 신호 방향 일치도 (최대 30점)
    pos = sum(1 for v in component_scores.values() if v > 0.05)
    neg = sum(1 for v in component_scores.values() if v < -0.05)
    consensus = abs(pos - neg) / max(len(component_scores), 1)
    cons_score = consensus * 30

    # 기술적 지표 충분성 (최대 20점)
    tech_keys = ["rsi_14", "macd", "ma20_position_pct", "from_20d_high_pct", "bollinger_pct"]
    tech_avail = sum(1 for k in tech_keys if ti.get(k) is not None)
    tech_score = tech_avail / len(tech_keys) * 20

    # 마이크론 강도 (최대 10점)
    strength = micron_features.get("micron_session_strength_score") or 50.0
    str_score = abs(strength - 50) / 50 * 10

    return round(min(data_score + cons_score + tech_score + str_score, 100.0), 1)
