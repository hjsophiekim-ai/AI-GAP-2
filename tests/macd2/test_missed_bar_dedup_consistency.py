"""missed-bar 복구 후 dedup 상태 일관성 (2026-10-20 실사고 merge 전 엄격 검증).

왜 이 파일이 따로 있나
--------------------
``test_history_stale_flag_loss.py`` 는 "신호 피드가 멎으면 플래그가 사라진다"는
**사고 자체**를 고정한다. 이 파일은 그 다음 질문만 본다 -- **피드가 돌아온 뒤
상태가 진짜로 일관되게 복구되는가.** 여기가 틀리면 사고가 한 번 나고 끝이
아니라 그날 남은 플래그가 전부 연쇄로 죽는다(2026-08-31 실사고의 형태).

깨지면 안 되는 성질 9가지 (사용자 지정, merge 게이트)
  1. UP_RED 보유 상태에서 history updater 가 stale 이 된다
  2. 그 사이 진짜 DOWN_BLUE 봉이 완성된다
  3. 복구 후 missed-bar replay 가 그 봉을 따라잡는다
  4. DOWN_BLUE 가 원장에 **정확히 1번** 기록된다
  5. ``last_detected_direction`` 이 **실제 최신 방향**과 일치한다
  6. 이후 진짜 UP_RED 가 false repeat 로 HOLD 되지 않는다
  7. 같은 플래그로 중복주문 0
  8. replay 가 주문을 **소급해서 내지 않는다**
  9. 재시작 후에도 dedup 상태가 일관된다

5번이 이 파일의 핵심이다. ``_replay_unevaluated_completed_bars`` 는 봉이 hwm
보다 **뒤**면 ``_advance_confirmed_primary`` 로 상태를 전진시키고, **앞**이면
(= 구멍이 뒤늦게 메워진 경우) 원장만 복구하고 상태는 건드리지 않는다
(2026-09-16 실사고 방지). 피드 정지는 전자에 해당하므로 상태가 따라와야 하며,
안 따라오면 6번이 깨진다.

전략 로직/플래그 정의는 건드리지 않는다 -- 전부 기존 production 함수를 그대로
태운 관측이다.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, state_store, worker
from app.trading.macd2.market_data import (
    MarketDataService,
    filter_complete_3m_bars,
    resample_completed_3m,
)
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from app.trading.macd2.signal_engine import (
    calculate_macd,
    evaluate_macd_crossover,
    exclude_preopen_padding_1m,
    make_signal_id,
)
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST
_DAY = datetime(2026, 10, 20, 9, 0, tzinfo=KST)
_BOOTSTRAP_NOW = _DAY + timedelta(days=2)


# ── harness ────────────────────────────────────────────────────────────────

def _sine_1m_closes(n: int, amplitude: float = 20.0) -> list[float]:
    period = max(n // 2, 1)
    return [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4) for i in range(n)]


_FULL_1M = pd.DataFrame([
    {"datetime": _DAY + timedelta(minutes=i), "open": c, "high": c + 0.1,
     "low": c - 0.1, "close": c, "volume": 100 + (i % 7) * 10}
    for i, c in enumerate(_sine_1m_closes(300))
])


def _completed_bars(df_1m: pd.DataFrame, now: datetime) -> pd.DataFrame:
    sig_1m, _ = exclude_preopen_padding_1m(df_1m)
    bars = resample_completed_3m(sig_1m, now=now)
    bars, _dropped = filter_complete_3m_bars(bars, sig_1m)
    return bars


def _truth_flags(now: datetime) -> list[tuple[datetime, Direction]]:
    bars = _completed_bars(_FULL_1M, now)
    out, last = [], None
    for i in range(1, len(bars) + 1):
        snap = calculate_macd(bars.iloc[:i])
        if snap is None:
            continue
        d = evaluate_macd_crossover(snap, last)
        if d != Direction.HOLD:
            last = d
            out.append((snap.bar_dt, d))
    return out


#: 11:03 UP_RED / 12:21 DOWN_BLUE / 13:36 UP_RED -- 실사고
#: (11:45 UP_RED / 12:06 DOWN_BLUE / 13:36 UP_RED)와 같은 구조.
_FLAGS = _truth_flags(_DAY + timedelta(minutes=300))
PRIOR_BAR, PRIOR_DIR = _FLAGS[0]
LOST_BAR, LOST_DIR = _FLAGS[1]
NEXT_BAR, NEXT_DIR = _FLAGS[2]


def _assert_scenario_shape():
    assert PRIOR_DIR == Direction.UP_RED, _FLAGS
    assert LOST_DIR == Direction.DOWN_BLUE, _FLAGS
    assert NEXT_DIR == Direction.UP_RED, _FLAGS


_assert_scenario_shape()


def _now_after(bar_dt: datetime) -> datetime:
    return bar_dt + timedelta(minutes=3, seconds=3)


def _service(df_1m: pd.DataFrame) -> MarketDataService:
    quotes = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0,
              config.WATCH_SYMBOL: 100.0}
    svc = MarketDataService(
        mode="mock",
        fetch_minute_candles=lambda *a, **k: (df_1m, {}),
        fetch_quote=lambda mode, symbol: (quotes.get(symbol), None),
    )
    result = svc.bootstrap(now=_BOOTSTRAP_NOW)
    assert result.ok, result.reason
    svc.refresh_quotes()
    return svc


class _StaleHistoryView:
    """history updater 가 멈춰 프레임이 전진하지 않는 상태."""

    def __init__(self, svc: MarketDataService, frozen: pd.DataFrame) -> None:
        self._svc = svc
        self._frozen = frozen.reset_index(drop=True)

    def get_history_df(self) -> pd.DataFrame:
        return self._frozen.copy()

    def __getattr__(self, name):
        return getattr(self._svc, name)


def _state() -> RuntimeState:
    s = state_store.default_state()
    s.strategy_version = config.STRATEGY_VERSION
    s.signal_rule = config.SIGNAL_RULE
    s.auto_trade_on = True
    s.budget = 10_000_000.0
    s.time_window_2_filter_enabled = False
    s.time_window_teg_filter_enabled = False
    s.time_window_3slot_filter_enabled = True
    return s


def _broker() -> FakeBroker:
    return FakeBroker(cash=10_000_000.0,
                      quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})


def _rows(direction: Direction | None = None) -> list[dict]:
    rows = ledger.load_signal_ledger(limit=2000)
    if direction is None:
        return rows
    return [r for r in rows if str(r.get("direction") or "") == direction.value]


def _rows_for_bar(bar_dt: datetime, direction: Direction) -> list[dict]:
    """그 **봉**의 플래그 행만. signal_id 가 봉 시각에서 파생되므로 그걸 쓴다."""
    sid = make_signal_id(bar_dt, direction)
    return [r for r in _rows() if str(r.get("signal_id") or "") == sid]


def _seed_up_red_holding(state: RuntimeState, broker: FakeBroker, now: datetime) -> None:
    """① UP_RED 플래그가 정상 기록돼 롱을 보유 중인 상태.

    replay 의 floor 는 "평가 여부를 아는 가장 오래된 봉"이므로, 피드가 멎기
    전까지 정상 평가했다는 사실을 PRIOR 봉으로 심어 둔다 -- 이게 없으면
    replay 가 하루치를 통째로 되감는지 여부 자체를 검증할 수 없다."""
    state.last_detected_direction = PRIOR_DIR
    state.latest_primary_flag = PRIOR_DIR
    state.latest_primary_signal_id = make_signal_id(PRIOR_BAR, PRIOR_DIR)
    state.last_confirmed_bar_ts = PRIOR_BAR.isoformat()
    state.evaluated_bar_ts_today = [PRIOR_BAR.isoformat()]
    state.position = PositionSnapshot(
        symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0, entry_at=now)
    state.time_window_position_active = True
    state.time_window_active_mode = "TW2_3SLOT"
    state.time_window_entry_session = "MORNING"
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")
    broker._positions[config.LONG_SYMBOL].avg_price = 15_000.0


# ══════════════════════════════════════════════════════════════════════════
# 전 과정 1~8 을 한 흐름으로 -- 중간에 끊으면 "복구됐는데 상태는 과거"라는
# 가장 위험한 조합을 놓친다.
# ══════════════════════════════════════════════════════════════════════════

def test_missed_bar_recovery_keeps_dedup_state_consistent_end_to_end():
    state, broker = _state(), _broker()
    stale_now = _now_after(LOST_BAR)
    _seed_up_red_holding(state, broker, PRIOR_BAR)
    orders_after_seed = len(broker.orders)

    # ①② 피드 정지 중 진짜 DOWN_BLUE 봉이 완성된다
    stale_svc = _StaleHistoryView(_service(_FULL_1M), _FULL_1M[_FULL_1M["datetime"] < LOST_BAR])
    run_once(broker=broker, market_data=stale_svc, state=state, now=stale_now)
    assert not _rows_for_bar(LOST_BAR, LOST_DIR), "전제 실패: stale 인데 플래그가 남았다"
    assert state.last_detected_direction == PRIOR_DIR
    assert state.order_block_reason == config.HISTORY_STALE_BLOCK_REASON

    # ③ 복구 -- replay 가 따라잡는다
    ok_svc = _service(_FULL_1M)
    run_once(broker=broker, market_data=ok_svc, state=state, now=stale_now)

    # ④ 정확히 1번
    assert len(_rows_for_bar(LOST_BAR, LOST_DIR)) == 1, _rows_for_bar(LOST_BAR, LOST_DIR)

    # ⑤ 실제 최신 방향과 일치 -- 이게 핵심이다
    assert state.last_detected_direction == LOST_DIR, (
        "복구는 됐는데 방향상태가 과거에 머물렀다 -- 이후 같은방향 플래그가 "
        "전부 false repeat 로 죽는다")

    # ⑧ replay 는 주문을 소급해 내지 않는다
    assert len(broker.orders) == orders_after_seed, (
        f"replay 가 주문을 냈다: {broker.orders[orders_after_seed:]}")

    # ⑦ 같은 tick 을 반복해도 행/주문이 늘지 않는다
    for _ in range(3):
        run_once(broker=broker, market_data=ok_svc, state=state, now=stale_now)
    assert len(_rows_for_bar(LOST_BAR, LOST_DIR)) == 1, "중복 원장행이 생겼다"
    assert len(broker.orders) == orders_after_seed, "중복 주문이 나갔다"

    # ⑥ 이후 진짜 UP_RED 가 false repeat 로 죽지 않는다
    next_now = _now_after(NEXT_BAR)
    run_once(broker=broker, market_data=ok_svc, state=state, now=next_now)
    assert _rows_for_bar(NEXT_BAR, NEXT_DIR), (
        f"{NEXT_BAR:%H:%M} {NEXT_DIR.value} 가 false repeat 로 삼켜졌다")
    assert state.last_detected_direction == NEXT_DIR


def test_recovered_flag_is_recorded_as_blocked_never_as_an_order():
    """⑧ 보강: 복구된 과거 봉의 원장 행은 주문이 아니라 차단 기록이어야 한다.

    ``_record_catchup_flag`` 는 BLOCKED 행만 쓴다 -- 진입시각/T+3 유효시간이
    지난 플래그로 뒤늦게 주문이 나가는 일은 구조적으로 불가능해야 한다."""
    state, broker = _state(), _broker()
    now = _now_after(LOST_BAR)
    _seed_up_red_holding(state, broker, PRIOR_BAR)
    base_orders = len(broker.orders)

    stale_svc = _StaleHistoryView(_service(_FULL_1M), _FULL_1M[_FULL_1M["datetime"] < LOST_BAR])
    run_once(broker=broker, market_data=stale_svc, state=state, now=now)
    run_once(broker=broker, market_data=_service(_FULL_1M), state=state, now=now)

    row = _rows_for_bar(LOST_BAR, LOST_DIR)[0]
    result = str(row.get("order_result") or row.get("result") or "")
    assert "EXECUTED" not in result.upper(), row
    assert len(broker.orders) == base_orders


def test_no_duplicate_rows_when_both_restart_walk_and_replay_see_the_same_bar():
    """⑨ 재시작 후에도 dedup 이 일관된다.

    같은 플래그를 재시작 catch-up walk 와 replay 가 둘 다 보더라도 원장 행은
    하나뿐이어야 한다(``ledger.append_signal`` 의 signal_id 평생 dedup)."""
    state, broker = _state(), _broker()
    now = _now_after(LOST_BAR)
    _seed_up_red_holding(state, broker, PRIOR_BAR)
    base_orders = len(broker.orders)

    stale_svc = _StaleHistoryView(_service(_FULL_1M), _FULL_1M[_FULL_1M["datetime"] < LOST_BAR])
    run_once(broker=broker, market_data=stale_svc, state=state, now=now)

    ok_svc = _service(_FULL_1M)
    run_once(broker=broker, market_data=ok_svc, state=state, now=now)
    assert len(_rows_for_bar(LOST_BAR, LOST_DIR)) == 1

    # 재시작 -- 같은 날, 같은 프레임을 다시 걷는다
    worker.initialize_strategy_session(market_data=ok_svc, state=state, now=now)
    assert len(_rows_for_bar(LOST_BAR, LOST_DIR)) == 1, "재시작이 중복 행을 만들었다"

    # 재시작 뒤 tick 도 마찬가지
    run_once(broker=broker, market_data=ok_svc, state=state, now=now)
    assert len(_rows_for_bar(LOST_BAR, LOST_DIR)) == 1
    assert len(broker.orders) == base_orders, "재시작 경로가 주문을 냈다"


def test_restart_during_the_stall_still_ends_up_on_the_real_latest_direction():
    """⑨ 보강: 정지 **중에** 재시작돼도 복구 후 방향상태가 실제와 맞아야 한다.

    2026-08-06 실사고(30분 안에 worker_instance_id 6개)처럼 불안정한 호스트는
    정지 구간 한가운데서 프로세스를 갈아끼운다."""
    state, broker = _state(), _broker()
    now = _now_after(LOST_BAR)
    _seed_up_red_holding(state, broker, PRIOR_BAR)

    stale_svc = _StaleHistoryView(_service(_FULL_1M), _FULL_1M[_FULL_1M["datetime"] < LOST_BAR])
    run_once(broker=broker, market_data=stale_svc, state=state, now=now)
    # 정지 구간에서 재시작 -- 아직 프레임은 멎어 있다
    worker.initialize_strategy_session(market_data=stale_svc, state=state, now=now)

    # 피드 복구
    ok_svc = _service(_FULL_1M)
    worker.initialize_strategy_session(market_data=ok_svc, state=state, now=now)
    run_once(broker=broker, market_data=ok_svc, state=state, now=now)

    assert state.last_detected_direction == LOST_DIR, (
        "정지 중 재시작을 거치면 방향상태가 실제와 어긋난다")
    assert len(_rows_for_bar(LOST_BAR, LOST_DIR)) == 1


def test_replay_never_rewinds_direction_state_behind_the_high_water_mark():
    """2026-09-16 방지선: hwm 보다 **앞선** 봉이 뒤늦게 메워져도 방향상태를
    과거로 되돌리지 않는다. 위 시나리오들과 반대 방향의 안전장치다."""
    state, broker = _state(), _broker()
    now = _now_after(NEXT_BAR)
    _seed_up_red_holding(state, broker, PRIOR_BAR)
    # 이미 NEXT 봉까지 평가한 상태로 만든 뒤, 중간 LOST 봉만 미평가로 남긴다
    state.last_detected_direction = NEXT_DIR
    state.last_confirmed_bar_ts = NEXT_BAR.isoformat()
    state.evaluated_bar_ts_today = [PRIOR_BAR.isoformat(), NEXT_BAR.isoformat()]

    run_once(broker=broker, market_data=_service(_FULL_1M), state=state, now=now)

    assert state.last_detected_direction == NEXT_DIR, (
        "뒤늦게 메워진 과거 봉이 현재 방향을 덮어썼다 -- 2026-09-16 사고 재발")
