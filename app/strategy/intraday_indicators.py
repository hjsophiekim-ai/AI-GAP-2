"""
intraday_indicators.py — 장중 기술적 지표 계산 (순수 함수, pandas 불필요)

각 캔들 딕셔너리 형식:
  {"time": "HHMMss", "open": float, "high": float, "low": float, "close": float, "volume": int}
캔들 리스트는 최신 순(newest first)으로 전달한다고 가정.
"""
import math


def calculate_vwap(candles: list[dict]) -> float:
    """당일 누적 VWAP. 데이터 없으면 0 반환."""
    if not candles:
        return 0.0
    total_tv = 0.0
    total_vol = 0
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3.0
        vol = c["volume"]
        total_tv += tp * vol
        total_vol += vol
    if total_vol == 0:
        return 0.0
    return total_tv / total_vol


def calculate_ema(candles: list[dict], window: int) -> list[float]:
    """close 가격의 EMA. 입력(최신순)을 역순으로 처리 후 다시 최신순 반환."""
    if not candles:
        return []
    closes = [c["close"] for c in reversed(candles)]  # oldest→newest
    k = 2.0 / (window + 1)
    ema_vals = []
    for i, price in enumerate(closes):
        if i == 0:
            ema_vals.append(price)
        else:
            ema_vals.append(price * k + ema_vals[-1] * (1 - k))
    return list(reversed(ema_vals))  # newest first


def calculate_rsi(candles: list[dict], period: int = 14) -> float:
    """최근 period개 캔들 기준 RSI. 데이터 부족 시 50.0 반환."""
    if len(candles) < period + 1:
        return 50.0
    # 최신순 → 오래된 순으로 period+1개 슬라이스
    recent = list(reversed(candles[:period + 1]))  # oldest→newest, length=period+1
    gains = []
    losses = []
    for i in range(1, len(recent)):
        diff = recent[i]["close"] - recent[i - 1]["close"]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema_from_list(values: list[float], window: int) -> list[float]:
    """값 리스트(oldest→newest)에서 EMA 계산, 같은 순서로 반환."""
    if not values:
        return []
    k = 2.0 / (window + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def calculate_macd(candles: list[dict]) -> dict:
    """MACD(12,26,9). 데이터 부족 시 {"macd": 0, "signal": 0, "hist": 0} 반환."""
    default = {"macd": 0.0, "signal": 0.0, "hist": 0.0}
    if len(candles) < 26:
        return default
    closes_old_first = list(reversed([c["close"] for c in candles]))
    ema12 = _ema_from_list(closes_old_first, 12)
    ema26 = _ema_from_list(closes_old_first, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    if len(macd_line) < 9:
        return default
    signal_line = _ema_from_list(macd_line, 9)
    macd_val = macd_line[-1]
    signal_val = signal_line[-1]
    return {
        "macd": round(macd_val, 4),
        "signal": round(signal_val, 4),
        "hist": round(macd_val - signal_val, 4),
    }


def resample_1m_to_3m(candles_1m: list[dict]) -> list[dict]:
    """1분봉(최신순)을 3분봉(최신순)으로 리샘플. 3개씩 그룹."""
    if not candles_1m:
        return []
    # 오래된 순으로 처리
    old_first = list(reversed(candles_1m))
    result = []
    for i in range(0, len(old_first) - 2, 3):
        group = old_first[i:i + 3]
        if len(group) < 3:
            break
        result.append({
            "time": group[0]["time"],
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"],
            "volume": sum(c["volume"] for c in group),
        })
    return list(reversed(result))  # newest first


def detect_bullish_reversal_1m(candles_1m: list[dict]) -> bool:
    """최신 1분봉 양봉 + 직전 1분봉 음봉이면 True (반전 신호)."""
    if len(candles_1m) < 2:
        return False
    latest = candles_1m[0]
    prev = candles_1m[1]
    latest_bullish = latest["close"] > latest["open"]
    prev_bearish = prev["close"] < prev["open"]
    return latest_bullish and prev_bearish


def detect_bearish_volume_candle_1m(candles_1m: list[dict]) -> bool:
    """최신 1분봉이 음봉이고 거래량이 직전 5개 평균의 1.5배 이상이면 True."""
    if len(candles_1m) < 6:
        return False
    latest = candles_1m[0]
    bearish = latest["close"] < latest["open"]
    if not bearish:
        return False
    avg_vol = sum(c["volume"] for c in candles_1m[1:6]) / 5.0
    if avg_vol == 0:
        return False
    return latest["volume"] >= avg_vol * 1.5


def calculate_intraday_high_pullback(current_price: float, intraday_high: float) -> float:
    """장중 고점 대비 현재가 괴리율(%). 음수 = 고점 아래."""
    if intraday_high <= 0:
        return 0.0
    return (current_price - intraday_high) / intraday_high * 100.0


def calculate_ema_slope(ema_values: list[float]) -> float:
    """EMA slope (newest - second newest). 양수 = 상승 추세."""
    if len(ema_values) < 2:
        return 0.0
    return ema_values[0] - ema_values[1]


def detect_williams_fractal_buy(candles: list[dict], lookback: int = 2) -> bool:
    """Williams Fractal 매수 신호 (하향 프랙탈 → 반전 상승 신호).

    최소 2*lookback+1개 캔들 필요. newest-first 입력.
    중심 캔들의 low가 주변 lookback개보다 낮으면 fractal bottom.
    """
    needed = 2 * lookback + 1
    if len(candles) < needed:
        return False
    old_first = list(reversed(candles[:needed]))
    center_idx = lookback
    center_low = old_first[center_idx]["low"]
    for i in range(needed):
        if i == center_idx:
            continue
        if old_first[i]["low"] <= center_low:
            return False
    return True


def calculate_volume_ratio(candles: list[dict], lookback: int = 3) -> float:
    """최신 1분봉 거래량 / 직전 lookback개 평균."""
    if len(candles) < lookback + 1:
        return 0.0
    latest_vol = candles[0]["volume"]
    prior_vols = [c["volume"] for c in candles[1:lookback + 1]]
    avg = sum(prior_vols) / len(prior_vols) if prior_vols else 0
    if avg == 0:
        return 0.0
    return latest_vol / avg
