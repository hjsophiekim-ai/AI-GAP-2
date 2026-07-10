"""
dynamic_exit_watcher.py — Dynamic Exit AI를 1초 주기 백그라운드 스레드로 실행.

Streamlit의 스크립트 재실행 모델과 무관하게 동작하도록 daemon thread로 구현했다.
스레드는 `data/state/hynix_auto_state.json`(파일)을 통해서만 Streamlit 세션과
상태를 주고받는다 — 별도 프로세스 간 공유 메모리가 필요 없다.

한계: 이 스레드는 앱을 서빙하는 파이썬 프로세스가 살아있는 동안만 동작한다.
프로세스가 재시작되면 스레드도 함께 사라지며, `ensure_watcher_running()`을
다시 호출해야 한다(Streamlit 페이지 로드 시 매번 호출하도록 되어 있어 실질적으로
페이지가 열려 있는 동안은 자동 복구된다).
"""

from __future__ import annotations

import csv
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.logger import logger
from app.trading.dynamic_exit_engine import DynamicExitEngine
from app.services.hynix_switch_state import load_state, save_state_atomic
from app.trading.hynix_switch_position_manager import _sell_all_or_ratio, _SYMBOL_NAME
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL

ROOT = Path(__file__).resolve().parent.parent.parent
_EXIT_LOG_PATH = ROOT / "data" / "logs" / "exit_engine_log.csv"
_EXIT_LOG_COLUMNS = [
    "timestamp", "symbol", "entry_price", "current_price", "profit_pct", "market_type",
    "tp", "sl", "trailing_stop", "profit_lock", "exit_score", "action", "reason",
]

_engine = DynamicExitEngine()


def _no_position_decision() -> dict:
    """포지션이 없을 때의 Dynamic Exit 판단 — 과거 SELL_ALL 등 유령 판단 방지용."""
    return {
        "action": "NO_POSITION", "reason": "보유 포지션 없음", "entry_time": None,
        "holding_minutes": 0, "exit_score": 0, "ratio": 0.0,
        "tp_pct": None, "sl_pct": None, "trailing_pct": None, "trailing_armed": False,
        "profit_lock_floor_pct": None, "market_type": None, "score_breakdown": {},
    }


def _fetch_current_price(symbol: str, mode: str) -> Optional[float]:
    if symbol == HYNIX_SYMBOL:
        return _fetch_hynix_price_cheap(mode)
    if symbol == INVERSE_SYMBOL:
        from app.data_sources.hynix_inverse_collector import collect_inverse_current

        return collect_inverse_current(mode=mode).get("current_price")
    return None


def _fetch_hynix_price_cheap(mode: str) -> Optional[float]:
    import os

    for candidate in (mode, "real", "mock"):
        if not candidate:
            continue
        app_key = os.environ.get(f"KIS_{candidate.upper()}_APP_KEY", "")
        app_secret = os.environ.get(f"KIS_{candidate.upper()}_APP_SECRET", "")
        if app_key and app_secret:
            try:
                from app.trading.kis_client import KISClient

                client = KISClient(
                    app_key=app_key, app_secret=app_secret,
                    account_no=os.environ.get("KIS_ACCOUNT_NO", "00000000"),
                    product_code=os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", "01"), mode=candidate,
                )
                quote = client.get_current_price(HYNIX_SYMBOL)
                if quote and quote.get("current_price"):
                    return quote["current_price"]
            except Exception as exc:
                logger.debug("[DynamicExitWatcher] KIS 현재가 실패: %s", exc)
            break
    try:
        from app.data_sources.auto_market_collector import _load_hynix_current_cache

        cached = _load_hynix_current_cache()
        return cached.get("current_price") if cached else None
    except Exception:
        return None


def _load_daily_df(symbol: str):
    if symbol != HYNIX_SYMBOL:
        return None  # 인버스 ETN은 일봉 캐시를 별도 수집하지 않음(1분봉 기반 신호만 사용)
    try:
        from app.data_sources.auto_market_collector import _load_hynix_daily_cache

        return _load_hynix_daily_cache()
    except Exception:
        return None


def _load_minute_df(symbol: str):
    try:
        if symbol == HYNIX_SYMBOL:
            from app.data_sources.auto_market_collector import _load_hynix_minute_cache

            return _load_hynix_minute_cache()
        if symbol == INVERSE_SYMBOL:
            from app.data_sources.hynix_inverse_collector import _load_inverse_minute_cache

            return _load_inverse_minute_cache()
    except Exception as exc:
        logger.debug("[DynamicExitWatcher] 분봉 캐시 로드 실패: %s", exc)
    return None


def _append_exit_log(row: dict) -> None:
    try:
        _EXIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        is_new = not _EXIT_LOG_PATH.exists()
        with _EXIT_LOG_PATH.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_EXIT_LOG_COLUMNS)
            if is_new:
                writer.writeheader()
            writer.writerow({col: row.get(col, "") for col in _EXIT_LOG_COLUMNS})
    except Exception as exc:
        logger.debug("[DynamicExitWatcher] exit_engine_log 기록 실패: %s", exc)


_broker_cache: dict = {}  # mode -> (broker, created_at_monotonic)
_position_manager_cache: dict = {}  # mode -> HynixPositionManager
_BROKER_CACHE_TTL_SECONDS = 30.0  # real 모드에서 매초 새 KIS 클라이언트/토큰을 만들지 않기 위한 재사용


def _get_cached_broker(mode: str, mock_budget_krw: float):
    import time

    entry = _broker_cache.get(mode)
    now_mono = time.monotonic()
    if entry is not None and (now_mono - entry[1]) < _BROKER_CACHE_TTL_SECONDS:
        return entry[0]

    if mode == "mock":
        from app.trading.dry_run_broker import DryRunBroker

        broker = DryRunBroker(initial_balance=mock_budget_krw)
    else:
        from app.config import get_config
        from app.trading.broker_factory import create_broker

        broker = create_broker(
            get_config(), mode="real",
            runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
        )
    _broker_cache[mode] = (broker, now_mono)
    _position_manager_cache.pop(mode, None)  # 브로커가 바뀌었으니 매니저도 새로 만든다
    return broker


def _get_position_manager(broker, mode: str):
    from app.trading.hynix_position_common import HynixPositionManager

    pm = _position_manager_cache.get(mode)
    if pm is None or pm.broker is not broker:
        pm = HynixPositionManager(broker, mode=mode)
        _position_manager_cache[mode] = pm
    return pm


def tick(now: Optional[datetime] = None, engine: Optional[DynamicExitEngine] = None) -> Optional[dict]:
    """1회 감시 실행(테스트 및 스레드 루프에서 공용으로 사용하는 순수 함수).

    Broker → PositionManager → State(캐시) 순서로만 데이터가 흐른다. 이 함수는
    보유 포지션 판정을 위해 state를 직접 신뢰하지 않고, 매 틱 PositionManager를
    통해 브로커를 확인(mock은 항상 새로고침, real은 5초 TTL 캐시)한 뒤에만 판단한다.
    """
    from app.trading.hynix_switch_position_manager import apply_position_manager_to_state

    now = now or datetime.now()
    engine = engine or _engine
    state = load_state()

    if not state.get("auto_trade_on") or state.get("stopped"):
        return None

    mode = state.get("mode", "mock")
    try:
        broker = _get_cached_broker(mode, state.get("mock_budget_krw", 10_000_000.0))
        position_manager = _get_position_manager(broker, mode)
        position_manager.sync()  # mock은 항상 새로고침, real은 내부 TTL(5초) 적용
        apply_position_manager_to_state(state, position_manager)
    except Exception as exc:
        logger.warning("[DynamicExitWatcher] PositionManager 동기화 실패, 이번 틱은 스킵: %s", exc)
        return None

    position = state.get("position") or {}
    symbol = position.get("symbol")
    if not symbol or (position.get("quantity") or 0) <= 0:
        # 포지션이 없으면 과거(청산 직전) 판단이 화면에 유령처럼 남지 않도록 즉시 초기화한다.
        state["dynamic_exit_last_decision"] = _no_position_decision()
        save_state_atomic(state)
        return None

    current_price = _fetch_current_price(symbol, mode)
    if not current_price:
        return None

    df_daily = _load_daily_df(symbol)
    df_1min = _load_minute_df(symbol)

    decision = engine.decide(position, df_daily, df_1min, current_price, now)
    state["position"] = position
    state["dynamic_exit_last_decision"] = {k: v for k, v in decision.items() if k != "snapshot"}

    if decision["action"] in ("SELL_ALL", "SELL_PARTIAL"):
        from app.trading.hynix_stop_loss_control import (
            STOP_LOSS_MODE_AUTO, check_auto_stop_loss_safety, verify_order_confirmed, log_stop_loss_event,
        )

        stop_loss_mode = state.get("stop_loss_mode", STOP_LOSS_MODE_AUTO)
        order_sent = False
        order_confirmed = False
        block_reason = None

        if stop_loss_mode != STOP_LOSS_MODE_AUTO:
            block_reason = f"손절모드={stop_loss_mode} — 자동매도 없이 알림만"
            state["pending_manual_stop_loss_alert"] = {
                "symbol": symbol, "name": position.get("name"), "action": decision["action"],
                "reason": decision["reason"], "current_price": current_price,
                "detected_at": now.isoformat(),
            }
        elif mode == "real":
            safety = check_auto_stop_loss_safety(state, mode, position_manager, symbol, now)
            if not safety["ok"]:
                block_reason = "real 자동손절 안전조건 미충족: " + "; ".join(safety["failed_checks"])
                state["pending_manual_stop_loss_alert"] = {
                    "symbol": symbol, "name": position.get("name"), "action": decision["action"],
                    "reason": block_reason, "current_price": current_price, "detected_at": now.isoformat(),
                }

        if block_reason is None:
            from app.trading.exit_order_coordinator import classify_exit_reason

            orders: list = []
            exit_reason_type = classify_exit_reason(decision["reason"])
            order_result = _sell_all_or_ratio(
                broker, position, current_price, decision["ratio"], decision["reason"], orders,
                mode=mode, exit_reason_type=exit_reason_type,
            )
            order_sent = bool(order_result.get("success"))
            if order_sent:
                sold_qty = order_result.get("sold_quantity", 0)
                realized = (current_price - (position.get("entry_price") or current_price)) * sold_qty
                state["realized_pnl_today_krw"] = state.get("realized_pnl_today_krw", 0.0) + realized
                state["last_sell_price"] = current_price
                state["last_trade_time"] = now.isoformat()
                state["last_stop_loss_signature"] = f"{symbol}:{now.strftime('%Y%m%d%H%M')}"
                state["pending_manual_stop_loss_alert"] = None

                # 매도 직후 "추정된 결과"가 아니라 브로커를 다시 조회해 확정한다(SoT 원칙).
                order_confirmed = verify_order_confirmed(position_manager, symbol, expect_cleared=(decision["action"] == "SELL_ALL"))
                apply_position_manager_to_state(state, position_manager)

            _append_exit_log({
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"), "symbol": symbol,
                "entry_price": position.get("entry_price"), "current_price": current_price,
                "profit_pct": decision["snapshot"].get("profit_pct"), "market_type": decision["market_type"],
                "tp": decision["tp_pct"], "sl": decision["sl_pct"], "trailing_stop": decision["trailing_armed"],
                "profit_lock": decision.get("profit_lock_floor_pct"), "exit_score": decision["exit_score"],
                "action": decision["action"], "reason": decision["reason"],
            })

        entry_price = position.get("entry_price")
        sl_pct = decision.get("sl_pct")
        tp_pct = decision.get("tp_pct")
        log_stop_loss_event({
            "mode": mode, "symbol": symbol, "name": position.get("name"),
            "entry_price": entry_price, "current_price": current_price,
            "stop_loss_price": (entry_price * (1 - sl_pct / 100)) if (entry_price and sl_pct is not None) else "",
            "stop_loss_pct": sl_pct,
            "take_profit_price": (entry_price * (1 + tp_pct / 100)) if (entry_price and tp_pct is not None) else "",
            "take_profit_pct": tp_pct,
            "stop_mode": stop_loss_mode, "action": decision["action"],
            "order_sent": order_sent, "order_confirmed": order_confirmed,
            "reason": block_reason or decision["reason"],
        })

    save_state_atomic(state)
    return decision


class DynamicExitWatcher(threading.Thread):
    def __init__(self, interval_seconds: float = 1.0):
        super().__init__(daemon=True, name="DynamicExitWatcher")
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("[DynamicExitWatcher] 백그라운드 감시 시작(%.1f초 주기)", self.interval_seconds)
        while not self._stop_event.is_set():
            try:
                tick()
            except Exception as exc:
                logger.error("[DynamicExitWatcher] tick 실패: %s", exc)
            try:
                # 자기 자신은 이 루프가 돌고 있다는 것 자체로 살아있음이 증명되므로,
                # 여기서는 "3분 자동매매 사이클" 스레드가 죽어있지 않은지만 함께 확인/재시작한다.
                from app.services.hynix_auto_trade_scheduler import ensure_cycle_thread_running

                ensure_cycle_thread_running()
            except Exception as exc:
                logger.error("[DynamicExitWatcher] 사이클 스레드 헬스체크 실패: %s", exc)
            self._stop_event.wait(self.interval_seconds)
        logger.info("[DynamicExitWatcher] 백그라운드 감시 종료")


_watcher_lock = threading.Lock()
_watcher_instance: Optional[DynamicExitWatcher] = None


def ensure_watcher_running(interval_seconds: float = 1.0) -> DynamicExitWatcher:
    """감시 스레드가 없거나 죽어 있으면 새로 시작한다(이미 실행 중이면 그대로 반환, 중복 실행 없음)."""
    global _watcher_instance
    with _watcher_lock:
        if _watcher_instance is None or not _watcher_instance.is_alive():
            _watcher_instance = DynamicExitWatcher(interval_seconds=interval_seconds)
            _watcher_instance.start()
        return _watcher_instance


def stop_watcher() -> None:
    global _watcher_instance
    with _watcher_lock:
        if _watcher_instance is not None:
            _watcher_instance.stop()
            _watcher_instance = None


def is_watcher_running() -> bool:
    return _watcher_instance is not None and _watcher_instance.is_alive()
