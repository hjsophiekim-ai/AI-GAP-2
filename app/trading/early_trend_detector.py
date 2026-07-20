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

# 요구사항(2026-07-20 최종) — 단계 사다리를 10~15%(즉시)→30%(15초 유지)→
# 50%(30초 유지 + ETF/기초자산 방향 일치)로 단순화한다. 처음부터 50%로
# 들어가는 것은 절대 허용하지 않는다(오늘 원장 기준 후행진입/반대매매 근본 수정).
STAGE_INITIAL = "INITIAL"
STAGE_HOLD_15S = "HOLD_15S"
STAGE_HOLD_30S_ALIGNED = "HOLD_30S_ALIGNED"
STAGE_CONFIRMED_EXPANDED = "CONFIRMED_EXPANDED"

# 하위호환 별칭(과거 3단계 이름을 참조하던 코드/테스트를 위해 유지).
STAGE_PROBE_5 = STAGE_INITIAL
STAGE_PROBE_15 = STAGE_HOLD_15S
STAGE_PROBE_25 = STAGE_HOLD_30S_ALIGNED

# 원장 signal_source에 단계별로 정확히 이 값들을 기록한다.
# hynix_switch_position_manager.run_switch_or_entry(signal_source=...)로 그대로
# 전달되며, 과거에는 이 파라미터 자체가 없어 모든 Early Detector 주문이 기본값
# ENHANCED_REGIME_SWITCH로만 기록됐다(2026-07-20 실측 버그).
_STAGE_SIGNAL_SOURCE = {
    STAGE_INITIAL: "EARLY_PROBE_INITIAL",
    STAGE_HOLD_15S: "EARLY_SCALE_1",
    STAGE_HOLD_30S_ALIGNED: "EARLY_SCALE_2",
    STAGE_CONFIRMED_EXPANDED: "EARLY_CONFIRMED_EXPAND",
}


def signal_source_for_stage(stage: Optional[str]) -> str:
    return _STAGE_SIGNAL_SOURCE.get(stage, "EARLY_TREND_DETECTOR")

# 요구사항3 — 조기진입 고정 손절(일반 장세 기본값). VOLATILE_RANGE는 이 값 대신
# VOLATILE_RANGE_SL_PCT/TP1/TP2 사다리를 쓴다(아래 should_exit_probe 참고).
FIXED_EARLY_STOP_PCT = -0.4
# 요구사항(2026-07-20 최종) — VOLATILE_RANGE 전용 TP1/TP2/SL/최대 보유시간.
VOLATILE_RANGE_TP1_PCT = 0.8
VOLATILE_RANGE_TP1_MIN_SELL_RATIO = 0.5
VOLATILE_RANGE_TP2_PCT = 1.75  # 요구사항 범위(+1.5~2.0%)의 중간값
VOLATILE_RANGE_SL_PCT = -0.5
VOLATILE_RANGE_MAX_HOLD_MINUTES = 8  # 요구사항 범위(5~8분)의 상한
# 요구사항6 — 예상 순이익(비용 차감 후)이 이 값 미만이면 진입 금지.
COST_GATE_MIN_NET_EDGE_PCT = 0.3
# 요구사항6 — 동일 방향 재진입 최소 쿨다운.
SAME_DIRECTION_COOLDOWN_SECONDS = 180
# 요구사항6 — 가짜신호 손절 2회 연속 시 중단 시간.
FAKE_SIGNAL_HALT_MINUTES = 20
FAKE_SIGNAL_HALT_THRESHOLD = 2
# 요구사항6 — 당일 조기진입 왕복거래 최대 횟수.
MAX_DAILY_ROUND_TRIPS = 5
# 요구사항(2026-07-20 최종) — 진입 후 5/15/30/60초 시점에 재평가하며, 30초
# 시점까지 재확인되지 않으면 즉시 철수한다(기존 60초는 최종 상한으로 유지).
RECONFIRMATION_CHECKPOINTS_SECONDS: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0)
HARD_RECONFIRMATION_DEADLINE_SECONDS = 30.0
NO_RECONFIRMATION_EXIT_SECONDS = 60.0
# 요구사항4 — CHASE_BLOCK 기준(장세 프로필에 자체 값이 없을 때의 고정 폴백).
# 최근 1분 고점/저점 부근(요구사항 2026-07-20 최종 — 기존 3분에서 축소).
CHASE_BLOCK_MOVE_PCT = 0.7
CHASE_BLOCK_EXTREME_MINUTES = 1
# 요구사항(2026-07-20 최종) — 반대 change-point 발생 시 기존 방향점수를 즉시
# 70% 감쇠한다(신뢰도를 없애 재진입을 어렵게 만들되 완전히 0으로 만들지는 않음).
OPPOSITE_CHANGE_POINT_DECAY_RATIO = 0.70

SIGNAL_SOURCE = "EARLY_TREND_DETECTOR"  # 하위호환 별칭(로그 등 일반 표기용) — 원장 필터링에는 ALL_SIGNAL_SOURCES를 쓴다.
ALL_SIGNAL_SOURCES = list(_STAGE_SIGNAL_SOURCE.values()) + [SIGNAL_SOURCE]

# 요구사항5 — 장세별 확정 전 탐색진입 상한. RANGE/DATA_INSUFFICIENT는 진입 자체를 막는다.
_REGIME_PROBE_CAP: dict[str, float] = {
    "RANGE": 0.0,
    "DATA_INSUFFICIENT": 0.0,
    "PANIC": 0.10,
    "VOLATILE_RANGE": 0.50,
    "STRONG_UP": 0.50,
    "STRONG_DOWN": 0.50,
    "HIGH_VOLATILITY": 0.50,
    "REVERSAL_CANDIDATE_UP": 0.50,
    "REVERSAL_CANDIDATE_DOWN": 0.50,
}

# 요구사항(2026-07-20 최종) — 경과시간(초) 기준 단계별 비중(장세 상한으로 다시
# 한 번 축소됨). 최초 확인 즉시 10~15%(중간값 12%), 15초 유지 시 30%, 30초
# 유지 + 방향 일치 시에만 50% — 처음부터 50%로 들어가지 않는다.
_STAGE_THRESHOLDS: list[tuple[float, str, float]] = [
    (30.0, STAGE_HOLD_30S_ALIGNED, 0.50),
    (15.0, STAGE_HOLD_15S, 0.30),
    (0.0, STAGE_INITIAL, 0.12),
]

# 요구사항2 — 40~50%(중간값) 확대는 STRONG_UP/STRONG_DOWN이 실제로 confirmed된 뒤에만.
_EXPANSION_TARGET_PCT = 0.50


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


def compute_composite_early_signal(
    *, fast_signal: dict, signal_symbol_agreement: Optional[bool] = None,
    live_direction: Optional[str] = None, etf_vwap_breakout: Optional[bool] = None,
    etf_structure_breakout: Optional[bool] = None, etf_volume_surge: Optional[bool] = None,
) -> dict:
    """요구사항1(2026-07-20 최종) — ETF 자체 5/10/20/30초 실시간 기울기, ETF
    자체 VWAP 이탈, 최근 1분봉 고점/저점 돌파, 거래량 급증, 기초자산-ETF 방향
    일치를 모두 입력으로 받아 조기신호를 만든다.

    1분봉 vote가 아직 방향을 확정하지 못했어도(direction=None) live_direction
    (5초 주기로 쌓은 실시간 가격 히스토리 기반)만으로 최소 신뢰도(55점)의 후보를
    만들 수 있다 — 1분봉이 새로 확정되기를 기다리다 반전 초입을 30~90초 이상
    놓치는 문제(2026-07-20 실측: 10:27 인버스 반전을 10:34에야 뒤늦게 매수)를
    막기 위함이다."""
    base = compute_early_signal(fast_signal, signal_symbol_agreement=signal_symbol_agreement)
    direction = base.get("direction")
    score = base.get("score", 0.0)
    reasons = list(base.get("reasons") or [])

    if direction in ("UP", "DOWN") and live_direction == direction:
        score = min(100.0, score + 15.0)
        reasons.append("실시간 5/10/20/30초 기울기 방향 일치 — 신뢰도 상향")
    elif direction is None and live_direction in ("UP", "DOWN"):
        direction = live_direction
        score = 55.0
        reasons = ["실시간 5/10/20/30초 기울기 단독 확정 — 1분봉 vote 미확정"]
    elif direction in ("UP", "DOWN") and live_direction and live_direction != direction:
        score *= 0.5
        reasons.append("실시간 기울기와 1분봉 vote 방향 불일치 — 신뢰도 하향")

    if direction in ("UP", "DOWN"):
        if etf_vwap_breakout:
            score = min(100.0, score + 10.0)
            reasons.append("ETF 자체 VWAP 돌파")
        if etf_structure_breakout:
            score = min(100.0, score + 10.0)
            reasons.append("최근 1분봉 고점/저점 돌파")
        if etf_volume_surge:
            score = min(100.0, score + 10.0)
            reasons.append("ETF 자체 거래량 급증")

    return {"direction": direction, "score": round(score, 2), "reasons": reasons, "vote_margin": base.get("vote_margin", 0)}


def is_opposite_change_point(previous_direction: Optional[str], current_signal: dict) -> bool:
    """요구사항2 — CUSUM/Bayesian 변화점 감지 대체: votes 우세방향이 반대로
    뒤집히면 변화점으로 본다(신규 통계 클래스를 추가하지 않는다는 사용자 확정에
    따른 heuristic 구현). live_slope_direction(5/10/20/30초 실시간 기울기, 요구
    사항1)이 주어지면 그것과의 반대 여부도 함께 본다 — 1분봉 vote가 아직
    뒤집히지 않았어도 ETF 자체의 실시간 기울기가 먼저 반전되면 그 즉시 변화점으로
    잡아 30~90초 이상 뒤늦게 반응하는 문제(2026-07-20 실측)를 줄인다."""
    current_direction = current_signal.get("direction")
    if previous_direction and current_direction and {previous_direction, current_direction} == {"UP", "DOWN"}:
        return True
    return False


def is_opposite_live_slope_reversal(previous_direction: Optional[str], live_direction: Optional[str]) -> bool:
    """요구사항1/4 — ETF 자체 5/10/20/30초 실시간 기울기가 기존 방향과 반대로
    확정되면(app.trading.early_trend_live_feed.compute_live_direction) 즉시
    반대 change-point로 취급한다."""
    if not previous_direction or not live_direction:
        return False
    return {previous_direction, live_direction} == {"UP", "DOWN"}


def apply_opposite_change_point_reaction(freq: dict, previous_direction: Optional[str], previous_score: Optional[float], now: datetime) -> dict:
    """요구사항(2026-07-20 최종) — 반대 change-point 발생 시 즉시: (1) 기존
    방향 점수를 70% 감쇠(UI/원장 표시용으로 기록), (2) 기존 방향으로의 신규
    재진입을 지금부터 다시 쿨다운 처리해 막는다(register_probe_entry와 동일한
    쿨다운 메커니즘 재사용 — 별도 자료구조를 새로 만들지 않는다). 확인
    스트릭(경과시간 타이머) 리셋은 호출부가 candidate 딕셔너리를 새로 만드는
    것으로 이미 처리된다."""
    freq = dict(freq)
    if previous_direction:
        freq = register_probe_entry(freq, previous_direction, now)
    freq["last_opposite_change_point_at"] = now.isoformat()
    freq["last_opposite_change_point_decayed_score"] = (
        round((previous_score or 0.0) * (1.0 - OPPOSITE_CHANGE_POINT_DECAY_RATIO), 4)
        if previous_score is not None else None
    )
    return freq


def stage_for_elapsed_seconds(elapsed_seconds: float, direction_aligned: bool = False) -> tuple[str, float]:
    """요구사항(2026-07-20 최종) — 10~15%(즉시, 중간값 12%) → 30%(15초 유지) →
    50%(30초 유지 + ETF/기초자산 방향 일치). 30초가 지났어도 direction_aligned가
    False면 50% 단계로 넘어가지 않고 30% 단계에 머문다 — 처음부터/조건 없이
    50%로 들어가는 것을 절대 허용하지 않는다."""
    for threshold, stage, pct in _STAGE_THRESHOLDS:
        if elapsed_seconds >= threshold:
            if stage == STAGE_HOLD_30S_ALIGNED and not direction_aligned:
                continue
            return stage, pct
    return STAGE_INITIAL, 0.12


def regime_probe_cap(confirmed_regime: Optional[str]) -> float:
    return _REGIME_PROBE_CAP.get(confirmed_regime, 0.0)


def compute_target_probe_pct(
    confirmed_regime: Optional[str], elapsed_seconds: float, direction_aligned: bool = False,
) -> tuple[str, float]:
    """요구사항2/5 — 경과시간 기준 단계에 장세별 상한을 곱해 실제 목표비중을 낸다."""
    cap = regime_probe_cap(confirmed_regime)
    stage, pct = stage_for_elapsed_seconds(elapsed_seconds, direction_aligned=direction_aligned)
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
    """요구사항4 — 신호 발생 후 실제 ETF가 이미 0.7% 이상 움직였거나 최근 1분
    극값 부근이면 CHASE_BLOCK(2026-07-20 최종 — 기존 3분에서 축소). adaptive_market_regime의 기존(死코드였던)
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
    confirmed_regime: Optional[str] = None, held_minutes: Optional[float] = None,
    tp1_taken: bool = False,
) -> dict:
    """요구사항3/(2026-07-20 최종) — 조기진입 철수/부분익절 판단(우선순위대로).
    반환: {"action": "HOLD"|"SELL_PARTIAL"|"SELL_ALL", "ratio": float, "reason": str|None}.

    VOLATILE_RANGE 확정 중에는 일반 고정 -0.4% 손절 대신 TP1(+0.8%, 50%+ 부분
    익절)/TP2(+1.5~2.0%, 전량)/SL(-0.5%)/신호약화 시 손익과 무관한 즉시
    전량청산/최대보유 5~8분 사다리를 쓴다. 재확인 시한은 30초로 통일한다(기존
    60초 — 30초 내 재확인 실패 시 즉시 철수, 요구사항 5/15/30/60초 재평가)."""
    hold = {"action": "HOLD", "ratio": 0.0, "reason": None}

    if confirmed_regime == "VOLATILE_RANGE":
        if net_return_pct is not None and net_return_pct <= VOLATILE_RANGE_SL_PCT:
            return {
                "action": "SELL_ALL", "ratio": 1.0,
                "reason": f"VOLATILE_RANGE 손절(net {net_return_pct:.2f}% <= {VOLATILE_RANGE_SL_PCT}%)",
            }
        if opposite_change_point or not signal_still_valid:
            why = "반대 변화점 발생" if opposite_change_point else "조기신호 소멸"
            return {
                "action": "SELL_ALL", "ratio": 1.0,
                "reason": f"VOLATILE_RANGE 신호약화({why}) — 손익과 무관하게 즉시 전량청산",
            }
        if net_return_pct is not None and net_return_pct >= VOLATILE_RANGE_TP2_PCT:
            return {
                "action": "SELL_ALL", "ratio": 1.0,
                "reason": f"VOLATILE_RANGE TP2(net {net_return_pct:.2f}% >= {VOLATILE_RANGE_TP2_PCT}%) — 전량익절",
            }
        if not tp1_taken and net_return_pct is not None and net_return_pct >= VOLATILE_RANGE_TP1_PCT:
            return {
                "action": "SELL_PARTIAL", "ratio": VOLATILE_RANGE_TP1_MIN_SELL_RATIO,
                "reason": (
                    f"VOLATILE_RANGE TP1(net {net_return_pct:.2f}% >= {VOLATILE_RANGE_TP1_PCT}%) — "
                    f"{VOLATILE_RANGE_TP1_MIN_SELL_RATIO * 100:.0f}%+ 부분익절"
                ),
            }
        if held_minutes is not None and held_minutes >= VOLATILE_RANGE_MAX_HOLD_MINUTES:
            return {
                "action": "SELL_ALL", "ratio": 1.0,
                "reason": f"VOLATILE_RANGE 최대보유시간({VOLATILE_RANGE_MAX_HOLD_MINUTES}분) 초과 — 전량청산",
            }
        if seconds_since_last_reconfirmation is not None and seconds_since_last_reconfirmation > HARD_RECONFIRMATION_DEADLINE_SECONDS:
            return {
                "action": "SELL_ALL", "ratio": 1.0,
                "reason": f"{HARD_RECONFIRMATION_DEADLINE_SECONDS:.0f}초 내 추가확인 실패 — 즉시 철수",
            }
        return hold

    if net_return_pct is not None and net_return_pct <= FIXED_EARLY_STOP_PCT:
        return {
            "action": "SELL_ALL", "ratio": 1.0,
            "reason": f"조기진입 고정손절(net {net_return_pct:.2f}% <= {FIXED_EARLY_STOP_PCT}%)",
        }
    if opposite_change_point:
        return {"action": "SELL_ALL", "ratio": 1.0, "reason": "반대 변화점 발생 — 즉시 철수"}
    if not signal_still_valid:
        return {"action": "SELL_ALL", "ratio": 1.0, "reason": "조기신호 소멸 — 즉시 철수"}
    if seconds_since_last_reconfirmation is not None and seconds_since_last_reconfirmation > HARD_RECONFIRMATION_DEADLINE_SECONDS:
        return {
            "action": "SELL_ALL", "ratio": 1.0,
            "reason": f"{HARD_RECONFIRMATION_DEADLINE_SECONDS:.0f}초 내 추가확인 실패 — 즉시 철수",
        }
    return hold
