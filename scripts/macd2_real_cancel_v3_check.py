"""실계좌 1회 검증 — 1주 저가 지정가 매수 → 주문번호 조회 → 취소 v3(TTTC0013U) → 주문번호 조회로 취소 확정.

  python scripts/macd2_real_cancel_v3_check.py --i-understand-this-places-a-REAL-order [--symbol 069500]
                                                [--below-pct 5] [--allow-token-issue]

· 실계좌(KIS_REAL_*)에 **진짜 주문 1주**를 낸다. 수량은 1로 고정(바꿀 수 없음).
· 기본 종목은 MACD2 가 거래하지 않는 069500(KODEX 200) — 운영 worker 의 포지션 대조와 섞이지 않게.
· 가격은 기준가(매수1호가, 없으면 현재가) 대비 --below-pct% 아래(틱 내림). 체결되지 않을 가격이다. 혹시 체결돼도 1주다.
· 허용 시간: 08:30~08:58(장전 동시호가 접수) 또는 09:00~15:15. 그 밖이면 주문하지 않는다.
· 기본은 캐시 토큰만 사용. 캐시가 없거나 만료면 중단한다(새 발급은 Render worker 토큰을 끊을 수 있다 —
  --allow-token-issue 는 사용자 승인 뒤에만).
· 결과는 data/live_validation/real_cancel_v3_check_YYYYMMDD_HHMMSS.json 에 남긴다.
판정: CANCEL_CONFIRMED(성공) / FILLED_BEFORE_CANCEL / CANCEL_NOT_CONFIRMED / STATUS_UNAVAILABLE / ORDER_FAILED
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
KST = timezone(timedelta(hours=9))


def _in_window(now: datetime) -> bool:
    hm = now.strftime("%H:%M")
    return ("08:30" <= hm <= "08:58") or ("09:00" <= hm <= "15:15")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--i-understand-this-places-a-REAL-order", dest="ok", action="store_true")
    ap.add_argument("--symbol", default="069500")
    ap.add_argument("--below-pct", type=float, default=5.0)
    ap.add_argument("--allow-token-issue", action="store_true")
    a = ap.parse_args()
    if not a.ok:
        print("실계좌에 1주 주문을 냅니다. --i-understand-this-places-a-REAL-order 를 주세요.")
        return 2
    if not (1.0 <= a.below_pct <= 20.0):
        print("--below-pct 는 1~20")
        return 2
    now = datetime.now(KST)
    if now.weekday() >= 5 or not _in_window(now):
        print(f"허용 시간이 아님({now:%a %H:%M}) — 08:30~08:58 또는 09:00~15:15 평일")
        return 2
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env", override=False)
    from app.trading.kis_client import create_kis_client
    from app.utils.stock_utils import get_tick_size

    c = create_kis_client("real")
    if c is None or getattr(c, "mode", "") != "real":
        print("real 클라이언트 생성 실패 — 중단")
        return 3
    if not a.allow_token_issue:
        if not c._load_token_cache():
            print("실계좌 캐시 토큰 없음/만료 — 새 발급은 Render 토큰을 끊을 수 있어 중단(승인 시 --allow-token-issue)")
            return 3

        def _no(*_a, **_k):
            raise RuntimeError("토큰 발급 차단(--allow-token-issue 없음)")
        c._invalidate_token = _no
        tok = c._token
        c.get_access_token = lambda: tok

    rec: dict = {"started_at": now.isoformat(), "symbol": a.symbol, "qty": 1, "steps": []}
    t = lambda: datetime.now(KST).strftime("%H:%M:%S.%f")[:-3]  # noqa: E731

    def step(name, **kw):
        rec["steps"].append({"at": t(), "step": name, **kw})
        print(f"[{t()}] {name}: {kw}")

    def finish(verdict: str, code: int) -> int:
        rec["verdict"] = verdict
        out = REPO / "data" / "live_validation" / f"real_cancel_v3_check_{now:%Y%m%d_%H%M%S}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rec, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"[{t()}] 판정: {verdict}  (기록: {out})")
        return code

    q = c.get_orderbook_quote(a.symbol)
    ref = float(q.get("bid1") or 0)
    if ref <= 0:
        ref = float((c.get_current_price(a.symbol) or {}).get("current_price") or 0)
    if ref <= 0:
        step("REF_PRICE_UNAVAILABLE", quote={k: q.get(k) for k in ("ok", "ask1", "bid1", "msg1")})
        return finish("ORDER_FAILED", 4)
    raw = ref * (1.0 - a.below_pct / 100.0)
    tick = float(get_tick_size(raw))
    price = int(math.floor(raw / tick) * tick)
    step("PRICE", ref=ref, price=price, below_pct=a.below_pct)

    r = c.buy(a.symbol, 1, price, order_type="limit")
    oid, org = r.get("order_id"), (r.get("raw") or {}).get("KRX_FWDG_ORD_ORGNO")
    step("BUY", success=r.get("success"), order_id=oid, org_no=org, msg_cd=r.get("msg_cd"), msg1=r.get("msg1"))
    if not r.get("success") or not oid or not org:
        return finish("ORDER_FAILED", 5)

    time.sleep(1.0)
    st = c.get_order_status(oid, symbol=a.symbol)
    step("STATUS_BEFORE_CANCEL", **{k: st.get(k) for k in ("ok", "found", "order_qty", "filled_qty", "remaining_qty",
                                                          "cancelled", "error")})
    status_ok_before = bool(st.get("ok") and st.get("found"))

    cr = c.cancel_order_v3(oid, org, a.symbol, allow_real=True)
    step("CANCEL_V3", **{k: cr.get(k) for k in ("success", "status", "tr_id", "rt_cd", "msg_cd", "msg1",
                                                 "cancel_order_id")})

    for i in range(10):
        time.sleep(1.0)
        st = c.get_order_status(oid, symbol=a.symbol)
        step(f"STATUS_{i + 1}", **{k: st.get(k) for k in ("ok", "found", "order_qty", "filled_qty", "remaining_qty",
                                                         "cancelled", "cancelled_qty", "error")})
        if st.get("ok") and st.get("found") and int(st.get("remaining_qty") or 0) == 0:
            if int(st.get("filled_qty") or 0) > 0:
                return finish("FILLED_BEFORE_CANCEL", 1)
            return finish("CANCEL_CONFIRMED" if (st.get("cancelled") or int(st.get("cancelled_qty") or 0) > 0)
                          else "CANCEL_NOT_CONFIRMED", 0 if st.get("cancelled") or st.get("cancelled_qty") else 1)
    return finish("CANCEL_NOT_CONFIRMED" if status_ok_before else "STATUS_UNAVAILABLE", 1)


if __name__ == "__main__":
    sys.exit(main())
