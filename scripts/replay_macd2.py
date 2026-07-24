"""scripts/replay_macd2.py — MACD2 replay/backtest driver (docs §17).

Imports the SAME functions the live Worker uses (signal_engine, risk_exit,
order_executor's quantity/idempotency logic) — no separate reimplementation
of MACD/signed-B/exit logic. Runs bar-by-bar against a CSV of 1-minute bars
using an in-memory ReplayBroker (network-free): next-1m-open fill, adverse
slippage, and TradeCostEngine-based cost modeling (the same generic,
non-MACD-v1 cost engine order_executor.py itself uses).

BLOCKED_DATA_MISSING: the real, previously-verified 7/21 and 7/22 signed-B
timelines live in data/cache/replay_*.csv, deleted in the 2026-07-23
incident and still pending Google Drive/OneDrive recovery at the time this
script was written. Real-timeline parity against those specific dates
cannot be run until that data is restored — this script has only been
exercised against synthetic CSVs (see tests/macd2/test_parity.py for the
synthetic-vs-MACD-v1 formula parity check, which uses the live functions
directly rather than this CLI wrapper).

Usage:
    python scripts/replay_macd2.py --csv path/to/1m_bars.csv --budget 10000000
"""
from __future__ import annotations

import argparse
import contextlib
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.trading.macd2 import config, ledger, order_executor, risk_exit  # noqa: E402
from app.trading.macd2.broker_adapter import BrokerOrderResult  # noqa: E402
from app.trading.macd2.models import Direction, PositionSnapshot  # noqa: E402
from app.trading.macd2.signal_engine import (  # noqa: E402
    calculate_macd, evaluate_macd_crossover, signed_b_condition, make_signal_id, resample_completed_3m,
)
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402

KST = config.KST


class ReplayBroker:
    """In-memory broker for replay — next-1m-open fill + adverse slippage, no network."""

    mode = "mock"

    def __init__(self, cash: float, *, adverse_slippage_pct: float = 0.05) -> None:
        self._cash = cash
        self._positions: dict[str, PositionSnapshot] = {}
        self._pending_price: dict[str, float] = {}
        self._adverse = adverse_slippage_pct / 100.0
        self._order_seq = 0
        self.fills: list[dict[str, Any]] = []

    def set_fill_price(self, symbol: str, price: float) -> None:
        self._pending_price[symbol] = price

    def get_orderable_cash(self, symbol: str) -> float:
        del symbol
        return self._cash

    def get_position(self, symbol: str) -> Optional[PositionSnapshot]:
        return self._positions.get(symbol)

    def reconcile_position(self, symbol: str) -> int:
        pos = self._positions.get(symbol)
        return int(pos.quantity) if pos else 0

    def _next_id(self) -> str:
        self._order_seq += 1
        return f"REPLAY-{self._order_seq:06d}"

    def buy_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        price = self._pending_price.get(symbol)
        if price is None or qty < 1:
            return BrokerOrderResult(False, self._next_id(), symbol, "BUY", qty, 0, 0.0, "NO_FILL_PRICE")
        fill_price = price * (1 + self._adverse)  # adverse: buys fill worse (higher)
        self._cash -= fill_price * qty
        self._positions[symbol] = PositionSnapshot(symbol=symbol, quantity=qty, avg_price=fill_price)
        self.fills.append({"side": "BUY", "symbol": symbol, "qty": qty, "price": fill_price})
        return BrokerOrderResult(True, self._next_id(), symbol, "BUY", qty, qty, fill_price, "OK")

    def sell_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        price = self._pending_price.get(symbol)
        pos = self._positions.get(symbol)
        if price is None or pos is None or pos.quantity < qty:
            return BrokerOrderResult(False, self._next_id(), symbol, "SELL", qty, 0, 0.0, "NO_FILL_PRICE_OR_POSITION")
        fill_price = price * (1 - self._adverse)  # adverse: sells fill worse (lower)
        self._cash += fill_price * qty
        del self._positions[symbol]
        self.fills.append({"side": "SELL", "symbol": symbol, "qty": qty, "price": fill_price})
        return BrokerOrderResult(True, self._next_id(), symbol, "SELL", qty, qty, fill_price, "OK")


def _net_return_pct(symbol: str, entry_price: float, current_price: float, quantity: int) -> float:
    if entry_price <= 0 or quantity <= 0 or current_price <= 0:
        return 0.0
    cost = TradeCostEngine().compute_net_pnl(
        symbol, entry_price, current_price, quantity, buy_order_type="market", sell_order_type="market",
    )
    return float(cost["net_pnl"]) / (entry_price * quantity) * 100.0


@contextlib.contextmanager
def _isolated_replay_ledger():
    """order_executor.execute_signal/execute_exit are reused directly here
    (docs §17: no duplicated strategy logic) — but that means they also call
    the SAME ledger.append_execution/append_signal a live run would. Replay
    output must never land in the real MACD2 execution/signal ledger, so
    ledger.py's path constants are redirected to a throwaway temp directory
    for the duration of the replay, then restored.
    """
    original = (ledger.LOGS_DIR_PATH, ledger.EXECUTION_LEDGER_PATH, ledger.SIGNAL_LEDGER_PATH)
    with tempfile.TemporaryDirectory(prefix="macd2_replay_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        ledger.LOGS_DIR_PATH = tmp_path
        ledger.EXECUTION_LEDGER_PATH = tmp_path / "replay_execution_ledger.csv"
        ledger.SIGNAL_LEDGER_PATH = tmp_path / "replay_signal_ledger.csv"
        try:
            yield
        finally:
            ledger.LOGS_DIR_PATH, ledger.EXECUTION_LEDGER_PATH, ledger.SIGNAL_LEDGER_PATH = original


@dataclass
class ReplayResult:
    signal_timeline: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    total_cost: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: Optional[float] = None
    max_drawdown: float = 0.0
    round_trip_count: int = 0


def _summarize(signal_timeline: list[dict], trades: list[dict], broker: ReplayBroker) -> ReplayResult:
    net_values: list[float] = []
    gross_total = 0.0
    cost_total = 0.0
    open_buys: dict[str, list[dict]] = {}
    for fill in broker.fills:
        if fill["side"] == "BUY":
            open_buys.setdefault(fill["symbol"], []).append(fill)
        else:
            queue = open_buys.get(fill["symbol"]) or []
            if not queue:
                continue
            buy = queue.pop(0)
            cost = TradeCostEngine().compute_net_pnl(
                fill["symbol"], buy["price"], fill["price"], fill["qty"],
                buy_order_type="market", sell_order_type="market",
            )
            net_values.append(cost["net_pnl"])
            gross_total += cost["gross_pnl"]
            cost_total += cost["total_cost"]

    wins = [v for v in net_values if v > 0]
    losses = [v for v in net_values if v < 0]
    win_rate = (len(wins) / len(net_values) * 100.0) if net_values else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (None if not wins else float("inf"))
    equity = peak = max_dd = 0.0
    for v in net_values:
        equity += v
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return ReplayResult(
        signal_timeline=signal_timeline, trades=trades,
        net_pnl=round(sum(net_values), 2), gross_pnl=round(gross_total, 2), total_cost=round(cost_total, 2),
        win_rate_pct=round(win_rate, 2), profit_factor=profit_factor, max_drawdown=round(max_dd, 2),
        round_trip_count=len(net_values),
    )


def run_replay(
    df_1m: pd.DataFrame,
    *,
    budget: float = config.DEFAULT_BUDGET,
    adverse_slippage_pct: float = 0.05,
) -> ReplayResult:
    """Bar-by-bar replay using the SAME signal_engine/risk_exit/order_executor
    functions the live Worker calls — no duplicated strategy logic (docs §17).
    """
    broker = ReplayBroker(cash=budget, adverse_slippage_pct=adverse_slippage_pct)
    signal_timeline: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    last_direction: Optional[Direction] = None
    processed: set[str] = set()
    position: Optional[PositionSnapshot] = None
    peak_net_return = 0.0
    profit_lock_active = False
    last_seen_bar: Optional[str] = None

    df_1m = df_1m.sort_values("datetime").reset_index(drop=True)

    with _isolated_replay_ledger():
        for i in range(len(df_1m)):
            now = pd.Timestamp(df_1m["datetime"].iloc[i]).to_pydatetime() + timedelta(minutes=1)
            bars_3m = resample_completed_3m(df_1m.iloc[: i + 1], now=now)
            macd_snap = calculate_macd(bars_3m)
            if macd_snap is None:
                continue
            bar_key = macd_snap.bar_dt.isoformat()
            if bar_key == last_seen_bar:
                continue
            last_seen_bar = bar_key

            if position is not None:
                current_price = float(df_1m["close"].iloc[i])
                broker.set_fill_price(position.symbol, current_price)
                net_return = _net_return_pct(position.symbol, position.avg_price, current_price, position.quantity)
                exits = risk_exit.evaluate_position_exits(
                    current_net_return=net_return, peak_net_return=peak_net_return,
                    profit_lock_active=profit_lock_active,
                )
                peak_net_return, profit_lock_active = exits.peak_net_return, exits.profit_lock_active
                if exits.exit_reason:
                    outcome = order_executor.execute_exit(
                        broker=broker, symbol=position.symbol, quantity=position.quantity,
                        exit_reason=exits.exit_reason, entry_price=position.avg_price,
                        reconcile_retries=1, reconcile_delay_sec=0.0,
                    )
                    if outcome.final_state.value == "EXECUTED":
                        trades.append({"exit_reason": exits.exit_reason, "symbol": position.symbol, "bar": bar_key})
                        position = None
                        peak_net_return, profit_lock_active = 0.0, False

            pattern = evaluate_macd_crossover(macd_snap, last_direction)
            shadow = signed_b_condition(macd_snap)
            signal_timeline.append({
                "bar": bar_key,
                "pattern": pattern.value,
                "previous_diff": macd_snap.previous_diff,
                "current_diff": macd_snap.current_diff,
                "signed_b_shadow": shadow.value,
                "hist_last3": macd_snap.hist_last3,
            })
            if pattern == Direction.HOLD:
                continue

            target = order_executor.target_symbol_for_direction(pattern)
            held_symbol = position.symbol if position else None
            if held_symbol == target:
                continue

            trading_date = macd_snap.bar_dt.strftime("%Y%m%d")
            signal_id = make_signal_id(macd_snap.bar_dt, pattern)
            next_open = float(df_1m["open"].iloc[i + 1]) if i + 1 < len(df_1m) else float(df_1m["close"].iloc[i])
            broker.set_fill_price(target, next_open)
            if position is not None:
                broker.set_fill_price(position.symbol, next_open)

            outcome = order_executor.execute_signal(
                broker=broker, direction=pattern, signal_id=signal_id,
                quotes={target: next_open}, position=position, budget=budget,
                processed_signal_ids=frozenset(processed), reconcile_retries=1, reconcile_delay_sec=0.0,
            )
            if outcome.final_state.value == "EXECUTED":
                processed.add(signal_id)
                last_direction = pattern
                position = PositionSnapshot(
                    symbol=target, quantity=outcome.quantity,
                    avg_price=outcome.buy_result.executed_price if outcome.buy_result else next_open,
                )
                peak_net_return, profit_lock_active = 0.0, False
                trades.append({"signal_id": signal_id, "direction": pattern.value, "symbol": target, "bar": bar_key})

    return _summarize(signal_timeline, trades, broker)


def _load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize(KST)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="MACD2 replay driver (docs section 17)")
    parser.add_argument("--csv", required=True, help="1-minute bar CSV: datetime,open,high,low,close[,volume]")
    parser.add_argument("--budget", type=float, default=config.DEFAULT_BUDGET)
    parser.add_argument("--adverse-slippage-pct", type=float, default=0.05)
    args = parser.parse_args()

    df_1m = _load_csv(Path(args.csv))
    result = run_replay(df_1m, budget=args.budget, adverse_slippage_pct=args.adverse_slippage_pct)

    print(
        f"round_trip_count={result.round_trip_count} net_pnl={result.net_pnl} "
        f"gross_pnl={result.gross_pnl} total_cost={result.total_cost} "
        f"win_rate_pct={result.win_rate_pct} profit_factor={result.profit_factor} "
        f"max_drawdown={result.max_drawdown}"
    )
    for trade in result.trades:
        print(trade)


if __name__ == "__main__":
    main()
