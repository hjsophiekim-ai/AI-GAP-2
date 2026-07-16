"""
test_kis_client_rate_limit.py — KIS 모의투자 EGW00201(초당 거래건수 초과) 회귀 테스트.

broker_factory.create_broker()가 3분 자동매매 사이클/30초 Fast Trend Watcher/
1초 Dynamic Exit Watcher마다 매번 새 KISClient 인스턴스를 만들기 때문에, 인스턴스
자체에 요청 기록을 두는 방식으로는 스레드 간 동시 호출을 막을 수 없다 — 여러
스레드가 같은 순간에 겹쳐 호출하면 그 자체로 KIS 모의투자 서버의 초당 요청수
제한에 걸린다(2026-07-16 실측: BUY 신호가 났는데 "POSITION_SYNC_PENDING - ...
HTTP 500 msg_cd=EGW00201: 초당 거래건수를 초과하였습니다"로 주문이 막힘).

app.trading.kis_client의 프로세스 전역(모듈 레벨) 레이트리미터가 mode별로 모든
KISClient 인스턴스에 걸쳐 최소 요청 간격을 강제하는지 검증한다.
"""
from __future__ import annotations

import app.trading.kis_client as kc


def test_throttle_waits_between_consecutive_calls_same_mode(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setitem(kc._MIN_REQUEST_INTERVAL_SECONDS, "mock", 0.2)
    kc._LAST_REQUEST_AT.clear()

    sleep_calls: list[float] = []
    monkeypatch.setattr(kc.time, "sleep", lambda s: sleep_calls.append(s))

    kc._throttle("mock")
    kc._throttle("mock")

    assert len(sleep_calls) == 1
    assert 0 < sleep_calls[0] <= 0.2


def test_throttle_is_independent_per_mode(monkeypatch):
    """mock 레이트리밋 대기가 real 호출을 불필요하게 막지 않아야 한다."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setitem(kc._MIN_REQUEST_INTERVAL_SECONDS, "mock", 5.0)
    monkeypatch.setitem(kc._MIN_REQUEST_INTERVAL_SECONDS, "real", 0.0)
    kc._LAST_REQUEST_AT.clear()

    sleep_calls: list[float] = []
    monkeypatch.setattr(kc.time, "sleep", lambda s: sleep_calls.append(s))

    kc._throttle("mock")
    kc._throttle("real")  # 별도 mode이므로 대기 없이 즉시 통과해야 한다

    assert sleep_calls == []


def test_two_kis_client_instances_share_the_same_mode_throttle(monkeypatch):
    """서로 다른 KISClient 인스턴스(스레드마다 새로 만들어지는 상황을 흉내)도
    같은 mode면 같은 전역 레이트리미터를 공유해야 한다."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setitem(kc._MIN_REQUEST_INTERVAL_SECONDS, "mock", 0.3)
    kc._LAST_REQUEST_AT.clear()

    sleep_calls: list[float] = []
    monkeypatch.setattr(kc.time, "sleep", lambda s: sleep_calls.append(s))

    class _FakeResponse:
        status_code = 200
        ok = True

        def json(self):
            return {"rt_cd": "0"}

    client_a = kc.KISClient(app_key="a", app_secret="a", account_no="1", mode="mock")
    client_b = kc.KISClient(app_key="b", app_secret="b", account_no="2", mode="mock")
    monkeypatch.setattr(client_a._session, "get", lambda *a, **kw: _FakeResponse())
    monkeypatch.setattr(client_b._session, "get", lambda *a, **kw: _FakeResponse())

    client_a._get("https://example.invalid")
    client_b._get("https://example.invalid")  # 다른 인스턴스여도 같은 mode 스로틀 적용

    assert len(sleep_calls) == 1


def test_pytest_bypass_skips_throttle_during_tests():
    """PYTEST_CURRENT_TEST가 설정된 정상적인 테스트 실행 중에는 sleep 없이 즉시
    반환돼야 한다(그렇지 않으면 전체 테스트 스위트가 매우 느려진다)."""
    import time as real_time

    t0 = real_time.monotonic()
    kc._throttle("mock")
    kc._throttle("mock")
    elapsed = real_time.monotonic() - t0
    assert elapsed < 0.2
