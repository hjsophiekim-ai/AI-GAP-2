"""09:03 예약매수는 3-SLOT 계열에서 존재하지 않는다 (2026-09-16 실거래 사고).

사고
----
H50 운영 중이었는데 08:00 BLUE 이후 scheduled_entry_armed_direction=DOWN_BLUE 가
만들어졌고, 09:03 에 실제로 인버스 주문이 나갔다. 09:00 에는 이미 RED 로 뒤집혀
있었으므로 사야 할 것은 레버리지였다.

구조적 원인
-----------
예약매수에는 **활성화 플래그도, 모드 게이트도 없었다**. 자매 기능인 프리마켓
승계(_premarket_carry_should_fire)는 "TW2/TEGv2 가 아니면 발동 안 함" 게이트를
처음부터 갖고 있었고 2026-09-01 에 TW2_3SLOT 도 명시적으로 제외했는데, 예약매수
쪽에만 그 게이트가 통째로 없어서 어떤 모드에서도 살아 있었다.

방어선
------
1차: 3-SLOT 계열에서는 arm 자체가 생기지 않는다 (service / UI)
2차: arm 이 있어도 발동하지 않는다 (worker._scheduled_entry_should_fire)
3차: 저장된 state 에 남아 있어도 복원 시 무효화된다 (state_store.deserialize)
4차: 그래도 실행되면 주문 직전 MACD 재확인이 막는다
     (tests/macd2/test_scheduled_entry_macd_recheck.py)

다른 전략(TW/TW2/TEGv2/무필터)의 예약매수는 건드리지 않는다 -- 아래 대조군이
그것을 고정한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, state_store, time_window_3slot as tw3, worker
from app.trading.macd2.models import Direction, RuntimeState
from app.trading.macd2.service import Macd2Service

KST = config.KST
_3SLOT_MODES = (
    ("time_window_3slot_filter_enabled", tw3.MODE_TW2_3SLOT),
    ("time_window_twf_filter_enabled", tw3.MODE_TWF_3SLOT),
    ("time_window_x2lite_filter_enabled", tw3.MODE_X2LITE_3SLOT),
    ("time_window_h50_filter_enabled", tw3.MODE_X2LITE_H50_3SLOT),
)


def _mode_state(flag: str) -> RuntimeState:
    s = state_store.default_state()
    s.auto_trade_on = True
    s.budget = 10_000_000.0
    for f, _m in _3SLOT_MODES:
        setattr(s, f, False)
    setattr(s, flag, True)
    return s


def _tw2_state() -> RuntimeState:
    """대조군 — 예약매수를 실제로 쓰는 레거시 전략."""
    s = state_store.default_state()
    s.auto_trade_on = True
    s.budget = 10_000_000.0
    for f, _m in _3SLOT_MODES:
        setattr(s, f, False)
    s.time_window_2_filter_enabled = True
    return s


# ── 1. 헬퍼 계약 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("flag,mode", _3SLOT_MODES)
def test_not_supported_in_every_3slot_mode(flag, mode):
    s = _mode_state(flag)
    assert tw3.active_3slot_mode(s) == mode
    assert tw3.scheduled_entry_supported(s) is False, f"{mode} 에서 예약매수가 허용된다"


def test_still_supported_for_the_strategies_that_use_it():
    assert tw3.scheduled_entry_supported(_tw2_state()) is True, "TW2 의 예약매수를 깨뜨렸다"
    flat = state_store.default_state()
    for f, _m in _3SLOT_MODES:
        setattr(flat, f, False)
    assert tw3.scheduled_entry_supported(flat) is True, "무필터에서도 예전대로 동작해야 한다"


# ── 2. 1차 방어: arm 자체가 생기지 않는다 ─────────────────────────────────
@pytest.mark.parametrize("flag,mode", _3SLOT_MODES)
def test_service_refuses_to_arm_and_leaves_state_clean(flag, mode):
    state_store.save_state(_mode_state(flag))
    svc = Macd2Service()

    for d in ("DOWN_BLUE", "UP_RED"):
        res = svc.arm_scheduled_entry(d, changed_by="ui")
        assert res.get("ok") is False, f"{mode} 에서 {d} 예약이 수락됐다"
        assert res.get("message") == config.SCHEDULED_ENTRY_NOT_SUPPORTED_IN_MODE

    back = state_store.load_state()
    assert back.scheduled_entry_armed_direction is None, "state 에 arm 이 생겼다"
    assert back.scheduled_entry_armed_at is None
    assert back.scheduled_entry_armed_by is None


def test_service_still_arms_for_tw2():
    state_store.save_state(_tw2_state())
    res = Macd2Service().arm_scheduled_entry("DOWN_BLUE", changed_by="ui")
    assert res.get("ok") is True and res.get("armed") is True, "TW2 예약이 막혔다"
    assert state_store.load_state().scheduled_entry_armed_direction == Direction.DOWN_BLUE


# ── 3. 2차 방어: arm 이 있어도 발동하지 않는다 ────────────────────────────
@pytest.mark.parametrize("flag,mode", _3SLOT_MODES)
def test_worker_never_fires_even_with_a_hand_edited_arm(flag, mode):
    s = _mode_state(flag)
    s.scheduled_entry_armed_direction = Direction.DOWN_BLUE     # 손으로 심은 arm
    s.scheduled_entry_armed_at = datetime(2026, 9, 16, 8, 5, tzinfo=KST).isoformat()
    now = datetime(2026, 9, 16, 9, 3, 10, tzinfo=KST)
    assert worker._scheduled_entry_should_fire(s, now) is False, f"{mode} 에서 발동한다"


def test_worker_still_fires_for_tw2():
    s = _tw2_state()
    s.scheduled_entry_armed_direction = Direction.DOWN_BLUE
    s.scheduled_entry_armed_at = datetime(2026, 9, 16, 8, 5, tzinfo=KST).isoformat()
    now = datetime(2026, 9, 16, 9, 3, 10, tzinfo=KST)
    assert worker._scheduled_entry_should_fire(s, now) is True, "TW2 발동이 막혔다"


# ── 4. 3차 방어: 저장된 arm 이 복원되지 않는다 ────────────────────────────
@pytest.mark.parametrize("flag,mode", _3SLOT_MODES)
def test_persisted_arm_is_invalidated_on_restore(flag, mode):
    s = _mode_state(flag)
    s.scheduled_entry_armed_direction = Direction.DOWN_BLUE
    s.scheduled_entry_armed_at = datetime(2026, 9, 16, 8, 5, tzinfo=KST).isoformat()
    s.scheduled_entry_armed_by = "ui"
    state_store.save_state(s)

    back = state_store.load_state()          # = 재기동
    assert back.scheduled_entry_armed_direction is None, \
        f"{mode} 로 복원됐는데 과거 예약이 되살아났다"
    assert tw3.active_3slot_mode(back) == mode, "모드 자체는 유지돼야 한다"


def test_persisted_arm_survives_for_tw2():
    """오늘 건 예약은 TW2 에서 복원돼야 한다(날짜 고정 금지 -- stale 판정이
    '오늘'을 기준으로 하므로 하드코딩하면 내일 깨진다)."""
    s = _tw2_state()
    s.scheduled_entry_armed_direction = Direction.UP_RED
    s.scheduled_entry_armed_at = datetime.now(KST).replace(hour=8, minute=5).isoformat()
    state_store.save_state(s)
    assert state_store.load_state().scheduled_entry_armed_direction == Direction.UP_RED


def test_a_stale_arm_is_dropped_even_for_tw2():
    """모드와 무관하게 '건 날에만 유효' 불변식이 적용된다."""
    s = _tw2_state()
    s.scheduled_entry_armed_direction = Direction.UP_RED
    s.scheduled_entry_armed_at = datetime(2026, 9, 10, 8, 5, tzinfo=KST).isoformat()
    state_store.save_state(s)
    assert state_store.load_state().scheduled_entry_armed_direction is None


# ── 5. 오늘 사고 replay ───────────────────────────────────────────────────
def test_replay_of_the_20260916_incident_under_h50():
    """08:00 BLUE -> (H50) arm 0건 -> 09:03 예약 BUY 0건.

    09:00 RED 는 기존 플래그 경로가 처리하며, 예약매수는 어디에도 끼어들지
    않는다. 사고 당시에는 이 세 줄이 전부 반대였다."""
    state_store.save_state(_mode_state("time_window_h50_filter_enabled"))
    svc = Macd2Service()

    # 08:00 BLUE 를 보고 예약을 시도하는 상황 (사용자 클릭이든 잔존이든)
    res = svc.arm_scheduled_entry("DOWN_BLUE", changed_by="ui")
    assert res.get("ok") is False
    s = state_store.load_state()
    assert s.scheduled_entry_armed_direction is None, "arm 0건이어야 한다"

    # 09:03 — 발동 대상 자체가 없다
    now = datetime(2026, 9, 16, 9, 3, 5, tzinfo=KST)
    assert worker._scheduled_entry_should_fire(s, now) is False

    # 손으로 arm 을 심어도(과거 state 잔존 시나리오) 여전히 발동하지 않는다
    s.scheduled_entry_armed_direction = Direction.DOWN_BLUE
    assert worker._scheduled_entry_should_fire(s, now) is False, \
        "잔존 arm 으로 09:03 인버스 매수가 다시 가능하다"

    # 모드는 그대로 H50 이고, 정상 진입 경로는 살아 있다
    assert tw3.active_3slot_mode(s) == tw3.MODE_X2LITE_H50_3SLOT


# ── 6. 두 09:03 경로가 같은 모드 게이트를 갖는지 (사고의 구조적 원인) ──────
def test_both_0903_paths_are_mode_gated():
    import inspect
    sched = inspect.getsource(worker._scheduled_entry_should_fire)
    carry = inspect.getsource(worker._premarket_carry_should_fire)
    assert "scheduled_entry_supported" in sched, "예약매수에 모드 게이트가 없다"
    assert ("time_window_2_filter_enabled" in carry
            or "scheduled_entry_supported" in carry), "승계 게이트가 사라졌다"


# ══════════════════════════════════════════════════════════════════════════
# 7. 환경변수 스위치 (2026-09-16 사용자 결정)
#    MACD2_SCHEDULED_ENTRY_ALLOW_IN_3SLOT — 기본 0(차단), 1이면 허용.
#    **1차 방어만** 제어한다. 2~4차 방어는 값과 무관하게 항상 유지.
# ══════════════════════════════════════════════════════════════════════════
class TestEnvSwitch:
    def test_default_is_off(self):
        assert config.SCHEDULED_ENTRY_ALLOW_IN_3SLOT is False, \
            "기본값이 차단(False)이 아니다"

    @pytest.mark.parametrize("flag,mode", _3SLOT_MODES)
    def test_off_blocks_arm_and_order_in_every_3slot_mode(self, flag, mode):
        state_store.save_state(_mode_state(flag))
        res = Macd2Service().arm_scheduled_entry("DOWN_BLUE", changed_by="ui")
        assert res.get("ok") is False
        s = state_store.load_state()
        assert s.scheduled_entry_armed_direction is None, "arm 이 생겼다"
        s.scheduled_entry_armed_direction = Direction.DOWN_BLUE      # 손으로 심어도
        now = datetime(2026, 9, 16, 9, 3, 5, tzinfo=KST)
        assert worker._scheduled_entry_should_fire(s, now) is False, "주문이 나간다"

    @pytest.mark.parametrize("flag,mode", _3SLOT_MODES)
    def test_on_restores_the_feature_in_3slot_modes(self, flag, mode, monkeypatch):
        monkeypatch.setattr(config, "SCHEDULED_ENTRY_ALLOW_IN_3SLOT", True)
        state_store.save_state(_mode_state(flag))
        res = Macd2Service().arm_scheduled_entry("DOWN_BLUE", changed_by="ui")
        assert res.get("ok") is True, f"{mode} 에서 스위치를 켰는데 arm 이 거부됐다"
        s = state_store.load_state()
        assert s.scheduled_entry_armed_direction == Direction.DOWN_BLUE,             "스위치를 켰는데 복원에서 arm 이 지워졌다"
        # arm 은 '오늘' 걸린 것이므로 stale 무효화에 걸리지 않는다
        now = datetime.now(KST).replace(hour=9, minute=3, second=5)
        assert worker._scheduled_entry_should_fire(s, now) is True

    @pytest.mark.parametrize("flag,mode", _3SLOT_MODES)
    def test_stale_arm_is_invalidated_even_with_the_switch_on(self, flag, mode, monkeypatch):
        """'stale 복원 무효화' 는 스위치와 무관하게 항상 동작한다 --
        예약은 그것을 건 날에만 유효하다."""
        monkeypatch.setattr(config, "SCHEDULED_ENTRY_ALLOW_IN_3SLOT", True)
        s = _mode_state(flag)
        s.scheduled_entry_armed_direction = Direction.DOWN_BLUE
        s.scheduled_entry_armed_at = datetime(2026, 9, 10, 8, 5, tzinfo=KST).isoformat()
        state_store.save_state(s)
        assert state_store.load_state().scheduled_entry_armed_direction is None,             f"{mode} 에서 엿새 전 예약이 되살아났다"

    def test_switch_does_not_affect_non_3slot_strategies(self, monkeypatch):
        """TW2 는 스위치 값과 무관하게 항상 쓸 수 있어야 한다."""
        for value in (False, True):
            monkeypatch.setattr(config, "SCHEDULED_ENTRY_ALLOW_IN_3SLOT", value)
            assert tw3.scheduled_entry_supported(_tw2_state()) is True

    # ── 스위치를 켜도 2~4차 방어는 그대로 ────────────────────────────────
    def test_2nd_line_macd_recheck_still_applies_when_switch_is_on(self, monkeypatch):
        monkeypatch.setattr(config, "SCHEDULED_ENTRY_ALLOW_IN_3SLOT", True)
        from tests.macd2.test_scheduled_entry_macd_recheck import (
            _Snap, _broker, _fire_at)
        from tests.macd2.test_early_take_profit_worker import _market

        svc, now0 = _market()
        now = now0.replace(hour=9, minute=3)
        s = _mode_state("time_window_h50_filter_enabled")
        s.auto_trade_on = True
        s.scheduled_entry_armed_direction = Direction.DOWN_BLUE
        s.scheduled_entry_armed_at = now.replace(hour=8, minute=5).isoformat()
        s.session_date = now.strftime("%Y%m%d")
        broker = _broker()

        out = _fire_at(s, broker, svc, now, _Snap(+0.9, now.replace(minute=0)))
        assert out is None and not broker.orders, \
            "스위치를 켰다고 MACD 재확인(2차 방어)까지 풀렸다"
        assert s.scheduled_entry_last_result == config.SCHEDULED_ENTRY_MACD_STATE_FLIPPED

    @pytest.mark.parametrize("flag,mode", _3SLOT_MODES)
    def test_3rd_line_stale_restore_invalidation_when_switch_is_off(self, flag, mode):
        """스위치 OFF 면 과거 arm 은 복원되지 않는다(3차 방어)."""
        s = _mode_state(flag)
        s.scheduled_entry_armed_direction = Direction.DOWN_BLUE
        s.scheduled_entry_armed_at = datetime.now(KST).replace(hour=8, minute=5).isoformat()
        state_store.save_state(s)
        assert state_store.load_state().scheduled_entry_armed_direction is None

    def test_4th_line_arm_expiry_applies_regardless_of_the_switch(self, monkeypatch):
        """타일(他日) arm 만료는 스위치/모드/rollover 분기와 무관하게 항상."""
        for value in (False, True):
            monkeypatch.setattr(config, "SCHEDULED_ENTRY_ALLOW_IN_3SLOT", value)
            for session_date in (None, "20260916", "20260915"):
                s = _tw2_state()
                s.session_date = session_date
                s.scheduled_entry_armed_direction = Direction.DOWN_BLUE
                s.scheduled_entry_armed_at = datetime(
                    2026, 9, 10, 8, 5, tzinfo=KST).isoformat()      # 엿새 전
                worker._apply_day_rollover(s, datetime(2026, 9, 16, 9, 0, tzinfo=KST))
                assert s.scheduled_entry_armed_direction is None, (
                    f"switch={value} session_date={session_date} 에서 과거 arm 이 남았다")
