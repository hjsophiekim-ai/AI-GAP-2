"""
test_kis_overseas_minute.py — KIS 해외주식 분봉 수집 모듈 테스트.

API 실제 호출 없이 mock 데이터로 로직을 검증합니다.
"""

import pandas as pd
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.data_sources.kis_overseas_minute import (
    classify_session,
    build_session_summary,
    fetch_mu_3min_bars,
    save_mu_data,
)


# ── 테스트용 1분봉 샘플 ──────────────────────────────────────────────────────

def _make_1min_df() -> pd.DataFrame:
    """프리마켓 30봉 + 정규장 10봉 포함 샘플."""
    rows = []
    # 프리마켓: 17:00 ~ 17:29
    for i in range(30):
        rows.append({
            "datetime": datetime(2026, 6, 29, 17, i),
            "open":  100.0 + i * 0.1,
            "high":  100.5 + i * 0.1,
            "low":   99.8  + i * 0.1,
            "close": 100.2 + i * 0.1,
            "volume": 1000 + i * 10,
            "session": "premarket",
        })
    # 정규장: 22:30 ~ 22:39
    for i in range(10):
        rows.append({
            "datetime": datetime(2026, 6, 29, 22, 30 + i),
            "open":  103.0 + i * 0.2,
            "high":  103.8 + i * 0.2,
            "low":   102.5 + i * 0.2,
            "close": 103.5 + i * 0.2,
            "volume": 5000 + i * 100,
            "session": "regular",
        })
    return pd.DataFrame(rows)


class TestClassifySession:
    """classify_session 함수 단위 테스트."""

    def test_premarket(self):
        dt = datetime(2026, 6, 29, 18, 0)  # 18:00 KST
        assert classify_session(dt) == "premarket"

    def test_regular_evening(self):
        dt = datetime(2026, 6, 29, 23, 0)  # 23:00 KST
        assert classify_session(dt) == "regular"

    def test_regular_early_morning(self):
        dt = datetime(2026, 6, 30, 3, 0)   # 03:00 KST (다음날)
        assert classify_session(dt) == "regular"

    def test_aftermarket(self):
        dt = datetime(2026, 6, 30, 6, 0)   # 06:00 KST
        assert classify_session(dt) == "aftermarket"

    def test_boundary_premarket_start(self):
        dt = datetime(2026, 6, 29, 17, 0)
        assert classify_session(dt) == "premarket"

    def test_boundary_regular_start(self):
        dt = datetime(2026, 6, 29, 22, 30)
        assert classify_session(dt) == "regular"


class TestBuildSessionSummary:
    """build_session_summary 함수 테스트."""

    def test_summary_columns(self):
        df = _make_1min_df()
        summary = build_session_summary(df)
        assert not summary.empty
        required = {"session", "open", "high", "low", "close", "volume", "return_pct"}
        assert required.issubset(set(summary.columns))

    def test_premarket_return_positive(self):
        df = _make_1min_df()
        summary = build_session_summary(df)
        pm = summary[summary["session"] == "premarket"]
        assert not pm.empty
        # 샘플은 가격이 오르므로 등락률 양수여야 함
        assert pm.iloc[0]["return_pct"] > 0

    def test_empty_input(self):
        result = build_session_summary(pd.DataFrame())
        assert result.empty


class TestFetchMu3minBars:
    """3분봉 resample 테스트."""

    def test_3min_resample(self):
        df_1min = _make_1min_df()
        df_3min = fetch_mu_3min_bars(source_df=df_1min)
        assert df_3min is not None
        assert not df_3min.empty
        # 3분봉은 1분봉보다 행 수가 적어야 함
        assert len(df_3min) < len(df_1min)

    def test_3min_columns(self):
        df_1min = _make_1min_df()
        df_3min = fetch_mu_3min_bars(source_df=df_1min)
        assert df_3min is not None
        required = {"datetime", "open", "high", "low", "close", "volume", "session"}
        assert required.issubset(set(df_3min.columns))

    def test_none_input(self):
        # source_df=None이면 API 호출 시도 → mock으로 None 반환
        with patch(
            "app.data_sources.kis_overseas_minute.fetch_mu_1min_bars",
            return_value=None,
        ):
            result = fetch_mu_3min_bars(mode="mock", source_df=None)
        assert result is None

    def test_3min_high_gte_low(self):
        df_1min = _make_1min_df()
        df_3min = fetch_mu_3min_bars(source_df=df_1min)
        assert df_3min is not None
        assert (df_3min["high"] >= df_3min["low"]).all()


class TestSaveMuData:
    """저장 함수 — 실제 디스크 쓰기 테스트."""

    def test_save_creates_files(self, tmp_path, monkeypatch):
        import app.data_sources.kis_overseas_minute as mod
        monkeypatch.setattr(mod, "_MICRON_DIR", tmp_path)

        df = _make_1min_df()
        save_mu_data(df_1min=df, df_3min=df)

        assert (tmp_path / "MU_1min.csv").exists()
        assert (tmp_path / "MU_3min.csv").exists()

    def test_save_none_no_error(self, tmp_path, monkeypatch):
        import app.data_sources.kis_overseas_minute as mod
        monkeypatch.setattr(mod, "_MICRON_DIR", tmp_path)
        save_mu_data(df_1min=None, df_3min=None, df_summary=None)
