"""Unit tests for app.trading.tsla_auto.worker — FakeBroker + fake market data only."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.tsla_auto import config, ledger, order_executor, state_store, worker
from app.trading.tsla_auto.market_data import MarketDataService
from app.trading.tsla_auto.models import Direction, PositionSnapshot, QuoteSnapshot
from app.trading.tsla_auto.worker import run_once
from tests.tsla_auto.fake_broker import FakeBroker

ET = config.ET
_START = datetime(2026, 7, 24, 9, 30, tzinfo=ET)  # a normal Friday, no holiday/early-close


def _1m_from_3m_closes(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        bar_start = start + timedelta(minutes=3 * i)
        for j in range(3):
            rows.append({
                "datetime": bar_start + timedelta(minutes=j), "open": close, "high": close,
                "low": close, "close": close, "volume": 10,
            })
    return pd.DataFrame(rows)


def _svc_with_quote(df_1m: pd.DataFrame, bootstrap_now: datetime, quote_prices: dict) -> MarketDataService:
    svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (df_1m, {}), fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None))
    svc.bootstrap(now=bootstrap_now)
    svc.refresh_quotes()
    return svc


def _fresh_state(*, strong_filter_on: bool = False) -> "RuntimeState":
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget_usd = 100_000.0
    state.strong_filter_enabled = strong_filter_on
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    return state


_QUOTES = {config.SIGNAL_SYMBOL: 250.0, config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0}


def _confirmed_up_scenario(*, strong_filter_on: bool = False, quotes: dict = None):
    quote_prices = {**_QUOTES, **(quotes or {})}
    df_1m = _1m_from_3m_closes(_START, [100.0] * 97 + [99.0, 100.0, 140.0])
    now = _START + timedelta(minutes=3 * 100, seconds=5)
    state = _fresh_state(strong_filter_on=strong_filter_on)
    state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
    broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: quote_prices[config.LONG_SYMBOL], config.INVERSE_SYMBOL: quote_prices[config.INVERSE_SYMBOL]})
    svc = _svc_with_quote(df_1m, now, quote_prices)
    return svc, state, broker, now


def test_run_once_skipped_when_auto_trade_off():
    df_1m = _1m_from_3m_closes(_START, [100.0] * 100)
    svc = _svc_with_quote(df_1m, _START + timedelta(minutes=310), _QUOTES)
    state = _fresh_state()
    state.auto_trade_on = False
    broker = FakeBroker(cash_usd=100_000.0)
    result = run_once(broker=broker, market_data=svc, state=state, now=_START + timedelta(minutes=310))
    assert result.skipped == "auto_trade_off"
    assert broker.orders == []


def test_run_once_not_ready_before_warmup():
    svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (pd.DataFrame(), {}))
    state = _fresh_state()
    broker = FakeBroker(cash_usd=100_000.0)
    result = run_once(broker=broker, market_data=svc, state=state, now=_START + timedelta(minutes=5))
    assert result.skipped == "NOT_READY"
    assert state.warmup_ready is False


def test_flat_entry_up_red_buys_tsll():
    svc, state, broker, now = _confirmed_up_scenario()
    result = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert result.actions == ["ENTRY:UP_RED"]
    assert state.position is not None
    assert state.position.symbol == config.LONG_SYMBOL
    assert broker.orders[-1].symbol == config.LONG_SYMBOL
    row = ledger.load_signal_ledger()[0]
    assert row["origin"] == config.ORIGIN_LIVE_CONFIRMED
    assert row["completed_bar_at"] == "142700"
    assert "142700" in row["signal_id"]


def test_flat_entry_down_blue_buys_tslz():
    df_1m = _1m_from_3m_closes(_START, [100.0] * 97 + [101.0, 100.0, 60.0])
    now = _START + timedelta(minutes=3 * 100, seconds=5)
    state = _fresh_state()
    state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
    broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
    svc = _svc_with_quote(df_1m, now, _QUOTES)
    result = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert result.actions == ["ENTRY:DOWN_BLUE"]
    assert state.position.symbol == config.INVERSE_SYMBOL


def test_same_completed_bar_is_never_reevaluated():
    svc, state, broker, now = _confirmed_up_scenario()
    run_once(broker=broker, market_data=svc, state=state, now=now)
    first_processed = list(state.processed_signal_ids)
    result2 = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert state.processed_signal_ids == first_processed
    assert not result2.actions


def test_duplicate_signal_id_never_reexecuted_across_many_ticks():
    svc, state, broker, now = _confirmed_up_scenario()
    run_once(broker=broker, market_data=svc, state=state, now=now)
    orders_after_first = len(broker.orders)
    for _ in range(20):
        run_once(broker=broker, market_data=svc, state=state, now=now)
    assert len(broker.orders) == orders_after_first


def test_entry_blocked_before_market_open():
    df_1m = _1m_from_3m_closes(_START, [100.0] * 100)
    svc = _svc_with_quote(df_1m, _START + timedelta(minutes=310), _QUOTES)
    state = _fresh_state()
    broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
    early_now = _START.replace(hour=9, minute=0)
    result = run_once(broker=broker, market_data=svc, state=state, now=early_now)
    assert state.position is None
    assert not any(a.startswith("ENTRY:") for a in result.actions)


def test_entry_allowed_at_1544_59_blocked_at_1545_00():
    """docs §6/§11: 15:44:59 신규진입 가능, 15:45:00 신규진입 0."""
    df_1m = _1m_from_3m_closes(_START, [100.0] * 97 + [99.0, 100.0, 140.0])
    bar_end = _START + timedelta(minutes=3 * 100)  # the new bar completes here
    state_before = _fresh_state()
    state_before.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
    broker_before = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
    svc_before = _svc_with_quote(df_1m, bar_end, _QUOTES)
    just_before_cutoff = _START.replace(hour=15, minute=44, second=59)
    result_before = run_once(broker=broker_before, market_data=svc_before, state=state_before, now=just_before_cutoff)
    assert result_before.actions == ["ENTRY:UP_RED"]

    state_at = _fresh_state()
    state_at.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
    broker_at = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
    svc_at = _svc_with_quote(df_1m, bar_end, _QUOTES)
    at_cutoff = _START.replace(hour=15, minute=45, second=0)
    result_at = run_once(broker=broker_at, market_data=svc_at, state=state_at, now=at_cutoff)
    assert result_at.actions == []
    assert broker_at.orders == []


def test_forced_liquidation_at_1550_overrides_everything():
    svc, state, broker, now = _confirmed_up_scenario()
    run_once(broker=broker, market_data=svc, state=state, now=now)  # establish TSLL position
    assert state.position is not None

    liquidate_now = _START.replace(hour=15, minute=50, second=0)
    result = run_once(broker=broker, market_data=svc, state=state, now=liquidate_now)
    assert result.actions == [f"FORCED_LIQUIDATION:{config.LONG_SYMBOL}"]
    assert state.position is None


def test_stop_loss_exits_full_position():
    svc, state, broker, now = _confirmed_up_scenario()
    run_once(broker=broker, market_data=svc, state=state, now=now)
    entry_price = state.position.avg_price
    # crash TSLL price so net_return <= -1.5%
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, entry_price * 0.90, datetime.now(ET), 0.0, "test", None)
    result = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))
    assert result.actions == [f"STOP_LOSS:{config.LONG_SYMBOL}"]
    assert state.position is None
    assert state.stop_loss_cooldown_direction == Direction.UP_RED
    assert state.last_stop_loss_exit_at is not None


def test_stop_loss_falls_back_to_stale_quote_when_fresh_quote_missing():
    """MACD2 parity (2026-08-04): STOP_LOSS is a risk-safety check on an
    ALREADY-held position, so a quote that misses the strict freshness
    window must never silently skip it — falls back to the last known
    (stale) price instead of leaving the position unmonitored."""
    svc, state, broker, now = _confirmed_up_scenario()
    run_once(broker=broker, market_data=svc, state=state, now=now)
    entry_price = state.position.avg_price
    # A real, but STALE (fetched 100 real seconds ago > QUOTE_MAX_AGE_SEC)
    # quote showing a -3% crash -- _fresh_quote_prices excludes it from
    # `quotes`, so only the market_data.get_quote() fallback can catch this.
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(
        config.LONG_SYMBOL, entry_price * 0.97, datetime.now(ET) - timedelta(seconds=100), None, "test", None,
    )
    result = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))
    assert result.actions == [f"STOP_LOSS:{config.LONG_SYMBOL}"]
    assert state.position is None


def test_quick_profit_requires_both_stale_peak_and_live_price_to_clear_bar():
    """MACD2 parity (2026-08-04): Quick-Profit must never fire off a
    same-minute peak that has already reversed by execution time -- the
    live price must ALSO independently clear +1.5%, or a 'take profit'
    could sell at/below entry."""
    svc, state, broker, now = _confirmed_up_scenario()
    state.quick_profit_enabled = True
    run_once(broker=broker, market_data=svc, state=state, now=now)
    entry_price = state.position.avg_price

    tick_now = now + timedelta(seconds=5)
    # Seed a stale same-minute peak far above the +1.5% bar...
    state.quick_profit_minute_symbol = config.LONG_SYMBOL
    state.quick_profit_minute_bucket = tick_now.astimezone(ET).replace(second=0, microsecond=0).isoformat()
    state.quick_profit_minute_high = entry_price * 1.05
    # ...but the LIVE price has already reverted to roughly breakeven.
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, entry_price * 1.001, datetime.now(ET), 0.0, "test", None)
    outcome = run_once(broker=broker, market_data=svc, state=state, now=tick_now)
    assert "QUICK_PROFIT_TAKE_PROFIT:TSLL" not in outcome.actions
    assert state.position is not None

    # Now the LIVE price also genuinely clears +1.5% -- the exit fires.
    later_now = tick_now + timedelta(seconds=5)
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, entry_price * 1.02, datetime.now(ET), 0.0, "test", None)
    outcome2 = run_once(broker=broker, market_data=svc, state=state, now=later_now)
    assert outcome2.actions == [f"QUICK_PROFIT_TAKE_PROFIT:{config.LONG_SYMBOL}"]
    assert state.position is None


def test_profit_lock_tracks_giveback_but_exit_is_disabled():
    svc, state, broker, now = _confirmed_up_scenario()
    run_once(broker=broker, market_data=svc, state=state, now=now)
    entry_price = state.position.avg_price
    # rally to activate profit lock (+1.5%), then give back > 0.8pp
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, entry_price * 1.03, datetime.now(ET), 0.0, "test", None)
    run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))
    assert state.profit_lock_active is True
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, entry_price * 1.015, datetime.now(ET), 0.0, "test", None)
    result = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=10))
    assert result.actions == []
    assert state.position is not None


def test_opposite_signal_takes_priority_over_profit_lock_giveback():
    """docs §12 — TSLA_AUTO's OWN priority order (differs from MACD2): Profit
    Lock exits BEFORE an approved opposite-signal switch is even considered."""
    svc, state, broker, now = _confirmed_up_scenario()
    run_once(broker=broker, market_data=svc, state=state, now=now)  # TSLL position established
    entry_price = state.position.avg_price
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, entry_price * 1.03, datetime.now(ET), 0.0, "test", None)
    run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))
    assert state.profit_lock_active is True

    # Next completed bar produces a DOWN_BLUE crossover (opposite direction)
    # WHILE the held TSLL position is simultaneously in profit-lock-exit territory.
    state.pending_signal = {
        "signal_id": "pending-down-blue",
        "direction": Direction.DOWN_BLUE.value,
        "signal_type": "REVERSAL",
        "detected_at": now.isoformat(),
        "order_requested": False,
    }
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, entry_price * 1.015, datetime.now(ET), 0.0, "test", None)
    next_now = now + timedelta(minutes=9, seconds=5)
    result = run_once(broker=broker, market_data=svc, state=state, now=next_now)

    assert result.actions == ["OPPOSITE_SIGNAL:DOWN_BLUE"]
    assert state.position is not None
    assert state.position.symbol == config.INVERSE_SYMBOL


def _strong_up_red_bars_3m(*, n: int = 60, jump: float = 100.0, volume_mult: float = 5.0) -> pd.DataFrame:
    """A completed-3m-bars frame (not 1m) with a strong, high-volume UP_RED
    crossover on the last bar — reused from the same shape
    tests/tsla_auto/test_strong_flag_filter.py builds, for directly unit
    testing worker._check_stop_loss_cooldown without fighting
    evaluate_macd_crossover's own repeat-direction suppression."""
    start = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
    rows = [
        {"datetime": start + timedelta(minutes=3 * i), "open": 1000.0, "high": 1001.0, "low": 999.0, "close": 1000.0, "volume": 1000.0}
        for i in range(n)
    ]
    bars = pd.DataFrame(rows)
    i = n - 1
    base = float(bars["close"].iloc[i - 3])
    prev_close = base
    for offset, close in zip((2, 1, 0), (base + jump * 0.25, base + jump * 0.60, base + jump)):
        row = i - offset
        bars.loc[row, ["open", "high", "low", "close", "volume"]] = [
            prev_close, max(prev_close, close) + 1.0, min(prev_close, close) - 1.0, close, 1000.0 * volume_mult,
        ]
        prev_close = close
    return bars


def test_stop_loss_reentry_cooldown_gate_blocks_weak_reentry_within_window():
    state = _fresh_state()
    state.stop_loss_cooldown_direction = Direction.UP_RED
    state.last_stop_loss_exit_at = (_START + timedelta(minutes=90)).isoformat()
    now = _START + timedelta(minutes=95)  # 5 minutes after the stop-loss exit -> inside 15-minute cooldown
    weak_bars = _strong_up_red_bars_3m(jump=1.0, volume_mult=1.0)  # too weak to ever clear the 85-point floor

    blocked, achieved_score = worker._check_stop_loss_cooldown(state, Direction.UP_RED, now, weak_bars)

    assert blocked is True
    assert achieved_score is not None and achieved_score < config.STOP_LOSS_REENTRY_OVERRIDE_SCORE_FLOOR


def test_stop_loss_reentry_cooldown_gate_allows_strong_reentry_via_override():
    state = _fresh_state()
    state.stop_loss_cooldown_direction = Direction.UP_RED
    state.last_stop_loss_exit_at = (_START + timedelta(minutes=90)).isoformat()
    now = _START + timedelta(minutes=95)
    strong_bars = _strong_up_red_bars_3m(jump=100.0, volume_mult=5.0)  # clears score>=85

    blocked, achieved_score = worker._check_stop_loss_cooldown(state, Direction.UP_RED, now, strong_bars)

    assert blocked is False
    assert achieved_score is not None and achieved_score >= config.STOP_LOSS_REENTRY_OVERRIDE_SCORE_FLOOR


def test_stop_loss_reentry_cooldown_gate_ignores_opposite_direction():
    """Cooldown only restricts re-entry in the SAME direction that was
    stopped out — an opposite-direction switch is unaffected."""
    state = _fresh_state()
    state.stop_loss_cooldown_direction = Direction.UP_RED
    state.last_stop_loss_exit_at = (_START + timedelta(minutes=90)).isoformat()
    now = _START + timedelta(minutes=95)
    bars = _strong_up_red_bars_3m(jump=1.0, volume_mult=1.0)

    blocked, _score = worker._check_stop_loss_cooldown(state, Direction.DOWN_BLUE, now, bars)
    assert blocked is False


def test_stop_loss_reentry_cooldown_gate_expires_after_15_minutes():
    state = _fresh_state()
    state.stop_loss_cooldown_direction = Direction.UP_RED
    state.last_stop_loss_exit_at = (_START + timedelta(minutes=90)).isoformat()
    now = _START + timedelta(minutes=90 + config.STOP_LOSS_REENTRY_COOLDOWN_MIN + 1)
    weak_bars = _strong_up_red_bars_3m(jump=1.0, volume_mult=1.0)

    blocked, _score = worker._check_stop_loss_cooldown(state, Direction.UP_RED, now, weak_bars)
    assert blocked is False


def test_stop_loss_reentry_cooldown_gate_wired_into_dispatch_when_strong_filter_on(monkeypatch):
    """Integration check: with strong_filter_enabled ON, run_once still
    consults the cooldown gate for a genuinely new confirmed signal, and
    records the correct block_reason — without needing to hand-craft a MACD
    dataset that satisfies both the crossover AND the score requirements
    simultaneously."""
    monkeypatch.setattr(worker, "_check_stop_loss_cooldown", lambda state, direction, now, bars_3m: (True, None))
    svc, state, broker, now = _confirmed_up_scenario(strong_filter_on=True)
    result = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert result.actions == []
    assert broker.orders == []
    assert state.order_block_reason == config.STOP_LOSS_REENTRY_COOLDOWN_BLOCK


def test_stop_loss_reentry_cooldown_gate_not_consulted_when_strong_filter_off(monkeypatch):
    """MACD2 parity (2026-08-04): MACD2 has no stop-loss re-entry cooldown at
    all, so with strong_filter_enabled OFF (the default), TSLA_AUTO must not
    consult the cooldown gate either — a genuinely new confirmed signal
    orders immediately, exactly like MACD2 with its own filters off."""
    monkeypatch.setattr(worker, "_check_stop_loss_cooldown", lambda state, direction, now, bars_3m: (True, None))
    svc, state, broker, now = _confirmed_up_scenario(strong_filter_on=False)
    result = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert result.actions == ["ENTRY:UP_RED"]


def test_daily_entry_limit_and_other_strong_filter_gates_do_not_apply_when_off():
    """MACD2 parity (2026-08-04): MACD2 has no daily entry cap, no min-hold
    block, no same-direction cooldown, no sideways/profile gate at all --
    those only exist inside strong_flag_filter.py, which is only consulted
    when strong_filter_enabled is True. With the new default (off), a
    genuinely new confirmed flag must enter immediately regardless of how
    high daily_entry_count already is (well past NORMAL_MAX_ENTRIES=4)."""
    svc, state, broker, now = _confirmed_up_scenario(strong_filter_on=False)
    state.daily_entry_count = 10  # already far past config.NORMAL_MAX_ENTRIES (4)
    result = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert result.actions == ["ENTRY:UP_RED"]
    assert state.position is not None


def test_worker_restart_does_not_reorder_a_bar_completed_before_baseline():
    df_1m = _1m_from_3m_closes(_START, [100.0] * 97 + [99.0, 100.0, 140.0])
    now = _START + timedelta(minutes=3 * 100, seconds=5)
    svc = _svc_with_quote(df_1m, now, _QUOTES)
    state = _fresh_state()
    broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})

    # Simulate Worker (re)start AFTER this bar already completed.
    worker.initialize_strategy_session(state, svc, now=now)
    result = run_once(broker=broker, market_data=svc, state=state, now=now + timedelta(seconds=5))
    assert result.actions == []
    assert broker.orders == []
    assert ledger.load_signal_ledger() == []

    # A genuinely NEW bar after restart still fires normally.
    df_1m2 = _1m_from_3m_closes(_START, [100.0] * 97 + [99.0, 100.0, 140.0, 101.0, 100.0, 60.0])
    svc._df_1m = df_1m2
    next_now = now + timedelta(minutes=9, seconds=5)
    result2 = run_once(broker=broker, market_data=svc, state=state, now=next_now)
    assert result2.actions == ["ENTRY:DOWN_BLUE"]


def test_history_gap_blocks_signal_and_order():
    full_1m = _1m_from_3m_closes(_START, [100.0] * 97 + [99.0, 100.0, 140.0])
    gap_minute = _START + timedelta(minutes=3 * 99 + 1)
    gapped_1m = full_1m[full_1m["datetime"] != gap_minute].reset_index(drop=True)
    now = _START + timedelta(minutes=3 * 100, seconds=5)
    state = _fresh_state()
    state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
    broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
    svc = _svc_with_quote(gapped_1m, now, _QUOTES)

    result = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert result.actions == []
    assert broker.orders == []
    assert state.order_block_reason == config.HISTORY_GAP


def test_strong_filter_off_still_orders_on_confirmed_flag():
    svc, state, broker, now = _confirmed_up_scenario(strong_filter_on=False)
    result = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert result.actions == ["ENTRY:UP_RED"]


def test_strong_filter_on_approved_orders():
    svc, state, broker, now = _confirmed_up_scenario(strong_filter_on=True)
    result = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert result.actions == ["ENTRY:UP_RED"]
    assert state.last_approved is True


def test_strong_filter_on_rejected_never_calls_broker(monkeypatch):
    from app.trading.tsla_auto import strong_flag_filter

    monkeypatch.setattr(strong_flag_filter, "required_scores_for", lambda **k: {"entry": 200.0, "reversal": 200.0, "fast_reversal": 200.0})
    monkeypatch.setattr(strong_flag_filter, "_v6_profile_ok", lambda **k: (False, "test profile rejected"))
    svc, state, broker, now = _confirmed_up_scenario(strong_filter_on=True)
    result = run_once(broker=broker, market_data=svc, state=state, now=now)
    assert broker.orders == []
    assert result.actions == [f"{config.FILTERED_OUT}:UP_RED"]
    rows = ledger.load_signal_ledger()
    assert len(rows) == 1
    assert rows[0]["order_result"] == config.FILTERED_OUT


def test_rejected_signal_never_re_judged_next_tick(monkeypatch):
    from app.trading.tsla_auto import strong_flag_filter

    monkeypatch.setattr(strong_flag_filter, "required_scores_for", lambda **k: {"entry": 200.0, "reversal": 200.0, "fast_reversal": 200.0})
    monkeypatch.setattr(strong_flag_filter, "_v6_profile_ok", lambda **k: (False, "test profile rejected"))
    svc, state, broker, now = _confirmed_up_scenario(strong_filter_on=True)
    run_once(broker=broker, market_data=svc, state=state, now=now)
    for _ in range(5):
        run_once(broker=broker, market_data=svc, state=state, now=now)
    assert broker.orders == []
    assert len(ledger.load_signal_ledger()) == 1


def test_daily_entry_count_increments_only_on_real_fill():
    svc, state, broker, now = _confirmed_up_scenario()
    assert state.daily_entry_count == 0
    run_once(broker=broker, market_data=svc, state=state, now=now)
    assert state.daily_entry_count == 1


def test_daily_entry_count_does_not_increment_on_rejected_order():
    broker_that_rejects = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0})
    broker_that_rejects.fail_next_buy = True
    df_1m = _1m_from_3m_closes(_START, [100.0] * 97 + [99.0, 100.0, 140.0])
    now = _START + timedelta(minutes=3 * 100, seconds=5)
    state = _fresh_state()
    state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
    svc = _svc_with_quote(df_1m, now, _QUOTES)
    run_once(broker=broker_that_rejects, market_data=svc, state=state, now=now)
    assert state.daily_entry_count == 0


def test_day_rollover_resets_session_scoped_fields():
    state = _fresh_state()
    state.session_date = "20260723"
    state.processed_signal_ids = ["stale-sid"]
    state.daily_entry_count = 3
    state.stop_loss_reentry_override_used_today = True
    worker._apply_day_rollover(state, _START)
    assert state.session_date == "20260724"
    assert state.processed_signal_ids == []
    assert state.daily_entry_count == 0
    assert state.stop_loss_reentry_override_used_today is False


def test_worker_never_holds_tsll_and_tslz_simultaneously_across_switch():
    svc, state, broker, now = _confirmed_up_scenario()
    run_once(broker=broker, market_data=svc, state=state, now=now)
    assert state.position.symbol == config.LONG_SYMBOL

    df_1m2 = _1m_from_3m_closes(_START, [100.0] * 97 + [99.0, 100.0, 140.0, 101.0, 100.0, 60.0])
    svc._df_1m = df_1m2
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, 30.0, datetime.now(ET), 0.0, "test", None)
    next_now = now + timedelta(minutes=9, seconds=5)
    result = run_once(broker=broker, market_data=svc, state=state, now=next_now)
    assert result.actions == ["OPPOSITE_SIGNAL:DOWN_BLUE"]
    assert state.position.symbol == config.INVERSE_SYMBOL
    assert broker.get_position(config.LONG_SYMBOL) is None


def test_worker_blocks_personal_holding_mismatch_before_switch():
    svc, state, broker, now = _confirmed_up_scenario()
    run_once(broker=broker, market_data=svc, state=state, now=now)
    assert state.position.symbol == config.LONG_SYMBOL
    broker.buy_limit(config.LONG_SYMBOL, 1, 30.0, "personal-extra")

    df_1m2 = _1m_from_3m_closes(_START, [100.0] * 97 + [99.0, 100.0, 140.0, 101.0, 100.0, 60.0])
    svc._df_1m = df_1m2
    svc._quotes[config.LONG_SYMBOL] = QuoteSnapshot(config.LONG_SYMBOL, 30.0, datetime.now(ET), 0.0, "test", None)
    next_now = now + timedelta(minutes=3, seconds=5)
    result = run_once(broker=broker, market_data=svc, state=state, now=next_now)

    assert result.actions == []
    assert state.order_block_reason == order_executor.STRATEGY_OWNERSHIP_MISMATCH
    assert [o.side for o in broker.orders].count("SELL") == 0


def test_down_blue_blocks_when_tslz_quote_unresolved():
    df_1m = _1m_from_3m_closes(_START, [100.0] * 97 + [101.0, 100.0, 60.0])
    now = _START + timedelta(minutes=3 * 100, seconds=5)
    state = _fresh_state()
    state.last_confirmed_bar_ts = (_START + timedelta(minutes=3 * 98)).isoformat()
    broker = FakeBroker(cash_usd=100_000.0, quotes={config.INVERSE_SYMBOL: 12.0})
    svc = _svc_with_quote(df_1m, now, {config.SIGNAL_SYMBOL: 250.0, config.LONG_SYMBOL: 30.0})

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert result.skipped == config.TSLZ_EXCHANGE_UNRESOLVED
    assert broker.orders == []


def test_compute_today_signal_overview_separates_live_and_historical():
    df_1m = _1m_from_3m_closes(_START, [100.0] * 97 + [99.0, 100.0, 140.0])
    now = _START + timedelta(minutes=3 * 100, seconds=5)
    bar_start = _START + timedelta(minutes=3 * 99)
    bar_end = bar_start + timedelta(minutes=3)

    hist = worker.compute_today_signal_overview(df_1m, now=now, session_started_at=(bar_end + timedelta(minutes=1)).isoformat())
    live = worker.compute_today_signal_overview(df_1m, now=now, session_started_at=(bar_start - timedelta(minutes=1)).isoformat())
    hist_match = [r for r in hist if r["bar_start_at"] == bar_start.isoformat()]
    live_match = [r for r in live if r["bar_start_at"] == bar_start.isoformat()]
    assert hist_match[0]["origin"] == config.ORIGIN_HISTORICAL_REPLAY_ONLY
    assert live_match[0]["origin"] == config.ORIGIN_LIVE_CONFIRMED
