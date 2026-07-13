"""test_trading_cost_engine.py — TradeCostEngine(수수료/거래세/슬리피지) 테스트."""

from __future__ import annotations

import pytest

from app.trading.trading_cost_engine import TradeCostEngine, is_etf_or_etn

_CFG = {
    "domestic_buy_fee_rate": 0.0001, "domestic_sell_fee_rate": 0.0001,
    "etf_buy_fee_rate": 0.00005, "etf_sell_fee_rate": 0.00005,
    "transaction_tax_rate": 0.002, "etf_transaction_tax_rate": 0.0,
    "clearing_fee_rate": 0.0, "slippage_rate_default": 0.0002,
    "slippage_rate_market_order": 0.0004, "slippage_rate_limit_order": 0.0001,
    "min_commission_krw": 0.0,
}


class TestSymbolClassification:
    def test_inverse_etn_is_etf(self):
        assert is_etf_or_etn("0197X0") is True

    def test_hynix_is_not_etf(self):
        assert is_etf_or_etn("000660") is False


class TestTradeCost:
    def test_stock_buy_no_tax(self):
        engine = TradeCostEngine(_CFG)
        cost = engine.compute_trade_cost("000660", "BUY", 100_000.0, 10)
        assert cost["fee"] == pytest.approx(100.0)
        assert cost["tax"] == 0.0

    def test_stock_sell_has_tax(self):
        engine = TradeCostEngine(_CFG)
        cost = engine.compute_trade_cost("000660", "SELL", 100_000.0, 10)
        assert cost["fee"] == pytest.approx(100.0)
        assert cost["tax"] == pytest.approx(2000.0)

    def test_etf_sell_has_no_tax(self):
        engine = TradeCostEngine(_CFG)
        cost = engine.compute_trade_cost("0197X0", "SELL", 10_000.0, 100)
        assert cost["tax"] == 0.0
        assert cost["fee"] == pytest.approx(50.0)

    def test_min_commission_applied(self):
        cfg = dict(_CFG)
        cfg["min_commission_krw"] = 500.0
        engine = TradeCostEngine(cfg)
        cost = engine.compute_trade_cost("000660", "BUY", 10_000.0, 1)
        assert cost["fee"] == pytest.approx(500.0)


class TestSlippage:
    def test_buy_slippage_raises_price(self):
        engine = TradeCostEngine(_CFG)
        adjusted = engine.estimate_slippage_adjusted_price("000660", "BUY", 100_000.0, "market")
        assert adjusted > 100_000.0

    def test_sell_slippage_lowers_price(self):
        engine = TradeCostEngine(_CFG)
        adjusted = engine.estimate_slippage_adjusted_price("000660", "SELL", 100_000.0, "market")
        assert adjusted < 100_000.0

    def test_market_order_slippage_larger_than_limit(self):
        engine = TradeCostEngine(_CFG)
        market_adj = engine.estimate_slippage_adjusted_price("000660", "BUY", 100_000.0, "market")
        limit_adj = engine.estimate_slippage_adjusted_price("000660", "BUY", 100_000.0, "limit")
        assert (market_adj - 100_000.0) > (limit_adj - 100_000.0)


class TestNetPnl:
    def test_net_pnl_less_than_gross_pnl_on_profit(self):
        engine = TradeCostEngine(_CFG)
        result = engine.compute_net_pnl("000660", entry_price=100_000.0, exit_price=103_000.0, quantity=10)
        gross = (103_000.0 - 100_000.0) * 10
        assert result["gross_pnl"] == pytest.approx(gross)
        assert result["net_pnl"] < result["gross_pnl"]

    def test_net_pnl_worse_than_gross_pnl_on_loss(self):
        engine = TradeCostEngine(_CFG)
        result = engine.compute_net_pnl("000660", entry_price=100_000.0, exit_price=97_000.0, quantity=10)
        assert result["net_pnl"] < result["gross_pnl"]

    def test_etf_net_pnl_has_no_tax_component(self):
        engine = TradeCostEngine(_CFG)
        result = engine.compute_net_pnl("0197X0", entry_price=10_000.0, exit_price=10_500.0, quantity=100)
        assert result["transaction_tax"] == 0.0

    def test_stock_net_pnl_includes_tax(self):
        engine = TradeCostEngine(_CFG)
        result = engine.compute_net_pnl("000660", entry_price=100_000.0, exit_price=103_000.0, quantity=10)
        assert result["transaction_tax"] > 0.0

    def test_round_trip_cost_pct_positive(self):
        engine = TradeCostEngine(_CFG)
        pct = engine.compute_round_trip_cost_pct("000660", "limit")
        assert pct > 0.0


class TestUnrealizedNetPnl:
    def test_unrealized_net_pnl_less_than_gross(self):
        engine = TradeCostEngine(_CFG)
        result = engine.compute_unrealized_net_pnl("000660", entry_price=100_000.0, current_price=102_000.0, quantity=10)
        assert result["net_unrealized_pnl"] < result["gross_unrealized_pnl"]

    def test_unrealized_pnl_deducts_estimated_exit_costs(self):
        engine = TradeCostEngine(_CFG)
        result = engine.compute_unrealized_net_pnl("000660", entry_price=100_000.0, current_price=100_000.0, quantity=10)
        # 가격 변동이 0이어도 매도 시 수수료/세금/슬리피지가 발생하므로 순미실현손익은 음수.
        assert result["gross_unrealized_pnl"] == 0.0
        assert result["net_unrealized_pnl"] < 0.0


class TestConfigDefaults:
    def test_falls_back_to_defaults_without_config(self, monkeypatch):
        import app.config as config_module

        class _FakeCfg:
            trading_cost = {}

        monkeypatch.setattr(config_module, "get_config", lambda: _FakeCfg())
        engine = TradeCostEngine()
        cost = engine.compute_trade_cost("000660", "BUY", 100_000.0, 10)
        assert cost["fee"] > 0
