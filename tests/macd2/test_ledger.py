"""Unit tests for app.trading.macd2.ledger — isolated to tmp_path via conftest.py."""
from __future__ import annotations

import csv
from datetime import datetime

import pytest

from app.trading.macd2 import config, ledger


def _signal_row(signal_id: str, direction: str = "UP_RED", order_result: str = "EXECUTED", block_reason: str = ""):
    return {
        "trading_date": "20260106", "completed_bar_at": "090300", "signal_id": signal_id,
        "signal_type": "INITIAL", "direction": direction, "macd": 1.0, "signal": 0.5,
        "hist_last3": "(0.1,0.2,0.3)", "detected_at": "2026-01-06T09:03:05+09:00",
        "order_requested_at": "2026-01-06T09:03:05+09:00", "order_result": order_result,
        "block_reason": block_reason,
    }


def _current_signal_row(signal_id: str, direction: str = "UP_RED"):
    row = _signal_row(signal_id, direction=direction)
    row.update({
        "strategy_name": "MACD2",
        "strategy_version": config.STRATEGY_VERSION,
        "signal_rule": config.SIGNAL_RULE,
        "session_started_at": "2026-01-06T09:00:00+09:00",
    })
    return row


def _current_signal_at(signal_id: str, completed_hms: str, direction: str, *, session_start: str, baseline: str):
    row = _current_signal_row(signal_id, direction=direction)
    row.update({
        "completed_bar_at": completed_hms,
        "signal_bar_at": f"2026-01-06T{completed_hms[0:2]}:{completed_hms[2:4]}:{completed_hms[4:6]}+09:00",
        "signal_confirmed_at": "",
        "detected_at": session_start,
        "session_started_at": session_start,
        "baseline_completed_bar_at": baseline,
    })
    return row


def _execution_row(order_id: str, side: str = "BUY", net_pnl: float = 0.0, gross_pnl: float = 0.0, fee: float = 0.0):
    return {
        "order_id": order_id, "signal_id": "sid-1", "timestamp": "20260106T090305",
        "mode": "mock", "symbol": "0193T0", "side": side, "requested_qty": 10, "executed_qty": 10,
        "requested_price": 15000.0, "executed_price": 15000.0, "position_before": 0, "position_after": 10,
        "gross_pnl": gross_pnl, "fee": fee, "slippage": 0.0, "net_pnl": net_pnl, "exit_reason": "",
        "broker_response": "{}",
    }


def test_ledger_paths_are_isolated_and_do_not_reference_v1():
    assert "macd_hynix" not in str(ledger.SIGNAL_LEDGER_PATH)
    assert "macd_hynix" not in str(ledger.EXECUTION_LEDGER_PATH)
    assert ledger.SIGNAL_LEDGER_PATH.name == "macd2_signal_ledger.csv"
    assert ledger.EXECUTION_LEDGER_PATH.name == "macd2_execution_ledger.csv"


def test_append_signal_writes_header_once():
    ledger.append_signal(_signal_row("sid-1"))
    ledger.append_signal(_signal_row("sid-2"))
    content = ledger.SIGNAL_LEDGER_PATH.read_text(encoding="utf-8")
    assert content.count("signal_id") == 1  # header appears exactly once
    rows = ledger.load_signal_ledger()
    assert len(rows) == 2


def test_append_signal_dedupes_by_signal_id():
    assert ledger.append_signal(_signal_row("sid-1")) is True
    assert ledger.append_signal(_signal_row("sid-1")) is False
    assert len(ledger.load_signal_ledger()) == 1


def test_append_signal_requires_signal_id():
    row = _signal_row("sid-1")
    row["signal_id"] = ""
    with pytest.raises(ValueError):
        ledger.append_signal(row)


def test_append_signal_aligns_new_row_to_reordered_on_disk_header():
    """2026-07-27 incident: a file already on disk with a header order that
    differs from the current code's canonical SIGNAL_LEDGER_COLUMNS list
    (e.g. from an earlier code version) must never cause a NEW row's values
    to land in the wrong columns. Simulate this by writing a header with
    ``strategy_name`` and ``forming_bar_start`` swapped relative to the
    canonical order, plus one legacy row in that same swapped order, then
    append a new row through the real API and verify both rows still read
    back correctly BY NAME.
    """
    swapped_columns = list(ledger.SIGNAL_LEDGER_COLUMNS)
    i, j = swapped_columns.index("strategy_name"), swapped_columns.index("forming_bar_start")
    swapped_columns[i], swapped_columns[j] = swapped_columns[j], swapped_columns[i]

    legacy_row = _current_signal_row("legacy-1")
    legacy_row["forming_bar_start"] = "2026-01-06T09:03:00+09:00"
    ledger.SIGNAL_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger.SIGNAL_LEDGER_PATH, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=swapped_columns)
        writer.writeheader()
        writer.writerow({col: legacy_row.get(col, "") for col in swapped_columns})

    new_row = _current_signal_row("new-1")
    new_row["forming_bar_start"] = "2026-01-06T09:06:00+09:00"
    assert ledger.append_signal(new_row) is True

    rows = ledger.load_signal_ledger()
    assert len(rows) == 2
    by_id = {r["signal_id"]: r for r in rows}
    assert by_id["legacy-1"]["strategy_name"] == "MACD2"
    assert by_id["legacy-1"]["forming_bar_start"] == "2026-01-06T09:03:00+09:00"
    assert by_id["new-1"]["strategy_name"] == "MACD2"
    assert by_id["new-1"]["forming_bar_start"] == "2026-01-06T09:06:00+09:00"

    # The on-disk header itself is untouched (no rewrite needed — nothing was
    # actually missing, only reordered) so the swapped order is still there;
    # confirming this is what makes the naive "write with canonical order"
    # bug possible if _append_row didn't read it back.
    with open(ledger.SIGNAL_LEDGER_PATH, newline="", encoding="utf-8") as fh:
        on_disk_header = next(csv.reader(fh))
    assert on_disk_header == swapped_columns


def test_append_execution_dedupes_by_order_id():
    assert ledger.append_execution(_execution_row("ord-1")) is True
    assert ledger.append_execution(_execution_row("ord-1")) is False
    assert len(ledger.load_execution_ledger()) == 1


def test_summarize_signals_counts_and_unexecuted():
    ledger.append_signal(_signal_row("sid-1", direction="UP_RED", order_result="EXECUTED"))
    ledger.append_signal(_signal_row("sid-2", direction="DOWN_BLUE", order_result="BLOCKED", block_reason="QUOTE_STALE"))
    ledger.append_signal(_signal_row("sid-3", direction="UP_RED", order_result=""))

    summary = ledger.summarize_signals("20260106")
    assert summary["red_count"] == 2
    assert summary["blue_count"] == 1
    assert summary["signal_count"] == 3
    assert len(summary["unexecuted_signals"]) == 2
    reasons = {u["signal_id"]: u["reason"] for u in summary["unexecuted_signals"]}
    assert reasons["sid-2"] == "QUOTE_STALE"


def test_summarize_signals_filters_old_strategy_rows():
    for i in range(7):
        row = _signal_row(f"old-{i}", order_result="BLOCKED", block_reason="ORDER_DATA_INVALID")
        row.update({"strategy_version": "OLD", "signal_rule": "SIGNED_B_LEGACY"})
        ledger.append_signal(row)

    summary = ledger.summarize_signals(
        "20260106",
        strategy_version=config.STRATEGY_VERSION,
        signal_rule=config.SIGNAL_RULE,
        session_started_at="2026-01-06T09:00:00+09:00",
    )
    assert summary["red_count"] == 0
    assert summary["blue_count"] == 0
    assert summary["signal_count"] == 0
    assert len(summary["excluded_signals"]) == 7


def test_summarize_signals_excludes_rows_from_a_different_worker_code_sha():
    """docs §2: a signal recorded by a different deployed code SHA (a redeploy
    happened mid-session, or a leftover row from an earlier day) never counts
    toward "current" stats — it only ever moves into excluded_signals, the
    on-disk row itself is never touched."""
    old_sha_row = _current_signal_row("cur-red-old-sha", direction="UP_RED")
    old_sha_row["worker_code_sha"] = "aaaaaaa"
    ledger.append_signal(old_sha_row)
    new_sha_row = _current_signal_row("cur-blue-new-sha", direction="DOWN_BLUE")
    new_sha_row["worker_code_sha"] = "bbbbbbb"
    ledger.append_signal(new_sha_row)

    summary = ledger.summarize_signals(
        "20260106",
        strategy_version=config.STRATEGY_VERSION,
        signal_rule=config.SIGNAL_RULE,
        session_started_at="2026-01-06T09:00:00+09:00",
        worker_code_sha="bbbbbbb",
    )

    assert summary["red_count"] == 0
    assert summary["blue_count"] == 1
    excluded_reasons = {r["signal_id"]: r["excluded_reason"] for r in summary["excluded_signals"]}
    assert excluded_reasons["cur-red-old-sha"] == "OLD_WORKER_SHA"

    # No filter passed -> backward-compatible, both rows counted (existing behavior unchanged).
    summary_unfiltered = ledger.summarize_signals(
        "20260106",
        strategy_version=config.STRATEGY_VERSION,
        signal_rule=config.SIGNAL_RULE,
        session_started_at="2026-01-06T09:00:00+09:00",
    )
    assert summary_unfiltered["red_count"] == 1
    assert summary_unfiltered["blue_count"] == 1


def test_summarize_signals_counts_current_strategy_only_and_latest():
    ledger.append_signal(_signal_row("old", direction="UP_RED"))
    ledger.append_signal(_current_signal_row("cur-red", direction="UP_RED"))
    ledger.append_signal(_current_signal_row("cur-blue", direction="DOWN_BLUE"))

    summary = ledger.summarize_signals(
        "20260106",
        strategy_version=config.STRATEGY_VERSION,
        signal_rule=config.SIGNAL_RULE,
        session_started_at="2026-01-06T09:00:00+09:00",
    )
    assert summary["red_count"] == 1
    assert summary["blue_count"] == 1
    assert summary["latest_signal_id"] == "cur-blue"


def test_summarize_signals_uses_baseline_not_session_start_for_pre_session():
    ledger.append_signal(_current_signal_at(
        "20260106_125700_DOWN_BLUE", "125700", "DOWN_BLUE",
        session_start="2026-01-06T12:59:21+09:00",
        baseline="2026-01-06T12:54:00+09:00",
    ))

    summary = ledger.summarize_signals(
        "20260106",
        strategy_version=config.STRATEGY_VERSION,
        signal_rule=config.SIGNAL_RULE,
        session_started_at="2026-01-06T12:59:21+09:00",
        session_baseline_bar_ts="2026-01-06T12:54:00+09:00",
    )

    assert summary["blue_count"] == 1
    assert summary["excluded_signals"] == []


def test_summarize_signals_excludes_baseline_completed_bar_only():
    ledger.append_signal(_current_signal_at(
        "baseline", "125700", "DOWN_BLUE",
        session_start="2026-01-06T13:00:31+09:00",
        baseline="2026-01-06T12:57:00+09:00",
    ))
    ledger.append_signal(_current_signal_at(
        "new", "130000", "DOWN_BLUE",
        session_start="2026-01-06T13:00:31+09:00",
        baseline="2026-01-06T12:57:00+09:00",
    ))

    summary = ledger.summarize_signals(
        "20260106",
        strategy_version=config.STRATEGY_VERSION,
        signal_rule=config.SIGNAL_RULE,
        session_baseline_bar_ts="2026-01-06T12:57:00+09:00",
    )

    assert summary["blue_count"] == 1
    assert summary["current_signal_ids"] == ["new"]
    assert summary["excluded_signals"][0]["excluded_reason"] == "PRE_SESSION_SIGNAL"


def test_summarize_signals_counts_consecutive_same_direction_as_one_onset_and_excludes_old_strategy():
    for i in range(8):
        row = _signal_row(f"old-{i}", direction="UP_RED")
        row.update({"strategy_version": "OLD", "signal_rule": "SIGNED_B_LEGACY"})
        ledger.append_signal(row)
    ledger.append_signal(_current_signal_at(
        "down-1257", "125700", "DOWN_BLUE",
        session_start="2026-01-06T12:59:21+09:00",
        baseline="2026-01-06T12:54:00+09:00",
    ))
    ledger.append_signal(_current_signal_at(
        "down-1300", "130000", "DOWN_BLUE",
        session_start="2026-01-06T13:00:31+09:00",
        baseline="2026-01-06T12:57:00+09:00",
    ))

    summary = ledger.summarize_signals(
        "20260106",
        strategy_version=config.STRATEGY_VERSION,
        signal_rule=config.SIGNAL_RULE,
    )

    assert summary["red_count"] == 0
    assert summary["blue_count"] == 1
    assert summary["signal_count"] == 1
    assert len([r for r in summary["excluded_signals"] if r["excluded_reason"] == "OLD_STRATEGY"]) == 8


def test_summarize_signals_excludes_malformed_schema_rows():
    """2026-07-27 incident: a row whose strategy_name column holds something
    other than "MACD2" (e.g. a forming_bar_start value that landed there via
    a column-order mismatch) must be excluded as MALFORMED_SCHEMA — never
    silently counted toward today's red/blue stats."""
    good = _current_signal_row("good-1", direction="UP_RED")
    ledger.append_signal(good)
    corrupted = _current_signal_row("corrupted-1", direction="DOWN_BLUE")
    corrupted["strategy_name"] = "2026-07-27T09:06:00+09:00"  # shifted value, not "MACD2"
    ledger.append_signal(corrupted)
    corrupted_direction = _current_signal_row("corrupted-2", direction="2026-07-27T09:07:00+09:00")
    ledger.append_signal(corrupted_direction)

    summary = ledger.summarize_signals(
        "20260106",
        strategy_version=config.STRATEGY_VERSION,
        signal_rule=config.SIGNAL_RULE,
        session_started_at="2026-01-06T09:00:00+09:00",
    )

    assert summary["red_count"] == 1
    assert summary["blue_count"] == 0
    assert summary["current_signal_ids"] == ["good-1"]
    malformed = [r for r in summary["excluded_signals"] if r["excluded_reason"] == "MALFORMED_SCHEMA"]
    assert {r["signal_id"] for r in malformed} == {"corrupted-1", "corrupted-2"}


def test_summarize_signals_excludes_six_previous_malformed_rows_from_today_stats():
    """6건의 이전 malformed 행(실제 사고 재현 규모)이 오늘 통계에서 모두
    제외되고, 정상 오늘 신호만 카운트된다."""
    for i in range(6):
        corrupted = _current_signal_row(f"malformed-{i}", direction="UP_RED" if i % 2 == 0 else "DOWN_BLUE")
        corrupted["strategy_name"] = f"2026-07-2{i}T09:0{i}:00+09:00"  # shifted column value
        ledger.append_signal(corrupted)
    ledger.append_signal(_current_signal_row("today-good", direction="UP_RED"))

    summary = ledger.summarize_signals(
        "20260106",
        strategy_version=config.STRATEGY_VERSION,
        signal_rule=config.SIGNAL_RULE,
        session_started_at="2026-01-06T09:00:00+09:00",
    )

    assert summary["current_signal_ids"] == ["today-good"]
    assert summary["red_count"] == 1
    assert summary["blue_count"] == 0
    malformed = [r for r in summary["excluded_signals"] if r["excluded_reason"] == "MALFORMED_SCHEMA"]
    assert len(malformed) == 6
    assert {r["signal_id"] for r in malformed} == {f"malformed-{i}" for i in range(6)}


def test_summarize_signals_excludes_pre_session_rows_by_detected_at():
    """summarize_signals' session_started_at argument must actually filter —
    a row detected BEFORE the current Worker session started (leftover from a
    previous run/test on the same trading date) is excluded as
    PRE_SESSION_ROW, while a row detected after session start on the SAME
    date is still counted normally (not misclassified as OLD_STRATEGY)."""
    previous_session_row = _current_signal_row("prev-session-1", direction="UP_RED")
    previous_session_row["detected_at"] = "2026-01-06T08:00:00+09:00"
    ledger.append_signal(previous_session_row)
    current_session_row = _current_signal_row("cur-session-1", direction="DOWN_BLUE")
    current_session_row["detected_at"] = "2026-01-06T09:05:00+09:00"
    ledger.append_signal(current_session_row)

    summary = ledger.summarize_signals(
        "20260106",
        strategy_version=config.STRATEGY_VERSION,
        signal_rule=config.SIGNAL_RULE,
        session_started_at="2026-01-06T09:00:00+09:00",
    )

    assert summary["red_count"] == 0
    assert summary["blue_count"] == 1
    assert summary["current_signal_ids"] == ["cur-session-1"]
    excluded_reasons = {r["signal_id"]: r["excluded_reason"] for r in summary["excluded_signals"]}
    assert excluded_reasons["prev-session-1"] == "PRE_SESSION_ROW"


def test_summarize_daily_trading_empty_ledger_does_not_raise():
    summary = ledger.summarize_daily_trading("20260106", budget=10_000_000)
    assert summary["has_data"] is False
    assert summary["round_trip_count"] == 0
    assert summary["net_pnl"] == 0.0


def test_summarize_daily_trading_computes_pnl_and_stats():
    ledger.append_execution(_execution_row("ord-1", side="BUY", net_pnl=0.0, gross_pnl=0.0, fee=100.0))
    ledger.append_execution(_execution_row("ord-2", side="SELL", net_pnl=5000.0, gross_pnl=5200.0, fee=200.0))
    ledger.append_execution(_execution_row("ord-3", side="BUY", net_pnl=0.0, gross_pnl=0.0, fee=100.0))
    ledger.append_execution(_execution_row("ord-4", side="SELL", net_pnl=-2000.0, gross_pnl=-1800.0, fee=200.0))

    summary = ledger.summarize_daily_trading("20260106", budget=10_000_000)
    assert summary["has_data"] is True
    assert summary["buy_count"] == 2
    assert summary["sell_count"] == 2
    assert summary["round_trip_count"] == 2
    assert summary["net_pnl"] == 3000.0
    assert summary["win_rate_pct"] == 50.0
    assert summary["profit_factor"] == pytest.approx(2.5)
    assert summary["max_drawdown"] == 2000.0  # peak 5000 -> trough 3000


def test_summarize_daily_trading_matches_iso_kst_timestamps_only_for_requested_day():
    row = _execution_row("iso-buy", side="BUY", fee=100.0)
    row["timestamp"] = "2026-07-31T09:03:05+09:00"
    ledger.append_execution(row)
    row = _execution_row("iso-sell", side="SELL", net_pnl=5000.0, gross_pnl=5200.0, fee=200.0)
    row["timestamp"] = "2026-07-31T09:09:05+09:00"
    ledger.append_execution(row)
    row = _execution_row("prior-day-sell", side="SELL", net_pnl=999999.0, gross_pnl=999999.0, fee=1.0)
    row["timestamp"] = "2026-07-30T15:00:00+09:00"
    ledger.append_execution(row)

    rows = ledger.load_execution_ledger()
    today_rows = ledger.filter_execution_rows_by_trading_date(rows, "20260731")
    summary = ledger.summarize_daily_trading("20260731", budget=10_000_000)

    assert [r["order_id"] for r in today_rows] == ["iso-buy", "iso-sell"]
    assert summary["has_data"] is True
    assert summary["buy_count"] == 1
    assert summary["sell_count"] == 1
    assert summary["net_pnl"] == 5000.0


def test_backfill_broker_direct_fills_inserts_and_updates_by_order_id():
    fill = {
        "order_id": "direct-1",
        "symbol": "0193T0",
        "side": "BUY",
        "quantity": 1,
        "price": 15000,
        "timestamp": "20260727132133",
    }

    assert ledger.backfill_broker_direct_fills([fill], mode="mock") == {
        "scanned": 1,
        "written": 1,
        "skipped": 0,
    }
    rows = ledger.load_execution_ledger()
    assert rows[0]["timestamp"] == "2026-07-27T13:21:33+09:00"
    assert rows[0]["executed_price"] == "15000.0"
    assert rows[0]["exit_reason"] == "BROKER_DIRECT_FILL_BACKFILL"

    fill["price"] = 15100
    fill["timestamp"] = "20260727132200"
    assert ledger.backfill_broker_direct_fills([fill], mode="mock")["written"] == 1
    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["timestamp"] == "2026-07-27T13:22:00+09:00"
    assert rows[0]["executed_price"] == "15100.0"


class _FakeOrderResult:
    """Minimal stand-in for app.models.OrderResult -- only the attributes
    append_broker_direct_execution actually reads."""

    def __init__(self, *, order_id, symbol, side, quantity, price, raw, timestamp="", mode="mock"):
        self.success = True
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.price = price
        self.raw = raw
        self.timestamp = timestamp
        self.mode = mode


class _FakeQuoteBroker:
    def __init__(self, price):
        self._price = price

    def get_current_price(self, symbol):
        del symbol
        return self._price


def test_broker_direct_execution_prefers_ord_tmd_over_naive_local_clock_timestamp():
    """2026-08-28 real incident: OrderResult.timestamp's default is naive
    SERVER-LOCAL clock time (UTC on Render), which _normalize_execution_
    timestamp used to mislabel as already-KST (a 12:46:33 KST order recorded
    as 03:46:33+09:00 -- the raw UTC clock reading with a KST offset stapled
    on, never actually converted). ORD_TMD (KIS's own order-time field, from
    the same order response) is always genuine KST and must win."""
    order_result = _FakeOrderResult(
        order_id="direct-tmd-1", symbol=config.INVERSE_SYMBOL, side="SELL",
        quantity=557, price=0.0, raw={"ODNO": "direct-tmd-1", "ORD_TMD": "124633"},
        timestamp="2026-08-28 03:46:33",  # naive server-local (UTC) clock value
    )

    assert ledger.append_broker_direct_execution(order_result) is True

    rows = ledger.load_execution_ledger()
    assert rows[0]["timestamp"].startswith(datetime.now(config.KST).strftime("%Y-%m-%d"))
    assert rows[0]["timestamp"].endswith("T12:46:33+09:00")


def test_broker_direct_execution_fills_real_price_and_fee_for_market_order():
    """A market SELL always requests price=0 (see order_executor._fallback_
    sell_price's identical reasoning) -- the BROKER_DIRECT stub used to
    blindly echo that 0 as executed_price and leave fee/gross_pnl/net_pnl at
    a misleading 0.0. It must now look up a real quote for executed_price
    and compute a real fee, while leaving gross_pnl/net_pnl genuinely blank
    (no entry-price context exists at this generic broker-layer hook)."""
    order_result = _FakeOrderResult(
        order_id="direct-price-1", symbol=config.INVERSE_SYMBOL, side="SELL",
        quantity=557, price=0.0, raw={"ODNO": "direct-price-1", "ORD_TMD": "124633"},
    )
    broker = _FakeQuoteBroker(6950.0)

    assert ledger.append_broker_direct_execution(order_result, broker=broker) is True

    row = ledger.load_execution_ledger()[0]
    assert row["requested_price"] == "0.0"
    assert row["executed_price"] == "6950.0"
    assert float(row["fee"]) > 0.0
    assert row["gross_pnl"] == ""
    assert row["net_pnl"] == ""


def test_append_execution_upgrades_broker_direct_placeholder_with_real_leg():
    """2026-08-28 real incident: a BROKER_DIRECT placeholder (written
    synchronously inside the broker call, price=0/pnl=0) and order_executor.
    _record_leg's real, fully-priced row for the SAME order_id both target
    the same order_id -- the real row must win, not lose to first-write-wins
    dedup (which used to silently drop it, leaving the placeholder as the
    ONLY record of a real trade)."""
    placeholder = _execution_row("shared-1", side="SELL", net_pnl=0.0, gross_pnl=0.0, fee=0.0)
    placeholder["signal_id"] = "BROKER_DIRECT"
    placeholder["executed_price"] = 0.0
    assert ledger.append_execution(placeholder) is True

    real_leg = _execution_row("shared-1", side="SELL", net_pnl=-1234.5, gross_pnl=-1000.0, fee=234.5)
    real_leg["signal_id"] = ""
    real_leg["executed_price"] = 6950.0
    assert ledger.append_execution(real_leg) is True

    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["signal_id"] == ""
    assert rows[0]["executed_price"] == "6950.0"
    assert rows[0]["net_pnl"] == "-1234.5"

    # A LATER placeholder must never clobber an already-real row (the reverse
    # direction was already guarded by _upsert_broker_direct_execution).
    late_placeholder = _execution_row("shared-1", side="SELL", net_pnl=0.0, gross_pnl=0.0, fee=0.0)
    late_placeholder["signal_id"] = "BROKER_DIRECT"
    assert ledger.append_execution(late_placeholder) is False
    assert ledger.load_execution_ledger()[0]["net_pnl"] == "-1234.5"


def test_reconcile_backfill_sell_computes_real_pnl_and_is_idempotent():
    order_id = ledger.append_reconcile_backfill_sell(
        symbol=config.INVERSE_SYMBOL, quantity=557, exit_price=6950.0, entry_price=6940.0,
        position_before=557, position_after=0, reconciled_at="2026-08-28T13:00:00+09:00",
        mode="mock", exit_reason="RECOVERED_TO_FLAT",
    )
    assert order_id is True
    row = ledger.load_execution_ledger()[0]
    assert row["source"] == "RECONCILE_BACKFILL"
    assert row["side"] == "SELL"
    assert row["exit_reason"] == "RECOVERED_TO_FLAT"
    assert float(row["net_pnl"]) > 0.0  # exit (6950) > entry (6940), fees are small vs the move
    assert row["position_before"] == "557"
    assert row["position_after"] == "0"

    # Idempotent: re-detecting the exact same gap must never double-write.
    again = ledger.append_reconcile_backfill_sell(
        symbol=config.INVERSE_SYMBOL, quantity=557, exit_price=6950.0, entry_price=6940.0,
        position_before=557, position_after=0, reconciled_at="2026-08-28T13:00:05+09:00",
        mode="mock", exit_reason="RECOVERED_TO_FLAT",
    )
    assert again is False
    assert len(ledger.load_execution_ledger()) == 1


def test_reconcile_backfill_sell_dedup_does_not_depend_on_moved_quote():
    first = ledger.append_reconcile_backfill_sell(
        symbol=config.INVERSE_SYMBOL, quantity=557, exit_price=6950.0, entry_price=6940.0,
        position_before=557, position_after=0, reconciled_at="2026-08-31T10:00:00+09:00",
        mode="mock", exit_reason="RECOVERED_TO_FLAT",
    )
    assert first is True

    # The missing SELL is the same reconcile gap. A later quote is only a
    # discovery-time estimate, not a distinct execution identity.
    again = ledger.append_reconcile_backfill_sell(
        symbol=config.INVERSE_SYMBOL, quantity=557, exit_price=6975.0, entry_price=6940.0,
        position_before=557, position_after=0, reconciled_at="2026-08-31T10:00:05+09:00",
        mode="mock", exit_reason="RECOVERED_TO_FLAT",
    )
    assert again is False
    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["executed_price"] == "6950.0"


def test_reconcile_backfill_sell_skips_when_real_sell_leg_already_exists():
    real_sell = _execution_row(
        "000831091831", side="SELL", net_pnl=-136407.99, gross_pnl=-130000.0, fee=6407.99,
    )
    real_sell.update({
        "timestamp": "2026-08-31T09:18:31+09:00",
        "symbol": config.INVERSE_SYMBOL,
        "requested_qty": 1019,
        "executed_qty": 1019,
        "requested_price": 7775.0,
        "executed_price": 7775.0,
        "position_before": 1019,
        "position_after": 0,
        "exit_reason": "TIME_WINDOW_STOP_LOSS",
        "source": "",
    })
    assert ledger.append_execution(real_sell) is True

    backfill = ledger.append_reconcile_backfill_sell(
        symbol=config.INVERSE_SYMBOL, quantity=1019, exit_price=7775.0, entry_price=7900.0,
        position_before=1019, position_after=0, reconciled_at="2026-08-31T09:18:35+09:00",
        mode="mock", exit_reason="RECOVERED_TO_FLAT",
    )

    assert backfill is False
    rows = ledger.load_execution_ledger()
    assert len(rows) == 1
    assert rows[0]["order_id"] == "000831091831"
    assert rows[0]["net_pnl"] == "-136407.99"


def test_reconcile_backfill_sell_allows_same_qty_later_independent_gap():
    real_sell = _execution_row(
        "000831091831", side="SELL", net_pnl=-136407.99, gross_pnl=-130000.0, fee=6407.99,
    )
    real_sell.update({
        "timestamp": "2026-08-31T09:18:31+09:00",
        "symbol": config.INVERSE_SYMBOL,
        "requested_qty": 1019,
        "executed_qty": 1019,
        "requested_price": 7775.0,
        "executed_price": 7775.0,
        "position_before": 1019,
        "position_after": 0,
        "exit_reason": "TIME_WINDOW_STOP_LOSS",
        "source": "",
    })
    assert ledger.append_execution(real_sell) is True

    later_backfill = ledger.append_reconcile_backfill_sell(
        symbol=config.INVERSE_SYMBOL, quantity=1019, exit_price=7800.0, entry_price=7900.0,
        position_before=1019, position_after=0, reconciled_at="2026-08-31T09:25:00+09:00",
        mode="mock", exit_reason="RECOVERED_TO_FLAT",
    )

    assert later_backfill is True
    assert len(ledger.load_execution_ledger()) == 2


def test_reconcile_backfill_sell_skips_when_same_backfill_event_already_exists_with_old_order_id():
    old_backfill = _execution_row(
        "OLD_RECONCILE_BACKFILL_SELL_0197X0_1019_7775", side="SELL",
        net_pnl=-136407.99, gross_pnl=-130000.0, fee=6407.99,
    )
    old_backfill.update({
        "timestamp": "2026-08-31T09:18:35+09:00",
        "symbol": config.INVERSE_SYMBOL,
        "requested_qty": 1019,
        "executed_qty": 1019,
        "requested_price": 7775.0,
        "executed_price": 7775.0,
        "position_before": 1019,
        "position_after": 0,
        "exit_reason": "RECOVERED_TO_FLAT",
        "source": "RECONCILE_BACKFILL",
    })
    assert ledger.append_execution(old_backfill) is True

    assert ledger.append_reconcile_backfill_sell(
        symbol=config.INVERSE_SYMBOL, quantity=1019, exit_price=7780.0, entry_price=7900.0,
        position_before=1019, position_after=0, reconciled_at="2026-08-31T09:18:38+09:00",
        mode="mock", exit_reason="RECOVERED_TO_FLAT",
    ) is False
    assert len(ledger.load_execution_ledger()) == 1


def test_reconcile_backfill_sell_skips_when_broker_sell_order_without_position_fields_exists():
    broker_sell = _execution_row(
        "000831091831", side="SELL", net_pnl=-136407.99, gross_pnl=-130000.0, fee=6407.99,
    )
    broker_sell.update({
        "timestamp": "2026-08-31T09:18:31+09:00",
        "signal_id": "BROKER_DIRECT",
        "symbol": config.INVERSE_SYMBOL,
        "requested_qty": 1019,
        "executed_qty": 1019,
        "requested_price": 7775.0,
        "executed_price": 7775.0,
        "position_before": "",
        "position_after": "",
        "exit_reason": "BROKER_DIRECT",
        "source": "",
    })
    assert ledger.append_execution(broker_sell) is True

    assert ledger.append_reconcile_backfill_sell(
        symbol=config.INVERSE_SYMBOL, quantity=1019, exit_price=7775.0, entry_price=7900.0,
        position_before=1019, position_after=0, reconciled_at="2026-08-31T09:18:35+09:00",
        mode="mock", exit_reason="RECOVERED_TO_FLAT",
    ) is False


def test_daily_summary_ignores_existing_duplicate_reconcile_sell_backfills_when_real_sell_exists():
    real_sell = _execution_row(
        "000831091831", side="SELL", net_pnl=-136407.99, gross_pnl=-130000.0, fee=6407.99,
    )
    real_sell.update({
        "timestamp": "2026-08-31T09:18:31+09:00",
        "symbol": config.INVERSE_SYMBOL,
        "requested_qty": 1019,
        "executed_qty": 1019,
        "requested_price": 7775.0,
        "executed_price": 7775.0,
        "position_before": 1019,
        "position_after": 0,
        "exit_reason": "TIME_WINDOW_STOP_LOSS",
        "source": "",
    })
    assert ledger.append_execution(real_sell) is True

    for second in (35, 38, 43, 47):
        # Simulate bad rows already written by the previous reconcile bug.
        row = _execution_row(
            f"RECONCILE_BACKFILL_SELL_20260831_0197X0_1019_{second}",
            side="SELL", net_pnl=-136407.99, gross_pnl=-130000.0, fee=6407.99,
        )
        row.update({
            "timestamp": f"2026-08-31T09:18:{second:02d}+09:00",
            "symbol": config.INVERSE_SYMBOL,
            "requested_qty": 1019,
            "executed_qty": 1019,
            "requested_price": 7775.0,
            "executed_price": 7775.0,
            "position_before": 1019,
            "position_after": 0,
            "exit_reason": "RECOVERED_TO_FLAT",
            "source": "RECONCILE_BACKFILL",
        })
        assert ledger.append_execution(row) is True

    raw_rows = ledger.load_execution_ledger()
    assert len(raw_rows) == 5

    countable_rows = ledger.filter_execution_rows_by_trading_date(raw_rows, "20260831")
    assert len(countable_rows) == 1
    assert countable_rows[0]["order_id"] == "000831091831"

    summary = ledger.summarize_daily_trading("20260831", budget=10_000_000)
    assert summary["sell_count"] == 1
    assert summary["round_trip_count"] == 1
    assert summary["net_pnl"] == pytest.approx(-136407.99)


def test_daily_summary_counts_same_qty_later_independent_reconcile_sell():
    real_sell = _execution_row(
        "000831091831", side="SELL", net_pnl=-136407.99, gross_pnl=-130000.0, fee=6407.99,
    )
    real_sell.update({
        "timestamp": "2026-08-31T09:18:31+09:00",
        "symbol": config.INVERSE_SYMBOL,
        "requested_qty": 1019,
        "executed_qty": 1019,
        "requested_price": 7775.0,
        "executed_price": 7775.0,
        "position_before": 1019,
        "position_after": 0,
        "exit_reason": "TIME_WINDOW_STOP_LOSS",
        "source": "",
    })
    assert ledger.append_execution(real_sell) is True

    assert ledger.append_reconcile_backfill_sell(
        symbol=config.INVERSE_SYMBOL, quantity=1019, exit_price=7800.0, entry_price=7900.0,
        position_before=1019, position_after=0, reconciled_at="2026-08-31T09:25:00+09:00",
        mode="mock", exit_reason="RECOVERED_TO_FLAT",
    ) is True

    summary = ledger.summarize_daily_trading("20260831", budget=10_000_000)
    assert summary["sell_count"] == 2
    assert summary["round_trip_count"] == 2


def test_daily_summary_ignores_reconcile_backfill_when_broker_sell_order_exists_without_position_fields():
    broker_sell = _execution_row(
        "000831091831", side="SELL", net_pnl=-136407.99, gross_pnl=-130000.0, fee=6407.99,
    )
    broker_sell.update({
        "timestamp": "2026-08-31T09:18:31+09:00",
        "signal_id": "BROKER_DIRECT",
        "symbol": config.INVERSE_SYMBOL,
        "requested_qty": 1019,
        "executed_qty": 1019,
        "requested_price": 7775.0,
        "executed_price": 7775.0,
        "position_before": "",
        "position_after": "",
        "exit_reason": "BROKER_DIRECT",
        "source": "",
    })
    assert ledger.append_execution(broker_sell) is True

    duplicate_backfill = _execution_row(
        "RECONCILE_BACKFILL_SELL_20260831_0197X0_1019", side="SELL",
        net_pnl=-136407.99, gross_pnl=-130000.0, fee=6407.99,
    )
    duplicate_backfill.update({
        "timestamp": "2026-08-31T09:18:35+09:00",
        "symbol": config.INVERSE_SYMBOL,
        "requested_qty": 1019,
        "executed_qty": 1019,
        "requested_price": 7775.0,
        "executed_price": 7775.0,
        "position_before": 1019,
        "position_after": 0,
        "exit_reason": "RECOVERED_TO_FLAT",
        "source": "RECONCILE_BACKFILL",
    })
    assert ledger.append_execution(duplicate_backfill) is True

    summary = ledger.summarize_daily_trading("20260831", budget=10_000_000)
    assert summary["sell_count"] == 1
    assert summary["round_trip_count"] == 1
    assert summary["net_pnl"] == pytest.approx(-136407.99)


def test_partial_exit_then_final_exit_round_trip_sums_correctly():
    """docs 시나리오: TP1 50% 부분익절 후, 남은 수량이 반대신호로 전량 청산되면
    하나의 왕복거래(단일 BUY qty에 대한 두 건의 SELL)로 gross/net PnL이 올바르게
    합산되어야 한다 -- 부분매도 한 건이 원장에서 빠지면 summarize_daily_trading의
    round_trip_count/net_pnl이 조용히 틀어진다."""
    ledger.append_execution(_execution_row("buy-1", side="BUY", net_pnl=0.0, gross_pnl=0.0, fee=100.0))
    ledger.append_execution(_execution_row("tp1-sell", side="SELL", net_pnl=2000.0, gross_pnl=2200.0, fee=200.0))
    ledger.append_reconcile_backfill_sell(
        symbol="0193T0", quantity=557, exit_price=15200.0, entry_price=15000.0,
        position_before=557, position_after=0, reconciled_at="2026-01-06T09:30:00+09:00",
        mode="mock", exit_reason="RECOVERED_TO_FLAT",
    )

    summary = ledger.summarize_daily_trading("20260106", budget=10_000_000)
    assert summary["has_data"] is True
    assert summary["sell_count"] == 2
    assert summary["round_trip_count"] == 2
    backfill_row = [r for r in ledger.load_execution_ledger() if r["source"] == "RECONCILE_BACKFILL"][0]
    assert summary["net_pnl"] == pytest.approx(2000.0 + float(backfill_row["net_pnl"]))


def test_2026_08_28_incident_full_replay_both_sell_legs_recorded_correctly():
    """End-to-end replay of the real 2026-08-28 0197X0 incident using the
    ACTUAL code paths (not hand-built rows), to verify all three fixed
    mechanisms integrate correctly for the exact real sequence:

      1. BUY 1114 @ 6940 (order-executor _record_leg-equivalent write).
      2. TP1 50% partial exit (557 = round(1114*0.5)) -- the broker call
         first writes an unpriced BROKER_DIRECT stub (append_broker_direct_
         execution, exactly what KisMockBroker.sell() does), THEN order_
         executor._record_leg writes the real, fully-priced row for the
         SAME order_id/order (append_execution's upgrade path).
      3. The remaining 557 later closes with no order-executor leg recorded
         at all (the real incident's missing-final-sell gap) -- reconcile's
         RECOVERED_TO_FLAT backfill (append_reconcile_backfill_sell) is the
         only thing that records it.

    Asserts exactly 2 SELL rows (the stub must NOT survive as a 3rd row),
    each with real order_id, real fill time (from ORD_TMD, not the naive-
    clock bug), real executed_price, real fee, and a real (non-blank,
    non-zero) net_pnl -- and that the full round trip (1 BUY + 2 SELL legs
    covering all 1114 shares) aggregates correctly.
    """
    from app.trading.trading_cost_engine import TradeCostEngine

    entry_price = 6940.0
    entry_qty = 1114
    tp1_qty = 557  # round(1114 * 0.5) == config.MORNING_TP1_SELL_RATIO
    remaining_qty = entry_qty - tp1_qty
    tp1_fill_price = 6950.0
    final_fill_price = 6900.0

    # 1) BUY leg (mirrors order_executor._record_leg's BUY branch).
    buy_cost = TradeCostEngine().compute_trade_cost(
        config.INVERSE_SYMBOL, "BUY", entry_price, entry_qty, order_type="market",
    )
    ledger.append_execution({
        "order_id": "0000023671", "signal_id": "20260828_112400_DOWN_BLUE:TW_CONFIRM",
        "timestamp": "2026-08-28T11:30:30+09:00", "mode": "mock", "symbol": config.INVERSE_SYMBOL,
        "side": "BUY", "requested_qty": entry_qty, "executed_qty": entry_qty,
        "requested_price": entry_price, "executed_price": entry_price,
        "position_before": 0, "position_after": entry_qty,
        "gross_pnl": 0.0, "fee": buy_cost["fee"], "slippage": 0.0, "net_pnl": 0.0,
        "exit_reason": "", "broker_response": "{}",
    })

    # 2a) TP1's broker call writes the unpriced BROKER_DIRECT stub FIRST --
    # exactly what KisMockBroker.sell()/_record_direct_execution_if_needed
    # does whenever suppression doesn't apply.
    tp1_order_result = _FakeOrderResult(
        order_id="0000028615", symbol=config.INVERSE_SYMBOL, side="SELL",
        quantity=tp1_qty, price=0.0,
        raw={"ODNO": "0000028615", "ORD_TMD": "124633"},
    )
    quote_broker = _FakeQuoteBroker(tp1_fill_price)
    assert ledger.append_broker_direct_execution(tp1_order_result, broker=quote_broker) is True

    stub_row = [r for r in ledger.load_execution_ledger() if r["order_id"] == "0000028615"][0]
    assert stub_row["signal_id"] == "BROKER_DIRECT"
    assert stub_row["executed_price"] == str(tp1_fill_price)
    assert stub_row["timestamp"].endswith("T12:46:33+09:00")  # ORD_TMD, not the naive-clock bug

    # 2b) order_executor._record_leg's real SELL write for the SAME order_id
    # (mirrors _record_leg's own SELL branch exactly: TradeCostEngine.
    # compute_net_pnl(entry_price, fill_price, qty), fee=sell_fee).
    tp1_cost = TradeCostEngine().compute_net_pnl(
        config.INVERSE_SYMBOL, entry_price, tp1_fill_price, tp1_qty,
        buy_order_type="market", sell_order_type="market",
    )
    assert ledger.append_execution({
        "order_id": "0000028615", "signal_id": "20260828_113000_TW2_TP1", "timestamp": "2026-08-28T12:46:33+09:00",
        "mode": "mock", "symbol": config.INVERSE_SYMBOL, "side": "SELL",
        "requested_qty": tp1_qty, "executed_qty": tp1_qty,
        "requested_price": tp1_fill_price, "executed_price": tp1_fill_price,
        "position_before": entry_qty, "position_after": remaining_qty,
        "gross_pnl": tp1_cost["gross_pnl"], "fee": tp1_cost["sell_fee"], "slippage": tp1_cost["slippage"],
        "net_pnl": tp1_cost["net_pnl"], "exit_reason": "TIME_WINDOW_TP1_PARTIAL", "broker_response": "{}",
    }) is True

    # 3) The remaining 557 vanish at the broker with no order-executor leg
    # ever recorded -- reconcile's RECOVERED_TO_FLAT backfill is the only
    # thing that records this SELL.
    assert ledger.append_reconcile_backfill_sell(
        symbol=config.INVERSE_SYMBOL, quantity=remaining_qty, exit_price=final_fill_price,
        entry_price=entry_price, position_before=remaining_qty, position_after=0,
        reconciled_at="2026-08-28T13:35:00+09:00", mode="mock", exit_reason="RECOVERED_TO_FLAT",
    ) is True

    rows = ledger.load_execution_ledger()
    sell_rows = sorted([r for r in rows if r["side"] == "SELL"], key=lambda r: r["timestamp"])
    assert len(sell_rows) == 2  # the stub was upgraded in place, never survives as a 3rd row
    assert [r["order_id"] for r in rows if r["side"] == "BUY"] == ["0000023671"]

    tp1_row, final_row = sell_rows
    assert tp1_row["order_id"] == "0000028615"
    assert tp1_row["signal_id"] != "BROKER_DIRECT"
    assert tp1_row["executed_qty"] == str(tp1_qty)
    assert tp1_row["executed_price"] == str(tp1_fill_price)
    assert float(tp1_row["fee"]) > 0.0
    assert float(tp1_row["net_pnl"]) != 0.0
    assert tp1_row["timestamp"].endswith("T12:46:33+09:00")

    assert final_row["executed_qty"] == str(remaining_qty)
    assert final_row["executed_price"] == str(final_fill_price)
    assert final_row["source"] == "RECONCILE_BACKFILL"
    assert float(final_row["fee"]) > 0.0
    assert float(final_row["net_pnl"]) != 0.0

    # Full round trip: BUY 1114 + SELL 557 + SELL 557 covers every share,
    # and the daily summary sees exactly one BUY / two SELL legs.
    assert int(tp1_row["executed_qty"]) + int(final_row["executed_qty"]) == entry_qty
    summary = ledger.summarize_daily_trading("20260828", budget=10_000_000)
    assert summary["buy_count"] == 1
    assert summary["sell_count"] == 2
    assert summary["net_pnl"] == pytest.approx(float(tp1_row["net_pnl"]) + float(final_row["net_pnl"]))

    # Idempotency: re-running the reconcile backfill for the exact same gap
    # (e.g. the next tick re-observing the same already-flat broker state
    # before state.position is durably persisted) must never double-write.
    assert ledger.append_reconcile_backfill_sell(
        symbol=config.INVERSE_SYMBOL, quantity=remaining_qty, exit_price=final_fill_price,
        entry_price=entry_price, position_before=remaining_qty, position_after=0,
        reconciled_at="2026-08-28T13:35:05+09:00", mode="mock", exit_reason="RECOVERED_TO_FLAT",
    ) is False
    assert len(ledger.load_execution_ledger()) == 3
