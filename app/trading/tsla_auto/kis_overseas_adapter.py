"""TSLA_AUTO KIS 해외주식 어댑터 — 독립적인 해외 인터페이스.

docs/TSLA_AUTO_LOGIC.md §KIS 해외주식 API를 그대로 따른다:

- 현재가상세(TR ``HHDFS00000300``)/분봉조회(TR ``HHDFS76950200``)는 이 저장소
  ``app/data_sources/kis_overseas_minute.py``가 MU 종목으로 이미 실제 운용
  중인 것과 **동일한 엔드포인트·TR_ID·인증 로직**을 재사용한다(그 모듈의
  ``_load_credentials``/``_get_access_token``/``BASE_URL_*``/TR 상수를 그대로
  import — 국내 ``app/trading/kis_client.py``는 이 모듈에서 절대 import하지
  않는다). MU 전용으로 하드코딩돼 있던 종목 파라미터만 임의 심볼로 일반화했다.
- 해외주식 잔고조회, 외화예수금, USD 주문가능금액, 종목별 주문가능수량,
  지정가 매수·매도, 정정·취소, 미체결조회, 체결내역은 이 저장소에 선례가
  전혀 없다 — 공식 KIS 문서로 TR_ID/엔드포인트/파라미터를 확인하기 전까지
  REAL 호출을 절대 구현하지 않는다(추정으로 만들지 않는다). 이 모듈을 통해
  이 기능을 호출하면 ``KisOverseasApiConfirmationRequired``를 raise한다 —
  절대 성공으로 가정하지 않는다. MOCK/테스트는 주입된 fake 함수만 사용한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from app.trading.tsla_auto import config

_1M_COLUMNS = ("datetime", "open", "high", "low", "close", "volume")


class KisOverseasApiConfirmationRequired(NotImplementedError):
    """확인되지 않은 KIS 해외주식 TR 기능을 REAL 모드로 호출하려 할 때 발생한다.
    docs: "공식 확인이 안 되는 기능은 KIS_OVERSEAS_API_CONFIRMATION_REQUIRED로
    차단하고 성공으로 가정하지 않는다." — 이 예외는 절대 조용히 삼키지 않는다."""

    def __init__(self, feature: str) -> None:
        super().__init__(
            f"KIS_OVERSEAS_API_CONFIRMATION_REQUIRED: {feature} — "
            "공식 KIS Open API 문서/샘플로 TR_ID·엔드포인트·파라미터를 확인하기 "
            "전까지 REAL 호출을 구현하지 않는다 (docs/TSLA_AUTO_LOGIC.md §KIS 해외주식 API)."
        )
        self.feature = feature


@dataclass(frozen=True)
class OverseasQuote:
    symbol: str
    price: float
    open: float
    high: float
    low: float
    volume: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OverseasPosition:
    symbol: str
    quantity: int
    avg_price: float
    exchange_code: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OverseasCashBalance:
    currency: str
    available_amount: float
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OverseasBuyableQuantity:
    symbol: str
    exchange_code: str
    order_price: float
    available_usd: float
    available_qty: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OverseasOrderResult:
    success: bool
    order_id: str
    symbol: str
    side: str
    requested_qty: int
    executed_qty: int = 0
    executed_price: float = 0.0
    rt_cd: str = ""
    msg_cd: str = ""
    msg1: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OverseasOrderRow:
    order_id: str
    symbol: str
    side: str
    order_qty: int
    executed_qty: int
    unfilled_qty: int
    order_price: float
    executed_price: float
    raw: dict[str, Any] = field(default_factory=dict)


TR_OVERSEAS_BALANCE_REAL = "TTTS3012R"
TR_OVERSEAS_BALANCE_MOCK = "VTTS3012R"
TR_OVERSEAS_BUYABLE_REAL = "TTTS3007R"
TR_OVERSEAS_BUYABLE_MOCK = "VTTS3007R"
TR_OVERSEAS_US_BUY_REAL = "TTTT1002U"
TR_OVERSEAS_US_BUY_MOCK = "VTTT1002U"
TR_OVERSEAS_US_SELL_REAL = "TTTT1006U"
TR_OVERSEAS_US_SELL_MOCK = "VTTT1001U"
TR_OVERSEAS_US_CANCEL_REAL = "TTTT1004U"
TR_OVERSEAS_US_CANCEL_MOCK = "VTTT1004U"
TR_OVERSEAS_OPEN_ORDERS_REAL = "TTTS3018R"
TR_OVERSEAS_OPEN_ORDERS_MOCK = ""
TR_OVERSEAS_FILLS_REAL = "TTTS3035R"
TR_OVERSEAS_FILLS_MOCK = "VTTS3035R"
TR_OVERSEAS_ASKING_PRICE = "HHDFS76200100"

OVERSEAS_STOCK_ORDER_ENDPOINT = "/uapi/overseas-stock/v1/trading/order"
OVERSEAS_STOCK_CANCEL_ENDPOINT = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
OVERSEAS_STOCK_OPEN_ORDERS_ENDPOINT = "/uapi/overseas-stock/v1/trading/inquire-nccs"
OVERSEAS_STOCK_FILLS_ENDPOINT = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
OVERSEAS_STOCK_ASKING_PRICE_ENDPOINT = "/uapi/overseas-price/v1/quotations/inquire-asking-price"
US_ORDER_EXCHANGES = frozenset({"NASD", "NYSE", "AMEX"})


def _empty_1m_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_1M_COLUMNS))


def _get_base_url(mode: str) -> str:
    from app.data_sources.kis_overseas_minute import BASE_URL_MOCK, BASE_URL_REAL

    return BASE_URL_REAL if mode == "real" else BASE_URL_MOCK


def _tr_id(mode: str, real: str, mock: str) -> str:
    return real if mode == "real" else mock


def _credentials_account(mode: str) -> tuple[str, str]:
    from app.data_sources.kis_overseas_minute import _load_credentials

    creds = _load_credentials(mode)
    cano = str(creds.get("account_no") or creds.get("account") or "")
    product = str(creds.get("account_product_code") or creds.get("product_code") or creds.get("product") or "")
    if not cano:
        import os

        prefix = "KIS_REAL" if mode == "real" else "KIS_MOCK"
        cano = os.getenv(f"{prefix}_ACCOUNT_NO", "") or os.getenv(f"{prefix}_CANO", "")
        product = product or os.getenv(f"{prefix}_ACNT_PRDT_CD", "") or os.getenv(f"{prefix}_PRODUCT_CODE", "")
    if "-" in cano and not product:
        cano, product = cano.split("-", 1)
    return cano[:8], product or "01"


def _num(raw: Any, default: float = 0.0) -> float:
    try:
        return float(str(raw).replace(",", "").strip()) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _int(raw: Any, default: int = 0) -> int:
    try:
        return int(float(str(raw).replace(",", "").strip())) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _mode_to_env_dv(mode: str) -> str:
    if mode == "real":
        return "real"
    if mode == "mock":
        return "demo"
    raise ValueError(f"unsupported KIS overseas mode: {mode!r}")


def _mask_account(cano: str, product: str) -> str:
    if not cano:
        return ""
    head = cano[:2]
    tail = cano[-2:] if len(cano) >= 2 else ""
    return f"{head}****{tail}-{product or '**'}"


def masked_account(mode: str) -> str:
    cano, product = _credentials_account(mode)
    return _mask_account(cano, product)


def _post_hashkey(mode: str, body: dict[str, Any]) -> str:
    import requests

    from app.data_sources.kis_overseas_minute import _load_credentials

    creds = _load_credentials(mode)
    resp = requests.post(f"{_get_base_url(mode)}/uapi/hashkey", headers={
        "appkey": creds["app_key"],
        "appsecret": creds["app_secret"],
        "Content-Type": "application/json; charset=utf-8",
    }, json=body, timeout=10)
    resp.raise_for_status()
    return str(resp.json().get("HASH") or resp.json().get("hash") or "")


def _post_headers(mode: str, tr_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from app.data_sources.kis_overseas_minute import _auth_headers

    headers = _auth_headers(mode, tr_id)
    hashkey = _post_hashkey(mode, body)
    if hashkey:
        headers["hashkey"] = hashkey
    return headers


def _extract_output(body: dict[str, Any]) -> dict[str, Any]:
    output = body.get("output", {}) if isinstance(body, dict) else {}
    if isinstance(output, list):
        return dict(output[0] if output else {})
    return dict(output or {})


def _extract_order_id(output: dict[str, Any]) -> str:
    for key in ("ODNO", "odno", "OVRS_ORD_NO", "ovrs_ord_no", "ORD_NO", "ord_no"):
        value = str(output.get(key) or "").strip()
        if value:
            return value
    return ""


def _side_from_row(row: dict[str, Any]) -> str:
    raw = str(row.get("sll_buy_dvsn_cd") or row.get("sll_buy_dvsn") or row.get("sll_buy_dvsn_name") or "").strip()
    if raw in {"01", "SELL", "매도"} or "매도" in raw:
        return "SELL"
    if raw in {"02", "BUY", "매수"} or "매수" in raw:
        return "BUY"
    return raw


def _order_row(row: dict[str, Any]) -> OverseasOrderRow:
    order_qty = _int(row.get("ft_ord_qty") or row.get("ord_qty") or row.get("qty"))
    executed_qty = _int(row.get("ft_ccld_qty") or row.get("tot_ccld_qty") or row.get("ccld_qty"))
    unfilled_qty = _int(row.get("nccs_qty"), max(order_qty - executed_qty, 0))
    return OverseasOrderRow(
        order_id=_extract_order_id(row),
        symbol=str(row.get("pdno") or row.get("ovrs_pdno") or row.get("item_cd") or "").strip(),
        side=_side_from_row(row),
        order_qty=order_qty,
        executed_qty=executed_qty,
        unfilled_qty=unfilled_qty,
        order_price=_num(row.get("ft_ord_unpr3") or row.get("ovrs_ord_unpr") or row.get("ord_unpr")),
        executed_price=_num(row.get("ft_ccld_unpr3") or row.get("avg_prvs") or row.get("ccld_unpr")),
        raw=dict(row),
    )


def fetch_overseas_current_price(
    mode: str, symbol: str, *, exchange_code: str = "NAS",
) -> tuple[Optional[OverseasQuote], Optional[str]]:
    """현재가상세(TR ``HHDFS00000300``) — app/data_sources/kis_overseas_minute.py
    가 MU로 이미 실제 호출하는 것과 동일한 엔드포인트/인증을 재사용, 심볼만
    일반화. Returns (quote_or_None, error_or_None)."""
    try:
        import requests

        from app.data_sources.kis_overseas_minute import (
            TR_OVERSEAS_CURRENT,
            _auth_headers,
            _load_credentials,
        )

        creds = _load_credentials(mode)
        base_url = _get_base_url(mode)
        url = f"{base_url}/uapi/overseas-price/v1/quotations/price"
        params = {"AUTH": "", "EXCD": exchange_code, "SYMB": symbol}
        resp = requests.get(url, headers=_auth_headers(mode, TR_OVERSEAS_CURRENT), params=params, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        output = body.get("output", {})
        if not output:
            return None, "empty_output"
        last = output.get("last")
        if last in (None, "", "0"):
            return None, "no_price"
        price = float(str(last).replace(",", ""))
        if price <= 0:
            return None, "non_positive_price"

        def _num(key: str, default: float) -> float:
            raw = output.get(key)
            try:
                return float(str(raw).replace(",", "")) if raw not in (None, "") else default
            except (TypeError, ValueError):
                return default

        quote = OverseasQuote(
            symbol=symbol,
            price=price,
            open=_num("open", price),
            high=_num("high", price),
            low=_num("low", price),
            volume=int(float(output.get("tvol") or 0)),
            raw=dict(output),
        )
        return quote, None
    except Exception as exc:  # pragma: no cover - real network path, not exercised in tests
        return None, repr(exc)


def fetch_overseas_minute_candles(
    mode: str, symbol: str, *, exchange_code: str = "NAS", nrec: int = 120, regular_only: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """분봉조회(TR ``HHDFS76950200``) — 최대 120건/회(KIS 실측 한도).
    app/data_sources/kis_overseas_minute.py의 인증/파라미터 패턴을 그대로
    재사용, 심볼만 일반화."""
    try:
        import requests

        from app.data_sources.kis_overseas_minute import (
            TR_OVERSEAS_MINUTE,
            _auth_headers,
            _load_credentials,
        )

        creds = _load_credentials(mode)
        base_url = _get_base_url(mode)
        url = f"{base_url}/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
        params = {
            "AUTH": "", "EXCD": exchange_code, "SYMB": symbol,
            "NMIN": "1", "PINC": "1", "NEXT": "", "NREC": str(min(nrec, 120)), "FILL": "", "KEYB": "",
        }
        resp = requests.get(url, headers=_auth_headers(mode, TR_OVERSEAS_MINUTE), params=params, timeout=15)
        resp.raise_for_status()
        output2 = resp.json().get("output2", [])
        if not output2:
            return _empty_1m_frame(), {"received_count": 0}

        rows = []
        for item in output2:
            exchange_date_str = str(item.get("xymd") or "")
            exchange_time_str = str(item.get("xhms") or "")
            kst_date_str = str(item.get("kymd") or "")
            kst_time_str = str(item.get("khms") or "")
            if not ((exchange_date_str and exchange_time_str) or (kst_date_str and kst_time_str)):
                continue
            try:
                if exchange_date_str and exchange_time_str:
                    dt = datetime.strptime(exchange_date_str + exchange_time_str, "%Y%m%d%H%M%S").replace(tzinfo=config.ET)
                else:
                    dt = (
                        datetime.strptime(kst_date_str + kst_time_str, "%Y%m%d%H%M%S")
                        .replace(tzinfo=config.KST)
                        .astimezone(config.ET)
                    )
                close_raw = item.get("last")
                if close_raw in (None, "", "0"):
                    continue
                close_px = float(str(close_raw).replace(",", ""))
            except (ValueError, TypeError):
                continue
            rows.append({
                "datetime": dt,
                "open": float(str(item.get("open") or close_px).replace(",", "")),
                "high": float(str(item.get("high") or close_px).replace(",", "")),
                "low": float(str(item.get("low") or close_px).replace(",", "")),
                "close": close_px,
                "volume": int(float(item.get("evol") or 0)),
            })
        if not rows:
            return _empty_1m_frame(), {"received_count": 0}
        df = pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)
        if regular_only:
            et = df["datetime"].dt.tz_convert(config.ET)
            minutes = et.dt.hour * 60 + et.dt.minute
            open_min = config.SESSION_OPEN.hour * 60 + config.SESSION_OPEN.minute
            close_min = config.REGULAR_CLOSE.hour * 60 + config.REGULAR_CLOSE.minute
            df = df[(minutes >= open_min) & (minutes < close_min)].reset_index(drop=True)
        return df, {"received_count": int(len(df))}
    except Exception as exc:  # pragma: no cover - real network path, not exercised in tests
        return _empty_1m_frame(), {"error": repr(exc)}


# ── 아래 기능은 이 저장소에 선례가 없다 — REAL 호출을 구현하지 않는다. ──────
# docs/TSLA_AUTO_LOGIC.md §KIS 해외주식 API "이 저장소에 선례가 전혀 없는 것"
# 표에 있는 각 항목과 1:1 대응한다. MOCK/테스트는 이 함수들을 절대 호출하지
# 않고, 주입된 fake 함수(또는 FakeOverseasBroker)만 사용한다.

def fetch_overseas_cash_balance(mode: str) -> tuple[Optional[OverseasCashBalance], Optional[str]]:
    """Read-only USD available cash from overseas balance output."""
    positions, cash, error = fetch_overseas_balance(mode)
    del positions
    return cash, error


def fetch_overseas_buyable_amount(
    mode: str, symbol: str, *, exchange_code: str = "NASD", price: float = 0.0,
) -> tuple[Optional[float], Optional[str]]:
    """Read-only USD buyable amount. Does not place or amend orders."""
    quote, error = fetch_overseas_buyable_quantity(mode, symbol, exchange_code=exchange_code, price=price)
    return (quote.available_usd if quote else None), error


def fetch_overseas_buyable_quantity(
    mode: str, symbol: str, *, exchange_code: str = "NASD", price: float = 0.0,
) -> tuple[Optional[OverseasBuyableQuantity], Optional[str]]:
    """Read-only overseas buyable quantity. Does not place or amend orders."""
    try:
        import requests

        from app.data_sources.kis_overseas_minute import _auth_headers

        cano, product = _credentials_account(mode)
        url = f"{_get_base_url(mode)}/uapi/overseas-stock/v1/trading/inquire-psamount"
        params = {
            "CANO": cano, "ACNT_PRDT_CD": product, "OVRS_EXCG_CD": exchange_code,
            "OVRS_ORD_UNPR": str(price or 0), "ITEM_CD": symbol,
        }
        tr = _tr_id(mode, TR_OVERSEAS_BUYABLE_REAL, TR_OVERSEAS_BUYABLE_MOCK)
        resp = requests.get(url, headers=_auth_headers(mode, tr), params=params, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        output = body.get("output", {}) if isinstance(body, dict) else {}
        if not output:
            return None, "empty_output"
        available_usd = _num(output.get("ord_psbl_frcr_amt") or output.get("frcr_ord_psbl_amt"))
        available_qty = _int(output.get("ord_psbl_qty") or output.get("max_ord_psbl_qty"))
        raw = dict(output)
        raw["_rt_cd"] = str(body.get("rt_cd") or "")
        raw["_msg_cd"] = str(body.get("msg_cd") or "")
        raw["_msg1"] = str(body.get("msg1") or "")
        return OverseasBuyableQuantity(symbol, exchange_code, float(price or 0.0), available_usd, available_qty, raw), None
    except Exception as exc:  # pragma: no cover - real network path
        return None, repr(exc)


def fetch_overseas_balance(
    mode: str, *, exchange_code: str = "NASD", currency: str = "USD",
) -> tuple[list[OverseasPosition], Optional[OverseasCashBalance], Optional[str]]:
    """Read-only overseas holdings and USD available amount."""
    try:
        import requests

        from app.data_sources.kis_overseas_minute import _auth_headers

        cano, product = _credentials_account(mode)
        url = f"{_get_base_url(mode)}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {
            "CANO": cano, "ACNT_PRDT_CD": product, "OVRS_EXCG_CD": exchange_code,
            "TR_CRCY_CD": currency, "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        }
        tr = _tr_id(mode, TR_OVERSEAS_BALANCE_REAL, TR_OVERSEAS_BALANCE_MOCK)
        resp = requests.get(url, headers=_auth_headers(mode, tr), params=params, timeout=10)
        resp.raise_for_status()
        body = resp.json()
        output1 = body.get("output1", []) if isinstance(body, dict) else []
        output2 = body.get("output2", {}) if isinstance(body, dict) else {}
        positions: list[OverseasPosition] = []
        for row in output1 or []:
            symbol = str(row.get("ovrs_pdno") or row.get("pdno") or row.get("symb") or "").strip()
            qty = _int(row.get("ovrs_cblc_qty") or row.get("hldg_qty"))
            if symbol and qty > 0:
                positions.append(OverseasPosition(
                    symbol=symbol, quantity=qty,
                    avg_price=_num(row.get("pchs_avg_pric") or row.get("avg_unpr")),
                    exchange_code=str(row.get("ovrs_excg_cd") or exchange_code),
                    raw=dict(row),
                ))
        cash_raw = output2[0] if isinstance(output2, list) and output2 else output2
        cash_raw_dict = dict(cash_raw or {})
        cash_raw_dict["_rt_cd"] = str(body.get("rt_cd") or "")
        cash_raw_dict["_msg_cd"] = str(body.get("msg_cd") or "")
        cash_raw_dict["_msg1"] = str(body.get("msg1") or "")
        cash = OverseasCashBalance(
            currency=currency,
            available_amount=_num((cash_raw or {}).get("frcr_pchs_amt1") or (cash_raw or {}).get("ord_psbl_frcr_amt")),
            raw=cash_raw_dict,
        )
        return positions, cash, None
    except Exception as exc:  # pragma: no cover - real network path
        return [], None, repr(exc)


def place_overseas_limit_order(
    mode: str, symbol: str, side: str, qty: int, price: float, *, exchange_code: str = "NAS",
) -> None:
    """해외주식 지정가 매수·매도 — TR_ID/엔드포인트 미확인. 절대 REAL 주문을
    시도하지 않는다."""
    raise KisOverseasApiConfirmationRequired(f"해외주식 지정가 {side} 주문 (overseas limit order)")


def cancel_overseas_order(mode: str, order_id: str, symbol: str) -> None:
    """정정·취소 — TR_ID/엔드포인트 미확인."""
    raise KisOverseasApiConfirmationRequired("해외주식 정정·취소 (overseas cancel/amend)")


def fetch_overseas_open_orders(mode: str, symbol: str = "") -> None:
    """미체결 조회 — TR_ID/엔드포인트 미확인."""
    raise KisOverseasApiConfirmationRequired("해외주식 미체결 조회 (overseas open orders)")


def fetch_overseas_fills(mode: str, symbol: str = "") -> None:
    """체결내역·부분체결·평균체결가 — TR_ID/엔드포인트 미확인."""
    raise KisOverseasApiConfirmationRequired("해외주식 체결내역 조회 (overseas fills)")


def fetch_overseas_asking_price(
    mode: str, symbol: str, *, exchange_code: str = "NAS",
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Read KIS overseas best bid/ask. Official TR: HHDFS76200100."""
    if mode == "mock":
        return None, "MOCK_ASKING_PRICE_UNSUPPORTED_BY_KIS"
    try:
        import requests

        from app.data_sources.kis_overseas_minute import _auth_headers

        resp = requests.get(
            f"{_get_base_url(mode)}{OVERSEAS_STOCK_ASKING_PRICE_ENDPOINT}",
            headers=_auth_headers(mode, TR_OVERSEAS_ASKING_PRICE),
            params={"AUTH": "", "EXCD": exchange_code, "SYMB": symbol},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        merged: dict[str, Any] = {}
        for key in ("output1", "output2", "output3"):
            output = body.get(key, {}) if isinstance(body, dict) else {}
            if isinstance(output, list):
                for row in output:
                    if isinstance(row, dict):
                        merged.update(row)
            elif isinstance(output, dict):
                merged.update(output)
        ask = _num(merged.get("paskp1") or merged.get("askp1") or merged.get("ovrs_askp1") or merged.get("ask1"))
        bid = _num(merged.get("pbidp1") or merged.get("bidp1") or merged.get("ovrs_bidp1") or merged.get("bid1"))
        if ask <= 0:
            return None, "ask1_unavailable"
        return {"ok": True, "symbol": symbol, "exchange_code": exchange_code, "ask1": ask, "bid1": bid, "raw": body}, None
    except Exception as exc:  # pragma: no cover - real network path
        return None, repr(exc)


def place_overseas_limit_order(  # type: ignore[no-redef]
    mode: str, symbol: str, side: str, qty: int, price: float, *, exchange_code: str = "NASD",
) -> OverseasOrderResult:
    """Place a regular-session US overseas stock limit order."""
    if exchange_code not in US_ORDER_EXCHANGES:
        raise KisOverseasApiConfirmationRequired(f"unconfirmed US order exchange code: {exchange_code}")
    if qty <= 0 or price <= 0:
        raise ValueError("qty and price must be positive for overseas limit order")
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    import requests

    cano, product = _credentials_account(mode)
    body = {
        "CANO": cano, "ACNT_PRDT_CD": product, "OVRS_EXCG_CD": exchange_code,
        "PDNO": symbol, "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": f"{float(price):.2f}",
        "CTAC_TLNO": "", "MGCO_APTM_ODNO": "", "SLL_TYPE": "00" if side == "SELL" else "",
        "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
    }
    tr = _tr_id(
        mode,
        TR_OVERSEAS_US_BUY_REAL if side == "BUY" else TR_OVERSEAS_US_SELL_REAL,
        TR_OVERSEAS_US_BUY_MOCK if side == "BUY" else TR_OVERSEAS_US_SELL_MOCK,
    )
    resp = requests.post(
        f"{_get_base_url(mode)}{OVERSEAS_STOCK_ORDER_ENDPOINT}",
        headers=_post_headers(mode, tr, body),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    output = _extract_output(data)
    order_id = _extract_order_id(output)
    rt_cd = str(data.get("rt_cd") or "")
    return OverseasOrderResult(
        success=rt_cd == "0" and bool(order_id),
        order_id=order_id,
        symbol=symbol,
        side=side,
        requested_qty=int(qty),
        rt_cd=rt_cd,
        msg_cd=str(data.get("msg_cd") or ""),
        msg1=str(data.get("msg1") or ""),
        raw=data,
    )


def place_overseas_market_order(
    mode: str, symbol: str, side: str, qty: int, *, exchange_code: str = "NASD",
) -> OverseasOrderResult:
    """Market BUY/SELL remains blocked unless an exact immediate-order code is confirmed."""
    raise KisOverseasApiConfirmationRequired(f"US regular market {side.upper()} order code")


def cancel_overseas_order(  # type: ignore[no-redef]
    mode: str, order_id: str, symbol: str, *, exchange_code: str = "NASD", qty: int = 1,
) -> OverseasOrderResult:
    """Cancel an overseas stock order. Official TR: TTTT1004U/VTTT1004U."""
    if exchange_code not in US_ORDER_EXCHANGES:
        raise KisOverseasApiConfirmationRequired(f"unconfirmed US cancel exchange code: {exchange_code}")
    if not order_id:
        raise ValueError("order_id is required")
    import requests

    cano, product = _credentials_account(mode)
    body = {
        "CANO": cano, "ACNT_PRDT_CD": product, "OVRS_EXCG_CD": exchange_code,
        "PDNO": symbol, "ORGN_ODNO": order_id, "RVSE_CNCL_DVSN_CD": "02",
        "ORD_QTY": str(int(max(qty, 1))), "OVRS_ORD_UNPR": "0",
        "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0",
    }
    tr = _tr_id(mode, TR_OVERSEAS_US_CANCEL_REAL, TR_OVERSEAS_US_CANCEL_MOCK)
    resp = requests.post(
        f"{_get_base_url(mode)}{OVERSEAS_STOCK_CANCEL_ENDPOINT}",
        headers=_post_headers(mode, tr, body),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    output = _extract_output(data)
    rt_cd = str(data.get("rt_cd") or "")
    return OverseasOrderResult(
        success=rt_cd == "0",
        order_id=_extract_order_id(output) or order_id,
        symbol=symbol,
        side="CANCEL",
        requested_qty=int(max(qty, 1)),
        rt_cd=rt_cd,
        msg_cd=str(data.get("msg_cd") or ""),
        msg1=str(data.get("msg1") or ""),
        raw=data,
    )


def amend_overseas_order(
    mode: str, order_id: str, symbol: str, qty: int, price: float, *, exchange_code: str = "NASD",
) -> OverseasOrderResult:
    """Amend an overseas stock order. Official TR: TTTT1004U/VTTT1004U."""
    if exchange_code not in US_ORDER_EXCHANGES:
        raise KisOverseasApiConfirmationRequired(f"unconfirmed US amend exchange code: {exchange_code}")
    if not order_id or qty <= 0 or price <= 0:
        raise ValueError("order_id, qty and price are required")
    import requests

    cano, product = _credentials_account(mode)
    body = {
        "CANO": cano, "ACNT_PRDT_CD": product, "OVRS_EXCG_CD": exchange_code,
        "PDNO": symbol, "ORGN_ODNO": order_id, "RVSE_CNCL_DVSN_CD": "01",
        "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": f"{float(price):.2f}",
        "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0",
    }
    tr = _tr_id(mode, TR_OVERSEAS_US_CANCEL_REAL, TR_OVERSEAS_US_CANCEL_MOCK)
    resp = requests.post(
        f"{_get_base_url(mode)}{OVERSEAS_STOCK_CANCEL_ENDPOINT}",
        headers=_post_headers(mode, tr, body),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    output = _extract_output(data)
    rt_cd = str(data.get("rt_cd") or "")
    return OverseasOrderResult(
        success=rt_cd == "0",
        order_id=_extract_order_id(output) or order_id,
        symbol=symbol,
        side="AMEND",
        requested_qty=int(qty),
        rt_cd=rt_cd,
        msg_cd=str(data.get("msg_cd") or ""),
        msg1=str(data.get("msg1") or ""),
        raw=data,
    )


def fetch_overseas_open_orders(  # type: ignore[no-redef]
    mode: str, symbol: str = "", *, exchange_code: str = "NASD",
) -> tuple[list[OverseasOrderRow], Optional[str], dict[str, Any]]:
    """Read overseas open orders. Official TR: TTTS3018R/VTTS3018R."""
    if mode == "mock":
        return [], "MOCK_OPEN_ORDERS_UNSUPPORTED_BY_KIS", {"rt_cd": "", "msg_cd": "", "msg1": "mock open orders unsupported"}
    try:
        import requests

        from app.data_sources.kis_overseas_minute import _auth_headers

        cano, product = _credentials_account(mode)
        resp = requests.get(
            f"{_get_base_url(mode)}{OVERSEAS_STOCK_OPEN_ORDERS_ENDPOINT}",
            headers=_auth_headers(mode, _tr_id(mode, TR_OVERSEAS_OPEN_ORDERS_REAL, TR_OVERSEAS_OPEN_ORDERS_MOCK)),
            params={
                "CANO": cano, "ACNT_PRDT_CD": product, "OVRS_EXCG_CD": exchange_code,
                "SORT_SQN": "DS", "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("output", []) if isinstance(body, dict) else []
        parsed = [_order_row(dict(row)) for row in (rows or []) if isinstance(row, dict)]
        if symbol:
            parsed = [row for row in parsed if row.symbol == symbol or not row.symbol]
        error = None if str(body.get("rt_cd") or "") in {"", "0"} else str(body.get("msg1") or body)
        return parsed, error, body
    except Exception as exc:  # pragma: no cover - real network path
        return [], repr(exc), {}


def fetch_overseas_fills(  # type: ignore[no-redef]
    mode: str,
    symbol: str = "",
    *,
    exchange_code: str = "%",
    start_date: str = "",
    end_date: str = "",
) -> tuple[list[OverseasOrderRow], Optional[str], dict[str, Any]]:
    """Read overseas order/fill history. Official TR: TTTS3035R/VTTS3035R."""
    try:
        import requests

        from app.data_sources.kis_overseas_minute import _auth_headers

        today = datetime.now(config.ET).strftime("%Y%m%d")
        cano, product = _credentials_account(mode)
        mock = mode == "mock"
        resp = requests.get(
            f"{_get_base_url(mode)}{OVERSEAS_STOCK_FILLS_ENDPOINT}",
            headers=_auth_headers(mode, _tr_id(mode, TR_OVERSEAS_FILLS_REAL, TR_OVERSEAS_FILLS_MOCK)),
            params={
                "CANO": cano, "ACNT_PRDT_CD": product, "PDNO": "" if mock else (symbol or "%"),
                "ORD_STRT_DT": start_date or today, "ORD_END_DT": end_date or today,
                "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00",
                "OVRS_EXCG_CD": "" if mock else exchange_code, "SORT_SQN": "DS",
                "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
                "CTX_AREA_NK200": "", "CTX_AREA_FK200": "",
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("output", []) if isinstance(body, dict) else []
        parsed = [_order_row(dict(row)) for row in (rows or []) if isinstance(row, dict)]
        if symbol:
            parsed = [row for row in parsed if row.symbol == symbol or not row.symbol]
        error = None if str(body.get("rt_cd") or "") in {"", "0"} else str(body.get("msg1") or body)
        return parsed, error, body
    except Exception as exc:  # pragma: no cover - real network path
        return [], repr(exc), {}


def fetch_overseas_market_calendar(mode: str) -> None:
    """미국 휴장·거래가능시간 조회 — KIS TR 존재 여부 미확인. 현재는
    app.trading.tsla_auto.market_session의 자체 캘린더로 대체한다(§미국시장
    캘린더 — 차단 아님, 자체 캘린더로 시작 가능)."""
    raise KisOverseasApiConfirmationRequired("미국 휴장·거래가능시간 조회 (overseas market calendar)")
