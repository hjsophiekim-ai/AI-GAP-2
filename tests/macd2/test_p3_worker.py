"""P3 regime stack — worker 경로 회귀/기능 테스트 (2026-09-27).

전부 실제 ``worker.run_once()`` 를 통과한다(fake broker + conftest 의 autouse
격리). 하네스는 ``tests/macd2/test_early_take_profit_worker.py`` 가 쓰는 것을
그대로 재사용한다 -- 새 인프라를 만들지 않는다.

잠그는 축:

  A. **OFF parity**  — N1 모드(= P3 OFF)에서 P3/섀도우 모듈 함수가 단 한 번도
     호출되지 않는다. "호출되면 실패" monkeypatch 로 증명한다.
  B. **TREND parity** — P3 모드라도 진입 regime 이 TREND 면 B3 가 개입하지
     않고 기존 래더 결과가 N1 모드와 **동일**하다.
  F. **feedback isolation** — 실거래가 B3 로 끝나도 섀도우 포지션은 BASE
     규칙대로 계속 살아 있다.
  J. **중복주문 방지** — 같은 조건으로 tick 을 두 번 돌려도 P3 부분매도는
     한 번만 나간다.
  K. **재시작 복원** — rescue 이후 재시작해도 잔량이 runner 모드로 돌아온다.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.trading.macd2 import (
    chop_regime,
    config,
    p3_stack,
    shadow_base,
    state_store,
    strategy_mode,
    worker,
)
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState, SignalState
from app.trading.macd2.worker import run_once
from tests.macd2.test_early_take_profit_worker import (
    _broker,
    _held_3slot_state,
    _market,
    _seed_completed_bar,
)
from tests.macd2.test_tw2_3slot_worker_regression import _patch_common

KST = config.KST


# ── 공용 ──────────────────────────────────────────────────────────────────
def _p3_state(*, now, mode: str, entry_regime: str | None,
              entry_price: float = 10_000.0, qty: int = 10,
              bar_close: float | None = None,
              entry_minutes_ago: float = 4.0) -> RuntimeState:
    """N1 계열 보유 포지션 + 전략모드 + 진입 regime 스냅샷."""
    state = _held_3slot_state(now=now, entry_price=entry_price, qty=qty,
                              early_tp_on=False, entry_chop=False, peak=0.0,
                              bar_close=bar_close)
    strategy_mode.apply(state, mode)
    state.mode = "mock"
    state.time_window_active_mode = "N1_3SLOT"
    state.position = PositionSnapshot(
        symbol=config.INVERSE_SYMBOL, quantity=qty, avg_price=entry_price,
        entry_at=now - timedelta(minutes=entry_minutes_ago),
    )
    state.last_time_window_entry_at = (now - timedelta(minutes=entry_minutes_ago)).isoformat()
    if entry_regime is not None:
        p3_stack.note_entry_regime(state, entry_regime)
    return state


def _forbid_p3(monkeypatch) -> None:
    """P3 OFF 경로에서 이 모듈 함수가 한 번이라도 불리면 즉시 실패."""
    def boom(*a, **kw):
        raise AssertionError(
            "P3 가 OFF(N1 모드)인데 P3 스택이 호출됐다 -- OFF parity 가 깨진다")

    monkeypatch.setattr(worker.p3_stack, "evaluate", boom)
    monkeypatch.setattr(worker.shadow_base, "advance_exits", boom)
    monkeypatch.setattr(worker.shadow_base, "on_confirmed_flag", boom)
    monkeypatch.setattr(worker.chop_regime, "current_regime", boom)


def _isolate_shadow_ledger(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(chop_regime, "LEDGER_DIR_PATH", tmp_path)
    monkeypatch.setattr(chop_regime, "LEDGER_PATH", tmp_path / "shadow.json")


def _ledger_before(now, *, h50: int, tp1: int, n: int = 10):
    """``now`` 보다 **먼저 끝난** 완료 shadow 거래 n 건.

    detector 는 판정시각(마지막 완성봉의 완성시각)보다 앞서 끝난 거래만 센다 --
    테스트 하네스의 기준일에 맞춰 만들어야 seed 가 실제로 읽힌다.
    """
    rows = []
    for i in range(n):
        exit_at = now - timedelta(hours=2, minutes=5 * (n - i))
        entry_at = exit_at - timedelta(minutes=30)
        rows.append(chop_regime.ShadowTrade(
            shadow_trade_id=chop_regime.make_shadow_trade_id(entry_at, "UP_RED", 1),
            trading_date=entry_at.astimezone(KST).strftime("%Y%m%d"),
            entry_time=entry_at.isoformat(), exit_time=exit_at.isoformat(),
            direction="UP_RED", slot=1, entry_price=10_000.0, exit_price=10_100.0,
            net_pct=1.0, h50_intervened=(i < h50), tp1_hit=(i < tp1),
        ))
    return rows


# ── A. OFF parity ────────────────────────────────────────────────────────
def test_n1_mode_never_calls_the_p3_stack(monkeypatch, tmp_path):
    """+1.2% 수익이라 B3 TP 조건을 충분히 넘긴 포지션이라도, N1 모드면
    P3 모듈이 호출조차 되지 않고 기존 래더가 그대로 관리해야 한다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_120.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_N1,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=10_120.0)
    broker = _broker(10_120.0)
    _patch_common(monkeypatch)
    _forbid_p3(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.position is not None, "N1 모드에서 B3 가 포지션을 끊으면 안 된다"
    assert state.p3_enabled is False


def test_n1_mode_writes_no_p3_state_and_no_shadow_ledger(monkeypatch, tmp_path):
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_050.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_N1, entry_regime=None,
                      bar_close=10_050.0)
    broker = _broker(10_050.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.p3_last_regime is None
    assert state.p3_shadow is None
    assert not (tmp_path / "shadow.json").exists()


# ── B. TREND parity ──────────────────────────────────────────────────────
def test_trend_entry_in_p3_mode_behaves_exactly_like_n1_mode(monkeypatch, tmp_path):
    """같은 가격/같은 봉에서 TREND 진입 포지션은 두 모드의 결과가 같아야 한다."""
    outcomes = {}
    for mode in (strategy_mode.MODE_N1, strategy_mode.MODE_P3):
        _isolate_shadow_ledger(monkeypatch, tmp_path / mode)
        svc, now0 = _market(inverse_price=10_120.0)
        state = _p3_state(now=now0, mode=mode,
                          entry_regime=chop_regime.REGIME_TREND, bar_close=10_120.0)
        broker = _broker(10_120.0)
        _patch_common(monkeypatch)
        run_once(broker=broker, state=state, market_data=svc, now=now0)
        outcomes[mode] = (
            state.position.quantity if state.position else None,
            state.time_window_position_active,
            state.time_window_tp1_done,
        )
    assert outcomes[strategy_mode.MODE_N1] == outcomes[strategy_mode.MODE_P3]


def test_p3_mode_with_trend_entry_does_not_cut_at_one_percent(monkeypatch, tmp_path):
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_120.0)     # 약 +1.2%
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_TREND, bar_close=10_120.0)
    broker = _broker(10_120.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.position is not None
    assert state.p3_tp_rescued is False


# ── B3 / P3 기능 ─────────────────────────────────────────────────────────
def test_chop_entry_reaching_one_percent_fast_takes_half_and_promotes(monkeypatch, tmp_path):
    """+1% 를 4분 만에 찍으면 50% 익절 + 잔량 승격(P3 rescue)."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_120.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=10_120.0,
                      entry_minutes_ago=4.0, qty=10)
    broker = _broker(10_120.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.p3_tp_rescued is True, "6분 이내 +1% 면 rescue 가 나야 한다"
    assert state.p3_promoted is True
    assert state.position is not None and state.position.quantity == 5
    assert p3_stack.position_mode(state) == p3_stack.MODE_P3_RUNNER
    assert p3_stack.governs_position(state) is False, "승격 후에는 기존 래더가 주인이다"


def test_chop_entry_reaching_one_percent_slowly_exits_in_full(monkeypatch, tmp_path):
    """+1% 를 9분 만에 찍으면 rescue 없이 전량 B3 TP."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_120.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=10_120.0,
                      entry_minutes_ago=9.0, qty=10)
    broker = _broker(10_120.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.p3_tp_rescued is False
    assert state.position is None, "B3 TP 는 전량청산이다"


def test_chop_entry_hitting_minus_one_percent_exits_in_full(monkeypatch, tmp_path):
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=9_880.0)      # 약 -1.2%
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=9_880.0,
                      entry_minutes_ago=5.0)
    broker = _broker(9_880.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.position is None


# ── J. 중복주문 방지 ─────────────────────────────────────────────────────
def test_second_tick_does_not_sell_again_after_a_rescue(monkeypatch, tmp_path):
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_120.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=10_120.0,
                      entry_minutes_ago=4.0, qty=10)
    broker = _broker(10_120.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)
    qty_after_first = state.position.quantity
    sells_after_first = len([o for o in broker.orders if o.side == "SELL"])
    assert sells_after_first == 1, "첫 tick 에서 부분매도가 정확히 한 번 나가야 한다"

    # 같은 조건으로 한 번 더 -- rescue 는 다시 나면 안 된다.
    _seed_completed_bar(state, now=now0 + timedelta(seconds=5), price=10_120.0)
    run_once(broker=broker, state=state, market_data=svc, now=now0 + timedelta(seconds=5))

    assert state.position is not None
    assert state.position.quantity == qty_after_first, "부분매도가 두 번 나갔다"
    assert len([o for o in broker.orders if o.side == "SELL"]) == sells_after_first


# ── K. 재시작 복원 ───────────────────────────────────────────────────────
def test_runner_mode_survives_a_restart(monkeypatch, tmp_path):
    """rescue 직후 저장 -> 재로딩해도 잔량이 P3-RUNNER 로 복원돼야 한다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "runtime.json")

    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)
    state.mode = "mock"
    p3_stack.note_entry_regime(state, chop_regime.REGIME_CHOP)
    p3_stack.note_first_tp(state, worker.KST and __import__("datetime").datetime(
        2026, 9, 22, 12, 19, tzinfo=KST))
    p3_stack.note_rescued(state, __import__("datetime").datetime(
        2026, 9, 22, 12, 19, tzinfo=KST))
    state_store.save_state(state)

    restored = state_store.load_state()

    assert strategy_mode.current(restored) == strategy_mode.MODE_P3
    assert restored.p3_enabled is True
    assert restored.p3_tp_rescued is True
    assert restored.p3_promoted is True
    assert restored.p3_entry_regime == chop_regime.REGIME_CHOP
    assert p3_stack.position_mode(restored) == p3_stack.MODE_P3_RUNNER


def test_p3_mode_is_not_silently_downgraded_to_n1_on_restart(monkeypatch, tmp_path):
    """저장된 모드가 P3 면 재시작만으로 N1 이 되면 안 된다(사용자 요구 §9)."""
    monkeypatch.setattr(state_store, "STATE_DIR_PATH", tmp_path)
    monkeypatch.setattr(state_store, "STATE_PATH", tmp_path / "runtime.json")
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)
    state.mode = "mock"
    state_store.save_state(state)

    restored = state_store.load_state()
    assert strategy_mode.current(restored) == strategy_mode.MODE_P3
    assert restored.time_window_n1_filter_enabled is True
    assert restored.c1_peak_protection_enabled is True
    assert restored.smart_sizing_enabled is True
    assert restored.p3_enabled is True


# ── F. feedback isolation ────────────────────────────────────────────────
def test_shadow_position_survives_a_real_b3_exit(monkeypatch, tmp_path):
    """실거래가 B3 로 끝나도 섀도우 포지션은 BASE 규칙대로 계속 살아 있다.

    이것이 detector 의 피드백 루프를 막는 유일한 근거다 -- 섀도우가 실거래를
    따라 끝나 버리면 B3 의 결과가 다시 detector 입력이 된다.
    """
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_120.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=chop_regime.REGIME_CHOP, bar_close=10_120.0,
                      entry_minutes_ago=9.0)
    # 섀도우가 같은 방향 포지션을 들고 있는 상태를 심는다.
    book = shadow_base.ShadowBook(
        trading_date=now0.astimezone(KST).strftime("%Y%m%d"),
        slots_used_today=1, morning_count=1,
        position=shadow_base.ShadowPosition(
            symbol=config.INVERSE_SYMBOL, direction=Direction.DOWN_BLUE.value,
            entry_at=(now0 - timedelta(minutes=9)).isoformat(),
            entry_price=10_000.0, session="MORNING", slot=1,
            trading_date=now0.astimezone(KST).strftime("%Y%m%d"),
        ),
    )
    shadow_base.store_book(state, book)
    broker = _broker(10_120.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.position is None, "실거래는 B3 TP 로 끝나야 한다"
    after = shadow_base.load_book(state)
    assert after.position is not None, (
        "섀도우 포지션이 실거래를 따라 끝났다 -- 피드백 격리가 깨진다")
    assert after.position.symbol == config.INVERSE_SYMBOL


def test_shadow_exit_is_recorded_with_h50_and_tp1_flags(monkeypatch, tmp_path):
    """섀도우가 닫히면 detector 가 보는 두 불리언이 ledger 에 남아야 한다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3)
    book = shadow_base.ShadowBook(
        trading_date="20260922",
        position=shadow_base.ShadowPosition(
            symbol=config.INVERSE_SYMBOL, direction=Direction.DOWN_BLUE.value,
            entry_at="2026-09-22T09:06:00+09:00", entry_price=10_000.0,
            session="MORNING", slot=1, trading_date="20260922",
            h50_intervened=True, tp1_hit=True,
        ),
    )
    exit_at = __import__("datetime").datetime(2026, 9, 22, 10, 33, tzinfo=KST)
    shadow_base._close(book, book.position, exit_at=exit_at,
                       exit_price=10_100.0, exit_reason="OPPOSITE_SIGNAL")

    rows = chop_regime.load_ledger()
    assert len(rows) == 1
    assert rows[0].h50_intervened is True
    assert rows[0].tp1_hit is True
    assert rows[0].shadow_only is True
    assert rows[0].exit_reason == "OPPOSITE_SIGNAL"


# ── fail-safe = BASE ─────────────────────────────────────────────────────
def test_detector_failure_does_not_block_trading(monkeypatch, tmp_path):
    """섀도우/detector 가 터져도 tick 은 정상 완주해야 한다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_050.0)
    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3,
                      entry_regime=None, bar_close=10_050.0)
    broker = _broker(10_050.0)
    _patch_common(monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("shadow ledger is on fire")

    monkeypatch.setattr(worker.chop_regime, "current_regime", boom)
    monkeypatch.setattr(worker.shadow_base, "advance_exits", boom)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.position is not None, "detector 실패가 포지션 관리를 막으면 안 된다"


# ── seed -> regime -> 진입 스냅샷 (day-1 사용 경로) ──────────────────────
def test_seeded_ledger_is_read_as_chop_on_a_live_tick(monkeypatch, tmp_path):
    """seed 된 shadow ledger 가 실제 tick 에서 CHOP 으로 읽혀야 한다.

    내일 장 시작부터 P3 가 WARMUP 없이 동작하는 근거가 이 경로다.
    """
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_050.0)
    # 연구 seed 와 같은 모양: H50 5/10, TP1 0/10 -> CHOP
    chop_regime.save_ledger(_ledger_before(now0, h50=5, tp1=0), source="seed-test")

    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3, entry_regime=None,
                      bar_close=10_050.0)
    broker = _broker(10_050.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.p3_last_regime == chop_regime.REGIME_CHOP
    assert state.p3_last_h50_rate == pytest.approx(0.5)
    assert state.p3_last_tp1_rate == pytest.approx(0.0)
    assert state.p3_last_shadow_sample == 10
    assert strategy_mode.execution_layer(state) == "P3"
    assert strategy_mode.shadow_status(state) == "READY"


def test_warmup_ledger_keeps_execution_on_base(monkeypatch, tmp_path):
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=10_050.0)
    chop_regime.save_ledger(_ledger_before(now0, h50=5, tp1=0, n=9),
                            source="seed-test")   # 9건 < 10

    state = _p3_state(now=now0, mode=strategy_mode.MODE_P3, entry_regime=None,
                      bar_close=10_050.0)
    broker = _broker(10_050.0)
    _patch_common(monkeypatch)

    run_once(broker=broker, state=state, market_data=svc, now=now0)

    assert state.p3_last_regime == chop_regime.REGIME_WARMUP
    assert strategy_mode.execution_layer(state) == "BASE"
    assert strategy_mode.shadow_status(state).startswith("WARMUP")
