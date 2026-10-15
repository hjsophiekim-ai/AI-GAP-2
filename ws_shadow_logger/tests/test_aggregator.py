from datetime import datetime, timedelta

import pytest

from ws_shadow_logger.aggregator import SymbolState

T0 = datetime(2026, 10, 15, 10, 0, 0)


def book(bq, aq):
    r = {"MKSC_SHRN_ISCD": "X", "BSOP_HOUR": "100000", "BIDP1": "100", "ASKP1": "105",
         "TOTAL_BIDP_RSQN": str(sum(bq)), "TOTAL_ASKP_RSQN": str(sum(aq))}
    for i in range(10):
        r[f"BIDP_RSQN{i + 1}"] = str(bq[i]); r[f"ASKP_RSQN{i + 1}"] = str(aq[i])
    return r


def tr(vol, side, cttr):
    return {"STCK_PRPR": "102", "CNTG_VOL": str(vol), "CCLD_DVSN": side, "CTTR": str(cttr),
            "SHNU_CNTG_SMTN": "10", "SELN_CNTG_SMTN": "5", "ACML_VOL": "15", "STCK_CNTG_HOUR": "100000"}


def test_imbalance_levels_and_spread():
    s = SymbolState("X"); s.on_book(book([30] + [10] * 9, [10] * 10), T0)
    r = s.snapshot(T0 + timedelta(seconds=1))
    assert r["imb_1"] == pytest.approx((30 - 10) / 40)
    assert r["imb_5"] == pytest.approx((70 - 50) / 120)
    assert r["imb_10"] == pytest.approx((120 - 100) / 220)
    assert r["spread"] == 5 and r["spread_bp"] == pytest.approx(5 / 102.5 * 1e4)
    assert r["bid_qty_1"] == 30 and r["ask_qty_10"] == 10


def test_execution_windows_and_ratio():
    s = SymbolState("X")
    s.on_trade(tr(10, "1", 100), T0 + timedelta(milliseconds=200))
    s.on_trade(tr(30, "5", 90), T0 + timedelta(milliseconds=700))
    s.on_trade(tr(20, "1", 95), T0 + timedelta(seconds=3, milliseconds=100))
    r1 = s.snapshot(T0 + timedelta(seconds=1))
    assert (r1["buy_vol_1s"], r1["sell_vol_1s"], r1["trades_1s"]) == (10, 30, 2)
    r4 = s.snapshot(T0 + timedelta(seconds=4))
    assert r4["buy_vol_1s"] == 20 and r4["trades_1s"] == 1
    assert r4["buy_ratio_5s"] == pytest.approx(30 / 60) and r4["trades_per_s_5"] == pytest.approx(3 / 5)
    assert r4["cttr"] == 95


def test_deltas_use_past_snapshots_only():
    s = SymbolState("X")
    s.on_book(book([10] * 10, [10] * 10), T0); s.on_trade(tr(1, "1", 100), T0)
    for k in range(1, 7):
        s.snapshot(T0 + timedelta(seconds=k))
    s.on_book(book([30] + [10] * 9, [10] * 10), T0 + timedelta(seconds=6, milliseconds=500))
    s.on_trade(tr(1, "1", 110), T0 + timedelta(seconds=6, milliseconds=600))
    r = s.snapshot(T0 + timedelta(seconds=7))
    assert r["d_imb1_1s"] == pytest.approx(0.5) and r["d_imb1_5s"] == pytest.approx(0.5)
    assert r["d_cttr_1s"] == pytest.approx(10)
    assert r["d_imb1_10s"] is None          # 10초 전 스냅샷 없음 → 채우지 않음
