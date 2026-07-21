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

import json
from datetime import datetime, timedelta
from typing import Optional

from app.utils.data_paths import LOGS_DIR

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
VOLATILE_RANGE_TP1_MIN_SELL_RATIO = 0.30
VOLATILE_RANGE_TP2_PCT = 1.35
VOLATILE_RANGE_TP2_SELL_RATIO = 0.35
VOLATILE_RANGE_SL_PCT = -0.5
VOLATILE_RANGE_MAX_HOLD_MINUTES = 8  # 요구사항 범위(5~8분)의 상한
# 요구사항6 — 예상 순이익(비용 차감 후)이 이 값 미만이면 진입 금지.
COST_GATE_MIN_NET_EDGE_PCT = 0.15
COST_GATE_MIN_GROSS_TO_COST_RATIO = 3.0
# 요구사항6 — 동일 방향 재진입 최소 쿨다운.
SAME_DIRECTION_COOLDOWN_SECONDS = 180
# 요구사항6 — 가짜신호 손절 2회 연속 시 중단 시간.
FAKE_SIGNAL_HALT_MINUTES = 20
FAKE_SIGNAL_HALT_THRESHOLD = 2
# 요구사항6 — 당일 조기진입 왕복거래 최대 횟수.
MAX_DAILY_ROUND_TRIPS = 5
SIGNAL_FRESH_SECONDS = 30.0
SIGNAL_REVALIDATE_SECONDS = 60.0
SIGNAL_EXPIRED_CODE = "SIGNAL_EXPIRED"
# 요구사항(2026-07-20 최종) — 진입 후 5/15/30/60초 시점에 재평가하며, 30초
# 시점까지 재확인되지 않으면 즉시 철수한다(기존 60초는 최종 상한으로 유지).
RECONFIRMATION_CHECKPOINTS_SECONDS: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0)
HARD_RECONFIRMATION_DEADLINE_SECONDS = 30.0
NO_RECONFIRMATION_EXIT_SECONDS = 60.0
# 요구사항4 — CHASE_BLOCK 기준(장세 프로필에 자체 값이 없을 때의 고정 폴백).
# 최근 1분 고점/저점 부근(요구사항 2026-07-20 최종 — 기존 3분에서 축소).
CHASE_BLOCK_MOVE_PCT = 0.6
CHASE_BLOCK_EXTREME_MINUTES = 1
MICRO_CHOP_LOOKBACK_MINUTES = 5
MICRO_CHOP_DIRECTION_FLIPS = 3
MICRO_CHOP_REVERSAL_EXITS = 2
MICRO_CHOP_VWAP_CROSSES = 3
MICRO_CHOP_MIN_MOVE_EFFICIENCY = 0.35
# 요구사항(2026-07-21 실측 버그 수정) — 예전에는 위 4개 기준 중 "단 하나"만
# 충족해도(OR) MICRO_CHOP이 활성화됐다. 상승/하락 신호가 이미 완전히 정렬됐는데도
# 5분 롤링창에 남아 있던 과거 횡보장 이벤트(예: vwap_crosses>=3) 하나만으로
# 하루 종일 거래가 0건이 되는 근본 원인이었다. 이제 "진짜 박스권"은 아래 4개
# 기준 중 최소 3개를 동시에 충족해야만 활성화된다(item5).
MICRO_CHOP_MIN_CRITERIA_COUNT = 3
# 요구사항(2026-07-21 재수정) — live_direction/VWAP 정렬이 이 초 이상 유지되면
# MICRO_CHOP을 즉시 해제한다(최초 20초에서 15초로 단축 — 더 빠른 해제).
MICRO_CHOP_RELEASE_SUSTAINED_SECONDS = 15.0
MICRO_CHOP_VWAP_ALIGNMENT_MIN_SECONDS = 15.0
# ETF 자체 기울기 구간(5/10/20초) 중 최소 이만큼 방향과 일치하면 해제한다.
MICRO_CHOP_RELEASE_MIN_WINDOW_AGREEMENT = 3
# 요구사항(2026-07-21 재수정) — MICRO_CHOP 상태 자체의 유효기간(TTL). 이 시간이
# 지나면 재평가 없이도 만료 처리해, 과거 판정이 무기한 남지 않게 한다.
MICRO_CHOP_STATE_TTL_SECONDS = 60.0
# 요구사항(2026-07-20 최종) — 반대 change-point 발생 시 기존 방향점수를 즉시
# 70% 감쇠한다(신뢰도를 없애 재진입을 어렵게 만들되 완전히 0으로 만들지는 않음).
OPPOSITE_CHANGE_POINT_DECAY_RATIO = 0.70
LIVE_REVERSAL_DECAY_RATIO = 0.80
REGIME_FAST_REVERSAL_RANGE = "FAST_REVERSAL_RANGE"

SIGNAL_SOURCE = "EARLY_TREND_DETECTOR"  # 하위호환 별칭(로그 등 일반 표기용) — 원장 필터링에는 ALL_SIGNAL_SOURCES를 쓴다.
ALL_SIGNAL_SOURCES = list(_STAGE_SIGNAL_SOURCE.values()) + [SIGNAL_SOURCE]

LATENCY_KEYS: tuple[str, ...] = (
    "detected_at",
    "direction_confirmed_at",
    "gates_started_at",
    "gates_completed_at",
    "account_query_started_at",
    "account_query_completed_at",
    "order_requested_at",
    "broker_accepted_at",
    "fill_confirmed_at",
    "position_synced_at",
)

# 요구사항5 — 장세별 확정 전 탐색진입 상한. RANGE/DATA_INSUFFICIENT는 진입 자체를 막는다.
_REGIME_PROBE_CAP: dict[str, float] = {
    "RANGE": 0.30,
    "DATA_INSUFFICIENT": 0.0,
    "PANIC": 0.30,
    "VOLATILE_RANGE": 0.70,
    "STRONG_UP": 0.80,
    "STRONG_DOWN": 0.80,
    "HIGH_VOLATILITY": 0.70,
    "REVERSAL_CANDIDATE_UP": 0.70,
    "REVERSAL_CANDIDATE_DOWN": 0.70,
    REGIME_FAST_REVERSAL_RANGE: 0.80,
}

# 요구사항(2026-07-20 최종) — 경과시간(초) 기준 단계별 비중(장세 상한으로 다시
# 한 번 축소됨). 최초 확인 즉시 10~15%(중간값 12%), 15초 유지 시 30%, 30초
# 유지 + 방향 일치 시에만 50% — 처음부터 50%로 들어가지 않는다.
_STAGE_THRESHOLDS: list[tuple[float, str, float]] = [
    (30.0, STAGE_HOLD_30S_ALIGNED, 0.70),
    (10.0, STAGE_HOLD_15S, 0.55),
    (0.0, STAGE_INITIAL, 0.30),
]

# 요구사항2 — 40~50%(중간값) 확대는 STRONG_UP/STRONG_DOWN이 실제로 confirmed된 뒤에만.
_EXPANSION_TARGET_PCT = 0.65


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

    return False


def make_signal_id(direction: str, detected_at: datetime) -> str:
    return f"EARLY:{direction}:{detected_at.strftime('%Y%m%d%H%M%S')}"


def make_episode_id(direction: str, detected_at: datetime) -> str:
    return f"EP:{direction}:{detected_at.strftime('%Y%m%d%H%M%S')}"


def signal_age_seconds(detected_at: Optional[str], now: datetime) -> Optional[float]:
    if not detected_at:
        return None
    try:
        detected = datetime.fromisoformat(str(detected_at))
    except Exception:
        return None
    return max(0.0, (now - detected).total_seconds())


def signal_validity(detected_at: Optional[str], now: datetime) -> str:
    age = signal_age_seconds(detected_at, now)
    if age is None:
        return "UNKNOWN"
    if age <= SIGNAL_FRESH_SECONDS:
        return "FRESH"
    if age <= SIGNAL_REVALIDATE_SECONDS:
        return "REVALIDATE"
    return "EXPIRED"


def default_latency_trace(*, signal_id: Optional[str] = None, worker_name: str = "UNKNOWN") -> dict:
    trace = {key: None for key in LATENCY_KEYS}
    trace.update({
        "signal_id": signal_id,
        "worker_name": worker_name,
        "main_cycle_waiting": False,
        "stage_latencies_seconds": {},
        "slowest_stage": None,
    })
    return trace


def mark_latency(trace: Optional[dict], key: str, at: datetime) -> dict:
    trace = dict(trace or default_latency_trace())
    if key in LATENCY_KEYS:
        trace[key] = at.isoformat()
    return compute_latency_summary(trace)


def compute_latency_summary(trace: dict) -> dict:
    trace = dict(trace or {})
    stage_pairs = {
        "detect_to_direction": ("detected_at", "direction_confirmed_at"),
        "gates": ("gates_started_at", "gates_completed_at"),
        "account_query": ("account_query_started_at", "account_query_completed_at"),
        "request_to_accept": ("order_requested_at", "broker_accepted_at"),
        "accept_to_fill": ("broker_accepted_at", "fill_confirmed_at"),
        "fill_to_sync": ("fill_confirmed_at", "position_synced_at"),
        "signal_to_order_requested": ("detected_at", "order_requested_at"),
        "signal_to_fill_confirmed": ("detected_at", "fill_confirmed_at"),
    }
    latencies = {}
    for name, (start_key, end_key) in stage_pairs.items():
        try:
            start = datetime.fromisoformat(str(trace.get(start_key)))
            end = datetime.fromisoformat(str(trace.get(end_key)))
        except Exception:
            continue
        latencies[name] = round(max(0.0, (end - start).total_seconds()), 3)
    trace["stage_latencies_seconds"] = latencies
    trace["slowest_stage"] = max(latencies, key=latencies.get) if latencies else None
    return trace


_LATENCY_LOG_DIR = LOGS_DIR / "early_trend_latency"


def log_latency_trace(trace: dict, now: datetime) -> None:
    """요구사항7(2026-07-21) — detected→order_requested 등 단계별 latency를 실운영
    기준으로 집계하려면(median/p95/60초 이상 신호 건수) 신호별 trace가 남아 있어야
    한다. order_requested_at이 실제로 찍힌(=주문을 실제로 시도한) trace만 남긴다 —
    HOLD/스킵된 틱까지 전부 남기면 "신호→주문" 지연 통계가 아니라 무의미해진다."""
    if not trace or not trace.get("order_requested_at"):
        return
    try:
        _LATENCY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _LATENCY_LOG_DIR / f"{now.strftime('%Y%m%d')}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trace, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def load_latency_traces_for_date(date_str: str) -> list[dict]:
    path = _LATENCY_LOG_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []
    traces: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    traces.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return traces


LATENCY_TARGET_MEDIAN_SECONDS = 10.0
LATENCY_TARGET_P95_SECONDS = 20.0
LATENCY_TARGET_MAX_SECONDS = 60.0


def compute_latency_stats_summary(traces: list[dict], *, stage: str = "signal_to_order_requested") -> dict:
    """요구사항7 — detected→order_requested 등 단일 stage의 실운영 latency를
    median/p95/60초 이상 신호 건수로 집계한다(목표: median<=10초, p95<=20초,
    60초 이상 신호 0건). 표본이 없으면 통계는 None/0으로 반환하고 목표달성
    여부도 None으로 둔다 — 표본부족을 "달성"으로 잘못 보고하지 않기 위함이다."""
    values: list[float] = []
    for trace in traces or []:
        stage_latencies = (trace or {}).get("stage_latencies_seconds") or {}
        value = stage_latencies.get(stage)
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    values.sort()
    n = len(values)
    if n == 0:
        return {
            "stage": stage, "sample_count": 0, "median_seconds": None, "p95_seconds": None,
            "max_seconds": None, "over_60s_count": 0, "meets_median_target": None,
            "meets_p95_target": None, "meets_zero_over_60s_target": None,
        }

    def _percentile(sorted_values: list[float], pct: float) -> float:
        if len(sorted_values) == 1:
            return sorted_values[0]
        rank = pct * (len(sorted_values) - 1)
        low, high = int(rank), min(int(rank) + 1, len(sorted_values) - 1)
        frac = rank - low
        return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * frac

    median = _percentile(values, 0.5)
    p95 = _percentile(values, 0.95)
    over_60s = sum(1 for v in values if v >= LATENCY_TARGET_MAX_SECONDS)
    return {
        "stage": stage,
        "sample_count": n,
        "median_seconds": round(median, 3),
        "p95_seconds": round(p95, 3),
        "max_seconds": round(values[-1], 3),
        "over_60s_count": over_60s,
        "meets_median_target": median <= LATENCY_TARGET_MEDIAN_SECONDS,
        "meets_p95_target": p95 <= LATENCY_TARGET_P95_SECONDS,
        "meets_zero_over_60s_target": over_60s == 0,
    }


def episode_first_entry_done(etd_state: dict, episode_id: Optional[str]) -> bool:
    if not episode_id:
        return False
    episode = ((etd_state or {}).get("episodes") or {}).get(episode_id) or {}
    return bool(episode.get("first_entry_done"))


def register_episode_first_entry(etd_state: dict, episode_id: str, signal_id: str, now: datetime) -> dict:
    etd_state = dict(etd_state or {})
    episodes = dict(etd_state.get("episodes") or {})
    episode = dict(episodes.get(episode_id) or {})
    episode.update({
        "episode_id": episode_id,
        "signal_id": signal_id,
        "first_entry_done": True,
        "entered_at": now.isoformat(),
    })
    episodes[episode_id] = episode
    etd_state["episodes"] = episodes
    return etd_state


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


def apply_live_reversal_candidate_reaction(freq: dict, previous_direction: Optional[str], previous_score: Optional[float], now: datetime) -> dict:
    """Short-reversal reaction: block the stale direction and decay its score by 80%."""
    freq = dict(freq)
    if previous_direction:
        freq = register_probe_entry(freq, previous_direction, now)
    freq["last_live_reversal_candidate_at"] = now.isoformat()
    freq["last_live_reversal_decayed_score"] = (
        round((previous_score or 0.0) * (1.0 - LIVE_REVERSAL_DECAY_RATIO), 4)
        if previous_score is not None else None
    )
    freq["confirmation_count_reset_at"] = now.isoformat()
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
    return STAGE_INITIAL, 0.30


def regime_probe_cap(confirmed_regime: Optional[str]) -> float:
    return _REGIME_PROBE_CAP.get(confirmed_regime, 0.0)


def compute_target_probe_pct(
    confirmed_regime: Optional[str], elapsed_seconds: float, direction_aligned: bool = False,
) -> tuple[str, float]:
    """요구사항2/5 — 경과시간 기준 단계에 장세별 상한을 곱해 실제 목표비중을 낸다."""
    if confirmed_regime == REGIME_FAST_REVERSAL_RANGE:
        if elapsed_seconds > SIGNAL_FRESH_SECONDS:
            return STAGE_HOLD_15S if direction_aligned else STAGE_INITIAL, 0.30
        if elapsed_seconds >= 10.0 and direction_aligned:
            return STAGE_HOLD_15S, 0.55
        return STAGE_INITIAL, 0.30
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
    gross_edge_pct = abs(expected_move_pct or 0.0)
    net_edge_pct = round(gross_edge_pct - cost_pct, 4)
    gross_to_cost_ratio = round(gross_edge_pct / cost_pct, 4) if cost_pct else float("inf")
    blocked = (
        net_edge_pct < COST_GATE_MIN_NET_EDGE_PCT
        or gross_to_cost_ratio < COST_GATE_MIN_GROSS_TO_COST_RATIO
    )
    return {
        "cost_pct": cost_pct,
        "expected_gross_edge_pct": round(gross_edge_pct, 4),
        "net_edge_pct": net_edge_pct,
        "gross_to_cost_ratio": gross_to_cost_ratio,
        "blocked": blocked,
        "min_net_edge_pct": COST_GATE_MIN_NET_EDGE_PCT,
        "min_gross_to_cost_ratio": COST_GATE_MIN_GROSS_TO_COST_RATIO,
    }


def default_micro_chop_state() -> dict:
    return {
        "active": False, "events": [], "direction_flips": 0, "reversal_exits": 0,
        "vwap_crosses": 0, "avg_move_efficiency": None, "criteria_met": {},
        "criteria_met_count": 0, "created_at": None, "activated_at": None,
        "updated_at": None, "last_evaluated_at": None, "expires_at": None,
        "released_at": None, "_state_date": None,
    }


def reset_micro_chop_state_if_stale(state: Optional[dict], now: datetime) -> dict:
    """요구사항(2026-07-21 재수정) — 프로세스 재시작 등으로 읽어들인 persistent
    MICRO_CHOP 상태가 오늘 날짜가 아니거나(어제 이전 세션의 잔여 상태) TTL이
    지났으면(expires_at 경과) 시작 시 자동으로 버린다. expires_at 필드 자체가
    없는 구버전 활성 상태도 안전하게 버린다(과거 어느 시점부터 활성화됐는지
    알 수 없으므로)."""
    state = dict(state or {})
    if not state:
        return default_micro_chop_state()
    today = now.strftime("%Y%m%d")
    if state.get("_state_date") and state.get("_state_date") != today:
        return default_micro_chop_state()
    expires_at = state.get("expires_at")
    if state.get("active"):
        if not expires_at:
            return default_micro_chop_state()
        try:
            if now >= datetime.fromisoformat(expires_at):
                return default_micro_chop_state()
        except Exception:
            return default_micro_chop_state()
    return state


def update_micro_chop_state(state: Optional[dict], *, direction: Optional[str], vwap_crossed: bool,
                            reversal_exit: bool, move_efficiency: Optional[float], now: datetime) -> dict:
    """요구사항(2026-07-21 실측 버그 수정) — 상승/하락 신호가 이미 완전히
    정렬됐는데도(structural=live=ETF 방향 UP 등) 5분 롤링창에 남아 있던 과거
    횡보장 이벤트 "단 하나"만으로 하루 종일 신규진입이 막히던 문제를 고친다.

    (a) 4개 기준(방향전환/VWAP교차/이동효율저하/swing 돌파 실패) 중 최소
        MICRO_CHOP_MIN_CRITERIA_COUNT(3)개를 동시에 충족해야만 활성화한다
        (기존에는 OR 1개만으로 활성화됐다).
    (b) 최초 활성화 시각(activated_at)/생성 시각(created_at)을 기록하고,
        TTL(MICRO_CHOP_STATE_TTL_SECONDS=60초) 기준 expires_at을 매 틱 갱신한다
        — 재평가 없이 오래 방치된 상태가 무기한 살아남지 않게 한다.
    (c) 날짜가 바뀌면(_state_date) 완전히 초기화한다.
    """
    state = reset_micro_chop_state_if_stale(state, now)
    today = now.strftime("%Y%m%d")
    events = list(state.get("events") or [])
    events.append({
        "t": now.isoformat(),
        "direction": direction,
        "vwap_crossed": bool(vwap_crossed),
        "reversal_exit": bool(reversal_exit),
        "move_efficiency": move_efficiency,
    })
    cutoff = now - timedelta(minutes=MICRO_CHOP_LOOKBACK_MINUTES)
    kept = []
    for event in events:
        try:
            if datetime.fromisoformat(event["t"]) >= cutoff:
                kept.append(event)
        except Exception:
            continue
    flips = 0
    prev_direction = None
    for event in kept:
        cur = event.get("direction")
        if cur in ("UP", "DOWN"):
            if prev_direction and cur != prev_direction:
                flips += 1
            prev_direction = cur
    reversal_exits = sum(1 for event in kept if event.get("reversal_exit"))
    vwap_crosses = sum(1 for event in kept if event.get("vwap_crossed"))
    efficiencies = [float(event["move_efficiency"]) for event in kept if event.get("move_efficiency") is not None]
    avg_efficiency = sum(efficiencies) / len(efficiencies) if efficiencies else None
    criteria = {
        "direction_flips": flips >= MICRO_CHOP_DIRECTION_FLIPS,
        "vwap_crosses": vwap_crosses >= MICRO_CHOP_VWAP_CROSSES,
        "low_move_efficiency": bool(avg_efficiency is not None and avg_efficiency < MICRO_CHOP_MIN_MOVE_EFFICIENCY and len(efficiencies) >= 3),
        # "swing 돌파 실패 2회 이상"의 근사 — reversal_exit 이벤트(조기진입 반전청산)를
        # swing 돌파 실패의 대리지표로 재사용한다(별도 swing-실패 이벤트 스트림이 없음).
        "swing_breakout_failures": reversal_exits >= MICRO_CHOP_REVERSAL_EXITS,
    }
    criteria_met_count = sum(1 for v in criteria.values() if v)
    active = criteria_met_count >= MICRO_CHOP_MIN_CRITERIA_COUNT
    was_active = bool(state.get("active"))
    created_at = state.get("created_at")
    activated_at = state.get("activated_at")
    if active and not was_active:
        activated_at = now.isoformat()
        created_at = created_at or activated_at
    elif not active:
        activated_at = None
    expires_at = (now + timedelta(seconds=MICRO_CHOP_STATE_TTL_SECONDS)).isoformat() if active else None
    state.update({
        "active": bool(active),
        "created_at": created_at,
        "activated_at": activated_at,
        "expires_at": expires_at,
        "ttl_seconds": MICRO_CHOP_STATE_TTL_SECONDS,
        "events": kept,
        "direction_flips": flips,
        "reversal_exits": reversal_exits,
        "vwap_crosses": vwap_crosses,
        "avg_move_efficiency": round(avg_efficiency, 4) if avg_efficiency is not None else None,
        "criteria_met": criteria,
        "criteria_met_count": criteria_met_count,
        "updated_at": now.isoformat(),
        "last_evaluated_at": now.isoformat(),
        "released_at": None if active else (now.isoformat() if was_active else state.get("released_at")),
        "_state_date": today,
    })
    return state


def update_vwap_alignment_tracker(tracker: Optional[dict], *, aligned: bool, now: datetime) -> dict:
    """요구사항5(2026-07-21) — VWAP "돌파 이벤트"(이번 틱에 막 교차)와 "정렬
    상태"(현재 올바른 쪽에서 유지 중인 시간)를 분리 추적한다. MICRO_CHOP 해제는
    신규 돌파 이벤트가 아니라 이 유지시간(aligned_since 기준)으로도 인정한다."""
    tracker = dict(tracker or {})
    if aligned:
        if not tracker.get("aligned_since"):
            tracker["aligned_since"] = now.isoformat()
    else:
        tracker["aligned_since"] = None
    tracker["aligned"] = bool(aligned)
    tracker["checked_at"] = now.isoformat()
    return tracker


def vwap_alignment_seconds(tracker: Optional[dict], now: datetime) -> Optional[float]:
    since = (tracker or {}).get("aligned_since")
    if not since:
        return None
    try:
        return max(0.0, (now - datetime.fromisoformat(since)).total_seconds())
    except Exception:
        return None


def evaluate_micro_chop_release(
    *,
    live_direction: Optional[str] = None,
    live_direction_held_seconds: Optional[float] = None,
    structural_direction: Optional[str] = None,
    confirm_window_directions: Optional[dict] = None,
    confirm_vwap_aligned_seconds: Optional[float] = None,
    new_swing_breakout: Optional[bool] = None,
    actionable_signal: Optional[str] = None,
    etf_mutual_confirmed: Optional[bool] = None,
    data_time_mismatch: bool = False,
) -> dict:
    """요구사항(2026-07-21 재수정) — MICRO_CHOP 즉시 해제 조건(방향과 무관하게
    완전히 대칭). 아래 중 하나라도 충족되면 해제한다(반환 {"release": bool,
    "reason": str|None}). data_time_mismatch가 True면(결측/시차초과 데이터)
    절대 해제하지 않는다 — 나쁜 데이터로 안전장치를 풀면 안 되기 때문이다."""
    if data_time_mismatch:
        return {"release": False, "reason": None}

    if (
        structural_direction in ("UP", "DOWN") and structural_direction == live_direction
        and live_direction_held_seconds is not None
        and live_direction_held_seconds >= MICRO_CHOP_RELEASE_SUSTAINED_SECONDS
    ):
        return {
            "release": True,
            "reason": f"structural/live 방향 일치({live_direction}) {live_direction_held_seconds:.0f}초 지속",
        }

    confirm = confirm_window_directions or {}
    if live_direction in ("UP", "DOWN") and all(confirm.get(w) == live_direction for w in (5, 10, 20)):
        return {"release": True, "reason": "매수ETF 5·10·20초 모두 방향과 일치"}

    if (
        confirm_vwap_aligned_seconds is not None
        and confirm_vwap_aligned_seconds >= MICRO_CHOP_VWAP_ALIGNMENT_MIN_SECONDS
    ):
        return {
            "release": True,
            "reason": f"매수ETF VWAP 정렬 {confirm_vwap_aligned_seconds:.0f}초 유지",
        }

    if new_swing_breakout:
        return {"release": True, "reason": "신규 swing 고점/저점 갱신"}

    if actionable_signal in ("HYNIX_STRONG_BUY", "INVERSE_STRONG_BUY") and etf_mutual_confirmed:
        return {"release": True, "reason": f"actionable_signal={actionable_signal} + ETF 상호확인"}

    return {"release": False, "reason": None}


def should_exit_probe(
    *, net_return_pct: Optional[float], seconds_since_last_reconfirmation: Optional[float],
    signal_still_valid: bool, opposite_change_point: bool,
    confirmed_regime: Optional[str] = None, held_minutes: Optional[float] = None,
    tp1_taken: bool = False,
    tp2_taken: bool = False,
    opposite_live_seconds: Optional[float] = None,
    strong_opposite_etf_confirmed: bool = False,
    actionable_direction: Optional[str] = None,
    position_direction: Optional[str] = None,
    held_etf_reversal_windows: Optional[dict] = None,
    opposite_etf_5s10s_confirmed: bool = False,
    structure_reversal_confirmed: bool = False,
    regime_reversal_confirmed: bool = False,
    episode_invalidated: bool = False,
    peak_net_return_pct: Optional[float] = None,
) -> dict:
    """요구사항3/(2026-07-20 최종) — 조기진입 철수/부분익절 판단(우선순위대로).
    반환: {"action": "HOLD"|"SELL_PARTIAL"|"SELL_ALL", "ratio": float, "reason": str|None}.

    VOLATILE_RANGE 확정 중에는 일반 고정 -0.4% 손절 대신 TP1(+0.8%, 50%+ 부분
    익절)/TP2(+1.5~2.0%, 전량)/SL(-0.5%)/신호약화 시 손익과 무관한 즉시
    전량청산/최대보유 5~8분 사다리를 쓴다. 재확인 시한은 30초로 통일한다(기존
    60초 — 30초 내 재확인 실패 시 즉시 철수, 요구사항 5/15/30/60초 재평가)."""
    hold = {"action": "HOLD", "ratio": 0.0, "reason": None}
    held_etf_reversal_windows = held_etf_reversal_windows or {}
    actionable_reversed = (
        actionable_direction in ("UP", "DOWN")
        and position_direction in ("UP", "DOWN")
        and actionable_direction != position_direction
    )
    held_5_10_20_reversed = all(bool(held_etf_reversal_windows.get(w)) for w in (5, 10, 20))
    profit_giveback_pct = None
    if peak_net_return_pct is not None and net_return_pct is not None:
        profit_giveback_pct = max(0.0, float(peak_net_return_pct) - float(net_return_pct))
    profit_lock_full_exit = (
        profit_giveback_pct is not None
        and float(peak_net_return_pct) >= VOLATILE_RANGE_TP1_PCT
        and profit_giveback_pct >= 0.5
    )
    confirmed_full_exit = (
        actionable_reversed
        or held_5_10_20_reversed
        or opposite_etf_5s10s_confirmed
        or structure_reversal_confirmed
        or regime_reversal_confirmed
        or episode_invalidated
        or strong_opposite_etf_confirmed
        or profit_lock_full_exit
    )

    if confirmed_regime in ("VOLATILE_RANGE", REGIME_FAST_REVERSAL_RANGE):
        regime_label = confirmed_regime
        if net_return_pct is not None and net_return_pct <= VOLATILE_RANGE_SL_PCT:
            return {
                "action": "SELL_ALL", "ratio": 1.0,
                "reason": f"{regime_label} 손절(net {net_return_pct:.2f}% <= {VOLATILE_RANGE_SL_PCT}%)",
            }
        if confirmed_full_exit:
            return {"action": "SELL_ALL", "ratio": 1.0, "reason": f"{regime_label} confirmed reversal/profit-lock full exit"}
        if not signal_still_valid:
            why = "early signal decayed"
            return {
                "action": "SELL_ALL", "ratio": 1.0,
                "reason": f"{regime_label} signal weakened ({why}) - full exit",
            }
        if opposite_change_point:
            if opposite_live_seconds is not None and opposite_live_seconds >= 10.0:
                return {"action": "SELL_ALL", "ratio": 1.0, "reason": f"{regime_label} opposite live direction confirmed"}
            if net_return_pct is not None and net_return_pct >= VOLATILE_RANGE_TP1_PCT:
                return {"action": "SELL_PARTIAL", "ratio": 0.50, "reason": f"{regime_label} weak opposite while profitable - lock 50%"}
            if opposite_live_seconds is not None and opposite_live_seconds >= 5.0:
                return {"action": "SELL_PARTIAL", "ratio": 0.40, "reason": f"{regime_label} weak opposite micro signal"}
            return {"action": "HOLD", "ratio": 0.0, "reason": "first opposite micro signal - stop scaling"}
        if net_return_pct is not None and net_return_pct >= VOLATILE_RANGE_TP2_PCT:
            return {
                "action": "HOLD" if tp2_taken else "SELL_PARTIAL",
                "ratio": 0.0 if tp2_taken else VOLATILE_RANGE_TP2_SELL_RATIO,
                "reason": f"{regime_label} TP2(net {net_return_pct:.2f}% >= {VOLATILE_RANGE_TP2_PCT}%) — 추가 부분익절 후 잔량 보유",
            }
        if not tp1_taken and net_return_pct is not None and net_return_pct >= VOLATILE_RANGE_TP1_PCT:
            return {
                "action": "SELL_PARTIAL", "ratio": VOLATILE_RANGE_TP1_MIN_SELL_RATIO,
                "reason": (
                    f"{regime_label} TP1(net {net_return_pct:.2f}% >= {VOLATILE_RANGE_TP1_PCT}%) — "
                    f"{VOLATILE_RANGE_TP1_MIN_SELL_RATIO * 100:.0f}%+ 부분익절"
                ),
            }
        if held_minutes is not None and held_minutes >= VOLATILE_RANGE_MAX_HOLD_MINUTES and (net_return_pct is None or net_return_pct <= 0):
            return {
                "action": "SELL_ALL", "ratio": 1.0,
                "reason": f"{regime_label} 최대보유시간({VOLATILE_RANGE_MAX_HOLD_MINUTES}분) 초과 — 전량청산",
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
    if confirmed_full_exit:
        return {"action": "SELL_ALL", "ratio": 1.0, "reason": "confirmed reversal/profit-lock full exit"}
    if opposite_change_point:
        if opposite_live_seconds is not None and opposite_live_seconds >= 15.0:
            return {"action": "SELL_ALL", "ratio": 1.0, "reason": "opposite live direction confirmed"}
        if net_return_pct is not None and net_return_pct >= VOLATILE_RANGE_TP1_PCT:
            return {"action": "SELL_PARTIAL", "ratio": 0.50, "reason": "weak opposite while profitable - lock 50%"}
        if opposite_live_seconds is not None and opposite_live_seconds >= 5.0:
            return {"action": "SELL_PARTIAL", "ratio": 0.40, "reason": "opposite live direction held 10s"}
        return {"action": "HOLD", "ratio": 0.0, "reason": "first opposite micro signal - stop scaling"}
    if not signal_still_valid:
        return {"action": "SELL_ALL", "ratio": 1.0, "reason": "조기신호 소멸 — 즉시 철수"}
    if seconds_since_last_reconfirmation is not None and seconds_since_last_reconfirmation > HARD_RECONFIRMATION_DEADLINE_SECONDS:
        return {
            "action": "SELL_ALL", "ratio": 1.0,
            "reason": f"{HARD_RECONFIRMATION_DEADLINE_SECONDS:.0f}초 내 추가확인 실패 — 즉시 철수",
        }
    return hold
