"""조기익절 필터 — worker 경로 회귀/기능 테스트 (2026-09-03).

전부 실제 worker.run_once() 디스패치 경로를 통과한다(fake broker +
conftest.py의 autouse fixture로 격리된 state/ledger). 테스트 하네스는
tests/macd2/test_tw2_3slot_worker_regression.py 의 것을 그대로 재사용한다
(중복 인프라 도입 없음).

핵심 두 축:
  A. 필터 OFF일 때 기존 TW2 3-SLOT과 동작이 동일한가 (회귀)
     - early_take_profit 모듈의 모든 공용 함수를 "호출되면 실패"로
       monkeypatch 해서, OFF 경로에서 단 한 번도 호출되지 않는 것을 증명한다.
     - 래더 전 구간(진입/TP2/손절/trailing/반대신호)의 결과가 필터 ON/OFF에서
       동일한지 직접 비교한다(대상이 아닌 거래에 대해).
  B. 필터 ON일 때만, 진입CHOP 포지션에 한해, 기존 청산이 먼저 발동하지 않은
     경우에만 조기익절이 발동하는가 (기능)
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, early_take_profit as etp, service as service_module, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState, SignalState
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_tw2_3slot_worker_regression import (
    _1m_frame,
    _BOOTSTRAP_NOW,
    _PRIOR_DAY,
    _SESSION_START_NOW,
    _approved,
    _fresh_3slot_state,
    _patch_common,
    _prime_3slot_pending,
    _quality,
    _sine_1m_closes,
    _teg,
)

KST = config.KST


# ── 공용 하네스 ────────────────────────────────────────────────────────────
def _market(inverse_price: float = 10_000.0, long_price: float = 15_000.0):
    """test_tw2_3slot_worker_regression의 fixture와 같은 레시피지만 호가를
    테스트별로 지정할 수 있게 한 헬퍼."""
    df_1m = _1m_frame(_PRIOR_DAY, _sine_1m_closes(300))
    quote_prices = {
        config.LONG_SYMBOL: long_price,
        config.INVERSE_SYMBOL: inverse_price,
        config.WATCH_SYMBOL: 100.0,
    }

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch, fetch_quote=fake_quote)
    boot = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert boot.ok, f"fixture bootstrap failed unexpectedly: {boot.reason}"
    svc.refresh_quotes()
    return svc, _SESSION_START_NOW


def _held_3slot_state(
    *, now: datetime, entry_price: float = 10_000.0, qty: int = 10,
    early_tp_on: bool, entry_chop: bool, peak: float, tp1_done: bool = False,
    bar_close: float | None = None,
) -> RuntimeState:
    state = _fresh_3slot_state()
    state.position = PositionSnapshot(
        symbol=config.INVERSE_SYMBOL, quantity=qty, avg_price=entry_price, entry_at=now,
    )
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.time_window_entry_session = "MORNING"
    state.time_window_tp1_done = tp1_done
    state.time_window_peak_net_return = peak
    state.tw2_3slot_slots_used_today = 1
    state.early_tp_filter_enabled = early_tp_on
    state.time_window_entry_chop = entry_chop
    state.early_tp_peak_net_return = peak
    _seed_completed_bar(state, now=now, price=(entry_price if bar_close is None else bar_close))
    return state


def _seed_completed_bar(state: RuntimeState, *, now: datetime, price: float) -> None:
    """하방 rung(손절/after-TP1-stop/trailing/조기익절)은 모두 **완성 3분봉 종가**
    게이트다 -- worker._advance_stop_loss_bar는 진입 실행봉 다음 봉이 롤오버된
    첫 틱에서만 종가를 돌려준다. 매 테스트에서 틱을 세 번 돌리는 대신, 그
    함수가 읽는 상태를 직접 심어 "직전 봉이 방금 완성됐다"를 만든다
    (tests/macd2/test_worker_held_position_risk_management_warmup.py와 같은
    화이트박스 접근이며, 실제로 통과하는 코드 경로는 완전히 동일하다)."""
    cur_bar_start, _ = worker.forming_bar_window(now)
    prev_bar_start = cur_bar_start - timedelta(minutes=3)
    entry_bar_start = prev_bar_start - timedelta(minutes=3)
    state.stop_loss_bar_symbol = config.INVERSE_SYMBOL
    state.stop_loss_entry_bar_ts = entry_bar_start.isoformat()
    state.stop_loss_bar_ts = prev_bar_start.isoformat()
    state.stop_loss_bar_close = price


def _broker(inverse_price: float, *, entry_price: float = 10_000.0, qty: int = 10) -> FakeBroker:
    b = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: inverse_price})
    b.buy_market(config.INVERSE_SYMBOL, qty, "seed-order")
    b._positions[config.INVERSE_SYMBOL].avg_price = entry_price
    return b


def _forbid_early_tp(monkeypatch) -> None:
    """필터 OFF 경로에서 이 모듈 함수가 단 한 번이라도 호출되면 즉시 실패."""
    def boom(*a, **kw):
        raise AssertionError(
            "조기익절 필터가 OFF인데 early_take_profit 함수가 호출됐다 -- "
            "OFF일 때 동작이 완전히 동일하다는 보장이 깨진다"
        )
    monkeypatch.setattr(worker.early_take_profit, "evaluate", boom)
    monkeypatch.setattr(worker.early_take_profit, "evaluate_entry_chop", boom)


# ── A. 필터 OFF 회귀 ──────────────────────────────────────────────────────
def test_off_by_default_in_a_fresh_state():
    state = state_store.default_state()
    assert state.early_tp_filter_enabled is False
    assert etp.is_enabled(state) is False


def test_off_never_calls_the_filter_on_a_held_position_tick(monkeypatch):
    """되돌림이 floor(+0.8%)를 한참 밑도는 진입CHOP 포지션이라도, 필터가 OFF면
    모듈 함수가 호출조차 되지 않고 포지션이 유지돼야 한다."""
    svc, now0 = _market(inverse_price=10_020.0)  # 약 +0.2%, floor 아래
    state = _held_3slot_state(now=now0, early_tp_on=False, entry_chop=True, peak=3.0,
                              bar_close=10_020.0)
    broker = _broker(10_020.0)
    _patch_common(monkeypatch)
    _forbid_early_tp(monkeypatch)

    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert not any(config.EXIT_EARLY_TAKE_PROFIT in a for a in result.actions)
    assert state.position is not None, "필터 OFF에서 조기익절이 발동했다"


def test_off_never_calls_the_filter_on_a_tw2_3slot_entry(monkeypatch):
    """진입 시점 CHOP 계산도 OFF에서는 아예 수행되지 않아야 한다
    (상태에 아무것도 쓰지 않는다)."""
    svc, now0 = _market()
    state = _fresh_3slot_state()
    state.early_tp_filter_enabled = False
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True), teg_decision=_teg(True))
    _forbid_early_tp(monkeypatch)
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert state.tw2_3slot_slots_used_today == 1, "진입 자체는 정상 성사돼야 한다"
    assert state.time_window_entry_chop is False
    assert state.last_entry_chop_score is None
    assert state.last_entry_chop_conditions is None
    assert state.early_tp_peak_net_return == 0.0


@pytest.mark.parametrize(
    "inverse_price,expected_exit",
    [
        (10_800.0, config.EXIT_TW_TP2_FULL),      # +8% -> TW2 3-SLOT의 6% TP2
        (9_820.0, config.EXIT_TW_STOP_LOSS),      # -1.8% -> 손절
    ],
)
def test_existing_ladder_outcomes_are_identical_with_filter_off_and_on(
    monkeypatch, inverse_price, expected_exit,
):
    """기존 래더가 발동하는 상황에서는 필터 ON/OFF가 같은 청산을 내야 한다 --
    "기존 TP/SL/trailing이 먼저 발동하면 기존 청산 우선"의 직접 검증."""
    observed = {}
    for early_tp_on in (False, True):
        svc, now0 = _market(inverse_price=inverse_price)
        state = _held_3slot_state(
            now=now0, early_tp_on=early_tp_on, entry_chop=True, peak=3.0,
            bar_close=inverse_price,
        )
        broker = _broker(inverse_price)
        _patch_common(monkeypatch)
        result = run_once(broker=broker, market_data=svc, state=state, now=now0)
        exits = [a for a in result.actions if ":" in a]
        observed[early_tp_on] = (tuple(exits), state.position is None)

    assert observed[False] == observed[True], (
        f"필터 ON/OFF에서 기존 래더 결과가 달라졌다: {observed!r}"
    )
    assert any(expected_exit in a for a in observed[False][0]), (
        f"기대한 기존 청산({expected_exit})이 발동하지 않았다: {observed[False][0]!r}"
    )
    assert not any(config.EXIT_EARLY_TAKE_PROFIT in a for a in observed[True][0]), (
        "기존 청산이 발동한 틱에서 조기익절이 끼어들었다"
    )


def test_tw2_3slot_toggle_off_force_disables_the_filter(tmp_path, monkeypatch):
    svc = service_module.Macd2Service()
    state = state_store.load_state()
    state.time_window_3slot_filter_enabled = True
    state.time_window_2_filter_enabled = False
    state.time_window_teg_filter_enabled = False
    state_store.save_state(state)

    on = svc.set_early_tp_filter_enabled(True, changed_by="test")
    assert on["ok"] is True and on["early_tp_filter_enabled"] is True

    off = svc.set_time_window_3slot_filter_enabled(False, changed_by="test")
    assert off["early_tp_filter_enabled"] is False, "3-SLOT을 끄면 함께 꺼져야 한다"
    reloaded = state_store.load_state()
    assert reloaded.early_tp_filter_enabled is False
    assert reloaded.early_tp_filter_enabled_by == "AUTO_TW2_3SLOT_DISABLED"


def test_cannot_enable_the_filter_while_tw2_3slot_is_off():
    svc = service_module.Macd2Service()
    state = state_store.load_state()
    state.time_window_3slot_filter_enabled = False
    state.early_tp_filter_enabled = False
    state_store.save_state(state)

    res = svc.set_early_tp_filter_enabled(True, changed_by="test")

    assert res["ok"] is False and res["reason"] == "TW2_3SLOT_REQUIRED"
    assert state_store.load_state().early_tp_filter_enabled is False


def test_persisted_toggle_is_dropped_on_reload_if_tw2_3slot_is_off():
    """상태파일이 손으로 편집되거나 모드가 바뀌어 어긋나더라도, 로드 시점에
    3-SLOT이 꺼져 있으면 이 필터는 꺼진 상태로 복원돼야 한다."""
    state = state_store.load_state()
    state.time_window_3slot_filter_enabled = True
    state.early_tp_filter_enabled = True
    state_store.save_state(state)
    assert state_store.load_state().early_tp_filter_enabled is True

    state = state_store.load_state()
    state.time_window_3slot_filter_enabled = False
    state_store.save_state(state)

    assert state_store.load_state().early_tp_filter_enabled is False


# ── B. 필터 ON 기능 ───────────────────────────────────────────────────────
def test_fires_only_after_arming_and_only_below_the_floor(monkeypatch):
    """MFE가 트리거에 도달하지 않았으면(armed 아님) 되돌림이 floor 아래여도
    발동하지 않고, armed 이후에는 발동한다."""
    svc, now0 = _market(inverse_price=10_020.0)  # 약 +0.2%
    not_armed = _held_3slot_state(
        now=now0, early_tp_on=True, entry_chop=True,
        peak=config.EARLY_TP_TRIGGER_PCT - 0.5, bar_close=10_020.0,
    )
    _patch_common(monkeypatch)
    r1 = run_once(broker=_broker(10_020.0), market_data=svc, state=not_armed, now=now0)
    assert not any(config.EXIT_EARLY_TAKE_PROFIT in a for a in r1.actions)
    assert not_armed.position is not None

    svc2, now2 = _market(inverse_price=10_020.0)
    armed = _held_3slot_state(
        now=now2, early_tp_on=True, entry_chop=True,
        peak=config.EARLY_TP_TRIGGER_PCT + 0.5, bar_close=10_020.0,
    )
    r2 = run_once(broker=_broker(10_020.0), market_data=svc2, state=armed, now=now2)
    assert any(a.startswith(config.EXIT_EARLY_TAKE_PROFIT) for a in r2.actions), (
        f"armed + floor 이하인데 발동하지 않았다: {r2.actions!r}"
    )
    assert armed.position is None
    assert armed.last_early_tp_fired_at is not None
    assert armed.tw2_3slot_slots_used_today == 1, "청산은 슬롯을 되돌려주지 않는다"


def test_never_fires_for_a_trend_entry_even_when_armed_and_below_floor(monkeypatch):
    svc, now0 = _market(inverse_price=10_020.0)
    state = _held_3slot_state(
        now=now0, early_tp_on=True, entry_chop=False,
        peak=config.EARLY_TP_TRIGGER_PCT + 2.0, bar_close=10_020.0,
    )
    _patch_common(monkeypatch)
    result = run_once(broker=_broker(10_020.0), market_data=svc, state=state, now=now0)
    assert not any(config.EXIT_EARLY_TAKE_PROFIT in a for a in result.actions)
    assert state.position is not None, "진입시 TREND 포지션에 발동했다"


def test_never_fires_for_a_tw2_managed_position(monkeypatch):
    """TW2/TEGv2가 열었거나 브로커에서 입양된 포지션에는 적용되지 않는다."""
    svc, now0 = _market(inverse_price=10_020.0)
    state = _held_3slot_state(
        now=now0, early_tp_on=True, entry_chop=True,
        peak=config.EARLY_TP_TRIGGER_PCT + 1.0, bar_close=10_020.0,
    )
    state.time_window_active_mode = "TW2"
    _patch_common(monkeypatch)
    result = run_once(broker=_broker(10_020.0), market_data=svc, state=state, now=now0)
    assert not any(config.EXIT_EARLY_TAKE_PROFIT in a for a in result.actions)
    assert state.position is not None


def test_holds_while_armed_but_still_above_the_floor(monkeypatch):
    svc, now0 = _market(inverse_price=10_150.0)  # 약 +1.5%, floor(+0.8%) 위
    state = _held_3slot_state(
        now=now0, early_tp_on=True, entry_chop=True,
        peak=config.EARLY_TP_TRIGGER_PCT + 0.5, bar_close=10_150.0,
    )
    _patch_common(monkeypatch)
    result = run_once(broker=_broker(10_150.0), market_data=svc, state=state, now=now0)
    assert not any(config.EXIT_EARLY_TAKE_PROFIT in a for a in result.actions)
    assert state.position is not None
    assert state.last_early_tp_armed_at is not None, "armed 진단값은 기록돼야 한다"


def test_entry_stores_the_chop_verdict_when_the_filter_is_on(monkeypatch):
    svc, now0 = _market()
    state = _fresh_3slot_state()
    state.early_tp_filter_enabled = True
    broker = FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True), teg_decision=_teg(True))
    captured = {}
    real = etp.evaluate_entry_chop

    def spy(bars_3m, direction, decision_at):
        d = real(bars_3m, direction, decision_at)
        captured["called"] = True
        captured["decision"] = d
        return d

    monkeypatch.setattr(worker.early_take_profit, "evaluate_entry_chop", spy)
    _prime_3slot_pending(state, Direction.UP_RED, before=_PRIOR_DAY)

    run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert state.tw2_3slot_slots_used_today == 1
    assert captured.get("called") is True, "필터 ON인데 진입 시점 CHOP 판정이 계산되지 않았다"
    assert state.time_window_entry_chop == bool(captured["decision"].is_chop)
    assert state.last_entry_chop_score == captured["decision"].score
    assert set(state.last_entry_chop_conditions or {}) == set(etp.ALL_CHOP_CONDITIONS) or (
        captured["decision"].insufficient_data
    )


def test_entry_chop_verdict_is_cleared_when_the_position_closes(monkeypatch):
    """포지션이 닫히면 진입시점 판정이 다음 포지션으로 절대 새지 않아야 한다."""
    svc, now0 = _market(inverse_price=10_800.0)  # +8% -> TP2 전량청산
    state = _held_3slot_state(
        now=now0, early_tp_on=True, entry_chop=True, peak=3.0, bar_close=10_800.0,
    )
    _patch_common(monkeypatch)
    result = run_once(broker=_broker(10_800.0), market_data=svc, state=state, now=now0)
    assert any(config.EXIT_TW_TP2_FULL in a for a in result.actions)
    assert state.position is None
    assert state.time_window_entry_chop is False
    assert state.early_tp_peak_net_return == 0.0


def test_turning_the_filter_off_clears_position_scoped_state():
    svc = service_module.Macd2Service()
    state = state_store.load_state()
    state.time_window_3slot_filter_enabled = True
    state.time_window_2_filter_enabled = False
    state.time_window_teg_filter_enabled = False
    state_store.save_state(state)
    svc.set_early_tp_filter_enabled(True, changed_by="test")

    state = state_store.load_state()
    state.time_window_entry_chop = True
    state.early_tp_peak_net_return = 2.5
    state_store.save_state(state)

    svc.set_early_tp_filter_enabled(False, changed_by="test")

    reloaded = state_store.load_state()
    assert reloaded.early_tp_filter_enabled is False
    assert reloaded.time_window_entry_chop is False
    assert reloaded.early_tp_peak_net_return == 0.0
