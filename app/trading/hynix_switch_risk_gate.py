"""
hynix_switch_risk_gate.py — 강제거래 시간창 판정 + VI/호가공백 감지 + 차단조건 통합.

09:00~09:10 관망, 09:10~14:50 신규진입 가능, 14:50 이후 신규매수 금지,
15:10 청산모드, 15:15 강제청산, 15:20 이후 신규주문 금지의 시간모델을 담당한다.
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
    "watch_only_start": "09:00",
    "watch_only_end": "09:10",
    "forced_trade_windows": [["09:10", "09:30"], ["10:30", "11:00"], ["13:30", "14:30"]],
    "entry_cutoff_time": "14:50",
    "liquidation_prep_time": "15:05",
    "liquidation_mode_time": "15:10",
    "liquidation_time": "15:15",
    "no_new_order_time": "15:20",
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


def is_watch_only(now: Optional[datetime] = None) -> bool:
    now = now or kst_now()
    sched = _load_schedule()
    return _parse_hm(sched["watch_only_start"]) <= now.time() < _parse_hm(sched["watch_only_end"])


def is_new_entry_allowed(now: Optional[datetime] = None) -> bool:
    """09:10~14:50 구간에서만 True (스위칭의 재매수 레그에도 동일 적용)."""
    now = now or kst_now()
    sched = _load_schedule()
    return _parse_hm(sched["watch_only_end"]) <= now.time() < _parse_hm(sched["entry_cutoff_time"])


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

    if is_watch_only(now):
        result["block_reason"] = "09:00~09:10 관망 구간"
        return result
    if not is_new_entry_allowed(now):
        result["block_reason"] = "14:50 이후 — 신규 진입 강제거래 불가"
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
