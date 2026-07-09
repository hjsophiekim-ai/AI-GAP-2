"""
hynix_position_common.py — 하이닉스/인버스 포지션 감지 공용 로직.

Enhanced 시스템(hynix_switch_*)과 레거시 제안형 시스템(hynix_auto_trade_service)이
동일한 포지션 판정 로직을 공유하기 위한 모듈이다. 여기 정의된 함수만 사용하고
각 시스템에서 별도로 "000660만 찾는" 코드를 중복 작성하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.services.hynix_auto_trade_service import HYNIX_SYMBOL, HYNIX_NAME
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME

TRADE_SYMBOLS = [HYNIX_SYMBOL, INVERSE_SYMBOL]
SYMBOL_NAME = {HYNIX_SYMBOL: HYNIX_NAME, INVERSE_SYMBOL: INVERSE_NAME}

POSITION_HYNIX = "HYNIX"
POSITION_INVERSE = "INVERSE"
POSITION_NONE = "NONE"
POSITION_CONFLICT = "CONFLICT"

MIN_SECONDS_BETWEEN_BUYS = 180


def _attr(position, key, default=None):
    if isinstance(position, dict):
        return position.get(key, default)
    return getattr(position, key, default)


def get_hynix_auto_position(positions: list) -> dict:
    """자동매매 유니버스(000660/0197X0) 내 현재 보유 상태를 판정한다.

    Parameters
    ----------
    positions : list[Position] 또는 list[dict] — symbol/quantity 속성(키)을 가진 목록

    Returns
    -------
    dict: {
        "current_position": "HYNIX"|"INVERSE"|"NONE"|"CONFLICT",
        "position": 해당 Position(단일 보유 시)| None,
        "hynix_position": Position|None, "inverse_position": Position|None,
        "error": str|None (CONFLICT일 때만),
    }
    """
    hynix_pos = next((p for p in positions if _attr(p, "symbol") == HYNIX_SYMBOL and (_attr(p, "quantity") or 0) > 0), None)
    inverse_pos = next((p for p in positions if _attr(p, "symbol") == INVERSE_SYMBOL and (_attr(p, "quantity") or 0) > 0), None)

    if hynix_pos and inverse_pos:
        return {
            "current_position": POSITION_CONFLICT, "position": None,
            "hynix_position": hynix_pos, "inverse_position": inverse_pos,
            "error": "000660과 0197X0을 동시에 보유 중 — 포지션 동기화 필요, 신규매수 금지",
        }
    if hynix_pos:
        return {"current_position": POSITION_HYNIX, "position": hynix_pos, "hynix_position": hynix_pos, "inverse_position": None, "error": None}
    if inverse_pos:
        return {"current_position": POSITION_INVERSE, "position": inverse_pos, "hynix_position": None, "inverse_position": inverse_pos, "error": None}
    return {"current_position": POSITION_NONE, "position": None, "hynix_position": None, "inverse_position": None, "error": None}


def is_duplicate_buy(current_position: str, final_action: str) -> Optional[str]:
    """이미 보유 중인 방향으로 또 매수 신호가 나오면 차단 사유를 반환(문제 없으면 None)."""
    if current_position == POSITION_HYNIX and final_action in ("HYNIX_STRONG_BUY", "HYNIX_BUY"):
        return "이미 하이닉스 보유 중 — 중복 매수 방지"
    if current_position == POSITION_INVERSE and final_action in ("INVERSE_STRONG_BUY", "INVERSE_BUY"):
        return "이미 인버스 보유 중 — 중복 매수 방지"
    return None


def is_buy_cooldown_active(last_trade_time: Optional[str], last_action: Optional[str], now: Optional[datetime] = None) -> bool:
    """마지막 매수 주문 후 최소 대기시간(기본 180초) 이내면 True(신규 매수만 차단, 매도는 항상 허용)."""
    if not last_trade_time or not last_action or "BUY" not in str(last_action).upper():
        return False
    now = now or datetime.now()
    try:
        last_dt = datetime.fromisoformat(last_trade_time)
    except Exception:
        return False
    return (now - last_dt) < timedelta(seconds=MIN_SECONDS_BETWEEN_BUYS)


def reset_mock_daily_state_if_new_day(state: dict, default_budget_krw: float = 10_000_000.0) -> dict:
    """state['date']가 오늘이 아니면 mock 전용 필드(현금/포지션/거래횟수)를 초기화한다."""
    today = datetime.now().strftime("%Y%m%d")
    if state.get("date") == today:
        return state
    state["date"] = today
    state["cash"] = state.get("mock_budget_krw", default_budget_krw)
    state["mock_budget_krw"] = state.get("mock_budget_krw", default_budget_krw)
    state["daily_trade_count"] = 0
    state["realized_pnl_today_krw"] = 0.0
    state["realized_pnl_today_pct"] = 0.0
    state["trades_today"] = []
    state["fired_windows"] = []
    state["liquidation_done"] = False
    state["liquidation_mode"] = False
    state["daily_pnl_baseline_equity"] = None
    state["last_order_cycle_bucket"] = None
    state["last_order_signature"] = None
    state["critical_alert"] = None
    return state
