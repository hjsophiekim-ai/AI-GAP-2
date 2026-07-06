"""Rule-based SK Hynix return model and current-price anchored price converter."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.data.market_data_validator import DataValidationError, validate_prediction_prices

ROOT = Path(__file__).resolve().parent.parent.parent
WEIGHTS_PATH = ROOT / "config" / "hynix_model_weights.json"
MODEL_VERSION = "rule_based_v1.1_current_anchor"


def _load_weights() -> dict:
    defaults = {
        "micron_premarket_aftermarket": 0.45,
        "kospilab_expected_price": 0.25,
        "sox_index": 0.10,
        "nvda": 0.07,
        "qqq_nasdaq_futures": 0.05,
        "usd_krw": 0.03,
        "hynix_momentum_volume": 0.05,
    }
    try:
        if WEIGHTS_PATH.exists():
            return json.loads(WEIGHTS_PATH.read_text(encoding="utf-8")).get("weights", defaults)
    except Exception:
        pass
    return defaults


def predict_hynix(
    micron_features: dict,
    kospilab_expected_price: Optional[float] = None,
    kospilab_expected_return_pct: Optional[float] = None,
    sox_return_pct: Optional[float] = None,
    nvda_return_pct: Optional[float] = None,
    qqq_return_pct: Optional[float] = None,
    usd_krw_change_pct: Optional[float] = None,
    hynix_current_price: Optional[float] = None,
    hynix_prev_close: Optional[float] = None,
    hynix_prev_return_pct: Optional[float] = None,
    hynix_return_3d_pct: Optional[float] = None,
    hynix_return_5d_pct: Optional[float] = None,
    hynix_return_10d_pct: Optional[float] = None,
    hynix_volume_change_pct: Optional[float] = None,
) -> dict:
    """Predict returns first, then convert to prices from SK Hynix current_price."""
    weights = _load_weights()
    signals = _build_signals(
        micron_features=micron_features,
        kospilab_return=kospilab_expected_return_pct,
        sox_return=sox_return_pct,
        nvda_return=nvda_return_pct,
        qqq_return=qqq_return_pct,
        usd_krw_change=usd_krw_change_pct,
        hynix_prev_return=hynix_prev_return_pct,
        hynix_return_3d=hynix_return_3d_pct,
        hynix_return_5d=hynix_return_5d_pct,
        hynix_volume_change=hynix_volume_change_pct,
    )
    composite = _weighted_composite(signals, weights)
    today_return_pct = _estimate_today_return(
        composite=composite,
        kospilab_return=kospilab_expected_return_pct,
        micron_strength=micron_features.get("micron_session_strength_score"),
    )
    tomorrow_return = _estimate_future_return(composite, days=1)
    day3_return = _estimate_future_return(composite, days=3)
    up_prob, down_prob = _estimate_probabilities(composite)
    confidence = _estimate_confidence(signals, micron_features)

    base_price, base_source = _resolve_price_anchor(hynix_current_price, hynix_prev_close)
    today_prices = _estimate_price_range(base_price, today_return_pct, composite)
    two_week = _estimate_two_week_range(base_price, composite)

    prediction = {
        "today_open_expected": today_prices["open"],
        "today_high_expected": today_prices["high"],
        "today_low_expected": today_prices["low"],
        "today_close_expected": today_prices["close"],
        "today_return_pct": round(today_return_pct, 2),
        "tomorrow_return_pct": round(tomorrow_return, 2),
        "day3_return_pct": round(day3_return, 2),
        "two_week_high_date": two_week["high_date"],
        "two_week_high_price": two_week["high_price"],
        "two_week_high_prob": two_week["high_prob"],
        "two_week_low_date": two_week["low_date"],
        "two_week_low_price": two_week["low_price"],
        "two_week_low_prob": two_week["low_prob"],
        "up_probability": round(up_prob, 1),
        "down_probability": round(down_prob, 1),
        "confidence_score": round(confidence, 1),
        "predicted_at": datetime.now().isoformat(),
        "model_version": MODEL_VERSION,
        "weights_used": weights,
        "composite_signal": round(composite, 4),
        "signals": signals,
        "current_price": float(hynix_current_price) if hynix_current_price else None,
        "base_price": base_price if base_price > 0 else None,
        "base_price_source": base_source,
        "hynix_prev_close": hynix_prev_close,
    }
    if hynix_current_price:
        validate_prediction_prices(prediction, float(hynix_current_price))
    return prediction


def _resolve_price_anchor(current_price: Optional[float], prev_close: Optional[float]) -> tuple[float, Optional[str]]:
    if current_price is not None and current_price > 0:
        return float(current_price), "current_price"
    if prev_close is not None and prev_close > 0:
        return float(prev_close), "prev_close"
    return 0.0, None


def _norm(value: Optional[float], scale: float = 3.0) -> float:
    if value is None:
        return 0.0
    return max(-1.0, min(1.0, float(value) / scale))


def _build_signals(
    micron_features: dict,
    kospilab_return: Optional[float],
    sox_return: Optional[float],
    nvda_return: Optional[float],
    qqq_return: Optional[float],
    usd_krw_change: Optional[float],
    hynix_prev_return: Optional[float],
    hynix_return_3d: Optional[float],
    hynix_return_5d: Optional[float],
    hynix_volume_change: Optional[float],
) -> dict:
    pm_ret = micron_features.get("micron_premarket_return")
    pm_mom30 = micron_features.get("micron_premarket_30m_momentum")
    pm_mom60 = micron_features.get("micron_premarket_60m_momentum")
    strength = micron_features.get("micron_session_strength_score")
    after_ret = micron_features.get("micron_aftermarket_return")

    mu_num, mu_den = 0.0, 0.0
    for value, scale, weight in [
        (pm_ret, 3.0, 0.40),
        (pm_mom30, 2.0, 0.20),
        (pm_mom60, 2.0, 0.15),
        (after_ret, 2.0, 0.10),
    ]:
        if value is not None:
            mu_num += _norm(value, scale) * weight
            mu_den += weight
    if strength is not None:
        mu_num += (float(strength) - 50.0) / 50.0 * 0.15
        mu_den += 0.15
    micron_signal = mu_num / mu_den if mu_den > 0 else None

    hy_num, hy_den = 0.0, 0.0
    for value, scale, weight in [
        (hynix_prev_return, 3.0, 0.30),
        (hynix_return_3d, 5.0, 0.30),
        (hynix_return_5d, 7.0, 0.20),
        (hynix_volume_change, 30.0, 0.20),
    ]:
        if value is not None:
            hy_num += _norm(value, scale) * weight
            hy_den += weight
    hynix_self = hy_num / hy_den if hy_den > 0 else None

    return {
        "micron": round(micron_signal, 4) if micron_signal is not None else None,
        "kospilab": round(_norm(kospilab_return, 2.0), 4) if kospilab_return is not None else None,
        "sox": round(_norm(sox_return, 2.0), 4) if sox_return is not None else None,
        "nvda": round(_norm(nvda_return, 3.0), 4) if nvda_return is not None else None,
        "qqq": round(_norm(qqq_return, 2.0), 4) if qqq_return is not None else None,
        "usd_krw": round(_norm(-usd_krw_change, 1.5), 4) if usd_krw_change is not None else None,
        "hynix_self": round(hynix_self, 4) if hynix_self is not None else None,
    }


def _weighted_composite(signals: dict, weights: dict) -> float:
    mapping = {
        "micron": "micron_premarket_aftermarket",
        "kospilab": "kospilab_expected_price",
        "sox": "sox_index",
        "nvda": "nvda",
        "qqq": "qqq_nasdaq_futures",
        "usd_krw": "usd_krw",
        "hynix_self": "hynix_momentum_volume",
    }
    total, weight_sum = 0.0, 0.0
    for signal_name, weight_name in mapping.items():
        value = signals.get(signal_name)
        if value is not None:
            weight = float(weights.get(weight_name, 0.0))
            total += float(value) * weight
            weight_sum += weight
    return total / weight_sum if weight_sum > 1e-9 else 0.0


def _estimate_today_return(composite: float, kospilab_return: Optional[float], micron_strength: Optional[float]) -> float:
    result = composite * 5.0
    if kospilab_return is not None:
        result = result * 0.70 + float(kospilab_return) * 0.30
    if micron_strength is not None:
        result += (float(micron_strength) - 50.0) / 50.0 * 0.5
    return round(result, 4)


def _estimate_price_range(base_price: float, today_return_pct: float, composite: float) -> dict:
    if base_price <= 0:
        return {"open": None, "high": None, "low": None, "close": None}
    volatility = abs(composite) * 2.0 + 1.5
    close = base_price * (1 + today_return_pct / 100)
    open_price = base_price * (1 + today_return_pct * 0.4 / 100)
    if composite >= 0:
        high = close * (1 + volatility / 200)
        low = open_price * (1 - volatility / 300)
    else:
        high = open_price * (1 + volatility / 300)
        low = close * (1 - volatility / 200)
    return {
        "open": _round_krx(open_price),
        "high": _round_krx(high),
        "low": _round_krx(low),
        "close": _round_krx(close),
    }


def _round_krx(price: float) -> int:
    if price <= 0:
        return 0
    if price < 5_000:
        unit = 5
    elif price < 10_000:
        unit = 10
    elif price < 50_000:
        unit = 50
    elif price < 100_000:
        unit = 100
    elif price < 500_000:
        unit = 500
    else:
        unit = 1_000
    return int(round(price / unit) * unit)


def _estimate_future_return(composite: float, days: int) -> float:
    return round(composite * 4.0 * (0.6 ** (days - 1)), 4)


def _estimate_two_week_range(base_price: float, composite: float) -> dict:
    empty = {"high_date": None, "high_price": None, "high_prob": None, "low_date": None, "low_price": None, "low_prob": None}
    if base_price <= 0:
        return empty
    today = datetime.now()
    high_days, low_days = (5, 10) if composite > 0.3 else ((8, 3) if composite > 0 else (3, 8))
    magnitude = min(abs(composite) * 10 + 5, 15)
    if composite >= 0:
        high_price = base_price * (1 + magnitude / 100)
        low_price = base_price * (1 - magnitude / 2 / 100)
    else:
        high_price = base_price * (1 + magnitude / 2 / 100)
        low_price = base_price * (1 - magnitude / 100)
    abs_composite = abs(composite)
    return {
        "high_date": _add_trading_days(today, high_days).strftime("%Y-%m-%d"),
        "high_price": _round_krx(high_price),
        "high_prob": round(min(0.30 + abs_composite * 0.40, 0.85), 2),
        "low_date": _add_trading_days(today, low_days).strftime("%Y-%m-%d"),
        "low_price": _round_krx(low_price),
        "low_prob": round(min(0.30 + abs_composite * 0.30, 0.75), 2),
    }


def _add_trading_days(start: datetime, n: int) -> datetime:
    day = start
    added = 0
    while added < n:
        day += timedelta(days=1)
        if day.weekday() < 5:
            added += 1
    return day


def _estimate_probabilities(composite: float) -> tuple[float, float]:
    up = 100 / (1 + math.exp(-composite * 4))
    return round(up, 1), round(100 - up, 1)


def _estimate_confidence(signals: dict, micron_features: dict) -> float:
    available = sum(1 for value in signals.values() if value is not None)
    data_score = available / max(len(signals), 1) * 40
    positive = sum(1 for value in signals.values() if value is not None and value > 0.05)
    negative = sum(1 for value in signals.values() if value is not None and value < -0.05)
    consensus_score = abs(positive - negative) / max(len(signals), 1) * 30
    strength = micron_features.get("micron_session_strength_score")
    strength_score = abs(float(strength) - 50.0) / 50.0 * 30 if strength is not None else 0.0
    return round(min(data_score + consensus_score + strength_score, 100.0), 1)
