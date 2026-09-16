"""09:03 예약매수 — 발동 직전 MACD 상태 재확인 (2026-09-16 실거래 사고).

사고 경위
---------
08:00 에 BLUE 가 확정돼 신호원장에 기록됐고, 사용자가 그걸 보고 09:03 예약매수를
DOWN_BLUE 로 걸어뒀다. 그런데 09:00 bar 에서 RED 로 뒤집혔다. 09:00 플래그의 T+3
재확인은 09:06 이라 09:03 시점에는 아직 "확정 플래그"가 없었고, 예약매수는
**예약된 방향을 그대로 믿고** 인버스를 매수했다 -- 사야 할 것은 레버리지였다.

자매 기능인 프리마켓 승계(_execute_premarket_carry_entry)는 처음부터
"09:03에도 동일 MACD STATE가 유지되면" 이라는 재확인을 하고 있었다
(MACD_STATE_NOT_HELD_AT_0903). 수동 예약만 그 검사가 빠져 있던 비대칭이 원인이다.

이 파일이 고정하는 것
--------------------
  1. 방향이 뒤집혔으면 **주문이 나가지 않는다** (브로커 호출 0건)
  2. 그 사실이 신호원장에 남는다 (조용히 사라지지 않는다)
  3. 하루 1회로 소진되고 arm 이 해제된다 (다음 tick 에 다시 쏘지 않는다)
  4. 방향이 유지되면 예전과 **100% 동일하게** 체결된다 (기능 무력화가 아니다)
  5. 완성봉이 없어 검증 자체가 불가능하면 쏘지 않고 재시도한다
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.models import Direction, RuntimeState
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_early_take_profit_worker import _market, _seed_completed_bar

KST = config.KST


class _Snap:
    """run_once 가 만드는 macd_snap 중 이 경로가 쓰는 필드만 가진 스텁."""

    def __init__(self, diff: float, bar_dt: datetime):
        self.current_diff = float(diff)
        self.macd = 0.0
        self.signal = 0.0
        self.bar_dt = bar_dt


def _armed_state(direction: Direction, *, now: datetime) -> RuntimeState:
    """예약매수를 **실제로 쓰는** 전략(TW2) 기준으로 만든다.

    2026-09-16 이후 3-SLOT 계열(H50 포함)에서는 예약매수가 1차 방어에서 이미
    차단되므로(test_scheduled_entry_disabled_in_3slot.py), 이 파일이 검증하는
    2차 방어선(주문 직전 MACD 재확인)은 예약매수가 살아 있는 모드에서만
    의미가 있다."""
    s = state_store.default_state()
    for f in ("time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
              "time_window_x2lite_filter_enabled", "time_window_h50_filter_enabled"):
        setattr(s, f, False)
    s.time_window_2_filter_enabled = True
    s.auto_trade_on = True
    s.budget = 10_000_000.0
    s.scheduled_entry_armed_direction = direction
    s.scheduled_entry_armed_at = now.replace(hour=8, minute=5).isoformat()
    s.scheduled_entry_armed_by = "ui"
    s.session_date = now.strftime("%Y%m%d")
    return s


def _broker() -> FakeBroker:
    return FakeBroker(cash=10_000_000.0,
                      quotes={config.LONG_SYMBOL: 15_000.0,
                              config.INVERSE_SYMBOL: 10_000.0})


def _fire_at(state, broker, svc, now, snap):
    return worker._execute_scheduled_entry(
        broker=broker, market_data=svc, state=state, now=now, macd_snap=snap)


# ── 1. 사고 재현: BLUE 예약 + 09:00 bar 에서 RED -> 주문 나가면 안 된다 ──────
def test_flipped_direction_places_no_order():
    svc, now0 = _market()
    now = now0.replace(hour=9, minute=3)
    state = _armed_state(Direction.DOWN_BLUE, now=now)
    broker = _broker()
    # 09:00-09:03 완성봉의 MACD 가 RED(diff > 0) 로 뒤집혔다
    snap = _Snap(+0.9, now.replace(minute=0))

    out = _fire_at(state, broker, svc, now, snap)

    assert out is None, "방향이 뒤집혔는데 체결됐다"
    assert not broker.orders, f"브로커에 주문이 나갔다: {broker.orders!r}"
    assert state.position is None
    assert state.scheduled_entry_last_result == config.SCHEDULED_ENTRY_MACD_STATE_FLIPPED


def test_the_block_is_recorded_in_the_signal_ledger():
    """조용히 사라지면 안 된다 -- 왜 안 샀는지 원장에 남아야 한다."""
    svc, now0 = _market()
    now = now0.replace(hour=9, minute=3)
    state = _armed_state(Direction.DOWN_BLUE, now=now)
    _fire_at(state, _broker(), svc, now, _Snap(+0.9, now.replace(minute=0)))

    rows = ledger.load_signal_ledger(limit=100)
    hit = [r for r in rows if "SCHEDULED" in str(r.get("signal_type", ""))]
    assert hit, f"예약매수 차단이 신호원장에 없다: {[r.get('signal_type') for r in rows]!r}"
    assert any(config.SCHEDULED_ENTRY_MACD_STATE_FLIPPED in str(r) for r in hit), \
        f"차단 사유가 원장에 없다: {hit!r}"


def test_it_is_consumed_and_does_not_retry_on_the_next_tick():
    svc, now0 = _market()
    now = now0.replace(hour=9, minute=3)
    state = _armed_state(Direction.DOWN_BLUE, now=now)
    broker = _broker()
    _fire_at(state, broker, svc, now, _Snap(+0.9, now.replace(minute=0)))

    assert state.scheduled_entry_armed_direction is None, "arm 이 남아 있다"
    assert state.scheduled_entry_executed_at, "하루 1회로 소진되지 않았다"
    assert worker._scheduled_entry_should_fire(state, now + timedelta(seconds=30)) is False

    # 방향이 다시 BLUE 로 돌아와도 재발동하면 안 된다 (게이트를 우회해 직접
    # 불러도 arm 이 없으므로 아무 것도 하지 않아야 한다)
    assert _fire_at(state, broker, svc, now + timedelta(seconds=30),
                    _Snap(-0.9, now.replace(minute=0))) is None
    assert not broker.orders, f"소진 후 재발동했다: {broker.orders!r}"


# ── 2. 기능 무력화가 아니다: 방향이 유지되면 예전과 동일하게 체결 ───────────
@pytest.mark.parametrize(
    "direction,diff,symbol",
    [(Direction.DOWN_BLUE, -0.9, config.INVERSE_SYMBOL),
     (Direction.UP_RED, +0.9, config.LONG_SYMBOL)],
)
def test_direction_still_held_fires_exactly_as_before(direction, diff, symbol):
    svc, now0 = _market()
    now = now0.replace(hour=9, minute=3)
    state = _armed_state(direction, now=now)
    broker = _broker()

    out = _fire_at(state, broker, svc, now, _Snap(diff, now.replace(minute=0)))

    assert out is not None, "방향이 유지되는데 체결되지 않았다 -- 기능이 죽었다"
    assert state.position is not None and state.position.symbol == symbol
    assert any(str(getattr(o, "side", "")).upper() == "BUY" for o in broker.orders)
    assert state.scheduled_entry_last_result == "EXECUTED"
    assert state.scheduled_entry_protected is True


# ── 3. 검증 불가 시에는 쏘지 않고 재시도 ───────────────────────────────────
def test_missing_macd_snap_does_not_fire_blind_and_stays_retryable():
    svc, now0 = _market()
    now = now0.replace(hour=9, minute=3)
    state = _armed_state(Direction.DOWN_BLUE, now=now)
    broker = _broker()

    out = _fire_at(state, broker, svc, now, None)

    assert out is None and not broker.orders, "검증도 없이 주문이 나갔다"
    assert state.scheduled_entry_executed_at is None, "일시적 상황인데 하루치를 소진했다"
    assert state.scheduled_entry_armed_direction == Direction.DOWN_BLUE, "arm 이 사라졌다"
    assert worker._scheduled_entry_should_fire(state, now + timedelta(seconds=30)) is True


# ── 4. 자매 기능과의 대칭 ─────────────────────────────────────────────────
def test_both_0903_paths_now_use_the_same_recheck_helper():
    """프리마켓 승계와 수동 예약이 같은 헬퍼로 재확인하는지 소스로 고정한다 --
    이 비대칭이 바로 사고의 원인이었다."""
    import inspect
    for fn in (worker._execute_scheduled_entry, worker._execute_premarket_carry_entry):
        src = inspect.getsource(fn)
        assert "_pending_direction_still_active" in src, f"{fn.__name__} 에 재확인이 없다"
