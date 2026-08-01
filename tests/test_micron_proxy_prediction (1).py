"""test_micron_proxy_prediction.py — Micron Proxy Prediction Engine 테스트.

순수 계산 함수(detect_micron_session/validate_micron_data_freshness/calculate_*)를
직접 호출해 네트워크 없이 테스트한다(프로젝트 house style: 이미 수집된 값을
인자로 넘기고 시각도 now 파라미터로 주입).
"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.models.micron_proxy_prediction import (
    SESSION_CLOSED, SESSION_OVERNIGHT_ATS, SESSION_REGULAR, SESSION_STALE_DATA,
    SOURCE_INSUFFICIENT, SOURCE_OVERNIGHT, SOURCE_REAL, SOURCE_SYNTHETIC,
    WARNING_DATA_INSUFFICIENT,
    calculate_effective_micron_score, calculate_nasdaq_futures_score,
    calculate_sox_futures_score, calculate_synthetic_micron_score,
    calculate_micron_recent_trend_score, compute_effective_micron_score_from_market_data,
    detect_micron_session, validate_micron_data_freshness, _time_of_day_weights,
    _coerce_dataframe_input,
)


# 2026-07-13(월)은 KST 밤 23:00 → 정규장(REGULAR), 낮 12:00 → CLOSED.
_REGULAR_KST = datetime(2026, 7, 13, 23, 0)
_CLOSED_KST = datetime(2026, 7, 13, 12, 0)


class TestSessionDetection:
    def test_real_regular_session_is_fresh_and_real(self):
        mu_data = {
            "current_price": 100.0, "last_trade_time": _REGULAR_KST - timedelta(minutes=1),
            "bar_interval": "1min", "volume_1m": 10_000, "source": "kis",
        }
        info = detect_micron_session(mu_data, now=_REGULAR_KST)
        assert info["session"] == SESSION_REGULAR
        assert info["freshness"]["is_fresh"] is True

    def test_overnight_ats_requires_realtime_source_and_evidence(self):
        mu_data = {
            "current_price": 100.0, "last_trade_time": _CLOSED_KST - timedelta(minutes=1),
            "bar_interval": "1min", "volume_1m": 8_000, "source": "kis",
        }
        info = detect_micron_session(mu_data, now=_CLOSED_KST)
        assert info["session"] == SESSION_OVERNIGHT_ATS

    def test_overnight_ats_not_claimed_without_realtime_source(self):
        # 지연 소스(yahoo/naver)만 있는데 시계상 CLOSED면 OVERNIGHT_ATS로 잘못 표기하지 않는다.
        mu_data = {
            "current_price": 100.0, "last_trade_time": _CLOSED_KST - timedelta(minutes=1),
            "bar_interval": "1min", "volume_1m": 8_000, "source": "yahoo",
        }
        info = detect_micron_session(mu_data, now=_CLOSED_KST)
        assert info["session"] == SESSION_CLOSED

    def test_stale_extended_hours_value_not_shown_as_live(self):
        """오래된 extended-hours 값을 live(REGULAR 등)로 표시하지 않는지."""
        mu_data = {
            "current_price": 100.0, "last_trade_time": _REGULAR_KST - timedelta(minutes=20),
            "bar_interval": "1min", "volume_1m": 10_000, "source": "kis",
        }
        info = detect_micron_session(mu_data, now=_REGULAR_KST)
        assert info["session"] == SESSION_STALE_DATA
        assert info["session"] != SESSION_REGULAR

    def test_validate_freshness_thresholds_per_bar_interval(self):
        now = datetime(2026, 7, 13, 10, 0)
        fresh_1min = validate_micron_data_freshness(now - timedelta(minutes=3), "1min", now=now)
        stale_1min = validate_micron_data_freshness(now - timedelta(minutes=8), "1min", now=now)
        fresh_15min = validate_micron_data_freshness(now - timedelta(minutes=25), "15min", now=now)
        assert fresh_1min["is_fresh"] is True
        assert stale_1min["is_fresh"] is False
        assert fresh_15min["is_fresh"] is True


class TestEffectiveMicronScore:
    def test_real_fresh_selects_real_micron_score(self):
        session_info = {"session": SESSION_REGULAR, "freshness": {"is_fresh": True}}
        result = calculate_effective_micron_score(
            session_info=session_info, real_micron_score=80.0, overnight_micron_score=None,
            micron_recent_trend_score=60.0, sox_futures_score=55.0, nasdaq_futures_score=55.0,
            us_semiconductor_proxy_score=55.0, korea_semiconductor_confirmation_score=55.0,
            synthetic_micron_score=55.0,
        )
        assert result["micron_score_source"] == SOURCE_REAL
        assert result["effective_micron_score"] == 80.0

    def test_overnight_fresh_selects_overnight_micron_score(self):
        session_info = {"session": SESSION_OVERNIGHT_ATS, "freshness": {"is_fresh": True}}
        result = calculate_effective_micron_score(
            session_info=session_info, real_micron_score=None, overnight_micron_score=72.0,
            micron_recent_trend_score=60.0, sox_futures_score=55.0, nasdaq_futures_score=55.0,
            us_semiconductor_proxy_score=55.0, korea_semiconductor_confirmation_score=55.0,
            synthetic_micron_score=55.0,
        )
        assert result["micron_score_source"] == SOURCE_OVERNIGHT
        assert result["effective_micron_score"] == 72.0

    def test_stale_switches_to_synthetic(self):
        session_info = {"session": SESSION_STALE_DATA, "freshness": {"is_fresh": False, "reason": "stale"}}
        result = calculate_effective_micron_score(
            session_info=session_info, real_micron_score=80.0, overnight_micron_score=None,
            micron_recent_trend_score=60.0, sox_futures_score=65.0, nasdaq_futures_score=60.0,
            us_semiconductor_proxy_score=58.0, korea_semiconductor_confirmation_score=52.0,
            synthetic_micron_score=63.0,
        )
        assert result["micron_score_source"] == SOURCE_SYNTHETIC
        assert result["effective_micron_score"] == 63.0
        assert any("STALE" in w or "synthetic" in w for w in result["warnings"])

    def test_all_external_data_missing_returns_neutral_with_warning(self):
        session_info = {"session": "DATA_UNAVAILABLE", "freshness": None}
        result = calculate_effective_micron_score(
            session_info=session_info, real_micron_score=None, overnight_micron_score=None,
            micron_recent_trend_score=None, sox_futures_score=None, nasdaq_futures_score=None,
            us_semiconductor_proxy_score=None, korea_semiconductor_confirmation_score=None,
            synthetic_micron_score=None,
        )
        assert result["micron_score_source"] == SOURCE_INSUFFICIENT
        assert result["effective_micron_score"] == 50.0
        assert WARNING_DATA_INSUFFICIENT in result["warnings"]


class TestSoxNasdaqProxyScores:
    def test_sox_score_increases_with_higher_return(self):
        low = calculate_sox_futures_score(-1.0)
        high = calculate_sox_futures_score(2.0)
        assert high["sox_futures_score"] > low["sox_futures_score"]

    def test_nasdaq_confidence_lower_when_opposite_direction_from_sox(self):
        agree = calculate_nasdaq_futures_score(2.0, sox_futures_score=90.0)
        disagree = calculate_nasdaq_futures_score(-2.0, sox_futures_score=90.0)
        assert agree["confidence_multiplier"] > disagree["confidence_multiplier"]
        assert disagree["direction_agrees_with_sox"] is False


class TestSyntheticScoreAndWeights:
    def test_synthetic_score_rises_with_sox_score(self):
        low = calculate_synthetic_micron_score(
            sox_futures_score=20.0, nasdaq_futures_score=50.0, us_semiconductor_proxy_score=50.0,
            micron_recent_trend_score=50.0, korea_semiconductor_confirmation_score=50.0, now_hm="10:30",
        )
        high = calculate_synthetic_micron_score(
            sox_futures_score=90.0, nasdaq_futures_score=50.0, us_semiconductor_proxy_score=50.0,
            micron_recent_trend_score=50.0, korea_semiconductor_confirmation_score=50.0, now_hm="10:30",
        )
        assert high["synthetic_micron_score"] > low["synthetic_micron_score"]

    def test_korea_weight_higher_in_afternoon_than_morning(self):
        morning = _time_of_day_weights("09:30")
        afternoon = _time_of_day_weights("14:00")
        assert afternoon["korea"] > morning["korea"]

    def test_confidence_lower_when_korea_conflicts_with_us_signals(self):
        aligned = calculate_synthetic_micron_score(
            sox_futures_score=80.0, nasdaq_futures_score=80.0, us_semiconductor_proxy_score=80.0,
            micron_recent_trend_score=50.0, korea_semiconductor_confirmation_score=75.0, now_hm="11:00",
        )
        conflict = calculate_synthetic_micron_score(
            sox_futures_score=80.0, nasdaq_futures_score=80.0, us_semiconductor_proxy_score=80.0,
            micron_recent_trend_score=50.0, korea_semiconductor_confirmation_score=20.0, now_hm="11:00",
        )
        assert conflict["korea_conflicts_with_us"] is True
        assert conflict["confidence"] < aligned["confidence"]


class TestPredictionAIV2Integration:
    def test_predictor_uses_effective_micron_score(self):
        from app.models.hynix_price_predictor import predict_hynix_multi_horizon

        market_data = {
            "mu": {}, "nvda": {}, "amd": {}, "avgo": {},
            "index": {"sox_return": 0.5, "qqq_return": 0.5},
            "domestic_index": {"kospi_return": 0.2}, "investor_flow": {}, "kospilab": {}, "hynix_minute": {},
        }
        bullish_proxy = {"effective_micron_score": 95.0, "micron_data_confidence": 90.0, "micron_score_source": "synthetic_micron"}
        bearish_proxy = {"effective_micron_score": 5.0, "micron_data_confidence": 90.0, "micron_score_source": "synthetic_micron"}

        bullish = predict_hynix_multi_horizon(
            market_data=market_data, hynix_current_price=200_000, hynix_prev_close=198_000,
            tech_indicators={}, micron_proxy=bullish_proxy,
        )
        bearish = predict_hynix_multi_horizon(
            market_data=market_data, hynix_current_price=200_000, hynix_prev_close=198_000,
            tech_indicators={}, micron_proxy=bearish_proxy,
        )
        assert bullish["effective_micron_score"] == 95.0
        assert bearish["effective_micron_score"] == 5.0
        assert bullish["expected_return_pct_close"] > bearish["expected_return_pct_close"]

    def test_low_micron_confidence_reduces_overall_confidence(self):
        from app.models.hynix_price_predictor import predict_hynix_multi_horizon

        market_data = {
            "mu": {}, "nvda": {}, "amd": {}, "avgo": {},
            "index": {"sox_return": 0.5, "qqq_return": 0.5},
            "domestic_index": {"kospi_return": 0.2}, "investor_flow": {}, "kospilab": {}, "hynix_minute": {},
        }
        high_conf_proxy = {"effective_micron_score": 60.0, "micron_data_confidence": 100.0}
        low_conf_proxy = {"effective_micron_score": 60.0, "micron_data_confidence": 10.0}

        high = predict_hynix_multi_horizon(
            market_data=market_data, hynix_current_price=200_000, hynix_prev_close=198_000,
            tech_indicators={}, micron_proxy=high_conf_proxy,
        )
        low = predict_hynix_multi_horizon(
            market_data=market_data, hynix_current_price=200_000, hynix_prev_close=198_000,
            tech_indicators={}, micron_proxy=low_conf_proxy,
        )
        assert low["confidence_close"] < high["confidence_close"]


class TestDataFrameInputValidation:
    """섹션 7 — 'str' object has no attribute 'empty' 재발 방지 테스트."""

    def _sample_df(self):
        return pd.DataFrame({
            "datetime": pd.date_range("2026-07-10 09:00", periods=5, freq="1min"),
            "open": [100.0] * 5, "high": [101.0] * 5, "low": [99.0] * 5,
            "close": [100.5, 100.6, 100.4, 100.7, 100.8], "volume": [1000] * 5,
        })

    def test_dataframe_input_accepted(self):
        df, status = _coerce_dataframe_input(self._sample_df())
        assert status == "OK"
        assert df is not None

    def test_string_file_path_input(self, tmp_path):
        csv_path = tmp_path / "mu_1min.csv"
        self._sample_df().to_csv(csv_path, index=False)
        df, status = _coerce_dataframe_input(str(csv_path))
        assert status == "FILE_PATH_LOADED"
        assert df is not None and len(df) == 5

    def test_missing_file_path_input(self, tmp_path):
        df, status = _coerce_dataframe_input(str(tmp_path / "does_not_exist.csv"))
        assert df is None
        assert status == "FILE_PATH_NOT_FOUND"

    def test_session_status_string_input(self):
        """state가 JSON 왕복하며 DataFrame이 str(df) repr로 직렬화된 경우를 흉내."""
        df, status = _coerce_dataframe_input("REGULAR")
        assert df is None
        assert status == "INVALID_TYPE_STR_SESSION"

    def test_none_input(self):
        df, status = _coerce_dataframe_input(None)
        assert df is None
        assert status == "NONE"

    def test_empty_dataframe_input(self):
        df, status = _coerce_dataframe_input(pd.DataFrame())
        assert df is None
        assert status == "EMPTY"

    def test_calculate_micron_recent_trend_score_never_crashes_on_bad_input(self):
        for bad_input in (None, "REGULAR", pd.DataFrame(), 12345, ["not", "a", "df"]):
            result = calculate_micron_recent_trend_score(df_1min=bad_input)
            assert "micron_recent_trend_score" in result
            assert result["df_1min_source_status"] in (
                "NONE", "INVALID_TYPE_STR_SESSION", "EMPTY", "STALE_TYPE_MISMATCH", "FILE_PATH_NOT_FOUND",
            )

    def test_compute_effective_score_never_crashes_when_df_1min_is_a_stale_string(self):
        """실제 사고 재현: market_data['mu']['df_1min']이 JSON 왕복으로 문자열이 된 경우."""
        market_data = {
            "mu": {
                "df_1min": "   datetime  open  high  low  close  volume\n0  2026-07-10 ...",
                "extended_hours": {"current_price": 100.0, "data_source": "kis", "freshness_seconds": 30.0},
            },
            "nvda": {}, "amd": {}, "avgo": {}, "index": {}, "domestic_index": {},
            "investor_flow": {}, "kospilab": {},
        }
        result = compute_effective_micron_score_from_market_data(market_data)
        assert "effective_micron_score" in result
        assert result["df_1min_source_status"] == "INVALID_TYPE_STR_SESSION"
        assert any("입력 타입 이상" in w for w in result.get("warnings", []))
