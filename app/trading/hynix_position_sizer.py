"""
hynix_position_sizer.py — SK하이닉스 예측 기반 매수/매도 비중 계산.

주의: 이 모듈은 "제안"만 계산한다. 실제 주문 실행은
app.services.hynix_auto_trade_service에서 리스크 가드 통과 후 수행한다.
"확정 수익", "무조건 상승" 같은 표현은 사용하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

MIN_CASH_RATIO_FOR_BUY = 0.20
MAX_DAILY_BUY_PCT = 0.20
MAX_SYMBOL_PCT = 0.70
TARGET_CASH_RATIO_AFTER_BIG_PROFIT = 0.35


@dataclass
class PositionSizingContext:
    total_equity: float
    cash: float
    current_position_value: float
    current_price: float
    recent_high: float
    recent_low: float
    short_term_score: float
    avg_buy_price: Optional[float] = None
    mu_return_pct: Optional[float] = None
    sox_return_pct: Optional[float] = None
    hynix_today_return_pct: Optional[float] = None
    target_1: Optional[float] = None
    target_2_probability: Optional[float] = None
    volume_confirmed: bool = True
    upper_wick_near_high: bool = False
    daily_pnl_pct: float = 0.0
    data_valid: bool = True
    today_already_bought_amount: float = 0.0


def calculate_position_size(ctx: PositionSizingContext) -> dict:
    """매수/매도 비중을 계산한다.

    Returns
    -------
    dict: {"action": "BUY"|"SELL"|"HOLD", "buy_cash_amount": float,
           "sell_quantity_ratio": float, "reasons": [...], "warnings": [...]}
    """
    reasons: list[str] = []
    warnings: list[str] = []

    if not ctx.data_valid:
        return _hold("데이터 검증 실패로 매수/매도 제안을 생성하지 않습니다.", reasons, warnings)

    sell_ratio, sell_reasons = _evaluate_sell(ctx)
    if sell_ratio > 0:
        reasons.extend(sell_reasons)
        return {
            "action": "SELL",
            "buy_cash_amount": 0.0,
            "sell_quantity_ratio": round(min(sell_ratio, 1.0), 4),
            "reasons": reasons,
            "warnings": warnings,
        }

    buy_amount, buy_reasons, buy_warnings = _evaluate_buy(ctx)
    reasons.extend(buy_reasons)
    warnings.extend(buy_warnings)
    if buy_amount <= 0:
        return {
            "action": "HOLD",
            "buy_cash_amount": 0.0,
            "sell_quantity_ratio": 0.0,
            "reasons": reasons or ["매수/매도 조건 미충족 — 대기"],
            "warnings": warnings,
        }
    return {
        "action": "BUY",
        "buy_cash_amount": round(buy_amount, 0),
        "sell_quantity_ratio": 0.0,
        "reasons": reasons,
        "warnings": warnings,
    }


def _hold(reason: str, reasons: list[str], warnings: list[str]) -> dict:
    reasons.append(reason)
    return {"action": "HOLD", "buy_cash_amount": 0.0, "sell_quantity_ratio": 0.0, "reasons": reasons, "warnings": warnings}


def _evaluate_sell(ctx: PositionSizingContext) -> tuple[float, list[str]]:
    reasons: list[str] = []
    if ctx.current_position_value <= 0 or ctx.avg_buy_price is None or ctx.avg_buy_price <= 0:
        ratio = 0.0
    else:
        profit_rate = round((ctx.current_price / ctx.avg_buy_price - 1.0) * 100, 6)
        if profit_rate >= 20.0:
            cash_ratio = ctx.cash / ctx.total_equity if ctx.total_equity > 0 else 1.0
            if cash_ratio < TARGET_CASH_RATIO_AFTER_BIG_PROFIT:
                needed_cash = TARGET_CASH_RATIO_AFTER_BIG_PROFIT * ctx.total_equity - ctx.cash
                ratio = max(0.0, min(1.0, needed_cash / ctx.current_position_value))
                reasons.append(f"평가수익률 +{profit_rate:.1f}% — 현금비중 {TARGET_CASH_RATIO_AFTER_BIG_PROFIT*100:.0f}% 회복까지 일부 매도")
            else:
                ratio = 0.0
        elif profit_rate >= 15.0:
            ratio = 0.75
            reasons.append(f"평가수익률 +{profit_rate:.1f}% — 트레이딩 물량 75% 매도 제안(+5/+10/+15% 누적)")
        elif profit_rate >= 10.0:
            ratio = 0.45
            reasons.append(f"평가수익률 +{profit_rate:.1f}% — 트레이딩 물량 45% 매도 제안(+5/+10% 누적)")
        elif profit_rate >= 5.0:
            ratio = 0.20
            reasons.append(f"평가수익률 +{profit_rate:.1f}% — 트레이딩 물량 20% 매도 제안")
        else:
            ratio = 0.0

    extra = 0.0
    if ctx.target_1 is not None and ctx.current_price > 0 and abs(ctx.current_price / ctx.target_1 - 1.0) <= 0.015:
        extra += 0.15
        reasons.append("현재가가 target_1 부근 — 일부 익절 제안")
    if ctx.target_2_probability is not None and ctx.target_2_probability < 40.0:
        extra += 0.15
        reasons.append(f"target_2 도달확률 {ctx.target_2_probability:.0f}% < 40% — 일부 익절 제안")
    if ctx.upper_wick_near_high and not ctx.volume_confirmed:
        extra += 0.15
        reasons.append("전고점 근처 거래량 감소 + 윗꼬리 발생 — 추가 익절 제안")

    if extra > 0 and ratio == 0.0 and ctx.current_position_value > 0:
        ratio = extra
    elif extra > 0:
        ratio = min(1.0, ratio + extra)

    return ratio, reasons


def _evaluate_buy(ctx: PositionSizingContext) -> tuple[float, list[str], list[str]]:
    reasons: list[str] = []
    warnings: list[str] = []

    if ctx.daily_pnl_pct <= -3.0:
        reasons.append(f"당일 누적 손익 {ctx.daily_pnl_pct:.1f}% ≤ -3% — 신규매수 중단")
        return 0.0, reasons, warnings

    if ctx.total_equity <= 0:
        return 0.0, ["총자산 정보 없음"], warnings
    cash_ratio = ctx.cash / ctx.total_equity
    if cash_ratio < MIN_CASH_RATIO_FOR_BUY:
        reasons.append(f"현금비중 {cash_ratio*100:.1f}% < {MIN_CASH_RATIO_FOR_BUY*100:.0f}% — 신규매수 금지")
        return 0.0, reasons, warnings

    if not ctx.recent_high or ctx.recent_high <= 0:
        return 0.0, ["recent_high 정보 없음 — 매수 제안 불가"], warnings
    drawdown_pct = round((ctx.current_price / ctx.recent_high - 1.0) * 100, 6)

    ratio = 0.0
    if drawdown_pct <= -30.0 and ctx.short_term_score >= 55:
        ratio = 0.30
        reasons.append(f"전고점 대비 {drawdown_pct:.1f}%, score {ctx.short_term_score:.0f} — 현금의 30% 매수 제안")
    elif drawdown_pct <= -25.0 and ctx.short_term_score >= 55:
        ratio = 0.25
        reasons.append(f"전고점 대비 {drawdown_pct:.1f}%, score {ctx.short_term_score:.0f} — 현금의 25% 매수 제안")
    elif drawdown_pct <= -20.0 and ctx.short_term_score >= 60:
        ratio = 0.20
        reasons.append(f"전고점 대비 {drawdown_pct:.1f}%, score {ctx.short_term_score:.0f} — 현금의 20% 매수 제안")
    elif drawdown_pct <= -15.0 and ctx.short_term_score >= 60:
        ratio = 0.10
        reasons.append(f"전고점 대비 {drawdown_pct:.1f}%, score {ctx.short_term_score:.0f} — 현금의 10% 매수 제안")
    else:
        reasons.append(f"전고점 대비 {drawdown_pct:.1f}%, score {ctx.short_term_score:.0f} — 매수 조건 미충족")
        return 0.0, reasons, warnings

    buy_amount = ctx.cash * ratio

    if (ctx.mu_return_pct is not None and ctx.mu_return_pct <= -5.0) or (ctx.sox_return_pct is not None and ctx.sox_return_pct <= -3.0):
        buy_amount *= 0.5
        warnings.append("MU -5% 이하 또는 SOX -3% 이하 — 매수금액 50% 축소")

    if ctx.hynix_today_return_pct is not None and ctx.hynix_today_return_pct >= 5.0:
        warnings.append(f"당일 +{ctx.hynix_today_return_pct:.1f}% 급등 — 신규매수 금지")
        return 0.0, reasons, warnings

    daily_cap = ctx.total_equity * MAX_DAILY_BUY_PCT - ctx.today_already_bought_amount
    if daily_cap <= 0:
        warnings.append("일일 최대 매수한도(총자산의 20%) 소진 — 신규매수 금지")
        return 0.0, reasons, warnings
    if buy_amount > daily_cap:
        buy_amount = daily_cap
        warnings.append("일일 최대 매수한도(총자산의 20%)에 맞춰 매수금액 축소")

    symbol_cap = ctx.total_equity * MAX_SYMBOL_PCT - ctx.current_position_value
    if symbol_cap <= 0:
        warnings.append("종목 최대 비중(총자산의 70%) 초과 — 신규매수 금지")
        return 0.0, reasons, warnings
    if buy_amount > symbol_cap:
        buy_amount = symbol_cap
        warnings.append("종목 최대 비중(총자산의 70%)에 맞춰 매수금액 축소")

    return max(0.0, buy_amount), reasons, warnings
