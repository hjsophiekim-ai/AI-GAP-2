"""WAIT XS TEG override (2026-11-03) — 오후 슬롯 TEG 거절만, WAIT 선택진입 승인 후보에 한해 푼다.

고정하는 계약
  · CHOP 모드 ∧ 재확인 ≥ 11:00 ∧ MU ENTRY_WAIT ∧ WAIT_SELECTIVE_APPROVED ∧ TEG 사유 ⊆ XS 해제집합 일 때만 True
  · 11:00 이전 / ENTRY_BAD·CONFIRM·판정불가 / WAIT 미승인 / XS 밖 사유 / CHOP 아닌 모드 / MU Assist OFF 는 False
  · CHOP 이 아니거나 MU Assist OFF 면 판정 기록조차 남기지 않는다(원장 행 모양 불변)
  · 기존 XS 해제집합은 그대로(조건을 섞거나 넓히지 않는다)
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.trading.macd2 import config, mu_assist, state_store, strategy_mode, teg_gate, worker
from app.trading.macd2.models import Direction

KST = config.KST
SID_1103 = "20261103_112700_DOWN_BLUE:TW2_3SLOT_CONFIRM"
NOW_1103 = datetime(2026, 11, 3, 11, 33, 0, tzinfo=KST)
REASONS_1103 = (teg_gate.COND_RECENT_CROSS, teg_gate.COND_EMA_STACK, teg_gate.COND_VWAP, teg_gate.COND_MIN_INTERVAL)
SID_1022 = "20261022_123300_DOWN_BLUE:TW2_3SLOT_CONFIRM"
NOW_1022 = datetime(2026, 10, 22, 12, 39, 0, tzinfo=KST)
APPROVED = {"decision": mu_assist.WAIT_SELECTIVE_APPROVED, "cttr": 81.913, "cttr_state": "MATCH",
            "opposite_gap": 207.2332}


@pytest.fixture(autouse=True)
def _reset():
    for d in (worker._MU_WAIT_NOTES, worker._MU_ENTRY_NOTES, worker._WAIT_XS_NOTES):
        d.clear()
    yield
    for d in (worker._MU_WAIT_NOTES, worker._MU_ENTRY_NOTES, worker._WAIT_XS_NOTES):
        d.clear()


def _state(mode=strategy_mode.MODE_CHOP, mu=True):
    s = state_store.default_state()
    strategy_mode.apply(s, mode, changed_by="test")
    s.mu_assist_enabled = mu
    return s


def _setup(monkeypatch, sid, status=mu_assist.ENTRY_WAIT, note=APPROVED):
    monkeypatch.setattr(mu_assist, "entry_status_for", lambda s: status)
    if note is not None:
        worker._MU_WAIT_NOTES[sid] = dict(note)


def _call(state, sid=SID_1103, now=NOW_1103, reasons=REASONS_1103):
    sm: dict = {}
    ok = worker._mu_wait_xs_teg_override(state, sid, Direction.DOWN_BLUE, now, list(reasons), sm)
    return ok, sm


def test_1103_1127_blue_is_overridden(monkeypatch):
    _setup(monkeypatch, SID_1103)
    ok, sm = _call(_state())
    assert ok is True
    assert sm["wait_xs_teg_decision"] == config.WAIT_XS_TEG_OVERRIDE
    assert sm["wait_xs_teg_override"] == sorted(REASONS_1103)
    row = worker._wait_xs_ledger_fields(worker._WAIT_XS_NOTES[SID_1103])
    assert row == {"wait_xs_teg_override": True, "wait_xs_teg_reasons": "|".join(sorted(REASONS_1103)),
                   "wait_xs_mu_status": mu_assist.ENTRY_WAIT, "wait_xs_exec_strength": 81.913,
                   "wait_xs_opposite_gap": 207.2332, "wait_xs_decision": "WAIT_XS_TEG_OVERRIDE"}


def test_1022_1233_blue_recent_cross_only_is_overridden(monkeypatch):
    _setup(monkeypatch, SID_1022, note={**APPROVED, "cttr": 70.833, "opposite_gap": 338.4646})
    ok, _ = _call(_state(), SID_1022, NOW_1022, (teg_gate.COND_RECENT_CROSS,))
    assert ok is True


@pytest.mark.parametrize("hh,mm,expect", [(10, 59, False), (11, 0, True), (14, 49, True)])
def test_start_time_boundary(monkeypatch, hh, mm, expect):
    _setup(monkeypatch, SID_1103)
    ok, sm = _call(_state(), now=NOW_1103.replace(hour=hh, minute=mm))
    assert ok is expect
    assert sm["wait_xs_teg_decision"] == (config.WAIT_XS_TEG_OVERRIDE if expect else config.WAIT_XS_TEG_NOT_ELIGIBLE)


def test_1028_morning_case_never_overridden(monkeypatch):
    sid = "20261028_093300_DOWN_BLUE:TW2_3SLOT_CONFIRM"
    _setup(monkeypatch, sid, note={**APPROVED, "cttr": 85.658, "opposite_gap": 70.2495})
    ok, _ = _call(_state(), sid, datetime(2026, 10, 28, 9, 39, tzinfo=KST),
                  (teg_gate.COND_EMA_STACK, teg_gate.COND_VWAP))
    assert ok is False


@pytest.mark.parametrize("status", [mu_assist.ENTRY_BAD, mu_assist.ENTRY_CONFIRM, mu_assist.ENTRY_MU_UNAVAILABLE, ""])
def test_non_wait_mu_status_is_not_eligible(monkeypatch, status):
    _setup(monkeypatch, SID_1103, status=status)
    ok, sm = _call(_state())
    assert ok is False and sm["wait_xs_teg_decision"] == config.WAIT_XS_TEG_NOT_ELIGIBLE


@pytest.mark.parametrize("decision", [mu_assist.WAIT_BLOCK_EXEC_STRENGTH, mu_assist.WAIT_BLOCK_OPPOSITE_GAP,
                                      mu_assist.WAIT_BLOCK_BOTH, None])
def test_wait_selective_not_approved_is_not_eligible(monkeypatch, decision):
    _setup(monkeypatch, SID_1103, note=None if decision is None else {**APPROVED, "decision": decision})
    ok, _ = _call(_state())
    assert ok is False


@pytest.mark.parametrize("extra", [teg_gate.COND_MACD_GAP_EXPANDING, teg_gate.COND_EMA_SPREAD_EXPANDING,
                                   teg_gate.COND_TW2_CONFIRMED])
def test_reason_outside_xs_allowlist_keeps_rejection(monkeypatch, extra):
    _setup(monkeypatch, SID_1103)
    ok, _ = _call(_state(), reasons=REASONS_1103 + (extra,))
    assert ok is False


def test_empty_reasons_is_not_eligible(monkeypatch):
    _setup(monkeypatch, SID_1103)
    assert _call(_state(), reasons=())[0] is False


@pytest.mark.parametrize("mode", [strategy_mode.MODE_N1, strategy_mode.MODE_P3, strategy_mode.MODE_E])
def test_non_chop_mode_does_nothing_and_records_nothing(monkeypatch, mode):
    _setup(monkeypatch, SID_1103)
    ok, sm = _call(_state(mode=mode))
    assert ok is False and sm == {} and worker._WAIT_XS_NOTES == {}


def test_mu_assist_off_does_nothing(monkeypatch):
    _setup(monkeypatch, SID_1103)
    ok, sm = _call(_state(mu=False))
    assert ok is False and sm == {} and worker._WAIT_XS_NOTES == {}


def test_xs_allowlist_unchanged():
    assert worker._MU_XS_OVERRIDABLE_TEG == frozenset({teg_gate.COND_RECENT_CROSS, teg_gate.COND_MIN_INTERVAL,
                                                       teg_gate.COND_EMA_STACK, teg_gate.COND_VWAP})


def test_ledger_fields_empty_without_evaluation():
    assert worker._wait_xs_ledger_fields(None) == {}
