"""MU ASSIST 최종형 (MACD2_MU_ASSIST_ENABLED, 2026-10-22) — worker.run_once() 경로 + 순수 함수 테스트.

고정하는 계약
-------------
0. OFF 면 훅이 한 번도 호출되지 않는다(원장 파일도 없다) — 기존 동작 그대로
1. 진입: T+2 에 ENTRY_* 상태 1행. ENTRY_BAD 만 T+3 신규진입 차단(슬롯 미소비). GOOD/WAIT/판정불가/기록없음은 그대로
2. 청산: H50 HOLD 대상 반대플래그 + T+2 gap 확대 + MU CONFIRM 셋 다일 때만 T+2 청산, T+3 반대매수 없음
3. gap 만 확대 / MU 만 CONFIRM / MU 판정불가 → 청산 없음, T+3 H50 HOLD 그대로
4. H50 이 없는 모드에서는 청산 no-op
5. P3 / E / CHOP / N1 각각 같은 토글로 동작
6. 중복 매도·중복 진입 0, MU 계산 오류 시 기존 전략 fallback
7. 순수 함수: 패턴/HY gap/진입상태 정의가 사전등록(PREREG_mu_pattern.md)과 같다
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.trading.macd2 import config, ledger, mu_assist, state_store, strategy_mode, worker
from app.trading.macd2 import small_whipsaw_hold as swh
from app.trading.macd2 import time_window_3slot as tw3
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_tw2_3slot_worker_regression import (  # noqa: F401  (fixture)
    _approved,
    _patch_common,
    _quality,
    _teg,
    tw2_3slot_market_data,
)

KST = config.KST


# ── helpers ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_mu_assist():
    mu_assist.reset_for_tests()
    yield
    mu_assist.reset_for_tests()


def _pattern(state: str, status: str = mu_assist.STATUS_OK) -> dict:
    ok = state != mu_assist.PATTERN_UNAVAILABLE
    return {"mu_t2_status": status, "mu_pattern_state": state,
            "mu_post2_bp": 5.0 if ok else "", "mu_acc2": 3.0 if ok else "", "mu_pre3_return_bp": 1.0 if ok else "",
            "mu_hold_ok": ok, "mu_lead_ok": ok, "mu_cross_at": ""}


def _on(monkeypatch, *, pattern: str = mu_assist.PATTERN_CONFIRM, hy: str = mu_assist.HY_EXPAND,
        status: str = mu_assist.STATUS_OK):
    """토글 ON + MU 패턴/HY 상태를 고정한다(배선 테스트). 계산 정의 자체는 아래 순수 함수 테스트가 본다."""
    monkeypatch.setattr(config, "MU_ASSIST_ENABLED", True)
    # 2026-10-23: 실제 ON/OFF 는 저장 state(mu_assist_enabled)가 기준이다. 배선 테스트는 state 를 만든 뒤
    # 켜므로 판정 함수를 고정한다(state 기준 동작 자체는 test_mu_assist_toggle.py 가 본다).
    monkeypatch.setattr(worker.mu_assist, "is_enabled", lambda *a, **k: True)
    monkeypatch.setattr(worker.mu_assist, "load_mu_1m", lambda *a, **k: None)
    monkeypatch.setattr(worker.mu_assist, "evaluate_pattern", lambda *a, **k: _pattern(pattern, status))
    monkeypatch.setattr(worker, "_hy_gap_t2_bar", lambda *a, **k: 0.0)
    monkeypatch.setattr(worker.mu_assist, "hy_gap_state", lambda *a, **k: hy)


def _mode_state(mode: str) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    strategy_mode.apply(state, mode)
    return state


def _legacy_state(flag: str) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    for f in ("time_window_2_filter_enabled", "time_window_teg_filter_enabled",
              "time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
              "time_window_x2lite_filter_enabled", "time_window_h50_filter_enabled",
              "time_window_n1_filter_enabled"):
        setattr(state, f, f == flag)
    return state


def _broker() -> FakeBroker:
    return FakeBroker(cash=10_000_000.0,
                      quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})


def _seed_inverse(state: RuntimeState, broker: FakeBroker, now: datetime, qty: int = 10) -> None:
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=qty,
                                      avg_price=10_000.0, entry_at=now - timedelta(minutes=30))
    state.time_window_position_active = True
    state.time_window_active_mode = tw3.active_3slot_mode(state) or tw3.MODE_N1_3SLOT
    state.time_window_entry_session = "MORNING"
    state.tw2_3slot_slots_used_today = 1
    state.tw2_3slot_morning_count = 1
    broker.buy_market(config.INVERSE_SYMBOL, qty, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0


def _hold(should_hold: bool) -> swh.HoldDecision:
    return swh.HoldDecision(should_hold=should_hold, trend=swh.TREND_DOWN, range_pct=1.2,
                            trend_ok=should_hold, range_ok=should_hold, insufficient_data=False,
                            reason="SMALL_WHIPSAW_HOLD" if should_hold else "RANGE_TOO_WIDE")


def _patch(monkeypatch, *, hold: bool = True, t2_gap: float | None = 1e9):
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True, 5),
                  teg_decision=_teg(True))
    monkeypatch.setattr(worker.small_whipsaw_hold, "evaluate_hold", lambda *a, **kw: _hold(hold))
    monkeypatch.setattr(worker.small_whipsaw_hold, "evaluate_release",
                        lambda *a, **kw: swh.ReleaseDecision(False, swh.TREND_DOWN, 0, 1.0, "HOLDING"))
    if t2_gap is not None:
        monkeypatch.setattr(worker, "calculate_provisional_macd",
                            lambda *a, **kw: SimpleNamespace(current_diff=t2_gap))


def _prime(state: RuntimeState, flag_bar: datetime, direction: Direction = Direction.UP_RED) -> None:
    state.tw2_3slot_pending_flag_direction = direction
    state.tw2_3slot_pending_flag_bar_ts = flag_bar.isoformat()


def _t2(flag_bar: datetime) -> datetime:
    return flag_bar + timedelta(minutes=5, seconds=20)


def _t3(flag_bar: datetime) -> datetime:
    return flag_bar + timedelta(minutes=6, seconds=5)


def _mu_rows() -> list[dict]:
    p = mu_assist.ledger_path()
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _sells(broker) -> list:
    return [o for o in broker.orders if o.side == "SELL" and o.success]


def _buys(broker) -> list:
    return [o for o in broker.orders if o.side == "BUY" and o.success]


def _block_rows(reason: str) -> list[dict]:
    return [r for r in ledger.load_signal_ledger(limit=0) if r.get("block_reason") == reason]


# ── 0. OFF ────────────────────────────────────────────────────────────────

def test_default_is_off():
    assert config.MU_ASSIST_ENABLED is False


def test_off_never_calls_any_hook_and_h50_holds_as_before(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_N1)
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True)

    def _boom(*_a, **_kw):
        raise AssertionError("OFF 인데 MU ASSIST 훅이 호출됐다")

    for name in ("_advance_mu_assist_entry_shadow", "_advance_mu_assist_h50_gap_exit"):
        monkeypatch.setattr(worker, name, _boom)
    for name in ("exited_on_flag", "entry_veto_reason", "evaluate_pattern", "load_mu_1m"):
        monkeypatch.setattr(worker.mu_assist, name, _boom)
    _prime(state, flag)

    run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    result = run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))

    assert any(a.startswith(config.H50_HOLD_BLOCK_REASON) for a in result.actions), result.actions
    assert state.position is not None and _sells(broker) == []
    assert not mu_assist.ledger_path().exists(), "OFF 인데 MU ASSIST 원장이 생겼다"


def _scenario(svc, state, broker, flag):
    _prime(state, flag)
    a1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(flag)).actions
    a2 = run_once(broker=broker, market_data=svc, state=state, now=_t3(flag)).actions
    return list(a1) + list(a2)


@pytest.mark.parametrize("held", [True, False])
def test_on_without_trigger_is_identical_to_off(monkeypatch, held):
    """ON 이어도 BAD 가 아니고 청산 조건이 안 맞으면 주문·액션·포지션이 OFF 와 같다."""
    from tests.macd2.test_h50_hold_lifecycle import _make_market_data, _reset_dispatch_history

    def run(on: bool):
        svc, now0 = _make_market_data()
        st, br = _mode_state(strategy_mode.MODE_N1), _broker()
        if held:
            _seed_inverse(st, br, now0)
        _patch(monkeypatch, hold=True, t2_gap=-1e9)
        if on:
            _on(monkeypatch, pattern=mu_assist.PATTERN_STALL, hy=mu_assist.HY_EXPAND)   # = WAIT
        return _scenario(svc, st, br, now0), br, st

    off_actions, br_a, st_a = run(False)
    _reset_dispatch_history()
    mu_assist.reset_for_tests()
    on_actions, br_b, st_b = run(True)

    legs = lambda b: [(o.symbol, o.side, o.requested_qty, o.executed_qty, o.success) for o in b.orders]
    assert on_actions == off_actions
    assert legs(br_b) == legs(br_a)
    assert (st_a.position is None) == (st_b.position is None)


# ── 1. 진입 ──────────────────────────────────────────────────────────────

def test_entry_bad_blocks_the_t3_entry_without_consuming_a_slot(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_N1)
    broker = _broker()
    _patch(monkeypatch)
    _on(monkeypatch, pattern=mu_assist.PATTERN_STALL, hy=mu_assist.HY_FLAT)
    _prime(state, flag)
    slots0 = int(state.tw2_3slot_slots_used_today or 0)

    run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    rows = [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_ENTRY]
    assert len(rows) == 1
    r = rows[0]
    assert r["mu_entry_assist_status"] == mu_assist.ENTRY_BAD
    assert r["mu_pattern_state"] == mu_assist.PATTERN_STALL and r["hy_gap_t2_state"] == mu_assist.HY_FLAT
    for col in ("mu_post2_bp", "mu_acc2", "mu_pattern_state", "hy_gap_t2_state", "mu_entry_assist_status"):
        assert col in r

    run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(flag) + timedelta(seconds=20))
    assert _buys(broker) == [], "ENTRY_BAD 인데 진입했다"
    assert len(_block_rows(config.MU_ASSIST_ENTRY_BAD_BLOCK_REASON)) == 1
    assert int(state.tw2_3slot_slots_used_today or 0) == slots0, "차단이 슬롯을 소비했다"


@pytest.mark.parametrize("pattern,hy", [
    (mu_assist.PATTERN_DIVERGE, mu_assist.HY_SHRINK),
    (mu_assist.PATTERN_STALL, mu_assist.HY_SHRINK),
    (mu_assist.PATTERN_DIVERGE, mu_assist.HY_FLAT),
])
def test_every_bad_combination_blocks(tw2_3slot_market_data, monkeypatch, pattern, hy):
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(strategy_mode.MODE_N1), _broker()
    _patch(monkeypatch)
    _on(monkeypatch, pattern=pattern, hy=hy)
    _prime(state, now0)
    run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    assert _buys(broker) == []
    assert len(_block_rows(config.MU_ASSIST_ENTRY_BAD_BLOCK_REASON)) == 1


@pytest.mark.parametrize("pattern,hy,status", [
    (mu_assist.PATTERN_CONFIRM, mu_assist.HY_EXPAND, mu_assist.ENTRY_CONFIRM),
    (mu_assist.PATTERN_STALL, mu_assist.HY_EXPAND, mu_assist.ENTRY_WAIT),
    (mu_assist.PATTERN_CONFIRM, mu_assist.HY_FLAT, mu_assist.ENTRY_WAIT),
    (mu_assist.PATTERN_DIVERGE, mu_assist.HY_EXPAND, mu_assist.ENTRY_WAIT),
    (mu_assist.PATTERN_UNAVAILABLE, mu_assist.HY_FLAT, mu_assist.ENTRY_MU_UNAVAILABLE),
])
def test_non_bad_states_enter_exactly_as_before(tw2_3slot_market_data, monkeypatch, pattern, hy, status):
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(strategy_mode.MODE_N1), _broker()
    _patch(monkeypatch)
    _on(monkeypatch, pattern=pattern, hy=hy,
        status=mu_assist.STATUS_MISSING if pattern == mu_assist.PATTERN_UNAVAILABLE else mu_assist.STATUS_OK)
    _prime(state, now0)
    run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    assert [r["mu_entry_assist_status"] for r in _mu_rows()] == [status]
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0) + timedelta(seconds=20))
    assert len(_buys(broker)) == 1, (status, broker.orders)


def test_missing_t2_record_fails_open(tw2_3slot_market_data, monkeypatch):
    """T+2 창을 놓쳐(재시작 등) 기록이 없으면 막지 않는다."""
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(strategy_mode.MODE_N1), _broker()
    _patch(monkeypatch)
    _on(monkeypatch, pattern=mu_assist.PATTERN_STALL, hy=mu_assist.HY_FLAT)
    _prime(state, now0)
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    assert len(_buys(broker)) == 1
    assert _mu_rows() == []


def test_entry_never_starts_before_t3(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(strategy_mode.MODE_N1), _broker()
    _patch(monkeypatch)
    _on(monkeypatch, pattern=mu_assist.PATTERN_CONFIRM, hy=mu_assist.HY_EXPAND)
    _prime(state, now0)
    for t in (now0 + timedelta(minutes=3, seconds=20), now0 + timedelta(minutes=4, seconds=20), _t2(now0)):
        run_once(broker=broker, market_data=svc, state=state, now=t)
    assert _buys(broker) == [], "T+3 전에 진입했다"


def test_mu_error_in_entry_falls_back_to_existing_strategy(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(strategy_mode.MODE_N1), _broker()
    _patch(monkeypatch)
    _on(monkeypatch)

    def _broken(*_a, **_k):
        raise ValueError("parser broke")

    monkeypatch.setattr(worker.mu_assist, "evaluate_pattern", _broken)
    _prime(state, now0)
    run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    assert len(_buys(broker)) == 1


# ── 2/3. 청산 ────────────────────────────────────────────────────────────

def test_gap_expanding_and_mu_confirm_exits_at_t2_and_never_buys_the_opposite(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_CHOP)
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True, t2_gap=1e9)
    _on(monkeypatch, pattern=mu_assist.PATTERN_CONFIRM, hy=mu_assist.HY_EXPAND)
    _prime(state, flag)

    r1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    assert any(a.startswith(config.EXIT_MU_ASSIST_H50_GAP) for a in r1.actions), r1.actions
    assert state.position is None and state.h50_hold_active is False
    run_once(broker=broker, market_data=svc, state=state, now=_t2(flag) + timedelta(seconds=20))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(flag) + timedelta(seconds=20))
    assert len(_sells(broker)) == 1, "중복 매도"
    assert len(_buys(broker)) == 1, "반대 ETF 를 샀다(시드 매수 1건만 있어야 한다)"
    ex = [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_H50_EXIT]
    assert len(ex) == 1 and ex[0]["exit_result"] == "EXECUTED" and ex[0]["exit_decision"] == "EXIT"
    exec_rows = ledger.load_execution_ledger(limit=0)
    assert [r["exit_reason"] for r in exec_rows if r.get("side") == "SELL"] == [config.EXIT_MU_ASSIST_H50_GAP]


@pytest.mark.parametrize("pattern,t2_gap,label", [
    (mu_assist.PATTERN_STALL, 1e9, "HOLD_MU_NOT_CONFIRM"),        # gap 만 확대 (10/22 12:21 형태)
    (mu_assist.PATTERN_DIVERGE, 1e9, "HOLD_MU_NOT_CONFIRM"),
    (mu_assist.PATTERN_UNAVAILABLE, 1e9, "HOLD_MU_UNAVAILABLE"),  # stale/결측 -> 기존 H50
    (mu_assist.PATTERN_CONFIRM, -1e9, "HOLD_GAP_NOT_EXPANDING"),  # MU 만 CONFIRM
])
def test_exit_needs_both_gap_and_mu_confirm(tw2_3slot_market_data, monkeypatch, pattern, t2_gap, label):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_CHOP)
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True, t2_gap=t2_gap)
    _on(monkeypatch, pattern=pattern, hy=mu_assist.HY_EXPAND,
        status=mu_assist.STATUS_MISSING if pattern == mu_assist.PATTERN_UNAVAILABLE else mu_assist.STATUS_OK)
    _prime(state, flag)

    r1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    assert not any(config.EXIT_MU_ASSIST_H50_GAP in a for a in r1.actions)
    r2 = run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))
    assert any(a.startswith(config.H50_HOLD_BLOCK_REASON) for a in r2.actions), r2.actions
    assert state.position is not None and _sells(broker) == []
    kept = [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_H50_KEPT]
    assert len(kept) == 1 and kept[0]["exit_decision"] == label


def test_stale_mu_waits_inside_the_window_then_falls_back(tw2_3slot_market_data, monkeypatch):
    """로거 분봉이 늦으면 T+2 창 안에서는 다시 시도하고, 끝까지 stale 이면 청산 없이 H50 HOLD."""
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state, broker = _mode_state(strategy_mode.MODE_CHOP), _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True, t2_gap=1e9)
    _on(monkeypatch, pattern=mu_assist.PATTERN_UNAVAILABLE, hy=mu_assist.HY_EXPAND, status=mu_assist.STATUS_STALE)
    _prime(state, flag)
    run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    assert [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_H50_KEPT] == []      # 아직 재시도 중
    run_once(broker=broker, market_data=svc, state=state, now=flag + timedelta(minutes=5, seconds=55))
    kept = [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_H50_KEPT]
    assert len(kept) == 1 and kept[0]["exit_decision"] == "HOLD_MU_UNAVAILABLE"
    assert _sells(broker) == []


def test_mu_error_in_exit_keeps_the_h50_hold(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(strategy_mode.MODE_CHOP), _broker()
    _seed_inverse(state, broker, now0)
    _patch(monkeypatch, hold=True, t2_gap=1e9)
    _on(monkeypatch)

    def _broken(*_a, **_k):
        raise ValueError("parser broke")

    monkeypatch.setattr(worker.mu_assist, "evaluate_pattern", _broken)
    _prime(state, now0)
    run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    r2 = run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    assert _sells(broker) == []
    assert any(a.startswith(config.H50_HOLD_BLOCK_REASON) for a in r2.actions)


def test_flag_that_h50_would_not_hold_is_left_to_the_t3_path(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(strategy_mode.MODE_CHOP), _broker()
    _seed_inverse(state, broker, now0)
    _patch(monkeypatch, hold=False, t2_gap=1e9)
    _on(monkeypatch)
    _prime(state, now0)
    r1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    assert not any(config.EXIT_MU_ASSIST_H50_GAP in a for a in r1.actions)


# ── 4. H50 없는 모드 / 5. 모드별 ──────────────────────────────────────────

def test_mode_without_h50_is_a_noop_for_exit(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = _legacy_state("time_window_x2lite_filter_enabled")
    assert swh.is_active(state) is False
    broker = _broker()
    _seed_inverse(state, broker, now0)
    _patch(monkeypatch, hold=True, t2_gap=1e9)
    _on(monkeypatch)
    _prime(state, now0)
    r1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    assert not any(config.EXIT_MU_ASSIST_H50_GAP in a for a in r1.actions)
    assert not [r for r in _mu_rows() if r["kind"] in (mu_assist.KIND_H50_EXIT, mu_assist.KIND_H50_KEPT)]


@pytest.mark.parametrize("mode", [strategy_mode.MODE_P3, strategy_mode.MODE_E,
                                  strategy_mode.MODE_CHOP, strategy_mode.MODE_N1])
def test_every_strategy_mode_uses_the_same_toggle(tw2_3slot_market_data, monkeypatch, mode):
    svc, now0 = tw2_3slot_market_data
    # 청산
    state = _mode_state(mode)
    assert swh.is_active(state) is True, f"{mode} 에서 H50 이 꺼져 있다"
    broker = _broker()
    _seed_inverse(state, broker, now0)
    _patch(monkeypatch, hold=True, t2_gap=1e9)
    _on(monkeypatch, pattern=mu_assist.PATTERN_CONFIRM, hy=mu_assist.HY_EXPAND)
    _prime(state, now0)
    r1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    assert any(a.startswith(config.EXIT_MU_ASSIST_H50_GAP) for a in r1.actions), (mode, r1.actions)
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    assert len(_sells(broker)) == 1 and len(_buys(broker)) == 1, (mode, broker.orders)


@pytest.mark.parametrize("mode", [strategy_mode.MODE_P3, strategy_mode.MODE_E,
                                  strategy_mode.MODE_CHOP, strategy_mode.MODE_N1])
def test_every_strategy_mode_applies_the_entry_veto(tw2_3slot_market_data, monkeypatch, mode):
    svc, now0 = tw2_3slot_market_data
    state, broker = _mode_state(mode), _broker()
    _patch(monkeypatch)
    _on(monkeypatch, pattern=mu_assist.PATTERN_STALL, hy=mu_assist.HY_SHRINK)
    _prime(state, now0)
    run_once(broker=broker, market_data=svc, state=state, now=_t2(now0))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(now0) + timedelta(minutes=16))
    assert _buys(broker) == [], (mode, broker.orders)
    assert len(_block_rows(config.MU_ASSIST_ENTRY_BAD_BLOCK_REASON)) == 1, mode


# ── 7. 순수 함수 (사전등록 정의) ──────────────────────────────────────────

def _series(fn, start=datetime(2026, 10, 22, 8, 0), end=datetime(2026, 10, 22, 11, 0)):
    import pandas as pd
    idx = pd.date_range(start, end, freq="1min", inclusive="left")
    return pd.Series([fn(t) for t in idx], index=idx)


FLAG = datetime(2026, 10, 22, 10, 0, tzinfo=KST)        # D = 10:03, T+2 = 10:05


def _v_shape(turn_min, after):
    """turn 전에는 하락, 이후 `after(분)` 으로 움직이는 MU."""
    turn = datetime(2026, 10, 22, 10, 0) + timedelta(minutes=turn_min)

    def f(t):
        k = (t - turn).total_seconds() / 60
        return 900.0 - 0.1 * k if k < 0 else 900.0 + after(k)
    return f


def test_pattern_confirm_stall_diverge_follow_the_prereg_definition():
    confirm = mu_assist.evaluate_pattern(_series(_v_shape(-8, lambda k: 0.05 * k * k)), Direction.UP_RED, FLAG)
    assert confirm["mu_pattern_state"] == mu_assist.PATTERN_CONFIRM, confirm
    # 플래그 전 급등 후 D 이후 정체 -> 방향은 같지만 둔화 (10/22 09:03 RED 형태)
    stall = mu_assist.evaluate_pattern(_series(_v_shape(-8, lambda k: 1.0 * min(k, 9.0))), Direction.UP_RED, FLAG)
    assert stall["mu_pattern_state"] == mu_assist.PATTERN_STALL, stall
    assert stall["mu_acc2"] < mu_assist.CONFIRM_ACC2_MIN
    diverge = mu_assist.evaluate_pattern(_series(lambda t: 900.0 - 0.001 * (t.hour * 60 + t.minute) ** 1.5),
                                         Direction.UP_RED, FLAG)
    assert diverge["mu_pattern_state"] == mu_assist.PATTERN_DIVERGE, diverge


def test_pattern_unavailable_when_missing_stale_or_warming_up():
    assert mu_assist.evaluate_pattern(None, Direction.UP_RED, FLAG)["mu_pattern_state"] == mu_assist.PATTERN_UNAVAILABLE
    stale = _series(_v_shape(-8, lambda k: 0.05 * k * k), end=datetime(2026, 10, 22, 10, 2))
    assert mu_assist.evaluate_pattern(stale, Direction.UP_RED, FLAG)["mu_t2_status"] == mu_assist.STATUS_STALE
    warm = _series(_v_shape(-8, lambda k: 0.05 * k * k), start=datetime(2026, 10, 22, 9, 50))
    assert mu_assist.evaluate_pattern(warm, Direction.UP_RED, FLAG)["mu_t2_status"] == mu_assist.STATUS_WARMUP


def test_hy_gap_state_and_entry_status_tables():
    assert mu_assist.hy_gap_state(-90.0, 0.0, 60.0) == mu_assist.HY_EXPAND     # 60 ≥ 2/3·90
    assert mu_assist.hy_gap_state(-90.0, 0.0, 59.0) == mu_assist.HY_FLAT
    assert mu_assist.hy_gap_state(-90.0, 0.0, -1.0) == mu_assist.HY_SHRINK
    assert mu_assist.hy_gap_state(None, 0.0, 1.0) == ""
    st = mu_assist.entry_assist_status
    assert st(mu_assist.PATTERN_CONFIRM, mu_assist.HY_EXPAND) == mu_assist.ENTRY_CONFIRM
    for p in (mu_assist.PATTERN_STALL, mu_assist.PATTERN_DIVERGE):
        for h in (mu_assist.HY_FLAT, mu_assist.HY_SHRINK):
            assert st(p, h) == mu_assist.ENTRY_BAD
    assert st(mu_assist.PATTERN_CONFIRM, mu_assist.HY_FLAT) == mu_assist.ENTRY_WAIT
    assert st(mu_assist.PATTERN_STALL, mu_assist.HY_EXPAND) == mu_assist.ENTRY_WAIT
    assert st(mu_assist.PATTERN_UNAVAILABLE, mu_assist.HY_FLAT) == mu_assist.ENTRY_MU_UNAVAILABLE
    assert st(mu_assist.PATTERN_CONFIRM, "") == mu_assist.ENTRY_MU_UNAVAILABLE


def test_t2_window_bounds():
    f = datetime(2026, 10, 22, 9, 3, tzinfo=KST)
    assert not mu_assist.in_t2_window(f, f + timedelta(minutes=5, seconds=5))
    assert mu_assist.in_t2_window(f, f + timedelta(minutes=5, seconds=10))
    assert mu_assist.in_t2_window(f, f + timedelta(minutes=5, seconds=59))
    assert not mu_assist.in_t2_window(f, f + timedelta(minutes=6))
