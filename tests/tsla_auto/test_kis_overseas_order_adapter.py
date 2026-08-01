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


def test_real_open_orders_uses_nccs_endpoint_and_parses_buy_rows(monkeypatch):
    import requests

    seen = {}

    def fake_headers(mode, tr_id):
        return {"tr_id": tr_id}

    def fake_get(url, *, headers, params, timeout):
        seen.update(url=url, headers=headers, params=params, timeout=timeout)
        return _Resp({
            "rt_cd": "0",
            "output": [{
                "ODNO": "O123",
                "pdno": "TSLL",
                "sll_buy_dvsn_cd": "02",
                "ft_ord_qty": "10",
                "ft_ccld_qty": "0",
                "nccs_qty": "10",
                "ft_ord_unpr3": "30.00",
            }],
        })

    monkeypatch.setattr("app.data_sources.kis_overseas_minute._auth_headers", fake_headers)
    monkeypatch.setattr(requests, "get", fake_get)

    rows, error, _raw = kis.fetch_overseas_open_orders("real", "TSLL", exchange_code="NASD")

    assert error is None
    assert seen["url"].endswith("/uapi/overseas-stock/v1/trading/inquire-nccs")
    assert seen["headers"]["tr_id"] == kis.TR_OVERSEAS_OPEN_ORDERS_REAL
    assert seen["params"]["OVRS_EXCG_CD"] == "NASD"
    assert [(r.order_id, r.symbol, r.side, r.unfilled_qty) for r in rows] == [("O123", "TSLL", "BUY", 10)]


def test_minute_candles_parse_kis_kst_timestamp_to_et_regular_session(monkeypatch):
    import requests

    def fake_headers(mode, tr_id):
        return {"tr_id": tr_id}

    def fake_creds(mode):
        return {"app_key": "key", "app_secret": "secret", "base_url": "https://mock.example"}

    def fake_get(url, *, headers, params, timeout):
        assert params["SYMB"] == "TSLA"
        return _Resp({
            "output2": [
                {"kymd": "20260731", "khms": "222900", "last": "300", "open": "300", "high": "301", "low": "299", "evol": "10"},
                {"kymd": "20260731", "khms": "223000", "last": "301", "open": "300", "high": "302", "low": "300", "evol": "20"},
            ]
        })

    monkeypatch.setattr("app.data_sources.kis_overseas_minute._auth_headers", fake_headers)
    monkeypatch.setattr("app.data_sources.kis_overseas_minute._load_credentials", fake_creds)
    monkeypatch.setattr(requests, "get", fake_get)

    df, diag = kis.fetch_overseas_minute_candles("mock", "TSLA", exchange_code="NAS", regular_only=True)

    assert diag["received_count"] == 1
    assert len(df) == 1
    assert df["datetime"].iloc[0].isoformat() == "2026-07-31T09:30:00-04:00"


def test_minute_candles_prefer_exchange_timestamp_when_present(monkeypatch):
    import requests

    def fake_headers(mode, tr_id):
        return {"tr_id": tr_id}

    def fake_creds(mode):
        return {"app_key": "key", "app_secret": "secret", "base_url": "https://mock.example"}

    def fake_get(url, *, headers, params, timeout):
        return _Resp({
            "output2": [
                {
                    "xymd": "20260731", "xhms": "093000",
                    "kymd": "20260731", "khms": "223000",
                    "last": "301", "open": "300", "high": "302", "low": "300", "evol": "20",
                }
            ]
        })

    monkeypatch.setattr("app.data_sources.kis_overseas_minute._auth_headers", fake_headers)
    monkeypatch.setattr("app.data_sources.kis_overseas_minute._load_credentials", fake_creds)
    monkeypatch.setattr(requests, "get", fake_get)

    df, diag = kis.fetch_overseas_minute_candles("mock", "TSLA", exchange_code="NAS", regular_only=True)

    assert diag["received_count"] == 1
    assert df["datetime"].iloc[0].isoformat() == "2026-07-31T09:30:00-04:00"
