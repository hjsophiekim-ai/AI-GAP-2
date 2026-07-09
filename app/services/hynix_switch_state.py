"""
hynix_switch_state.py — 하이닉스⇄인버스 자동매매 상태 저장/복구.

mock과 real은 완전히 분리된 파일에 저장한다(`hynix_auto_state_mock.json`,
`hynix_auto_state_real.json`) — mock 거래가 real 화면에 섞이거나 반대로 섞이는
사고를 방지한다. 어느 파일을 볼지는 `active_mode` 포인터 파일로 판단하며,
UI에서 모드를 바꾸면 이 포인터도 함께 갱신된다. 손상/누락 시 예외를 던지지
않고 안전 기본값으로 복구하며, 저장은 임시파일 write 후 os.replace()로
원자적으로 수행한다.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.logger import logger

ROOT = Path(__file__).resolve().parent.parent.parent
_STATE_DIR = ROOT / "data" / "state"

_DEFAULT_MOCK_BUDGET_KRW = 10_000_000.0


def _today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _state_path(mode: str) -> Path:
    mode = mode if mode in ("mock", "real") else "mock"
    return _STATE_DIR / f"hynix_auto_state_{mode}.json"


def _active_mode_pointer_path() -> Path:
    return _STATE_DIR / "hynix_auto_state_active_mode.json"


def get_active_mode() -> str:
    """UI가 마지막으로 선택한 mode(mock/real). 포인터가 없으면 mock."""
    try:
        path = _active_mode_pointer_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("mode", "mock") if data.get("mode") in ("mock", "real") else "mock"
    except Exception as exc:
        logger.debug("[HynixSwitchState] active_mode 포인터 로드 실패: %s", exc)
    return "mock"


def set_active_mode(mode: str) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _active_mode_pointer_path().write_text(json.dumps({"mode": mode}), encoding="utf-8")
    except Exception as exc:
        logger.debug("[HynixSwitchState] active_mode 포인터 저장 실패: %s", exc)


def _empty_position() -> dict:
    return {
        "symbol": None, "name": None, "quantity": 0, "avg_price": None, "entry_price": None,
        "entry_time": None, "partial_tp1_done": False, "partial_sl1_done": False,
        "highest_price": None, "lowest_price": None,
        "trailing_armed": False, "trailing_peak_price": None,
        "profit_lock_peak_pct": 0.0,
    }


def default_state(mode: str = "mock") -> dict:
    return {
        "date": _today_str(),
        "mode": mode,
        "position": _empty_position(),
        # 사용자 지정 평면(flat) 필드 — position 내용과 저장 시 동기화됨
        "current_position": None,
        "current_position_type": "NONE",
        "symbol": None,
        "name": None,
        "entry_price": None,
        "quantity": 0,
        "entry_time": None,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "daily_trade_count": 0,
        "liquidation_done": False,
        # 예산/현금(mock 로컬 시뮬레이션 예산, real은 브로커 조회값으로 매 사이클 갱신)
        "mock_budget_krw": _DEFAULT_MOCK_BUDGET_KRW,
        "cash": _DEFAULT_MOCK_BUDGET_KRW if mode == "mock" else None,
        # 내부 운영 필드
        "trades_today": [],
        "realized_pnl_today_krw": 0.0,
        "realized_pnl_today_pct": 0.0,
        "daily_pnl_baseline_equity": None,
        "fired_windows": [],
        "liquidation_mode": False,
        "residual_position_error": False,
        "position_conflict": False,
        "critical_alert": None,
        "auto_trade_on": False,
        "weight_auto_apply_enabled": False,
        "daily_report_generated_date": None,
        "stopped": False,
        "stopped_reason": None,
        "last_order_cycle_bucket": None,
        "last_order_signature": None,
        "last_buy_price": None,
        "last_sell_price": None,
        "last_trade_time": None,
        "last_action": None,
        "last_order_id": None,
    }


def _sync_flat_fields(state: dict) -> None:
    pos = state.get("position") or {}
    symbol = pos.get("symbol")
    state["current_position"] = symbol
    state["current_position_type"] = (
        "HYNIX" if symbol == "000660" else "INVERSE" if symbol == "0197X0" else "NONE"
    )
    state["symbol"] = symbol
    state["name"] = pos.get("name")
    state["entry_price"] = pos.get("entry_price")
    state["quantity"] = pos.get("quantity", 0)
    state["entry_time"] = pos.get("entry_time")
    state["realized_pnl"] = state.get("realized_pnl_today_krw", 0.0)


def load_state(mode: Optional[str] = None) -> dict:
    """상태 로드. mode를 지정하지 않으면 활성 모드(active_mode 포인터)를 사용.

    파일 없음/손상 시 예외 없이 안전 기본값 반환.
    """
    mode = mode or get_active_mode()
    path = _state_path(mode)
    try:
        if not path.exists():
            return default_state(mode)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state 파일이 dict 형식이 아님")

        defaults = default_state(mode)
        state = {**defaults, **raw}
        state["mode"] = mode
        state["position"] = {**_empty_position(), **(raw.get("position") or {})}

        today = _today_str()
        if state.get("date") != today:
            pos = state["position"]
            if pos.get("symbol") and (pos.get("quantity") or 0) > 0:
                state["residual_position_error"] = True
                logger.error(
                    "[HynixSwitchState] 전일 포지션이 청산되지 않고 남아있음(프로그램 오류 의심, mode=%s): %s",
                    mode, pos,
                )
            state["date"] = today
            state["daily_trade_count"] = 0
            state["trades_today"] = []
            state["realized_pnl_today_krw"] = 0.0
            state["realized_pnl_today_pct"] = 0.0
            state["fired_windows"] = []
            state["liquidation_done"] = False
            state["liquidation_mode"] = False
            state["daily_pnl_baseline_equity"] = None
            state["last_order_cycle_bucket"] = None
            state["last_order_signature"] = None
            state["critical_alert"] = None
            if mode == "mock":
                state["cash"] = state.get("mock_budget_krw", _DEFAULT_MOCK_BUDGET_KRW)

        _sync_flat_fields(state)
        return state
    except Exception as exc:
        logger.error("[HynixSwitchState] 상태 로드 실패(mode=%s) — 안전 기본값으로 복구: %s", mode, exc)
        return default_state(mode)


def save_state_atomic(state: dict) -> None:
    """상태를 원자적으로 저장(임시파일 write 후 os.replace). mode별 파일에 저장."""
    try:
        _sync_flat_fields(state)
        mode = state.get("mode", "mock")
        path = _state_path(mode)
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.error("[HynixSwitchState] 상태 저장 실패: %s", exc)


def reset_mock_state(budget_krw: Optional[float] = None) -> dict:
    """'mock 계좌 초기화' 버튼 — mock 상태를 완전히 새로 시작한다(포지션/거래횟수/현금 리셋)."""
    state = default_state("mock")
    if budget_krw is not None:
        state["mock_budget_krw"] = float(budget_krw)
        state["cash"] = float(budget_krw)
    save_state_atomic(state)

    try:
        from app.trading.dry_run_broker import _DATA_DIR

        dry_run_path = _DATA_DIR / f"{_today_str()}_dry_portfolio.json"
        if dry_run_path.exists():
            dry_run_path.unlink()
    except Exception as exc:
        logger.debug("[HynixSwitchState] DryRunBroker 포트폴리오 파일 삭제 실패(무해): %s", exc)

    return state
