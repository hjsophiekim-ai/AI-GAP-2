"""Naver Finance fallback for foreign/institutional net-buy (외국인/기관 순매수)."""

from __future__ import annotations

import logging
from io import StringIO
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _decode_response(response: requests.Response) -> str:
    response.encoding = "euc-kr"
    text = response.text
    if not text or "�" in text[:500]:
        response.encoding = response.apparent_encoding or "euc-kr"
        text = response.text
    return text


def _to_number(value: object) -> Optional[float]:
    if value is None or pd.isna(value):
        return None
    cleaned = str(value).replace(",", "").replace("+", "").strip()
    if cleaned in ("", "-"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_naver_investor_flow(code: str = "000660") -> dict:
    """Fetch the most recent day's 외국인/기관 순매매량 from Naver Finance.

    Returns {"foreign_net_buy": int|None, "institution_net_buy": int|None,
             "date": str|None, "status": "success"|"failed", "error": str|None}
    """
    result = {
        "foreign_net_buy": None,
        "institution_net_buy": None,
        "date": None,
        "status": "failed",
        "error": None,
    }
    try:
        url = f"https://finance.naver.com/item/frgn.naver?code={code}"
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        text = _decode_response(response)

        tables = pd.read_html(StringIO(text))
        target = None
        for table in tables:
            cols = [str(c) for c in table.columns]
            if any("기관" in c for c in cols) and any("외국인" in c for c in cols):
                target = table
                break
        if target is None:
            result["error"] = "investor flow table not found"
            return result

        target = target.dropna(how="all").reset_index(drop=True)
        date_col = next((c for c in target.columns if "날짜" in str(c)), None)
        inst_col = next((c for c in target.columns if "기관" in str(c) and "순매매" in str(c)), None)
        frgn_col = next((c for c in target.columns if "외국인" in str(c) and "순매매" in str(c)), None)
        if inst_col is None or frgn_col is None:
            result["error"] = "investor flow columns not found"
            return result

        row = target.iloc[0]
        foreign = _to_number(row.get(frgn_col))
        institution = _to_number(row.get(inst_col))
        if foreign is None and institution is None:
            result["error"] = "investor flow row empty"
            return result

        result.update(
            foreign_net_buy=int(foreign) if foreign is not None else None,
            institution_net_buy=int(institution) if institution is not None else None,
            date=str(row.get(date_col)) if date_col else None,
            status="success",
        )
        return result
    except Exception as exc:
        logger.warning("[NAVER] 투자자매매동향 조회 실패 %s: %s", code, exc)
        result["error"] = str(exc)
        return result
