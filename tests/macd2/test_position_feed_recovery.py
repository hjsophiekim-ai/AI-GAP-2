"""잔고조회 실패가 신호 경로를 멈추지 않고, 스스로 회복한다 -- 2026-10-21.

고정하는 것 (실제 10/20 데이터, 5초 tick)
  * get_positions 실패 중에도 history 갱신 / 완성봉 평가 / 플래그 / 원장 기록은 계속
  * 실패 중 신규주문 0 (POSITION_DATA_ERROR), 보유 포지션 임의청산 0, CRITICAL 경고
  * 연속 실패 -> circuit breaker + 지수 backoff 로 브로커 호출량 억제
  * 연결/세션 오류면 새 KISClient(새 Session), rate-limit(EGW00215) 이면 기다리기만
  * 성공 시 POSITION_DATA_RECOVERED 로그, 실패 횟수/마지막 성공 시각 기록
  * 복구 후 reconcile 정상, 중복주문 0
  * history 정지 자동복구와 동시에 일어나도 둘 다 회복
  * 표시/진단용 잔고조회는 공유 TTL 캐시 + 동시호출 합치기, Hynix 감시의 KIS 모의계좌
    1초 폴링에도 TTL 적용

전략/플래그 정의/청산 규칙은 검증 대상이 아니며 건드리지 않는다.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest

from app.trading import balance_cache
from app.trading.macd2 import broker_adapter, config, position_feed, worker
from app.trading.macd2.models import Direction
from tests.macd2.test_history_feed_recovery import (  # noqa: F401 (sims fixture)
    LOST_ID,
    NEXT_ID,
    _Sim,
    _World,
    _at,
    _rows_for,
    sims,
)

_EGW00215 = "KIS 실계좌 잔고 조회 실패: HTTP 500 msg_cd=EGW00215: 초당 거래건수를 초과하였습니다"
_REMOTE = ("KIS 실계좌 잔고 조회 실패: ConnectionError(ProtocolError('Connection aborted.', "
           "RemoteDisconnected('Remote end closed connection without response')))")


class _FlakyPositions:
    """FakeBroker 의 잔고조회만 계획대로 실패시키는 래퍼.

    ``kind(world, self) -> None | "egw" | "remote"``. remote 는 '깨진 Session' 을
    흉내낸다: ``refresh_kis_session`` 으로 세션을 갈기 전까지 계속 실패한다."""

    def __init__(self, inner, world: _World, kind):
        self._inner, self.world, self.kind = inner, world, kind
        self.calls: list[datetime] = []
        self.failed: list[datetime] = []
        self.session_gen = 0
        self.refreshes = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_positions(self):
        self.calls.append(self.world.clock)
        mode = self.kind(self.world, self)
        if mode == "egw":
            self.failed.append(self.world.clock)
            raise RuntimeError(_EGW00215)
        if mode == "remote":
            self.failed.append(self.world.clock)
            raise RuntimeError(_REMOTE)
        return self._inner.get_positions()

    def refresh_kis_session(self):
        self.session_gen += 1
        self.refreshes += 1
        return True


def _start(monkeypatch, sims, kind, *, world=None):
    world = world or _World(lambda idx, w: "ok")
    sim = _Sim(world, monkeypatch, _at("11:30", 5))
    sims.append(sim)
    sim.run(_at("11:31", 5), _at("11:57", 5))              # 11:51 UP_RED 진입까지 정상
    assert sim.state.position is not None, "시나리오 전제: 11:45 UP_RED 보유"
    flaky = _FlakyPositions(sim.broker, world, kind)
    sim.broker = flaky
    return sim, flaky


def _run_5s(sim: _Sim, start: datetime, end: datetime, *, cycle=True) -> list[tuple[datetime, float, str]]:
    out = []
    t = start
    while t <= end:
        t0 = time.monotonic()
        sim.tick(t, cycle=cycle)
        out.append((t, time.monotonic() - t0, sim.state.last_position_reconcile_result))
        t += timedelta(seconds=5)
    return out


def _orders(sim):
    return [(o.symbol, o.side) for o in sim.broker.orders]


# ══════════════════════════════════════════════════════════════════════════
# 1. EGW00215 22분 연속 -- 그 사이 12:06 DOWN_BLUE 발생
# ══════════════════════════════════════════════════════════════════════════

def test_egw00215_storm_never_stops_the_signal_path_and_recovers(monkeypatch, sims, caplog):
    window = (_at("11:58"), _at("12:20"))

    def kind(w, _b):
        return "egw" if window[0] <= w.clock < window[1] else None

    sim, flaky = _start(monkeypatch, sims, kind)
    with caplog.at_level("WARNING"):
        during = _run_5s(sim, _at("11:58", 0), _at("12:19", 55))
        # 실패 중: 12:06 DOWN_BLUE 가 이미 원장에 있다(복구 전에)
        lost_during = _rows_for(LOST_ID)
        newest_during = sim.md.history_diag(now=_at("12:19", 55))["history_frame_newest_at"]
        after = _run_5s(sim, _at("12:20", 0), _at("12:25", 0))
        sim.run(_at("12:26", 5), _at("13:45", 5))

    # 신호 루프는 멈추지 않았다: 모든 tick 이 돌았고 각 tick 은 짧았다
    assert len(during) == 22 * 12
    assert max(d for _t, d, _r in during) < 5.0, "잔고 실패가 tick 을 붙잡았다"
    # history 갱신 / 완성봉 평가 / 플래그 / 원장 기록 계속
    assert newest_during >= _at("12:19").isoformat()
    assert len(lost_during) == 1, "잔고조회 실패 중 12:06 DOWN_BLUE 가 기록되지 않았다"
    # 실패 중 판정은 POSITION_DATA_ERROR(신규주문 차단)
    assert all(r == worker.POSITION_DATA_ERROR for _t, _d, r in during)
    # circuit breaker: 264 tick 동안 브로커 호출은 소수
    calls_in_window = [c for c in flaky.calls if window[0] <= c < window[1]]
    assert len(calls_in_window) <= 40, len(calls_in_window)
    # rate-limit 이므로 새 클라이언트로 갈지 않는다(같은 appkey 라 소용없다)
    assert flaky.refreshes == 0
    # 보유 포지션: 임의청산 0 + CRITICAL 경고
    buys_sells_in_window = [t for t, n in sim.orders_at.items() if window[0] <= t < window[1] and n]
    assert not buys_sells_in_window, buys_sells_in_window
    assert any(position_feed.HOLDING_ALERT in r.getMessage() for r in caplog.records)
    # 복구: 로그 + 상태 + reconcile 정상
    assert any("POSITION_DATA_RECOVERED" in r.getMessage() for r in caplog.records)
    assert sim.state.position_feed_failures == 0
    assert sim.state.position_feed_status == position_feed.FEED_OK
    assert sim.state.position_feed_last_success_at is not None
    assert after[-1][2] in (worker.MATCH_POSITION, worker.MATCH_FLAT), after[-1]
    # 중복주문 0: 진입 1, 청산 최대 1
    orders = _orders(sim)
    assert orders.count((config.LONG_SYMBOL, "BUY")) == 1
    assert orders.count((config.LONG_SYMBOL, "SELL")) <= 1
    assert orders.count((config.INVERSE_SYMBOL, "BUY")) <= 1
    # 이후 새 플래그도 정상 기록
    assert len(_rows_for(NEXT_ID)) == 1
    assert len(_rows_for(LOST_ID)) == 1


# ══════════════════════════════════════════════════════════════════════════
# 2. RemoteDisconnected (깨진 Session) -> 새 KISClient 로 즉시 회복
# ══════════════════════════════════════════════════════════════════════════

def test_remote_disconnected_switches_to_a_fresh_session_and_recovers(monkeypatch, sims, caplog):
    def kind(w, b):
        return "remote" if (w.clock >= _at("11:58") and b.session_gen == 0) else None

    sim, flaky = _start(monkeypatch, sims, kind)
    with caplog.at_level("WARNING"):
        ticks = _run_5s(sim, _at("11:58", 0), _at("12:01", 0))
    assert flaky.refreshes == 1, "연결 오류인데 새 Session 으로 갈지 않았다"
    first_ok = next(i for i, (_t, _d, r) in enumerate(ticks) if r != worker.POSITION_DATA_ERROR)
    assert first_ok <= 2, ticks[:4]
    assert sim.state.position_feed_client_refreshes == 1
    assert any("POSITION_DATA_RECOVERED" in r.getMessage() for r in caplog.records)
    assert len(flaky.failed) == 1


# ══════════════════════════════════════════════════════════════════════════
# 3. 30초 연속 실패 -> breaker 열림/backoff -> 회복 -> reconcile 정상
# ══════════════════════════════════════════════════════════════════════════

def test_thirty_seconds_of_failures_open_the_breaker_then_recover(monkeypatch, sims):
    window = (_at("12:00"), _at("12:00", 30))

    def kind(w, _b):
        return "egw" if window[0] <= w.clock < window[1] else None

    sim, flaky = _start(monkeypatch, sims, kind)
    _run_5s(sim, _at("11:58", 0), _at("11:59", 55))
    ticks = _run_5s(sim, _at("12:00", 0), _at("12:02", 0))
    in_window = [c for c in flaky.calls if window[0] <= c < window[1]]
    # 처음 3번은 그대로 시도, 그 뒤로는 backoff 로 건너뜀(5초 tick 6개 중 일부만 호출)
    assert 3 <= len(in_window) < 6, in_window
    assert sim.state.position_feed_failures == 0
    recovered_at = next(t for t, _d, r in ticks if t >= window[1] and r != worker.POSITION_DATA_ERROR)
    # backoff 상한 안에서 회복
    assert (recovered_at - window[1]).total_seconds() <= position_feed.BACKOFF_BASE_SEC * 2 + 5
    assert sim.state.last_position_reconcile_result in (worker.MATCH_POSITION, worker.MATCH_FLAT)
    sim.run(_at("12:03", 5), _at("12:15", 5))
    assert len(_rows_for(LOST_ID)) == 1


# ══════════════════════════════════════════════════════════════════════════
# 4. 잔고조회 장애 + history 정지가 동시에 -- 둘 다 회복, 플래그 복원, 중복 0
# ══════════════════════════════════════════════════════════════════════════

def test_position_and_history_outage_together_both_recover(monkeypatch, sims):
    def history(idx, w):
        if w.clock < _at("11:57"):
            return "ok"
        if idx == 0:
            return "hang"
        return "remote" if w.clock < _at("12:40") else "ok"

    world = _World(history)

    def kind(w, _b):
        return "egw" if _at("11:58") <= w.clock < _at("12:30") else None

    sim, flaky = _start(monkeypatch, sims, kind, world=world)
    world.clock = _at("11:57", 30)
    assert world.hung.wait(5)
    _run_5s(sim, _at("11:58", 0), _at("12:39", 55), cycle=False)
    lost_before = len(_rows_for(LOST_ID))
    sim.tick(_at("12:40", 5), cycle=False)
    assert sim.wait_frame(_at("12:39"))
    sim.run(_at("12:40", 35), _at("13:45", 5))

    assert lost_before == 0, "history 가 멈춘 동안에는 12:06 이 보일 수 없다(재현 전제)"
    lost = _rows_for(LOST_ID)
    assert len(lost) == 1 and lost[0]["block_reason"] == worker.LATE_COMPLETED_BAR_REPLAY
    nxt = _rows_for(NEXT_ID)
    assert len(nxt) == 1 and nxt[0]["block_reason"] != worker.LATE_COMPLETED_BAR_REPLAY
    assert sim.state.last_detected_direction == Direction.UP_RED
    assert sim.state.position_feed_failures == 0
    assert sim.md.live_history_updater_count() == 1
    # 장애 구간 주문 0, 이후에도 진입 중복 0
    assert not [t for t, n in sim.orders_at.items() if _at("11:58") <= t < _at("12:40") and n]
    assert _orders(sim).count((config.LONG_SYMBOL, "BUY")) == 1


# ══════════════════════════════════════════════════════════════════════════
# 5. 단위 계약
# ══════════════════════════════════════════════════════════════════════════

def test_backoff_grows_exponentially_and_is_capped():
    seq = [position_feed.backoff_sec(n) for n in range(1, 10)]
    assert seq[:2] == [0.0, 0.0]
    assert seq[2:6] == [5.0, 10.0, 20.0, 40.0]
    assert max(seq) == position_feed.BACKOFF_MAX_SEC


@pytest.mark.parametrize("err,expected", [
    (_REMOTE, True),
    ("ReadTimeout(ReadTimeoutError(\"HTTPSConnectionPool: Read timed out.\"))", True),
    (_EGW00215, False),
    ("rt_cd=1: 모의투자 장종료", False),
])
def test_only_connection_errors_get_a_fresh_session(err, expected):
    assert position_feed.is_connection_error(err) is expected


def test_adapter_refresh_swaps_in_a_brand_new_kis_client(monkeypatch):
    made = []

    class _Kis:
        def __init__(self, mode):
            self.mode = mode
            self._session = object()

    def _factory(mode):
        made.append(_Kis(mode))
        return made[-1]

    monkeypatch.setattr("app.trading.kis_client.create_kis_client", _factory)

    class _Inner:
        kis = _Kis("real")

    inner = _Inner()
    old = inner.kis
    adapter = broker_adapter.MockBrokerAdapter(broker=inner)
    assert adapter.refresh_kis_session() is True
    assert inner.kis is not old and inner.kis._session is not old._session
    assert inner.kis.mode == "real" and len(made) == 1


def test_display_balance_reads_share_a_ttl_cache_and_coalesce_concurrent_calls():
    balance_cache.clear()
    gate = threading.Event()
    calls = []

    class _Kis:
        mode, account_no, product_code = "real", "12345678", "01"

        def get_balance(self):
            calls.append(1)
            gate.wait(2)
            return {"positions": [], "cash": 1.0}

    kis = _Kis()
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        balance_cache.get_balance_shared(kis, max_age_sec=5.0))) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(3)
    assert len(calls) == 1, "동시 호출이 합쳐지지 않았다"
    assert len(results) == 5 and all(r["cash"] == 1.0 for r in results)
    balance_cache.get_balance_shared(kis, max_age_sec=5.0)
    assert len(calls) == 1, "TTL 안인데 다시 불렀다"
    balance_cache.clear()


def test_balance_errors_are_never_cached():
    balance_cache.clear()
    seq = [{"positions": [], "error": "EGW00215"}, {"positions": [], "cash": 2.0}]

    class _Kis:
        mode, account_no, product_code = "real", "1", "01"

        def get_balance(self):
            return seq.pop(0)

    kis = _Kis()
    assert balance_cache.get_balance_shared(kis, 60.0)["error"] == "EGW00215"
    assert balance_cache.get_balance_shared(kis, 60.0)["cash"] == 2.0
    balance_cache.clear()
