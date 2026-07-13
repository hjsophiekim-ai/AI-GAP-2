"""test_hynix_strategy_shadow_tracker.py — 전략별 가상 포트폴리오/비교 통계 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import app.trading.hynix_strategy_shadow_tracker as sst


@pytest.fixture(autouse=True)
def _isolate_shadow_ledger_path(tmp_path, monkeypatch):
    monkeypatch.setattr(sst, "_SHADOW_LEDGER_PATH", tmp_path / "hynix_strategy_shadow_ledger.csv")


class TestVirtualPortfolio:
    def test_enters_position_on_directional_signal(self):
        vp = sst.default_virtual_portfolio(budget=10_000_000.0)
        now = datetime(2026, 7, 14, 9, 5)
        vp = sst.update_virtual_strategy(vp, "ACTIVE_ONLY", now, action="HYNIX", price=100_000.0, target_pct=30.0)
        assert vp["symbol"] == "000660"
        assert vp["quantity"] > 0
        assert vp["cash"] < 10_000_000.0

    def test_hold_does_not_open_position(self):
        vp = sst.default_virtual_portfolio()
        now = datetime(2026, 7, 14, 9, 5)
        vp = sst.update_virtual_strategy(vp, "ACTIVE_ONLY", now, action="HOLD", price=100_000.0, target_pct=0.0)
        assert vp["symbol"] is None

    def test_direction_flip_closes_and_reopens(self):
        vp = sst.default_virtual_portfolio()
        now = datetime(2026, 7, 14, 9, 5)
        vp = sst.update_virtual_strategy(vp, "ACTIVE_ONLY", now, action="HYNIX", price=100_000.0, target_pct=30.0)
        assert vp["symbol"] == "000660"
        vp = sst.update_virtual_strategy(vp, "ACTIVE_ONLY", now + timedelta(minutes=5), action="INVERSE", price=9_000.0, target_pct=30.0)
        assert vp["symbol"] == "0197X0"

        df = sst.load_shadow_ledger()
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "000660"

    def test_realized_pnl_recorded_on_close(self):
        vp = sst.default_virtual_portfolio(budget=10_000_000.0)
        now = datetime(2026, 7, 14, 9, 5)
        vp = sst.update_virtual_strategy(vp, "ACTIVE_ONLY", now, action="HYNIX", price=100_000.0, target_pct=100.0)
        qty = vp["quantity"]
        vp = sst.update_virtual_strategy(vp, "ACTIVE_ONLY", now + timedelta(minutes=10), action="HOLD", price=102_000.0, target_pct=0.0)
        assert vp["symbol"] is None

        df = sst.load_shadow_ledger()
        assert len(df) == 1
        assert df.iloc[0]["pnl_krw"] == pytest.approx(qty * 2000.0)
        assert df.iloc[0]["pnl_pct"] == pytest.approx(2.0)

    def test_new_day_resets_portfolio(self):
        vp = sst.default_virtual_portfolio(budget=10_000_000.0)
        day1 = datetime(2026, 7, 14, 9, 5)
        vp = sst.update_virtual_strategy(vp, "ACTIVE_ONLY", day1, action="HYNIX", price=100_000.0, target_pct=30.0)
        assert vp["symbol"] == "000660"

        day2 = datetime(2026, 7, 15, 9, 5)
        vp2 = sst.update_virtual_strategy(vp, "ACTIVE_ONLY", day2, action="HOLD", price=100_000.0, target_pct=0.0)
        assert vp2["symbol"] is None
        assert vp2["cash"] == pytest.approx(10_000_000.0)

    def test_force_close_all_liquidates(self):
        vp = sst.default_virtual_portfolio()
        now = datetime(2026, 7, 14, 9, 5)
        vp = sst.update_virtual_strategy(vp, "ACTIVE_ONLY", now, action="HYNIX", price=100_000.0, target_pct=30.0)
        vp = sst.force_close_all(vp, price=101_000.0, now=now + timedelta(hours=6), strategy_name="ACTIVE_ONLY")
        assert vp["symbol"] is None
        df = sst.load_shadow_ledger()
        assert len(df) == 1


class TestComparisonStats:
    def _make_trade(self, strategy, symbol, entry, exit_, pnl_pct, now):
        vp = sst.default_virtual_portfolio(budget=10_000_000.0)
        vp = sst.update_virtual_strategy(vp, strategy, now, action=("HYNIX" if symbol == "000660" else "INVERSE"), price=entry, target_pct=100.0)
        sst.update_virtual_strategy(vp, strategy, now + timedelta(minutes=5), action="HOLD", price=exit_, target_pct=0.0)

    def test_comparison_includes_all_four_strategies(self):
        now = datetime(2026, 7, 14, 9, 5)
        self._make_trade("ACTIVE_ONLY", "000660", 100_000.0, 102_000.0, 2.0, now)
        stats = sst.compute_strategy_comparison_stats()
        assert set(stats.keys()) == set(sst.ALL_STRATEGIES)
        assert stats["ACTIVE_ONLY"]["trade_count"] == 1
        assert stats["CYCLE_ONLY"]["trade_count"] == 0

    def test_adaptive_fusion_prefers_real_stats_when_provided(self):
        now = datetime(2026, 7, 14, 9, 5)
        self._make_trade("ADAPTIVE_FUSION", "000660", 100_000.0, 90_000.0, -10.0, now)
        real_stats = {"trade_count": 5, "profit_factor": 2.0, "total_return_pct": 3.0}
        stats = sst.compute_strategy_comparison_stats(adaptive_fusion_real_stats=real_stats)
        assert stats["ADAPTIVE_FUSION"] == real_stats

    def test_fallback_recommended_when_fusion_underperforms(self):
        now = datetime(2026, 7, 14, 9, 5)
        self._make_trade("ACTIVE_ONLY", "000660", 100_000.0, 103_000.0, 3.0, now)
        real_stats = {"trade_count": 5, "profit_factor": 0.5, "total_return_pct": -1.0}
        result = sst.compare_adaptive_fusion_vs_active_only(adaptive_fusion_real_stats=real_stats)
        assert result["should_fallback"] is True

    def test_no_fallback_when_fusion_outperforms(self):
        now = datetime(2026, 7, 14, 9, 5)
        self._make_trade("ACTIVE_ONLY", "000660", 100_000.0, 101_000.0, 1.0, now)
        real_stats = {"trade_count": 5, "profit_factor": 3.0, "total_return_pct": 5.0}
        result = sst.compare_adaptive_fusion_vs_active_only(adaptive_fusion_real_stats=real_stats)
        assert result["should_fallback"] is False
