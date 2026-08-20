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


def test_place_order_retries_egw00201_until_success(monkeypatch):
    class _FakeResponse:
        def __init__(self, ok, status_code, body):
            self.ok = ok
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    client = kc.KISClient(app_key="a", app_secret="a", account_no="1", mode="mock")
    monkeypatch.setattr(client, "get_hashkey", lambda body: "hash")
    monkeypatch.setattr(client, "_auth_headers", lambda tr_id: {"tr_id": tr_id})
    monkeypatch.setitem(kc._RATE_LIMIT_RETRY_DELAY_SECONDS, "mock", 0.01)

    sleep_calls = []
    monkeypatch.setattr(kc.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    responses = [
        _FakeResponse(False, 500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}),
        _FakeResponse(False, 500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}),
        _FakeResponse(True, 200, {"rt_cd": "0", "msg_cd": "40600000", "msg1": "OK", "output": {"ODNO": "ORD-1"}}),
    ]

    def _fake_post(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(client, "_post", _fake_post)

    result = client._place_order(
        "VTTC0802U",
        {"CANO": "1", "ACNT_PRDT_CD": "01", "PDNO": "0193T0", "ORD_DVSN": "01", "ORD_QTY": "1", "ORD_UNPR": "0"},
        "buy",
        "0193T0",
        1,
        0,
    )

    assert result["success"] is True
    assert result["order_id"] == "ORD-1"
    assert sleep_calls == [0.01, 0.01]


def test_get_today_fills_retries_egw00201_until_success(monkeypatch):
    class _FakeResponse:
        def __init__(self, ok, status_code, body):
            self.ok = ok
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    client = kc.KISClient(app_key="a", app_secret="a", account_no="1", product_code="01", mode="mock")
    monkeypatch.setattr(client, "_auth_headers", lambda tr_id: {"tr_id": tr_id})
    monkeypatch.setitem(kc._RATE_LIMIT_RETRY_DELAY_SECONDS, "mock", 0.01)

    sleep_calls = []
    monkeypatch.setattr(kc.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    responses = [
        _FakeResponse(False, 500, {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}),
        _FakeResponse(True, 200, {
            "rt_cd": "0",
            "msg_cd": "00000000",
            "msg1": "OK",
            "output1": [{
                "pdno": "0193T0",
                "sll_buy_dvsn_cd": "02",
                "odno": "ORD-FILL-1",
                "tot_ccld_qty": "1",
                "avg_prvs": "15000",
                "ord_tmd": "132133",
            }],
        }),
    ]

    monkeypatch.setattr(client, "_get", lambda *args, **kwargs: responses.pop(0))

    result = client.get_today_fills(symbol="0193T0")

    assert result["ok"] is True
    assert result["fills"][0]["order_id"] == "ORD-FILL-1"
    assert sleep_calls == [0.01]


def _fake_candle_response(rows):
    class _FakeResponse:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    return _FakeResponse({"rt_cd": "0", "msg_cd": "0", "output2": rows})


def _rate_limited_response():
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}

    return _FakeResponse()


def test_get_minute_candles_defaults_to_j_market_div(monkeypatch):
    """기존 호출자와의 하위호환 — market_div를 넘기지 않으면 여전히 "J"."""
    client = kc.KISClient(app_key="a", app_secret="a", account_no="1", mode="mock")
    monkeypatch.setattr(client, "_auth_headers", lambda tr_id: {"tr_id": tr_id})
    captured = {}

    def _fake_get(url, **kwargs):
        captured["params"] = kwargs.get("params")
        return _fake_candle_response([])

    monkeypatch.setattr(client, "_get", _fake_get)
    client.get_minute_candles("000660", count=10)
    assert captured["params"]["FID_COND_MRKT_DIV_CODE"] == "J"


def test_get_minute_candles_for_date_passes_through_nx_market_div(monkeypatch):
    """NXT 통합 체결가 조회 — market_div="NX"가 그대로 FID_COND_MRKT_DIV_CODE에 실린다."""
    client = kc.KISClient(app_key="a", app_secret="a", account_no="1", mode="mock")
    monkeypatch.setattr(client, "_auth_headers", lambda tr_id: {"tr_id": tr_id})
    captured = {}

    def _fake_get(url, **kwargs):
        captured["params"] = kwargs.get("params")
        return _fake_candle_response([
            {"stck_bsop_date": "20260820", "stck_cntg_hour": "080100", "stck_oprc": "1600", "stck_hgpr": "1600", "stck_lwpr": "1600", "stck_prpr": "1600", "cntg_vol": "10"},
        ])

    monkeypatch.setattr(client, "_get", _fake_get)
    rows = client.get_minute_candles_for_date("000660", "20260820", count=10, market_div="NX")
    assert captured["params"]["FID_COND_MRKT_DIV_CODE"] == "NX"
    assert rows[0]["time"] == "080100"


def test_get_minute_candles_retries_egw00201_until_success(monkeypatch):
    client = kc.KISClient(app_key="a", app_secret="a", account_no="1", mode="mock")
    monkeypatch.setattr(client, "_auth_headers", lambda tr_id: {"tr_id": tr_id})
    monkeypatch.setitem(kc._RATE_LIMIT_RETRY_DELAY_SECONDS, "mock", 0.01)
    sleep_calls = []
    monkeypatch.setattr(kc.time, "sleep", lambda s: sleep_calls.append(s))

    responses = [
        _rate_limited_response(),
        _rate_limited_response(),
        _fake_candle_response([
            {"stck_bsop_date": "20260820", "stck_cntg_hour": "090000", "stck_oprc": "1600", "stck_hgpr": "1600", "stck_lwpr": "1600", "stck_prpr": "1600", "cntg_vol": "10"},
        ]),
    ]
    monkeypatch.setattr(client, "_get", lambda *a, **kw: responses.pop(0))

    rows = client.get_minute_candles("000660", count=10, market_div="NX")

    assert len(rows) == 1
    assert sleep_calls == [0.01, 0.01]
    assert client.last_minute_candle_error is None


def test_get_minute_candles_for_date_does_not_silently_return_empty_when_still_rate_limited(monkeypatch):
    """조건 6: 재시도를 모두 소진하고도 여전히 rate limit이면, 이 시간대에
    실제로 거래가 없었다는 정상 빈 페이지와 구분되도록 last_minute_candle_error
    를 남기고 []를 반환해야 한다(조용히 "성공"으로 보이면 안 됨) — market_data.py
    의 페이징 루프는 이 error 신호로만 진짜 없음/일시 실패를 구분한다."""
    client = kc.KISClient(app_key="a", app_secret="a", account_no="1", mode="mock")
    monkeypatch.setattr(client, "_auth_headers", lambda tr_id: {"tr_id": tr_id})
    monkeypatch.setitem(kc._RATE_LIMIT_RETRY_DELAY_SECONDS, "mock", 0.01)
    monkeypatch.setattr(kc.time, "sleep", lambda s: None)
    monkeypatch.setattr(client, "_get", lambda *a, **kw: _rate_limited_response())

    rows = client.get_minute_candles_for_date("000660", "20260820", count=10, market_div="NX")

    assert rows == []
    assert client.last_minute_candle_error is not None


def test_get_current_price_defaults_to_j_and_accepts_nx(monkeypatch):
    """2026-08-20 fix: 대시보드 실시간 현재가(inquire-price)도 market_div를
    받는다 -- 기본값 "J"는 기존 호출자와의 하위호환을 위해 유지되고, "NX"를
    넘기면 FID_COND_MRKT_DIV_CODE에 그대로 실린다(정규장 마감 이후에도 계속
    갱신되는 NXT 체결가를 받기 위함)."""
    client = kc.KISClient(app_key="a", app_secret="a", account_no="1", mode="mock")
    monkeypatch.setattr(client, "_auth_headers", lambda tr_id: {"tr_id": tr_id})
    captured = {}

    class _FakeResponse:
        ok = True
        status_code = 200

        def json(self):
            return {"rt_cd": "0", "output": {"stck_prpr": "1692000"}}

    def _fake_get(url, **kwargs):
        captured["params"] = kwargs.get("params")
        return _FakeResponse()

    monkeypatch.setattr(client, "_get", _fake_get)

    client.get_current_price("000660")
    assert captured["params"]["FID_COND_MRKT_DIV_CODE"] == "J"

    client.get_current_price("000660", market_div="NX")
    assert captured["params"]["FID_COND_MRKT_DIV_CODE"] == "NX"
