"""Unit tests for app.trading.macd2.ledger — isolated to tmp_path via conftest.py."""
from __future__ import annotations

import csv

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
