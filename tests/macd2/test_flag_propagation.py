"""Flag-propagation regression tests (2026-09-11 real incidents).

Every test drives the REAL ``worker.run_once()`` / ``worker.initialize_
strategy_session()`` path with a fake broker and conftest.py's autouse
tmp_path isolation — never a pure-function-only check. The harness recipe
(sine 1m frame + ``_patch_common`` decision patches) is reused verbatim from
tests/macd2/test_tw2_3slot_worker_regression.py so no new test
infrastructure is introduced.

What is covered, and why each case exists:

  1. A confirmed crossover detected on a RECONCILE-BLOCKED tick is persisted
     to the signal ledger AND registers its T+3 candidate, without placing
     any order on that tick (2026-09-11 incident: it was detected — state
     moved — but produced zero ledger rows and zero candidate, so the
     reversal exit never ran and a BLUE position was held to a stop-loss).
  2. The candidate registered during the block resolves normally on the next
     healthy tick, i.e. "reconcile 종료 후 정상 처리".
  3. A restart catch-up replay records INTRADAY flags too, not only premarket
     ones (2026-09-10 incident: UI last FLAG EVENT 18:54 UP_RED vs signal
     ledger last row 17:21 UP_RED).
  4. A post-cutoff flag is recorded but never ordered.
  5. The same flag observed twice (restart + reconcile) yields exactly one
     ledger row and one order.
  6. Every flag the UI's own recompute reports as LIVE_CONFIRMED has a
     matching signal-ledger row — the "direction changed but ledger missing
     = 0" invariant.

Reversal-exit behaviour itself (T+3 approval switches, a non-whipsaw T+3
rejection still liquidating, a whipsaw rejection holding) is already covered
by test_tw2_3slot_worker_regression.py's section 6 and is deliberately not
duplicated here — this file only covers the propagation path feeding it.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, state_store, teg_gate, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import (
    Direction,
    MacdSnapshot,
    MajorFlagDecision,
    PositionSnapshot,
    RuntimeState,
)
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST


# ── harness (mirrors test_tw2_3slot_worker_regression.py) ──────────────────

def _sine_1m_closes(n_minutes: int, amplitude: float = 20.0) -> list[float]:
    period = max(n_minutes // 2, 1)
    return [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4) for i in range(n_minutes)]


def _1m_frame(start: datetime, closes: list[float]) -> pd.DataFrame:
    rows = [
        {
            "datetime": start + timedelta(minutes=i),
            "open": c, "high": c + 0.1, "low": c - 0.1, "close": c,
            "volume": 100 + (i % 7) * 10,
        }
        for i, c in enumerate(closes)
    ]
    return pd.DataFrame(rows)


_DAY = datetime(2026, 1, 5, 9, 0, tzinfo=KST)
_BOOTSTRAP_NOW = _DAY + timedelta(days=2)
_NOW0 = _DAY + timedelta(minutes=3 * (config.SIGNAL_MIN_BAR_INDEX + 1))  # 10:21 KST


@pytest.fixture
def market_data():
    df_1m = _1m_frame(_DAY, _sine_1m_closes(300))
    quote_prices = {
        config.LONG_SYMBOL: 15_000.0,
        config.INVERSE_SYMBOL: 10_000.0,
        config.WATCH_SYMBOL: 100.0,
    }

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    result = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert result.ok, f"fixture bootstrap failed unexpectedly: {result.reason}"
    svc.refresh_quotes()
    return svc, df_1m


def _fresh_3slot_state() -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    state.time_window_2_filter_enabled = False
    state.time_window_teg_filter_enabled = False
    state.time_window_3slot_filter_enabled = True
    return state


def _broker() -> FakeBroker:
    return FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )


def _approved(window: str = "W1_MORNING_AGGRESSIVE") -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=True, score=5.0, required_score=4.0, decision="APPROVED",
        reasons=(), component_scores={}, metrics={"window": window},
        is_reversal=False, fast_reversal=False, block_reason=None,
    )


def _rejected(reason: str) -> MajorFlagDecision:
    return MajorFlagDecision(
        approved=False, score=1.0, required_score=4.0, decision=reason,
        reasons=(reason,), component_scores={}, metrics={},
        is_reversal=False, fast_reversal=False, block_reason=reason,
    )


def _patch_common(monkeypatch, *, entry_decision=None, teg_approved: bool = True):
    if entry_decision is not None:
        monkeypatch.setattr(
            worker.time_window_filter, "evaluate_time_window_entry", lambda *a, **kw: entry_decision,
        )
    monkeypatch.setattr(
        worker.time_window_filter, "evaluate_tw2_extra_vetoes", lambda *a, **kw: (False, None),
    )
    monkeypatch.setattr(
        worker.teg_gate, "evaluate_teg",
        lambda *a, **kw: teg_gate.TEGDecision(
            approved=teg_approved, conditions={}, metrics={}, reject_reasons=(),
        ),
    )
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
    monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)


def _snap(bar_dt: datetime) -> MacdSnapshot:
    """A real MacdSnapshot — _record_signal_ledger reads macd/signal/diff
    columns off it, so a stub object is not enough."""
    return MacdSnapshot(
        bar_dt=bar_dt, macd=1.0, signal=0.5, hist=0.5, hist_last3=(-0.2, 0.1, 0.5),
        completed_3m_count=40, previous_diff=-0.1, current_diff=0.5, relation="ABOVE",
    )


def _force_crossover(monkeypatch, direction: Direction):
    """Deterministic confirmed crossover on whichever completed bar this tick
    evaluates — the same "patch the decision function" technique the existing
    TW2/3-SLOT worker tests use instead of waiting on an organic sine cross."""
    monkeypatch.setattr(worker, "evaluate_macd_crossover", lambda *_a, **_kw: direction)


def _force_reconcile_block(monkeypatch, reason: str = worker.POSITION_DATA_ERROR):
    monkeypatch.setattr(worker, "reconcile_position_state", lambda *_a, **_kw: reason)


class _Knobs:
    """Per-tick switches for a multi-tick scenario.

    NEVER use ``monkeypatch.undo()`` to change behaviour between ticks — it
    also reverts conftest.py's autouse ``_isolate_macd2_state`` fixture, which
    points the ledgers at tmp_path, and the very next order would target the
    REAL data/logs ledger (caught only by ledger._assert_safe_to_write_ledger).
    Patch once, then mutate these fields.
    """

    def __init__(self, monkeypatch, *, entry_decision=None, teg_approved: bool = True,
                 patch_cross: bool = True):
        self.cross: Direction = Direction.HOLD
        self.reconcile: str | None = None
        self.entry_decision = entry_decision
        self._real_reconcile = worker.reconcile_position_state
        if patch_cross:
            monkeypatch.setattr(worker, "evaluate_macd_crossover", lambda *_a, **_kw: self.cross)
        monkeypatch.setattr(
            worker, "reconcile_position_state",
            lambda *a, **kw: self.reconcile if self.reconcile else self._real_reconcile(*a, **kw),
        )
        monkeypatch.setattr(
            worker.time_window_filter, "evaluate_time_window_entry",
            lambda *a, **kw: self.entry_decision,
        )
        monkeypatch.setattr(
            worker.time_window_filter, "evaluate_tw2_extra_vetoes", lambda *a, **kw: (False, None),
        )
        monkeypatch.setattr(
            worker.teg_gate, "evaluate_teg",
            lambda *a, **kw: teg_gate.TEGDecision(
                approved=teg_approved, conditions={}, metrics={}, reject_reasons=(),
            ),
        )
        monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_RETRIES", 1)
        monkeypatch.setattr(worker, "ORDER_FILL_RECONCILE_DELAY_SEC", 0.0)


def _flag_rows(direction: Direction | None = None) -> list[dict]:
    rows = ledger.load_signal_ledger(limit=1000)
    if direction is None:
        return rows
    return [r for r in rows if str(r.get("direction") or "") == direction.value]


def _seed_inverse_position(state: RuntimeState, broker: FakeBroker, now: datetime) -> None:
    state.position = PositionSnapshot(
        symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now,
    )
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.time_window_entry_session = "MORNING"
    state.tw2_3slot_slots_used_today = 1
    state.tw2_3slot_morning_count = 1
    state.last_detected_direction = Direction.DOWN_BLUE
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0


# ── 1. reconcile-blocked tick: ledger row + T+3 candidate, no order ────────

def test_reconcile_blocked_tick_records_flag_and_registers_t3_candidate(market_data, monkeypatch):
    """2026-09-11 incident, minimal reproduction: BLUE held, RED crossover
    lands on a tick whose reconcile is blocked."""
    svc, _df = market_data
    state = _fresh_3slot_state()
    broker = _broker()
    _seed_inverse_position(state, broker, _NOW0)
    _patch_common(monkeypatch)
    _force_crossover(monkeypatch, Direction.UP_RED)
    _force_reconcile_block(monkeypatch)
    orders_before = len(broker.orders)

    result = run_once(broker=broker, market_data=svc, state=state, now=_NOW0)

    assert result.skipped == worker.POSITION_DATA_ERROR
    # detection still happened (2026-08-31 fix, unchanged)
    assert state.last_detected_direction == Direction.UP_RED
    # ...and now propagation happens too
    assert state.tw2_3slot_pending_flag_direction == Direction.UP_RED, (
        "a flag detected during a reconcile block must still register its T+3 candidate"
    )
    assert state.tw2_3slot_pending_flag_bar_ts is not None
    red_rows = _flag_rows(Direction.UP_RED)
    assert red_rows, "the detected UP_RED flag must appear in the signal ledger"
    assert len(broker.orders) == orders_before, "no order may be placed on a reconcile-blocked tick"
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL


def test_reconcile_blocked_tick_places_no_order_even_when_gate_would_approve(market_data, monkeypatch):
    """A gate that WOULD approve must not turn the blocked tick into an order;
    the event is kept on the existing pending-signal queue instead."""
    svc, _df = market_data
    state = _fresh_3slot_state()
    # no T+3 filter at all -> legacy immediate-dispatch mode
    state.time_window_3slot_filter_enabled = False
    broker = _broker()
    _patch_common(monkeypatch)
    _force_crossover(monkeypatch, Direction.UP_RED)
    _force_reconcile_block(monkeypatch)

    result = run_once(broker=broker, market_data=svc, state=state, now=_NOW0)

    assert result.skipped == worker.POSITION_DATA_ERROR
    assert len(broker.orders) == 0, "reconcile-blocked tick must never order"
    assert state.pending_signal is not None, "the event must survive on the pending queue"
    assert state.pending_signal["direction"] == Direction.UP_RED.value
    assert state.pending_signal["reason"] == worker.POSITION_DATA_ERROR
    audit = [
        r for r in ledger.load_signal_ledger(limit=1000)
        if str(r.get("signal_id") or "").endswith(worker.RECONCILE_DEFERRED_SUFFIX)
    ]
    assert audit, "a deferred-order flag must still leave an audit row"
    # the real signal_id stays free so the later order row is never deduped away
    assert all(
        not str(r.get("signal_id") or "").endswith(worker.RECONCILE_DEFERRED_SUFFIX)
        or str(r.get("block_reason") or "") == worker.POSITION_DATA_ERROR
        for r in audit
    )


# ── 2. the registered candidate resolves normally once reconcile recovers ──

def test_candidate_registered_during_block_resolves_on_next_healthy_tick(market_data, monkeypatch):
    """T+3 resolution must run on the next healthy tick — the reversal exit
    the 2026-09-11 incident lost entirely."""
    svc, _df = market_data
    state = _fresh_3slot_state()
    broker = _broker()
    _seed_inverse_position(state, broker, _NOW0)
    # quality rejection => sell-only liquidation of the held BLUE position
    knobs = _Knobs(monkeypatch, entry_decision=_rejected(config.TW_REJECT_LOW_QUALITY_SCORE))
    knobs.cross = Direction.UP_RED
    knobs.reconcile = worker.POSITION_DATA_ERROR
    run_once(broker=broker, market_data=svc, state=state, now=_NOW0)
    assert state.tw2_3slot_pending_flag_direction == Direction.UP_RED

    # reconcile recovers; next completed bar arrives (T+3)
    knobs.cross = Direction.HOLD
    knobs.reconcile = None
    result = run_once(broker=broker, market_data=svc, state=state, now=_NOW0 + timedelta(minutes=3))

    assert any(a.startswith("TW2_3SLOT_SELL_ONLY") for a in result.actions), result.actions
    assert state.position is None, "the reversal exit must actually liquidate the BLUE position"


# ── 3. restart catch-up records intraday flags, not only premarket ─────────

@pytest.mark.parametrize(
    "bar_time,expected_reason",
    [
        (datetime(2026, 1, 5, 8, 30, tzinfo=KST), "BEFORE_SESSION_OPEN"),
        (datetime(2026, 1, 5, 10, 33, tzinfo=KST), worker.RESTART_CATCH_UP_REPLAY),
        (datetime(2026, 1, 5, 18, 54, tzinfo=KST), worker.RESTART_CATCH_UP_REPLAY),
    ],
)
def test_catchup_flag_is_recorded_with_the_right_block_reason(bar_time, expected_reason):
    """2026-09-10 incident: an intraday (>= 09:00) catch-up flag used to be
    dropped silently, so state/UI showed a flag the ledger never had. The
    18:54 row is that exact case."""
    state = _fresh_3slot_state()
    snap = _snap(bar_time)
    now = bar_time + timedelta(minutes=10)

    worker._record_catchup_flag(state, snap, Direction.UP_RED, now)

    rows = ledger.load_signal_ledger(limit=1000)
    assert len(rows) == 1, rows
    assert rows[0]["block_reason"] == expected_reason
    assert rows[0]["direction"] == Direction.UP_RED.value
    assert rows[0]["signal_id"] == worker.make_signal_id(bar_time, Direction.UP_RED)


def test_catchup_flag_for_another_day_is_not_recorded():
    """Unchanged guard: only TODAY's bars are recorded by the catch-up walk."""
    state = _fresh_3slot_state()
    snap = _snap(datetime(2026, 1, 2, 10, 33, tzinfo=KST))

    worker._record_catchup_flag(state, snap, Direction.UP_RED, datetime(2026, 1, 5, 10, 40, tzinfo=KST))

    assert ledger.load_signal_ledger(limit=1000) == []


def test_intraday_restart_catchup_leaves_no_ledger_gap(market_data, monkeypatch):
    """End-to-end through the real initialize_strategy_session() replay: every
    flag the walk reconstructs for today must have a ledger row."""
    svc, df_1m = market_data
    state = _fresh_3slot_state()
    # mid-session restart: a same-day last_confirmed_bar_ts exists
    state.last_confirmed_bar_ts = (_DAY + timedelta(minutes=30)).isoformat()
    now = _DAY + timedelta(minutes=240)

    worker.initialize_strategy_session(market_data=svc, state=state, now=now)

    overview = worker.compute_today_signal_overview(df_1m, now=now, session_started_at=None)
    replayed = [
        row for row in overview
        if datetime.fromisoformat(row["bar_start_at"]) > _DAY + timedelta(minutes=30)
        and datetime.fromisoformat(row["bar_end_at"]) <= now
    ]
    ledger_ids = {str(r.get("signal_id") or "") for r in ledger.load_signal_ledger(limit=1000)}
    # the walk deliberately stops one bar short of the newest, so allow that one
    missing = [
        row["signal_id"] for row in replayed[:-1]
        if row["signal_id"] not in ledger_ids
    ]
    assert not missing, f"catch-up replay left flags with no ledger row: {missing}"


# ── 4. post-cutoff flag: recorded, never ordered ───────────────────────────

def test_after_cutoff_flag_is_recorded_but_order_is_blocked(market_data, monkeypatch):
    svc, _df = market_data
    state = _fresh_3slot_state()
    broker = _broker()
    _patch_common(monkeypatch, entry_decision=_approved())
    _force_crossover(monkeypatch, Direction.UP_RED)
    after_cutoff = _DAY.replace(hour=15, minute=0)

    run_once(broker=broker, market_data=svc, state=state, now=after_cutoff)

    rows = _flag_rows(Direction.UP_RED)
    assert rows, "a post-cutoff flag must still be recorded"
    assert any(str(r.get("block_reason") or "") == "NEW_ENTRY_CUTOFF" for r in rows), rows
    assert len(broker.orders) == 0, "no order after NEW_ENTRY_CUTOFF"


def test_before_session_open_flag_is_recorded_but_order_is_blocked(market_data, monkeypatch):
    svc, _df = market_data
    state = _fresh_3slot_state()
    broker = _broker()
    _patch_common(monkeypatch, entry_decision=_approved())
    _force_crossover(monkeypatch, Direction.DOWN_BLUE)
    # 08:57 tick: today's 08:54 bar has closed, session not open yet.
    # The frame starts early enough that MACD(12,26,9) is already warm by then
    # (production gets this from bootstrap's prior-day merge).
    early_df = _1m_frame(_DAY.replace(hour=4, minute=0), _sine_1m_closes(300))

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return early_df, {}

    svc2 = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=lambda m, s: (100.0, None))
    assert svc2.bootstrap(now=_DAY.replace(hour=4, minute=0) + timedelta(days=2)).ok
    svc2.refresh_quotes()

    run_once(broker=broker, market_data=svc2, state=state, now=_DAY.replace(hour=8, minute=57))

    rows = _flag_rows(Direction.DOWN_BLUE)
    assert rows, "a premarket flag must still be recorded"
    assert any(str(r.get("block_reason") or "") == "BEFORE_SESSION_OPEN" for r in rows), rows
    assert len(broker.orders) == 0


# ── 5. duplicate observation -> exactly one ledger row / one order ─────────

def test_same_flag_observed_twice_records_one_row_and_one_order(market_data, monkeypatch):
    svc, _df = market_data
    state = _fresh_3slot_state()
    broker = _broker()
    _seed_inverse_position(state, broker, _NOW0)
    _patch_common(monkeypatch)
    _force_crossover(monkeypatch, Direction.UP_RED)
    _force_reconcile_block(monkeypatch)

    run_once(broker=broker, market_data=svc, state=state, now=_NOW0)
    first = len(_flag_rows(Direction.UP_RED))
    # same bar observed again on another blocked tick (restart/reconcile loop)
    run_once(broker=broker, market_data=svc, state=state, now=_NOW0 + timedelta(seconds=5))
    # and once more via the catch-up replay recorder for the very same bar
    snap = _snap(datetime.fromisoformat(state.tw2_3slot_pending_flag_bar_ts))
    worker._record_catchup_flag(state, snap, Direction.UP_RED, _NOW0)

    assert first == 1, "first observation records exactly one row"
    assert len(_flag_rows(Direction.UP_RED)) == 1, "duplicate observations must not add rows"
    assert len(broker.orders) == 1, "seed order only — no duplicate order after re-observation"


# ── 6. "direction changed but ledger missing = 0" invariant ────────────────

def test_every_live_confirmed_ui_flag_has_a_signal_ledger_row(market_data, monkeypatch):
    """The UI's 마지막 FLAG EVENT comes from compute_today_signal_overview (a
    pure recompute) unioned with signal-ledger rows, so a flag the recompute
    reports LIVE_CONFIRMED while the ledger lacks it is exactly the
    2026-09-10 inconsistency. Drive real ticks across the day — every third
    one reconcile-blocked — and assert the two agree."""
    svc, df_1m = market_data
    state = _fresh_3slot_state()
    broker = _broker()
    knobs = _Knobs(
        monkeypatch, entry_decision=_rejected(config.TW_REJECT_LOW_QUALITY_SCORE), patch_cross=False,
    )
    session_start = _DAY + timedelta(minutes=3 * (config.SIGNAL_MIN_BAR_INDEX + 1))
    state.session_started_at = session_start.isoformat()

    last_tick = _DAY + timedelta(minutes=240)
    overview = worker.compute_today_signal_overview(
        df_1m, now=last_tick, session_started_at=session_start.isoformat(),
    )
    live = [r for r in overview if r["origin"] == worker.ORIGIN_LIVE_CONFIRMED
            and datetime.fromisoformat(r["bar_end_at"]) <= last_tick]
    assert live, "fixture must produce at least one LIVE_CONFIRMED flag"

    # replay ticks at every bar close, blocking reconcile on every 3rd bar
    tick = session_start
    i = 0
    while tick <= last_tick:
        knobs.reconcile = worker.POSITION_DATA_ERROR if i % 3 == 2 else None
        run_once(broker=broker, market_data=svc, state=state, now=tick)
        tick += timedelta(minutes=3)
        i += 1

    ledger_ids = {str(r.get("signal_id") or "").split(":")[0]
                  for r in ledger.load_signal_ledger(limit=2000)}
    missing = [r["signal_id"] for r in live if r["signal_id"] not in ledger_ids]
    assert not missing, f"direction changed but ledger missing: {missing}"


# ── 7. restart catch-up under 3-SLOT must go through T+3, never bypass it ──

def _restart_catchup_state(*, threeslot: bool) -> RuntimeState:
    state = _fresh_3slot_state()
    state.time_window_3slot_filter_enabled = threeslot
    state.time_window_twf_filter_enabled = False
    state.time_window_2_filter_enabled = False
    state.time_window_teg_filter_enabled = False
    # mid-session restart: a same-day last_confirmed_bar_ts already exists, so
    # initialize_strategy_session replays today's remaining bars.
    state.last_confirmed_bar_ts = (_DAY + timedelta(minutes=30)).isoformat()
    return state


def test_restart_catchup_under_3slot_registers_t3_candidate_not_bare_pending(market_data):
    """2026-09-11 fix: the catch-up walk used to hand its find to the bare
    state.pending_signal, whose _execute_or_wait path never consults
    _judge_entry_gate -- entering with no T+3 recheck, no quality score, no
    TEG gate and no slot accounting. It must use the 3-SLOT pending slot."""
    svc, _df = market_data
    state = _restart_catchup_state(threeslot=True)

    worker.initialize_strategy_session(market_data=svc, state=state, now=_DAY + timedelta(minutes=240))

    assert state.tw2_3slot_pending_flag_direction is not None, (
        "a 3-SLOT restart catch-up flag must land on the 3-SLOT pending slot"
    )
    assert state.tw2_3slot_pending_flag_bar_ts is not None
    assert state.pending_signal is None, (
        "it must NOT go to the bare pending_signal queue (that path bypasses T+3/quality/TEG)"
    )


def test_restart_catchup_legacy_path_still_uses_pending_signal(market_data):
    """Guard the other side: with every 3-SLOT/TW filter off, the legacy
    bare-pending_signal handoff is unchanged (test_worker.py's own
    test_second_restart_does_not_discard_a_still_pending_catchup_signal pins
    the same behaviour)."""
    svc, _df = market_data
    state = _restart_catchup_state(threeslot=False)

    worker.initialize_strategy_session(market_data=svc, state=state, now=_DAY + timedelta(minutes=240))

    assert state.tw2_3slot_pending_flag_direction is None
    assert state.pending_signal is not None
    assert state.pending_signal["reason"] == "RESTART_CATCH_UP_MULTI_BAR_GAP"


def test_restart_catchup_3slot_places_no_order_when_quality_teg_rejects(market_data, monkeypatch):
    """필수 테스트: quality/TEG 실패 시 재시작 후에도 주문이 나가지 않는다."""
    svc, _df = market_data
    state = _restart_catchup_state(threeslot=True)
    broker = _broker()
    _Knobs(monkeypatch, entry_decision=_rejected(config.TW_REJECT_LOW_QUALITY_SCORE),
           teg_approved=False, patch_cross=False)

    worker.initialize_strategy_session(market_data=svc, state=state, now=_DAY + timedelta(minutes=240))
    assert state.tw2_3slot_pending_flag_direction is not None

    # the next live ticks must resolve it through the REAL gate, not order
    for i in range(3):
        run_once(broker=broker, market_data=svc, state=state,
                 now=_DAY + timedelta(minutes=240 + 3 * i))

    assert len(broker.orders) == 0, "a quality/TEG-rejected catch-up flag must never order"
    assert state.position is None
    assert state.tw2_3slot_slots_used_today == 0, "a rejected candidate must not consume a slot"
    reasons = [str(r.get("block_reason") or "") for r in ledger.load_signal_ledger(limit=1000)]
    assert config.TW_REJECT_LOW_QUALITY_SCORE in reasons, (
        f"the T+3 rejection must be recorded with its real reason: {reasons}"
    )


def test_restart_twice_does_not_duplicate_3slot_candidate_or_order(market_data, monkeypatch):
    """필수 테스트: 재시작 전후 동일 플래그 중복주문 금지."""
    svc, _df = market_data
    state = _restart_catchup_state(threeslot=True)
    broker = _broker()
    _Knobs(monkeypatch, entry_decision=_rejected(config.TW_REJECT_LOW_QUALITY_SCORE),
           patch_cross=False)

    worker.initialize_strategy_session(market_data=svc, state=state, now=_DAY + timedelta(minutes=240))
    first_dir = state.tw2_3slot_pending_flag_direction
    first_bar = state.tw2_3slot_pending_flag_bar_ts
    assert first_dir is not None, "the catch-up replay must actually find a flag for this to mean anything"
    # second back-to-back restart, no new market data
    worker.initialize_strategy_session(market_data=svc, state=state, now=_DAY + timedelta(minutes=241))

    assert state.tw2_3slot_pending_flag_direction == first_dir, "the candidate must survive a 2nd restart"
    assert state.tw2_3slot_pending_flag_bar_ts == first_bar, "and must not be clobbered by the replay"
    assert len(broker.orders) == 0


# ── 8. a flag on a tick that already executed must not be dropped ──────────

def test_flag_on_executed_tick_is_recorded_and_kept_as_candidate(market_data, monkeypatch):
    """필수 테스트: 체결 tick과 같은 봉에서 반대플래그 발생 시 ledger/pending 누락 0건.

    T+3 해소가 체결되는 tick에 같은 봉의 새 크로스오버가 겹치는 상황 -- flat
    경로의 early return 5곳이 전부 플래그 기록보다 앞서 return 한다."""
    svc, _df = market_data
    state = _fresh_3slot_state()
    broker = _broker()
    knobs = _Knobs(monkeypatch, entry_decision=_approved())
    # a pending candidate from the PREVIOUS bar resolves (and enters) this tick
    state.tw2_3slot_pending_flag_direction = Direction.UP_RED
    # an OLD flag bar so _resolve_tw2_3slot_candidate is past the flag's own
    # bar T and actually resolves this tick (same recipe as
    # test_tw2_3slot_worker_regression.py::_prime_3slot_pending)
    state.tw2_3slot_pending_flag_bar_ts = _DAY.isoformat()
    # ...while THIS bar produces a brand-new opposite crossover
    knobs.cross = Direction.DOWN_BLUE

    result = run_once(broker=broker, market_data=svc, state=state, now=_NOW0)

    assert any(a.startswith("TW2_3SLOT_ENTRY") for a in result.actions), result.actions
    assert len(broker.orders) == 1, "exactly the one entry this tick -- no extra order for the new flag"
    assert _flag_rows(Direction.DOWN_BLUE), "the new DOWN_BLUE flag must be in the signal ledger"
    assert state.tw2_3slot_pending_flag_direction == Direction.DOWN_BLUE, (
        "the new flag must be registered as this mode's own T+3 candidate"
    )
    assert state.tw2_3slot_pending_flag_bar_ts is not None


def test_flag_on_executed_tick_is_not_double_recorded(market_data, monkeypatch):
    """필수 테스트: 중복 ledger/order 0건 -- 같은 봉을 여러 tick 관측해도 1건."""
    svc, _df = market_data
    state = _fresh_3slot_state()
    broker = _broker()
    knobs = _Knobs(monkeypatch, entry_decision=_approved())
    state.tw2_3slot_pending_flag_direction = Direction.UP_RED
    # an OLD flag bar so _resolve_tw2_3slot_candidate is past the flag's own
    # bar T and actually resolves this tick (same recipe as
    # test_tw2_3slot_worker_regression.py::_prime_3slot_pending)
    state.tw2_3slot_pending_flag_bar_ts = _DAY.isoformat()
    knobs.cross = Direction.DOWN_BLUE

    run_once(broker=broker, market_data=svc, state=state, now=_NOW0)
    rows_after_first = len(_flag_rows(Direction.DOWN_BLUE))
    orders_after_first = len(broker.orders)
    # same bar observed again on later ticks
    run_once(broker=broker, market_data=svc, state=state, now=_NOW0 + timedelta(seconds=5))
    run_once(broker=broker, market_data=svc, state=state, now=_NOW0 + timedelta(seconds=10))

    assert rows_after_first == 1
    assert len(_flag_rows(Direction.DOWN_BLUE)) == 1, "no duplicate ledger row for the same flag"
    assert len(broker.orders) == orders_after_first, "no duplicate order"


def test_executed_tick_without_new_flag_is_completely_unchanged(market_data, monkeypatch):
    """필수 테스트: 기존 정상 거래 결과/슬롯/청산 결과 변화 0건.

    같은 tick에 새 크로스오버가 없으면(HOLD) 이 수정은 아무 것도 하지 않는다."""
    svc, _df = market_data
    state = _fresh_3slot_state()
    broker = _broker()
    knobs = _Knobs(monkeypatch, entry_decision=_approved())
    state.tw2_3slot_pending_flag_direction = Direction.UP_RED
    # an OLD flag bar so _resolve_tw2_3slot_candidate is past the flag's own
    # bar T and actually resolves this tick (same recipe as
    # test_tw2_3slot_worker_regression.py::_prime_3slot_pending)
    state.tw2_3slot_pending_flag_bar_ts = _DAY.isoformat()
    knobs.cross = Direction.HOLD  # no new flag this tick

    result = run_once(broker=broker, market_data=svc, state=state, now=_NOW0)

    assert any(a.startswith("TW2_3SLOT_ENTRY") for a in result.actions), result.actions
    assert len(broker.orders) == 1
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert state.tw2_3slot_slots_used_today == 1, "slot accounting unchanged"
    assert state.tw2_3slot_pending_flag_direction is None, "no phantom candidate is created"
    rows = ledger.load_signal_ledger(limit=1000)
    assert len(rows) == 1, f"exactly the entry's own row, nothing extra: {rows}"
    assert "EXECUTED" in str(rows[0].get("order_result") or "")
