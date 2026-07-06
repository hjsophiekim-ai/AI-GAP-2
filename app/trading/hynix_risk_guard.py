"""
hynix_risk_guard.py — SK하이닉스 자동매매 주문 전 안전장치.

가격 오류, 소스 간 괴리, 분봉 지연, 일일 누적 손실을 점검해
주문을 막을지(blocks_buy/blocks_sell) 판단한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

PRICE_ERROR_THRESHOLD_PCT = 30.0
SOURCE_DIVERGENCE_TOLERANCE_PCT = 1.0
MINUTE_STALE_THRESHOLD_MIN = 10.0
DAILY_LOSS_LIMIT_PCT = -3.0


def check_risk_guards(
    prev_close: Optional[float],
    current_price: Optional[float],
    source_prices: dict,
    minute_bar_timestamp: Optional[datetime],
    now: Optional[datetime] = None,
    total_equity: Optional[float] = None,
    daily_pnl_pct: float = 0.0,
) -> dict:
    """
    Returns
    -------
    dict: {"passed": bool, "blocks_buy": bool, "blocks_sell": bool, "reasons": [...]}
    """
    now = now or datetime.now()
    reasons: list[str] = []
    blocks_buy = False
    blocks_sell = False

    if current_price is None or current_price <= 0:
        reasons.append("현재가 없음 — 모든 주문 금지")
        return {"passed": False, "blocks_buy": True, "blocks_sell": True, "reasons": reasons}

    if prev_close is not None and prev_close > 0:
        change_pct = abs(current_price / prev_close - 1.0) * 100
        if change_pct >= PRICE_ERROR_THRESHOLD_PCT:
            reasons.append(
                f"현재가가 전일 종가 대비 ±{PRICE_ERROR_THRESHOLD_PCT:.0f}% 이상 변동({change_pct:.1f}%) — 가격 오류 가능성, 모든 주문 금지"
            )
            blocks_buy = True
            blocks_sell = True

    try:
        from app.data.market_data_validator import validate_hynix_current_sources

        ok, msg, _detail = validate_hynix_current_sources(source_prices, tolerance_pct=SOURCE_DIVERGENCE_TOLERANCE_PCT)
        if not ok:
            reasons.append(f"가격 소스 교차검증 실패 ({msg}) — 모든 주문 금지")
            blocks_buy = True
            blocks_sell = True
    except Exception as exc:
        reasons.append(f"가격 소스 검증 불가({exc}) — 모든 주문 금지")
        blocks_buy = True
        blocks_sell = True

    if minute_bar_timestamp is None:
        reasons.append("분봉 데이터 시각 없음 — 모든 주문 금지")
        blocks_buy = True
        blocks_sell = True
    else:
        delay_min = (now - minute_bar_timestamp).total_seconds() / 60.0
        if delay_min >= MINUTE_STALE_THRESHOLD_MIN:
            reasons.append(f"분봉 데이터가 {delay_min:.1f}분 지연 (>= {MINUTE_STALE_THRESHOLD_MIN:.0f}분) — 모든 주문 금지")
            blocks_buy = True
            blocks_sell = True

    if daily_pnl_pct <= DAILY_LOSS_LIMIT_PCT:
        reasons.append(f"당일 누적 손익 {daily_pnl_pct:.1f}% <= {DAILY_LOSS_LIMIT_PCT:.0f}% — 신규매수만 중단(매도는 허용)")
        blocks_buy = True

    passed = not (blocks_buy and blocks_sell)
    return {"passed": passed, "blocks_buy": blocks_buy, "blocks_sell": blocks_sell, "reasons": reasons}
