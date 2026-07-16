"""
hynix_stop_loss_control.py — 손절 실행 방식 제어(자동/수동알림/일괄수동) + 손절 로그.

세 가지 모드:
  AUTO         — 손절가 도달 시 시스템이 즉시 자동 매도(real은 6가지 안전조건 통과해야만).
  ALERT_ONLY   — 자동 매도하지 않고 화면/로그/상태에 알림만 남긴다. 사용자가 직접 매도해야 한다.
  BATCH_MANUAL — ALERT_ONLY와 동일하게 자동 매도는 하지 않되, UI의 "일괄 수동손절" 버튼으로
                 언제든 즉시 전량 청산할 수 있다(버튼 자체는 모드와 무관하게 항상 노출됨).

Dynamic Exit AI(1초 감시)는 이 모듈을 통해서만 실제 매도를 실행하며, real 모드의 자동매도는
`check_auto_stop_loss_safety()`가 6개 조건을 모두 통과해야만 진행한다.
"""

from __future__ import annotations

import csv
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.utils.time_utils import kst_now
from app.utils.data_paths import LOGS_DIR
from app.data_sources.hynix_long_collector import LONG_SYMBOL as HYNIX_SYMBOL, LONG_NAME as HYNIX_NAME
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME

STOP_LOSS_MODE_AUTO = "AUTO"
STOP_LOSS_MODE_ALERT_ONLY = "ALERT_ONLY"
STOP_LOSS_MODE_BATCH_MANUAL = "BATCH_MANUAL"
STOP_LOSS_MODES = [STOP_LOSS_MODE_AUTO, STOP_LOSS_MODE_ALERT_ONLY, STOP_LOSS_MODE_BATCH_MANUAL]

STOP_LOSS_MODE_LABELS = {
    STOP_LOSS_MODE_AUTO: "A. 자동손절 ON",
    STOP_LOSS_MODE_ALERT_ONLY: "B. 수동손절 알림만",
    STOP_LOSS_MODE_BATCH_MANUAL: "C. 일괄 수동손절",
}

_SYMBOL_NAME = {HYNIX_SYMBOL: HYNIX_NAME, INVERSE_SYMBOL: INVERSE_NAME}

ROOT = Path(__file__).resolve().parent.parent.parent
_STOP_LOSS_LOG_PATH = LOGS_DIR / "stop_loss_log.csv"
_STOP_LOSS_LOG_COLUMNS = [
    "timestamp", "mode", "symbol", "name", "entry_price", "current_price",
    "stop_loss_price", "stop_loss_pct", "take_profit_price", "take_profit_pct",
    "stop_mode", "action", "order_sent", "order_confirmed", "reason",
]

_FORCED_LIQUIDATION_LOG_PATH = LOGS_DIR / "forced_liquidation_log.csv"
_FORCED_LIQUIDATION_LOG_COLUMNS = [
    "timestamp", "mode", "symbol", "quantity", "entry_price", "current_price",
    "liquidation_attempted", "order_sent", "order_confirmed", "result", "reason",
]

_MARKET_OPEN = dtime(9, 0)
_MARKET_ORDER_CUTOFF = dtime(15, 20)


def is_order_time_allowed(now: Optional[datetime] = None) -> bool:
    """대략적인 KRX 주문 가능 시간(09:00~15:20). 손절 매도 주문 실행 전 공통 체크."""
    now = now or kst_now()
    return _MARKET_OPEN <= now.time() <= _MARKET_ORDER_CUTOFF


def log_stop_loss_event(record: dict) -> None:
    """data/logs/stop_loss_log.csv 에 append (항상 기록 — 실제 매도 여부와 무관)."""
    try:
        _STOP_LOSS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not _STOP_LOSS_LOG_PATH.exists()
        row = dict(record)
        row.setdefault("timestamp", kst_now().strftime("%Y-%m-%d %H:%M:%S"))
        with _STOP_LOSS_LOG_PATH.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_STOP_LOSS_LOG_COLUMNS)
            if is_new:
                writer.writeheader()
            writer.writerow({col: row.get(col, "") for col in _STOP_LOSS_LOG_COLUMNS})
    except Exception as exc:
        logger.debug("[StopLossControl] stop_loss_log 기록 실패: %s", exc)


def log_forced_liquidation_event(record: dict) -> None:
    """data/logs/forced_liquidation_log.csv 에 append (15:15 당일 강제청산 시도는 항상 기록)."""
    try:
        _FORCED_LIQUIDATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not _FORCED_LIQUIDATION_LOG_PATH.exists()
        row = dict(record)
        row.setdefault("timestamp", kst_now().strftime("%Y-%m-%d %H:%M:%S"))
        with _FORCED_LIQUIDATION_LOG_PATH.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_FORCED_LIQUIDATION_LOG_COLUMNS)
            if is_new:
                writer.writeheader()
            writer.writerow({col: row.get(col, "") for col in _FORCED_LIQUIDATION_LOG_COLUMNS})
    except Exception as exc:
        logger.debug("[StopLossControl] forced_liquidation_log 기록 실패: %s", exc)


def apply_stop_loss_mode_gate(
    state: dict, mode: str, symbol: str, position_manager, action_label: str,
    current_price: Optional[float] = None, now: Optional[datetime] = None,
) -> dict:
    """손절성 매도(Dynamic Exit AI SL / 레거시 TP·SL의 SL / 15:15 강제청산) 공용 게이트.

    AUTO가 아니면 무조건 차단(알림만 남김). AUTO+real이면 6가지 안전조건까지 확인한다.
    AUTO+mock은 즉시 통과. 반환: {"blocked": bool, "reason": str|None}.
    """
    now = now or kst_now()
    stop_loss_mode = state.get("stop_loss_mode", STOP_LOSS_MODE_AUTO)

    if stop_loss_mode != STOP_LOSS_MODE_AUTO:
        reason = f"손절모드={stop_loss_mode} — 자동매도 없이 알림만"
        state["pending_manual_stop_loss_alert"] = {
            "symbol": symbol, "action": action_label, "reason": reason,
            "current_price": current_price, "detected_at": now.isoformat(),
        }
        return {"blocked": True, "reason": reason}

    if mode == "real" and position_manager is not None:
        safety = check_auto_stop_loss_safety(state, mode, position_manager, symbol, now)
        if not safety["ok"]:
            reason = "real 자동손절 안전조건 미충족: " + "; ".join(safety["failed_checks"])
            state["pending_manual_stop_loss_alert"] = {
                "symbol": symbol, "action": action_label, "reason": reason,
                "current_price": current_price, "detected_at": now.isoformat(),
            }
            return {"blocked": True, "reason": reason}

    return {"blocked": False, "reason": None}


def check_auto_stop_loss_safety(
    state: dict, mode: str, position_manager, symbol: Optional[str], now: Optional[datetime] = None,
) -> dict:
    """real 자동손절 실행 전 6가지 조건을 모두 확인한다. 하나라도 실패하면 진행 금지.

    조건: ①real 자동매매 ON ②자동손절(AUTO) 모드 ③실제 계좌에 해당 종목 보유 확인
    ④(호출부에서 손절가 도달을 이미 확인했다는 전제) ⑤주문 가능 시간 ⑥중복 매도 주문 없음.
    """
    now = now or kst_now()
    failed: list[str] = []

    if mode == "real" and not state.get("auto_trade_on"):
        failed.append("real 자동매매가 OFF 상태")
    if state.get("stop_loss_mode", STOP_LOSS_MODE_AUTO) != STOP_LOSS_MODE_AUTO:
        failed.append(f"손절 모드가 AUTO가 아님(현재: {state.get('stop_loss_mode')})")
    if not symbol:
        failed.append("현재 보유 종목 없음")
    else:
        held_symbol = position_manager.current_position.get("symbol")
        held_qty = position_manager.current_position.get("quantity") or 0
        if held_symbol != symbol or held_qty <= 0:
            failed.append(f"브로커 기준 실제 보유 확인 실패(symbol={held_symbol}, qty={held_qty})")
    if not is_order_time_allowed(now):
        failed.append(f"주문 가능 시간 아님({now.strftime('%H:%M')})")
    last_stop_loss_signature = state.get("last_stop_loss_signature")
    current_bucket = now.strftime("%Y%m%d%H%M")
    if last_stop_loss_signature == f"{symbol}:{current_bucket}":
        failed.append("동일 분(bucket) 내 중복 손절 매도 주문 방지")

    return {"ok": len(failed) == 0, "failed_checks": failed}


def verify_order_confirmed(position_manager, symbol: str, expect_cleared: bool = True) -> bool:
    """매도 주문 성공 응답만 믿지 않고, 브로커를 재조회해 실제로 수량이 빠졌는지 확인한다."""
    try:
        position_manager.sync(force=True)
    except Exception as exc:
        logger.warning("[StopLossControl] 체결 확인용 재조회 실패: %s", exc)
        return False
    current = position_manager.current_position
    if expect_cleared:
        return current.get("symbol") != symbol or (current.get("quantity") or 0) <= 0
    return True


def execute_manual_stop_loss(mode: str, symbol_filter: Optional[str] = None) -> dict:
    """'일괄 수동손절' 버튼 핸들러. symbol_filter=None이면 하이닉스+인버스 전량 청산.

    1) mode 확인 2) 해당 mode 브로커로 실제 보유수량 조회 3) 현재가 조회 후 매도
    4) 재조회로 체결 확인 5) 로그 기록 6) 결과 반환.
    """
    from app.services.hynix_switch_state import load_state, save_state_atomic
    from app.trading.hynix_position_common import HynixPositionManager
    from app.trading.hynix_switch_position_manager import apply_position_manager_to_state
    from app.trading.dynamic_exit_watcher import _get_cached_broker, _fetch_current_price

    now = kst_now()
    state = load_state(mode=mode)
    results: list[dict] = []

    try:
        broker = _get_cached_broker(mode, state.get("mock_budget_krw", 10_000_000.0))
    except Exception as exc:
        return {"success": False, "message": f"브로커 초기화 실패: {exc}", "results": results}

    position_manager = HynixPositionManager(broker, mode=mode)
    position_manager.sync(force=True)

    def _raw_held(symbol: str):
        """position_manager.current_position은 단일 종목 보유만 표현하므로(CONFLICT 시 symbol=None),
        청산 대상 보유수량은 브로커 원본 포지션에서 직접 조회한다 — 두 종목을 동시 보유한
        CONFLICT 상태도 이 버튼으로 정상적으로 해소할 수 있어야 한다."""
        try:
            positions = broker.get_positions()
        except Exception:
            positions = []
        for p in positions:
            p_symbol = p.get("symbol") if isinstance(p, dict) else getattr(p, "symbol", None)
            if p_symbol != symbol:
                continue
            qty = p.get("quantity") if isinstance(p, dict) else getattr(p, "quantity", None)
            avg_price = p.get("avg_price") if isinstance(p, dict) else getattr(p, "avg_price", None)
            if (qty or 0) > 0:
                return qty, avg_price
        return 0, None

    targets = [symbol_filter] if symbol_filter else [HYNIX_SYMBOL, INVERSE_SYMBOL]
    for symbol in targets:
        quantity, avg_price = _raw_held(symbol)
        if quantity <= 0:
            results.append({"symbol": symbol, "success": False, "message": "보유 수량 없음(스킵)"})
            continue

        current_price = _fetch_current_price(symbol, mode) or avg_price
        if not current_price:
            results.append({"symbol": symbol, "success": False, "message": "현재가 조회 실패"})
            continue

        name = _SYMBOL_NAME.get(symbol, symbol)
        order = broker.sell(symbol, name, quantity, current_price)
        order_sent = bool(getattr(order, "success", False) or (isinstance(order, dict) and order.get("success")))
        position_manager.sync(force=True)
        order_confirmed = verify_order_confirmed(position_manager, symbol, expect_cleared=True) if order_sent else False

        apply_position_manager_to_state(state, position_manager)
        state["last_sell_price"] = current_price
        state["last_trade_time"] = now.isoformat()
        state["last_action"] = "SELL"

        log_stop_loss_event({
            "mode": mode, "symbol": symbol, "name": name,
            "entry_price": avg_price, "current_price": current_price,
            "stop_loss_price": "", "stop_loss_pct": "", "take_profit_price": "", "take_profit_pct": "",
            "stop_mode": "BATCH_MANUAL", "action": "MANUAL_BATCH_SELL",
            "order_sent": order_sent, "order_confirmed": order_confirmed,
            "reason": "사용자 일괄 수동손절 버튼 클릭",
        })

        results.append({
            "symbol": symbol, "success": order_sent and order_confirmed, "order_sent": order_sent,
            "order_confirmed": order_confirmed, "quantity": quantity, "price": current_price,
        })

    save_state_atomic(state)
    all_ok = all(r.get("success") for r in results if r.get("message") != "보유 수량 없음(스킵)")
    attempted = [r for r in results if r.get("message") != "보유 수량 없음(스킵)"]
    return {
        "success": all_ok if attempted else False,
        "message": "전량 청산 완료" if (all_ok and attempted) else ("청산할 보유 종목 없음" if not attempted else "일부 청산 실패"),
        "results": results,
    }
