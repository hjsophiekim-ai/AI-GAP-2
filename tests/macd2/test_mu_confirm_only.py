"""MU Assist 일반 진입 CONFIRM-only (2026-11-03).

잠그는 계약
 A  ENTRY_CONFIRM                       -> 진입 (veto 없음)
 B  ENTRY_WAIT                          -> MU_ASSIST_NOT_CONFIRMED
 C  MU_UNAVAILABLE / DIVERGE 비BAD / 기록없음 -> MU_ASSIST_NOT_CONFIRMED
 D  ENTRY_BAD                           -> MU_ASSIST_ENTRY_BAD (기존 그대로, 사유가 B/C 와 다름)
 E  MU CATCH-UP PASS                    -> 비CONFIRM 이어도 기존처럼 진입 (BAD 예외도 그대로)
 F  XS                                  -> STRONG 정의(ENTRY_CONFIRM ∧ 체결강도 MATCH) 무변경

worker 경로 회귀(WAIT/판정불가/기록없음 차단·슬롯 미소비)는 test_mu_assist.py 에 있다.
"""
from __future__ import annotations

import pytest

from app.trading.macd2 import config, exec_strength, mu_assist, mu_catchup, worker
from tests.macd2.test_mu_catchup import _GATES, _buys, _row, _world
from app.trading.macd2.worker import run_once


@pytest.fixture(autouse=True)
def _reset():
    mu_assist.reset_for_tests()
    yield
    mu_assist.reset_for_tests()


def _status(monkeypatch, status):
    monkeypatch.setattr(mu_assist, "entry_status_for", lambda sid: status)


# ── A~D. 판정 함수 ─────────────────────────────────────────────────────────
def test_a_confirm_is_not_vetoed(monkeypatch):
    _status(monkeypatch, mu_assist.ENTRY_CONFIRM)
    assert mu_assist.entry_veto_reason("X") is None


def test_b_wait_is_wait_block(monkeypatch):
    """ENTRY_WAIT 는 별도 사유 MU_ASSIST_WAIT_BLOCK -- worker 가 선택진입 조건으로 풀 수 있다."""
    _status(monkeypatch, mu_assist.ENTRY_WAIT)
    assert mu_assist.entry_veto_reason("X") == config.MU_ASSIST_WAIT_BLOCK_REASON == "MU_ASSIST_WAIT_BLOCK"


@pytest.mark.parametrize("status", [mu_assist.ENTRY_DIVERGE, mu_assist.ENTRY_MU_UNAVAILABLE, ""])
def test_c_unavailable_is_not_confirmed(monkeypatch, status):
    _status(monkeypatch, status)
    assert mu_assist.entry_veto_reason("X") == config.MU_ASSIST_UNAVAILABLE_BLOCK_REASON == "MU_ASSIST_UNAVAILABLE_BLOCK"


def test_d_bad_keeps_its_own_reason(monkeypatch):
    _status(monkeypatch, mu_assist.ENTRY_BAD)
    assert mu_assist.entry_veto_reason("X") == config.MU_ASSIST_ENTRY_BAD_BLOCK_REASON
    assert config.MU_ASSIST_ENTRY_BAD_BLOCK_REASON != config.MU_ASSIST_UNAVAILABLE_BLOCK_REASON


# ── E. CATCH-UP 예외 그대로 ───────────────────────────────────────────────
def _not_confirmed(monkeypatch):
    monkeypatch.setattr(worker.mu_assist, "entry_veto_reason",
                        lambda sid: config.MU_ASSIST_UNAVAILABLE_BLOCK_REASON)


def _immediate(monkeypatch):
    from app.trading.macd2 import e_strategy
    imm = e_strategy.EarlyPassDecision(evaluated=True, immediate=True, trigger=None, dist_pct=None,
                                       c1=True, c2=True, c3=True, up=True, upfast=False, reason="TEST_IMMEDIATE")
    monkeypatch.setattr(worker.e_strategy, "evaluate_early_pass", lambda **kw: imm)


def test_e_catchup_pass_still_enters_when_mu_is_not_confirmed(monkeypatch, tmp_path):
    svc, now0, state, broker, calls = _world(monkeypatch, tmp_path)
    _not_confirmed(monkeypatch)
    _immediate(monkeypatch)
    r = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert len(_buys(broker)) == 1 and r.actions == [f"TW2_3SLOT_ENTRY:{config.LONG_SYMBOL}"], r.actions
    row = _row(state)
    assert row["order_result"] == "EXECUTED" and row["mu_catchup_pass"] == "PASS"
    assert row["catchup_overridden_gates"].endswith(config.MU_ASSIST_UNAVAILABLE_BLOCK_REASON)


def test_e_catchup_pass_with_bad_is_unchanged(monkeypatch, tmp_path):
    """기존 CATCH-UP 테스트와 같은 결과 — ENTRY_BAD 예외 기록·진입 경로 그대로."""
    svc, now0, state, broker, calls = _world(monkeypatch, tmp_path)
    _immediate(monkeypatch)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert len(_buys(broker)) == 1
    assert _row(state)["catchup_overridden_gates"] == _GATES


def test_e_catchup_fail_with_not_confirmed_is_blocked(monkeypatch, tmp_path):
    """CATCH-UP 이 PASS 가 아니면 비CONFIRM 은 그대로 차단 — 기존 거절(gap)이 먼저 걸리면 그 사유."""
    svc, now0, state, broker, calls = _world(monkeypatch, tmp_path, cu_status="FAIL")
    _not_confirmed(monkeypatch)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert _buys(broker) == []


# ── F. XS 무변경 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("status,expect", [
    (mu_assist.ENTRY_CONFIRM, True), (mu_assist.ENTRY_WAIT, False),
    (mu_assist.ENTRY_MU_UNAVAILABLE, False), (mu_assist.ENTRY_BAD, False),
])
def test_f_xs_strong_definition_is_unchanged(monkeypatch, status, expect):
    from datetime import datetime
    from app.trading.macd2 import state_store
    from app.trading.macd2.models import Direction
    s = state_store.default_state()
    s.mu_assist_enabled, s.mu_assist_xs_enabled = True, True
    _status(monkeypatch, status)
    monkeypatch.setattr(exec_strength, "evaluate",
                        lambda d, now: {"state": exec_strength.MATCH, "cttr": 120.0})
    got = worker._mu_xs_strong(s, "X", Direction.UP_RED, datetime(2026, 11, 3, 10, 0, tzinfo=config.KST))
    assert (got is not None) is expect


def test_f_xs_strong_candidates_are_never_vetoed_by_confirm_only(monkeypatch):
    """XS STRONG 은 정의상 ENTRY_CONFIRM 이므로 CONFIRM-only veto 가 걸릴 수 없다."""
    _status(monkeypatch, mu_assist.ENTRY_CONFIRM)
    assert mu_assist.entry_veto_reason("X") is None


def test_stale_has_its_own_reason(monkeypatch):
    """판정불가 중 T+2 분봉이 STALE 이면 MU_ASSIST_STALE_BLOCK (2026-11-03 최종 규칙)."""
    _status(monkeypatch, mu_assist.ENTRY_MU_UNAVAILABLE)
    monkeypatch.setattr(mu_assist, "entry_t2_status_for", lambda sid: mu_assist.STATUS_STALE)
    assert mu_assist.entry_veto_reason("X") == config.MU_ASSIST_STALE_BLOCK_REASON == "MU_ASSIST_STALE_BLOCK"
    monkeypatch.setattr(mu_assist, "entry_t2_status_for", lambda sid: mu_assist.STATUS_MISSING)
    assert mu_assist.entry_veto_reason("X") == config.MU_ASSIST_UNAVAILABLE_BLOCK_REASON


def test_entry_ledger_fields():
    assert mu_assist.entry_ledger_fields(None) == {}
    r = mu_assist.entry_ledger_fields({"status": "MU_UNAVAILABLE", "final_veto": config.MU_ASSIST_STALE_BLOCK_REASON})
    assert r == {"mu_entry_status": "MU_UNAVAILABLE", "mu_unavailable_block": False, "mu_stale_block": True}
    r = mu_assist.entry_ledger_fields({"status": "ENTRY_WAIT", "final_veto": ""})
    assert r["mu_unavailable_block"] is False and r["mu_stale_block"] is False
