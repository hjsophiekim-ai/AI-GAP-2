from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.trading.tsla_auto import kis_overseas_adapter as kis


@dataclass
class _Resp:
    payload: dict

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


@pytest.fixture(autouse=True)
def _kis_basics(monkeypatch):
    monkeypatch.setattr(kis, "_credentials_account", lambda mode: ("12345678", "01"))
    monkeypatch.setattr(kis, "_get_base_url", lambda mode: "https://mock.example" if mode == "mock" else "https://real.example")
    monkeypatch.setattr(kis, "_post_headers", lambda mode, tr_id, body: {"tr_id": tr_id, "hashkey": "HASH"})


def test_mock_us_buy_uses_mock_endpoint_and_tr_id(monkeypatch):
    import requests

    calls = []

    def fake_post(url, *, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return _Resp({"rt_cd": "0", "msg_cd": "MOK", "msg1": "accepted", "output": {"ODNO": "12345"}})

    monkeypatch.setattr(requests, "post", fake_post)

    result = kis.place_overseas_limit_order("mock", "TSLL", "BUY", 1, 7.11, exchange_code="NASD")

    assert result.success is True
    assert result.order_id == "12345"
    assert calls[0][0].endswith("/uapi/overseas-stock/v1/trading/order")
    assert calls[0][1]["tr_id"] == kis.TR_OVERSEAS_US_BUY_MOCK
    assert calls[0][2]["OVRS_EXCG_CD"] == "NASD"
    assert calls[0][2]["ORD_DVSN"] == "00"


def test_mock_us_sell_uses_distinct_mock_sell_tr_id(monkeypatch):
    import requests

    seen = {}

    def fake_post(url, *, headers, json, timeout):
        seen.update(headers=headers, body=json)
        return _Resp({"rt_cd": "0", "msg1": "accepted", "output": {"ODNO": "98765"}})

    monkeypatch.setattr(requests, "post", fake_post)

    result = kis.place_overseas_limit_order("mock", "TSLL", "SELL", 1, 7.10, exchange_code="NASD")

    assert result.success is True
    assert seen["headers"]["tr_id"] == kis.TR_OVERSEAS_US_SELL_MOCK
    assert seen["headers"]["tr_id"] != kis.TR_OVERSEAS_US_BUY_MOCK
    assert seen["headers"]["tr_id"] == "VTTT1001U"
    assert seen["body"]["SLL_TYPE"] == "00"


def test_real_and_mock_order_tr_ids_are_separated(monkeypatch):
    import requests

    trs = []

    def fake_post(url, *, headers, json, timeout):
        trs.append(headers["tr_id"])
        return _Resp({"rt_cd": "0", "output": {"ODNO": f"O{len(trs)}"}})

    monkeypatch.setattr(requests, "post", fake_post)

    kis.place_overseas_limit_order("mock", "TSLL", "BUY", 1, 7.11, exchange_code="NASD")
    kis.place_overseas_limit_order("real", "TSLL", "BUY", 1, 7.11, exchange_code="NASD")

    assert trs == [kis.TR_OVERSEAS_US_BUY_MOCK, kis.TR_OVERSEAS_US_BUY_REAL]


def test_cancel_uses_mock_cancel_endpoint_and_tr_id(monkeypatch):
    import requests

    seen = {}

    def fake_post(url, *, headers, json, timeout):
        seen.update(url=url, headers=headers, body=json)
        return _Resp({"rt_cd": "0", "output": {"ODNO": "C123"}})

    monkeypatch.setattr(requests, "post", fake_post)

    result = kis.cancel_overseas_order("mock", "12345", "TSLL", exchange_code="NASD")

    assert result.success is True
    assert seen["url"].endswith("/uapi/overseas-stock/v1/trading/order-rvsecncl")
    assert seen["headers"]["tr_id"] == kis.TR_OVERSEAS_US_CANCEL_MOCK
    assert seen["body"]["RVSE_CNCL_DVSN_CD"] == "02"


def test_unconfirmed_exchange_code_blocks_order():
    with pytest.raises(kis.KisOverseasApiConfirmationRequired):
        kis.place_overseas_limit_order("mock", "TSLZ", "BUY", 1, 7.11, exchange_code="BATS")


def test_asking_price_parses_bid_ask(monkeypatch):
    import requests

    def fake_headers(mode, tr_id):
        assert tr_id == kis.TR_OVERSEAS_ASKING_PRICE
        return {"tr_id": tr_id}

    def fake_get(url, *, headers, params, timeout):
        assert params == {"AUTH": "", "EXCD": "NAS", "SYMB": "TSLL"}
        return _Resp({"output1": {"paskp1": "7.20", "pbidp1": "7.18"}})

    monkeypatch.setattr("app.data_sources.kis_overseas_minute._auth_headers", fake_headers)
    monkeypatch.setattr(requests, "get", fake_get)

    quote, error = kis.fetch_overseas_asking_price("real", "TSLL", exchange_code="NAS")

    assert error is None
    assert quote["ask1"] == 7.20
    assert quote["bid1"] == 7.18


def test_mock_open_orders_reports_official_unsupported():
    rows, error, raw = kis.fetch_overseas_open_orders("mock", "TSLL", exchange_code="NASD")

    assert rows == []
    assert error == "MOCK_OPEN_ORDERS_UNSUPPORTED_BY_KIS"
    assert raw["msg1"] == "mock open orders unsupported"
