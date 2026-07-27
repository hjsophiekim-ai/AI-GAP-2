from __future__ import annotations

from app.trading.kis_mock_broker import KisMockBroker
from app.trading.macd2 import config, ledger
from app.trading.macd2.broker_adapter import MockBrokerAdapter


class _FakeKis:
    def __init__(self):
        self.next_order_id = "ORD-DIRECT-1"

    def buy(self, symbol, quantity, price, order_type="limit"):
        return {
            "success": True,
            "order_id": self.next_order_id,
            "message": "OK",
            "raw": {"ODNO": self.next_order_id},
            "http_status": 200,
            "rt_cd": "0",
            "msg_cd": "40600000",
            "msg1": "OK",
        }

    def sell(self, symbol, quantity, price, order_type="limit"):
        return {
            "success": True,
            "order_id": self.next_order_id,
            "message": "OK",
            "raw": {"ODNO": self.next_order_id},
            "http_status": 200,
            "rt_cd": "0",
            "msg_cd": "40590000",
            "msg1": "OK",
        }


def test_direct_mock_kis_buy_is_written_to_macd2_execution_ledger():
    broker = KisMockBroker(_FakeKis())

    result = broker.buy(config.LONG_SYMBOL, config.LONG_SYMBOL, 1, 0, order_type="market")

    assert result.success is True
    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["order_id"] == "ORD-DIRECT-1"
    assert rows[0]["signal_id"] == "BROKER_DIRECT"
    assert rows[0]["symbol"] == config.LONG_SYMBOL
    assert rows[0]["side"] == "BUY"
    assert rows[0]["requested_qty"] == "1"
    assert rows[0]["executed_qty"] == "1"
    assert rows[0]["exit_reason"] == "BROKER_DIRECT"


def test_direct_mock_kis_sell_is_written_to_macd2_execution_ledger():
    fake_kis = _FakeKis()
    fake_kis.next_order_id = "ORD-DIRECT-SELL-1"
    broker = KisMockBroker(fake_kis)

    result = broker.sell(config.LONG_SYMBOL, config.LONG_SYMBOL, 1, 0, order_type="market")

    assert result.success is True
    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["order_id"] == "ORD-DIRECT-SELL-1"
    assert rows[0]["side"] == "SELL"
    assert rows[0]["executed_qty"] == "1"


def test_macd2_adapter_suppresses_direct_row_so_order_executor_can_write_detail():
    broker = KisMockBroker(_FakeKis())
    adapter = MockBrokerAdapter(broker=broker)

    result = adapter.buy_market(config.LONG_SYMBOL, 1, "signal-1")

    assert result.success is True
    assert result.order_id == "ORD-DIRECT-1"
    assert ledger.load_execution_ledger() == []


def test_non_macd2_symbol_direct_order_is_not_written_to_macd2_ledger():
    broker = KisMockBroker(_FakeKis())

    result = broker.buy("005930", "Samsung", 1, 0, order_type="market")

    assert result.success is True
    assert ledger.load_execution_ledger() == []
