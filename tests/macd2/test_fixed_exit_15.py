"""고정 ±1.5% 청산 토글 (2026-10-15) — 계약 고정.

  A. 기본 OFF, 레거시 state 도 OFF. OFF 면 fixed_exit 판정 함수가 한 번도 불리지 않는다.
  B. ON 이면 N1 / P3 / E / CHOP 네 모드 모두
       · 틱 순수익률 >= +1.5% -> 즉시 전량 (FIXED_TP15)
       · 완성 3분봉 종가 순수익률 <= -1.5% -> 전량 (FIXED_SL15), 진입봉/형성중 봉은 제외
       · TP1 부분익절 / B3 / P3 rescue / trailing / after-TP1 stop / C1 / max-hold 없음
       · 15:00 강제청산은 그대로
  C. 반대 플래그 처리(T+3 resolve / whipsaw-watch / H50)는 ON 에서도 그대로 호출된다.
  D. 재시작·모드전환·날짜변경 후에도 토글 유지, 서비스 setter 저장, UI 토글.
  E. 사이징은 전략 그대로: 그날 첫 거래가 FIXED_SL15 로 끝나도 다음 진입 x1.20(POST_STOP).

실거래 데이터 위 주문 단위 parity(OFF) / 연구 A안 교차검증(ON) 은 23영업일 재생으로 따로
확인했다 — docs/macd2_fixed_exit_15.md.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import (
    chop_regime,
    config,
    fixed_exit,
    ledger,
    p3_stack,
    state_store,
    strategy_mode,
    worker,
)
from app.trading.macd2 import service as service_module
from app.trading.macd2.models import PositionSnapshot, RuntimeState
from app.trading.macd2.worker import run_once
from tests.macd2.test_early_take_profit_worker import _broker, _held_3slot_state, _market
from tests.macd2.test_tw2_3slot_worker_regression import _patch_common

KST = config.KST
ALL = strategy_mode.ALL_MODES
ENTRY = 10_000.0


# ── 공용 ──────────────────────────────────────────────────────────────────
def _isolate_shadow_ledger(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(chop_regime, "LEDGER_DIR_PATH", tmp_path)
    monkeypatch.setattr(chop_regime, "LEDGER_PATH", tmp_path / "shadow.json")


def _state(*, now, mode, on, price=ENTRY, bar_close=None, qty=10, entry_minutes_ago=9.0,
           regime=chop_regime.REGIME_CHOP, tp1_done=False, peak=0.0) -> RuntimeState:
    """모드별 보유 포지션. P3 계열은 CHOP 진입으로 각인해 B3/rescue 가 **켜질 수 있는** 상태로 만든다
    -- 그래야 ON 이 그것들을 실제로 끄는지 보인다."""
    s = _held_3slot_state(now=now, entry_price=ENTRY, qty=qty, early_tp_on=False, entry_chop=False,
                          peak=peak, tp1_done=tp1_done, bar_close=bar_close)
    strategy_mode.apply(s, mode)
    s.mode = "mock"
    s.time_window_active_mode = "N1_3SLOT"
    entry_at = now - timedelta(minutes=entry_minutes_ago)
    s.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=qty, avg_price=ENTRY, entry_at=entry_at)
    s.last_time_window_entry_at = entry_at.isoformat()
    if strategy_mode.is_p3_based(mode):
        p3_stack.note_entry_regime(s, regime)
    s.fixed_exit_15_enabled = on
    return s


def _run(monkeypatch, tmp_path, *, mode, on, price, bar_close=None, now=None, **kw):
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=price)
    now = now or now0
    state = _state(now=now, mode=mode, on=on, price=price, bar_close=bar_close, **kw)
    broker = _broker(price)
    _patch_common(monkeypatch)
    n_orders = len(broker.orders)
    result = run_once(broker=broker, state=state, market_data=svc, now=now)
    return state, broker.orders[n_orders:], result


def _sells(orders):
    return [o for o in orders if o.side == "SELL"]


def _last_exit_reason():
    rows = [r for r in ledger.load_execution_ledger(limit=50) if r.get("side") == "SELL"]
    return rows[-1].get("exit_reason") if rows else None


def _forbid_fixed(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("토글 OFF 인데 fixed_exit 판정이 호출됐다 -- OFF parity 가 깨진다")
    monkeypatch.setattr(worker.fixed_exit, "evaluate_tick", boom)
    monkeypatch.setattr(worker.fixed_exit, "evaluate_bar", boom)
    monkeypatch.setattr(worker, "_advance_fixed_exit", boom)


# ── A. 기본값 / OFF ───────────────────────────────────────────────────────
def test_default_is_off_everywhere():
    s = state_store.default_state()
    assert s.fixed_exit_15_enabled is False
    assert RuntimeState().fixed_exit_15_enabled is False
    for m in ALL:
        strategy_mode.apply(s, m)
        assert fixed_exit.is_active(s) is False


def test_legacy_state_file_without_the_key_is_off():
    raw = state_store.serialize(state_store.default_state())
    raw.pop("fixed_exit_15_enabled")
    raw.pop("fixed_exit_15_changed_at", None)
    raw.pop("fixed_exit_15_changed_by", None)
    assert state_store.deserialize(raw).fixed_exit_15_enabled is False


def test_thresholds_are_inclusive_and_one_sided():
    assert fixed_exit.evaluate_tick(1.5) == config.EXIT_FIXED_TP15 == "FIXED_TP15"
    assert fixed_exit.evaluate_tick(1.4999) is None
    assert fixed_exit.evaluate_tick(-5.0) is None            # 손절은 틱으로 보지 않는다
    assert fixed_exit.evaluate_bar(-1.5) == config.EXIT_FIXED_SL15 == "FIXED_SL15"
    assert fixed_exit.evaluate_bar(-1.4999) is None
    assert fixed_exit.evaluate_bar(5.0) is None


def test_toggle_without_a_selected_strategy_mode_does_nothing():
    """레거시 전략(strategy_mode 비어 있음)을 쓰는 state 에는 적용하지 않는다."""
    s = state_store.default_state()
    s.strategy_mode = None
    s.fixed_exit_15_enabled = True
    assert fixed_exit.is_enabled(s) is True and fixed_exit.is_active(s) is False


@pytest.mark.parametrize("mode", ALL)
@pytest.mark.parametrize("price,bar_close", [(10_200.0, None), (10_140.0, None), (9_800.0, 9_800.0),
                                             (10_000.0, None)])
def test_off_never_touches_the_fixed_exit_path(monkeypatch, tmp_path, mode, price, bar_close):
    _forbid_fixed(monkeypatch)
    _run(monkeypatch, tmp_path, mode=mode, on=False, price=price, bar_close=bar_close)


# ── B. ON: 네 모드 모두 ───────────────────────────────────────────────────
@pytest.mark.parametrize("mode", ALL)
def test_on_takes_full_profit_at_plus_one_point_five_on_the_tick(monkeypatch, tmp_path, mode):
    state, orders, result = _run(monkeypatch, tmp_path, mode=mode, on=True, price=10_200.0)
    sells = _sells(orders)
    assert len(sells) == 1 and sells[0].executed_qty == 10, "전량 한 번"
    assert state.position is None
    assert f"FIXED_TP15:{config.INVERSE_SYMBOL}" in result.actions
    assert _last_exit_reason() == "FIXED_TP15"


@pytest.mark.parametrize("mode", ALL)
def test_on_does_not_take_partial_or_b3_profit_below_one_point_five(monkeypatch, tmp_path, mode):
    """+1.4%: P3 계열 CHOP 진입이면 B3/rescue(+1%) 가, N1 이면 래더가 손댈 수 있는 자리다."""
    state, orders, _ = _run(monkeypatch, tmp_path, mode=mode, on=True, price=10_140.0,
                            entry_minutes_ago=4.0)
    assert _sells(orders) == []
    assert state.position is not None and state.position.quantity == 10
    assert state.p3_tp_rescued is False and state.time_window_tp1_done is False


@pytest.mark.parametrize("mode", ALL)
def test_on_stops_out_on_a_completed_3m_bar_close_at_minus_one_point_five(monkeypatch, tmp_path, mode):
    state, orders, result = _run(monkeypatch, tmp_path, mode=mode, on=True, price=9_800.0, bar_close=9_800.0)
    sells = _sells(orders)
    assert len(sells) == 1 and sells[0].executed_qty == 10
    assert state.position is None
    assert f"FIXED_SL15:{config.INVERSE_SYMBOL}" in result.actions
    assert _last_exit_reason() == "FIXED_SL15"


@pytest.mark.parametrize("mode", ALL)
def test_on_holds_a_minus_one_point_two_bar_that_p3_or_ladder_might_cut(monkeypatch, tmp_path, mode):
    """-1.2%: B3 SL(-1%) 자리. ON 이면 -1.5% 전에는 손절하지 않는다."""
    state, orders, _ = _run(monkeypatch, tmp_path, mode=mode, on=True, price=9_880.0, bar_close=9_880.0)
    assert _sells(orders) == []
    assert state.position is not None and state.position.quantity == 10


@pytest.mark.parametrize("mode", ALL)
def test_on_ignores_a_tick_below_minus_one_point_five_until_the_bar_completes(monkeypatch, tmp_path, mode):
    """형성 중인 봉(아직 완성 안 됨)은 손절 근거가 아니다."""
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=9_700.0)
    state = _state(now=now0, mode=mode, on=True, bar_close=9_700.0)
    cur_bar_start, _ = worker.forming_bar_window(now0)
    state.stop_loss_bar_ts = cur_bar_start.isoformat()     # 지금 봉이 아직 형성 중
    broker = _broker(9_700.0)
    _patch_common(monkeypatch)
    n0 = len(broker.orders)
    run_once(broker=broker, state=state, market_data=svc, now=now0)
    assert _sells(broker.orders[n0:]) == [] and state.position is not None


@pytest.mark.parametrize("mode", ALL)
def test_on_never_stops_out_on_the_entry_bar(monkeypatch, tmp_path, mode):
    _isolate_shadow_ledger(monkeypatch, tmp_path)
    svc, now0 = _market(inverse_price=9_700.0)
    state = _state(now=now0, mode=mode, on=True, bar_close=9_700.0)
    state.stop_loss_entry_bar_ts = state.stop_loss_bar_ts   # 방금 완성된 봉 = 진입 실행봉
    broker = _broker(9_700.0)
    _patch_common(monkeypatch)
    n0 = len(broker.orders)
    run_once(broker=broker, state=state, market_data=svc, now=now0)
    assert _sells(broker.orders[n0:]) == [] and state.position is not None


@pytest.mark.parametrize("mode", ALL)
def test_on_has_no_trailing_or_after_tp1_stop(monkeypatch, tmp_path, mode):
    """TP1 이후 +0.5% 로 밀린 runner — 기존 래더라면 after-TP1 stop/trailing 자리."""
    state, orders, _ = _run(monkeypatch, tmp_path, mode=mode, on=True, price=10_050.0, bar_close=10_050.0,
                            tp1_done=True, peak=3.0, regime=chop_regime.REGIME_TREND)
    assert _sells(orders) == []
    assert state.position is not None and state.position.quantity == 10


@pytest.mark.parametrize("mode", ALL)
def test_on_skips_c1_and_p3_max_hold(monkeypatch, tmp_path, mode):
    def boom(**kw):
        raise AssertionError("ON 인데 C1 / B3 max-hold 가 돌았다")
    monkeypatch.setattr(worker, "_advance_c1_peak_protection", boom)
    monkeypatch.setattr(worker, "_advance_p3_max_hold", boom)
    state, orders, _ = _run(monkeypatch, tmp_path, mode=mode, on=True, price=10_050.0, bar_close=10_050.0,
                            entry_minutes_ago=45.0)
    assert _sells(orders) == [] and state.position is not None


@pytest.mark.parametrize("mode", ALL)
def test_on_keeps_15h_forced_liquidation(monkeypatch, tmp_path, mode):
    _, now0 = _market()
    at15 = now0.replace(hour=15, minute=0, second=5)
    state, orders, result = _run(monkeypatch, tmp_path, mode=mode, on=True, price=10_050.0, now=at15)
    assert len(_sells(orders)) == 1 and state.position is None
    assert any(a.startswith("FORCED_LIQUIDATION") for a in result.actions)
    assert _last_exit_reason() == config.EXIT_FORCED_LIQUIDATION


def test_off_still_lets_p3_b3_cut_at_minus_one(monkeypatch, tmp_path):
    """대조군: 같은 -1.2% 를 OFF(P3, CHOP 진입)에서는 기존대로 B3 가 자른다."""
    state, orders, _ = _run(monkeypatch, tmp_path, mode=strategy_mode.MODE_P3, on=False,
                            price=9_880.0, bar_close=9_880.0)
    assert len(_sells(orders)) == 1 and state.position is None


# ── C. 반대 플래그 처리는 ON 에서도 그대로 호출된다 ─────────────────────────
@pytest.mark.parametrize("mode", ALL)
@pytest.mark.parametrize("on", [False, True])
def test_opposite_flag_handlers_are_called_the_same_way(monkeypatch, tmp_path, mode, on):
    calls = []

    def spy(name):
        def _f(**kw):
            calls.append(name)
            return None
        return _f
    for name in ("_resolve_tw2_3slot_candidate", "_advance_whipsaw_watch", "_advance_h50_hold",
                 "_resolve_time_window_candidate"):
        monkeypatch.setattr(worker, name, spy(name))
    _run(monkeypatch, tmp_path, mode=mode, on=on, price=10_050.0, bar_close=10_050.0)
    assert calls == ["_resolve_time_window_candidate", "_resolve_tw2_3slot_candidate",
                     "_advance_whipsaw_watch", "_advance_h50_hold"]


# ── D. 저장 / 복원 / 모드 전환 / UI ────────────────────────────────────────
@pytest.mark.parametrize("mode", ALL)
def test_toggle_survives_restart_and_mode_switches(mode):
    s = state_store.default_state()
    strategy_mode.apply(s, mode)
    s.fixed_exit_15_enabled = True
    r = state_store.deserialize(dict(state_store.serialize(s)))
    strategy_mode.restore(r)
    assert r.fixed_exit_15_enabled is True and fixed_exit.is_active(r)
    assert strategy_mode.current(r) == mode
    for other in ALL:
        strategy_mode.apply(r, other)
        assert r.fixed_exit_15_enabled is True and fixed_exit.is_active(r)


@pytest.mark.parametrize("mode", ALL)
def test_fixed_sl15_counts_as_first_trade_stop_for_sizing(mode):
    """사이징은 전략 그대로 -- 그날 첫 거래 손절 뒤 x1.20 규칙이 FIXED_SL15 에도 걸린다(TP 는 아님)."""
    from app.trading.macd2 import position_sizing
    for reason, expected in ((config.EXIT_FIXED_SL15, True), (config.EXIT_FIXED_TP15, False),
                             (config.EXIT_TW_STOP_LOSS, True)):
        s = state_store.default_state()
        strategy_mode.apply(s, mode)
        s.fixed_exit_15_enabled = True
        s.x2lite_entry_seq_today = 1
        position_sizing.note_full_exit(s, reason)
        assert position_sizing.first_trade_stop_loss_active(s) is (expected and position_sizing.is_active(s))


def test_day_rollover_and_stop_do_not_clear_the_toggle():
    s = state_store.default_state()
    strategy_mode.apply(s, strategy_mode.MODE_E)
    s.fixed_exit_15_enabled = True
    worker._apply_day_rollover(s, datetime(2026, 10, 16, 8, 0, tzinfo=KST))
    assert s.fixed_exit_15_enabled is True


def test_service_setter_persists_through_the_state_file():
    svc = service_module.Macd2Service.__new__(service_module.Macd2Service)
    st = state_store.load_state()
    strategy_mode.apply(st, strategy_mode.MODE_CHOP)
    state_store.save_state(st)
    out = svc.set_fixed_exit_15_enabled(True, changed_by="test")
    assert out["ok"] and out["fixed_exit_15_enabled"] and out["active"] and out["previous"] is False
    reloaded = state_store.load_state()
    assert reloaded.fixed_exit_15_enabled is True and reloaded.fixed_exit_15_changed_by == "test"
    svc.set_strategy_mode(strategy_mode.MODE_N1, changed_by="test")
    assert state_store.load_state().fixed_exit_15_enabled is True, "모드를 바꿔도 유지"
    out = svc.set_fixed_exit_15_enabled(False)
    assert out["fixed_exit_15_enabled"] is False and state_store.load_state().fixed_exit_15_enabled is False


TOGGLE_LABEL = "고정 ±1.5% 청산"


@pytest.mark.parametrize("mode", ALL)
def test_ui_toggle_is_shown_in_every_mode_and_persists(mode):
    from tests.macd2.test_ui_strategy_mode import _run as _ui_run
    st = state_store.load_state()
    strategy_mode.apply(st, mode)
    st.fixed_exit_15_enabled = False
    state_store.save_state(st)
    at = _ui_run()
    tg = [t for t in at.toggle if t.label == TOGGLE_LABEL]
    assert len(tg) == 1 and tg[0].value is False
    assert any("EXIT: **전략 기본**" in str(c.value) for c in at.caption)
    tg[0].set_value(True).run()
    assert state_store.load_state().fixed_exit_15_enabled is True
    assert any("고정 ±1.5% 청산: 🟢 ON" in str(m.value) for m in at.markdown)
    assert any("EXIT: **FIXED ±1.5%**" in str(c.value) for c in at.caption)
