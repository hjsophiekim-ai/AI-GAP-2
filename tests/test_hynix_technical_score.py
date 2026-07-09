"""
test_hynix_technical_score.py — calculate_hynix_technical_score() 검증.

볼린저 하단 이탈 후 회복 / Williams %R -80 이하 이탈 후 회복 시
점수가 상승하는지를 실제 가격 시퀀스로 비교 검증한다.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.models.hynix_technical_score import calculate_hynix_technical_score, _breached_then_recovered


def _build_dip_daily(recover: bool, n_flat: int = 30) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n_flat + 6, freq="D")
    closes = [100_000 + (i % 2) * 50 for i in range(n_flat)]
    closes += [96_000, 93_000, 90_000, 88_000, 87_000]
    closes.append(96_000 if recover else 86_500)

    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    opens = [c * 0.999 for c in closes]
    volumes = [1_000_000] * len(closes)

    return pd.DataFrame({
        "datetime": dates[: len(closes)], "open": opens, "high": highs,
        "low": lows, "close": closes, "volume": volumes,
    })


def test_bollinger_and_williams_recovery_increases_score():
    df_recover = _build_dip_daily(recover=True)
    df_persist = _build_dip_daily(recover=False)

    result_recover = calculate_hynix_technical_score(df_recover, None)
    result_persist = calculate_hynix_technical_score(df_persist, None)

    assert result_recover["hynix_technical_score"] > result_persist["hynix_technical_score"]


def test_breached_then_recovered_helper():
    breached = pd.Series([False, False, True, True, False])
    assert _breached_then_recovered(breached) is True

    still_breached = pd.Series([False, True, True, True, True])
    assert _breached_then_recovered(still_breached) is False

    never_breached = pd.Series([False, False, False, False, False])
    assert _breached_then_recovered(never_breached) is False


def test_insufficient_data_returns_neutral_score():
    tiny_df = pd.DataFrame({
        "datetime": pd.date_range("2026-01-01", periods=3, freq="D"),
        "open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3], "close": [1, 2, 3], "volume": [1, 1, 1],
    })
    result = calculate_hynix_technical_score(tiny_df, None)
    assert result["hynix_technical_score"] == 50.0
    assert result["warnings"]
