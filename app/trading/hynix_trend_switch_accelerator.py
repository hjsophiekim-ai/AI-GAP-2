"""
hynix_trend_switch_accelerator.py — Enhanced 하이닉스⇄0197X0(ENHANCED_LEGACY 경로,
active_strategy_enabled=False일 때) 전용 추세 전환 가속기.

Adaptive Fusion(hynix_adaptive_fusion_engine.py)이나 Active Strategy와는 완전히
별개다 — 이 모듈은 evaluate_pullback_gate()의 "무조건 눌림목 대기" 방식이 강한
신호/추세 반전에도 장시간 진입을 막던 문제(2026-07-14 실측)를 해결하기 위해,
같은 방향/반대 방향 신호의 "연속 확인 횟수"를 근거로 눌림목 대기를 건너뛰거나
즉시 전환할지 결정한다. 실제 주문 사이징/체결은 여전히 hynix_switch_position_manager
가 담당하며, 이 모듈은 "진입해도 되는지 + 목표 비중 + entry_type"만 반환한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.logger import logger

ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = ROOT / "config" / "hynix_enhanced_weights.json"

_DEFAULTS = {
    "exploratory_position_pct": 0.20,
    "confirmed_position_pct_min": 0.50,
    "confirmed_position_pct_max": 0.70,
    "same_direction_reentry_cooldown_seconds": 180,
    "daily_target_round_trips_min": 4,
    "daily_target_round_trips_max": 5,
    "max_daily_round_trips": 8,
    "normal_signal_pullback_wait_minutes": 3,
    "exploratory_stop_loss_pct": -0.8,
    "normal_stop_loss_atr_multiplier": 1.2,
    "normal_stop_loss_cap_pct": -1.5,
    "consecutive_loss_halve_threshold": 2,
    "consecutive_loss_block_threshold": 3,
    "daily_loss_block_pct": -2.0,
}

ACTION_TO_DIRECTION = {
    "HYNIX_STRONG_BUY": "HYNIX", "HYNIX_BUY": "HYNIX",
    "INVERSE_STRONG_BUY": "INVERSE", "INVERSE_BUY": "INVERSE",
}


def load_trend_accel_config() -> dict:
    try:
        if _CONFIG_PATH.exists():
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            return {**_DEFAULTS, **(data.get("trend_switch_accel") or {})}
    except Exception as exc:
        logger.debug("[TrendSwitchAccel] 설정 로드 실패, 기본값 사용: %s", exc)
    return dict(_DEFAULTS)


def signal_direction(final_action: Optional[str]) -> Optional[str]:
    return ACTION_TO_DIRECTION.get(final_action or "")


def is_strong_signal(final_action: Optional[str]) -> bool:
    return bool(final_action) and str(final_action).endswith("_STRONG_BUY")


# ---------------------------------------------------------------------------
# 연속 확인 카운터(same_direction_streak / reversal_streak)
# ---------------------------------------------------------------------------

def default_confirm_state() -> dict:
    return {
        "direction": None, "same_direction_streak": 0,
        "reversal_streak": 0, "reversal_against_symbol": None,
        "last_signal_at": None, "_state_date": None,
    }


def default_frequency_state() -> dict:
    return {
        "round_trips_today": 0, "consecutive_losses": 0,
        "last_entry_at": None, "last_entry_direction": None,
        "_state_date": None,
    }


def _today(now: datetime) -> str:
    return now.strftime("%Y%m%d")


def _reset_if_new_day(tracker: Optional[dict], now: datetime, factory) -> dict:
    tracker = dict(tracker) if tracker else factory()
    if tracker.get("_state_date") != _today(now):
        tracker = factory()
        tracker["_state_date"] = _today(now)
    return tracker


def update_confirm_tracker(
    tracker: Optional[dict], final_action: Optional[str], held_symbol: Optional[str],
    desired_symbol: Optional[str], now: datetime,
) -> dict:
    """같은 방향 신호 연속 횟수(same_direction_streak)와, 보유 종목과 반대 방향
    신호가 연속된 횟수(reversal_streak)를 갱신한다.

    final_action이 HOLD(방향 없음)면 두 카운터 모두 리셋한다 — 신호가 사라지면
    "확인"도 사라져야, 몇 사이클 전의 낡은 확신으로 뒤늦게 진입/전환하지 않는다.
    """
    tracker = _reset_if_new_day(tracker, now, default_confirm_state)

    direction = signal_direction(final_action)
    if direction is None:
        tracker.update(direction=None, same_direction_streak=0, reversal_streak=0, reversal_against_symbol=None)
        return tracker

    if tracker.get("direction") == direction:
        tracker["same_direction_streak"] = int(tracker.get("same_direction_streak", 0)) + 1
    else:
        tracker["same_direction_streak"] = 1
        tracker["direction"] = direction
    tracker["last_signal_at"] = now.isoformat()

    opposes_held = held_symbol is not None and desired_symbol is not None and desired_symbol != held_symbol
    if opposes_held:
        if tracker.get("reversal_against_symbol") == held_symbol:
            tracker["reversal_streak"] = int(tracker.get("reversal_streak", 0)) + 1
        else:
            tracker["reversal_streak"] = 1
            tracker["reversal_against_symbol"] = held_symbol
    else:
        tracker["reversal_streak"] = 0
        tracker["reversal_against_symbol"] = None

    return tracker


def register_frequency_entry(frequency_state: Optional[dict], direction: Optional[str], now: datetime) -> dict:
    state = _reset_if_new_day(frequency_state, now, default_frequency_state)
    state["last_entry_at"] = now.isoformat()
    state["last_entry_direction"] = direction
    return state


def register_round_trip_closed(frequency_state: Optional[dict], was_loss: bool, now: datetime) -> dict:
    state = _reset_if_new_day(frequency_state, now, default_frequency_state)
    state["round_trips_today"] = int(state.get("round_trips_today", 0)) + 1
    state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1 if was_loss else 0
    return state


# ---------------------------------------------------------------------------
# ATR 기반 동적 손절폭(정상 진입 전용, 최대 -1.5% 캡)
# ---------------------------------------------------------------------------

def normal_entry_stop_loss_pct(atr_pct: Optional[float], cfg: Optional[dict] = None) -> float:
    cfg = cfg or load_trend_accel_config()
    cap = -abs(cfg["normal_stop_loss_cap_pct"])
    if atr_pct is None or atr_pct <= 0:
        return cap
    dynamic = -abs(atr_pct * cfg["normal_stop_loss_atr_multiplier"])
    return max(dynamic, cap)  # ATR 손절폭이 캡보다 더 벌어지지 않게(둘 중 덜 음수인 쪽=더 타이트한 쪽 채택 안전마진 없음)


def _confirmed_position_pct(cfg: dict) -> float:
    return (cfg["confirmed_position_pct_min"] + cfg["confirmed_position_pct_max"]) / 2.0


# ---------------------------------------------------------------------------
# 진입/전환 종합 판단
# ---------------------------------------------------------------------------

def plan_entry(
    *, final_action: Optional[str], held_symbol: Optional[str], desired_symbol: Optional[str],
    confirm_tracker: dict, frequency_state: dict, pullback_result: Optional[dict],
    now: datetime, data_ok: bool, has_unconfirmed_order: bool,
    daily_return_pct: Optional[float], atr_pct: Optional[float] = None,
) -> dict:
    """반환: proceed, position_pct(0~1 비율 또는 None=기존 사이징 유지), entry_type,
    immediate_switch(bool), stop_loss_pct(포지션에 태깅할 손절 기준), block_reason,
    same_direction_streak, reversal_streak."""
    cfg = load_trend_accel_config()
    same_streak = int(confirm_tracker.get("same_direction_streak", 0))
    reversal_streak = int(confirm_tracker.get("reversal_streak", 0))

    result = {
        "proceed": False, "position_pct": None, "entry_type": None, "immediate_switch": False,
        "stop_loss_pct": None, "block_reason": None,
        "same_direction_streak": same_streak, "reversal_streak": reversal_streak,
    }

    if desired_symbol is None:
        result["block_reason"] = "HOLD — 신규 진입 신호 없음"
        return result
    if not data_ok:
        result["block_reason"] = "데이터 stale — 진입 금지"
        return result
    if has_unconfirmed_order:
        result["block_reason"] = "미체결/부분체결 주문 존재 — 신규 진입 금지"
        return result
    if int(frequency_state.get("consecutive_losses", 0)) >= cfg["consecutive_loss_block_threshold"]:
        result["block_reason"] = f"연속손실 {frequency_state.get('consecutive_losses')}회 — 신규진입 중단"
        return result
    if daily_return_pct is not None and daily_return_pct <= cfg["daily_loss_block_pct"]:
        result["block_reason"] = f"일 손실 {daily_return_pct:.2f}% ≤ {cfg['daily_loss_block_pct']}% — 신규진입 중단"
        return result
    if int(frequency_state.get("round_trips_today", 0)) >= cfg["max_daily_round_trips"]:
        result["block_reason"] = f"당일 왕복거래 {cfg['max_daily_round_trips']}회 도달 — 신규진입 중단"
        return result

    strong = is_strong_signal(final_action)
    is_switch_target = held_symbol is not None and desired_symbol != held_symbol
    if is_switch_target and reversal_streak < 2:
        result["block_reason"] = f"opposite direction signal confirmation pending ({reversal_streak}/2)"
        return result

    # 동일 방향 재진입 쿨다운(3분, 방향 전환 시 면제)
    if not (is_switch_target and reversal_streak >= 2):
        last_dir = frequency_state.get("last_entry_direction")
        last_at = frequency_state.get("last_entry_at")
        if last_dir == signal_direction(final_action) and last_at:
            try:
                elapsed = (now - datetime.fromisoformat(last_at)).total_seconds()
            except Exception:
                elapsed = cfg["same_direction_reentry_cooldown_seconds"]
            cooldown = cfg["same_direction_reentry_cooldown_seconds"]
            if elapsed < cooldown:
                result["block_reason"] = f"동일 방향 재진입 쿨다운({cooldown:.0f}초) — {elapsed:.0f}초 경과"
                return result

    def _halved_if_needed(pct: float) -> float:
        if int(frequency_state.get("consecutive_losses", 0)) >= cfg["consecutive_loss_halve_threshold"]:
            return round(pct * 0.5, 4)
        return pct

    # ── 1) 기존 포지션과 반대 방향 신호 2회 연속 — 즉시 전환(눌림목 불요) ──────
    if is_switch_target and reversal_streak >= 2:
        pct = _halved_if_needed(cfg["exploratory_position_pct"])
        result.update(
            proceed=True, position_pct=pct, entry_type="EXPLORATORY", immediate_switch=True,
            stop_loss_pct=cfg["exploratory_stop_loss_pct"],
        )
        return result

    # ── 2) STRONG_BUY 연속 확인 — 눌림목 불요, 1회차 탐색/2회차 이상 확정 ─────
    if strong and same_streak >= 1:
        pct = _halved_if_needed(cfg["exploratory_position_pct"])
        result.update(
            proceed=True, position_pct=pct, entry_type="EXPLORATORY",
            stop_loss_pct=cfg["exploratory_stop_loss_pct"],
        )
        result["immediate_switch"] = is_switch_target
        return result

    # ── 3) 일반 신호 — 눌림목 대기(호출부가 evaluate_pullback_gate 결과를 전달) ──
    if pullback_result is None:
        result["block_reason"] = "눌림목 판정 불가(데이터 없음)"
        return result
    if pullback_result.get("breakdown"):
        result["block_reason"] = pullback_result.get("message") or "국소 저점 붕괴 — 진입 금지"
        return result
    if pullback_result.get("proceed"):
        pct = _halved_if_needed(1.0) if frequency_state.get("consecutive_losses", 0) >= cfg["consecutive_loss_halve_threshold"] else None
        result.update(
            proceed=True, position_pct=(None if pct == 1.0 else pct), entry_type="NORMAL",
            stop_loss_pct=normal_entry_stop_loss_pct(atr_pct, cfg),
        )
        return result

    result["block_reason"] = pullback_result.get("message")
    return result
