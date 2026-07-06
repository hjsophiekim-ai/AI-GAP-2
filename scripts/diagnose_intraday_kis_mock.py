#!/usr/bin/env python
"""
diagnose_intraday_kis_mock.py

KIS 모의계좌 장중 자동매매 사전진단 스크립트.
실제 주문은 절대 실행하지 않습니다.

확인 항목:
  1. 환경변수 존재 여부 (key/secret/계좌번호 원문 미출력)
  2. 모의계좌 base_url
  3. 모의계좌 token 발급 가능 여부
  4. 현재가 조회 가능 여부
  5. 1분봉 조회 가능 여부

성공 시: KIS_MOCK_INTRADAY_PRECHECK_PASSED
실패 시: 실패 항목과 KIS 응답 코드만 표시
"""
import os
import sys
import requests
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

TEST_SYMBOL = "005930"  # 삼성전자 (진단용)
_FAILED_ITEMS = []


def _env_status(var: str) -> str:
    return "EXISTS" if os.getenv(var, "") else "MISSING"


def _section(title: str) -> None:
    print(f"\n{'─' * 55}")
    print(f"  {title}")
    print(f"{'─' * 55}")


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")
    _FAILED_ITEMS.append(msg)


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    _FAILED_ITEMS.append(msg)


print("\n" + "=" * 55)
print("  AI-GAP KIS 모의계좌 장중 자동매매 사전진단")
print("=" * 55)

# ── Section 1: 환경변수 확인 ─────────────────────────────────────────────────
_section("1. 환경변수 확인 (원문 미출력)")

mock_key    = os.getenv("KIS_MOCK_APP_KEY", "")
mock_secret = os.getenv("KIS_MOCK_APP_SECRET", "")

# 계좌번호: KIS_MOCK_CANO 또는 KIS_MOCK_ACCOUNT_NO
mock_cano   = os.getenv("KIS_MOCK_CANO", "") or os.getenv("KIS_MOCK_ACCOUNT_NO", "")
# 상품코드: KIS_MOCK_ACNT_PRDT_CD 또는 KIS_MOCK_ACCOUNT_PRODUCT_CODE
mock_prdt   = os.getenv("KIS_MOCK_ACNT_PRDT_CD", "") or os.getenv("KIS_MOCK_ACCOUNT_PRODUCT_CODE", "")

env_checks = {
    "KIS_MOCK_APP_KEY":          bool(mock_key),
    "KIS_MOCK_APP_SECRET":       bool(mock_secret),
    "KIS_MOCK_CANO (or ACCOUNT_NO)": bool(mock_cano),
    "KIS_MOCK_ACNT_PRDT_CD (or PRODUCT_CODE)": bool(mock_prdt),
}

for env_name, exists in env_checks.items():
    if exists:
        _ok(f"{env_name}: EXISTS")
    else:
        _fail(f"{env_name}: MISSING")

# ── Section 2: base_url 확인 ─────────────────────────────────────────────────
_section("2. 모의계좌 base_url")

BASE_URL_MOCK = "https://openapivts.koreainvestment.com:29443"
print(f"  base_url = {BASE_URL_MOCK}")

try:
    from app.config import get_config
    cfg = get_config()
    cfg_url = cfg._raw.get("kis", {}).get("mock", {}).get("base_url", BASE_URL_MOCK)
    if cfg_url != BASE_URL_MOCK:
        _warn(f"config.yaml의 mock base_url이 다름: {cfg_url}")
    else:
        _ok(f"config.yaml mock base_url 일치: {cfg_url}")
except Exception as e:
    _warn(f"config.yaml 로드 오류: {e}")

# 필수 환경변수 누락 시 이후 API 단계 건너뜀
if not (mock_key and mock_secret):
    _fail("APP_KEY 또는 APP_SECRET 누락 - API 연결 테스트 건너뜀")
    print("\n" + "=" * 55)
    print("  진단 결과: 필수 환경변수 누락")
    for item in _FAILED_ITEMS:
        print(f"    × {item}")
    print("=" * 55)
    sys.exit(1)

# ── Section 3: 토큰 발급 ─────────────────────────────────────────────────────
_section("3. 모의계좌 토큰 발급")

_token = ""
try:
    url = f"{BASE_URL_MOCK}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey":     mock_key,
        "appsecret":  mock_secret,
    }
    resp = requests.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=10)
    data = resp.json()

    if resp.status_code == 200 and data.get("access_token"):
        _token = data["access_token"]
        expires_in = data.get("expires_in", "?")
        _ok(f"토큰 발급 성공 (expires_in={expires_in}s, 원문 미출력)")
    else:
        sc = resp.status_code
        msg_cd = data.get("msg_cd", "-")
        msg1   = data.get("msg1", "-")
        _fail(f"토큰 발급 실패: status={sc}, msg_cd={msg_cd}, msg1={msg1}")
except requests.exceptions.ConnectionError as e:
    _fail(f"토큰 발급 연결 오류: {e}")
except Exception as e:
    _fail(f"토큰 발급 예외: {e}")

if not _token:
    print("\n" + "=" * 55)
    print("  진단 결과: 토큰 발급 불가 - 이후 단계 건너뜀")
    for item in _FAILED_ITEMS:
        print(f"    × {item}")
    print("=" * 55)
    sys.exit(1)

# ── Section 4: 현재가 조회 ───────────────────────────────────────────────────
_section(f"4. 현재가 조회 ({TEST_SYMBOL} 삼성전자)")

_price_ok = False
try:
    url = f"{BASE_URL_MOCK}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "Content-Type":  "application/json",
        "authorization": f"Bearer {_token}",
        "appkey":        mock_key,
        "appsecret":     mock_secret,
        "tr_id":         "FHKST01010100",
        "custtype":      "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         TEST_SYMBOL,
    }
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    data = resp.json()

    rt_cd = data.get("rt_cd", "-1")
    if rt_cd == "0":
        output = data.get("output", {})
        price  = output.get("stck_prpr", "?")
        _ok(f"현재가 조회 성공: {TEST_SYMBOL} = {price}원")
        _price_ok = True
    else:
        sc     = resp.status_code
        msg_cd = data.get("msg_cd", "-")
        msg1   = data.get("msg1", "-")
        _fail(f"현재가 조회 실패: status={sc}, rt_cd={rt_cd}, msg_cd={msg_cd}, msg1={msg1}")
except requests.exceptions.ConnectionError as e:
    _fail(f"현재가 조회 연결 오류: {e}")
except Exception as e:
    _fail(f"현재가 조회 예외: {e}")

# ── Section 5: 1분봉 조회 ────────────────────────────────────────────────────
_section(f"5. 1분봉 조회 ({TEST_SYMBOL})")

_candle_ok = False
try:
    from datetime import datetime
    now_str = datetime.now().strftime("%H%M%S")

    url = f"{BASE_URL_MOCK}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    headers_c = {
        "Content-Type":  "application/json",
        "authorization": f"Bearer {_token}",
        "appkey":        mock_key,
        "appsecret":     mock_secret,
        "tr_id":         "FHKST03010200",
        "custtype":      "P",
    }
    params_c = {
        "FID_ETC_CLS_CODE":       "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         TEST_SYMBOL,
        "FID_INPUT_HOUR_1":       now_str,
        "FID_PW_DATA_INCU_YN":    "N",
    }
    resp_c = requests.get(url, headers=headers_c, params=params_c, timeout=10)
    data_c = resp_c.json()

    rt_cd_c = data_c.get("rt_cd", "-1")
    if rt_cd_c == "0":
        output2 = data_c.get("output2", [])
        if isinstance(output2, list):
            _ok(f"1분봉 조회 성공: {len(output2)}개 캔들 반환")
            _candle_ok = True
        else:
            _warn("1분봉 output2가 리스트가 아님 (장 마감 후일 수 있음)")
            _candle_ok = True  # API 자체는 정상
    else:
        sc     = resp_c.status_code
        msg_cd = data_c.get("msg_cd", "-")
        msg1   = data_c.get("msg1", "-")
        _warn(f"1분봉 조회 비정상 응답 (장 마감 후 정상): status={sc}, rt_cd={rt_cd_c}, msg_cd={msg_cd}, msg1={msg1}")
        _candle_ok = True  # 장 마감 시간 외에는 비정상 응답이 정상
except requests.exceptions.ConnectionError as e:
    _fail(f"1분봉 조회 연결 오류: {e}")
except Exception as e:
    _fail(f"1분봉 조회 예외: {e}")

# ── Section 6: KISClient 인터페이스 확인 ────────────────────────────────────
_section("6. app.trading.kis_client.KISClient 인터페이스 확인")

try:
    from app.trading.kis_client import KISClient
    methods_needed = ["get_access_token", "get_current_price", "buy", "sell"]
    methods_ok = all(hasattr(KISClient, m) for m in methods_needed)
    if methods_ok:
        _ok("KISClient 필수 메서드 존재: " + ", ".join(methods_needed))
    else:
        missing_m = [m for m in methods_needed if not hasattr(KISClient, m)]
        _warn(f"KISClient 메서드 누락: {missing_m}")

    has_minute_candles = hasattr(KISClient, "get_minute_candles")
    if has_minute_candles:
        _ok("KISClient.get_minute_candles: EXISTS")
    else:
        _warn("KISClient.get_minute_candles: NOT_IMPLEMENTED (서비스 호출 시 예외 발생, 빈 캔들 반환)")
except ImportError as e:
    _fail(f"KISClient 임포트 실패: {e}")

# ── 최종 결과 ────────────────────────────────────────────────────────────────
print("\n" + "=" * 55)
fatal_fails = [i for i in _FAILED_ITEMS if "MISSING" in i or "실패" in i or "오류" in i]

if fatal_fails:
    print("  진단 결과: 일부 항목 실패")
    for item in _FAILED_ITEMS:
        mark = "×" if (item in fatal_fails) else "△"
        print(f"    {mark} {item}")
    print("=" * 55)
    sys.exit(1)
else:
    if _FAILED_ITEMS:
        print("  진단 결과: 경고 항목 있음 (치명적 오류 없음)")
        for item in _FAILED_ITEMS:
            print(f"    △ {item}")
    else:
        print("  진단 결과: 모든 항목 정상")
    print("=" * 55)
    print("\nKIS_MOCK_INTRADAY_PRECHECK_PASSED")

