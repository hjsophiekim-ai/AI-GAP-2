"""MOCK 전용 — 국내 정정취소 API(VTTC0803U) 실검증: 1주 지정가 매수(체결되지 않을 가격) → 취소 → 주문번호 상태로 확정.

  python scripts/macd2_mock_cancel_check.py --i-understand-this-places-a-mock-order [--symbol 0193T0] [--ticks-below 20]

· 장중(09:00~15:20)에만 의미가 있다. 모의계좌(KIS_MOCK_*)만 쓴다 — real 이면 즉시 종료.
· 기본은 캐시 토큰만 사용(새 발급 금지: 같은 appkey 의 다른 세션 토큰이 끊길 수 있다). --allow-token-issue 를 줘야 발급.
· 체결되지 않도록 매수1호가보다 --ticks-below 틱 아래 가격으로 1주. 혹시 체결돼도 1주다.
· 결과: 접수 주문번호/주문조직번호 → 취소 접수 → 잔량 0·취소수량 확인까지의 시각과 판정을 출력.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--i-understand-this-places-a-mock-order", dest="ok", action="store_true")
    ap.add_argument("--symbol", default="0193T0")
    ap.add_argument("--ticks-below", type=int, default=20)
    ap.add_argument("--allow-token-issue", action="store_true")
    a = ap.parse_args()
    if not a.ok:
        print("모의 주문을 실제로 냅니다. --i-understand-this-places-a-mock-order 를 주세요.")
        return 2
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env", override=False)
    from app.trading.kis_client import create_kis_client
    from app.utils.stock_utils import get_tick_size

    c = create_kis_client("mock")
    if getattr(c, "mode", "") != "mock":
        print("mode 가 mock 이 아니다 — 중단")
        return 3
    if not a.allow_token_issue:
        assert c._load_token_cache(), "모의 캐시 토큰 없음/만료 — --allow-token-issue 없이 발급하지 않음"

        def _no(*_a, **_k):
            raise RuntimeError("토큰 발급 차단(--allow-token-issue 없음)")
        c._invalidate_token = _no
        tok = c._token
        c.get_access_token = lambda: tok

    t = lambda: datetime.now().strftime("%H:%M:%S.%f")[:-3]  # noqa: E731
    q = c.get_orderbook_quote(a.symbol)
    bid1 = float(q.get("bid1") or 0)
    if not q.get("ok") or bid1 <= 0:
        print("호가 조회 실패:", {k: q.get(k) for k in ("ok", "ask1", "bid1", "msg1")})
        return 4
    price = int(bid1)
    for _ in range(a.ticks_below):
        price = int(price - get_tick_size(price))
    print(f"[{t()}] 호가 ask1={q.get('ask1')} bid1={bid1} → 1주 지정가 {price} (체결되지 않을 가격)")
    r = c.buy(a.symbol, 1, price, order_type="limit")
    oid, org = r.get("order_id"), (r.get("raw") or {}).get("KRX_FWDG_ORD_ORGNO")
    print(f"[{t()}] 매수 접수 success={r.get('success')} order_id={oid} org_no={org} msg={r.get('msg1')}")
    if not r.get("success") or not oid:
        return 6
    time.sleep(1.0)
    print(f"[{t()}] 상태:", c.get_order_status(oid, symbol=a.symbol))
    cr = c.cancel_order_rvsecncl(oid, org, a.symbol)
    print(f"[{t()}] 취소 접수 success={cr.get('success')} cancel_order_id={cr.get('order_id')} "
          f"rt_cd={cr.get('rt_cd')} msg_cd={cr.get('msg_cd')} msg1={cr.get('msg1')}")
    for i in range(10):
        time.sleep(1.0)
        st = c.get_order_status(oid, symbol=a.symbol)
        print(f"[{t()}] 상태#{i + 1}: filled={st.get('filled_qty')} remaining={st.get('remaining_qty')} "
              f"cancelled={st.get('cancelled')} cncl_qty={st.get('cancelled_qty')} found={st.get('found')} err={st.get('error')}")
        if st.get("ok") and st.get("found") and int(st.get("remaining_qty") or 0) == 0:
            verdict = "CANCEL_CONFIRMED" if int(st.get("filled_qty") or 0) == 0 else "FILLED_BEFORE_CANCEL"
            print(f"[{t()}] 판정: {verdict}")
            return 0
    print(f"[{t()}] 판정: CANCEL_NOT_CONFIRMED_WITHIN_10S")
    return 1


if __name__ == "__main__":
    sys.exit(main())
