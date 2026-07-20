"""
early_trend_detector_backtest.py — Adaptive Regime 단독 vs Adaptive+Early Trend
Detector 결합형 비교 백테스트.

한계(명시적 근사) — 이 프로젝트의 캐시 데이터는 1분봉 OHLCV뿐이고 실제 호가
잔량/체결 tick 데이터가 없다. 따라서:
  - "조기신호"는 라이브 코드와 동일하게 app.trading.hynix_fast_trend.
    compute_fast_trend_signal()(1분봉 기반 6-vote)로 근사한다.
  - CHASE_BLOCK의 스프레드/슬리피지 초과 조건은 tick 데이터가 없어 이동폭/
    극값 조건만 반영한다(주문 실행 자체를 시뮬레이션하지 않는다).
  - 손익은 항상 app.trading.trading_cost_engine.TradeCostEngine으로 계산해
    수수료/거래세/슬리피지를 반영한다(단순 가격차가 아니다).
  - 단일 심볼·단일 방향(STRONG_UP 또는 STRONG_DOWN 한쪽) 추세만 비교한다 —
    라이브 시스템의 하이닉스⇄인버스 스위칭 전체를 재현하지 않는다.

두 전략:
  ADAPTIVE_ONLY  — confirmed_regime이 STRONG_UP/DOWN으로 확정된 시점에만
                   전량 진입하고, confirmed regime 기준 effective_sl_pct 또는
                   반대 방향 확정 시 청산한다(현재 라이브 하드손절 로직과 동일
                   원리 — app.trading.adaptive_market_regime.
                   effective_sl_pct_for_position()).
  ADAPTIVE_PLUS_EARLY — 위 전략에 더해, confirmed 이전에도
                   app.trading.early_trend_detector의 단계별 탐색진입(5%→15%→
                   25%)을 실행하고, STRONG_UP/DOWN이 실제로 confirmed되면
                   40~50%로 확대해 ADAPTIVE_ONLY와 같은 청산 로직으로 넘어간다.
                   확대 전 탐색 중에는 should_exit_probe()의 고정 -0.4%/신호
                   소멸/반대 변화점/60초 미확인 조건으로 청산한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

MIN_BARS_REQUIRED = 21


def _empty_metrics() -> dict:
    return {
        "trade_count": 0, "net_return_pct": 0.0, "max_drawdown_pct": 0.0,
        "avg_entry_delay_seconds": None, "false_signal_loss_pct": 0.0,
        "total_trade_cost_pct": 0.0, "profit_factor": None,
    }


def _finalize_metrics(equity_curve_pct: list[float], trades: list[dict], entry_delays: list[float]) -> dict:
    if not trades:
        metrics = _empty_metrics()
        metrics["avg_entry_delay_seconds"] = (sum(entry_delays) / len(entry_delays)) if entry_delays else None
        return metrics

    net_returns = [t["net_pnl_pct"] for t in trades]
    net_return_pct = round(sum(net_returns), 4)
    wins = [r for r in net_returns if r > 0]
    losses = [r for r in net_returns if r < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)

    false_signal_losses = [t["net_pnl_pct"] for t in trades if t.get("is_false_signal") and t["net_pnl_pct"] < 0]
    false_signal_loss_pct = round(sum(false_signal_losses), 4)
    total_cost_pct = round(sum(t.get("cost_pct", 0.0) for t in trades), 4)

    if equity_curve_pct:
        peak = equity_curve_pct[0]
        max_dd = 0.0
        for value in equity_curve_pct:
            peak = max(peak, value)
            max_dd = min(max_dd, value - peak)
        max_drawdown_pct = round(max_dd, 4)
    else:
        max_drawdown_pct = 0.0

    return {
        "trade_count": len(trades), "net_return_pct": net_return_pct, "max_drawdown_pct": max_drawdown_pct,
        "avg_entry_delay_seconds": (sum(entry_delays) / len(entry_delays)) if entry_delays else None,
        "false_signal_loss_pct": false_signal_loss_pct, "total_trade_cost_pct": total_cost_pct,
        "profit_factor": profit_factor,
    }


def run_comparison_backtest(
    df_1min: pd.DataFrame, symbol: str, *, prev_close: Optional[float] = None,
) -> dict:
    """ADAPTIVE_ONLY와 ADAPTIVE_PLUS_EARLY를 같은 1분봉 위에서 비교한다.

    Returns: {"adaptive_only": {...}, "adaptive_plus_early": {...}} — 각각
    trade_count/net_return_pct/max_drawdown_pct/avg_entry_delay_seconds/
    false_signal_loss_pct/total_trade_cost_pct/profit_factor.
    """
    from app.trading.adaptive_market_regime import (
        classify_raw_regime, update_regime_confirmation, effective_sl_pct_for_position,
        STRONG_UP, STRONG_DOWN,
    )
    from app.trading.hynix_fast_trend import compute_fast_trend_signal
    from app.trading import early_trend_detector as etd
    from app.trading.trading_cost_engine import TradeCostEngine

    if df_1min is None or df_1min.empty or len(df_1min) < MIN_BARS_REQUIRED:
        empty = _empty_metrics()
        return {"adaptive_only": dict(empty), "adaptive_plus_early": dict(empty)}

    df = df_1min.sort_values("datetime").reset_index(drop=True)
    cost_engine = TradeCostEngine()

    confirmation_state = None
    adaptive_position: Optional[dict] = None
    adaptive_trades: list[dict] = []
    adaptive_equity: list[float] = []
    adaptive_realized_pct = 0.0

    early_position: Optional[dict] = None
    early_candidate: Optional[dict] = None
    early_trades: list[dict] = []
    early_equity: list[float] = []
    early_realized_pct = 0.0
    entry_delays: list[float] = []
    previous_confirmed_regime: Optional[str] = None

    def _direction_of(regime: str) -> Optional[str]:
        if regime == STRONG_UP:
            return "UP"
        if regime == STRONG_DOWN:
            return "DOWN"
        return None

    def _net_return_pct(entry_price: float, current_price: float, qty: float = 1.0) -> float:
        cost = cost_engine.compute_unrealized_net_pnl(symbol, entry_price=entry_price, current_price=current_price, quantity=qty)
        invested = entry_price * qty
        return round(cost["net_unrealized_pnl"] / invested * 100.0, 4) if invested else 0.0

    def _close_trade(position: dict, exit_price: float, exit_time: datetime, is_false_signal: bool = False) -> dict:
        result = cost_engine.compute_net_pnl(symbol, entry_price=position["entry_price"], exit_price=exit_price, quantity=1.0)
        invested = position["entry_price"]
        net_pct = round(result["net_pnl"] / invested * 100.0, 4) if invested else 0.0
        cost_pct = round(result["total_cost"] / invested * 100.0, 4) if invested else 0.0
        return {
            "entry_time": position["entry_time"], "exit_time": exit_time, "direction": position["direction"],
            "net_pnl_pct": net_pct, "cost_pct": cost_pct, "is_false_signal": is_false_signal,
        }

    for i in range(MIN_BARS_REQUIRED - 1, len(df)):
        window = df.iloc[: i + 1]
        now_ts = window.iloc[-1]["datetime"]
        if not isinstance(now_ts, datetime):
            now_ts = pd.Timestamp(now_ts).to_pydatetime()
        current_price = float(window.iloc[-1]["close"])

        raw = classify_raw_regime(window, prev_close=prev_close, now=now_ts)
        confirmation_state = update_regime_confirmation(confirmation_state, raw["regime"], now_ts)
        confirmed_regime = confirmation_state["confirmed_regime"]

        fast_signal = compute_fast_trend_signal(window.tail(20), now=now_ts)
        early_signal = etd.compute_early_signal(fast_signal)

        # ── ADAPTIVE_ONLY ──────────────────────────────────────────────────
        if adaptive_position is None:
            direction = _direction_of(confirmed_regime)
            if direction and confirmed_regime != previous_confirmed_regime:
                adaptive_position = {"direction": direction, "entry_price": current_price, "entry_time": now_ts}
        else:
            sl_pct = effective_sl_pct_for_position(confirmed_regime, symbol)
            net_pct = _net_return_pct(adaptive_position["entry_price"], current_price)
            reversed_direction = _direction_of(confirmed_regime) not in (None, adaptive_position["direction"])
            if net_pct <= sl_pct or reversed_direction:
                adaptive_trades.append(_close_trade(adaptive_position, current_price, now_ts))
                adaptive_realized_pct += adaptive_trades[-1]["net_pnl_pct"]
                adaptive_position = None
        adaptive_unrealized = (
            _net_return_pct(adaptive_position["entry_price"], current_price) if adaptive_position else 0.0
        )
        adaptive_equity.append(adaptive_realized_pct + adaptive_unrealized)

        # ── ADAPTIVE_PLUS_EARLY ──────────────────────────────────────────────
        if early_position is None:
            direction = early_signal.get("direction")
            if direction and early_signal.get("score", 0) >= 50.0:
                if early_candidate is None or early_candidate.get("direction") != direction:
                    early_candidate = {"direction": direction, "first_detected_at": now_ts, "reference_price": current_price}
                elapsed = max(0.0, (now_ts - early_candidate["first_detected_at"]).total_seconds())
                stage, pct = etd.compute_target_probe_pct(confirmed_regime, elapsed)
                if pct > 0.0:
                    early_position = {
                        "direction": direction, "entry_price": current_price, "entry_time": now_ts,
                        "detected_at": early_candidate["first_detected_at"], "stage": stage,
                        "last_reconfirmed_at": now_ts, "graduated": False,
                    }
                    entry_delays.append(0.0)  # 진입 지연 자체는 아래에서 confirmed 시점과 비교해 갱신
                    early_candidate = None
            else:
                early_candidate = None
        else:
            direction = early_position["direction"]
            if not early_position["graduated"]:
                confirmed_direction = _direction_of(confirmed_regime)
                if confirmed_direction == direction and confirmed_regime in (STRONG_UP, STRONG_DOWN):
                    # 확정 — 이 시점을 "adaptive-only가 진입했을 시점"과 비교해 진입지연을 기록한다.
                    delay_seconds = max(0.0, (now_ts - early_position["entry_time"]).total_seconds())
                    if entry_delays and entry_delays[-1] == 0.0:
                        entry_delays[-1] = delay_seconds
                    early_position["graduated"] = True
                    early_position["stage"] = etd.STAGE_CONFIRMED_EXPANDED
                else:
                    net_pct = _net_return_pct(early_position["entry_price"], current_price)
                    signal_still_valid = early_signal.get("direction") == direction and early_signal.get("score", 0) >= 50.0
                    if signal_still_valid:
                        early_position["last_reconfirmed_at"] = now_ts
                    seconds_since_reconfirm = (now_ts - early_position["last_reconfirmed_at"]).total_seconds()
                    opposite_cp = etd.is_opposite_change_point(direction, early_signal)
                    exit_plan = etd.should_exit_probe(
                        net_return_pct=net_pct, seconds_since_last_reconfirmation=seconds_since_reconfirm,
                        signal_still_valid=signal_still_valid, opposite_change_point=opposite_cp,
                        confirmed_regime=confirmed_regime,
                    )
                    if exit_plan["action"] != "HOLD":
                        early_trades.append(_close_trade(early_position, current_price, now_ts, is_false_signal=True))
                        early_realized_pct += early_trades[-1]["net_pnl_pct"]
                        early_position = None
            else:
                sl_pct = effective_sl_pct_for_position(confirmed_regime, symbol)
                net_pct = _net_return_pct(early_position["entry_price"], current_price)
                reversed_direction = _direction_of(confirmed_regime) not in (None, direction)
                if net_pct <= sl_pct or reversed_direction:
                    early_trades.append(_close_trade(early_position, current_price, now_ts))
                    early_realized_pct += early_trades[-1]["net_pnl_pct"]
                    early_position = None
        early_unrealized = _net_return_pct(early_position["entry_price"], current_price) if early_position else 0.0
        early_equity.append(early_realized_pct + early_unrealized)

        previous_confirmed_regime = confirmed_regime

    # 데이터 구간 종료 시점에도 포지션이 열려 있으면(실거래라면 15:15 강제청산에
    # 해당) 마지막 가격으로 청산해 손익에 반영한다 — 그렇지 않으면 추세가 데이터
    # 끝까지 이어진 흔한 경우(오히려 수익권일 가능성이 높음)가 전부 trade_count=0/
    # net_return_pct=0으로 누락된다.
    final_price = float(df.iloc[-1]["close"])
    final_time = df.iloc[-1]["datetime"]
    if not isinstance(final_time, datetime):
        final_time = pd.Timestamp(final_time).to_pydatetime()
    if adaptive_position is not None:
        adaptive_trades.append(_close_trade(adaptive_position, final_price, final_time))
    if early_position is not None:
        early_trades.append(_close_trade(early_position, final_price, final_time))

    entry_delays = [d for d in entry_delays if d > 0.0]
    return {
        "adaptive_only": _finalize_metrics(adaptive_equity, adaptive_trades, []),
        "adaptive_plus_early": _finalize_metrics(early_equity, early_trades, entry_delays),
    }
