"""Verify MACD2 service startup lifecycle and stalled-worker diagnosis."""
from __future__ import annotations

import contextlib
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config, service as service_module, state_store  # noqa: E402
from app.trading.macd2.market_data import BootstrapResult  # noqa: E402
from app.trading.macd2.models import QuoteSnapshot, RuntimeStatus  # noqa: E402
from tests.macd2.fake_broker import FakeBroker  # noqa: E402

KST = config.KST


def _minute_bars(start: datetime, bars: int = 330) -> pd.DataFrame:
    rows = []
    for i in range(bars):
        dt = start + timedelta(minutes=i)
        close = 100.0 + (i // 3) * 0.1
        rows.append({"datetime": dt, "open": close, "high": close, "low": close, "close": close, "volume": 10})
    return pd.DataFrame(rows)


class FakeMarketData:
    def __init__(self, mode: str = "mock") -> None:
        self.mode = mode
        self._df = _minute_bars(datetime(2026, 7, 31, 3, 20, tzinfo=KST), 360)
        self.quote_started = False
        self.history_started = False
        self._last_bootstrap = {}

    def refresh_quotes(self):
        return None

    def start_quote_updater(self, interval_sec: float):
        self.quote_started = True

    def start_history_updater(self, interval_sec: float):
        self.history_started = True

    def stop_quote_updater(self, join_timeout: float = 0):
        self.quote_started = False

    def stop_history_updater(self, join_timeout: float = 0):
        self.history_started = False

    def quote_updater_alive(self):
        return self.quote_started

    def history_updater_alive(self):
        return self.history_started

    def bootstrap(self, now: datetime):
        self._last_bootstrap = {"ok": True, "now": now.isoformat()}
        return BootstrapResult(True, "OK", len(self._df), len(self._df), len(self._df), 120, 0.0)

    def get_history_df(self):
        return self._df

    def get_quote(self, symbol: str):
        return QuoteSnapshot(symbol, 100.0 if symbol == config.WATCH_SYMBOL else 10_000.0, datetime.now(KST), 0.0, "fake")

    def quote_statuses(self):
        return {config.WATCH_SYMBOL: "VALID", config.LONG_SYMBOL: "VALID", config.INVERSE_SYMBOL: "VALID"}

    def quote_status(self):
        return "READY"

    def quote_normalization_diag(self):
        return {}

    def get_last_bootstrap_diag(self):
        return self._last_bootstrap


@contextlib.contextmanager
def _isolated_state():
    original = (state_store.STATE_DIR_PATH, state_store.STATE_PATH)
    with tempfile.TemporaryDirectory(prefix="macd2_lifecycle_") as tmp:
        tmp_path = Path(tmp)
        state_store.STATE_DIR_PATH = tmp_path
        state_store.STATE_PATH = tmp_path / config.RUNTIME_STATE_FILENAME
        try:
            yield
        finally:
            state_store.STATE_DIR_PATH, state_store.STATE_PATH = original


@contextlib.contextmanager
def _patched_service_deps():
    original_broker = service_module.create_macd2_broker
    original_market = service_module.MarketDataService
    original_other = service_module.other_strategy_active
    service_module.create_macd2_broker = lambda mode, **kwargs: FakeBroker(cash=10_000_000.0)
    service_module.MarketDataService = FakeMarketData
    service_module.other_strategy_active = lambda: (False, "")
    try:
        yield
    finally:
        service_module.create_macd2_broker = original_broker
        service_module.MarketDataService = original_market
        service_module.other_strategy_active = original_other


def main() -> int:
    checks: dict[str, bool] = {}
    with _isolated_state(), _patched_service_deps():
        svc = service_module.Macd2Service()
        res = svc.start(mode="mock", budget=1_000_000.0)
        checks["bootstrap_complete"] = bool(res.get("ok"))
        checks["thread_alive_after_start"] = svc.supervisor_status()["worker_alive"]
        deadline = time.time() + 2.0
        while svc.supervisor_status().get("tick_n", 0) < 1 and time.time() < deadline:
            time.sleep(0.05)
        first = svc.supervisor_status()
        checks["first_tick"] = first.get("tick_n", 0) >= 1
        tick_n = first.get("tick_n", 0)
        time.sleep(0.2)
        checks["tick_continues_after_0900"] = svc.supervisor_status().get("tick_n", 0) >= tick_n

        svc._worker.stop(join_timeout=2.0)
        stalled = svc.get_snapshot()["state"]
        checks["dead_worker_not_running"] = stalled.ui_mode == RuntimeStatus.WORKER_STALLED and stalled.order_block_reason == "WORKER_THREAD_DEAD"

        retry = svc.retry_bootstrap()
        deadline = time.time() + 2.0
        while svc.supervisor_status().get("tick_n", 0) < 1 and time.time() < deadline:
            time.sleep(0.05)
        checks["restart_worker_alive"] = bool(retry.get("ok")) and svc.supervisor_status()["worker_alive"]
        svc.stop("verify_done")

    print("=== MACD2 startup lifecycle verification ===")
    for key, value in checks.items():
        print(f"{key}: {'PASS' if value else 'FAIL'}")
    ok = all(checks.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
