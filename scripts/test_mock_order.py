"""
KIS 모의투자 주문 테스트 스크립트.

기본 실행: 주문 없이 mock 계좌 연결만 확인합니다.
--execute 옵션: 실제로 mock 계좌에 소량 주문을 실행합니다.

실행:
    python scripts/test_mock_order.py           # 연결 확인만
    python scripts/test_mock_order.py --execute  # 실제 mock 주문 실행

주의:
    - mock(모의투자) 주문만 실행합니다.
    - real(실전) 주문은 절대 실행하지 않습니다.
"""

import sys
import argparse
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from app.trading.kis_client import create_kis_client
from app.logger import logger

TEST_SYMBOL = "005930"  # 삼성전자
TEST_QUANTITY = 1
TEST_PRICE_MARGIN = 0.98  # 현재가의 98% 지정가 (체결 가능성 낮게)


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


def main(execute: bool = False) -> None:
    print("\n🔍 KIS 모의투자 주문 테스트")
    print(f"   실행 모드: {'실제 주문' if execute else '연결 확인만 (--execute 옵션 없음)'}")
    if execute:
        print("   ⚠️  mock(모의투자) 계좌에 소량 주문이 실행됩니다.")
        print("      real(실전) 주문은 이 스크립트에서 절대 실행하지 않습니다.")

    # ── 1. mock 클라이언트 초기화 ──────────────────────────────────────────
    section("1. mock KISClient 초기화")
    mock_client = create_kis_client("mock")
    if mock_client is None:
        fail("mock KISClient 생성 실패 — .env의 KIS_MOCK_* 환경변수를 확인하세요.")
        return
    ok("mock KISClient 초기화 완료")

    # ── 2. 토큰 발급 ────────────────────────────────────────────────────────
    section("2. mock access token 발급")
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

    # ── 3. 현재가 조회 ────────────────────────────────────────────────────
    section(f"3. {TEST_SYMBOL} 현재가 조회")
    price_data = mock_client.get_current_price(TEST_SYMBOL)
    if price_data:
        current_price = int(price_data["current_price"])
        ok(f"현재가: {current_price:,}원")
        ok(f"등락률: {price_data['change_rate']:.2f}%")
    else:
        warn("현재가 조회 실패 (장 마감 또는 API 응답 없음)")
        current_price = 70_000  # 테스트용 fallback

    # ── 4. 주문가능금액 조회 ─────────────────────────────────────────────
    section("4. mock 주문가능금액 조회")
    try:
        buyable = mock_client.get_buyable_cash()
        ok(f"주문가능금액: {buyable:,.0f}원")
    except Exception as e:
        warn(f"주문가능금액 조회 실패: {e}")

    # ── 5. 주문 실행 (--execute 옵션일 때만) ─────────────────────────────
    if not execute:
        section("5. 주문 실행 (건너뜀)")
        warn("--execute 옵션이 없으므로 실제 주문은 실행하지 않습니다.")
        warn("실제 mock 주문을 실행하려면: python scripts/test_mock_order.py --execute")
        print()
        ok("연결 테스트 완료")
        return

    section("5. mock 매수 주문 실행")
    order_price = int(current_price * TEST_PRICE_MARGIN)
    print(f"  주문 정보: {TEST_SYMBOL} {TEST_QUANTITY}주 @ {order_price:,}원 (지정가)")
    print(f"  ※ 낮은 가격으로 주문하므로 미체결 가능성이 높습니다.")

    try:
        result = mock_client.buy(TEST_SYMBOL, TEST_QUANTITY, order_price, "limit")
        if result["success"]:
            ok(f"매수 주문 성공 — order_id={result.get('order_id', 'N/A')}")
            ok(f"메시지: {result.get('message', '')}")

            # ── 5-1. 즉시 취소 주문 (체결 전 정리) ─────────────────────
            # 실제 취소 API는 별도 TR이 필요하므로 여기서는 안내만 합니다.
            warn("미체결 주문은 KIS 모의투자 앱에서 직접 취소하세요.")
        else:
            fail(f"매수 주문 실패: {result.get('message', 'unknown')}")
    except Exception as e:
        fail(f"매수 주문 예외: {e}")

    print(f"\n{'='*55}")
    print("  테스트 완료")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KIS 모의투자 주문 테스트")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="실제로 mock 계좌에 소량 주문을 실행합니다.",
    )
    args = parser.parse_args()
    main(execute=args.execute)
