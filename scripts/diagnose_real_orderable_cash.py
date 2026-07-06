"""
diagnose_real_orderable_cash.py

실계좌 주문가능금액 진단 스크립트 (확장판).
KIS 실계좌에서 예탁금총금액(인출가능금액)과 주문가능금액을 분리 조회하고,
앱 주문가능금액(EXPECTED_APP_ORDERABLE_CASH)과 가장 가까운 API 필드를 찾는다.
실제 주문은 전혀 발생하지 않는다.

사용법:
    python scripts/diagnose_real_orderable_cash.py
    EXPECTED_APP_ORDERABLE_CASH=24000000 python scripts/diagnose_real_orderable_cash.py
"""

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

# 프로젝트 루트를 PYTHONPATH에 추가
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

EXPECTED_APP_ORDERABLE_CASH = float(os.getenv("EXPECTED_APP_ORDERABLE_CASH", "24000000"))

_DIAG_DATE = datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_DIR = _root / "data" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_RAW_FILE = _LOG_DIR / f"orderable_cash_diag_{_DIAG_DATE}.json"


def _mask(s: str) -> str:
    """민감정보 마스킹 — 앞 4자리 + ..."""
    if not s:
        return "(없음)"
    return s[:4] + "..." if len(s) > 4 else "***"


def _check_env() -> bool:
    required = {
        "KIS_REAL_APP_KEY": os.getenv("KIS_REAL_APP_KEY", ""),
        "KIS_REAL_APP_SECRET": os.getenv("KIS_REAL_APP_SECRET", ""),
    }
    cano = (
        os.getenv("KIS_REAL_CANO", "")
        or os.getenv("KIS_ACCOUNT_NO", "")
    )
    missing = [k for k, v in required.items() if not v]
    if not cano:
        missing.append("KIS_REAL_CANO 또는 KIS_ACCOUNT_NO")
    if missing:
        print(f"[FAIL] 환경변수 미설정: {missing}")
        print("  → .env 파일에 KIS_REAL_APP_KEY / KIS_REAL_APP_SECRET / KIS_REAL_CANO 확인")
        return False
    print("[OK] 환경변수 확인 완료")
    print(f"       KIS_REAL_APP_KEY  = {_mask(required['KIS_REAL_APP_KEY'])}")
    print(f"       KIS_REAL_APP_SECRET = {_mask(required['KIS_REAL_APP_SECRET'])}")
    print(f"       CANO (계좌번호 8자리) = {_mask(cano)}")
    return True


def _build_client():
    """
    create_kis_client('real')을 통해 클라이언트 생성.
    KIS_REAL_CANO → KIS_ACCOUNT_NO 우선순위로 계좌번호를 읽음.
    모든 diagnose 스크립트가 동일 경로를 사용해야 토큰 캐시가 공유된다.
    """
    from app.trading.kis_client import create_kis_client
    client = create_kis_client("real")
    if client is None:
        raise RuntimeError(
            "KISClient 초기화 실패. .env의 KIS_REAL_APP_KEY / KIS_REAL_APP_SECRET / "
            "KIS_REAL_CANO(또는 KIS_ACCOUNT_NO) 확인."
        )
    return client


def _diag_token(client) -> bool:
    print("\n[1] 토큰 발급 테스트 (ensure_token = get_access_token alias)")
    try:
        token = client.ensure_token()
        masked = token[:8] + "..." if len(token) > 8 else "OK"
        expires_str = client._token_expires_at.strftime("%H:%M:%S") if client._token_expires_at else "?"
        print(f"  [OK] 토큰 발급 성공 ({masked})  만료: {expires_str}")
        return True
    except Exception as e:
        esc = str(e)
        # 민감정보 제거 후 출력
        print(f"  [FAIL] 토큰 발급 실패")
        if hasattr(e, "http_status"):
            print(f"         HTTP {e.http_status} | rt_cd={getattr(e,'rt_cd','')} | "
                  f"msg_cd={getattr(e,'msg_cd','')} | msg1={getattr(e,'msg1','')}")
        else:
            # key/secret 노출 방지
            safe_msg = esc[:200]
            print(f"         {safe_msg}")
        return False


def _diag_balance(client) -> tuple[bool, dict]:
    print("\n[2] 계좌 잔고 조회 (inquire-balance)")
    try:
        result = client.get_balance()
        if "error" in result:
            print(f"  [FAIL] 잔고 조회 오류: {result['error']}")
            return False, {}
        withdrawable = result.get("cash", 0)
        orderable_from_bal = result.get("orderable_cash", 0)
        pos_cnt = len(result.get("positions", []))
        print(f"  인출가능금액 (dnca_tot_amt)      = {withdrawable:>15,.0f} 원")
        print(f"  주문가능현금 (ord_psbl_cash bal) = {orderable_from_bal:>15,.0f} 원  ← balance output2 기준")
        print(f"  보유종목 수                      = {pos_cnt}개")
        if withdrawable == orderable_from_bal == 0:
            print("  [WARN] 두 값이 모두 0입니다. 장 외 시간이거나 계좌 설정을 확인하세요.")
        return True, result
    except Exception as e:
        print(f"  [FAIL] 잔고 조회 예외: {e}")
        return False, {}


def _diag_psbl_order_raw(client, symbol: str = "005930", price: int = 0, label: str = "") -> dict:
    """inquire-psbl-order raw output 전체 조회."""
    raw = client.get_buyable_cash_raw(symbol=symbol, price=price)
    output = raw.get("output", {})
    return {"symbol": symbol, "price": price, "label": label, "raw": raw, "output": output}


def _print_amount_fields(output: dict, title: str = "") -> dict:
    """output dict에서 금액으로 보이는 필드를 한글 설명과 함께 출력."""
    if title:
        print(f"\n  [{title}]")
    known = {
        "ord_psbl_cash":      "주문가능현금 (현금성, 인출가능 근사)",
        "nrcvb_buy_amt":      "재매수가능금액 (D+2 매도대금 포함 ← 앱 주문가능금액 후보)",
        "psbl_qty":           "주문가능수량",
        "max_buy_qty":        "최대매수가능수량",
        "cma_evlu_amt":       "CMA 평가금액",
        "cma_wdrw_psbl_amt":  "CMA 출금가능금액",
        "ruse_psbl_amt":      "재사용가능금액",
        "fund_rpbl_ruse_psbl_amt": "펀드상환가능재사용가능금액",
        "bfdy_buy_amt":       "전일매수금액",
        "thdt_buy_amt":       "당일매수금액",
        "psbl_qty_calc_unpr": "주문가능수량계산단가",
        "stsl_psbl_qty":      "매도가능수량",
    }
    amounts = {}
    for k, v in output.items():
        try:
            val = float(str(v).replace(",", ""))
        except (ValueError, TypeError):
            continue
        desc = known.get(k, "")
        flag = "★" if desc else " "
        amounts[k] = val
        if val != 0 or desc:
            print(f"  {flag} {k:<40} = {val:>15,.0f}  {desc}")
    return amounts


def _find_closest(amounts: dict, target: float) -> tuple[str, float, float]:
    """target과 가장 가까운 (field_name, value, diff) 반환."""
    best_k, best_v, best_diff = "", 0.0, float("inf")
    for k, v in amounts.items():
        d = abs(v - target)
        if d < best_diff:
            best_k, best_v, best_diff = k, v, d
    return best_k, best_v, best_diff


def _diag_orderable_full(client) -> bool:
    print("\n[3] 주문가능금액 전체 raw 조회 (inquire-psbl-order)")

    cases = [
        ("A", "005930", 0,      "시장가"),
        ("B", "005930", 70000,  "지정가(7만원)"),
        ("C", "000660", 0,      "SK하이닉스 시장가"),
        ("D", "035420", 0,      "NAVER 시장가"),
    ]

    all_raw = {}
    all_amounts: dict[str, float] = {}

    for case_id, symbol, price, label in cases:
        print(f"\n  케이스 {case_id}: {symbol} {label}")
        r = _diag_psbl_order_raw(client, symbol, price, label)
        if "error" in r.get("raw", {}):
            print(f"    [FAIL] {r['raw']['error']}")
        else:
            amts = _print_amount_fields(r["output"], f"{symbol} {label}")
            all_amounts.update(amts)
        all_raw[f"case_{case_id}_{symbol}"] = r.get("raw", {})

    return all_raw, all_amounts


def _diag_breakdown(client) -> bool:
    print("\n[4] 현금 종합 분석 (get_account_cash_breakdown)")
    try:
        bd = client.get_account_cash_breakdown()
        print(f"  withdrawable_amount (인출가능)  = {bd['withdrawable_amount']:>15,.0f} 원")
        print(f"  ord_psbl_cash (현금성주문가능)  = {bd.get('ord_psbl_cash', 0):>15,.0f} 원")
        print(f"  nrcvb_buy_amt (재매수가능)      = {bd.get('nrcvb_buy_amt', 0):>15,.0f} 원  ← 앱 주문가능금액 후보")
        print(f"  orderable_cash (실제매수한도)   = {bd['orderable_cash']:>15,.0f} 원")
        print(f"  settlement_pending_cash         = {bd['settlement_pending_cash']:>15,.0f} 원  (D+2 미결제 추정)")
        if bd["settlement_pending_cash"] > 0:
            print("  → 매도 후 D+2 결제 전 금액이 있습니다. 매수는 가능하나 인출은 불가합니다.")
        return True, bd
    except Exception as e:
        print(f"  [FAIL] 현금 분석 예외: {e}")
        return False, {}


def _diag_stock_buyable(client) -> bool:
    print("\n[5] 종목별 매수가능금액 조회 (get_buyable_cash / get_stock_buyable_amount)")
    symbols = [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("035420", "NAVER")]
    ok = True
    for symbol, name in symbols:
        try:
            buyable = client.get_stock_buyable_amount(symbol=symbol, price=0)
            print(f"  {symbol} ({name:<12}) 매수가능금액 = {buyable:>15,.0f} 원")
        except Exception as e:
            print(f"  [FAIL] {symbol} 매수가능금액 조회 예외: {e}")
            ok = False
    return ok


def _compare_with_app(all_amounts: dict, withdrawable: float, nrcvb: float, orderable: float) -> None:
    print(f"\n[6] 앱 주문가능금액과 비교")
    print(f"  EXPECTED_APP_ORDERABLE_CASH = {EXPECTED_APP_ORDERABLE_CASH:,.0f} 원")
    print(f"  인출가능금액 (withdrawable)   = {withdrawable:,.0f} 원")
    print(f"  재매수가능금액 (nrcvb_buy)    = {nrcvb:,.0f} 원")
    print(f"  orderable_cash (최종 선택)    = {orderable:,.0f} 원")

    if all_amounts:
        k, v, d = _find_closest(all_amounts, EXPECTED_APP_ORDERABLE_CASH)
        pct = d / EXPECTED_APP_ORDERABLE_CASH * 100.0 if EXPECTED_APP_ORDERABLE_CASH else 0
        print(f"\n  closest_to_app_orderable:")
        print(f"    field_name = {k}")
        print(f"    value      = {v:,.0f} 원")
        print(f"    diff       = {d:,.0f} 원  ({pct:.1f}%)")
        if d < EXPECTED_APP_ORDERABLE_CASH * 0.05:
            print(f"    [OK] 5% 이내 일치 → 이 필드가 앱 주문가능금액입니다!")
        else:
            print(f"    [WARN] 5% 이상 차이 → 장 외 시간이거나 파라미터 조합을 바꿔보세요.")

    # withdrawable 사용 금지 확인
    w_diff = abs(withdrawable - EXPECTED_APP_ORDERABLE_CASH)
    o_diff = abs(orderable - EXPECTED_APP_ORDERABLE_CASH)
    if o_diff < w_diff:
        print(f"\n  [OK] orderable_cash({orderable:,.0f})가 withdrawable({withdrawable:,.0f})보다")
        print(f"       앱 주문가능금액({EXPECTED_APP_ORDERABLE_CASH:,.0f})에 더 가깝습니다.")
        print(f"       → 매수 기준: orderable_cash 사용 (withdrawable 미사용)")
    else:
        print(f"\n  [WARN] 두 값 모두 앱 주문가능금액과 차이가 있습니다.")
        print(f"         장 외 시간에 실행했거나 다른 필드일 수 있습니다.")


def main():
    print("=" * 66)
    print("KIS 실계좌 주문가능금액 진단 스크립트 (확장판)")
    print("※ 실제 주문은 발생하지 않습니다.")
    print(f"※ 앱 주문가능금액 기준: {EXPECTED_APP_ORDERABLE_CASH:,.0f} 원")
    print(f"   (변경: env EXPECTED_APP_ORDERABLE_CASH=금액)")
    print("=" * 66)

    if not _check_env():
        sys.exit(1)

    try:
        client = _build_client()
        print(f"[OK] KISClient 초기화 완료 (mode=real, base_url={client.base_url})")
    except Exception as e:
        print(f"[FAIL] KISClient 초기화 실패: {e}")
        sys.exit(1)

    raw_log = {"run_at": _DIAG_DATE, "expected_app_orderable": EXPECTED_APP_ORDERABLE_CASH}

    # ── 단계별 진단 ──────────────────────────────────────────────────────
    ok_token = _diag_token(client)
    ok_bal, bal_result = _diag_balance(client)
    all_raw, all_amounts = _diag_orderable_full(client)
    ok_bd, bd = _diag_breakdown(client)
    ok_stock = _diag_stock_buyable(client)

    # ── 앱과 비교 ─────────────────────────────────────────────────────────
    withdrawable = bd.get("withdrawable_amount", bal_result.get("cash", 0.0)) if isinstance(bd, dict) else 0.0
    nrcvb = bd.get("nrcvb_buy_amt", 0.0) if isinstance(bd, dict) else 0.0
    orderable = bd.get("orderable_cash", 0.0) if isinstance(bd, dict) else 0.0
    _compare_with_app(all_amounts, withdrawable, nrcvb, orderable)

    # ── raw 저장 ──────────────────────────────────────────────────────────
    raw_log.update({
        "balance": {
            "withdrawable": withdrawable,
            "orderable_from_balance": bal_result.get("orderable_cash", 0),
        },
        "breakdown": {k: v for k, v in (bd.items() if isinstance(bd, dict) else [])} ,
        "psbl_order_cases": all_raw,
    })
    try:
        with open(_RAW_FILE, "w", encoding="utf-8") as f:
            json.dump(raw_log, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n[OK] raw 진단 결과 저장: {_RAW_FILE}")
    except Exception as e:
        print(f"[WARN] raw 저장 실패: {e}")

    # ── 최종 요약 ─────────────────────────────────────────────────────────
    steps = [ok_token, ok_bal, True, ok_bd, ok_stock]  # all_raw는 항상 True
    passed = sum(1 for r in steps if r)
    total = len(steps)
    print(f"\n{'=' * 66}")
    print(f"진단 결과: {passed}/{total} 단계 통과")
    print(f"인출가능금액   = {withdrawable:,.0f} 원  (출금 가능, 매수 기준 아님)")
    print(f"재매수가능금액 = {nrcvb:,.0f} 원  (앱 주문가능금액 후보)")
    print(f"매수 한도      = {orderable:,.0f} 원  (프로그램이 사용하는 값)")
    if passed == total:
        print("[SUCCESS] 실계좌 주문가능금액 조회 정상 작동")
    else:
        print("[WARNING] 일부 단계 실패. 위 메시지를 확인하세요.")
    print("=" * 66)


if __name__ == "__main__":
    main()
