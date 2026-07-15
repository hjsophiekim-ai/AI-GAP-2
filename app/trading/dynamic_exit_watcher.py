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
import app.trading.hynix_big_trend_engine as bte

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


def _compute_big_trend_decision(state: dict, position: dict, symbol: str, current_price: float, df_1min, now: datetime) -> Optional[dict]:
    """Big Trend Holding AI(app.trading.hynix_big_trend_engine) 1회 계산 — 항상 호출되어
    Shadow 로그로 남으며, state["big_trend_holding_enabled"]가 켜졌을 때만 호출부가
    이 결과로 실제 청산 action/ratio를 대체한다. 예외 발생 시 None을 반환해 호출부가
    기존 DynamicExitEngine 판단만으로 안전하게 계속 동작하도록 한다."""
    from app.trading.trading_cost_engine import TradeCostEngine

    entry_price = position.get("entry_price")
    quantity = position.get("quantity") or 0
    if not entry_price or quantity <= 0:
        return None

    cost = TradeCostEngine().compute_unrealized_net_pnl(symbol, entry_price=entry_price, current_price=current_price, quantity=quantity)
    invested = entry_price * quantity
    net_return_pct = round(cost["net_unrealized_pnl"] / invested * 100.0, 4) if invested else 0.0

    peak_net_return_pct = max(position.get("peak_net_return_pct", net_return_pct), net_return_pct)
    position["peak_net_return_pct"] = peak_net_return_pct

    shadow = state.get("last_cycle_ai_result") or {}
    prob = shadow.get("probability") or {}
    cyc = shadow.get("cycle") or {}
    decision_v2 = shadow.get("decision_v2") or {}
    inverse_probability = prob.get("sell_probability")
    hynix_probability = prob.get("buy_probability")

    snapshot_engine = DynamicExitEngine()
    snapshot = snapshot_engine.build_snapshot(position, _load_daily_df(symbol), df_1min, current_price, now)

    features = bte.build_big_trend_features(df_1min, snapshot, inverse_probability, hynix_probability)
    trend = bte.compute_trend_strength_score(features)
    direction = trend["dominant_direction"]

    reversal_signals = bte.build_reversal_signals(
        features, direction, decision_v2.get("final_action_v2"), cyc.get("cycle_phase"),
    )

    held_minutes = snapshot.get("held_minutes")
    volatility_class = "HIGH_VOL" if (features.get("atr_pct") or 0) >= 1.5 else ("LOW_VOL" if (features.get("atr_pct") or 0) <= 0.5 else "NORMAL")
    is_strong_trend_initial = bool(held_minutes is not None and held_minutes <= 10 and trend["trend_strength_score"] >= 75.0)
    sl_pct = bte.effective_sl_pct(volatility_class, is_strong_trend_initial)
    hard_stop_triggered = net_return_pct <= sl_pct

    big_trend_state = state.get("big_trend_state") or {}
    recent_flip_count = big_trend_state.get("recent_direction_flip_count", 0)
    first_tp_taken = bool(position.get("big_trend_first_tp_taken"))
    regime_state = position.get("big_trend_regime_state") or bte.default_regime_state()

    engine = bte.HynixBigTrendEngine()
    result = engine.compute(
        features=features, held_symbol=symbol, entry_price=entry_price, current_price=current_price,
        net_return_pct=net_return_pct, peak_net_return_pct=peak_net_return_pct,
        reversal_probability_3m=None, reversal_probability_5m=None, reversal_probability_15m=None,
        reversal_signals=reversal_signals, recent_direction_flip_count=recent_flip_count,
        hard_stop_triggered=hard_stop_triggered, first_tp_taken=first_tp_taken,
        volatility_class=volatility_class, is_strong_trend_initial_phase=is_strong_trend_initial,
        regime_state=regime_state, now=now,
    )

    # regime_state는 포지션 단위로 유지한다(청산 후 재진입 시 자연히 초기화됨 —
    # position 딕셔너리 자체가 새로 만들어지므로 별도 리셋 로직이 필요 없다).
    position["big_trend_regime_state"] = result.get("regime_state") or regime_state

    final_action = result.get("final_hold_action")
    if final_action in (bte.ACTION_TAKE_PROFIT_25, bte.ACTION_TAKE_PROFIT_50):
        position["big_trend_first_tp_taken"] = True

    log_row = {
        "timestamp": now.isoformat(timespec="seconds"), "symbol": symbol, "entry_price": entry_price,
        "current_price": current_price, "net_return_pct": net_return_pct, "peak_net_return_pct": peak_net_return_pct,
        "profit_giveback_pct": bte.compute_profit_giveback_pct(peak_net_return_pct, net_return_pct),
        "dominant_direction": result["dominant_direction"], "trend_regime": result["trend_regime"],
        "trend_strength_score": result["trend_strength_score"], "trend_persistence_score": result["trend_persistence_score"],
        "reversal_probability_3m": result["reversal_probability_3m"], "reversal_probability_5m": result["reversal_probability_5m"],
        "reversal_probability_15m": result["reversal_probability_15m"], "hold_confidence": result["hold_confidence"],
        "exit_confidence": result["exit_confidence"], "profit_lock_floor_pct": result["current_profit_lock_pct"],
        "trailing_pct": result["trailing_pct"], "position_pct": result["max_position_pct"],
        "recommended_action": final_action, "executed_action": None,
        "reason_top1": (result["reasons"][0] if result["reasons"] else ""),
        "reason_top2": (result["reasons"][1] if len(result["reasons"]) > 1 else ""),
        "reason_top3": (result["reasons"][2] if len(result["reasons"]) > 2 else ""),
    }
    bte.log_big_trend_decision(log_row)

    return {
        **{k: v for k, v in result.items() if k != "reversal_confirmation"},
        "net_return_pct": net_return_pct, "peak_net_return_pct": peak_net_return_pct,
        "effective_sl_pct": sl_pct, "hard_stop_triggered": hard_stop_triggered,
        "decision": {"action": final_action, "reasons": result.get("reasons", []), "tp_ratio": result.get("tp_ratio")},
        "log_row": log_row,
    }


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
        from app.config import get_config
        from app.trading.broker_factory import create_broker

        broker = create_broker(get_config(), mode="mock")
    else:
        from app.config import get_config
        from app.trading.broker_factory import create_broker

        _cfg = get_config()
        broker = create_broker(
            _cfg, mode="real", confirm_text=_cfg.full_auto_real_confirm_text(),
            runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
        )
    _broker_cache[mode] = (broker, now_mono)
    _position_manager_cache.pop(mode, None)  # 브로커가 바뀌었으니 매니저도 새로 만든다
    return broker


def clear_runtime_caches() -> None:
    """계좌/설정 변경(환경설정 다시 읽기) 시 캐시된 브로커·PositionManager를 즉시 폐기한다.

    30초 TTL로 자연 만료되긴 하지만, 계좌를 바꾼 직후에도 최대 30초간 이전 계좌의
    브로커가 재사용될 수 있어(요구사항: 계좌 변경 후 기존 broker cache 재사용 금지)
    reload_runtime_configuration()이 이 함수를 호출해 즉시 무효화한다."""
    _broker_cache.clear()
    _position_manager_cache.clear()


def _get_position_manager(broker, mode: str):
    from app.trading.hynix_position_common import HynixPositionManager

    pm = _position_manager_cache.get(mode)
    if pm is None or pm.broker is not broker:
        pm = HynixPositionManager(broker, mode=mode)
        _position_manager_cache[mode] = pm
    return pm


def tick(now: Optional[datetime] = None, engine: Optional[DynamicExitEngine] = None) -> Optional[dict]:
    """1회 감시 실행의 공개 진입점 — mode별 state 락으로 감싼 얇은 wrapper.

    이 틱(1초 주기)과 app.services.hynix_switch_engine.update_hynix_auto_trade_loop(3분
    주기 + 수동 실행)가 같은 mode의 realized_pnl_today_krw 등을 동시에
    read-modify-write하면 lost update가 발생할 수 있어(2026-07-10 실측 사고),
    동일한 mode별 락(with_state_lock)으로 두 진입점을 직렬화한다.
    """
    from app.services.hynix_switch_state import with_state_lock

    peek_state = load_state()
    resolved_mode = peek_state.get("mode", "mock")
    with with_state_lock(resolved_mode):
        return _tick_locked(now=now, engine=engine)


def _tick_locked(now: Optional[datetime] = None, engine: Optional[DynamicExitEngine] = None) -> Optional[dict]:
    """1회 감시 실행의 실제 구현(반드시 with_state_lock(mode) 안에서만 호출).

    Broker → PositionManager → State(캐시) 순서로만 데이터가 흐른다. 이 함수는
    보유 포지션 판정을 위해 state를 직접 신뢰하지 않고, 매 틱 PositionManager를
    통해 브로커를 확인(mock은 항상 새로고침, real은 5초 TTL 캐시)한 뒤에만 판단한다.
    락 획득 전에 미리 읽은 state(mode 판별용)는 재사용하지 않고, 락을 잡은 뒤
    항상 다시 로드한다(read-modify-write의 "read"는 반드시 락 내부에서 일어나야 한다).
    """
    from app.trading.hynix_switch_position_manager import apply_position_manager_to_state

    now = now or datetime.now()
    engine = engine or _engine
    state = load_state()

    if not state.get("auto_trade_on") or state.get("stopped"):
        return None

    mode = state.get("mode", "mock")
    position = state.get("position") or {}
    symbol = position.get("symbol")
    flat_without_recovery_context = (
        (not symbol or (position.get("quantity") or 0) <= 0)
        and not position.get("entry_price")
        and not position.get("entry_time")
    )
    if flat_without_recovery_context:
        # Dynamic Exit는 보유 포지션 청산 전용이다. 보유가 없는데도 1초마다
        # KIS 잔고를 조회하면 EGW00201/tokenP 제한으로 Enhanced 신규진입까지 막힌다.
        state["dynamic_exit_last_decision"] = _no_position_decision()
        save_state_atomic(state)
        return None
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

    # ── Big Trend Holding AI(섹션 1~13 — 장중 큰 추세 추종) ────────────────────
    # 항상 계산·로그(Shadow)하고, state["big_trend_holding_enabled"]가 켜져 있을 때만
    # (mock 전용) 실제 청산 action/ratio를 이 엔진 결과로 대체한다. 초기 손절
    # 안전장치(effective_sl_pct)는 토글과 무관하게 항상 최우선으로 적용된다.
    big_trend_result = None
    try:
        big_trend_result = _compute_big_trend_decision(state, position, symbol, current_price, df_1min, now)
    except Exception as exc:
        logger.debug("[DynamicExitWatcher] Big Trend Holding 계산 실패(무해 — 기존 로직 계속 동작): %s", exc)

    if big_trend_result:
        state["last_big_trend_result"] = {k: v for k, v in big_trend_result.items() if k != "reversal_confirmation"}
        if mode == "mock" and state.get("big_trend_holding_enabled"):
            hard_stop = big_trend_result["hard_stop_triggered"]
            action_map = {
                bte.ACTION_TAKE_PROFIT_25: ("SELL_PARTIAL", 0.25),
                bte.ACTION_TAKE_PROFIT_50: ("SELL_PARTIAL", big_trend_result["decision"].get("tp_ratio", 0.5)),
                bte.ACTION_EXIT_ALL: ("SELL_ALL", 1.0),
                bte.ACTION_SWITCH_TO_HYNIX: ("SELL_ALL", 1.0),
                bte.ACTION_SWITCH_TO_INVERSE: ("SELL_ALL", 1.0),
            }
            if hard_stop:
                decision["action"], decision["ratio"] = "SELL_ALL", 1.0
                decision["reason"] = f"손절({big_trend_result['net_return_pct']:.2f}%≤{big_trend_result['effective_sl_pct']:.2f}%) — Big Trend 안전장치"
            else:
                final_action = big_trend_result["decision"].get("action")
                if final_action in action_map:
                    decision["action"], decision["ratio"] = action_map[final_action]
                    decision["reason"] = "; ".join(big_trend_result["decision"].get("reasons", [])) or final_action
                else:
                    # 섹션 20 — Regime 전환 자체가 즉시 축소를 요구하면(HOLD/HOLD_REDUCED로
                    # 끝나는 사이클이라도) 그 축소를 적용한다.
                    transition = big_trend_result.get("regime_transition_action") or {}
                    if transition.get("action") == "REDUCE_POSITION" and transition.get("reduce_ratio", 0) > 0:
                        decision["action"], decision["ratio"] = "SELL_PARTIAL", transition["reduce_ratio"]
                        decision["reason"] = f"Regime 전환({big_trend_result.get('raw_trend_regime')}) — {transition['reduce_ratio']*100:.0f}% 축소"
                    else:
                        decision["action"], decision["ratio"] = "HOLD", 0.0
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
                mode=mode, exit_reason_type=exit_reason_type, signal_source="DYNAMIC_EXIT",
                position_manager=position_manager,
            )
            order_sent = bool(order_result.get("success"))
            if order_sent:
                from app.trading.hynix_switch_position_manager import _resolve_realized_pnl

                sold_qty = order_result.get("sold_quantity", 0)
                # net_pnl/gross_pnl은 _execute_sell()이 원장 기록과 함께 계산해 order_result에
                # 넣어준 값이다 — 여기서 (current_price-entry_price)*qty(Gross)를 다시 계산해
                # 쌓으면 "오늘 실현손익(순손익)"이 원장의 net_realized_pnl과 어긋난다
                # (2026-07-13 사용자 리포트: Dynamic Exit AI 매도가 이 버그의 주 원인 중 하나였다).
                net_realized, gross_realized = _resolve_realized_pnl(
                    order_result, current_price, position.get("entry_price") or current_price, sold_qty,
                )
                state["realized_pnl_today_krw"] = state.get("realized_pnl_today_krw", 0.0) + net_realized
                state["gross_realized_pnl_today_krw"] = state.get("gross_realized_pnl_today_krw", 0.0) + gross_realized
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
