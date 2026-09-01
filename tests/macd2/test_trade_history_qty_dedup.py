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
