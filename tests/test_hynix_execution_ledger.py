"""test_hynix_execution_ledger.py — 단일 거래 원장 기록/집계/재구성 테스트."""

from datetime import datetime

import pytest

from app.services import hynix_execution_ledger as ledger


@pytest.fixture(autouse=True)
def _isolate_ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "hynix_execution_ledger.csv"
    monkeypatch.setattr(ledger, "_LEDGER_PATH", path)
    return path


class TestRecordAndLoad:
    def test_record_execution_writes_row(self):
        trade_id = ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9000.0, executed_price=9000.0, success=True, mode="mock",
            signal_source="ENHANCED_LEGACY", before_qty=0, after_qty=100,
        )
        assert trade_id
        df = ledger.load_ledger()
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "0197X0"
        assert bool(df.iloc[0]["success"]) is True

    def test_failed_orders_are_also_recorded(self):
        ledger.record_execution(
            action="BUY", symbol="000660", requested_qty=1, executed_qty=0,
            requested_price=2200000.0, executed_price=None, success=False, mode="mock",
        )
        df = ledger.load_ledger()
        assert len(df) == 1
        assert bool(df.iloc[0]["success"]) is False


class TestTradeCounters:
    def _seed_round_trip(self, is_test=False):
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9000.0, executed_price=9000.0, success=True, mode="mock",
            before_qty=0, after_qty=100, is_test_order=is_test,
            now=datetime(2026, 7, 10, 11, 15),
        )
        ledger.record_execution(
            action="SELL", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9100.0, executed_price=9100.0, success=True, mode="mock",
            before_qty=100, after_qty=0, realized_pnl=10000.0, is_test_order=is_test,
            now=datetime(2026, 7, 10, 11, 20),
        )

    def test_counts_buy_sell_and_round_trip(self):
        self._seed_round_trip()
        counters = ledger.compute_trade_counters("20260710")
        assert counters["buy_fill_count"] == 1
        assert counters["sell_fill_count"] == 1
        assert counters["round_trip_count"] == 1
        assert counters["test_order_count"] == 0

    def test_test_orders_excluded_from_live_counters(self):
        self._seed_round_trip(is_test=True)
        counters = ledger.compute_trade_counters("20260710")
        assert counters["live_order_count"] == 0
        assert counters["test_order_count"] == 2


class TestRealizedPnlBreakdown:
    def test_sums_realized_pnl_from_sells_only(self):
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9000.0, executed_price=9000.0, success=True, mode="mock",
            before_qty=0, after_qty=100, now=datetime(2026, 7, 10, 11, 15),
        )
        ledger.record_execution(
            action="SELL", symbol="0197X0", requested_qty=50, executed_qty=50,
            requested_price=8900.0, executed_price=8900.0, success=True, mode="mock",
            before_qty=100, after_qty=50, realized_pnl=-5000.0, now=datetime(2026, 7, 10, 11, 20),
        )
        ledger.record_execution(
            action="SELL", symbol="0197X0", requested_qty=50, executed_qty=50,
            requested_price=8800.0, executed_price=8800.0, success=True, mode="mock",
            before_qty=50, after_qty=0, realized_pnl=-10000.0, now=datetime(2026, 7, 10, 11, 32),
        )
        breakdown = ledger.compute_realized_pnl_breakdown("20260710")
        assert breakdown["total_realized_pnl"] == -15000.0
        assert len(breakdown["trades"]) == 2

    def test_excludes_test_orders_from_pnl(self):
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=1, executed_qty=1,
            requested_price=9000.0, executed_price=9000.0, success=True, mode="mock",
            before_qty=0, after_qty=1, is_test_order=True, now=datetime(2026, 7, 10, 10, 31),
        )
        ledger.record_execution(
            action="SELL", symbol="0197X0", requested_qty=1, executed_qty=1,
            requested_price=9000.0, executed_price=9000.0, success=True, mode="mock",
            before_qty=1, after_qty=0, realized_pnl=99999.0, is_test_order=True,
            now=datetime(2026, 7, 10, 10, 31),
        )
        breakdown = ledger.compute_realized_pnl_breakdown("20260710")
        assert breakdown["total_realized_pnl"] == 0.0


class TestReconcile:
    def test_flags_position_mismatch(self):
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9000.0, executed_price=9000.0, success=True, mode="mock",
            before_qty=0, after_qty=100, now=datetime(2026, 7, 10, 11, 15),
        )

        class _FakeBroker:
            def get_positions(self):
                return [{"symbol": "0197X0", "quantity": 999}]

        result = ledger.reconcile_execution_ledger("20260710", broker=_FakeBroker())
        assert result["position_match"] is False
        assert result["mismatches"]

class TestPerformanceStats:
    def test_computes_win_rate_and_profit_factor(self):
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9000.0, executed_price=9000.0, success=True, mode="mock",
            before_qty=0, after_qty=100, signal_source="ACTIVE_STRATEGY_MOCK", now=datetime(2026, 7, 10, 11, 0),
        )
        ledger.record_execution(
            action="SELL", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9200.0, executed_price=9200.0, success=True, mode="mock",
            before_qty=100, after_qty=0, realized_pnl=20000.0, signal_source="ACTIVE_STRATEGY_MOCK",
            now=datetime(2026, 7, 10, 11, 30),
        )
        ledger.record_execution(
            action="BUY", symbol="000660", requested_qty=10, executed_qty=10,
            requested_price=100000.0, executed_price=100000.0, success=True, mode="mock",
            before_qty=0, after_qty=10, signal_source="ENHANCED_LEGACY", now=datetime(2026, 7, 10, 13, 0),
        )
        ledger.record_execution(
            action="SELL", symbol="000660", requested_qty=10, executed_qty=10,
            requested_price=99000.0, executed_price=99000.0, success=True, mode="mock",
            before_qty=10, after_qty=0, realized_pnl=-10000.0, signal_source="ENHANCED_LEGACY",
            now=datetime(2026, 7, 10, 13, 15),
        )
        stats = ledger.compute_performance_stats("20260710")
        assert stats["win_rate"] == 50.0
        assert stats["profit_factor"] == pytest.approx(2.0)
        assert stats["cumulative_realized_pnl"] == 10000.0
        assert stats["pnl_by_signal_source"]["ACTIVE_STRATEGY_MOCK"] == 20000.0
        assert stats["avg_holding_minutes"] == pytest.approx((30 + 15) / 2)
        assert stats["round_trip_count"] == 2


    def test_matching_position_has_no_mismatch(self):
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9000.0, executed_price=9000.0, success=True, mode="mock",
            before_qty=0, after_qty=100, now=datetime(2026, 7, 10, 11, 15),
        )

        class _FakeBroker:
            def get_positions(self):
                return [{"symbol": "0197X0", "quantity": 100}]

        result = ledger.reconcile_execution_ledger("20260710", broker=_FakeBroker())
        assert result["position_match"] is True
        assert result["mismatches"] == []
