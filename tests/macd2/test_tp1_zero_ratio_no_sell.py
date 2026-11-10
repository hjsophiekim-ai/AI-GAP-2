"""2026-11-10 실사고 회귀: TP1 매도비중 0.0 이면 아무것도 팔지 않는다.

수동 매수 0193T0 2100주가 adopt 되어 N1 TREND 래더(TP1 3.5%, 매도비중 0.0 =
"TP1 에서 팔지 않고 trailing 만 arm")로 관리됐는데, +3.5% 도달 틱에
``max(1, round(2100 * 0.0))`` = 1 이 되어 실계좌에서 1주가 매도됐다.
연구엔진/shadow_base 는 비중 0 이면 매도 없이 tp1_done 만 각인한다.
"""
from __future__ import annotations

from app.trading.macd2 import config, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import PositionSnapshot
from app.trading.macd2.time_window_position_manager import PositionManagementDecision
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_worker_time_window import (
    _BOOTSTRAP_NOW, _PRIOR_DAY, _SESSION_START_NOW, _1m_frame, _fresh_state, _sine_1m_closes,
)

QTY = 2100


def _setup(monkeypatch, inverse_quote: float):
    df_1m = _1m_frame(_PRIOR_DAY, _sine_1m_closes(300))
    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: inverse_quote, config.WATCH_SYMBOL: 100.0}
    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=lambda mode, symbol, count, hour1: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quotes.get(symbol), None),
    )
    assert svc.bootstrap(now=_BOOTSTRAP_NOW).ok
    svc.refresh_quotes()
    now0 = _SESSION_START_NOW
    state = _fresh_state()
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=QTY, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_entry_session = "MORNING"
    broker = FakeBroker(cash=100_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: inverse_quote})
    broker.buy_market(config.INVERSE_SYMBOL, QTY, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    broker.orders.clear()
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)
    return svc, state, broker, now0


def _sells(broker):
    return [o for o in broker.orders if getattr(o, "side", None) == "SELL"]


def test_tick_tp1_with_zero_sell_ratio_sends_no_order_and_arms_tp1(monkeypatch):
    # +4.5% raw -> TP1(3%)..TP2(6%) 사이 = TP1_PARTIAL, 비중만 0.0 (N1 추세 래더와 같은 값)
    monkeypatch.setattr(worker.time_window_position_manager, "MORNING_TP1_SELL_RATIO", 0.0)
    svc, state, broker, now0 = _setup(monkeypatch, 10_450.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert _sells(broker) == [], f"매도비중 0 인데 매도 주문이 나갔다: {broker.orders!r}"
    assert broker._positions[config.INVERSE_SYMBOL].quantity == QTY
    assert state.position is not None and state.position.quantity == QTY
    assert state.time_window_tp1_done is True, "비중 0 TP1 은 매도 없이 tp1_done 을 각인해야 한다(trailing arm)"
    assert any(a.startswith("TP1_ARM_ONLY") for a in result.actions), result.actions
    assert not any(a.startswith(config.EXIT_TW_TP1_PARTIAL) for a in result.actions), result.actions

    # 다음 틱에도 TP1 이 재발동하지 않는다 (같은 +4.5%)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert _sells(broker) == []
    assert state.position.quantity == QTY


def test_bar_close_tp1_with_zero_sell_ratio_sends_no_order_and_arms_tp1(monkeypatch):
    monkeypatch.setattr(worker.time_window_position_manager, "MORNING_TP1_SELL_RATIO", 0.0)
    svc, state, broker, now0 = _setup(monkeypatch, 10_050.0)
    # 틱 경로는 HOLD, 완성봉 종가는 TP1 구간 -> 완성봉 경로만 격리
    monkeypatch.setattr(
        worker.time_window_position_manager, "evaluate_take_profit_immediate",
        lambda **kw: PositionManagementDecision(None, 0.0, kw["tp1_done"], kw["net_return_pct"], "HOLD_TICK"),
    )
    monkeypatch.setattr(worker, "_advance_stop_loss_bar", lambda state, symbol, price, now: 10_450.0)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert _sells(broker) == [], f"매도비중 0 인데 매도 주문이 나갔다: {broker.orders!r}"
    assert state.position is not None and state.position.quantity == QTY
    assert state.time_window_tp1_done is True
    assert any(a.startswith("TP1_ARM_ONLY") for a in result.actions), result.actions


def test_nonzero_sell_ratio_still_sells_partial(monkeypatch):
    """대조군: 비중 0.2 는 기존대로 20% 를 판다 (수정 범위가 0 에만 한정됨을 고정)."""
    monkeypatch.setattr(worker.time_window_position_manager, "MORNING_TP1_SELL_RATIO", 0.2)
    svc, state, broker, now0 = _setup(monkeypatch, 10_450.0)

    run_once(broker=broker, market_data=svc, state=state, now=now0)

    sells = _sells(broker)
    assert len(sells) == 1 and sells[0].requested_qty == round(QTY * 0.2), broker.orders
    assert state.time_window_tp1_done is True
