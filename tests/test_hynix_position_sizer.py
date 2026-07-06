"""
test_hynix_position_sizer.py — 포지션 사이징 모듈 테스트.
"""

from __future__ import annotations

import pytest

from app.trading.hynix_position_sizer import PositionSizingContext, calculate_position_size


def _ctx(**overrides) -> PositionSizingContext:
    base = dict(
        total_equity=100_000_000.0,
        cash=50_000_000.0,
        current_position_value=0.0,
        current_price=170_000.0,
        recent_high=200_000.0,
        recent_low=150_000.0,
        short_term_score=65.0,
    )
    base.update(overrides)
    return PositionSizingContext(**base)


class TestBuyCascade:
    def test_drawdown_15pct_buys_10pct_of_cash(self):
        ctx = _ctx(current_price=170_000.0, short_term_score=60.0)  # -15%
        result = calculate_position_size(ctx)
        assert result["action"] == "BUY"
        assert result["buy_cash_amount"] == pytest.approx(ctx.cash * 0.10, rel=0.01)

    def test_drawdown_20pct_buys_20pct_of_cash(self):
        ctx = _ctx(current_price=160_000.0, short_term_score=60.0)  # -20%
        result = calculate_position_size(ctx)
        assert result["action"] == "BUY"
        assert result["buy_cash_amount"] == pytest.approx(ctx.cash * 0.20, rel=0.01)

    def test_drawdown_25pct_buys_25pct_of_cash(self):
        ctx = _ctx(current_price=150_000.0, short_term_score=55.0)  # -25%
        result = calculate_position_size(ctx)
        assert result["action"] == "BUY"
        assert result["buy_cash_amount"] == pytest.approx(ctx.cash * 0.25, rel=0.01)

    def test_drawdown_30pct_buys_30pct_of_cash(self):
        ctx = _ctx(current_price=140_000.0, short_term_score=55.0)  # -30%
        result = calculate_position_size(ctx)
        assert result["action"] == "BUY"
        assert result["buy_cash_amount"] == pytest.approx(ctx.cash * 0.30, rel=0.01)

    def test_no_drawdown_no_buy(self):
        ctx = _ctx(current_price=195_000.0, short_term_score=90.0)
        result = calculate_position_size(ctx)
        assert result["action"] == "HOLD"


class TestBuyPenaltiesAndGuards(object):
    def test_mu_crash_halves_buy(self):
        ctx = _ctx(current_price=160_000.0, short_term_score=60.0, mu_return_pct=-6.0)
        result = calculate_position_size(ctx)
        assert result["action"] == "BUY"
        assert result["buy_cash_amount"] == pytest.approx(ctx.cash * 0.20 * 0.5, rel=0.01)

    def test_sox_crash_halves_buy(self):
        ctx = _ctx(current_price=160_000.0, short_term_score=60.0, sox_return_pct=-4.0)
        result = calculate_position_size(ctx)
        assert result["buy_cash_amount"] == pytest.approx(ctx.cash * 0.20 * 0.5, rel=0.01)

    def test_today_spike_blocks_buy(self):
        ctx = _ctx(current_price=160_000.0, short_term_score=60.0, hynix_today_return_pct=6.0)
        result = calculate_position_size(ctx)
        assert result["action"] == "HOLD"

    def test_low_cash_ratio_blocks_buy(self):
        ctx = _ctx(current_price=140_000.0, short_term_score=55.0, cash=10_000_000.0)
        result = calculate_position_size(ctx)
        assert result["action"] == "HOLD"

    def test_daily_loss_limit_blocks_buy(self):
        ctx = _ctx(current_price=140_000.0, short_term_score=55.0, daily_pnl_pct=-4.0)
        result = calculate_position_size(ctx)
        assert result["action"] == "HOLD"

    def test_symbol_cap_reduces_buy(self):
        ctx = _ctx(
            current_price=140_000.0, short_term_score=55.0,
            current_position_value=69_000_000.0,
        )
        result = calculate_position_size(ctx)
        if result["action"] == "BUY":
            assert result["buy_cash_amount"] <= ctx.total_equity * 0.70 - ctx.current_position_value + 1

    def test_data_invalid_blocks_everything(self):
        ctx = _ctx(current_price=140_000.0, short_term_score=90.0, data_valid=False)
        result = calculate_position_size(ctx)
        assert result["action"] == "HOLD"


class TestSellTiers:
    def test_profit_5pct_sells_20pct(self):
        ctx = _ctx(current_price=105_000.0, avg_buy_price=100_000.0, current_position_value=10_000_000.0)
        result = calculate_position_size(ctx)
        assert result["action"] == "SELL"
        assert result["sell_quantity_ratio"] == pytest.approx(0.20, abs=0.01)

    def test_profit_10pct_sells_45pct(self):
        ctx = _ctx(current_price=110_000.0, avg_buy_price=100_000.0, current_position_value=10_000_000.0)
        result = calculate_position_size(ctx)
        assert result["sell_quantity_ratio"] == pytest.approx(0.45, abs=0.01)

    def test_profit_15pct_sells_75pct(self):
        ctx = _ctx(current_price=115_000.0, avg_buy_price=100_000.0, current_position_value=10_000_000.0)
        result = calculate_position_size(ctx)
        assert result["sell_quantity_ratio"] == pytest.approx(0.75, abs=0.01)

    def test_profit_20pct_sells_to_restore_cash_ratio(self):
        ctx = _ctx(
            current_price=120_000.0, avg_buy_price=100_000.0,
            current_position_value=50_000_000.0, cash=10_000_000.0, total_equity=100_000_000.0,
        )
        result = calculate_position_size(ctx)
        assert result["action"] == "SELL"
        assert result["sell_quantity_ratio"] > 0

    def test_target_1_proximity_triggers_partial_sell(self):
        ctx = _ctx(
            current_price=100_000.0, avg_buy_price=100_000.0, current_position_value=10_000_000.0,
            target_1=100_500.0,
        )
        result = calculate_position_size(ctx)
        assert result["action"] == "SELL"

    def test_low_target2_probability_triggers_partial_sell(self):
        ctx = _ctx(
            current_price=100_000.0, avg_buy_price=100_000.0, current_position_value=10_000_000.0,
            target_2_probability=20.0,
        )
        result = calculate_position_size(ctx)
        assert result["action"] == "SELL"
