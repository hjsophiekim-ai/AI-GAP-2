"""재시작 후 확인시한 지난 T+3 후보 / 확인행 표시시각 (2026-10-13 실사례).

10/13: 08:00 DOWN_BLUE 플래그의 T+3 확인이 재시작(08:40) 뒤 첫 tick 에서 08:36 봉으로
처리돼 원장에 "08:36 BLUE" 처럼 보이는 확인행이 남았다(REJECT_NOT_CONFIRMED, 주문 없음).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.trading.macd2 import config, ledger, order_executor, state_store, worker
from app.trading.macd2.models import Direction, MacdSnapshot, PositionSnapshot, SignalState

KST = config.KST
FLAG = datetime(2026, 10, 13, 8, 0, tzinfo=KST)


def _snap(bar_dt):
    return MacdSnapshot(bar_dt=bar_dt, macd=-1.0, signal=0.5, hist=-1.5,
                        hist_last3=(0.2, 0.1, -1.5), completed_3m_count=100,
                        previous_diff=0.1, current_diff=-1.5)


def _bars(last_hhmm: str):
    h, m = map(int, last_hhmm.split(":"))
    end = datetime(2026, 10, 13, h, m, tzinfo=KST)
    ts = pd.date_range(FLAG - timedelta(minutes=9), end, freq="3min")
    return pd.DataFrame({"datetime": ts, "close": 1.0}), list(range(3, len(ts)))   # 오늘 봉 = 08:00~


def test_confirmation_row_shows_the_original_flag_bar_time():
    st = state_store.default_state()
    sid = "20261013_080000_DOWN_BLUE:TW2_3SLOT_CONFIRM"
    out = order_executor.ExecutionOutcome(sid, Direction.DOWN_BLUE, config.INVERSE_SYMBOL,
                                          SignalState.BLOCKED, block_reason="REJECT_NOT_CONFIRMED")
    worker._record_signal_ledger(st, _snap(FLAG + timedelta(minutes=36)), Direction.DOWN_BLUE,
                                 "TW2_3SLOT_CONFIRM", sid, FLAG + timedelta(minutes=40), out)
    row = ledger.load_signal_ledger()[-1]
    assert row["completed_bar_at"] == "080000"                     # 08:36 이 아니라 플래그 08:00
    assert row["signal_bar_at"].startswith("2026-10-13T08:00:00")
    assert row["time_window_confirm_bar_at"].startswith("2026-10-13T08:36:00")   # 처리 봉은 따로 남김
    # 확인행이 아닌 일반 플래그 행은 그대로
    sid2 = "20261013_084500_UP_RED"
    out2 = order_executor.ExecutionOutcome(sid2, Direction.UP_RED, config.LONG_SYMBOL, SignalState.BLOCKED)
    worker._record_signal_ledger(st, _snap(FLAG + timedelta(minutes=45)), Direction.UP_RED,
                                 "INITIAL", sid2, FLAG + timedelta(minutes=48), out2)
    assert ledger.load_signal_ledger()[-1]["completed_bar_at"] == "084500"


def test_restart_flat_expired_t3_candidate_is_closed_not_re_pended():
    st = state_store.default_state()
    bars, today = _bars("08:36")                                    # 재시작 시 최신 완성봉 08:36
    flag_snap = _snap(FLAG)
    assert worker._restart_t3_candidate_expired(st, bars, today, flag_snap) is True
    worker._close_expired_restart_candidate(st, flag_snap, Direction.DOWN_BLUE,
                                            FLAG + timedelta(minutes=40), suffix="TW2_3SLOT_CONFIRM")
    assert st.tw2_3slot_pending_flag_direction is None              # pending 으로 올리지 않음
    sid = "20261013_080000_DOWN_BLUE:TW2_3SLOT_CONFIRM"
    assert sid in st.processed_signal_ids                           # 이후 같은 플래그 확인행 중복 없음
    row = ledger.load_signal_ledger()[-1]
    assert row["signal_id"] == sid and row["block_reason"] == worker.RESTART_T3_EXPIRED
    assert row["completed_bar_at"] == "080000"
    # 두 번째 재시작에도 원장 행이 늘지 않는다
    n = len(ledger.load_signal_ledger())
    worker._close_expired_restart_candidate(st, flag_snap, Direction.DOWN_BLUE,
                                            FLAG + timedelta(minutes=50), suffix="TW2_3SLOT_CONFIRM")
    assert len(ledger.load_signal_ledger()) == n


def test_restart_keeps_candidate_when_still_in_window_or_holding():
    st = state_store.default_state()
    bars, today = _bars("08:03")                                    # 플래그 바로 다음 봉 -> 아직 확인 전
    assert worker._restart_t3_candidate_expired(st, bars, today, _snap(FLAG)) is False
    bars, today = _bars("08:36")
    st.position = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=10, avg_price=15000.0)
    # 보유 중이면 기존대로 후보를 올린다 -- 반대신호 청산/휩쏘 보류 판단 불변
    assert worker._restart_t3_candidate_expired(st, bars, today, _snap(FLAG)) is False
