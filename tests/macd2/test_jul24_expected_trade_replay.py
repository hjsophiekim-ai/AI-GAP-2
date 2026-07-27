"""Offline fake-order replay for the 2026-07-24 expected MACD2 trade table.

The replay is intentionally data-driven and network-free. Provide actual
2026-07-24 1m CSVs through MACD2_JUL24_1M_DIR:

    000660_1m.csv
    0193T0_1m.csv
    0197X0_1m.csv

No production code is changed and tests/macd2/conftest.py redirects MACD2
state/ledgers to tmp_path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, order_executor
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.models import Direction, PositionSnapshot, SignalState
from app.trading.trading_cost_engine import TradeCostEngine

KST = config.KST
TRADING_DATE = "20260724"
INITIAL_BUDGET = 9_200_000.0

EXPECTED_SIGNALS = [
    ("09:48", Direction.UP_RED, config.LONG_SYMBOL),
    ("10:51", Direction.DOWN_BLUE, config.INVERSE_SYMBOL),
    ("12:06", Direction.UP_RED, config.LONG_SYMBOL),
    ("12:57", Direction.DOWN_BLUE, config.INVERSE_SYMBOL),
    ("13:24", Direction.UP_RED, config.LONG_SYMBOL),
]


@dataclass
class ReplayPosition:
    symbol: str
    quantity: int
    avg_price: float


class Jul24FakeBroker:
    mode = "mock"

    def __init__(self, cash: float) -> None:
        self.cash = float(cash)
        self.position: Optional[ReplayPosition] = None
        self.fill_prices: dict[str, float] = {}
        self.orders: list[BrokerOrderResult] = []
        self.sequence: list[str] = []
        self._order_seq = 0

    def set_fill_price(self, symbol: str, price: float) -> None:
        self.fill_prices[symbol] = float(price)

    def get_orderable_cash(self, symbol: str) -> float:
        del symbol
        return self.cash

    def get_position(self, symbol: str):
        if self.position and self.position.symbol == symbol:
            return PositionSnapshot(symbol=self.position.symbol, quantity=self.position.quantity, avg_price=self.position.avg_price)
        return None

    def reconcile_position(self, symbol: str) -> int:
        return int(self.position.quantity) if self.position and self.position.symbol == symbol else 0

    def _next_order_id(self) -> str:
        self._order_seq += 1
        return f"OFFLINE-JUL24-{self._order_seq:03d}"

    def buy_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        price = self.fill_prices.get(symbol)
        self.sequence.append(f"BUY_REQUEST:{symbol}")
        if price is None or qty < 1:
            result = BrokerOrderResult(False, self._next_order_id(), symbol, "BUY", qty, 0, 0.0, "NO_FILL_PRICE")
            self.orders.append(result)
            return result
        self.cash -= price * qty
        self.position = ReplayPosition(symbol=symbol, quantity=qty, avg_price=price)
        result = BrokerOrderResult(True, self._next_order_id(), symbol, "BUY", qty, qty, price, "OK")
        self.orders.append(result)
        self.sequence.append(f"BUY_FILLED:{symbol}")
        return result

    def sell_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        price = self.fill_prices.get(symbol)
        self.sequence.append(f"SELL_REQUEST:{symbol}")
        if price is None or self.position is None or self.position.symbol != symbol or self.position.quantity < qty:
            result = BrokerOrderResult(False, self._next_order_id(), symbol, "SELL", qty, 0, 0.0, "NO_POSITION")
            self.orders.append(result)
            return result
        self.cash += price * qty
        self.position = None
        result = BrokerOrderResult(True, self._next_order_id(), symbol, "SELL", qty, qty, price, "OK")
        self.orders.append(result)
        self.sequence.append(f"SELL_FILLED:{symbol}")
        self.sequence.append(f"SELL_QTY_AFTER_ZERO:{symbol}")
        return result


def _data_dir() -> Path:
    raw = os.environ.get("MACD2_JUL24_1M_DIR")
    if not raw:
        pytest.skip("MACD2_JUL24_1M_DIR is not set; actual 2026-07-24 1m CSVs are not available offline")
    path = Path(raw)
    if not path.exists():
        pytest.skip(f"MACD2_JUL24_1M_DIR does not exist: {path}")
    return path


def _load_symbol_frame(data_dir: Path, symbol: str) -> pd.DataFrame:
    path = data_dir / f"{symbol}_1m.csv"
    if not path.exists():
        pytest.skip(f"missing actual 2026-07-24 1m CSV: {path}")
    df = pd.read_csv(path)
    required = {"datetime", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise AssertionError(f"{path} missing columns: {sorted(missing)}")
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize(KST)
    else:
        df["datetime"] = df["datetime"].dt.tz_convert(KST)
    df = df[df["datetime"].dt.strftime("%Y%m%d") == TRADING_DATE].reset_index(drop=True)
    if df.empty:
        pytest.skip(f"{path} has no {TRADING_DATE} rows")
    return df


def _minute_at(df: pd.DataFrame, hhmm: str) -> pd.Series:
    target = datetime.strptime(f"{TRADING_DATE}{hhmm}", "%Y%m%d%H:%M").replace(tzinfo=KST)
    row = df[df["datetime"] == target]
    if row.empty:
        raise AssertionError(f"missing signal-time quote row: {target.isoformat()}")
    return row.iloc[0]


def _next_open(df: pd.DataFrame, hhmm: str) -> float:
    target = datetime.strptime(f"{TRADING_DATE}{hhmm}", "%Y%m%d%H:%M").replace(tzinfo=KST) + timedelta(minutes=1)
    row = df[df["datetime"] >= target]
    if row.empty:
        raise AssertionError(f"missing next fill row at/after {target.isoformat()}")
    return float(row.iloc[0]["open"])


def _signal_id(hhmm: str, direction: Direction) -> str:
    dt = datetime.strptime(f"{TRADING_DATE}{hhmm}", "%Y%m%d%H:%M")
    return f"{dt:%Y%m%d_%H%M%S}_{direction.value}_PROVISIONAL"


def _position_snapshot(pos: Optional[ReplayPosition]) -> Optional[PositionSnapshot]:
    if pos is None:
        return None
    return PositionSnapshot(symbol=pos.symbol, quantity=pos.quantity, avg_price=pos.avg_price)


def _summaries(execution_rows: list[dict], budget: float) -> dict:
    summary = ledger.summarize_daily_trading(TRADING_DATE, budget=budget)
    return {
        "total_orders": len(execution_rows),
        "round_trips": summary["round_trip_count"],
        "gross": summary["gross_pnl"],
        "total_cost": summary["total_cost"],
        "net": summary["net_pnl"],
        "return_pct": summary["return_pct"],
        "win_rate_pct": summary["win_rate_pct"],
        "profit_factor": summary["profit_factor"],
        "mdd": summary["max_drawdown"],
    }


def test_offline_fake_order_replay_matches_jul24_expected_trade_table(tmp_path):
    data_dir = _data_dir()
    frames = {
        config.WATCH_SYMBOL: _load_symbol_frame(data_dir, config.WATCH_SYMBOL),
        config.LONG_SYMBOL: _load_symbol_frame(data_dir, config.LONG_SYMBOL),
        config.INVERSE_SYMBOL: _load_symbol_frame(data_dir, config.INVERSE_SYMBOL),
    }
    before_files = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    broker = Jul24FakeBroker(INITIAL_BUDGET)
    processed: set[str] = set()
    result_rows = []
    cumulative_net = 0.0
    cost_engine = TradeCostEngine()
    open_entry: Optional[tuple[str, int, float]] = None

    for idx, (hhmm, direction, target_symbol) in enumerate(EXPECTED_SIGNALS, start=1):
        sid = _signal_id(hhmm, direction)
        before_symbol = broker.position.symbol if broker.position else "flat"
        target_frame = frames[target_symbol]
        quote_now = float(_minute_at(frames[config.WATCH_SYMBOL], hhmm)["close"])
        fill_price = _next_open(target_frame, hhmm)
        sell_symbol = sell_qty = sell_price = sell_qty_after = ""
        gross = cost = net = 0.0

        if broker.position is not None and broker.position.symbol != target_symbol:
            held = broker.position
            sell_symbol = held.symbol
            sell_qty = held.quantity
            sell_price = _next_open(frames[held.symbol], hhmm)
            broker.set_fill_price(held.symbol, sell_price)

        broker.set_fill_price(target_symbol, fill_price)
        outcome = order_executor.execute_signal(
            broker=broker,
            direction=direction,
            signal_id=sid,
            quotes={target_symbol: fill_price},
            position=_position_snapshot(broker.position),
            budget=INITIAL_BUDGET,
            processed_signal_ids=frozenset(processed),
            reconcile_retries=1,
            reconcile_delay_sec=0.0,
        )
        if outcome.signal_id in processed:
            raise AssertionError(f"duplicate signal executed: {outcome.signal_id}")
        if outcome.final_state != SignalState.EXECUTED:
            raise AssertionError(f"{sid} failed: {outcome.final_state} {outcome.block_reason}")
        processed.add(outcome.signal_id)

        if sell_symbol:
            sell_qty_after = outcome.sell_qty_after
            assert sell_qty_after == 0
            assert open_entry is not None
            entry_symbol, entry_qty, entry_price = open_entry
            assert entry_symbol == sell_symbol
            pnl = cost_engine.compute_net_pnl(
                sell_symbol, entry_price, float(sell_price), int(sell_qty),
                buy_order_type="market", sell_order_type="market",
            )
            gross, cost, net = pnl["gross_pnl"], pnl["total_cost"], pnl["net_pnl"]
            cumulative_net += net

        buy_qty = outcome.quantity
        used = buy_qty * fill_price
        open_entry = (target_symbol, buy_qty, fill_price)
        result_rows.append({
            "seq": idx,
            "signal_bar_at": hhmm,
            "direction": direction.value,
            "signal_id": sid,
            "before": before_symbol,
            "signal_quote_000660": quote_now,
            "sell_symbol": sell_symbol,
            "sell_qty": sell_qty,
            "sell_price": sell_price,
            "sell_qty_after": sell_qty_after,
            "buy_symbol": target_symbol,
            "buy_qty": buy_qty,
            "buy_price": fill_price,
            "used_amount": used,
            "cash_after": broker.cash,
            "after": broker.position.symbol if broker.position else "flat",
            "gross_pnl": gross,
            "cost": cost,
            "net_pnl": net,
            "cumulative_net_pnl": round(cumulative_net, 2),
            "cumulative_return_pct": round(cumulative_net / INITIAL_BUDGET * 100.0, 4),
            "executor_called": True,
            "broker_called": True,
            "final_result": outcome.final_state.value,
        })

    forced_rows = []
    if broker.position is not None:
        held = broker.position
        liquidation_hhmm = "15:00"
        exit_price = _next_open(frames[held.symbol], liquidation_hhmm)
        pnl = cost_engine.compute_net_pnl(
            held.symbol, held.avg_price, exit_price, held.quantity,
            buy_order_type="market", sell_order_type="market",
        )
        forced_rows.append({
            "scenario": "15:00_FORCED_LIQUIDATION",
            "symbol": held.symbol,
            "qty": held.quantity,
            "sell_price": exit_price,
            "gross_pnl": pnl["gross_pnl"],
            "cost": pnl["total_cost"],
            "net_pnl": pnl["net_pnl"],
            "net_after_forced": round(cumulative_net + pnl["net_pnl"], 2),
        })

    assert [r["signal_bar_at"] for r in result_rows] == [s[0] for s in EXPECTED_SIGNALS]
    assert [r["direction"] for r in result_rows] == [s[1].value for s in EXPECTED_SIGNALS]
    assert [r["buy_symbol"] for r in result_rows] == [s[2] for s in EXPECTED_SIGNALS]
    assert len(processed) == 5
    assert len(processed) == len(set(processed))
    assert [r["sell_qty_after"] for r in result_rows if r["sell_symbol"]] == [0, 0, 0, 0]
    assert broker.sequence[:1] == [f"BUY_REQUEST:{config.LONG_SYMBOL}"]
    for old_symbol, new_symbol in [
        (config.LONG_SYMBOL, config.INVERSE_SYMBOL),
        (config.INVERSE_SYMBOL, config.LONG_SYMBOL),
        (config.LONG_SYMBOL, config.INVERSE_SYMBOL),
        (config.INVERSE_SYMBOL, config.LONG_SYMBOL),
    ]:
        joined = "|".join(broker.sequence)
        expected = f"SELL_REQUEST:{old_symbol}|SELL_FILLED:{old_symbol}|SELL_QTY_AFTER_ZERO:{old_symbol}|BUY_REQUEST:{new_symbol}|BUY_FILLED:{new_symbol}"
        assert expected in joined

    execution_rows = ledger.load_execution_ledger(limit=100)
    summary = _summaries(execution_rows, INITIAL_BUDGET)
    assert summary["total_orders"] == 9
    assert summary["round_trips"] == 4
    assert forced_rows and forced_rows[0]["symbol"] == config.LONG_SYMBOL
    after_files = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert before_files == [p for p in after_files if not str(p).endswith(("macd2_execution_ledger.csv", "macd2_signal_ledger.csv"))]
