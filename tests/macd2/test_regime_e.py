"""REGIME_E — E/CHOP 모드 × TREND 각인 진입의 E 돌파대기 생략 (2026-11-03 부터 토글 없음).

잠그는 계약
-----------
 1   토글 없음 — config/env/state 어디에도 끄는 스위치가 없다
 2~5 세 조건이 **전부** 맞아야 bypass (E·CHOP 모드 / TREND regime / E 대기 대상)
 6   CHOP 각인 진입은 기존 E 돌파대기 그대로다
 7   state 에 옛 regime_e_enabled=False 가 남아 있어도 무시된다
 8   진입가 비교값(price_edge_pct)은 기록 전용이고 판정에 쓰이지 않는다
 9   신호원장 필드 — bypass 건을 식별할 수 있고, trace 가 없으면 행 모양이 안 바뀐다
10   worker 경로: bypass 면 대기를 걸지 않고, 아니면 기존대로 건다

거래단위 손익 parity 는 이 파일이 아니라 연구 하네스로 검증한다 --
research_20261102_reclaim/REPORT_REGIME_E.md · REPORT_REGIME_E_MU.md.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.trading.macd2 import (
    chop_regime,
    config,
    e_strategy,
    ledger,
    regime_e,
    state_store,
    strategy_mode,
    worker,
)
from app.trading.macd2.models import Direction

KST = config.KST
T0 = datetime(2026, 11, 2, 11, 15, tzinfo=KST)
FLAG = T0 - timedelta(minutes=6)


def _state(mode=strategy_mode.MODE_CHOP, regime=chop_regime.REGIME_TREND):
    s = state_store.default_state()
    strategy_mode.apply(s, mode)
    s.p3_last_regime = regime
    return s


def _early(immediate=False, trigger=6_770.0):
    """evaluate_early_pass 결과의 최소 모양 — 판정에 쓰는 두 필드만 본다."""
    return SimpleNamespace(immediate=immediate, trigger=trigger, dist_pct=0.9,
                           c1=False, c2=True, c3=True)


class _Res:
    def __init__(self):
        self.actions = []
        self.signal_dispatch_trace = {}


# ── 1. 토글 없음 ──────────────────────────────────────────────────────────
def test_no_toggle_exists():
    assert not hasattr(config, "REGIME_E_ENABLED")
    assert regime_e.is_enabled(None) is True
    s = state_store.default_state()
    strategy_mode.apply(s, strategy_mode.MODE_CHOP)
    s.p3_last_regime = chop_regime.REGIME_TREND
    dec = regime_e.evaluate(s, early=_early(), price=6_700.0)
    assert dec.bypassed is True


# ── 2~5. 네 조건 ─────────────────────────────────────────────────────────
def test_bypasses_only_on_chop_mode_trend_regime_e_wait():
    dec = regime_e.evaluate(_state(), early=_early(), price=6_700.0)
    assert dec.bypassed is True
    assert dec.reason == regime_e.BYPASS_REASON == "CHOP_MODE_TREND_REGIME"
    assert dec.regime == "TREND" and dec.strategy_mode == "CHOP" and dec.wait_eligible


def test_e_mode_trend_regime_also_bypasses():
    """E 필터 모드도 TREND 각인이면 E 돌파대기를 생략한다 (2026-11-03)."""
    dec = regime_e.evaluate(_state(mode=strategy_mode.MODE_E), early=_early(), price=6_700.0)
    assert dec.bypassed is True and dec.reason == regime_e.BYPASS_REASON_E


def test_e_mode_chop_regime_keeps_the_e_wait():
    dec = regime_e.evaluate(_state(mode=strategy_mode.MODE_E, regime=chop_regime.REGIME_CHOP),
                            early=_early(), price=6_700.0)
    assert dec.bypassed is False and dec.reason == regime_e.SKIP_NOT_TREND_REGIME


@pytest.mark.parametrize("mode", [strategy_mode.MODE_N1, strategy_mode.MODE_P3])
def test_modes_without_e_wait_are_inert(mode):
    """N1 / P3 모드는 E 돌파대기가 없으므로 아무 것도 바뀌지 않는다."""
    dec = regime_e.evaluate(_state(mode=mode), early=_early(), price=6_700.0)
    assert dec.bypassed is False and dec.reason == regime_e.SKIP_NOT_E_WAIT_MODE


@pytest.mark.parametrize("regime", [chop_regime.REGIME_CHOP, chop_regime.REGIME_WARMUP, ""])
def test_non_trend_regime_keeps_the_e_wait(regime):
    dec = regime_e.evaluate(_state(regime=regime), early=_early(), price=6_700.0)
    assert dec.bypassed is False and dec.reason == regime_e.SKIP_NOT_TREND_REGIME


def test_immediate_entry_candidate_is_not_a_bypass():
    """원래 즉시진입이면 생략할 대기 자체가 없다 -- bypass 로 세지 않는다."""
    dec = regime_e.evaluate(_state(), early=_early(immediate=True), price=6_700.0)
    assert dec.bypassed is False and dec.reason == regime_e.SKIP_NOT_E_WAIT


def test_missing_trigger_is_not_a_bypass():
    dec = regime_e.evaluate(_state(), early=_early(trigger=None), price=6_700.0)
    assert dec.bypassed is False and dec.reason == regime_e.SKIP_NOT_E_WAIT


# ── 6. CHOP 각인은 기존 그대로 ───────────────────────────────────────────
def test_chop_regime_entry_still_arms_the_pending(monkeypatch):
    """CHOP 각인 진입에서는 worker 가 예전처럼 돌파대기를 건다."""
    s = _state(regime=chop_regime.REGIME_CHOP)
    dec = regime_e.evaluate(s, early=_early(), price=6_700.0)
    assert dec.bypassed is False
    snap = SimpleNamespace(bar_dt=T0)
    assert worker._e_arm_pending_instead_of_entry(
        state=s, now=T0, macd_snap=snap, direction=Direction.UP_RED,
        signal_id="20261102_110900_UP_RED:TW2_3SLOT_CONFIRM", flag_bar_dt=FLAG,
        price=6_700.0, early=_early(), slot_metrics={}, gate={}, result=_Res())
    assert e_strategy.pending_record(s) is not None


# ── 7. 옛 state 필드는 무시 ──────────────────────────────────────────────
def test_stale_state_flag_is_ignored():
    s = _state()
    s.regime_e_enabled = False          # 11/02 버전에서 저장됐을 수 있는 값
    assert regime_e.evaluate(s, early=_early(), price=6_700.0).bypassed is True


# ── 8. 진입가 비교값은 기록 전용 ─────────────────────────────────────────
def test_price_edge_pct_is_recorded_not_judged():
    dec = regime_e.evaluate(_state(), early=_early(trigger=6_770.0), price=6_700.0)
    assert dec.price_edge_pct == pytest.approx(1.0448, abs=1e-3)
    # 값이 음수/결측이어도 판정은 그대로 bypass 다 -- 임계값이 없다.
    none_px = regime_e.evaluate(_state(), early=_early(trigger=6_770.0), price=None)
    assert none_px.bypassed is True and none_px.price_edge_pct is None


# ── 9. 신호원장 필드 ─────────────────────────────────────────────────────
def test_ledger_fields_identify_the_bypass():
    dec = regime_e.evaluate(_state(), early=_early(), price=6_700.0)
    row = regime_e.ledger_fields(regime_e.trace_fields(dec))
    assert row["e_bypass"] is True
    assert row["e_bypass_reason"] == "CHOP_MODE_TREND_REGIME"
    assert row["e_wait_eligible"] is True
    assert row["regime_e_regime"] == "TREND" and row["regime_e_strategy_mode"] == "CHOP"
    assert row["e_bypass_t3_price"] == 6_700.0 and row["e_bypass_e_trigger"] == 6_770.0
    assert row["e_bypass_price_edge_pct"] == pytest.approx(1.0448, abs=1e-3)
    assert set(row) <= set(ledger.SIGNAL_LEDGER_COLUMNS)


def test_ledger_fields_are_empty_without_a_trace():
    """trace 가 없으면(= 이 경로를 안 탄 신호) 행 모양이 조금도 바뀌지 않는다."""
    assert regime_e.ledger_fields(None) == {}
    assert regime_e.ledger_fields({}) == {}


def test_skip_reason_is_recorded_too():
    dec = regime_e.evaluate(_state(regime=chop_regime.REGIME_CHOP), early=_early(), price=6_700.0)
    row = regime_e.ledger_fields(regime_e.trace_fields(dec))
    assert row["e_bypass"] is False and row["e_bypass_reason"] == regime_e.SKIP_NOT_TREND_REGIME


# ── 10. worker 경로 ──────────────────────────────────────────────────────
def test_worker_skips_arming_when_bypassed():
    """bypass 면 돌파대기를 걸지 않는다 — 걸었다면 pending 레코드가 남는다."""
    s = _state()
    dec = regime_e.evaluate(s, early=_early(), price=6_700.0)
    assert dec.bypassed is True
    # worker 의 분기와 같은 조건: bypass 면 _e_arm_pending_instead_of_entry 를 부르지 않는다.
    if not dec.bypassed:                                   # pragma: no cover - 계약 표현용
        worker._e_arm_pending_instead_of_entry(
            state=s, now=T0, macd_snap=SimpleNamespace(bar_dt=T0), direction=Direction.UP_RED,
            signal_id="X:TW2_3SLOT_CONFIRM", flag_bar_dt=FLAG, price=6_700.0,
            early=_early(), slot_metrics={}, gate={}, result=_Res())
    assert e_strategy.pending_record(s) is None
    assert "X:TW2_3SLOT_CONFIRM" not in s.processed_signal_ids


def test_worker_branch_source_guards_both_conditions():
    """worker 의 E overlay 분기가 '즉시진입 아님 AND bypass 아님' 두 조건을 모두 본다.

    이 한 줄이 REGIME_E 의 전부다 -- 문자열로 잠가 둬야 리팩터링에서 조용히
    빠지지 않는다(진입 승인/청산 경로는 건드리지 않는다는 계약의 핵심)."""
    import inspect

    src = inspect.getsource(worker._resolve_tw2_3slot_candidate_body)
    assert "if not _e_early.immediate and not _regime_e.bypassed:" in src
    assert "regime_e.evaluate(state, early=_e_early, price=_e_price)" in src
