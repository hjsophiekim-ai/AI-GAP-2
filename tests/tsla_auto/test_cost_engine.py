"""Unit tests for app.trading.tsla_auto.cost_engine."""
from __future__ import annotations

from app.trading.tsla_auto.cost_engine import OverseasTradeCostEngine


def test_default_rates_match_tsla_auto_display_assumptions():
    engine = OverseasTradeCostEngine()
    assert engine.fee_rate("BUY") == 0.0025
    assert engine.fee_rate("SELL") == 0.0025
    assert round(engine.fx_effective_rate(), 6) == 0.0005
    assert engine._slippage_rate("limit") == 0.0005
    assert engine._cfg["sec_section31_fee_rate"] > 0.0  # public rate, not silently zeroed
    assert engine._cfg["finra_taf_rate_per_share"] > 0.0


def test_regulatory_fees_only_on_sell():
    engine = OverseasTradeCostEngine()
    buy_cost = engine.compute_trade_cost_usd("BUY", 30.0, 100)
    sell_cost = engine.compute_trade_cost_usd("SELL", 30.0, 100)
    assert buy_cost["sec_fee_usd"] == 0.0
    assert buy_cost["finra_taf_usd"] == 0.0
    assert sell_cost["sec_fee_usd"] > 0.0
    assert sell_cost["finra_taf_usd"] > 0.0


def test_finra_taf_capped():
    engine = OverseasTradeCostEngine(cost_config={"finra_taf_rate_per_share": 1.0, "finra_taf_cap_usd": 5.0})
    cost = engine.compute_trade_cost_usd("SELL", 30.0, 1000)
    assert cost["finra_taf_usd"] == 5.0  # capped, not 1000 * 1.0


def test_net_pnl_usd_gross_minus_costs():
    engine = OverseasTradeCostEngine()
    result = engine.compute_net_pnl_usd(entry_price=30.0, exit_price=32.0, quantity=100)
    assert result["gross_pnl_usd"] == 200.0
    assert result["net_pnl_usd"] < result["gross_pnl_usd"]
    assert result["cost_source"] == "estimated"


def test_actual_cost_overrides_estimate():
    engine = OverseasTradeCostEngine()
    result = engine.compute_net_pnl_usd(entry_price=30.0, exit_price=32.0, quantity=100, actual_cost_usd=1.23)
    assert result["cost_source"] == "actual_kis"
    assert result["total_cost_usd"] == 1.23
    assert result["net_pnl_usd"] == round(200.0 - 1.23, 4)


def test_actual_slippage_uses_requested_vs_executed_price_when_available():
    engine = OverseasTradeCostEngine()
    fallback = engine.compute_slippage_usd(requested_price=30.0, executed_price=30.0, quantity=100)
    actual = engine.compute_slippage_usd(requested_price=30.0, executed_price=30.05, quantity=100)
    assert fallback == 1.5
    assert round(actual, 4) == 5.0
