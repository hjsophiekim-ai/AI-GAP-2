"""
diagnose_kis_order_precheck.py

실제 주문 없이 매수 사전 검증을 수행합니다.
실행: python scripts/diagnose_kis_order_precheck.py [mock|real]

출력 항목:
  - 모드 및 base_url 확인
  - 환경변수 존재 여부 (값 자체는 출력하지 않음)
  - ETF 필터 드라이런 (샘플 종목명으로 필터 동작 확인)
  - buy_plan 유효성 검증 시뮬레이션

실제 주문은 절대 실행하지 않습니다.
"""

import sys
import os
from pathlib import Path
from datetime import datetime

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _check_mode_url(mode: str) -> None:
    """모드별 base_url 확인."""
    from app.trading.kis_client import BASE_URL_MOCK, BASE_URL_REAL
    url = BASE_URL_MOCK if mode == "mock" else BASE_URL_REAL
    print(f"   mode={mode}")
    print(f"   base_url={url}")


def _check_env(mode: str) -> dict:
    """환경변수 존재 여부 (값은 출력하지 않음)."""
    if mode == "mock":
        keys = ["KIS_MOCK_APP_KEY", "KIS_MOCK_APP_SECRET", "KIS_MOCK_ACCOUNT_NO"]
    else:
        keys = ["KIS_REAL_APP_KEY", "KIS_REAL_APP_SECRET", "KIS_ACCOUNT_NO"]
    result = {}
    for k in keys:
        result[k] = bool(os.environ.get(k, ""))
    return result


def _etf_filter_dryrun() -> None:
    """ETF 필터 드라이런 — 실제 주문 없이 종목명 필터 동작 확인."""
    from app.trading.order_manager import _is_etf_like

    test_cases = [
        ("069500", "KODEX 200", True),
        ("448100", "WON 200", True),
        ("462870", "시프트업", False),
        ("307950", "현대오토에버", False),
        ("012320", "경동인베스트", False),
        ("010690", "화신", False),
        ("114800", "KODEX 인버스", True),
        ("122630", "KODEX 레버리지", True),
        ("195930", "TIGER 200 채권혼합", True),
        ("005930", "삼성전자", False),
        ("000660", "SK하이닉스", False),
        ("035420", "NAVER", False),
    ]

    print(f"   {'종목코드':<10} {'종목명':<25} {'예상제외':<8} {'실제결과':<8} {'일치'}")
    print(f"   {'-'*70}")
    all_pass = True
    for symbol, name, expected_excluded in test_cases:
        reason = _is_etf_like(symbol, name)
        actual_excluded = bool(reason)
        match = actual_excluded == expected_excluded
        if not match:
            all_pass = False
        mark = "[OK]" if match else "[NG]"
        exc_str = "제외" if actual_excluded else "통과"
        exp_str = "제외" if expected_excluded else "통과"
        print(f"   {symbol:<10} {name:<25} {exp_str:<8} {exc_str:<8} {mark}  {reason[:30] if reason else ''}")

    print()
    if all_pass:
        print("   ETF 필터 테스트: 전체 통과 [OK]")
    else:
        print("   ETF 필터 테스트: 일부 실패 [NG] (위 결과 확인)")


def _validate_buy_plan_sample(mode: str) -> None:
    """샘플 buy_plan 유효성 검증 시뮬레이션."""
    import types
    from app.trading.order_manager import _is_etf_like

    sample_plans = [
        types.SimpleNamespace(
            symbol="462870", name="시프트업",
            current_price=45000.0, allocated_quantity=2,
        ),
        types.SimpleNamespace(
            symbol="448100", name="WON 200",
            current_price=12000.0, allocated_quantity=3,
        ),
        types.SimpleNamespace(
            symbol="307950", name="현대오토에버",
            current_price=178000.0, allocated_quantity=1,
        ),
        types.SimpleNamespace(
            symbol="012320", name="경동인베스트",
            current_price=5000.0, allocated_quantity=0,  # qty=0 → validation error
        ),
    ]

    print(f"   {'종목코드':<10} {'종목명':<20} {'수량':<5} {'가격':<10} {'결과'}")
    print(f"   {'-'*65}")
    for p in sample_plans:
        etf_reason = _is_etf_like(p.symbol, p.name)
        if etf_reason:
            result = f"제외 ({etf_reason[:30]})"
        elif p.allocated_quantity < 1:
            result = "스킵 (수량 부족)"
        elif p.current_price <= 0:
            result = "스킵 (가격 오류)"
        else:
            result = f"주문 대상 ({p.allocated_quantity}주 @ {p.current_price:,.0f}원)"
        print(f"   {p.symbol:<10} {p.name:<20} {p.allocated_quantity:<5} {p.current_price:<10,.0f} {result}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "mock"
    if mode not in ("mock", "real"):
        print(f"사용법: python scripts/diagnose_kis_order_precheck.py [mock|real]")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"KIS 주문 사전 검증: mode={mode}")
    print(f"실행시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    print("▶ 모드 / URL 확인")
    _check_mode_url(mode)
    print()

    print("▶ 환경변수 확인")
    env_check = _check_env(mode)
    for k, v in env_check.items():
        print(f"   {k}: {'[OK] 존재' if v else '[NG] 없음'}")
    print()

    print("▶ ETF 필터 드라이런")
    _etf_filter_dryrun()
    print()

    print("▶ 샘플 buy_plan 유효성 검증")
    _validate_buy_plan_sample(mode)
    print()

    print("진단 완료. 실제 주문은 실행되지 않았습니다.")
    print("※ API 키/시크릿/계좌번호는 출력하지 않습니다.\n")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(_REPO_ROOT / ".env")
    except ImportError:
        pass
    main()
