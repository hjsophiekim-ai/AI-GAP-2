"""Verify the MACD2 12:45 DOWN_BLUE switch path with fake broker/replay data."""
from __future__ import annotations

import contextlib
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config, ledger, worker  # noqa: E402
from app.trading.macd2.market_data import MarketDataService  # noqa: E402
from app.trading.macd2.models import Direction, MajorFlagDecision, QuoteSnapshot, RuntimeState  # noqa: E402
from tests.macd2.fake_broker import FakeBroker  # noqa: E402

KST = config.KST


def _load(symbol_name: str) -> pd.DataFrame:
    frames = []
    for ymd in ("20260730", "20260731"):
        frame = pd.read_csv(ROOT / "data" / "cache" / f"replay_{ymd}_{symbol_name}_1m.csv")
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="raise").dt.tz_localize(KST)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)


class ReplayMarketData:
    def __init__(self, hynix: pd.DataFrame, long_df: pd.DataFrame, inverse_df: pd.DataFrame) -> None:
        self.hynix = hynix
        self.long_df = long_df
        self.inverse_df = inverse_df
        self.now = datetime(2026, 7, 31, 11, 30, tzinfo=KST)

    def set_now(self, now: datetime) -> None:
        self.now = now

    def get_history_df(self) -> pd.DataFrame:
        return self.hynix[self.hynix["datetime"] < self.now].reset_index(drop=True)

    def _price(self, frame: pd.DataFrame) -> float:
        rows = frame[frame["datetime"] < self.now]
        return float(rows.iloc[-1]["close"])

    def get_quote(self, symbol: str):
        if symbol == config.WATCH_SYMBOL:
            price = self._price(self.hynix)
        elif symbol == config.LONG_SYMBOL:
            price = self._price(self.long_df)
        elif symbol == config.INVERSE_SYMBOL:
            price = self._price(self.inverse_df)
        else:
            return None
        return QuoteSnapshot(symbol, price, self.now, 0.0, "replay", None)

    def quote_statuses(self, symbols=None):
        return {s: "VALID" for s in (symbols or (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL))}

    def quote_normalization_diag(self):
        return {}


@contextlib.contextmanager
def _isolated_ledger():
    original = (ledger.LOGS_DIR_PATH, ledger.EXECUTION_LEDGER_PATH, ledger.SIGNAL_LEDGER_PATH)
    with tempfile.TemporaryDirectory(prefix="macd2_1245_switch_") as tmp:
        tmp_path = Path(tmp)
        ledger.LOGS_DIR_PATH = tmp_path
        ledger.EXECUTION_LEDGER_PATH = tmp_path / "execution.csv"
        ledger.SIGNAL_LEDGER_PATH = tmp_path / "signals.csv"
        try:
            yield
        finally:
            ledger.LOGS_DIR_PATH, ledger.EXECUTION_LEDGER_PATH, ledger.SIGNAL_LEDGER_PATH = original


@contextlib.contextmanager
def _patched_major(decision_factory: Callable[[Direction], MajorFlagDecision] | None):
    if decision_factory is None:
        yield
        return
    original_eval = worker.major_flag_filter.evaluate_major_flag
    original_apply = worker.major_flag_filter.apply_major_trade_gates

    def fake_evaluate(*args, **kwargs):
        direction = kwargs.get("flag_direction") or args[1]
        return decision_factory(Direction(direction))

    def fake_apply(decision, **kwargs):
        return decision

    worker.major_flag_filter.evaluate_major_flag = fake_evaluate
    worker.major_flag_filter.apply_major_trade_gates = fake_apply
    try:
        yield
    finally:
        worker.major_flag_filter.evaluate_major_flag = original_eval
        worker.major_flag_filter.apply_major_trade_gates = original_apply


def _state(filter_on: bool) -> RuntimeState:
    state = RuntimeState(auto_trade_on=True, mode="mock", budget=10_000_000.0)
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.major_filter_enabled = False
    state.last_confirmed_bar_ts = datetime(2026, 7, 31, 11, 18, tzinfo=KST).isoformat()
    state.last_detected_direction = Direction.DOWN_BLUE
    state.macd_color_last_regime = "POSITIVE_REGIME_BLUE"
    return state


def _run_case(name: str, *, filter_on: bool, decision_factory=None) -> tuple[bool, dict]:
    hynix = _load("hynix")
    long_df = _load("long")
    inverse_df = _load("inverse")
    market = ReplayMarketData(hynix, long_df, inverse_df)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 11870.0, config.INVERSE_SYMBOL: 13000.0})
    state = _state(filter_on)
    reports = []

    with _isolated_ledger(), _patched_major(decision_factory):
        for tick_now in (
            datetime(2026, 7, 31, 11, 24, 2, tzinfo=KST),
            datetime(2026, 7, 31, 11, 27, 2, tzinfo=KST),
            datetime(2026, 7, 31, 11, 30, 2, tzinfo=KST),
            *[datetime(2026, 7, 31, 12, minute, 2, tzinfo=KST) for minute in range(0, 49, 3)],
        ):
            if tick_now >= datetime(2026, 7, 31, 12, 0, tzinfo=KST):
                state.major_filter_enabled = filter_on
            market.set_now(tick_now)
            for symbol in (config.LONG_SYMBOL, config.INVERSE_SYMBOL):
                q = market.get_quote(symbol)
                broker.set_quote(symbol, q.price)
            result = worker.run_once(broker=broker, market_data=market, state=state, now=tick_now)
            if result.actions:
                reports.append((tick_now.isoformat(), list(result.actions), state.latest_primary_signal_id))
        rows = ledger.load_signal_ledger(limit=1000)

    sell_orders = [o for o in broker.orders if o.side == "SELL"]
    buy_orders = [o for o in broker.orders if o.side == "BUY"]
    long_pos = broker.get_position(config.LONG_SYMBOL)
    inverse_pos = broker.get_position(config.INVERSE_SYMBOL)
    summary = {
        "case": name,
        "actions": reports,
        "ledger": [(r.get("signal_id"), r.get("direction"), r.get("order_result"), r.get("block_reason")) for r in rows],
        "sell_orders": [(o.order_id, o.symbol, o.requested_qty, o.executed_qty, o.success) for o in sell_orders],
        "buy_orders": [(o.order_id, o.symbol, o.requested_qty, o.executed_qty, o.success) for o in buy_orders],
        "long_qty": 0 if long_pos is None else long_pos.quantity,
        "inverse_qty": 0 if inverse_pos is None else inverse_pos.quantity,
    }
    switched_1245 = any("20260731_124500_DOWN_BLUE" in str(row[0]) for row in summary["ledger"])
    no_early_switch = not any("20260731_123900_DOWN_BLUE" in str(row[0]) for row in summary["ledger"])
    no_dual = not (summary["long_qty"] > 0 and summary["inverse_qty"] > 0)
    if name == "filter_rejected":
        ok = (
            switched_1245
            and no_early_switch
            and len(sell_orders) == 0
            and len(buy_orders) == 1
            and summary["long_qty"] > 0
            and summary["inverse_qty"] == 0
        )
    else:
        ok = (
            switched_1245
            and no_early_switch
            and len(sell_orders) == 1
            and sell_orders[0].symbol == config.LONG_SYMBOL
            and sell_orders[0].order_id
            and sell_orders[0].executed_qty == sell_orders[0].requested_qty
            and len([o for o in buy_orders if o.symbol == config.INVERSE_SYMBOL]) == 1
            and summary["long_qty"] == 0
            and summary["inverse_qty"] > 0
            and no_dual
        )
    return ok, summary


def _approved(direction: Direction) -> MajorFlagDecision:
    return MajorFlagDecision(True, 100.0, 65.0, config.MAJOR_APPROVED, ("forced approval",), {}, {}, False, False)


def _rejected(direction: Direction) -> MajorFlagDecision:
    return MajorFlagDecision(
        False, 0.0, 65.0, config.MAJOR_SCORE_BELOW_THRESHOLD,
        ("forced rejection",), {}, {}, False, False,
        block_reason=config.MAJOR_SCORE_BELOW_THRESHOLD,
    )


def main() -> int:
    cases = [
        ("filter_off", {"filter_on": False, "decision_factory": None}),
        ("filter_approved", {"filter_on": True, "decision_factory": _approved}),
        ("filter_rejected", {"filter_on": True, "decision_factory": _rejected}),
    ]
    all_ok = True
    for name, kwargs in cases:
        ok, summary = _run_case(name, **kwargs)
        all_ok = all_ok and ok
        print(f"=== {name} ===")
        print(summary)
        print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
