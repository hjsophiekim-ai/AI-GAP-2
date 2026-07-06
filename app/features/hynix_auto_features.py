"""
hynix_auto_features.py — 자동 수집 데이터에서 예측용 feature 자동 생성.

collect_all() 결과를 받아 predict_hynix() / evaluate_swing_flag()에
필요한 모든 feature를 계산합니다.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd


def build_auto_features(market_data: dict) -> dict:
    """
    auto_market_collector.collect_all() 결과에서 예측 feature 생성.

    Parameters
    ----------
    market_data : collect_all() 반환 dict
        keys: mu, nvda, index, hynix, kospilab

    Returns
    -------
    dict
        micron_features  : compute_micron_features() 호환 dict (11 keys)
        predictor_kwargs : predict_hynix() 추가 인자 dict
        swing_kwargs     : evaluate_swing_flag() 추가 인자 dict
        tech_indicators  : 기술적 지표 dict
        hynix_prev_close : float | None
        data_quality     : 수집된 데이터 품질 요약 (0~1)
        sources          : 각 데이터 소스 현황
    """
    mu_data    = market_data.get("mu", {})
    nvda_data  = market_data.get("nvda", {})
    idx_data   = market_data.get("index", {})
    hynix_data = market_data.get("hynix", {})
    klab_data  = market_data.get("kospilab", {})

    # ── MU micron features ────────────────────────────────────────────────────
    micron_features = _build_micron_features(
        df_1min=mu_data.get("df_1min"),
        current_price=mu_data.get("current_price"),
    )

    # ── 코스피랩 ──────────────────────────────────────────────────────────────
    klab_ret    = klab_data.get("hynix_reference_return")
    klab_price  = klab_data.get("hynix_reference_price")

    # ── NVDA ─────────────────────────────────────────────────────────────────
    nvda_return = nvda_data.get("regular_return") or nvda_data.get("premarket_return")

    # ── 지수 ─────────────────────────────────────────────────────────────────
    sox_return     = idx_data.get("sox_return")
    qqq_return     = idx_data.get("qqq_return")
    usdkrw_change  = idx_data.get("usdkrw_change")

    # ── SK하이닉스 기술적 지표 ────────────────────────────────────────────────
    df_daily   = hynix_data.get("df_daily")
    prev_close = hynix_data.get("prev_close")
    current_price = hynix_data.get("current_price")
    tech_indicators = _build_tech_indicators(df_daily)

    # ── predict_hynix 추가 인자 ───────────────────────────────────────────────
    predictor_kwargs: dict = {
        "kospilab_expected_price":      klab_price,
        "kospilab_expected_return_pct": klab_ret,
        "sox_return_pct":               sox_return,
        "nvda_return_pct":              nvda_return,
        "qqq_return_pct":               qqq_return,
        "usd_krw_change_pct":           usdkrw_change,
        "hynix_prev_close":             prev_close,
        "hynix_current_price":           current_price,
        "hynix_prev_return_pct":        tech_indicators.get("return_3d_pct"),
        "hynix_return_3d_pct":          tech_indicators.get("return_3d_pct"),
        "hynix_return_5d_pct":          tech_indicators.get("return_5d_pct"),
        "hynix_return_10d_pct":         tech_indicators.get("return_10d_pct"),
        "hynix_volume_change_pct":      tech_indicators.get("volume_change_pct"),
    }

    # ── evaluate_swing_flag 추가 인자 ─────────────────────────────────────────
    swing_kwargs: dict = {
        "kospilab_expected_return_pct": klab_ret,
        "tech_indicators":              tech_indicators,
        "sox_return_pct":               sox_return,
        "nvda_return_pct":              nvda_return,
        "qqq_return_pct":               qqq_return,
        "usd_krw_change_pct":           usdkrw_change,
        "hynix_prev_close":             prev_close,
        "hynix_current_price":           current_price,
    }

    # ── 데이터 품질 점수 (MU stale 여부 반영) ─────────────────────────────────
    mu_is_stale = bool(mu_data.get("is_stale"))
    quality = _compute_data_quality(micron_features, predictor_kwargs, tech_indicators, mu_is_stale=mu_is_stale)

    return {
        "micron_features":   micron_features,
        "predictor_kwargs":  predictor_kwargs,
        "swing_kwargs":      swing_kwargs,
        "tech_indicators":   tech_indicators,
        "hynix_prev_close":  prev_close,
        "hynix_current_price": current_price,
        "hynix_daily_count": 0 if df_daily is None else len(df_daily),
        "kospilab_return":   klab_ret,
        "data_quality":      quality,
        "sources": {
            "mu":       mu_data.get("source"),
            "nvda":     nvda_data.get("source"),
            "index":    idx_data.get("source"),
            "hynix":    hynix_data.get("source"),
            "kospilab": klab_data.get("source_status"),
        },
        "mu_is_stale":       mu_is_stale,
        "mu_data_gap_reason": mu_data.get("data_gap_reason", "NORMAL"),
        "mu_last_session":   mu_data.get("last_session"),
    }


def _build_micron_features(
    df_1min: Optional[pd.DataFrame],
    current_price: Optional[dict],
) -> dict:
    """MU 1분봉과 현재가에서 micron_features 생성."""
    try:
        from app.features.micron_premarket_features import compute_micron_features
        return compute_micron_features(
            df_1min=df_1min,
            current_price=current_price,
        )
    except Exception:
        return {k: None for k in [
            "micron_premarket_return", "micron_premarket_open_to_now",
            "micron_premarket_high_to_now", "micron_premarket_low_to_now",
            "micron_premarket_30m_momentum", "micron_premarket_60m_momentum",
            "micron_premarket_vwap", "micron_premarket_volume_change",
            "micron_regular_return", "micron_aftermarket_return",
            "micron_session_strength_score",
        ]}


def _build_tech_indicators(df_daily: Optional[pd.DataFrame]) -> dict:
    """SK하이닉스 일봉에서 기술적 지표 계산."""
    empty = {k: None for k in [
        "rsi_14", "macd", "macd_signal_cross", "ma5_position_pct",
        "ma20_position_pct", "ma60_position_pct", "from_20d_high_pct",
        "from_20d_low_pct", "bollinger_pct", "prev_candle_type",
        "return_3d_pct", "return_5d_pct", "return_10d_pct", "volume_change_pct",
        "atr_14_pct",
    ]}
    if df_daily is None or df_daily.empty:
        return empty
    try:
        from app.models.hynix_swing_flag import compute_hynix_tech_indicators
        return compute_hynix_tech_indicators(df_daily)
    except Exception:
        return empty


def _compute_data_quality(
    micron_features: dict,
    predictor_kwargs: dict,
    tech_indicators: dict,
    mu_is_stale: bool = False,
) -> float:
    """
    수집된 데이터의 품질 점수 계산 (0~1).

    핵심 지표 가중치:
    - MU 강도 점수 존재: 0.30 (단, 미국 개장일에 MU 데이터가 15분 이상
      stale이면 이 항목의 절반만 인정 — 휴장으로 인한 stale은 여기 반영되지
      않는다. collect_mu_data가 휴장/주말일 때는 is_stale=False로 두기 때문)
    - 코스피랩 등락률 존재: 0.25
    - 기술적 지표(RSI) 존재: 0.20
    - NVDA/SOX 존재: 0.15
    - 하이닉스 전일 종가 존재: 0.10
    """
    score = 0.0
    if micron_features.get("micron_session_strength_score") is not None:
        score += 0.15 if mu_is_stale else 0.30
    if predictor_kwargs.get("kospilab_expected_return_pct") is not None:
        score += 0.25
    if tech_indicators.get("rsi_14") is not None:
        score += 0.20
    if predictor_kwargs.get("nvda_return_pct") is not None or predictor_kwargs.get("sox_return_pct") is not None:
        score += 0.15
    if predictor_kwargs.get("hynix_prev_close") is not None:
        score += 0.10
    return round(min(score, 1.0), 2)
