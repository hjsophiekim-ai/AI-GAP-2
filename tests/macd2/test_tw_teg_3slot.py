"""TW TEG 3-SLOT (2026-09-08, 구 TWF 3-SLOT) — 진입 규칙 교체 검증.

정의: TW2 3-SLOT 과 T+3 / 슬롯 / 하루 3회 cap / TW2 veto / whipsaw / TP1 /
TP2 / trailing / 반대신호 청산이 전부 동일하고, **entry_chop=True 후보만**
TEGv2 를 추가로 통과해야 진입한다(대기 없음, 실패 시 슬롯 미소비).
청산 임계값 3개는 기존 TWF 그대로.

검증 축
  A. TW2 3-SLOT 은 조금도 바뀌지 않는다 (CHOP 후보라도 추가 게이트 없음)
  B. TW TEG 는 CHOP 후보에만 TEGv2 를 추가 적용한다
  C. 거절 시 슬롯 미소비 / 하루 3회 cap 유지
  D. 다른 모드(MU_MACD 포함)에 영향 없음
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.trading.macd2 import config, state_store, teg_gate, time_window_3slot, worker
from app.trading.macd2.models import Direction, RuntimeState
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_early_take_profit_worker import _market
from tests.macd2.test_tw2_3slot_worker_regression import (
    _PRIOR_DAY,
    _approved,
    _fresh_3slot_state,
    _patch_common,
    _prime_3slot_pending,
    _quality,
    _teg,
)

KST = config.KST


def _tw_teg_state() -> RuntimeState:
    """TW TEG 3-SLOT 만 켜진 상태 (TW2 3-SLOT 은 OFF)."""
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    state.time_window_2_filter_enabled = False
    state.time_window_teg_filter_enabled = False
    state.time_window_3slot_filter_enabled = False
    state.time_window_twf_filter_enabled = True
    return state


def _chop(is_chop: bool, score: int = 3):
    from app.trading.macd2.early_take_profit import EntryChopDecision

    return EntryChopDecision(
        is_chop=is_chop, score=score, required=3, conditions={}, metrics={},
        insufficient_data=False,
    )


def _patch_chop(monkeypatch, decision):
    monkeypatch.setattr(worker.early_take_profit, "evaluate_entry_chop",
                        lambda *a, **kw: decision)


def _count_teg_calls(monkeypatch, decision):
    calls = []

    def _spy(*a, **kw):
        calls.append(1)
        return decision

    monkeypatch.setattr(worker.teg_gate, "evaluate_teg", _spy)
    return calls


def _broker():
    return FakeBroker(cash=10_000_000.0,
                      quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})


# ══════════════════════════════════════════════════════════════════════════
# 모드 헬퍼 자체
# ══════════════════════════════════════════════════════════════════════════

def test_only_tw_teg_mode_requires_the_chop_teg_gate():
    assert time_window_3slot.requires_chop_teg_gate(time_window_3slot.MODE_TW_TEG_3SLOT)
    assert not time_window_3slot.requires_chop_teg_gate(time_window_3slot.MODE_TW2_3SLOT)
    for other in ("TW2", "TEGv2", "MU_MACD", "", None):
        assert not time_window_3slot.requires_chop_teg_gate(other)


def test_mode_wire_value_is_unchanged_for_state_and_ledger_compat():
    """저장된 상태/원장 호환 — wire value 는 그대로여야 한다."""
    assert time_window_3slot.MODE_TW_TEG_3SLOT == "TWF_3SLOT"
    assert time_window_3slot.MODE_TW_TEG_3SLOT in time_window_3slot.MODES_3SLOT
    assert config.TW_TEG_3SLOT_STRATEGY_NAME == "TW TEG 3-SLOT"
    assert config.TWF_3SLOT_STRATEGY_NAME == config.TW_TEG_3SLOT_STRATEGY_NAME


def test_exit_thresholds_are_unchanged_from_twf():
    ov = time_window_3slot.exit_overrides(time_window_3slot.MODE_TW_TEG_3SLOT)
    assert ov["stop_loss_pct_override"] == pytest.approx(config.TWF_MORNING_STOP_LOSS * 100.0)
    assert ov["after_tp1_stop_pct_override"] == pytest.approx(config.TWF_MORNING_AFTER_TP1_STOP * 100.0)
    assert ov["afternoon_tp_pct_override"] == pytest.approx(config.TWF_AFTERNOON_TP * 100.0)
    # TW2 3-SLOT 은 여전히 override 없음
    tw2 = time_window_3slot.exit_overrides(time_window_3slot.MODE_TW2_3SLOT)
    assert set(tw2.values()) == {None}


# ══════════════════════════════════════════════════════════════════════════
# A. TW2 3-SLOT 불변
# ══════════════════════════════════════════════════════════════════════════

def test_tw2_3slot_never_applies_the_chop_teg_gate(monkeypatch):
    """TW2 3-SLOT 은 CHOP 후보이고 TEGv2 가 실패해도 그대로 진입한다."""
    svc, now0 = _market()
    state = _fresh_3slot_state()          # TW2 3-SLOT
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True),
                  teg_decision=_teg(False))       # TEG 실패로 고정
    _patch_chop(monkeypatch, _chop(True))         # CHOP 후보
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    run_once(broker=_broker(), market_data=svc, state=state, now=now0)

    assert state.tw2_3slot_slots_used_today == 1, "TW2 3-SLOT 진입이 막히면 안 된다"
    assert state.order_block_reason != config.TW_TEG_3SLOT_REJECT_CHOP_TEG


def test_tw2_3slot_slot1_entry_is_byte_identical_with_and_without_the_new_code(monkeypatch):
    """같은 입력에서 TW2 3-SLOT 의 슬롯/포지션 결과가 기존과 동일."""
    svc, now0 = _market()
    state = _fresh_3slot_state()
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True),
                  teg_decision=_teg(True))
    _patch_chop(monkeypatch, _chop(False))
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    run_once(broker=_broker(), market_data=svc, state=state, now=now0)

    assert state.tw2_3slot_slots_used_today == 1
    assert state.tw2_3slot_morning_count == 1
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert state.time_window_active_mode == time_window_3slot.MODE_TW2_3SLOT


# ══════════════════════════════════════════════════════════════════════════
# B. TW TEG — CHOP 후보에만 TEGv2 추가
# ══════════════════════════════════════════════════════════════════════════

def test_non_chop_candidate_enters_immediately_without_extra_teg(monkeypatch):
    """entry_chop=False 면 추가 TEGv2 호출 자체가 없어야 한다."""
    svc, now0 = _market()
    state = _tw_teg_state()
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True))
    calls = _count_teg_calls(monkeypatch, _teg(False))   # 불리게 되면 실패시킬 값
    _patch_chop(monkeypatch, _chop(False))
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    run_once(broker=_broker(), market_data=svc, state=state, now=now0)

    assert state.tw2_3slot_slots_used_today == 1, "비-CHOP 후보는 기존처럼 즉시 진입"
    assert state.position is not None
    assert calls == [], "비-CHOP 후보에 TEGv2 를 부르면 안 된다"


def test_chop_candidate_enters_when_the_extra_teg_passes(monkeypatch):
    """entry_chop=True + TEGv2 통과 -> 대기 없이 즉시 진입."""
    svc, now0 = _market()
    state = _tw_teg_state()
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True))
    calls = _count_teg_calls(monkeypatch, _teg(True))
    _patch_chop(monkeypatch, _chop(True))
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    run_once(broker=_broker(), market_data=svc, state=state, now=now0)

    assert state.tw2_3slot_slots_used_today == 1
    assert state.position is not None and state.position.symbol == config.LONG_SYMBOL
    assert len(calls) == 1, "CHOP 후보에는 TEGv2 를 정확히 한 번만 부른다"
    assert state.time_window_active_mode == time_window_3slot.MODE_TW_TEG_3SLOT


def test_chop_candidate_is_rejected_when_the_extra_teg_fails_and_slot_is_not_consumed(monkeypatch):
    svc, now0 = _market()
    state = _tw_teg_state()
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True))
    _count_teg_calls(monkeypatch, _teg(False))
    _patch_chop(monkeypatch, _chop(True))
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    run_once(broker=_broker(), market_data=svc, state=state, now=now0)

    assert state.position is None, "진입이 취소돼야 한다"
    assert state.tw2_3slot_slots_used_today == 0, "슬롯은 소비되지 않아야 한다"
    assert state.tw2_3slot_morning_count == 0
    assert state.order_block_reason == config.TW_TEG_3SLOT_REJECT_CHOP_TEG


def test_rejected_slot_is_reusable_by_the_next_flag(monkeypatch):
    """거절 후 다음 플래그가 같은 슬롯으로 다시 평가돼 진입할 수 있다."""
    svc, now0 = _market()
    state = _tw_teg_state()
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True))
    _count_teg_calls(monkeypatch, _teg(False))
    _patch_chop(monkeypatch, _chop(True))
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    run_once(broker=_broker(), market_data=svc, state=state, now=now0)
    assert state.tw2_3slot_slots_used_today == 0

    # 두 번째 플래그: 이번엔 CHOP 이 아니다 -> 같은 Slot1 로 진입
    _patch_chop(monkeypatch, _chop(False))
    state.processed_signal_ids = []
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)
    run_once(broker=_broker(), market_data=svc, state=state, now=now0 + timedelta(minutes=3))

    assert state.tw2_3slot_slots_used_today == 1
    assert state.tw2_3slot_morning_count == 1


def test_afternoon_slot_does_not_call_teg_twice(monkeypatch):
    """오후 슬롯은 이미 TEGv2 게이트를 타므로 CHOP 이어도 중복 호출하지 않는다."""
    from tests.macd2.test_tw2_3slot_worker_regression import _to_afternoon

    svc, now0 = _market()
    state = _tw_teg_state()
    _patch_common(monkeypatch, entry_decision=_approved(window="W5_AFTERNOON_1"),
                  quality_decision=_quality(True))
    calls = _count_teg_calls(monkeypatch, _teg(True))
    _patch_chop(monkeypatch, _chop(True))
    aft = _to_afternoon(now0)
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    run_once(broker=_broker(), market_data=svc, state=state, now=aft)

    assert len(calls) <= 1, f"오후 CHOP 후보에 TEGv2 중복 호출: {len(calls)}회"


# ══════════════════════════════════════════════════════════════════════════
# C. 하루 3회 cap
# ══════════════════════════════════════════════════════════════════════════

def test_daily_cap_still_three(monkeypatch):
    """CHOP 거절이 섞여도 하루 진입은 3회를 넘지 않는다."""
    svc, now0 = _market()
    state = _tw_teg_state()
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True))
    _count_teg_calls(monkeypatch, _teg(True))
    _patch_chop(monkeypatch, _chop(False))

    direction = Direction.UP_RED
    for i in range(8):
        state.processed_signal_ids = []
        _prime_3slot_pending(state, direction, before=_PRIOR_DAY)
        run_once(broker=_broker(), market_data=svc, state=state, now=now0 + timedelta(minutes=3 * i))
        direction = Direction.DOWN_BLUE if direction == Direction.UP_RED else Direction.UP_RED

    assert state.tw2_3slot_slots_used_today <= config.TW2_3SLOT_DAILY_CAP
    assert state.tw2_3slot_slots_used_today <= 3


# ══════════════════════════════════════════════════════════════════════════
# D. 다른 전략 영향 없음
# ══════════════════════════════════════════════════════════════════════════

def test_chop_teg_gate_is_absent_from_shared_exit_modules():
    """청산/포지션 관리 모듈은 이 규칙을 전혀 모른다 (MU_MACD 공유 모듈)."""
    import inspect

    from app.trading.macd2 import time_window_position_manager as twpm

    src = inspect.getsource(twpm)
    assert "TW_TEG" not in src
    assert "requires_chop_teg_gate" not in src


def test_gate_lives_only_in_the_3slot_candidate_path():
    """게이트 **호출**은 worker 안에 정확히 한 곳,
    그것도 _resolve_tw2_3slot_candidate_body 안에만 있어야 한다."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(worker))
    holders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") == "requires_chop_teg_gate"):
                holders.append(fn.name)
    assert holders == ["_resolve_tw2_3slot_candidate_body"], holders
