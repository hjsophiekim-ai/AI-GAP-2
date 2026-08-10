"""'2% 3회진입' filter tests (2026-08-10 v3 — scores EVERY confirmed flag
of the day; the 4th+ flag is no longer auto-blocked, and a weak 1st-3rd
flag is no longer auto-approved).

Covers:

A. Pure `evaluate_single_entry` unit tests — invalid-direction rejection,
   daily-fill-cap rejection (checked BEFORE scoring), score-threshold
   approval/rejection, sequence bonus flipping a borderline flag, a
   high-quality 4th+ flag still getting approved, and near-zero BLUE being
   diagnostic-only (never added to the score).
B. Worker integration — toggle OFF leaves legacy behavior completely
   unchanged (gate never invoked); toggle ON gates a NEW BUY only by score,
   not just the daily fill cap; a rejected REVERSAL still liquidates the
   held position (sell-only/no-re-entry) exactly like the other three
   optional filters.

Section A reuses test_major_flag_filter.py's own synthetic completed-3m
bar shape (flat base + sharp last-two-bar jump) since evaluate_single_entry
takes bars_3m directly. Section B reuses test_major_flag_filter.py's own
1-MINUTE worker-integration fixture (`_1m_from_3m_closes`, flat OHLC=close
per minute) instead, since that is what survives worker.py's real
resample_completed_3m/filter_complete_3m_bars round-trip cleanly -- both
duplicated locally so this file has no cross-test-file import.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.trading.macd2 import config, state_store
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.single_entry_filter import evaluate_single_entry
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST

# ── A. Synthetic completed-3m bar builder (same shape as test_major_flag_filter.py) ──
_BASE_PRICE = 1000.0
_BASE_SPREAD = 5.0
_BASE_VOLUME = 1000.0
_BASE_BARS = 40
_DAY1 = datetime(2026, 8, 6, 9, 0, tzinfo=KST)


def _flat_bars(n: int = _BASE_BARS, *, start: datetime = _DAY1, price: float = _BASE_PRICE) -> pd.DataFrame:
    rows = [
        {"datetime": start + timedelta(minutes=3 * i), "open": price, "high": price + _BASE_SPREAD,
         "low": price - _BASE_SPREAD, "close": price, "volume": _BASE_VOLUME}
        for i in range(n)
    ]
    return pd.DataFrame(rows)


def _shape_last_two_bars(bars: pd.DataFrame, direction: Direction, *, jump: float = 100.0, volume_mult: float = 5.0) -> pd.DataFrame:
    i = len(bars) - 1
    mid_i = i - 1
    base = float(bars["close"].iloc[mid_i - 1])
    mid_jump = jump / 2.0
    if direction is Direction.UP_RED:
        mid_close, close = base + mid_jump, base + jump
        mid_high, mid_low = mid_close + _BASE_SPREAD, base - _BASE_SPREAD
        high, low = close + _BASE_SPREAD, mid_close - _BASE_SPREAD
    else:
        mid_close, close = base - mid_jump, base - jump
        mid_high, mid_low = base + _BASE_SPREAD, mid_close - _BASE_SPREAD
        high, low = mid_close + _BASE_SPREAD, close - _BASE_SPREAD
    bars.loc[mid_i, ["open", "high", "low", "close", "volume"]] = [base, mid_high, mid_low, mid_close, _BASE_VOLUME * volume_mult]
    bars.loc[i, ["open", "high", "low", "close", "volume"]] = [mid_close, high, low, close, _BASE_VOLUME * volume_mult]
    return bars


def _crossover_bars(direction: Direction, *, n: int = _BASE_BARS, jump: float = 100.0, volume_mult: float = 5.0) -> pd.DataFrame:
    return _shape_last_two_bars(_flat_bars(n), direction, jump=jump, volume_mult=volume_mult)


def _decision_now(bars: pd.DataFrame) -> datetime:
    return pd.Timestamp(bars["datetime"].iloc[-1]).to_pydatetime() + timedelta(minutes=3)


def _simple_df_1m_from_bar_closes(bars_3m: pd.DataFrame) -> pd.DataFrame:
    """A plain 1-minute frame (flat OHLC=close per minute, like
    test_major_flag_filter.py's `_1m_from_3m_closes`) tracking the SAME
    close trajectory as `bars_3m` -- only used here for the pure
    evaluate_single_entry() 15m-price-slope feature, never resampled."""
    rows = []
    for _, row in bars_3m.iterrows():
        for j in range(3):
            close = float(row["close"])
            rows.append({"datetime": row["datetime"] + timedelta(minutes=j), "open": close, "high": close, "low": close, "close": close, "volume": 10})
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════════════
# A. Pure evaluate_single_entry unit tests
# ══════════════════════════════════════════════════════════════════════════
def test_invalid_direction_is_rejected():
    decision = evaluate_single_entry(None, None, Direction.HOLD, _DAY1, 1, 0)
    assert decision.approved is False
    assert decision.decision == config.FILTER_INPUT_NOT_CROSSOVER


def test_daily_limit_reached_rejects_before_any_scoring():
    """A strong (score-passing) flag is still rejected once today's fill
    count already reached the cap -- checked BEFORE bars are even touched."""
    decision = evaluate_single_entry(None, None, Direction.UP_RED, _DAY1, 1, config.SINGLE_ENTRY_MAX_DAILY_ENTRIES)
    assert decision.approved is False
    assert decision.decision == config.SINGLE_ENTRY_DAILY_LIMIT_REACHED


def test_strong_first_flag_is_approved():
    bars = _crossover_bars(Direction.UP_RED, jump=150.0)
    df_1m = _simple_df_1m_from_bar_closes(bars)
    now = _decision_now(bars)

    decision = evaluate_single_entry(bars, df_1m, Direction.UP_RED, now, 1, 0)

    assert decision.approved is True
    assert decision.decision == config.SINGLE_ENTRY_APPROVED
    assert decision.score >= config.SINGLE_ENTRY_SCORE_MIN
    assert decision.metrics["flag_seq"] == 1


def test_weak_fourth_flag_is_rejected_not_auto_approved_or_auto_blocked():
    """A weak flag with NO seq bonus (seq=4) must be rejected by SCORE, and
    the decision must be SCORE_BELOW_THRESHOLD, not a blanket seq>3 block
    (there is no such block any more)."""
    bars = _crossover_bars(Direction.UP_RED, jump=3.0, volume_mult=1.0)  # barely a flag, low component scores
    df_1m = _simple_df_1m_from_bar_closes(bars)
    now = _decision_now(bars)

    decision = evaluate_single_entry(bars, df_1m, Direction.UP_RED, now, 4, 0)

    assert decision.approved is False
    assert decision.decision == config.SINGLE_ENTRY_SCORE_BELOW_THRESHOLD
    assert decision.score < config.SINGLE_ENTRY_SCORE_MIN


def test_seq_bonus_flips_a_borderline_flag_from_rejected_to_approved():
    """The SAME bars (same underlying quality) must score higher as the
    1st flag of the day than as the 4th, purely from the sequence bonus."""
    bars = _crossover_bars(Direction.UP_RED, jump=3.0, volume_mult=1.0)
    df_1m = _simple_df_1m_from_bar_closes(bars)
    now = _decision_now(bars)

    as_first = evaluate_single_entry(bars, df_1m, Direction.UP_RED, now, 1, 0)
    as_fourth = evaluate_single_entry(bars, df_1m, Direction.UP_RED, now, 4, 0)

    assert as_first.score - as_fourth.score == config.SINGLE_ENTRY_SEQ_BONUS_1
    assert as_first.approved is True
    assert as_fourth.approved is False


def test_high_quality_fourth_flag_can_still_be_approved():
    """A 4th+ flag gets NO sequence bonus but is never auto-blocked -- if
    its own quality clears the bar on its own, it is approved."""
    bars = _crossover_bars(Direction.UP_RED, jump=150.0)
    df_1m = _simple_df_1m_from_bar_closes(bars)
    now = _decision_now(bars)

    decision = evaluate_single_entry(bars, df_1m, Direction.UP_RED, now, 4, 0)

    assert decision.approved is True
    assert decision.decision == config.SINGLE_ENTRY_APPROVED
    assert decision.metrics["flag_seq"] == 4


def test_near_zero_blue_is_diagnostic_only_never_scored():
    """near_zero_blue must be recorded in metrics but must NOT change the
    score -- reconstruct the total score from the other components alone
    and confirm it matches exactly (no hidden near-zero bonus)."""
    bars = _crossover_bars(Direction.DOWN_BLUE, jump=60.0)
    df_1m = _simple_df_1m_from_bar_closes(bars)
    now = _decision_now(bars)

    decision = evaluate_single_entry(bars, df_1m, Direction.DOWN_BLUE, now, 1, 0)

    assert "near_zero_blue" in decision.metrics
    assert isinstance(decision.metrics["near_zero_blue"], bool)
    reconstructed = (
        decision.metrics["major_score"]
        + decision.component_scores["seq_bonus"]
        + decision.component_scores["gap_expansion_bonus"]
        + decision.component_scores["ema10_slope_bonus"]
        + decision.component_scores["price_slope_15m_bonus"]
        + decision.component_scores["overheat_penalty"]
    )
    assert decision.score == reconstructed  # near-zero contributes nothing


# ══════════════════════════════════════════════════════════════════════════
# B. Worker integration — order gate only, gates a NEW BUY only
# ══════════════════════════════════════════════════════════════════════════
_WORKER_START = datetime(2026, 7, 24, 9, 0, tzinfo=KST)
_WORKER_QUOTES = {
    config.WATCH_SYMBOL: 140.0,
    config.LONG_SYMBOL: 15_000.0,
    config.INVERSE_SYMBOL: 10_000.0,
}


def _1m_from_3m_closes(start: datetime, closes: list, *, spread: float = 2.0) -> pd.DataFrame:
    """A small nonzero high/low spread (unlike a perfectly flat OHLC=close)
    keeps ATR meaningfully nonzero, so a genuinely small move scores
    genuinely low instead of registering as an unbounded multiple of a
    near-zero baseline ATR."""
    rows = []
    for i, close in enumerate(closes):
        bar_start = start + timedelta(minutes=3 * i)
        for j in range(3):
            rows.append({
                "datetime": bar_start + timedelta(minutes=j),
                "open": close, "high": close + spread, "low": close - spread, "close": close, "volume": 10,
            })
    return pd.DataFrame(rows)


def _svc_with_quote(df_1m: pd.DataFrame, bootstrap_now: datetime, quote_prices: dict) -> MarketDataService:
    svc = MarketDataService(
        mode="mock", fetch_minute_candles=lambda *a: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quote_prices.get(symbol), None),
    )
    svc.bootstrap(now=bootstrap_now)
    svc.refresh_quotes()
    return svc


def _fresh_state(*, single_entry_enabled: bool) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.single_entry_filter_enabled = single_entry_enabled
    return state


def _confirmed_flag_scenario(
    *, n: int, closes: list, single_entry_enabled: bool,
    daily_single_entry_count: int = 0, daily_confirmed_flag_count: int = 0,
):
    confirm_at = _WORKER_START + timedelta(minutes=3 * n, seconds=5)
    df_1m = _1m_from_3m_closes(_WORKER_START, closes)
    state = _fresh_state(single_entry_enabled=single_entry_enabled)
    state.daily_single_entry_count = daily_single_entry_count
    state.daily_confirmed_flag_count = daily_confirmed_flag_count
    state.last_confirmed_bar_ts = (_WORKER_START + timedelta(minutes=3 * (n - 2))).isoformat()
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: _WORKER_QUOTES[config.LONG_SYMBOL], config.INVERSE_SYMBOL: _WORKER_QUOTES[config.INVERSE_SYMBOL]})
    svc = _svc_with_quote(df_1m, confirm_at, _WORKER_QUOTES)
    return svc, state, broker, confirm_at


_STRONG_UP_CLOSES = [100.0] * 97 + [99.5, 99.9, 140.0]  # same jump test_major_flag_filter.py verifies scores well above any threshold
_WEAK_UP_CLOSES = [100.0] * 97 + [99.9, 99.95, 100.5]  # a real but low-quality flag (score ~36, no seq bonus)


def test_toggle_off_leaves_legacy_behavior_completely_unchanged():
    svc, state, broker, confirm_at = _confirmed_flag_scenario(n=100, closes=_STRONG_UP_CLOSES, single_entry_enabled=False)

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert result.actions == ["ENTRY:UP_RED"]
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert state.last_single_entry_approved is None
    assert state.last_single_entry_decision is None


def test_toggle_on_strong_flag_under_cap_approves_new_buy():
    svc, state, broker, confirm_at = _confirmed_flag_scenario(
        n=100, closes=_STRONG_UP_CLOSES, single_entry_enabled=True,
        daily_single_entry_count=config.SINGLE_ENTRY_MAX_DAILY_ENTRIES - 1,
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert result.actions == ["ENTRY:UP_RED"]
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert state.last_single_entry_approved is True
    assert state.last_single_entry_decision == config.SINGLE_ENTRY_APPROVED
    assert state.daily_single_entry_count == config.SINGLE_ENTRY_MAX_DAILY_ENTRIES
    assert state.daily_confirmed_flag_count == 1
    assert state.last_single_entry_flag_seq == 1
    assert state.last_single_entry_at is not None


def test_toggle_on_weak_flag_is_rejected_by_score_even_under_cap():
    """Being under the daily fill cap is no longer sufficient -- a weak
    flag (here, with no seq bonus since it's the 4th of the day) must
    still be rejected by score."""
    svc, state, broker, confirm_at = _confirmed_flag_scenario(
        n=100, closes=_WEAK_UP_CLOSES, single_entry_enabled=True, daily_confirmed_flag_count=3,
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert not any(a.startswith("ENTRY:") for a in result.actions)
    assert state.position is None
    assert state.last_single_entry_approved is False
    assert state.last_single_entry_decision == config.SINGLE_ENTRY_SCORE_BELOW_THRESHOLD


def test_toggle_on_at_daily_cap_blocks_new_buy_even_for_a_strong_flag():
    svc, state, broker, confirm_at = _confirmed_flag_scenario(
        n=100, closes=_STRONG_UP_CLOSES, single_entry_enabled=True,
        daily_single_entry_count=config.SINGLE_ENTRY_MAX_DAILY_ENTRIES,
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert not any(a.startswith("ENTRY:") for a in result.actions)
    assert state.position is None
    assert state.last_single_entry_approved is False
    assert state.last_single_entry_decision == config.SINGLE_ENTRY_DAILY_LIMIT_REACHED


def test_strong_fourth_flag_still_approved_when_under_cap():
    """A 4th confirmed flag of the day (daily_confirmed_flag_count already
    3) with NO seq bonus, but strong enough on its own, must still enter --
    the old hard seq>3 block no longer exists."""
    svc, state, broker, confirm_at = _confirmed_flag_scenario(
        n=100, closes=_STRONG_UP_CLOSES, single_entry_enabled=True,
        daily_single_entry_count=0, daily_confirmed_flag_count=3,
    )

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert result.actions == ["ENTRY:UP_RED"]
    assert state.last_single_entry_approved is True
    assert state.last_single_entry_flag_seq == 4


def test_reversal_rejected_by_score_still_liquidates_held_position():
    """A confirmed OPPOSITE flag that the gate rejects (by score, not the
    daily cap this time) must still sell the currently-held ETF (sell-
    only/no-re-entry) -- it just does not flip into the opposite ETF."""
    n = 100
    weak_down_closes = [140.0] * 97 + [140.1, 140.05, 139.5]  # a real but low-quality DOWN_BLUE flag
    confirm_at = _WORKER_START + timedelta(minutes=3 * n, seconds=5)
    df_1m = _1m_from_3m_closes(_WORKER_START, weak_down_closes)
    state = _fresh_state(single_entry_enabled=True)
    state.daily_confirmed_flag_count = 3  # 4th of the day -- no seq bonus
    state.last_confirmed_bar_ts = (_WORKER_START + timedelta(minutes=3 * (n - 2))).isoformat()
    state.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0)
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    broker.buy_market(config.LONG_SYMBOL, 10, "seed")
    svc = _svc_with_quote(df_1m, confirm_at, _WORKER_QUOTES)

    result = run_once(broker=broker, market_data=svc, state=state, now=confirm_at)

    assert any(a.startswith("OPPOSITE_SIGNAL_SELL_ONLY:") for a in result.actions)
    assert state.position is None
    assert broker.get_position(config.LONG_SYMBOL) is None
    assert broker.get_position(config.INVERSE_SYMBOL) is None
    assert state.last_single_entry_approved is False
    assert state.last_single_entry_decision == config.SINGLE_ENTRY_SCORE_BELOW_THRESHOLD
