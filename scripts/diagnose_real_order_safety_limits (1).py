#!/usr/bin/env python
"""
diagnose_real_order_safety_limits.py
실계좌 주문 안전한도 진단 스크립트. 실제 주문은 하지 않습니다.
"""
import sys
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.chdir(_ROOT)

from app.config import get_config


def main():
    cfg = get_config()
    limits = cfg.get_real_order_limits()

    print("=" * 60)
    print("실계좌 주문 안전한도 진단")
    print("=" * 60)

    print("\n[환경변수 / 설정값]")
    print(f"  REAL_MAX_ORDER_AMOUNT               : {os.getenv('REAL_MAX_ORDER_AMOUNT', '(미설정)')}")
    print(f"  REAL_MAX_DAILY_ORDER_AMOUNT         : {os.getenv('REAL_MAX_DAILY_ORDER_AMOUNT', '(미설정)')}")
    print(f"  REAL_MAX_POSITION_AMOUNT_PER_SYMBOL : {os.getenv('REAL_MAX_POSITION_AMOUNT_PER_SYMBOL', '(미설정)')}")
    print(f"  AUTO_REDUCE_QUANTITY_ON_SAFETY_LIMIT: {os.getenv('AUTO_REDUCE_QUANTITY_ON_SAFETY_LIMIT', '(미설정)')}")

    print("\n[적용되는 한도값]")
    print(f"  1회 주문한도   : {limits['per_order']:>15,.0f} 원")
    print(f"  하루 주문한도  : {limits['daily']:>15,.0f} 원")
    print(f"  종목당 보유한도: {limits['per_symbol']:>15,.0f} 원")
    print(f"  수량 자동조정  : {'활성' if limits['auto_reduce'] else '비활성'}")

    print("\n[Top3 주문 예정금액 판정]")
    test_orders = [
        ("000660", "SK하이닉스",  1, 2_901_000),
        ("066570", "LG전자",     13,   228_500),
        ("064400", "LG씨엔에스", 21,    89_400),
    ]

    daily_sum = 0.0
    for symbol, name, qty, price in test_orders:
        order_amt = qty * price
        per_order_ok = order_amt <= limits["per_order"]
        per_symbol_ok = order_amt <= limits["per_symbol"]
        daily_sum += order_amt
        daily_ok = daily_sum <= limits["daily"]

        all_ok = per_order_ok and per_symbol_ok and daily_ok
        status = "[OK]" if all_ok else "[NG]"

        if not per_order_ok:
            detail = f"1회 주문한도 초과 ({order_amt:,.0f} > {limits['per_order']:,.0f})"
            if limits["auto_reduce"]:
                safe_qty = int(limits["per_order"] * 0.98 / price)
                detail += f" → 자동조정 시 {safe_qty}주" if safe_qty >= 1 else " → 자동조정 불가 (1주 미만)"
        elif not per_symbol_ok:
            detail = f"종목당 보유한도 초과 ({order_amt:,.0f} > {limits['per_symbol']:,.0f})"
        elif not daily_ok:
            detail = f"일일한도 초과 (누계 {daily_sum:,.0f} > {limits['daily']:,.0f})"
        else:
            detail = "모든 한도 내"

        print(f"  {status} {name}({symbol}): {qty}주×{price:,}원 = {order_amt:,.0f}원 | {detail}")

    print(f"\n  일별 주문 합계 : {daily_sum:>15,.0f} 원")
    daily_ok_all = daily_sum <= limits["daily"]
    print(f"  일별 한도 판정 : {'[OK]' if daily_ok_all else '[NG]'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
