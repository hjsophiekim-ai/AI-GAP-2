"""
diagnose_kis_token.py

KIS 토큰 발급 전체 진단 (mock + real).
실행: python scripts/diagnose_kis_token.py [mock|real]
     (인자 없으면 mock + real 모두 진단)

출력 항목:
  - 환경변수 존재 여부 (값 자체는 절대 출력 안 함)
  - DEFAULT_TRADING_MODE, ENABLE_REAL_TRADING 값
  - tokenP 요청 URL, 응답 status_code, rt_cd, msg_cd, msg1
  - 토큰 파일 캐시 상태

API 키 / 시크릿 / 계좌번호는 절대 출력하지 않습니다.
"""

import sys
import os
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

BASE_URL_MOCK = "https://openapivts.koreainvestment.com:29443"
BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"

# ── 모드별 환경변수 목록 ──────────────────────────────────────────────────────

_ENV_VARS = {
    "mock": [
        "KIS_MOCK_APP_KEY",
        "KIS_MOCK_APP_SECRET",
        "KIS_MOCK_ACCOUNT_NO",
        "KIS_MOCK_ACCOUNT_PRODUCT_CODE",
        "KIS_MOCK_CANO",
        "KIS_MOCK_ACNT_PRDT_CD",
    ],
    "real": [
        "KIS_REAL_APP_KEY",
        "KIS_REAL_APP_SECRET",
        "KIS_ACCOUNT_NO",
        "KIS_ACCOUNT_PRODUCT_CODE",
        "KIS_REAL_CANO",
        "KIS_REAL_ACNT_PRDT_CD",
    ],
}

_GLOBAL_VARS = [
    "DEFAULT_TRADING_MODE",
    "ENABLE_REAL_TRADING",
]


def _sep(title: str = "") -> None:
    if title:
        print(f"\n{'─'*60}")
        print(f"  {title}")
        print(f"{'─'*60}")
    else:
        print(f"{'─'*60}")


def _check_env(mode: str) -> dict[str, bool]:
    result = {}
    for k in _ENV_VARS[mode]:
        result[k] = bool(os.getenv(k, "").strip())
    return result


def _check_token_cache(mode: str) -> dict:
    cache_path = _REPO_ROOT / "data" / "cache" / f"kis_token_{mode}.json"
    if not cache_path.exists():
        return {"exists": False, "path": str(cache_path)}
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        expires_at_str = data.get("expires_at", "")
        base_url = data.get("base_url", "")
        valid = False
        if expires_at_str:
            exp_dt = datetime.fromisoformat(expires_at_str)
            valid = datetime.now() < exp_dt - timedelta(minutes=5)
        return {
            "exists": True,
            "path": str(cache_path),
            "expires_at": expires_at_str,
            "valid": valid,
            "base_url": base_url,
        }
    except Exception as e:
        return {"exists": True, "path": str(cache_path), "parse_error": str(e)}


def _get_credentials(mode: str) -> tuple[str, str, str]:
    """앱키/시크릿 가져오기 (값은 리턴하지만 절대 출력 금지)."""
    if mode == "mock":
        key = os.getenv("KIS_MOCK_APP_KEY", "").strip()
        secret = os.getenv("KIS_MOCK_APP_SECRET", "").strip()
        base_url = BASE_URL_MOCK
    else:
        key = os.getenv("KIS_REAL_APP_KEY", "").strip()
        secret = os.getenv("KIS_REAL_APP_SECRET", "").strip()
        base_url = BASE_URL_REAL
    return key, secret, base_url


def _test_token_api(mode: str, key: str, secret: str, base_url: str) -> dict:
    """tokenP API 직접 호출 (캐시 우회)."""
    url = f"{base_url}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": key,
        "appsecret": secret,
    }
    try:
        resp = requests.post(url, json=body, timeout=12)
        http_status = resp.status_code
        try:
            resp_data = resp.json()
        except Exception:
            resp_data = {}
        rt_cd = resp_data.get("rt_cd", "")
        msg_cd = resp_data.get("msg_cd", "")
        msg1 = resp_data.get("msg1", resp_data.get("error_description", ""))
        token_present = bool(resp_data.get("access_token", ""))
        return {
            "url": url,
            "http_status": http_status,
            "success": http_status == 200 and token_present,
            "rt_cd": rt_cd,
            "msg_cd": msg_cd,
            "msg1": msg1,
            "token_received": token_present,
        }
    except Exception as exc:
        return {"url": url, "error": str(exc)}


def _diagnose_mode(mode: str) -> None:
    _sep(f"[{mode.upper()}] 진단")

    base_url = BASE_URL_MOCK if mode == "mock" else BASE_URL_REAL
    print(f"  base_url: {base_url}")

    # 1. 환경변수 체크
    print("\n  [환경변수]")
    env_result = _check_env(mode)
    all_ok = True
    for k, v in env_result.items():
        icon = "OK" if v else "NG"
        print(f"    [{icon}] {k}: {'존재' if v else '없음'}")
        if not v:
            all_ok = False

    # 2. 토큰 캐시
    print("\n  [토큰 파일 캐시]")
    cache = _check_token_cache(mode)
    if not cache.get("exists"):
        print(f"    없음: {cache.get('path', '')}")
    elif "parse_error" in cache:
        print(f"    파싱 오류: {cache['parse_error']}")
    else:
        valid_str = "유효" if cache.get("valid") else "만료됨"
        print(f"    상태: {valid_str}")
        print(f"    만료: {cache.get('expires_at', '')}")
        print(f"    base_url: {cache.get('base_url', '')}")

    # 3. tokenP API 호출
    print("\n  [tokenP API 호출]")
    key, secret, api_base = _get_credentials(mode)
    if not (key and secret):
        app_key_env = "KIS_MOCK_APP_KEY" if mode == "mock" else "KIS_REAL_APP_KEY"
        app_secret_env = "KIS_MOCK_APP_SECRET" if mode == "mock" else "KIS_REAL_APP_SECRET"
        print(f"    건너뜀: {app_key_env} 또는 {app_secret_env} 없음")
        return

    result = _test_token_api(mode, key, secret, api_base)
    if "error" in result:
        print(f"    연결 오류: {result['error']}")
        return

    icon = "OK" if result["success"] else "NG"
    print(f"    요청 URL: {result['url']}")
    print(f"    HTTP 상태: {result['http_status']}")
    print(f"    결과: [{icon}] {'성공' if result['success'] else '실패'}")
    if result.get("rt_cd"):
        print(f"    rt_cd  : {result['rt_cd']!r}")
    if result.get("msg_cd"):
        print(f"    msg_cd : {result['msg_cd']!r}")
    if result.get("msg1"):
        print(f"    msg1   : {result['msg1']!r}")
    print(f"    토큰 수신: {result['token_received']}")


def main() -> None:
    modes_to_run: list[str]
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg not in ("mock", "real"):
            print("사용법: python scripts/diagnose_kis_token.py [mock|real]")
            sys.exit(1)
        modes_to_run = [arg]
    else:
        modes_to_run = ["mock", "real"]

    print(f"\n{'='*60}")
    print("  KIS 토큰 발급 전체 진단")
    print(f"  실행시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # 전역 변수
    _sep("전역 환경변수")
    for gv in _GLOBAL_VARS:
        val = os.getenv(gv, "")
        display = repr(val) if val else "(미설정)"
        print(f"  {gv}: {display}")
    print()
    print("  ※ 토큰 발급은 장중 여부와 무관하게 가능해야 합니다.")
    print("    실패 시 대부분 app key/secret, base_url, 환경변수 문제입니다.")

    for mode in modes_to_run:
        _diagnose_mode(mode)

    _sep()
    print("  진단 완료.")
    print("  ※ API 키/시크릿/계좌번호/토큰 값은 출력하지 않습니다.\n")


if __name__ == "__main__":
    main()
