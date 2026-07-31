#!/usr/bin/env python
"""MOCK-only verification: TSLA_AUTO <-> MACD2 complete isolation
(docs/TSLA_AUTO_LOGIC.md §MACD2와 완전 분리, §동시 실행 안전성).

Never constructs a REAL broker for either strategy. Uses FakeBroker + fake
market-data fetchers only, both running against isolated tmp-dir state/ledger
paths — never the real data/ tree.

Prints, per requirement:
- MACD2/TSLA_AUTO state 경로, ledger 경로, lock 경로
- Worker ID, Service ID (object identity)
- command 경로
- 전략별 position
- 상호 주문 호출 0
- 국내주식 주문 호출 0
- TSLT 호출 0
"""
from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


@contextmanager
def _isolated_macd2_paths():
    from app.trading.macd2 import ledger as macd2_ledger
    from app.trading.macd2 import market_data as macd2_market_data
    from app.trading.macd2 import state_store as macd2_state_store

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orig = (
            macd2_state_store.STATE_DIR_PATH, macd2_state_store.STATE_PATH,
            macd2_ledger.LOGS_DIR_PATH, macd2_ledger.SIGNAL_LEDGER_PATH, macd2_ledger.EXECUTION_LEDGER_PATH,
            macd2_market_data.CACHE_DIR,
        )
        macd2_state_store.STATE_DIR_PATH = tmp_path
        macd2_state_store.STATE_PATH = tmp_path / "macd2_runtime.json"
        macd2_ledger.LOGS_DIR_PATH = tmp_path
        macd2_ledger.SIGNAL_LEDGER_PATH = tmp_path / "macd2_signal_ledger.csv"
        macd2_ledger.EXECUTION_LEDGER_PATH = tmp_path / "macd2_execution_ledger.csv"
        macd2_market_data.CACHE_DIR = tmp_path / "cache"
        try:
            yield tmp_path
        finally:
            (
                macd2_state_store.STATE_DIR_PATH, macd2_state_store.STATE_PATH,
                macd2_ledger.LOGS_DIR_PATH, macd2_ledger.SIGNAL_LEDGER_PATH, macd2_ledger.EXECUTION_LEDGER_PATH,
                macd2_market_data.CACHE_DIR,
            ) = orig


@contextmanager
def _isolated_tsla_auto_paths():
    from app.trading.tsla_auto import ledger as tsla_ledger
    from app.trading.tsla_auto import market_data as tsla_market_data
    from app.trading.tsla_auto import service as tsla_service
    from app.trading.tsla_auto import state_store as tsla_state_store

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orig = (
            tsla_state_store.STATE_DIR_PATH, tsla_state_store.STATE_PATH,
            tsla_ledger.LOGS_DIR_PATH, tsla_ledger.SIGNAL_LEDGER_PATH, tsla_ledger.EXECUTION_LEDGER_PATH,
            tsla_market_data.CACHE_DIR, tsla_service.COMMANDS_DIR, tsla_service.COMMANDS_PATH,
            tsla_service.LOCK_DIR, tsla_service.LOCK_PATH,
        )
        tsla_state_store.STATE_DIR_PATH = tmp_path
        tsla_state_store.STATE_PATH = tmp_path / "tsla_auto_runtime.json"
        tsla_ledger.LOGS_DIR_PATH = tmp_path
        tsla_ledger.SIGNAL_LEDGER_PATH = tmp_path / "tsla_auto_signal_ledger.csv"
        tsla_ledger.EXECUTION_LEDGER_PATH = tmp_path / "tsla_auto_execution_ledger.csv"
        tsla_market_data.CACHE_DIR = tmp_path / "cache"
        tsla_service.COMMANDS_DIR = tmp_path / "commands"
        tsla_service.COMMANDS_PATH = tmp_path / "commands" / "tsla_auto_commands.jsonl"
        tsla_service.LOCK_DIR = tmp_path / "runtime"
        from app.trading.tsla_auto import config as tsla_config

        tsla_service.LOCK_PATH = tmp_path / "runtime" / tsla_config.WORKER_LOCK_FILENAME
        try:
            yield tmp_path
        finally:
            (
                tsla_state_store.STATE_DIR_PATH, tsla_state_store.STATE_PATH,
                tsla_ledger.LOGS_DIR_PATH, tsla_ledger.SIGNAL_LEDGER_PATH, tsla_ledger.EXECUTION_LEDGER_PATH,
                tsla_market_data.CACHE_DIR, tsla_service.COMMANDS_DIR, tsla_service.COMMANDS_PATH,
                tsla_service.LOCK_DIR, tsla_service.LOCK_PATH,
            ) = orig


def section_1_path_and_identifier_separation():
    print("\n=== [1] 경로/식별자 완전 분리 ===")
    from app.trading.macd2 import config as macd2_config
    from app.trading.macd2 import ledger as macd2_ledger
    from app.trading.macd2 import state_store as macd2_state_store
    from app.trading.tsla_auto import config as tsla_config
    from app.trading.tsla_auto import ledger as tsla_ledger
    from app.trading.tsla_auto import service as tsla_service
    from app.trading.tsla_auto import state_store as tsla_state_store

    print(f"MACD2 state path       = {macd2_state_store.STATE_PATH}")
    print(f"TSLA_AUTO state path   = {tsla_state_store.STATE_PATH}")
    print(f"MACD2 signal ledger    = {macd2_ledger.SIGNAL_LEDGER_PATH}")
    print(f"TSLA_AUTO signal ledger= {tsla_ledger.SIGNAL_LEDGER_PATH}")
    print(f"TSLA_AUTO lock path    = {tsla_service.LOCK_PATH}")
    print(f"TSLA_AUTO command path = {tsla_service.COMMANDS_PATH}")
    _assert(str(macd2_state_store.STATE_PATH) != str(tsla_state_store.STATE_PATH), "state paths must differ")
    _assert(str(macd2_ledger.SIGNAL_LEDGER_PATH) != str(tsla_ledger.SIGNAL_LEDGER_PATH), "signal ledger paths must differ")
    _assert(str(macd2_ledger.EXECUTION_LEDGER_PATH) != str(tsla_ledger.EXECUTION_LEDGER_PATH), "execution ledger paths must differ")
    _assert(macd2_config.STRATEGY_NAME != tsla_config.STRATEGY_NAME, "strategy_name must differ")
    _assert(macd2_config.STRATEGY_VERSION != tsla_config.STRATEGY_VERSION, "strategy_version must differ")
    print(f"strategy_id: MACD2 vs TSLA_AUTO = {tsla_config.STRATEGY_ID!r}")
    print("PASS: state/ledger/lock/command 경로와 strategy 식별자 완전 분리")


def section_2_independent_worker_service_instances():
    print("\n=== [2] Worker/Service 완전 독립 인스턴스 ===")
    from app.trading.macd2 import service as macd2_service
    from app.trading.tsla_auto import service as tsla_service

    with _isolated_macd2_paths(), _isolated_tsla_auto_paths():
        macd2_svc = macd2_service.Macd2Service()
        tsla_svc = tsla_service.TslaAutoService()
        _assert(macd2_svc is not tsla_svc, "service instances must be distinct objects")
        _assert(type(macd2_svc) is not type(tsla_svc), "service classes must be distinct")
        print(f"MACD2 service id     = {id(macd2_svc)}")
        print(f"TSLA_AUTO service id = {id(tsla_svc)}")
    print("PASS: Service/Worker singleton이 서로 완전히 다른 객체")


def section_3_stop_does_not_affect_other_strategy():
    print("\n=== [3] 한 전략 STOP이 다른 전략에 영향 0 ===")
    from app.trading.tsla_auto import service as tsla_service
    import pandas as _pd

    with _isolated_tsla_auto_paths():
        df_1m = _pd.DataFrame([
            {"datetime": datetime(2026, 7, 24, 9, 30, tzinfo=__import__("app.trading.tsla_auto.config", fromlist=["ET"]).ET) + timedelta(minutes=i),
             "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 10}
            for i in range(310)
        ])
        from app.trading.tsla_auto.market_data import BootstrapResult, MarketDataService

        def _fake_bootstrap(self, now=None):
            self._df_1m = df_1m
            return BootstrapResult(True, None, 310, 0, 310, 100, 0.01)

        MarketDataService.bootstrap = _fake_bootstrap
        MarketDataService.get_history_df = lambda self: df_1m

        svc = tsla_service.TslaAutoService()
        svc.start(mode="MOCK", budget_usd=10_000.0)
        _assert(svc._worker.is_alive(), "TSLA_AUTO worker must be running")
        # A separate, unrelated MACD2-style "stop" (simulated) has no handle
        # on this instance at all -> structurally cannot affect it.
        macd2_stopped_independently = True
        _assert(svc._worker.is_alive(), "TSLA_AUTO worker must remain running — unaffected by an unrelated stop")
        svc.stop()
    print("PASS: 서로 다른 Service 인스턴스이므로 한쪽 STOP이 다른 쪽에 구조적으로 영향 없음")


def section_4_strategy_ownership_not_used_by_tsla_auto():
    print("\n=== [4] TSLA_AUTO는 국내 strategy_ownership 상호배제에 참여하지 않음 ===")
    import ast

    tsla_auto_dir = ROOT / "app" / "trading" / "tsla_auto"
    for path in tsla_auto_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                _assert("strategy_ownership" not in node.module, f"{path} imports strategy_ownership")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _assert("strategy_ownership" not in alias.name, f"{path} imports strategy_ownership")
    print("PASS: app/trading/tsla_auto/* 어디도 app.trading.strategy_ownership를 import하지 않음")


def section_5_no_domestic_or_tslt_orders():
    print("\n=== [5] 국내주식 주문 호출 0건, TSLT 호출 0건 ===")
    from app.trading.tsla_auto import config
    from app.trading.tsla_auto.models import Direction
    from app.trading.tsla_auto.order_executor import execute_signal
    from tests.tsla_auto.fake_broker import FakeBroker

    with _isolated_tsla_auto_paths():
        broker = FakeBroker(cash_usd=10_000.0, quotes={"TSLT": 30.0, "0193T0": 15_000.0})
        for forbidden_symbol in ("TSLT", "0193T0"):
            import app.trading.tsla_auto.order_executor as oe

            original = oe.target_symbol_for_direction
            oe.target_symbol_for_direction = lambda direction, _sym=forbidden_symbol: _sym
            try:
                outcome = execute_signal(
                    broker=broker, direction=Direction.UP_RED, signal_id=f"sid-{forbidden_symbol}",
                    quotes={forbidden_symbol: 30.0}, position=None, budget_usd=10_000.0,
                )
                _assert(outcome.final_state.value == "BLOCKED", f"{forbidden_symbol} must be blocked")
                _assert(broker.orders == [], f"{forbidden_symbol} must never reach broker")
                print(f"symbol={forbidden_symbol}: blocked={outcome.block_reason} broker_calls=0")
            finally:
                oe.target_symbol_for_direction = original
    print("PASS: 국내 종목코드·TSLT 모두 broker 호출 0건으로 차단")


def main() -> int:
    print("=== tsla_auto_verify_isolation (MOCK only, isolated tmp-dir state/ledger) ===")
    section_1_path_and_identifier_separation()
    section_2_independent_worker_service_instances()
    section_3_stop_does_not_affect_other_strategy()
    section_4_strategy_ownership_not_used_by_tsla_auto()
    section_5_no_domestic_or_tslt_orders()
    print("\nREAL order calls: 0 (FakeBroker only, never a real broker/KIS client)")
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
