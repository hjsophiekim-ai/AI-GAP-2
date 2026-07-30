#!/usr/bin/env python
"""MOCK-only verification: MACD2 flag-time correctness + confirmed-signal
origin isolation (docs/MACD2_LOGIC.md 2026-07-31 플래그 정합성 수정).

Covers, all via FakeBroker + a fake MarketDataService fetcher (never a real
broker/KIS client):

1. 13:42-style completed bar -> flag_time/signal_id use bar_start, detected_at
   only after bar_end.
2. Forming-bar candidate never calls broker/order_executor/ledger.
3. HISTORY_GAP (a 3m bar missing 1 of its 3 constituent 1m bars) blocks that
   bar's evaluation/order until the gap is backfilled.
4. compute_today_signal_overview() separates LIVE_CONFIRMED vs
   HISTORICAL_REPLAY_ONLY, purely for display (never order_executor input).
5. Ledger stats isolate the current worker_code_sha from an older one.
6. MAJOR_FLAG filter OFF/ON order-authority behavior is unchanged.
7. The same completed bar re-evaluated repeatedly never re-orders.

Every scenario runs against an isolated tmp-dir state/ledger — this script
never reads or writes the real data/state or data/logs trees, and never
constructs a REAL broker.
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

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2 import market_data as market_data_module
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST
_START = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
_QUOTES = {config.WATCH_SYMBOL: 140.0, config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


@contextmanager
def _isolated_macd2_paths():
    """Redirect state/ledger/cache paths to a throwaway tmp dir — mirrors
    tests/macd2/conftest.py's autouse fixture. Never touches real data/."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orig = (
            state_store.STATE_DIR_PATH, state_store.STATE_PATH,
            ledger.LOGS_DIR_PATH, ledger.SIGNAL_LEDGER_PATH, ledger.EXECUTION_LEDGER_PATH,
            market_data_module.CACHE_DIR,
        )
        state_store.STATE_DIR_PATH = tmp_path
        state_store.STATE_PATH = tmp_path / "macd2_runtime.json"
        ledger.LOGS_DIR_PATH = tmp_path
        ledger.SIGNAL_LEDGER_PATH = tmp_path / "macd2_signal_ledger.csv"
        ledger.EXECUTION_LEDGER_PATH = tmp_path / "macd2_execution_ledger.csv"
        market_data_module.CACHE_DIR = tmp_path / "cache"
        try:
            yield
        finally:
            (
                state_store.STATE_DIR_PATH, state_store.STATE_PATH,
                ledger.LOGS_DIR_PATH, ledger.SIGNAL_LEDGER_PATH, ledger.EXECUTION_LEDGER_PATH,
                market_data_module.CACHE_DIR,
            ) = orig


def _1m_from_3m_closes(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        bar_start = start + timedelta(minutes=3 * i)
        for j in range(3):
            rows.append({
                "datetime": bar_start + timedelta(minutes=j),
                "open": close, "high": close, "low": close, "close": close, "volume": 10,
            })
    return pd.DataFrame(rows)


_EMPTY_1M = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])


def _svc_with_quote(df_1m: pd.DataFrame, bootstrap_now: datetime, quote_prices: dict) -> MarketDataService:
    """MOCK-only MarketDataService — ``fetch_minute_candles_for_date`` (the
    prior-day warm-up fetch) is ALSO stubbed here: the default implementation
    would otherwise construct a real KIS client (docs: 000660 prior-day
    warm-up historically routes through a read-only REAL client) even in
    mode="mock". Every fetcher this script uses is a fake — never real KIS.
    """
    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_minute_candles_for_date=lambda *a: (_EMPTY_1M, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    svc.bootstrap(now=bootstrap_now)
    svc.refresh_quotes()
    return svc


def _fresh_state(*, filter_on: bool = False):
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.major_filter_enabled = filter_on
    return state


def _confirmed_up_scenario(*, filter_on: bool):
    """A single, genuinely strong UP_RED completed-bar crossover (100 -> 140
    jump) — strong enough to clear the default MAJOR_FLAG thresholds too."""
    df_1m = _1m_from_3m_closes(_START, [100.0] * 99 + [140.0])
    now = _START + timedelta(minutes=3 * 100, seconds=5)
    state = _fresh_state(filter_on=filter_on)
    state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
    broker = FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: _QUOTES[config.LONG_SYMBOL], config.INVERSE_SYMBOL: _QUOTES[config.INVERSE_SYMBOL]},
    )
    svc = _svc_with_quote(df_1m, now, _QUOTES)
    return svc, state, broker, now


def section_1_flag_time_and_signal_id() -> None:
    print("\n=== [1] flag_time / signal_id / evaluated_at (13:57-14:00 bar) ===")
    with _isolated_macd2_paths():
        svc, state, broker, now = _confirmed_up_scenario(filter_on=False)
        bar_start = _START + timedelta(minutes=3 * 99)
        bar_end = bar_start + timedelta(minutes=3)
        result = run_once(broker=broker, market_data=svc, state=state, now=now)
        _assert(result.actions == ["ENTRY:UP_RED"], "expected ENTRY:UP_RED")
        row = ledger.load_signal_ledger()[0]
        print(f"bar_start_at={bar_start.isoformat()}  bar_end_at={bar_end.isoformat()}")
        print(f"completed_bar_at={row['completed_bar_at']}  signal_id={row['signal_id']}")
        print(f"detected_at={row['detected_at']}  order_requested_at={row['order_requested_at']}")
        _assert(row["completed_bar_at"] == bar_start.strftime("%H%M%S") == "135700", "completed_bar_at must be bar_start")
        _assert("135700" in row["signal_id"], "signal_id must contain bar_start HHMMSS")
        detected = datetime.fromisoformat(row["detected_at"])
        order_requested = datetime.fromisoformat(row["order_requested_at"])
        _assert(detected >= bar_end, "detected_at must be at/after bar_end")
        _assert(order_requested >= bar_end, "order_requested_at must be at/after bar_end")
    print("PASS: flag_time/signal_id = bar_start; detected_at/order_requested_at >= bar_end")


def section_2_candidate_never_dispatches() -> None:
    print("\n=== [2] forming-bar candidate never dispatches an order ===")
    with _isolated_macd2_paths():
        start = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
        df_1m = _1m_from_3m_closes(start, [100.0] * 100)
        svc = _svc_with_quote(df_1m, df_1m["datetime"].iloc[-1] + timedelta(minutes=1), _QUOTES)
        now = start + timedelta(minutes=3 * 100, seconds=5)
        state = _fresh_state(filter_on=False)
        broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
        run_once(broker=broker, market_data=svc, state=state, now=now)  # arms the shadow candidate
        result = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))  # candidate confirmed (shadow)
        _assert(result.actions == [], "candidate must never produce an action")
        _assert(broker.orders == [], "candidate must never call broker")
        _assert(ledger.load_signal_ledger() == [], "candidate must never write to the signal ledger")
        print(f"candidate_confirmed_at={state.candidate_confirmed_at}  provisional_flag={state.provisional_flag}")
        print(f"broker_calls={len(broker.orders)}  signal_ledger_rows={len(ledger.load_signal_ledger())}")
    print("PASS: forming-bar candidate stays shadow-only, broker calls = 0")


def section_3_history_gap() -> None:
    print("\n=== [3] HISTORY_GAP blocks evaluation until backfilled ===")
    with _isolated_macd2_paths():
        full_1m = _1m_from_3m_closes(_START, [100.0] * 99 + [140.0])
        gap_minute = _START + timedelta(minutes=3 * 99 + 1)  # middle minute of the new bar
        gapped_1m = full_1m[full_1m["datetime"] != gap_minute].reset_index(drop=True)
        now = _START + timedelta(minutes=3 * 100, seconds=5)
        state = _fresh_state(filter_on=False)
        state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
        broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
        svc = _svc_with_quote(gapped_1m, now, _QUOTES)

        result = run_once(broker=broker, market_data=svc, state=state, now=now)
        _assert(result.actions == [], "gapped bar must not produce a signal")
        _assert(broker.orders == [], "gapped bar must not call broker")
        _assert(ledger.load_signal_ledger() == [], "gapped bar must not write to the signal ledger")
        _assert(state.order_block_reason == "HISTORY_GAP", "block reason must be HISTORY_GAP")
        print(f"gapped tick: order_block_reason={state.order_block_reason}  broker_calls={len(broker.orders)}")

        svc._df_1m = full_1m  # simulate the gap being backfilled by a later incremental merge
        result2 = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))
        _assert(result2.actions == ["ENTRY:UP_RED"], "backfilled bar must dispatch normally")
        print(f"after backfill: actions={result2.actions}  broker_calls={len(broker.orders)}")
    print("PASS: HISTORY_GAP blocked then resolved correctly; 주문 0건 while gapped")


def section_4_live_vs_historical_overview() -> None:
    print("\n=== [4] LIVE_CONFIRMED vs HISTORICAL_REPLAY_ONLY (recomputed overview) ===")
    df_1m = _1m_from_3m_closes(_START, [100.0] * 99 + [140.0])
    now = _START + timedelta(minutes=3 * 100, seconds=5)
    bar_start = _START + timedelta(minutes=3 * 99)
    bar_end = bar_start + timedelta(minutes=3)

    hist_overview = worker.compute_today_signal_overview(
        df_1m, now=now, session_started_at=(bar_end + timedelta(minutes=1)).isoformat(),
    )
    live_overview = worker.compute_today_signal_overview(
        df_1m, now=now, session_started_at=(bar_start - timedelta(minutes=1)).isoformat(),
    )
    hist_match = [r for r in hist_overview if r["bar_start_at"] == bar_start.isoformat()]
    live_match = [r for r in live_overview if r["bar_start_at"] == bar_start.isoformat()]
    _assert(len(hist_match) == 1 and hist_match[0]["origin"] == "HISTORICAL_REPLAY_ONLY", "must classify as HISTORICAL_REPLAY_ONLY")
    _assert(len(live_match) == 1 and live_match[0]["origin"] == "LIVE_CONFIRMED", "must classify as LIVE_CONFIRMED")
    print(f"session_started_at AFTER bar_end -> {hist_match[0]}")
    print(f"session_started_at BEFORE bar_start -> {live_match[0]}")
    print("PASS: LIVE_CONFIRMED/HISTORICAL_REPLAY_ONLY correctly separated (display-only recompute)")


def section_5_worker_code_sha_isolation() -> None:
    print("\n=== [5] current SHA 통계와 과거 SHA 통계 분리 ===")
    with _isolated_macd2_paths():
        trading_date = "20260724"
        current_sha = worker.git_sha()
        old_row = {
            "trading_date": trading_date, "completed_bar_at": "090000", "signal_id": f"{trading_date}_090000_UP_RED",
            "signal_type": "INITIAL", "direction": "UP_RED", "detected_at": "2026-07-24T09:00:05+09:00",
            "order_requested_at": "2026-07-24T09:00:05+09:00", "order_result": "EXECUTED", "block_reason": "",
            "strategy_name": config.STRATEGY_NAME, "strategy_version": config.STRATEGY_VERSION,
            "signal_rule": config.SIGNAL_RULE, "worker_code_sha": "0000000",
            "session_started_at": "2026-07-24T09:00:00+09:00",
        }
        new_row = {
            "trading_date": trading_date, "completed_bar_at": "093000", "signal_id": f"{trading_date}_093000_DOWN_BLUE",
            "signal_type": "INITIAL", "direction": "DOWN_BLUE", "detected_at": "2026-07-24T09:30:05+09:00",
            "order_requested_at": "2026-07-24T09:30:05+09:00", "order_result": "EXECUTED", "block_reason": "",
            "strategy_name": config.STRATEGY_NAME, "strategy_version": config.STRATEGY_VERSION,
            "signal_rule": config.SIGNAL_RULE, "worker_code_sha": current_sha,
            "session_started_at": "2026-07-24T09:00:00+09:00",
        }
        ledger.append_signal(old_row)
        ledger.append_signal(new_row)

        summary = ledger.summarize_signals(
            trading_date, strategy_version=config.STRATEGY_VERSION, signal_rule=config.SIGNAL_RULE,
            worker_code_sha=current_sha,
        )
        _assert(summary["red_count"] == 0, "old-sha UP_RED row must not count as current")
        _assert(summary["blue_count"] == 1, "current-sha DOWN_BLUE row must count as current")
        excluded = {r["signal_id"]: r["excluded_reason"] for r in summary["excluded_signals"]}
        _assert(excluded.get(old_row["signal_id"]) == "OLD_WORKER_SHA", "old-sha row must be excluded as OLD_WORKER_SHA")
        print(f"current_sha={current_sha!r}")
        print(f"current stats: red_count={summary['red_count']} blue_count={summary['blue_count']}")
        print(f"excluded_signals reasons={excluded}")
    print("PASS: 이전 SHA 신호는 current 통계에서 제외되고 과거/제외 신호로만 표시")


def section_6_filter_off_on() -> None:
    print("\n=== [6] 필터 OFF/ON 주문권한 ===")
    with _isolated_macd2_paths():
        off_svc, off_state, off_broker, off_now = _confirmed_up_scenario(filter_on=False)
        off_result = run_once(broker=off_broker, market_data=off_svc, state=off_state, now=off_now)
        _assert(off_result.actions == ["ENTRY:UP_RED"], "filter OFF must dispatch on a confirmed flag")
        print(f"filter OFF: actions={off_result.actions}  broker_calls={len(off_broker.orders)}")

    with _isolated_macd2_paths():
        on_svc, on_state, on_broker, on_now = _confirmed_up_scenario(filter_on=True)
        on_result = run_once(broker=on_broker, market_data=on_svc, state=on_state, now=on_now)
        _assert(on_result.actions == ["ENTRY:UP_RED"], "filter ON must dispatch when MAJOR_APPROVED")
        _assert(on_state.last_major_approved is True, "must be MAJOR_APPROVED")
        print(
            f"filter ON (approved): actions={on_result.actions}  "
            f"major_score={on_state.last_major_score}  required={on_state.last_major_required_score}"
        )

    original_min = config.MAJOR_ENTRY_SCORE_MIN
    config.MAJOR_ENTRY_SCORE_MIN = 200.0  # unreachable score -> forces rejection
    try:
        with _isolated_macd2_paths():
            rej_svc, rej_state, rej_broker, rej_now = _confirmed_up_scenario(filter_on=True)
            rej_result = run_once(broker=rej_broker, market_data=rej_svc, state=rej_state, now=rej_now)
            _assert(rej_broker.orders == [], "rejected case must not call broker")
            _assert(rej_result.actions == [f"{config.FILTERED_OUT}:UP_RED"], "rejected case must be FILTERED_OUT")
            print(f"filter ON (rejected): actions={rej_result.actions}  broker_calls={len(rej_broker.orders)}")
    finally:
        config.MAJOR_ENTRY_SCORE_MIN = original_min
    print("PASS: 필터 OFF=기존 주문 흐름 유지, 필터 ON=승인만 주문·탈락은 broker 호출 0")


def section_7_no_duplicate_signal_id() -> None:
    print("\n=== [7] 동일 signal_id 반복 tick -> 중복 주문 0건 ===")
    with _isolated_macd2_paths():
        svc, state, broker, now = _confirmed_up_scenario(filter_on=False)
        run_once(broker=broker, market_data=svc, state=state, now=now)
        for _ in range(20):
            run_once(broker=broker, market_data=svc, state=state, now=now)
        buy_orders = [o for o in broker.orders if o.side == "BUY"]
        rows = ledger.load_signal_ledger()
        _assert(len(buy_orders) == 1, f"expected exactly 1 BUY, got {len(buy_orders)}")
        _assert(len(rows) == 1, f"expected exactly 1 signal ledger row, got {len(rows)}")
        print(f"21 ticks against the same completed bar -> BUY orders={len(buy_orders)}  signal_ledger_rows={len(rows)}")
    print("PASS: 중복 signal_id 재주문 0건")


def main() -> int:
    print("=== macd2_verify_flag_time_and_origin (MOCK only, isolated tmp-dir state/ledger) ===")
    section_1_flag_time_and_signal_id()
    section_2_candidate_never_dispatches()
    section_3_history_gap()
    section_4_live_vs_historical_overview()
    section_5_worker_code_sha_isolation()
    section_6_filter_off_on()
    section_7_no_duplicate_signal_id()
    print("\nREAL order calls: 0 (FakeBroker only, never a real broker/KIS client)")
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
