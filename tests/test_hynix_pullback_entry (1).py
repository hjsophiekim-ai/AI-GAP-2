"""
test_hynix_pullback_entry.py — detect_pullback() 눌림목 판정 검증.
"""

from __future__ import annotations

import pandas as pd

from app.trading.hynix_pullback_entry import detect_pullback


def _build_df(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2026-07-09 09:10:00", periods=len(closes), freq="min")
    highs = [c + 20 for c in closes]
    lows = [c - 20 for c in closes]
    opens = [c - 5 for c in closes]
    volumes = [1000] * len(closes)
    return pd.DataFrame({"datetime": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes})


def test_insufficient_data_returns_false():
    result = detect_pullback(_build_df([100000, 100010, 100020]))
    assert result["is_pullback"] is False
    assert result["reason"] == "데이터 부족"


def test_shallow_pullback_with_bounce_detected():
    rising = [99000 + i * 95 for i in range(20)]  # 99000 -> 100805
    decline = [100700, 100600, 100500, 100450, 100400, 100390, 100410]  # 반등: 100410>=100390
    result = detect_pullback(_build_df(rising + decline))
    assert result["is_pullback"] is True
    assert result["bounce_confirmed"] is True
    assert 0.3 <= result["pullback_pct"] <= 2.0


def test_pullback_depth_ok_but_no_bounce_yet():
    rising = [99000 + i * 95 for i in range(20)]
    decline = [100700, 100600, 100500, 100450, 100410, 100390, 100370]  # 계속 하락(반등 없음)
    result = detect_pullback(_build_df(rising + decline))
    assert result["is_pullback"] is False
    assert result["bounce_confirmed"] is False


def test_too_shallow_near_highs_not_a_pullback():
    rising = [99000 + i * 95 for i in range(20)]
    decline = [100800, 100798, 100796, 100795, 100793, 100791, 100790]  # 고점 대비 거의 후퇴 없음
    result = detect_pullback(_build_df(rising + decline))
    assert result["is_pullback"] is False
    assert result["pullback_pct"] < 0.3


def test_too_deep_pullback_rejected():
    rising = [99000 + i * 95 for i in range(20)]
    decline = [99500, 98000, 97000, 96500, 96400, 96380, 96420]  # 고점 대비 4%+ 급락
    result = detect_pullback(_build_df(rising + decline))
    assert result["is_pullback"] is False
    assert result["pullback_pct"] > 2.0


def test_breakdown_of_recent_low_rejected_even_if_shallow_pct():
    bars_flat = [100000, 100050, 100000, 100050, 100000, 100050, 100000, 100050, 100000, 100050,
                 100000, 100050, 100000, 100050, 100000]  # 15봉, 최저 종가 100000 형성
    bars_rise = [100200, 100300, 100400, 100500, 100600, 100700, 100800, 100900, 101000, 101000]  # 10봉 상승
    bars_decline = [100200, 100150, 100100, 100010, 100015]  # 최근 저점(100000) 근접까지 후퇴, 반등은 함
    result = detect_pullback(_build_df(bars_flat + bars_rise + bars_decline))
    assert result["is_pullback"] is False
    assert "저점" in result["reason"]
