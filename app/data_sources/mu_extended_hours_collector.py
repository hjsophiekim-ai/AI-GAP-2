"""mu_extended_hours_collector.py — MU(마이크론) 정규장/프리마켓/애프터마켓
데이터를 세션별로 구분 수집하고, 장외 흐름을 점수화한다.

데이터 소스 우선순위(명세):
  1) KIS 해외주식 현재가상세/분봉조회 (kis_overseas_minute.py)
  2) Alpaca Market Data API
  3) Polygon aggregate 1m
  4) Finnhub quote (분봉은 무료 플랜 미지원 — 현재가만)
  5) Yahoo/yfinance — 장외 실시간 판단용이 아니라 최후 보조로만 사용
     (성공해도 is_realtime=False, is_delayed=True로 명시 표시)

Alpaca/Polygon/Finnhub/Yahoo 단계는 이미 구축된 app.market.us_market_data의
fetch_us_quote_multi()/fetch_us_minute_bars_dataframe()를 그대로 재사용한다
(중복 구현 금지 — 이 저장소의 기존 다중소스 체인과 반드시 동일하게 동작해야
서로 다른 곳에서 다른 값이 나오는 혼란을 막을 수 있다).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from app.logger import logger
except ImportError:
    logger = logging.getLogger(__name__)

import pandas as pd

from app.utils.data_paths import LOGS_DIR

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = LOGS_DIR / "mu_extended_hours"

REALTIME_SOURCES = {"kis", "alpaca", "polygon", "finnhub"}
DELAYED_SOURCES = {"yahoo", "naver"}
THIN_VOLUME_THRESHOLD = 5_000  # 이 미만이면 장외 거래량이 너무 적다고 판단(신뢰도 감점)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _vwap(df: pd.DataFrame) -> Optional[float]:
    if df is None or df.empty:
        return None
    try:
        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        vol = df["volume"].astype(float)
        total_vol = vol.sum()
        if total_vol <= 0:
            return None
        return float((typical * vol).sum() / total_vol)
    except Exception:
        return None


def _slope_pct(df_1min: pd.DataFrame, minutes: int) -> Optional[float]:
    """1분봉 close 기준 최근 N분간 총 변화율(%). 봉이 부족하면 None."""
    if df_1min is None or len(df_1min) < 2:
        return None
    n = min(minutes, len(df_1min) - 1)
    if n <= 0:
        return None
    closes = df_1min["close"].astype(float)
    start, end = closes.iloc[-1 - n], closes.iloc[-1]
    if start == 0:
        return None
    return round((end / start - 1.0) * 100, 4)


def _fetch_via_kis(mode: str) -> Optional[dict]:
    try:
        from app.data_sources.kis_overseas_minute import (
            build_session_summary, classify_current_session_type,
            fetch_mu_1min_bars, fetch_mu_3min_bars, fetch_mu_5min_bars, fetch_mu_current_price,
        )

        current = fetch_mu_current_price(mode=mode)
        df_1min = fetch_mu_1min_bars(mode=mode)
        if current is None and (df_1min is None or df_1min.empty):
            return None

        df_3min = fetch_mu_3min_bars(mode=mode, source_df=df_1min) if df_1min is not None else None
        df_5min = fetch_mu_5min_bars(mode=mode, source_df=df_1min) if df_1min is not None else None
        summary = build_session_summary(df_1min) if df_1min is not None and not df_1min.empty else pd.DataFrame()

        return {
            "source": "kis", "current_price": current.get("price") if current else None,
            "df_1min": df_1min, "df_3min": df_3min, "df_5min": df_5min, "session_summary": summary,
            "timestamp": current.get("timestamp") if current else _now_iso(),
        }
    except Exception as exc:
        logger.debug("[MUExtendedHours] KIS 수집 실패: %s", exc)
        return None


def _fetch_via_multi_provider() -> Optional[dict]:
    try:
        from app.market import us_market_data as umd

        quote = umd.fetch_us_quote_multi("MU")
        df_1min, bar_source = umd.fetch_us_minute_bars_dataframe("MU", limit=120)
        if not quote.get("success") and df_1min is None:
            return None

        source = bar_source if (df_1min is not None and bar_source not in (None, "none")) else quote.get("source", "none")
        df_3min = df_1min.set_index("datetime").resample("3min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna(subset=["close"]).reset_index() if df_1min is not None and not df_1min.empty else None
        df_5min = df_1min.set_index("datetime").resample("5min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna(subset=["close"]).reset_index() if df_1min is not None and not df_1min.empty else None

        return {
            "source": source, "current_price": quote.get("price"),
            "df_1min": df_1min, "df_3min": df_3min, "df_5min": df_5min, "session_summary": pd.DataFrame(),
            "timestamp": quote.get("timestamp") or _now_iso(),
        }
    except Exception as exc:
        logger.debug("[MUExtendedHours] 다중소스 수집 실패: %s", exc)
        return None


def compute_mu_extended_hours_score(
    mu_data: dict,
    sox_return_pct: Optional[float] = None,
    qqq_return_pct: Optional[float] = None,
    nvda_return_pct: Optional[float] = None,
    amd_return_pct: Optional[float] = None,
) -> dict:
    """
    mu_extended_hours_score(0~100, 높을수록 강세) — 순수 함수. 호출부가 이미
    수집한 SOXX/QQQ/NVDA/AMD 등락률을 넘겨주면(중복 네트워크 호출 방지) 상대
    강도/동조 컴포넌트도 반영한다. 넘기지 않으면 해당 컴포넌트만 제외한다.
    """
    change_pct = mu_data.get("change_pct_from_previous_close")
    slope_3m = mu_data.get("slope_3m")
    slope_15m = mu_data.get("slope_15m")
    session_type = mu_data.get("session_type")
    is_extended = session_type in ("PREMARKET", "AFTERHOURS")

    vwap_ref = mu_data.get("vwap_premarket") if session_type == "PREMARKET" else mu_data.get("vwap_afterhours")
    current_price = mu_data.get("current_price")
    vwap_position = None
    if vwap_ref and current_price:
        vwap_position = (current_price - vwap_ref) / vwap_ref * 100

    volume_1m = mu_data.get("volume_1m")

    def _norm(v, scale):
        if v is None:
            return None
        return max(0.0, min(100.0, 50.0 + (v / scale) * 50.0))

    components = {
        "change_pct_component": _norm(change_pct, 3.0),
        "slope_3m_component": _norm(slope_3m, 0.5),
        "slope_15m_component": _norm(slope_15m, 1.0),
        "vwap_position_component": _norm(vwap_position, 1.0),
        "volume_component": 40.0 if (volume_1m is not None and volume_1m < THIN_VOLUME_THRESHOLD) else (
            60.0 if volume_1m is not None else None
        ),
        "relative_strength_vs_sox": _norm(
            (change_pct - sox_return_pct) if (change_pct is not None and sox_return_pct is not None) else None, 2.0,
        ),
        "relative_strength_vs_qqq": _norm(
            (change_pct - qqq_return_pct) if (change_pct is not None and qqq_return_pct is not None) else None, 2.0,
        ),
        "nvda_amd_comovement": _norm(
            ((nvda_return_pct or 0.0) + (amd_return_pct or 0.0)) / 2.0
            if (nvda_return_pct is not None or amd_return_pct is not None) else None, 3.0,
        ),
    }
    weights = {
        "change_pct_component": 0.25, "slope_3m_component": 0.15, "slope_15m_component": 0.15,
        "vwap_position_component": 0.15, "volume_component": 0.05,
        "relative_strength_vs_sox": 0.12, "relative_strength_vs_qqq": 0.08, "nvda_amd_comovement": 0.05,
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
        "mu_extended_hours_score": score, "components": components,
        "coverage": round(total_w / sum(weights.values()), 2) if weights else 0.0,
        "is_extended_hours": is_extended,
    }


def collect_mu_extended_hours(mode: Optional[str] = None) -> dict:
    """MU 세션 구분 데이터 + 장외 점수를 수집한다. 실패해도 예외를 던지지 않는다."""
    from app.data_sources.kis_overseas_minute import classify_current_session_type, to_dual_timezone

    if mode is None:
        import os
        for candidate in ("real", "mock"):
            if os.environ.get(f"KIS_{candidate.upper()}_APP_KEY") and os.environ.get(f"KIS_{candidate.upper()}_APP_SECRET"):
                mode = candidate
                break

    session_type = classify_current_session_type()
    now = datetime.now()

    fetched = None
    if mode:
        fetched = _fetch_via_kis(mode)
    if fetched is None:
        fetched = _fetch_via_multi_provider()

    result = {
        "symbol": "MU", "session_type": session_type, "current_price": None, "previous_close": None,
        "premarket_price": None, "afterhours_price": None,
        "change_pct_from_previous_close": None, "change_pct_from_regular_close": None,
        "bar_1m": None, "bar_3m": None, "bar_5m": None, "volume_1m": None, "volume_3m": None,
        "vwap_premarket": None, "vwap_afterhours": None,
        "slope_3m": None, "slope_5m": None, "slope_15m": None,
        "data_source": "none", "timestamp": _now_iso(), "timezones": to_dual_timezone(now),
        "freshness_seconds": None, "is_realtime": False, "is_delayed": False, "is_extended_hours": session_type in ("PREMARKET", "AFTERHOURS"),
        "confidence_penalty_reason": [],
    }

    if fetched is None:
        result["confidence_penalty_reason"].append("모든 소스(KIS/Alpaca/Polygon/Finnhub/Yahoo) 수집 실패")
        _log_mu_extended_hours(result, score_info=None)
        return result

    source = fetched.get("source") or "none"
    df_1min, df_3min, df_5min = fetched.get("df_1min"), fetched.get("df_3min"), fetched.get("df_5min")
    session_summary = fetched.get("session_summary")
    current_price = fetched.get("current_price")

    result["data_source"] = source
    result["current_price"] = current_price
    result["is_realtime"] = source in REALTIME_SOURCES
    result["is_delayed"] = source in DELAYED_SOURCES
    if result["is_delayed"]:
        result["confidence_penalty_reason"].append(f"'{source}' 최후 보조 소스 사용 — 장외 실시간성 낮음")

    result["bar_1m"] = "collected" if df_1min is not None and not df_1min.empty else "unavailable"
    result["bar_3m"] = "collected" if df_3min is not None and not df_3min.empty else "unavailable"
    result["bar_5m"] = "collected" if df_5min is not None and not df_5min.empty else "unavailable"

    if df_1min is not None and not df_1min.empty:
        result["volume_1m"] = int(df_1min.iloc[-1]["volume"])
        last_ts = df_1min.iloc[-1]["datetime"]
        try:
            result["freshness_seconds"] = max(0.0, (now - pd.Timestamp(last_ts).to_pydatetime().replace(tzinfo=None)).total_seconds())
        except Exception:
            result["freshness_seconds"] = None
        if result["freshness_seconds"] is not None and result["freshness_seconds"] > 300:
            result["confidence_penalty_reason"].append(f"마지막 봉이 {result['freshness_seconds']:.0f}초 전(5분 초과 지연)")
        result["slope_3m"] = _slope_pct(df_1min, 3)
        result["slope_5m"] = _slope_pct(df_1min, 5)
        result["slope_15m"] = _slope_pct(df_1min, 15)
        if result["volume_1m"] is not None and result["volume_1m"] < THIN_VOLUME_THRESHOLD and result["is_extended_hours"]:
            result["confidence_penalty_reason"].append(f"장외 1분 거래량 {result['volume_1m']}주 — 거래 희박")

    if df_3min is not None and not df_3min.empty:
        result["volume_3m"] = int(df_3min.iloc[-1]["volume"])

    if session_summary is not None and not session_summary.empty:
        pm = session_summary[session_summary["session"] == "premarket"]
        am = session_summary[session_summary["session"] == "aftermarket"]
        reg = session_summary[session_summary["session"] == "regular"]
        if not pm.empty:
            result["premarket_price"] = float(pm.iloc[-1]["close"])
        if not am.empty:
            result["afterhours_price"] = float(am.iloc[-1]["close"])
        if df_1min is not None and not df_1min.empty:
            result["vwap_premarket"] = _vwap(df_1min[df_1min["session"] == "premarket"])
            result["vwap_afterhours"] = _vwap(df_1min[df_1min["session"] == "aftermarket"])
        regular_close_today = float(reg.iloc[-1]["close"]) if not reg.empty else None
    else:
        regular_close_today = None

    try:
        from app.market import us_market_data as umd

        last_session = umd.fetch_us_last_session("MU")
        if last_session.get("success"):
            result["previous_close"] = last_session.get("close")
    except Exception as exc:
        logger.debug("[MUExtendedHours] previous_close 조회 실패: %s", exc)

    if current_price and result["previous_close"]:
        result["change_pct_from_previous_close"] = round((current_price / result["previous_close"] - 1.0) * 100, 4)
    regular_ref = regular_close_today or result["previous_close"]
    if current_price and regular_ref:
        result["change_pct_from_regular_close"] = round((current_price / regular_ref - 1.0) * 100, 4)

    if not result["is_extended_hours"] and session_type != "CLOSED":
        pass  # 정규장: 장외 페널티 대상 아님
    elif result["is_extended_hours"] and result.get("volume_1m") is None:
        result["confidence_penalty_reason"].append("장외 거래량 데이터 없음")

    score_info = compute_mu_extended_hours_score(result)
    result["mu_extended_hours_score"] = score_info["mu_extended_hours_score"]
    result["mu_extended_hours_score_components"] = score_info["components"]

    _log_mu_extended_hours(result, score_info)
    return result


def _log_mu_extended_hours(mu_data: dict, score_info: Optional[dict], reflected_weight: Optional[float] = None) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{datetime.now().strftime('%Y%m%d')}.jsonl"
        record = {
            "timestamp": mu_data.get("timestamp"), "session_type": mu_data.get("session_type"),
            "price": mu_data.get("current_price"), "bar_1m": mu_data.get("bar_1m"), "bar_3m": mu_data.get("bar_3m"),
            "slope_3m": mu_data.get("slope_3m"), "slope_15m": mu_data.get("slope_15m"),
            "score": mu_data.get("mu_extended_hours_score") if score_info else None,
            "source": mu_data.get("data_source"), "freshness": mu_data.get("freshness_seconds"),
            "reflected_hynix_prediction_weight": reflected_weight,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.debug("[MUExtendedHours] 로그 기록 실패(무해): %s", exc)
