#!/usr/bin/env python
"""TSLA_AUTO offline replay driver (docs §18 Phase 3 — replay·shadow 검증).

Steps run_once() across a full synthetic trading day (09:30-16:00 ET) at
WORKER_INTERVAL_SEC cadence, using FakeBroker + a fixed synthetic 1-minute
dataset (never real KIS calls, never a real broker). Prints every signal
dispatched (confirmed flags, strong-filter decisions, orders) and a final
summary — a dry run to sanity-check the whole day's behavior before any
real-money use.

Never writes to the real data/ tree (isolated tmp-dir state/ledger) and
never places a REAL order.
"""
from __future__ import annotations

import math
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.tsla_auto import config, ledger, state_store
from app.trading.tsla_auto import market_data as market_data_module
from app.trading.tsla_auto.market_data import MarketDataService
from app.trading.tsla_auto.worker import run_once
from tests.tsla_auto.fake_broker import FakeBroker

ET = config.ET
_DAY = datetime(2026, 7, 24, 9, 30, tzinfo=ET)  # a normal trading day, no holiday/early-close


@contextmanager
def _isolated_paths():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orig = (
            state_store.STATE_DIR_PATH, state_store.STATE_PATH, ledger.LOGS_DIR_PATH,
            ledger.SIGNAL_LEDGER_PATH, ledger.EXECUTION_LEDGER_PATH, market_data_module.CACHE_DIR,
        )
        state_store.STATE_DIR_PATH = tmp_path
        state_store.STATE_PATH = tmp_path / "tsla_auto_runtime.json"
        ledger.LOGS_DIR_PATH = tmp_path
        ledger.SIGNAL_LEDGER_PATH = tmp_path / "tsla_auto_signal_ledger.csv"
        ledger.EXECUTION_LEDGER_PATH = tmp_path / "tsla_auto_execution_ledger.csv"
        market_data_module.CACHE_DIR = tmp_path / "cache"
        try:
            yield
        finally:
            (
                state_store.STATE_DIR_PATH, state_store.STATE_PATH, ledger.LOGS_DIR_PATH,
                ledger.SIGNAL_LEDGER_PATH, ledger.EXECUTION_LEDGER_PATH, market_data_module.CACHE_DIR,
            ) = orig


def _synthetic_session_1m(start: datetime, n_minutes: int = 391, amplitude: float = 30.0) -> pd.DataFrame:
    """A sine-wave TSLA price path across the whole regular session —
    guarantees multiple real UP_RED/DOWN_BLUE crossovers for the replay to
    react to (never a hand-picked "answer" bar — a generic oscillation)."""
    period = max(n_minutes // 4, 1)
    rows = []
    for i in range(n_minutes):
        close = round(300.0 + amplitude * math.sin(2 * math.pi * i / period), 4)
        rows.append({
            "datetime": start + timedelta(minutes=i), "open": close, "high": close + 0.2,
            "low": close - 0.2, "close": close, "volume": 1000 + (i % 50) * 20,
        })
    return pd.DataFrame(rows)


def main() -> int:
    print("=== tsla_auto_replay (offline dry-run, FakeBroker + synthetic sine-wave TSLA session) ===")
    with _isolated_paths():
        df_1m = _synthetic_session_1m(_DAY)
        long_price, inverse_price = 30.0, 12.0
        quote_prices = {config.SIGNAL_SYMBOL: df_1m["close"].iloc[-1], config.LONG_SYMBOL: long_price, config.INVERSE_SYMBOL: inverse_price}
        svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (df_1m, {}), fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None))
        bootstrap_now = _DAY + timedelta(minutes=len(df_1m) + 5)
        boot = svc.bootstrap(now=bootstrap_now)
        print(f"bootstrap: ok={boot.ok} reason={boot.reason} completed_3m_count={boot.completed_3m_count}")
        svc.refresh_quotes()

        state = state_store.default_state()
        state.auto_trade_on = True
        state.budget_usd = 100_000.0
        state.strategy_name = config.STRATEGY_NAME
        state.strategy_version = config.STRATEGY_VERSION
        state.signal_rule = config.SIGNAL_RULE
        broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: long_price, config.INVERSE_SYMBOL: inverse_price})

        session_end = _DAY + timedelta(minutes=len(df_1m) - 1)
        # Step by 3 minutes (one completed-bar cadence) rather than the live
        # 5-second WORKER_INTERVAL_SEC — a full session at 5s ticks is ~4700
        # iterations of redundant same-bar re-evaluation; 3-min steps still
        # exercise every completed bar exactly once, matching how the real
        # Worker's bar-once dedup makes intra-bar ticks a no-op anyway.
        step = timedelta(minutes=3)
        now = _DAY + timedelta(minutes=27)  # after EMA_SLOW=26 warm-up bars
        tick_count = 0
        while now <= session_end:
            result = run_once(broker=broker, market_data=svc, state=state, now=now)
            if result.actions:
                print(f"{now.strftime('%H:%M:%S')} ET -> actions={result.actions} regime={state.market_regime} position={state.position}")
            tick_count += 1
            now += step

        print(f"\ntotal ticks={tick_count}")
        print(f"final position={state.position}")
        print(f"daily_entry_count={state.daily_entry_count}")
        print(f"processed_signal_ids={len(state.processed_signal_ids)}")
        rows = ledger.load_signal_ledger()
        print(f"signal ledger rows={len(rows)}")
        for row in rows:
            print(f"  {row['signal_id']} direction={row['direction']} order_result={row['order_result']} origin={row['origin']}")
        exec_rows = ledger.load_execution_ledger()
        print(f"execution ledger rows={len(exec_rows)}")

    print("\nREAL order calls: 0 (FakeBroker only, never a real broker/KIS client)")
    print("ALL REPLAY TICKS COMPLETED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
