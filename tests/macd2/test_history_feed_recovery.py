"""신호 피드(history) 자동 정상화 -- 2026-10-21 (10/20 실사고 2차).

사고
----
2026-10-20 11:57 무렵 1분봉 수집이 멈췄고, **대시보드가 12:06~15:00 내내 열려
있어 기존 워치독이 5초마다 돌았는데도** 복구되지 않았다(사용자 확인). 그 결과
12:06 DOWN_BLUE 가 원장 행 없이 사라졌고, 방향상태가 11:45 UP_RED 에 고착돼
13:36 UP_RED 까지 '반복' 으로 삼켜졌다. c8a73e1 은 같은 판정/복구 함수를 루프
안으로 옮겼을 뿐이라 같은 상황을 고치지 못한다.

이 파일이 고정하는 것 (전부 실제 10/19~10/20 데이터로 end-to-end)
--------------------------------------------------------------------
  * 판정 기준 = worker 가 보는 **프레임**이 전진했는가 (KIS 응답 성공 여부 아님)
  * 멈춘(alive-but-stuck) 스레드는 은퇴시키고 새 세대 + 새 KISClient 로 교체
  * 복구 뒤 newest 이후 빠진 1분봉 backfill -> 놓친 봉을 시간순 replay
  * 12:06 DOWN_BLUE 원장 1회, 13:36 UP_RED 정상 기록, 소급 주문 0, 중복 주문 0
  * 복구 전에는 HISTORY_FEED_STALE 로 신규진입 차단, 보유 포지션 임의청산 0
  * 장애 유형별: 반영 실패 / RemoteDisconnected / hang / 연속 rate-limit /
    복구 중 재실패 / backfill

데이터: tests/macd2/fixtures/hynix_1m_20261019_20261020.csv
  10/19 = KIS NX 1분봉, 10/20 = ws_shadow_logger 1초 스냅샷(000660)으로 재구성한
  1분봉. production 함수로 계산한 10/20 플래그에 11:45 UP_RED / 12:06 DOWN_BLUE /
  13:36 UP_RED 가 그대로 있다(test_fixture_reproduces_the_incident_flags).

전략/플래그 정의/청산 규칙은 검증 대상이 아니며 건드리지 않는다.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd
import pytest

from app.trading.macd2 import config, ledger, market_data as md_mod, state_store, worker
from app.trading.macd2.market_data import (
    MarketDataService,
    filter_complete_3m_bars,
    resample_completed_3m,
)
from app.trading.macd2.models import Direction, RuntimeState
from app.trading.macd2.signal_engine import (
    calculate_macd,
    evaluate_macd_crossover,
    exclude_preopen_padding_1m,
    make_signal_id,
)
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker

KST = config.KST
_FIXTURE = Path(__file__).parent / "fixtures" / "hynix_1m_20261019_20261020.csv"


def _load_fixture() -> pd.DataFrame:
    df = pd.read_csv(_FIXTURE)
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(KST)
    return df


_ALL_1M = _load_fixture()


def _at(hhmm: str, sec: int = 0) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 10, 20, h, m, sec, tzinfo=KST)


def _bar(hhmm: str) -> datetime:
    return _at(hhmm)


LOST_BAR = _bar("12:06")      # 놓친 DOWN_BLUE
NEXT_BAR = _bar("13:36")      # 오염되면 삼켜지는 UP_RED
PRIOR_BAR = _bar("11:45")     # 유실 직전 플래그
LOST_ID = make_signal_id(LOST_BAR, Direction.DOWN_BLUE)
NEXT_ID = make_signal_id(NEXT_BAR, Direction.UP_RED)

#: 진짜 수집 스레드의 주기(실시간 초). 시뮬레이션 1틱(=1분)보다 훨씬 짧게 둬서
#: 장애가 풀리면 스레드가 곧바로 다음 사이클을 돈다.
_THREAD_INTERVAL_SEC = 0.2

_PRICES = {config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0,
           config.WATCH_SYMBOL: 1_230_000.0}


# ── 가짜 KIS 세계 ──────────────────────────────────────────────────────────

class _World:
    """시뮬레이션 시각까지의 실제 1분봉만 보여주는 KIS.

    클라이언트는 **생성 순서(idx)** 별로 동작이 다르다 -- 깨진 Session 은 그
    클라이언트에만 붙어 있고, 새로 만든 클라이언트는 멀쩡할 수 있다는 실제
    성질을 그대로 흉내낸다. ``behavior(idx, world) -> "ok"|"remote"|"ratelimit"|"hang"``.
    """

    def __init__(self, behavior: Callable[[int, "_World"], str]):
        self.clock = _at("11:30", 5)
        self.behavior = behavior
        self.clients: list[_FakeClient] = []
        self.release = threading.Event()        # hang 해제
        self.hung = threading.Event()           # hang 진입 신호

    def _rows(self, day_ymd: str, hour1: str, limit_to_clock: bool) -> pd.DataFrame:
        rows = _ALL_1M[_ALL_1M["datetime"].dt.strftime("%Y%m%d") == day_ymd]
        if limit_to_clock:
            rows = rows[rows["datetime"] <= pd.Timestamp(self.clock)]
        if hour1:
            cutoff = datetime.strptime(hour1, "%H%M%S").time()
            rows = rows[rows["datetime"].dt.time <= cutoff]
        return rows.tail(30)

    @staticmethod
    def _candles(rows: pd.DataFrame) -> list[dict]:
        return [
            {"date": r.datetime.strftime("%Y%m%d"), "time": r.datetime.strftime("%H%M%S"),
             "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
            for r in rows.itertuples()
        ]

    def create_client(self, mode):
        del mode
        client = _FakeClient(self, len(self.clients))
        self.clients.append(client)
        return client

    def fetch_for_date(self, mode, symbol, date_ymd, count, hour1):
        del mode, symbol, count
        rows = self._rows(date_ymd, hour1, limit_to_clock=False)
        return md_mod._candles_to_df(self._candles(rows)), {"received_count": len(rows)}


class _FakeClient:
    def __init__(self, world: _World, idx: int):
        self.world = world
        self.idx = idx
        self._session = object()
        self.last_minute_candle_error = None
        self.calls = 0

    def get_minute_candles(self, symbol, period_min=1, count=30, hour1="", market_div=""):
        del symbol, period_min, count, market_div
        self.calls += 1
        mode = self.world.behavior(self.idx, self.world)
        if mode == "hang":
            self.world.hung.set()
            self.world.release.wait(timeout=30)
            self.last_minute_candle_error = "ReadTimeout('read timed out')"
            return []
        if mode == "remote":
            self.last_minute_candle_error = (
                "RemoteDisconnected('Remote end closed connection without response')")
            return []
        if mode == "ratelimit":
            self.last_minute_candle_error = "EGW00201 초당 거래건수를 초과하였습니다"
            return []
        self.last_minute_candle_error = None
        day = self.world.clock.strftime("%Y%m%d")
        return self.world._candles(self.world._rows(day, hour1, limit_to_clock=True))


# ── 시뮬레이터 ─────────────────────────────────────────────────────────────

def _state() -> RuntimeState:
    state = state_store.default_state()
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    state.time_window_2_filter_enabled = False
    state.time_window_teg_filter_enabled = False
    state.time_window_3slot_filter_enabled = True
    return state


class _Sim:
    """worker 루프 한 바퀴 = (수집 사이클) -> 루프 내 워치독 -> 호가 -> run_once.

    ``cycle=True`` 는 수집 스레드의 주기 사이클을 결정적으로 흉내낸다(현재
    세대의 클라이언트를 쓴다). hang 시나리오처럼 **실제 스레드**가 멈춰야 하는
    구간에서는 ``cycle=False`` 로 두고 진짜 updater 스레드에 맡긴다."""

    def __init__(self, world: _World, monkeypatch, start: datetime):
        monkeypatch.setattr("app.trading.kis_client.create_kis_client", world.create_client)
        self.world = world
        world.clock = start
        self.md = MarketDataService(
            mode="mock",
            fetch_minute_candles_for_date=world.fetch_for_date,
            fetch_quote=lambda mode, symbol: (_PRICES.get(symbol), None),
            clock=lambda: world.clock,
        )
        result = self.md.bootstrap(now=start)
        assert result.ok, result.reason
        # production 과 같은 순서: bootstrap 직후 수집 스레드 기동(service.start).
        self.md.start_history_updater(interval_sec=_THREAD_INTERVAL_SEC)
        self.md.refresh_quotes()
        self.state = _state()
        self.broker = FakeBroker(cash=10_000_000.0, quotes={
            config.LONG_SYMBOL: _PRICES[config.LONG_SYMBOL],
            config.INVERSE_SYMBOL: _PRICES[config.INVERSE_SYMBOL]})
        self.worker = worker.Macd2Worker(
            broker=self.broker, market_data=self.md,
            get_state=lambda: self.state, save_state=lambda _s: None)
        self.verdicts: list[tuple[datetime, str | None]] = []
        self.block_reasons: list[tuple[datetime, str | None]] = []
        self.orders_at: dict[datetime, int] = {}

    def tick(self, t: datetime, *, cycle: bool = True) -> None:
        self.world.clock = t
        if cycle:
            self.md.merge_incremental_1m(now=t)
        verdict = self.worker._advance_history_watchdog(t)
        self.verdicts.append((t, verdict))
        self.md.refresh_quotes()
        before = len(self.broker.orders)
        run_once(broker=self.broker, market_data=self.md, state=self.state, now=t)
        self.orders_at[t] = len(self.broker.orders) - before
        self.block_reasons.append((t, self.state.order_block_reason))

    def run(self, start: datetime, end: datetime, *, step_sec: int = 60, cycle=True) -> None:
        t = start
        while t <= end:
            self.tick(t, cycle=cycle)
            t += timedelta(seconds=step_sec)

    def wait_frame(self, at_least: datetime, timeout: float = 10.0) -> bool:
        """진짜 updater 스레드가 프레임을 ``at_least`` 까지 전진시킬 때까지 기다린다."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            newest = md_mod._frame_newest(self.md.get_history_df())
            if newest is not None and newest >= pd.Timestamp(at_least):
                return True
            time.sleep(0.02)
        return False

    def close(self) -> None:
        self.world.release.set()
        self.md.stop_history_updater(join_timeout=2.0)
        for t in threading.enumerate():
            if t.name.startswith("macd2-history-updater"):
                t.join(timeout=2.0)


def _rows_for(signal_id: str) -> list[dict]:
    return [r for r in ledger.load_signal_ledger(limit=5000)
            if str(r.get("signal_id") or "") == signal_id]


def _flag_ids_today() -> list[str]:
    out = []
    for r in ledger.load_signal_ledger(limit=5000):
        sid = str(r.get("signal_id") or "")
        if sid.startswith("20261020_") and ":" not in sid:
            out.append(sid)
    return out


@pytest.fixture
def sims():
    made: list[_Sim] = []
    yield made
    for s in made:
        s.close()


# ══════════════════════════════════════════════════════════════════════════
# 0. 데이터가 실사고를 그대로 담고 있다
# ══════════════════════════════════════════════════════════════════════════

def test_fixture_reproduces_the_incident_flags():
    now = _at("15:30")
    sig, _ = exclude_preopen_padding_1m(_ALL_1M)
    bars = resample_completed_3m(sig, now=now)
    bars, _ = filter_complete_3m_bars(bars, sig)
    flags, last = [], None
    for i in range(1, len(bars) + 1):
        snap = calculate_macd(bars.iloc[:i])
        if snap is None:
            continue
        d = evaluate_macd_crossover(snap, last)
        if d != Direction.HOLD:
            last = d
            flags.append((snap.bar_dt.astimezone(KST).strftime("%m%d %H:%M"), d))
    assert ("1020 11:45", Direction.UP_RED) in flags
    assert ("1020 12:06", Direction.DOWN_BLUE) in flags
    assert ("1020 13:36", Direction.UP_RED) in flags


# ── 공통 시나리오 ──────────────────────────────────────────────────────────

_STALL_AT = _at("11:57")
_TICK0 = _at("11:31", 5)


def _run_incident(sim: _Sim, *, recover_at: datetime, stall_cycle: bool,
                  until: datetime = _at("13:45", 5), wait_hang: bool = False) -> dict:
    """11:31 정상 -> 11:57 정지 -> ``recover_at`` 에 KIS 정상화 -> ``until``.

    정지 구간은 ``stall_cycle`` 로 수집 사이클을 시뮬레이터가 돌릴지(오류형) 진짜
    스레드에 맡길지(hang 형) 정한다. 복구 직후 스레드가 프레임을 따라잡을 때까지
    기다린 뒤 1틱 더 돌려 worker 가 따라잡은 봉을 평가하게 한다."""
    sim.run(_TICK0, _at("11:56", 5))
    orders_before = len(sim.broker.orders)
    held_before = sim.state.position
    sim.world.clock = _at("11:57", 5)
    if wait_hang:
        assert sim.world.hung.wait(5), "gen0 스레드가 hang 에 들어가지 않았다"
    gates = []
    t = _at("11:57", 5)
    while t < recover_at:
        sim.tick(t, cycle=stall_cycle)
        gates.append((t, sim.state.quote_history_mismatch_reason))
        t += timedelta(seconds=60)
    lost_rows_during_stall = len(_rows_for(LOST_ID))
    sim.tick(t, cycle=stall_cycle)                     # KIS 정상화 첫 틱
    caught_up = sim.wait_frame(recover_at - timedelta(minutes=1))
    sim.tick(t + timedelta(seconds=30), cycle=True)    # 따라잡은 봉 평가
    dir_after_recovery = sim.state.last_detected_direction
    sim.run(t + timedelta(seconds=90), until)
    return {
        "orders_before": orders_before, "held_before": held_before, "gates": gates,
        "lost_rows_during_stall": lost_rows_during_stall, "caught_up": caught_up,
        "dir_after_recovery": dir_after_recovery,
    }


def _no_orders_since_stall(sim: _Sim) -> dict:
    return {t: n for t, n in sim.orders_at.items() if t >= _STALL_AT and n}


def _assert_live_reversal_only(sim: _Sim) -> None:
    """12:06 봉 완성 전에 복구된 경우: 12:06 DOWN_BLUE 는 live 플래그라 T+3 확인
    시점(12:12)에 보유 UP_RED 청산 1건이 정상 전략대로 나간다. 그 외 주문 0,
    같은 주문 중복 0."""
    since = sim.broker.orders[1:]          # [0] = 11:51 UP_RED 진입
    assert [(o.symbol, o.side) for o in since] == [(config.LONG_SYMBOL, "SELL")], since
    when = [t for t, n in sim.orders_at.items() if t >= _STALL_AT and n]
    assert len(when) == 1 and when[0] >= _at("12:12"), when
    assert sim.state.position is None or sim.state.position.quantity == 0


def _assert_signal_path_restored(sim: _Sim, out: dict, *, lost_via_catchup: bool) -> None:
    # 정지 중: 신규진입은 기존 HISTORY_STALE 게이트가 막고, 주문 0
    stale_gates = [g for t, g in out["gates"] if t >= _at("12:00")]
    assert stale_gates and all(g == "HISTORY_STALE" for g in stale_gates), out["gates"]
    if lost_via_catchup:
        assert out["lost_rows_during_stall"] == 0, "정지 중인데 12:06 이 기록됐다(재현 실패)"
    # 복구됨: 프레임이 다시 전진했고, 이후 새 3분봉 플래그가 생성된다
    assert out["caught_up"], "복구 후 프레임이 따라잡지 못했다"
    newest = md_mod._frame_newest(sim.md.get_history_df())
    assert newest >= pd.Timestamp(_at("13:44")), newest
    # 12:06 DOWN_BLUE: 원장 정확히 1회
    lost = _rows_for(LOST_ID)
    assert len(lost) == 1, lost
    if lost_via_catchup:
        assert lost[0]["block_reason"] == worker.LATE_COMPLETED_BAR_REPLAY
        assert lost[0]["order_result"] == "BLOCKED"
    # 13:36 UP_RED: '반복'으로 삼켜지지 않고 live 로 기록된다
    nxt = _rows_for(NEXT_ID)
    assert len(nxt) == 1, "13:36 UP_RED 가 dedup 오염에 삼켜졌다"
    assert nxt[0]["block_reason"] != worker.LATE_COMPLETED_BAR_REPLAY, "13:36 이 live 로 평가되지 않았다"
    # 시간순 replay 후 방향상태 = 최신 플래그 방향
    assert sim.state.last_detected_direction == Direction.UP_RED
    # 소급 주문 0 · 중복 주문 0
    assert not _no_orders_since_stall(sim), f"정지/복구 이후 주문이 나갔다: {_no_orders_since_stall(sim)}"
    assert len(sim.broker.orders) == out["orders_before"]
    # 보유 포지션 임의청산 0
    if out["held_before"] is not None:
        assert sim.state.position is not None
        assert sim.state.position.quantity == out["held_before"].quantity
    # 프레임을 쓰는 updater 는 정확히 하나
    assert sim.md.live_history_updater_count() == 1
    # 같은 봉을 다시 지나가도 원장 행이 늘지 않는다
    sim.run(_at("13:46", 5), _at("13:48", 5))
    assert len(_rows_for(LOST_ID)) == 1 and len(_rows_for(NEXT_ID)) == 1


# ══════════════════════════════════════════════════════════════════════════
# 1. 10/20 재현: 스레드 hang -> 새 세대도 KIS 단절 -> 12:40 정상화 (43분 정지)
# ══════════════════════════════════════════════════════════════════════════

def test_stuck_thread_then_kis_outage_auto_recovers_and_replays_the_lost_flag(monkeypatch, sims, caplog):
    def behavior(idx, w):
        if w.clock < _STALL_AT:
            return "ok"
        if idx == 0:
            return "hang"
        return "remote" if w.clock < _at("12:40") else "ok"

    sim = _Sim(_World(behavior), monkeypatch, _at("11:30", 5))
    sims.append(sim)
    with caplog.at_level("WARNING"):
        out = _run_incident(sim, recover_at=_at("12:40", 5), stall_cycle=False, wait_hang=True)

    assert out["held_before"] is not None, "시나리오 전제: 11:45 UP_RED 로 보유 중"
    _assert_signal_path_restored(sim, out, lost_via_catchup=True)
    assert out["dir_after_recovery"] == Direction.DOWN_BLUE, "replay 후 방향이 최신(12:06)과 다르다"

    verdicts = {v for _t, v in sim.verdicts if v}
    assert config.HISTORY_UPDATER_ALIVE_BUT_STALE in verdicts
    diag = sim.md.history_diag(now=_at("13:48"))
    assert diag["history_replaced_stuck_count"] >= 1, "멈춘 스레드를 교체하지 않았다"
    assert diag["history_generation"] >= 2
    assert diag["history_backfill_count"] >= 1
    bf = diag["history_last_backfill"]
    assert bf["complete"] and pd.Timestamp(bf["from"]) <= pd.Timestamp(_STALL_AT), bf
    # 보유 중 정지 -> CRITICAL 경고(자동청산 없음)
    assert any(config.HISTORY_STALE_HOLDING_ALERT in r.getMessage() for r in caplog.records)
    # 복구 클라이언트는 매번 새로 만든 것이다(gen0 의 깨진 클라이언트 재사용 0)
    assert len(sim.world.clients) >= 3


# ══════════════════════════════════════════════════════════════════════════
# 2. KIS 응답은 성공하는데 프레임 반영이 실패 (10/20 의 '판정 정상' 사각지대)
# ══════════════════════════════════════════════════════════════════════════

class _MergeFault:
    def __init__(self, world: _World, until: datetime):
        self.world, self.until = world, until
        self.real = md_mod._trim_to_recent_trading_days

    def __call__(self, df, *a, **k):
        if _STALL_AT <= self.world.clock < self.until:
            raise AttributeError("Can only use .dt accessor with datetimelike values")
        return self.real(df, *a, **k)


def test_fetch_succeeds_but_frame_merge_fails_is_detected_and_heals(monkeypatch, sims):
    world = _World(lambda idx, w: "ok")
    sim = _Sim(world, monkeypatch, _at("11:30", 5))
    sims.append(sim)
    monkeypatch.setattr(md_mod, "_trim_to_recent_trading_days", _MergeFault(world, _at("12:40")))
    real_merge = sim.md.merge_incremental_1m

    def _cycle(*a, **k):        # 수집 스레드의 except 경로와 동일하게 삼키고 기록
        try:
            return real_merge(*a, **k)
        except Exception as exc:
            sim.md._note_history_exception(exc)
            return None
    sim.md.merge_incremental_1m = _cycle

    out = _run_incident(sim, recover_at=_at("12:40", 5), stall_cycle=True)

    # KIS 는 계속 새 봉을 줬다 -- 예전 기준(수신 시각)으로는 '정상'이었다
    stall_verdicts = [v for t, v in sim.verdicts if _at("12:00") <= t < _at("12:40")]
    assert config.HISTORY_UPDATER_ALIVE_BUT_STALE in stall_verdicts, (
        "프레임 반영 실패를 워치독이 잡지 못했다(10/20 사각지대)")
    _assert_signal_path_restored(sim, out, lost_via_catchup=True)


# ══════════════════════════════════════════════════════════════════════════
# 3. RemoteDisconnected 가 한 클라이언트(Session)에 붙어 있다 -> 새 클라이언트로 치유
# ══════════════════════════════════════════════════════════════════════════

def test_remote_disconnected_session_heals_with_a_fresh_client(monkeypatch, sims):
    def behavior(idx, w):
        return "remote" if (idx == 0 and w.clock >= _STALL_AT) else "ok"

    sim = _Sim(_World(behavior), monkeypatch, _at("11:30", 5))
    sims.append(sim)
    _run_incident(sim, recover_at=_at("12:03", 5), stall_cycle=True)

    # 12:06 봉이 완성되기 전에 복구 -> 놓친 플래그 없이 live 로 평가된다
    lost = _rows_for(LOST_ID)
    assert len(lost) == 1 and lost[0]["block_reason"] != worker.LATE_COMPLETED_BAR_REPLAY
    assert len(sim.world.clients) >= 2, "복구가 새 KISClient 를 만들지 않았다"
    gen0_calls = sim.world.clients[0].calls
    sim.run(_at("13:49", 5), _at("13:51", 5))
    assert sim.world.clients[0].calls == gen0_calls, "깨진 gen0 클라이언트를 계속 쓴다"
    assert sim.state.last_detected_direction == Direction.UP_RED
    _assert_live_reversal_only(sim)
    assert len(_rows_for(NEXT_ID)) == 1


# ══════════════════════════════════════════════════════════════════════════
# 4. hang 스레드 교체 + 은퇴 스레드의 늦은 응답은 프레임을 쓰지 못한다(lease)
# ══════════════════════════════════════════════════════════════════════════

def test_stuck_thread_is_replaced_and_its_late_result_is_discarded(monkeypatch, sims):
    def behavior(idx, w):
        return "hang" if (idx == 0 and w.clock >= _STALL_AT) else "ok"

    world = _World(behavior)
    sim = _Sim(world, monkeypatch, _at("11:30", 5))
    sims.append(sim)
    sim.run(_TICK0, _at("11:56", 5))
    world.clock = _at("11:57", 5)
    assert world.hung.wait(5)
    zombie = sim.md._history_updater_thread
    sim.run(_at("11:57", 5), _at("12:02", 5), cycle=False)
    assert sim.md.history_diag()["history_replaced_stuck_count"] == 1
    assert zombie.name.startswith("macd2-history-updater-retired")
    assert sim.md.live_history_updater_count() == 1
    assert sim.wait_frame(_at("12:01"))

    # 은퇴 스레드가 '옛 값' 을 들고 돌아온다 -> 프레임/진단 어디에도 쓰이면 안 된다
    frame_before = sim.md.get_history_df()
    errors_before = sim.md.history_diag()["history_consecutive_failures"]
    real_get = _FakeClient.get_minute_candles

    def stale_return(self, *a, **k):
        if self.idx == 0:
            self.world.hung.set()
            self.world.release.wait(timeout=30)
            return [{"date": "20261020", "time": "120000", "open": 1, "high": 1,
                     "low": 1, "close": 1, "volume": 1}]
        return real_get(self, *a, **k)
    monkeypatch.setattr(_FakeClient, "get_minute_candles", stale_return)
    world.release.set()
    zombie.join(timeout=5)
    assert not zombie.is_alive(), "은퇴 스레드가 멈춤 신호 뒤에도 끝나지 않았다"
    df = sim.md.get_history_df()
    assert not (pd.to_numeric(df["close"]) == 1).any(), "은퇴 세대의 늦은 응답이 프레임을 덮어썼다"
    assert len(df) >= len(frame_before)
    assert sim.md.history_diag()["history_consecutive_failures"] == errors_before
    assert sim.md.history_diag()["history_retired_threads_alive"] == 0


# ══════════════════════════════════════════════════════════════════════════
# 5. 연속 rate-limit (EGW00201) -- 폭주 없이 기다렸다가 정상화
# ══════════════════════════════════════════════════════════════════════════

def test_sustained_rate_limit_recovers_without_storming(monkeypatch, sims):
    def behavior(idx, w):
        return "ratelimit" if _STALL_AT <= w.clock < _at("12:40") else "ok"

    sim = _Sim(_World(behavior), monkeypatch, _at("11:30", 5))
    sims.append(sim)
    out = _run_incident(sim, recover_at=_at("12:40", 5), stall_cycle=True)
    _assert_signal_path_restored(sim, out, lost_via_catchup=True)

    assert any(v for _t, v in sim.verdicts), "rate-limit 정지를 감지하지 못했다"
    # 복구 직후 180초 판정 유예 + 쿨다운 -> 43분 동안 재기동은 많아야 3분에 1번 꼴
    count = sim.md.history_diag(now=_at("13:48"))["history_recovery_count"]
    assert 1 <= count <= 43 // 3 + 1, count


# ══════════════════════════════════════════════════════════════════════════
# 6. 복구 도중 또 실패 (오류 -> 새 세대 hang -> 새 세대 오류 -> 정상)
# ══════════════════════════════════════════════════════════════════════════

def test_recovery_that_fails_again_keeps_retrying_until_healthy(monkeypatch, sims):
    plan = {0: "remote", 1: "hang", 2: "remote"}

    def behavior(idx, w):
        if w.clock < _STALL_AT:
            return "ok"
        return plan.get(idx, "ok")

    sim = _Sim(_World(behavior), monkeypatch, _at("11:30", 5))
    sims.append(sim)
    out = _run_incident(sim, recover_at=_at("12:40", 5), stall_cycle=False)
    diag = sim.md.history_diag(now=_at("13:48"))
    assert diag["history_generation"] >= 3
    assert diag["history_replaced_stuck_count"] >= 1
    assert out["caught_up"]
    assert len(_rows_for(LOST_ID)) == 1
    assert len(_rows_for(NEXT_ID)) == 1
    assert sim.state.last_detected_direction == Direction.UP_RED
    # 이 계획에서는 gen3(정상)이 12:06 완성 전에 올라와 12:06 은 live 로 평가된다
    if _rows_for(LOST_ID)[0]["block_reason"] == worker.LATE_COMPLETED_BAR_REPLAY:
        assert not _no_orders_since_stall(sim)
    else:
        _assert_live_reversal_only(sim)
    assert sim.md.live_history_updater_count() == 1


# ══════════════════════════════════════════════════════════════════════════
# 7. 10/20 처럼 3시간 정지 -> 여러 페이지 backfill, 두 플래그 모두 시간순 복원
# ══════════════════════════════════════════════════════════════════════════

def test_three_hour_outage_backfills_every_page_and_restores_flags_in_order(monkeypatch, sims):
    def behavior(idx, w):
        return "remote" if _STALL_AT <= w.clock < _at("14:50") else "ok"

    sim = _Sim(_World(behavior), monkeypatch, _at("11:30", 5))
    sims.append(sim)
    out = _run_incident(sim, recover_at=_at("14:50", 5), stall_cycle=True, until=_at("14:55", 5))

    bf = sim.md.history_diag(now=_at("14:55"))["history_last_backfill"]
    assert bf["complete"] and bf["pages"] >= 4, bf
    lost, nxt = _rows_for(LOST_ID), _rows_for(NEXT_ID)
    assert len(lost) == 1 and len(nxt) == 1
    # 둘 다 정지 구간 안이라 FLAG 복원 전용(주문 없음)
    assert lost[0]["block_reason"] == worker.LATE_COMPLETED_BAR_REPLAY
    assert nxt[0]["block_reason"] == worker.LATE_COMPLETED_BAR_REPLAY
    # 시간순 replay -> 최종 방향 = 마지막 플래그(13:36 UP_RED). 되감기 없음.
    assert out["dir_after_recovery"] == Direction.UP_RED
    assert not _no_orders_since_stall(sim)
    # 정지 구간 1분봉이 전부 복원됐다
    df = sim.md.get_history_df()
    have = set(pd.to_datetime(df["datetime"]))
    missing = [m for m in pd.date_range(_at("11:56"), _at("14:50"), freq="1min") if m not in have]
    assert not missing, f"backfill 되지 않은 1분봉: {missing[:5]}"
