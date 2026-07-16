"""
test_adaptive_market_regime.py — ADAPTIVE_MARKET_REGIME 통합 엔진 검증.

요구된 6개 케이스:
  1) 갭하락 후 좁은 횡보는 RANGE
  2) 거래량 동반 3분 급락은 PANIC
  3) 강한 추세는 작은 반대봉(1회 후보)에도 유지(2회 연속 확인 전까지 유지)
  4) RANGE에서 +2% 전량익절 및 -0.8% 손절 프로필
  5) 장세 변경 시 설정값(리스크 프로필) 자동 교체
  6) 신규진입 게이트 오류가 손절·청산을 막지 않음(엔진 레벨 격리 — 여기서는
     get_risk_profile()이 알 수 없는/예외적인 입력에도 항상 안전한 폴백을
     반환해 하위 소비자(Dynamic Exit)가 절대 예외로 멈추지 않는지 검증한다)
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.adaptive_market_regime import (
    classify_raw_regime, get_risk_profile, default_regime_confirmation_state,
    update_regime_confirmation, displayed_regime, is_opposite_trend,
    is_chase_blocked, is_entry_at_recent_extreme, opposite_signal_response,
    compute_and_confirm_regime, adaptive_regime_to_primary_trend_result,
    count_reversal_signals, default_reversal_switch_state, evaluate_reversal_switch,
    mark_reversal_stage_executed,
    REVERSAL_ACTION_REDUCE_EXISTING, REVERSAL_ACTION_FULL_EXIT,
    REVERSAL_ACTION_EXPLORATORY_ENTRY_OPPOSITE, REVERSAL_ACTION_EXPAND_OPPOSITE,
    REVERSAL_ACTION_REDUCE_TO_CORE,
    STRONG_UP, STRONG_DOWN, RANGE, VOLATILE_RANGE, HIGH_VOLATILITY, PANIC,
    REVERSAL_CANDIDATE_UP, REVERSAL_CANDIDATE_DOWN, DATA_INSUFFICIENT,
)


def _bars(prices: list[float], volumes: list[float] | None = None, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 7, 16, 9, 0)
    volumes = volumes or [1000.0] * len(prices)
    rows = []
    for i, (p, v) in enumerate(zip(prices, volumes)):
        rows.append({
            "datetime": start + timedelta(minutes=i),
            "open": p, "high": p * 1.001, "low": p * 0.999, "close": p, "volume": v,
        })
    return pd.DataFrame(rows)


def test_gap_down_then_narrow_range_is_range():
    """요구사항 — 갭하락 후 좁은 횡보는 RANGE."""
    prices = [98.0]
    # 98 근처에서 아주 좁게(±0.3% 이내) 오르내리며 90분간 횡보 — 뚜렷한 추세/변동성 없음.
    for i in range(1, 90):
        wiggle = 0.15 if i % 2 == 0 else -0.1
        prices.append(round(prices[-1] + wiggle, 2))
        # 98 근처로 살짝 되돌린다(진짜 추세로 번지지 않게)
        prices[-1] = 98.0 + (prices[-1] - 98.0) * 0.3
    df = _bars(prices)

    result = classify_raw_regime(df, prev_close=100.0)

    assert result["gap_direction"] == "DOWN"
    assert result["regime"] == RANGE


def test_volume_spike_3min_crash_is_panic():
    """요구사항 — 거래량 동반 3분 급락은 PANIC."""
    # 안정적인 90분 이후, 마지막 3분에 급락 + 거래량 폭증.
    prices = [100.0 + (0.02 if i % 2 == 0 else -0.02) for i in range(90)]
    volumes = [1000.0] * 90
    # 마지막 3분: 급락(3분간 -3%) + 거래량 5배.
    crash_prices = [prices[-1] * 0.99, prices[-1] * 0.975, prices[-1] * 0.97]
    crash_volumes = [5000.0, 6000.0, 7000.0]
    prices.extend(crash_prices)
    volumes.extend(crash_volumes)
    df = _bars(prices, volumes)

    result = classify_raw_regime(df)

    assert result["return_3m_pct"] is not None and result["return_3m_pct"] < -1.0
    assert result["relative_volume"] is not None and result["relative_volume"] >= 2.0
    assert result["regime"] == PANIC


def test_strong_trend_survives_single_opposite_candidate():
    """요구사항 — 강한 추세는 작은 반대봉(1회 후보)에도 유지된다. 2회 연속
    확인 전까지는 confirmed_regime이 그대로 유지돼야 한다."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_regime_confirmation_state()
    state = update_regime_confirmation(state, STRONG_UP, now)
    state = update_regime_confirmation(state, STRONG_UP, now)  # 이미 STRONG_UP이면 즉시 유지(첫 확정)
    assert state["confirmed_regime"] == STRONG_UP

    # 단 1회, 반대(하락) 후보가 잡혀도 아직 확정되면 안 된다.
    later = now + timedelta(minutes=3)
    state = update_regime_confirmation(state, STRONG_DOWN, later)
    assert state["confirmed_regime"] == STRONG_UP  # 아직 유지
    assert state["candidate_regime"] == STRONG_DOWN
    assert state["candidate_count"] == 1
    # 이 대기 상태는 REVERSAL_CANDIDATE_DOWN으로 노출된다(단, 실제 판단 기준은
    # 여전히 STRONG_UP 유지).
    assert displayed_regime(state) == REVERSAL_CANDIDATE_DOWN

    # 2회 연속 확인되면 그제서야 전환된다.
    even_later = later + timedelta(minutes=3)
    state = update_regime_confirmation(state, STRONG_DOWN, even_later)
    assert state["confirmed_regime"] == STRONG_DOWN
    assert state["previous_regime"] == STRONG_UP


def test_hard_override_bypasses_confirmation_immediately():
    """하드손절/15:15 강제청산/반대추세 확정은 확인 절차 없이 즉시 전환된다."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_regime_confirmation_state()
    state = update_regime_confirmation(state, STRONG_UP, now)
    state = update_regime_confirmation(state, STRONG_UP, now)
    assert state["confirmed_regime"] == STRONG_UP

    forced = update_regime_confirmation(state, RANGE, now, hard_override=PANIC)
    assert forced["confirmed_regime"] == PANIC
    assert forced["previous_regime"] == STRONG_UP


def test_regime_confirmation_exposes_reversal_alias_fields():
    now = datetime(2026, 7, 16, 10, 0)
    state = default_regime_confirmation_state()
    state = update_regime_confirmation(state, STRONG_UP, now)
    state = update_regime_confirmation(state, STRONG_UP, now)

    candidate = update_regime_confirmation(state, STRONG_DOWN, now + timedelta(minutes=3))
    assert candidate["reversal_direction"] == "DOWN"
    assert candidate["reversal_confirmation_count"] == 1

    confirmed = update_regime_confirmation(candidate, STRONG_DOWN, now + timedelta(minutes=6))
    assert confirmed["previous_confirmed_regime"] == STRONG_UP
    assert confirmed["regime_changed_at"] == (now + timedelta(minutes=6)).isoformat(timespec="seconds")


def test_range_profile_matches_spec_take_profit_and_stop_loss():
    """요구사항 — RANGE에서 +2% 전량익절 및 -0.8% 손절."""
    profile = get_risk_profile(RANGE)

    assert profile["tp2_pct"] == 2.0
    assert profile["tp2_ratio"] == 1.0
    assert profile["sl_pct"] == 0.8
    assert profile["max_hold_minutes"] == 20


def test_regime_change_swaps_risk_profile_automatically():
    """요구사항 — 장세 변경 시 설정값(익절/손절/트레일링/비중)이 자동으로 교체된다."""
    range_profile = get_risk_profile(RANGE)
    panic_profile = get_risk_profile(PANIC)
    strong_profile = get_risk_profile(STRONG_UP)
    high_vol_profile = get_risk_profile(HIGH_VOLATILITY)

    assert range_profile["sl_pct"] != panic_profile["sl_pct"]
    assert panic_profile["position_pct_multiplier"] < range_profile["position_pct_multiplier"]
    assert strong_profile["uses_trailing"] is True and range_profile["uses_trailing"] is False
    assert high_vol_profile["position_pct_multiplier"] == 0.5
    assert 0.10 <= panic_profile["position_pct_multiplier"] <= 0.20


def test_data_insufficient_blocks_new_entries_but_has_safe_fallback():
    """DATA_INSUFFICIENT는 신규주문을 금지하되(요구사항2), 기존 포지션 보호를
    위한 값 자체는 항상 안전하게(예외 없이) 반환되어야 한다(요구사항6의 엔진
    레벨 대응 — get_risk_profile()은 어떤 장세를 넣어도 절대 예외를 던지지
    않는다)."""
    profile = get_risk_profile(DATA_INSUFFICIENT)
    assert profile.get("block_new_entries") is True

    # 존재하지 않는/손상된 regime 값이 들어와도 예외 없이 RANGE로 폴백해야 한다 —
    # 이래야 신규진입 게이트 계산이 실패해도 청산(Dynamic Exit) 쪽은 항상 안전한
    # 프로필을 받아 정상 동작을 계속할 수 있다.
    fallback = get_risk_profile("NOT_A_REAL_REGIME")
    assert fallback == get_risk_profile(RANGE)


def test_is_opposite_trend_helper():
    assert is_opposite_trend(STRONG_UP, STRONG_DOWN) is True
    assert is_opposite_trend(STRONG_UP, STRONG_UP) is False
    assert is_opposite_trend(STRONG_UP, RANGE) is False


def test_classify_raw_regime_insufficient_data_returns_data_insufficient():
    assert classify_raw_regime(None)["regime"] == DATA_INSUFFICIENT
    assert classify_raw_regime(pd.DataFrame())["regime"] == DATA_INSUFFICIENT
    short_df = _bars([100.0] * 5)
    assert classify_raw_regime(short_df)["regime"] == DATA_INSUFFICIENT


# ── VOLATILE_RANGE(2026-07-16 요구사항) ──────────────────────────────────────

def _bars_with_range(prices: list[float], range_pct: float = 0.5, start: datetime | None = None) -> pd.DataFrame:
    """high/low에 range_pct%만큼의 폭을 줘 ATR%를 임의로 키울 수 있는 bar 생성기."""
    start = start or datetime(2026, 7, 16, 9, 0)
    rows = []
    for i, p in enumerate(prices):
        rows.append({
            "datetime": start + timedelta(minutes=i),
            "open": p, "high": p * (1 + range_pct / 100), "low": p * (1 - range_pct / 100),
            "close": p, "volume": 1000.0,
        })
    return pd.DataFrame(rows)


def _zigzag_prices(cycles: int = 8, cycle_minutes: int = 8, amplitudes: list[float] | None = None, base: float = 100.0) -> list[float]:
    """base를 중심으로 오르내리는 휩쏘 패턴. cycle마다 진폭을 다르게 줘야
    한다 — 모든 cycle의 진폭이 완전히 같으면 15분봉으로 리샘플했을 때 고점/저점이
    우연히 정확히 같은 값으로 반복되어(비항상 부등식 >=/<=가 동시에 참이 되는
    퇴화 케이스) swing 구조가 "항상 정렬된 것처럼" 잘못 보인다."""
    amplitudes = amplitudes or [0.8, 1.4, 1.0, 1.6, 0.9, 1.3, 1.1, 1.5]
    prices: list[float] = []
    half = cycle_minutes // 2
    for c in range(cycles):
        amp = amplitudes[c % len(amplitudes)]
        for i in range(1, half + 1):
            prices.append(round(base * (1 + amp / 100 * i / half), 4))
        for i in range(1, half + 1):
            prices.append(round(base * (1 + amp / 100 * (half - i) / half), 4))
    return prices


def test_volatile_range_classification_choppy_box():
    """요구사항 — 최근 VWAP 교차 3회 이상 + ATR 기준 변동성 존재 + (추세불일치/
    잦은 스윙반전/낮은 방향이동효율 중 최소 1개)이면 VOLATILE_RANGE로 분류된다."""
    prices = _zigzag_prices(cycles=8, cycle_minutes=8, base=100.0)
    df = _bars_with_range(prices, range_pct=0.6)

    result = classify_raw_regime(df)

    assert result["vwap_cross_count"] is not None and result["vwap_cross_count"] >= 3
    assert result["atr_pct"] is not None and result["atr_pct"] >= 1.0
    assert result["regime"] == VOLATILE_RANGE
    assert result["box_high"] is not None and result["box_low"] is not None
    assert result["box_high"] > result["box_low"]


def test_volatile_range_not_triggered_when_atr_too_low():
    """VWAP 교차는 잦아도 ATR(변동성) 자체가 낮으면(좁고 조용한 RANGE) VOLATILE_RANGE로
    보지 않는다 — 좁은 횡보 회귀 케이스와 반드시 구분돼야 한다."""
    prices = _zigzag_prices(cycles=8, cycle_minutes=8, base=100.0)
    df = _bars_with_range(prices, range_pct=0.05)  # ATR이 낮도록 고저폭을 아주 좁힘

    result = classify_raw_regime(df)

    assert result["atr_pct"] is not None and result["atr_pct"] < 1.0
    assert result["regime"] != VOLATILE_RANGE


def test_volatile_range_profile_matches_spec():
    """요구사항(2026-07-16, 초단기 실행모드 최종판) — +0.8%에서 50% 매도, +1.3%에서
    전량매도, -0.6%에서 전량손절, 최대 보유 8분, 최초 10%/2회 확인 시 20~25%."""
    profile = get_risk_profile(VOLATILE_RANGE)

    assert profile["tp1_pct"] == 0.8 and profile["tp1_ratio"] == 0.5
    assert profile["tp2_pct"] == 1.3 and profile["tp2_ratio"] == 1.0
    assert profile["sl_pct"] == 0.6
    assert profile["max_hold_minutes"] == 8
    assert profile["entry_stage_1_pct"] == 0.10
    assert 0.20 <= profile["entry_stage_2_pct"] <= 0.25
    assert 0.10 <= profile["position_pct_min"] <= profile["position_pct_max"] <= 0.25
    assert profile["block_big_trend_holding"] is True
    assert profile["block_wide_legacy_stop_loss"] is True
    assert profile["exit_on_opposite_signal_confirmations"] == 2
    assert profile["opposite_signal_reduce_confirmations"] == 1
    assert profile["opposite_signal_reduce_ratio"] == 0.5
    assert profile["require_box_edge_entry"] is True
    assert profile["consecutive_loss_threshold"] == 2
    assert profile["consecutive_loss_cooldown_minutes"] == 20
    assert profile["chase_block_move_pct"] == 0.7
    assert profile["no_chase_at_recent_extreme_minutes"] == 3
    assert profile["pullback_wait_max_seconds"] == 30
    assert 15 <= profile["fast_watcher_interval_seconds"] <= 20
    assert profile["switch_recheck_seconds"] == 20
    assert profile["switch_reentry_pct"] == 0.10


# ── STRONG_UP/DOWN 엄격한 정렬 조건(2026-07-16 요구사항) ─────────────────────

def _steady_rise_prices(minutes: int = 120, start_price: float = 100.0, per_minute_pct: float = 0.05) -> list[float]:
    prices = []
    price = start_price
    for _ in range(minutes):
        price = round(price * (1 + per_minute_pct / 100), 4)
        prices.append(price)
    return prices


def test_strong_up_requires_full_alignment_of_trend_vwap_swing():
    """요구사항 — STRONG_UP은 15분/30분 추세, VWAP, 고저점 구조가 전부 같은
    방향으로 정렬돼야 하고(느슨한 다수결이 아님), 추세 지속시간·방향이동효율·
    거래량 또는 ATR 확인까지 충족해야 한다. 꾸준한 상승 구간(+실제 변동폭)은
    이 조건들을 모두 자연스럽게 만족한다."""
    prices = _steady_rise_prices(minutes=120, per_minute_pct=0.05)
    df = _bars_with_range(prices, range_pct=0.6)

    result = classify_raw_regime(df)

    assert result["trend_15m"] == "UP"
    assert result["trend_30m"] == "UP"
    assert result["above_vwap"] is True
    assert result["swing"].get("higher_high") and result["swing"].get("higher_low")
    assert result["trend_duration_minutes"] >= 15
    assert result["regime"] == STRONG_UP


def test_strong_down_requires_full_alignment_of_trend_vwap_swing():
    prices = _steady_rise_prices(minutes=120, per_minute_pct=-0.05)
    df = _bars_with_range(prices, range_pct=0.6)

    result = classify_raw_regime(df)

    assert result["trend_15m"] == "DOWN"
    assert result["trend_30m"] == "DOWN"
    assert result["above_vwap"] is False
    assert result["regime"] == STRONG_DOWN


def test_strong_up_rejected_when_no_volume_or_atr_confirmation():
    """요구사항 — 추세 정렬 조건을 만족해도 거래량/ATR 확인이 없으면(너무 조용한
    시세면) STRONG으로 확정하지 않는다."""
    prices = _steady_rise_prices(minutes=120, per_minute_pct=0.05)
    df = _bars(prices)  # 기본 padding(0.1%)은 ATR/거래량 확인 임계값에 못 미침

    result = classify_raw_regime(df)

    assert result["regime"] != STRONG_UP


# ── VOLATILE_RANGE 초단기 실행 보호(2026-07-16) — CHASE_BLOCK/최근극값/반대신호 ──

def test_chase_block_triggers_when_etf_moved_past_threshold():
    """요구사항 — 신호 발생 후 ETF가 이미 0.7% 이상 움직였으면 CHASE_BLOCK."""
    result = is_chase_blocked(signal_reference_price=10_000.0, current_price=10_075.0, regime=VOLATILE_RANGE)
    assert result["blocked"] is True
    assert result["moved_pct"] == pytest.approx(0.75, abs=0.01)


def test_chase_block_allows_entry_within_threshold():
    result = is_chase_blocked(signal_reference_price=10_000.0, current_price=10_050.0, regime=VOLATILE_RANGE)
    assert result["blocked"] is False
    assert result["moved_pct"] == pytest.approx(0.5, abs=0.01)


def test_chase_block_is_noop_for_regimes_without_the_setting():
    result = is_chase_blocked(signal_reference_price=10_000.0, current_price=10_500.0, regime=RANGE)
    assert result["blocked"] is False
    assert result["threshold_pct"] is None


def test_no_chase_at_recent_high_blocks_buy_entry():
    """요구사항 — 최근 3분 고점 부근에서 매수 추격진입 금지."""
    df = _bars([100.0, 100.5, 101.0, 101.5, 102.0])
    blocked = is_entry_at_recent_extreme(current_price=102.0, df_1min=df, direction="BUY", regime=VOLATILE_RANGE)
    assert blocked is True


def test_no_chase_at_recent_low_blocks_inverse_entry():
    df = _bars([102.0, 101.5, 101.0, 100.5, 100.0])
    # 마지막 bar의 low(100.0*0.999=99.9)에 바짝 붙은 가격 — "최근 저점 부근".
    blocked = is_entry_at_recent_extreme(current_price=99.92, df_1min=df, direction="SELL", regime=VOLATILE_RANGE)
    assert blocked is True


def test_entry_away_from_extreme_is_not_blocked():
    df = _bars([100.0, 101.0, 100.5, 101.5, 100.8])
    blocked = is_entry_at_recent_extreme(current_price=100.8, df_1min=df, direction="BUY", regime=VOLATILE_RANGE)
    assert blocked is False


def test_opposite_signal_response_reduces_then_exits():
    """요구사항 — 반대 강신호 1회면 50% 축소, 2회면 전량청산."""
    assert opposite_signal_response(0, VOLATILE_RANGE) is None
    reduce = opposite_signal_response(1, VOLATILE_RANGE)
    assert reduce["action"] == "SELL_PARTIAL" and reduce["ratio"] == pytest.approx(0.5)
    exit_all = opposite_signal_response(2, VOLATILE_RANGE)
    assert exit_all["action"] == "SELL_ALL" and exit_all["ratio"] == 1.0


def test_opposite_signal_response_noop_for_regimes_without_the_setting():
    assert opposite_signal_response(2, RANGE) is None


# ── 단일 진입점(compute_and_confirm_regime) — 신규진입/스위칭/손절/익절/보유시간
# 모두 이 하나의 결과를 공유한다(요구사항3) ──────────────────────────────────

def test_compute_and_confirm_regime_returns_confirmed_profile_and_state():
    prices = _steady_rise_prices(minutes=120, per_minute_pct=0.05)
    df = _bars_with_range(prices, range_pct=0.6)
    now = datetime(2026, 7, 16, 10, 0)

    result = compute_and_confirm_regime(df, confirmation_state=None, now=now)

    assert result["raw_regime"] == STRONG_UP
    # 최초 1회 확인만으로는 confirmed_regime이 즉시 STRONG_UP으로 바뀌지 않는다
    # (default_regime_confirmation_state()는 DATA_INSUFFICIENT에서 시작 — 2회
    # 연속 확인이 필요하다).
    assert result["confirmed_regime"] == DATA_INSUFFICIENT
    assert result["confirmation_state"]["candidate_regime"] == STRONG_UP
    assert result["confirmation_state"]["candidate_count"] == 1

    result2 = compute_and_confirm_regime(df, confirmation_state=result["confirmation_state"], now=now)
    assert result2["confirmed_regime"] == STRONG_UP
    assert result2["profile"] == get_risk_profile(STRONG_UP)
    assert result2["previous_regime"] == DATA_INSUFFICIENT


# ── PRIMARY_TREND 브릿지(2026-07-16, 남은 통합 작업1) — 별도 재분류 없이 이미
# 계산된 adaptive_regime 결과에서만 파생시킨다 ──────────────────────────────

def test_adaptive_regime_to_primary_trend_result_maps_strong_up_to_up():
    adaptive_result = {
        "confirmed_regime": STRONG_UP, "reasons": ["above VWAP", "15m trend UP"],
        "snapshot": {
            "gap_direction": "UP", "gap_pct": 0.3, "above_vwap": True, "vwap": 100_000.0,
            "trend_15m": "UP", "trend_30m": "UP", "ema20_slope_pct": 0.1,
            "swing": {"higher_high": True, "higher_low": True}, "relative_volume": 1.5,
            "up_votes": 6, "down_votes": 0, "computed_at": "2026-07-16T10:00:00",
        },
    }
    result = adaptive_regime_to_primary_trend_result(adaptive_result)
    assert result["primary_trend"] == "UP"
    assert result["above_vwap"] is True
    assert result["trend_15m"] == "UP" and result["trend_30m"] == "UP"
    assert result["swing_15m"] == {"higher_high": True, "higher_low": True}


def test_adaptive_regime_to_primary_trend_result_maps_strong_down_to_down():
    adaptive_result = {"confirmed_regime": STRONG_DOWN, "snapshot": {"above_vwap": False}}
    result = adaptive_regime_to_primary_trend_result(adaptive_result)
    assert result["primary_trend"] == "DOWN"


def test_adaptive_regime_to_primary_trend_result_maps_everything_else_to_range():
    for regime in (RANGE, VOLATILE_RANGE, HIGH_VOLATILITY, PANIC, REVERSAL_CANDIDATE_UP, REVERSAL_CANDIDATE_DOWN, DATA_INSUFFICIENT):
        result = adaptive_regime_to_primary_trend_result({"confirmed_regime": regime, "snapshot": {}})
        assert result["primary_trend"] == "RANGE"


def test_adaptive_regime_to_primary_trend_result_handles_none_input():
    result = adaptive_regime_to_primary_trend_result(None)
    assert result["primary_trend"] == "RANGE"


# ── 장세별 LIVE 프로필 최종 확인(2026-07-16, 남은 통합 작업4) ─────────────────

def test_live_profiles_match_final_spec_exactly():
    strong_up = get_risk_profile(STRONG_UP)
    assert strong_up["tp1_pct"] == 2.0 and strong_up["tp1_ratio"] == 0.25  # +2%에서 25% 익절
    assert strong_up["uses_trailing"] is True  # 75% ATR trailing
    strong_down = get_risk_profile(STRONG_DOWN)
    assert strong_down["tp1_pct"] == 2.0 and strong_down["tp1_ratio"] == 0.25
    assert strong_down["uses_trailing"] is True

    range_profile = get_risk_profile(RANGE)
    assert range_profile["tp1_pct"] == 1.5 and range_profile["tp2_pct"] == 2.0
    assert range_profile["sl_pct"] == 0.8
    assert range_profile["max_hold_minutes"] == 20

    volatile_range = get_risk_profile(VOLATILE_RANGE)
    assert volatile_range["tp1_pct"] == 0.8 and volatile_range["tp2_pct"] == 1.3
    assert volatile_range["sl_pct"] == 0.6
    assert volatile_range["max_hold_minutes"] == 8

    panic = get_risk_profile(PANIC)
    assert 0.10 <= panic["position_pct_multiplier"] <= 0.20  # 소액진입
    assert 0.6 <= panic["sl_pct"] <= 0.8
    assert panic["max_hold_minutes"] == 10


# ── 장중 다중 추세전환 상태머신(2026-07-16) ───────────────────────────────────

def _snapshot(**overrides) -> dict:
    base = {
        "above_vwap": True, "trend_5m": "UP", "trend_15m": "UP", "trend_30m": "UP",
        "swing": {"higher_high": True, "higher_low": True, "lower_high": False, "lower_low": False},
        "ema20_slope_pct": 0.05, "relative_volume": 1.0,
    }
    base.update(overrides)
    return base


def _down_reversal_snapshot(rel_vol=2.0) -> dict:
    """STRONG_UP 보유 중 5개 신호 전부(VWAP/추세/스윙/EMA/거래량)가 하락반전을 가리키는 스냅샷."""
    return _snapshot(
        above_vwap=False, trend_5m="DOWN", trend_15m="DOWN",
        swing={"higher_high": False, "higher_low": False, "lower_high": True, "lower_low": True},
        ema20_slope_pct=-0.05, relative_volume=rel_vol,
    )


def _up_reversal_snapshot(rel_vol=2.0) -> dict:
    """STRONG_DOWN 보유 중 5개 신호 전부가 상승반전을 가리키는 스냅샷."""
    return _snapshot(
        above_vwap=True, trend_5m="UP", trend_15m="UP",
        swing={"higher_high": True, "higher_low": True, "lower_high": False, "lower_low": False},
        ema20_slope_pct=0.05, relative_volume=rel_vol,
    )


def test_count_reversal_signals_all_five_against_up():
    votes, reasons = count_reversal_signals(_down_reversal_snapshot(), "UP")
    assert votes == 5
    assert len(reasons) == 5


def test_count_reversal_signals_all_five_against_down():
    votes, reasons = count_reversal_signals(_up_reversal_snapshot(), "DOWN")
    assert votes == 5


def test_count_reversal_signals_below_threshold_with_calm_snapshot():
    # 보유방향(UP)과 정렬된 평범한 스냅샷 — 반전 신호가 없어야 한다.
    votes, _ = count_reversal_signals(_snapshot(), "UP")
    assert votes == 0


def test_evaluate_reversal_switch_no_position_resets_state():
    state = {"direction": "UP", "confirm_count": 2, "stage": "REDUCED"}
    result = evaluate_reversal_switch(state, held_direction=None, snapshot=_snapshot(), now=datetime(2026, 7, 16, 10, 0))
    assert result["direction"] is None
    assert result["confirm_count"] == 0
    assert result["pending_action"] is None


def test_evaluate_reversal_switch_single_confirmation_only_reduces_partially():
    """요구사항 — 가짜 반전 1회에서 전액 스위칭 금지: 1회 확인은 부분 축소만
    트리거하고 전량청산/스위칭은 절대 하지 않는다."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_reversal_switch_state()

    result = evaluate_reversal_switch(state, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now)

    assert result["stage"] == "REDUCED"
    assert result["confirm_count"] == 1
    action = result["pending_action"]
    assert action["action"] == REVERSAL_ACTION_REDUCE_EXISTING
    assert 0.25 <= action["ratio"] <= 0.50
    assert action["block_new_adds"] is True


def test_evaluate_reversal_switch_unconfirmed_tick_resets_before_any_stage():
    """1회 확인 신호 자체가 부족하면(3개 미만) 아무 조치도 없고, 아직 REDUCED
    단계에 진입하지 않았다면 confirm_count가 리셋된다."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_reversal_switch_state()

    weak = _snapshot(above_vwap=False)  # VWAP 하나만 반전(1표) — 3표 미만
    result = evaluate_reversal_switch(state, held_direction="UP", snapshot=weak, now=now)

    assert result["pending_action"] is None
    assert result["stage"] == "NONE"
    assert result["confirm_count"] == 0


def test_evaluate_reversal_switch_two_confirmations_triggers_full_exit():
    """요구사항 — 2회 확인 시 기존 포지션 전량청산."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_reversal_switch_state()

    r1 = evaluate_reversal_switch(state, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now)
    assert r1["stage"] == "REDUCED"

    later = now + timedelta(seconds=25)
    r2 = evaluate_reversal_switch(r1, held_direction="UP", snapshot=_down_reversal_snapshot(), now=later)

    assert r2["stage"] == "FULLY_EXITED"
    assert r2["confirm_count"] == 2
    assert r2["pending_action"]["action"] == REVERSAL_ACTION_FULL_EXIT
    assert r2["pending_action"]["ratio"] == 1.0


def test_evaluate_reversal_switch_blocks_opposite_entry_until_flat_confirmed():
    """요구사항 — 잔량 0 확인 전 반대 ETF 주문 금지."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_reversal_switch_state()
    r1 = evaluate_reversal_switch(state, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now)
    r2 = evaluate_reversal_switch(r1, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now + timedelta(seconds=25))
    assert r2["stage"] == "FULLY_EXITED"

    # 잔량이 아직 0이 아니면(broker_confirmed_flat=False) 반대 ETF 탐색진입이 나가면 안 된다.
    r3 = evaluate_reversal_switch(r2, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now + timedelta(seconds=50), broker_confirmed_flat=False)
    assert r3["pending_action"] is None
    assert r3["stage"] == "FULLY_EXITED"

    # 잔량 0이 확인되면 그제서야 탐색진입.
    r4 = evaluate_reversal_switch(r3, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now + timedelta(seconds=75), broker_confirmed_flat=True)
    assert r4["stage"] == "FLAT_CONFIRMED"
    assert r4["pending_action"]["action"] == REVERSAL_ACTION_EXPLORATORY_ENTRY_OPPOSITE
    assert 0.10 <= r4["pending_action"]["ratio"] <= 0.20


def test_evaluate_reversal_switch_third_confirmation_expands():
    """요구사항 — 3회 확인 또는 강한 붕괴 시 30~50%까지 확대."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_reversal_switch_state()
    r1 = evaluate_reversal_switch(state, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now)
    r2 = evaluate_reversal_switch(r1, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now + timedelta(seconds=25))
    r3 = evaluate_reversal_switch(r2, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now + timedelta(seconds=50), broker_confirmed_flat=True)
    assert r3["pending_action"]["action"] == REVERSAL_ACTION_EXPLORATORY_ENTRY_OPPOSITE

    # 탐색진입 실행 확정(held_direction은 아직 그대로 "UP"로 넘어오는 케이스도 있을 수
    # 있으나, 실제로는 실행 계층이 반대 포지션 진입 후 방향을 갱신한다 — 여기서는
    # 상태머신 자체의 단계 전진만 검증한다).
    executed = mark_reversal_stage_executed(r3, action=REVERSAL_ACTION_EXPLORATORY_ENTRY_OPPOSITE, executed_qty=20, now=now + timedelta(seconds=60))
    assert executed["stage"] == "EXPLORATORY_ENTERED"

    r4 = evaluate_reversal_switch(executed, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now + timedelta(seconds=90))
    assert r4["stage"] == "EXPANDED"
    assert r4["confirm_count"] == 3
    assert r4["pending_action"]["action"] == REVERSAL_ACTION_EXPAND_OPPOSITE
    assert 0.30 <= r4["pending_action"]["target_ratio"] <= 0.50


def test_evaluate_reversal_switch_hard_stop_bypasses_confirmation_count():
    """요구사항6 — PANIC/하드손절은 확인횟수와 무관하게 즉시 전량청산."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_reversal_switch_state()  # 확인 이력 전무(0회)

    result = evaluate_reversal_switch(state, held_direction="UP", snapshot=_snapshot(), now=now, hard_stop_triggered=True)

    assert result["pending_action"]["action"] == REVERSAL_ACTION_FULL_EXIT
    assert result["pending_action"]["ratio"] == 1.0


def test_evaluate_reversal_switch_strong_down_to_up_is_symmetric():
    """요구사항3 — STRONG_DOWN→UP도 완전히 대칭적으로 적용된다."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_reversal_switch_state()

    r1 = evaluate_reversal_switch(state, held_direction="DOWN", snapshot=_up_reversal_snapshot(), now=now)
    assert r1["stage"] == "REDUCED"
    assert r1["pending_action"]["action"] == REVERSAL_ACTION_REDUCE_EXISTING

    r2 = evaluate_reversal_switch(r1, held_direction="DOWN", snapshot=_up_reversal_snapshot(), now=now + timedelta(seconds=25))
    assert r2["stage"] == "FULLY_EXITED"
    assert r2["pending_action"]["action"] == REVERSAL_ACTION_FULL_EXIT

    r3 = evaluate_reversal_switch(r2, held_direction="DOWN", snapshot=_up_reversal_snapshot(), now=now + timedelta(seconds=50), broker_confirmed_flat=True)
    assert r3["pending_action"]["action"] == REVERSAL_ACTION_EXPLORATORY_ENTRY_OPPOSITE


def test_evaluate_reversal_switch_regime_downgraded_to_range_reduces_to_core():
    """요구사항4 — STRONG_TREND→RANGE는 Core 25%로 축소하고 반대 ETF 즉시진입은 금지한다."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_reversal_switch_state()

    result = evaluate_reversal_switch(
        state, held_direction="UP", snapshot=_snapshot(), now=now, regime_downgraded_to_range=True,
    )

    action = result["pending_action"]
    assert action["action"] == REVERSAL_ACTION_REDUCE_TO_CORE
    assert action["target_ratio"] == 0.25
    assert action["block_opposite_entry"] is True


def test_multiple_intraday_reversals_can_occur_in_one_day():
    """요구사항 — 하루에 여러 번 regime 전환이 가능해야 한다. 한 번의 반전 사이클이
    끝나고(EXPANDED, 이제 INVERSE를 보유 중) held_direction이 "DOWN"으로 바뀌면
    상태머신이 새로 리셋되어 다음 반전을 처음부터 다시 감시할 수 있어야 한다."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_reversal_switch_state()
    r1 = evaluate_reversal_switch(state, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now)
    r2 = evaluate_reversal_switch(r1, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now + timedelta(seconds=25))
    r3 = evaluate_reversal_switch(r2, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now + timedelta(seconds=50), broker_confirmed_flat=True)
    executed = mark_reversal_stage_executed(r3, action=REVERSAL_ACTION_EXPLORATORY_ENTRY_OPPOSITE, executed_qty=20, now=now + timedelta(seconds=60))
    assert executed["stage"] == "EXPLORATORY_ENTERED"

    # 이제 실제로 0197X0(인버스)을 보유 중이므로 held_direction="DOWN"으로 넘어온다 —
    # 오늘 두 번째 반전(다시 상승 반전)을 처음부터 감시할 수 있어야 한다. 여기서는
    # DOWN 보유와 정렬된(반전 신호 없는) 스냅샷을 써야 한다 — 기본 _snapshot()은
    # 상승 정렬이라 DOWN 보유 기준으로는 그 자체가 반전 신호이므로 부적절하다.
    later_today = now + timedelta(hours=2)
    down_aligned_calm = _down_reversal_snapshot(rel_vol=1.0)
    fresh_start = evaluate_reversal_switch(executed, held_direction="DOWN", snapshot=down_aligned_calm, now=later_today)
    assert fresh_start["direction"] == "DOWN"
    assert fresh_start["confirm_count"] == 0
    assert fresh_start["stage"] == "NONE"

    up_r1 = evaluate_reversal_switch(fresh_start, held_direction="DOWN", snapshot=_up_reversal_snapshot(), now=later_today + timedelta(seconds=25))
    assert up_r1["stage"] == "REDUCED"
    assert up_r1["pending_action"]["action"] == REVERSAL_ACTION_REDUCE_EXISTING


def test_mark_reversal_stage_executed_logs_transition():
    """요구사항8 — 모든 상태전환을 기록한다(이전/현재 regime, 전환시각, 확인횟수, 실행수량)."""
    now = datetime(2026, 7, 16, 10, 0)
    state = default_reversal_switch_state()
    r1 = evaluate_reversal_switch(state, held_direction="UP", snapshot=_down_reversal_snapshot(), now=now)

    logged = mark_reversal_stage_executed(
        r1, action=REVERSAL_ACTION_REDUCE_EXISTING, executed_qty=50, now=now,
        previous_regime="STRONG_UP", current_regime="REVERSAL_CANDIDATE_DOWN",
    )

    assert len(logged["transitions"]) == 1
    entry = logged["transitions"][0]
    assert entry["action"] == REVERSAL_ACTION_REDUCE_EXISTING
    assert entry["executed_qty"] == 50
    assert entry["previous_regime"] == "STRONG_UP"
    assert entry["current_regime"] == "REVERSAL_CANDIDATE_DOWN"
    assert entry["confirm_count"] == 1
    assert entry["at"]
