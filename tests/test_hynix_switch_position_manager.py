"""
test_hynix_switch_position_manager.py — 스위칭/TP·SL/당일 강제청산 검증.

mock/real 모두 브로커의 buy()/sell() 호출 방식은 동일하므로(브로커 구현만 다름),
아래 DummyBroker로 두 모드의 매매 로직을 동일하게 검증한다.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from app.models import OrderResult
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL, HYNIX_NAME
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME
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


def _holding_state(symbol: str, quantity: int = 10, entry_price: float = 100_000.0) -> dict:
    state = default_state()
    state["position"] = {
        "symbol": symbol, "name": HYNIX_NAME if symbol == HYNIX_SYMBOL else INVERSE_NAME,
        "quantity": quantity, "avg_price": entry_price, "entry_price": entry_price,
        "entry_time": datetime.now().isoformat(), "partial_tp1_done": False, "partial_sl1_done": False,
    }
    return state


def test_switch_from_hynix_to_inverse_sells_then_buys():
    state = _holding_state(HYNIX_SYMBOL)
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "INVERSE_BUY", 101_000, 5_000, now=now)

    assert result["acted"] is True
    assert len(broker.sell_calls) == 1 and broker.sell_calls[0][0] == HYNIX_SYMBOL
    assert len(broker.buy_calls) == 1 and broker.buy_calls[0][0] == INVERSE_SYMBOL
    assert state["position"]["symbol"] == INVERSE_SYMBOL


def test_switch_from_inverse_to_hynix_sells_then_buys():
    state = _holding_state(INVERSE_SYMBOL, entry_price=5_000)
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 101_000, 5_100, now=now)

    assert result["acted"] is True
    assert broker.sell_calls[0][0] == INVERSE_SYMBOL
    assert broker.buy_calls[0][0] == HYNIX_SYMBOL
    assert state["position"]["symbol"] == HYNIX_SYMBOL


def test_buy_bumps_to_minimum_one_share_when_sizing_too_small():
    """사이징(20%) 금액으로는 1주도 못 사지만, 실제 매수가능금액은 충분하면 최소 1주로 상향해야 한다."""
    from app.services.hynix_switch_state import default_state

    state = default_state()  # 무보유
    broker = DummyBroker(buyable_cash=10_000_000)  # 20% = 2,000,000원 < 9,000,000원(1주가)
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 9_000_000, 5_000, now=now)

    assert result["acted"] is True
    assert len(broker.buy_calls) == 1
    assert broker.buy_calls[0] == (HYNIX_SYMBOL, 1, 9_000_000)
    assert state["position"]["symbol"] == HYNIX_SYMBOL
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
    state = _holding_state(HYNIX_SYMBOL)
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 15, 0)  # 14:50 이후

    result = run_switch_or_entry(state, broker, "INVERSE_BUY", 101_000, 5_000, now=now)

    assert len(broker.sell_calls) == 1
    assert len(broker.buy_calls) == 0
    assert state["position"]["symbol"] is None
    assert "14:50" in result["message"]


def test_liquidation_success_clears_position():
    state = _holding_state(HYNIX_SYMBOL)
    broker = DummyBroker(sell_success=True)
    now = datetime(2026, 7, 9, 15, 16)

    result = run_liquidation_if_needed(now, state, broker, 101_000, 5_000)

    assert result["liquidated"] is True
    assert state["position"]["symbol"] is None
    assert state["liquidation_done"] is True
    assert state["critical_alert"] is None


def test_liquidation_failure_retries_once_then_critical_alert():
    state = _holding_state(HYNIX_SYMBOL)
    broker = DummyBroker(sell_success=False)
    now = datetime(2026, 7, 9, 15, 16)

    result = run_liquidation_if_needed(now, state, broker, 101_000, 5_000)

    assert result["liquidated"] is False
    assert len(broker.sell_calls) == 2  # 1회 시도 + 1회 재시도
    assert state["position"]["symbol"] == HYNIX_SYMBOL  # 포지션 유지
    assert state["critical_alert"] is not None


def test_liquidation_not_triggered_before_1515():
    state = _holding_state(HYNIX_SYMBOL)
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
    position = {"symbol": HYNIX_SYMBOL, "quantity": 10, "entry_price": 100_000.0, "partial_tp1_done": False, "partial_sl1_done": False}
    trigger = evaluate_tp_sl(position, current_price=102_100.0)  # +2.1%
    assert trigger is not None and trigger["tag"] == "tp2" and trigger["ratio"] == 1.0


def test_tp1_partial_take_profit_triggers():
    position = {"symbol": HYNIX_SYMBOL, "quantity": 10, "entry_price": 100_000.0, "partial_tp1_done": False, "partial_sl1_done": False}
    trigger = evaluate_tp_sl(position, current_price=101_300.0)  # +1.3%
    assert trigger is not None and trigger["tag"] == "tp1" and trigger["ratio"] == 0.5


def test_sl2_full_stop_loss_triggers():
    position = {"symbol": HYNIX_SYMBOL, "quantity": 10, "entry_price": 100_000.0, "partial_tp1_done": False, "partial_sl1_done": False}
    trigger = evaluate_tp_sl(position, current_price=98_400.0)  # -1.6%
    assert trigger is not None and trigger["tag"] == "sl2"


def test_run_tp_sl_if_needed_executes_and_clears_position():
    state = _holding_state(HYNIX_SYMBOL, quantity=10, entry_price=100_000.0)
    broker = DummyBroker(sell_success=True)

    result = run_tp_sl_if_needed(state, broker, hynix_price=102_500.0, inverse_price=5_000.0)

    assert result["triggered"] is True and result["executed"] is True
    assert state["position"]["symbol"] is None


class TestExecutionLedgerCostFields:
    """2026-07-13 사용자 검증 — 모든 체결에 거래비용 필드가 숫자(0.0 포함)로 기록되고,
    Prediction V2 등 Adaptive Fusion 메타데이터가 전달되면 그대로 원장에 남는지 확인."""

    def test_buy_records_nonzero_buy_fee_and_no_nan(self):
        from app.trading.hynix_switch_position_manager import _buy_new
        from app.services.hynix_execution_ledger import load_ledger

        broker = DummyBroker(buy_success=True, buyable_cash=10_000_000.0)
        orders: list = []
        _buy_new(broker, HYNIX_SYMBOL, current_price=100_000.0, cash_amount=1_000_000.0, reason="test", orders=orders, mode="mock")

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
        position = {"symbol": HYNIX_SYMBOL, "quantity": 10, "entry_price": 100_000.0}
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
            broker, HYNIX_SYMBOL, current_price=100_000.0, cash_amount=1_000_000.0, reason="test",
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
        _buy_new(broker, HYNIX_SYMBOL, current_price=100_000.0, cash_amount=1_000_000.0, reason="test", orders=orders, mode="mock")
        position = {"symbol": HYNIX_SYMBOL, "quantity": 10, "entry_price": 100_000.0}
        _sell_all_or_ratio(broker, position, current_price=103_000.0, ratio=1.0, reason="test", orders=orders, mode="mock")

        df = load_ledger()
        ledger_net_sum = round(float(pd.to_numeric(df[df["action"] == "SELL"]["net_pnl"], errors="coerce").sum()), 2)
        stats = compute_performance_stats(datetime.now().strftime("%Y%m%d"))
        assert round(stats["cumulative_realized_pnl"], 0) == pytest.approx(round(ledger_net_sum, 0), abs=1.0)
