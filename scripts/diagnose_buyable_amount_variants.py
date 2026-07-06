"""
diagnose_buyable_amount_variants.py

종목별 매수가능금액 다양한 파라미터 조합 진단 스크립트.
실제 주문은 절대 발생하지 않습니다.

기능:
  - 종목 / 주문가격 / 주문구분 / CMA포함 여부를 조합해 inquire-psbl-order 응답 비교
  - 응답 raw JSON을 data/logs/buyable_amount_diagnosis_YYYYMMDD.json에 저장
  - EXPECTED_APP_ORDERABLE_CASH와 가장 가까운 API/필드를 출력

사용법:
    python scripts/diagnose_buyable_amount_variants.py
    EXPECTED_APP_ORDERABLE_CASH=24000000 python scripts/diagnose_buyable_amount_variants.py
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

EXPECTED_APP_ORDERABLE_CASH = float(os.getenv("EXPECTED_APP_ORDERABLE_CASH", "24000000"))

_DATE = datetime.now().strftime("%Y%m%d")
_TS   = datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_DIR = _root / "data" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_OUT_FILE = _LOG_DIR / f"buyable_amount_diagnosis_{_DATE}.json"


def _build_client():
    from app.trading.kis_client import create_kis_client
    client = create_kis_client("real")
    if client is None:
        raise RuntimeError(
            "KISClient 초기화 실패. KIS_REAL_APP_KEY / KIS_REAL_APP_SECRET / "
            "KIS_REAL_CANO(또는 KIS_ACCOUNT_NO) 환경변수를 확인하세요."
        )
    return client


# inquire-psbl-order output 필드 한글 설명
_FIELD_DESC = {
    "ord_psbl_cash":           "주문가능현금 (현금성, 인출가능 근사)",
    "nrcvb_buy_amt":           "재매수가능금액 (D+2 매도대금 포함 ← 앱 주문가능금액 후보)",
    "psbl_qty":                "주문가능수량",
    "max_buy_qty":             "최대매수가능수량",
    "cma_evlu_amt":            "CMA 평가금액",
    "cma_wdrw_psbl_amt":       "CMA 출금가능금액",
    "ruse_psbl_amt":           "재사용가능금액",
    "fund_rpbl_ruse_psbl_amt": "펀드상환가능재사용가능금액",
    "bfdy_buy_amt":            "전일매수금액",
    "thdt_buy_amt":            "당일매수금액",
    "psbl_qty_calc_unpr":      "주문가능수량계산단가",
    "stsl_psbl_qty":           "매도가능수량",
}


def _get_current_price(client, symbol: str) -> int:
    try:
        data = client.get_current_price(symbol)
        if data and data.get("current_price", 0) > 0:
            return int(data["current_price"])
    except Exception:
        pass
    return 0


def _extract_amounts(output: dict) -> dict[str, float]:
    amounts = {}
    for k, v in output.items():
        try:
            amounts[k] = float(str(v).replace(",", ""))
        except (ValueError, TypeError):
            pass
    return amounts


def _find_closest(amounts: dict[str, float], target: float) -> tuple[str, float, float]:
    best_k, best_v, best_diff = "", 0.0, float("inf")
    for k, v in amounts.items():
        d = abs(v - target)
        if d < best_diff:
            best_k, best_v, best_diff = k, v, d
    return best_k, best_v, best_diff


def main():
    print("=" * 70)
    print("종목별 매수가능금액 다양한 파라미터 조합 진단")
    print("※ 실제 주문은 절대 발생하지 않습니다.")
    print(f"※ 앱 주문가능금액 기준: {EXPECTED_APP_ORDERABLE_CASH:,.0f} 원")
    print("=" * 70)

    # 클라이언트 초기화
    try:
        client = _build_client()
        print(f"[OK] KISClient 초기화 완료 (mode=real)")
    except Exception as e:
        print(f"[FAIL] 클라이언트 초기화 실패: {e}")
        sys.exit(1)

    # 토큰 발급
    try:
        client.ensure_token()
        print("[OK] 토큰 발급 완료")
    except Exception as e:
        print(f"[FAIL] 토큰 발급 실패: {e}")
        sys.exit(1)

    # 현재가 조회
    prices = {}
    for sym, name in [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("035420", "NAVER")]:
        p = _get_current_price(client, sym)
        prices[sym] = p
        print(f"  현재가 {sym}({name}) = {p:,}원" if p else f"  현재가 {sym}({name}) = 조회 실패 (장외)")

    # 조합 매트릭스
    # (케이스ID, 종목, 주문가격, 주문구분, CMA포함, 설명)
    cases = []
    for sym, name in [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("035420", "NAVER")]:
        cur = prices.get(sym, 0)
        # A: 시장가
        cases.append((f"{sym}-A", sym, name, 0,   "01", "Y", "시장가/CMA포함"))
        cases.append((f"{sym}-B", sym, name, 0,   "01", "N", "시장가/CMA미포함"))
        # B: 지정가 현재가
        if cur > 0:
            cases.append((f"{sym}-C", sym, name, cur, "00", "Y", f"지정가({cur:,}원)/CMA포함"))
        # C: 지정가 0 (시장가 fallback)
        cases.append((f"{sym}-D", sym, name, 0,   "00", "Y", "지정가(0원)/CMA포함"))

    # 결과 수집
    all_results = []
    global_amounts: dict[str, float] = {}

    print(f"\n{'─' * 70}")
    print(f"  {'케이스':<12} {'종목':<8} {'설명':<28} {'ord_psbl_cash':>14} {'nrcvb_buy_amt':>14}")
    print(f"{'─' * 70}")

    for case_id, sym, name, price, ord_dvsn, cma_incl, desc in cases:
        try:
            raw = client.get_buyable_cash_raw(
                symbol=sym, price=price, ord_dvsn=ord_dvsn, cma_incl=cma_incl
            )
            output = raw.get("output", {})
            ord_psbl = float(output.get("ord_psbl_cash", 0) or 0)
            nrcvb    = float(output.get("nrcvb_buy_amt", 0) or 0)
            amts = _extract_amounts(output)
            global_amounts.update(amts)

            print(f"  {case_id:<12} {sym:<8} {desc:<28} {ord_psbl:>14,.0f} {nrcvb:>14,.0f}")

            all_results.append({
                "case_id": case_id,
                "symbol": sym,
                "name": name,
                "price": price,
                "ord_dvsn": ord_dvsn,
                "cma_incl": cma_incl,
                "desc": desc,
                "ord_psbl_cash": ord_psbl,
                "nrcvb_buy_amt": nrcvb,
                "rt_cd": raw.get("rt_cd", ""),
                "msg_cd": raw.get("msg_cd", ""),
                "msg1": raw.get("msg1", ""),
                "output": output,
            })
        except Exception as e:
            print(f"  {case_id:<12} {sym:<8} {desc:<28} [ERROR] {e}")
            all_results.append({"case_id": case_id, "error": str(e)})

    print(f"{'─' * 70}")

    # 최적 필드 탐색
    print(f"\n[분석] 앱 주문가능금액({EXPECTED_APP_ORDERABLE_CASH:,.0f}원)와 가장 가까운 필드:")
    k, v, diff = _find_closest(global_amounts, EXPECTED_APP_ORDERABLE_CASH)
    if k:
        pct = diff / EXPECTED_APP_ORDERABLE_CASH * 100.0 if EXPECTED_APP_ORDERABLE_CASH else 0
        desc = _FIELD_DESC.get(k, "")
        print(f"  field_name = {k}")
        print(f"  value      = {v:,.0f} 원")
        print(f"  diff       = {diff:,.0f} 원  ({pct:.1f}%)")
        print(f"  설명       = {desc or '(알 수 없음)'}")
        if pct < 5:
            print(f"  [OK] 5% 이내 일치 → 이 필드를 매수 기준으로 사용하세요!")
        else:
            print(f"  [WARN] 5% 이상 차이. 장 외 시간이거나 파라미터 조합을 바꿔보세요.")
    else:
        print("  [WARN] 비교 가능한 금액 필드를 찾지 못했습니다.")

    # 안전 검증: withdrawable 사용 금지
    bal = client.get_balance()
    withdrawable = bal.get("cash", 0.0)
    nrcvb_best = global_amounts.get("nrcvb_buy_amt", 0.0)
    print(f"\n[안전검증] withdrawable={withdrawable:,.0f} | nrcvb_buy_amt={nrcvb_best:,.0f}")
    if nrcvb_best > withdrawable:
        print(f"  [OK] nrcvb_buy_amt > withdrawable → 매수 기준으로 nrcvb_buy_amt 사용 올바름")
    elif nrcvb_best == 0:
        print(f"  [WARN] nrcvb_buy_amt=0 (장 외 시간). 장 중 재실행 필요.")
    else:
        print(f"  [INFO] nrcvb_buy_amt <= withdrawable. 현재 계좌 상태 확인 필요.")

    # 종목별 매수가능금액/수량 요약
    print(f"\n[종목별 매수가능금액 요약]")
    print(f"  {'종목':<10} {'현재가':>8} {'매수가능금액':>15} {'매수가능수량':>12}")
    for sym, name in [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("035420", "NAVER")]:
        buyable = client.get_buyable_cash(sym, 0)
        cur_price = prices.get(sym, 0)
        qty = int(buyable / cur_price) if cur_price > 0 else 0
        print(f"  {sym}({name[:4]:<4}) {cur_price:>8,} {buyable:>15,.0f} {qty:>12,}주")

    # raw 저장
    save_data = {
        "run_at": _TS,
        "expected_app_orderable": EXPECTED_APP_ORDERABLE_CASH,
        "current_prices": prices,
        "closest_field": {"name": k, "value": v, "diff": diff} if k else {},
        "cases": all_results,
    }
    try:
        with open(_OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n[OK] raw JSON 저장 완료: {_OUT_FILE}")
    except Exception as e:
        print(f"[WARN] 파일 저장 실패: {e}")

    print("\n[완료] 실제 주문은 실행되지 않았습니다.")


if __name__ == "__main__":
    main()
