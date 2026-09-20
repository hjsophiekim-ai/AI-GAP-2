"""C1 Peak Protection — worker 경로 테스트 (2026-09-19).

고정하는 계약
-------------
A. C1 OFF parity — OFF 면 peak_protection 모듈 함수가 **단 한 번도** 호출되지
   않는다(모든 공용 함수를 "호출되면 실패"로 monkeypatch 해서 증명). 상태도
   쓰이지 않는다.
F. 우선순위    — C1 호출부는 소스상 FORCED_LIQUIDATION / 손절·TP 래더 /
   반대신호 resolve / whipsaw-watch / H50 **전부 뒤**에 있다. 각 경로는 발동 시
   run_once 에서 return 하므로, C1 은 그 무엇도 청산하지 않았을 때만 평가된다.
G. 중복 방지   — 같은 완성봉에서 두 번 평가해도 SELL 은 1회뿐(c1_last_checked_bar_ts).
+ 발동/비발동 기능, 방향대칭, 원장 진단 필드.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.trading.macd2 import config, peak_protection as pp, worker
from app.trading.macd2.models import (
    Direction, PositionSnapshot, RuntimeState, SignalState,
)

KST = config.KST
NOW = datetime(2026, 9, 19, 10, 33, tzinfo=KST)
BAR = datetime(2026, 9, 19, 10, 30, tzinfo=KST)

WORKER_SRC = Path(worker.__file__).read_text(encoding="utf-8")


class _Snap:
    """MacdSnapshot 중 C1 이 읽는 두 필드만 있는 최소 대역."""

    def __init__(self, hist: float, bar_dt: datetime = BAR):
        self.hist = hist
        self.bar_dt = bar_dt


def _pos(symbol=None, avg_price=10_000.0, qty=100):
    return PositionSnapshot(
        symbol=symbol or config.LONG_SYMBOL, quantity=qty, avg_price=avg_price,
    )


def _state(*, enabled=True, peak=6.5, bar_close=10_480.0, **kw) -> RuntimeState:
    s = RuntimeState()
    s.time_window_h50_filter_enabled = True          # N1 계열
    s.time_window_position_active = True
    s.time_window_active_mode = "X2LITE_H50_3SLOT"
    s.c1_peak_protection_enabled = bool(enabled)
    s.c1_peak_net_return = float(peak)
    s.stop_loss_bar_close = bar_close
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class _Result:
    def __init__(self):
        self.actions = []


class _Outcome:
    def __init__(self, executed=True):
        self.final_state = SignalState.EXECUTED if executed else SignalState.FAILED
        self.target_symbol = config.LONG_SYMBOL
        self.sell_result = None


@pytest.fixture
def capture_exit(monkeypatch):
    """실제 주문 대신 호출을 기록한다. order_executor 를 직접 부르지 않고
    worker 의 기존 청산 디스패치(_execute_reversal_exit_only_for_filtered_entry)를
    그대로 지난다는 것도 함께 고정한다."""
    calls = []

    def fake(**kw):
        calls.append(kw)
        return _Outcome()

    monkeypatch.setattr(worker, "_execute_reversal_exit_only_for_filtered_entry", fake)
    monkeypatch.setattr(worker, "_apply_exit_outcome", lambda *a, **k: None)
    return calls


# ── F. 우선순위 (소스 순서로 고정) ────────────────────────────────────────
def test_c1_call_site_is_after_every_existing_exit_path():
    # FORCED_LIQ / 손절 / TP 래더 / ETP 는 priority chain 주석보다도 앞에서 돈다.
    order = [
        "if _advance_held_position_risk_management(",
        "# ── Held position: priority chain",
        "tw_resolve_outcome = _resolve_time_window_candidate(",
        "tw2_3slot_resolve_outcome = _resolve_tw2_3slot_candidate(",
        "whipsaw_watch_outcome = _advance_whipsaw_watch(",
        "h50_outcome = _advance_h50_hold(",
        "c1_outcome = _advance_c1_peak_protection(",
    ]
    positions = []
    for token in order:
        idx = WORKER_SRC.find(token)
        assert idx > 0, f"호출부를 찾지 못함: {token}"
        positions.append(idx)
    assert positions == sorted(positions), (
        "C1 호출부가 기존 청산 경로보다 앞에 있으면 안 된다: " + str(positions))


def test_every_preceding_exit_path_returns_before_c1():
    """C1 호출부 직전 블록들이 전부 '발동했으면 return' 형태인지 확인 —
    그래야 'C1 은 아무 청산도 없을 때만 평가된다' 가 성립한다."""
    head = WORKER_SRC.find("# ── Held position: priority chain")
    tail = WORKER_SRC.find("c1_outcome = _advance_c1_peak_protection(", head)
    block = WORKER_SRC[head:tail]
    for token in ("tw_resolve_outcome", "tw2_3slot_resolve_outcome",
                  "whipsaw_watch_outcome", "h50_outcome"):
        m = re.search(rf"if {token} is not None and {token}\.final_state == SignalState\.EXECUTED:"
                      r"(?:.|\n)*?return result", block)
        assert m, f"{token} 발동 시 early-return 이 없다"
    # 래더(_advance_held_position_risk_management)는 이 블록보다도 앞에서 돌고,
    # 발동하면 run_once 가 그 자리에서 return 한다.
    assert WORKER_SRC.find("if _advance_held_position_risk_management(") < head
    _ladder = WORKER_SRC[WORKER_SRC.find("if _advance_held_position_risk_management("):head]
    assert "return result" in _ladder


def test_c1_outcome_returns_immediately():
    seg = WORKER_SRC[WORKER_SRC.find("c1_outcome = _advance_c1_peak_protection("):]
    assert re.match(
        r"c1_outcome = _advance_c1_peak_protection\((?:.|\n)*?"
        r"if c1_outcome is not None and c1_outcome\.final_state == SignalState\.EXECUTED:\n"
        r"\s+_preserve_confirmed_flag\(TICK_ALREADY_EXECUTED\)\n\s+return result", seg)


# ── A. OFF parity ────────────────────────────────────────────────────────
@pytest.mark.parametrize("why, kw", [
    ("토글 OFF", dict(enabled=False)),
    ("N1 계열 아님", dict(time_window_h50_filter_enabled=False,
                          time_window_3slot_filter_enabled=True)),
])
def test_off_never_touches_module_or_state(monkeypatch, capture_exit, why, kw):
    poisoned = [n for n in ("evaluate", "gap_reversed", "note_peak", "note_armed",
                            "note_checked_bar", "note_triggered", "thresholds")]
    for name in poisoned:
        monkeypatch.setattr(
            pp, name,
            lambda *a, _n=name, **k: pytest.fail(f"C1 OFF 인데 peak_protection.{_n} 호출됨"))
    s = _state(**kw)
    before = (s.c1_armed, s.c1_last_checked_bar_ts, s.c1_triggered_at)
    out = worker._advance_c1_peak_protection(
        broker=None, state=s, now=NOW, macd_snap=_Snap(-30.0),
        position=_pos(), result=_Result(),
    )
    assert out is None, why
    assert capture_exit == []
    assert (s.c1_armed, s.c1_last_checked_bar_ts, s.c1_triggered_at) == before


def test_kill_switch_off_is_a_full_noop(monkeypatch, capture_exit):
    monkeypatch.setattr(config, "C1_ENABLED", False)
    monkeypatch.setattr(pp, "evaluate",
                        lambda *a, **k: pytest.fail("kill switch OFF 인데 평가됨"))
    assert worker._advance_c1_peak_protection(
        broker=None, state=_state(), now=NOW, macd_snap=_Snap(-30.0),
        position=_pos(), result=_Result()) is None
    assert capture_exit == []


# ── 발동 / 비발동 ─────────────────────────────────────────────────────────
def test_fires_when_armed_and_gap_reversed_and_giveback_met(capture_exit):
    s = _state(peak=6.5, bar_close=10_480.0)     # net ≈ +4.6% -> 반납 ≈ 1.9%p
    r = _Result()
    out = worker._advance_c1_peak_protection(
        broker=None, state=s, now=NOW, macd_snap=_Snap(-30.0),
        position=_pos(), result=r)
    assert out is not None and len(capture_exit) == 1
    kw = capture_exit[0]
    assert kw["decision"].block_reason == pp.EXIT_C1_PEAK_PROTECTION
    assert kw["direction"] == Direction.DOWN_BLUE          # 보유 LONG -> 반대
    assert "C1_PEAK_PROTECTION" in kw["signal_id_override"]
    m = kw["decision"].metrics
    assert m["c1_arm_threshold_pct"] == pytest.approx(config.C1_ARM_MFE_PCT)
    assert m["c1_giveback_threshold_pct"] == pytest.approx(config.C1_GIVEBACK_PCT)
    assert m["c1_macd_hist"] == pytest.approx(-30.0)
    assert m["c1_peak_net_return_pct"] == pytest.approx(6.5)
    assert m["c1_giveback_pct"] > config.C1_GIVEBACK_PCT
    assert m["c1_held_direction"] == Direction.UP_RED.value
    assert s.c1_armed is True and s.c1_triggered_at is not None
    assert r.actions and r.actions[0].startswith(pp.EXIT_C1_PEAK_PROTECTION)


@pytest.mark.parametrize("why, peak, bar_close, hist", [
    ("미무장(MFE<5)", 4.9, 10_480.0, -30.0),
    ("gap 미반전", 6.5, 10_480.0, +30.0),
    ("gap 축소일 뿐", 6.5, 10_480.0, +0.5),
    ("반납 부족", 6.5, 10_640.0, -30.0),
])
def test_does_not_fire(capture_exit, why, peak, bar_close, hist):
    s = _state(peak=peak, bar_close=bar_close)
    out = worker._advance_c1_peak_protection(
        broker=None, state=s, now=NOW, macd_snap=_Snap(hist),
        position=_pos(), result=_Result())
    assert out is None, why
    assert capture_exit == [], why


def test_direction_symmetry_through_worker(capture_exit):
    """인버스 보유(DOWN_BLUE)는 gap >= 0 이 반전이다."""
    s = _state(peak=6.5, bar_close=10_480.0)
    pos = _pos(symbol=config.INVERSE_SYMBOL)
    assert worker._advance_c1_peak_protection(
        broker=None, state=s, now=NOW, macd_snap=_Snap(-30.0),
        position=pos, result=_Result()) is None          # 같은 부호면 HOLD
    assert capture_exit == []
    s2 = _state(peak=6.5, bar_close=10_480.0)
    assert worker._advance_c1_peak_protection(
        broker=None, state=s2, now=NOW, macd_snap=_Snap(+30.0),
        position=pos, result=_Result()) is not None      # 반대 부호면 발동
    assert len(capture_exit) == 1
    assert capture_exit[0]["direction"] == Direction.UP_RED


# ── G. 같은 완성봉 중복 방지 ──────────────────────────────────────────────
def test_same_bar_is_never_evaluated_twice(capture_exit):
    s = _state(peak=6.5, bar_close=10_480.0)
    snap = _Snap(-30.0)
    first = worker._advance_c1_peak_protection(
        broker=None, state=s, now=NOW, macd_snap=snap,
        position=_pos(), result=_Result())
    assert first is not None and len(capture_exit) == 1
    # 같은 봉으로 몇 번을 더 돌려도 SELL 은 늘지 않는다
    for _ in range(3):
        assert worker._advance_c1_peak_protection(
            broker=None, state=s, now=NOW, macd_snap=snap,
            position=_pos(), result=_Result()) is None
    assert len(capture_exit) == 1
    # 다음 완성봉이면 다시 평가된다
    assert worker._advance_c1_peak_protection(
        broker=None, state=s, now=NOW + timedelta(minutes=3),
        macd_snap=_Snap(-30.0, BAR + timedelta(minutes=3)),
        position=_pos(), result=_Result()) is not None
    assert len(capture_exit) == 2


def test_armed_is_sticky_but_cleared_when_flat(capture_exit):
    s = _state(peak=6.5, bar_close=10_640.0)   # 반납 부족 -> arm 만 된다
    worker._advance_c1_peak_protection(
        broker=None, state=s, now=NOW, macd_snap=_Snap(-30.0),
        position=_pos(), result=_Result())
    assert s.c1_armed is True and s.c1_armed_at is not None
    assert capture_exit == []
    # 포지션이 사라지면 C1 상태가 정리된다
    worker._advance_c1_peak_protection(
        broker=None, state=s, now=NOW, macd_snap=_Snap(-30.0),
        position=None, result=_Result())
    assert s.c1_armed is False and s.c1_peak_net_return == 0.0


# ── 보유 포지션이 시간대필터 관리 대상이 아니면 관여하지 않는다 ──────────
def test_inactive_tw_position_is_ignored(capture_exit):
    s = _state(peak=6.5, bar_close=10_480.0, time_window_position_active=False)
    assert worker._advance_c1_peak_protection(
        broker=None, state=s, now=NOW, macd_snap=_Snap(-30.0),
        position=_pos(), result=_Result()) is None
    assert capture_exit == []


def test_missing_bar_close_or_snap_is_a_noop(capture_exit):
    s = _state(peak=6.5, bar_close=None)
    assert worker._advance_c1_peak_protection(
        broker=None, state=s, now=NOW, macd_snap=_Snap(-30.0),
        position=_pos(), result=_Result()) is None
    s2 = _state(peak=6.5, bar_close=10_480.0)
    assert worker._advance_c1_peak_protection(
        broker=None, state=s2, now=NOW, macd_snap=None,
        position=_pos(), result=_Result()) is None
    assert capture_exit == []


# ── 원장 컬럼 계약 ────────────────────────────────────────────────────────
def test_ledger_columns_are_additive_and_gated():
    from app.trading.macd2 import ledger
    assert ledger.C1_LEDGER_COLUMNS == [
        "c1_peak_net_return_pct", "c1_current_net_return_pct", "c1_giveback_pct",
        "c1_macd_hist", "c1_arm_threshold_pct", "c1_giveback_threshold_pct",
        "c1_armed_at", "c1_held_direction",
    ]
    # 기존 컬럼은 순서·이름이 그대로고, C1 컬럼은 **맨 뒤에만** 붙는다
    base = ledger.EXECUTION_LEDGER_COLUMNS
    assert base[-len(ledger.C1_LEDGER_COLUMNS):] == ledger.C1_LEDGER_COLUMNS
    assert "early_tp_floor_pct" in base[:-len(ledger.C1_LEDGER_COLUMNS)]
    # 원장 파일이 없으면 조용히 False (예외 없음)
    assert ledger.record_c1_fields("", {}) is False
