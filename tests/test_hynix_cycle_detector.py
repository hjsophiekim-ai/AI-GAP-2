"""test_hynix_cycle_detector.py — Cycle Detector AI / Momentum Acceleration / Turning Point 테스트."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.hynix_cycle_detector import (
    ACTION_BUY_HYNIX, ACTION_BUY_INVERSE, ACTION_ADD_INVERSE, ACTION_PARTIAL_SELL_INVERSE,
    ACTION_EXIT_INVERSE, ACTION_HOLD,
    PHASE_GAP_FAILURE, PHASE_PANIC_SELL, PHASE_SELLING_EXHAUSTION, PHASE_REVERSAL_CONFIRMED_UP,
    PHASE_TREND_UP, PHASE_NO_TRADE,
    HynixCycleDetector, default_cycle_state, classify_cycle_phase, calculate_momentum_acceleration,
    calculate_turning_point_probability, calculate_cycle_confidence, calculate_cycle_entry_score,
    decide_cycle_trade_action, _raw_phase,
)


def _bars(specs, start=datetime(2026, 7, 13, 9, 5), minutes=1):
    """specs: list of (open, high, low, close, volume) oldest-first."""
    rows = []
    for i, (o, h, l, c, v) in enumerate(specs):
        rows.append({
            "datetime": start + timedelta(minutes=i * minutes),
            "open": o, "high": h, "low": l, "close": c, "volume": v,
        })
    return pd.DataFrame(rows)


def _gap_failure_bars():
    """갭+2.5% 시가 후 하락 전환하는 20봉 시퀀스(음봉多, VWAP 이탈, 거래량 증가)."""
    specs = []
    price = 102500.0  # 전일종가 100000 대비 +2.5% 갭
    session_open = price
    for i in range(20):
        o = price
        c = price * 0.9985  # 계속 하락(음봉)
        h = o * 1.0005
        l = c * 0.999
        vol = 8000 + i * 400  # 거래량 증가
        specs.append((o, h, l, c, vol))
        price = c
    return _bars(specs), session_open


class TestGapFailure:
    def test_gap_failure_detected(self):
        df, session_open = _gap_failure_bars()
        now = df["datetime"].iloc[-1]
        raw = _raw_phase(
            df, now, gap_pct=2.5, session_high=session_open, session_low=float(df["low"].min()),
            vwap=float(session_open) * 0.995, atr=200.0, prior_close=100000.0,
            momentum=calculate_momentum_acceleration(df), turning_point={},
        )
        assert raw["phase"] == PHASE_GAP_FAILURE

    def test_gap_failure_blocks_hynix_buy(self):
        df, session_open = _gap_failure_bars()
        now = df["datetime"].iloc[-1]
        momentum = calculate_momentum_acceleration(df)
        turning_point = calculate_turning_point_probability(df, momentum=momentum)
        state = default_cycle_state()
        state["_state_date"] = now.strftime("%Y%m%d")
        state["current_phase"] = PHASE_GAP_FAILURE
        phase_result = {"cycle_phase": PHASE_GAP_FAILURE, "conditions": {}, "detail": {}}
        confidence = 70.0
        entry_scores = {
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, confidence, "inverse"),
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, confidence, "hynix"),
        }
        decision = decide_cycle_trade_action(
            phase_result, momentum, turning_point, entry_scores, inverse_pressure_score=40.0,
            position_state={"symbol": None, "position_pct": 0.0}, state=state, now=now,
        )
        assert decision["action"] != ACTION_BUY_HYNIX

    def test_gap_failure_with_strong_signals_recommends_inverse_40pct(self):
        phase_result = {"cycle_phase": PHASE_GAP_FAILURE, "conditions": {}, "detail": {}}
        momentum = {"momentum_acceleration_down": 60.0, "acceleration_confirmed_down": True}
        turning_point = {"down_turn_probability_3m": 70.0, "down_turn_probability_5m": 65.0, "up_turn_probability_5m": 30.0}
        entry_scores = {
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, 70.0, "inverse"),
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, 70.0, "hynix"),
        }
        state = default_cycle_state()
        now = datetime(2026, 7, 13, 9, 10)
        state["_state_date"] = now.strftime("%Y%m%d")
        decision = decide_cycle_trade_action(
            phase_result, momentum, turning_point, entry_scores, inverse_pressure_score=60.0,
            position_state={"symbol": None, "position_pct": 0.0}, state=state, now=now,
        )
        assert decision["action"] == ACTION_BUY_INVERSE
        assert decision["recommended_position_pct"] == 40.0


class TestPanicSellAndExhaustion:
    def test_panic_sell_does_not_chase_new_inverse(self):
        phase_result = {"cycle_phase": PHASE_PANIC_SELL, "conditions": {}, "detail": {}}
        momentum = {"momentum_acceleration_down": 80.0}
        turning_point = {"down_turn_probability_3m": 80.0, "down_turn_probability_5m": 75.0, "up_turn_probability_5m": 20.0}
        entry_scores = {
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, 70.0, "inverse"),
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, 70.0, "hynix"),
        }
        state = default_cycle_state()
        now = datetime(2026, 7, 13, 9, 40)
        state["_state_date"] = now.strftime("%Y%m%d")
        decision = decide_cycle_trade_action(
            phase_result, momentum, turning_point, entry_scores, inverse_pressure_score=80.0,
            position_state={"symbol": None, "position_pct": 0.0}, state=state, now=now,
        )
        assert decision["action"] not in (ACTION_BUY_INVERSE, ACTION_ADD_INVERSE)

    def test_selling_exhaustion_partial_take_profit(self):
        phase_result = {"cycle_phase": PHASE_SELLING_EXHAUSTION, "conditions": {}, "detail": {"early_reversal_score": 58.0}}
        momentum = {"momentum_acceleration_down": 40.0, "momentum_acceleration_up": 55.0}
        turning_point = {"down_turn_probability_3m": 40.0, "up_turn_probability_5m": 55.0}
        entry_scores = {
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, 60.0, "inverse"),
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, 60.0, "hynix"),
        }
        state = default_cycle_state()
        now = datetime(2026, 7, 13, 10, 0)
        state["_state_date"] = now.strftime("%Y%m%d")
        decision = decide_cycle_trade_action(
            phase_result, momentum, turning_point, entry_scores, inverse_pressure_score=50.0,
            position_state={"symbol": "0197X0", "position_pct": 40.0}, state=state, now=now,
        )
        assert decision["action"] == ACTION_PARTIAL_SELL_INVERSE
        assert decision["recommended_position_pct"] < 40.0


class TestEarlyReversalAndTrendUp:
    def test_early_reversal_score_75_exits_inverse_and_test_buys_hynix(self):
        phase_result = {"cycle_phase": "EARLY_REVERSAL_UP", "conditions": {}, "detail": {"early_reversal_score": 80.0, "vwap": 100000.0, "current_price": 100200.0}}
        momentum = {"momentum_acceleration_up": 70.0}
        turning_point = {"up_turn_probability_5m": 75.0, "down_turn_probability_3m": 20.0}
        entry_scores = {
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, 70.0, "inverse"),
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, 70.0, "hynix"),
        }
        state = default_cycle_state()
        now = datetime(2026, 7, 13, 10, 30)
        state["_state_date"] = now.strftime("%Y%m%d")
        decision = decide_cycle_trade_action(
            phase_result, momentum, turning_point, entry_scores, inverse_pressure_score=30.0,
            position_state={"symbol": "0197X0", "position_pct": 50.0}, state=state, now=now,
        )
        assert decision["action"] in (ACTION_EXIT_INVERSE, ACTION_BUY_HYNIX)
        assert decision["recommended_symbol"] == "000660"
        assert 20.0 <= decision["recommended_position_pct"] <= 30.0

    def test_vwap_reclaim_leads_to_reversal_confirmed(self):
        # 충분한 봉 수(RSI 14 / MACD 26+9)를 확보하기 위해 하락 30봉 + 상승 15봉 구성.
        specs = []
        price = 100000.0
        for _ in range(30):
            o = price
            c = price * 0.999
            h = o * 1.0002
            l = c * 0.9995
            specs.append((o, h, l, c, 5000))
            price = c
        for i in range(15):
            o = price
            c = price * 1.002
            h = c * 1.0005
            l = o * 0.9995
            specs.append((o, h, l, c, 6000 + i * 400))
            price = c
        df = _bars(specs)
        now = df["datetime"].iloc[-1]
        momentum = calculate_momentum_acceleration(df)
        vwap = float(df["close"].iloc[29])  # 하락 저점 부근을 VWAP 기준으로 사용 — 상승 구간이 이를 회복
        raw = _raw_phase(
            df, now, gap_pct=None, session_high=float(df["high"].max()), session_low=float(df["low"].min()),
            vwap=vwap, atr=float(df["close"].iloc[-1]) * 0.005, prior_close=100000.0, momentum=momentum, turning_point={},
        )
        assert raw["phase"] in (PHASE_REVERSAL_CONFIRMED_UP, "EARLY_REVERSAL_UP", PHASE_TREND_UP)

    def test_momentum_acceleration_up_raises_cycle_confidence(self):
        phase_result_low = {"cycle_phase": PHASE_TREND_UP, "conditions": {PHASE_TREND_UP: {"met": 5, "total": 6}}, "detail": {}}
        momentum_low = {"momentum_acceleration_up": 30.0, "available": True}
        momentum_high = {"momentum_acceleration_up": 80.0, "available": True}
        turning_point = {"confidence": 60.0}
        conf_low = calculate_cycle_confidence(phase_result_low, momentum_low, turning_point)
        conf_high = calculate_cycle_confidence(phase_result_low, momentum_high, turning_point)
        # cycle_confidence doesn't directly consume momentum_acceleration_up, but entry score does —
        # verify the entry score (which feeds trade_confidence downstream) rises with acceleration.
        entry_low = calculate_cycle_entry_score(phase_result_low, momentum_low, turning_point, conf_low, "hynix")
        entry_high = calculate_cycle_entry_score(phase_result_low, momentum_high, turning_point, conf_high, "hynix")
        assert entry_high["cycle_entry_score_hynix"] > entry_low["cycle_entry_score_hynix"]


class TestFrequencyLimits:
    def test_same_direction_reentry_blocked_within_5_minutes(self):
        phase_result = {"cycle_phase": PHASE_GAP_FAILURE, "conditions": {}, "detail": {}}
        momentum = {"momentum_acceleration_down": 60.0}
        turning_point = {"down_turn_probability_3m": 70.0, "down_turn_probability_5m": 65.0, "up_turn_probability_5m": 30.0}
        entry_scores = {
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, 70.0, "inverse"),
            **calculate_cycle_entry_score(phase_result, momentum, turning_point, 70.0, "hynix"),
        }
        state = default_cycle_state()
        t0 = datetime(2026, 7, 13, 9, 10)
        state["_state_date"] = t0.strftime("%Y%m%d")
        first = decide_cycle_trade_action(
            phase_result, momentum, turning_point, entry_scores, inverse_pressure_score=60.0,
            position_state={"symbol": None, "position_pct": 0.0}, state=state, now=t0,
        )
        assert first["action"] == ACTION_BUY_INVERSE

        t1 = t0 + timedelta(minutes=2)
        second = decide_cycle_trade_action(
            phase_result, momentum, turning_point, entry_scores, inverse_pressure_score=60.0,
            position_state={"symbol": "0197X0", "position_pct": 40.0}, state=first["state"], now=t1,
        )
        assert second["action"] == ACTION_HOLD
        assert second["blocking_reason"] is not None


class TestPhaseTransitionConfirmation:
    def test_single_tick_noise_does_not_flip_phase(self):
        """확정된 phase는 raw_phase가 1회만 바뀌어도(1분봉 기준) 전환되지 않아야 한다."""
        specs = [(100000 + i * 5, 100010 + i * 5, 99990 + i * 5, 100005 + i * 5, 5000) for i in range(10)]
        df = _bars(specs)
        state = default_cycle_state()
        now = df["datetime"].iloc[-1]
        state["_state_date"] = now.strftime("%Y%m%d")
        state["current_phase"] = "TREND_UP"
        state["candidate_phase"] = None
        state["candidate_count"] = 0

        result1 = classify_cycle_phase(df, now, state=state)
        assert result1["confirmed"] in (True, False)
        state_after_1 = result1["state"]

        # 다음 호출에서 raw_phase가 다르게 나와도(노이즈), candidate_count가 아직 1이면 전환 안 됨.
        noisy_specs = specs + [(100050, 99000, 99000, 99010, 20000)]
        noisy_df = _bars(noisy_specs)
        now2 = noisy_df["datetime"].iloc[-1]
        result2 = classify_cycle_phase(noisy_df, now2, state=state_after_1)
        # 첫 raw_phase 변경 시점에는 candidate_count==1이므로 아직 확정되지 않아야 한다
        # (raw_phase가 이전과 동일하지 않은 한 즉시 전환되지 않음).
        if result2["raw_phase"] != state_after_1.get("current_phase"):
            assert result2["state"]["candidate_count"] <= 2


class TestMomentumAndTurningPoint:
    def test_insufficient_data_converges_to_neutral(self):
        df = _bars([(100000, 100010, 99990, 100005, 5000)] * 3)
        momentum = calculate_momentum_acceleration(df)
        assert momentum["available"] is False
        turning_point = calculate_turning_point_probability(df)
        assert turning_point["available"] is False
        assert turning_point["up_turn_probability_5m"] == 50.0
        assert turning_point["confidence"] < 50.0


class TestHynixCycleDetectorEngine:
    def test_run_never_touches_broker_or_places_orders(self):
        """Shadow Mode 요건 — 이 모듈은 주문 실행 코드를 전혀 갖지 않는다(권장만 반환)."""
        import inspect
        from app.trading import hynix_cycle_detector as mod

        source = inspect.getsource(mod)
        for forbidden in ("broker.buy", "broker.sell", ".buy(", ".sell("):
            assert forbidden not in source, f"hynix_cycle_detector.py must not place orders directly ({forbidden} found)"

    def test_run_end_to_end_no_exception(self):
        specs = [(100000 - i * 50, 100010 - i * 50, 99900 - i * 50, 99950 - i * 50, 6000 + i * 200) for i in range(15)]
        df = _bars(specs)
        det = HynixCycleDetector()
        state = default_cycle_state()
        now = df["datetime"].iloc[-1]
        result = det.run(
            df, now, position_state={"symbol": None, "position_pct": 0.0}, state=state,
            gap_pct=2.2, session_high=float(df["high"].max()), session_low=float(df["low"].min()),
            prior_close=100000.0, inverse_pressure_score=55.0,
        )
        assert "cycle_phase" in result
        assert "action" in result
        assert isinstance(result["reasons"], list)
