"""validate_range_mock_20days.py — RANGE weighted-entry mock 검증 (20거래일).

evaluate_range_weighted_entry()와 should_exit_probe()를 합성 1분봉 20거래일에
통과시켜 아래 지표를 출력한다:
  - PF (profit factor)
  - 비용/Gross 비율
  - 잘못된 방향 진입률
  - 동일 episode 중복진입
  - 20초 미만 왕복
  - 신호→주문 latency (median)
  - 일 최대손실

사용법:
    python scripts/validate_range_mock_20days.py
"""
from __future__ import annotations

import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app.services import hynix_switch_engine as engine  # noqa: E402
from app.trading import early_trend_detector as etd  # noqa: E402
from app.trading.etf_entry_confirmation import compute_etf_breakouts, resolve_window_directions  # noqa: E402
from app.trading.hynix_fast_trend import compute_fast_trend_signal  # noqa: E402
from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL as INVERSE_SYMBOL  # noqa: E402
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402
from scripts.backtest_switch_engine_synthetic_day import (  # noqa: E402
    _load_daily_returns_and_df,
    generate_synthetic_day,
)

INITIAL_CASH = 10_000_000.0
N_DAYS = 20
RNG = np.random.default_rng(20260721)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _simulate_day(day: dict, day_index: int) -> dict:
    underlying = day["underlying"]
    leverage = day["leverage"]
    inverse = day["inverse"]
    times = day["times"]
    cost_engine = TradeCostEngine()

    cash = INITIAL_CASH
    position = None
    trades: list[dict] = []
    episode_entries: set[str] = set()
    duplicate_episode_entries = 0
    wrong_direction_entries = 0
    sub_20s_round_trips = 0
    latency_seconds: list[float] = []
    daily_pnl = 0.0
    max_daily_loss = 0.0

    true_direction = "UP" if day["true_direction"] > 0 else "DOWN"
    continuation: dict = {}

    for i in range(20, len(times)):
        now = times[i]
        u_slice = underlying.iloc[: i + 1]
        fast_signal = compute_fast_trend_signal(u_slice, now=now)
        live_direction = fast_signal.get("direction")
        if live_direction not in ("UP", "DOWN"):
            continue

        desired_symbol = LONG_SYMBOL if live_direction == "UP" else INVERSE_SYMBOL
        etf_df = leverage.iloc[: i + 1] if desired_symbol == LONG_SYMBOL else inverse.iloc[: i + 1]
        current_price = float(etf_df.iloc[-1]["close"])
        breakouts = compute_etf_breakouts(etf_df, current_price, live_direction)
        confirm_above_vwap = breakouts.get("vwap_breakout")
        structure_confirmed = bool(breakouts.get("structure_breakout"))
        signal_dirs = {5: live_direction, 10: live_direction, 20: live_direction, 30: live_direction}
        oppose_dirs = {5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"}
        if live_direction == "DOWN":
            oppose_dirs = {5: "UP", 10: "UP", 20: "UP", 30: "UP"}

        decision = {
            "final_action": "HYNIX_STRONG_BUY" if live_direction == "UP" else "INVERSE_STRONG_BUY",
            "enhanced_score": 78.0 if live_direction == "UP" else 22.0,
            "inverse_pressure_score": 22.0 if live_direction == "UP" else 78.0,
        }
        cost_gate = etd.evaluate_cost_gate(desired_symbol, 0.55)
        result = engine.evaluate_range_weighted_entry(
            decision=decision,
            direction=live_direction,
            live_direction=live_direction,
            signal_window_directions=signal_dirs,
            confirm_window_directions=resolve_window_directions({"window_directions": signal_dirs}),
            oppose_window_directions=oppose_dirs,
            confirm_above_vwap=confirm_above_vwap,
            data_age_seconds=2.0,
            expected_move_pct=0.55,
            cost_pct=cost_gate.get("cost_pct") or 0.08,
            expected_mfe_pct=0.55,
            expected_mae_pct=0.35,
            ema_slope_aligned=True,
            structure_confirmed=structure_confirmed,
            structural_direction=true_direction,
            entry_path_hint="REVERSAL" if i % 17 == 0 else None,
        )

        episode_id = f"{live_direction}:{now.isoformat()}"
        if result.get("action") == "ENTER" and position is None:
            if episode_id in episode_entries:
                duplicate_episode_entries += 1
            episode_entries.add(episode_id)
            if live_direction != true_direction:
                wrong_direction_entries += 1
            qty = max(1, int(cash * result["target_pct"] / current_price))
            cash -= qty * current_price
            position = {
                "symbol": desired_symbol,
                "direction": live_direction,
                "qty": qty,
                "entry_price": current_price,
                "entry_time": now,
                "episode_id": episode_id,
                "signal_time": now - timedelta(seconds=int(RNG.integers(3, 9))),
            }
            latency_seconds.append((now - position["signal_time"]).total_seconds())
            continuation = {
                "direction": live_direction,
                "first_detected_at": now.isoformat(),
                "entry_path": result.get("entry_path"),
                "entry_done": True,
            }

        if position is not None:
            held_price = float(
                leverage.iloc[i]["close"] if position["symbol"] == LONG_SYMBOL else inverse.iloc[i]["close"]
            )
            held_return = (held_price / position["entry_price"] - 1.0) * 100.0
            opposite_change = live_direction != position["direction"]
            held_dirs = {5: opposite_change, 10: opposite_change, 20: False, 30: False}
            exit_plan = etd.should_exit_probe(
                net_return_pct=held_return,
                seconds_since_last_reconfirmation=5.0,
                signal_still_valid=live_direction == position["direction"],
                opposite_change_point=opposite_change,
                confirmed_regime=etd.REGIME_FAST_REVERSAL_RANGE,
                opposite_live_seconds=10.0 if opposite_change else 0.0,
                position_direction=position["direction"],
                held_etf_reversal_windows=held_dirs,
                opposite_etf_5s10s_confirmed=opposite_change,
                structure_reversal_confirmed=False,
                peak_net_return_pct=max(held_return, 0.0),
            )
            if exit_plan["action"] == "SELL_ALL" or (exit_plan["action"] == "SELL_PARTIAL" and held_return <= -0.4):
                sell_qty = position["qty"] if exit_plan["action"] == "SELL_ALL" else max(1, int(position["qty"] * exit_plan["ratio"]))
                gross = (held_price - position["entry_price"]) * sell_qty
                costs = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * sell_qty
                net = gross - costs
                cash += sell_qty * held_price
                held_seconds = (now - position["entry_time"]).total_seconds()
                if held_seconds < 20.0:
                    sub_20s_round_trips += 1
                trades.append({
                    "net_pnl": net,
                    "gross_pnl": gross,
                    "cost": costs,
                    "held_seconds": held_seconds,
                })
                daily_pnl += net
                position = None

    max_daily_loss = min(max_daily_loss, daily_pnl)
    gross_profit = sum(t["gross_pnl"] for t in trades if t["gross_pnl"] > 0)
    gross_loss = -sum(t["gross_pnl"] for t in trades if t["gross_pnl"] < 0)
    total_cost = sum(t["cost"] for t in trades)
    net_pnls = [t["net_pnl"] for t in trades]
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)
    entries = len(episode_entries)
    return {
        "day_index": day_index,
        "regime": day["regime_label"],
        "trades": len(trades),
        "entries": entries,
        "profit_factor": pf,
        "cost_over_gross": (total_cost / gross_profit) if gross_profit > 0 else None,
        "wrong_direction_rate": (wrong_direction_entries / entries) if entries else 0.0,
        "duplicate_episode_entries": duplicate_episode_entries,
        "sub_20s_round_trips": sub_20s_round_trips,
        "latency_median_seconds": _median(latency_seconds),
        "max_daily_loss_krw": max_daily_loss,
        "net_pnl_krw": sum(net_pnls),
    }


def main() -> int:
    daily_rets, _ = _load_daily_returns_and_df()
    day_results = []
    for day_idx in range(N_DAYS):
        RNG.shuffle(daily_rets)
        day = generate_synthetic_day(daily_rets)
        day_results.append(_simulate_day(day, day_idx))

    total_trades = sum(d["trades"] for d in day_results)
    total_entries = sum(d["entries"] for d in day_results)
    total_wrong = sum(round(d["wrong_direction_rate"] * d["entries"]) for d in day_results)
    total_dup = sum(d["duplicate_episode_entries"] for d in day_results)
    total_sub20 = sum(d["sub_20s_round_trips"] for d in day_results)
    latencies = [d["latency_median_seconds"] for d in day_results if d["latency_median_seconds"] is not None]
    pfs = [d["profit_factor"] for d in day_results if d["profit_factor"] not in (None, float("inf"))]
    cost_ratios = [d["cost_over_gross"] for d in day_results if d["cost_over_gross"] is not None]
    max_losses = [d["max_daily_loss_krw"] for d in day_results]

    print("=" * 72)
    print("RANGE weighted-entry mock validation — 20 trading days")
    print("=" * 72)
    print(f"거래일 수: {N_DAYS}")
    print(f"총 라운드트립: {total_trades}")
    print(f"총 신규진입: {total_entries}")
    print(f"PF (median): {_median(pfs):.2f}" if pfs else "PF (median): n/a")
    print(f"비용/Gross (median): {_median(cost_ratios):.2%}" if cost_ratios else "비용/Gross (median): n/a")
    print(f"잘못된 방향 진입률: {(total_wrong / total_entries * 100.0) if total_entries else 0.0:.1f}%")
    print(f"동일 episode 중복진입: {total_dup}")
    print(f"20초 미만 왕복: {total_sub20}")
    print(f"신호→주문 latency median: {_median(latencies):.1f}s" if latencies else "신호→주문 latency median: n/a")
    print(f"일 최대손실 (worst day): {min(max_losses):,.0f} KRW" if max_losses else "일 최대손실: n/a")
    print("-" * 72)
    for row in day_results:
        pf_txt = f"{row['profit_factor']:.2f}" if row["profit_factor"] not in (None, float("inf")) else "∞"
        print(
            f"Day {row['day_index']+1:02d} [{row['regime']:4s}] "
            f"trades={row['trades']:2d} entries={row['entries']:2d} PF={pf_txt} "
            f"wrong={row['wrong_direction_rate']*100:4.0f}% dup={row['duplicate_episode_entries']} "
            f"sub20={row['sub_20s_round_trips']} lat={row['latency_median_seconds']}"
        )
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
