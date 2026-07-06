"""
test_swing_price_logic.py — 스윙 플래그 가격 구간 로직 검증.

목표가 > 손절가 (stop_loss < target) 항상 보장되어야 합니다.
"""

from __future__ import annotations

import pytest
from app.models.hynix_swing_flag import (
    evaluate_swing_flag,
    _compute_price_zones,
    _score_to_flag,
    STRONG_BUY, BUY, WAIT_BUY, NEUTRAL, TAKE_PROFIT, SELL, STRONG_SELL,
)


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _micro(pm_ret: float = 2.0) -> dict:
    return {
        "micron_premarket_return":        pm_ret,
        "micron_premarket_30m_momentum":  pm_ret * 0.5,
        "micron_premarket_60m_momentum":  pm_ret * 0.3,
        "micron_premarket_vwap":          None,
        "micron_premarket_volume_change": None,
        "micron_premarket_open_to_now":   None,
        "micron_premarket_high_to_now":   None,
        "micron_premarket_low_to_now":    None,
        "micron_regular_return":          None,
        "micron_aftermarket_return":      None,
        "micron_session_strength_score":  70.0,
    }


def _ti_full() -> dict:
    return {
        "rsi_14": 45.0,
        "macd": 100.0,
        "macd_signal_cross": 0,
        "ma5_position_pct": 1.0,
        "ma20_position_pct": 2.0,
        "ma60_position_pct": 3.0,
        "from_20d_high_pct": -5.0,
        "from_20d_low_pct": 8.0,
        "bollinger_pct": 45.0,
        "prev_candle_type": 1,
        "return_3d_pct": 1.5,
        "return_5d_pct": 2.0,
        "return_10d_pct": 3.0,
        "volume_change_pct": 10.0,
    }


# ── _compute_price_zones 직접 테스트 ─────────────────────────────────────────

class TestComputePriceZones:
    """stop_loss < target 항상 보장 검증."""

    def test_long_direction_target_gt_stop(self):
        prices = _compute_price_zones(
            hynix_prev_close=180_000,
            swing_score=75.0,
            composite=0.5,    # 매수 방향
            ti=_ti_full(),
            prediction=None,
        )
        assert prices["target_price"] is not None
        assert prices["stop_loss_price"] is not None
        assert prices["stop_loss_price"] < prices["target_price"], (
            f"LONG: stop({prices['stop_loss_price']}) >= target({prices['target_price']})"
        )

    def test_sell_direction_target_gt_stop(self):
        prices = _compute_price_zones(
            hynix_prev_close=180_000,
            swing_score=25.0,
            composite=-0.5,   # 매도 방향
            ti=_ti_full(),
            prediction=None,
        )
        assert prices["target_price"] is not None
        assert prices["stop_loss_price"] is not None
        assert prices["stop_loss_price"] < prices["target_price"], (
            f"SELL: stop({prices['stop_loss_price']}) >= target({prices['target_price']})"
        )

    def test_no_base_price_returns_none(self):
        prices = _compute_price_zones(
            hynix_prev_close=None,
            swing_score=50.0,
            composite=0.0,
            ti={},
            prediction=None,
        )
        assert prices["target_price"] is None
        assert prices["stop_loss_price"] is None

    def test_uses_prediction_when_no_prev_close(self):
        prices = _compute_price_zones(
            hynix_prev_close=None,
            swing_score=70.0,
            composite=0.4,
            ti=_ti_full(),
            prediction={"today_close_expected": 180_000},
        )
        assert prices["target_price"] is not None

    def test_sell_stop_below_base(self):
        base = 180_000
        prices = _compute_price_zones(
            hynix_prev_close=base,
            swing_score=20.0,
            composite=-0.8,
            ti=_ti_full(),
            prediction=None,
        )
        assert prices["stop_loss_price"] < base, (
            f"SELL stop({prices['stop_loss_price']}) should be below base({base})"
        )

    def test_long_stop_below_base(self):
        base = 180_000
        prices = _compute_price_zones(
            hynix_prev_close=base,
            swing_score=80.0,
            composite=0.8,
            ti=_ti_full(),
            prediction=None,
        )
        assert prices["stop_loss_price"] < base, (
            f"LONG stop({prices['stop_loss_price']}) should be below base({base})"
        )

    def test_long_target_above_base(self):
        base = 180_000
        prices = _compute_price_zones(
            hynix_prev_close=base,
            swing_score=80.0,
            composite=0.8,
            ti=_ti_full(),
            prediction=None,
        )
        assert prices["target_price"] > base, (
            f"LONG target({prices['target_price']}) should be above base({base})"
        )


# ── evaluate_swing_flag 통합 검증 ────────────────────────────────────────────

class TestEvaluateSwingFlagPriceLogic:
    """evaluate_swing_flag 전체 플로우에서 가격 논리 검증."""

    def _run(self, pm_ret: float, sox: float, nvda: float) -> dict:
        return evaluate_swing_flag(
            micron_features=_micro(pm_ret=pm_ret),
            kospilab_expected_return_pct=pm_ret * 0.8,
            tech_indicators=_ti_full(),
            sox_return_pct=sox,
            nvda_return_pct=nvda,
            qqq_return_pct=sox * 0.5,
            usd_krw_change_pct=-0.1,
            hynix_prev_close=180_000,
            prediction=None,
        )

    def test_strong_buy_price_logic(self):
        result = self._run(pm_ret=3.0, sox=2.0, nvda=2.5)
        t = result["target_price"]
        s = result["stop_loss_price"]
        if t and s:
            assert s < t, f"STRONG_BUY: stop({s}) >= target({t})"

    def test_sell_price_logic(self):
        result = self._run(pm_ret=-3.0, sox=-2.0, nvda=-2.5)
        t = result["target_price"]
        s = result["stop_loss_price"]
        if t and s:
            assert s < t, f"SELL: stop({s}) >= target({t}) — 사용자 보고 버그"

    def test_neutral_has_score(self):
        result = self._run(pm_ret=0.0, sox=0.0, nvda=0.0)
        assert "swing_score" in result
        assert 0 <= result["swing_score"] <= 100

    def test_all_flags_have_required_keys(self):
        result = self._run(pm_ret=2.0, sox=1.0, nvda=1.5)
        required = [
            "swing_score", "swing_flag", "flag_label", "flag_color",
            "bottom_probability", "top_probability",
            "buy_zone_low", "buy_zone_high",
            "sell_zone_low", "sell_zone_high",
            "target_price", "stop_loss_price",
            "confidence_score", "action_text",
        ]
        for k in required:
            assert k in result, f"키 없음: {k}"

    def test_known_bug_reproduced_then_fixed(self):
        """사용자 보고: 목표가 314,000원, 손절가 338,000원 버그 픽스 확인."""
        result = evaluate_swing_flag(
            micron_features=_micro(pm_ret=-2.5),
            kospilab_expected_return_pct=-2.0,
            tech_indicators={k: None for k in _ti_full()},
            sox_return_pct=-1.5,
            nvda_return_pct=-2.0,
            qqq_return_pct=-1.0,
            usd_krw_change_pct=0.3,
            hynix_prev_close=330_000,
            prediction=None,
        )
        t = result.get("target_price")
        s = result.get("stop_loss_price")
        if t is not None and s is not None:
            assert s < t, (
                f"버그 재현: stop({s:,}원) >= target({t:,}원). "
                "손절가가 목표가보다 높으면 안 됩니다."
            )

    @pytest.mark.parametrize("composite", [-1.0, -0.5, 0.0, 0.5, 1.0])
    def test_price_logic_across_composite_range(self, composite):
        from app.models.hynix_swing_flag import _compute_price_zones
        prices = _compute_price_zones(
            hynix_prev_close=180_000,
            swing_score=50 + composite * 50,
            composite=composite,
            ti=_ti_full(),
            prediction=None,
        )
        t = prices.get("target_price")
        s = prices.get("stop_loss_price")
        if t is not None and s is not None:
            assert s < t, (
                f"composite={composite}: stop({s:,}) >= target({t:,})"
            )
