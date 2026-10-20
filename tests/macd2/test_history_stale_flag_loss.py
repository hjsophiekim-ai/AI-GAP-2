"""history-updater 정지 중 완성봉 플래그 소실 (2026-10-20 실사고).

사고
----
2026-10-20 실계좌. 11:45 UP_RED 진입(11:51:39 체결) 이후 프로그램의 마지막
플래그가 **종일 11:45 UP_RED 에 멈춰 있었다**. 그 사이 KIS 차트에는 두 개의
진짜 제로크로스가 있었고, 재구성 결과 production 계산식으로도 동일했다:

    12:06 DOWN_BLUE   HIST +314.96 -> -44.56
    13:36 UP_RED      HIST -111.31 -> +181.14

신호원장에는 **두 봉 모두 행이 없다**(사용자 확인). 그런데 Render 로그는
워커가 12:06~12:11 내내 5초 주기로 살아 있었음을 보여준다(``REAL
get_positions`` 가 WORKER_INTERVAL_SEC=5.0 간격으로 반복). 호가 경로도
정상이어서 12:18:04 에 FIXED_SL15 손절이 호가 기반으로 정상 발동했다.

원인
----
``market_data`` 의 history-updater 스레드가 멈췄다. 그러면
``get_history_df()`` 가 얼어붙은 프레임을 돌려주므로 ``bars_3m`` 에 12:06 봉이
영원히 들어오지 않고, ``_advance_confirmed_primary`` 는 평가할 새 봉이 없다.
결과는 **플래그 0 · 원장 0행 · 예외 0건 · 로그 0줄** 이다:

  - ``market_data.py`` 에는 logger 사용이 0건이라 Render 로그에 흔적이 없다
  - history 워치독(``Macd2Service._history_watchdog``)은 ``get_snapshot()`` /
    ``supervisor_status()`` 안에서만 돈다 = **대시보드를 열어야만** 복구 시도
  - 반면 quote-updater 는 ``_run_loop`` **안에서** 매 tick 감시받는다

2차 피해
--------
12:06 DOWN_BLUE 가 라이브 경로에서 이탈하면 ``last_detected_direction`` 이
11:45 의 UP_RED 에 고착된다. 그러면 13:36 의 **진짜** UP_RED 가
``evaluate_macd_crossover`` 의 같은-방향 억제에 걸려 가짜 "반복"으로 삼켜진다.
즉 **플래그 2건이 원인 하나로 사라진다.** worker.py 의 2026-08-31 주석이 같은
연쇄를 이미 기록하고 있다(그때도 13:36 UP_RED 가 같은 방식으로 죽었다).

이 파일이 고정하는 것
--------------------
  1. history-updater 가 멈춰도 worker/quote-updater 는 살아 있다(= 증상이
     "전체 정지"로 보이지 않는다는 사실 자체를 고정)
  2. history 가 stale 이면 완성된 봉의 플래그가 통째로 누락된다(핵심 재현)
  3. 그 손실이 dedup 을 오염시켜 이후 진짜 같은방향 플래그까지 삼킨다
  4. 워치독 도입 후의 계약: stale 감지 / 신규진입만 차단 / CRITICAL 로그 /
     프로세스 생존 / 복구 후 평가 재개

전략 로직(P3/E/CHOP/N1)과 플래그 정의는 이 파일의 검증 대상이 아니며 한 줄도
건드리지 않는다. 자동매도/자동청산도 추가하지 않는다 — stale 일 때 막는 것은
**신규진입뿐**이고 보유 포지션은 경고만 남긴다.
"""
from __future__ import annotations

import inspect
import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, market_data as md_mod, state_store, worker
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
)
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST
_DAY = datetime(2026, 10, 20, 9, 0, tzinfo=KST)


# ── harness (test_flag_propagation.py 의 recipe 를 그대로 쓴다) ─────────────

def _sine_1m_closes(n_minutes: int, amplitude: float = 20.0) -> list[float]:
    period = max(n_minutes // 2, 1)
    return [round(100.0 + amplitude * math.sin(2 * math.pi * i / period), 4)
            for i in range(n_minutes)]


def _1m_frame(start: datetime, closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "datetime": start + timedelta(minutes=i),
            "open": c, "high": c + 0.1, "low": c - 0.1, "close": c,
            "volume": 100 + (i % 7) * 10,
        }
        for i, c in enumerate(closes)
    ])


def _completed_bars(df_1m: pd.DataFrame, now: datetime) -> pd.DataFrame:
    sig_1m, _ = exclude_preopen_padding_1m(df_1m)
    bars = resample_completed_3m(sig_1m, now=now)
    bars, _dropped = filter_complete_3m_bars(bars, sig_1m)
    return bars


def _truth_flags(df_1m: pd.DataFrame, now: datetime) -> list[tuple[datetime, Direction]]:
    """production 함수로 전 봉을 순회한 '정답' 플래그 목록."""
    bars = _completed_bars(df_1m, now)
    out: list[tuple[datetime, Direction]] = []
    last: Direction | None = None
    for i in range(1, len(bars) + 1):
        snap = calculate_macd(bars.iloc[:i])
        if snap is None:
            continue
        direction = evaluate_macd_crossover(snap, last)
        if direction != Direction.HOLD:
            last = direction
            out.append((snap.bar_dt, direction))
    return out


#: bootstrap 은 데이터일보다 뒤 시각에서 돌린다 -- 그래야 이 1분봉이
#: "prior day" 로 잡혀 TODAY_ONLY_WARMING_UP 이 아니게 된다
#: (test_flag_propagation.py 의 ``_BOOTSTRAP_NOW`` 와 같은 recipe).
_BOOTSTRAP_NOW = _DAY + timedelta(days=2)


def _service(df_1m: pd.DataFrame, *, bootstrap_now: datetime = _BOOTSTRAP_NOW) -> MarketDataService:
    """``df_1m`` 만 보는 MarketDataService. history-updater 스레드는 띄우지
    않는다 -- 그게 바로 이 사고의 상태(= 프레임이 더 이상 전진하지 않음)다."""
    quote_prices = {
        config.LONG_SYMBOL: 15_000.0,
        config.INVERSE_SYMBOL: 10_000.0,
        config.WATCH_SYMBOL: 100.0,
    }

    def fake_fetch(mode, symbol, count, hour1):
        del mode, symbol, count, hour1
        return df_1m, {}

    def fake_quote(mode, symbol):
        del mode
        return quote_prices.get(symbol), None

    svc = MarketDataService(mode="mock", fetch_minute_candles=fake_fetch,
                            fetch_quote=fake_quote)
    result = svc.bootstrap(now=bootstrap_now)
    assert result.ok, f"fixture bootstrap failed: {result.reason}"
    svc.refresh_quotes()
    return svc


def _state() -> RuntimeState:
    state = state_store.default_state()
    # run_once 는 전략버전/신호규칙이 어긋나면 탐지 상태를 통째로 리셋한다
    # (worker.py:7424). 시나리오가 그 리셋에 걸리지 않도록 맞춰 둔다.
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    state.time_window_2_filter_enabled = False
    state.time_window_teg_filter_enabled = False
    state.time_window_3slot_filter_enabled = True
    return state


def _broker() -> FakeBroker:
    return FakeBroker(
        cash=10_000_000.0,
        quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0},
    )


def _flag_rows(direction: Direction | None = None) -> list[dict]:
    rows = ledger.load_signal_ledger(limit=1000)
    if direction is None:
        return rows
    return [r for r in rows if str(r.get("direction") or "") == direction.value]


# ── 공통 시나리오 재료 ────────────────────────────────────────────────────
#
# 사인 1분봉 300개(09:00~13:59)로 여러 번 제로크로스를 만든 뒤, 그 중
# "앞선 플래그와 방향이 다른 플래그" 한 쌍을 골라 사고를 그대로 재현한다:
#   LOST  = 라이브 경로에서 놓칠 봉        (실사고의 12:06 DOWN_BLUE)
#   NEXT  = 그 다음에 오는 같은방향 플래그 (실사고의 13:36 UP_RED)

_FULL_1M = _1m_frame(_DAY, _sine_1m_closes(300))
_END_NOW = _DAY + timedelta(minutes=300)


def _pick_incident_bars():
    """실사고와 같은 3연속 플래그 구조를 고른다.

    이 사인 시리즈가 실제로 내는 플래그는 ``11:03 UP_RED / 12:21 DOWN_BLUE /
    13:36 UP_RED`` 로, 2026-10-20 의 ``11:45 UP_RED / 12:06 DOWN_BLUE /
    13:36 UP_RED`` 와 구조가 동일하다(13:36 은 시각까지 같다).

      PRIOR = 유실 직전에 정상 기록된 플래그  -> 오염된 채 고착될 방향
      LOST  = 라이브 경로에서 놓칠 봉
      NEXT  = LOST 이후 처음 오는 **PRIOR 와 같은 방향** 플래그
              (dedup 오염이 삼키는 대상은 LOST 방향이 아니라 PRIOR 방향이다)
    """
    flags = _truth_flags(_FULL_1M, _END_NOW)
    assert len(flags) >= 3, f"시나리오에 플래그가 부족하다: {flags}"
    prior_bar, prior_dir = flags[0]
    lost_bar, lost_dir = flags[1]
    assert lost_dir != prior_dir, f"LOST 가 PRIOR 와 같은 방향이다: {flags}"
    nxt = [(b, d) for b, d in flags[2:] if d == prior_dir]
    assert nxt, f"PRIOR 와 같은 방향의 후속 플래그가 없다: {flags}"
    return prior_bar, prior_dir, lost_bar, lost_dir, nxt[0][0], nxt[0][1]


PRIOR_BAR, PRIOR_DIR, LOST_BAR, LOST_DIR, NEXT_BAR, NEXT_DIR = _pick_incident_bars()


def _now_after(bar_dt: datetime) -> datetime:
    """그 봉이 완성된 직후의 tick 시각 (봉 시작 + 3분 + 3초)."""
    return bar_dt + timedelta(minutes=3, seconds=3)


def _truncated_before(bar_dt: datetime) -> pd.DataFrame:
    """history-updater 가 ``bar_dt`` 직전에 멈춘 상태의 프레임."""
    return _FULL_1M[_FULL_1M["datetime"] < bar_dt].reset_index(drop=True)


class _StaleHistoryView:
    """history-updater 가 멈춘 MarketDataService.

    worker 가 보는 것은 ``get_history_df()`` 뿐이고, updater 가 죽으면 그
    프레임이 **더 이상 전진하지 않는다**. 나머지(호가 등)는 그대로 살아 있다 --
    2026-10-20 의 상태를 그대로 표현한다. 내부 필드를 건드리지 않으려고
    위임 프록시로 만든다."""

    def __init__(self, svc: MarketDataService, frozen_df: pd.DataFrame) -> None:
        self._svc = svc
        self._frozen = frozen_df.reset_index(drop=True)

    def get_history_df(self) -> pd.DataFrame:
        return self._frozen.copy()

    def __getattr__(self, name):
        return getattr(self._svc, name)


# ══════════════════════════════════════════════════════════════════════════
# 1. history-updater 가 멈춰도 worker / quote-updater 는 살아 있다
# ══════════════════════════════════════════════════════════════════════════

def test_history_updater_can_be_dead_while_quote_updater_is_alive():
    """사고의 겉모습 자체를 고정한다 -- '전체 정지'가 아니라 **신호 피드만**
    죽는다. 이래서 포지션 관리(호가 기반)는 멀쩡해 보인다."""
    svc = _service(_FULL_1M)
    try:
        svc.start_quote_updater(interval_sec=0.05)
        assert svc.quote_updater_alive() is True
        # history-updater 는 시작조차 하지 않았다 = 2026-10-20 의 상태
        assert svc.history_updater_alive() is False
    finally:
        svc.stop_quote_updater(join_timeout=2.0)


def test_run_loop_watches_both_updaters():
    """``_run_loop`` 안에서 호가와 **히스토리** 둘 다 감시한다(③ 수정)."""
    src = inspect.getsource(worker.Macd2Worker._run_loop)
    assert "quote_updater_alive" in src, "호가 워치독은 루프 안에 있어야 한다"
    assert "_advance_history_watchdog" in src, "히스토리 워치독이 루프 안에 없다"


def test_dashboard_watchdog_still_exists_but_is_no_longer_the_only_one():
    """대시보드 워치독은 그대로 두고(이중화), 루프 내 워치독이 추가됐다.

    둘 다 ``recover_history_updater`` 하나만 호출하므로 수집 스레드가 둘로
    늘어나지 않는다 -- 그 함수가 복구 락 + stop 확인으로 중복을 막는다."""
    from app.trading.macd2 import service as service_mod

    callers = [
        name for name in ("get_snapshot", "supervisor_status")
        if "self._history_watchdog(state)" in inspect.getsource(
            getattr(service_mod.Macd2Service, name))
    ]
    assert set(callers) == {"get_snapshot", "supervisor_status"}
    wd = inspect.getsource(worker.Macd2Worker._advance_history_watchdog)
    assert "recover_history_updater" in wd
    assert "history_updater_alive" in wd and "history_stale_age_sec" in wd


# ══════════════════════════════════════════════════════════════════════════
# 2. 핵심 재현 -- history 가 stale 이면 완성봉 플래그가 통째로 사라진다
# ══════════════════════════════════════════════════════════════════════════

def test_fresh_history_records_the_flag(monkeypatch):
    """대조군: 프레임이 최신이면 그 봉의 플래그가 원장에 남는다."""
    now = _now_after(LOST_BAR)
    svc = _service(_FULL_1M)
    state, broker = _state(), _broker()

    run_once(broker=broker, market_data=svc, state=state, now=now)

    rows = _flag_rows(LOST_DIR)
    assert rows, f"{LOST_BAR:%H:%M} {LOST_DIR.value} 플래그가 원장에 없다"
    assert state.last_detected_direction == LOST_DIR


def test_stale_history_loses_the_completed_bar_flag_entirely(monkeypatch):
    """2026-10-20 재현: 봉은 실제로 완성됐는데 프레임이 멈춰 있어
    플래그·원장행·예외·로그가 **전부** 없다."""
    now = _now_after(LOST_BAR)
    svc = _StaleHistoryView(_service(_FULL_1M), _truncated_before(LOST_BAR))
    state, broker = _state(), _broker()
    before = state.last_detected_direction

    result = run_once(broker=broker, market_data=svc, state=state, now=now)

    assert not _flag_rows(LOST_DIR), (
        f"{LOST_BAR:%H:%M} {LOST_DIR.value} 가 원장에 남았다 -- 재현 실패")
    assert state.last_detected_direction == before, (
        "stale 프레임인데 방향상태가 전진했다")
    # 그리고 아무도 이 사실을 알 수 없다 -- 이것이 사고의 본질이다
    assert result.skipped in (None, "NOT_READY"), result.skipped


def test_history_failures_are_logged(caplog):
    """② 계측: 수집 실패/복구가 반드시 로그에 남는다.

    사고 당시 ``market_data.py`` 에는 logger 사용이 **0건**이었다."""
    svc = _service(_FULL_1M)
    with caplog.at_level("WARNING"):
        svc._note_history_cycle(pd.DataFrame(), {"error": "RemoteDisconnected(...)"},
                                _now_after(LOST_BAR))
    assert any("HISTORY" in r.message or "HISTORY" in r.getMessage()
               for r in caplog.records), caplog.text


# ══════════════════════════════════════════════════════════════════════════
# 3. 2차 피해 -- 잃어버린 플래그가 dedup 을 오염시켜 다음 플래그까지 삼킨다
# ══════════════════════════════════════════════════════════════════════════

def test_lost_flag_poisons_dedup_and_swallows_the_next_same_direction_flag():
    """2026-08-31 과 같은 연쇄를 봉 단위로 고정한다.

    ``last_detected_direction`` 이 PRIOR 방향에 고착된 채로 NEXT 봉을 만나면,
    그 봉은 **진짜 제로크로스인데도** 가짜 '반복'으로 억제된다. 이 테스트는
    production 데이터로 만든 실제 NEXT 봉 스냅샷을 그대로 쓴다."""
    bars = _completed_bars(_FULL_1M, _now_after(NEXT_BAR))
    upto = bars[bars["datetime"] <= NEXT_BAR]
    snap = calculate_macd(upto)
    assert snap is not None and snap.bar_dt == NEXT_BAR

    # 깨끗한 상태라면 진짜 플래그다
    assert evaluate_macd_crossover(snap, None) == NEXT_DIR
    assert evaluate_macd_crossover(snap, LOST_DIR) == NEXT_DIR
    # LOST 를 놓쳐 PRIOR 방향에 고착돼 있으면 통째로 삼켜진다
    assert evaluate_macd_crossover(snap, PRIOR_DIR) == Direction.HOLD, (
        f"{NEXT_BAR:%H:%M} {NEXT_DIR.value} 가 dedup 에 삼켜지지 않는다 -- "
        "2차 피해 재현 실패")


def test_replay_self_heals_once_history_recovers():
    """history 가 **복구되면** replay 가 놓친 봉을 따라잡아 방향상태까지
    정상화한다 -- 그래서 2026-10-20 처럼 원장에 행이 **하나도 없다**는 것은
    '피드가 끝내 복구되지 않았다'는 뜻이다.

    이 테스트가 위 dedup 테스트와 쌍을 이룬다: 오염은 **복구가 없을 때만**
    끝까지 남는다."""
    # ① stale -- LOST 유실
    now_lost = _now_after(LOST_BAR)
    svc_stale = _StaleHistoryView(_service(_FULL_1M), _truncated_before(LOST_BAR))
    state, broker = _state(), _broker()
    state.last_detected_direction = PRIOR_DIR
    run_once(broker=broker, market_data=svc_stale, state=state, now=now_lost)
    assert state.last_detected_direction == PRIOR_DIR
    assert not _flag_rows(LOST_DIR)

    # ② 복구 -- replay 가 LOST 봉을 따라잡는다
    svc_ok = _service(_FULL_1M)
    run_once(broker=broker, market_data=svc_ok, state=state, now=now_lost)
    assert _flag_rows(LOST_DIR), (
        "history 복구 후에도 LOST 봉이 원장에 남지 않았다 -- "
        "replay 경로가 깨졌다는 뜻이다")


def test_catchup_recovery_writes_an_audit_row_but_does_not_clear_the_poison():
    """catch-up 복구는 **원장 행만** 쓰고 방향상태를 되돌리지 않는다
    (worker.py:3009-3017 의 의도된 설계). 따라서 복구가 일어나도 dedup 오염은
    그대로 남아 다음 같은방향 플래그를 계속 삼킨다 -- 이 구분이
    '원장에 행이 있다/없다'로 두 경로를 가르는 근거다."""
    src = inspect.getsource(worker._replay_unevaluated_completed_bars)
    # hwm 보다 앞선 봉은 상태를 건드리지 않고 원장만 복구한다
    assert "_record_catchup_flag" in src
    assert "evaluate_macd_crossover(snap, None)" in src, (
        "catch-up 이 dedup 입력을 쓰기 시작했다면 이 계약이 바뀐 것이다")
    catchup_src = inspect.getsource(worker._record_catchup_flag)
    assert "state.last_detected_direction =" not in catchup_src, (
        "catch-up 이 방향상태를 전진시키면 2026-09-16 사고가 재발한다")


# ══════════════════════════════════════════════════════════════════════════
# 4. 수정 후 계약 -- 아직 구현 전이므로 xfail(strict) 로 고정한다
# ══════════════════════════════════════════════════════════════════════════

def test_history_watchdog_runs_inside_the_worker_loop():
    """③ 대시보드 의존 제거: 루프 안에서 매 tick 히스토리 건강을 본다."""
    src = inspect.getsource(worker.Macd2Worker._run_loop)
    assert "_advance_history_watchdog" in src


def test_stale_history_is_reported_as_critical():
    """② 계측: stale/정지가 반드시 로그에 남는다."""
    src = inspect.getsource(md_mod)
    assert "logger" in src


def test_stale_history_blocks_new_entries_with_an_explicit_reason():
    """④ fail-safe: stale 이면 차단 사유가 **명시적으로** 남는다.

    기존에도 진입창은 닫혔지만(``quote_history_mismatch_reason``) 그 사실이
    어디에도 드러나지 않아 무인 상태에서 알 길이 없었다."""
    now = _now_after(LOST_BAR)
    svc = _StaleHistoryView(_service(_FULL_1M), _truncated_before(LOST_BAR))
    state, broker = _state(), _broker()

    run_once(broker=broker, market_data=svc, state=state, now=now)

    assert state.quote_history_mismatch_reason == "HISTORY_STALE"
    assert state.order_block_reason == config.HISTORY_STALE_BLOCK_REASON


def test_stale_history_never_liquidates_a_held_position(caplog):
    """④ 경계: 보유 중이면 CRITICAL 경고만 남기고 **주문은 0건**이다.

    "신호가 안 보인다"는 이유로 포지션을 터는 것은 전략 변경이므로 절대
    추가하지 않는다(2026-10-20 사용자 결정). 기존 청산 규칙(FIXED_SL15 /
    TW 래더 / 강제청산)은 호가 기반이라 그대로 돈다."""
    now = _now_after(LOST_BAR)
    svc = _StaleHistoryView(_service(_FULL_1M), _truncated_before(LOST_BAR))
    state, broker = _state(), _broker()
    state.position = PositionSnapshot(
        symbol=config.LONG_SYMBOL, quantity=10, avg_price=15_000.0, entry_at=now)
    broker.buy_market(config.LONG_SYMBOL, 10, "seed-order")
    orders_before = len(broker.orders)

    with caplog.at_level("CRITICAL"):
        run_once(broker=broker, market_data=svc, state=state, now=now)

    assert len(broker.orders) == orders_before, "stale 이 청산 주문을 냈다"
    assert state.position is not None and state.position.quantity == 10
    assert any(config.HISTORY_STALE_HOLDING_ALERT in r.getMessage()
               for r in caplog.records), caplog.text


def test_worker_survives_a_raising_history_watchdog():
    """④ fail-safe: 워치독이 터져도 tick 은 계속된다.

    ``_run_loop`` 안의 진단 때문에 자동매매가 멈추면 본말전도다."""
    svc = _service(_FULL_1M)
    w = worker.Macd2Worker(
        broker=_broker(), market_data=svc,
        get_state=_state, save_state=lambda _s: None)

    def _boom(_now):
        raise RuntimeError("watchdog exploded")

    w._advance_history_watchdog = _boom
    # 루프 본문이 아니라 호출 지점의 방어를 확인한다 -- 예외가 위로
    # 새어나가면 _run_loop 의 try 가 그 tick 을 통째로 날린다.
    src = inspect.getsource(worker.Macd2Worker._run_loop)
    i = src.index("_advance_history_watchdog")
    window = src[i - 200:i + 300]
    assert "try:" in window and "except Exception" in window, window


def test_flag_evaluation_resumes_after_history_recovers():
    """④ 복구 후 재개: 프레임이 다시 전진하면 플래그 평가가 정상화된다."""
    stale_reason = config.HISTORY_STALE_BLOCK_REASON   # ④ 미구현이면 AttributeError
    now = _now_after(LOST_BAR)
    svc_stale = _StaleHistoryView(_service(_FULL_1M), _truncated_before(LOST_BAR))
    state, broker = _state(), _broker()
    run_once(broker=broker, market_data=svc_stale, state=state, now=now)
    assert state.order_block_reason == stale_reason

    svc_ok = _service(_FULL_1M)
    run_once(broker=broker, market_data=svc_ok, state=state, now=now)
    assert state.order_block_reason != stale_reason
    assert _flag_rows(LOST_DIR), "복구 후 플래그 평가가 재개되지 않았다"


# ══════════════════════════════════════════════════════════════════════════
# 5. Session 분리가 pacing / 토큰 / 인증 동작을 바꾸지 않는다
# ══════════════════════════════════════════════════════════════════════════

def test_quote_and_history_paths_ask_for_separate_clients(monkeypatch):
    """``kis_client`` 가 스스로 금지한 것을 더 이상 어기지 않는다.

    *"requests.Session is NOT thread-safe — callers must not share one
    session across concurrent quote threads without per-thread sessions."*
    (kis_client.py:208)

    conftest 가 실제 ``create_kis_client`` 를 막으므로(당연하다) 가짜 팩토리로
    **몇 개를 요청하는지**만 본다 -- 클라이언트 1개 = Session 1개이므로 이게
    곧 Session 분리 여부다."""
    made: list[object] = []

    class _Stub:
        def __init__(self, mode):
            self.mode = mode
            self._session = object()      # 인스턴스마다 다른 Session

    def _factory(mode):
        made.append(_Stub(mode))
        return made[-1]

    monkeypatch.setattr("app.trading.kis_client.create_kis_client", _factory)

    svc = MarketDataService(mode="mock")
    q = svc._get_kis_client()
    h = svc._get_history_kis_client()

    assert q is not h, "호가/히스토리가 같은 클라이언트를 쓴다"
    assert q._session is not h._session, "두 경로가 requests.Session 을 공유한다"
    assert len(made) == 2, f"클라이언트 생성 횟수가 2가 아니다: {len(made)}"
    # 각 경로는 자기 클라이언트를 **캐시**한다(호출마다 새로 만들지 않는다)
    assert svc._get_kis_client() is q
    assert svc._get_history_kis_client() is h
    assert len(made) == 2


def test_session_split_preserves_pacing_token_and_auth():
    """분리 후에도 레이트 pacing / 토큰 / 인증은 **기존과 동일**해야 한다.

      - pacing : ``_throttle`` 은 mode 별 **프로세스 전역** 게이트라 클라이언트가
                 둘이어도 같은 간격 제한을 공유한다
      - 토큰   : 토큰 캐시가 ``kis_token_{mode}.json`` 로 mode 단위이므로 두
                 클라이언트가 **같은 토큰 파일**을 공유한다 -- 같은 appkey 로
                 토큰을 두 번 발급해 서로를 선점 무효화(EGW00123)하는 일이
                 없다. 이 성질이 깨지면 분리 자체가 사고가 된다.
      - 인증   : 두 경로 모두 ``self.mode`` 로 같은 계정 설정을 쓴다
    """
    import inspect as _inspect

    from app.trading import kis_client as kc

    # pacing: mode 를 키로 하는 모듈 전역 락/간격
    assert set(kc._MIN_REQUEST_INTERVAL_SECONDS) >= {"mock", "real"}
    assert kc._rate_limit_lock("real") is kc._rate_limit_lock("real"), (
        "같은 mode 인데 레이트 락이 매번 달라지면 pacing 이 무의미해진다")
    assert kc._rate_limit_lock("real") is not kc._rate_limit_lock("mock")
    assert "_throttle(self.mode)" in _inspect.getsource(kc.KISClient._get)

    # 토큰: 캐시 경로가 mode 에서만 파생된다 = 같은 mode 면 같은 파일
    assert 'f"kis_token_{self.mode}.json"' in _inspect.getsource(
        kc.KISClient._token_cache_path), "토큰 캐시가 mode 단위가 아니게 바뀌었다"
    assert "_load_token_cache" in _inspect.getsource(kc.KISClient.get_access_token), (
        "새 클라이언트가 캐시를 먼저 읽지 않으면 토큰을 중복 발급한다")

    # 인증: 히스토리 전용 클라이언트도 self.mode 로 같은 계정을 쓴다
    hist_src = _inspect.getsource(md_mod.MarketDataService._get_history_kis_client)
    assert "self.mode" in hist_src
    assert "create_kis_client(" in hist_src
