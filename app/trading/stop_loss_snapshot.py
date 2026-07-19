"""
stop_loss_snapshot.py — 손절 계산의 단일 입력(position_snapshot).

모든 손절 평가 경로(legacy evaluate_tp_sl, Dynamic Exit watcher, Big Trend
Holding, SELL_ONLY_RECOVERY, Fast watcher/reversal exit)가 각자 entry_price/
current_price/effective_sl_pct를 따로 계산하던 것을, 이 모듈이 만드는 단일
스냅샷 하나로 통일한다.

  entry_price      — KIS 실제 평단(pchs_avg_pric, position_manager.current_
                      position["avg_price"])을 최우선으로 쓴다. 브로커 재조회가
                      안 됐을 때만 state["position"]["entry_price"]로 폴백한다
                      (최초매수가/과거 캐시값이 아니라 "최근에 확인된 평단"이라는
                      점이 중요 — 추가매수 후에는 이 값이 곧바로 새 가중평균으로
                      바뀐다).
  current_price    — 실시간 조회가(呼출자가 넘긴다). stale 캐시로 대체됐다면
                      current_price_stale=True로 표시해, 화면과 손절판단이 같은
                      사실을 본다.
  effective_sl_pct — 현재 confirmed adaptive regime 프로필 하나에서만 도출한다
                      (adaptive_market_regime.effective_sl_pct_for_position).
                      과거 regime/이전 effective_sl_pct/UI 캐시값은 절대 쓰지
                      않는다 — 이 함수가 매번 새로 계산한다.
  position_snapshot_id — 이 스냅샷이 계산된 순간을 식별하는 값. UI가 "화면에
                      표시된 값"과 "실제 주문 판단에 쓰인 값"이 같은 스냅샷에서
                      나왔는지 검증할 수 있게 한다.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional


def _snapshot_id(symbol: str, entry_price: float, current_price: float, quantity: float, now: datetime) -> str:
    raw = f"{symbol}|{entry_price}|{current_price}|{quantity}|{now.isoformat()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def build_stop_loss_snapshot(
    *, symbol: Optional[str], quantity: float, kis_entry_price: Optional[float],
    fallback_entry_price: Optional[float], current_price: Optional[float],
    current_price_stale: bool = False, current_price_source: Optional[str] = None,
    confirmed_regime: Optional[str], now: Optional[datetime] = None,
) -> Optional[dict]:
    """손절 계산의 단일 입력(position_snapshot)을 만든다. 포지션이 없으면 None.

    entry_price는 kis_entry_price(브로커 재조회로 막 확인된 평단)를 최우선으로
    쓰고, 그것이 없을 때만 fallback_entry_price(state 캐시)를 쓴다 — 최초매수가나
    과거 regime/effective_sl_pct는 여기 들어올 여지가 아예 없다(호출부가 항상
    "지금" 값만 넘긴다).
    """
    from app.trading.adaptive_market_regime import effective_sl_pct_for_position
    from app.trading.trading_cost_engine import TradeCostEngine
    from app.utils.time_utils import kst_now

    now = now or kst_now()
    if not symbol or (quantity or 0) <= 0 or not current_price:
        return None

    entry_price = kis_entry_price or fallback_entry_price
    if not entry_price or entry_price <= 0:
        return None

    gross_return_pct = round((current_price / entry_price - 1.0) * 100.0, 4)
    try:
        cost = TradeCostEngine().compute_unrealized_net_pnl(
            symbol, entry_price=entry_price, current_price=current_price, quantity=quantity,
        )
        invested = entry_price * quantity
        net_return_pct = round(cost["net_unrealized_pnl"] / invested * 100.0, 4) if invested else gross_return_pct
    except Exception:
        net_return_pct = gross_return_pct

    effective_sl_pct = effective_sl_pct_for_position(confirmed_regime, symbol)
    hard_stop_triggered = net_return_pct <= effective_sl_pct

    return {
        "position_snapshot_id": _snapshot_id(symbol, entry_price, current_price, quantity, now),
        "computed_at": now.isoformat(timespec="seconds"),
        "symbol": symbol, "quantity": quantity,
        "entry_price": entry_price, "entry_price_source": "KIS_AVG_PRICE" if kis_entry_price else "STATE_FALLBACK",
        "current_price": current_price, "current_price_stale": bool(current_price_stale),
        "current_price_source": current_price_source,
        "confirmed_regime": confirmed_regime or "DATA_INSUFFICIENT",
        "effective_sl_pct": effective_sl_pct,
        "gross_return_pct": gross_return_pct, "net_return_pct": net_return_pct,
        "hard_stop_triggered": hard_stop_triggered,
    }
