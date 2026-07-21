"""test_hynix_execution_ledger.py — 단일 거래 원장 기록/집계/재구성 테스트."""

from datetime import datetime
from pathlib import Path

import pandas as pd
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

    def test_zero_qty_and_reconcile_backfill_excluded_from_trade_counters(self):
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=1, executed_qty=0,
            requested_price=9000.0, executed_price=None, success=True, mode="mock",
            before_qty=0, after_qty=0, now=datetime(2026, 7, 10, 11, 15),
        )
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9000.0, executed_price=9000.0, success=True, mode="mock",
            before_qty=0, after_qty=100, signal_source=ledger.SIGNAL_SOURCE_KIS_RECONCILE_BACKFILL,
            now=datetime(2026, 7, 10, 11, 16),
        )

        df = ledger.load_ledger("20260710")
        assert bool(df.iloc[0]["success"]) is False

        counters = ledger.compute_trade_counters("20260710")
        stats = ledger.compute_performance_stats("20260710")
        assert counters["live_order_count"] == 0
        assert stats["order_fill_count"] == 0


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


class TestCurrentPositionDetail:
    def test_no_symbol_returns_empty(self):
        result = ledger.compute_current_position_detail(None)
        assert result["has_position"] is False

    def test_single_entry_computes_avg_price_and_times(self):
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=468, executed_qty=468,
            requested_price=10680.0, executed_price=10680.0, success=True, mode="mock",
            before_qty=0, after_qty=468, now=datetime(2026, 7, 13, 9, 5),
        )
        result = ledger.compute_current_position_detail("0197X0", total_equity=5_000_000.0)
        assert result["has_position"] is True
        assert result["avg_buy_price"] == pytest.approx(10680.0)
        assert result["total_invested_krw"] == pytest.approx(468 * 10680.0)
        assert result["buy_count_in_position"] == 1
        assert result["first_entry_time"].startswith("2026-07-13T09:05")
        assert result["last_add_time"] == result["first_entry_time"]
        assert result["position_pct"] == pytest.approx(468 * 10680.0 / 5_000_000.0 * 100, rel=1e-3)

    def test_scale_in_computes_weighted_avg_and_last_add_time(self):
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=200, executed_qty=200,
            requested_price=10000.0, executed_price=10000.0, success=True, mode="mock",
            before_qty=0, after_qty=200, now=datetime(2026, 7, 13, 9, 5),
        )
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=200, executed_qty=200,
            requested_price=10200.0, executed_price=10200.0, success=True, mode="mock",
            before_qty=200, after_qty=400, now=datetime(2026, 7, 13, 9, 30),
        )
        result = ledger.compute_current_position_detail("0197X0")
        assert result["avg_buy_price"] == pytest.approx(10100.0)
        assert result["buy_count_in_position"] == 2
        assert result["first_entry_time"].startswith("2026-07-13T09:05")
        assert result["last_add_time"].startswith("2026-07-13T09:30")

    def test_closed_position_returns_empty(self):
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9000.0, executed_price=9000.0, success=True, mode="mock",
            before_qty=0, after_qty=100, now=datetime(2026, 7, 13, 9, 0),
        )
        ledger.record_execution(
            action="SELL", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9200.0, executed_price=9200.0, success=True, mode="mock",
            before_qty=100, after_qty=0, realized_pnl=20000.0, now=datetime(2026, 7, 13, 9, 30),
        )
        result = ledger.compute_current_position_detail("0197X0")
        assert result["has_position"] is False

    def test_new_entry_after_full_exit_ignores_prior_episode(self):
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9000.0, executed_price=9000.0, success=True, mode="mock",
            before_qty=0, after_qty=100, now=datetime(2026, 7, 13, 9, 0),
        )
        ledger.record_execution(
            action="SELL", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9200.0, executed_price=9200.0, success=True, mode="mock",
            before_qty=100, after_qty=0, realized_pnl=20000.0, now=datetime(2026, 7, 13, 9, 30),
        )
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=300, executed_qty=300,
            requested_price=10500.0, executed_price=10500.0, success=True, mode="mock",
            before_qty=0, after_qty=300, now=datetime(2026, 7, 13, 10, 0),
        )
        result = ledger.compute_current_position_detail("0197X0")
        assert result["has_position"] is True
        assert result["avg_buy_price"] == pytest.approx(10500.0)
        assert result["buy_count_in_position"] == 1
        assert result["first_entry_time"].startswith("2026-07-13T10:00")


class TestAdaptiveFusionSchemaMigration:
    def test_new_columns_present_in_fresh_ledger(self):
        ledger.record_execution(
            action="BUY", symbol="000660", requested_qty=1, executed_qty=1,
            requested_price=100000.0, executed_price=100000.0, success=True, mode="mock",
            before_qty=0, after_qty=1, signal_source=ledger.SIGNAL_SOURCE_ADAPTIVE_FUSION,
            active_probability=70.0, prediction_v2_probability=65.0, cycle_probability=60.0,
            fused_probability=68.0, prediction_v2_weight=0.2, dominant_model="ACTIVE_FUSION",
            model_agreement=80.0, expected_value=0.25, target_position_pct=35.0,
        )
        df = ledger.load_ledger()
        assert df.iloc[0]["active_probability"] == pytest.approx(70.0)
        assert df.iloc[0]["fused_probability"] == pytest.approx(68.0)
        assert df.iloc[0]["target_position_pct"] == pytest.approx(35.0)
        assert df.iloc[0]["dominant_model"] == "ACTIVE_FUSION"
        # 거래비용 필드는 명시하지 않아도 NaN이 아니라 0.0으로 채워져야 한다.
        assert df.iloc[0]["buy_fee"] == pytest.approx(0.0)
        assert df.iloc[0]["net_pnl"] == pytest.approx(0.0)

    def test_migrates_legacy_header_without_losing_rows(self, tmp_path):
        legacy_path = ledger._LEDGER_PATH
        legacy_header = [
            "trade_id", "parent_trade_id", "timestamp", "mode", "environment", "strategy_name",
            "signal_source", "action", "symbol", "requested_qty", "executed_qty", "requested_price",
            "executed_price", "before_qty", "after_qty", "cash_before", "cash_after", "realized_pnl",
            "fees", "tax", "success", "order_id", "position_confirmed", "is_test_order",
        ]
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        with legacy_path.open("w", newline="", encoding="utf-8-sig") as fh:
            import csv as _csv
            writer = _csv.DictWriter(fh, fieldnames=legacy_header)
            writer.writeheader()
            writer.writerow({
                "trade_id": "legacy1", "parent_trade_id": "", "timestamp": "2026-07-01T09:00:00",
                "mode": "mock", "environment": "MOCK", "strategy_name": "hynix_switch",
                "signal_source": "ENHANCED_LEGACY", "action": "BUY", "symbol": "0197X0",
                "requested_qty": 100, "executed_qty": 100, "requested_price": 9000.0,
                "executed_price": 9000.0, "before_qty": 0, "after_qty": 100,
                "cash_before": 10000000.0, "cash_after": 9100000.0, "realized_pnl": "",
                "fees": 0.0, "tax": 0.0, "success": True, "order_id": "", "position_confirmed": "",
                "is_test_order": False,
            })

        # 새 컬럼을 쓰는 기록이 들어오면 기존 legacy 행을 잃지 않고 헤더가 확장되어야 한다.
        ledger.record_execution(
            action="SELL", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9200.0, executed_price=9200.0, success=True, mode="mock",
            before_qty=100, after_qty=0, realized_pnl=20000.0,
            signal_source=ledger.SIGNAL_SOURCE_ADAPTIVE_FUSION, fused_probability=72.0,
        )

        df = ledger.load_ledger()
        assert len(df) == 2
        assert df.iloc[0]["trade_id"] == "legacy1"
        assert pd.isna(df.iloc[0]["fused_probability"]) or df.iloc[0]["fused_probability"] == ""
        assert df.iloc[1]["fused_probability"] == pytest.approx(72.0)

    def test_load_ledger_never_raises_keyerror_on_legacy_header(self):
        legacy_header = [
            "trade_id", "parent_trade_id", "timestamp", "mode", "environment", "strategy_name",
            "signal_source", "action", "symbol", "requested_qty", "executed_qty", "requested_price",
            "executed_price", "before_qty", "after_qty", "cash_before", "cash_after", "realized_pnl",
            "fees", "tax", "success", "order_id", "position_confirmed", "is_test_order",
        ]
        ledger._LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ledger._LEDGER_PATH.open("w", newline="", encoding="utf-8-sig") as fh:
            import csv as _csv
            writer = _csv.DictWriter(fh, fieldnames=legacy_header)
            writer.writeheader()
            writer.writerow({
                "trade_id": "legacy1", "parent_trade_id": "", "timestamp": "2026-07-01T09:00:00",
                "mode": "mock", "environment": "MOCK", "strategy_name": "hynix_switch",
                "signal_source": "ENHANCED_LEGACY", "action": "BUY", "symbol": "0197X0",
                "requested_qty": 100, "executed_qty": 100, "requested_price": 9000.0,
                "executed_price": 9000.0, "before_qty": 0, "after_qty": 100,
                "cash_before": 10000000.0, "cash_after": 9100000.0, "realized_pnl": "",
                "fees": 0.0, "tax": 0.0, "success": True, "order_id": "", "position_confirmed": "",
                "is_test_order": False,
            })
        df = ledger.load_ledger()
        assert "active_probability" in df.columns
        assert len(df) == 1


class TestTradeCostReconstruction:
    """섹션 8 — 비용엔진 도입 이전 기록(gross_pnl/buy_fee 등이 0/빈 값)의 완료 왕복거래를
    재계산하는 리포트/백필 함수 테스트."""

    def _seed_pre_cost_engine_trade(self, symbol, buy_price, sell_price, qty, ts_buy, ts_sell):
        ledger.record_execution(
            action="BUY", symbol=symbol, requested_qty=qty, executed_qty=qty,
            requested_price=buy_price, executed_price=buy_price, success=True, mode="mock",
            before_qty=0, after_qty=qty, now=ts_buy,
        )
        return ledger.record_execution(
            action="SELL", symbol=symbol, requested_qty=qty, executed_qty=qty,
            requested_price=sell_price, executed_price=sell_price, success=True, mode="mock",
            before_qty=qty, after_qty=0, realized_pnl=round((sell_price - buy_price) * qty, 2), now=ts_sell,
        )

    def test_reconstruct_computes_gross_and_net_for_completed_round_trip(self):
        self._seed_pre_cost_engine_trade(
            "0197X0", 10_680.0, 10_720.0, 468, datetime(2026, 7, 13, 9, 54, 6), datetime(2026, 7, 13, 10, 14, 43),
        )
        report = ledger.reconstruct_trade_costs_for_date("20260713")
        assert len(report["trades"]) == 1
        t = report["trades"][0]
        assert t["gross_pnl"] == pytest.approx((10_720.0 - 10_680.0) * 468)
        assert t["net_pnl"] < t["gross_pnl"]
        assert t["transaction_tax"] == pytest.approx(0.0)  # 0197X0은 ETF/ETN — 거래세 면제
        assert report["totals"]["net_realized_pnl"] == pytest.approx(t["net_pnl"])

    def test_reconstruct_stock_includes_transaction_tax(self):
        self._seed_pre_cost_engine_trade(
            "000660", 100_000.0, 103_000.0, 10, datetime(2026, 7, 13, 9, 0, 0), datetime(2026, 7, 13, 9, 30, 0),
        )
        report = ledger.reconstruct_trade_costs_for_date("20260713")
        assert report["trades"][0]["transaction_tax"] > 0.0

    def test_reconstruct_handles_multiple_round_trips_fifo(self):
        self._seed_pre_cost_engine_trade(
            "0197X0", 10_000.0, 10_100.0, 100, datetime(2026, 7, 13, 9, 0, 0), datetime(2026, 7, 13, 9, 10, 0),
        )
        self._seed_pre_cost_engine_trade(
            "0197X0", 10_200.0, 10_050.0, 100, datetime(2026, 7, 13, 9, 15, 0), datetime(2026, 7, 13, 9, 25, 0),
        )
        report = ledger.reconstruct_trade_costs_for_date("20260713")
        assert len(report["trades"]) == 2
        assert report["trades"][0]["trade_no"] == 1
        assert report["trades"][1]["trade_no"] == 2

    def test_backfill_updates_ledger_rows_in_place(self):
        sell_trade_id = self._seed_pre_cost_engine_trade(
            "0197X0", 10_680.0, 10_720.0, 468, datetime(2026, 7, 13, 9, 54, 6), datetime(2026, 7, 13, 10, 14, 43),
        )
        result = ledger.backfill_trading_costs_into_ledger("20260713")
        assert result["updated_rows"] == 1

        df = ledger.load_ledger("20260713")
        row = df[df["trade_id"] == sell_trade_id].iloc[0]
        assert row["gross_pnl"] == pytest.approx((10_720.0 - 10_680.0) * 468)
        assert row["net_pnl"] < row["gross_pnl"]
        assert row["realized_pnl"] == pytest.approx(row["net_pnl"])

    def test_cost_breakdown_stats_sums_across_todays_sells(self):
        from datetime import datetime as _dt

        ledger.record_execution(
            action="BUY", symbol="000660", requested_qty=10, executed_qty=10,
            requested_price=100_000.0, executed_price=100_000.0, success=True, mode="mock",
            before_qty=0, after_qty=10, buy_fee=150.0, now=_dt(2026, 7, 13, 9, 0),
        )
        ledger.record_execution(
            action="SELL", symbol="000660", requested_qty=10, executed_qty=10,
            requested_price=103_000.0, executed_price=103_000.0, success=True, mode="mock",
            before_qty=10, after_qty=0, gross_pnl=30_000.0, buy_fee=150.0, sell_fee=155.0,
            transaction_tax=1800.0, slippage_cost=40.0, net_pnl=27_855.0, realized_pnl=27_855.0,
            now=_dt(2026, 7, 13, 9, 30),
        )
        stats = ledger.compute_cost_breakdown_stats("20260713")
        assert stats["total_buy_fee"] == pytest.approx(150.0)
        assert stats["total_sell_fee"] == pytest.approx(155.0)
        assert stats["total_transaction_tax"] == pytest.approx(1800.0)
        assert stats["total_slippage_cost"] == pytest.approx(40.0)
        assert stats["gross_realized_pnl"] == pytest.approx(30_000.0)
        assert stats["total_trading_cost"] == pytest.approx(2145.0)
        assert stats["net_realized_pnl"] == pytest.approx(27_855.0)

    def test_daily_net_pnl_source_of_truth_for_20260713_ledger(self):
        sell_rows = [
            ("2026-07-13T10:14:43", 468, 10_720.0, 18_720.0, 749.74, 752.54, 0.0, 1001.52, 16_216.20, "DRY-SELL-20260713-0001"),
            ("2026-07-13T10:32:41", 470, 10_980.0, 152_750.0, 751.18, 774.09, 0.0, 1016.85, 150_207.89, "DRY-SELL-20260713-0002"),
            ("2026-07-13T10:37:02", 464, 10_790.0, -78_880.0, 762.82, 750.98, 0.0, 1009.20, -81_403.00, "DRY-SELL-20260713-0003"),
            ("2026-07-13T10:50:33", 468, 11_100.0, 154_440.0, 756.05, 779.22, 0.0, 1023.52, 151_881.21, "DRY-SELL-20260713-0004"),
            ("2026-07-13T11:10:16", 461, 11_205.0, 48_405.0, 767.56, 774.83, 0.0, 1028.26, 45_834.35, "DRY-SELL-20260713-0005"),
            ("2026-07-13T11:13:05", 458, 11_060.0, -77_860.0, 771.50, 759.82, 0.0, 1020.88, -80_412.20, "DRY-SELL-20260713-0006"),
            ("2026-07-13T11:41:44", 462, 11_150.0, 50_820.0, 765.07, 772.69, 0.0, 1025.18, 48_257.06, "DRY-SELL-20260713-0007"),
            ("2026-07-13T12:07:13", 459, 11_515.0, 158_355.0, 769.05, 792.81, 0.0, 1041.24, 155_751.90, "DRY-SELL-20260713-0008"),
        ]
        buy_rows = [
            ("2026-07-13T09:54:06", 468, 10_680.0, "DRY-20260713-0001"),
            ("2026-07-13T10:16:27", 470, 10_655.0, "DRY-20260713-0002"),
            ("2026-07-13T10:34:35", 464, 10_960.0, "DRY-20260713-0003"),
            ("2026-07-13T10:37:29", 468, 10_770.0, "DRY-20260713-0004"),
            ("2026-07-13T10:51:08", 461, 11_100.0, "DRY-20260713-0005"),
            ("2026-07-13T11:10:45", 458, 11_230.0, "DRY-20260713-0006"),
            ("2026-07-13T11:23:10", 462, 11_040.0, "DRY-20260713-0007"),
            ("2026-07-13T11:45:01", 459, 11_170.0, "DRY-20260713-0008"),
        ]

        for i, (buy_ts, buy_qty, buy_price, buy_order_id) in enumerate(buy_rows):
            ledger.record_execution(
                action="BUY", symbol="0197X0", requested_qty=buy_qty, executed_qty=buy_qty,
                requested_price=buy_price, executed_price=buy_price, success=True, mode="mock",
                before_qty=0, after_qty=buy_qty, order_id=buy_order_id, is_test_order=False,
                now=datetime.fromisoformat(buy_ts),
            )
            sell_ts, sell_qty, sell_price, gross, buy_fee, sell_fee, tax, slippage, net, sell_order_id = sell_rows[i]
            ledger.record_execution(
                action="SELL", symbol="0197X0", requested_qty=sell_qty, executed_qty=sell_qty,
                requested_price=sell_price, executed_price=sell_price, success=True, mode="mock",
                before_qty=sell_qty, after_qty=0, order_id=sell_order_id, is_test_order=False,
                gross_pnl=gross, buy_fee=buy_fee, sell_fee=sell_fee, transaction_tax=tax,
                slippage_cost=slippage, net_pnl=net, realized_pnl=net,
                now=datetime.fromisoformat(sell_ts),
            )

        stats = ledger.calculate_daily_net_pnl_from_ledger("20260713")
        assert stats["ledger_raw_row_count"] == 16
        assert stats["operating_trade_count"] == 16
        assert stats["display_row_count"] == 16
        assert stats["buy_fill_count"] == 8
        assert stats["sell_fill_count"] == 8
        assert stats["round_trip_count"] == 8
        assert stats["gross_realized_pnl"] == pytest.approx(426_750.00)
        assert stats["total_buy_fee"] == pytest.approx(6_092.97)
        assert stats["total_sell_fee"] == pytest.approx(6_156.98)
        assert stats["total_transaction_tax"] == pytest.approx(0.00)
        assert stats["total_slippage_cost"] == pytest.approx(8_166.64)
        assert stats["total_trading_cost"] == pytest.approx(20_416.59)
        assert stats["net_realized_pnl"] == pytest.approx(406_333.41)
        assert stats["total_trading_cost"] >= 0
        assert stats["gross_realized_pnl"] != stats["net_realized_pnl"]
        assert stats["net_daily_return_pct"] == pytest.approx(4.063334, abs=0.000001)

        display = stats["trades"]
        assert len(display) == 16
        last = display.iloc[-1]
        assert last["timestamp"].isoformat() == "2026-07-13T12:07:13"
        assert last["action"] == "SELL"
        assert last["symbol"] == "0197X0"
        assert int(last["executed_qty"]) == 459
        assert float(last["executed_price"]) == pytest.approx(11_515.0)
        assert last["order_id"] == "DRY-SELL-20260713-0008"

    def test_legacy_rows_missing_cost_engine_fields_still_show_their_loss(self, _isolate_ledger_path):
        """2026-07-15 사용자 리포트 — 거래비용 엔진 도입 이전(gross_pnl/net_pnl이
        아예 NaN) 체결 중 손실 거래가 있으면, 그 손실 금액이 Gross/Net 실현손익
        요약에서 조용히 0으로 사라지면 안 된다(레거시 realized_pnl로 복원해야 함)."""
        ledger.record_execution(
            action="BUY", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=9_000.0, executed_price=9_000.0, success=True, mode="real",
            before_qty=0, after_qty=100, now=datetime(2026, 7, 10, 11, 20, 0),
        )
        ledger.record_execution(
            action="SELL", symbol="0197X0", requested_qty=100, executed_qty=100,
            requested_price=8_925.0, executed_price=8_925.0, success=True, mode="real",
            before_qty=100, after_qty=0, realized_pnl=-8_175.0,
            now=datetime(2026, 7, 10, 11, 21, 0),
        )
        df = ledger.load_ledger()
        # 거래비용 엔진 도입 이전 스키마를 흉내내기 위해 gross_pnl/net_pnl을 직접 NaN으로
        # 되돌린다(당시엔 이 컬럼 자체가 없었고, 이후 스키마 마이그레이션이 빈 값으로 채웠다).
        df.loc[df["action"] == "SELL", ["gross_pnl", "net_pnl"]] = pd.NA
        df.to_csv(_isolate_ledger_path, index=False, encoding="utf-8-sig")

        stats = ledger.calculate_daily_net_pnl_from_ledger("20260710")
        assert stats["gross_realized_pnl"] == pytest.approx(-8_175.0)
        assert stats["net_realized_pnl"] == pytest.approx(-8_175.0)

    def test_backfill_does_not_duplicate_on_second_run(self):
        self._seed_pre_cost_engine_trade(
            "0197X0", 10_680.0, 10_720.0, 468, datetime(2026, 7, 13, 9, 54, 6), datetime(2026, 7, 13, 10, 14, 43),
        )
        first = ledger.backfill_trading_costs_into_ledger("20260713")
        second = ledger.backfill_trading_costs_into_ledger("20260713")
        assert first["updated_rows"] == 1
        assert second["updated_rows"] == 0


# =============================================================================
# 2026-07-16 — 모든 확정 체결 경로가 거쳐야 하는 단일 기록 지점(record_confirmed_fill)
# 과 KIS 실보유수량 vs 원장 순수량 재조정(reconcile_symbol_with_kis) 회귀 테스트.
# 증상: KIS에 0193T0 129주 실제 보유가 확인되는데 원장 매수/매도/총체결이 모두 0건.
# =============================================================================

class TestRecordConfirmedFill:
    def test_new_buy_immediately_increments_ledger(self):
        outcome = ledger.record_confirmed_fill(
            action="BUY", symbol="0193T0", executed_qty=129, executed_price=18_500.0,
            mode="real", before_qty=0, after_qty=129, order_id="0000012345",
            now=datetime(2026, 7, 16, 10, 0, 0),
        )
        assert outcome["recorded"] is True
        assert outcome["duplicate"] is False
        df = ledger.load_ledger("20260716")
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "0193T0"
        assert int(df.iloc[0]["executed_qty"]) == 129

    def test_same_order_id_is_not_recorded_twice(self):
        kwargs = dict(
            action="BUY", symbol="0193T0", executed_qty=129, executed_price=18_500.0,
            mode="real", before_qty=0, after_qty=129, order_id="0000012345",
            now=datetime(2026, 7, 16, 10, 0, 0),
        )
        first = ledger.record_confirmed_fill(**kwargs)
        second = ledger.record_confirmed_fill(**kwargs)
        assert first["recorded"] is True
        assert second["recorded"] is False and second["duplicate"] is True
        assert len(ledger.load_ledger("20260716")) == 1

    def test_no_order_id_dedups_on_timestamp_qty_price_delta(self):
        kwargs = dict(
            action="BUY", symbol="0193T0", executed_qty=129, executed_price=18_500.0,
            mode="mock", before_qty=0, after_qty=129, order_id="",
            now=datetime(2026, 7, 16, 10, 0, 0),
        )
        first = ledger.record_confirmed_fill(**kwargs)
        second = ledger.record_confirmed_fill(**kwargs)
        assert first["recorded"] is True
        assert second["duplicate"] is True
        assert len(ledger.load_ledger("20260716")) == 1

    def test_partial_sell_and_switching_each_recorded(self):
        ledger.record_confirmed_fill(
            action="BUY", symbol="0193T0", executed_qty=129, executed_price=18_500.0,
            mode="real", before_qty=0, after_qty=129, order_id="ORD-1",
            now=datetime(2026, 7, 16, 10, 0, 0),
        )
        ledger.record_confirmed_fill(
            action="SELL", symbol="0193T0", executed_qty=64, executed_price=18_700.0,
            mode="real", before_qty=129, after_qty=65, order_id="ORD-2",
            signal_source=ledger.SIGNAL_SOURCE_DYNAMIC_EXIT,
            now=datetime(2026, 7, 16, 10, 5, 0),
        )
        ledger.record_confirmed_fill(
            action="SELL", symbol="0193T0", executed_qty=65, executed_price=18_650.0,
            mode="real", before_qty=65, after_qty=0, order_id="ORD-3",
            now=datetime(2026, 7, 16, 10, 6, 0),
        )
        ledger.record_confirmed_fill(
            action="BUY", symbol="0197X0", executed_qty=1000, executed_price=9_000.0,
            mode="real", before_qty=0, after_qty=1000, order_id="ORD-4",
            now=datetime(2026, 7, 16, 10, 6, 30),
        )
        df = ledger.load_ledger("20260716")
        assert len(df) == 4
        assert list(df["action"]) == ["BUY", "SELL", "SELL", "BUY"]

    def test_ledger_write_failure_reports_error_without_raising(self, monkeypatch):
        def _raise_open(self, *a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "open", _raise_open, raising=True)
        outcome = ledger.record_confirmed_fill(
            action="BUY", symbol="0193T0", executed_qty=129, executed_price=18_500.0,
            mode="real", before_qty=0, after_qty=129, order_id="ORD-FAIL",
        )
        assert outcome["recorded"] is False
        assert outcome["duplicate"] is False
        assert outcome["error"]


class TestLedgerNetQuantityReconciliation:
    def test_ledger_net_quantity_matches_buy_minus_sell(self):
        ledger.record_confirmed_fill(
            action="BUY", symbol="0193T0", executed_qty=129, executed_price=18_500.0,
            mode="real", before_qty=0, after_qty=129, order_id="ORD-1",
            now=datetime(2026, 7, 16, 10, 0, 0),
        )
        ledger.record_confirmed_fill(
            action="SELL", symbol="0193T0", executed_qty=29, executed_price=18_700.0,
            mode="real", before_qty=129, after_qty=100, order_id="ORD-2",
            now=datetime(2026, 7, 16, 10, 5, 0),
        )
        assert ledger.compute_ledger_net_quantity("0193T0", "real", "20260716") == 100

    def test_reconcile_backfills_missing_buy_via_kis_today_fills(self):
        """KIS는 129주 실보유(2분 전 체결)를 보고하지만 원장은 0건 — 당일체결조회로
        누락된 매수를 복구한다."""
        class _Broker:
            def get_today_fills(self, symbol=""):
                return {
                    "ok": True, "error": None,
                    "fills": [{
                        "symbol": "0193T0", "side": "BUY", "order_id": "KIS-ORD-1",
                        "quantity": 129, "price": 18_500.0, "timestamp": "20260716095800",
                    }],
                }

        result = ledger.reconcile_symbol_with_kis(
            "0193T0", "real", broker_qty=129, avg_price=18_500.0, broker=_Broker(),
            now=datetime(2026, 7, 16, 10, 0, 0),
        )
        assert result["mismatch"] is True
        assert len(result["backfilled"]) == 1
        assert ledger.compute_ledger_net_quantity("0193T0", "real", "20260716") == 129
        df = ledger.load_ledger("20260716")
        assert df.iloc[0]["signal_source"] == ledger.SIGNAL_SOURCE_KIS_RECONCILE_BACKFILL
        assert df.iloc[0]["order_id"] == "KIS-ORD-1"

    def test_reconcile_falls_back_to_approximate_fill_when_kis_fills_unavailable(self):
        class _Broker:
            def get_today_fills(self, symbol=""):
                return {"ok": False, "error": "HTTP 500", "fills": []}

        result = ledger.reconcile_symbol_with_kis(
            "0193T0", "real", broker_qty=129, avg_price=18_500.0, broker=_Broker(),
            now=datetime(2026, 7, 16, 10, 0, 0),
        )
        assert result["mismatch"] is True
        assert len(result["backfilled"]) == 1
        assert result["backfilled"][0]["source"] == "approximate_avg_price"
        assert ledger.compute_ledger_net_quantity("0193T0", "real", "20260716") == 129

    def test_reconcile_no_mismatch_when_ledger_already_matches_kis(self):
        ledger.record_confirmed_fill(
            action="BUY", symbol="0193T0", executed_qty=129, executed_price=18_500.0,
            mode="real", before_qty=0, after_qty=129, order_id="ORD-1",
            now=datetime(2026, 7, 16, 10, 0, 0),
        )
        result = ledger.reconcile_symbol_with_kis(
            "0193T0", "real", broker_qty=129, avg_price=18_500.0, broker=None,
            now=datetime(2026, 7, 16, 10, 5, 0),
        )
        assert result["mismatch"] is False
        assert result["backfilled"] == []

    def test_reconcile_broker_flat_ledger_positive_does_not_create_fake_sell(self):
        ledger.record_confirmed_fill(
            action="BUY", symbol="0193T0", executed_qty=129, executed_price=18_500.0,
            mode="real", before_qty=0, after_qty=129, order_id="ORD-1",
            now=datetime(2026, 7, 16, 10, 0, 0),
        )

        class _Broker:
            def get_today_fills(self, symbol=""):
                return {"ok": False, "error": "fills unavailable", "fills": []}

        result = ledger.reconcile_symbol_with_kis(
            "0193T0", "real", broker_qty=0, avg_price=None, broker=_Broker(),
            now=datetime(2026, 7, 16, 10, 5, 0),
        )

        assert result["mismatch"] is True
        assert result["mismatch_code"] == "LEDGER_BROKER_MISMATCH"
        assert result["backfilled"] == []
        assert ledger.compute_ledger_net_quantity("0193T0", "real", "20260716") == 129

    def test_reconcile_does_not_duplicate_backfill_across_cycles(self):
        """한 사이클에서 backfill된 뒤, 다음 사이클에서 KIS 수량이 그대로면 다시
        backfill하지 않는다(중복 backfill 방지)."""
        class _Broker:
            def get_today_fills(self, symbol=""):
                return {"ok": False, "error": "unavailable", "fills": []}

        first = ledger.reconcile_symbol_with_kis(
            "0193T0", "real", broker_qty=129, avg_price=18_500.0, broker=_Broker(),
            now=datetime(2026, 7, 16, 10, 0, 0),
        )
        second = ledger.reconcile_symbol_with_kis(
            "0193T0", "real", broker_qty=129, avg_price=18_500.0, broker=_Broker(),
            now=datetime(2026, 7, 16, 10, 3, 0),
        )
        assert len(first["backfilled"]) == 1
        assert second["mismatch"] is False
        assert second["backfilled"] == []
        assert len(ledger.load_ledger("20260716")) == 1


class TestGetLedgerPath:
    def test_get_ledger_path_matches_module_constant(self, _isolate_ledger_path):
        assert ledger.get_ledger_path() == _isolate_ledger_path
