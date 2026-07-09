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
from app.services.hynix_switch_state import load_state, save_state_atomic, _empty_position
from app.trading.hynix_switch_position_manager import _sell_all_or_ratio, _SYMBOL_NAME
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL
from app.trading.hynix_position_common import get_hynix_auto_position, POSITION_CONFLICT

ROOT = Path(__file__).resolve().parent.parent.parent
_EXIT_LOG_PATH = ROOT / "data" / "logs" / "exit_engine_log.csv"
_EXIT_LOG_COLUMNS = [
    "timestamp", "symbol", "entry_price", "current_price", "profit_pct", "market_type",
    "tp", "sl", "trailing_stop", "profit_lock", "exit_score", "action", "reason",
]

_engine = DynamicExitEngine()


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


def tick(now: Optional[datetime] = None, engine: Optional[DynamicExitEngine] = None) -> Optional[dict]:
    """1회 감시 실행(테스트 및 스레드 루프에서 공용으로 사용하는 순수 함수)."""
    now = now or datetime.now()
    engine = engine or _engine
    state = load_state()

    if not state.get("auto_trade_on") or state.get("stopped"):
        return None

    position = state.get("position") or {}
    # UI/엔진과 동일한 공용 포지션 판정 로직을 사용(000660/0197X0 어느 쪽이든 동일하게 인식).
    detected = get_hynix_auto_position([position] if position.get("symbol") else [])
    if detected["current_position"] == POSITION_CONFLICT:
        logger.error("[DynamicExitWatcher] %s", detected.get("error"))
        return None
    symbol = position.get("symbol")
    if not symbol or (position.get("quantity") or 0) <= 0:
        return None

    mode = state.get("mode", "mock")
    current_price = _fetch_current_price(symbol, mode)
    if not current_price:
        return None

    df_daily = _load_daily_df(symbol)
    df_1min = _load_minute_df(symbol)

    decision = engine.decide(position, df_daily, df_1min, current_price, now)
    state["position"] = position
    state["dynamic_exit_last_decision"] = {k: v for k, v in decision.items() if k != "snapshot"}

    order_result = None
    if decision["action"] in ("SELL_ALL", "SELL_PARTIAL"):
        try:
            from app.trading.broker_factory import create_broker
            from app.config import get_config

            broker = create_broker(
                get_config(), mode=mode, runtime_real_mode=(mode == "real"),
                runtime_enable_real_buy=(mode == "real"), runtime_enable_real_sell=(mode == "real"),
            )
        except Exception as exc:
            logger.error("[DynamicExitWatcher] 브로커 생성 실패: %s", exc)
            save_state_atomic(state)
            return decision

        orders: list = []
        order_result = _sell_all_or_ratio(broker, position, current_price, decision["ratio"], decision["reason"], orders)
        if order_result.get("success"):
            sold_qty = order_result.get("sold_quantity", 0)
            realized = (current_price - (position.get("entry_price") or current_price)) * sold_qty
            state["realized_pnl_today_krw"] = state.get("realized_pnl_today_krw", 0.0) + realized
            state["daily_trade_count"] = state.get("daily_trade_count", 0) + 1
            state["last_sell_price"] = current_price
            state["last_trade_time"] = now.isoformat()
            if decision["action"] == "SELL_ALL" or order_result.get("remaining_quantity", 0) <= 0:
                state["position"] = _empty_position()
            else:
                position["quantity"] = order_result.get("remaining_quantity")
                state["position"] = position

        _append_exit_log({
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"), "symbol": symbol,
            "entry_price": position.get("entry_price"), "current_price": current_price,
            "profit_pct": decision["snapshot"].get("profit_pct"), "market_type": decision["market_type"],
            "tp": decision["tp_pct"], "sl": decision["sl_pct"], "trailing_stop": decision["trailing_armed"],
            "profit_lock": decision.get("profit_lock_floor_pct"), "exit_score": decision["exit_score"],
            "action": decision["action"], "reason": decision["reason"],
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
