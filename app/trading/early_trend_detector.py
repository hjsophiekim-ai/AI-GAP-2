"""
early_trend_detector.py — Early Trend Detector: Adaptive Market Regime 하위의
제한적 탐색진입(probe entry) 엔진.

단독 전략이 아니다 — 최종 주문권한과 최대 비중은 항상 Adaptive Regime
(app.trading.adaptive_market_regime)이 결정한다. 이 모듈은 3분봉이 확정되기
전 초기 방향전환을 heuristic 점수로 감지해 "최초 탐색진입(5%)"만 허용하고,
STRONG_UP/STRONG_DOWN이 실제로 confirmed되기 전까지는 25%를 넘지 않는다.
확대(40~50%)는 confirmed_regime이 실제로 매칭될 때만 run_switch_or_entry()의
기존 target-weight 증액 경로(가중평균 entry_price 갱신 포함)로 실행된다 —
이 모듈 자체는 주문을 실행하지 않고 판단만 반환한다.

CUSUM/Bayesian 같은 별도의 통계적 change-point 클래스는 이 작업 범위에
포함하지 않는다(사용자 확정) — app.trading.hynix_fast_trend.
compute_fast_trend_signal()의 6-vote 방향판단이 뒤집히는 것을 "변화점" 근사로
재사용한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

ENTRY_TYPE_EARLY_PROBE = "EARLY_PROBE"

STAGE_PROBE_5 = "PROBE_5"
STAGE_PROBE_15 = "PROBE_15"
STAGE_PROBE_25 = "PROBE_25"
STAGE_CONFIRMED_EXPANDED = "CONFIRMED_EXPANDED"

# 요구사항3 — 조기진입 고정 손절(사용자 확정: 범위가 아니라 -0.4% 고정).
FIXED_EARLY_STOP_PCT = -0.4
# 요구사항6 — 예상 순이익(비용 차감 후)이 이 값 미만이면 진입 금지.
COST_GATE_MIN_NET_EDGE_PCT = 0.3
# 요구사항6 — 동일 방향 재진입 최소 쿨다운.
SAME_DIRECTION_COOLDOWN_SECONDS = 180
# 요구사항6 — 가짜신호 손절 2회 연속 시 중단 시간.
FAKE_SIGNAL_HALT_MINUTES = 20
FAKE_SIGNAL_HALT_THRESHOLD = 2
# 요구사항6 — 당일 조기진입 왕복거래 최대 횟수.
MAX_DAILY_ROUND_TRIPS = 5
# 요구사항3 — 60초 내 추가확인 실패 시 철수.
NO_RECONFIRMATION_EXIT_SECONDS = 60
# 요구사항4 — CHASE_BLOCK 기준(장세 프로필에 자체 값이 없을 때의 고정 폴백).
CHASE_BLOCK_MOVE_PCT = 0.7
CHASE_BLOCK_EXTREME_MINUTES = 3

SIGNAL_SOURCE = "EARLY_TREND_DETECTOR"

# 요구사항5 — 장세별 확정 전 탐색진입 상한. RANGE/DATA_INSUFFICIENT는 진입 자체를 막는다.
_REGIME_PROBE_CAP: dict[str, float] = {
    "RANGE": 0.0,
    "DATA_INSUFFICIENT": 0.0,
    "PANIC": 0.10,
    "VOLATILE_RANGE": 0.25,
    "STRONG_UP": 0.25,
    "STRONG_DOWN": 0.25,
    "HIGH_VOLATILITY": 0.25,
    "REVERSAL_CANDIDATE_UP": 0.25,
    "REVERSAL_CANDIDATE_DOWN": 0.25,
}

# 요구사항2 — 경과시간(초) 기준 단계별 비중(장세 상한으로 다시 한 번 축소됨).
_STAGE_THRESHOLDS: list[tuple[float, str, float]] = [
    (30.0, STAGE_PROBE_25, 0.25),
    (10.0, STAGE_PROBE_15, 0.15),
    (0.0, STAGE_PROBE_5, 0.05),
]

# 요구사항2 — 40~50%(중간값) 확대는 STRONG_UP/STRONG_DOWN이 실제로 confirmed된 뒤에만.
_EXPANSION_TARGET_PCT = 0.45


def default_probe_state() -> dict:
    return {
        "active": False, "direction": None, "detected_at": None,
        "signal_reference_price": None, "stage": None, "position_pct": 0.0,
        "last_reconfirmed_at": None, "expanded": False,
    }


def default_frequency_state() -> dict:
    return {
        "date": None, "round_trips_today": 0,
        "consecutive_fake_signal_losses": 0, "halted_until": None,
        "last_entry_at": None, "last_entry_direction": None,
    }


def reset_frequency_state_if_new_day(freq: Optional[dict], today: str) -> dict:
    freq = dict(freq) if freq else default_frequency_state()
    for key, value in default_frequency_state().items():
        freq.setdefault(key, value)
    if freq.get("date") != today:
        freq["date"] = today
        freq["round_trips_today"] = 0
    return freq


def is_halted(freq: dict, now: datetime) -> tuple[bool, Optional[float]]:
    """요구사항6 — 가짜신호 손절 2회 연속 시 20분 중단. (halted, remaining_seconds)."""
    halted_until = freq.get("halted_until")
    if not halted_until:
        return False, None
    try:
        until_dt = datetime.fromisoformat(halted_until)
    except Exception:
        return False, None
    remaining = (until_dt - now).total_seconds()
    if remaining <= 0:
        return False, None
    return True, remaining


def is_same_direction_cooldown_active(freq: dict, direction: str, now: datetime) -> bool:
    """요구사항6 — 동일 방향 재진입 최소 3분 쿨다운."""
    if freq.get("last_entry_direction") != direction or not freq.get("last_entry_at"):
        return False
    try:
        last_dt = datetime.fromisoformat(freq["last_entry_at"])
    except Exception:
        return False
    return (now - last_dt).total_seconds() < SAME_DIRECTION_COOLDOWN_SECONDS


def register_probe_entry(freq: dict, direction: str, now: datetime) -> dict:
    freq = dict(freq)
    freq["last_entry_at"] = now.isoformat()
    freq["last_entry_direction"] = direction
    return freq


def register_probe_round_trip_closed(freq: dict, now: datetime, was_fake_signal_loss: bool) -> dict:
    """요구사항6 — 왕복거래 카운트 + 가짜신호 연속손절 서킷브레이커."""
    freq = dict(freq)
    freq["round_trips_today"] = int(freq.get("round_trips_today", 0)) + 1
    if was_fake_signal_loss:
        freq["consecutive_fake_signal_losses"] = int(freq.get("consecutive_fake_signal_losses", 0)) + 1
        if freq["consecutive_fake_signal_losses"] >= FAKE_SIGNAL_HALT_THRESHOLD:
            freq["halted_until"] = (now + timedelta(minutes=FAKE_SIGNAL_HALT_MINUTES)).isoformat()
    else:
        freq["consecutive_fake_signal_losses"] = 0
    return freq


def daily_round_trip_cap_reached(today: str) -> bool:
    """요구사항6 — 당일 조기진입 왕복거래 최대 5회. 원장을 signal_source로 필터링해
    확인한다(별도 카운터를 새로 만들지 않고 기존 hynix_execution_ledger를 재사용)."""
    from app.services.hynix_execution_ledger import compute_strategy_real_stats

    stats = compute_strategy_real_stats([SIGNAL_SOURCE], today)
    return int(stats.get("trade_count", 0)) >= MAX_DAILY_ROUND_TRIPS


def compute_early_signal(fast_signal: dict, signal_symbol_agreement: Optional[bool] = None) -> dict:
    """app.trading.hynix_fast_trend.compute_fast_trend_signal() 결과를 바탕으로
    조기신호 점수(0~100)와 방향을 만든다. 별도 통계 change-point 클래스 없이,
    votes 격차·거래량 급증을 heuristic 점수로 합성한다(요구사항1 대체 구현)."""
    direction = fast_signal.get("direction")
    reasons = list(fast_signal.get("top_factors") or [])
    if direction not in ("UP", "DOWN"):
        return {"direction": None, "score": 0.0, "reasons": reasons, "vote_margin": 0}

    up_votes = fast_signal.get("up_votes", 0) or 0
    down_votes = fast_signal.get("down_votes", 0) or 0
    margin = abs(up_votes - down_votes)
    vol_ratio = fast_signal.get("volume_ratio")
    score = 40.0 + margin * 12.0 + max(0.0, (vol_ratio or 1.0) - 1.0) * 20.0
    if signal_symbol_agreement is False:
        score *= 0.5
        reasons = reasons + ["000660/ETF 방향 불일치 — 신뢰도 하향"]
    return {"direction": direction, "score": round(min(100.0, score), 2), "reasons": reasons, "vote_margin": margin}


def is_opposite_change_point(previous_direction: Optional[str], current_signal: dict) -> bool:
    """요구사항2 — CUSUM/Bayesian 변화점 감지 대체: votes 우세방향이 반대로
    뒤집히면 변화점으로 본다(신규 통계 클래스를 추가하지 않는다는 사용자 확정에
    따른 heuristic 구현)."""
    current_direction = current_signal.get("direction")
    if not previous_direction or not current_direction:
        return False
    return {previous_direction, current_direction} == {"UP", "DOWN"}


def stage_for_elapsed_seconds(elapsed_seconds: float) -> tuple[str, float]:
    for threshold, stage, pct in _STAGE_THRESHOLDS:
        if elapsed_seconds >= threshold:
            return stage, pct
    return STAGE_PROBE_5, 0.05


def regime_probe_cap(confirmed_regime: Optional[str]) -> float:
    return _REGIME_PROBE_CAP.get(confirmed_regime, 0.0)


def compute_target_probe_pct(confirmed_regime: Optional[str], elapsed_seconds: float) -> tuple[str, float]:
    """요구사항2/5 — 경과시간 기준 단계에 장세별 상한을 곱해 실제 목표비중을 낸다."""
    cap = regime_probe_cap(confirmed_regime)
    stage, pct = stage_for_elapsed_seconds(elapsed_seconds)
    return stage, round(min(pct, cap), 4)


def expansion_target_pct(confirmed_regime: Optional[str], probe_direction: str, holding_inverse: bool) -> Optional[float]:
    """요구사항2 — STRONG_UP/STRONG_DOWN이 실제로 confirmed되고 그 방향이 지금
    보유 중인 탐색진입과 일치할 때만 40~50%(중간값 45%)까지 확대를 허용한다."""
    matches = (
        (confirmed_regime == "STRONG_UP" and probe_direction == "UP" and not holding_inverse)
        or (confirmed_regime == "STRONG_DOWN" and probe_direction == "DOWN" and holding_inverse)
    )
    return _EXPANSION_TARGET_PCT if matches else None


def evaluate_chase_block(
    *, signal_reference_price: Optional[float], current_price: Optional[float],
    confirmed_regime: Optional[str], df_1min, direction: str,
) -> dict:
    """요구사항4 — 신호 발생 후 실제 ETF가 이미 0.7% 이상 움직였거나 최근 3분
    극값 부근이면 CHASE_BLOCK. adaptive_market_regime의 기존(死코드였던)
    is_chase_blocked()/is_entry_at_recent_extreme()를 재사용하되, 그 프로필에
    해당 값이 없는 장세(RANGE/STRONG_UP/DOWN 등 VOLATILE_RANGE 전용 필드라
    비어있는 경우)에는 이 모듈의 고정폭(CHASE_BLOCK_MOVE_PCT/_EXTREME_MINUTES)을
    폴백으로 적용한다."""
    from app.trading.adaptive_market_regime import is_chase_blocked, is_entry_at_recent_extreme, _recent_window

    move = is_chase_blocked(signal_reference_price, current_price, confirmed_regime)
    if move.get("threshold_pct") is None and signal_reference_price and current_price:
        moved_pct = round(abs(float(current_price) / float(signal_reference_price) - 1.0) * 100.0, 4)
        move = {"blocked": moved_pct >= CHASE_BLOCK_MOVE_PCT, "moved_pct": moved_pct, "threshold_pct": CHASE_BLOCK_MOVE_PCT}

    buy_or_sell = "BUY" if direction == "UP" else "SELL"
    extreme_blocked = is_entry_at_recent_extreme(current_price, df_1min, buy_or_sell, confirmed_regime)
    if not extreme_blocked and df_1min is not None and current_price is not None:
        recent = _recent_window(df_1min, CHASE_BLOCK_EXTREME_MINUTES)
        if recent is not None and not recent.empty:
            try:
                recent_high, recent_low = float(recent["high"].max()), float(recent["low"].min())
                if buy_or_sell == "BUY":
                    extreme_blocked = current_price >= recent_high * 0.999
                else:
                    extreme_blocked = current_price <= recent_low * 1.001
            except Exception:
                extreme_blocked = False

    reasons = []
    if move.get("blocked"):
        reasons.append(f"CHASE_BLOCK: 신호가 대비 {move.get('moved_pct')}% 이동(임계 {move.get('threshold_pct')}%)")
    if extreme_blocked:
        reasons.append(f"CHASE_BLOCK: 최근 {CHASE_BLOCK_EXTREME_MINUTES}분 고점/저점 부근")
    return {"blocked": bool(move.get("blocked") or extreme_blocked), "moved_pct": move.get("moved_pct"), "reasons": reasons}


def evaluate_cost_gate(symbol: str, expected_move_pct: float) -> dict:
    """요구사항6 — 수수료·세금·슬리피지 반영 예상 순이익이 0.3% 미만이면 진입 금지."""
    from app.trading.trading_cost_engine import TradeCostEngine

    cost_pct = TradeCostEngine().compute_round_trip_cost_pct(symbol)
    net_edge_pct = round(abs(expected_move_pct or 0.0) - cost_pct, 4)
    return {"cost_pct": cost_pct, "net_edge_pct": net_edge_pct, "blocked": net_edge_pct < COST_GATE_MIN_NET_EDGE_PCT}


def should_exit_probe(
    *, net_return_pct: Optional[float], seconds_since_last_reconfirmation: Optional[float],
    signal_still_valid: bool, opposite_change_point: bool,
) -> Optional[str]:
    """요구사항3 — 조기진입 철수 조건(우선순위대로). None이면 유지."""
    if net_return_pct is not None and net_return_pct <= FIXED_EARLY_STOP_PCT:
        return f"조기진입 고정손절(net {net_return_pct:.2f}% <= {FIXED_EARLY_STOP_PCT}%)"
    if opposite_change_point:
        return "반대 변화점 발생 — 즉시 철수"
    if not signal_still_valid:
        return "조기신호 소멸 — 즉시 철수"
    if seconds_since_last_reconfirmation is not None and seconds_since_last_reconfirmation > NO_RECONFIRMATION_EXIT_SECONDS:
        return f"{NO_RECONFIRMATION_EXIT_SECONDS}초 내 추가확인 실패 — 즉시 철수"
    return None
