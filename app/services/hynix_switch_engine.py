"""
hynix_switch_engine.py — 하이닉스⇄인버스 Enhanced 자동매매 오케스트레이터.

3분마다(또는 UI 자동새로고침 주기마다) 아래 순서를 반복한다:
① kospilab 갱신 ② 마이크론 실시간 갱신 ③~⑥ 점수/판단 계산 ⑦ 보유종목 확인
⑧ 강제청산/TP·SL/스위칭 실행 ⑨ 로그 기록 ⑩ 결과 반환(UI 렌더링용).

각 단계는 개별 try/except로 감싸 부분 실패해도 나머지는 계속 진행한다.
"""

from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

from app.logger import logger
from app.utils.time_utils import kst_now
from app.utils.runtime_info import read_runtime_info
from app.trading.hynix_symbols import (
    SIGNAL_SYMBOL,
    LONG_SYMBOL as HYNIX_SYMBOL,
    SHORT_SYMBOL as INVERSE_SYMBOL,
    action_for_live_direction,
    symbol_for_live_direction,
)
from app.services.hynix_switch_state import load_state, save_state_atomic, set_active_mode, reset_mock_state
from app.services.hynix_switch_logger import log_enhanced_prediction, log_trade
from app.trading.hynix_switch_risk_gate import (
    is_new_entry_allowed, describe_new_entry_window, get_liquidation_phase,
    should_force_trade, _parse_hm,
)
from app.trading.hynix_switch_position_manager import (
    run_liquidation_if_needed, run_tp_sl_if_needed, run_switch_or_entry, _current_price, _ACTION_TO_SYMBOL,
    apply_position_manager_to_state, run_reversal_switch_if_needed,
)
from app.trading.hynix_position_common import HynixPositionManager
from app.trading.hynix_pullback_entry import detect_pullback
from app.trading.hynix_trend_switch_accelerator import (
    default_confirm_state,
    default_frequency_state as default_trend_frequency_state,
    plan_entry as plan_trend_switch_entry,
    signal_direction,
    update_confirm_tracker,
)
from app.trading.hynix_fast_trend import compute_fast_trend_signal
from app.trading.hynix_primary_trend import (
    PRIMARY_TREND_RANGE, PRIMARY_TREND_UP, PRIMARY_TREND_DOWN,
    classify_short_term_move, compute_primary_trend, default_reversal_confirmation_state,
    inverse_block_vote_count, new_hynix_entry_blocked, new_inverse_entry_blocked, update_reversal_confirmation,
)
from app.trading.adaptive_market_regime import adaptive_regime_to_primary_trend_result

_PULLBACK_MORNING_WINDOW_END = "10:00"
_PULLBACK_PATIENCE_MINUTES = 2

_SIGNAL_DISPLAY_MAP = {
    "HYNIX_BUY": "BUY", "HYNIX_STRONG_BUY": "BUY",
    "INVERSE_BUY": "INVERSE", "INVERSE_STRONG_BUY": "INVERSE",
    "HOLD": "HOLD",
}

# 파이프라인 트레이스 단계 순서 — UI가 "어디서 멈췄는지"를 이 순서로 판정한다.
_PIPELINE_STAGES = [
    "prediction_signal", "entry_approved", "risk_manager", "order_sent",
    "broker_executed", "position_confirmed", "ui_synced",
]


_EARLY_ORDER_LOCK = threading.Lock()


def _map_prediction_signal(final_action: str) -> str:
    return _SIGNAL_DISPLAY_MAP.get(final_action, "HOLD")


def _blank_pipeline_trace() -> dict:
    """Signal 생성과 실제 체결을 분리해서 보여주기 위한 단계별 추적 정보의 기본값.

    각 단계는 True(성공/승인)/False(실패/차단)/None(해당 없음, 예: HOLD라 진입 자체를
    시도하지 않음) 중 하나이며, `stopped_stage`는 그 중 실제로 막힌 첫 단계 이름이다
    (UI가 여기서 빨간색으로 표시한다).
    """
    return {
        "prediction_signal": "HOLD",
        "entry_approved": None, "entry_approved_reason": "",
        "risk_manager_ok": True, "risk_manager_reason": "정상",
        "risk_approved": True,
        "order_sent": False,
        # execution_stage: run_switch_or_entry()가 실제로 보고한 단계("entry"=주문이
        # 필요 없었음/진입 자체를 시도 안 함, "state_sync"=POSITION_SYNC_PENDING으로
        # 차단, "order_sent"=주문을 실제로 시도함). order_sent=False가 "주문 전송
        # 실패"인지 "애초에 주문이 필요 없었음"인지 구분하는 데 쓴다.
        "execution_stage": None,
        # 요구사항(2026-07-16) — run_switch_or_entry()의 실제 결과는 entry_approved_reason
        # (진입 "승인" 사유)과 완전히 분리된 필드에 저장한다. blocking_reason이 "왜
        # 주문이 안 나갔는지"를 설명할 때 이 필드들만 사용하고, 승인 문구를 실패
        # 사유로 재사용하지 않는다.
        "execution_message": None, "order_failure_code": None, "broker_error": None,
        "requested_symbol": None, "requested_qty": None, "order_price": None,
        "buyable_cash": None, "sized_cash": None, "cooldown_remaining": None, "pending_order": False,
        # 과거(사이클 시작 전) 스냅샷 vs 이번 사이클에 새로 계산한 live 상태를 UI에서
        # 완전히 분리해 보여주기 위한 필드(요구사항1) — 눌림목 대기 같은 과거
        # 스냅샷이 남아 있어도 이번 사이클의 실제 진입 판단(live)과 혼동되지 않게 한다.
        "snapshot_pullback_status": None, "snapshot_confirmation_count": None,
        "live_entry_gate_status": None, "live_confirmation_count": None,
        "broker_executed": False,
        "position_confirmed": None,
        "ui_synced": None,
        "trade_counter": 0,
        "stopped_stage": None,
        "blocking_reason": None,
        "enhanced_direction_approval": None,
        "enhanced_direct_order_blocked": False,
        "early_decision": None,
        "early_order_result": None,
        "signal_summary": None,
    }


def _raw_score_leader(decision: dict) -> str:
    hynix_score = decision.get("enhanced_score")
    inverse_score = decision.get("inverse_pressure_score")
    try:
        hynix_score_f = float(hynix_score)
        inverse_score_f = float(inverse_score)
    except Exception:
        return "NEUTRAL"
    if hynix_score_f > inverse_score_f:
        return "HYNIX"
    if inverse_score_f > hynix_score_f:
        return "INVERSE"
    return "NEUTRAL"


def _live_trade_direction_label(state: dict) -> str:
    live = state.get("live_trade_direction") or {}
    direction = live.get("direction")
    if direction not in ("UP", "DOWN"):
        return "NONE"
    if live.get("status") == "REVERSAL_CANDIDATE":
        return f"REVERSAL_CANDIDATE_{direction}"
    return direction



def _adaptive_position_cap_info(state: Optional[dict]) -> dict:
    """Adaptive Regime position-cap for new entries (DATA_INSUFFICIENT → 0%).

    Existing policy: DATA_INSUFFICIENT has block_new_entries=True and
    position_pct_multiplier=0.0 — no exploratory live entry is allowed until a
    real regime confirms. Early-session safety stays fail-closed.
    """
    from app.trading.adaptive_market_regime import DATA_INSUFFICIENT, get_risk_profile

    adaptive = (state or {}).get("adaptive_regime") or {}
    regime = adaptive.get("confirmed_regime") or adaptive.get("regime") or DATA_INSUFFICIENT
    profile = get_risk_profile(str(regime))
    try:
        multiplier = float(profile.get("position_pct_multiplier") if profile.get("position_pct_multiplier") is not None else 1.0)
    except Exception:
        multiplier = 1.0
    block_new = bool(profile.get("block_new_entries")) or multiplier <= 0.0
    return {
        "regime": regime,
        "position_cap": max(0.0, multiplier),
        "block_new_entries": block_new,
        "profile": profile,
    }


def _effective_target_pct_with_adaptive_cap(target_pct: Optional[float], state: Optional[dict]) -> dict:
    """Apply Adaptive Regime cap to a Weighted Controller target ratio.

    Returns audit fields: position_cap, target_ratio, effective_target_pct, skip_reason.
    """
    info = _adaptive_position_cap_info(state)
    try:
        raw = float(target_pct) if target_pct is not None else 0.0
    except Exception:
        raw = 0.0
    if raw > 1.0:
        raw = raw / 100.0
    raw = max(0.0, min(1.0, raw))
    effective = raw * float(info["position_cap"])
    skip_reason = None
    if info["block_new_entries"] or float(info["position_cap"]) <= 0.0:
        effective = 0.0
        skip_reason = "DATA_INSUFFICIENT_POSITION_CAP_ZERO"
    elif effective <= 0.0:
        skip_reason = "TARGET_PCT_ZERO"
    return {
        "position_cap": float(info["position_cap"]),
        "target_ratio": raw,
        "effective_target_pct": effective,
        "order_skip_reason": skip_reason,
        "regime": info["regime"],
        "block_new_entries": info["block_new_entries"],
    }


def _orders_are_today(orders: Optional[list], *, today: Optional[str] = None) -> list:
    """Keep only same-calendar-day order rows for UI/state display."""
    day = today or kst_now().strftime("%Y%m%d")
    kept: list = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        ts = str(order.get("timestamp") or "")
        day_key = ts[:10].replace("-", "")
        if not ts:
            continue
        if day_key == day:
            kept.append(order)
    return kept


def _switch_order_succeeded(switch_result: Optional[dict]) -> bool:
    return bool(
        switch_result
        and switch_result.get("acted")
        and any(bool(o.get("success")) for o in (switch_result.get("orders") or []) if isinstance(o, dict))
    )


def _is_hynix_live_uptrend_block(final_action: str, reason: str, state: dict) -> bool:
    if final_action not in ("INVERSE_BUY", "INVERSE_STRONG_BUY"):
        return False
    text = str(reason or "").upper()
    live_direction = (state.get("live_trade_direction") or {}).get("direction")
    live_trend = state.get("last_live_hynix_trend") or {}
    live_returns = live_trend.get("returns") or {}
    primary = state.get("last_primary_trend") or {}
    live_short_up = (
        live_trend.get("above_vwap") is True
        and (live_returns.get("3m") or 0.0) > 0.0
        and (live_returns.get("5m") or 0.0) > 0.0
        and (live_trend.get("ema_slope_pct") or 0.0) > 0.0
    )
    short_up = (
        live_direction == "UP"
        or ("PRIMARY_TREND=UP" in text)
        or live_short_up
        or (
            primary.get("above_vwap") is True
            and primary.get("above_ema20") is True
            and str(primary.get("trend_5m") or "").upper() == "UP"
        )
    )
    return bool(short_up)


def _trace_block_reason(trace: dict, decision_action: str, entry_reason: str, state: dict) -> str:
    if _is_hynix_live_uptrend_block(decision_action, entry_reason, state):
        return "LIVE_HYNIX_UPTREND"
    early_decision = trace.get("early_decision") or {}
    early_reason_code = early_decision.get("reason_code")
    if early_reason_code and early_reason_code != "TARGET_ALREADY_FILLED":
        return str(early_reason_code)
    return str(trace.get("order_failure_code") or trace.get("stopped_stage") or "ENTRY_BLOCKED")


def _normalize_direction(value: Optional[str]) -> Optional[str]:
    text = str(value or "").upper()
    if text in ("UP", "HYNIX", "LONG", HYNIX_SYMBOL):
        return "UP"
    if text in ("DOWN", "INVERSE", "SHORT", INVERSE_SYMBOL):
        return "DOWN"
    return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _score_gap_dynamic_pct(
    *,
    score_gap: float,
    low: float,
    high: float,
    confidence: Optional[float] = None,
    stop_loss_distance_pct: Optional[float] = None,
    buyable_cash: Optional[float] = None,
    current_price: Optional[float] = None,
) -> float:
    # Base moves through the requested range as the score gap widens beyond 30.
    span_position = _clamp((float(score_gap) - 30.0) / 30.0, 0.0, 1.0)
    target = low + (high - low) * span_position

    if confidence is not None:
        conf = _clamp(float(confidence), 50.0, 95.0)
        target *= 0.85 + ((conf - 50.0) / 45.0) * 0.30
    if stop_loss_distance_pct is not None:
        stop_distance = abs(float(stop_loss_distance_pct))
        if stop_distance > 1.2:
            target *= 0.85
        elif stop_distance <= 0.6:
            target *= 1.05
    if buyable_cash is not None and current_price:
        # Do not surface an entry size that cannot buy even one ETF share.
        if float(buyable_cash) < float(current_price):
            return 0.0

    return round(_clamp(target, low, high), 4)


def evaluate_score_gap_entry_ladder(
    *,
    score_gap: float,
    desired_direction: str,
    live_direction: Optional[str],
    structural_direction: Optional[str] = None,
    etf_mid_term_aligned: bool = False,
    etf_confirmation_state: Optional[str] = None,
    confidence: Optional[float] = None,
    stop_loss_distance_pct: Optional[float] = None,
    buyable_cash: Optional[float] = None,
    current_price: Optional[float] = None,
) -> dict:
    desired = _normalize_direction(desired_direction)
    live = _normalize_direction(live_direction)
    structural = _normalize_direction(structural_direction)
    score_gap = float(score_gap or 0.0)

    if desired is None:
        return {"action": None, "target_pct": None, "reason_code": "NO_DIRECTION"}
    if live and live != desired:
        return {"action": "HOLD", "target_pct": 0.0, "reason_code": "LIVE_DIRECTION_CONFLICT"}
    if score_gap < 30.0:
        return {"action": None, "target_pct": None, "reason_code": "SMALL_SCORE_GAP"}

    confirm_state = str(etf_confirmation_state or "").upper()
    if live == desired:
        fully_reconfirmed = confirm_state in ("ETF_CONFIRM_UP", "ETF_CONFIRM_DOWN")
        low, high, code = (0.40, 0.60, "RECONFIRM_EXPAND") if score_gap >= 40.0 and fully_reconfirmed else (0.30, 0.50, "LIVE_ALIGNED")
        return {
            "action": "ENTER",
            "target_pct": _score_gap_dynamic_pct(
                score_gap=score_gap, low=low, high=high, confidence=confidence,
                stop_loss_distance_pct=stop_loss_distance_pct, buyable_cash=buyable_cash,
                current_price=current_price,
            ),
            "reason_code": code,
        }

    if score_gap >= 40.0 and confirm_state in ("ALIGNED_PULLBACK", "NONE", "") and (
        structural == desired or etf_mid_term_aligned
    ):
        return {
            "action": "ENTER",
            "target_pct": _score_gap_dynamic_pct(
                score_gap=score_gap, low=0.20, high=0.30, confidence=confidence,
                stop_loss_distance_pct=stop_loss_distance_pct, buyable_cash=buyable_cash,
                current_price=current_price,
            ),
            "reason_code": "PULLBACK_PROBE",
        }

    return {"action": None, "target_pct": None, "reason_code": "WAIT_FOR_CONFIRMATION"}


def _score_gap_from_decision(decision: dict) -> float:
    try:
        return abs(float(decision.get("enhanced_score")) - float(decision.get("inverse_pressure_score")))
    except Exception:
        return 0.0


def evaluate_trend_continuation_entry(
    *,
    decision: dict,
    live_direction: Optional[str],
    live_direction_held_seconds: Optional[float],
    desired_direction: Optional[str] = None,
    confirm_window_directions: Optional[dict] = None,
    oppose_window_directions: Optional[dict] = None,
    confirm_above_vwap: Optional[bool] = None,
    moved_pct_since_signal: Optional[float] = None,
    expected_net_edge_ok: bool = True,
    confidence: Optional[float] = None,
    stop_loss_distance_pct: Optional[float] = None,
    buyable_cash: Optional[float] = None,
    current_price: Optional[float] = None,
) -> dict:
    """Evaluate TREND_CONTINUATION_ENTRY without requiring a new reversal signal."""
    live = _normalize_direction(live_direction)
    enhanced = decision.get("enhanced_score")
    inverse = decision.get("inverse_pressure_score")
    try:
        enhanced_f = float(enhanced)
        inverse_f = float(inverse)
    except Exception:
        return {"action": "HOLD", "reason_code": "CONTINUATION_TOO_WEAK", "entry_path": "NONE", "target_pct": 0.0}

    if desired_direction is None:
        desired = "UP" if enhanced_f >= inverse_f else "DOWN"
    else:
        desired = _normalize_direction(desired_direction)
    if desired not in ("UP", "DOWN") or live not in ("UP", "DOWN"):
        return {"action": "HOLD", "reason_code": "CONTINUATION_TOO_WEAK", "entry_path": "NONE", "target_pct": 0.0}
    if live != desired:
        return {"action": "HOLD", "reason_code": "LIVE_DIRECTION_CONFLICT", "entry_path": "NONE", "target_pct": 0.0}

    leader_score = enhanced_f if desired == "UP" else inverse_f
    score_gap = abs(enhanced_f - inverse_f)
    if score_gap < 10.0 or leader_score < 60.0:
        return {"action": "HOLD", "reason_code": "CONTINUATION_TOO_WEAK", "entry_path": "NONE", "score_gap": score_gap, "target_pct": 0.0}
    if score_gap < 20.0:
        return {"action": "HOLD", "reason_code": "WAIT_FOR_CONFIRMATION", "entry_path": "NONE", "score_gap": score_gap, "target_pct": 0.0}

    if live_direction_held_seconds is None or float(live_direction_held_seconds) < 15.0:
        return {"action": "HOLD", "reason_code": "CONTINUATION_TOO_WEAK", "entry_path": "NONE", "score_gap": score_gap, "target_pct": 0.0}

    confirm = dict(confirm_window_directions or {})
    oppose = dict(oppose_window_directions or {})
    confirm_up_count = sum(1 for w in (5, 10, 20, 30) if confirm.get(w) == "UP")
    if not (confirm.get(5) == "UP" or confirm.get(10) == "UP") or confirm_up_count < 3:
        return {"action": "HOLD", "reason_code": "CONTINUATION_TOO_WEAK", "entry_path": "NONE", "score_gap": score_gap, "target_pct": 0.0}
    if confirm_above_vwap is not True:
        return {"action": "HOLD", "reason_code": "CONTINUATION_TOO_WEAK", "entry_path": "NONE", "score_gap": score_gap, "target_pct": 0.0}
    if oppose.get(5) == "UP" and oppose.get(10) == "UP":
        return {"action": "HOLD", "reason_code": "LIVE_DIRECTION_CONFLICT", "entry_path": "NONE", "score_gap": score_gap, "target_pct": 0.0}
    if moved_pct_since_signal is not None and float(moved_pct_since_signal) >= 0.6:
        return {"action": "HOLD", "reason_code": "CHASE_BLOCK", "entry_path": "NONE", "score_gap": score_gap, "target_pct": 0.0}
    if not expected_net_edge_ok:
        return {"action": "HOLD", "reason_code": "CHASE_BLOCK", "entry_path": "NONE", "score_gap": score_gap, "target_pct": 0.0}

    if score_gap >= 40.0:
        low, high = 0.40, 0.60
    elif score_gap >= 30.0:
        low, high = 0.30, 0.50
    else:
        low, high = 0.20, 0.30
    return {
        "action": "ENTER",
        "reason_code": "CONTINUATION_ENTRY_APPROVED",
        "entry_path": "CONTINUATION",
        "score_gap": score_gap,
        "target_pct": _score_gap_dynamic_pct(
            score_gap=score_gap, low=low, high=high, confidence=confidence,
            stop_loss_distance_pct=stop_loss_distance_pct, buyable_cash=buyable_cash,
            current_price=current_price,
        ),
    }


def evaluate_range_weighted_entry(
    *,
    decision: dict,
    direction: str,
    live_direction: Optional[str],
    signal_window_directions: Optional[dict],
    confirm_window_directions: Optional[dict],
    oppose_window_directions: Optional[dict],
    confirm_above_vwap: Optional[bool],
    live_direction_held_seconds: Optional[float] = None,
    data_age_seconds: Optional[float] = None,
    moved_pct_since_signal: Optional[float] = None,
    expected_move_pct: Optional[float] = None,
    cost_pct: Optional[float] = None,
    expected_mfe_pct: Optional[float] = None,
    expected_mae_pct: Optional[float] = None,
    ema_slope_aligned: Optional[bool] = None,
    micro_chop_active: bool = False,
    confidence: Optional[float] = None,
    stop_loss_distance_pct: Optional[float] = None,
    buyable_cash: Optional[float] = None,
    current_price: Optional[float] = None,
    entry_path_hint: Optional[str] = None,
    soft_reason_codes: Optional[list[str]] = None,
    structure_confirmed: Optional[bool] = None,
    structural_direction: Optional[str] = None,
    day_regime: Optional[str] = None,
    range_config: Optional["RangeWeightedConfig"] = None,
) -> dict:
    """Weighted RANGE entry evidence with profitability gate.

    Direction is the intended trade direction in underlying terms: UP buys the
    leverage ETF, DOWN buys the inverse ETF. The traded ETF should move UP for
    both directions, while the opposite ETF should not show strong UP pressure.
    """
    from app.trading.range_weighted_optimize import (
        RangeWeightedConfig,
        evidence_thresholds_for_regime,
        get_range_weighted_config,
        min_net_edge_for_regime,
    )

    cfg = range_config or get_range_weighted_config()
    regime = str(day_regime or "NORMAL").upper()
    thresholds = evidence_thresholds_for_regime(regime, cfg)
    min_net_edge_required = min_net_edge_for_regime(regime, cfg)
    desired = _normalize_direction(direction)
    live = _normalize_direction(live_direction)
    signal_dirs = dict(signal_window_directions or {})
    confirm_dirs = dict(confirm_window_directions or {})
    oppose_dirs = dict(oppose_window_directions or {})
    enhanced = float(decision.get("enhanced_score") or 50.0)
    inverse = float(decision.get("inverse_pressure_score") or 50.0)
    score_gap = abs(enhanced - inverse)
    raw_leader_direction = "UP" if enhanced >= inverse else "DOWN"
    contributions: dict[str, float] = {}
    hard_blocks: list[str] = []
    soft_adjustments: list[str] = []

    if desired not in ("UP", "DOWN"):
        hard_blocks.append("DATA_INSUFFICIENT")
    if data_age_seconds is not None and float(data_age_seconds) > 5.0:
        hard_blocks.append("DATA_TIME_MISMATCH")
    if live in ("UP", "DOWN") and live != desired:
        hard_blocks.append("LIVE_DIRECTION_CONFLICT")
    if confirm_dirs.get(5) == "DOWN" and confirm_dirs.get(10) == "DOWN":
        hard_blocks.append("ETF_5S_10S_BOTH_OPPOSITE")
    if moved_pct_since_signal is not None and float(moved_pct_since_signal) >= 0.6:
        soft_adjustments.append("CHASE_RISK_SIZE_REDUCED")

    missing_edge_input = expected_move_pct is None
    if missing_edge_input:
        hard_blocks.append("DATA_INSUFFICIENT")
    expected_move = float(expected_move_pct or 0.0)
    cost = float(cost_pct if cost_pct is not None else 0.0)
    safety_buffer = cfg.safety_buffer
    net_edge = expected_move - cost - safety_buffer
    gross_cost_ratio = (expected_move / cost) if cost > 0 else float("inf")
    mfe = float(expected_mfe_pct if expected_mfe_pct is not None else expected_move)
    mae = float(expected_mae_pct if expected_mae_pct is not None else (stop_loss_distance_pct or 0.4))
    reward_risk = (mfe / mae) if mae > 0 else 0.0
    if not missing_edge_input and net_edge < min_net_edge_required:
        hard_blocks.append("LOW_NET_EDGE")
    if not missing_edge_input and reward_risk < cfg.min_reward_risk:
        hard_blocks.append("POOR_REWARD_RISK")
    if regime == "AMBIGUOUS" and score_gap < cfg.min_score_gap_ambiguous:
        hard_blocks.append("AMBIGUOUS_LOW_SCORE_GAP")

    contributions["live_direction"] = 18.0 if live == desired else 0.0
    required_signal = "UP" if desired == "UP" else "DOWN"
    signal_matches = sum(1 for w in (5, 10, 20, 30) if signal_dirs.get(w) == required_signal)
    confirm_matches = sum(1 for w in (5, 10, 20, 30) if confirm_dirs.get(w) == "UP")
    oppose_weak = sum(1 for w in (5, 10, 20, 30) if oppose_dirs.get(w) == "DOWN")
    structure_ok = bool(structure_confirmed)
    contributions["signal_slopes"] = min(16.0, signal_matches * 4.0)
    contributions["entry_etf_slopes"] = min(24.0, confirm_matches * 6.0)
    contributions["opposite_etf_weak"] = min(14.0, oppose_weak * 3.5)
    contributions["vwap"] = 12.0 if confirm_above_vwap is True else 0.0
    contributions["score_gap"] = min(16.0, max(0.0, score_gap - 10.0) / 30.0 * 16.0)
    if raw_leader_direction != desired:
        contributions["score_gap"] = 0.0
    evidence_score = round(sum(contributions.values()), 2)

    if micro_chop_active:
        soft_adjustments.append("MICRO_CHOP_SIZE_REDUCED")
    for code in soft_reason_codes or []:
        if code and code not in hard_blocks and code not in soft_adjustments:
            soft_adjustments.append(str(code))

    normalized_hint = str(entry_path_hint or "").upper()
    if (
        regime == "AMBIGUOUS"
        and cfg.ambiguous_block_reversal
        and normalized_hint == "REVERSAL"
        and not structure_ok
    ):
        hard_blocks.append("AMBIGUOUS_REVERSAL_BLOCKED")
    if hard_blocks:
        reason = hard_blocks[0]
        return {
            "action": "HOLD", "entry_path": "NONE", "reason_code": reason,
            "evidence_score": evidence_score, "contributions": contributions,
            "soft_adjustments": soft_adjustments, "target_pct": 0.0,
            "score_gap": score_gap, "expected_move_pct": expected_move, "expected_gross_edge_pct": expected_move, "cost_pct": cost,
            "expected_net_edge_pct": round(net_edge, 4), "gross_cost_ratio": round(gross_cost_ratio, 4) if gross_cost_ratio != float("inf") else gross_cost_ratio,
            "reward_risk": round(reward_risk, 4), "hard_blocks": hard_blocks,
            "structure_confirmed": bool(structure_confirmed),
            "structural_direction": _normalize_direction(structural_direction),
            "strong_structure_confirmed": False,
            "structural_signal_label": "HOLD",
            "day_regime": regime,
        }
    original_action_label = str(decision.get("final_action") or "").upper()
    strong_requested = original_action_label.endswith("_STRONG_BUY")
    pullback_shape = (
        confirm_above_vwap is True
        and confirm_dirs.get(20) == "UP"
        and confirm_dirs.get(30) == "UP"
        and (confirm_dirs.get(5) == "DOWN" or confirm_dirs.get(10) == "DOWN")
        and not (confirm_dirs.get(5) == "DOWN" and confirm_dirs.get(10) == "DOWN")
    )
    if evidence_score < thresholds["weak"]:
        reason = "CONTINUATION_TOO_WEAK"
        action = "HOLD"
        low = high = 0.0
    elif evidence_score < thresholds["neutral"]:
        reason = "RANGE_EVIDENCE_NEUTRAL"
        action = "HOLD"
        low = high = 0.0
    elif evidence_score < thresholds["mid"]:
        reason = "REVERSAL_ENTRY" if normalized_hint == "REVERSAL" else ("PULLBACK_ENTRY" if confirm_matches >= 2 else "CONTINUATION_ENTRY_APPROVED")
        action = "ENTER"
        low, high = 0.20, 0.30
    elif evidence_score < thresholds["strong"]:
        reason = "REVERSAL_ENTRY" if normalized_hint == "REVERSAL" else ("PULLBACK_ENTRY" if pullback_shape else "CONTINUATION_ENTRY_APPROVED")
        action = "ENTER"
        low, high = 0.30, 0.50
    else:
        reason = "REVERSAL_ENTRY" if normalized_hint == "REVERSAL" else ("PULLBACK_ENTRY" if pullback_shape else "CONTINUATION_ENTRY_APPROVED")
        action = "ENTER"
        low, high = (0.50, 0.70) if net_edge >= 0.30 else (0.30, 0.50)
    if micro_chop_active and action == "ENTER":
        low, high = min(low, 0.20), min(high, 0.30)
    structural_dir = _normalize_direction(structural_direction)
    bounce_against_structure = action == "ENTER" and structural_dir in ("UP", "DOWN") and structural_dir != desired and not structure_ok
    if bounce_against_structure:
        action = "HOLD"
        reason = "BOUNCE_UP" if desired == "UP" else "BOUNCE_DOWN"
        low = high = 0.0
    strong_structure_confirmed = bool(
        evidence_score >= 65.0
        and confirm_matches >= 3
        and confirm_above_vwap is True
        and ema_slope_aligned is True
        and structure_ok
        and reward_risk >= 1.3
    )
    if action == "HOLD":
        structural_signal_label = reason if reason in ("BOUNCE_UP", "BOUNCE_DOWN") else "HOLD"
    elif reason == "PULLBACK_ENTRY":
        structural_signal_label = "PULLBACK"
    elif strong_requested and strong_structure_confirmed:
        structural_signal_label = original_action_label
    elif bounce_against_structure:
        structural_signal_label = "BOUNCE_UP" if desired == "UP" else "BOUNCE_DOWN"
    else:
        structural_signal_label = "BUY" if desired == "UP" else "INVERSE_BUY"
    target_pct = 0.0 if action == "HOLD" else _score_gap_dynamic_pct(
        score_gap=score_gap, low=low, high=high, confidence=confidence,
        stop_loss_distance_pct=stop_loss_distance_pct, buyable_cash=buyable_cash,
        current_price=current_price,
    )
    if regime == "STRONG_TREND" and action == "ENTER":
        target_pct = min(0.70, float(target_pct) + cfg.trend_day_size_boost)
    return {
        "action": action, "entry_path": "REVERSAL" if reason == "REVERSAL_ENTRY" else ("PULLBACK" if reason == "PULLBACK_ENTRY" else "CONTINUATION"),
        "reason_code": reason, "evidence_score": evidence_score, "contributions": contributions,
        "soft_adjustments": soft_adjustments, "target_pct": target_pct,
        "score_gap": score_gap, "expected_move_pct": expected_move, "expected_gross_edge_pct": expected_move, "cost_pct": cost,
        "expected_net_edge_pct": round(net_edge, 4), "gross_cost_ratio": round(gross_cost_ratio, 4) if gross_cost_ratio != float("inf") else gross_cost_ratio,
        "reward_risk": round(reward_risk, 4), "hard_blocks": [],
        "structure_confirmed": structure_ok,
        "structural_direction": structural_dir,
        "strong_structure_confirmed": strong_structure_confirmed,
        "structural_signal_label": structural_signal_label,
        "day_regime": regime,
    }


def _etf_mid_term_aligned_for_ladder(state: dict, desired_symbol: str, desired_direction: str) -> bool:
    desired = _normalize_direction(desired_direction)
    primary = state.get("last_primary_trend") or {}
    if _normalize_direction(primary.get("primary_trend")) == desired:
        return True

    confirmation = (state.get("early_trend_detector") or {}).get("etf_confirmation") or {}
    evidence = confirmation.get("evidence") or {}
    confirm_dirs = evidence.get("confirm_window_directions") or {}
    if desired == "UP":
        return confirm_dirs.get(20) == "UP" and confirm_dirs.get(30) == "UP"
    if desired == "DOWN":
        return confirm_dirs.get(20) == "UP" and confirm_dirs.get(30) == "UP" and desired_symbol == INVERSE_SYMBOL
    return False


def _build_signal_summary(
    *, decision: dict, trace: dict, state: dict, now: datetime, new_entry_allowed_now: bool,
    new_entry_window: Optional[dict] = None,
) -> dict:
    raw_leader = _raw_score_leader(decision)
    prediction_signal = trace.get("prediction_signal") or _map_prediction_signal(decision.get("final_action", "HOLD"))
    decision_action = decision.get("final_action", "HOLD")
    entry_reason = trace.get("entry_approved_reason") or ""
    block_reason = None
    if not new_entry_allowed_now:
        block_reason = "NEW_ENTRY_TIME_CLOSED"
    elif trace.get("entry_approved") is False:
        block_reason = _trace_block_reason(trace, decision_action, entry_reason, state)
    actionable_signal = prediction_signal
    if decision_action == "HOLD" or block_reason or trace.get("entry_approved") is False:
        actionable_signal = "HOLD"
    if trace.get("order_sent"):
        final_action = "BUY" if prediction_signal in ("BUY", "INVERSE") else prediction_signal
    else:
        final_action = "HOLD"
    if block_reason == "LIVE_HYNIX_UPTREND":
        live_label = _live_trade_direction_label(state)
        if live_label == "NONE":
            live_label = "UP"
    else:
        live_label = _live_trade_direction_label(state)

    if not new_entry_allowed_now:
        conclusion = "14:50 이후 신호와 무관하게 신규진입 금지 → HOLD"
    elif block_reason == "LIVE_HYNIX_UPTREND" and raw_leader == "INVERSE":
        conclusion = "원점수는 INVERSE 우세이나 하이닉스 단기상승 확인으로 인버스 진입 차단 → HOLD"
    elif actionable_signal == "HOLD":
        conclusion = f"원점수는 {raw_leader} 우세이나 실행 가능한 신규진입 신호 없음 → HOLD"
    else:
        conclusion = f"원점수 {raw_leader}, 실행신호 {actionable_signal} → {final_action}"

    return {
        "computed_at": now.isoformat(),
        "calculated_at": now.isoformat(),
        "raw_score_leader": raw_leader,
        "hynix_score": decision.get("enhanced_score"),
        "inverse_score": decision.get("inverse_pressure_score"),
        "live_trade_direction": live_label,
        "actionable_signal": actionable_signal,
        "final_action": final_action,
        "decision_final_action": decision_action,
        "prediction_signal": prediction_signal,
        "block_reason": block_reason,
        "entry_approved_reason": entry_reason,
        "new_entry_allowed": bool(new_entry_allowed_now),
        "new_entry_rule": (new_entry_window or {}).get("rule"),
        "conclusion": conclusion,
    }


def _snapshot_field(value, snapshot_id: str, calculated_at: str, **extra) -> dict:
    field = {"value": value, "snapshot_id": snapshot_id, "calculated_at": calculated_at}
    field.update(extra)
    return field


def _decision_snapshot_id(now: datetime) -> str:
    return f"hynix-decision-{now.strftime('%Y%m%dT%H%M%S')}"


def _weighted_entry_fusion_metadata(state: dict, continuation_eval: dict) -> dict:
    """원장·OrderCoordinator에 넣을 WEIGHTED 신규 BUY 감사 메타데이터."""
    import json

    cont = state.get("trend_continuation_entry") or {}
    snap = state.get("last_completed_decision_snapshot") or {}
    evidence = continuation_eval.get("contributions") or continuation_eval.get("weighted_evidence") or {}
    if isinstance(evidence, dict):
        try:
            evidence_str = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        except Exception:
            evidence_str = str(evidence)
    else:
        evidence_str = str(evidence or "")
    episode_id = cont.get("direction_episode_id") or cont.get("last_entry_episode_id") or ""
    runtime = read_runtime_info() or {}
    return {
        "actual_entry_engine": "WEIGHTED_ORDER_CONTROLLER_LIVE",
        "entry_path": continuation_eval.get("entry_path") or "",
        "weighted_evidence": evidence_str,
        "expected_net_edge": continuation_eval.get("expected_net_edge_pct"),
        "reward_risk": continuation_eval.get("reward_risk"),
        "direction_episode_id": episode_id,
        "decision_snapshot_id": snap.get("snapshot_id") or "",
        "deployed_git_sha": runtime.get("git_sha") or "",
        "episode_id": episode_id,
        "target_position_pct": continuation_eval.get("target_pct"),
    }


def _recent_1m_momentum_declining(enhanced_result: dict) -> bool:
    detail = enhanced_result.get("momentum_detail") or {}
    candidates = (
        detail.get("recent_1m_momentum"),
        detail.get("momentum_1m"),
        detail.get("return_1m"),
        detail.get("price_change_1m_pct"),
        enhanced_result.get("recent_1m_momentum"),
    )
    for value in candidates:
        try:
            return float(value) < 0.0
        except Exception:
            continue
    try:
        return float(enhanced_result.get("intraday_momentum_score")) < 50.0
    except Exception:
        return False


def _build_completed_decision_snapshot(
    *,
    enhanced_result: dict,
    decision: dict,
    trace: dict,
    state: dict,
    now: datetime,
    orders_this_cycle: list,
    new_entry_allowed_now: bool,
) -> dict:
    calculated_at = now.isoformat()
    snapshot_id = _decision_snapshot_id(now)
    signal_summary = trace.get("signal_summary") or {}
    raw_leader = signal_summary.get("raw_score_leader") or _raw_score_leader(decision)
    live_direction = signal_summary.get("live_trade_direction") or _live_trade_direction_label(state)
    actionable_signal = signal_summary.get("actionable_signal") or "HOLD"
    final_action = signal_summary.get("final_action") or "HOLD"
    block_reason = signal_summary.get("block_reason")
    early_decision = trace.get("early_decision") or {}
    early_reason = early_decision.get("reason") or early_decision.get("reason_code")
    continuation_result = (state.get("trend_continuation_entry") or {}).get("last_result", {})
    continuation_state = state.get("trend_continuation_entry") or {}
    primary_block_reason = block_reason or continuation_result.get("reason_code") or early_reason
    secondary_reasons = [
        reason for reason in (
            early_reason,
            continuation_result.get("reason_code"),
            signal_summary.get("entry_approved_reason"),
        )
        if reason and reason != primary_block_reason
    ]
    momentum_score = enhanced_result.get("intraday_momentum_score")
    explanation = signal_summary.get("conclusion")
    try:
        final_score = float(decision.get("enhanced_score"))
    except Exception:
        final_score = None
    if (
        final_action == "HOLD"
        and live_direction == "NONE"
        and final_score is not None
        and _recent_1m_momentum_declining(enhanced_result)
    ):
        explanation = f"최신 최종점수 {final_score:.1f}, 단기 방향 미확정 및 최근 1분 모멘텀 하락 → HOLD"

    snapshot = {
        "snapshot_id": snapshot_id,
        "calculated_at": calculated_at,
        "cycle_status": "COMPLETED",
        "raw_score_leader": _snapshot_field(
            raw_leader, snapshot_id, calculated_at,
            hynix_score=decision.get("enhanced_score"),
            inverse_score=decision.get("inverse_pressure_score"),
            label="raw component score",
        ),
        "enhanced_score": _snapshot_field(
            decision.get("enhanced_score"), snapshot_id, calculated_at,
            inverse_score=decision.get("inverse_pressure_score"),
            label="final enhanced decision score",
        ),
        "live_trade_direction": _snapshot_field(live_direction, snapshot_id, calculated_at),
        "actionable_signal": _snapshot_field(actionable_signal, snapshot_id, calculated_at),
        "final_action": _snapshot_field(final_action, snapshot_id, calculated_at),
        "block_reason": _snapshot_field(block_reason, snapshot_id, calculated_at),
        "momentum_score": _snapshot_field(momentum_score, snapshot_id, calculated_at),
        "early_reason": _snapshot_field(early_reason, snapshot_id, calculated_at),
        "primary_block_reason": _snapshot_field(primary_block_reason, snapshot_id, calculated_at),
        "secondary_reasons": _snapshot_field(secondary_reasons, snapshot_id, calculated_at),
        "entry_path": _snapshot_field(continuation_result.get("entry_path") or "NONE", snapshot_id, calculated_at),
        "continuation_reason": _snapshot_field((state.get("trend_continuation_entry") or {}).get("last_reason_code"), snapshot_id, calculated_at),
        "score_gap": _snapshot_field(_score_gap_from_decision(decision), snapshot_id, calculated_at),
        "range_evidence_score": _snapshot_field(continuation_result.get("evidence_score"), snapshot_id, calculated_at),
        "live_direction_held_seconds": _snapshot_field(continuation_state.get("live_direction_held_seconds"), snapshot_id, calculated_at),
        "etf_window_directions": _snapshot_field(continuation_state.get("confirm_window_directions"), snapshot_id, calculated_at),
        "expected_move_pct": _snapshot_field(continuation_result.get("expected_move_pct"), snapshot_id, calculated_at),
        "expected_gross_edge_pct": _snapshot_field(continuation_result.get("expected_gross_edge_pct"), snapshot_id, calculated_at),
        "cost_pct": _snapshot_field(continuation_result.get("cost_pct"), snapshot_id, calculated_at),
        "expected_net_edge_pct": _snapshot_field(continuation_result.get("expected_net_edge_pct"), snapshot_id, calculated_at),
        "gross_cost_ratio": _snapshot_field(continuation_result.get("gross_cost_ratio"), snapshot_id, calculated_at),
        "reward_risk": _snapshot_field(continuation_result.get("reward_risk"), snapshot_id, calculated_at),
        "explanation": explanation,
        "enhanced_result": {**(enhanced_result or {})},
        "decision": {**(decision or {})},
        "signal_summary": {**signal_summary, "snapshot_id": snapshot_id, "calculated_at": calculated_at},
        "pipeline_trace": {**(trace or {}), "snapshot_id": snapshot_id, "calculated_at": calculated_at},
        "orders_this_cycle": _orders_are_today(list(orders_this_cycle or []), today=now.strftime("%Y%m%d")),
        "new_entry_allowed": bool(new_entry_allowed_now),
    }
    return snapshot


def _update_fast_worker_decision_snapshot(state: dict, *, now: datetime, continuation_state: dict, early_result: Optional[dict] = None) -> None:
    decision = state.get("last_decision") or {}
    calculated_at = now.isoformat()
    snapshot_id = _decision_snapshot_id(now)
    continuation_result = continuation_state.get("last_result") or {}
    live = state.get("live_trade_direction") or {}
    early_reason = (early_result or {}).get("reason_code") or (early_result or {}).get("reason")
    last_block = continuation_state.get("last_block_reason")
    last_switch = continuation_state.get("last_switch") or (early_result or {}).get("switch") or {}
    sizing_audit = continuation_state.get("order_sizing_audit") or {}
    order_skip = (
        sizing_audit.get("order_skip_reason")
        or last_switch.get("failure_code")
        or last_switch.get("order_skip_reason")
        or last_block
    )
    controller_enter = continuation_result.get("action") == "ENTER"
    order_ok = _switch_order_succeeded(last_switch) or _switch_order_succeeded((early_result or {}).get("switch"))
    # Never display BUY + empty block when Adaptive cap / sizing / Fast Worker
    # gates prevented an actual order. Cap=0 → explicit HOLD.
    if order_ok:
        action = "BUY"
        block_reason = None
        primary_block_reason = None
    elif order_skip == "DATA_INSUFFICIENT_POSITION_CAP_ZERO" or (
        controller_enter and float(sizing_audit.get("position_cap") or 1.0) <= 0.0
    ):
        action = "HOLD"
        block_reason = "DATA_INSUFFICIENT_POSITION_CAP_ZERO"
        primary_block_reason = "DATA_INSUFFICIENT_POSITION_CAP_ZERO"
    elif controller_enter and order_skip:
        action = "HOLD"
        block_reason = str(order_skip)
        primary_block_reason = str(order_skip)
    elif controller_enter and not order_ok:
        # ENTER approved but Fast Worker did not complete the broker path — no silent BUY.
        action = "HOLD"
        block_reason = str(last_block or early_reason or "FAST_WORKER_ENTRY_NOT_EXECUTED")
        primary_block_reason = block_reason
    else:
        action = "HOLD"
        block_reason = continuation_result.get("reason_code")
        primary_block_reason = block_reason or early_reason
    secondary_reasons = [
        reason for reason in (early_reason, continuation_result.get("reason_code"), last_block, sizing_audit.get("order_skip_reason"))
        if reason and reason != primary_block_reason
    ]
    today_orders = _orders_are_today(
        ((last_switch or {}).get("orders") or [])
        or (((early_result or {}).get("switch") or {}).get("orders") or [])
    )
    snapshot = {
        "snapshot_id": snapshot_id,
        "calculated_at": calculated_at,
        "cycle_status": "COMPLETED",
        "raw_score_leader": _snapshot_field(_raw_score_leader(decision), snapshot_id, calculated_at, hynix_score=decision.get("enhanced_score"), inverse_score=decision.get("inverse_pressure_score"), label="raw component score"),
        "enhanced_score": _snapshot_field(decision.get("enhanced_score"), snapshot_id, calculated_at, inverse_score=decision.get("inverse_pressure_score"), label="final enhanced decision score"),
        "live_trade_direction": _snapshot_field(live.get("direction") or "NONE", snapshot_id, calculated_at),
        "actionable_signal": _snapshot_field(action, snapshot_id, calculated_at),
        "final_action": _snapshot_field(action, snapshot_id, calculated_at),
        "block_reason": _snapshot_field(block_reason, snapshot_id, calculated_at),
        "momentum_score": _snapshot_field((state.get("last_enhanced_result") or {}).get("intraday_momentum_score"), snapshot_id, calculated_at),
        "early_reason": _snapshot_field(early_reason, snapshot_id, calculated_at),
        "primary_block_reason": _snapshot_field(primary_block_reason, snapshot_id, calculated_at),
        "secondary_reasons": _snapshot_field(secondary_reasons, snapshot_id, calculated_at),
        "entry_path": _snapshot_field(continuation_result.get("entry_path") or "NONE", snapshot_id, calculated_at),
        "structural_signal_label": _snapshot_field(continuation_result.get("structural_signal_label") or action, snapshot_id, calculated_at),
        "structure_confirmed": _snapshot_field(continuation_result.get("structure_confirmed"), snapshot_id, calculated_at),
        "strong_structure_confirmed": _snapshot_field(continuation_result.get("strong_structure_confirmed"), snapshot_id, calculated_at),
        "continuation_reason": _snapshot_field(continuation_result.get("reason_code"), snapshot_id, calculated_at),
        "score_gap": _snapshot_field(continuation_result.get("score_gap") or _score_gap_from_decision(decision), snapshot_id, calculated_at),
        "range_evidence_score": _snapshot_field(continuation_result.get("evidence_score"), snapshot_id, calculated_at),
        "live_direction_held_seconds": _snapshot_field(continuation_state.get("live_direction_held_seconds"), snapshot_id, calculated_at),
        "etf_window_directions": _snapshot_field(continuation_state.get("confirm_window_directions"), snapshot_id, calculated_at),
        "expected_move_pct": _snapshot_field(continuation_result.get("expected_move_pct"), snapshot_id, calculated_at),
        "expected_gross_edge_pct": _snapshot_field(continuation_result.get("expected_gross_edge_pct"), snapshot_id, calculated_at),
        "cost_pct": _snapshot_field(continuation_result.get("cost_pct"), snapshot_id, calculated_at),
        "expected_net_edge_pct": _snapshot_field(continuation_result.get("expected_net_edge_pct"), snapshot_id, calculated_at),
        "gross_cost_ratio": _snapshot_field(continuation_result.get("gross_cost_ratio"), snapshot_id, calculated_at),
        "reward_risk": _snapshot_field(continuation_result.get("reward_risk"), snapshot_id, calculated_at),
        "position_cap": _snapshot_field(sizing_audit.get("position_cap"), snapshot_id, calculated_at),
        "target_ratio": _snapshot_field(sizing_audit.get("target_ratio"), snapshot_id, calculated_at),
        "calculated_quantity": _snapshot_field(sizing_audit.get("calculated_quantity"), snapshot_id, calculated_at),
        "order_skip_reason": _snapshot_field(sizing_audit.get("order_skip_reason") or order_skip, snapshot_id, calculated_at),
        "signal_detected_at": continuation_state.get("first_detected_at"),
        "snapshot_calculated_at": calculated_at,
        "order_requested_at": ((last_switch or {}).get("orders") or [{}])[0].get("timestamp") if (last_switch or {}).get("orders") else None,
        "explanation": (
            f"{continuation_result.get('structural_signal_label') or 'TREND_CONTINUATION_ENTRY'} 승인"
            if action == "BUY" else f"Continuation 차단: {primary_block_reason or block_reason}"
        ),
        "enhanced_result": dict(state.get("last_enhanced_result") or {}),
        "decision": dict(decision),
        "signal_summary": {
            "snapshot_id": snapshot_id,
            "calculated_at": calculated_at,
            "raw_score_leader": _raw_score_leader(decision),
            "live_trade_direction": live.get("direction") or "NONE",
            "actionable_signal": action,
            "final_action": action,
            "structural_signal_label": continuation_result.get("structural_signal_label") or action,
            "block_reason": block_reason,
            "primary_block_reason": primary_block_reason,
            "secondary_reasons": secondary_reasons,
            "conclusion": (
                f"{continuation_result.get('structural_signal_label') or 'TREND_CONTINUATION_ENTRY'} 승인"
                if action == "BUY" else f"Continuation 차단: {primary_block_reason or block_reason}"
            ),
        },
        "pipeline_trace": {"snapshot_id": snapshot_id, "calculated_at": calculated_at, "early_decision": early_result or {}, "continuation": continuation_result},
        "orders_this_cycle": today_orders,
    }
    state["last_completed_decision_snapshot"] = snapshot
    state["last_signal_summary"] = snapshot["signal_summary"]


def _downgrade_unconfirmed_strong_decision(decision: dict, state: dict) -> dict:
    final_action = str((decision or {}).get("final_action") or "")
    if final_action not in ("HYNIX_STRONG_BUY", "INVERSE_STRONG_BUY"):
        return decision

    desired = "UP" if final_action == "HYNIX_STRONG_BUY" else "DOWN"
    fallback_action = "HYNIX_BUY" if desired == "UP" else "INVERSE_BUY"
    bounce_label = "BOUNCE_UP" if desired == "UP" else "BOUNCE_DOWN"
    weighted = ((state.get("trend_continuation_entry") or {}).get("last_result") or {})
    adaptive = state.get("adaptive_regime") or {}
    structural_direction = _normalize_direction(
        weighted.get("structural_direction")
        or (state.get("last_primary_trend") or {}).get("primary_trend")
        or adaptive.get("confirmed_regime")
    )
    strong_ok = bool(
        weighted.get("strong_structure_confirmed") is True
        and weighted.get("evidence_score") is not None
        and float(weighted.get("evidence_score") or 0.0) >= 65.0
        and weighted.get("structural_signal_label") == final_action
    )
    if strong_ok:
        return decision

    adjusted = dict(decision or {})
    adjusted["final_action"] = fallback_action
    adjusted["strong_downgraded"] = True
    adjusted["strong_downgrade_reason"] = "STRONG_STRUCTURE_NOT_CONFIRMED"
    adjusted["structural_signal_label"] = (
        bounce_label if structural_direction in ("UP", "DOWN") and structural_direction != desired else fallback_action
    )
    adjusted["reasons"] = list(adjusted.get("reasons") or []) + [
        "STRONG label downgraded: weighted evidence/ETF/VWAP/EMA/structure/reward-risk confirmation missing"
    ]
    return adjusted


def _macd_williams_confirmation(df_1min, direction: Optional[str]) -> dict:
    direction = _normalize_direction(direction)
    if df_1min is None or getattr(df_1min, "empty", True) or direction not in ("UP", "DOWN"):
        return {"confirmed": False, "macd": None, "williams_r": None, "reason": "DATA_INSUFFICIENT"}
    try:
        import pandas as pd

        work = df_1min.copy()
        close = pd.to_numeric(work["close"], errors="coerce").dropna()
        high = pd.to_numeric(work["high"], errors="coerce").dropna() if "high" in work.columns else close
        low = pd.to_numeric(work["low"], errors="coerce").dropna() if "low" in work.columns else close
        if len(close) < 26 or len(high) < 14 or len(low) < 14:
            return {"confirmed": False, "macd": None, "williams_r": None, "reason": "DATA_INSUFFICIENT"}
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        macd_delta = float(macd.iloc[-1] - macd.iloc[-2])
        highest = high.rolling(14).max()
        lowest = low.rolling(14).min()
        span = (highest - lowest).replace(0.0, float("nan"))
        wr = ((highest - close) / span * -100.0).dropna()
        if len(wr) < 2:
            return {"confirmed": False, "macd": round(macd_delta, 6), "williams_r": None, "reason": "DATA_INSUFFICIENT"}
        wr_prev = float(wr.iloc[-2])
        wr_now = float(wr.iloc[-1])
        if direction == "UP":
            confirmed = macd_delta > 0 and wr_prev <= -80.0 and wr_now > -80.0
            reason = "MACD_UP_WILLIAMS_OVERSOLD_EXIT" if confirmed else "MACD_WILLIAMS_NOT_CONFIRMED"
        else:
            confirmed = macd_delta < 0 and wr_prev >= -20.0 and wr_now < -20.0
            reason = "MACD_DOWN_WILLIAMS_OVERBOUGHT_EXIT" if confirmed else "MACD_WILLIAMS_NOT_CONFIRMED"
        return {
            "confirmed": bool(confirmed),
            "macd": round(macd_delta, 6),
            "williams_r": round(wr_now, 4),
            "reason": reason,
        }
    except Exception as exc:
        return {"confirmed": False, "macd": None, "williams_r": None, "reason": f"ERROR:{exc}"}


RANGE_PROBE_HOLD_MIN_SECONDS = 30.0
RANGE_PROBE_HOLD_MAX_SECONDS = 45.0
RANGE_MACD_CONFIRM_WINDOW_SECONDS = 20.0


def _range_episode_probe_defaults() -> dict:
    return {
        "episode_status": None,
        "reversal_probe_done": False,
        "probe_entered_at": None,
        "probe_exit_at": None,
        "probe_macd_confirmed": False,
        "awaiting_structural_reentry": False,
        "last_swing_breakout_at": None,
        "last_vwap_reclaim_at": None,
        "prev_above_vwap": None,
        "prev_above_vwap_by_symbol": {},
    }


def reset_range_episode_probe_state(
    continuation_state: dict,
    *,
    now: datetime,
    direction: str,
    episode_id: str,
    reference_price: float | None = None,
) -> None:
    """방향 episode가 바뀔 때 probe 잠금·카운터를 초기화한다."""
    continuation_state.update(_range_episode_probe_defaults())
    continuation_state.update({
        "direction": direction,
        "direction_episode_id": episode_id,
        "first_detected_at": now.isoformat(),
        "reference_price": reference_price,
        "entry_done": False,
        "scale_in_done": False,
        "entry_path": None,
        "last_block_reason": None,
    })


def _structural_event_after_probe_exit(continuation_state: dict, event_at_key: str) -> bool:
    exit_at = continuation_state.get("probe_exit_at")
    event_at = continuation_state.get(event_at_key)
    if not exit_at or not event_at:
        return bool(event_at)
    try:
        return datetime.fromisoformat(event_at) >= datetime.fromisoformat(exit_at)
    except Exception:
        return False


def update_range_episode_structural_events(
    continuation_state: dict,
    *,
    now: datetime,
    swing_breakout: bool,
    vwap_reclaim: bool,
) -> None:
    """swing/VWAP 구조 이벤트를 기록하고 PROBE_FAILED·재진입 대기를 해제한다."""
    if swing_breakout:
        continuation_state["last_swing_breakout_at"] = now.isoformat()
    if vwap_reclaim:
        continuation_state["last_vwap_reclaim_at"] = now.isoformat()

    needs_unlock = continuation_state.get("awaiting_structural_reentry")
    if needs_unlock and (swing_breakout or vwap_reclaim):
        after_exit = (
            _structural_event_after_probe_exit(continuation_state, "last_swing_breakout_at")
            if swing_breakout
            else _structural_event_after_probe_exit(continuation_state, "last_vwap_reclaim_at")
        )
        if after_exit or not continuation_state.get("probe_exit_at"):
            continuation_state["episode_status"] = None
            continuation_state["awaiting_structural_reentry"] = False
            continuation_state["entry_done"] = False
            continuation_state["last_block_reason"] = None


def detect_opposite_episode_transition(
    *,
    existing_direction: str | None,
    new_direction: str,
    live_direction_matches: bool,
    confirm_dirs: dict,
    existing_structure_broken: bool,
    new_etf_vwap_reclaim: bool,
    new_etf_vwap_break: bool = False,
    new_swing_breakout: bool = False,
) -> bool:
    """opposite episode 전환은 아래 OR 중 하나만 충족하면 된다.

    1) swing structure breakout / structure broken against existing direction
       (existing structure break OR new-direction swing breakout; 5/10 불필요)
    2) 반대 ETF VWAP reclaim/break + ETF 5/10초 방향 확인 (5초 단독 불가)
    """
    if not existing_direction:
        return True
    if existing_direction == new_direction:
        return False
    if not live_direction_matches:
        return False
    if existing_structure_broken or new_swing_breakout:
        return True
    dirs_5_10_aligned = (
        confirm_dirs.get(5) == new_direction and confirm_dirs.get(10) == new_direction
    )
    vwap_ok = bool(new_etf_vwap_reclaim or new_etf_vwap_break)
    return bool(vwap_ok and dirs_5_10_aligned)


def range_episode_allows_entry(
    continuation_state: dict,
    *,
    entry_path: str | None,
    swing_breakout: bool,
    vwap_reclaim: bool,
    direction_changed: bool,
) -> tuple[bool, str | None]:
    """동일 episode 내 과매매 방지 — REVERSAL probe 1회, PROBE_FAILED는 동일 REVERSAL만 차단."""
    if direction_changed:
        return True, None

    structural_unlock = swing_breakout or vwap_reclaim
    normalized_path = (entry_path or "").upper() or None
    probe_failed = continuation_state.get("episode_status") == "PROBE_FAILED"
    # PROBE_FAILED locks repeating the same REVERSAL only; CONTINUATION needs new structure.
    if probe_failed and normalized_path == "REVERSAL":
        return False, "PROBE_FAILED_REVERSAL_BLOCKED"
    if probe_failed and normalized_path != "REVERSAL" and not structural_unlock:
        return False, "AWAITING_STRUCTURAL_REENTRY"
    if normalized_path == "REVERSAL" and continuation_state.get("reversal_probe_done"):
        return False, "REVERSAL_PROBE_ONCE_PER_EPISODE"
    if continuation_state.get("awaiting_structural_reentry") and not structural_unlock:
        return False, "AWAITING_STRUCTURAL_REENTRY"
    if continuation_state.get("entry_done"):
        # New structure after PROBE_FAILED must not keep CONTINUATION locked by entry_done.
        if probe_failed and normalized_path != "REVERSAL" and structural_unlock:
            continuation_state["entry_done"] = False
            continuation_state["awaiting_structural_reentry"] = False
            continuation_state["last_block_reason"] = None
            return True, None
        return False, "ENTRY_DONE_FOR_EPISODE"
    if probe_failed and normalized_path != "REVERSAL" and structural_unlock:
        continuation_state["awaiting_structural_reentry"] = False
        continuation_state["last_block_reason"] = None
    return True, None


def mark_range_reversal_probe_entered(
    continuation_state: dict,
    *,
    now: datetime,
    entry_path: str | None,
) -> None:
    if entry_path == "REVERSAL":
        continuation_state["reversal_probe_done"] = True
        continuation_state["probe_entered_at"] = now.isoformat()
        continuation_state["awaiting_structural_reentry"] = False


def mark_range_probe_failed(continuation_state: dict, *, now: datetime, reason: str) -> None:
    continuation_state["episode_status"] = "PROBE_FAILED"
    continuation_state["probe_failed_at"] = now.isoformat()
    continuation_state["probe_failed_reason"] = reason
    continuation_state["awaiting_structural_reentry"] = True
    continuation_state["probe_exit_at"] = now.isoformat()


def mark_range_probe_exit(
    continuation_state: dict,
    *,
    now: datetime,
    entry_path: str | None,
    reason: str,
    probe_failed: bool = False,
) -> None:
    continuation_state["probe_exit_at"] = now.isoformat()
    continuation_state["last_probe_exit_reason"] = reason
    continuation_state["awaiting_structural_reentry"] = True
    if probe_failed:
        mark_range_probe_failed(continuation_state, now=now, reason=reason)


def mark_range_episode_exit_awaiting_structure(
    continuation_state: dict,
    *,
    now: datetime,
    reason: str,
) -> None:
    """episode 내 완결 청산 후 구조 이벤트 전 재진입을 대기한다(REVERSAL probe 1회 규칙 유지)."""
    continuation_state["probe_exit_at"] = now.isoformat()
    continuation_state["last_probe_exit_reason"] = reason
    continuation_state["awaiting_structural_reentry"] = True


def promote_reversal_probe_to_continuation(continuation_state: dict, *, now: datetime) -> None:
    """45초 후 구조·방향·순이익 유지 시 REVERSAL probe를 CONTINUATION으로 승격."""
    continuation_state["entry_path"] = "CONTINUATION"
    continuation_state["episode_status"] = None
    continuation_state["awaiting_structural_reentry"] = False
    continuation_state["probe_promoted_at"] = now.isoformat()
    continuation_state["last_block_reason"] = None


def evaluate_weighted_range_probe_exit(
    *,
    continuation: dict,
    probe_direction: str,
    structure_reversal_confirmed: bool,
    held_window_dirs: dict,
    macd_confirmed: bool,
    etf_direction_aligned: bool,
    now: datetime,
    net_return_pct: float | None = None,
    hard_stop_pct: float | None = None,
) -> dict:
    """REVERSAL probe 보유·청산 — 30~45초 홀드, 5초 단독 역행 무시."""
    if hard_stop_pct is not None and net_return_pct is not None and net_return_pct <= hard_stop_pct:
        return {
            "action": "SELL_ALL",
            "ratio": 1.0,
            "reason": "hard stop",
            "probe_failed": False,
        }

    if structure_reversal_confirmed:
        return {
            "action": "SELL_ALL",
            "ratio": 1.0,
            "reason": "swing structure invalidated",
            "probe_failed": True,
        }

    required_opp = "DOWN" if probe_direction == "UP" else "UP"
    held_5_10_opposite = (
        held_window_dirs.get(5) == required_opp and held_window_dirs.get(10) == required_opp
    )
    if held_5_10_opposite:
        return {
            "action": "SELL_ALL",
            "ratio": 1.0,
            "reason": "5s+10s opposite confirmed",
            "probe_failed": False,
        }

    probe_entered_at = continuation.get("probe_entered_at") or continuation.get("first_detected_at")
    try:
        elapsed = (now - datetime.fromisoformat(probe_entered_at)).total_seconds() if probe_entered_at else 0.0
    except Exception:
        elapsed = 0.0

    if etf_direction_aligned and not structure_reversal_confirmed and elapsed < RANGE_PROBE_HOLD_MAX_SECONDS:
        return {"action": "HOLD", "ratio": 0.0, "reason": "probe hold window", "probe_failed": False}

    if elapsed >= RANGE_PROBE_HOLD_MAX_SECONDS and not macd_confirmed:
        if (
            etf_direction_aligned
            and not structure_reversal_confirmed
            and net_return_pct is not None
            and net_return_pct > 0.0
        ):
            return {
                "action": "PROMOTE_CONTINUATION",
                "ratio": 0.0,
                "reason": "REVERSAL probe promoted to CONTINUATION",
                "probe_failed": False,
            }
        return {
            "action": "SELL_ALL",
            "ratio": 1.0,
            "reason": "MACD/Williams not confirmed within hold window",
            "probe_failed": True,
        }

    if elapsed >= RANGE_PROBE_HOLD_MIN_SECONDS and not etf_direction_aligned:
        return {
            "action": "SELL_ALL",
            "ratio": 1.0,
            "reason": "ETF direction lost during probe hold",
            "probe_failed": True,
        }

    return {"action": "HOLD", "ratio": 0.0, "reason": None, "probe_failed": False}


def evaluate_weighted_continuation_exit(
    *,
    net_return_pct: float | None,
    hard_stop_pct: float,
    structure_reversal_confirmed: bool,
    regime_reversal_confirmed: bool,
    held_window_dirs: dict,
    position_direction: str,
    tp1_taken: bool = False,
    tp2_taken: bool = False,
    confirmed_regime: str | None = None,
) -> dict:
    """CONTINUATION 청산 — 5초 단독 역행 무시, hard stop·regime 반전은 즉시."""
    from app.trading.early_trend_detector import (
        REGIME_FAST_REVERSAL_RANGE,
        VOLATILE_RANGE_SL_PCT,
        VOLATILE_RANGE_TP1_MIN_SELL_RATIO,
        VOLATILE_RANGE_TP1_PCT,
        VOLATILE_RANGE_TP2_PCT,
        VOLATILE_RANGE_TP2_SELL_RATIO,
    )

    hold = {"action": "HOLD", "ratio": 0.0, "reason": None}
    if net_return_pct is not None and net_return_pct <= hard_stop_pct:
        return {"action": "SELL_ALL", "ratio": 1.0, "reason": "hard stop"}

    if regime_reversal_confirmed:
        return {"action": "SELL_ALL", "ratio": 1.0, "reason": "regime reversal confirmed"}

    if structure_reversal_confirmed:
        return {"action": "SELL_ALL", "ratio": 1.0, "reason": "swing structure invalidated"}

    required_opp = "DOWN" if position_direction == "UP" else "UP"
    if held_window_dirs.get(5) == required_opp and held_window_dirs.get(10) == required_opp:
        return {"action": "SELL_ALL", "ratio": 1.0, "reason": "5s+10s opposite confirmed"}

    regime_label = confirmed_regime or REGIME_FAST_REVERSAL_RANGE
    if net_return_pct is not None and net_return_pct <= VOLATILE_RANGE_SL_PCT:
        return {
            "action": "SELL_ALL",
            "ratio": 1.0,
            "reason": f"{regime_label} 손절(net {net_return_pct:.2f}% <= {VOLATILE_RANGE_SL_PCT}%)",
        }
    if not tp2_taken and net_return_pct is not None and net_return_pct >= VOLATILE_RANGE_TP2_PCT:
        return {
            "action": "SELL_PARTIAL",
            "ratio": VOLATILE_RANGE_TP2_SELL_RATIO,
            "reason": f"{regime_label} TP2(net {net_return_pct:.2f}% >= {VOLATILE_RANGE_TP2_PCT}%)",
        }
    if not tp1_taken and net_return_pct is not None and net_return_pct >= VOLATILE_RANGE_TP1_PCT:
        return {
            "action": "SELL_PARTIAL",
            "ratio": VOLATILE_RANGE_TP1_MIN_SELL_RATIO,
            "reason": (
                f"{regime_label} TP1(net {net_return_pct:.2f}% >= {VOLATILE_RANGE_TP1_PCT}%) — "
                f"{VOLATILE_RANGE_TP1_MIN_SELL_RATIO * 100:.0f}%+ 부분익절"
            ),
        }
    return hold


def _first_blocked_stage(trace: dict) -> Optional[str]:
    """Signal이 BUY/SELL/INVERSE인데 실제로 어느 단계에서 멈췄는지 첫 번째로 찾는다.

    HOLD는 애초에 아무것도 시도하지 않는 게 정상이므로 항상 None(정상)이다.
    """
    if trace["prediction_signal"] == "HOLD":
        return None
    if trace["entry_approved"] is False:
        return "entry_approved"
    if not trace["risk_manager_ok"]:
        return "risk_manager"
    if trace.get("enhanced_direct_order_blocked"):
        early_decision = trace.get("early_decision") or {}
        early_order = trace.get("early_order_result") or {}
        if early_order.get("broker_executed"):
            return None
        if early_decision.get("reason_code") == "TARGET_ALREADY_FILLED":
            return None
        return "early_order"
    if not trace["order_sent"]:
        # order_sent=False만으로는 "주문 전송 실패"로 단정하지 않는다 — 이미 목표
        # 종목을 보유해서 추가 진입이 필요 없었거나(entry), POSITION_SYNC_PENDING으로
        # 차단됐을 수 있다(state_sync). 실행 계층(run_switch_or_entry)이 보고한
        # execution_stage를 우선 신뢰하고, 없을 때만(레거시 경로 등) order_sent로 본다.
        exec_stage = trace.get("execution_stage")
        if exec_stage in ("entry", "state_sync"):
            return exec_stage
        return "order_sent"
    if not trace["broker_executed"]:
        return "broker_executed"
    if trace["position_confirmed"] is False:
        return "position_confirmed"
    if trace["ui_synced"] is False:
        return "ui_synced"
    return None


_ORDER_FAILURE_CODE_TEXT = {
    "ORDER_QTY_ZERO": "계산된 매수 수량이 0주(투입 현금이 1주 가격보다 작음)",
    "BUYABLE_CASH_ZERO": "매수가능금액(buyable cash) 조회 결과 0원",
    "PRICE_UNAVAILABLE": "현재가 조회 실패로 가격을 확인할 수 없음",
    "COOLDOWN_ACTIVE": "직전 매수 후 쿨다운 시간 이내 — 신규 매수 대기",
    "ORDER_IN_FLIGHT": "이미 처리 중인 주문이 있어 중복 전송 방지",
    "BROKER_REJECTED": "브로커가 주문을 거부함",
    "EXECUTION_EXCEPTION": "주문 실행 중 예외 발생",
}


def _build_blocking_reason(trace: dict) -> Optional[str]:
    """stopped_stage를 사람이 읽을 수 있는 한 줄 사유로 변환 (UI의 blocking_reason 필드).

    요구사항(2026-07-16) — entry/state_sync/order_sent 단계는 절대 entry_approved_reason
    (진입 "승인" 문구)을 실패 사유로 재사용하지 않는다. run_switch_or_entry()가 실제로
    보고한 execution_message/order_failure_code/broker_error만 사용하고, 실패코드가
    있으면 사람이 읽을 한 줄 설명(+ 원본 코드)을 함께 보여준다."""
    stage = trace.get("stopped_stage")
    if not stage:
        return None

    def _execution_reason(fallback: str) -> str:
        code = trace.get("order_failure_code")
        msg = trace.get("execution_message")
        broker_error = trace.get("broker_error")
        if code:
            text = _ORDER_FAILURE_CODE_TEXT.get(code, code)
            detail = f" — {broker_error}" if broker_error else (f" ({msg})" if msg else "")
            return f"{code}: {text}{detail}"
        if msg:
            return msg
        return fallback

    reason_map = {
        "entry_approved": trace.get("entry_approved_reason"),
        "risk_manager": trace.get("risk_manager_reason"),
        "entry": _execution_reason("이미 목표 종목 보유 중이거나 추가 진입이 필요 없어 주문을 시도하지 않음"),
        "state_sync": _execution_reason("POSITION_SYNC_PENDING — 브로커 잔고 확인 전이라 주문 차단"),
        "order_sent": _execution_reason("주문이 브로커로 전송되지 않음(가격 조회 실패/쿨다운/허용 시간대 아님 등)"),
        "broker_executed": "주문은 전송됐으나 브로커 체결 실패",
        "position_confirmed": "체결 후 재조회한 포지션이 기대와 불일치",
        "ui_synced": "상태 저장(디스크 반영) 실패 — 다음 사이클에서 재시도됨",
        "early_order": (
            f"{((trace.get('early_decision') or {}).get('reason_code') or 'NO_EARLY_SIGNAL')}: "
            f"{((trace.get('early_decision') or {}).get('reason') or 'Early Detector 주문 미실행')}"
        ),
    }
    return f"[{stage}] {reason_map.get(stage) or '알 수 없음'}"


def evaluate_pullback_gate(
    state: dict, desired_symbol: str, final_action: str, now: datetime, forced_info: dict, hynix_df_1min, mode: str,
    primary_trend_result: Optional[dict] = None,
) -> dict:
    """General BUY pullback gate with a two-minute maximum wait."""
    held_symbol = (state.get("position") or {}).get("symbol")
    ptrend = primary_trend_result or {}
    primary_trend = ptrend.get("primary_trend", PRIMARY_TREND_RANGE)
    live_trade = state.get("live_trade_direction") or {}
    live_direction_for_desired = "UP" if desired_symbol == HYNIX_SYMBOL else "DOWN" if desired_symbol == INVERSE_SYMBOL else None
    live_reversal_allows_direction = (
        live_trade.get("status") == "REVERSAL_CANDIDATE"
        and live_trade.get("direction") == live_direction_for_desired
    )
    trend_blocked_reason = None
    if not live_reversal_allows_direction and final_action in ("INVERSE_BUY", "INVERSE_STRONG_BUY") and new_inverse_entry_blocked(
        primary_trend, ptrend.get("above_vwap"), ptrend.get("above_ema20"), ptrend,
    ):
        votes, vote_reasons = inverse_block_vote_count(ptrend)
        trend_blocked_reason = (
            f"PRIMARY_TREND=UP with {votes} uptrend confirmations({', '.join(vote_reasons)}) - new INVERSE entry blocked "
            f"(short-term move classified as {classify_short_term_move(primary_trend, None)}"
            "; requires 2x-confirmed VWAP/15m-trend/swing-low breakdown to flip)"
        )
    elif not live_reversal_allows_direction and final_action in ("HYNIX_BUY", "HYNIX_STRONG_BUY") and new_hynix_entry_blocked(
        primary_trend, ptrend.get("above_vwap"), ptrend.get("above_ema20"),
    ):
        trend_blocked_reason = (
            "PRIMARY_TREND=DOWN and price below VWAP/EMA20 - new HYNIX entry blocked "
            "(requires 2x-confirmed VWAP/15m-trend/swing-high breakout to flip)"
        )
    if trend_blocked_reason:
        state["last_trend_switch_plan"] = {
            "proceed": False, "dominant_direction": signal_direction(final_action), "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": None, "block_reason": trend_blocked_reason,
            "primary_trend": primary_trend,
        }
        return {
            "proceed": False, "deadline_expired": False, "pullback_wait_remaining_seconds": None,
            "message": trend_blocked_reason,
        }
    confirm_tracker = update_confirm_tracker(
        state.get("trend_switch_confirm_tracker") or default_confirm_state(),
        final_action, held_symbol, desired_symbol, now,
    )
    frequency_state = state.get("trend_switch_frequency_state") or default_trend_frequency_state()
    state["trend_switch_confirm_tracker"] = confirm_tracker
    has_unconfirmed_order = bool(
        state.get("order_in_flight")
        or state.get("pending_order")
        or state.get("trend_switch_unconfirmed_order")
    )
    pre_plan = plan_trend_switch_entry(
        final_action=final_action,
        held_symbol=held_symbol,
        desired_symbol=desired_symbol,
        confirm_tracker=confirm_tracker,
        frequency_state=frequency_state,
        pullback_result=None,
        now=now,
        data_ok=bool(desired_symbol),
        has_unconfirmed_order=has_unconfirmed_order,
        daily_return_pct=state.get("realized_pnl_today_pct"),
        atr_pct=None,
        override_daily_loss_block=bool(state.get("daily_loss_block_override")),
    )
    state["last_trend_switch_plan"] = {
        **pre_plan,
        "dominant_direction": signal_direction(final_action),
        "desired_symbol": desired_symbol,
        "pullback_wait_remaining_seconds": None,
    }
    if pre_plan.get("proceed"):
        return {
            "proceed": True,
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": 0,
            "message": (
                f"TrendSwitchAccel 즉시 진입: {pre_plan.get('entry_type')} "
                f"{(pre_plan.get('position_pct') or 0) * 100:.0f}%"
            ),
        }
    pre_block = str(pre_plan.get("block_reason") or "")
    if pre_block and "눌림목" not in pre_block:
        return {
            "proceed": False,
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": None,
            "message": pre_block,
        }

    pending = state.get("pending_entry")
    if not pending or pending.get("action") != final_action or pending.get("symbol") != desired_symbol:
        pending = {"action": final_action, "symbol": desired_symbol, "since": now.isoformat()}
        state["pending_entry"] = pending

    try:
        since = datetime.fromisoformat(pending["since"])
    except Exception:
        since = now

    deadline = since + timedelta(minutes=_PULLBACK_PATIENCE_MINUTES)
    window = forced_info.get("window")
    if window:
        try:
            _, end_str = window.split("-")
            deadline = min(deadline, datetime.combine(now.date(), _parse_hm(end_str)))
        except Exception:
            pass

    remaining_seconds = max(0, int((deadline - now).total_seconds()))
    if now >= deadline:
        deadline_plan = plan_trend_switch_entry(
            final_action=final_action,
            held_symbol=held_symbol,
            desired_symbol=desired_symbol,
            confirm_tracker=confirm_tracker,
            frequency_state=frequency_state,
            pullback_result={"proceed": True, "message": "pullback wait expired with current signal revalidated"},
            now=now,
            data_ok=bool(desired_symbol),
            has_unconfirmed_order=has_unconfirmed_order,
            daily_return_pct=state.get("realized_pnl_today_pct"),
            atr_pct=None,
            override_daily_loss_block=bool(state.get("daily_loss_block_override")),
        )
        state["last_trend_switch_plan"] = {
            **deadline_plan,
            "dominant_direction": signal_direction(final_action),
            "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": 0,
        }
        return {
            "proceed": bool(deadline_plan.get("proceed")),
            "deadline_expired": True,
            "pullback_wait_remaining_seconds": 0,
            "message": (
                f"눌림목 대기 데드라인 2분 만료({deadline.strftime('%H:%M')}) - 현재 신호 재검증 후 진입 허용"
                if deadline_plan.get("proceed") else deadline_plan.get("block_reason")
            ),
        }

    df_for_check = hynix_df_1min if desired_symbol == HYNIX_SYMBOL else _load_inverse_1min_for_pullback(mode)
    pullback = detect_pullback(df_for_check)
    try:
        pullback_pct = float(pullback.get("pullback_pct") or pullback.get("drop_pct") or pullback.get("distance_pct") or 0.0)
    except Exception:
        pullback_pct = 0.0
    if pullback_pct >= 4.0:
        message = f"deep pullback {pullback_pct:.2f}% - re-evaluate current trend instead of waiting on old high"
        wait_plan = {**pre_plan, "block_reason": message}
        state["last_trend_switch_plan"] = {
            **wait_plan,
            "dominant_direction": signal_direction(final_action),
            "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": 0,
            "dynamic_pullback_basis": pullback,
        }
        return {
            "proceed": False,
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": 0,
            "message": message,
        }
    if pullback.get("is_pullback"):
        pullback_plan = plan_trend_switch_entry(
            final_action=final_action,
            held_symbol=held_symbol,
            desired_symbol=desired_symbol,
            confirm_tracker=confirm_tracker,
            frequency_state=frequency_state,
            pullback_result={"proceed": True, "message": pullback.get("reason")},
            now=now,
            data_ok=bool(desired_symbol),
            has_unconfirmed_order=has_unconfirmed_order,
            daily_return_pct=state.get("realized_pnl_today_pct"),
            atr_pct=None,
            override_daily_loss_block=bool(state.get("daily_loss_block_override")),
        )
        state["last_trend_switch_plan"] = {
            **pullback_plan,
            "dominant_direction": signal_direction(final_action),
            "desired_symbol": desired_symbol,
            "pullback_wait_remaining_seconds": remaining_seconds,
        }
        return {
            "proceed": bool(pullback_plan.get("proceed")),
            "deadline_expired": False,
            "pullback_wait_remaining_seconds": remaining_seconds,
            "message": (
                f"눌림목 진입 조건 충족: {pullback.get('reason')}"
                if pullback_plan.get("proceed") else pullback_plan.get("block_reason")
            ),
        }
    wait_plan = {
        **pre_plan,
        "block_reason": f"눌림목 대기 중({pullback.get('reason')}) - {deadline.strftime('%H:%M')}까지 대기",
    }
    state["last_trend_switch_plan"] = {
        **wait_plan,
        "dominant_direction": signal_direction(final_action),
        "desired_symbol": desired_symbol,
        "pullback_wait_remaining_seconds": remaining_seconds,
    }
    return {
        "proceed": False,
        "deadline_expired": False,
        "pullback_wait_remaining_seconds": remaining_seconds,
        "message": wait_plan["block_reason"],
    }


def _load_inverse_1min_for_pullback(mode: str):
    try:
        from app.data_sources.hynix_inverse_collector import collect_inverse_minute

        result = collect_inverse_minute(mode=mode)
        return result.get("df_1min")
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] 인버스 1분봉 조회 실패(눌림목 판단용): %s", exc)
        return None


def _create_strategy_broker(cfg, mode: str):
    """Create the account adapter for the selected mode while keeping strategy logic common.

    mock/real must share signals, gates and sizing. The only branch here is the
    broker adapter and real-order safety confirmation required before a real KIS
    order-capable broker can be constructed.
    """
    from app.trading.broker_factory import create_broker

    if mode == "real":
        gate_status = (
            cfg.enhanced_real_gate_status(current_mode=mode)
            if hasattr(cfg, "enhanced_real_gate_status")
            else {"ready": cfg.full_auto_real_confirm_ok(), "blocking_reasons": [], "checks": {}}
        )
        if not bool(gate_status.get("ready")):
            raise RuntimeError(
                "REAL gate blocked: "
                + ", ".join(gate_status.get("blocking_reasons") or ["UNKNOWN"])
            )
        return create_broker(
            cfg, mode="real", confirm_text=cfg.full_auto_real_confirm_text(),
            runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
        )
    return create_broker(cfg, mode=mode)


def set_control(
    auto_trade_on: Optional[bool] = None, mode: Optional[str] = None,
    allow_mock_loss_override: Optional[bool] = None, mock_budget_krw: Optional[float] = None,
) -> dict:
    """UI에서 자동매매 ON/OFF, mock/real 모드, mock 예산을 설정할 때 사용."""
    if mode is not None:
        set_active_mode(mode)
    state = load_state(mode=mode)
    if auto_trade_on is not None:
        state["auto_trade_on"] = bool(auto_trade_on)
    if mode is not None:
        state["mode"] = mode
    if allow_mock_loss_override is not None:
        state["allow_mock_loss_override"] = bool(allow_mock_loss_override)
    if mock_budget_krw is not None:
        state["mock_budget_krw"] = float(mock_budget_krw)
        if state.get("mode") == "mock" and not (state.get("position") or {}).get("symbol"):
            state["cash"] = float(mock_budget_krw)
    save_state_atomic(state)
    return state


def reset_mock_account(budget_krw: Optional[float] = None) -> dict:
    """UI의 'mock 계좌 초기화' 버튼 — 포지션/거래횟수/현금을 오늘자 기준으로 완전히 새로 시작."""
    return reset_mock_state(budget_krw=budget_krw)


DAILY_RETURN_UNKNOWN = "DAILY_RETURN_UNKNOWN"
ACCOUNT_EQUITY_MISMATCH = "ACCOUNT_EQUITY_MISMATCH"
_EQUITY_MISMATCH_TOLERANCE_PCT = 0.5
_EQUITY_MISMATCH_RETRY_ATTEMPTS = 3
_EQUITY_MISMATCH_RETRY_DELAY_SECONDS = 3
# 요구사항(2026-07-16 실측) — 60초는 3분 주기 사이클 + KIS 레이트리밋(EGW00201)
# 백오프/재시도 지연을 감안하면 너무 짧다. 실제 매수 직후 브로커 잔고에 그
# 매수가 반영되기까지(레이트리밋으로 재시도가 몇 번 겹치면) 60초를 쉽게 넘길 수
# 있고, 그 사이에 유예가 끝나버리면 방금 막 체결된 정상 거래를
# ACCOUNT_EQUITY_MISMATCH로 오판해 신규주문을 막아버린다(2026-07-16 실측: 500만원
# 매수 직후 인버스 신호가 이 사유로 차단됨). 사이클 1~2회분 여유를 두도록 5분으로 늘린다.
_ACCOUNT_SETTLEMENT_GRACE_SECONDS = 300
_ACCOUNT_SNAPSHOT_FALLBACK_SECONDS = 90


def _position_market_value(position) -> float:
    if isinstance(position, dict):
        qty = float(position.get("quantity", position.get("hldg_qty", 0)) or 0)
        price = float(position.get("current_price", position.get("prpr", 0)) or 0)
        market_value = position.get("market_value")
    else:
        qty = float(getattr(position, "quantity", 0) or 0)
        price = float(getattr(position, "current_price", 0) or 0)
        market_value = getattr(position, "market_value", None)
    if market_value not in (None, ""):
        try:
            return float(market_value)
        except Exception:
            pass
    return qty * price


def _account_settlement_grace_active(state: dict, now: datetime) -> bool:
    candidates = [
        state.get("last_trade_time"),
        state.get("last_order_time"),
        (state.get("position") or {}).get("entry_time"),
    ]
    for raw in candidates:
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw))
        except Exception:
            continue
        if 0 <= (now - ts).total_seconds() <= _ACCOUNT_SETTLEMENT_GRACE_SECONDS:
            return True
    return False


def _read_account_equity_snapshot(broker, now: datetime) -> dict:
    """Read one account snapshot and calculate equity from that same response."""
    snapshot = {
        "ok": False, "cash": None, "holdings_market_value": None, "current_equity": None,
        "positions": [], "error": None, "source": None, "as_of": now.isoformat(timespec="seconds"),
    }
    try:
        if hasattr(broker, "kis") and hasattr(broker.kis, "get_balance"):
            bal = broker.kis.get_balance()
            if bal.get("error"):
                snapshot["error"] = bal.get("error")
                snapshot["rt_cd"] = bal.get("rt_cd")
                snapshot["msg_cd"] = bal.get("msg_cd")
                snapshot["msg1"] = bal.get("msg1")
                snapshot["response_field_names"] = bal.get("response_field_names")
                snapshot["output1_field_names"] = bal.get("output1_field_names")
                snapshot["output2_field_names"] = bal.get("output2_field_names")
                return snapshot
            positions = bal.get("positions") or []
            cash = bal.get("cash", None)
            if cash is None:
                snapshot["error"] = "cash field missing"
                return snapshot
            holdings = sum(_position_market_value(p) for p in positions)
            snapshot.update({
                "ok": True, "cash": float(cash), "holdings_market_value": holdings,
                "current_equity": float(cash) + holdings, "positions": positions,
                "source": "kis.get_balance",
                "response_field_names": bal.get("response_field_names"),
                "output1_field_names": bal.get("output1_field_names"),
                "output2_field_names": bal.get("output2_field_names"),
            })
            return snapshot

        positions = broker.get_positions()
        cash = broker.get_balance() if hasattr(broker, "get_balance") else None
        if cash is None:
            snapshot["error"] = "broker cash field missing"
            return snapshot
        holdings = sum(_position_market_value(p) for p in positions)
        snapshot.update({
            "ok": True, "cash": float(cash), "holdings_market_value": holdings,
            "current_equity": float(cash) + holdings, "positions": positions,
            "source": "broker.get_balance+get_positions",
        })
        return snapshot
    except Exception as exc:
        snapshot["error"] = str(exc)
        return snapshot


def _recent_valid_account_snapshot(state: dict, now: datetime) -> Optional[dict]:
    """Return a recent successful account snapshot when KIS is temporarily rate-limited.

    This is only a short-lived read-through fallback for EGW00201/tokenP throttling.
    It must not turn a missing or failed account response into 0 won.
    """
    snapshot = state.get("last_account_equity_snapshot") or {}
    if not snapshot.get("ok"):
        return None
    if snapshot.get("cash") is None or snapshot.get("current_equity") is None:
        return None
    raw_as_of = snapshot.get("as_of")
    if not raw_as_of:
        return None
    try:
        as_of = datetime.fromisoformat(str(raw_as_of))
    except Exception:
        return None
    try:
        age_seconds = (now - as_of).total_seconds()
    except Exception:
        return None
    if age_seconds < 0 or age_seconds > _ACCOUNT_SNAPSHOT_FALLBACK_SECONDS:
        return None
    cached = dict(snapshot)
    cached["ok"] = True
    cached["cached_fallback"] = True
    cached["cached_age_seconds"] = int(age_seconds)
    cached["source"] = f"{cached.get('source') or 'account_snapshot'}+recent_cache"
    return cached


def compute_net_daily_return(
    state: dict, position: Optional[dict], hynix_price: Optional[float], inverse_price: Optional[float],
    cash: Optional[float], positions_from_broker: Optional[list], cash_fetch_ok: bool,
    settlement_grace_active: bool = False,
) -> dict:
    """risk_manager와 UI가 공유하는 단일 일일손익 계산(요구사항 1/5절).

    net_daily_return = (net_realized_pnl + net_unrealized_pnl) / starting_equity.
    실현손익은 원장(state["realized_pnl_today_krw"])을, 미실현손익은 현재 보유
    포지션+현재가로 계산한다 — 둘 다 "지금 이 순간의 계좌 잔고 조회"에 의존하지
    않으므로, KIS API 오류(레이트리밋 등)로 잔고조회가 0을 반환해도 이 계산 자체는
    영향받지 않는다(2026-07-14 실측 버그: total_equity=0 → 일손실 -100%로 오판).

    cash/positions_from_broker는 오직 교차검증(current_equity 대조)에만 쓰이며,
    주 계산식에는 들어가지 않는다. cash_fetch_ok=False(조회 실패/빈 응답/필드
    누락)면 DAILY_RETURN_UNKNOWN으로 신규주문만 보류하고 손실로 기록하지 않는다.
    """
    net_realized_pnl = state.get("realized_pnl_today_krw", 0.0)
    result = {
        "starting_equity": state.get("daily_pnl_baseline_equity"),
        "net_realized_pnl": net_realized_pnl, "net_unrealized_pnl": 0.0,
        "net_daily_return": None, "current_equity": None,
        "calculation_source": "ledger_unified", "blocked_reason": None,
        "equity_tolerance_pct": _EQUITY_MISMATCH_TOLERANCE_PCT,
        "settlement_grace_active": settlement_grace_active,
    }

    has_position = bool(
        position and position.get("symbol") and (position.get("quantity") or 0) > 0 and position.get("entry_price"),
    )

    def _local_unrealized_pnl() -> float:
        if not has_position:
            return 0.0
        cur = hynix_price if position["symbol"] == HYNIX_SYMBOL else inverse_price
        if cur is None:
            return 0.0
        try:
            from app.trading.trading_cost_engine import TradeCostEngine

            cost_result = TradeCostEngine().compute_unrealized_net_pnl(
                position["symbol"], entry_price=position["entry_price"], current_price=cur,
                quantity=position["quantity"],
            )
            return float(cost_result["net_unrealized_pnl"])
        except Exception:
            return float((cur - position["entry_price"]) * position["quantity"])

    # 요구사항(2026-07-16 실측) — 방금 체결된 실매수 직후 KIS 잔고조회(output1)가
    # 아직 새 보유종목을 반영하지 못해 positions_from_broker가 비어있거나 보유 중인
    # 심볼을 포함하지 않는 짧은 지연이 실제로 발생한다(모의/실전 공통). 이 상태로
    # holdings_value를 계산하면 current_equity가 방금 매수한 금액만큼 실제보다 낮게
    # 잡혀 큰 괴리가 생기고, 이를 ACCOUNT_EQUITY_MISMATCH(계좌 데이터 이상)로 오판해
    # 신규주문이 계속 차단됐다(BUY 신호가 risk_manager에서 반복 차단됨) — 브로커
    # 포지션 동기화 지연은 계좌 이상이 아니므로, cash_fetch_ok=False와 동일하게
    # 원장/entry_price 기준(신뢰 가능) 미실현손익 계산으로 즉시 대체한다.
    if has_position and cash_fetch_ok and positions_from_broker is not None:
        broker_has_held_symbol = any(
            (p.get("symbol") if isinstance(p, dict) else getattr(p, "symbol", None)) == position.get("symbol")
            for p in positions_from_broker
        )
        if not broker_has_held_symbol:
            cash_fetch_ok = False
            result["calculation_warning"] = "BROKER_POSITION_SYNC_LAG_LEDGER_FALLBACK"

    if not cash_fetch_ok:
        starting_equity = result["starting_equity"]
        if starting_equity and starting_equity > 0:
            net_unrealized_pnl = _local_unrealized_pnl()
            result["net_unrealized_pnl"] = net_unrealized_pnl
            result["net_daily_return"] = round((net_realized_pnl + net_unrealized_pnl) / starting_equity * 100.0, 4)
            result["calculation_warning"] = result.get("calculation_warning") or "ACCOUNT_SNAPSHOT_UNAVAILABLE_LEDGER_FALLBACK"
            return result
        result["blocked_reason"] = DAILY_RETURN_UNKNOWN
        return result

    holdings_value = sum(
        (getattr(p, "market_value", None) if not isinstance(p, dict) else p.get("market_value")) or 0.0
        for p in (positions_from_broker or [])
    )
    current_equity = (float(cash) + holdings_value) if cash is not None else None
    result["current_equity"] = current_equity

    starting_equity = result["starting_equity"]
    if starting_equity is None:
        # 요구사항(2026-07-20 실측) — 당일 "첫 유효 조회"가 반드시 장 시작 직후(오늘
        # 거래가 전혀 없던 시점)에 일어난다는 보장이 없다. KIS 토큰 발급 지연/
        # 레이트리밋(EGW00201/EGW00123 — 이 파일에 이미 여러 번 실측된 문제)이나
        # 앱 재시작으로 첫 성공 조회가 오늘 이미 실현손익이 쌓였거나 포지션을 보유해
        # 미실현손익이 있는 "이후" 시점에 일어나면, 그 순간의 current_equity를 그대로
        # 기준자산으로 저장했다 — 그 결과 이후 매 사이클 원장 기준(net_daily_return)이
        # 이미 기준자산에 반영된 그 손익을 다시 더해 이중계산하면서, 원장 기준과
        # 현재자산 기준 수익률 사이에 "기준자산을 캡처한 시점까지의 손익"만큼 고정된
        # 괴리가 생겼다. 이 괴리는 일시적 API 글리치가 아니라 구조적인 오차라서
        # 재시도(3회)/정산지연 설명/5분 유예 중 어느 것으로도 해소되지 않고, 그날
        # 남은 사이클 내내 ACCOUNT_EQUITY_MISMATCH로 신규주문이 계속 차단됐다.
        # 수정: 기준자산을 "지금 시점의 current_equity"가 아니라 오늘 이미 발생한
        # 실현+미실현 손익을 역산해서 뺀 값(=진짜 하루 시작 시점 자산)으로 설정한다.
        if current_equity is not None and current_equity > 0:
            net_unrealized_pnl = _local_unrealized_pnl()
            adjusted_baseline = current_equity - net_realized_pnl - net_unrealized_pnl
            if adjusted_baseline > 0:
                state["daily_pnl_baseline_equity"] = adjusted_baseline
                result["starting_equity"] = adjusted_baseline
                result["net_unrealized_pnl"] = net_unrealized_pnl
                result["net_daily_return"] = round((net_realized_pnl + net_unrealized_pnl) / adjusted_baseline * 100.0, 4)
                # 요구사항 — 방금 막 확정한 기준자산에서 도출된 수익률 하나만으로
                # 곧바로 일일손실 강제중단(-2.5%)까지 실행하지는 않는다(호출부가
                # 이 플래그를 보고 스킵). 첫 스냅샷은 아직 가격/잔고 데이터가 안정된
                # 것으로 확인되지 않았고, 예전 코드가 net_daily_return=0.0으로
                # 고정했던 것도 바로 이 "첫 표본의 노이즈로 즉시 조치하지 않는다"는
                # 취지였다 — 다음 사이클에도 같은(이미 확정된) 기준자산으로 재계산해
                # 여전히 -2.5% 이하면 그때는 정상적으로 차단된다.
                result["baseline_just_established"] = True
            else:
                # 오늘 이미 발생한 손실이 현재자산 이상으로 극단적인 경우(이론상
                # 방어) — 역산 기준자산이 0 이하로 나오면 대신 현재자산을 그대로
                # 기준으로 삼는다(0/음수 기준자산으로 나눗셈하지 않도록).
                state["daily_pnl_baseline_equity"] = current_equity
                result["starting_equity"] = current_equity
                result["net_daily_return"] = 0.0
                result["baseline_just_established"] = True
        else:
            result["blocked_reason"] = DAILY_RETURN_UNKNOWN
        return result

    if starting_equity <= 0:
        # 이전 버전의 버그(또는 수동 조작)로 이미 저장된 기준자산이 0/음수라면,
        # 무한 차단 대신 "기준자산 없음"과 동일하게 취급해 위와 같은 방식으로
        # 즉시 재계산(self-heal)한다 — 하루 종일 차단된 채로 남지 않는다.
        if current_equity is not None and current_equity > 0:
            net_unrealized_pnl = _local_unrealized_pnl()
            adjusted_baseline = current_equity - net_realized_pnl - net_unrealized_pnl
            if adjusted_baseline > 0:
                state["daily_pnl_baseline_equity"] = adjusted_baseline
                result["starting_equity"] = adjusted_baseline
                result["net_unrealized_pnl"] = net_unrealized_pnl
                result["net_daily_return"] = round((net_realized_pnl + net_unrealized_pnl) / adjusted_baseline * 100.0, 4)
                result["baseline_rebased"] = True
                result["baseline_just_established"] = True
                return result
        result["blocked_reason"] = ACCOUNT_EQUITY_MISMATCH
        return result

    # 요구사항 6절 — current_equity<=0인데 현금/원장잔고가 실제로 존재해야 하는 상황
    if current_equity is not None and current_equity <= 0 and (net_realized_pnl != 0 or starting_equity > 0):
        result["blocked_reason"] = ACCOUNT_EQUITY_MISMATCH if has_position else DAILY_RETURN_UNKNOWN
        return result

    # ── 미실현손익(요구사항 2절 — 보유 없으면 0) ─────────────────────────────
    net_unrealized_pnl = _local_unrealized_pnl()
    result["net_unrealized_pnl"] = net_unrealized_pnl

    net_daily_return = (net_realized_pnl + net_unrealized_pnl) / starting_equity * 100.0

    # 요구사항 6절 — 원장기준 수익률과 현재자산기준 수익률 차이가 0.1%p 초과하면
    # 계좌 데이터 불일치로 간주하고 -100% 같은 값으로 기록하지 않는다.
    if current_equity is not None and current_equity > 0:
        equity_ratio_return = (current_equity / starting_equity - 1.0) * 100.0
        if abs(equity_ratio_return - net_daily_return) > _EQUITY_MISMATCH_TOLERANCE_PCT:
            if not has_position and net_realized_pnl == 0 and net_unrealized_pnl == 0:
                state["daily_pnl_baseline_equity"] = current_equity
                result["starting_equity"] = current_equity
                result["net_daily_return"] = 0.0
                result["equity_ratio_return"] = 0.0
                result["baseline_rebased"] = True
                return result
            # KRX는 매도대금을 T+2에 정산한다 — 오늘 청산한 왕복거래의 실현손익은
            # 원장(net_realized_pnl)에는 즉시 반영되지만, 브로커의 실제 현금(cash)에는
            # 아직 반영되지 않은 게 정상이다. 이 "정산 지연"만으로 설명되는 차이라면
            # (미실현손익 기준 기대 수익률과는 실제로 일치) 계좌 이상이 아니므로 차단하지
            # 않는다 — 이전에는 60초 유예만으로는 턱없이 부족해 실현손익이 있는 날은
            # 이후 신규주문이 하루 종일 계속 차단되는 문제가 있었다.
            unsettled_adjusted_return = (net_unrealized_pnl / starting_equity) * 100.0
            settlement_adjusted_mismatch = abs(equity_ratio_return - unsettled_adjusted_return)
            if settlement_adjusted_mismatch <= _EQUITY_MISMATCH_TOLERANCE_PCT:
                result["net_daily_return"] = round(net_daily_return, 4)
                result["equity_ratio_return"] = round(equity_ratio_return, 4)
                result["mismatch_explained_by_unsettled_realized_pnl"] = True
                result["unsettled_realized_pnl_krw"] = net_realized_pnl
                return result
            if settlement_grace_active:
                result["net_daily_return"] = round(net_daily_return, 4)
                result["equity_ratio_return"] = round(equity_ratio_return, 4)
                result["mismatch_deferred"] = True
                return result
            result["blocked_reason"] = ACCOUNT_EQUITY_MISMATCH
            result["equity_ratio_return"] = round(equity_ratio_return, 4)
            return result

    result["net_daily_return"] = round(net_daily_return, 4)
    return result


def _compute_net_daily_return_with_retries(
    state: dict, broker, position: Optional[dict], hynix_price: Optional[float], inverse_price: Optional[float],
    now: datetime, *, attempts: int = _EQUITY_MISMATCH_RETRY_ATTEMPTS,
    delay_seconds: int = _EQUITY_MISMATCH_RETRY_DELAY_SECONDS,
) -> dict:
    attempts = max(1, int(attempts or 1))
    grace = _account_settlement_grace_active(state, now)
    history = []
    last_result = None
    last_snapshot = None
    for idx in range(attempts):
        snapshot = _read_account_equity_snapshot(broker, now)
        if not snapshot.get("ok"):
            cached_snapshot = _recent_valid_account_snapshot(state, now)
            if cached_snapshot is not None:
                cached_snapshot["live_error"] = snapshot.get("error")
                cached_snapshot["live_msg_cd"] = snapshot.get("msg_cd")
                cached_snapshot["live_msg1"] = snapshot.get("msg1")
                snapshot = cached_snapshot
        last_snapshot = snapshot
        result = compute_net_daily_return(
            state, position, hynix_price, inverse_price,
            cash=snapshot.get("cash"), positions_from_broker=snapshot.get("positions"),
            cash_fetch_ok=bool(snapshot.get("ok")), settlement_grace_active=grace,
        )
        result["account_snapshot"] = {
            "as_of": snapshot.get("as_of"), "source": snapshot.get("source"),
            "cash": snapshot.get("cash"), "holdings_market_value": snapshot.get("holdings_market_value"),
            "current_equity": snapshot.get("current_equity"), "ok": snapshot.get("ok"),
            "error": snapshot.get("error"),
            "positions": snapshot.get("positions") or [],
            "rt_cd": snapshot.get("rt_cd"), "msg_cd": snapshot.get("msg_cd"), "msg1": snapshot.get("msg1"),
            "response_field_names": snapshot.get("response_field_names"),
            "output1_field_names": snapshot.get("output1_field_names"),
            "output2_field_names": snapshot.get("output2_field_names"),
        }
        for key in ("cached_fallback", "cached_age_seconds", "live_error", "live_msg_cd", "live_msg1"):
            if snapshot.get(key) is not None:
                result["account_snapshot"][key] = snapshot.get(key)
        result["mismatch_retry_index"] = idx + 1
        history.append({
            "attempt": idx + 1,
            "blocked_reason": result.get("blocked_reason"),
            "current_equity": result.get("current_equity"),
            "net_daily_return": result.get("net_daily_return"),
            "equity_ratio_return": result.get("equity_ratio_return"),
            "snapshot": result["account_snapshot"],
        })
        last_result = result
        if result.get("blocked_reason") != ACCOUNT_EQUITY_MISMATCH:
            break
        if idx < attempts - 1:
            time.sleep(delay_seconds)
    last_result = last_result or {"blocked_reason": DAILY_RETURN_UNKNOWN}
    last_result["equity_check_history"] = history
    last_result["equity_check_attempts"] = len(history)
    last_result["settlement_grace_active"] = grace
    if last_snapshot:
        state["last_account_equity_snapshot"] = last_result.get("account_snapshot")
    return last_result


def check_real_mode_gates(state: dict, cfg=None) -> dict:
    """명세 5절 — real 실제 주문 전 필수 게이트 체크리스트(읽기 전용 진단 함수).

    이 함수는 어떤 게이트도 새로 활성화하지 않는다 — 기존 config.yaml/.env/
    trading_policy.yaml 값을 그대로 조회만 한다. Active Strategy는 여전히 mock
    전용이며, 이 체크리스트는 향후 real 연결 승인 여부를 판단하기 위한 참고용이다.
    """
    from app.config import get_config

    cfg = cfg or get_config()
    real_gate_status = (
        cfg.enhanced_real_gate_status(current_mode=state.get("mode", "mock"))
        if hasattr(cfg, "enhanced_real_gate_status")
        else {"ready": cfg.full_auto_real_confirm_ok(), "blocking_reasons": []}
    )
    checks = {
        "ui_mode_real": state.get("mode") == "real",
        "real_master_switch": cfg.real_trading_enabled(),
        "auto_trade_enabled": bool(state.get("auto_trade_on")),
        "real_auto_order_enabled": bool(real_gate_status.get("ready")),
        "no_position_conflict": not bool(state.get("position_conflict")),
    }
    all_pass = all(checks.values())
    failed = [name for name, ok in checks.items() if not ok]
    return {
        "checks": checks,
        "all_pass": all_pass,
        "failed_gates": failed,
        "real_gate_status": real_gate_status,
    }


def _run_active_strategy_entry(
    state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    now: datetime, orders_this_cycle: list, enhanced_ai_score: Optional[float] = None,
    position_manager=None,
) -> dict:
    """ACTIVE STRATEGY diagnostics for the common mock/real strategy profile.

    This path is evaluated identically in mock and real when the common toggle is
    enabled. Actual order ownership remains with the common execution engines
    below, so broker/account differences do not alter the decision surface.
    """
    final_decision = {"executable": False, "order_sent": False, "signal_source": "SHADOW_ONLY"}
    state["last_final_execution_decision"] = final_decision
    return {
        "acted": False,
        "message": "ACTIVE_STRATEGY shadow-only: actual broker orders are owned by ENHANCED_REGIME_SWITCH",
        "action": "SHADOW_ONLY",
        "decision": {},
        "final_decision": final_decision,
        "failure_reason": "SHADOW_ONLY",
    }

    from app.trading.hynix_switch_position_manager import _buy_new, _sell_all_or_ratio
    from app.trading.hynix_active_strategy_engine import (
        decide_active_strategy_action, default_active_strategy_state,
        register_position_opened, register_position_closed,
        to_final_execution_decision, generate_idempotency_key, is_duplicate_signal, register_idempotency_key,
        REASON_INSUFFICIENT_CASH, REASON_DUPLICATE_SIGNAL, REASON_STALE_PRICE, REASON_ORDER_EXCEPTION,
        REASON_RISK_LIMIT, REASON_COOLDOWN,
        ACTION_ENTER_HYNIX, ACTION_ENTER_INVERSE, ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH,
    )
    from app.trading.hynix_trading_mode import DEFAULT_MODE

    shadow = state.get("last_cycle_ai_result") or {}
    cyc = shadow.get("cycle") or {}
    prob = shadow.get("probability") or {}
    turning_point = cyc.get("turning_point") or {}
    momentum = cyc.get("momentum") or {}

    mode_name = state.get("trading_mode", DEFAULT_MODE)
    strategy_state = state.get("active_strategy_state") or default_active_strategy_state(mode_name)

    position = state.get("position") or {}
    position_state = {
        "symbol": position.get("symbol"), "quantity": position.get("quantity") or 0,
        "entry_price": position.get("entry_price"),
    }

    # expected_move_pct — 전용 예측 필드가 아직 없어 momentum의 최근 3분 속도를 근사치로
    # 사용한다(정밀한 값이 아니라 "과도한 진입/완화를 막는 최소 안전장치" 목적).
    raw_vel = momentum.get("raw_velocity_3")
    expected_move_pct = round(abs(raw_vel) * 2.0, 3) if raw_vel is not None else 0.25

    decision_result = decide_active_strategy_action(
        mode=mode_name, now=now,
        buy_probability=prob.get("buy_probability", 0.0), inverse_probability=prob.get("sell_probability", 0.0),
        hold_probability=prob.get("hold_probability", 100.0),
        model_confidence=turning_point.get("confidence", 50.0), expected_move_pct=expected_move_pct,
        down_turn_probability_3m=turning_point.get("down_turn_probability_3m"),
        up_turn_probability_3m=turning_point.get("up_turn_probability_3m"),
        momentum_inflection_or_acceleration=momentum.get("momentum_acceleration_up"),
        cycle_phase=cyc.get("cycle_phase"), order_flow_confidence=None,
        atr_pct=None, consecutive_stop_losses=strategy_state.get("consecutive_stop_losses", 0),
        recent_pnl_pct=state.get("realized_pnl_today_pct"), daily_return_pct=state.get("realized_pnl_today_pct"),
        position_state=position_state, strategy_state=strategy_state,
        data_ok=bool(hynix_price and inverse_price), position_conflict=bool(state.get("position_conflict")),
        enhanced_ai_score=enhanced_ai_score, micron_ai_score=shadow.get("effective_micron_score"),
    )
    state["active_strategy_state"] = decision_result["state"]
    action = decision_result["action"]

    # ── FinalExecutionDecision: 이번 사이클의 실행 신호를 단일 객체로 통일한다.
    # 주문 실행은 이 객체의 executable/blocking_reason만 근거로 삼는다.
    final_decision = to_final_execution_decision(decision_result, held_symbol=position_state.get("symbol"))
    state["last_final_execution_decision"] = final_decision

    acted = False
    order_id = executed_price = executed_qty = None
    failure_reason = None
    message = final_decision.get("blocking_reason") or "; ".join(final_decision.get("reasons", [])) or "HOLD"

    if not final_decision["executable"]:
        final_decision.update(order_sent=False, order_id=None, executed_price=None, executed_qty=None, failure_reason=None)
        return {
            "acted": False, "message": message, "action": action,
            "decision": decision_result, "final_decision": final_decision, "failure_reason": None,
        }

    # ── 중복 신호 방지(명세 8절): 같은 분 단위 cycle_id 안에서 동일 action+symbol 재실행 금지 ──
    cycle_id = now.strftime("%Y%m%d%H%M")
    idem_key = generate_idempotency_key(now, mode_name, cycle_id, final_decision["action"], final_decision["symbol"] or "")
    if is_duplicate_signal(state["active_strategy_state"], idem_key):
        failure_reason = REASON_DUPLICATE_SIGNAL
        message = f"중복 신호 차단(idempotency_key={idem_key})"
        final_decision.update(order_sent=False, order_id=None, executed_price=None, executed_qty=None, failure_reason=failure_reason)
        return {
            "acted": False, "message": message, "action": action,
            "decision": decision_result, "final_decision": final_decision, "failure_reason": failure_reason,
        }

    if action in (ACTION_ENTER_HYNIX, ACTION_ENTER_INVERSE):
        symbol = decision_result["recommended_symbol"]
        price = _current_price(symbol, hynix_price, inverse_price)
        pct = decision_result["recommended_position_pct"]
        if not price:
            failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 주문 미실행"
        elif pct <= 0:
            failure_reason, message = REASON_RISK_LIMIT, "권장 비중 0% — 주문 미실행"
        else:
            try:
                full_cash = float(broker.get_buyable_cash())
            except Exception as exc:
                full_cash, failure_reason, message = 0.0, REASON_ORDER_EXCEPTION, f"매수가능금액 조회 실패: {exc}"
            if full_cash > 0:
                cash_amount = full_cash * (pct / 100.0)
                if cash_amount < price:
                    failure_reason = REASON_INSUFFICIENT_CASH
                    message = f"매수가능금액 부족(필요 {price:,.0f}원, 가용 {cash_amount:,.0f}원)"
                else:
                    try:
                        buy_result = _buy_new(
                            broker, symbol, price, cash_amount, f"Active Strategy({mode_name}) 진입 {pct:.0f}%",
                            orders_this_cycle, mode="mock", signal_source="ACTIVE_ONLY",
                            position_manager=position_manager,
                        )
                    except Exception as exc:
                        buy_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
                    if buy_result.get("success"):
                        if buy_result.get("position_sync_status") == "POSITION_SYNC_PENDING":
                            failure_reason = REASON_ORDER_EXCEPTION
                            message = buy_result.get("message", "POSITION_SYNC_PENDING")
                            state["position_sync_block_new_orders"] = True
                            state["position_sync_status"] = "POSITION_SYNC_PENDING"
                            state["critical_alert"] = message
                            return {
                                "acted": True, "message": message, "action": action,
                                "decision": decision_result, "final_decision": {}, "failure_reason": failure_reason,
                            }
                        acted = True
                        qty = buy_result.get("bought_quantity", 0)
                        order_id, executed_price, executed_qty = buy_result.get("order_id"), price, qty
                        state["position"] = {
                            "symbol": symbol, "name": symbol, "quantity": qty, "avg_price": price,
                            "entry_price": price, "entry_time": now.isoformat(), "partial_tp1_done": False, "partial_sl1_done": False,
                        }
                        state["active_strategy_state"] = register_position_opened(state["active_strategy_state"], symbol, price, pct, now)
                        message = f"Active Strategy 진입: {symbol} {pct:.0f}%({qty}주)"
                    elif not failure_reason:
                        failure_reason = REASON_ORDER_EXCEPTION
                        message = buy_result.get("message", "매수 실패")

    elif action in (ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH) and position_state.get("symbol"):
        symbol = position_state["symbol"]
        price = _current_price(symbol, hynix_price, inverse_price)
        ratio = 1.0 if action in (ACTION_EXIT_ALL, ACTION_SWITCH) else max(0.01, min(1.0, decision_result["recommended_position_pct"] / 100.0))
        if not price:
            failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 매도 미실행"
        else:
            try:
                sell_result = _sell_all_or_ratio(
                    broker, position, price, ratio, f"Active Strategy({mode_name}) {action}", orders_this_cycle,
                    mode="mock", exit_reason_type="active_strategy", signal_source="ACTIVE_ONLY",
                    position_manager=position_manager,
                )
            except Exception as exc:
                sell_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
            if sell_result.get("success"):
                acted = True
                order_id, executed_price = sell_result.get("order_id"), price
                executed_qty = sell_result.get("sold_quantity")
                remaining = sell_result.get("remaining_quantity")
                if sell_result.get("position_sync_status") == "POSITION_SYNC_PENDING" or remaining is None:
                    state["position_sync_block_new_orders"] = True
                    state["position_sync_status"] = "POSITION_SYNC_PENDING"
                    state["critical_alert"] = "POSITION_SYNC_PENDING - broker balance confirmation failed after sell"
                    failure_reason = REASON_ORDER_EXCEPTION
                    message = state["critical_alert"]
                    return {
                        "acted": True, "message": message, "action": action,
                        "decision": decision_result, "final_decision": {}, "failure_reason": failure_reason,
                    }
                if remaining <= 0:
                    state["position"] = {
                        "symbol": None, "quantity": 0, "avg_price": None, "entry_price": None,
                        "entry_time": None, "name": None, "partial_tp1_done": False, "partial_sl1_done": False,
                    }
                    state["active_strategy_state"] = register_position_closed(state["active_strategy_state"], was_stop_loss=False, now=now)
                else:
                    state["position"]["quantity"] = remaining
                message = f"Active Strategy 청산/축소: {symbol} {action} (비중 {ratio*100:.0f}%)"
            elif not failure_reason:
                failure_reason = REASON_COOLDOWN if sell_result.get("blocked_by_coordinator") else REASON_ORDER_EXCEPTION
                message = sell_result.get("message", "매도 실패")

    if acted:
        state["active_strategy_state"] = register_idempotency_key(state["active_strategy_state"], idem_key)

    final_decision.update(
        order_sent=acted, order_id=order_id, executed_price=executed_price, executed_qty=executed_qty,
        failure_reason=(failure_reason if not acted else None),
    )
    state["last_final_execution_decision"] = final_decision

    return {
        "acted": acted, "message": message, "action": action,
        "decision": decision_result, "final_decision": final_decision, "failure_reason": failure_reason,
    }


def _run_adaptive_fusion_entry(
    state: dict, broker, hynix_price: Optional[float], inverse_price: Optional[float],
    now: datetime, orders_this_cycle: list, enhanced_ai_score: Optional[float] = None,
    hynix_df_1min=None, position_manager=None,
) -> dict:
    """ADAPTIVE FUSION diagnostics for the common mock/real strategy profile.

    2026-07-13 사용자 검증: 이전에는 Prediction V2가 buy_probability/sell_probability
    "값"만 _run_active_strategy_entry의 입력으로 흘러들어갔을 뿐, V2 자신의 독자 판단
    (decide_final_action_v2)이나 Cycle AI/Micron Proxy의 독자 판단은 실제 체결
    signal_source에 전혀 반영되지 않았다(항상 ACTIVE_STRATEGY_MOCK/DYNAMIC_EXIT만
    기록됨). 이 함수는 5개 모델(ACTIVE_FUSION/PREDICTION_V2/CYCLE_AI/EARLY_PREDICTION/
    MICRON_PROXY)의 독자 확률을 실제로 융합해 신규 진입을 결정하고, 그 기여도를
    ledger(active_probability/prediction_v2_probability/.../dominant_model/
    prediction_v2_weight)에 그대로 남긴다. 보유 포지션의 청산/전환 관리는 이미 검증된
    decide_active_strategy_action()의 판단을 그대로 재사용한다(이번 턴에서 선제청산
    로직까지 전부 새로 검증하기보다, 신규진입 판단에 V2를 반영하는 핵심 문제부터
    안전하게 해결하기 위함 — 확장 여지는 남겨둔다).
    """
    fusion_decision = {"executable": False, "signal_source": "SHADOW_ONLY", "weights": {}, "dominant_model": "SHADOW_ONLY"}
    final_decision = {"executable": False, "order_sent": False, "signal_source": "SHADOW_ONLY"}
    state["last_fusion_decision"] = fusion_decision
    state["last_final_execution_decision"] = final_decision
    return {
        "acted": False,
        "message": "ADAPTIVE_FUSION shadow-only: actual broker orders are owned by ENHANCED_REGIME_SWITCH",
        "action": "SHADOW_ONLY",
        "decision": {},
        "fusion_decision": fusion_decision,
        "final_decision": final_decision,
        "failure_reason": "SHADOW_ONLY",
    }

    from app.trading.hynix_switch_position_manager import _buy_new, _sell_all_or_ratio
    from app.trading.hynix_active_strategy_engine import (
        decide_active_strategy_action, default_active_strategy_state,
        register_position_opened, register_position_closed,
        generate_idempotency_key, is_duplicate_signal, register_idempotency_key,
        REASON_INSUFFICIENT_CASH, REASON_DUPLICATE_SIGNAL, REASON_STALE_PRICE, REASON_ORDER_EXCEPTION,
        REASON_RISK_LIMIT, REASON_COOLDOWN,
        ACTION_ENTER_HYNIX, ACTION_ENTER_INVERSE, ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH,
    )
    from app.trading.hynix_trading_mode import DEFAULT_MODE
    from app.trading.hynix_adaptive_fusion_engine import (
        HynixAdaptiveFusionEngine, evaluate_prediction_v2_performance,
        default_hold_tracker, update_hold_tracker, default_whipsaw_state, register_direction_flip,
        default_frequency_state, register_frequency_entry, register_frequency_round_trip_closed,
        compute_live_hynix_trend,
        MODEL_PREDICTION_V2, MODEL_STATUS_ADVISORY, MODEL_STATUS_LIVE_VALIDATED,
        ACTION_HYNIX as FUSION_HYNIX, ACTION_INVERSE as FUSION_INVERSE, ACTION_HOLD as FUSION_HOLD,
    )
    from app.services.hynix_execution_ledger import (
        SIGNAL_SOURCE_ACTIVE_ONLY, SIGNAL_SOURCE_ADAPTIVE_FUSION, SIGNAL_SOURCE_PREDICTION_V2_ASSISTED,
    )

    shadow = state.get("last_cycle_ai_result") or {}
    cyc = shadow.get("cycle") or {}
    prob = shadow.get("probability") or {}
    decision_v2 = shadow.get("decision_v2") or {"final_action_v2": "HOLD"}
    turning_point = cyc.get("turning_point") or {}
    momentum = cyc.get("momentum") or {}

    mode_name = state.get("trading_mode", DEFAULT_MODE)
    strategy_state = state.get("active_strategy_state") or default_active_strategy_state(mode_name)

    position = state.get("position") or {}
    position_state = {
        "symbol": position.get("symbol"), "quantity": position.get("quantity") or 0,
        "entry_price": position.get("entry_price"),
    }
    has_position = bool(position_state["symbol"]) and position_state["quantity"] > 0

    raw_vel = momentum.get("raw_velocity_3")
    expected_move_pct = round(abs(raw_vel) * 2.0, 3) if raw_vel is not None else 0.25
    data_ok = bool(hynix_price and inverse_price)

    # ── Model A: ACTIVE_FUSION 자체 판단(이미 검증된 로직 — 보유 포지션 관리는 이 결과를 그대로 쓴다) ──
    active_decision_result = decide_active_strategy_action(
        mode=mode_name, now=now,
        buy_probability=prob.get("buy_probability", 0.0), inverse_probability=prob.get("sell_probability", 0.0),
        hold_probability=prob.get("hold_probability", 100.0),
        model_confidence=turning_point.get("confidence", 50.0), expected_move_pct=expected_move_pct,
        down_turn_probability_3m=turning_point.get("down_turn_probability_3m"),
        up_turn_probability_3m=turning_point.get("up_turn_probability_3m"),
        momentum_inflection_or_acceleration=momentum.get("momentum_acceleration_up"),
        cycle_phase=cyc.get("cycle_phase"), order_flow_confidence=None,
        atr_pct=None, consecutive_stop_losses=strategy_state.get("consecutive_stop_losses", 0),
        recent_pnl_pct=state.get("realized_pnl_today_pct"), daily_return_pct=state.get("realized_pnl_today_pct"),
        position_state=position_state, strategy_state=strategy_state,
        data_ok=data_ok, position_conflict=bool(state.get("position_conflict")),
        enhanced_ai_score=enhanced_ai_score, micron_ai_score=shadow.get("effective_micron_score"),
    )
    state["active_strategy_state"] = active_decision_result["state"]

    # ── Model B~E 입력 준비 ──────────────────────────────────────────────────
    prediction_v2_performance = evaluate_prediction_v2_performance(now)
    micron_snapshot = state.get("last_micron_proxy_snapshot") or {}
    micron_proxy = None
    if micron_snapshot.get("effective_micron_score") is not None:
        micron_proxy = {
            "effective_micron_score": micron_snapshot.get("effective_micron_score"),
            "micron_data_confidence": micron_snapshot.get("confidence"),
            "micron_score_source": micron_snapshot.get("score_source"),
            "calculated_at": micron_snapshot.get("calculated_at"),
        }
        try:
            if micron_snapshot.get("calculated_at"):
                age = (now - datetime.fromisoformat(str(micron_snapshot.get("calculated_at")))).total_seconds() / 60.0
                if age < 0:
                    # 캔들/스냅샷 시각이 현재(KST) 기준 미래로 보임 — 시계/타임존
                    # 불일치(DATA_TIME_ERROR)이지 "매우 신선한 데이터"가 아니다.
                    # 절대 0으로 뭉개서 fresh처럼 취급하면 안 된다(2026-07-16 실측
                    # 버그: age=-538분이 is_stale=False로 통과됨).
                    micron_proxy["age_minutes"] = round(age, 2)
                    micron_proxy["is_stale"] = True
                    micron_proxy["data_time_error"] = True
                    logger.error(
                        "[HynixSwitchEngine] Micron proxy DATA_TIME_ERROR: age=%.1f분(음수) — "
                        "시계/타임존 불일치 의심, stale로 처리",
                        age,
                    )
                else:
                    micron_proxy["age_minutes"] = round(age, 2)
                    micron_proxy["is_stale"] = age > 15.0
                    micron_proxy["data_time_error"] = False
        except Exception:
            pass

    hold_tracker = state.get("adaptive_fusion_hold_tracker") or default_hold_tracker()
    whipsaw_state = state.get("adaptive_fusion_whipsaw_state") or default_whipsaw_state()
    frequency_state = state.get("adaptive_fusion_frequency_state") or default_frequency_state()
    live_hynix_trend = compute_live_hynix_trend(hynix_df_1min, now)
    prior_live = state.get("live_hynix_trend_state") or {}
    if live_hynix_trend.get("hynix_uptrend_confirmed"):
        live_hynix_trend["hynix_uptrend_streak"] = int(prior_live.get("hynix_uptrend_streak", 0) or 0) + 1
        live_hynix_trend["hynix_downtrend_streak"] = 0
    elif live_hynix_trend.get("hynix_downtrend_confirmed"):
        live_hynix_trend["hynix_downtrend_streak"] = int(prior_live.get("hynix_downtrend_streak", 0) or 0) + 1
        live_hynix_trend["hynix_uptrend_streak"] = 0
    else:
        live_hynix_trend["hynix_uptrend_streak"] = 0
        live_hynix_trend["hynix_downtrend_streak"] = 0
    state["live_hynix_trend_state"] = {
        "hynix_uptrend_streak": live_hynix_trend.get("hynix_uptrend_streak", 0),
        "hynix_downtrend_streak": live_hynix_trend.get("hynix_downtrend_streak", 0),
        "last_direction": live_hynix_trend.get("direction"),
        "as_of": live_hynix_trend.get("as_of"),
    }
    state["last_live_hynix_trend"] = live_hynix_trend

    engine = HynixAdaptiveFusionEngine()
    fusion_decision = engine.decide(
        now=now, active_decision_result=active_decision_result,
        prediction_v2_probability=prob, prediction_v2_decision=decision_v2,
        prediction_v2_performance=prediction_v2_performance, cycle_result=cyc,
        cycle_ai_validated=bool(state.get("cycle_ai_validated", False)), micron_proxy=micron_proxy,
        held_symbol=position_state["symbol"], position_conflict=bool(state.get("position_conflict")),
        data_ok=data_ok, price_is_stale=not data_ok,
        daily_return_pct=state.get("realized_pnl_today_pct"), orders_today_count=state.get("daily_trade_count", 0),
        hold_tracker=hold_tracker, whipsaw_state=whipsaw_state, frequency_state=frequency_state,
        consecutive_stop_losses=strategy_state.get("consecutive_stop_losses", 0),
        live_hynix_trend=live_hynix_trend,
    )

    fused_action = fusion_decision["final_action"]
    hold_tracker = update_hold_tracker(hold_tracker, has_position, fused_action, now)
    state["adaptive_fusion_hold_tracker"] = hold_tracker
    last_direction = state.get("adaptive_fusion_last_direction")
    if fused_action in (FUSION_HYNIX, FUSION_INVERSE):
        if last_direction and last_direction != fused_action:
            whipsaw_state = register_direction_flip(whipsaw_state, now)
        state["adaptive_fusion_last_direction"] = fused_action
    state["adaptive_fusion_whipsaw_state"] = whipsaw_state
    state["last_fusion_decision"] = fusion_decision

    # ── signal_source: Prediction V2가 실제로 (SHADOW를 벗어나) 기여했을 때만 그렇다고 표기한다 ──
    pv2_weight = fusion_decision["weights"].get(MODEL_PREDICTION_V2, 0.0)
    pv2_status = prediction_v2_performance.get("model_status")
    pv2_applied = pv2_status in (MODEL_STATUS_ADVISORY, MODEL_STATUS_LIVE_VALIDATED) and pv2_weight > 0
    if not pv2_applied:
        signal_source = SIGNAL_SOURCE_ACTIVE_ONLY
    elif fusion_decision["dominant_model"] == MODEL_PREDICTION_V2:
        signal_source = SIGNAL_SOURCE_PREDICTION_V2_ASSISTED
    else:
        signal_source = SIGNAL_SOURCE_ADAPTIVE_FUSION

    fusion_metadata = {
        "active_probability": fusion_decision["fused_hynix_probability"],  # 참고용(대표값) — 상세는 아래 개별 확률 사용
        "prediction_v2_probability": prob.get("buy_probability"),
        "cycle_probability": turning_point.get("up_turn_probability_3m"),
        "fused_probability": max(fusion_decision["fused_hynix_probability"], fusion_decision["fused_inverse_probability"]),
        "prediction_v2_weight": pv2_weight if pv2_applied else 0.0,
        "dominant_model": fusion_decision["dominant_model"],
        "model_agreement": fusion_decision["model_agreement"],
        "expected_value": fusion_decision["expected_value"],
        "target_position_pct": fusion_decision["target_position_pct"],
    }

    action = None
    acted = False
    order_id = executed_price = executed_qty = None
    failure_reason = None
    message = fusion_decision.get("blocking_reason") or "; ".join(fusion_decision.get("reasons", [])[:1]) or "HOLD"

    if has_position:
        # 보유 포지션 관리(청산/전환/Scale)는 이미 검증된 ACTIVE_FUSION 판단을 그대로 쓴다.
        action = active_decision_result["action"]
        if action in (ACTION_SCALE_OUT_PARTIAL, ACTION_EXIT_ALL, ACTION_SWITCH):
            symbol = position_state["symbol"]
            price = _current_price(symbol, hynix_price, inverse_price)
            ratio = 1.0 if action in (ACTION_EXIT_ALL, ACTION_SWITCH) else max(0.01, min(1.0, active_decision_result["recommended_position_pct"] / 100.0))
            if not price:
                failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 매도 미실행"
            else:
                try:
                    sell_result = _sell_all_or_ratio(
                        broker, position, price, ratio, f"Adaptive Fusion({mode_name}) {action}", orders_this_cycle,
                        mode="mock", exit_reason_type="active_strategy", signal_source=signal_source,
                        fusion_metadata=fusion_metadata, position_manager=position_manager,
                    )
                except Exception as exc:
                    sell_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
                if sell_result.get("success"):
                    acted = True
                    order_id, executed_price = sell_result.get("order_id"), price
                    executed_qty = sell_result.get("sold_quantity")
                    remaining = sell_result.get("remaining_quantity")
                    if sell_result.get("position_sync_status") == "POSITION_SYNC_PENDING" or remaining is None:
                        state["position_sync_block_new_orders"] = True
                        state["position_sync_status"] = "POSITION_SYNC_PENDING"
                        state["critical_alert"] = "POSITION_SYNC_PENDING - broker balance confirmation failed after sell"
                        failure_reason = REASON_ORDER_EXCEPTION
                        message = state["critical_alert"]
                        return {
                            "acted": True, "message": message, "action": action,
                            "decision": fusion_decision, "final_decision": {}, "failure_reason": failure_reason,
                        }
                    if remaining <= 0:
                        state["position"] = {
                            "symbol": None, "quantity": 0, "avg_price": None, "entry_price": None,
                            "entry_time": None, "name": None, "partial_tp1_done": False, "partial_sl1_done": False,
                        }
                        state["active_strategy_state"] = register_position_closed(state["active_strategy_state"], was_stop_loss=False, now=now)
                        state["adaptive_fusion_frequency_state"] = register_frequency_round_trip_closed(frequency_state, now)
                    else:
                        state["position"]["quantity"] = remaining
                    message = f"Adaptive Fusion 청산/축소: {symbol} {action} (비중 {ratio*100:.0f}%)"
                elif not failure_reason:
                    failure_reason = REASON_COOLDOWN if sell_result.get("blocked_by_coordinator") else REASON_ORDER_EXCEPTION
                    message = sell_result.get("message", "매도 실패")
    elif fusion_decision["executable"] and fused_action in (FUSION_HYNIX, FUSION_INVERSE) and fusion_decision.get("symbol"):
        symbol = fusion_decision["symbol"]
        pct = fusion_decision["target_position_pct"]
        price = _current_price(symbol, hynix_price, inverse_price)
        cycle_id = now.strftime("%Y%m%d%H%M")
        idem_key = generate_idempotency_key(now, mode_name, cycle_id, fused_action, symbol)
        if is_duplicate_signal(state["active_strategy_state"], idem_key):
            failure_reason, message = REASON_DUPLICATE_SIGNAL, f"중복 신호 차단(idempotency_key={idem_key})"
        elif not price:
            failure_reason, message = REASON_STALE_PRICE, f"{symbol} 현재가 없음 — 주문 미실행"
        elif pct <= 0:
            failure_reason, message = REASON_RISK_LIMIT, "권장 비중 0% — 주문 미실행"
        else:
            try:
                full_cash = float(broker.get_buyable_cash())
            except Exception as exc:
                full_cash, failure_reason, message = 0.0, REASON_ORDER_EXCEPTION, f"매수가능금액 조회 실패: {exc}"
            if full_cash > 0:
                cash_amount = full_cash * (pct / 100.0)
                if cash_amount < (price or 0):
                    failure_reason = REASON_INSUFFICIENT_CASH
                    message = f"매수가능금액 부족(필요 {price:,.0f}원, 가용 {cash_amount:,.0f}원)"
                else:
                    try:
                        buy_result = _buy_new(
                            broker, symbol, price, cash_amount, f"Adaptive Fusion({mode_name}) 진입 {pct:.0f}%",
                            orders_this_cycle, mode="mock", signal_source=signal_source, fusion_metadata=fusion_metadata,
                            position_manager=position_manager,
                        )
                    except Exception as exc:
                        buy_result, failure_reason = {"success": False, "message": str(exc)}, REASON_ORDER_EXCEPTION
                    if buy_result.get("success"):
                        if buy_result.get("position_sync_status") == "POSITION_SYNC_PENDING":
                            failure_reason = REASON_ORDER_EXCEPTION
                            message = buy_result.get("message", "POSITION_SYNC_PENDING")
                            state["position_sync_block_new_orders"] = True
                            state["position_sync_status"] = "POSITION_SYNC_PENDING"
                            state["critical_alert"] = message
                            return {
                                "acted": True, "message": message, "action": fused_action,
                                "decision": fusion_decision, "final_decision": {}, "failure_reason": failure_reason,
                            }
                        acted = True
                        action = ACTION_ENTER_HYNIX if symbol == "000660" else ACTION_ENTER_INVERSE
                        qty = buy_result.get("bought_quantity", 0)
                        order_id, executed_price, executed_qty = buy_result.get("order_id"), price, qty
                        state["position"] = {
                            "symbol": symbol, "name": symbol, "quantity": qty, "avg_price": price,
                            "entry_price": price, "entry_time": now.isoformat(), "partial_tp1_done": False, "partial_sl1_done": False,
                            "entry_type": fusion_decision.get("entry_type") or "NORMAL",
                        }
                        state["active_strategy_state"] = register_position_opened(state["active_strategy_state"], symbol, price, pct, now)
                        state["active_strategy_state"] = register_idempotency_key(state["active_strategy_state"], idem_key)
                        state["adaptive_fusion_frequency_state"] = register_frequency_entry(frequency_state, fused_action, now)
                        message = f"Adaptive Fusion 진입: {symbol} {pct:.0f}%({qty}주, {fusion_decision.get('entry_type') or 'NORMAL'}) — {signal_source}"
                    elif not failure_reason:
                        failure_reason = REASON_ORDER_EXCEPTION
                        message = buy_result.get("message", "매수 실패")

    from app.trading.hynix_active_strategy_engine import build_final_execution_decision
    final_decision = build_final_execution_decision(
        action=(fused_action if not has_position else (active_decision_result.get("action") or "HOLD")),
        symbol=fusion_decision.get("symbol") or position_state.get("symbol"),
        target_position_pct=fusion_decision.get("target_position_pct", 0.0),
        confidence=fusion_decision.get("fused_confidence", 50.0), signal_source=signal_source,
        reasons=fusion_decision.get("reasons", []), executable=bool(acted),
        blocking_reason=(failure_reason if not acted else None),
    )
    final_decision.update(
        order_sent=acted, order_id=order_id, executed_price=executed_price, executed_qty=executed_qty,
        failure_reason=(failure_reason if not acted else None),
    )
    state["last_final_execution_decision"] = final_decision

    return {
        "acted": acted, "message": message, "action": action,
        "decision": fusion_decision, "final_decision": final_decision, "failure_reason": failure_reason,
    }


def _run_shadow_cycle_ai_and_decision_v2(
    state: dict, enhanced_result: dict, decision: dict, df_1min, hynix_price, inverse_price, now: datetime,
) -> Optional[dict]:
    """SHADOW MODE — Cycle Detector AI + Prediction AI V2(BUY/SELL/HOLD 확률+Adaptive
    Threshold)를 기존 enhanced_score 기반 실제 주문 흐름과 나란히 계산·기록만 한다.

    이 함수의 결과는 어떤 이유로도 `decision`/실제 주문 실행에 영향을 주지 않는다 —
    명세(Cycle Detector 17절)가 요구하는 최소 5거래일 Shadow Mode 검증을 위한 것이며,
    실제 주문 연결은 별도의 명시적 승인 이후에만 이루어진다. 실패해도 예외를 삼키고
    None을 반환한다(호출부 로직에 영향 없음).
    """
    try:
        from app.trading.hynix_cycle_detector import (
            HynixCycleDetector, default_cycle_state, log_cycle_ai_prediction,
        )
        from app.models.hynix_decision_v2 import (
            compute_buy_sell_hold_probability, decide_final_action_v2, adaptive_threshold_update,
            default_threshold_state,
        )
        from app.models.micron_proxy_prediction import compute_effective_micron_score_from_market_data

        market_data = enhanced_result.get("market_data") or {}
        micron_proxy = compute_effective_micron_score_from_market_data(market_data, now=now)
        effective_micron_score = micron_proxy.get("effective_micron_score")
        korea_score = micron_proxy.get("korea_semiconductor_confirmation_score")

        # 매 사이클 Micron Proxy 결과를 state에 스냅샷으로 저장한다 — 이번 사이클에서
        # 원본 데이터가 없어 계산이 실패/생략되더라도 이 필드는 마지막 성공 값을 그대로
        # 유지하므로, UI는 "데이터 없음"으로 빈 화면을 보이는 대신 마지막 성공 계산
        # 결과 + 경과시간을 표시할 수 있다.
        state["last_micron_proxy_snapshot"] = {
            "real_micron_score": micron_proxy.get("real_micron_score"),
            "synthetic_micron_score": micron_proxy.get("synthetic_micron_score"),
            "effective_micron_score": effective_micron_score,
            "score_source": micron_proxy.get("micron_score_source"),
            "confidence": micron_proxy.get("micron_data_confidence"),
            "calculated_at": now.isoformat(timespec="seconds"),
        }

        gap_pct = None
        session_high = session_low = None
        prior_close = enhanced_result.get("hynix_prev_close")
        if df_1min is not None and not df_1min.empty:
            session_high = float(df_1min["high"].max())
            session_low = float(df_1min["low"].min())
            if prior_close:
                gap_pct = (float(df_1min["open"].iloc[0]) / prior_close - 1.0) * 100.0

        position = state.get("position") or {}
        position_state = {
            "symbol": position.get("symbol"),
            "position_pct": 100.0 if (position.get("quantity") or 0) > 0 else 0.0,
        }

        cycle_state = state.get("cycle_ai_state") or default_cycle_state()
        cycle_result = HynixCycleDetector().run(
            df_1min, now, position_state=position_state, state=cycle_state,
            gap_pct=gap_pct, session_high=session_high, session_low=session_low, prior_close=prior_close,
            inverse_pressure_score=decision.get("inverse_pressure_score"),
            korea_semiconductor_confirmation_score=korea_score, effective_micron_score=effective_micron_score,
        )
        state["cycle_ai_state"] = cycle_result["state"]

        threshold_state = state.get("decision_v2_threshold_state") or default_threshold_state()
        probability = compute_buy_sell_hold_probability(
            cycle_result["turning_point"], enhanced_score=decision.get("enhanced_score"),
            effective_micron_score=effective_micron_score,
        )
        decision_v2 = decide_final_action_v2(probability, threshold_state)
        state["decision_v2_threshold_state"] = adaptive_threshold_update(
            threshold_state, decision_v2["final_action_v2"], now,
        )

        # 우선순위 스택(Cycle Phase → Turning Point → Momentum → Entry Timing → Enhanced →
        # Effective Micron): Cycle Detector가 HOLD가 아닌 액션을 냈으면 그것을 combined 액션으로,
        # 아니면 확률 기반 decision_v2의 액션을 combined 액션으로 삼는다(로그/UI 비교용일 뿐,
        # 실제 주문에는 반영되지 않음).
        combined_action = cycle_result["action"] if cycle_result["action"] != "HOLD" else decision_v2["final_action_v2"]

        shadow_result = {
            "cycle": cycle_result, "probability": probability, "decision_v2": decision_v2,
            "combined_shadow_action": combined_action, "effective_micron_score": effective_micron_score,
            "korea_semiconductor_confirmation_score": korea_score,
        }
        state["last_cycle_ai_result"] = shadow_result

        reasons = (cycle_result.get("reasons") or [])[:3]
        log_cycle_ai_prediction({
            "timestamp": now.isoformat(timespec="seconds"), "hynix_price": hynix_price, "inverse_price": inverse_price,
            "cycle_phase": cycle_result.get("cycle_phase"), "previous_cycle_phase": cycle_result.get("previous_cycle_phase"),
            "phase_duration_seconds": cycle_result.get("phase_duration_seconds"),
            "momentum_velocity": (cycle_result.get("momentum") or {}).get("raw_velocity_3"),
            "momentum_acceleration_up": (cycle_result.get("momentum") or {}).get("momentum_acceleration_up"),
            "momentum_acceleration_down": (cycle_result.get("momentum") or {}).get("momentum_acceleration_down"),
            "early_reversal_score": (cycle_result.get("state", {}) or {}).get("early_reversal_score"),
            "up_turn_3m": (cycle_result.get("turning_point") or {}).get("up_turn_probability_3m"),
            "up_turn_5m": (cycle_result.get("turning_point") or {}).get("up_turn_probability_5m"),
            "up_turn_10m": (cycle_result.get("turning_point") or {}).get("up_turn_probability_10m"),
            "down_turn_3m": (cycle_result.get("turning_point") or {}).get("down_turn_probability_3m"),
            "down_turn_5m": (cycle_result.get("turning_point") or {}).get("down_turn_probability_5m"),
            "down_turn_10m": (cycle_result.get("turning_point") or {}).get("down_turn_probability_10m"),
            "cycle_confidence": cycle_result.get("cycle_confidence"),
            "cycle_entry_score": max((cycle_result.get("entry_scores") or {}).values(), default=None),
            "enhanced_score": decision.get("enhanced_score"), "effective_micron_score": effective_micron_score,
            "recommended_symbol": cycle_result.get("recommended_symbol"),
            "recommended_position_pct": cycle_result.get("recommended_position_pct"),
            "final_action": combined_action, "order_sent": False, "order_executed": False,
            "reason_top1": reasons[0] if len(reasons) > 0 else "",
            "reason_top2": reasons[1] if len(reasons) > 1 else "",
            "reason_top3": reasons[2] if len(reasons) > 2 else "",
        })
        return shadow_result
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] Shadow Cycle AI/Decision V2 계산 실패(무해, 실거래에 영향 없음): %s", exc)
        return None


def update_hynix_auto_trade_loop(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """1회 실행 사이클의 공개 진입점 — mode별 state 락으로 감싼 얇은 wrapper.

    백그라운드 3분 사이클 스레드, Dynamic Exit Watcher(1초 주기), Streamlit 수동 실행이
    모두 같은 mode의 state를 동시에 load→수정→save할 수 있어(lost update 위험 — 실제로
    2026-07-10 부분손절 손익 1건이 이렇게 누락된 사고가 있었다), 이 함수 호출 전체를
    mode별 락(app.services.hynix_switch_state.with_state_lock)으로 직렬화한다.
    """
    from app.services.hynix_switch_state import with_state_lock

    resolved_mode = mode
    if resolved_mode is None:
        resolved_mode = load_state(mode=None).get("mode", "mock")
    with with_state_lock(resolved_mode):
        return _update_hynix_auto_trade_loop_locked(mode=mode, now=now)


def compute_eod_regime_only(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """요구사항(2026-07-16) — 장 마감 후(is_within_operating_window()==False)에도
    오늘 저장된(또는 방금 조회 가능하면 최신) 1분봉으로 EOD(End-Of-Day) regime
    분석을 수행해 오늘 최종 장세를 표시한다. 이 함수는 시세 조회 + 장세분류 +
    state 저장만 한다 — run_switch_or_entry/run_tp_sl_if_needed/
    run_liquidation_if_needed 등 주문 실행 함수는 절대 호출하지 않는다.
    """
    from app.services.hynix_switch_state import with_state_lock
    from app.trading.adaptive_market_regime import compute_and_confirm_regime
    from app.data_sources.auto_market_collector import collect_hynix_minute, collect_hynix_daily

    now = now or kst_now()
    resolved_mode = mode or load_state(mode=None).get("mode", "mock")
    with with_state_lock(resolved_mode):
        state = load_state(mode=resolved_mode)
        try:
            minute_result = collect_hynix_minute(mode=resolved_mode)
            daily_result = collect_hynix_daily(mode=resolved_mode)
            eod_result = compute_and_confirm_regime(
                minute_result.get("df_1min"), prev_close=daily_result.get("prev_close"),
                confirmation_state=state.get("adaptive_regime_eod_confirmation"), now=now,
            )
            state["adaptive_regime_eod_confirmation"] = eod_result["confirmation_state"]
            state["adaptive_regime_eod"] = {k: v for k, v in eod_result.items() if k != "confirmation_state"}
            save_state_atomic(state)
            return state["adaptive_regime_eod"]
        except Exception as exc:
            logger.error("[HynixSwitchEngine] EOD regime 분석 실패: %s", exc)
            return {"error": str(exc)}


def _early_signal_symbol_agreement(etd_state: dict, fast_signal: dict, hynix_price: Optional[float], inverse_price: Optional[float]) -> Optional[bool]:
    """요구사항1 — "000660과 실제 ETF 방향 일치도". fast_signal 자체는 000660(SIGNAL_
    SYMBOL) 1분봉 기준이므로, 실제 거래종목(0193T0/0197X0) 현재가가 직전 틱 대비
    같은 방향으로 움직였는지를 가벼운 대리지표로 비교한다(별도 분봉 캐시를 새로
    수집하지 않는다). 비교 대상이 없는 최초 틱은 None(불확실 — 감점하지 않음)."""
    direction = fast_signal.get("direction")
    if direction not in ("UP", "DOWN"):
        return None
    prev = etd_state.get("prev_etf_prices") or {}
    prev_hynix, prev_inverse = prev.get(HYNIX_SYMBOL), prev.get(INVERSE_SYMBOL)
    etd_state["prev_etf_prices"] = {HYNIX_SYMBOL: hynix_price, INVERSE_SYMBOL: inverse_price}
    if direction == "UP":
        return (hynix_price >= prev_hynix) if (prev_hynix and hynix_price) else None
    return (inverse_price >= prev_inverse) if (prev_inverse and inverse_price) else None


def _load_etf_own_minute_cache(symbol: str):
    """desired_symbol(0193T0/0197X0) 자신의 1분봉을 로컬 캐시에서만 읽는다(새
    KIS 호출 없음) — 5초 주기 Early Detector 피드가 VWAP/구조돌파/거래량 급증을
    판단할 때 매 틱 새로 분봉 API를 부르지 않기 위함(요구사항1, KIS 호출량 안전)."""
    try:
        if symbol == HYNIX_SYMBOL:
            from app.data_sources.hynix_long_collector import _load_long_minute_cache

            return _load_long_minute_cache()
        if symbol == INVERSE_SYMBOL:
            from app.data_sources.hynix_inverse_collector import _load_inverse_minute_cache

            return _load_inverse_minute_cache()
    except Exception:
        return None
    return None


def _quote_timestamp(quote: dict, fallback: datetime) -> Optional[datetime]:
    if not isinstance(quote, dict):
        return None
    for key in ("timestamp", "collected_at", "updated_at", "as_of", "last_update_time"):
        raw = quote.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
    if quote.get("stale") or quote.get("status") == "stale_cache" or quote.get("source") == "cache":
        return None
    return fallback


def _data_time_mismatch_status(quotes: dict[str, dict], fetched_at: dict[str, datetime], *, now: datetime) -> dict:
    entries = {}
    timestamps = []
    for symbol, quote in quotes.items():
        ts = _quote_timestamp(quote, fetched_at.get(symbol, now))
        age = (now - ts).total_seconds() if ts is not None else None
        entries[symbol] = {
            "timestamp": ts.isoformat() if ts is not None else None,
            "age_seconds": round(age, 3) if age is not None else None,
            "source": (quote or {}).get("source"),
            "status": (quote or {}).get("status"),
            "stale": bool((quote or {}).get("stale")),
        }
        if ts is not None:
            timestamps.append(ts)
    max_delta = None
    if len(timestamps) == len(quotes) and timestamps:
        max_delta = (max(timestamps) - min(timestamps)).total_seconds()
    blocked = len(timestamps) != len(quotes) or (max_delta is not None and max_delta > 5.0)
    return {
        "blocked": blocked,
        "reason_code": "DATA_TIME_MISMATCH" if blocked else None,
        "max_delta_seconds": round(max_delta, 3) if max_delta is not None else None,
        "symbols": entries,
    }


def _early_reason_code(reason: Optional[str]) -> str:
    text = str(reason or "")
    upper = text.upper()
    if "SIGNAL_EXPIRED" in upper:
        return "SIGNAL_EXPIRED"
    if "DATA_TIME_MISMATCH" in upper:
        return "DATA_TIME_MISMATCH"
    if "MICRO_CHOP" in upper:
        return "MICRO_CHOP"
    if "COOLDOWN" in upper:
        return "REENTRY_COOLDOWN"
    if "BUYABLE_CASH" in upper or "ORDER_QTY_ZERO" in upper or "PRICE_UNAVAILABLE" in upper:
        return "ETF_DATA_INSUFFICIENT"
    if "CHASE_BLOCK" in upper:
        return "CHASE_BLOCK"
    if "COST_EDGE" in upper:
        return "COST_EDGE_BLOCK"
    if "TARGET_ALREADY_FILLED" in upper:
        return "TARGET_ALREADY_FILLED"
    if "시간" in text or "09:" in text:
        return "TIME_GATE_BLOCK"
    if "CHASE_BLOCK" in text or "추격" in text or "고점" in text or "저점" in text:
        return "CHASE_BLOCK"
    if "거래비용" in text or "예상순이익" in text:
        return "COST_EDGE_BLOCK"
    if "쿨다운" in text or "재진입" in text:
        return "REENTRY_COOLDOWN"
    if "현재가" in text or "ETF" in text and "실패" in text:
        return "ETF_DATA_INSUFFICIENT"
    if "방향" in text and "불일치" in text:
        return "ETF_DIRECTION_MISMATCH"
    if "이미 목표" in text or "추가 진입이 필요" in text or "단계 유지" in text:
        return "TARGET_ALREADY_FILLED"
    return "NO_EARLY_SIGNAL"


def _augment_fast_signal_with_enhanced_approval(fast_signal: dict, final_action: str, decision: dict) -> dict:
    """요구사항(2026-07-21 방향편향 수정) — Enhanced(raw_score_leader/final_action)는
    더 이상 Early Detector의 실제 진입 방향(actionable_direction)을 덮어쓰지 않는다.

    과거에는 여기서 final_action을 근거로 fast_signal의 direction/up_votes/down_votes/
    returns를 강제로 갈아끼웠다 — 이는 "raw_score_leader는 참고 표시로만 사용하고
    실제 주문 방향은 actionable_direction만 사용한다"는 원칙을 어기고, 원점수(raw
    score)가 실시간 방향(actionable_direction)을 역전시킬 수 있는 방향편향의 원인이었다.

    이제는 final_action을 참고용 표시 필드로만 첨부하고, fast_signal 본체(이번 틱에
    실제 계산된 5/10/20/30초 기울기·투표 기반 direction/up_votes/down_votes/returns)는
    전혀 건드리지 않는다 — 실제 진입 방향은 항상 Early Detector가 이번 틱에 직접
    계산한 live fast_signal.direction(actionable_direction)만 따른다.
    """
    result = dict(fast_signal or {})
    result["raw_score_leader_final_action"] = final_action
    result["raw_score_leader_reference_only"] = True
    return result


def _record_early_result_on_trace(trace: dict, early_result: Optional[dict]) -> None:
    if early_result is None:
        trace["early_decision"] = {
            "attempted": False, "skipped": True, "reason_code": "NO_EARLY_SIGNAL",
            "reason": "Early Detector did not run for this cycle",
        }
        trace["early_order_result"] = None
        return

    switch = early_result.get("switch") or {}
    orders = [o for o in (switch.get("orders") or []) if isinstance(o, dict)]
    sent_orders = [o for o in orders if o.get("action") in ("BUY", "SELL")]
    succeeded_orders = [o for o in sent_orders if o.get("success")]
    reason_code = early_result.get("reason_code") or _early_reason_code(early_result.get("reason") or switch.get("message"))
    trace["early_decision"] = {
        "attempted": True,
        "skipped": bool(early_result.get("skipped")),
        "reason_code": reason_code,
        "reason": early_result.get("reason") or switch.get("message"),
        "stage": early_result.get("stage"),
        "target_pct": early_result.get("target_pct") or early_result.get("expanded_to") or early_result.get("staged_to"),
        "signal_direction": (early_result.get("signal") or {}).get("direction"),
        "signal_score": (early_result.get("signal") or {}).get("score"),
    }
    trace["early_order_result"] = {
        "order_sent": bool(sent_orders),
        "broker_executed": bool(succeeded_orders),
        "execution_stage": switch.get("stage"),
        "execution_message": switch.get("message"),
        "order_failure_code": switch.get("failure_code"),
        "broker_error": switch.get("broker_error"),
        "requested_symbol": switch.get("requested_symbol"),
        "requested_qty": switch.get("requested_qty"),
        "order_price": switch.get("order_price"),
        "buyable_cash": switch.get("buyable_cash"),
        "sized_cash": switch.get("sized_cash"),
        "orders": orders,
    }


def _run_early_trend_detector_tick(
    *, state: dict, mode: str, now: datetime, fast_signal: dict, df_1min,
    confirmed_regime: Optional[str], broker, position_manager,
    hynix_price: Optional[float], inverse_price: Optional[float],
    live_slopes: Optional[dict] = None,
) -> Optional[dict]:
    """Early Trend Detector — Adaptive Market Regime 하위의 제한적 탐색진입 엔진.

    최종 주문권한/최대비중은 항상 Adaptive Regime(confirmed_regime)이 결정한다 —
    이 함수는 (a) 무포지션일 때 최초 탐색진입(10~15%→30%→50%)만 시작하거나,
    (b) 이미 이 엔진이 만든 EARLY_PROBE 포지션을 들고 있을 때 단계 진행 또는
    STRONG_UP/DOWN 확정 후 확대(50%)만 판단한다. 조기진입의 철수(고정 -0.4%
    또는 VOLATILE_RANGE TP1/TP2/SL/신호약화/30초 미확인)는 1초 주기 Dynamic
    Exit Watcher가 담당한다(이 함수는 5초 주기 피드에서 호출돼도 철수는 항상
    가장 빠른 워처가 전담한다).

    live_slopes: app.trading.early_trend_live_feed로 5초 주기로 쌓은
    {symbol: {"direction": ..., "slopes": {...}}} — 1분봉 vote보다 먼저 반전을
    잡기 위한 입력(요구사항1).

    반환값이 None이면 "이 틱에서 관여하지 않음"을 뜻하며, 호출부는 기존 레거시
    로직(PRIMARY_TREND 기반 Fast Watcher)을 그대로 이어서 실행해야 한다.
    """
    from app.trading import early_trend_detector as etd
    from app.trading.etf_entry_confirmation import (
        compute_etf_breakouts, compute_etf_volume_surge, classify_etf_direction_confirmation,
        resolve_window_directions, has_any_slope_data,
        ETF_CONFIRM_UP, ETF_CONFIRM_DOWN, ALIGNED_PULLBACK as ETF_ALIGNED_PULLBACK,
        ETF_CONFIRMATION_PENDING,
    )

    live_slopes = live_slopes or {}
    position = state.get("position") or {}
    held_symbol = position.get("symbol")
    today = now.strftime("%Y%m%d")
    etd_state = dict(state.get("early_trend_detector") or {})
    freq = etd.reset_frequency_state_if_new_day(etd_state.get("frequency"), today)
    etd_state["frequency"] = freq
    live = bool(state.get("early_trend_detector_live"))
    etd_state["engine_mode"] = "LIVE" if live else "SHADOW"
    etd_state["order_worker_name"] = etd_state.get("order_worker_name") or "MAIN_CYCLE"

    if held_symbol and position.get("entry_type") != etd.ENTRY_TYPE_EARLY_PROBE:
        # 이미 이 엔진이 아닌 다른 경로로 만들어진 일반 포지션을 보유 중이다 —
        # Early Detector는 관여하지 않고 기존 로직에 맡긴다.
        state["early_trend_detector"] = etd_state
        return None

    if held_symbol is None:
        # ── 최초 탐색진입 판단 ──────────────────────────────────────────────
        halted, remaining = etd.is_halted(freq, now)
        if halted:
            etd_state["last_block_reason"] = f"가짜신호 서킷브레이커 — {remaining / 60.0:.0f}분 후 재개"
            etd_state["candidate"] = {}
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "REENTRY_COOLDOWN"}

        data_time_status = etd_state.get("data_time_status") or {}
        if data_time_status.get("blocked"):
            etd_state["last_block_reason"] = "DATA_TIME_MISMATCH"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "DATA_TIME_MISMATCH"}

        signal_symbol_agreement = _early_signal_symbol_agreement(etd_state, fast_signal, hynix_price, inverse_price)

        # 요구사항1 — live_slopes는 종목별(HYNIX_SYMBOL/INVERSE_SYMBOL) 자기 자신의
        # 가격 기울기다. INVERSE_SYMBOL은 기초자산과 반대로 움직이므로, "기초자산
        # 방향" 기준 단일 판단으로 정규화한다(레버리지 상승=기초자산 UP, 인버스
        # 상승=기초자산 DOWN). 두 심볼의 함의가 서로 다르면(둘 다 있는데 불일치)
        # 아직 불확실로 본다 — 한쪽만 있으면 그 값을 그대로 쓴다.
        _prev_signal_direction = (etd_state.get("last_signal") or {}).get("direction")
        _prev_signal_score = (etd_state.get("last_signal") or {}).get("score")
        _live_hynix_dir = (live_slopes.get(HYNIX_SYMBOL) or {}).get("direction")
        _live_inverse_dir = (live_slopes.get(INVERSE_SYMBOL) or {}).get("direction")
        _live_inverse_implied = {"UP": "DOWN", "DOWN": "UP"}.get(_live_inverse_dir)
        _live_direction_candidates = [d for d in (_live_hynix_dir, _live_inverse_implied) if d]
        _live_direction_for_data = (
            _live_direction_candidates[0]
            if _live_direction_candidates and len(set(_live_direction_candidates)) == 1
            else None
        )

        # 두 후보 방향(UP=HYNIX_SYMBOL, DOWN=INVERSE_SYMBOL) 중 지금 판단 대상이
        # 되는 쪽의 ETF 자체 VWAP 이탈/1분봉 구조돌파/거래량 급증을 미리 계산한다
        # — 1분봉 vote가 아직 확정하지 못했어도 실시간 기울기가 먼저 확정되면
        # 그 방향으로 조기신호를 만들 수 있게 한다(요구사항1).
        _vote_direction = fast_signal.get("direction") if fast_signal.get("direction") in ("UP", "DOWN") else None
        _candidate_direction_for_data = _vote_direction or _live_direction_for_data or _prev_signal_direction
        _probe_symbol = HYNIX_SYMBOL if _candidate_direction_for_data == "UP" else INVERSE_SYMBOL if _candidate_direction_for_data == "DOWN" else None
        _etf_df = _load_etf_own_minute_cache(_probe_symbol) if _probe_symbol else None
        if _etf_df is not None and "datetime" in _etf_df.columns:
            try:
                _etf_df = _etf_df[_etf_df["datetime"] <= now].copy()
                if _etf_df.empty:
                    _etf_df = None
            except Exception:
                pass
        _etf_price_for_data = hynix_price if _probe_symbol == HYNIX_SYMBOL else inverse_price if _probe_symbol == INVERSE_SYMBOL else None
        _breakouts = compute_etf_breakouts(_etf_df, _etf_price_for_data, _candidate_direction_for_data) if _probe_symbol else {}
        _volume_surge = compute_etf_volume_surge(_etf_df) if _etf_df is not None else None
        _micro_chop = etd.update_micro_chop_state(
            etd_state.get("micro_chop"),
            direction=_live_direction_for_data,
            vwap_crossed=bool(_breakouts.get("vwap_breakout")),
            reversal_exit=False,
            move_efficiency=None,
            now=now,
        )
        etd_state["micro_chop"] = _micro_chop

        early_signal = etd.compute_composite_early_signal(
            fast_signal=fast_signal, signal_symbol_agreement=signal_symbol_agreement,
            live_direction=_live_direction_for_data, etf_vwap_breakout=_breakouts.get("vwap_breakout"),
            etf_structure_breakout=_breakouts.get("structure_breakout"), etf_volume_surge=_volume_surge,
        )
        direction = early_signal.get("direction")
        etd_state["last_signal"] = early_signal

        # 요구사항4 — 이전 후보 방향과 반대로 확정되면(1분봉 vote 또는 실시간
        # 기울기 어느 쪽이든) 즉시 기존 방향 점수를 70% 감쇠하고 그 방향 재진입을
        # 지금부터 다시 쿨다운 처리한다 — 확인 스트릭은 아래에서 candidate가 새로
        # 만들어지며 자연히 리셋된다.
        _prev_candidate = etd_state.get("candidate") or {}
        _prev_candidate_direction = _prev_candidate.get("direction")
        if direction in ("UP", "DOWN") and _prev_candidate_direction and direction != _prev_candidate_direction:
            freq = etd.apply_opposite_change_point_reaction(freq, _prev_candidate_direction, _prev_signal_score, now)
            etd_state["frequency"] = freq

        if direction not in ("UP", "DOWN") or early_signal["score"] < 50.0:
            etd_state["candidate"] = {}
            etd_state["last_block_reason"] = "조기신호 없음/약함"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "NO_EARLY_SIGNAL", "signal": early_signal}

        desired_symbol = HYNIX_SYMBOL if direction == "UP" else INVERSE_SYMBOL
        _other_symbol_for_confirm = INVERSE_SYMBOL if direction == "UP" else HYNIX_SYMBOL
        current_etf_price = hynix_price if direction == "UP" else inverse_price
        if not current_etf_price:
            etd_state["last_block_reason"] = "ETF 현재가 조회 실패"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "ETF_DATA_INSUFFICIENT", "signal": early_signal}

        # 요구사항(2026-07-21 실측 버그 수정) — 1분봉 단일 표본 비교
        # (signal_symbol_agreement)가 아니라 5/10/20/30초 다중 구간(live_slopes) +
        # VWAP + swing 구조로 판정한다. 000660이 30분 넘게 상승 중이어도 한 틱
        # 눌림/VWAP 순간 이탈 하나만으로 레버리지 신규진입 전체가 막히던 문제의
        # 근본 원인이었다. ETF_CONFIRM_UP/DOWN·ALIGNED_PULLBACK은 통과시키고,
        # ETF_DIRECTION_MISMATCH/ETF_DATA_INSUFFICIENT/DATA_TIME_MISMATCH만 차단한다.
        #
        # live_slopes 자체가 아직 없는 경우(Fast Worker가 이번 세션에 한 번도
        # 안 돌았거나 막 시작한 직후)에는 이 새 게이트가 기존에 없던 차단을 새로
        # 만들면 안 되므로, 그럴 때만 기존(2026-07-20) signal_symbol_agreement
        # 단일표본 비교로 되돌아간다 — "데이터가 이미 있는데 노이즈로 오판"하는
        # 경우만 고치고, "데이터가 아예 아직 없는" 경우의 기존 동작은 바꾸지 않는다.
        _confirm_df = _load_etf_own_minute_cache(desired_symbol)
        if _confirm_df is not None and "datetime" in _confirm_df.columns:
            try:
                _confirm_df = _confirm_df[_confirm_df["datetime"] <= now].copy()
                if _confirm_df.empty:
                    _confirm_df = None
            except Exception:
                pass
        _confirm_breakouts = compute_etf_breakouts(_confirm_df, current_etf_price, direction)
        _confirm_swing_broken = None
        if _confirm_df is not None:
            if direction == "UP" and _confirm_breakouts.get("recent_low"):
                _confirm_swing_broken = current_etf_price < _confirm_breakouts["recent_low"]
            elif direction == "DOWN" and _confirm_breakouts.get("recent_high"):
                _confirm_swing_broken = current_etf_price > _confirm_breakouts["recent_high"]

        if has_any_slope_data(live_slopes.get(desired_symbol)):
            _dt_symbols = (etd_state.get("data_time_status") or {}).get("symbols") or {}
            _confirmation = classify_etf_direction_confirmation(
                direction=direction,
                signal_direction=(live_slopes.get(SIGNAL_SYMBOL) or {}).get("direction"),
                confirm_window_directions=resolve_window_directions(live_slopes.get(desired_symbol)),
                oppose_window_directions=resolve_window_directions(live_slopes.get(_other_symbol_for_confirm)),
                confirm_above_vwap=_confirm_breakouts.get("vwap_breakout"),
                confirm_swing_broken_against=_confirm_swing_broken,
                structural_direction=(state.get("last_primary_trend") or {}).get("primary_trend"),
                # data_time_status가 아직 계산되지 않은 호출에는 age 검증 자체를
                # 건너뛴다(None) — 위에서 data_time_status.blocked를 이미 별도로
                # 확인했으므로 중복 검증이다.
                data_ages_seconds=(
                    {
                        "signal": (_dt_symbols.get(SIGNAL_SYMBOL) or {}).get("age_seconds"),
                        "confirm": (_dt_symbols.get(desired_symbol) or {}).get("age_seconds"),
                        "oppose": (_dt_symbols.get(_other_symbol_for_confirm) or {}).get("age_seconds"),
                    }
                    if _dt_symbols else None
                ),
            )
            etd_state["etf_confirmation"] = _confirmation
            _confirm_state = _confirmation["state"]
            if _confirm_state not in (ETF_CONFIRM_UP, ETF_CONFIRM_DOWN, ETF_ALIGNED_PULLBACK, ETF_CONFIRMATION_PENDING):
                etd_state["last_block_reason"] = f"{_confirm_state}: {_confirmation['reason']}"
                state["early_trend_detector"] = etd_state
                return {
                    "skipped": True, "reason": etd_state["last_block_reason"], "reason_code": _confirm_state,
                    "signal": early_signal, "etf_confirmation": _confirmation,
                }
        else:
            _confirm_state = None
            if signal_symbol_agreement is False:
                etd_state["last_block_reason"] = "ETF_DIRECTION_MISMATCH — 기초자산 신호와 실제 ETF 방향 불일치"
                state["early_trend_detector"] = etd_state
                return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "ETF_DIRECTION_MISMATCH", "signal": early_signal}
        # 요구사항6(2026-07-21 재수정) — MICRO_CHOP은 DATA_TIME_MISMATCH/
        # ETF_DATA_INSUFFICIENT(이미 위에서 확인함)/CHASE_BLOCK/신규진입 금지시간
        # (상위 호출부에서 이미 확인함)보다 낮은 우선순위의 보조 게이트다. 아래
        # candidate 생성 + CHASE_BLOCK 확인이 먼저 끝난 뒤에야 MICRO_CHOP을
        # 평가한다(더 근본적인 차단 사유가 있으면 그게 먼저 보고돼야 한다).
        candidate = dict(etd_state.get("candidate") or {})
        if candidate.get("direction") != direction:
            candidate = {
                "direction": direction, "first_detected_at": now.isoformat(), "reference_price": current_etf_price,
                "change_point_detected_at": now.isoformat(), "direction_confirmed_at": now.isoformat(),
            }
        try:
            _candidate_detected_at = datetime.fromisoformat(candidate["first_detected_at"])
        except Exception:
            _candidate_detected_at = now
            candidate["first_detected_at"] = now.isoformat()
        signal_id = candidate.get("signal_id") or etd.make_signal_id(direction, _candidate_detected_at)
        episode_id = candidate.get("episode_id") or etd.make_episode_id(direction, _candidate_detected_at)
        candidate["signal_id"] = signal_id
        candidate["episode_id"] = episode_id
        candidate["signal_age_seconds"] = round(max(0.0, (now - _candidate_detected_at).total_seconds()), 3)
        etd_state["candidate"] = candidate

        try:
            first_detected_at = datetime.fromisoformat(candidate["first_detected_at"])
        except Exception:
            first_detected_at = now
        elapsed = max(0.0, (now - first_detected_at).total_seconds())
        latency = dict(etd_state.get("latency") or etd.default_latency_trace(
            signal_id=signal_id, worker_name=etd_state.get("order_worker_name") or "MAIN_CYCLE"
        ))
        latency["signal_id"] = signal_id
        latency["worker_name"] = etd_state.get("order_worker_name") or latency.get("worker_name") or "MAIN_CYCLE"
        latency["detected_at"] = candidate.get("first_detected_at")
        latency["direction_confirmed_at"] = candidate.get("direction_confirmed_at") or now.isoformat()
        latency["main_cycle_waiting"] = latency.get("worker_name") != "EARLY_FAST_WORKER"
        latency = etd.mark_latency(latency, "gates_started_at", now)
        etd_state["latency"] = latency
        etd_state["signal_id"] = signal_id
        etd_state["episode_id"] = episode_id
        etd_state["signal_age_seconds"] = candidate["signal_age_seconds"]

        validity = etd.signal_validity(candidate.get("first_detected_at"), now)
        etd_state["signal_validity"] = validity
        if validity == "EXPIRED":
            etd_state["last_block_reason"] = etd.SIGNAL_EXPIRED_CODE
            etd_state["signal_expired"] = True
            etd_state["candidate"] = {}
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": etd.SIGNAL_EXPIRED_CODE, "signal": early_signal, "signal_id": signal_id, "episode_id": episode_id}
        if validity == "REVALIDATE" and _live_direction_for_data != direction:
            etd_state["last_block_reason"] = "SIGNAL_EXPIRED: 30~60초 신호 즉시 재검증 실패"
            etd_state["signal_expired"] = True
            etd_state["candidate"] = {}
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": etd.SIGNAL_EXPIRED_CODE, "signal": early_signal, "signal_id": signal_id, "episode_id": episode_id}

        if etd.episode_first_entry_done(etd_state, episode_id):
            etd_state["last_block_reason"] = "TARGET_ALREADY_FILLED: trend_episode 최초 진입 이미 실행"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "TARGET_ALREADY_FILLED", "signal": early_signal, "signal_id": signal_id, "episode_id": episode_id}

        if etd.is_same_direction_cooldown_active(freq, direction, now):
            etd_state["last_block_reason"] = "동일 방향 재진입 쿨다운(3분)"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "REENTRY_COOLDOWN", "signal": early_signal}

        chase = etd.evaluate_chase_block(
            signal_reference_price=candidate.get("reference_price"), current_price=current_etf_price,
            confirmed_regime=confirmed_regime, df_1min=_etf_df, direction="UP",
        )
        etd_state["chase"] = chase
        if chase["blocked"]:
            _chase_reason = "; ".join(chase["reasons"]) or "CHASE_BLOCK"
            try:
                if _etf_df is not None and current_etf_price:
                    _recent = _etf_df.sort_values("datetime").iloc[-1:]
                    _recent_high = float(_recent["high"].max())
                    _recent_low = float(_recent["low"].min())
                    _distance_to_high_pct = round((_recent_high - float(current_etf_price)) / _recent_high * 100.0, 4) if _recent_high else None
                    _chase_reason = (
                        f"{_chase_reason} | ETF={desired_symbol} price={current_etf_price} "
                        f"recent_1m_high={_recent_high} recent_1m_low={_recent_low} "
                        f"distance_to_high_pct={_distance_to_high_pct}"
                    )
                    chase.update({
                        "symbol": desired_symbol,
                        "recent_1m_high": _recent_high,
                        "recent_1m_low": _recent_low,
                        "distance_to_high_pct": _distance_to_high_pct,
                    })
            except Exception:
                pass
            etd_state["last_block_reason"] = _chase_reason
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "CHASE_BLOCK", "signal": early_signal}

        # 요구사항(2026-07-21 재수정) — MICRO_CHOP은 위 DATA_TIME_MISMATCH/
        # ETF_DATA_INSUFFICIENT/CHASE_BLOCK보다 낮은 우선순위 보조 게이트다.
        # 저장된 micro_chop=True를 그대로 읽고 차단하지 않는다 — 매 틱 최신
        # live_slopes/VWAP/구조 데이터로 해제 조건을 다시 계산한다(요구사항8).
        _vwap_alignment_tracker = dict((etd_state.get("vwap_alignment") or {}).get(desired_symbol) or {})
        _vwap_alignment_tracker = etd.update_vwap_alignment_tracker(
            _vwap_alignment_tracker, aligned=bool(_confirm_breakouts.get("vwap_breakout")), now=now,
        )
        _vwap_alignment_all = dict(etd_state.get("vwap_alignment") or {})
        _vwap_alignment_all[desired_symbol] = _vwap_alignment_tracker
        etd_state["vwap_alignment"] = _vwap_alignment_all
        _confirm_vwap_aligned_seconds = etd.vwap_alignment_seconds(_vwap_alignment_tracker, now)

        if _micro_chop.get("active"):
            _confirm_window_dirs_for_release = resolve_window_directions(live_slopes.get(desired_symbol))
            _release = etd.evaluate_micro_chop_release(
                live_direction=(state.get("live_trade_direction") or {}).get("direction"),
                live_direction_held_seconds=(state.get("live_trade_direction") or {}).get("direction_held_seconds"),
                structural_direction=(state.get("last_primary_trend") or {}).get("primary_trend"),
                confirm_window_directions=_confirm_window_dirs_for_release,
                confirm_vwap_aligned_seconds=_confirm_vwap_aligned_seconds,
                new_swing_breakout=_confirm_breakouts.get("structure_breakout"),
                actionable_signal=fast_signal.get("raw_score_leader_final_action"),
                etf_mutual_confirmed=(_confirm_state in (ETF_CONFIRM_UP, ETF_CONFIRM_DOWN)) if _confirm_state is not None else None,
                data_time_mismatch=bool((etd_state.get("data_time_status") or {}).get("blocked")),
            )
            etd_state["micro_chop_release_check"] = _release
            if _release["release"]:
                _micro_chop["active"] = False
                _micro_chop["released_at"] = now.isoformat()
                _micro_chop["release_reason"] = _release["reason"]
                etd_state["micro_chop"] = _micro_chop
            else:
                try:
                    _mc_elapsed = (now - datetime.fromisoformat(_micro_chop["activated_at"])).total_seconds()
                except Exception:
                    _mc_elapsed = None
                etd_state["last_block_reason"] = (
                    f"MICRO_CHOP: 박스권 신규진입 차단(발생 {_micro_chop.get('activated_at') or '-'}, "
                    f"경과 {_mc_elapsed:.0f}초, TTL {_micro_chop.get('ttl_seconds')}초, "
                    f"방향전환 {_micro_chop.get('direction_flips')}회, VWAP교차 {_micro_chop.get('vwap_crosses')}회, "
                    f"이동효율 {_micro_chop.get('avg_move_efficiency')}, 미해제 사유=조건 미충족)"
                    if _mc_elapsed is not None else "MICRO_CHOP: 박스권 신규진입 차단"
                )
                state["early_trend_detector"] = etd_state
                return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "MICRO_CHOP", "signal": early_signal, "micro_chop": _micro_chop}

        _returns = fast_signal.get("returns") or {}
        expected_move_pct = max(abs(float(_returns.get(k) or 0.0)) for k in ("1m", "3m", "5m"))
        cost_gate = etd.evaluate_cost_gate(desired_symbol, expected_move_pct)
        etd_state["cost_gate"] = cost_gate
        if cost_gate["blocked"]:
            def _fmt_pct(value) -> str:
                try:
                    return f"{float(value):.2f}%"
                except (TypeError, ValueError):
                    return "n/a"

            def _fmt_ratio(value) -> str:
                try:
                    return f"{float(value):.2f}x"
                except (TypeError, ValueError):
                    return "n/a"

            etd_state["last_block_reason"] = (
                f"거래비용 게이트 — 예상Gross {_fmt_pct(cost_gate.get('expected_gross_edge_pct'))}, "
                f"비용 {_fmt_pct(cost_gate.get('cost_pct'))}, 예상순이익 {_fmt_pct(cost_gate.get('net_edge_pct'))}, "
                f"Gross/비용 {_fmt_ratio(cost_gate.get('gross_to_cost_ratio'))}"
            )
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "COST_EDGE_BLOCK", "signal": early_signal}

        # 요구사항2 — 50% 단계는 30초 유지 "그리고" ETF/기초자산 방향 일치일 때만.
        # 요구사항(2026-07-21) — ALIGNED_PULLBACK(5·10초만 일시 눌림)은 완전 확인이
        # 아니므로 direction_aligned=False로 취급해 30% 단계에 머문다(item5의
        # "최초 20~30% 진입"과 동일한 효과) — 이후 재확인되어 ETF_CONFIRM_*로
        # 바뀌면 direction_aligned=True가 되어 기존 시간창 로직대로 자연히
        # 50%/70%까지 확대된다. 새 상태머신을 추가하지 않고 기존 사다리를 그대로 쓴다.
        # _confirm_state가 None이면(live_slopes 데이터 자체가 아직 없어 위에서
        # 구버전 signal_symbol_agreement 폴백으로 처리한 경우) 기존(2026-07-20)
        # 방식 그대로 signal_symbol_agreement를 direction_aligned으로 쓴다.
        direction_aligned = (
            _confirm_state in (ETF_CONFIRM_UP, ETF_CONFIRM_DOWN)
            if _confirm_state is not None else bool(signal_symbol_agreement)
        )
        stage, target_pct = etd.compute_target_probe_pct(confirmed_regime, elapsed, direction_aligned=direction_aligned)
        etd_state["stage"], etd_state["target_pct"] = stage, target_pct
        if target_pct <= 0.0:
            etd_state["last_block_reason"] = f"NO_PROBE_TARGET: confirmed_regime={confirmed_regime}, stage={stage}"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "NO_EARLY_SIGNAL", "signal": early_signal}

        if not current_etf_price:
            etd_state["last_block_reason"] = "ETF 현재가 조회 실패"
            state["early_trend_detector"] = etd_state
            return {"skipped": True, "reason": etd_state["last_block_reason"], "reason_code": "ETF_DATA_INSUFFICIENT"}

        etd_state["signal_price"] = candidate.get("reference_price")
        etd_state["current_price"] = current_etf_price
        etd_state["last_block_reason"] = None
        moved_pct_since_signal = None
        if candidate.get("reference_price") and current_etf_price:
            moved_pct_since_signal = round(abs(current_etf_price / candidate["reference_price"] - 1.0) * 100.0, 4)
        etd_state["etf_move_pct_since_signal"] = moved_pct_since_signal
        latency = etd.mark_latency(etd_state.get("latency"), "gates_completed_at", now)
        etd_state["latency"] = latency

        # Early Detector는 입력·SHADOW만 — LIVE 주문은 WEIGHTED_ORDER_CONTROLLER 전용.
        # early_trend_detector_live=True여도 EARLY_PROBE/ENHANCED fallback BUY를 내지 않는다.
        state["early_trend_detector"] = etd_state
        state["configured_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
        state["actual_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
        return {
            "skipped": True,
            "reason": (
                "SHADOW 모드 — 계산만 하고 실제 진입은 하지 않음"
                if not live
                else "SHADOW — Early Detector provides inputs only; WEIGHTED_ORDER_CONTROLLER owns live entries"
            ),
            "reason_code": "EARLY_INPUT_ONLY" if live else "NO_EARLY_SIGNAL",
            "signal": early_signal, "stage": stage, "target_pct": target_pct,
            "signal_id": signal_id, "episode_id": episode_id, "latency": latency,
            "order_permission": "DIAGNOSTIC_ONLY",
        }

    # ── 이미 EARLY_PROBE 보유 중 — Early 확대/단계진행 BUY 금지(청산만 Dynamic Exit) ──
    probe = dict(etd_state.get("probe") or etd.default_probe_state())
    direction = probe.get("direction")
    try:
        detected_at = datetime.fromisoformat(probe["detected_at"]) if probe.get("detected_at") else now
    except Exception:
        detected_at = now
    elapsed = max(0.0, (now - detected_at).total_seconds())

    holding_inverse = held_symbol == INVERSE_SYMBOL
    current_etf_price = inverse_price if holding_inverse else hynix_price
    etd_state["signal_price"] = probe.get("signal_reference_price")
    etd_state["current_price"] = current_etf_price
    etd_state["stage"] = probe.get("stage")
    etd_state["target_pct"] = probe.get("position_pct")
    if probe.get("signal_reference_price") and current_etf_price:
        etd_state["etf_move_pct_since_signal"] = round(abs(current_etf_price / probe["signal_reference_price"] - 1.0) * 100.0, 4)

    state["early_trend_detector"] = etd_state
    state["configured_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
    state["actual_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
    if not live:
        return None
    return {
        "skipped": True,
        "reason": "SHADOW — Early Detector expansion blocked; WEIGHTED_ORDER_CONTROLLER owns live entries",
        "reason_code": "EARLY_INPUT_ONLY",
        "order_permission": "DIAGNOSTIC_ONLY",
        "probe": probe,
        "elapsed_seconds": elapsed,
    }



def run_early_trend_fast_feed_tick(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """요구사항(2026-07-20 최종) — Early Trend Detector 전용 5초 주기 경량 틱.

    calculate_enhanced_hynix_prediction_score() 등 무거운 전체 재계산(30초
    주기 run_fast_trend_watcher_tick이 계속 담당)은 다시 하지 않는다 — KIS
    분봉/계좌 API를 새로 늘리지 않기 위해서다. 이 함수는 오직:
      (a) 가벼운 현재가 조회 2건(collect_long_current/collect_inverse_current)만으로
          진짜 5/10/20/30초 기울기(app.trading.early_trend_live_feed)를 쌓고,
      (b) 최근(최대 30초 전) 계산된 fast_signal/confirmed_regime을 그대로 재사용해
          Early Trend Detector의 신규진입/단계진행/확대 판단만 더 빠른 주기로 수행한다.
    반대 change-point 발생 시의 즉시 철수는 1초 주기 Dynamic Exit Watcher가 이
    live_slopes를 읽어 담당한다(app.trading.dynamic_exit_watcher._tick_locked)."""
    from app.services.hynix_switch_state import with_state_lock
    from app.trading import early_trend_live_feed as feed
    from app.trading import early_trend_detector as etd
    from app.trading.etf_entry_confirmation import resolve_window_directions
    from app.trading.range_weighted_optimize import (
        daily_loss_limit_reached_from_pct,
        get_range_weighted_config,
        load_optimized_config,
        resolve_day_regime_from_cache,
    )

    load_optimized_config()
    now = now or kst_now()
    resolved_mode = mode or load_state(mode=None).get("mode", "mock")
    with with_state_lock(resolved_mode):
        state = load_state(mode=resolved_mode)
        state["mode"] = resolved_mode
        state["weighted_entry_controller_only"] = True
        if not state.get("auto_trade_on") or state.get("stopped"):
            return {"skipped": True, "reason": "auto off or stopped"}
        early_enabled = bool(state.get("early_trend_detector_enabled"))

        try:
            from app.data_sources.hynix_long_collector import collect_long_current
            from app.data_sources.hynix_inverse_collector import collect_inverse_current
            from app.data_sources.auto_market_collector import _fetch_hynix_current_from_kis

            def _fetch_signal_quote():
                fetched_at = kst_now()
                price = _fetch_hynix_current_from_kis(resolved_mode)
                return fetched_at, {
                    "symbol": SIGNAL_SYMBOL,
                    "current_price": price,
                    "source": "kis_direct",
                    "status": "success" if price is not None else "unavailable",
                    "timestamp": fetched_at.isoformat(),
                    "stale": price is None,
                }

            def _fetch_long_quote():
                quote = collect_long_current(mode=resolved_mode)
                return kst_now(), quote

            def _fetch_inverse_quote():
                quote = collect_inverse_current(mode=resolved_mode)
                return kst_now(), quote

            with ThreadPoolExecutor(max_workers=3) as pool:
                fs = pool.submit(_fetch_signal_quote)
                fl = pool.submit(_fetch_long_quote)
                fi = pool.submit(_fetch_inverse_quote)
                signal_fetched_at, signal_quote = fs.result()
                long_fetched_at, long_quote = fl.result()
                inverse_fetched_at, inverse_quote = fi.result()
            signal_quote = {
                **signal_quote,
                "timestamp": signal_quote.get("timestamp") or signal_fetched_at.isoformat(),
            }
        except Exception as exc:
            logger.debug("[EarlyTrendFastFeed] 현재가 조회 실패: %s", exc)
            return {"skipped": True, "reason": f"price fetch failed: {exc}"}

        long_price = long_quote.get("current_price")
        inverse_price = inverse_quote.get("current_price")
        signal_price = signal_quote.get("current_price")

        etd_state = dict(state.get("early_trend_detector") or {})
        data_time_status = _data_time_mismatch_status(
            {
                SIGNAL_SYMBOL: signal_quote,
                HYNIX_SYMBOL: long_quote,
                INVERSE_SYMBOL: inverse_quote,
            },
            {
                SIGNAL_SYMBOL: signal_fetched_at,
                HYNIX_SYMBOL: long_fetched_at,
                INVERSE_SYMBOL: inverse_fetched_at,
            },
            now=now,
        )
        etd_state["data_time_status"] = data_time_status
        history = etd_state.get("price_history") or {}
        history = feed.record_price_sample(history, SIGNAL_SYMBOL, signal_price, now)
        history = feed.record_price_sample(history, HYNIX_SYMBOL, long_price, now)
        history = feed.record_price_sample(history, INVERSE_SYMBOL, inverse_price, now)
        etd_state["price_history"] = history
        etd_state["live_slopes"] = {
            SIGNAL_SYMBOL: feed.compute_live_direction(history, SIGNAL_SYMBOL, now),
            HYNIX_SYMBOL: feed.compute_live_direction(history, HYNIX_SYMBOL, now),
            INVERSE_SYMBOL: feed.compute_live_direction(history, INVERSE_SYMBOL, now),
        }
        live_trade = feed.compute_live_trade_direction(
            history, now, signal_symbol=SIGNAL_SYMBOL, long_symbol=HYNIX_SYMBOL, inverse_symbol=INVERSE_SYMBOL,
        )
        previous_live_direction = (state.get("live_trade_direction") or {}).get("direction")
        _prev_direction_held_since = (state.get("live_trade_direction") or {}).get("direction_held_since")
        cached_fast_signal = (state.get("fast_trend_watcher") or {}).get("last_signal") or {}
        if live_trade.get("direction") in ("UP", "DOWN"):
            live_direction = live_trade["direction"]
            cached_fast_signal = {
                **cached_fast_signal,
                "direction": live_direction,
                "signal_id": f"FAST_LIVE:{live_direction}:{now.strftime('%Y%m%d%H%M%S')}",
                "up_votes": max(int(cached_fast_signal.get("up_votes") or 0), 6 if live_direction == "UP" else 0),
                "down_votes": max(int(cached_fast_signal.get("down_votes") or 0), 6 if live_direction == "DOWN" else 0),
                "returns": {"3m": max(abs((cached_fast_signal.get("returns") or {}).get("3m") or 0.0), 1.0)},
                "top_factors": list(cached_fast_signal.get("top_factors") or []) + ["EARLY_FAST_WORKER actionable live direction"],
            }
        _price_action_direction = live_trade.get("direction")
        _pa_symbol = HYNIX_SYMBOL if _price_action_direction == "UP" else (INVERSE_SYMBOL if _price_action_direction == "DOWN" else None)
        _pa_oppose_symbol = INVERSE_SYMBOL if _pa_symbol == HYNIX_SYMBOL else (HYNIX_SYMBOL if _pa_symbol == INVERSE_SYMBOL else None)
        _pa_signal_dirs = resolve_window_directions(etd_state["live_slopes"].get(SIGNAL_SYMBOL))
        _pa_confirm_slope = etd_state["live_slopes"].get(_pa_symbol) if _pa_symbol else {}
        _pa_confirm_dirs = resolve_window_directions(_pa_confirm_slope)
        _pa_oppose_dirs = resolve_window_directions(etd_state["live_slopes"].get(_pa_oppose_symbol)) if _pa_oppose_symbol else {}
        _pa_confirm_above_vwap = None
        _pa_swing_breakout = False
        _pa_macd_williams = {"confirmed": False, "reason": "DATA_INSUFFICIENT"}
        if _pa_symbol:
            try:
                from app.trading.etf_entry_confirmation import compute_etf_breakouts, compute_etf_vwap

                _pa_df = _load_etf_own_minute_cache(_pa_symbol)
                _pa_price = long_price if _pa_symbol == HYNIX_SYMBOL else inverse_price
                _pa_vwap = compute_etf_vwap(_pa_df) if _pa_df is not None else None
                _pa_confirm_above_vwap = bool(_pa_vwap is not None and _pa_price is not None and float(_pa_price) >= float(_pa_vwap))
                _pa_breakouts = compute_etf_breakouts(_pa_df, _pa_price, "UP") if _pa_df is not None and _pa_price is not None else {}
                _pa_swing_breakout = bool(_pa_breakouts.get("recent_high") and float(_pa_price) > float(_pa_breakouts["recent_high"]))
                _pa_macd_williams = _macd_williams_confirmation(_pa_df, _price_action_direction)
            except Exception:
                _pa_confirm_above_vwap = None
                _pa_swing_breakout = False
        _pa_slopes = (_pa_confirm_slope or {}).get("slopes") or {}
        try:
            _s5, _s10, _s20 = float(_pa_slopes.get(5) or 0.0), float(_pa_slopes.get(10) or 0.0), float(_pa_slopes.get(20) or 0.0)
        except Exception:
            _s5 = _s10 = _s20 = 0.0
        _pa_acceleration = _s5 > 0 and _s10 > 0 and _s20 > 0 and abs(_s5) >= abs(_s10) >= abs(_s20)
        factors = {
            "slope_5s_10s_reversal": bool(
                previous_live_direction
                and _price_action_direction in ("UP", "DOWN")
                and previous_live_direction != _price_action_direction
                and _pa_signal_dirs.get(5) == _price_action_direction
                and _pa_signal_dirs.get(10) == _price_action_direction
            ),
            "vwap_reclaim_with_slope": bool(_pa_confirm_above_vwap is True and (_pa_confirm_dirs.get(5) == "UP" or _pa_confirm_dirs.get(10) == "UP")),
            "swing_high_low_breakout": bool(_pa_swing_breakout),
            "acceleration_5_10_20_strengthening": bool(_pa_acceleration),
            "etf_mutual_direction_confirmed": bool(
                _pa_confirm_dirs.get(5) == "UP"
                and _pa_confirm_dirs.get(10) == "UP"
                and _pa_oppose_dirs.get(5) == "DOWN"
                and _pa_oppose_dirs.get(10) == "DOWN"
            ),
        }
        # 2026-07-22: 가격행동 조기진입(D)은 SHADOW 격리 — LIVE 주문에 쓰지 않는다.
        # 1분봉 선형보간 리플레이로는 활성화하지 않으며, 실 5초 틱만 SHADOW 기록.
        from app.trading.strategy_architecture import (
            price_action_shadow_payload,
            get_episode_gate_mode,
        )
        from app.trading.macd_williams_episode import confirm_episode_direction

        _pa_factor_count = sum(1 for ok in factors.values() if ok)
        etd_state["price_action_reversal"] = {
            "direction": _price_action_direction,
            "factors": factors,
            "factor_count": _pa_factor_count,
            "confirm_window_directions": _pa_confirm_dirs,
            "oppose_window_directions": _pa_oppose_dirs,
            "confirm_above_vwap": _pa_confirm_above_vwap,
            "swing_breakout": _pa_swing_breakout,
            "macd_williams_confirmation": _pa_macd_williams,
            "shadow": price_action_shadow_payload(
                direction=_price_action_direction,
                factors=factors,
                factor_count=_pa_factor_count,
                macd_williams=_pa_macd_williams,
                source="live_5s_tick",
            ),
            "live_order_forbidden": True,
        }
        # C: 3분봉 MACD+Williams episode 확인기 (broker 주문 금지)
        try:
            from app.data_sources.auto_market_collector import _load_hynix_minute_cache

            _signal_1m = _load_hynix_minute_cache()
        except Exception:
            _signal_1m = None
        _episode_confirm = confirm_episode_direction(
            _signal_1m,
            proposed_direction=live_trade.get("direction"),
            now=now,
        )
        etd_state["macd_williams_episode"] = _episode_confirm
        state["macd_williams_episode_gate_mode"] = get_episode_gate_mode(state)
        reversal_candidate = feed.update_reversal_candidate_state(
            etd_state.get("reversal_candidate"),
            live_direction=live_trade.get("direction"),
            previous_direction=previous_live_direction,
            factors=factors,
            now=now,
        )
        # 요구사항(2026-07-21) — live_trade_direction이 몇 초째 같은 방향을
        # 유지 중인지 추적한다(MICRO_CHOP 해제 조건 "20초 이상 유지"에 사용).
        # 방향이 바뀌거나 미확정이면 리셋한다.
        if previous_live_direction == live_trade.get("direction") and live_trade.get("direction") in ("UP", "DOWN") and _prev_direction_held_since:
            _direction_held_since = _prev_direction_held_since
        elif live_trade.get("direction") in ("UP", "DOWN"):
            _direction_held_since = now.isoformat()
        else:
            _direction_held_since = None
        try:
            _direction_held_seconds = (
                (now - datetime.fromisoformat(_direction_held_since)).total_seconds()
                if _direction_held_since else None
            )
        except Exception:
            _direction_held_seconds = None
        _direction_episode_id = (
            f"{live_trade.get('direction')}:{_direction_held_since}"
            if live_trade.get("direction") in ("UP", "DOWN") and _direction_held_since else None
        )

        if reversal_candidate.get("status") == "REVERSAL_CANDIDATE":
            freq = etd.reset_frequency_state_if_new_day(etd_state.get("frequency"), now.strftime("%Y%m%d"))
            prev_signal = (etd_state.get("last_signal") or {})
            etd_state["frequency"] = etd.apply_live_reversal_candidate_reaction(
                freq, previous_live_direction, prev_signal.get("score"), now,
            )
            state["live_trade_direction"] = {
                **live_trade,
                "direction": reversal_candidate.get("candidate_direction") or live_trade.get("direction"),
                "status": "REVERSAL_CANDIDATE",
                "structural_trend": (state.get("last_primary_trend") or {}).get("primary_trend"),
                "existing_direction_blocked": True,
                "first_detected_at": reversal_candidate.get("first_detected_at"),
                "confirmed_at": reversal_candidate.get("confirmed_at"),
                "detection_to_confirmation_delay_seconds": reversal_candidate.get("detection_to_confirmation_delay_seconds"),
                "direction_held_since": _direction_held_since,
                "direction_held_seconds": _direction_held_seconds,
                "direction_episode_id": _direction_episode_id,
            }
            cached_fast_signal = {
                **cached_fast_signal,
                "direction": state["live_trade_direction"]["direction"],
                "up_votes": 6 if state["live_trade_direction"]["direction"] == "UP" else 0,
                "down_votes": 6 if state["live_trade_direction"]["direction"] == "DOWN" else 0,
                "returns": {"3m": 0.9},
                "top_factors": reversal_candidate.get("active_factors") or [],
            }
            cached_confirmed_regime = etd.REGIME_FAST_REVERSAL_RANGE
        else:
            # 요구사항6(2026-07-21) — live direction=UP/DOWN인데 status가 별개
            # 필드(reversal_candidate.status)로만 결정돼 "NONE"으로 남는 불일치가
            # 있었다. direction이 확정돼 있으면 status를 그로부터 직접 도출해
            # 항상 서로 일치하게 만든다. MICRO_CHOP 여부는 이 함수 호출 뒤
            # _run_early_trend_detector_tick에서 다시 덮어쓴다(그쪽이 실제
            # micro_chop 판정을 갖고 있다).
            _live_dir_now = live_trade.get("direction")
            if _live_dir_now == "UP":
                _status_now = "ALIGNED_UP"
            elif _live_dir_now == "DOWN":
                _status_now = "ALIGNED_DOWN"
            elif not live_trade.get("windows_available"):
                _status_now = "DATA_INSUFFICIENT"
            else:
                _status_now = reversal_candidate.get("status") or "DATA_INSUFFICIENT"
            state["live_trade_direction"] = {
                **live_trade,
                "status": _status_now,
                "structural_trend": (state.get("last_primary_trend") or {}).get("primary_trend"),
                "existing_direction_blocked": bool(reversal_candidate.get("existing_direction_blocked")),
                "first_detected_at": reversal_candidate.get("first_detected_at"),
                "confirmed_at": reversal_candidate.get("confirmed_at"),
                "detection_to_confirmation_delay_seconds": reversal_candidate.get("detection_to_confirmation_delay_seconds"),
                "direction_held_since": _direction_held_since,
                "direction_held_seconds": _direction_held_seconds,
                "direction_episode_id": _direction_episode_id,
            }
            cached_confirmed_regime = (state.get("adaptive_regime") or {}).get("confirmed_regime")
        etd_state["reversal_candidate"] = reversal_candidate
        state["early_trend_detector"] = etd_state

        live = bool(early_enabled and state.get("early_trend_detector_live"))
        # Early ON/OFF·LIVE는 조기신호 입력(SHADOW) 여부만 바꾼다. auto_trade_on이면
        # WEIGHTED_ORDER_CONTROLLER_LIVE가 항상 신규진입을 소유하며, Early 토글로
        # Fast Worker를 early-return 시켜서는 안 된다.
        state["configured_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
        state["actual_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
        state["entry_orchestrator"] = {
            "name": "OrderCoordinator",
            "mode": "WEIGHTED_ORDER_CONTROLLER_LIVE",
            "reason": "evaluate_range_weighted_entry owns REVERSAL/CONTINUATION/PULLBACK entry orders",
            "updated_at": now.isoformat(),
        }
        etd_state["early_live_input"] = live
        etd_state["early_enabled_input"] = early_enabled

        if not is_new_entry_allowed(now):
            position = state.get("position") or {}
            if position.get("entry_type") != etd.ENTRY_TYPE_EARLY_PROBE:
                save_state_atomic(state)
                return {"skipped": True, "reason": "new entry window closed", "live_slopes": etd_state["live_slopes"]}

        if not _EARLY_ORDER_LOCK.acquire(blocking=False):
            etd_state["last_block_reason"] = "DUPLICATE_ORDER_LOCK: Early order worker already running"
            state["early_trend_detector"] = etd_state
            save_state_atomic(state)
            return {"skipped": True, "reason": etd_state["last_block_reason"], "live_slopes": etd_state["live_slopes"]}

        try:
            from app.config import get_config
            from app.trading.hynix_position_common import HynixPositionManager

            cfg = get_config()
            broker = _create_strategy_broker(cfg, resolved_mode)
            position_manager = HynixPositionManager(broker, mode=resolved_mode)
            state["configured_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
            state["actual_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
            state["entry_orchestrator"] = {
                "name": "OrderCoordinator",
                "mode": "WEIGHTED_ORDER_CONTROLLER_LIVE",
                "reason": "evaluate_range_weighted_entry owns REVERSAL/CONTINUATION/PULLBACK entry orders",
                "updated_at": now.isoformat(),
            }
            etd_state["order_worker_name"] = "EARLY_FAST_WORKER"
            etd_state["latency"] = etd.mark_latency(
                etd.default_latency_trace(worker_name="EARLY_FAST_WORKER"), "account_query_started_at", now
            )
            state["early_trend_detector"] = etd_state
            position_manager.sync()
            etd_state = dict(state.get("early_trend_detector") or etd_state)
            etd_state["latency"] = etd.mark_latency(etd_state.get("latency"), "account_query_completed_at", kst_now())
            state["early_trend_detector"] = etd_state
            apply_position_manager_to_state(state, position_manager)

            def _fast_order_succeeded(switch_result: dict) -> bool:
                return bool(
                    switch_result
                    and switch_result.get("acted")
                    and any(bool(o.get("success")) for o in (switch_result.get("orders") or []) if isinstance(o, dict))
                )

            _early_signal = etd.compute_composite_early_signal(
                fast_signal=cached_fast_signal,
                signal_symbol_agreement=None,
                live_direction=(state.get("live_trade_direction") or {}).get("direction"),
                etf_vwap_breakout=None,
                etf_structure_breakout=None,
                etf_volume_surge=None,
            ) if early_enabled else {"direction": None, "score": 0.0}
            _early_reason_code = (
                None
                if early_enabled and _early_signal.get("direction") in ("UP", "DOWN") and _early_signal.get("score", 0.0) >= 50.0
                else ("EARLY_INPUT_DISABLED" if not early_enabled else "NO_EARLY_SIGNAL")
            )
            early_result = {
                "skipped": bool(_early_reason_code),
                "reason": _early_reason_code or "EARLY_SIGNAL_DIAGNOSTIC_ONLY",
                "reason_code": _early_reason_code,
                "signal": _early_signal,
                # D 가격행동 REVERSAL은 SHADOW — LIVE entry_path로 쓰지 않음
                "entry_path": None,
                "order_permission": "DIAGNOSTIC_ONLY",
                "price_action_shadow": True,
            }
            continuation_state = dict(state.get("trend_continuation_entry") or {})
            continuation_state["last_evaluated_at"] = now.isoformat()
            continuation_state["evaluation_count_today"] = int(continuation_state.get("evaluation_count_today") or 0) + 1
            desired_live_direction = live_trade.get("direction")
            decision_for_continuation = state.get("last_decision") or {}
            _episode_confirm = etd_state.get("macd_williams_episode") or {}
            if desired_live_direction not in ("UP", "DOWN"):
                # C 확인기가 반대면 enhanced가 방향을 덮어쓰지 못함
                from app.trading.macd_williams_episode import enhanced_may_set_direction

                _gap = _score_gap_from_decision(decision_for_continuation)
                try:
                    _hynix_score = float(decision_for_continuation.get("enhanced_score") or 50.0)
                    _inverse_score = float(decision_for_continuation.get("inverse_pressure_score") or 50.0)
                except Exception:
                    _hynix_score, _inverse_score = 50.0, 50.0
                _enhanced_leader = "UP" if _hynix_score >= _inverse_score else "DOWN"
                if (
                    _gap >= 30.0
                    and enhanced_may_set_direction(
                        _episode_confirm,
                        enhanced_leader=_enhanced_leader,
                        live_direction=desired_live_direction,
                    )
                ):
                    desired_live_direction = _enhanced_leader
                elif _episode_confirm.get("indicator_direction") in ("UP", "DOWN"):
                    # enhanced 차단 시 episode 지표 방향을 참고(주문은 A만)
                    desired_live_direction = _episode_confirm["indicator_direction"]
            desired_symbol = symbol_for_live_direction(desired_live_direction)
            current_etf_price = long_price if desired_symbol == HYNIX_SYMBOL else (inverse_price if desired_symbol == INVERSE_SYMBOL else None)
            from app.trading.etf_entry_confirmation import resolve_window_directions, trade_aligned_window_directions

            confirm_dirs_raw = (
                resolve_window_directions(etd_state["live_slopes"].get(desired_symbol))
                if desired_symbol else {}
            )
            oppose_symbol = INVERSE_SYMBOL if desired_symbol == HYNIX_SYMBOL else HYNIX_SYMBOL
            oppose_dirs_raw = (
                resolve_window_directions(etd_state["live_slopes"].get(oppose_symbol))
                if desired_symbol else {}
            )
            # episode/반대전환 로직은 trade-aligned(인버스 반전) 방향을 쓰고,
            # evaluate_range_weighted_entry는 진입 ETF 가격공간(상승=UP)을 기대한다.
            confirm_dirs = (
                trade_aligned_window_directions(confirm_dirs_raw, symbol=desired_symbol)
                if desired_symbol else {}
            )
            oppose_dirs = (
                trade_aligned_window_directions(oppose_dirs_raw, symbol=oppose_symbol)
                if desired_symbol else {}
            )
            confirm_above_vwap = None
            confirm_swing_breakout = None
            if desired_symbol and current_etf_price:
                try:
                    from app.trading.etf_entry_confirmation import compute_etf_vwap

                    _confirm_df = _load_etf_own_minute_cache(desired_symbol)
                    _vwap = compute_etf_vwap(_confirm_df) if _confirm_df is not None else None
                    confirm_above_vwap = bool(_vwap is not None and float(current_etf_price) >= float(_vwap))
                    try:
                        from app.trading.etf_entry_confirmation import compute_etf_breakouts

                        _breakouts = compute_etf_breakouts(_confirm_df, current_etf_price, desired_live_direction) if _confirm_df is not None else {}
                        if desired_live_direction == "UP":
                            confirm_swing_breakout = bool(_breakouts.get("recent_high") and float(current_etf_price) > float(_breakouts["recent_high"]))
                        elif desired_live_direction == "DOWN":
                            confirm_swing_breakout = bool(_breakouts.get("recent_low") and float(current_etf_price) < float(_breakouts["recent_low"]))
                    except Exception:
                        confirm_swing_breakout = None
                    continuation_state["vwap"] = _vwap
                except Exception:
                    confirm_above_vwap = None
            _existing_episode_direction = continuation_state.get("direction")
            _vwap_by_symbol = dict(continuation_state.get("prev_above_vwap_by_symbol") or {})
            _prev_above_vwap = _vwap_by_symbol.get(desired_symbol) if desired_symbol else continuation_state.get("prev_above_vwap")
            _vwap_reclaim = bool(
                confirm_above_vwap
                and _prev_above_vwap is False
                and confirm_dirs.get(5) == desired_live_direction
                and confirm_dirs.get(10) == desired_live_direction
            )
            _existing_structure_broken = False
            if _existing_episode_direction and _existing_episode_direction != desired_live_direction:
                try:
                    from app.trading.etf_entry_confirmation import is_swing_structure_broken_against

                    _existing_symbol = symbol_for_live_direction(_existing_episode_direction) or HYNIX_SYMBOL
                    _existing_df = _load_etf_own_minute_cache(_existing_symbol)
                    _existing_price = long_price if _existing_symbol == HYNIX_SYMBOL else inverse_price
                    if _existing_df is not None and _existing_price:
                        # Inverse is held long for market DOWN — use trade-aligned UP.
                        _structure_dir = (
                            "UP" if _existing_symbol == INVERSE_SYMBOL else _existing_episode_direction
                        )
                        _existing_structure_broken = is_swing_structure_broken_against(
                            _existing_df, float(_existing_price), _structure_dir,
                        )
                except Exception:
                    _existing_structure_broken = False
            _opposite_episode_confirmed = detect_opposite_episode_transition(
                existing_direction=_existing_episode_direction,
                new_direction=desired_live_direction,
                live_direction_matches=live_trade.get("direction") == desired_live_direction,
                confirm_dirs=confirm_dirs,
                existing_structure_broken=_existing_structure_broken,
                new_etf_vwap_reclaim=_vwap_reclaim,
                new_swing_breakout=bool(confirm_swing_breakout),
            )
            _direction_episode_changed = False
            if continuation_state.get("direction") != desired_live_direction and (
                not _existing_episode_direction or _opposite_episode_confirmed
            ):
                _direction_episode_changed = True
                reset_range_episode_probe_state(
                    continuation_state,
                    now=now,
                    direction=desired_live_direction,
                    episode_id=f"{desired_live_direction}:{now.isoformat()}",
                    reference_price=current_etf_price,
                )
            continuation_state["prev_above_vwap"] = confirm_above_vwap
            if desired_symbol:
                _vwap_by_symbol[desired_symbol] = confirm_above_vwap
                continuation_state["prev_above_vwap_by_symbol"] = _vwap_by_symbol
            update_range_episode_structural_events(
                continuation_state,
                now=now,
                swing_breakout=bool(confirm_swing_breakout),
                vwap_reclaim=_vwap_reclaim,
            )
            moved_pct = None
            try:
                if continuation_state.get("reference_price") and current_etf_price:
                    moved_pct = round(abs(float(current_etf_price) / float(continuation_state["reference_price"]) - 1.0) * 100.0, 4)
            except Exception:
                moved_pct = None
            _returns = cached_fast_signal.get("returns") or {}
            try:
                expected_move_pct = max(abs(float(_returns.get(k) or 0.0)) for k in ("1m", "3m", "5m"))
            except Exception:
                expected_move_pct = 0.0
            cost_gate = etd.evaluate_cost_gate(desired_symbol, expected_move_pct) if desired_symbol else {"blocked": True}
            _cost_pct = cost_gate.get("cost_pct")
            _expected_move_for_edge = expected_move_pct
            _day_regime = resolve_day_regime_from_cache()
            _range_cfg = get_range_weighted_config()
            continuation_eval = evaluate_range_weighted_entry(
                decision=decision_for_continuation,
                direction=desired_live_direction,
                live_direction=desired_live_direction,
                live_direction_held_seconds=(state.get("live_trade_direction") or {}).get("direction_held_seconds"),
                signal_window_directions=resolve_window_directions(etd_state["live_slopes"].get(SIGNAL_SYMBOL)),
                confirm_window_directions=confirm_dirs_raw,
                oppose_window_directions=oppose_dirs_raw,
                confirm_above_vwap=confirm_above_vwap,
                moved_pct_since_signal=moved_pct,
                expected_move_pct=_expected_move_for_edge,
                cost_pct=_cost_pct,
                expected_mfe_pct=_expected_move_for_edge,
                expected_mae_pct=abs(float(etd.FIXED_EARLY_STOP_PCT)),
                ema_slope_aligned=(
                    (desired_live_direction == "UP" and float(cached_fast_signal.get("ema_slope_pct") or 0.0) >= 0.0)
                    or (desired_live_direction == "DOWN" and float(cached_fast_signal.get("ema_slope_pct") or 0.0) <= 0.0)
                ),
                micro_chop_active=bool((etd_state.get("micro_chop") or {}).get("active")),
                confidence=(state.get("adaptive_regime") or {}).get("confidence"),
                stop_loss_distance_pct=abs(float(etd.FIXED_EARLY_STOP_PCT)),
                buyable_cash=broker.get_buyable_cash() if hasattr(broker, "get_buyable_cash") else None,
                current_price=current_etf_price,
                # D REVERSAL hint 제거 — A weighted RANGE만 CONTINUATION/PULLBACK 경로
                entry_path_hint=None,
                structure_confirmed=bool(confirm_swing_breakout),
                structural_direction=(state.get("last_primary_trend") or {}).get("primary_trend"),
                soft_reason_codes=[
                    code for code in (
                        (_early_reason_code if early_enabled else None),
                        ((etd_state.get("etf_confirmation") or {}).get("state")),
                        "MICRO_CHOP" if bool((etd_state.get("micro_chop") or {}).get("active")) else None,
                    ) if code
                ],
                day_regime=_day_regime,
                range_config=_range_cfg,
            )
            continuation_state.update({
                "last_result": continuation_eval,
                "last_reason_code": continuation_eval.get("reason_code"),
                "score_gap": continuation_eval.get("score_gap"),
                "live_direction_held_seconds": (state.get("live_trade_direction") or {}).get("direction_held_seconds"),
                "confirm_window_directions": confirm_dirs,
                "oppose_window_directions": oppose_dirs,
                "confirm_swing_breakout": confirm_swing_breakout,
                "structure_confirmed": continuation_eval.get("structure_confirmed"),
                "structural_signal_label": continuation_eval.get("structural_signal_label"),
                "moved_pct_since_signal": moved_pct,
                "cost_gate": cost_gate,
                "expected_move_basis": "max_abs_fast_return_1m_3m_5m",
                "macd_williams_confirmation": (etd_state.get("price_action_reversal") or {}).get("macd_williams_confirmation"),
            })
            if continuation_eval.get("action") == "ENTER" and desired_symbol:
                from app.trading.strategy_architecture import (
                    chase_hard_block,
                    entry_timing_ok,
                    episode_gate_blocks_entry,
                    get_episode_gate_mode,
                )

                _daily_ret = state.get("realized_pnl_today_pct")
                _gate_mode = get_episode_gate_mode(state)
                _ep_confirm = etd_state.get("macd_williams_episode") or {}
                _held_sec = (state.get("live_trade_direction") or {}).get("direction_held_seconds")
                _timing_ok, _timing_reason = entry_timing_ok(_held_sec)
                _sizing = _effective_target_pct_with_adaptive_cap(continuation_eval.get("target_pct"), state)
                try:
                    _calc_qty = int(
                        (_sizing["effective_target_pct"] * float(broker.get_buyable_cash() or 0.0))
                        // float(current_etf_price)
                    ) if current_etf_price else 0
                except Exception:
                    _calc_qty = 0
                _sizing["calculated_quantity"] = max(0, int(_calc_qty or 0))
                continuation_state["order_sizing_audit"] = _sizing

                def _mark_fast_entry_block(code: str) -> None:
                    nonlocal early_result
                    continuation_state["last_block_reason"] = code
                    early_result = {
                        "skipped": True,
                        "reason": code,
                        "reason_code": code,
                        "continuation": continuation_eval,
                        "order_sizing_audit": _sizing,
                        "order_permission": "BLOCKED",
                    }

                if _sizing.get("order_skip_reason") == "DATA_INSUFFICIENT_POSITION_CAP_ZERO":
                    # DATA_INSUFFICIENT policy: no exploratory live entry (cap=0).
                    _mark_fast_entry_block("DATA_INSUFFICIENT_POSITION_CAP_ZERO")
                elif daily_loss_limit_reached_from_pct(_daily_ret, _range_cfg):
                    _mark_fast_entry_block("DAILY_LOSS_LIMIT")
                elif chase_hard_block(moved_pct):
                    _mark_fast_entry_block("CHASE_BLOCK")
                elif not _timing_ok:
                    _mark_fast_entry_block(_timing_reason or "ENTRY_TIMING_BLOCK")
                elif episode_gate_blocks_entry(_gate_mode, _ep_confirm):
                    continuation_state["episode_gate_mode"] = _gate_mode
                    _mark_fast_entry_block("MACD_WILLIAMS_EPISODE_NOT_CONFIRMED")
                else:
                    held_symbol = (state.get("position") or {}).get("symbol")
                    _entry_path_for_key = continuation_eval.get("entry_path") or "CONTINUATION"
                    _episode_id_for_order = continuation_state.get("direction_episode_id") or f"{desired_live_direction}:{continuation_state.get('first_detected_at')}"
                    order_key = f"{_episode_id_for_order}:ENTRY"
                    _opposite_switch = bool(held_symbol and held_symbol != desired_symbol)
                    # Preserve whatever opposite-episode confirmation the surrounding
                    # Fast Worker tick already computed (_opposite_episode_confirmed).
                    _opposite_switch_allowed = (
                        live_trade.get("direction") == desired_live_direction
                        and _opposite_episode_confirmed
                    )
                    if _opposite_switch and not _opposite_switch_allowed:
                        _mark_fast_entry_block("OPPOSITE_EPISODE_NOT_CONFIRMED")
                    else:
                        _allows_entry, _entry_block = range_episode_allows_entry(
                            continuation_state,
                            entry_path=_entry_path_for_key,
                            swing_breakout=bool(confirm_swing_breakout),
                            vwap_reclaim=_vwap_reclaim,
                            direction_changed=_direction_episode_changed,
                        )
                        if not _allows_entry:
                            _mark_fast_entry_block(_entry_block or "RANGE_EPISODE_ENTRY_BLOCKED")
                        elif held_symbol == desired_symbol or continuation_state.get("entry_done") or continuation_state.get("last_order_key") == order_key:
                            _mark_fast_entry_block(
                                "TARGET_ALREADY_FILLED" if held_symbol == desired_symbol else "FAST_WORKER_ENTRY_ALREADY_ATTEMPTED"
                            )
                        else:
                            final_action = action_for_live_direction(desired_live_direction) or (
                                "HYNIX_BUY" if desired_live_direction == "UP" else "INVERSE_BUY"
                            )
                            _entry_audit = _weighted_entry_fusion_metadata(state, continuation_eval)
                            _entry_audit["direction_episode_id"] = _episode_id_for_order
                            _entry_audit["episode_id"] = _episode_id_for_order
                            _entry_audit["target_position_pct"] = _sizing["effective_target_pct"]
                            _entry_audit["position_cap"] = _sizing["position_cap"]
                            switch = run_switch_or_entry(
                                state, broker, final_action, long_price, inverse_price, now=now,
                                forced=True, reason=continuation_eval.get("reason_code") or "WEIGHTED_ORDER_CONTROLLER",
                                position_manager=position_manager, target_position_pct=_sizing["effective_target_pct"],
                                entry_type="WEIGHTED_RANGE_ENTRY",
                                signal_source="WEIGHTED_ORDER_CONTROLLER",
                                fusion_metadata=_entry_audit,
                            )
                            continuation_state["last_order_key"] = order_key
                            continuation_state["last_switch"] = switch
                            _sizing["calculated_quantity"] = int(switch.get("requested_qty") or _sizing.get("calculated_quantity") or 0)
                            if switch.get("failure_code") or switch.get("order_skip_reason"):
                                _sizing["order_skip_reason"] = switch.get("failure_code") or switch.get("order_skip_reason")
                            continuation_state["order_sizing_audit"] = _sizing
                            if _fast_order_succeeded(switch):
                                continuation_state["entry_done"] = True
                                continuation_state["entry_path"] = continuation_eval.get("entry_path")
                                continuation_state["last_entry_episode_id"] = _episode_id_for_order
                                continuation_state["approved_entry_count_today"] = int(continuation_state.get("approved_entry_count_today") or 0) + 1
                                mark_range_reversal_probe_entered(
                                    continuation_state,
                                    now=now,
                                    entry_path=continuation_eval.get("entry_path"),
                                )
                                position_manager.sync(force=True)
                                apply_position_manager_to_state(state, position_manager)
                                early_result = {
                                    "skipped": False,
                                    "reason_code": continuation_eval.get("reason_code"),
                                    "entry_path": continuation_eval.get("entry_path"),
                                    "switch": switch,
                                    "continuation": continuation_eval,
                                    "order_sizing_audit": _sizing,
                                }
                            else:
                                _fail = (
                                    switch.get("failure_code")
                                    or switch.get("order_skip_reason")
                                    or switch.get("message")
                                    or "FAST_WORKER_ORDER_NOT_SENT"
                                )
                                continuation_state["last_block_reason"] = str(_fail)
                                early_result = {
                                    "skipped": True,
                                    "reason": str(_fail),
                                    "reason_code": str(_fail),
                                    "switch": switch,
                                    "continuation": continuation_eval,
                                    "order_sizing_audit": _sizing,
                                    "order_permission": "BLOCKED",
                                }
            elif continuation_eval.get("action") == "ENTER" and not desired_symbol:
                continuation_state["last_block_reason"] = "NO_LIVE_DIRECTION_SYMBOL"
                early_result = {
                    "skipped": True,
                    "reason": "NO_LIVE_DIRECTION_SYMBOL",
                    "reason_code": "NO_LIVE_DIRECTION_SYMBOL",
                    "continuation": continuation_eval,
                    "order_permission": "BLOCKED",
                }
            _held_for_scale = (state.get("position") or {}).get("symbol")
            if (
                desired_symbol
                and _held_for_scale == desired_symbol
                and continuation_state.get("entry_done")
                and not continuation_state.get("scale_in_done")
            ):
                _episode_id_for_scale = continuation_state.get("direction_episode_id") or f"{desired_live_direction}:{continuation_state.get('first_detected_at')}"
                _macd_conf = (etd_state.get("price_action_reversal") or {}).get("macd_williams_confirmation") or {}
                try:
                    _confirm_elapsed = (
                        now - datetime.fromisoformat(continuation_state.get("first_detected_at") or now.isoformat())
                    ).total_seconds()
                except Exception:
                    _confirm_elapsed = None
                if _macd_conf.get("confirmed") and _confirm_elapsed is not None and 10.0 <= _confirm_elapsed <= 20.0:
                    _scale_target = _score_gap_dynamic_pct(
                        score_gap=float(continuation_state.get("score_gap") or continuation_eval.get("score_gap") or 30.0),
                        low=0.40,
                        high=0.60,
                        confidence=(state.get("adaptive_regime") or {}).get("confidence"),
                        stop_loss_distance_pct=abs(float(etd.FIXED_EARLY_STOP_PCT)),
                        buyable_cash=broker.get_buyable_cash() if hasattr(broker, "get_buyable_cash") else None,
                        current_price=current_etf_price,
                    )
                    _scale_key = f"{_episode_id_for_scale}:SCALE_IN"
                    if _scale_target > 0 and continuation_state.get("last_order_key") != _scale_key:
                        final_action = action_for_live_direction(desired_live_direction) or ("HYNIX_BUY" if desired_live_direction == "UP" else "INVERSE_BUY")
                        _scale_audit = _weighted_entry_fusion_metadata(state, continuation_eval)
                        _scale_audit["direction_episode_id"] = _episode_id_for_scale
                        _scale_audit["episode_id"] = _episode_id_for_scale
                        _scale_audit["entry_path"] = continuation_state.get("entry_path") or _scale_audit.get("entry_path")
                        _scale_audit["target_position_pct"] = _scale_target
                        switch = run_switch_or_entry(
                            state, broker, final_action, long_price, inverse_price, now=now,
                            forced=True, reason="MACD_WILLIAMS_CONFIRMED_SCALE_IN",
                            position_manager=position_manager, target_position_pct=_scale_target,
                            entry_type="WEIGHTED_ORDER_CONTROLLER_SCALE_IN",
                            signal_source="WEIGHTED_ORDER_CONTROLLER",
                            fusion_metadata=_scale_audit,
                        )
                        continuation_state["last_order_key"] = _scale_key
                        continuation_state["last_scale_switch"] = switch
                        if _fast_order_succeeded(switch):
                            continuation_state["scale_in_done"] = True
                            position_manager.sync(force=True)
                            apply_position_manager_to_state(state, position_manager)
                            early_result = {
                                "skipped": False,
                                "reason_code": "MACD_WILLIAMS_SCALE_IN",
                                "entry_path": continuation_state.get("entry_path"),
                                "switch": switch,
                                "continuation": continuation_eval,
                            }
            state["trend_continuation_entry"] = continuation_state
            _update_fast_worker_decision_snapshot(state, now=now, continuation_state=continuation_state, early_result=early_result)
        except Exception as exc:
            logger.error("[EarlyTrendFastFeed] tick 실패: %s", exc)
            save_state_atomic(state)
            return {"skipped": True, "reason": f"tick failed: {exc}", "live_slopes": etd_state["live_slopes"]}
        finally:
            _EARLY_ORDER_LOCK.release()

        save_state_atomic(state)
        return {"skipped": False, "early_result": early_result, "live_slopes": etd_state["live_slopes"]}


def run_fast_trend_watcher_tick(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """30s fast live-trend watcher entry point.

    It only uses live Hynix minute trend for direction flips. The 3-minute AI
    cycle remains the broader confirmation/diagnostic path.
    """
    from app.models.hynix_enhanced_score import calculate_enhanced_hynix_prediction_score
    from app.models.hynix_action_decider import decide_hynix_or_inverse_action
    from app.services.hynix_switch_state import with_state_lock

    now = now or kst_now()
    resolved_mode = mode or load_state(mode=None).get("mode", "mock")
    with with_state_lock(resolved_mode):
        state = load_state(mode=resolved_mode)
        state["mode"] = resolved_mode
        if not state.get("auto_trade_on") or state.get("stopped"):
            return {"skipped": True, "reason": "auto off or stopped"}
        if not is_new_entry_allowed(now):
            _window_reason = describe_new_entry_window(now)["rule"]
            status = state.get("fast_trend_watcher") or {}
            status.update({"last_checked_at": now.isoformat(), "blocked_reason": _window_reason})
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": _window_reason, "state": state}

        enhanced_result = calculate_enhanced_hynix_prediction_score(mode=resolved_mode)
        fast_decision = decide_hynix_or_inverse_action(enhanced_result, current_position=state.get("current_position"))
        state["last_enhanced_result"] = enhanced_result
        state["last_decision"] = fast_decision
        df_1min = (enhanced_result.get("market_data") or {}).get("hynix_minute", {}).get("df_1min")
        fast_signal = compute_fast_trend_signal(df_1min, now=now)
        primary_trend_result = compute_primary_trend(
            df_1min, prev_close=enhanced_result.get("hynix_prev_close"), now=now,
        )
        primary_trend = primary_trend_result.get("primary_trend")
        state["last_primary_trend"] = primary_trend_result
        status = dict(state.get("fast_trend_watcher") or {})

        # 요구사항: fast confirmed direction(candidate_direction/confirmation_count)은
        # 날짜(위 auto off/rollover 분기에서 이미 처리) 뿐 아니라 계좌/보유수량이 바뀌면도
        # 초기화해야 한다 — 이전 사이클과 보유 상태가 달라졌다면 그 사이의 연속 확인
        # 횟수는 지금의 포지션에 대해 더 이상 유효하지 않다.
        position_now = state.get("position") or {}
        position_signature = f"{resolved_mode}:{position_now.get('symbol')}:{position_now.get('quantity') or 0}"
        if status.get("position_signature") not in (None, position_signature):
            status["candidate_direction"] = None
            status["confirmation_count"] = 0
        status["position_signature"] = position_signature

        direction = fast_signal.get("direction")
        if direction in ("UP", "DOWN"):
            if status.get("candidate_direction") == direction:
                status["confirmation_count"] = int(status.get("confirmation_count", 0)) + 1
            else:
                status["candidate_direction"] = direction
                status["confirmation_count"] = 1
        else:
            status["confirmation_count"] = 0
        status.update({
            "direction": direction,
            "last_signal": fast_signal,
            "last_enhanced_decision": fast_decision,
            "last_checked_at": now.isoformat(timespec="seconds"),
            "actual_order_driver": "WEIGHTED_ORDER_CONTROLLER_LIVE",
            "primary_trend": primary_trend,
            "blocked_reason": "WEIGHTED_ORDER_CONTROLLER owns new entries; 30s watcher is diagnostics-only",
        })
        state["fast_trend_watcher"] = status
        state["weighted_entry_controller_only"] = True
        state["configured_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
        state["actual_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
        save_state_atomic(state)
        return {
            "skipped": True,
            "reason": status["blocked_reason"],
            "fast_signal": fast_signal,
            "primary_trend": primary_trend_result,
            "state": state,
        }

        # ── Early Trend Detector(요구사항, 토글 기본 OFF) ───────────────────
        # confirmed_regime 기반으로 RANGE 외 장세에서도 제한적 탐색진입을
        # 판단해야 하므로, 아래의 "PRIMARY_TREND==RANGE만 허용" 레거시 게이트보다
        # 먼저 평가한다. 실패해도(예: 브로커 오류) 레거시 로직은 그대로 이어진다.
        if state.get("early_trend_detector_enabled"):
            try:
                from app.trading.adaptive_market_regime import compute_and_confirm_regime

                _etd_adaptive = compute_and_confirm_regime(
                    df_1min, prev_close=enhanced_result.get("hynix_prev_close"),
                    confirmation_state=state.get("adaptive_regime_confirmation"), now=now,
                )
                state["adaptive_regime_confirmation"] = _etd_adaptive["confirmation_state"]
                state["adaptive_regime"] = {k: v for k, v in _etd_adaptive.items() if k != "confirmation_state"}
                _etd_confirmed_regime = _etd_adaptive.get("confirmed_regime")

                if state.get("early_trend_detector_live"):
                    _etd_state = dict(state.get("early_trend_detector") or {})
                    _etd_state["order_worker_name"] = "EARLY_FAST_WORKER"
                    _etd_state["main_cycle_waiting"] = False
                    _etd_state["last_main_cycle_note"] = "Early LIVE 신규진입은 5초 Fast Worker가 직접 실행"
                    state["early_trend_detector"] = _etd_state
                    status["actual_order_driver"] = "EARLY_FAST_WORKER"
                    status["blocked_reason"] = "Early LIVE entries handled by 5s Fast Worker"
                    state["fast_trend_watcher"] = status
                    save_state_atomic(state)
                    return {
                        "skipped": True,
                        "reason": status["blocked_reason"],
                        "fast_signal": fast_signal,
                        "state": state,
                    }

                from app.config import get_config
                from app.trading.broker_factory import create_broker

                cfg = get_config()
                _etd_broker = _create_strategy_broker(cfg, resolved_mode)
                _etd_position_manager = HynixPositionManager(_etd_broker, mode=resolved_mode)
                _etd_position_manager.sync(force=True)
                apply_position_manager_to_state(state, _etd_position_manager)

                from app.data_sources.hynix_long_collector import collect_long_current
                from app.data_sources.hynix_inverse_collector import collect_inverse_current

                _etd_long_quote = collect_long_current(mode=resolved_mode)
                _etd_inverse_quote = collect_inverse_current(mode=resolved_mode)

                early_result = _run_early_trend_detector_tick(
                    state=state, mode=resolved_mode, now=now, fast_signal=fast_signal, df_1min=df_1min,
                    confirmed_regime=_etd_confirmed_regime, broker=_etd_broker, position_manager=_etd_position_manager,
                    hynix_price=_etd_long_quote.get("current_price"), inverse_price=_etd_inverse_quote.get("current_price"),
                    live_slopes=(state.get("early_trend_detector") or {}).get("live_slopes"),
                )
                if early_result is not None:
                    save_state_atomic(state)
                    return early_result
            except Exception as exc:
                logger.debug("[EarlyTrendDetector] tick failed (harmless — legacy fast watcher continues): %s", exc)

        # 요구사항: Fast Watcher의 빠른 스위칭은 PRIMARY_TREND가 RANGE일 때만 허용한다.
        # UP/DOWN 추세 중에는 1·3·5분 신호가 그 추세에 대한 PULLBACK일 수 있으므로,
        # 이 30초 워처가 단독으로 전환하지 않는다 — 실제 추세반전은 update_reversal_confirmation
        # (VWAP 이탈 + 15분 추세 + 주요 저점/고점 붕괴, 2회 연속 확인)이 별도로 담당한다.
        if primary_trend != PRIMARY_TREND_RANGE:
            move_kind = classify_short_term_move(primary_trend, direction)
            status["blocked_reason"] = (
                f"PRIMARY_TREND={primary_trend} - fast rapid switching only runs in RANGE "
                f"(this move classified as {move_kind})"
            )
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "primary_trend": primary_trend_result, "state": state}

        if direction not in ("UP", "DOWN") or int(status.get("confirmation_count", 0)) < 2:
            save_state_atomic(state)
            return {"skipped": True, "reason": "fast trend not confirmed", "fast_signal": fast_signal, "state": state}

        final_action = "HYNIX_BUY" if direction == "UP" else "INVERSE_BUY"
        desired_symbol = _ACTION_TO_SYMBOL.get(final_action)
        held_symbol = (state.get("position") or {}).get("symbol")
        if desired_symbol == held_symbol:
            status["blocked_reason"] = "already holding confirmed fast direction"
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}

        idempotency_key = f"FAST:{direction}:{now.strftime('%Y%m%d%H%M')}"
        previous_fast_execution = ((state.get("fast_trend_watcher") or {}).get("last_execution") or {})
        previous_orders = previous_fast_execution.get("orders") or []
        previous_had_success_order = any(bool(o.get("success")) for o in previous_orders if isinstance(o, dict))
        if state.get("last_execution_idempotency_key") == idempotency_key and previous_had_success_order:
            status["blocked_reason"] = "duplicate fast watcher idempotency key"
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}
        _is_reversal_vs_holding = bool(held_symbol and desired_symbol != held_symbol)
        # 요구사항8 — same_direction_streak(연속 같은 방향 확인 횟수)과 reversal_streak
        # (현재 보유와 반대 방향 전환이 확인된 횟수)은 서로 다른 개념이다. Fast Watcher의
        # confirmation_count는 "같은 방향이 몇 틱 연속 확인됐는지"만 측정하므로,
        # 실제로 보유 종목과 반대 방향으로 전환하는 상황(_is_reversal_vs_holding)이
        # 아니면 reversal_streak은 0이어야 한다 — 과거에는 두 필드에 항상 같은 값을
        # 넣어 "그냥 같은 방향 유지"도 "반전 확인"처럼 보이게 했다.
        _confirmation_count = int(status.get("confirmation_count", 0))
        state["last_trend_switch_plan"] = {
            "proceed": True,
            "position_pct": 0.20,
            "entry_type": "EXPLORATORY",
            "immediate_switch": _is_reversal_vs_holding,
            "dominant_direction": "HYNIX" if direction == "UP" else "INVERSE",
            "desired_symbol": desired_symbol,
            "same_direction_streak": _confirmation_count,
            "reversal_streak": _confirmation_count if _is_reversal_vs_holding else 0,
            "pullback_wait_remaining_seconds": 0,
            "block_reason": None,
            "source": "FAST_TREND_WATCHER",
        }

        try:
            from app.config import get_config

            cfg = get_config()
            broker = _create_strategy_broker(cfg, resolved_mode)
            position_manager = HynixPositionManager(broker, mode=resolved_mode)
            position_manager.sync(force=True)
            apply_position_manager_to_state(state, position_manager)
            # 요구사항(2026-07-15) — 실행가는 000660이 아니라 LONG_SYMBOL(0193T0)의
            # 실제 현재가여야 한다(3분 사이클과 동일 원칙).
            from app.data_sources.hynix_long_collector import collect_long_current

            _fast_long_quote = collect_long_current(mode=resolved_mode)
            _fast_long_price = _fast_long_quote.get("current_price")
            if not _fast_long_price:
                status["blocked_reason"] = f"LONG_SYMBOL(0193T0) 현재가 조회 실패: {_fast_long_quote.get('error')}"
                state["fast_trend_watcher"] = status
                save_state_atomic(state)
                return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}
            if _is_reversal_vs_holding:
                # 요구사항7 — Fast Watcher는 REVERSAL_CANDIDATE만 만들고 단독으로 반대
                # ETF 전액 주문을 넣지 못한다. 최종 주문권한은 공용 반전 스위칭
                # 상태머신(run_reversal_switch_if_needed)만 갖는다 — run_switch_or_entry
                # (즉시 전량 스위칭)는 호출하지 않는다.
                #
                # 요구사항1 — 장세를 하루 고정값으로 쓰지 않고 20~30초마다 재평가한다.
                # 메인 3분 사이클과 동일한 confirmation_state 키(state["adaptive_regime_
                # confirmation"])를 공유해(별도 재분류가 아니라 같은 단일 소스를 더 빠른
                # 주기로 갱신) Fast Watcher의 30초 틱마다 함께 갱신한다.
                from app.trading.adaptive_market_regime import compute_and_confirm_regime

                _fast_adaptive_result = compute_and_confirm_regime(
                    df_1min, prev_close=enhanced_result.get("hynix_prev_close"),
                    confirmation_state=state.get("adaptive_regime_confirmation"), now=now,
                )
                state["adaptive_regime_confirmation"] = _fast_adaptive_result["confirmation_state"]
                state["adaptive_regime"] = {k: v for k, v in _fast_adaptive_result.items() if k != "confirmation_state"}
                _previous_confirmed_regime = _fast_adaptive_result.get("previous_regime")
                _current_confirmed_regime = _fast_adaptive_result.get("confirmed_regime")
                switch = run_reversal_switch_if_needed(
                    state, broker, _fast_long_price, enhanced_result.get("inverse_current_price"),
                    now=now, position_manager=position_manager,
                    hard_stop_triggered=(_current_confirmed_regime == "PANIC"),
                    regime_downgraded_to_range=(
                        _previous_confirmed_regime in ("STRONG_UP", "STRONG_DOWN")
                        and _current_confirmed_regime == "RANGE"
                    ),
                    snapshot=_fast_adaptive_result.get("snapshot"),
                    previous_regime=_previous_confirmed_regime, current_regime=_current_confirmed_regime,
                    allow_final_actions=False,
                )
            else:
                switch = run_switch_or_entry(
                    state, broker, final_action,
                    _fast_long_price, enhanced_result.get("inverse_current_price"),
                    now=now, forced=True, reason="FAST_TREND_WATCHER 2x confirmed",
                    position_manager=position_manager, target_position_pct=0.20, entry_type="EXPLORATORY",
                )
            status["last_execution"] = {
                "idempotency_key": idempotency_key,
                "final_action": final_action,
                "result_message": switch.get("message"),
                "orders": switch.get("orders", []),
            }
            if any(bool(o.get("success")) for o in (switch.get("orders") or []) if isinstance(o, dict)):
                state["last_execution_idempotency_key"] = idempotency_key
            elif state.get("last_execution_idempotency_key") == idempotency_key:
                state["last_execution_idempotency_key"] = None
            position_manager.sync(force=True)
            apply_position_manager_to_state(state, position_manager)
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            return {"skipped": False, "fast_signal": fast_signal, "switch": switch, "state": state}
        except Exception as exc:
            status["blocked_reason"] = f"fast watcher execution failed: {exc}"
            state["fast_trend_watcher"] = status
            save_state_atomic(state)
            logger.error("[FastTrendWatcher] execution failed: %s", exc)
            return {"skipped": True, "reason": status["blocked_reason"], "fast_signal": fast_signal, "state": state}


def _update_hynix_auto_trade_loop_locked(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """1회 실행 사이클의 실제 구현(반드시 with_state_lock(mode) 안에서만 호출).

    `now`는 테스트에서 시각을 주입하기 위한 선택 인자이며, 운영 시에는 항상 현재시각이 쓰인다.
    """
    warnings: list[str] = []
    now = now or kst_now()
    state = load_state(mode=mode)
    mode = mode or state.get("mode", "mock")
    state["mode"] = mode
    state["actual_order_driver"] = "ENHANCED_REGIME_SWITCH"

    if state.get("stopped"):
        trace = _blank_pipeline_trace()
        trace["risk_manager_ok"] = False
        trace["risk_manager_reason"] = state.get("stopped_reason") or "자동매매 정지 상태"
        trace["risk_approved"] = False
        trace["stopped_stage"] = "risk_manager"
        trace["blocking_reason"] = f"[risk_manager] {trace['risk_manager_reason']}"
        return {
            "skipped": True, "reason": state.get("stopped_reason") or "자동매매 정지 상태", "state": state,
            "pipeline_trace": trace,
        }

    trace = _blank_pipeline_trace()

    # ── ①~⑥ 점수/판단 계산 (기존 데이터 흐름 재사용) ────────────────────────
    try:
        from app.models.hynix_enhanced_score import calculate_enhanced_hynix_prediction_score

        enhanced_result = calculate_enhanced_hynix_prediction_score(mode=mode)
    except Exception as exc:
        logger.error("[HynixSwitchEngine] enhanced_score 계산 실패: %s", exc)
        warnings.append(f"enhanced_score 계산 실패: {exc}")
        enhanced_result = {
            "base_prediction_score": 50.0, "existing_micron_score": 50.0,
            "hynix_technical_score": 50.0, "intraday_momentum_score": 50.0,
            "inverse_pressure_score": 50.0, "enhanced_score": 50.0,
            "reason_top5": [], "data_valid": {"base_prediction": False, "hynix_technical": False},
            "hynix_current_price": None, "inverse_current_price": None, "warnings": [str(exc)],
        }

    try:
        from app.models.hynix_action_decider import decide_hynix_or_inverse_action

        decision = decide_hynix_or_inverse_action(enhanced_result, current_position=state.get("position"))
    except Exception as exc:
        logger.error("[HynixSwitchEngine] action_decider 실패: %s", exc)
        warnings.append(f"action_decider 실패: {exc}")
        decision = {"final_action": "HOLD", "enhanced_score": enhanced_result.get("enhanced_score", 50.0),
                    "inverse_pressure_score": enhanced_result.get("inverse_pressure_score", 50.0),
                    "score_gap": 0.0, "score_gap_below_forced_trade_threshold": True, "reasons": [str(exc)]}

    trace["prediction_signal"] = _map_prediction_signal(decision.get("final_action", "HOLD"))

    hynix_signal_price = enhanced_result.get("hynix_current_price")
    inverse_price = enhanced_result.get("inverse_current_price")
    df_1min = (enhanced_result.get("market_data") or {}).get("hynix_minute", {}).get("df_1min")

    # 요구사항(2026-07-16) — ADAPTIVE_MARKET_REGIME을 신규진입/스위칭/손절/익절/
    # 보유시간이 전부 공유하는 단일 계산 지점으로 만든다. Enhanced 자동매매가
    # ON이면(auto_trade_on) 별도 수동 체크박스 없이 이 결과가 곧바로 LIVE로
    # 적용되고, OFF면 SHADOW(계산·표시만, 주문에 미반영)로 남는다.
    try:
        from app.trading.adaptive_market_regime import compute_and_confirm_regime

        adaptive_regime_result = compute_and_confirm_regime(
            df_1min, prev_close=enhanced_result.get("hynix_prev_close"),
            confirmation_state=state.get("adaptive_regime_confirmation"), now=now,
        )
        state["adaptive_regime_confirmation"] = adaptive_regime_result["confirmation_state"]
        state["adaptive_regime"] = {k: v for k, v in adaptive_regime_result.items() if k != "confirmation_state"}
    except Exception as exc:
        logger.error("[HynixSwitchEngine] adaptive_regime 계산 실패: %s", exc)
        warnings.append(f"adaptive_regime 계산 실패: {exc}")
    state["adaptive_regime_enabled"] = bool(state.get("auto_trade_on"))
    state["adaptive_regime_mode"] = "LIVE" if state["adaptive_regime_enabled"] else "SHADOW"
    decision = _downgrade_unconfirmed_strong_decision(decision, state)
    trace["prediction_signal"] = _map_prediction_signal(decision.get("final_action", "HOLD"))

    # ── SHADOW MODE: Cycle Detector AI + Prediction AI V2(BUY/SELL/HOLD 확률) ──
    # 아래 호출은 `decision`/실제 주문에 절대 영향을 주지 않는다 — 계산·로그·state 저장만
    # 수행하며, 예외가 나도 무해하게 삼켜진다. 실제 주문 연결은 별도 승인 후 진행한다.
    # 신호 계산 참고용이므로 여기서는 000660 가격(hynix_signal_price)을 그대로 쓴다.
    _run_shadow_cycle_ai_and_decision_v2(state, enhanced_result, decision, df_1min, hynix_signal_price, inverse_price, now)

    # 요구사항(2026-07-15) — 실제 매매 종목은 000660이 아니라 LONG_SYMBOL(0193T0)이다.
    # 이 지점부터 `hynix_price`는 000660이 아니라 0193T0의 실제 현재가여야 한다 —
    # run_switch_or_entry/run_tp_sl_if_needed/run_liquidation_if_needed/evaluate_pullback_gate
    # 등 실행 계층에 넘기는 값이 그대로 주문가격·손익 계산 기준이 되기 때문이다.
    try:
        from app.data_sources.hynix_long_collector import collect_long_current

        _long_quote = collect_long_current(mode=mode)
        hynix_price = _long_quote.get("current_price")
        if _long_quote.get("stale"):
            warnings.append(f"LONG_SYMBOL(0193T0) 현재가가 최신이 아닐 수 있음: {_long_quote.get('error')}")
    except Exception as exc:
        logger.error("[HynixSwitchEngine] LONG_SYMBOL(0193T0) 현재가 조회 실패: %s", exc)
        warnings.append(f"LONG_SYMBOL(0193T0) 현재가 조회 실패: {exc}")
        hynix_price = None

    signal_data_ok = bool((enhanced_result.get("data_valid") or {}).get("hynix_signal_price", True))
    price_data_ok = hynix_price is not None and signal_data_ok
    order_api_ok = True
    broker = None
    real_gate_ok = True
    real_gate_status = None

    auto_trade_on = bool(state.get("auto_trade_on"))
    position_manager = None
    if auto_trade_on:
        try:
            from app.config import get_config

            cfg = get_config()
            if mode == "real":
                real_gate_status = (
                    cfg.enhanced_real_gate_status(current_mode=mode)
                    if hasattr(cfg, "enhanced_real_gate_status")
                    else {"ready": cfg.full_auto_real_confirm_ok(), "blocking_reasons": [], "checks": {}}
                )
                real_gate_ok = bool(real_gate_status.get("ready"))
                trace["real_gate_status"] = real_gate_status
                if not real_gate_ok:
                    warnings.append(
                        "REAL 완전자동 게이트 미충족: "
                        + ", ".join(real_gate_status.get("blocking_reasons") or ["UNKNOWN"])
                        + " — 주문 실행 생략"
                    )
            if real_gate_ok:
                broker = _create_strategy_broker(cfg, mode)

            if broker is not None:
                # Broker가 유일한 Source of Truth — position_manager.sync()로 실제 포지션을
                # 먼저 확정하고, state는 그 결과를 담는 캐시로만 갱신한다.
                position_manager = HynixPositionManager(broker, mode=mode)
                position_manager.sync(force=True)
                apply_position_manager_to_state(state, position_manager)
                if state.get("position_conflict"):
                    warnings.append(state.get("critical_alert") or "0193T0/0197X0 동시 보유 감지 — 신규매수 금지")
        except Exception as exc:
            order_api_ok = False
            warnings.append(f"브로커 초기화 실패: {exc}")
            logger.error("[HynixSwitchEngine] 브로커 초기화 실패: %s", exc)

    total_equity = None
    daily_pnl_pct = None
    daily_return_blocked_this_cycle = False
    net_return_result = None
    if broker is not None:
        try:
            net_return_result = _compute_net_daily_return_with_retries(
                state, broker, state.get("position"), hynix_price, inverse_price, now,
            )
            snapshot = net_return_result.get("account_snapshot") or {}
            positions = snapshot.get("positions") or []
            cash = snapshot.get("cash")
            total_equity = snapshot.get("current_equity")
            is_mock_override = mode == "mock" and state.get("allow_mock_loss_override")

            # 요구사항 1/5절 — risk_manager와 UI가 동일한 값(원장 실현손익 + 미실현손익
            # / 시작자산)을 쓰도록 통합한다. get_buyable_cash()는 KIS API 오류(예: "1분당
            # 1회" 토큰발급 레이트리밋)가 나도 예외 없이 0.0을 반환하는 하위호환 계약이
            # 있어(KisMockBroker.get_orderable_cash), 그 값을 곧바로 "계좌가 0원이 됐다"로
            # 써버리면 일일 손실이 -100%로 오판된다(2026-07-14 실측). 새 계산식은 실현손익을
            # 원장(state)에서, 미실현손익을 보유 포지션+현재가에서 가져오므로 이 계좌조회
            # 글리치의 영향을 받지 않는다 — cash/positions는 교차검증에만 쓰인다.
            state["daily_return_calculation"] = {k: v for k, v in net_return_result.items()}
            daily_pnl_pct = net_return_result["net_daily_return"]

            if net_return_result["blocked_reason"]:
                daily_return_blocked_this_cycle = True
                warnings.append(
                    f"일일손익 판정 보류({net_return_result['blocked_reason']}) — "
                    f"신규주문만 일시 보류(기존 정지상태는 변경하지 않음)"
                )
                logger.warning(
                    "[HynixSwitchEngine] %s — 손익 판정 보류, -100%% 등으로 기록하지 않음",
                    net_return_result["blocked_reason"],
                )
            else:
                state["total_equity"] = total_equity
                # 요구사항(2026-07-20) — 이번 사이클에 기준자산이 방금 (재)확정됐다면
                # (baseline_just_established), 그 즉시 계산된 수익률만으로 자동매매를
                # 강제 중단하지 않는다 — 아직 가격/잔고 데이터가 안정적인지 한 번도
                # 확인되지 않은 첫 표본이다. 다음 사이클부터는 이미 확정된(같은)
                # 기준자산으로 다시 계산되므로, 진짜 -2.5% 이하 손실이면 그때 정상
                # 차단된다.
                if net_return_result.get("baseline_just_established"):
                    pass
                elif daily_pnl_pct is not None and daily_pnl_pct <= -2.5 and mode == "real":
                    state["stopped"] = True
                    state["stopped_reason"] = f"일 누적 손실 {daily_pnl_pct:.2f}% ≤ -2.5% — REAL 자동매매 강제 중단"
                    logger.error(state["stopped_reason"])
                elif daily_pnl_pct is not None and daily_pnl_pct <= -2.5 and mode == "mock" and not is_mock_override:
                    state["stopped"] = True
                    state["stopped_reason"] = f"일 누적 손실 {daily_pnl_pct:.2f}% ≤ -2.5% — MOCK 자동매매 중단(설정에서 계속 테스트 가능)"
        except Exception as exc:
            order_api_ok = False
            warnings.append(f"계좌 조회 실패: {exc}")

    fired_windows = state.get("fired_windows", [])
    forced_info = should_force_trade(
        decision, fired_windows, price_data_ok, order_api_ok, df_1min, daily_pnl_pct, now=now,
        override_daily_loss_block=bool(state.get("daily_loss_block_override")),
    )

    liquidation_phase_now = get_liquidation_phase(now)

    orders_this_cycle: list = []
    attempted_entry = False
    # 요구사항(2026-07-20) — 신규진입 시간창(is_new_entry_allowed)은 더 이상 이
    # 플래그에 포함하지 않는다. trading_allowed는 이제 "기존 포지션 손절/익절/
    # 반전청산/15:15 강제청산을 이번 사이클에 시도해도 되는가"만 뜻하며, 신규
    # 진입 가능 여부는 아래 new_entry_allowed_now로 별도 판단한다 — 그래야 신규
    # 진입 금지 구간(14:50 이후 등)에도 보유 포지션 청산은 정상 실행된다.
    trading_allowed = (
        auto_trade_on and real_gate_ok and not state.get("stopped")
        and not daily_return_blocked_this_cycle and broker is not None
        and not state.get("position_sync_block_new_orders")
    )
    new_entry_window = describe_new_entry_window(now)
    # 요구사항(2026-07-21 실운영 검증) — Render 배포 SHA가 origin/main과 어긋나면
    # (자동배포 지연/실패 등) 실제 운영 중인 코드가 방금 검증·푸시한 코드와 다를 수
    # 있으므로, 그 불일치가 해소되기 전까지는 신규진입만 차단한다(기존 포지션
    # 손절/익절/청산은 계속 정상 실행 — 안전을 위해 막지 않는다). runtime_info는
    # app/ui/streamlit_app.py 시작 시 기록되며, "SHA Match" UI 지표와 같은 값이다.
    _runtime_info = read_runtime_info()
    if not _runtime_info.get("orders_enabled_by_deployment", True):
        new_entry_window = {
            **new_entry_window,
            "allowed": False,
            "rule": (
                f"DEPLOYMENT_SHA_MISMATCH(local={_runtime_info.get('git_sha')}, "
                f"origin={_runtime_info.get('origin_main_sha')}, render={_runtime_info.get('render_sha')}) "
                "— 배포 SHA 불일치로 신규진입 차단"
            ),
        }
    new_entry_allowed_now = new_entry_window["allowed"]
    _early_configured = bool(state.get("early_trend_detector_enabled"))
    _early_live = bool(_early_configured and state.get("early_trend_detector_live"))
    state["configured_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
    state["actual_entry_engine"] = "WEIGHTED_ORDER_CONTROLLER_LIVE"
    state["entry_orchestrator"] = {
        "name": "OrderCoordinator",
        "mode": "WEIGHTED_ORDER_CONTROLLER_LIVE",
        "reason": (
            "evaluate_range_weighted_entry owns all live new entries; "
            f"Early={'LIVE_INPUT' if _early_live else ('SHADOW_INPUT' if _early_configured else 'OFF')}"
        ),
        "updated_at": now.isoformat(),
    }
    state["weighted_entry_controller_only"] = True

    if not trading_allowed:
        trace["risk_manager_ok"] = False
        if state.get("stopped"):
            trace["risk_manager_reason"] = state.get("stopped_reason") or "자동매매 중단 상태"
        elif daily_return_blocked_this_cycle:
            blocked_reason = (net_return_result or {}).get("blocked_reason") or DAILY_RETURN_UNKNOWN
            trace["risk_manager_reason"] = f"{blocked_reason} — 계좌 데이터 이상으로 이번 사이클 신규주문 일시 보류"
        elif state.get("position_sync_block_new_orders"):
            trace["risk_manager_reason"] = state.get("critical_alert") or "POSITION_SYNC_PENDING"
        elif not auto_trade_on:
            trace["risk_manager_reason"] = "자동매매 OFF"
        elif not real_gate_ok:
            if real_gate_status:
                trace["risk_manager_reason"] = (
                    "REAL_GATE_NOT_READY: "
                    + ", ".join(real_gate_status.get("blocking_reasons") or ["UNKNOWN"])
                )
            else:
                trace["risk_manager_reason"] = "REAL_GATE_NOT_READY"
        elif broker is None:
            trace["risk_manager_reason"] = "브로커 초기화 실패"
    elif state.get("position_conflict"):
        trace["risk_manager_ok"] = False
        trace["risk_manager_reason"] = state.get("critical_alert") or "0193T0/0197X0 동시 보유 — 포지션 동기화 필요"

    if not new_entry_allowed_now:
        warnings.append(f"신규진입 시간창: {new_entry_window['rule']}")

    if trading_allowed:
        try:
            liq = run_liquidation_if_needed(now, state, broker, hynix_price, inverse_price, position_manager=position_manager)
            orders_this_cycle.extend(liq.get("orders", []))
        except Exception as exc:
            logger.error("[HynixSwitchEngine] 강제청산 처리 실패: %s", exc)
            warnings.append(f"강제청산 처리 실패: {exc}")
            liq = {"liquidated": False}

        if liquidation_phase_now == "closed" and not liq.get("liquidated"):
            warnings.append("15:20 이후 — 신규 주문 판단 없이 상태 정리만 수행")
        elif not liq.get("liquidated"):
            try:
                tp_sl = run_tp_sl_if_needed(state, broker, hynix_price, inverse_price, position_manager=position_manager, now=now)
                orders_this_cycle.extend(tp_sl.get("orders", []))
            except Exception as exc:
                logger.error("[HynixSwitchEngine] TP/SL 처리 실패: %s", exc)
                warnings.append(f"TP/SL 처리 실패: {exc}")
                tp_sl = {"triggered": False}

            # 요구사항(2026-07-16) — 장중 다중 추세전환 상태머신: STRONG_TREND 보유 중
            # 반전 신호가 쌓이면 TP/SL(가격 기준)과는 별개로 선제 축소·전량청산·
            # (브로커 확인 후) 반대 ETF 탐색진입·확대를 단계적으로 실행한다. 이번
            # 사이클에 이미 한 번만 계산된 공용 state["adaptive_regime"] 결과(snapshot/
            # confirmed_regime/previous_regime)만 사용한다 — 별도 재분류하지 않는다.
            try:
                _ar = state.get("adaptive_regime") or {}
                _current_confirmed_regime = _ar.get("confirmed_regime")
                _previous_confirmed_regime = _ar.get("previous_regime")
                _regime_downgraded_to_range = (
                    _previous_confirmed_regime in ("STRONG_UP", "STRONG_DOWN")
                    and _current_confirmed_regime == "RANGE"
                )
                reversal_switch_result = run_reversal_switch_if_needed(
                    state, broker, hynix_price, inverse_price, now=now, position_manager=position_manager,
                    hard_stop_triggered=(_current_confirmed_regime == "PANIC"),
                    regime_downgraded_to_range=_regime_downgraded_to_range,
                    snapshot=_ar.get("snapshot"),
                    previous_regime=_previous_confirmed_regime, current_regime=_current_confirmed_regime,
                )
                orders_this_cycle.extend(reversal_switch_result.get("orders", []))
            except Exception as exc:
                logger.error("[HynixSwitchEngine] 반전 스위칭 처리 실패: %s", exc)
                warnings.append(f"반전 스위칭 처리 실패: {exc}")

            if not signal_data_ok:
                trace["entry_approved"] = False
                trace["entry_approved_reason"] = "DATA_UNIT_MISMATCH/DATA_ERROR — 000660 신호가격 검증 실패로 신규 진입 차단"
                warnings.append(trace["entry_approved_reason"])
            elif not tp_sl.get("triggered"):
                # 요구사항(2026-07-16 실측) — active_strategy_enabled가 켜져 있으면 이
                # 자리가 예전엔 `elif`로 아래 ENHANCED_REGIME_SWITCH 실제 판단과 배타적
                # 이었다. _run_active_strategy_entry/_run_adaptive_fusion_entry는 맨
                # 위에서 무조건 "acted": False(SHADOW_ONLY)만 반환하도록 이미 비활성화돼
                # 있는데(그 아래 실제 매수/매도 코드는 도달 불가능한 죽은 코드), 이
                # 토글이 켜져 있으면 그 사이클에 실제 엔진(ENHANCED_REGIME_SWITCH)이
                # 아예 실행되지 않아 BUY/INVERSE 신호가 나도 주문이 전혀 들어가지
                # 않았다 — "ACTIVE_STRATEGY shadow-only: actual broker orders are owned
                # by ENHANCED_REGIME_SWITCH"라는, 마치 다른 엔진이 대신 처리한 것처럼
                # 보이는 메시지만 남고 사실은 아무도 처리하지 않았다. 진단용 shadow
                # 계산(state["last_final_execution_decision"] 등 UI 표시용)은 그대로
                # 유지하되, 더 이상 아래 실제 진입 로직을 막지 않는다 — 항상 실행한다.
                if state.get("active_strategy_enabled"):
                    try:
                        # 요구사항(2026-07-21) — inverse_pressure_score로 enhanced_ai_score를
                        # 방향성 있게 보정하던 _boost_enhanced_score_with_inverse_pressure()는
                        # 이미 항등함수(return enhanced_score)로 무력화돼 있었고, 그 비대칭
                        # 의도가 담긴 죽은 코드였으므로 제거했다. enhanced_score를 그대로 쓴다.
                        _enhanced_score_for_entry = decision.get("enhanced_score")
                        if state.get("adaptive_fusion_enabled"):
                            _run_adaptive_fusion_entry(
                                state, broker, hynix_price, inverse_price, now, [],
                                enhanced_ai_score=_enhanced_score_for_entry, hynix_df_1min=df_1min,
                                position_manager=position_manager,
                            )
                        else:
                            _run_active_strategy_entry(
                                state, broker, hynix_price, inverse_price, now, [],
                                enhanced_ai_score=_enhanced_score_for_entry, position_manager=position_manager,
                            )
                    except Exception as exc:
                        logger.error("[HynixSwitchEngine] Active Strategy/Adaptive Fusion 진단 계산 실패(무해): %s", exc)
                        warnings.append(f"Active Strategy/Adaptive Fusion 진단 계산 실패: {exc}")

                final_action = decision.get("final_action", "HOLD")
                forced = False
                reason = "; ".join(decision.get("reasons", []))
                if final_action == "HOLD" and forced_info.get("should_force"):
                    final_action = forced_info.get("forced_direction") or "HOLD"
                    forced = True
                    reason = f"강제거래창({forced_info.get('window')}) — {reason}"

                if final_action != "HOLD":
                    held_symbol = (state.get("position") or {}).get("symbol")
                    desired_symbol = _ACTION_TO_SYMBOL.get(final_action)
                    is_new_entry = desired_symbol is not None and held_symbol != desired_symbol
                    trace["enhanced_direction_approval"] = {
                        "approved": bool(is_new_entry and desired_symbol),
                        "final_action": final_action,
                        "desired_symbol": desired_symbol,
                        "direction": "UP" if desired_symbol == HYNIX_SYMBOL else "DOWN" if desired_symbol == INVERSE_SYMBOL else None,
                        "reason": reason,
                    }

                    # 요구사항(2026-07-16) — 이번 사이클이 갱신하기 전, "과거(직전 사이클까지)"
                    # 스냅샷을 먼저 떼어 UI에 별도로 보여준다. 예: 직전 사이클엔 눌림목
                    # 대기 중이었는데 이번 사이클엔 이미 확인횟수가 쌓여 즉시 진입이
                    # 승인된 경우, 과거 스냅샷("눌림목 대기")과 이번 사이클의 live 승인
                    # 상태가 뒤섞여 보이지 않게 한다.
                    _snapshot_trend_plan = dict(state.get("last_trend_switch_plan") or {})
                    _snapshot_confirm_tracker = dict(state.get("trend_switch_confirm_tracker") or {})
                    trace["snapshot_confirmation_count"] = _snapshot_confirm_tracker.get("same_direction_streak")
                    if _snapshot_trend_plan.get("block_reason"):
                        trace["snapshot_pullback_status"] = _snapshot_trend_plan["block_reason"]
                    elif _snapshot_trend_plan.get("proceed") is True:
                        trace["snapshot_pullback_status"] = "PROCEED(직전 사이클 승인)"
                    elif _snapshot_trend_plan:
                        trace["snapshot_pullback_status"] = "눌림목 대기 중(직전 사이클)"
                    else:
                        trace["snapshot_pullback_status"] = None

                    proceed = True
                    _score_gap_target_pct = None
                    # Early/Active/Fusion 토글과 무관 — 메인 3분 사이클은 신규매수를
                    # 직접 실행하지 않는다. 실진입은 Fast Worker의
                    # evaluate_range_weighted_entry -> run_switch_or_entry(WEIGHTED)만.
                    if not is_new_entry:
                        trace["entry_approved"] = True
                        trace["entry_approved_reason"] = "이미 목표 종목 보유 중 — 추가 진입 불필요"
                    else:
                        proceed = False
                        trace["enhanced_direct_order_blocked"] = True
                        if not new_entry_allowed_now:
                            early_result = {
                                "skipped": True,
                                "reason": new_entry_window["rule"],
                                "reason_code": "TIME_GATE_BLOCK",
                            }
                        else:
                            early_result = {
                                "skipped": True,
                                "reason": "FAST_WORKER_OWNS_ENTRY",
                                "reason_code": "FAST_WORKER_OWNS_ENTRY",
                                "order_permission": "DIAGNOSTIC_ONLY",
                            }
                        _record_early_result_on_trace(trace, early_result)
                        _early_order_sent = bool((trace.get("early_order_result") or {}).get("order_sent"))
                        _early_broker_executed = bool((trace.get("early_order_result") or {}).get("broker_executed"))
                        _early_reason_code = (trace.get("early_decision") or {}).get("reason_code")
                        if _early_broker_executed or _early_reason_code == "TARGET_ALREADY_FILLED":
                            trace["entry_approved"] = True
                            trace["entry_approved_reason"] = (
                                "Fast Worker weighted controller owns entries"
                            )
                        else:
                            trace["entry_approved"] = False
                            _early_reason_text = (trace.get("early_decision") or {}).get("reason") or _early_reason_code or "NO_EARLY_SIGNAL"
                            trace["entry_approved_reason"] = f"MAIN_CYCLE_ENTRY_DEFERRED: {_early_reason_text}"

                    # 요구사항(2026-07-16) — 이번 사이클에 새로 계산된 live 상태(위
                    # snapshot_*와 명확히 구분됨). same_direction_streak(연속 확인
                    # 횟수) 규칙: 1회=일반적으로 대기, 2회=탐색진입(20~30%), 3회
                    # 이상=확대(50%) — 단, STRONG 신호는 1회 확인만으로도 즉시
                    # 탐색진입을 허용한다(plan_entry의 "STRONG_BUY 첫 신호는 계속
                    # 소액 탐색 진입을 허용한다" 분기, entry_approved_reason에
                    # "강한 신호 — 눌림목 대기 생략"으로 명시됨).
                    trace["live_confirmation_count"] = (state.get("trend_switch_confirm_tracker") or {}).get("same_direction_streak")
                    trace["live_entry_gate_status"] = trace.get("entry_approved_reason")

                    if proceed:
                        attempted_entry = True
                        state.pop("pending_entry", None)
                        try:
                            switch = run_switch_or_entry(
                                state, broker, final_action, hynix_price, inverse_price,
                                now=now, forced=forced, reason=reason, position_manager=position_manager,
                                # 요구사항(2026-07-21) — score-gap 사다리가 정한 비중이 있으면
                                # 그대로 쓴다(30~50%/20~30% 등). 없으면 None으로 기존 기본
                                # 사이징 로직(run_switch_or_entry 내부 기본값)을 그대로 쓴다.
                                target_position_pct=_score_gap_target_pct,
                            )
                            orders_this_cycle.extend(switch.get("orders", []))
                            trace["execution_stage"] = switch.get("stage")
                            # 요구사항(2026-07-16) — run_switch_or_entry()의 실제 결과는
                            # entry_approved_reason(진입 "승인" 사유 필드)을 덮어쓰지 않고
                            # 별도 필드에 저장한다. 과거에는 실제로 브로커가 주문을 거부
                            # (action=BUY, success=False)했을 때도 "_switch_sent_orders"가
                            # 비어있지 않다고 판정돼 entry_approved_reason이 그대로 남아,
                            # blocking_reason에 "EXPLORATORY 30% 진입 승인" 같은 승인 문구가
                            # 실패 사유인 것처럼 표시되는 사고가 있었다(2026-07-16 사용자
                            # 리포트: Entry Approved=YES, Order Sent=NO인데도 blocking_reason
                            # 이 승인 문구를 그대로 보여줌).
                            trace["execution_message"] = switch.get("message")
                            trace["order_failure_code"] = switch.get("failure_code")
                            trace["broker_error"] = switch.get("broker_error")
                            trace["requested_symbol"] = switch.get("requested_symbol")
                            trace["requested_qty"] = switch.get("requested_qty")
                            trace["order_price"] = switch.get("order_price")
                            trace["buyable_cash"] = switch.get("buyable_cash")
                            trace["sized_cash"] = switch.get("sized_cash")
                            trace["cooldown_remaining"] = switch.get("cooldown_remaining")
                            trace["pending_order"] = switch.get("pending_order", False)
                            _switch_sent_orders = [
                                o for o in (switch.get("orders") or [])
                                if o.get("action") in ("BUY", "SELL") and o.get("success")
                            ]
                            if not _switch_sent_orders:
                                # entry_approved_reason 자체는 "진입이 승인됐는지/왜"만
                                # 담는다 — 주문 실행 결과는 위 execution_message/
                                # order_failure_code로만 노출한다(같은 필드를 재사용하지
                                # 않음, 요구사항2).
                                pass
                        except Exception as exc:
                            logger.error("[HynixSwitchEngine] 스위칭/진입 처리 실패: %s", exc)
                            warnings.append(f"스위칭/진입 처리 실패: {exc}")
                            trace["execution_stage"] = "order_sent"
                            trace["execution_message"] = f"스위칭/진입 처리 예외: {exc}"
                            trace["order_failure_code"] = "EXECUTION_EXCEPTION"
                            trace["broker_error"] = str(exc)
                else:
                    state.pop("pending_entry", None)
                    trace["entry_approved_reason"] = "HOLD — 신규 진입 신호 없음"

        if forced_info.get("should_force") and forced_info.get("window") and attempted_entry:
            if forced_info["window"] not in fired_windows:
                fired_windows.append(forced_info["window"])
                state["fired_windows"] = fired_windows

        # 이번 사이클에 주문을 실행했다면, "확정된 것으로 추정한 상태"가 아니라 브로커에
        # 실제로 무엇이 체결됐는지 다시 확인하고 그 결과로 state(캐시)를 갱신한다.
        # (buy()/sell() → broker.positions 갱신 → get_positions() → position_manager.sync() → state 캐시)
        if orders_this_cycle and position_manager is not None:
            try:
                position_manager.sync(force=True)
                apply_position_manager_to_state(state, position_manager)
            except Exception as exc:
                logger.error("[HynixSwitchEngine] 주문 후 포지션 재확인 실패: %s", exc)
                warnings.append(f"주문 후 포지션 재확인 실패: {exc}")

    # ── Order Sent / Broker Executed / Position Confirmed 판정 ──────────────
    sent_orders = [o for o in orders_this_cycle if o.get("action") in ("BUY", "SELL")]
    trace["order_sent"] = bool(sent_orders)
    trace["broker_executed"] = any(o.get("success") for o in sent_orders)
    if trace["broker_executed"]:
        state["last_trade_time"] = now.isoformat()
    if sent_orders:
        last_order = sent_orders[-1]
        if not last_order.get("success"):
            trace["position_confirmed"] = False
        elif last_order.get("action") == "BUY":
            pos_now = state.get("position") or {}
            trace["position_confirmed"] = (
                pos_now.get("symbol") == last_order.get("symbol") and (pos_now.get("quantity") or 0) > 0
            )
        else:  # SELL
            # 부분매도(expected_remaining_qty>0)는 같은 심볼이 남은 수량과 정확히
            # 일치해야 confirmed다 — "심볼이 사라졌는지"만 보면 부분매도는 항상
            # False로 오판된다. 전량매도(expected_remaining_qty==0)만 심볼 소거를 기대한다.
            pos_now = state.get("position") or {}
            expected_remaining = last_order.get("expected_remaining_qty")
            actual_qty = pos_now.get("quantity") or 0
            if expected_remaining == 0:
                trace["position_confirmed"] = pos_now.get("symbol") != last_order.get("symbol") or actual_qty == 0
            else:
                trace["position_confirmed"] = (
                    pos_now.get("symbol") == last_order.get("symbol") and actual_qty == expected_remaining
                )
            trace["prediction_signal"] = "SELL"  # TP/SL/강제청산/스위칭 매도 — 예측신호와 별개로 실제 실행된 것

    # ── 미실현손익/당일수익률 갱신 ────────────────────────────────────────────
    # unrealized_pnl은 GrossPnL이 아니라 NetPnL이다 — "지금 판다면" 발생할 매도수수료/
    # 거래세/슬리피지를 선차감해 표시한다(docs/requirements.md 섹션 2.10). 일손익
    # 리스크 게이트(daily_return_pct 기반 신규진입 중단/강제청산)도 이 값을 그대로
    # 쓰므로, Gross보다 보수적인(더 이르게 위험을 인식하는) 방향으로 안전하게 작동한다.
    position = state.get("position") or {}
    unrealized_pnl = 0.0
    gross_unrealized_pnl = 0.0
    if position.get("symbol") and (position.get("quantity") or 0) > 0 and position.get("entry_price"):
        cur = _current_price(position["symbol"], hynix_price, inverse_price)
        if cur is not None:
            try:
                from app.trading.trading_cost_engine import TradeCostEngine

                cost_result = TradeCostEngine().compute_unrealized_net_pnl(
                    position["symbol"], entry_price=position["entry_price"], current_price=cur,
                    quantity=position["quantity"],
                )
                unrealized_pnl = cost_result["net_unrealized_pnl"]
                gross_unrealized_pnl = cost_result["gross_unrealized_pnl"]
                state["unrealized_pnl_cost_breakdown"] = cost_result
            except Exception:
                unrealized_pnl = gross_unrealized_pnl = (cur - position["entry_price"]) * position["quantity"]
    state["unrealized_pnl"] = unrealized_pnl
    state["gross_unrealized_pnl"] = gross_unrealized_pnl

    # net_daily_return = (net_realized_pnl + net_unrealized_pnl) / starting_equity(당일 시작
    # 자산) — 반드시 당일 시작 시점 자산(daily_pnl_baseline_equity)을 분모로 써야 한다.
    # total_equity(지금 이 순간의 계좌평가액)를 분모로 쓰지 않는다 — 그 값 자체가
    # 이미 오늘 손익을 반영해 시시각각 변하고, 계좌조회 실패 시 0을 반환할 수 있어
    # (2026-07-14 실측: 이 경로 때문에 일손실 -100%로 오판돼 자동매매가 잘못
    # 정지됨) 분모로 쓰면 위험하다. risk_manager 게이트(compute_net_daily_return, 위
    # 참조)와 반드시 같은 분모/공식을 써야 하므로(요구사항 1/5절) baseline이 아직
    # 확정되지 않았으면(당일 첫 유효 조회가 아직 없었으면) 갱신을 건너뛰고 이전 값을
    # 유지한다 — 0이나 total_equity로 대체하지 않는다.
    starting_equity = state.get("daily_pnl_baseline_equity")
    if starting_equity and starting_equity > 0:
        state["realized_pnl_today_pct"] = round(
            (state.get("realized_pnl_today_krw", 0.0) + unrealized_pnl) / starting_equity * 100.0, 4,
        )
        state["gross_realized_pnl_today_pct"] = round(
            (state.get("gross_realized_pnl_today_krw", 0.0) + gross_unrealized_pnl) / starting_equity * 100.0, 4,
        )
        # risk_manager와 UI가 같은 값을 쓰도록(요구사항 5절) 이번 사이클(주문 반영 후)
        # 최신 미실현손익 기준으로 daily_return_calculation도 함께 갱신한다.
        dr = state.get("daily_return_calculation") or {}
        dr.update({
            "starting_equity": starting_equity,
            "net_realized_pnl": state.get("realized_pnl_today_krw", 0.0),
            "net_unrealized_pnl": unrealized_pnl,
            "net_daily_return": state["realized_pnl_today_pct"],
            "calculation_source": "ledger_unified",
        })
        state["daily_return_calculation"] = dr

    trace["trade_counter"] = state.get("daily_trade_count", 0)
    trace["ui_synced"] = save_state_atomic(state)
    trace["stopped_stage"] = _first_blocked_stage(trace)
    # 사용자 요청 필드명(risk_approved/blocking_reason)도 함께 노출 — risk_manager_ok/
    # stopped_stage와 같은 값을 가리키는 별칭이다.
    trace["risk_approved"] = trace["risk_manager_ok"]
    trace["blocking_reason"] = _build_blocking_reason(trace)
    trace["signal_summary"] = _build_signal_summary(
        decision=decision,
        trace=trace,
        state=state,
        now=now,
        new_entry_allowed_now=new_entry_allowed_now,
        new_entry_window=new_entry_window,
    )
    completed_decision_snapshot = _build_completed_decision_snapshot(
        enhanced_result=enhanced_result,
        decision=decision,
        trace=trace,
        state=state,
        now=now,
        orders_this_cycle=orders_this_cycle,
        new_entry_allowed_now=new_entry_allowed_now,
    )

    # 백그라운드 스레드에서만 사이클이 돌아도(=Streamlit 세션에 아무도 접속하지 않아도)
    # UI가 "사이클 미실행"을 보여주지 않도록, 이번 사이클 결과(최종 trace 포함)를
    # state에도 남겨 한 번 더 저장한다.
    state["last_pipeline_trace"] = trace
    state["last_cycle_computed_at"] = now.isoformat()
    state["last_hynix_price"] = hynix_price  # legacy alias: LONG_SYMBOL(0193T0) execution price
    state["last_long_price"] = hynix_price  # LONG_SYMBOL(0193T0) — 실제 매매/손익 기준
    state["last_hynix_signal_price"] = hynix_signal_price  # 000660 — 감시 기초자산(신호 계산 참고용)
    state["last_inverse_price"] = inverse_price
    state["last_enhanced_result"] = enhanced_result
    state["last_decision"] = decision
    state["last_signal_summary"] = trace["signal_summary"]
    state["last_completed_decision_snapshot"] = completed_decision_snapshot
    save_state_atomic(state)

    # ── 로그 기록 ────────────────────────────────────────────────────────────
    try:
        log_enhanced_prediction({
            "hynix_price": hynix_price, "inverse_price": inverse_price,
            "base_prediction_score": enhanced_result.get("base_prediction_score"),
            "existing_micron_score": enhanced_result.get("existing_micron_score"),
            "hynix_technical_score": enhanced_result.get("hynix_technical_score"),
            "intraday_momentum_score": enhanced_result.get("intraday_momentum_score"),
            "inverse_pressure_score": enhanced_result.get("inverse_pressure_score"),
            "enhanced_score": enhanced_result.get("enhanced_score"),
            "final_action": decision.get("final_action"),
            "reason_top5": enhanced_result.get("reason_top5"),
        })
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] 예측 로그 기록 실패: %s", exc)

    failed_orders = [o for o in orders_this_cycle if not o.get("success")]
    if failed_orders:
        for o in failed_orders:
            warnings.append(f"주문 실패/스킵: [{o.get('action')}] {o.get('symbol')} — {o.get('message')}")

    for order in orders_this_cycle:
        try:
            log_trade({
                **order, "mode": mode,
                "base_prediction_score": enhanced_result.get("base_prediction_score"),
                "existing_micron_score": enhanced_result.get("existing_micron_score"),
                "hynix_technical_score": enhanced_result.get("hynix_technical_score"),
                "inverse_pressure_score": enhanced_result.get("inverse_pressure_score"),
                "enhanced_score": enhanced_result.get("enhanced_score"),
                "realized_pnl": state.get("realized_pnl_today_krw"),
                "unrealized_pnl": unrealized_pnl,
                "daily_return": state.get("realized_pnl_today_pct"),
            })
        except Exception as exc:
            logger.debug("[HynixSwitchEngine] 거래 로그 기록 실패: %s", exc)

    # ── 판단 로그 + 예측/실제 결과 추적 (실제 주문 여부와 무관하게 항상 수행) ──
    try:
        from app.services.hynix_prediction_tracker import log_trade_decision, check_and_resolve_pending_outcomes

        log_trade_decision(
            now, hynix_price, inverse_price, enhanced_result, decision,
            actual_trade_executed=any(o.get("success") for o in orders_this_cycle),
            position_symbol=(state.get("position") or {}).get("symbol"),
        )
        check_and_resolve_pending_outcomes(now, hynix_price, inverse_price)
    except Exception as exc:
        logger.debug("[HynixSwitchEngine] 판단/결과 추적 로그 실패: %s", exc)

    liquidation_phase = liquidation_phase_now

    # ── 장 종료 후 1일 1회: 종가 outcome 확정 + 일별 리포트 + 가중치 추천 ──────
    today_str = now.strftime("%Y%m%d")
    if liquidation_phase == "closed" and state.get("daily_report_generated_date") != today_str and hynix_price:
        try:
            from app.services.hynix_prediction_tracker import resolve_close_outcomes
            from app.services.hynix_prediction_report import generate_daily_prediction_report
            from app.services.hynix_weight_recommender import recommend_weight_adjustment
            from app.services.hynix_weight_manager import maybe_auto_apply_in_mock

            resolve_close_outcomes(
                date_str=today_str, hynix_close_price=hynix_price, inverse_close_price=inverse_price,
                realized_pnl_today_krw=state.get("realized_pnl_today_krw", 0.0),
            )
            generate_daily_prediction_report(date_str=today_str)
            recommend_weight_adjustment()
            maybe_auto_apply_in_mock(mode, bool(state.get("weight_auto_apply_enabled")))

            from app.services.hynix_exit_recommender import recommend_exit_parameters, generate_daily_exit_learning

            recommend_exit_parameters()
            generate_daily_exit_learning(date_str=today_str)

            state["daily_report_generated_date"] = today_str
            save_state_atomic(state)
        except Exception as exc:
            logger.error("[HynixSwitchEngine] 장종료 리포트/추천 생성 실패: %s", exc)
            warnings.append(f"장종료 리포트/추천 생성 실패: {exc}")

    return {
        "skipped": False,
        "computed_at": now.isoformat(),
        "mode": mode,
        "auto_trade_on": auto_trade_on,
        "new_entry_allowed": is_new_entry_allowed(now),
        "new_entry_window_rule": describe_new_entry_window(now)["rule"],
        "liquidation_phase": liquidation_phase,
        "hynix_current_price": hynix_price,
        "hynix_signal_price": hynix_signal_price,
        "long_current_price": hynix_price,
        "inverse_current_price": inverse_price,
        "enhanced_result": enhanced_result,
        "decision": decision,
        "forced_info": forced_info,
        "orders_this_cycle": orders_this_cycle,
        "state": state,
        # UI/Dynamic Exit AI는 이 필드(브로커 sync 직후 결과)를 읽어야 한다.
        # state["position"]/state["daily_trade_count"]는 이 값을 그대로 옮겨 담은 캐시일 뿐이다.
        "position_manager": position_manager.to_cache_dict() if position_manager is not None else None,
        "warnings": warnings + (enhanced_result.get("warnings") or []),
        "pipeline_trace": trace,
        "decision_snapshot": completed_decision_snapshot,
        # SHADOW MODE 전용 — 실제 주문에 영향 없음(비교/검증 목적).
        "cycle_ai_shadow_result": state.get("last_cycle_ai_result"),
    }


def execute_hynix_auto_trade(mode: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """update_hynix_auto_trade_loop()의 공개 래퍼."""
    return update_hynix_auto_trade_loop(mode=mode, now=now)
