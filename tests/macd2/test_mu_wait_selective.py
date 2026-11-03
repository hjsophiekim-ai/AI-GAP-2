"""MU Assist WAIT 선택진입 (2026-11-03).

ENTRY_WAIT 는 원칙적으로 MU_ASSIST_WAIT_BLOCK. 단 ① 하이닉스 체결강도 방향일치(exec_strength
production 정의) ∧ ② 반대 gap = max(0, −hy_gap_prev) ≤ 400 이면 진입 허용.
우선순위: CATCH-UP > CONFIRM > WAIT 선택 > BAD 차단 > 판정불가 차단.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.trading.macd2 import config, exec_strength, ledger, mu_assist, strategy_mode, worker
from app.trading.macd2.worker import run_once
from tests.macd2.test_tw2_3slot_worker_regression import tw2_3slot_market_data  # noqa: F401 (fixture)
from tests.macd2.test_mu_assist import _block_rows, _broker, _buys, _mode_state, _on, _patch, _prime, _t2, _t3


@pytest.fixture(autouse=True)
def _reset():
    mu_assist.reset_for_tests()
    worker._MU_WAIT_NOTES.clear()
    yield
    mu_assist.reset_for_tests()
    worker._MU_WAIT_NOTES.clear()


# ── 판정 함수 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cs,g_prev,decision,opp", [
    (exec_strength.MATCH, -107.8, mu_assist.WAIT_SELECTIVE_APPROVED, 107.8),        # 10/21 09:06
    (exec_strength.MATCH, -400.0, mu_assist.WAIT_SELECTIVE_APPROVED, 400.0),        # 경계 포함
    (exec_strength.MATCH, -400.01, mu_assist.WAIT_BLOCK_OPPOSITE_GAP, 400.01),
    (exec_strength.MATCH, -1303.8, mu_assist.WAIT_BLOCK_OPPOSITE_GAP, 1303.8),      # 10/22 09:06
    (exec_strength.MISMATCH, -354.8, mu_assist.WAIT_BLOCK_EXEC_STRENGTH, 354.8),    # 10/23 10:54
    (exec_strength.MISMATCH, -418.0, mu_assist.WAIT_BLOCK_BOTH, 418.0),             # 10/21 09:57
    (exec_strength.UNAVAILABLE, -100.0, mu_assist.WAIT_BLOCK_EXEC_STRENGTH, 100.0),
    (exec_strength.MATCH, None, mu_assist.WAIT_BLOCK_OPPOSITE_GAP, None),           # gap 판정 불가
    (exec_strength.MATCH, 250.0, mu_assist.WAIT_SELECTIVE_APPROVED, 0.0),           # 이미 같은 방향 = 반대 gap 0
])
def test_decision_table(cs, g_prev, decision, opp):
    d, o = mu_assist.wait_selective_decision(cs, g_prev)
    assert d == decision
    assert (o is None and opp is None) or o == pytest.approx(opp)


def test_threshold_is_fixed_at_400():
    assert config.MU_WAIT_OPPOSITE_GAP_MAX == 400.0


def test_ledger_columns_are_additive_at_the_end():
    cols = ledger.SIGNAL_LEDGER_COLUMNS
    new = ["mu_wait_selective", "mu_wait_exec_strength_match", "mu_wait_exec_strength_value",
           "mu_wait_opposite_gap", "mu_wait_gap_threshold", "mu_wait_decision"]
    assert cols[-len(new):] == new
    assert mu_assist.wait_ledger_fields(None) == {}
    row = mu_assist.wait_ledger_fields({"decision": mu_assist.WAIT_SELECTIVE_APPROVED, "cttr": 113.9,
                                        "cttr_state": "MATCH", "opposite_gap": 145.8})
    assert row["mu_wait_selective"] is True and row["mu_wait_decision"] == "WAIT_SELECTIVE_APPROVED"
    assert row["mu_wait_gap_threshold"] == 400.0 and set(row) <= set(cols)


# ── worker 경로 ───────────────────────────────────────────────────────────
def _wait_world(tw2_3slot_market_data, monkeypatch, *, cs, g_prev):
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(strategy_mode.MODE_N1), _broker()
    _patch(monkeypatch)
    _on(monkeypatch, pattern=mu_assist.PATTERN_STALL, hy=mu_assist.HY_EXPAND)       # = ENTRY_WAIT
    monkeypatch.setattr(worker.exec_strength, "evaluate",
                        lambda d, now, data_dir=None: {"cttr": 113.9 if cs == "MATCH" else 84.0, "state": cs})
    monkeypatch.setattr(worker.mu_assist, "entry_gap_prev_for", lambda sid: g_prev)
    _prime(state, now0)
    run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    return svc, now0, state, broker


def _wait_rows():
    return [r for r in ledger.load_signal_ledger(limit=0) if r.get("mu_wait_decision")]


def test_wait_with_match_and_small_gap_enters(tw2_3slot_market_data, monkeypatch):
    svc, now0, state, broker = _wait_world(tw2_3slot_market_data, monkeypatch, cs=exec_strength.MATCH, g_prev=-145.8)
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0) + timedelta(seconds=20))
    assert len(_buys(broker)) == 1, broker.orders
    rows = _wait_rows()
    assert rows and rows[-1]["mu_wait_decision"] == mu_assist.WAIT_SELECTIVE_APPROVED
    assert rows[-1]["mu_wait_selective"] == "True" and rows[-1]["mu_wait_opposite_gap"] == "145.8"


@pytest.mark.parametrize("cs,g_prev,decision", [
    (exec_strength.MISMATCH, -354.8, mu_assist.WAIT_BLOCK_EXEC_STRENGTH),
    (exec_strength.MATCH, -455.4, mu_assist.WAIT_BLOCK_OPPOSITE_GAP),
    (exec_strength.MISMATCH, -418.0, mu_assist.WAIT_BLOCK_BOTH),
])
def test_wait_otherwise_blocked_without_consuming_a_slot(tw2_3slot_market_data, monkeypatch, cs, g_prev, decision):
    svc, now0, state, broker = _wait_world(tw2_3slot_market_data, monkeypatch, cs=cs, g_prev=g_prev)
    slots0 = int(state.tw2_3slot_slots_used_today or 0)
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0) + timedelta(seconds=20))
    assert _buys(broker) == []
    blocked = _block_rows(config.MU_ASSIST_WAIT_BLOCK_REASON)
    assert len(blocked) == 1 and blocked[0]["mu_wait_decision"] == decision
    assert int(state.tw2_3slot_slots_used_today or 0) == slots0


def test_confirm_never_runs_wait_selective(tw2_3slot_market_data, monkeypatch):
    """CONFIRM 은 기존처럼 진입하고 WAIT 판정 컬럼이 비어 있다(행 모양 불변)."""
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(strategy_mode.MODE_N1), _broker()
    _patch(monkeypatch)
    _on(monkeypatch, pattern=mu_assist.PATTERN_CONFIRM, hy=mu_assist.HY_EXPAND)

    def _boom(*a, **k):
        raise AssertionError("CONFIRM 인데 WAIT 선택진입 판정을 불렀다")
    monkeypatch.setattr(worker.mu_assist, "wait_selective_decision", _boom)
    _prime(state, now0)
    run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0) + timedelta(seconds=20))
    assert len(_buys(broker)) == 1
    assert _wait_rows() == []


# ── CATCH-UP 우선 ─────────────────────────────────────────────────────────
def test_catchup_pass_overrides_wait_block(monkeypatch, tmp_path):
    from tests.macd2.test_mu_catchup import _world, _row
    from tests.macd2.test_mu_confirm_only import _immediate
    svc, now0, state, broker, calls = _world(monkeypatch, tmp_path)
    monkeypatch.setattr(worker.mu_assist, "entry_veto_reason", lambda sid: config.MU_ASSIST_WAIT_BLOCK_REASON)
    monkeypatch.setattr(worker.mu_assist, "entry_gap_prev_for", lambda sid: -1303.8)    # 선택진입이면 막혔을 값
    monkeypatch.setattr(worker.exec_strength, "evaluate",
                        lambda d, now, data_dir=None: {"cttr": 90.0, "state": exec_strength.MISMATCH})
    _immediate(monkeypatch)
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert len(_buys(broker)) == 1
    row = _row(state)
    assert row["mu_catchup_pass"] == "PASS"
    assert row["catchup_overridden_gates"].endswith(config.MU_ASSIST_WAIT_BLOCK_REASON)
    assert row["mu_wait_decision"] == mu_assist.WAIT_BLOCK_BOTH          # 판정은 기록되지만 CATCH-UP 이 우선
