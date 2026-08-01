from __future__ import annotations

import threading
import time

from app.models import OrderResult, Position
from app.trading.exit_order_coordinator import reset_for_tests, snapshot
from app.trading.hynix_symbols import LONG_SYMBOL, LONG_NAME
from app.trading.hynix_switch_position_manager import _sell_all_or_ratio


class ThreadSafeBroker:
    mode = "mock"

    def __init__(self, quantity: int = 10, delay: float = 0.0, sell_success: bool = True):
        self.quantity = quantity
        self.delay = delay
        self.sell_success = sell_success
        self.sell_calls = []
        self._lock = threading.Lock()

    def get_positions(self):
        with self._lock:
            if self.quantity <= 0:
                return []
            return [Position(symbol=LONG_SYMBOL, name=LONG_NAME, quantity=self.quantity, avg_price=100_000)]

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            sent = min(int(quantity), self.quantity)
            self.sell_calls.append((symbol, sent, price))
            if self.sell_success:
                self.quantity -= sent
            return OrderResult(
                success=self.sell_success,
                mode="mock",
                account_type="mock",
                symbol=symbol,
                name=name,
                side="sell",
                quantity=sent,
                price=price,
                order_type=order_type,
                order_id=f"S{len(self.sell_calls)}" if self.sell_success else "",
                message="ok" if self.sell_success else "timeout",
            )


def _position(quantity: int = 10):
    return {
        "symbol": LONG_SYMBOL,
        "name": LONG_NAME,
        "quantity": quantity,
        "entry_price": 100_000,
        "avg_price": 100_000,
    }


def _sell(broker, ratio, reason, *, exit_event_id, severity):
    orders = []
    return _sell_all_or_ratio(
        broker,
        _position(10),
        current_price=99_000,
        ratio=ratio,
        reason=reason,
        orders=orders,
        mode="mock",
        exit_reason_type="stop_loss" if severity == "HARD_STOP" else "switch",
        signal_source="TEST",
        fusion_metadata={
            "episode_id": "EP1",
            "exit_event_id": exit_event_id,
            "severity": severity,
            "detected_at": "2026-07-20T10:00:00",
        },
    )


def test_same_exit_event_duplicate_callback_sends_one_sell():
    reset_for_tests()
    broker = ThreadSafeBroker(quantity=10, delay=0.05)
    results = []

    threads = [
        threading.Thread(target=lambda: results.append(_sell(broker, 1.0, "hard stop", exit_event_id="EV1", severity="HARD_STOP")))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(broker.sell_calls) == 1
    assert broker.sell_calls[0][1] == 10
    assert broker.quantity == 0
    assert sum(1 for r in results if r.get("blocked_by_coordinator")) == 1
    assert snapshot()["blocked_duplicate_count"] == 1


def test_partial_and_full_exit_concurrent_never_oversells():
    reset_for_tests()
    broker = ThreadSafeBroker(quantity=10, delay=0.05)
    results = []

    weak = threading.Thread(target=lambda: results.append(_sell(broker, 0.4, "weak reversal", exit_event_id="WEAK1", severity="WEAK")))
    strong = threading.Thread(target=lambda: results.append(_sell(broker, 1.0, "strong reversal", exit_event_id="STRONG1", severity="STRONG")))
    weak.start()
    strong.start()
    weak.join()
    strong.join()

    total_sold = sum(call[1] for call in broker.sell_calls)
    assert total_sold == 10
    assert broker.quantity == 0
    assert len(broker.sell_calls) in (1, 2)
    assert all(call[1] >= 0 for call in broker.sell_calls)


def test_profit_lock_and_hard_stop_concurrent_sell_remaining_only():
    reset_for_tests()
    broker = ThreadSafeBroker(quantity=10, delay=0.05)
    results = []

    profit_lock = threading.Thread(target=lambda: results.append(_sell(broker, 0.5, "profit lock", exit_event_id="PL1", severity="WEAK")))
    hard_stop = threading.Thread(target=lambda: results.append(_sell(broker, 1.0, "hard stop", exit_event_id="HS1", severity="HARD_STOP")))
    profit_lock.start()
    hard_stop.start()
    profit_lock.join()
    hard_stop.join()

    assert sum(call[1] for call in broker.sell_calls) == 10
    assert broker.quantity == 0


def test_timeout_failed_order_allows_explicit_retry_attempt():
    reset_for_tests()
    broker = ThreadSafeBroker(quantity=10, sell_success=False)
    first = _sell(broker, 1.0, "hard stop", exit_event_id="TIMEOUT1", severity="HARD_STOP")
    broker.sell_success = True
    second = _sell(broker, 1.0, "hard stop retry", exit_event_id="TIMEOUT1", severity="HARD_STOP")

    assert first["success"] is False
    assert second["success"] is True
    assert len(broker.sell_calls) == 2
    assert broker.quantity == 0
