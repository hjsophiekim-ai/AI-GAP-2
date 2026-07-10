"""micron_proxy_prediction.py — Micron(MU) Proxy Prediction Engine.

목표: MU가 거래 중이면 실제 MU 데이터를 쓰고, MU 데이터가 없거나(장 마감/야간)
또는 오래됐으면(stale) 마지막 가격 하나의 가중치를 깎는 대신 SOX/Nasdaq
선물 proxy, 미국 반도체 basket, 한국 반도체 확인 신호를 결합해 "지금 MU가
거래된다면 어느 방향일 가능성이 높은가"를 추정한다.

용어 주의: 이 저장소/제공 데이터에는 CME SOX 선물이나 "Micron futures" 같은
단일종목 선물 상품이 없다. "SOX futures score"/"Nasdaq futures score"는
실제 선물 체결가가 아니라 ETF/지수 proxy(SOXX/SOX, NQ=F/QQQ)에 기반한
근사치다. UI/로그에 노출할 때도 반드시 "SOX semiconductor futures proxy",
"Nasdaq futures proxy"로 표기하고 "Micron futures"라고 표기하지 않는다.

이 모듈의 calculate_*/detect_*/validate_*/explain_* 함수들은 순수 함수다
(네트워크 호출 없음, 이미 수집된 값을 인자로 받는다) — 테스트가 쉽고, 실시간
수집(collect_*)과 스코어링 로직(calculate_*)을 분리해 어떤 이유로도 예외를
던지지 않는다(모든 실패는 결과 dict의 warnings로 표현). 실시간 수집은
MicronProxyPredictionEngine.collect_and_predict()에서만 일어난다.
"""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger

ROOT = Path(__file__).resolve().parent.parent.parent

_MU_1MIN_CSV = ROOT / "data" / "micron" / "MU_1min.csv"
_MU_3MIN_CSV = ROOT / "data" / "micron" / "MU_3min.csv"
_LOG_PATH = ROOT / "data" / "logs" / "micron_proxy_prediction_log.csv"
_WEIGHT_RECO_PATH = ROOT / "data" / "state" / "micron_proxy_weight_recommendation.json"

MODEL_VERSION = "micron_proxy_prediction_v1"

# ── 세션 상수 ──────────────────────────────────────────────────────────────
SESSION_REGULAR = "REGULAR"
SESSION_PREMARKET = "PREMARKET"
SESSION_AFTER_HOURS = "AFTER_HOURS"
SESSION_OVERNIGHT_ATS = "OVERNIGHT_ATS"
SESSION_CLOSED = "CLOSED"
SESSION_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
SESSION_STALE_DATA = "STALE_DATA"

# 명세 2절 — 봉 주기별 기본 freshness 기준(분)
FRESHNESS_THRESHOLD_MINUTES = {"1min": 5.0, "3min": 10.0, "5min": 15.0, "15min": 30.0}

# app.data_sources.mu_extended_hours_collector와 동일한 실시간/지연 소스 분류를
# 재사용한다(다른 곳에서 다른 기준으로 "실시간"을 판단하는 혼란 방지).
REALTIME_SOURCES = {"kis", "alpaca", "polygon", "finnhub"}
DELAYED_SOURCES = {"yahoo", "naver"}
THIN_VOLUME_THRESHOLD = 5_000

# 명세 9절 — micron_score_source
SOURCE_REAL = "real_micron"
SOURCE_OVERNIGHT = "overnight_micron"
SOURCE_SYNTHETIC = "synthetic_micron"
SOURCE_TREND_KOREA_ONLY = "trend_and_korea_only"
SOURCE_INSUFFICIENT = "insufficient_data"

WARNING_DATA_INSUFFICIENT = "MICRON_PROXY_DATA_INSUFFICIENT"

# 명세 10절 — lead-lag 학습
LEAD_LAG_MIN_SAMPLES = 500
LEAD_LAG_CANDIDATE_MINUTES = (0, 1, 3, 5, 10, 15, 30)
LEAD_LAG_TARGETS = ("MU", "SOX_FUTURES_PROXY", "NASDAQ_FUTURES_PROXY", "SMH_SOXX", "SAMSUNG", "KOREA_SEMI_ETF", "INVESTOR_FLOW")

CSV_LOG_FIELDS = [
    "timestamp", "micron_session", "real_micron_price", "real_micron_last_time",
    "real_micron_age_minutes", "real_micron_score", "overnight_micron_score",
    "micron_recent_trend_score", "sox_futures_price", "sox_futures_score",
    "nasdaq_futures_price", "nasdaq_futures_score", "us_semiconductor_proxy_score",
    "korea_semiconductor_confirmation_score", "synthetic_micron_score",
    "effective_micron_score", "micron_score_source", "micron_data_confidence",
    "prediction_signal", "warning",
]


# =============================================================================
# 1. 세션 판정 / 신선도 검증
# =============================================================================

def validate_micron_data_freshness(
    last_update_time: Optional[datetime],
    bar_interval: str = "1min",
    now: Optional[datetime] = None,
) -> dict:
    """last_update_time과 현재시각의 차이를 bar_interval별 freshness 기준과 비교한다.

    Returns
    -------
    dict: is_fresh, age_minutes, threshold_minutes, bar_interval, reason
    """
    now = now or datetime.now()
    threshold = FRESHNESS_THRESHOLD_MINUTES.get(bar_interval, FRESHNESS_THRESHOLD_MINUTES["1min"])

    if last_update_time is None:
        return {
            "is_fresh": False, "age_minutes": None, "threshold_minutes": threshold,
            "bar_interval": bar_interval, "reason": "마지막 체결/캔들 시각 없음",
        }

    try:
        ts = last_update_time
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        age_minutes = max(0.0, (now - ts).total_seconds() / 60.0)
    except Exception as exc:
        return {
            "is_fresh": False, "age_minutes": None, "threshold_minutes": threshold,
            "bar_interval": bar_interval, "reason": f"시각 비교 실패: {exc}",
        }

    is_fresh = age_minutes <= threshold
    reason = (
        f"{bar_interval} 기준 {age_minutes:.1f}분 경과(기준 {threshold:.0f}분 이내) — 신선함"
        if is_fresh else
        f"{bar_interval} 기준 {age_minutes:.1f}분 경과(기준 {threshold:.0f}분 초과) — STALE"
    )
    return {
        "is_fresh": is_fresh, "age_minutes": round(age_minutes, 2),
        "threshold_minutes": threshold, "bar_interval": bar_interval, "reason": reason,
    }


def detect_micron_session(mu_data: Optional[dict], now: Optional[datetime] = None) -> dict:
    """현재 KST 시각 + 실제 MU 데이터 상태로 세션을 판정한다.

    단순 시간표가 아니라 마지막 체결/캔들 시각, 거래량, 데이터 제공자 소스를
    함께 확인한다(명세 2절). mu_data는 다음 키를 사용한다(없으면 None 취급):
      current_price, last_trade_time(또는 last_bar_time), bar_interval,
      volume_1m(또는 volume), source, prior_volume(직전 구간 거래량, 선택)

    Returns
    -------
    dict: session, clock_session, freshness(dict), has_trade_evidence,
          volume_increasing, reason
    """
    from app.utils.us_market_calendar import get_session_kst

    now = now or datetime.now()
    clock = get_session_kst(now)  # "premarket"|"regular"|"aftermarket"|"weekend"|"holiday"|"closed"
    clock_map = {
        "premarket": SESSION_PREMARKET, "regular": SESSION_REGULAR,
        "aftermarket": SESSION_AFTER_HOURS, "weekend": SESSION_CLOSED,
        "holiday": SESSION_CLOSED, "closed": SESSION_CLOSED,
    }
    clock_session = clock_map.get(clock, SESSION_CLOSED)

    if not mu_data or mu_data.get("current_price") is None:
        return {
            "session": SESSION_DATA_UNAVAILABLE, "clock_session": clock_session,
            "freshness": None, "has_trade_evidence": False, "volume_increasing": None,
            "reason": "MU 현재가/체결 데이터를 전혀 수집하지 못함",
        }

    last_time = mu_data.get("last_trade_time") or mu_data.get("last_bar_time")
    bar_interval = mu_data.get("bar_interval", "1min")
    freshness = validate_micron_data_freshness(last_time, bar_interval, now=now)

    volume = mu_data.get("volume_1m", mu_data.get("volume"))
    prior_volume = mu_data.get("prior_volume")
    has_trade_evidence = bool(volume and volume > 0) or bool(mu_data.get("has_bid_ask"))
    volume_increasing = None
    if volume is not None and prior_volume is not None and prior_volume > 0:
        volume_increasing = volume > prior_volume

    if not freshness["is_fresh"]:
        return {
            "session": SESSION_STALE_DATA, "clock_session": clock_session,
            "freshness": freshness, "has_trade_evidence": has_trade_evidence,
            "volume_increasing": volume_increasing,
            "reason": f"{freshness['reason']} — 시계상 세션({clock_session})과 무관하게 STALE_DATA로 분류",
        }

    if clock_session in (SESSION_PREMARKET, SESSION_REGULAR, SESSION_AFTER_HOURS):
        return {
            "session": clock_session, "clock_session": clock_session, "freshness": freshness,
            "has_trade_evidence": has_trade_evidence, "volume_increasing": volume_increasing,
            "reason": f"시계상 {clock_session}이며 데이터도 신선함(체결 근거: {has_trade_evidence})",
        }

    # 시계상 CLOSED인데 데이터가 신선함 → 야간 ATS 거래 증거가 있어야만 인정한다.
    source = mu_data.get("source")
    is_realtime_source = source in REALTIME_SOURCES
    if has_trade_evidence and is_realtime_source:
        return {
            "session": SESSION_OVERNIGHT_ATS, "clock_session": clock_session, "freshness": freshness,
            "has_trade_evidence": True, "volume_increasing": volume_increasing,
            "reason": f"시계상 CLOSED이지만 실시간 소스({source})에서 신선하고 체결 근거가 있는 야간 데이터 확인",
        }

    return {
        "session": SESSION_CLOSED, "clock_session": clock_session, "freshness": freshness,
        "has_trade_evidence": has_trade_evidence, "volume_increasing": volume_increasing,
        "reason": "시계상 CLOSED이며 야간 ATS 체결 근거 불충분 — CLOSED 유지",
    }


# =============================================================================
# 기술적 계산 헬퍼 (외부 ta 라이브러리 없이 순수 pandas로 계산)
# =============================================================================

def _rsi(closes: pd.Series, period: int = 14) -> Optional[float]:
    if closes is None or len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.rolling(period).mean().iloc[-1]
    avg_loss = loss.rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return None
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def _macd_histogram(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[float]:
    if closes is None or len(closes) < slow + signal:
        return None
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = (macd_line - signal_line).iloc[-1]
    return round(float(hist), 4) if not pd.isna(hist) else None


def _vwap(df: pd.DataFrame) -> Optional[float]:
    if df is None or df.empty:
        return None
    try:
        cols = df.columns
        high = df["high"] if "high" in cols else df["close"]
        low = df["low"] if "low" in cols else df["close"]
        typical = (high + low + df["close"]) / 3.0
        vol = df["volume"].astype(float)
        total = vol.sum()
        if total <= 0:
            return None
        return float((typical * vol).sum() / total)
    except Exception:
        return None


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


def _norm01(value: Optional[float], scale: float) -> Optional[float]:
    if value is None:
        return None
    return max(0.0, min(100.0, 50.0 + (value / scale) * 50.0))


def _z_score(value: Optional[float], history: Optional[list]) -> Optional[float]:
    """history: 최근 N거래일 등락률(%) 리스트. 표본이 부족하면 None."""
    if value is None or not history or len(history) < 5:
        return None
    s = pd.Series(history, dtype=float)
    std = s.std()
    if std is None or std == 0 or pd.isna(std):
        return None
    return round((value - s.mean()) / std, 3)


def _high_low_structure(df: pd.DataFrame, lookback: int = 20) -> Optional[str]:
    """최근 lookback봉의 고점/저점 구조: HIGHER_HIGHS | LOWER_LOWS | MIXED."""
    if df is None or len(df) < 6:
        return None
    work = df.tail(min(lookback, len(df)))
    mid = len(work) // 2
    first_half, second_half = work.iloc[:mid], work.iloc[mid:]
    try:
        higher_high = second_half["high"].max() > first_half["high"].max() if "high" in work.columns else second_half["close"].max() > first_half["close"].max()
        lower_low = second_half["low"].min() < first_half["low"].min() if "low" in work.columns else second_half["close"].min() < first_half["close"].min()
    except Exception:
        return None
    if higher_high and not lower_low:
        return "HIGHER_HIGHS"
    if lower_low and not higher_high:
        return "LOWER_LOWS"
    return "MIXED"


# =============================================================================
# 2. Micron 최근 전체 흐름 점수
# =============================================================================

def calculate_micron_recent_trend_score(
    df_1min: Optional[pd.DataFrame] = None,
    daily_closes: Optional[list] = None,
    regular_session_open_close: Optional[tuple] = None,
    extended_vs_regular_close_pct: Optional[float] = None,
    now: Optional[datetime] = None,
) -> dict:
    """실시간 데이터가 끊겨도 마지막 가격 하나가 아니라 최근 전체 흐름을 요약한다.

    Parameters
    ----------
    df_1min : 1분봉 DataFrame(columns: datetime/open/high/low/close/volume), 최근 순 정렬.
    daily_closes : 최근 일별 종가 리스트(오래된 순), 3/5거래일 수익률 계산용.
    regular_session_open_close : (정규장 시가, 정규장 종가) — 갭/장마감 방향 판정용.
    extended_vs_regular_close_pct : 정규장 종가 대비 프리/애프터/야간 변화율(%).
    """
    components: dict = {}
    warnings: list = []

    if df_1min is not None and not df_1min.empty and len(df_1min) >= 2:
        closes = df_1min["close"].astype(float)
        components["return_15m"] = _norm01(_pct_return(closes, 15), 1.0)
        components["return_30m"] = _norm01(_pct_return(closes, 30), 1.5)
        components["return_60m"] = _norm01(_pct_return(closes, 60), 2.0)
        vwap = _vwap(df_1min)
        last_close = float(closes.iloc[-1])
        vwap_pos_pct = (last_close - vwap) / vwap * 100 if vwap else None
        components["vwap_position"] = _norm01(vwap_pos_pct, 1.0)
        components["rsi"] = _rsi(closes)
        components["rsi_score"] = _norm01((components["rsi"] - 50.0) if components["rsi"] is not None else None, 25.0)
        macd_hist = _macd_histogram(closes)
        components["macd_score"] = _norm01(macd_hist, max(abs(macd_hist) * 3, 0.05)) if macd_hist is not None else None
        vol = df_1min["volume"].astype(float)
        recent_vol = vol.tail(15).mean() if len(vol) >= 15 else vol.mean()
        base_vol = vol.head(max(len(vol) - 15, 1)).mean() if len(vol) > 15 else vol.mean()
        vol_trend_pct = (recent_vol / base_vol - 1.0) * 100 if base_vol and base_vol > 0 else None
        components["volume_trend_score"] = _norm01(vol_trend_pct, 50.0)
        structure = _high_low_structure(df_1min)
        components["structure_score"] = {"HIGHER_HIGHS": 70.0, "LOWER_LOWS": 30.0, "MIXED": 50.0, None: None}.get(structure)
        last = df_1min.iloc[-1]
        try:
            components["last_bar_direction_score"] = 60.0 if float(last["close"]) > float(last["open"]) else (
                40.0 if float(last["close"]) < float(last["open"]) else 50.0
            )
        except Exception:
            components["last_bar_direction_score"] = None

        # 급등 후 되돌림 / 급락 후 회복 판정 (최근 60봉 중 극값 대비 마지막 위치)
        window = closes.tail(min(60, len(closes)))
        if len(window) >= 10:
            peak_idx = window.idxmax()
            trough_idx = window.idxmin()
            last_idx = window.index[-1]
            if peak_idx != last_idx and window.loc[peak_idx] > 0 and (last_idx - peak_idx) > 0:
                pullback_pct = (window.iloc[-1] / window.loc[peak_idx] - 1.0) * 100
                components["pullback_after_spike_score"] = _norm01(pullback_pct, 1.0) if pullback_pct < -0.3 else None
            if trough_idx != last_idx and window.loc[trough_idx] > 0 and (last_idx - trough_idx) > 0:
                recovery_pct = (window.iloc[-1] / window.loc[trough_idx] - 1.0) * 100
                components["recovery_after_drop_score"] = _norm01(recovery_pct, 1.0) if recovery_pct > 0.3 else None
    else:
        warnings.append("1분봉 데이터 없음 — 정규장 전체/일봉 데이터만으로 추세 산출")

    if regular_session_open_close and all(v is not None for v in regular_session_open_close):
        r_open, r_close = regular_session_open_close
        if r_open:
            components["regular_session_full_return"] = _norm01((r_close / r_open - 1.0) * 100, 2.0)

    if extended_vs_regular_close_pct is not None:
        components["gap_vs_regular_close_score"] = _norm01(extended_vs_regular_close_pct, 1.5)

    if daily_closes and len(daily_closes) >= 2:
        s = pd.Series(daily_closes, dtype=float)
        components["return_3d"] = _norm01(_pct_return(s, 3), 3.0)
        components["return_5d"] = _norm01(_pct_return(s, 5), 5.0)
    else:
        warnings.append("최근 3/5거래일 종가 없음 — 일봉 추세 컴포넌트 제외")

    weights = {
        "return_15m": 0.12, "return_30m": 0.10, "return_60m": 0.08, "vwap_position": 0.10,
        "rsi_score": 0.08, "macd_score": 0.08, "volume_trend_score": 0.06, "structure_score": 0.06,
        "last_bar_direction_score": 0.04, "pullback_after_spike_score": 0.05, "recovery_after_drop_score": 0.05,
        "regular_session_full_return": 0.08, "gap_vs_regular_close_score": 0.05,
        "return_3d": 0.03, "return_5d": 0.02,
    }
    weighted, total_w = 0.0, 0.0
    for k, w in weights.items():
        v = components.get(k)
        if v is None:
            continue
        weighted += v * w
        total_w += w
    score = round(weighted / total_w, 2) if total_w > 0 else 50.0

    if score >= 70:
        direction = "STRONG_UP"
    elif score >= 55:
        direction = "UP"
    elif score >= 45:
        direction = "NEUTRAL"
    elif score >= 30:
        direction = "DOWN"
    else:
        direction = "STRONG_DOWN"

    return {
        "micron_recent_trend_score": score, "micron_recent_trend_direction": direction,
        "components": components, "coverage": round(total_w, 3), "warnings": warnings,
    }


# =============================================================================
# 3/4. SOX / Nasdaq 선물 proxy 점수
# =============================================================================

def calculate_sox_futures_score(
    sox_return_pct: Optional[float],
    momentum_1m_pct: Optional[float] = None,
    momentum_3m_pct: Optional[float] = None,
    momentum_5m_pct: Optional[float] = None,
    recent_20d_returns: Optional[list] = None,
) -> dict:
    """SOX semiconductor futures proxy score(0~100). 실제 CME 선물이 아니라
    SOXX/SOX ETF·지수 등락률 기반 proxy임을 명시적으로 표기한다."""
    if sox_return_pct is None:
        return {
            "sox_futures_score": 50.0, "label": "SOX semiconductor futures proxy",
            "z_score": None, "available": False, "warnings": ["SOX proxy(SOXX/SOX) 데이터 없음 — 중립값 사용"],
        }

    base_score = _norm01(sox_return_pct, 1.5)
    base_score = base_score if base_score is not None else 50.0
    z = _z_score(sox_return_pct, recent_20d_returns)
    momentum_signal, momentum_w = 0.0, 0.0
    for m, w in ((momentum_1m_pct, 0.2), (momentum_3m_pct, 0.3), (momentum_5m_pct, 0.5)):
        if m is not None:
            m_score = _norm01(m, 1.0)
            momentum_signal += (m_score if m_score is not None else 50.0) * w
            momentum_w += w
    momentum_score = momentum_signal / momentum_w if momentum_w > 0 else None

    if z is not None:
        z_score_component = max(0.0, min(100.0, 50.0 + z * 15.0))
        momentum_component = momentum_score if momentum_score is not None else base_score
        score = round(base_score * 0.55 + z_score_component * 0.25 + momentum_component * 0.20, 2)
    elif momentum_score is not None:
        score = round(base_score * 0.7 + momentum_score * 0.3, 2)
    else:
        score = round(base_score, 2)

    return {
        "sox_futures_score": max(0.0, min(100.0, score)), "label": "SOX semiconductor futures proxy",
        "z_score": z, "return_pct": sox_return_pct, "available": True, "warnings": [],
    }


def calculate_nasdaq_futures_score(
    nasdaq_return_pct: Optional[float],
    momentum_5m_pct: Optional[float] = None,
    momentum_15m_pct: Optional[float] = None,
    momentum_30m_pct: Optional[float] = None,
    sox_futures_score: Optional[float] = None,
    recent_20d_returns: Optional[list] = None,
) -> dict:
    """Nasdaq(NQ/MNQ) futures proxy score(0~100) + SOX 대비 방향 일치 confidence."""
    if nasdaq_return_pct is None:
        return {
            "nasdaq_futures_score": 50.0, "label": "Nasdaq futures proxy", "available": False,
            "direction_agrees_with_sox": None, "confidence_multiplier": 0.5,
            "warnings": ["Nasdaq futures proxy(NQ=F/QQQ) 데이터 없음 — 중립값 사용"],
        }

    base_score = _norm01(nasdaq_return_pct, 1.2)
    base_score = base_score if base_score is not None else 50.0
    z = _z_score(nasdaq_return_pct, recent_20d_returns)
    momentum_signal, momentum_w = 0.0, 0.0
    for m, w in ((momentum_5m_pct, 0.3), (momentum_15m_pct, 0.35), (momentum_30m_pct, 0.35)):
        if m is not None:
            m_score = _norm01(m, 1.0)
            momentum_signal += (m_score if m_score is not None else 50.0) * w
            momentum_w += w
    momentum_score = momentum_signal / momentum_w if momentum_w > 0 else None

    if z is not None:
        z_component = max(0.0, min(100.0, 50.0 + z * 15.0))
        momentum_component = momentum_score if momentum_score is not None else base_score
        score = round(base_score * 0.55 + z_component * 0.25 + momentum_component * 0.20, 2)
    elif momentum_score is not None:
        score = round(base_score * 0.7 + momentum_score * 0.3, 2)
    else:
        score = round(base_score, 2)

    direction_agrees = None
    confidence_multiplier = 1.0
    if sox_futures_score is not None:
        nasdaq_dir = 1 if score > 52 else (-1 if score < 48 else 0)
        sox_dir = 1 if sox_futures_score > 52 else (-1 if sox_futures_score < 48 else 0)
        if nasdaq_dir != 0 and sox_dir != 0:
            direction_agrees = nasdaq_dir == sox_dir
            confidence_multiplier = 1.15 if direction_agrees else 0.70
        else:
            confidence_multiplier = 0.9

    return {
        "nasdaq_futures_score": max(0.0, min(100.0, score)), "label": "Nasdaq futures proxy",
        "z_score": z, "return_pct": nasdaq_return_pct, "available": True,
        "direction_agrees_with_sox": direction_agrees, "confidence_multiplier": round(confidence_multiplier, 3),
        "warnings": [],
    }


# =============================================================================
# 6. 미국 반도체 Proxy Basket
# =============================================================================

_DEFAULT_BASKET_WEIGHTS = {
    "SMH": 0.20, "SOXX": 0.15, "NVDA": 0.20, "AMD": 0.12, "AVGO": 0.13,
    "SNDK": 0.08, "INTC": 0.07, "TSM": 0.05,
}


def calculate_us_semiconductor_proxy_score(
    basket_returns: dict,
    weights: Optional[dict] = None,
) -> dict:
    """basket_returns: {symbol: return_pct}. 시가총액 가중/설명력 가중치를
    recommend_micron_proxy_weights()가 생성한 추천값이 있으면 그것을, 없으면
    기본 가중치를 사용한다(고정 하드코딩을 피하려는 취지 — 값 자체는
    LEAD_LAG_MIN_SAMPLES 표본이 쌓이기 전까지는 기본값을 유지)."""
    weights = weights or _DEFAULT_BASKET_WEIGHTS
    available = {k: v for k, v in basket_returns.items() if v is not None}
    if not available:
        return {
            "us_semiconductor_proxy_score": 50.0, "available_symbols": [], "coverage": 0.0,
            "weights_used": weights, "warnings": ["미국 반도체 basket 데이터 전부 없음 — 중립값 사용"],
        }

    weighted, total_w = 0.0, 0.0
    per_symbol_scores = {}
    for symbol, ret in available.items():
        w = weights.get(symbol, 0.05)
        s = _norm01(ret, 3.0)
        s = s if s is not None else 50.0
        per_symbol_scores[symbol] = s
        weighted += s * w
        total_w += w
    score = round(weighted / total_w, 2) if total_w > 0 else 50.0
    coverage = round(total_w / sum(weights.get(k, 0.05) for k in weights), 3) if weights else 0.0

    direction_agreement = sum(1 for s in per_symbol_scores.values() if s > 52)
    direction_agreement = max(direction_agreement, sum(1 for s in per_symbol_scores.values() if s < 48))
    agreement_ratio = direction_agreement / len(per_symbol_scores) if per_symbol_scores else 0.0

    return {
        "us_semiconductor_proxy_score": score, "per_symbol_scores": per_symbol_scores,
        "available_symbols": list(available.keys()), "coverage": min(1.0, coverage),
        "direction_agreement_ratio": round(agreement_ratio, 3), "weights_used": weights, "warnings": [],
    }


# =============================================================================
# 7. 한국 반도체 확인 점수
# =============================================================================

def calculate_korea_semiconductor_confirmation_score(korea_data: dict) -> dict:
    """korea_data 키(모두 선택):
      hynix_vwap_position_pct, hynix_return_1m_pct, hynix_return_3m_pct, hynix_return_5m_pct,
      samsung_return_pct, korea_semi_etf_return_pct, kospi_return_pct, kospi200_return_pct,
      foreign_net_buy, institution_net_buy, execution_strength(체결강도, 선택),
      order_book_imbalance(호가 불균형, 선택), hynix_high_low_structure
    """
    components: dict = {}
    unavailable: list = []

    components["hynix_vwap_position"] = _norm01(korea_data.get("hynix_vwap_position_pct"), 1.0)
    components["hynix_momentum_1m"] = _norm01(korea_data.get("hynix_return_1m_pct"), 0.5)
    components["hynix_momentum_3m"] = _norm01(korea_data.get("hynix_return_3m_pct"), 1.0)
    components["hynix_momentum_5m"] = _norm01(korea_data.get("hynix_return_5m_pct"), 1.5)
    components["samsung_confirmation"] = _norm01(korea_data.get("samsung_return_pct"), 2.0)
    components["korea_semi_etf"] = _norm01(korea_data.get("korea_semi_etf_return_pct"), 2.0)
    components["kospi200"] = _norm01(korea_data.get("kospi200_return_pct") or korea_data.get("kospi_return_pct"), 1.5)

    foreign_net = korea_data.get("foreign_net_buy")
    institution_net = korea_data.get("institution_net_buy")
    flow_values = [v for v in (foreign_net, institution_net) if v is not None]
    flow_signal = sum(flow_values) / len(flow_values) if flow_values else None
    components["investor_flow"] = _norm01(flow_signal, 1_500_000.0) if flow_signal is not None else None

    # 체결강도/호가 불균형 데이터 소스가 이 프로젝트에는 없음 — 명세대로 unavailable로 명시.
    components["execution_strength"] = _norm01(korea_data.get("execution_strength"), 30.0) if korea_data.get("execution_strength") is not None else None
    components["order_book_imbalance"] = _norm01(korea_data.get("order_book_imbalance"), 1.0) if korea_data.get("order_book_imbalance") is not None else None

    structure = korea_data.get("hynix_high_low_structure")
    components["structure_score"] = {"HIGHER_HIGHS": 65.0, "LOWER_LOWS": 35.0, "MIXED": 50.0}.get(structure)

    unavailable = [k for k, v in components.items() if v is None]

    weights = {
        "hynix_vwap_position": 0.15, "hynix_momentum_1m": 0.08, "hynix_momentum_3m": 0.10,
        "hynix_momentum_5m": 0.07, "samsung_confirmation": 0.15, "korea_semi_etf": 0.10,
        "kospi200": 0.10, "investor_flow": 0.15, "execution_strength": 0.05,
        "order_book_imbalance": 0.03, "structure_score": 0.02,
    }
    weighted, total_w = 0.0, 0.0
    for k, w in weights.items():
        v = components.get(k)
        if v is None:
            continue
        weighted += v * w
        total_w += w
    score = round(weighted / total_w, 2) if total_w > 0 else 50.0

    return {
        "korea_semiconductor_confirmation_score": score, "components": components,
        "coverage": round(total_w, 3), "unavailable": unavailable,
    }


# =============================================================================
# 8. Synthetic Micron Score (시간대별 동적 가중치)
# =============================================================================

# 명세 8절 — 시간대별(장중) 기본 배분. 09:00 이전/15:15 이후는 마지막 구간을 그대로 유지.
_TIME_WEIGHT_SCHEDULE = [
    ("09:00", "10:00", {"real_or_overnight_mu": 0.40, "sox": 0.25, "nasdaq": 0.10, "us_basket": 0.15, "korea": 0.10}),
    ("10:00", "11:30", {"real_or_overnight_mu": 0.10, "trend": 0.15, "sox": 0.30, "nasdaq": 0.10, "us_basket": 0.15, "korea": 0.20}),
    ("11:30", "13:30", {"trend": 0.10, "sox": 0.25, "nasdaq": 0.10, "us_basket": 0.15, "korea": 0.40}),
    ("13:30", "15:15", {"trend": 0.05, "sox": 0.20, "nasdaq": 0.10, "us_basket": 0.10, "korea": 0.55}),
]
# 09:00 이전(개장 직전) / 15:15 이후(장 막판)는 각각 첫/마지막 구간 배분을 그대로 사용한다.
_DEFAULT_SYNTHETIC_WEIGHTS = {"sox": 0.35, "nasdaq": 0.15, "us_basket": 0.20, "trend": 0.15, "korea": 0.15}


def _time_of_day_weights(now_hm: Optional[str] = None) -> dict:
    now_hm = now_hm or datetime.now().strftime("%H:%M")
    for start, end, weights in _TIME_WEIGHT_SCHEDULE:
        if start <= now_hm < end:
            return dict(weights)
    if now_hm < _TIME_WEIGHT_SCHEDULE[0][0]:
        return dict(_TIME_WEIGHT_SCHEDULE[0][2])
    if now_hm >= _TIME_WEIGHT_SCHEDULE[-1][1]:
        return dict(_TIME_WEIGHT_SCHEDULE[-1][2])
    return dict(_DEFAULT_SYNTHETIC_WEIGHTS)


def calculate_synthetic_micron_score(
    sox_futures_score: Optional[float],
    nasdaq_futures_score: Optional[float],
    us_semiconductor_proxy_score: Optional[float],
    micron_recent_trend_score: Optional[float],
    korea_semiconductor_confirmation_score: Optional[float],
    now_hm: Optional[str] = None,
    real_or_overnight_mu_score: Optional[float] = None,
    korea_conflicts_with_us: Optional[bool] = None,
) -> dict:
    """MU 데이터가 CLOSED/STALE_DATA/DATA_UNAVAILABLE일 때의 대체 점수.

    시간대별 동적 가중치(명세 8절)를 적용하고, real_or_overnight_mu_score가
    주어지면(예: 09:00~10:00에 overnight ATS가 신선한 경우) 그 항목도 배합에
    포함한다. 한국 시장 흐름과 미국계 신호가 충돌하면 confidence를 낮춘다.
    """
    weights = _time_of_day_weights(now_hm)
    values = {
        "real_or_overnight_mu": real_or_overnight_mu_score,
        "sox": sox_futures_score,
        "nasdaq": nasdaq_futures_score,
        "us_basket": us_semiconductor_proxy_score,
        "trend": micron_recent_trend_score,
        "korea": korea_semiconductor_confirmation_score,
    }
    weighted, total_w = 0.0, 0.0
    used_weights = {}
    for key, w in weights.items():
        v = values.get(key)
        if v is None:
            continue
        weighted += v * w
        total_w += w
        used_weights[key] = w
    score = round(weighted / total_w, 2) if total_w > 0 else 50.0

    confidence = 70.0 if total_w >= 0.6 else (55.0 if total_w >= 0.3 else 35.0)
    if korea_conflicts_with_us is None and korea_semiconductor_confirmation_score is not None:
        us_side_values = [v for v in (sox_futures_score, nasdaq_futures_score, us_semiconductor_proxy_score) if v is not None]
        if us_side_values:
            us_avg = sum(us_side_values) / len(us_side_values)
            korea_conflicts_with_us = (us_avg > 55 and korea_semiconductor_confirmation_score < 45) or (
                us_avg < 45 and korea_semiconductor_confirmation_score > 55
            )
    if korea_conflicts_with_us:
        confidence *= 0.7

    return {
        "synthetic_micron_score": max(0.0, min(100.0, score)), "confidence": round(confidence, 1),
        "weights_used": used_weights, "raw_weight_schedule": weights,
        "korea_conflicts_with_us": bool(korea_conflicts_with_us), "coverage": round(total_w, 3),
    }


# =============================================================================
# 9. Effective Micron Score
# =============================================================================

def calculate_effective_micron_score(
    session_info: dict,
    real_micron_score: Optional[float],
    overnight_micron_score: Optional[float],
    micron_recent_trend_score: Optional[float],
    sox_futures_score: Optional[float],
    nasdaq_futures_score: Optional[float],
    us_semiconductor_proxy_score: Optional[float],
    korea_semiconductor_confirmation_score: Optional[float],
    synthetic_micron_score: Optional[float],
    synthetic_confidence: Optional[float] = None,
    now_hm: Optional[str] = None,
) -> dict:
    """A~E 분기(명세 9절)를 적용해 effective_micron_score/micron_score_source/
    micron_data_confidence/warnings를 결정한다."""
    session = session_info.get("session") if session_info else SESSION_DATA_UNAVAILABLE
    freshness = (session_info or {}).get("freshness") or {}
    warnings: list = []

    real_fresh = session in (SESSION_REGULAR, SESSION_PREMARKET, SESSION_AFTER_HOURS) and real_micron_score is not None
    overnight_fresh = session == SESSION_OVERNIGHT_ATS and overnight_micron_score is not None

    if real_fresh:
        effective_score = real_micron_score
        source = SOURCE_REAL
        confidence = 90.0 if freshness.get("is_fresh") else 70.0
    elif overnight_fresh:
        effective_score = overnight_micron_score
        source = SOURCE_OVERNIGHT
        confidence = 75.0
    elif synthetic_micron_score is not None and session in (SESSION_STALE_DATA, SESSION_CLOSED, SESSION_DATA_UNAVAILABLE):
        effective_score = synthetic_micron_score
        source = SOURCE_SYNTHETIC
        confidence = synthetic_confidence if synthetic_confidence is not None else 60.0
        if session == SESSION_STALE_DATA:
            warnings.append("실제 MU 데이터가 STALE — synthetic_micron_score로 전환")
    elif micron_recent_trend_score is not None or korea_semiconductor_confirmation_score is not None:
        parts = [v for v in (micron_recent_trend_score, korea_semiconductor_confirmation_score) if v is not None]
        effective_score = round(sum(parts) / len(parts), 2)
        source = SOURCE_TREND_KOREA_ONLY
        confidence = 45.0
        warnings.append("SOX/Nasdaq/미국 반도체 basket 데이터 부족 — 추세+한국 확인점수만으로 추정")
    else:
        effective_score = 50.0
        source = SOURCE_INSUFFICIENT
        confidence = 15.0
        warnings.append(WARNING_DATA_INSUFFICIENT)

    if session == SESSION_STALE_DATA and source in (SOURCE_REAL, SOURCE_OVERNIGHT):
        # 방어적 가드 — STALE 세션에서는 real/overnight 분기로 들어올 수 없어야 하지만,
        # 호출부 실수로 신선하지 않은 값이 들어온 경우 라이브로 잘못 표시하지 않는다.
        effective_score = synthetic_micron_score if synthetic_micron_score is not None else 50.0
        source = SOURCE_SYNTHETIC if synthetic_micron_score is not None else SOURCE_INSUFFICIENT
        confidence = min(confidence, 60.0)
        warnings.append("STALE 데이터가 real/overnight 분기로 유입되어 synthetic으로 강제 전환")

    return {
        "real_micron_score": real_micron_score, "overnight_micron_score": overnight_micron_score,
        "micron_recent_trend_score": micron_recent_trend_score, "sox_futures_score": sox_futures_score,
        "nasdaq_futures_score": nasdaq_futures_score, "us_semiconductor_proxy_score": us_semiconductor_proxy_score,
        "korea_semiconductor_confirmation_score": korea_semiconductor_confirmation_score,
        "synthetic_micron_score": synthetic_micron_score,
        "effective_micron_score": round(max(0.0, min(100.0, effective_score)), 2),
        "micron_score_source": source, "micron_data_confidence": round(max(0.0, min(100.0, confidence)), 1),
        "micron_session": session, "warnings": warnings,
    }


def explain_micron_prediction(result: dict) -> str:
    """UI/로그용 사람이 읽을 수 있는 설명 문자열."""
    source_label = {
        SOURCE_REAL: "실제 MU 데이터", SOURCE_OVERNIGHT: "MU 야간 ATS 데이터",
        SOURCE_SYNTHETIC: "SOX/Nasdaq futures proxy 기반 synthetic 추정",
        SOURCE_TREND_KOREA_ONLY: "Micron 추세 + 한국 반도체 확인만으로 추정",
        SOURCE_INSUFFICIENT: "데이터 부족 — 중립값",
    }.get(result.get("micron_score_source"), "알 수 없음")

    lines = [
        f"세션: {result.get('micron_session')}",
        f"Effective Micron Score: {result.get('effective_micron_score')} (출처: {source_label})",
        f"데이터 신뢰도(confidence): {result.get('micron_data_confidence')}",
    ]
    if result.get("sox_futures_score") is not None:
        lines.append(f"SOX semiconductor futures proxy: {result['sox_futures_score']}")
    if result.get("nasdaq_futures_score") is not None:
        lines.append(f"Nasdaq futures proxy: {result['nasdaq_futures_score']}")
    if result.get("korea_semiconductor_confirmation_score") is not None:
        lines.append(f"한국 반도체 확인점수: {result['korea_semiconductor_confirmation_score']}")
    for w in result.get("warnings") or []:
        lines.append(f"경고: {w}")
    return "\n".join(lines)


# =============================================================================
# 10. Lead-Lag 학습 (표본 500개 미만이면 추천값 생성 안 함)
# =============================================================================

def recommend_micron_proxy_weights(log_path: Optional[Path] = None) -> dict:
    """data/logs/micron_proxy_prediction_log.csv를 읽어 각 외부 지표가 하이닉스
    수익률보다 몇 분 먼저 움직이는지(lead-lag) 계산하고, 표본이 충분하면
    추천 가중치를 data/state/micron_proxy_weight_recommendation.json에 저장한다.
    표본이 500개 미만이면 추천값을 생성하지 않는다(명세 10절)."""
    path = log_path or _LOG_PATH
    created_at = datetime.now().isoformat()

    if not path.exists():
        result = {
            "skipped": True, "reason": f"로그 파일 없음({path})", "sample_size": 0,
            "recommended_weights": None, "lead_lag": None, "created_at": created_at,
        }
        _save_weight_recommendation(result)
        return result

    try:
        df = pd.read_csv(path)
    except Exception as exc:
        result = {
            "skipped": True, "reason": f"로그 파일 로드 실패: {exc}", "sample_size": 0,
            "recommended_weights": None, "lead_lag": None, "created_at": created_at,
        }
        _save_weight_recommendation(result)
        return result

    sample_size = int(len(df))
    if sample_size < LEAD_LAG_MIN_SAMPLES:
        result = {
            "skipped": True,
            "reason": f"샘플 부족(sample_size={sample_size} < {LEAD_LAG_MIN_SAMPLES}) — 추천값 생성 생략",
            "sample_size": sample_size, "recommended_weights": None, "lead_lag": None, "created_at": created_at,
        }
        _save_weight_recommendation(result)
        return result

    lead_lag_results = {}
    score_cols = {
        "sox_futures_score": "SOX_FUTURES_PROXY", "nasdaq_futures_score": "NASDAQ_FUTURES_PROXY",
        "us_semiconductor_proxy_score": "US_SEMI_BASKET", "korea_semiconductor_confirmation_score": "KOREA_SEMI",
    }
    if "effective_micron_score" not in df.columns:
        result = {
            "skipped": True, "reason": "로그에 effective_micron_score 컬럼 없음",
            "sample_size": sample_size, "recommended_weights": None, "lead_lag": None, "created_at": created_at,
        }
        _save_weight_recommendation(result)
        return result

    target = pd.to_numeric(df["effective_micron_score"], errors="coerce")
    for col, label in score_cols.items():
        if col not in df.columns:
            continue
        series = pd.to_numeric(df[col], errors="coerce")
        best_lag, best_corr = 0, 0.0
        for lag in LEAD_LAG_CANDIDATE_MINUTES:
            shifted = series.shift(lag)
            valid = pd.concat([shifted, target], axis=1).dropna()
            if len(valid) < 30:
                continue
            corr = valid.iloc[:, 0].corr(valid.iloc[:, 1])
            if corr is not None and not pd.isna(corr) and abs(corr) > abs(best_corr):
                best_lag, best_corr = lag, float(corr)
        lead_lag_results[label] = {"best_lag_minutes": best_lag, "correlation": round(best_corr, 4)}

    total_abs_corr = sum(abs(v["correlation"]) for v in lead_lag_results.values()) or 1.0
    recommended_weights = {
        label.lower(): round(abs(v["correlation"]) / total_abs_corr, 4) for label, v in lead_lag_results.items()
    }

    result = {
        "skipped": False, "reason": f"표본 {sample_size}건 기준 lead-lag 분석 완료",
        "sample_size": sample_size, "recommended_weights": recommended_weights,
        "lead_lag": lead_lag_results, "created_at": created_at,
    }
    _save_weight_recommendation(result)
    return result


def _save_weight_recommendation(result: dict) -> None:
    try:
        _WEIGHT_RECO_PATH.parent.mkdir(parents=True, exist_ok=True)
        _WEIGHT_RECO_PATH.write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("[MicronProxyPrediction] 가중치 추천 저장 실패: %s", exc)


def load_micron_proxy_weight_recommendation() -> Optional[dict]:
    try:
        if not _WEIGHT_RECO_PATH.exists():
            return None
        return json.loads(_WEIGHT_RECO_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[MicronProxyPrediction] 가중치 추천 로드 실패: %s", exc)
        return None


# =============================================================================
# 13. 로그
# =============================================================================

def log_micron_proxy_prediction(result: dict, log_path: Optional[Path] = None) -> None:
    path = log_path or _LOG_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        row = {field: result.get(field) for field in CSV_LOG_FIELDS}
        row["timestamp"] = result.get("timestamp") or datetime.now().isoformat(timespec="seconds")
        row["warning"] = "; ".join(result.get("warnings") or []) if isinstance(result.get("warnings"), list) else (result.get("warning") or "")
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_LOG_FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:
        logger.debug("[MicronProxyPrediction] CSV 로그 기록 실패(무해): %s", exc)


# =============================================================================
# MicronProxyPredictionEngine — 실시간 수집 + 스코어링 오케스트레이션
# =============================================================================

def _run_pipeline(
    mu_data_for_session: Optional[dict], mu_ext: dict, df_1min: Optional[pd.DataFrame],
    index_data: dict, nvda_data: dict, amd_data: dict, avgo_data: dict, korea_data: dict,
    now: datetime, extra_warnings: Optional[list] = None,
) -> dict:
    """collect_and_predict()와 compute_effective_micron_score_from_market_data()가
    공유하는 스코어링 파이프라인(네트워크 호출 없음 — 이미 수집된 값만 사용)."""
    warnings = list(extra_warnings or [])

    session_info = detect_micron_session(mu_data_for_session, now=now)
    session = session_info["session"]

    real_score = None
    overnight_score = None
    if session in (SESSION_REGULAR, SESSION_PREMARKET, SESSION_AFTER_HOURS, SESSION_OVERNIGHT_ATS):
        change_pct = mu_ext.get("change_pct_from_previous_close")
        slope_3m = mu_ext.get("slope_3m")
        base = _norm01(change_pct, 3.0)
        slope_component = _norm01(slope_3m, 0.5)
        parts = [v for v in (base, slope_component) if v is not None]
        combined = round(sum(parts) / len(parts), 2) if parts else None
        if session == SESSION_OVERNIGHT_ATS:
            overnight_score = combined
        else:
            real_score = combined

    trend = calculate_micron_recent_trend_score(
        df_1min=df_1min, extended_vs_regular_close_pct=mu_ext.get("change_pct_from_regular_close"), now=now,
    )

    sox = calculate_sox_futures_score(index_data.get("sox_return"))
    nasdaq = calculate_nasdaq_futures_score(index_data.get("qqq_return"), sox_futures_score=sox.get("sox_futures_score"))
    basket_returns = {
        "SOXX": index_data.get("sox_return"), "NVDA": nvda_data.get("regular_return"),
        "AMD": amd_data.get("regular_return"), "AVGO": avgo_data.get("regular_return"),
    }
    us_basket = calculate_us_semiconductor_proxy_score(basket_returns)
    korea = calculate_korea_semiconductor_confirmation_score(korea_data)

    now_hm = now.strftime("%H:%M")
    synthetic = calculate_synthetic_micron_score(
        sox_futures_score=sox.get("sox_futures_score"), nasdaq_futures_score=nasdaq.get("nasdaq_futures_score"),
        us_semiconductor_proxy_score=us_basket.get("us_semiconductor_proxy_score"),
        micron_recent_trend_score=trend.get("micron_recent_trend_score"),
        korea_semiconductor_confirmation_score=korea.get("korea_semiconductor_confirmation_score"),
        now_hm=now_hm, real_or_overnight_mu_score=real_score if real_score is not None else overnight_score,
    )

    effective = calculate_effective_micron_score(
        session_info=session_info, real_micron_score=real_score, overnight_micron_score=overnight_score,
        micron_recent_trend_score=trend.get("micron_recent_trend_score"),
        sox_futures_score=sox.get("sox_futures_score"), nasdaq_futures_score=nasdaq.get("nasdaq_futures_score"),
        us_semiconductor_proxy_score=us_basket.get("us_semiconductor_proxy_score"),
        korea_semiconductor_confirmation_score=korea.get("korea_semiconductor_confirmation_score"),
        synthetic_micron_score=synthetic.get("synthetic_micron_score"), synthetic_confidence=synthetic.get("confidence"),
        now_hm=now_hm,
    )
    effective["warnings"] = list(dict.fromkeys((effective.get("warnings") or []) + warnings))
    effective["timestamp"] = now.isoformat(timespec="seconds")
    effective["real_micron_price"] = mu_ext.get("current_price")
    last_time = mu_data_for_session.get("last_trade_time") if mu_data_for_session else None
    effective["real_micron_last_time"] = last_time.isoformat() if last_time else None
    effective["real_micron_age_minutes"] = (session_info.get("freshness") or {}).get("age_minutes")
    effective["sox_futures_price"] = None
    effective["nasdaq_futures_price"] = None
    effective["prediction_signal"] = effective["micron_score_source"]
    effective["session_info"] = session_info
    effective["explanation"] = explain_micron_prediction(effective)
    return effective


def compute_effective_micron_score_from_market_data(market_data: dict, now: Optional[datetime] = None) -> dict:
    """이미 collect_all()(또는 동일 스키마)로 수집된 market_data를 재사용해
    effective_micron_score를 계산한다(네트워크 재호출 없음). Prediction AI V2
    (hynix_price_predictor.py)가 자체 Micron 입력을 이 함수 결과로 교체하기
    위한 진입점이다."""
    now = now or datetime.now()
    market_data = market_data or {}

    mu = market_data.get("mu", {}) or {}
    mu_ext = mu.get("extended_hours") or {}
    df_1min = mu.get("df_1min")

    mu_data_for_session = None
    if mu_ext.get("current_price") is not None:
        last_time = None
        try:
            if df_1min is not None and not df_1min.empty:
                last_time = pd.Timestamp(df_1min["datetime"].iloc[-1]).to_pydatetime()
            elif mu_ext.get("freshness_seconds") is not None:
                last_time = now - timedelta(seconds=float(mu_ext["freshness_seconds"]))
        except Exception:
            last_time = None
        mu_data_for_session = {
            "current_price": mu_ext.get("current_price"), "last_trade_time": last_time,
            "bar_interval": "1min" if df_1min is not None and not df_1min.empty else "3min",
            "volume_1m": mu_ext.get("volume_1m"), "source": mu_ext.get("data_source"),
        }

    index_data = market_data.get("index", {}) or {}
    nvda_data = market_data.get("nvda", {}) or {}
    amd_data = market_data.get("amd", {}) or {}
    avgo_data = market_data.get("avgo", {}) or {}
    domestic_index = market_data.get("domestic_index", {}) or {}
    investor_flow = market_data.get("investor_flow", {}) or {}
    kospilab = market_data.get("kospilab", {}) or {}

    korea_data = {
        "kospi_return_pct": domestic_index.get("kospi_return"),
        "kospi200_return_pct": domestic_index.get("kospi200_return"),
        "foreign_net_buy": investor_flow.get("foreign_net_buy"),
        "institution_net_buy": investor_flow.get("institution_net_buy"),
        "samsung_return_pct": kospilab.get("samsung_reference_return"),
    }

    effective = _run_pipeline(
        mu_data_for_session, mu_ext, df_1min, index_data, nvda_data, amd_data, avgo_data, korea_data, now,
    )
    log_micron_proxy_prediction(effective)
    return effective


class MicronProxyPredictionEngine:
    """실시간 데이터 수집(collect_and_predict)과 순수 계산 함수(모듈 레벨)를
    묶는 얇은 오케스트레이터. calculate_*/detect_*/validate_*/explain_* 메서드는
    테스트 편의를 위해 모듈 레벨 함수를 그대로 위임한다."""

    # ── 스펙 1절에 명시된 메서드명을 그대로 노출 ──────────────────────────
    def detect_micron_session(self, mu_data: Optional[dict], now: Optional[datetime] = None) -> dict:
        return detect_micron_session(mu_data, now=now)

    def validate_micron_data_freshness(self, last_update_time, bar_interval: str = "1min", now: Optional[datetime] = None) -> dict:
        return validate_micron_data_freshness(last_update_time, bar_interval=bar_interval, now=now)

    def calculate_micron_recent_trend_score(self, *args, **kwargs) -> dict:
        return calculate_micron_recent_trend_score(*args, **kwargs)

    def calculate_sox_futures_score(self, *args, **kwargs) -> dict:
        return calculate_sox_futures_score(*args, **kwargs)

    def calculate_nasdaq_futures_score(self, *args, **kwargs) -> dict:
        return calculate_nasdaq_futures_score(*args, **kwargs)

    def calculate_us_semiconductor_proxy_score(self, *args, **kwargs) -> dict:
        return calculate_us_semiconductor_proxy_score(*args, **kwargs)

    def calculate_korea_semiconductor_confirmation_score(self, *args, **kwargs) -> dict:
        return calculate_korea_semiconductor_confirmation_score(*args, **kwargs)

    def calculate_synthetic_micron_score(self, *args, **kwargs) -> dict:
        return calculate_synthetic_micron_score(*args, **kwargs)

    def calculate_effective_micron_score(self, *args, **kwargs) -> dict:
        return calculate_effective_micron_score(*args, **kwargs)

    def explain_micron_prediction(self, result: dict) -> str:
        return explain_micron_prediction(result)

    # ── 실시간 수집 오케스트레이션 ────────────────────────────────────────
    def collect_and_predict(self, mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
        """실제 데이터 수집부터 effective_micron_score까지 전체 파이프라인을
        실행한다. 어떤 이유로도 예외를 던지지 않는다(모든 실패는 warnings)."""
        now = now or datetime.now()
        warnings: list = []

        # ── MU 실데이터 수집 (기존 mu_extended_hours_collector 재사용) ──
        mu_ext = {}
        try:
            from app.data_sources.mu_extended_hours_collector import collect_mu_extended_hours
            mu_ext = collect_mu_extended_hours(mode=mode) or {}
        except Exception as exc:
            warnings.append(f"MU 장외 데이터 수집 실패: {exc}")

        df_1min = None
        try:
            from app.data_sources.kis_overseas_minute import fetch_mu_1min_bars
            df_1min = fetch_mu_1min_bars(mode=mode or "real")
        except Exception as exc:
            logger.debug("[MicronProxyPrediction] MU 1분봉 수집 실패(무해): %s", exc)

        mu_data_for_session = None
        if mu_ext.get("current_price") is not None:
            last_time = None
            try:
                if df_1min is not None and not df_1min.empty:
                    last_time = pd.Timestamp(df_1min["datetime"].iloc[-1]).to_pydatetime()
            except Exception:
                last_time = None
            mu_data_for_session = {
                "current_price": mu_ext.get("current_price"),
                "last_trade_time": last_time,
                "bar_interval": "1min" if df_1min is not None and not df_1min.empty else "3min",
                "volume_1m": mu_ext.get("volume_1m"),
                "source": mu_ext.get("data_source"),
            }

        # ── SOX/Nasdaq futures proxy + 미국 반도체 basket ──
        index_data = {}
        nvda_data = amd_data = avgo_data = {}
        try:
            from app.data_sources.auto_market_collector import (
                collect_index_data, collect_nvda_data, collect_amd_data, collect_avgo_data,
            )
            index_data = collect_index_data() or {}
            nvda_data = collect_nvda_data(mode=mode) or {}
            amd_data = collect_amd_data(mode=mode) or {}
            avgo_data = collect_avgo_data(mode=mode) or {}
        except Exception as exc:
            warnings.append(f"SOX/Nasdaq/basket 데이터 수집 실패: {exc}")

        # ── 한국 반도체 확인 점수 ──
        korea_data = {}
        try:
            from app.data_sources.auto_market_collector import collect_domestic_index_data, collect_investor_flow, collect_kospilab_data
            domestic_index = collect_domestic_index_data() or {}
            investor_flow = collect_investor_flow(mode=mode) or {}
            kospilab = collect_kospilab_data() or {}
            korea_data = {
                "kospi_return_pct": domestic_index.get("kospi_return"),
                "kospi200_return_pct": domestic_index.get("kospi200_return"),
                "foreign_net_buy": investor_flow.get("foreign_net_buy"),
                "institution_net_buy": investor_flow.get("institution_net_buy"),
                "samsung_return_pct": kospilab.get("samsung_reference_return"),
            }
        except Exception as exc:
            warnings.append(f"한국 반도체 확인 데이터 수집 실패: {exc}")

        effective = _run_pipeline(
            mu_data_for_session, mu_ext, df_1min, index_data, nvda_data, amd_data, avgo_data, korea_data, now,
            extra_warnings=warnings,
        )
        log_micron_proxy_prediction(effective)
        return effective
