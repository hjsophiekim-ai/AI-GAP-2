"""조기익절 필터 발동 시 거래원장(macd2_execution_ledger.csv) 기록 회귀테스트
(2026-09-03).

핵심은 "실제 EARLY_TAKE_PROFIT SELL 행에 6개 값이 정확히 남는가" — 그래서
대부분의 테스트가 실제 worker.run_once() 를 통과해 order_executor.execute_exit
가 쓴 원장 행을 그대로 읽어 검증한다(하네스는
tests/macd2/test_early_take_profit_worker.py / test_tw2_3slot_worker_regression.py
의 것을 재사용).

추가로 ledger.record_early_tp_fields() 자체의 계약(exit_reason 게이트,
idempotency, 기존 컬럼 불변, 스키마 폭 유지)을 직접 검증한다.
"""
from __future__ import annotations

import csv
import json

import pytest

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.models import SignalState
from app.trading.macd2.worker import run_once
from tests.macd2.test_early_take_profit_worker import (
    _broker,
    _held_3slot_state,
    _market,
    _seed_completed_bar,
)
from tests.macd2.test_tw2_3slot_worker_regression import _patch_common

_PRESERVED_COLUMNS = (
    "order_id", "signal_id", "timestamp", "mode", "symbol", "side",
    "requested_qty", "executed_qty", "requested_price", "executed_price",
    "position_before", "position_after", "gross_pnl", "fee", "slippage",
    "net_pnl", "exit_reason", "broker_response", "source",
)


def _rows() -> list[dict]:
    with open(ledger.EXECUTION_LEDGER_PATH, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _header() -> list[str]:
    with open(ledger.EXECUTION_LEDGER_PATH, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh).fieldnames or [])


def _sell_rows(exit_reason: str) -> list[dict]:
    return [r for r in _rows() if r["side"] == "SELL" and r["exit_reason"] == exit_reason]


def _fire_early_tp(monkeypatch, *, chop_score=3, chop_conditions=None, armed_at="2026-01-05T09:33:00+09:00",
                   peak=2.4137, bar_close=10_020.0):
    """진입CHOP + armed 상태에서 floor 아래 완성봉을 만들어 실제로 발동시킨다."""
    svc, now0 = _market(inverse_price=bar_close)
    state = _held_3slot_state(
        now=now0, early_tp_on=True, entry_chop=True, peak=peak, bar_close=bar_close,
    )
    state.last_entry_chop_score = chop_score
    state.last_entry_chop_conditions = chop_conditions if chop_conditions is not None else {
        "ema10_ema20_spread_not_expanding": False,
        "ema20_slope_not_aligned": True,
        "recent_confirmed_crosses": True,
        "vwap_repeat": True,
    }
    state.last_early_tp_armed_at = armed_at
    _patch_common(monkeypatch)
    result = run_once(broker=_broker(bar_close), market_data=svc, state=state, now=now0)
    assert any(a.startswith(config.EXIT_EARLY_TAKE_PROFIT) for a in result.actions), (
        f"조기익절이 발동하지 않았다: {result.actions!r}"
    )
    return state, result


# ── 1. 실제 발동 SELL 행에 6개 값이 남는가 ────────────────────────────────
def test_early_take_profit_sell_row_carries_all_six_diagnostic_values(monkeypatch):
    conditions = {
        "ema10_ema20_spread_not_expanding": False,
        "ema20_slope_not_aligned": True,
        "recent_confirmed_crosses": True,
        "vwap_repeat": True,
    }
    _fire_early_tp(
        monkeypatch, chop_score=3, chop_conditions=conditions,
        armed_at="2026-01-05T09:33:00+09:00", peak=2.4137,
    )

    sells = _sell_rows(config.EXIT_EARLY_TAKE_PROFIT)
    assert len(sells) == 1, f"EARLY_TAKE_PROFIT SELL 행이 정확히 1건이어야 한다: {sells!r}"
    row = sells[0]

    assert row["early_tp_entry_chop_score"] == "3"
    assert json.loads(row["early_tp_entry_chop_conditions"]) == conditions
    assert row["early_tp_armed_at"] == "2026-01-05T09:33:00+09:00"
    assert float(row["early_tp_peak_net_return_pct"]) == pytest.approx(2.4137)
    assert float(row["early_tp_trigger_pct"]) == pytest.approx(config.EARLY_TP_TRIGGER_PCT)
    assert float(row["early_tp_floor_pct"]) == pytest.approx(config.EARLY_TP_FLOOR_PCT)


def test_the_row_also_still_has_the_real_fill_facts(monkeypatch):
    """진단 컬럼을 얹어도 가격/수량/PnL/수수료/청산사유는 그대로여야 한다."""
    _fire_early_tp(monkeypatch)

    row = _sell_rows(config.EXIT_EARLY_TAKE_PROFIT)[0]
    assert row["exit_reason"] == config.EXIT_EARLY_TAKE_PROFIT
    assert row["side"] == "SELL"
    assert row["symbol"] == config.INVERSE_SYMBOL
    assert int(row["executed_qty"]) == 10
    assert float(row["executed_price"]) > 0.0
    assert int(row["position_after"]) == 0
    assert row["net_pnl"] not in ("", None)
    assert row["fee"] not in ("", None)
    assert row["order_id"] not in ("", None)


def test_peak_recorded_is_the_pre_reset_value_not_the_cleared_state(monkeypatch):
    """_apply_exit_outcome 가 state를 비운 뒤에도 원장에는 발동 당시 MFE가
    남아 있어야 한다 — 이 수정의 존재 이유."""
    state, _ = _fire_early_tp(monkeypatch, peak=3.75)

    assert state.early_tp_peak_net_return == 0.0, "state는 청산과 함께 초기화되는 것이 정상"
    assert state.time_window_entry_chop is False
    row = _sell_rows(config.EXIT_EARLY_TAKE_PROFIT)[0]
    assert float(row["early_tp_peak_net_return_pct"]) == pytest.approx(3.75), (
        "원장에는 초기화 전 값이 남아야 한다"
    )


def test_armed_at_is_always_populated_on_a_fired_row(monkeypatch):
    """armed_at 이 아직 비어 있는 상태로 발동하는 경우 -- worker가 같은 틱에서
    arming을 먼저 기록하므로(arm과 fire가 같은 완성봉에서 일어나는 정상 경로)
    원장에는 그 틱 시각이 남고 절대 빈값이 아니다. chop_score/conditions 는
    진입 당시 값이 없으면 빈값/빈 dict 로 남는다."""
    state, _ = _fire_early_tp(
        monkeypatch, armed_at=None, chop_score=None, chop_conditions={},
    )
    row = _sell_rows(config.EXIT_EARLY_TAKE_PROFIT)[0]
    assert row["early_tp_armed_at"] != "", "발동한 행의 armed_at 이 비어 있다"
    assert row["early_tp_armed_at"] == state.last_early_tp_armed_at
    assert row["early_tp_entry_chop_score"] == ""
    assert json.loads(row["early_tp_entry_chop_conditions"]) == {}


# ── 2. 조기익절이 아닌 거래는 빈값 + 스키마 유지 ──────────────────────────
@pytest.mark.parametrize(
    "bar_close,expected_exit",
    [(10_800.0, config.EXIT_TW_TP2_FULL), (9_820.0, config.EXIT_TW_STOP_LOSS)],
)
def test_other_exits_leave_the_six_columns_empty(monkeypatch, bar_close, expected_exit):
    svc, now0 = _market(inverse_price=bar_close)
    state = _held_3slot_state(
        now=now0, early_tp_on=True, entry_chop=True, peak=3.0, bar_close=bar_close,
    )
    state.last_entry_chop_score = 4
    state.last_early_tp_armed_at = "2026-01-05T09:30:00+09:00"
    _patch_common(monkeypatch)
    result = run_once(broker=_broker(bar_close), market_data=svc, state=state, now=now0)
    assert any(expected_exit in a for a in result.actions), f"{result.actions!r}"

    rows = _sell_rows(expected_exit)
    assert rows, f"{expected_exit} SELL 행이 없다"
    for row in rows:
        for col in ledger.EARLY_TP_LEDGER_COLUMNS:
            assert row[col] == "", (
                f"조기익절이 아닌 청산({expected_exit})의 {col} 이 비어 있지 않다: {row[col]!r}"
            )


def test_buy_rows_keep_the_columns_present_but_empty(monkeypatch):
    """진입(BUY) 행에도 6개 컬럼이 존재하되 빈값이어야 한다. FakeBroker의 시드
    매수는 order_executor를 거치지 않아 원장에 남지 않으므로, 실제 진입 레그와
    같은 형태의 BUY 행을 원장 API로 직접 넣어 확인한다."""
    ledger.append_execution({
        "order_id": "ORD-BUY", "signal_id": "sig-entry", "timestamp": "20260105T093000",
        "mode": "mock", "symbol": config.INVERSE_SYMBOL, "side": "BUY",
        "requested_qty": 10, "executed_qty": 10,
        "requested_price": 10_000.0, "executed_price": 10_000.0,
        "position_before": 0, "position_after": 10,
        "gross_pnl": 0.0, "fee": 15.0, "slippage": 0.0, "net_pnl": 0.0,
        "exit_reason": "", "broker_response": "{}",
    })
    _fire_early_tp(monkeypatch)

    buys = [r for r in _rows() if r["side"] == "BUY"]
    assert buys, "BUY 행이 없다"
    for row in buys:
        for col in ledger.EARLY_TP_LEDGER_COLUMNS:
            assert col in row and row[col] == "", f"BUY 행의 {col} 이 비어 있지 않다"
    # 같은 파일에 조기익절 SELL 행은 값이 채워져 있어야 한다(둘이 공존)
    assert _sell_rows(config.EXIT_EARLY_TAKE_PROFIT)[0]["early_tp_floor_pct"] != ""


def test_csv_schema_stays_uniform_across_all_rows(monkeypatch):
    """모든 행이 동일한 컬럼 폭을 갖고, 헤더에 6개 컬럼이 정확히 한 번씩 있어야
    한다(DictReader/pandas.read_csv 가 깨지지 않는 조건)."""
    _fire_early_tp(monkeypatch)

    header = _header()
    for col in ledger.EARLY_TP_LEDGER_COLUMNS:
        assert header.count(col) == 1, f"{col} 이 헤더에 {header.count(col)}번 나온다"
    with open(ledger.EXECUTION_LEDGER_PATH, newline="", encoding="utf-8") as fh:
        widths = {len(r) for r in csv.reader(fh)}
    assert len(widths) == 1, f"행마다 컬럼 폭이 다르다: {widths!r}"
    assert widths.pop() == len(header)

    import pandas as pd
    df = pd.read_csv(ledger.EXECUTION_LEDGER_PATH)
    assert list(df.columns) == header


# ── 3. record_early_tp_fields() 계약 ─────────────────────────────────────
def _seed_row(exit_reason: str, order_id: str = "ORD-EARLY-1") -> dict:
    ledger.append_execution({
        "order_id": order_id, "signal_id": "sig-1", "timestamp": "20260105T093300",
        "mode": "mock", "symbol": config.INVERSE_SYMBOL, "side": "SELL",
        "requested_qty": 10, "executed_qty": 10,
        "requested_price": 10_020.0, "executed_price": 10_020.0,
        "position_before": 10, "position_after": 0,
        "gross_pnl": 200.0, "fee": 30.0, "slippage": 6.0, "net_pnl": 164.0,
        "exit_reason": exit_reason, "broker_response": "{}",
    })
    return next(r for r in _rows() if r["order_id"] == order_id)


_FIELDS = {
    "early_tp_entry_chop_score": 3,
    "early_tp_entry_chop_conditions": '{"vwap_repeat": true}',
    "early_tp_armed_at": "2026-01-05T09:31:00+09:00",
    "early_tp_peak_net_return_pct": 1.93,
    "early_tp_trigger_pct": 1.5,
    "early_tp_floor_pct": 0.8,
}


def test_patch_is_rejected_for_a_non_early_tp_exit_row():
    _seed_row(config.EXIT_TW_STOP_LOSS, order_id="ORD-SL")
    assert ledger.record_early_tp_fields("ORD-SL", _FIELDS) is False
    row = next(r for r in _rows() if r["order_id"] == "ORD-SL")
    for col in ledger.EARLY_TP_LEDGER_COLUMNS:
        assert row[col] == "", "다른 청산사유 행에 진단값이 써졌다"


def test_patch_returns_false_for_unknown_order_id_and_changes_nothing():
    _seed_row(config.EXIT_EARLY_TAKE_PROFIT)
    before = _rows()
    assert ledger.record_early_tp_fields("NOPE", _FIELDS) is False
    assert _rows() == before


def test_patch_returns_false_for_empty_order_id():
    assert ledger.record_early_tp_fields("", _FIELDS) is False


def test_patch_is_idempotent_and_never_overwrites_an_earlier_snapshot():
    _seed_row(config.EXIT_EARLY_TAKE_PROFIT)
    assert ledger.record_early_tp_fields("ORD-EARLY-1", _FIELDS) is True
    after_first = _rows()

    # 두 번째 호출: 다른 값을 넘겨도 파일이 전혀 바뀌지 않아야 한다
    later = dict(_FIELDS, early_tp_peak_net_return_pct=0.0, early_tp_armed_at="")
    assert ledger.record_early_tp_fields("ORD-EARLY-1", later) is True
    assert _rows() == after_first, "중복 호출이 기존 스냅샷을 덮어썼다"
    assert len(_rows()) == len(after_first), "중복 호출이 행을 추가했다"


def test_patch_never_touches_the_preserved_columns():
    before = _seed_row(config.EXIT_EARLY_TAKE_PROFIT)
    assert ledger.record_early_tp_fields("ORD-EARLY-1", _FIELDS) is True
    after = next(r for r in _rows() if r["order_id"] == "ORD-EARLY-1")
    for col in _PRESERVED_COLUMNS:
        assert after[col] == before[col], f"{col} 이 변경됐다: {before[col]!r} -> {after[col]!r}"


def test_patch_does_not_disturb_other_rows():
    _seed_row(config.EXIT_TW_TP2_FULL, order_id="ORD-OTHER")
    _seed_row(config.EXIT_EARLY_TAKE_PROFIT, order_id="ORD-EARLY-1")
    other_before = next(r for r in _rows() if r["order_id"] == "ORD-OTHER")

    assert ledger.record_early_tp_fields("ORD-EARLY-1", _FIELDS) is True

    assert next(r for r in _rows() if r["order_id"] == "ORD-OTHER") == other_before
    assert len(_rows()) == 2


def test_patch_writes_empty_string_for_none_values():
    _seed_row(config.EXIT_EARLY_TAKE_PROFIT)
    fields = dict(_FIELDS, early_tp_armed_at=None, early_tp_entry_chop_score=None)
    assert ledger.record_early_tp_fields("ORD-EARLY-1", fields) is True
    row = next(r for r in _rows() if r["order_id"] == "ORD-EARLY-1")
    assert row["early_tp_armed_at"] == ""
    assert row["early_tp_entry_chop_score"] == ""


def test_patch_backfills_the_columns_into_a_legacy_header_file():
    """새 컬럼이 없던(구버전) 원장 파일에도 _ensure_columns 로 컬럼이 추가되고
    기존 행들의 값이 보존돼야 한다."""
    legacy_cols = [c for c in ledger.EXECUTION_LEDGER_COLUMNS
                   if c not in ledger.EARLY_TP_LEDGER_COLUMNS]
    ledger.ensure_paths()
    with open(ledger.EXECUTION_LEDGER_PATH, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=legacy_cols)
        writer.writeheader()
        writer.writerow({c: "" for c in legacy_cols} | {
            "order_id": "ORD-LEGACY", "signal_id": "sig-old", "side": "SELL",
            "symbol": config.INVERSE_SYMBOL, "executed_qty": "7", "executed_price": "9999.0",
            "net_pnl": "123.0", "exit_reason": config.EXIT_EARLY_TAKE_PROFIT,
        })

    assert ledger.record_early_tp_fields("ORD-LEGACY", _FIELDS) is True

    row = next(r for r in _rows() if r["order_id"] == "ORD-LEGACY")
    assert row["executed_qty"] == "7" and row["net_pnl"] == "123.0"
    assert row["early_tp_armed_at"] == _FIELDS["early_tp_armed_at"]
    for col in ledger.EARLY_TP_LEDGER_COLUMNS:
        assert col in _header()


# ── 4. 필터 OFF 회귀 ─────────────────────────────────────────────────────
def test_filter_off_writes_no_early_tp_row_and_no_diagnostic_values(monkeypatch):
    svc, now0 = _market(inverse_price=9_820.0)  # -1.8% -> 기존 손절
    state = _held_3slot_state(
        now=now0, early_tp_on=False, entry_chop=True, peak=3.0, bar_close=9_820.0,
    )
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        worker.ledger, "record_early_tp_fields",
        lambda *a, **kw: pytest.fail("필터 OFF인데 원장 패치가 호출됐다"),
    )
    result = run_once(broker=_broker(9_820.0), market_data=svc, state=state, now=now0)

    assert any(config.EXIT_TW_STOP_LOSS in a for a in result.actions)
    assert _sell_rows(config.EXIT_EARLY_TAKE_PROFIT) == []
    for row in _rows():
        for col in ledger.EARLY_TP_LEDGER_COLUMNS:
            assert row[col] == ""
