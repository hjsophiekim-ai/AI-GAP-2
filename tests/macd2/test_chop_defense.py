"""CHOP-STACK (왕복장 방어모드) 단위 테스트 — 2026-10-11.

연구: research_20261011b_chop_stack.

잠그는 계약은 네 가지다.
  1. CHOP 모드가 아니면 세 규칙 전부 **no-op** (P3/N1 동작 불변)
  2. 2슬롯 캡은 모드로만 바뀐다
  3. MC-DELAY 는 방향전환 2회 + 왕복 경로가 **둘 다** 성립해야 켜지고,
     켜진 뒤에는 10:30 이후 돌파확인(MACD gap 포함)에만 풀린다 — 판정은 완성봉마다
  4. 구조손절은 박스 확정이탈/회복/타임아웃에서 즉시 집행, 그 외에만 유예
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from app.trading.macd2 import chop_defense, config, strategy_mode
from app.trading.macd2.models import Direction, RuntimeState

KST = config.KST


def _state(mode: str) -> RuntimeState:
    st = RuntimeState()
    strategy_mode.apply(st, mode)
    return st


def _bars(n: int, *, start: str = "09:00", base: float = 1_700_000.0,
          amp: float = 6_000.0, period: int = 9, trend: float = 0.0) -> pd.DataFrame:
    """톱니(왕복) 1분봉. ``trend`` 를 주면 한 방향으로 끌고 간다."""
    t0 = dt.datetime(2026, 10, 8, int(start[:2]), int(start[3:]), tzinfo=KST)
    rows = []
    for i in range(n):
        px = base + amp * np.sin(2 * np.pi * i / period) + trend * i
        rows.append(dict(datetime=t0 + dt.timedelta(minutes=i), open=px,
                         high=px + 500.0, low=px - 500.0, close=px, volume=1))
    return pd.DataFrame(rows)


def _flag(st, hhmm: str, direction: Direction) -> None:
    chop_defense.note_morning_flag(
        st, dt.datetime(2026, 10, 8, int(hhmm[:2]), int(hhmm[2:]), tzinfo=KST), direction)


# ── 1. 비활성 모드에서는 전부 no-op ──────────────────────────────────────
@pytest.mark.parametrize("mode", [strategy_mode.MODE_N1, strategy_mode.MODE_P3])
def test_inactive_modes_are_untouched(mode):
    st = _state(mode)
    now = dt.datetime(2026, 10, 8, 10, 10, tzinfo=KST)
    df = _bars(80)
    for hhmm, d in (("0924", Direction.UP_RED), ("0936", Direction.DOWN_BLUE),
                    ("1012", Direction.UP_RED)):
        _flag(st, hhmm, d)
    chop_defense.note_bars(st, now, df)

    assert chop_defense.is_active(st) is False
    assert chop_defense.daily_cap(st) == int(config.TW2_3SLOT_DAILY_CAP)
    assert chop_defense.morning_no_trade(st, now, df) is False
    assert chop_defense.b3_stop_action(
        st, now=now, entry_at=now - dt.timedelta(minutes=10),
        net_return_pct=-1.05, direction=Direction.UP_RED) == chop_defense.STOP_EXECUTE
    # note_bars 는 비활성이면 캐시조차 만들지 않는다
    assert chop_defense.cached_box(st) is None


# ── 2. 슬롯 캡 ───────────────────────────────────────────────────────────
def test_daily_cap_is_mode_driven():
    assert chop_defense.daily_cap(_state(strategy_mode.MODE_P3)) == int(config.TW2_3SLOT_DAILY_CAP)
    assert chop_defense.daily_cap(_state(strategy_mode.MODE_CHOP)) == int(config.CHOP_DAILY_CAP)
    assert int(config.CHOP_DAILY_CAP) == 2


# ── 3. MC-DELAY ──────────────────────────────────────────────────────────
def test_mc_needs_both_reversals_and_chop_path():
    """ON 은 승인 시점이 아니라 **완성봉마다** 판정한다 (연구 mc1.py, 2026-10-12)."""
    now = dt.datetime(2026, 10, 8, 10, 10, tzinfo=KST)
    chop_df = _bars(80)                       # 왕복 경로 09:00~
    trend_df = _bars(80, amp=200.0, trend=800.0)   # 한 방향 추세

    # (a) 방향전환이 1회뿐이면 켜지지 않는다
    st = _state(strategy_mode.MODE_CHOP)
    _flag(st, "0924", Direction.UP_RED)
    _flag(st, "0936", Direction.DOWN_BLUE)
    chop_defense.advance_mc(st, now, chop_df)
    assert chop_defense.morning_no_trade(st, now, chop_df) is False

    # (b) 방향전환 2회라도 경로가 추세면 켜지지 않는다
    st = _state(strategy_mode.MODE_CHOP)
    for hhmm, d in (("0924", Direction.UP_RED), ("0936", Direction.DOWN_BLUE),
                    ("0950", Direction.UP_RED)):
        _flag(st, hhmm, d)
    chop_defense.advance_mc(st, now, trend_df)
    assert chop_defense.morning_no_trade(st, now, trend_df) is False

    # (c) 둘 다 성립하면 **2번째 방향전환 플래그 봉 이후 첫 왕복 봉**에서 켜진다
    st = _state(strategy_mode.MODE_CHOP)
    for hhmm, d in (("0924", Direction.UP_RED), ("0936", Direction.DOWN_BLUE),
                    ("0950", Direction.UP_RED)):
        _flag(st, hhmm, d)
    assert chop_defense.morning_no_trade(st, now, chop_df) is True
    assert "0950" <= st.chop_mc_on_at <= "1009"


def test_mc_does_not_arm_on_bars_after_the_window():
    """ON 판정은 CHOP_MC_WINDOW_END(10:30) 이하 봉만 본다 — 그 뒤 왕복은 무시."""
    st = _state(strategy_mode.MODE_CHOP)
    for hhmm, d in (("0924", Direction.UP_RED), ("0936", Direction.DOWN_BLUE),
                    ("1012", Direction.UP_RED)):
        _flag(st, hhmm, d)
    trend = _bars(91, amp=200.0, trend=800.0)                    # 09:00~10:30 추세
    chop = _bars(30, start="10:31", base=float(trend["close"].iloc[-1]))   # 10:31~ 왕복
    df = pd.concat([trend, chop], ignore_index=True)
    late = dt.datetime(2026, 10, 8, 11, 1, tzinfo=KST)
    assert chop_defense.morning_no_trade(st, late, df) is False
    assert st.chop_mc_on_at is None


def test_mc_releases_on_one_directional_breakout_with_macd_gap():
    st = _state(strategy_mode.MODE_CHOP)
    for hhmm, d in (("0924", Direction.UP_RED), ("0936", Direction.DOWN_BLUE),
                    ("0950", Direction.UP_RED)):
        _flag(st, hhmm, d)
    on_at = dt.datetime(2026, 10, 8, 10, 10, tzinfo=KST)
    chop_df = _bars(80)
    assert chop_defense.morning_no_trade(st, on_at, chop_df) is True

    # 10:30 전에는 돌파가 있어도 풀리지 않는다
    early = dt.datetime(2026, 10, 8, 10, 25, tzinfo=KST)
    up = pd.concat([_bars(70), _bars(30, start="10:10", base=1_760_000.0, amp=300.0, trend=1500.0)],
                   ignore_index=True)
    assert chop_defense.morning_no_trade(st, early, up) is True
    assert st.chop_mc_off_at is None

    # 10:30 이후 한 방향(+MACD gap 같은 방향)으로 밀면 그 첫 봉에서 해제된다.
    later = dt.datetime(2026, 10, 8, 10, 41, tzinfo=KST)
    assert chop_defense.morning_no_trade(st, later, up) is False
    assert "1030" <= st.chop_mc_off_at <= "1039"
    # 한 번 풀리면 그 날은 다시 켜지지 않는다
    assert chop_defense.morning_no_trade(st, later, chop_df) is False


def test_mc_is_evaluated_every_tick_even_without_an_approval(monkeypatch):
    """승인이 없어도 worker 가 매 tick advance_mc 를 부른다 — 소스 계약."""
    import inspect
    from app.trading.macd2 import worker
    src = inspect.getsource(worker.run_once)
    assert "chop_defense.advance_mc(state, now, market_data.get_history_df())" in src


def test_flags_outside_the_morning_window_are_ignored():
    st = _state(strategy_mode.MODE_CHOP)
    _flag(st, "0924", Direction.UP_RED)
    _flag(st, "1100", Direction.DOWN_BLUE)     # 창 밖
    _flag(st, "1300", Direction.UP_RED)        # 창 밖
    assert [f[0] for f in st.chop_mc_flags] == ["0924"]


# ── 4. 구조손절 ──────────────────────────────────────────────────────────
def _armed_state(now, df):
    st = _state(strategy_mode.MODE_CHOP)
    chop_defense.note_bars(st, now, df)
    return st


def test_structural_stop_defers_inside_the_box():
    now = dt.datetime(2026, 10, 8, 10, 10, tzinfo=KST)
    st = _armed_state(now, _bars(80))
    act = chop_defense.b3_stop_action(
        st, now=now, entry_at=now - dt.timedelta(minutes=5),
        net_return_pct=-1.05, direction=Direction.UP_RED)
    assert act == chop_defense.STOP_DEFER


def test_structural_stop_executes_on_recovery():
    now = dt.datetime(2026, 10, 8, 10, 10, tzinfo=KST)
    st = _armed_state(now, _bars(80))
    act = chop_defense.b3_stop_action(
        st, now=now, entry_at=now - dt.timedelta(minutes=5),
        net_return_pct=float(config.CHOP_STOP_RECOVER_PCT), direction=Direction.UP_RED)
    assert act == chop_defense.STOP_EXECUTE


def test_structural_stop_executes_on_timeout_and_maxhold():
    now = dt.datetime(2026, 10, 8, 10, 10, tzinfo=KST)
    df = _bars(80)
    # 유예 타임아웃: 첫 터치로부터 CHOP_STOP_DEFER_MIN 경과
    st = _armed_state(now, df)
    entry = now - dt.timedelta(minutes=5)
    assert chop_defense.b3_stop_action(
        st, now=now, entry_at=entry, net_return_pct=-1.05,
        direction=Direction.UP_RED) == chop_defense.STOP_DEFER
    later = now + dt.timedelta(minutes=float(config.CHOP_STOP_DEFER_MIN))
    chop_defense.note_bars(st, later, df)
    assert chop_defense.b3_stop_action(
        st, now=later, entry_at=entry, net_return_pct=-1.05,
        direction=Direction.UP_RED) == chop_defense.STOP_EXECUTE

    # B3 max-hold: 진입으로부터 P3_B3_MAX_HOLD_MIN 경과
    st2 = _armed_state(now, df)
    old_entry = now - dt.timedelta(minutes=float(config.P3_B3_MAX_HOLD_MIN))
    assert chop_defense.b3_stop_action(
        st2, now=now, entry_at=old_entry, net_return_pct=-1.05,
        direction=Direction.UP_RED) == chop_defense.STOP_EXECUTE


def test_structural_stop_executes_without_bars():
    """박스를 못 만들면 기존 손절 그대로 — fail-safe."""
    now = dt.datetime(2026, 10, 8, 9, 2, tzinfo=KST)
    st = _state(strategy_mode.MODE_CHOP)
    chop_defense.note_bars(st, now, _bars(2))
    assert chop_defense.b3_stop_action(
        st, now=now, entry_at=now - dt.timedelta(minutes=1),
        net_return_pct=-1.05, direction=Direction.UP_RED) == chop_defense.STOP_EXECUTE


def test_structural_stop_executes_on_confirmed_breakdown():
    """보유 반대방향으로 박스를 확정이탈하면 유예하지 않는다."""
    now = dt.datetime(2026, 10, 8, 10, 30, tzinfo=KST)
    # 아래로 쭉 빠지는 경로 — 레버리지(UP) 보유에는 구조적 이탈이다.
    df = _bars(90, amp=200.0, trend=-1500.0)
    st = _armed_state(now, df)
    act = chop_defense.b3_stop_action(
        st, now=now, entry_at=now - dt.timedelta(minutes=5),
        net_return_pct=-1.05, direction=Direction.UP_RED)
    assert act == chop_defense.STOP_EXECUTE


# ── 5. 모드 전환 / 영속 ──────────────────────────────────────────────────
def test_leaving_chop_mode_clears_daily_state():
    st = _state(strategy_mode.MODE_CHOP)
    _flag(st, "0924", Direction.UP_RED)
    st.chop_mc_on_at = "1012"
    strategy_mode.apply(st, strategy_mode.MODE_P3)
    assert st.chop_defense_enabled is False
    assert st.chop_mc_flags == []
    assert st.chop_mc_on_at is None


def test_chop_mode_is_inferable_from_flags():
    """restore() 가 모드 기록을 지우지 않도록 — 재시작에서 P3 로 떨어지면 안 된다."""
    st = _state(strategy_mode.MODE_CHOP)
    mode, reason = strategy_mode.infer_from_legacy(st)
    assert mode == strategy_mode.MODE_CHOP, reason
