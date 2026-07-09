"""
hynix_micron_realtime_score.py — 마이크론(MU) 실시간 1분/3분봉 기반 점수.

기존 마이크론 데이터/점수 파이프라인(mu_extended_hours_collector, kis_overseas_minute,
micron_premarket_features)을 그대로 재사용하고, 그 위에 "실시간 1분/3분봉 모멘텀"
점수만 새로 계산한다. 우선순위:
  1) 실시간 1분/3분봉 모멘텀 점수 (data/micron/MU_1min.csv, MU_3min.csv)
  2) micron_session_strength_score (micron_premarket_features.compute_micron_features)
  3) mu_extended_hours_score (mu_extended_hours_collector.compute_mu_extended_hours_score)
  4) 중립값 50
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger

ROOT = Path(__file__).resolve().parent.parent.parent
_MU_1MIN_CSV = ROOT / "data" / "micron" / "MU_1min.csv"
_MU_3MIN_CSV = ROOT / "data" / "micron" / "MU_3min.csv"

_STALE_MINUTES = 20.0

_1MIN_LOOKBACK = 20
_1MIN_SPAN_PCT = 1.0
_3MIN_LOOKBACK = 10
_3MIN_SPAN_PCT = 1.8


def _load_fresh_csv(path: Path) -> Optional[pd.DataFrame]:
    try:
        if not path.exists():
            return None
        df = pd.read_csv(path)
        if df.empty or "datetime" not in df.columns:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        if df.empty:
            return None
        last_ts = df["datetime"].iloc[-1].to_pydatetime().replace(tzinfo=None)
        age_min = (datetime.now() - last_ts).total_seconds() / 60.0
        if age_min > _STALE_MINUTES:
            return None
        return df
    except Exception as exc:
        logger.debug("[MicronRealtimeScore] CSV 로드 실패(%s): %s", path, exc)
        return None


def _momentum_score(df: pd.DataFrame, lookback: int, span_pct: float) -> Optional[float]:
    if df is None or len(df) < 2:
        return None
    work = df.tail(min(lookback, len(df)))
    try:
        first_open = float(work.iloc[0]["open"])
        last_close = float(work.iloc[-1]["close"])
    except Exception:
        return None
    if first_open <= 0:
        return None
    cum_return_pct = (last_close / first_open - 1.0) * 100.0
    score = 50.0 + max(-50.0, min(50.0, cum_return_pct / span_pct * 50.0))

    last = work.iloc[-1]
    try:
        if float(last["close"]) > float(last["open"]):
            score += 3.0
        elif float(last["close"]) < float(last["open"]):
            score -= 3.0
    except Exception:
        pass

    return round(max(0.0, min(100.0, score)), 2)


def calculate_existing_micron_score(mode: Optional[str] = None) -> dict:
    """existing_micron_score 계산 (우선순위 폴백 포함).

    Returns
    -------
    dict: micron_1min_score, micron_3min_score, micron_session_type,
          micron_last_update_time, existing_micron_score, source, warnings
    """
    result = {
        "micron_1min_score": None,
        "micron_3min_score": None,
        "micron_session_type": None,
        "micron_last_update_time": None,
        "existing_micron_score": 50.0,
        "source": "neutral_default",
        "warnings": [],
    }

    ext_result: Optional[dict] = None
    try:
        from app.data_sources.mu_extended_hours_collector import collect_mu_extended_hours

        ext_result = collect_mu_extended_hours(mode=mode)
        result["micron_session_type"] = ext_result.get("session_type")
    except Exception as exc:
        result["warnings"].append(f"mu_extended_hours 수집 실패: {exc}")

    df_1min = _load_fresh_csv(_MU_1MIN_CSV)
    df_3min = _load_fresh_csv(_MU_3MIN_CSV)

    score_1m = _momentum_score(df_1min, _1MIN_LOOKBACK, _1MIN_SPAN_PCT) if df_1min is not None else None
    score_3m = _momentum_score(df_3min, _3MIN_LOOKBACK, _3MIN_SPAN_PCT) if df_3min is not None else None

    if score_1m is not None or score_3m is not None:
        result["micron_1min_score"] = score_1m
        result["micron_3min_score"] = score_3m
        if score_1m is not None and score_3m is not None:
            final = 0.60 * score_1m + 0.40 * score_3m
        elif score_1m is not None:
            final = score_1m
        else:
            final = score_3m
        result["existing_micron_score"] = round(max(0.0, min(100.0, final)), 2)
        result["source"] = "realtime_1m_3m_candles"
        last_ts = None
        if df_1min is not None and not df_1min.empty:
            last_ts = df_1min["datetime"].iloc[-1]
        elif df_3min is not None and not df_3min.empty:
            last_ts = df_3min["datetime"].iloc[-1]
        result["micron_last_update_time"] = last_ts.isoformat() if last_ts is not None else None
        return result

    result["warnings"].append("실시간 1분/3분봉 데이터 없음/지연(20분 초과) — micron_session_strength_score로 대체")

    try:
        from app.features.micron_premarket_features import compute_micron_features

        raw_1min = None
        if _MU_1MIN_CSV.exists():
            try:
                raw_1min = pd.read_csv(_MU_1MIN_CSV)
                raw_1min["datetime"] = pd.to_datetime(raw_1min["datetime"], errors="coerce")
                raw_1min = raw_1min.dropna(subset=["datetime"])
            except Exception:
                raw_1min = None
        features = compute_micron_features(raw_1min)
        strength = features.get("micron_session_strength_score")
        if strength is not None:
            result["existing_micron_score"] = round(max(0.0, min(100.0, float(strength))), 2)
            result["source"] = "micron_session_strength_score"
            result["micron_last_update_time"] = datetime.now().isoformat()
            return result
    except Exception as exc:
        result["warnings"].append(f"micron_session_strength_score 계산 실패: {exc}")

    if ext_result is not None and ext_result.get("mu_extended_hours_score") is not None:
        result["existing_micron_score"] = round(max(0.0, min(100.0, float(ext_result["mu_extended_hours_score"]))), 2)
        result["source"] = "mu_extended_hours_score"
        result["micron_last_update_time"] = ext_result.get("timestamp")
        return result

    result["warnings"].append("모든 마이크론 점수 소스 실패 — 중립값(50) 사용")
    result["micron_last_update_time"] = datetime.now().isoformat()
    return result
