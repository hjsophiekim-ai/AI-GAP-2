"""신규진입 허용(entry_arming) — 감시와 신규진입의 분리 (2026-10-13).

전부 ``@pytest.mark.entry_arming`` 이라 conftest 의 '기본 허용' fixture 를 끄고
production 게이트를 그대로 태운다. worker 경로는 test_tw2_3slot_worker_regression
의 하네스(실제 run_once + fake broker)를 재사용한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.trading.macd2 import config, entry_arming, ledger, state_store, strategy_mode, worker
from app.trading.macd2.models import Direction, PositionSnapshot
from app.trading.macd2.worker import TickResult, run_once
from tests.macd2 import test_tw2_3slot_worker_regression as R
from tests.macd2.test_tw2_3slot_worker_regression import tw2_3slot_market_data  # noqa: F401  (fixture)
from tests.macd2.fake_broker import FakeBroker

pytestmark = pytest.mark.entry_arming

KST = config.KST
FLAG = R._PRIOR_DAY                      # 플래그 봉 시작 -> 발생(마감) = FLAG + 3분


def _broker():
    return FakeBroker(cash=10_000_000.0, quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})


def _arm(state, at: datetime):
    entry_arming.set_enabled(state, True, now_iso=at.isoformat(), changed_by="test")


def _buys(broker):
    return [o for o in broker.orders if str(getattr(o, "side", "") or (o.get("side") if isinstance(o, dict) else "")).upper() == "BUY"]


def _sells(broker):
    return [o for o in broker.orders if str(getattr(o, "side", "") or (o.get("side") if isinstance(o, dict) else "")).upper() == "SELL"]


def _held_inverse(state, broker, now0):
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=10, avg_price=10_000.0, entry_at=now0)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.time_window_entry_session = "MORNING"
    state.tw2_3slot_slots_used_today = 1
    broker.buy_market(config.INVERSE_SYMBOL, 10, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0
    broker.orders.clear()


# ── 순수함수 ─────────────────────────────────────────────────────────────

def test_default_is_off_and_blocks():
    st = state_store.default_state()
    assert st.entry_enabled is False and st.entry_armed_at is None
    assert entry_arming.block_reason(st, signal_id="20260105_090000_UP_RED") == entry_arming.ENTRY_DISARMED


def test_origin_is_flag_bar_close_and_prefers_earlier_flag_bar():
    sid = "20260105_100600_UP_RED:E_BRK"
    assert entry_arming.signal_origin_at(sid) == datetime(2026, 1, 5, 10, 9, tzinfo=KST)
    flag = datetime(2026, 1, 5, 10, 0, tzinfo=KST)               # T+3 확인봉보다 이른 플래그봉
    assert entry_arming.signal_origin_at(sid, flag) == datetime(2026, 1, 5, 10, 3, tzinfo=KST)
    assert entry_arming.signal_origin_at("MANUAL_UP_RED_x") is None


def test_set_enabled_transitions():
    st = state_store.default_state()
    t1, t2 = "2026-01-05T10:00:00+09:00", "2026-01-05T10:30:00+09:00"
    assert entry_arming.set_enabled(st, True, now_iso=t1) is False
    assert st.entry_armed_at == t1
    entry_arming.set_enabled(st, True, now_iso=t2)               # ON 상태에서 다시 ON -> 기준 유지
    assert st.entry_armed_at == t1
    assert entry_arming.set_enabled(st, False, now_iso=t2) is True
    assert st.entry_enabled is False and st.entry_armed_at is None
    entry_arming.set_enabled(st, True, now_iso=t2)               # 다시 ON -> 새 기준
    assert st.entry_armed_at == t2


def test_pre_arm_and_unknown_origin_are_blocked():
    st = state_store.default_state()
    _arm(st, datetime(2026, 1, 5, 10, 1, tzinfo=KST))
    # 10:00 봉은 10:03 에 마감 -> ON(10:01) 이후 발생 -> 허용
    assert entry_arming.block_reason(st, signal_id="20260105_100000_UP_RED") is None
    # 09:57 봉은 10:00 마감 -> ON 이전 발생 -> 소급 금지
    assert entry_arming.block_reason(st, signal_id="20260105_095700_UP_RED") == entry_arming.ENTRY_PRE_ARM_SIGNAL
    assert entry_arming.block_reason(st, signal_id="NO_TIMESTAMP") == entry_arming.ENTRY_PRE_ARM_SIGNAL


# ── 재시작 / roundtrip ───────────────────────────────────────────────────

def test_state_roundtrip_restores_on_and_armed_at():
    st = state_store.default_state()
    _arm(st, datetime(2026, 1, 5, 10, 1, tzinfo=KST))
    state_store.save_state(st)
    back = state_store.load_state()
    assert back.entry_enabled is True
    assert back.entry_armed_at == st.entry_armed_at
    entry_arming.set_enabled(back, False, now_iso="2026-01-05T11:00:00+09:00")
    state_store.save_state(back)
    again = state_store.load_state()
    assert again.entry_enabled is False and again.entry_armed_at is None


def test_legacy_state_without_fields_loads_off(tmp_path):
    st = state_store.default_state()
    state_store.save_state(st)
    import json
    raw = json.loads(state_store.STATE_PATH.read_text(encoding="utf-8"))
    for k in ("entry_enabled", "entry_armed_at", "entry_changed_at", "entry_changed_by"):
        raw.pop(k, None)
    state_store.STATE_PATH.write_text(json.dumps(raw), encoding="utf-8")
    assert state_store.load_state().entry_enabled is False
    raw["entry_enabled"] = True                                   # armed_at 없는 ON 은 OFF 로
    state_store.STATE_PATH.write_text(json.dumps(raw), encoding="utf-8")
    assert state_store.load_state().entry_enabled is False


# ── worker 경로 (실제 run_once) ──────────────────────────────────────────

def test_off_flag_recognized_and_ledgered_but_zero_orders(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = R._fresh_3slot_state()
    broker = _broker()
    R._patch_common(monkeypatch, entry_decision=R._approved(), quality_decision=R._quality(True), teg_decision=R._teg(True))

    R._prime_3slot_pending(state, Direction.UP_RED, before=FLAG)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert broker.orders == []
    assert state.position is None
    assert any(a.startswith(entry_arming.ENTRY_DISARMED) for a in result.actions), result.actions
    assert int(state.tw2_3slot_slots_used_today or 0) == 0          # 슬롯 소비 없음
    rows = ledger.load_signal_ledger()
    assert rows and rows[-1]["block_reason"] == entry_arming.ENTRY_DISARMED   # 신호원장 기록은 계속


def test_pending_from_before_on_is_never_backfilled_then_new_signal_orders(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = R._fresh_3slot_state()
    broker = _broker()
    R._patch_common(monkeypatch, entry_decision=R._approved(), quality_decision=R._quality(True), teg_decision=R._teg(True))

    # OFF 중 발생한 플래그(FLAG) 가 pending 으로 남은 채 ON
    _arm(state, FLAG + timedelta(minutes=10))
    R._prime_3slot_pending(state, Direction.UP_RED, before=FLAG)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert broker.orders == [], "ON 이전 플래그를 소급 체결했다"
    assert any(a.startswith(entry_arming.ENTRY_PRE_ARM_SIGNAL) for a in result.actions), result.actions

    # ON 이후 발생한 새 플래그 -> 정상 주문
    R._prime_3slot_pending(state, Direction.DOWN_BLUE, before=FLAG + timedelta(minutes=12))
    result = run_once(broker=broker, market_data=svc, state=state, now=R._step(now0, 3))
    assert any(a.startswith("TW2_3SLOT_ENTRY") for a in result.actions), result.actions
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert len(_buys(broker)) == 1


def test_on_then_off_blocks_the_next_new_signal(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = R._fresh_3slot_state()
    broker = _broker()
    R._patch_common(monkeypatch, entry_decision=R._approved(), quality_decision=R._quality(True), teg_decision=R._teg(True))
    _arm(state, FLAG - timedelta(days=1))
    entry_arming.set_enabled(state, False, now_iso=FLAG.isoformat())

    R._prime_3slot_pending(state, Direction.UP_RED, before=FLAG + timedelta(minutes=30))
    run_once(broker=broker, market_data=svc, state=state, now=now0)
    assert broker.orders == []
    assert state.position is None


def test_off_opposite_signal_still_sells_the_held_position_but_never_buys(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    state = R._fresh_3slot_state()
    broker = _broker()
    _held_inverse(state, broker, now0)
    R._patch_common(monkeypatch, entry_decision=R._approved())   # ON 이었으면 풀 스위치

    R._prime_3slot_pending(state, Direction.UP_RED, before=FLAG)
    result = run_once(broker=broker, market_data=svc, state=state, now=now0)

    assert any(a.startswith("EXIT_ONLY_" + entry_arming.ENTRY_DISARMED) for a in result.actions), result.actions
    assert state.position is None
    assert config.INVERSE_SYMBOL not in broker._positions
    assert config.LONG_SYMBOL not in broker._positions
    assert len(_sells(broker)) == 1 and _buys(broker) == []
    rows = ledger.load_signal_ledger()
    assert rows[-1]["order_result"] == entry_arming.SELL_EXECUTED_ENTRY_DISARMED


def test_off_rejected_reversal_sell_only_path_is_unchanged(tw2_3slot_market_data, monkeypatch):
    R.test_non_whipsaw_reversal_reject_still_liquidates_the_held_position(tw2_3slot_market_data, monkeypatch)


def test_off_take_profit_still_fires(monkeypatch):
    R.test_take_profit_fires_at_tw2s_own_6pct_threshold_not_5pct(monkeypatch)


def test_off_whipsaw_hold_is_unchanged(tw2_3slot_market_data, monkeypatch):
    R.test_whipsaw_classified_reversal_holds_instead_of_selling(
        tw2_3slot_market_data, monkeypatch, config.TW_REJECT_NOT_CONFIRMED)


# ── 주문 직전 chokepoint — 모드 무관 (N1/P3/E/CHOP) ─────────────────────

@pytest.mark.parametrize("mode", list(strategy_mode.ALL_MODES))
@pytest.mark.parametrize("sid_suffix", ["", ":E_BRK"])         # 일반 / E 돌파 체결
def test_chokepoint_blocks_every_mode(tw2_3slot_market_data, monkeypatch, mode, sid_suffix):
    svc, now0 = tw2_3slot_market_data
    R._patch_common(monkeypatch)
    sid = FLAG.strftime("%Y%m%d_%H%M%S") + "_UP_RED" + sid_suffix
    for armed, expect in ((None, entry_arming.ENTRY_DISARMED),
                          (FLAG + timedelta(minutes=5), entry_arming.ENTRY_PRE_ARM_SIGNAL),
                          (FLAG - timedelta(minutes=5), None)):
        state = R._fresh_3slot_state()
        strategy_mode.apply(state, mode)
        if armed is not None:
            _arm(state, armed)
        broker = _broker()
        res = TickResult()
        out = worker._execute_or_wait(
            broker=broker, market_data=svc, state=state, now=now0,
            macd_snap=SimpleNamespace(bar_dt=FLAG), direction=Direction.UP_RED,
            signal_id=sid, signal_type="INITIAL", position=None, result=res,
            signal_origin_bar_dt=FLAG,
        )
        if expect is None:
            assert len(_buys(broker)) == 1, (mode, res.actions)
        else:
            assert broker.orders == [], (mode, expect)
            assert out.block_reason == expect and res.skipped == expect
            assert sid in state.processed_signal_ids


def test_pending_retry_of_a_disarmed_signal_is_dropped(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    R._patch_common(monkeypatch)
    state = R._fresh_3slot_state()
    sid = FLAG.strftime("%Y%m%d_%H%M%S") + "_UP_RED"
    state.pending_signal = {"signal_id": sid, "direction": "UP_RED"}
    broker = _broker()
    out = worker._execute_or_wait(
        broker=broker, market_data=svc, state=state, now=now0,
        macd_snap=SimpleNamespace(bar_dt=FLAG), direction=Direction.UP_RED,
        signal_id=sid, signal_type="INITIAL", position=None, result=TickResult(),
    )
    assert out.block_reason == entry_arming.ENTRY_DISARMED
    assert state.pending_signal is None and broker.orders == []


# ── 09:03 예약 / 프리마켓 승계 / UI 수동매수 ──────────────────────────────

def test_scheduled_entry_is_consumed_without_order_when_off(tw2_3slot_market_data):
    svc, now0 = tw2_3slot_market_data
    state = R._fresh_3slot_state()
    state.scheduled_entry_armed_direction = Direction.UP_RED
    broker = _broker()
    worker._execute_scheduled_entry(broker=broker, market_data=svc, state=state, now=now0, macd_snap=None)
    assert broker.orders == []
    assert state.scheduled_entry_last_result == entry_arming.ENTRY_DISARMED
    assert state.scheduled_entry_armed_direction is None


def test_premarket_carry_flag_before_on_is_not_backfilled(tw2_3slot_market_data):
    svc, now0 = tw2_3slot_market_data
    state = R._fresh_3slot_state()
    _arm(state, FLAG + timedelta(minutes=10))
    state.premarket_carry_candidate_direction = Direction.UP_RED
    state.premarket_carry_candidate_bar_ts = FLAG.isoformat()
    broker = _broker()
    worker._execute_premarket_carry_entry(broker=broker, market_data=svc, state=state, now=now0, macd_snap=None)
    assert broker.orders == []
    assert state.premarket_carry_last_result == entry_arming.ENTRY_PRE_ARM_SIGNAL
    assert state.premarket_carry_candidate_direction is None


def test_service_toggle_and_manual_entry_gate(monkeypatch):
    from app.trading.macd2 import service as service_module
    svc = service_module.Macd2Service()
    st = state_store.default_state()
    st.auto_trade_on = True
    state_store.save_state(st)
    svc._worker = SimpleNamespace(is_alive=lambda: True)
    svc._broker = object()
    svc._market_data = object()
    assert svc.manual_entry("UP_RED")["message"] == entry_arming.ENTRY_DISARMED

    res = svc.set_entry_enabled(True, changed_by="test")
    assert res["ok"] and res["entry_enabled"] and res["previous"] is False and res["entry_armed_at"]
    assert state_store.load_state().entry_enabled is True
    res = svc.set_entry_enabled(False, changed_by="test")
    assert res["entry_enabled"] is False and state_store.load_state().entry_armed_at is None


def test_mode_switch_never_touches_entry_flag():
    st = state_store.default_state()
    _arm(st, FLAG)
    armed = st.entry_armed_at
    for m in strategy_mode.ALL_MODES:
        strategy_mode.apply(st, m)
        assert st.entry_enabled is True and st.entry_armed_at == armed


# ── 2026-10-13 추가: 날짜 변경 / 중지 / 시작 시 자동 OFF ──────────────────

def test_day_rollover_turns_entry_off_and_same_day_keeps_it():
    st = state_store.default_state()
    day1 = datetime(2026, 1, 5, 10, 0, tzinfo=KST)
    _arm(st, day1)
    worker._apply_day_rollover(st, day1 + timedelta(hours=3))      # 같은 날
    assert st.entry_enabled is True
    worker._apply_day_rollover(st, day1 + timedelta(days=1, hours=-1))   # 다음날 09:00
    assert st.entry_enabled is False and st.entry_armed_at is None
    assert st.entry_changed_by == "day_rollover"


def test_yesterdays_on_never_buys_today_even_before_rollover():
    st = state_store.default_state()
    _arm(st, datetime(2026, 1, 5, 10, 0, tzinfo=KST))
    assert entry_arming.block_reason(st, signal_id="20260106_090000_UP_RED") == entry_arming.ENTRY_DISARMED


def _svc_with_on_state():
    from app.trading.macd2 import service as service_module
    svc = service_module.Macd2Service()
    st = state_store.default_state()
    st.auto_trade_on = True
    _arm(st, datetime.now(KST))
    state_store.save_state(st)
    return svc


def test_stop_turns_entry_off():
    svc = _svc_with_on_state()
    svc.stop("user_stop")
    st = state_store.load_state()
    assert st.entry_enabled is False and st.entry_armed_at is None and st.auto_trade_on is False


def test_stop_and_liquidate_turns_entry_off():
    svc = _svc_with_on_state()
    svc._broker = _broker()
    res = svc.stop_and_liquidate_all("test_liquidate")
    assert res["ok"] is True
    assert state_store.load_state().entry_enabled is False


def test_user_start_starts_with_entry_off_but_auto_recover_keeps_it(monkeypatch):
    monkeypatch.setattr(config, "AUTO_TRADE_HARD_DISABLED", True)   # start 를 일찍 끝내게
    svc = _svc_with_on_state()
    svc.start(mode="mock", _auto_recover=True)                       # 장중 자동복구: 유지
    assert state_store.load_state().entry_enabled is True
    svc.start(mode="mock")                                            # 사용자 [자동매매 시작]
    assert state_store.load_state().entry_enabled is False
