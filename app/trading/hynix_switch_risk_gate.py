"""
hynix_switch_risk_gate.py — 신규진입 시간창 판정 + VI/호가공백 감지 + 차단조건 통합.

요구사항(2026-07-20) — 기존 "09:00~09:10 관망(watch-only)" 규칙은 완전히
삭제했다. 신규진입 허용/금지는 이제 다음 3구간으로만 판단한다:
  09:00~09:15 신규진입 허용
  09:15~09:30 신규진입 금지
  09:30~14:50 신규진입 허용(기존과 동일)
  그 외 시간대(장 시작 전/14:50 이후) 신규진입 금지
15:10 청산모드, 15:15 강제청산, 15:20 이후 신규주문 금지의 시간모델은 그대로다.

이 시간창은 신규진입에만 적용된다 — 기존 포지션의 손절/익절/반전청산/15:15
강제청산(run_liquidation_if_needed/run_tp_sl_if_needed/
run_reversal_switch_if_needed/Dynamic Exit Watcher)은 이 모듈의 게이트를 거치지
않고 항상 실행된다. is_new_entry_allowed()가 모든 신규진입 경로(Early Trend
Detector/ENHANCED_REGIME_SWITCH/Active Strategy/Fast Watcher)가 공유하는
단일 판정 지점이다.

VI/호가공백 감지는 참고할 기존 코드가 없어 가격·거래량 이상치 기반 휴리스틱으로
구현했다(정밀도 낮을 수 있음 — 추후 KIS 응답 필드 확인 시 교체 가능하도록 분리).
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger
from app.utils.time_utils import kst_now

ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = ROOT / "config" / "hynix_enhanced_weights.json"

_DEFAULT_SCHEDULE = {
    # 요구사항(2026-07-20) — 09:00~09:10 관망 규칙 삭제, 3구간 신규진입 규칙으로 대체.
    "new_entry_morning_open": "09:00",
    "new_entry_morning_blackout_start": "09:15",
    "new_entry_morning_blackout_end": "09:30",
    "forced_trade_windows": [["09:00", "09:15"], ["10:30", "11:00"], ["13:30", "14:30"]],
    "entry_cutoff_time": "14:50",
    "liquidation_prep_time": "15:05",
    "liquidation_mode_time": "15:10",
    "liquidation_time": "15:15",
    "no_new_order_time": "15:20",
    # 장외 시간대(운영창 밖)에는 백그라운드 사이클이 시세/주문/계좌조회를 하지 않고
    # heartbeat만 유지한다 — 09:00 장시작보다 넉넉히 이른 08:50부터, 15:30 정규장
    # 종료까지를 "운영창"으로 둔다.
    "operating_window_start": "08:50",
    "operating_window_end": "15:30",
}

_VI_MOVE_THRESHOLD_PCT = 6.0
_GAP_FROZEN_BARS = 5
_DAILY_LOSS_LIMIT_PCT = -2.5


def _load_schedule() -> dict:
    try:
        if _CONFIG_PATH.exists():
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            return {**_DEFAULT_SCHEDULE, **(data.get("schedule") or {})}
    except Exception as exc:
        logger.debug("[SwitchRiskGate] 스케줄 로드 실패, 기본값 사용: %s", exc)
    return dict(_DEFAULT_SCHEDULE)


def _parse_hm(text: str) -> dtime:
    h, m = text.split(":")
    return dtime(int(h), int(m))


def is_within_operating_window(now: Optional[datetime] = None) -> bool:
    """08:50~15:30(KST, 기본값) 운영창 안이면 True.

    이 창 밖에서는 백그라운드 사이클(HynixAutoTradeCycleThread)이 시세/주문/
    계좌조회를 하지 않고 heartbeat만 유지해야 한다 — 장외 시간에도 3분마다 전체
    사이클을 계속 돌리면 KIS API를 불필요하게 반복 호출하고, cycle_count_today가
    실제 거래 가능 사이클 수와 무관하게 밤새 누적된다(2026-07-16 실측:
    cycle_count_today=284)."""
    now = now or kst_now()
    sched = _load_schedule()
    return _parse_hm(sched["operating_window_start"]) <= now.time() < _parse_hm(sched["operating_window_end"])


def is_new_entry_allowed(now: Optional[datetime] = None) -> bool:
    """신규진입 허용 여부(요구사항 2026-07-20) — 09:00~09:15, 09:30~14:50 구간만 True.

    09:15~09:30은 신규진입 금지(구간 전체 — 확정 신뢰도/방향일치 등 예외 없음).
    이 함수 하나가 Early Trend Detector/ENHANCED_REGIME_SWITCH/Active Strategy/
    Fast Watcher 등 모든 신규진입 경로가 공유하는 단일 판정 지점이다(스위칭의
    재매수 레그에도 동일 적용). 기존 포지션의 손절/익절/반전청산/15:15 강제청산은
    이 함수를 거치지 않는다 — 시간대와 무관하게 항상 실행된다."""
    now = now or kst_now()
    sched = _load_schedule()
    t = now.time()
    morning_open = _parse_hm(sched["new_entry_morning_open"])
    blackout_start = _parse_hm(sched["new_entry_morning_blackout_start"])
    blackout_end = _parse_hm(sched["new_entry_morning_blackout_end"])
    cutoff = _parse_hm(sched["entry_cutoff_time"])
    if morning_open <= t < blackout_start:
        return True
    if blackout_start <= t < blackout_end:
        return False
    return blackout_end <= t < cutoff


def describe_new_entry_window(now: Optional[datetime] = None) -> dict:
    """UI 표시용(요구사항) — 지금 신규진입이 허용되는지와 적용 중인 시간 규칙을
    사람이 읽을 문장으로 함께 반환한다."""
    now = now or kst_now()
    sched = _load_schedule()
    t = now.time()
    morning_open_s, blackout_start_s = sched["new_entry_morning_open"], sched["new_entry_morning_blackout_start"]
    blackout_end_s, cutoff_s = sched["new_entry_morning_blackout_end"], sched["entry_cutoff_time"]
    morning_open, blackout_start = _parse_hm(morning_open_s), _parse_hm(blackout_start_s)
    blackout_end, cutoff = _parse_hm(blackout_end_s), _parse_hm(cutoff_s)

    allowed = is_new_entry_allowed(now)
    if t < morning_open:
        rule = f"장 시작 전 — {morning_open_s}부터 신규진입 허용"
    elif morning_open <= t < blackout_start:
        rule = f"{morning_open_s}~{blackout_start_s} 신규진입 허용"
    elif blackout_start <= t < blackout_end:
        rule = f"{blackout_start_s}~{blackout_end_s} 신규진입 금지(보유 포지션 손절·익절·반전청산·15:15 강제청산은 계속 실행)"
    elif blackout_end <= t < cutoff:
        rule = f"{blackout_end_s}~{cutoff_s} 신규진입 허용"
    else:
        rule = f"{cutoff_s} 이후 — 신규진입 금지(청산만 진행)"
    return {"allowed": allowed, "rule": rule, "checked_at": now.isoformat(timespec="seconds")}


def get_liquidation_phase(now: Optional[datetime] = None) -> str:
    """'normal' | 'prep' | 'liquidation_mode' | 'closed'."""
    now = now or kst_now()
    sched = _load_schedule()
    t = now.time()
    if t >= _parse_hm(sched["no_new_order_time"]):
        return "closed"
    if t >= _parse_hm(sched["liquidation_mode_time"]):
        return "liquidation_mode"
    if t >= _parse_hm(sched["liquidation_prep_time"]):
        return "prep"
    return "normal"


def should_liquidate_now(now: Optional[datetime] = None) -> bool:
    now = now or kst_now()
    sched = _load_schedule()
    return now.time() >= _parse_hm(sched["liquidation_time"])


def check_forced_trade_window(now: Optional[datetime] = None, fired_windows: Optional[list] = None) -> Optional[str]:
    """현재 시각이 강제판단 시간대 안이고 아직 그 창에서 실행하지 않았으면 창 라벨(예: '09:10-09:30') 반환."""
    now = now or kst_now()
    fired_windows = fired_windows or []
    sched = _load_schedule()
    for start_s, end_s in sched["forced_trade_windows"]:
        label = f"{start_s}-{end_s}"
        if label in fired_windows:
            continue
        if _parse_hm(start_s) <= now.time() <= _parse_hm(end_s):
            return label
    return None


def detect_vi_or_gap(df_1min: Optional[pd.DataFrame]) -> dict:
    """VI(변동성완화장치) 발동/호가 공백 근사 감지 (휴리스틱)."""
    result = {"vi_suspected": False, "orderbook_gap_suspected": False, "reason": None}
    if df_1min is None or len(df_1min) < 2:
        return result
    try:
        work = df_1min.sort_values("datetime").tail(max(_GAP_FROZEN_BARS, 2))
        last = work.iloc[-1]
        last_open = float(last["open"])
        move_pct = abs(float(last["close"]) / last_open - 1.0) * 100 if last_open > 0 else 0.0
        if move_pct >= _VI_MOVE_THRESHOLD_PCT:
            result["vi_suspected"] = True
            result["reason"] = f"1분봉 급변 {move_pct:.1f}% (VI 발동 가능성)"

        if len(work) >= _GAP_FROZEN_BARS:
            price_frozen = work["close"].nunique() == 1
            volume_zero = float(work["volume"].fillna(0).sum()) == 0
            if price_frozen and volume_zero:
                result["orderbook_gap_suspected"] = True
                gap_reason = f"최근 {_GAP_FROZEN_BARS}봉 가격·거래량 동결(호가 공백 의심)"
                result["reason"] = f"{result['reason']} | {gap_reason}" if result["reason"] else gap_reason
    except Exception as exc:
        logger.debug("[SwitchRiskGate] VI/호가공백 감지 실패: %s", exc)
    return result


def resolve_forced_direction(decision_result: dict) -> str:
    """final_action이 HOLD인데 강제거래해야 할 때 유리한 방향을 반환."""
    enhanced = decision_result.get("enhanced_score", 50.0)
    inverse = decision_result.get("inverse_pressure_score", 50.0)
    return "HYNIX_BUY" if enhanced >= inverse else "INVERSE_BUY"


def should_force_trade(
    decision_result: dict,
    fired_windows: list,
    price_data_ok: bool,
    order_api_ok: bool,
    df_1min: Optional[pd.DataFrame],
    daily_pnl_pct: Optional[float],
    now: Optional[datetime] = None,
) -> dict:
    """강제거래(하루 최소 2회 보장) 수행 여부 종합 판단."""
    now = now or kst_now()
    result = {"should_force": False, "window": None, "forced_direction": None, "block_reason": None}

    if not is_new_entry_allowed(now):
        result["block_reason"] = describe_new_entry_window(now)["rule"]
        return result

    window = check_forced_trade_window(now, fired_windows)
    if window is None:
        result["block_reason"] = "강제판단 시간대 아님"
        return result

    if not price_data_ok:
        result["block_reason"] = "가격 데이터 없음"
        return result
    if not order_api_ok:
        result["block_reason"] = "주문 API 오류"
        return result

    vi_gap = detect_vi_or_gap(df_1min)
    if vi_gap["vi_suspected"]:
        result["block_reason"] = f"VI 발동 감지: {vi_gap['reason']}"
        return result
    if vi_gap["orderbook_gap_suspected"]:
        result["block_reason"] = f"호가 공백 과다: {vi_gap['reason']}"
        return result

    if daily_pnl_pct is not None and daily_pnl_pct <= _DAILY_LOSS_LIMIT_PCT:
        result["block_reason"] = f"일 누적 손실 {daily_pnl_pct:.2f}% ≤ {_DAILY_LOSS_LIMIT_PCT:.1f}% — 강제거래 중단"
        return result

    if decision_result.get("final_action") == "HOLD" and decision_result.get("score_gap_below_forced_trade_threshold"):
        result["block_reason"] = f"보류 + 양방향 점수차 {decision_result.get('score_gap')} < 5점 — 강제거래 skip"
        return result

    result["should_force"] = True
    result["window"] = window
    if decision_result.get("final_action") == "HOLD":
        result["forced_direction"] = resolve_forced_direction(decision_result)
    return result
