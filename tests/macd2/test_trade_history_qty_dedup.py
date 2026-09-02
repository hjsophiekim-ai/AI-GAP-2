"""Regression test for the 2026-09-01 real incident: the "매매내역 한눈에
보기" trade-history table showed 2,220 shares for an actual 1,110-share
inverse (0197X0) fill. Root cause: a real order_executor._record_leg row and
a LATER ledger.append_reconcile_backfill_buy row can both get written for
the SAME underlying fill (different order_ids by construction, so
ledger.append_execution's own order_id dedup never catches the pair) when a
reconcile pass notices the broker-side position increase slightly before the
real order's own fill-confirmation polling finishes. Both rows land in the
same _aggregate_trade_legs group (same symbol/side, within gap_minutes) and
their quantities were summed, silently doubling the displayed total. Fixed
by app/ui/pages/11_MACD_자동매매2.py's new _dedupe_reconcile_restatements().

Imports the page module directly via importlib (it is a Streamlit page file
with a Korean/digit-prefixed name, not a normal importable module) and calls
its pure aggregation functions with synthetic rows -- no Streamlit runtime,
no ledger/state files touched.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_APP_PATH = Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py"


@pytest.fixture(scope="module")
def ui_page():
    spec = importlib.util.spec_from_file_location("macd2_ui_page_under_test", _APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["macd2_ui_page_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _base_row(**overrides) -> dict:
    row = {
        "order_id": "REAL-ORD-1", "signal_id": "sid-1", "symbol": "0197X0", "side": "BUY",
        "executed_qty": 1110, "executed_price": 5000.0, "timestamp": "2026-08-31T09:06:00+09:00",
        "position_before": 0, "position_after": 1110, "fee": 100.0, "net_pnl": 0.0, "source": "",
        "exit_reason": "",
    }
    row.update(overrides)
    return row


def test_real_row_plus_backfill_restatement_does_not_double_count(ui_page):
    real_row = _base_row(order_id="REAL-ORD-1")
    backfill_row = _base_row(
        order_id="RECONCILE_BACKFILL_BUY_0197X0_1110_5000.0_0_1110",
        signal_id="", timestamp="2026-08-31T09:07:30+09:00", fee=0.0,
        source="RECONCILE_BACKFILL",
    )
    rows = ui_page._trade_history_rows([real_row, backfill_row], [])
    assert len(rows) == 1
    assert rows[0]["총 체결수량"] == "1,110주"


def test_genuine_incremental_partial_fill_still_sums_correctly(ui_page):
    leg1 = _base_row(order_id="REAL-ORD-A", executed_qty=555, position_before=0, position_after=555,
                      timestamp="2026-08-31T09:06:00+09:00")
    leg2 = _base_row(order_id="REAL-ORD-B", executed_qty=555, position_before=555, position_after=1110,
                      timestamp="2026-08-31T09:06:30+09:00")
    rows = ui_page._trade_history_rows([leg1, leg2], [])
    assert len(rows) == 1
    assert rows[0]["총 체결수량"] == "1,110주"


def test_two_independent_backfills_for_different_transitions_both_kept(ui_page):
    # Same symbol/side, but genuinely different position transitions -- the
    # dedup filter itself must not collapse these (unrelated to the separate,
    # pre-existing "orphan group with no real decision" hiding rule, so this
    # exercises _dedupe_reconcile_restatements directly rather than the full
    # _trade_history_rows -> _aggregate_trade_legs orphan-hiding pipeline).
    backfill_a = _base_row(order_id="RECONCILE_BACKFILL_BUY_0197X0_500_5000.0_0_500",
                            source="RECONCILE_BACKFILL", executed_qty=500,
                            position_before=0, position_after=500, fee=0.0,
                            timestamp="2026-08-31T09:06:00+09:00")
    backfill_b = _base_row(order_id="RECONCILE_BACKFILL_BUY_0197X0_610_5000.0_500_1110",
                            source="RECONCILE_BACKFILL", executed_qty=610,
                            position_before=500, position_after=1110, fee=0.0,
                            timestamp="2026-08-31T09:06:30+09:00")
    kept = ui_page._dedupe_reconcile_restatements([backfill_a, backfill_b])
    assert len(kept) == 2


def test_orphan_backfill_alone_is_still_hidden_as_before(ui_page):
    """Pre-existing 2026-08-31 behavior (unrelated to this fix): a backfill
    row with no adjacent real decision is hidden from the main table."""
    backfill_row = _base_row(
        order_id="RECONCILE_BACKFILL_BUY_0197X0_1110_5000.0_0_1110",
        source="RECONCILE_BACKFILL", fee=0.0,
    )
    rows = ui_page._trade_history_rows([backfill_row], [])
    assert rows == []


def test_signal_ledger_whipsaw_hold_shows_korean_label_2026_09_02(ui_page):
    """2026-09-02 user request: a reversal candidate that whipsaw-HOLDs
    (opposite flag confirmed, but T+3 re-confirmation rejects for a whipsaw
    reason so the held position is NOT liquidated) must show as "휩쏘보류" in
    the 신호 원장 UI instead of the raw internal literal
    "TIME_WINDOW_WHIPSAW_HOLD" -- worker.py writes that exact same literal
    for both TW2 and TW2 3-SLOT whipsaw-hold cases, so one mapping covers
    both modes. The underlying block_reason is still shown alongside it."""
    row = {"order_result": "TIME_WINDOW_WHIPSAW_HOLD", "block_reason": "TW_REJECT_MACD_GAP_NOT_EXPANDING"}
    summary = ui_page._order_summary(row)
    assert summary == "휩쏘보류 / TW_REJECT_MACD_GAP_NOT_EXPANDING"
    assert "TIME_WINDOW_WHIPSAW_HOLD" not in summary


def _signal_row(**overrides) -> dict:
    row = {
        "signal_id": "20260902_135700_UP_RED:TW2_3SLOT_CONFIRM", "signal_type": "TW2_3SLOT_CONFIRM",
        "direction": "UP_RED", "completed_bar_at": "20260902090300", "signal_bar_at": "",
        "detected_at": "2026-09-02T09:03:12+09:00", "signal_confirmed_at": "",
        "order_requested_at": "", "order_result": "", "final_result": "",
        "block_reason": "", "failure_stage": "",
    }
    row.update(overrides)
    return row


def test_t3_resolution_row_was_previously_hidden_now_shows_with_reason_2026_09_02(ui_page):
    """2026-09-02 user request: the actual T+3 re-confirmation OUTCOME for a
    flat/new-entry candidate (worker.py's signal_type="TW2_3SLOT_CONFIRM"/
    "TIME_WINDOW_CONFIRM") was entirely excluded from _is_display_signal's
    allowed set -- the user could only ever see the earlier T-registration
    row ("FILTERED_OUT / TIME_WINDOW_PENDING_CONFIRMATION", which just means
    "not decided yet", not a real rejection reason). The real reason
    (already present in block_reason all along) was invisible. Confirm the
    resolution row is now included and its reason renders."""
    rejected = _signal_row(order_result="FILTERED_OUT", block_reason="TW2_3SLOT_REJECT_QUALITY")
    assert ui_page._is_display_signal(rejected) is True
    timeline = ui_page._signal_timeline_rows([rejected])
    assert len(timeline) == 2  # 플래그/확정 + 주문
    order_row = timeline[1]
    assert order_row["구분"] == "주문"
    assert "TW2_3SLOT_REJECT_QUALITY" in order_row["내용"]


def test_whipsaw_hold_row_now_actually_reaches_the_timeline_end_to_end_2026_09_02(ui_page):
    """The 휩쏘보류 label fix (test above) only matters if the row carrying it
    is actually shown -- verify the full pipeline (_is_display_signal ->
    _signal_timeline_rows -> _order_summary), not just _order_summary in
    isolation, since a whipsaw-hold row also uses signal_type=
    "TW2_3SLOT_CONFIRM"/"TIME_WINDOW_CONFIRM" and was equally hidden before
    this fix."""
    whipsaw = _signal_row(order_result="TIME_WINDOW_WHIPSAW_HOLD", block_reason="TW_REJECT_MACD_GAP_NOT_EXPANDING")
    assert ui_page._is_display_signal(whipsaw) is True
    timeline = ui_page._signal_timeline_rows([whipsaw])
    assert timeline[0]["내용"] == "재확인(T+3) UP_RED"
    assert timeline[1]["내용"] == "휩쏘보류 / TW_REJECT_MACD_GAP_NOT_EXPANDING"


def test_pre_existing_pending_registration_row_still_shows_unaffected(ui_page):
    """Regression: the T-registration row (signal_type="INITIAL", order_
    result=FILTERED_OUT/TIME_WINDOW_PENDING_CONFIRMATION) was already
    visible before this fix and must remain exactly so -- this fix only
    ADDS the previously-hidden resolution row type, never changes this one."""
    pending = _signal_row(signal_type="INITIAL", order_result="FILTERED_OUT", block_reason="TIME_WINDOW_PENDING_CONFIRMATION")
    assert ui_page._is_display_signal(pending) is True
    timeline = ui_page._signal_timeline_rows([pending])
    assert timeline[1]["내용"] == "FILTERED_OUT / TIME_WINDOW_PENDING_CONFIRMATION"


def test_residual_cleanup_merges_into_the_main_exit_leg_2026_09_01(ui_page):
    """2026-09-01 real incident: a 809-share leverage TP exit's own sell left
    1 share held; order_executor._attempt_residual_cleanup sweeps it via a
    SEPARATE raw ledger row (source=RESIDUAL_CLEANUP, side=SELL). The UI must
    merge it into the SAME displayed round-trip as the main exit leg, showing
    the TRUE total quantity (810), not just the main leg's 809 or the
    residual's stray 1 -- and never as a second separate trade row."""
    main_leg = _base_row(
        order_id="REAL-ORD-EXIT-1", signal_id="", side="SELL", symbol="0193T0",
        exit_reason="TIME_WINDOW_TP2_FULL",
        executed_qty=809, executed_price=15_200.0, timestamp="2026-09-01T09:32:48+09:00",
        position_before=810, position_after=1, fee=1500.0, net_pnl=900_000.0, source="",
    )
    residual_leg = _base_row(
        order_id="RESIDUAL_CLEANUP:0193T0:2026-09-01T09:32:50+09:00", signal_id="", side="SELL", symbol="0193T0",
        exit_reason="TIME_WINDOW_TP2_FULL_RESIDUAL_CLEANUP",
        executed_qty=1, executed_price=15_180.0, timestamp="2026-09-01T09:32:50+09:00",
        position_before=1, position_after=0, fee=2.0, net_pnl=1_100.0, source="RESIDUAL_CLEANUP",
    )
    rows = ui_page._trade_history_rows([main_leg, residual_leg], [])
    assert len(rows) == 1, "must never show as two separate round-trip trades"
    row = rows[0]
    assert row["총 체결수량"] == "810주"
    expected_avg_price = (809 * 15_200.0 + 1 * 15_180.0) / 810
    assert float(row["체결가(수량가중평균)"].replace(",", "")) == pytest.approx(expected_avg_price, abs=0.5)
    assert row["총 순이익"] == "901,100원"
    assert row["총 수수료"] == "1,502원"
