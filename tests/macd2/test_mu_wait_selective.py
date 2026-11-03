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
    worker._MU_WAIT_NOTES.clear(); worker._MU_ENTRY_NOTES.clear()
    yield
    mu_assist.reset_for_tests()
    worker._MU_WAIT_NOTES.clear(); worker._MU_ENTRY_NOTES.clear()


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
    i = cols.index(new[0])
    assert cols[i:i + len(new)] == new                                   # 연속·순서 유지
    assert cols[-3:] == ["mu_entry_status", "mu_unavailable_block", "mu_stale_block"]
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


# ── 12일 WAIT 7건 회귀 (2026-11-03 연구 표본, 값은 production 원장 hy_gap_prev / 로거 체결강도) ──
@pytest.mark.parametrize("case,cs,g_prev,approved", [
    ("1021_0906_RED", exec_strength.MATCH, -107.8342, True),
    ("1019_1142_RED", exec_strength.MATCH, -231.5855, True),
    ("1028_1225_RED", exec_strength.MATCH, -145.8169, True),
    ("1023_1054_BLUE", exec_strength.MISMATCH, -354.8331, False),     # 체결강도 불일치
    ("1021_0957_BLUE", exec_strength.MISMATCH, -418.0444, False),     # gap 418 (+체결강도 불일치)
    ("1022_0906_RED", exec_strength.MATCH, -1303.783, False),         # gap 1304
    ("1102_1117_RED", exec_strength.MATCH, -455.4129, False),         # gap 455
])
def test_twelve_day_wait_sample(case, cs, g_prev, approved):
    d, _ = mu_assist.wait_selective_decision(cs, g_prev)
    assert (d == mu_assist.WAIT_SELECTIVE_APPROVED) is approved, (case, d)


# ── 11/03 실제 사례 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("t2_status,reason", [
    (mu_assist.STATUS_MISSING, config.MU_ASSIST_UNAVAILABLE_BLOCK_REASON),     # 09:03 BLUE / 09:21 RED
    (mu_assist.STATUS_STALE, config.MU_ASSIST_STALE_BLOCK_REASON),
])
def test_1103_unavailable_entries_are_blocked(tw2_3slot_market_data, monkeypatch, t2_status, reason):
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(strategy_mode.MODE_N1), _broker()
    _patch(monkeypatch)
    _on(monkeypatch, pattern=mu_assist.PATTERN_UNAVAILABLE, hy=mu_assist.HY_FLAT, status=t2_status)
    _prime(state, now0)
    slots0 = int(state.tw2_3slot_slots_used_today or 0)
    run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    # STALE 은 T+2 창 끝(F+5:50) 까지 재시도한 뒤에 기록된다 -- production 처럼 창 끝 tick 을 돌린다.
    run_once(broker=broker, market_data=svc, state=state, now=now0 + timedelta(minutes=5, seconds=55))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0) + timedelta(seconds=20))
    assert _buys(broker) == []
    rows = _block_rows(reason)
    assert len(rows) == 1
    assert rows[0]["mu_entry_status"] == mu_assist.ENTRY_MU_UNAVAILABLE
    assert rows[0]["mu_unavailable_block" if reason == config.MU_ASSIST_UNAVAILABLE_BLOCK_REASON
                   else "mu_stale_block"] == "True"
    assert int(state.tw2_3slot_slots_used_today or 0) == slots0


def test_1103_1127_blue_wait_passes_mu_but_daily_slot_cap_still_blocks(tw2_3slot_market_data, monkeypatch):
    """11:27 BLUE — ENTRY_WAIT, 체결강도 98.39(<100 = BLUE 일치), 반대 gap 207 -> MU 단계 통과,
    그러나 하루 슬롯 한도는 그대로라 최종 차단(기존과 같은 DAILY_SLOT_CAP)."""
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(strategy_mode.MODE_N1), _broker()
    _patch(monkeypatch)
    _on(monkeypatch, pattern=mu_assist.PATTERN_STALL, hy=mu_assist.HY_EXPAND)          # ENTRY_WAIT
    monkeypatch.setattr(worker.exec_strength, "evaluate",
                        lambda d, now, data_dir=None: {"cttr": 98.39, "state": exec_strength.MATCH})
    monkeypatch.setattr(worker.mu_assist, "entry_gap_prev_for", lambda sid: -207.0)
    _prime(state, now0)
    state.tw2_3slot_slots_used_today = worker._tw2_3slot_daily_cap(state)
    run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    assert _buys(broker) == []
    rows = _wait_rows()
    assert rows, "WAIT 판정이 원장에 없다"
    r = rows[-1]
    assert r["mu_wait_decision"] == mu_assist.WAIT_SELECTIVE_APPROVED and r["mu_entry_status"] == mu_assist.ENTRY_WAIT
    assert "CAP" in r["block_reason"], r["block_reason"]
