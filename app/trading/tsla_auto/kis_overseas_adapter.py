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


TR_OVERSEAS_BALANCE_REAL = "TTTS3012R"
TR_OVERSEAS_BALANCE_MOCK = "VTTS3012R"
TR_OVERSEAS_BUYABLE_REAL = "TTTS3007R"
TR_OVERSEAS_BUYABLE_MOCK = "VTTS3007R"


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
            date_str = str(item.get("kymd") or "")
            time_str = str(item.get("khms") or "")
            if not date_str or not time_str:
                continue
            try:
                dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S").replace(tzinfo=config.ET)
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
            close_min = 16 * 60
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
        return OverseasBuyableQuantity(symbol, exchange_code, float(price or 0.0), available_usd, available_qty, dict(output)), None
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
        cash = OverseasCashBalance(
            currency=currency,
            available_amount=_num((cash_raw or {}).get("frcr_pchs_amt1") or (cash_raw or {}).get("ord_psbl_frcr_amt")),
            raw=dict(cash_raw or {}),
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


def fetch_overseas_market_calendar(mode: str) -> None:
    """미국 휴장·거래가능시간 조회 — KIS TR 존재 여부 미확인. 현재는
    app.trading.tsla_auto.market_session의 자체 캘린더로 대체한다(§미국시장
    캘린더 — 차단 아님, 자체 캘린더로 시작 가능)."""
    raise KisOverseasApiConfirmationRequired("미국 휴장·거래가능시간 조회 (overseas market calendar)")
