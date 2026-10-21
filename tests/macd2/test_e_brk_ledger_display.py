"""E 돌파 체결행 표시시각 (2026-10-21 실사례).

10/21 09:45 DOWN_BLUE: 09:51 T+3 승인 -> E 돌파 대기(trigger 1,297,000) -> 09:57 돌파 ->
09:58:30 인버스 체결. 체결행(``...:E_BRK``)이 체결을 처리한 tick 의 최신 봉(09:54)으로
찍혀 화면에 "09:54 -> 09:57 플래그 DOWN_BLUE" 로, 즉 **새 플래그처럼** 보였다
(그 시각 MACD diff 는 −3,005 로 교차 없음). T+3 확인행에 이미 쓰던 규칙(10/13)을
E 돌파 체결행에도 적용한다 -- 표시 전용, 주문/판정 무관.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.trading.macd2 import config, e_strategy, ledger, order_executor, state_store, worker
from app.trading.macd2.models import Direction, MacdSnapshot, SignalState

KST = config.KST
FLAG = datetime(2026, 10, 21, 9, 45, tzinfo=KST)
_APP_PATH = Path(__file__).parent.parent.parent / "app" / "ui" / "pages" / "11_MACD_자동매매2.py"


@pytest.fixture(scope="module")
def ui_page():
    spec = importlib.util.spec_from_file_location("macd2_ui_page_ebrk_under_test", _APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["macd2_ui_page_ebrk_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _snap(bar_dt):
    return MacdSnapshot(bar_dt=bar_dt, macd=-1.0, signal=0.5, hist=-3005.0,
                        hist_last3=(-2396.3, -3004.9, -3769.3), completed_3m_count=100,
                        previous_diff=-2396.3, current_diff=-3004.9)


def _record(sid, snap_bar, signal_type="INITIAL", state=SignalState.EXECUTED):
    st = state_store.default_state()
    out = order_executor.ExecutionOutcome(sid, Direction.DOWN_BLUE, config.INVERSE_SYMBOL, state)
    worker._record_signal_ledger(st, _snap(snap_bar), Direction.DOWN_BLUE, signal_type, sid,
                                 FLAG + timedelta(minutes=3, seconds=3), out)
    return [r for r in ledger.load_signal_ledger(limit=1000) if r["signal_id"] == sid][-1]


@pytest.mark.parametrize("approval_id", [
    "20261021_094500_DOWN_BLUE:TW2_3SLOT_CONFIRM",      # 3-SLOT 승인 id 기반
    "20261021_094500_DOWN_BLUE",                         # 플래그 id 기반
])
def test_breakout_fill_row_is_stamped_with_the_original_flag_bar(approval_id):
    sid = e_strategy.breakout_signal_id({"signal_id": approval_id})
    assert sid.endswith(":E_BRK")
    row = _record(sid, FLAG + timedelta(minutes=9))           # 체결 처리 tick 의 최신 봉 = 09:54
    assert row["completed_bar_at"] == "094500", "09:54 가 아니라 원래 플래그 09:45"
    assert row["signal_bar_at"].startswith("2026-10-21T09:45:00")
    assert row["signal_confirmed_at"].startswith("2026-10-21T09:51:00")    # T+3 승인 시점
    assert row["time_window_flag_bar_at"].startswith("2026-10-21T09:45:00")
    assert row["time_window_confirm_bar_at"].startswith("2026-10-21T09:48:00")


def test_ordinary_rows_are_unchanged():
    row = _record("20261021_095400_DOWN_BLUE", FLAG + timedelta(minutes=9))
    assert row["completed_bar_at"] == "095400"
    assert row["signal_confirmed_at"].startswith("2026-10-21T09:57:00")
    # 10/13 T+3 확인행 규칙도 그대로
    row = _record("20261021_094500_DOWN_BLUE:TW2_3SLOT_CONFIRM", FLAG + timedelta(minutes=6),
                  signal_type="TW2_3SLOT_CONFIRM", state=SignalState.BLOCKED)
    assert row["completed_bar_at"] == "094500"
    assert row["time_window_confirm_bar_at"].startswith("2026-10-21T09:51:00")


def test_screen_shows_breakout_fill_not_a_new_flag(ui_page):
    sid = "20261021_094500_DOWN_BLUE:TW2_3SLOT_CONFIRM:E_BRK"
    row = _record(sid, FLAG + timedelta(minutes=9))
    row["order_requested_at"] = "2026-10-21T09:58:30+09:00"
    timeline = ui_page._signal_timeline_rows([row])
    assert timeline[0]["시각"] == "09:45:00 -> 09:51:00"
    assert timeline[0]["내용"] == "돌파 체결(E) DOWN_BLUE"
    assert timeline[1]["시각"] == "09:58:30"                     # 실제 체결 시각은 주문 행에
    assert ui_page._signal_label({"signal_id": "20261021_094500_DOWN_BLUE:TW2_3SLOT_CONFIRM",
                                  "signal_type": "INITIAL"}) == "재확인(T+3) 승인"


def test_flag_event_overview_no_longer_gets_a_phantom_0954_event():
    """플래그 이벤트 대조(원장 vs 재계산)는 접미사를 뗀 기본 id 로 맞춘다. 체결행 시각이
    09:54 로 찍히면 09:45 플래그와 시각이 어긋난 이벤트가 생길 수 있었다."""
    sid = "20261021_094500_DOWN_BLUE:TW2_3SLOT_CONFIRM:E_BRK"
    row = _record(sid, FLAG + timedelta(minutes=9))
    assert worker.base_signal_id(sid) == "20261021_094500_DOWN_BLUE"
    assert worker._ledger_row_bar_at(row).startswith("2026-10-21T09:45:00")
