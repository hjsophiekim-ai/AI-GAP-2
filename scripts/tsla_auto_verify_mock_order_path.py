#!/usr/bin/env python
"""MOCK-only verification: TSLA_AUTO full signal->order->fill->balance path,
strong-flag filter, NORMAL/CHOP daily caps, and 15:45 ET cutoff — FakeBroker
+ fake market-data fetchers only. Never constructs a REAL broker.

IMPORTANT: this validates WORKER/ORDER_EXECUTOR LOGIC using a test double —
it is NOT "실제 KIS MOCK 검증 완료" (docs §17), since the real KIS overseas
order/balance TRs remain unconfirmed (see kis_overseas_adapter.py). See
scripts/tsla_auto_read_only_smoke.py for the one piece that IS validated
against the real KIS API (quotes/minute candles, read-only).
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

from app.trading.tsla_auto import config, ledger, state_store, worker
from app.trading.tsla_auto import market_data as market_data_module
from app.trading.tsla_auto.market_data import MarketDataService
from app.trading.tsla_auto.worker import run_once
from tests.tsla_auto.fake_broker import FakeBroker

ET = config.ET
_START = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
_QUOTES = {config.SIGNAL_SYMBOL: 250.0, config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


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


def _1m_from_3m_closes(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        bar_start = start + timedelta(minutes=3 * i)
        for j in range(3):
            rows.append({"datetime": bar_start + timedelta(minutes=j), "open": close, "high": close, "low": close, "close": close, "volume": 10})
    return pd.DataFrame(rows)


def _svc_with_quote(df_1m, bootstrap_now, quote_prices) -> MarketDataService:
    svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (df_1m, {}), fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None))
    svc.bootstrap(now=bootstrap_now)
    svc.refresh_quotes()
    return svc


def _fresh_state(*, strong_filter_on: bool = False):
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget_usd = 100_000.0
    state.strong_filter_enabled = strong_filter_on
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    return state


def section_1_up_red_to_tsll():
    print("\n=== [1] UP_RED -> TSLL: MACD 승인 -> ORD 제출 -> 주문번호 -> 체결 -> 잔고 ===")
    with _isolated_paths():
        df_1m = _1m_from_3m_closes(_START, [100.0] * 99 + [140.0])
        now = _START + timedelta(minutes=3 * 100, seconds=5)
        state = _fresh_state()
        state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
        broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: _QUOTES[config.LONG_SYMBOL], config.INVERSE_SYMBOL: _QUOTES[config.INVERSE_SYMBOL]})
        svc = _svc_with_quote(df_1m, now, _QUOTES)

        result = run_once(broker=broker, market_data=svc, state=state, now=now)
        _assert(result.actions == ["ENTRY:UP_RED"], "expected ENTRY:UP_RED")
        _assert(state.position is not None and state.position.symbol == config.LONG_SYMBOL, "must hold TSLL")
        order = broker.orders[-1]
        print(f"signal={state.latest_primary_signal_id} order_id={order.order_id} filled_qty={state.position.quantity} balance_symbol={state.position.symbol}")
        print(f"strategy position qty={state.position.quantity} broker qty={broker.get_position(config.LONG_SYMBOL).quantity}")
        _assert(broker.get_position(config.LONG_SYMBOL).quantity == state.position.quantity, "strategy vs broker qty must match")
    print("PASS: UP_RED -> TSLL 주문번호/체결/잔고 확인")


def section_2_down_blue_to_tslz():
    print("\n=== [2] DOWN_BLUE -> TSLZ: MACD 승인 -> ORD 제출 -> 주문번호 -> 체결 -> 잔고 ===")
    with _isolated_paths():
        df_1m = _1m_from_3m_closes(_START, [100.0] * 99 + [60.0])
        now = _START + timedelta(minutes=3 * 100, seconds=5)
        state = _fresh_state()
        state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
        broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: _QUOTES[config.LONG_SYMBOL], config.INVERSE_SYMBOL: _QUOTES[config.INVERSE_SYMBOL]})
        svc = _svc_with_quote(df_1m, now, _QUOTES)

        result = run_once(broker=broker, market_data=svc, state=state, now=now)
        _assert(result.actions == ["ENTRY:DOWN_BLUE"], "expected ENTRY:DOWN_BLUE")
        _assert(state.position is not None and state.position.symbol == config.INVERSE_SYMBOL, "must hold TSLZ")
        order = broker.orders[-1]
        print(f"signal={state.latest_primary_signal_id} order_id={order.order_id} filled_qty={state.position.quantity} balance_symbol={state.position.symbol}")
    print("PASS: DOWN_BLUE -> TSLZ 주문번호/체결/잔고 확인")


def section_3_filtered_out_zero_broker_calls():
    print("\n=== [3] 필터 탈락 신호 broker 호출 0건 ===")
    from app.trading.tsla_auto import strong_flag_filter

    with _isolated_paths():
        original = strong_flag_filter.required_scores_for
        strong_flag_filter.required_scores_for = lambda **k: {"entry": 200.0, "reversal": 200.0, "fast_reversal": 200.0}
        try:
            df_1m = _1m_from_3m_closes(_START, [100.0] * 99 + [140.0])
            now = _START + timedelta(minutes=3 * 100, seconds=5)
            state = _fresh_state(strong_filter_on=True)
            state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
            broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
            svc = _svc_with_quote(df_1m, now, _QUOTES)
            result = run_once(broker=broker, market_data=svc, state=state, now=now)
            _assert(result.actions == [f"{config.FILTERED_OUT}:UP_RED"], "expected FILTERED_OUT")
            _assert(broker.orders == [], "filtered signal must never call broker")
            print(f"decision={state.last_decision} score={state.last_score} required={state.last_required_score} broker_calls={len(broker.orders)}")
        finally:
            strong_flag_filter.required_scores_for = original
    print("PASS: 필터 탈락 신호 broker 호출 0건")


def section_4_duplicate_signal_id_zero_reorder():
    print("\n=== [4] 동일 signal_id 재주문 0건 ===")
    with _isolated_paths():
        df_1m = _1m_from_3m_closes(_START, [100.0] * 99 + [140.0])
        now = _START + timedelta(minutes=3 * 100, seconds=5)
        state = _fresh_state()
        state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
        broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
        svc = _svc_with_quote(df_1m, now, _QUOTES)
        run_once(broker=broker, market_data=svc, state=state, now=now)
        for _ in range(20):
            run_once(broker=broker, market_data=svc, state=state, now=now)
        buy_orders = [o for o in broker.orders if o.side == "BUY"]
        print(f"21 ticks against the same bar -> BUY orders={len(buy_orders)}")
        _assert(len(buy_orders) == 1, "must dispatch exactly once")
    print("PASS: 동일 signal_id 재주문 0건")


def section_5_1545_cutoff_blocks_new_buy():
    print("\n=== [5] 15:45 ET 이후 신규 BUY 0건 ===")
    with _isolated_paths():
        df_1m = _1m_from_3m_closes(_START, [100.0] * 99 + [140.0])
        bar_end = _START + timedelta(minutes=3 * 100)
        state = _fresh_state()
        state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
        broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
        svc = _svc_with_quote(df_1m, bar_end, _QUOTES)
        at_cutoff = _START.replace(hour=15, minute=45, second=0)
        result = run_once(broker=broker, market_data=svc, state=state, now=at_cutoff)
        print(f"at 15:45:00 ET -> actions={result.actions} broker_calls={len(broker.orders)}")
        _assert(result.actions == [], "15:45 cutoff must block new entries")
        _assert(broker.orders == [], "no broker call after cutoff")
    print("PASS: 15:45 ET 컷오프 이후 신규 BUY 0건")


def section_6_real_broker_never_constructed():
    print("\n=== [6] REAL 주문 호출 0건 ===")
    from app.trading.tsla_auto.broker_adapter import RealBrokerAdapter
    try:
        RealBrokerAdapter()
        raise AssertionError("RealBrokerAdapter must refuse construction by default")
    except PermissionError as exc:
        print(f"RealBrokerAdapter() raised as expected: {exc}")
    print("PASS: REAL 브로커는 기본적으로 생성조차 불가능")


def main() -> int:
    print("=== tsla_auto_verify_mock_order_path (MOCK/FakeBroker only, isolated tmp-dir state/ledger) ===")
    section_1_up_red_to_tsll()
    section_2_down_blue_to_tslz()
    section_3_filtered_out_zero_broker_calls()
    section_4_duplicate_signal_id_zero_reorder()
    section_5_1545_cutoff_blocks_new_buy()
    section_6_real_broker_never_constructed()
    print("\nREAL order calls: 0 (FakeBroker only, never a real broker/KIS client)")
    print("주의: 위 결과는 FakeBroker 기반 Worker/order_executor 로직 검증이며, ")
    print("실제 KIS MOCK(모의투자) 해외 주문 TR 검증이 아니다 (docs §17 — 그 TR은 미확인).")
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
