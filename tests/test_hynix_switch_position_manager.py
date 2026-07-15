"""
test_hynix_switch_position_manager.py — 스위칭/TP·SL/당일 강제청산 검증.

mock/real 모두 브로커의 buy()/sell() 호출 방식은 동일하므로(브로커 구현만 다름),
아래 DummyBroker로 두 모드의 매매 로직을 동일하게 검증한다.

2026-07-15부터 "하이닉스 매수"는 SK하이닉스(000660)를 직접 매수하지 않고
KODEX SK하이닉스단일종목레버리지(LONG_SYMBOL/0193T0)를 매수한다 — 000660은
신호 계산에만 쓰이고 실제 주문 종목이 아니다. 아래 테스트는 모두 LONG_SYMBOL을
"하이닉스 상승 측 실거래 종목"으로 사용한다.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from app.models import OrderResult
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL, HYNIX_NAME
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME
from app.data_sources.hynix_long_collector import LONG_SYMBOL, LONG_NAME
from app.trading.hynix_switch_position_manager import (
    run_switch_or_entry, run_liquidation_if_needed, run_tp_sl_if_needed, evaluate_tp_sl,
)
from app.services.hynix_switch_state import default_state


class DummyBroker:
    def __init__(self, buy_success: bool = True, sell_success: bool = True, buyable_cash: float = 10_000_000):
        self.buy_success = buy_success
        self.sell_success = sell_success
        self.buyable_cash = buyable_cash
        self.buy_calls: list = []
        self.sell_calls: list = []

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        self.buy_calls.append((symbol, quantity, price))
        return OrderResult(
            success=self.buy_success, mode="dry_run", account_type="dry_run", symbol=symbol, name=name,
            side="buy", quantity=quantity, price=price, order_type=order_type,
            order_id="B1" if self.buy_success else "", message="ok" if self.buy_success else "매수 실패",
        )

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, price))
        return OrderResult(
            success=self.sell_success, mode="dry_run", account_type="dry_run", symbol=symbol, name=name,
            side="sell", quantity=quantity, price=price, order_type=order_type,
            order_id="S1" if self.sell_success else "", message="ok" if self.sell_success else "매도 실패",
        )

    def get_positions(self):
        return []

    def get_balance(self):
        return self.buyable_cash

    def get_current_price(self, symbol):
        return None

    def get_buyable_cash(self):
        return self.buyable_cash


class PositionSyncBroker(DummyBroker):
    def __init__(self, positions_after_sell, fail_positions: bool = False):
        super().__init__(sell_success=True)
        self.positions_after_sell = positions_after_sell
        self.fail_positions = fail_positions
        self.position_calls = 0

    def get_positions(self):
        self.position_calls += 1
        if self.fail_positions:
            raise RuntimeError("balance unavailable")
        return list(self.positions_after_sell)


class LiquidationSyncBroker(DummyBroker):
    def __init__(self, initial_positions):
        super().__init__(sell_success=True)
        self.positions = list(initial_positions)

    def get_positions(self):
        return list(self.positions)

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        result = super().sell(symbol, name, quantity, price, order_type)
        if result.success:
            self.positions = [
                {**p, "quantity": max(0, int(p.get("quantity", 0)) - quantity)}
                for p in self.positions
                if p.get("symbol") != symbol or max(0, int(p.get("quantity", 0)) - quantity) > 0
            ]
        return result


def _holding_state(symbol: str, quantity: int = 10, entry_price: float = 100_000.0) -> dict:
    state = default_state()
    state["position"] = {
        "symbol": symbol, "name": LONG_NAME if symbol == LONG_SYMBOL else INVERSE_NAME,
        "quantity": quantity, "avg_price": entry_price, "entry_price": entry_price,
        "entry_time": datetime.now().isoformat(), "partial_tp1_done": False, "partial_sl1_done": False,
    }
    return state


def test_signal_symbol_direct_order_is_forbidden():
    """요구사항8(2026-07-15) — SIGNAL_SYMBOL(000660) 직접 매수·매도는 완전히 금지된다.
    잘못된 호출로 000660이 주문 경로에 흘러들어와도 브로커에 도달하기 전에 막혀야
    한다(테스트는 예외 발생으로 확인한다)."""
    from app.trading.hynix_switch_position_manager import _buy_new, _sell_all_or_ratio

    broker = DummyBroker(buy_success=True, buyable_cash=10_000_000.0)
    with pytest.raises(ValueError):
        _buy_new(broker, HYNIX_SYMBOL, current_price=100_000.0, cash_amount=1_000_000.0, reason="test", orders=[], mode="mock")
    assert broker.buy_calls == []

    position = {"symbol": HYNIX_SYMBOL, "quantity": 10, "entry_price": 100_000.0}
    with pytest.raises(ValueError):
        _sell_all_or_ratio(broker, position, current_price=103_000.0, ratio=1.0, reason="test", orders=[], mode="mock")
    assert broker.sell_calls == []


def test_hynix_buy_signal_trades_long_symbol_not_signal_symbol():
    """요구사항1/2 — HYNIX_BUY 신호는 000660이 아니라 LONG_SYMBOL(0193T0)을 매수한다."""
    state = default_state()
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 101_000, 5_000, now=now)

    assert result["acted"] is True
    assert len(broker.buy_calls) == 1 and broker.buy_calls[0][0] == LONG_SYMBOL
    assert state["position"]["symbol"] == LONG_SYMBOL


def test_switch_from_hynix_to_inverse_sells_then_buys():
    state = _holding_state(LONG_SYMBOL)
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "INVERSE_BUY", 101_000, 5_000, now=now)

    assert result["acted"] is True
    assert len(broker.sell_calls) == 1 and broker.sell_calls[0][0] == LONG_SYMBOL
    assert len(broker.buy_calls) == 1 and broker.buy_calls[0][0] == INVERSE_SYMBOL
    assert state["position"]["symbol"] == INVERSE_SYMBOL


def test_switch_from_inverse_to_hynix_sells_then_buys():
    state = _holding_state(INVERSE_SYMBOL, entry_price=5_000)
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 101_000, 5_100, now=now)

    assert result["acted"] is True
    assert broker.sell_calls[0][0] == INVERSE_SYMBOL
    assert broker.buy_calls[0][0] == LONG_SYMBOL
    assert state["position"]["symbol"] == LONG_SYMBOL


def test_buy_bumps_to_minimum_one_share_when_sizing_too_small():
    """사이징(20%) 금액으로는 1주도 못 사지만, 실제 매수가능금액은 충분하면 최소 1주로 상향해야 한다."""
    from app.services.hynix_switch_state import default_state

    state = default_state()  # 무보유
    broker = DummyBroker(buyable_cash=10_000_000)  # 20% = 2,000,000원 < 9,000,000원(1주가)
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 9_000_000, 5_000, now=now)

    assert result["acted"] is True
    assert len(broker.buy_calls) == 1
    assert broker.buy_calls[0] == (LONG_SYMBOL, 1, 9_000_000)
    assert state["position"]["symbol"] == LONG_SYMBOL
    assert state["position"]["quantity"] == 1


def test_buy_skip_is_logged_when_even_one_share_unaffordable():
    """1주도 살 수 없을 때 조용히 누락되지 않고 스킵 사유가 orders에 기록되어야 한다."""
    from app.services.hynix_switch_state import default_state

    state = default_state()
    broker = DummyBroker(buyable_cash=1_000_000)  # 1주(9,000,000원)도 못 사는 총 현금
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 9_000_000, 5_000, now=now)

    assert len(broker.buy_calls) == 0
    assert state["position"]["symbol"] is None
    assert any(o["action"] == "BUY_SKIPPED" and o["success"] is False for o in result["orders"])


def test_new_entry_blocked_after_1450_sells_only():
    state = _holding_state(LONG_SYMBOL)
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 15, 0)  # 14:50 이후

    result = run_switch_or_entry(state, broker, "INVERSE_BUY", 101_000, 5_000, now=now)

    assert len(broker.sell_calls) == 1
    assert len(broker.buy_calls) == 0
    assert state["position"]["symbol"] is None
    assert "14:50" in result["message"]


def test_position_sync_pending_blocks_already_holding_assumption():
    """요구사항3 — POSITION_SYNC_PENDING이면 로컬 보유 플래그("이미 보유 중")를
    신뢰하지 않고, 재동기화를 시도한 뒤에도 확인되지 않으면 주문을 차단해야 한다.
    position_manager 없이는 재동기화가 불가능하므로 반드시 차단(state_sync)된다."""
    state = _holding_state(LONG_SYMBOL)  # 로컬 상태는 "이미 LONG_SYMBOL 보유 중"으로 보임
    state["position_sync_status"] = "POSITION_SYNC_PENDING"
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 101_000, 5_000, now=now)

    assert result["acted"] is False
    assert result["stage"] == "state_sync"
    assert len(broker.buy_calls) == 0
    assert len(broker.sell_calls) == 0


def test_position_sync_pending_with_no_local_holding_still_blocks_new_entry():
    """요구사항3 — 보유 없음(로컬 flat) + POSITION_SYNC_PENDING 조합에서도 "이미
    보유 없으니 바로 진입 가능"으로 판단하지 않고 재동기화 확인 전까지 차단한다."""
    state = default_state()
    state["position_sync_status"] = "POSITION_SYNC_PENDING"
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 101_000, 5_000, now=now)

    assert result["acted"] is False
    assert result["stage"] == "state_sync"
    assert len(broker.buy_calls) == 0


class _FillTrackingBroker(DummyBroker):
    """DummyBroker에 브로커 잔고 재조회(get_positions)가 실제 체결 수량을 반영하도록
    확장한 버전 — POSITION_SYNC_PENDING 재동기화가 실제로 해소되는 경로를 검증할 때 쓴다."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._filled: dict = {}

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        result = super().buy(symbol, name, quantity, price, order_type)
        if result.success:
            self._filled[symbol] = self._filled.get(symbol, 0) + quantity
        return result

    def get_positions(self):
        return [
            {"symbol": sym, "quantity": qty, "avg_price": 100_000.0}
            for sym, qty in self._filled.items() if qty > 0
        ]


def test_position_sync_pending_resolves_once_position_manager_confirms():
    """position_manager가 주어지고 재동기화가 성공하면(POSITION_SYNC_PENDING이었어도)
    확인된 실제 보유 상태를 기준으로 정상 진행되어야 한다."""
    from app.trading.hynix_position_common import HynixPositionManager

    state = default_state()
    state["position_sync_status"] = "POSITION_SYNC_PENDING"
    broker = _FillTrackingBroker()
    position_manager = HynixPositionManager(broker, mode="mock")
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 101_000, 5_000, now=now, position_manager=position_manager)

    assert result["acted"] is True
    assert len(broker.buy_calls) == 1
    assert state["position_sync_status"] == "SYNCED"


def test_duplicate_order_blocked_within_same_cycle_single_engine():
    """요구사항4 — 단일 주문 엔진(run_switch_or_entry) 안에서 같은 3분 주기(cycle_bucket)에
    동일 신호가 재호출되면 두 번째 호출은 중복 주문을 내지 않는다."""
    state = default_state()
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    result1 = run_switch_or_entry(state, broker, "HYNIX_BUY", 101_000, 5_000, now=now)
    assert result1["acted"] is True
    assert len(broker.buy_calls) == 1

    # 같은 사이클(같은 3분 버킷) 안에서 같은 신호로 다시 호출 — 이미 보유 중이므로
    # 두 번째 호출은 "already holding"으로 스킵되고 중복 매수가 일어나지 않는다.
    result2 = run_switch_or_entry(state, broker, "HYNIX_BUY", 101_000, 5_000, now=now)
    assert len(broker.buy_calls) == 1
    assert result2["acted"] is False


def test_liquidation_success_clears_position():
    state = _holding_state(LONG_SYMBOL)
    broker = DummyBroker(sell_success=True)
    now = datetime(2026, 7, 9, 15, 16)

    result = run_liquidation_if_needed(now, state, broker, 101_000, 5_000)

    assert result["liquidated"] is True
    assert state["position"]["symbol"] is None
    assert state["liquidation_done"] is True
    assert state["critical_alert"] is None


def test_liquidation_failure_retries_once_then_critical_alert():
    state = _holding_state(LONG_SYMBOL)
    broker = DummyBroker(sell_success=False)
    now = datetime(2026, 7, 9, 15, 16)

    result = run_liquidation_if_needed(now, state, broker, 101_000, 5_000)

    assert result["liquidated"] is False
    assert len(broker.sell_calls) == 2  # 1회 시도 + 1회 재시도
    assert state["position"]["symbol"] == LONG_SYMBOL  # 포지션 유지
    assert state["critical_alert"] is not None


def test_liquidation_not_triggered_before_1515():
    state = _holding_state(LONG_SYMBOL)
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 15, 10)

    result = run_liquidation_if_needed(now, state, broker, 101_000, 5_000)

    assert result["liquidated"] is False
    assert len(broker.sell_calls) == 0


@pytest.mark.parametrize("mode_label", ["mock", "real"])
def test_liquidation_rule_identical_for_mock_and_real(mode_label):
    """청산 시간 로직은 브로커 구현과 무관하게 동일하게 동작해야 한다."""
    state = _holding_state(INVERSE_SYMBOL, entry_price=5_000)
    broker = DummyBroker(sell_success=True)
    now = datetime(2026, 7, 9, 15, 15, 30)

    result = run_liquidation_if_needed(now, state, broker, 101_000, 5_100)

    assert result["liquidated"] is True
    assert state["position"]["symbol"] is None


def test_tp2_full_take_profit_triggers():
    position = {"symbol": LONG_SYMBOL, "quantity": 10, "entry_price": 100_000.0, "partial_tp1_done": False, "partial_sl1_done": False}
    trigger = evaluate_tp_sl(position, current_price=102_100.0)  # +2.1%
    assert trigger is not None and trigger["tag"] == "tp2" and trigger["ratio"] == 1.0


def test_tp1_partial_take_profit_triggers():
    position = {"symbol": LONG_SYMBOL, "quantity": 10, "entry_price": 100_000.0, "partial_tp1_done": False, "partial_sl1_done": False}
    trigger = evaluate_tp_sl(position, current_price=101_300.0)  # +1.3%
    assert trigger is not None and trigger["tag"] == "tp1" and trigger["ratio"] == 0.5


def test_sl2_full_stop_loss_triggers():
    position = {"symbol": LONG_SYMBOL, "quantity": 10, "entry_price": 100_000.0, "partial_tp1_done": False, "partial_sl1_done": False}
    trigger = evaluate_tp_sl(position, current_price=98_400.0)  # -1.6%
    assert trigger is not None and trigger["tag"] == "sl2"


def test_run_tp_sl_if_needed_executes_and_clears_position():
    state = _holding_state(LONG_SYMBOL, quantity=10, entry_price=100_000.0)
    broker = DummyBroker(sell_success=True)

    result = run_tp_sl_if_needed(state, broker, hynix_price=102_500.0, inverse_price=5_000.0)

    assert result["triggered"] is True and result["executed"] is True
    assert state["position"]["symbol"] is None


def test_partial_sell_keeps_kis_confirmed_remaining_quantity():
    state = _holding_state(INVERSE_SYMBOL, quantity=114, entry_price=5_000.0)
    state["position"]["partial_tp1_done"] = False
    broker = PositionSyncBroker([
        {"symbol": INVERSE_SYMBOL, "name": INVERSE_NAME, "quantity": 18, "avg_price": 5_000.0}
    ])

    from app.trading.hynix_switch_position_manager import _sell_all_or_ratio, _apply_sell_result_to_state_position

    orders = []
    result = _sell_all_or_ratio(
        broker, state["position"], current_price=5_100.0, ratio=96 / 114,
        reason="partial test", orders=orders, mode="mock",
    )
    _apply_sell_result_to_state_position(state, state["position"], result, mark_partial="tp1")

    assert result["success"] is True
    assert result["remaining_quantity"] == 18
    assert state["position"]["symbol"] == INVERSE_SYMBOL
    assert state["position"]["quantity"] == 18
    assert state["position_sync_block_new_orders"] is False


def test_buy_success_uses_broker_confirmed_filled_quantity():
    from app.trading.hynix_position_common import HynixPositionManager
    from app.trading.hynix_switch_position_manager import _buy_new

    broker = PositionSyncBroker([
        {"symbol": INVERSE_SYMBOL, "name": INVERSE_NAME, "quantity": 114, "avg_price": 5_000.0}
    ])
    pm = HynixPositionManager(broker, mode="mock")
    orders = []

    result = _buy_new(
        broker, INVERSE_SYMBOL, current_price=5_000.0, cash_amount=570_000.0,
        reason="buy confirm", orders=orders, mode="mock", position_manager=pm,
    )

    assert result["success"] is True
    assert result["position_sync_status"] == "SYNCED"
    assert result["filled_quantity"] == 114
    assert result["actual_quantity"] == 114


def test_sell_sync_failure_does_not_delete_position_and_blocks_new_orders():
    state = _holding_state(INVERSE_SYMBOL, quantity=114, entry_price=5_000.0)
    broker = PositionSyncBroker([], fail_positions=True)

    from app.trading.hynix_switch_position_manager import (
        POSITION_SYNC_PENDING,
        _sell_all_or_ratio,
        _apply_sell_result_to_state_position,
    )

    orders = []
    result = _sell_all_or_ratio(
        broker, state["position"], current_price=5_100.0, ratio=96 / 114,
        reason="partial test", orders=orders, mode="mock",
    )
    _apply_sell_result_to_state_position(state, state["position"], result)

    assert result["success"] is True
    assert result["remaining_quantity"] is None
    assert result["position_sync_status"] == POSITION_SYNC_PENDING
    assert state["position"]["symbol"] == INVERSE_SYMBOL
    assert state["position"]["quantity"] == 114
    assert state["position_sync_block_new_orders"] is True


def test_recent_flat_sync_allows_transient_position_sync_failure():
    from datetime import datetime
    from app.trading.hynix_switch_position_manager import apply_position_manager_to_state

    state = default_state()
    state["position_sync_last_ok_at"] = datetime.now().isoformat()
    state["position_sync_last_position"] = {"symbol": None, "quantity": 0}

    class _FailedPositionManager:
        last_sync_ok = False
        last_sync_error = "HTTP 500 msg_cd=EGW00201: rate limit"
        current_position = {"symbol": None, "quantity": 0}

    apply_position_manager_to_state(state, _FailedPositionManager())

    assert state["position_sync_status"] == "SYNCED_RECENT_CACHE"
    assert state["position_sync_block_new_orders"] is False
    assert "EGW00201" in state["position_sync_error"]


def test_sync_failure_without_recent_flat_confirmation_still_blocks():
    from app.trading.hynix_switch_position_manager import POSITION_SYNC_PENDING, apply_position_manager_to_state

    state = default_state()

    class _FailedPositionManager:
        last_sync_ok = False
        last_sync_error = "tokenP 403"
        current_position = {"symbol": None, "quantity": 0}

    apply_position_manager_to_state(state, _FailedPositionManager())

    assert state["position_sync_status"] == POSITION_SYNC_PENDING
    assert state["position_sync_block_new_orders"] is True


def test_confirm_remaining_quantity_polls_until_fill_becomes_visible():
    """요구사항3(2026-07-15) — 조회 자체는 성공했지만 아직 체결 전 수량과 동일하면
    (=아직 반영 안 됨) 바로 확정하지 말고 지정된 간격으로 재조회해야 한다."""
    from app.trading.hynix_switch_position_manager import _confirm_remaining_quantity_from_broker

    class _DelayedFillBroker:
        def __init__(self):
            self.calls = 0

        def get_positions(self):
            self.calls += 1
            if self.calls < 3:
                return []  # 아직 체결 미반영(before_qty=0과 동일)
            return [{"symbol": LONG_SYMBOL, "quantity": 28, "avg_price": 100_000.0}]

    broker = _DelayedFillBroker()
    result = _confirm_remaining_quantity_from_broker(
        broker, LONG_SYMBOL, attempts=5, delay_seconds=0.01, retry_while_qty_equals=0,
    )

    assert broker.calls == 3
    assert result["ok"] is True
    assert result["quantity"] == 28


def test_confirm_remaining_quantity_gives_up_after_max_attempts_still_unchanged():
    from app.trading.hynix_switch_position_manager import _confirm_remaining_quantity_from_broker

    class _NeverFillsBroker:
        def get_positions(self):
            return []

    result = _confirm_remaining_quantity_from_broker(
        _NeverFillsBroker(), LONG_SYMBOL, attempts=3, delay_seconds=0.01, retry_while_qty_equals=0,
    )

    assert result["ok"] is True
    assert result["quantity"] == 0
    assert result["attempts"] == 3


def test_kis_confirmed_holding_overrides_stale_no_holding_state():
    """요구사항4(2026-07-15) — 원장은 0197X0 매수 1480주/매도 648주(잔량 832주)인데
    로컬 state는 "보유 없음"으로 남아있던 실측 버그. KIS(position_manager)가 실제로
    832주 보유를 보고하면, KIS를 최종 기준으로 state가 그 값으로 동기화되어야 한다 —
    로컬의 낡은 "보유 없음"을 그대로 두지 않는다."""
    from app.trading.hynix_switch_position_manager import apply_position_manager_to_state

    state = default_state()
    state["position"] = {**state["position"], "symbol": None, "quantity": 0}  # 로컬은 "보유 없음"

    class _KisConfirmedPositionManager:
        last_sync_ok = True
        broker = object()
        current_position = {
            "symbol": INVERSE_SYMBOL, "name": INVERSE_NAME, "quantity": 1480 - 648,
            "avg_price": 9_000.0, "conflict": False,
        }

    apply_position_manager_to_state(state, _KisConfirmedPositionManager())

    assert state["position_sync_status"] == "SYNCED"
    assert state["position"]["symbol"] == INVERSE_SYMBOL
    assert state["position"]["quantity"] == 832


def test_liquidation_queries_broker_even_when_local_state_is_empty():
    from app.trading.hynix_position_common import HynixPositionManager

    state = default_state()
    state["mode"] = "mock"
    broker = LiquidationSyncBroker([
        {"symbol": INVERSE_SYMBOL, "name": INVERSE_NAME, "quantity": 18, "avg_price": 5_000.0}
    ])
    pm = HynixPositionManager(broker, mode="mock")

    result = run_liquidation_if_needed(
        datetime(2026, 7, 9, 15, 16), state, broker,
        hynix_price=101_000.0, inverse_price=5_100.0, position_manager=pm,
    )

    assert result["liquidated"] is True
    assert broker.sell_calls == [(INVERSE_SYMBOL, 18, 5_100.0)]
    assert state["position"]["symbol"] is None
    assert state["liquidation_done"] is True


class TestExecutionLedgerCostFields:
    """2026-07-13 사용자 검증 — 모든 체결에 거래비용 필드가 숫자(0.0 포함)로 기록되고,
    Prediction V2 등 Adaptive Fusion 메타데이터가 전달되면 그대로 원장에 남는지 확인."""

    def test_buy_records_nonzero_buy_fee_and_no_nan(self):
        from app.trading.hynix_switch_position_manager import _buy_new
        from app.services.hynix_execution_ledger import load_ledger

        broker = DummyBroker(buy_success=True, buyable_cash=10_000_000.0)
        orders: list = []
        _buy_new(broker, LONG_SYMBOL, current_price=100_000.0, cash_amount=1_000_000.0, reason="test", orders=orders, mode="mock")

        df = load_ledger()
        row = df.iloc[0]
        assert row["buy_fee"] > 0.0
        assert not pd.isna(row["buy_fee"])
        assert not pd.isna(row["sell_fee"])
        assert not pd.isna(row["transaction_tax"])
        assert not pd.isna(row["slippage_cost"])
        assert not pd.isna(row["gross_pnl"])
        assert not pd.isna(row["net_pnl"])
        assert row["sell_fee"] == pytest.approx(0.0)
        assert row["gross_pnl"] == pytest.approx(0.0)

    def test_stock_sell_records_transaction_tax(self):
        from app.trading.hynix_switch_position_manager import _sell_all_or_ratio
        from app.services.hynix_execution_ledger import load_ledger

        broker = DummyBroker(sell_success=True)
        position = {"symbol": LONG_SYMBOL, "quantity": 10, "entry_price": 100_000.0}
        orders: list = []
        _sell_all_or_ratio(broker, position, current_price=103_000.0, ratio=1.0, reason="test", orders=orders, mode="mock")

        df = load_ledger()
        row = df[df["action"] == "SELL"].iloc[-1]
        assert row["transaction_tax"] > 0.0
        assert row["gross_pnl"] == pytest.approx((103_000.0 - 100_000.0) * 10)
        assert row["net_pnl"] < row["gross_pnl"]

    def test_etf_sell_records_zero_transaction_tax_explicitly(self):
        from app.trading.hynix_switch_position_manager import _sell_all_or_ratio
        from app.services.hynix_execution_ledger import load_ledger
        import pandas as _pd

        broker = DummyBroker(sell_success=True)
        position = {"symbol": INVERSE_SYMBOL, "quantity": 100, "entry_price": 10_000.0}
        orders: list = []
        _sell_all_or_ratio(broker, position, current_price=10_200.0, ratio=1.0, reason="test", orders=orders, mode="mock")

        df = load_ledger()
        row = df[(df["action"] == "SELL") & (df["symbol"] == INVERSE_SYMBOL)].iloc[-1]
        assert not _pd.isna(row["transaction_tax"])
        assert row["transaction_tax"] == pytest.approx(0.0)

    def test_fusion_metadata_recorded_when_provided(self):
        from app.trading.hynix_switch_position_manager import _buy_new
        from app.services.hynix_execution_ledger import load_ledger

        broker = DummyBroker(buy_success=True, buyable_cash=10_000_000.0)
        orders: list = []
        fusion_metadata = {
            "active_probability": 70.0, "prediction_v2_probability": 65.0, "cycle_probability": 55.0,
            "fused_probability": 68.0, "prediction_v2_weight": 0.18, "dominant_model": "ACTIVE_FUSION",
            "model_agreement": 82.0, "expected_value": 0.3, "target_position_pct": 35.0,
        }
        _buy_new(
            broker, LONG_SYMBOL, current_price=100_000.0, cash_amount=1_000_000.0, reason="test",
            orders=orders, mode="mock", signal_source="ADAPTIVE_FUSION", fusion_metadata=fusion_metadata,
        )

        df = load_ledger()
        row = df.iloc[0]
        assert row["signal_source"] == "ADAPTIVE_FUSION"
        assert row["prediction_v2_probability"] == pytest.approx(65.0)
        assert row["dominant_model"] == "ACTIVE_FUSION"
        assert row["prediction_v2_weight"] == pytest.approx(0.18)

    def test_ui_net_pnl_sum_matches_ledger_net_pnl_sum(self):
        from app.trading.hynix_switch_position_manager import _buy_new, _sell_all_or_ratio
        from app.services.hynix_execution_ledger import load_ledger, compute_performance_stats

        broker = DummyBroker(buy_success=True, sell_success=True, buyable_cash=10_000_000.0)
        orders: list = []
        _buy_new(broker, LONG_SYMBOL, current_price=100_000.0, cash_amount=1_000_000.0, reason="test", orders=orders, mode="mock")
        position = {"symbol": LONG_SYMBOL, "quantity": 10, "entry_price": 100_000.0}
        _sell_all_or_ratio(broker, position, current_price=103_000.0, ratio=1.0, reason="test", orders=orders, mode="mock")

        df = load_ledger()
        ledger_net_sum = round(float(pd.to_numeric(df[df["action"] == "SELL"]["net_pnl"], errors="coerce").sum()), 2)
        stats = compute_performance_stats(datetime.now().strftime("%Y%m%d"))
        assert round(stats["cumulative_realized_pnl"], 0) == pytest.approx(round(ledger_net_sum, 0), abs=1.0)
