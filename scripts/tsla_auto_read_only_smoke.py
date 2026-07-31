#!/usr/bin/env python
"""READ_ONLY smoke test: TSLA/TSLL/TSLZ 현재가 + TSLA 분봉을 실제 KIS 해외
API(confirmed: HHDFS00000300/HHDFS76950200)로 조회를 시도한다.

이 스크립트는 주문·잔고 함수를 전혀 호출하지 않는다(그 기능 자체가 이
저장소에서 KIS_OVERSEAS_API_CONFIRMATION_REQUIRED로 차단되어 있다 —
kis_overseas_adapter.py 참조). 이 환경에 KIS_REAL_APP_KEY/APP_SECRET 등이
설정되어 있지 않거나 이 실행 환경에 아웃바운드 네트워크 접근이 없으면
실패가 정상이며, 이는 "READ_ONLY 검증 완료"가 아니라 "미검증"으로 정직하게
보고해야 한다 — 실패를 성공으로 가장하지 않는다.

REAL 주문 호출: 0건 (이 스크립트는애초에 order_executor/broker_adapter를
import하지 않는다).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.tsla_auto import config
from app.trading.tsla_auto.kis_overseas_adapter import (
    fetch_overseas_current_price,
    fetch_overseas_minute_candles,
)


def _try_quote(symbol: str) -> dict:
    quote, error = fetch_overseas_current_price("real", symbol, exchange_code=config.EXCHANGE_CODE)
    return {
        "symbol": symbol, "ok": quote is not None, "price": quote.price if quote else None,
        "error": error,
    }


def _try_minute_candles(symbol: str) -> dict:
    df, diag = fetch_overseas_minute_candles("real", symbol, exchange_code=config.EXCHANGE_CODE, nrec=120)
    return {
        "symbol": symbol, "ok": not df.empty, "received_count": int(len(df)),
        "newest": df["datetime"].iloc[-1].isoformat() if not df.empty else None, "diag": diag,
    }


def main() -> int:
    print("=== tsla_auto_read_only_smoke (실제 KIS 해외 API 호출 시도 — 주문 함수는 import조차 하지 않음) ===")
    print(f"exchange_code={config.EXCHANGE_CODE!r} (docs §KIS 해외주식 API — TSLL/TSLZ가 실제로도 NAS인지는 미확인)")

    results = {"quotes": [], "minute_candles": []}
    any_ok = False
    for symbol in (config.SIGNAL_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL):
        r = _try_quote(symbol)
        results["quotes"].append(r)
        any_ok = any_ok or r["ok"]
        print(f"[현재가] {symbol}: ok={r['ok']} price={r['price']} error={r['error']}")

    r = _try_minute_candles(config.SIGNAL_SYMBOL)
    results["minute_candles"].append(r)
    any_ok = any_ok or r["ok"]
    print(f"[분봉] {config.SIGNAL_SYMBOL}: ok={r['ok']} received_count={r['received_count']} newest={r['newest']}")
    if r.get("diag", {}).get("error"):
        print(f"       error={r['diag']['error']}")

    print("\nREAL 주문 호출: 0건 (order_executor/broker_adapter 미import)")
    if not any_ok:
        print(
            "\n결과: 미검증 — 이 실행 환경에서 KIS 해외 API에 실제로 접근하지 못했다 "
            "(자격증명 미설정 또는 네트워크 접근 불가로 추정). "
            "이것을 'READ_ONLY 검증 완료'로 보고하지 않는다."
        )
        return 1
    print("\n결과: 위 성공한 항목에 한해 실제 KIS 해외 API 연결이 확인됨.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
