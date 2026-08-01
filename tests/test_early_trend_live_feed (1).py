"""
test_early_trend_live_feed.py — Early Trend Detector의 5초 주기 실시간 가격
히스토리(진짜 5/10/20/30초 기울기) 단위테스트.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.trading import early_trend_live_feed as feed


def _seed_history(prices: list[float], now: datetime, step_seconds: float = 5.0) -> dict:
    history: dict = {}
    start = now - timedelta(seconds=step_seconds * (len(prices) - 1))
    for i, price in enumerate(prices):
        history = feed.record_price_sample(history, "X", price, start + timedelta(seconds=step_seconds * i))
    return history


def test_record_price_sample_trims_older_than_max_history():
    now = datetime(2026, 7, 20, 10, 0, 0)
    history = {}
    for i in range(30):
        history = feed.record_price_sample(history, "X", 1000.0 + i, now + timedelta(seconds=5 * i))
    last_now = now + timedelta(seconds=5 * 29)
    ages = [
        (last_now - datetime.fromisoformat(s["t"])).total_seconds() for s in history["X"]
    ]
    assert all(age <= feed.MAX_HISTORY_SECONDS for age in ages)


def test_record_price_sample_ignores_missing_price():
    history = feed.record_price_sample({}, "X", None, datetime(2026, 7, 20, 10, 0, 0))
    assert history == {}


def test_slope_pct_at_returns_none_with_insufficient_history():
    now = datetime(2026, 7, 20, 10, 0, 0)
    history = feed.record_price_sample({}, "X", 1000.0, now)
    assert feed.slope_pct_at(history, "X", now, 30.0) is None


def test_slope_pct_at_computes_positive_slope_for_uptrend():
    now = datetime(2026, 7, 20, 10, 0, 30)
    prices = [1000.0, 1000.5, 1001.2, 1002.0, 1003.0, 1004.5, 1006.0]
    history = _seed_history(prices, now)
    slope_30s = feed.slope_pct_at(history, "X", now, 30.0)
    assert slope_30s is not None and slope_30s > 0


def test_slope_pct_at_computes_negative_slope_for_downtrend():
    now = datetime(2026, 7, 20, 10, 0, 30)
    prices = [1006.0, 1004.5, 1003.0, 1002.0, 1001.2, 1000.5, 1000.0]
    history = _seed_history(prices, now)
    slope_30s = feed.slope_pct_at(history, "X", now, 30.0)
    assert slope_30s is not None and slope_30s < 0


def test_compute_live_direction_requires_all_available_windows_to_agree():
    now = datetime(2026, 7, 20, 10, 0, 30)
    # 지속적 상승 — 5/10/20/30초 전부 UP으로 일치해야 한다.
    prices = [1000.0, 1000.5, 1001.2, 1002.0, 1003.0, 1004.5, 1006.0]
    history = _seed_history(prices, now)
    result = feed.compute_live_direction(history, "X", now)
    assert result["direction"] == "UP"
    assert result["windows_available"] >= 2


def test_compute_live_direction_returns_none_when_windows_disagree():
    now = datetime(2026, 7, 20, 10, 0, 30)
    # 30초 전 큰 하락 이후 최근 10초는 반등 중 — 짧은 구간(5/10초)은 UP, 긴
    # 구간(20/30초)은 여전히 DOWN을 가리켜 방향이 갈린다.
    prices = [1010.0, 990.0, 980.0, 975.0, 970.0, 972.0, 976.0]
    history = _seed_history(prices, now)
    result = feed.compute_live_direction(history, "X", now)
    assert result["direction"] is None


def test_compute_live_direction_ignores_noise_below_threshold():
    now = datetime(2026, 7, 20, 10, 0, 30)
    # 아주 미세한 흔들림(임계 미만) — 방향을 확정하지 않는다.
    prices = [1000.0, 1000.001, 1000.002, 1000.001, 1000.003, 1000.002, 1000.001]
    history = _seed_history(prices, now)
    result = feed.compute_live_direction(history, "X", now)
    assert result["direction"] is None


def test_live_trade_direction_normalizes_inverse_etf_direction():
    now = datetime(2026, 7, 20, 10, 0, 30)
    history = {}
    for symbol, prices in {
        "000660": [1000, 1002, 1004, 1006],
        "0193T0": [5000, 5010, 5020, 5030],
        "0197X0": [10000, 9980, 9960, 9940],
    }.items():
        start = now - timedelta(seconds=15)
        for i, price in enumerate(prices):
            history = feed.record_price_sample(history, symbol, price, start + timedelta(seconds=5 * i))

    result = feed.compute_live_trade_direction(
        history, now, signal_symbol="000660", long_symbol="0193T0", inverse_symbol="0197X0",
    )

    assert result["direction"] == "UP"
    assert result["up_votes"] >= 2


def test_reversal_candidate_requires_three_factors_for_fifteen_seconds():
    now = datetime(2026, 7, 20, 10, 0, 0)
    weak = feed.update_reversal_candidate_state(
        {}, live_direction="UP", previous_direction="DOWN",
        factors={"signal_slope_reversal": True}, now=now,
    )
    assert weak["status"] == "NONE"

    first = feed.update_reversal_candidate_state(
        {}, live_direction="UP", previous_direction="DOWN",
        factors={"signal_slope_reversal": True, "etf_pair_direction_confirmed": True, "volume_increase": True},
        now=now,
    )
    assert first["status"] == "OBSERVING"

    confirmed = feed.update_reversal_candidate_state(
        first, live_direction="UP", previous_direction="DOWN",
        factors={"signal_slope_reversal": True, "etf_pair_direction_confirmed": True, "volume_increase": True},
        now=now + timedelta(seconds=16),
    )
    assert confirmed["status"] == "REVERSAL_CANDIDATE"
    assert confirmed["existing_direction_blocked"] is True
    assert confirmed["detection_to_confirmation_delay_seconds"] >= 15


def _synthetic_1m_down(n: int = 40, start: float = 100.0, step: float = -0.15):
    import pandas as pd

    rows = []
    price = start
    t0 = datetime(2026, 7, 22, 10, 0, 0)
    for i in range(n):
        o = price
        c = price + step
        h = max(o, c) + 0.02
        l = min(o, c) - 0.02
        rows.append({
            "datetime": t0 + timedelta(minutes=i),
            "open": o, "high": h, "low": l, "close": c, "volume": 1000 + i,
        })
        price = c
    return pd.DataFrame(rows)


def test_structural_live_direction_down_with_three_of_four_factors():
    df = _synthetic_1m_down()
    now = datetime(2026, 7, 22, 10, 40, 0)
    result = feed.compute_structural_live_direction(
        df,
        etf_window_directions={5: "DOWN", 10: "DOWN", 20: "UP", 30: "UP"},
        now=now,
    )
    assert result["down_count"] >= 3
    assert result["direction"] == "DOWN"
    assert result["down_factors"]["lh_ll_3m"] is True
    assert result["down_factors"]["returns_10m_15m_neg"] is True
    assert result["down_factors"]["below_vwap_or_ema"] is True
    assert result["down_factors"]["etf_windows_down_ge2"] is True


def test_structural_live_direction_up_symmetric():
    import pandas as pd

    rows = []
    price = 100.0
    t0 = datetime(2026, 7, 22, 10, 0, 0)
    for i in range(40):
        o = price
        c = price + 0.15
        rows.append({
            "datetime": t0 + timedelta(minutes=i),
            "open": o, "high": max(o, c) + 0.02, "low": min(o, c) - 0.02,
            "close": c, "volume": 1000 + i,
        })
        price = c
    df = pd.DataFrame(rows)
    result = feed.compute_structural_live_direction(
        df,
        etf_window_directions={5: "UP", 10: "UP", 20: "DOWN"},
        now=datetime(2026, 7, 22, 10, 40, 0),
    )
    assert result["direction"] == "UP"
    assert result["up_count"] >= 3


def test_merge_prefers_structural_over_etf_seconds_and_enhanced():
    etf = {"direction": "UP", "up_votes": 2, "down_votes": 0}
    structural = {"direction": "DOWN", "down_count": 3, "up_count": 1, "status": "STRUCTURAL_DOWN"}
    merged = feed.merge_live_trade_direction(etf, structural)
    assert merged["direction"] == "DOWN"
    assert merged["direction_source"] == "structural_minute"
    assert merged["day_bias_excluded"] is True
    assert merged["etf_seconds_direction"] == "UP"


def test_drawdown_gates_forbid_hynix_buy_and_episode_candidate():
    # Peak then drop >1.5% with LH/LL
    import pandas as pd

    rows = []
    t0 = datetime(2026, 7, 22, 10, 0, 0)
    # climb to 100
    for i in range(20):
        p = 90 + i * 0.5
        rows.append({"datetime": t0 + timedelta(minutes=i), "open": p, "high": p + 0.2, "low": p - 0.2, "close": p, "volume": 1000})
    # drop to ~98 (from high 99.5+ → >1.5%)
    high_price = rows[-1]["close"]
    for j in range(20):
        p = high_price * (1.0 - 0.002 * (j + 1))  # gradual decline
        rows.append({
            "datetime": t0 + timedelta(minutes=20 + j),
            "open": p + 0.1, "high": p + 0.15, "low": p - 0.2, "close": p, "volume": 1000,
        })
    df = pd.DataFrame(rows)
    gates = feed.compute_session_drawdown_gates(df, now=t0 + timedelta(minutes=39))
    assert gates["drawdown_from_high_pct"] is not None
    assert gates["drawdown_from_high_pct"] <= -1.0
    assert gates["down_episode_candidate"] is True or gates["forbid_hynix_buy"] is True


def test_event_bonus_expires_after_two_3m_bars():
    now = datetime(2026, 7, 22, 10, 0, 0)
    state = feed.update_event_bonus_state(
        {},
        active_event_keys=["볼린저 하단 이탈 후 회복"],
        now=now,
        completed_3m_index=10,
    )
    assert feed.event_bonus_scale(state, "볼린저 하단 이탈 후 회복") == 1.0

    later = feed.update_event_bonus_state(
        state,
        active_event_keys=["볼린저 하단 이탈 후 회복"],
        now=now + timedelta(minutes=7),
        completed_3m_index=12,  # +2 bars
    )
    assert feed.event_bonus_scale(later, "볼린저 하단 이탈 후 회복") == 0.0
    assert later["events"]["볼린저 하단 이탈 후 회복"]["expired"] is True

    points = [(12.0, "볼린저 하단 이탈 후 회복"), (10.0, "현재가 VWAP 상회")]
    scaled, keys = feed.scale_event_bonus_points(points, later)
    assert keys == ["볼린저 하단 이탈 후 회복"]
    assert scaled == [(10.0, "현재가 VWAP 상회")]


def test_event_bonus_hard_stop_at_ten_minutes():
    now = datetime(2026, 7, 22, 10, 0, 0)
    state = feed.update_event_bonus_state(
        {},
        active_event_keys=["RSI(14) 30 이하 이탈 후 재돌파"],
        now=now,
        completed_3m_index=1,
    )
    # Same 3m index but 11 minutes later → hard decay
    expired = feed.update_event_bonus_state(
        state,
        active_event_keys=["RSI(14) 30 이하 이탈 후 재돌파"],
        now=now + timedelta(minutes=11),
        completed_3m_index=1,
    )
    assert feed.event_bonus_scale(expired, "RSI(14) 30 이하 이탈 후 재돌파") == 0.0


def _synthetic_1m_decline(start: datetime, bars: int = 40, start_price: float = 2_000_000.0):
    import pandas as pd

    rows = []
    price = start_price
    for i in range(bars):
        # Steady decline with LH/LL on 3m structure.
        open_p = price
        close_p = price * (1.0 - 0.0012)
        high_p = open_p * 1.0002
        low_p = close_p * 0.9995
        rows.append({
            "datetime": start + timedelta(minutes=i),
            "open": open_p, "high": high_p, "low": low_p, "close": close_p,
            "volume": 5000 + i * 10,
        })
        price = close_p
    return pd.DataFrame(rows)


def test_structural_live_direction_down_with_three_of_four_factors():
    now = datetime(2026, 7, 22, 10, 50, 0)
    df = _synthetic_1m_decline(datetime(2026, 7, 22, 10, 10, 0), bars=40)
    result = feed.compute_structural_live_direction(
        df,
        etf_window_directions={5: "DOWN", 10: "DOWN", 20: "UP", 30: "UP"},
        now=now,
    )
    assert result["down_count"] >= 3
    assert result["direction"] == "DOWN"
    assert result["down_factors"]["lh_ll_3m"] is True
    assert result["down_factors"]["returns_10m_15m_neg"] is True
    assert result["down_factors"]["below_vwap_or_ema"] is True
    assert result["down_factors"]["etf_windows_down_ge2"] is True


def test_structural_live_direction_up_symmetric():
    now = datetime(2026, 7, 22, 10, 50, 0)
    import pandas as pd

    rows = []
    price = 1_900_000.0
    start = datetime(2026, 7, 22, 10, 10, 0)
    for i in range(40):
        open_p = price
        close_p = price * (1.0 + 0.0012)
        rows.append({
            "datetime": start + timedelta(minutes=i),
            "open": open_p, "high": close_p * 1.0003, "low": open_p * 0.9997,
            "close": close_p, "volume": 5000,
        })
        price = close_p
    df = pd.DataFrame(rows)
    result = feed.compute_structural_live_direction(
        df, etf_window_directions={5: "UP", 10: "UP", 20: "DOWN"}, now=now,
    )
    assert result["direction"] == "UP"
    assert result["up_count"] >= 3


def test_merge_prefers_structural_over_etf_seconds_and_ignores_enhanced():
    etf = {"direction": "UP", "up_votes": 2, "down_votes": 0}
    structural = {"direction": "DOWN", "down_count": 3, "up_count": 0, "status": "STRUCTURAL_DOWN"}
    merged = feed.merge_live_trade_direction(etf, structural)
    assert merged["direction"] == "DOWN"
    assert merged["direction_source"] == "structural_minute"
    assert merged["day_bias_excluded"] is True
    assert merged["etf_seconds_direction"] == "UP"


def test_drawdown_gates_forbid_hynix_buy_and_episode_candidate():
    now = datetime(2026, 7, 22, 11, 0, 0)
    df = _synthetic_1m_decline(datetime(2026, 7, 22, 10, 0, 0), bars=50, start_price=2_000_000.0)
    gates = feed.compute_session_drawdown_gates(df, now=now)
    assert gates["drawdown_from_high_pct"] is not None
    assert gates["drawdown_from_high_pct"] <= -1.0
    assert gates["forbid_hynix_buy"] is True
    assert gates["down_episode_candidate"] is True


def test_event_bonus_decays_after_two_3m_bars():
    now = datetime(2026, 7, 22, 10, 30, 0)
    state = feed.update_event_bonus_state(
        {},
        active_event_keys=["볼린저 하단 이탈 후 회복"],
        now=now,
        completed_3m_index=10,
    )
    assert feed.event_bonus_scale(state, "볼린저 하단 이탈 후 회복") == 1.0

    later = feed.update_event_bonus_state(
        state,
        active_event_keys=["볼린저 하단 이탈 후 회복"],
        now=now + timedelta(minutes=7),
        completed_3m_index=12,  # +2 completed 3m bars
    )
    assert feed.event_bonus_scale(later, "볼린저 하단 이탈 후 회복") == 0.0
    scaled, _ = feed.scale_event_bonus_points(
        [(12.0, "볼린저 하단 이탈 후 회복"), (10.0, "현재가 VWAP 상회")],
        later,
    )
    assert scaled == [(10.0, "현재가 VWAP 상회")]


def test_event_bonus_hard_stop_at_ten_minutes_no_rearm():
    now = datetime(2026, 7, 22, 10, 30, 0)
    state = feed.update_event_bonus_state(
        {},
        active_event_keys=["RSI(14) 30 이하 이탈 후 재돌파"],
        now=now,
        completed_3m_index=5,
    )
    expired = feed.update_event_bonus_state(
        state,
        active_event_keys=["RSI(14) 30 이하 이탈 후 재돌파"],
        now=now + timedelta(minutes=11),
        completed_3m_index=8,
    )
    assert feed.event_bonus_scale(expired, "RSI(14) 30 이하 이탈 후 재돌파") == 0.0
    # Still active on daily signal — must not re-arm a fresh bonus.
    still = feed.update_event_bonus_state(
        expired,
        active_event_keys=["RSI(14) 30 이하 이탈 후 재돌파"],
        now=now + timedelta(minutes=12),
        completed_3m_index=9,
    )
    assert feed.event_bonus_scale(still, "RSI(14) 30 이하 이탈 후 재돌파") == 0.0
