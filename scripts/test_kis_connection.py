"""
KIS API 연결 테스트 스크립트.

실행:
    python scripts/test_kis_connection.py

- .env 로딩 확인
- mock 계좌 설정 확인
- mock access token 발급 테스트
- 삼성전자(005930) 현재가 조회 테스트
- mock 잔고 조회 테스트
- real 계좌 설정 존재 여부 확인 (주문 없음)
- DART API 키 존재 여부 확인
"""

import sys
import os
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 프로젝트 루트를 PYTHONPATH에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from app.config import get_config, get_kis_account_config, get_dart_api_key
from app.logger import logger


def section(title: str) -> None:
    print(f"\n{'='*55}")
    print(f"  {title}")
    print(f"{'='*55}")


def ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠️  {msg}")


def fail(msg: str) -> None:
    print(f"  ❌ {msg}")


def main():
    cfg = get_config()
    print("\n[INFO] AI-GAP KIS API 연결 테스트")
    print(f"   현재 모드: {cfg.mode}")

    # ── 1. .env 로딩 ───────────────────────────────────────────────────
    section("1. .env 환경변수 확인")
    required_mock = [
        "KIS_MOCK_APP_KEY", "KIS_MOCK_APP_SECRET",
        "KIS_MOCK_ACCOUNT_NO", "KIS_MOCK_ACCOUNT_PRODUCT_CODE",
    ]
    required_real = [
        "KIS_REAL_APP_KEY", "KIS_REAL_APP_SECRET",
        "KIS_ACCOUNT_NO", "KIS_ACCOUNT_PRODUCT_CODE",
    ]
    all_ok = True
    for k in required_mock:
        v = os.getenv(k)
        if v:
            ok(f"{k}: SET")
        else:
            fail(f"{k}: NOT SET")
            all_ok = False

    if not all_ok:
        fail(".env에 mock 계좌 환경변수가 없습니다. 테스트를 중단합니다.")
        return

    # ── 2. mock 계좌 설정 확인 ──────────────────────────────────────────
    section("2. mock 계좌 설정 확인")
    try:
        mock_cfg = get_kis_account_config("mock")
        ok(f"account_no: ***{mock_cfg['account_no'][-4:]}")
        ok(f"product_code: {mock_cfg['product_code']}")
        ok(f"base_url: {mock_cfg['base_url']}")
    except ValueError as e:
        fail(f"mock 계좌 설정 오류: {e}")
        return

    # ── 3. mock access token 발급 ────────────────────────────────────────
    section("3. mock access token 발급")
    from app.trading.kis_client import create_kis_client
    mock_client = create_kis_client("mock")
    if mock_client is None:
        fail("mock KISClient 생성 실패")
        return

    try:
        token = mock_client.get_access_token()
        if token:
            ok(f"토큰 발급 성공 (length={len(token)})")
        else:
            fail("토큰이 비어있습니다.")
            return
    except Exception as e:
        fail(f"토큰 발급 실패: {e}")
        return

    # ── 4. 현재가 조회 (삼성전자 005930) ────────────────────────────────
    section("4. 삼성전자(005930) 현재가 조회")
    try:
        price_data = mock_client.get_current_price("005930")
        if price_data:
            ok(f"현재가: {price_data['current_price']:,.0f}원")
            ok(f"시가: {price_data['open']:,.0f}원")
            ok(f"등락률: {price_data['change_rate']:.2f}%")
            ok(f"거래대금: {price_data['trade_value']:,.0f}원")
        else:
            warn("현재가 데이터 없음 (장 마감 또는 API 응답 없음)")
    except Exception as e:
        fail(f"현재가 조회 실패: {e}")

    # ── 5. mock 잔고 조회 ────────────────────────────────────────────────
    section("5. mock 잔고 조회")
    try:
        balance = mock_client.get_balance()
        if "error" not in balance:
            ok(f"예수금: {balance['cash']:,.0f}원")
            ok(f"보유종목: {len(balance['positions'])}개")
        else:
            warn(f"잔고 조회 오류: {balance.get('error')}")
    except Exception as e:
        fail(f"잔고 조회 실패: {e}")

    # ── 6. mock 주문가능금액 조회 ────────────────────────────────────────
    section("6. mock 주문가능금액 조회")
    try:
        buyable = mock_client.get_buyable_cash()
        ok(f"주문가능금액: {buyable:,.0f}원")
    except Exception as e:
        fail(f"주문가능금액 조회 실패: {e}")

    # ── 7. real 계좌 설정 존재 여부 ──────────────────────────────────────
    section("7. real 계좌 설정 확인 (주문 없음)")
    try:
        real_cfg = get_kis_account_config("real")
        ok(f"real account_no: ***{real_cfg['account_no'][-4:]}")
        ok("real 계좌 환경변수: SET")
    except ValueError as e:
        warn(f"real 계좌 환경변수 미설정: {e}")

    real_enabled = cfg.real_trading_enabled()
    if real_enabled:
        warn("⚠️  safety.enable_real_trading = true  ← 실전투자 활성화 상태!")
    else:
        ok("safety.enable_real_trading = false (안전)")

    # ── 8. DART API 키 확인 ──────────────────────────────────────────────
    section("8. DART API 키 확인")
    dart_key = get_dart_api_key()
    if dart_key:
        ok("DART_API_KEY: SET")
    else:
        warn("DART_API_KEY: NOT SET — 공시 점수 비활성화")

    # ── 요약 ─────────────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print("  테스트 완료")
    print(f"{'='*55}")
    print("  mock 주문 테스트: python scripts/test_mock_order.py --execute")
    print("  앱 실행: streamlit run app/ui/streamlit_app.py")
    print()


if __name__ == "__main__":
    main()
