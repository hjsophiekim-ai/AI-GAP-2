"""
kis_overseas_minute.py — KIS 해외주식 현재가·분봉 수집 모듈.

MU(마이크론) 종목의 1분봉·현재가를 수집하고
프리마켓/정규장/애프터마켓 세션별로 분리 저장합니다.

KIS 해외주식 API는 실전(real) 키를 기본 사용합니다.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

try:
    from app.data.market_data_validator import parse_mu_price_str, validate_mu_price, auto_fix_mu_price
    _VALIDATOR_OK = True
except ImportError:
    _VALIDATOR_OK = False

    def parse_mu_price_str(raw):  # type: ignore[misc]
        if raw is None:
            return None
        try:
            val = float(str(raw).replace(",", "").strip())
            if val <= 0:
                return None
            if 20 <= val <= 500:
                return val
            for div in (10, 100):
                fixed = val / div
                if 20 <= fixed <= 500:
                    return fixed
            return None
        except Exception:
            return None

_ROOT = Path(__file__).resolve().parent.parent.parent
_MICRON_DIR = _ROOT / "data" / "micron"
_TOKEN_CACHE_DIR = _ROOT / "data" / "cache"

# ── KIS API 설정 ─────────────────────────────────────────────────────────────
BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
BASE_URL_MOCK = "https://openapivts.koreainvestment.com:29443"

TR_OVERSEAS_CURRENT = "HHDFS00000300"   # 해외주식현재가상세
TR_OVERSEAS_MINUTE  = "HHDFS76950200"   # 해외주식분봉조회

# ── 한국시간(KST) 기준 세션 구간 (분 단위) ────────────────────────────────────
# 서머타임 미적용 기본값. 서머타임 시 PREMARKET_END를 22:30→21:30 등으로 조정.
_PREMARKET_START = 17 * 60        # 17:00
_PREMARKET_END   = 22 * 60 + 30   # 22:30
_REGULAR_END     = 5 * 60         # 05:00 (익일)
_AFTER_START     = 5 * 60         # 05:00
_AFTER_END       = 9 * 60         # 09:00

# ── 메모리 토큰 캐시 ──────────────────────────────────────────────────────────
_TOKEN_CACHE: dict[str, str] = {}
_TOKEN_EXPIRY: dict[str, datetime] = {}


# ── 인증 헬퍼 ────────────────────────────────────────────────────────────────

def _load_credentials(mode: str = "real") -> dict:
    """환경변수에서 KIS 인증 정보 로드."""
    if mode == "real":
        return {
            "app_key":    os.environ.get("KIS_REAL_APP_KEY", ""),
            "app_secret": os.environ.get("KIS_REAL_APP_SECRET", ""),
            "base_url":   os.environ.get("KIS_REAL_BASE_URL", BASE_URL_REAL),
        }
    return {
        "app_key":    os.environ.get("KIS_MOCK_APP_KEY", ""),
        "app_secret": os.environ.get("KIS_MOCK_APP_SECRET", ""),
        "base_url":   os.environ.get("KIS_MOCK_BASE_URL", BASE_URL_MOCK),
    }


def _token_cache_path(mode: str) -> Path:
    return _TOKEN_CACHE_DIR / f"kis_token_{mode}.json"


def _get_access_token(mode: str = "real") -> str:
    """
    액세스 토큰 발급.
    1) 메모리 캐시 → 2) 파일 캐시 → 3) tokenP API
    """
    now = datetime.now()

    # 1. 메모리 캐시 (5분 버퍼)
    if (
        mode in _TOKEN_CACHE
        and now < _TOKEN_EXPIRY.get(mode, datetime.min) - timedelta(minutes=5)
    ):
        return _TOKEN_CACHE[mode]

    # 2. 파일 캐시
    cache_path = _token_cache_path(mode)
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            expires_at = datetime.fromisoformat(data.get("expires_at", "2000-01-01"))
            if now < expires_at - timedelta(minutes=5) and data.get("access_token"):
                _TOKEN_CACHE[mode] = data["access_token"]
                _TOKEN_EXPIRY[mode] = expires_at
                return _TOKEN_CACHE[mode]
        except Exception:
            pass

    # 3. API 발급
    creds = _load_credentials(mode)
    if not creds["app_key"] or not creds["app_secret"]:
        raise ValueError(
            f"KIS {mode.upper()} 인증 정보 없음 — "
            f".env의 KIS_{mode.upper()}_APP_KEY / KIS_{mode.upper()}_APP_SECRET 확인"
        )

    url = f"{creds['base_url']}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": creds["app_key"],
        "appsecret": creds["app_secret"],
    }
    resp = requests.post(url, json=body, timeout=10)
    resp.raise_for_status()
    token_data = resp.json()

    token = token_data.get("access_token", "")
    if not token:
        raise ValueError(f"토큰 발급 실패: {token_data}")

    expires_in = int(token_data.get("expires_in", 86400))
    expires_at = now + timedelta(seconds=expires_in)
    _TOKEN_CACHE[mode] = token
    _TOKEN_EXPIRY[mode] = expires_at

    _TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(
            {"access_token": token, "expires_at": expires_at.isoformat(), "mode": mode},
            f,
        )

    return token


def _auth_headers(mode: str, tr_id: str) -> dict:
    """KIS API 공통 헤더 생성."""
    creds = _load_credentials(mode)
    token = _get_access_token(mode)
    return {
        "authorization": f"Bearer {token}",
        "appkey": creds["app_key"],
        "appsecret": creds["app_secret"],
        "tr_id": tr_id,
        "custtype": "P",
        "Content-Type": "application/json; charset=utf-8",
    }


# ── 세션 분류 ──────────────────────────────────────────────────────────────────

def classify_session(ts: datetime) -> str:
    """
    KST 타임스탬프를 세션명으로 분류.

    Returns
    -------
    "premarket" | "regular" | "aftermarket" | "unknown"
    """
    minutes = ts.hour * 60 + ts.minute
    if _PREMARKET_START <= minutes < _PREMARKET_END:
        return "premarket"
    if minutes >= _PREMARKET_END or minutes < _REGULAR_END:
        return "regular"
    if _AFTER_START <= minutes < _AFTER_END:
        return "aftermarket"
    return "unknown"


# ── API 호출 ──────────────────────────────────────────────────────────────────

def fetch_mu_current_price(mode: str = "real") -> Optional[dict]:
    """
    MU 현재가 조회 (HHDFS00000300).

    Returns
    -------
    dict | None
        {price, open, high, low, volume, timestamp} 또는 None
    """
    try:
        creds = _load_credentials(mode)
        url = f"{creds['base_url']}/uapi/overseas-price/v1/quotations/price"
        params = {"AUTH": "", "EXCD": "NAS", "SYMB": "MU"}
        resp = requests.get(
            url,
            headers=_auth_headers(mode, TR_OVERSEAS_CURRENT),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        output = resp.json().get("output", {})
        if not output:
            return None
        price = parse_mu_price_str(output.get("last"))
        if price is None:
            return None  # 가격 범위 검증 실패 또는 파싱 불가
        return {
            "price":     price,
            "open":      parse_mu_price_str(output.get("open")) or price,
            "high":      parse_mu_price_str(output.get("high")) or price,
            "low":       parse_mu_price_str(output.get("low")) or price,
            "volume":    int(output.get("tvol", 0) or 0),
            "symbol":    "MU",
            "timestamp": datetime.now().isoformat(),
        }
    except Exception:
        return None


def fetch_mu_1min_bars(
    mode: str = "real",
    nrec: int = 120,
) -> Optional[pd.DataFrame]:
    """
    MU 1분봉 조회 (HHDFS76950200).

    Parameters
    ----------
    mode : "real" or "mock"
    nrec : 최대 레코드 수 (최대 120)

    Returns
    -------
    DataFrame | None
        columns: [datetime, open, high, low, close, volume, session]
    """
    try:
        creds = _load_credentials(mode)
        url = (
            f"{creds['base_url']}"
            "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
        )
        params = {
            "AUTH": "",
            "EXCD": "NAS",
            "SYMB": "MU",
            "NMIN": "1",
            "PINC": "1",
            "NEXT": "",
            "NREC": str(min(nrec, 120)),
            "FILL": "",
            "KEYB": "",
        }
        resp = requests.get(
            url,
            headers=_auth_headers(mode, TR_OVERSEAS_MINUTE),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        output2 = resp.json().get("output2", [])
        if not output2:
            return None

        rows = []
        for item in output2:
            date_str = item.get("kymd", "")   # YYYYMMDD
            time_str = item.get("khms", "")   # HHMMSS
            if not date_str or not time_str:
                continue
            try:
                dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
                close_px = parse_mu_price_str(item.get("last"))
                if close_px is None:
                    continue  # 가격 범위 오류인 봉은 건너뜀
                rows.append({
                    "datetime": dt,
                    "open":     parse_mu_price_str(item.get("open")) or close_px,
                    "high":     parse_mu_price_str(item.get("high")) or close_px,
                    "low":      parse_mu_price_str(item.get("low")) or close_px,
                    "close":    close_px,
                    "volume":   int(item.get("evol", 0) or 0),
                })
            except Exception:
                continue

        if not rows:
            return None

        df = pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)
        df["session"] = df["datetime"].apply(classify_session)
        return df

    except Exception:
        return None


def fetch_mu_3min_bars(
    mode: str = "real",
    source_df: Optional[pd.DataFrame] = None,
) -> Optional[pd.DataFrame]:
    """
    3분봉 생성 — 1분봉을 resample해서 생성.

    Parameters
    ----------
    mode      : "real" or "mock"
    source_df : 기존 1분봉 DataFrame. None이면 API 호출.

    Returns
    -------
    DataFrame | None
        columns: [datetime, open, high, low, close, volume, session]
    """
    if source_df is None:
        source_df = fetch_mu_1min_bars(mode=mode)
    if source_df is None or source_df.empty:
        return None

    df = source_df.copy().set_index("datetime")
    ohlcv = (
        df[["open", "high", "low", "close", "volume"]]
        .resample("3min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["close"])   # close가 없는 빈 봉 제거
        .reset_index()
    )
    ohlcv["session"] = ohlcv["datetime"].apply(classify_session)
    return ohlcv


def build_session_summary(df_1min: pd.DataFrame) -> pd.DataFrame:
    """
    세션별 OHLCV 요약 생성.

    Returns
    -------
    DataFrame
        columns: [session, open, high, low, close, volume, bar_count, return_pct, updated_at]
    """
    if df_1min is None or df_1min.empty:
        return pd.DataFrame()

    rows = []
    for session_name in ["premarket", "regular", "aftermarket"]:
        sess = df_1min[df_1min["session"] == session_name]
        if sess.empty:
            continue
        o = float(sess.iloc[0]["open"])
        c = float(sess.iloc[-1]["close"])
        ret = (c / o - 1) * 100 if o > 0 else 0.0
        rows.append({
            "session":    session_name,
            "open":       o,
            "high":       float(sess["high"].max()),
            "low":        float(sess["low"].min()),
            "close":      c,
            "volume":     int(sess["volume"].sum()),
            "bar_count":  len(sess),
            "return_pct": round(ret, 4),
            "updated_at": datetime.now().isoformat(),
        })
    return pd.DataFrame(rows)


# ── 저장 ─────────────────────────────────────────────────────────────────────

def save_mu_data(
    df_1min: Optional[pd.DataFrame] = None,
    df_3min: Optional[pd.DataFrame] = None,
    df_summary: Optional[pd.DataFrame] = None,
) -> None:
    """분봉 데이터를 CSV로 저장."""
    _MICRON_DIR.mkdir(parents=True, exist_ok=True)
    if df_1min is not None and not df_1min.empty:
        df_1min.to_csv(_MICRON_DIR / "MU_1min.csv", index=False, encoding="utf-8-sig")
    if df_3min is not None and not df_3min.empty:
        df_3min.to_csv(_MICRON_DIR / "MU_3min.csv", index=False, encoding="utf-8-sig")
    if df_summary is not None and not df_summary.empty:
        df_summary.to_csv(_MICRON_DIR / "MU_session_summary.csv", index=False, encoding="utf-8-sig")


def collect_and_save_mu(mode: str = "real") -> dict:
    """
    MU 데이터 전체 수집 → 저장 → 결과 반환.

    Returns
    -------
    dict
        {current_price, df_1min, df_3min, df_summary, error}
    """
    result: dict = {
        "current_price": None,
        "df_1min":       None,
        "df_3min":       None,
        "df_summary":    None,
        "error":         None,
    }
    try:
        result["current_price"] = fetch_mu_current_price(mode=mode)
        df_1min = fetch_mu_1min_bars(mode=mode)
        if df_1min is not None and not df_1min.empty:
            df_3min  = fetch_mu_3min_bars(mode=mode, source_df=df_1min)
            df_sum   = build_session_summary(df_1min)
            result["df_1min"]   = df_1min
            result["df_3min"]   = df_3min
            result["df_summary"] = df_sum
            save_mu_data(df_1min, df_3min, df_sum)
        else:
            result["error"] = "분봉 데이터 없음 (장외 시간이거나 API 미응답)"
    except Exception as exc:
        result["error"] = str(exc)
    return result
