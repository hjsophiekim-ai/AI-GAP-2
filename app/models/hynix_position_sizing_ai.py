"""hynix_position_sizing_ai.py — PositionSizingAI (명세 6절).

단순 확률만으로 비중을 정하지 않고, confidence/expected_move/ATR/최근손익/연속손절/
cycle phase/order flow/보유시간을 함께 반영해 최종 추천 비중과 expected_value를
계산한다. expected_value <= 0이면 진입하지 않는다.
"""

from __future__ import annotations

from typing import Optional

DEFAULT_FEE_RATE_PCT = 0.015  # 편도 수수료(%) 근사
DEFAULT_TAX_RATE_PCT = 0.18  # 매도세(%) 근사(국내 ETF/주식 매도시)
DEFAULT_SLIPPAGE_PCT = 0.05


def calculate_expected_value(
    win_probability: float, expected_profit_pct: float, expected_loss_pct: float,
    fee_rate_pct: float = DEFAULT_FEE_RATE_PCT, tax_rate_pct: float = DEFAULT_TAX_RATE_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> float:
    """expected_value(%) = 승률*기대이익 - 패률*기대손실 - 수수료 - 세금 - 슬리피지."""
    win_p = max(0.0, min(1.0, win_probability / 100.0))
    loss_p = 1.0 - win_p
    costs = fee_rate_pct * 2 + tax_rate_pct + slippage_pct  # 매수+매도 편도 수수료 2회 + 매도세 1회 + 슬리피지
    return round(win_p * expected_profit_pct - loss_p * abs(expected_loss_pct) - costs, 4)


class PositionSizingAI:
    """명세 6절 — recommended_position_pct/scale_in_pct/scale_out_pct/capital_at_risk/expected_value 산출."""

    def calculate_expected_value(self, *args, **kwargs) -> float:
        return calculate_expected_value(*args, **kwargs)

    def recommend_position_size(
        self,
        buy_or_inverse_probability: float, confidence: float, expected_move_pct: float,
        atr_pct: Optional[float] = None, recent_pnl_pct: Optional[float] = None,
        consecutive_stop_losses: int = 0, cycle_phase: Optional[str] = None,
        order_flow_confidence: Optional[float] = None, holding_minutes: float = 0.0,
        base_position_pct: float = 0.0, mode: Optional[str] = None,
    ) -> dict:
        """확률 기반 base_position_pct(호출부가 hynix_trading_mode의 사다리로 계산해
        넘김)를 여러 리스크 신호로 보정하고, expected_value가 0 이하면 0으로 낮춘다."""
        scale = 1.0
        reasons: list = []

        if atr_pct is not None and atr_pct > 0:
            # ATR이 클수록(변동성 높을수록) 비중을 소폭 줄인다 — 동일 확률이라도 변동성이
            # 크면 같은 명목 비중의 리스크가 커지기 때문.
            if atr_pct >= 3.0:
                scale *= 0.7
                reasons.append(f"ATR {atr_pct:.1f}% 높음 — 비중 30% 축소")
            elif atr_pct >= 2.0:
                scale *= 0.85
                reasons.append(f"ATR {atr_pct:.1f}% 다소 높음 — 비중 15% 축소")

        if consecutive_stop_losses >= 2:
            scale *= 0.5
            reasons.append(f"연속손절 {consecutive_stop_losses}회 — 비중 절반")
        if consecutive_stop_losses >= 3:
            scale = 0.0
            reasons.append("연속손절 3회 이상 — 신규 진입 금지")

        if recent_pnl_pct is not None and recent_pnl_pct < -1.0:
            scale *= 0.7
            reasons.append(f"최근 손익 {recent_pnl_pct:+.2f}% 부진 — 비중 축소")

        if cycle_phase == "NO_TRADE":
            scale = 0.0
            reasons.append("Cycle Phase NO_TRADE — 진입 금지")

        if order_flow_confidence is not None and order_flow_confidence < 30.0:
            scale *= 0.85
            reasons.append("Order Flow 데이터 신뢰도 낮음 — 비중 소폭 축소")

        if holding_minutes and holding_minutes > 180:
            scale *= 0.9
            reasons.append("장시간 보유 — 비중 소폭 축소(과최적화 방지)")

        expected_profit_pct = max(0.1, expected_move_pct)
        expected_loss_pct = max(0.1, expected_move_pct * 0.6)
        ev = calculate_expected_value(buy_or_inverse_probability, expected_profit_pct, expected_loss_pct)

        recommended_pct = round(max(0.0, base_position_pct * scale), 1) if ev > 0 else 0.0
        if ev <= 0:
            reasons.append(f"expected_value {ev:+.3f}% <= 0 — 진입하지 않음")

        capital_at_risk = round(recommended_pct * expected_loss_pct / 100.0, 3)

        return {
            "recommended_position_pct": recommended_pct,
            "scale_in_pct": recommended_pct, "scale_out_pct": 0.0,
            "capital_at_risk": capital_at_risk, "expected_value": ev,
            "risk_scale_applied": round(scale, 3), "reasons": reasons,
        }
