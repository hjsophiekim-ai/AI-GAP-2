#!/usr/bin/env python
"""Submit a 1-share KIS MOCK overseas-stock validation order.

This script is intentionally validation-only:
- mode is always KIS MOCK
- quantity is fixed to 1
- it writes no TSLA_AUTO operational ledger/state
- it never calls REAL endpoints or REAL TR_IDs
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.tsla_auto import config, market_session
from app.trading.tsla_auto.kis_overseas_adapter import (
    TR_OVERSEAS_US_BUY_REAL,
    TR_OVERSEAS_US_CANCEL_REAL,
    TR_OVERSEAS_US_SELL_REAL,
    cancel_overseas_order,
    fetch_overseas_balance,
    fetch_overseas_buyable_quantity,
    fetch_overseas_current_price,
    fetch_overseas_fills,
    fetch_overseas_open_orders,
    masked_account,
    place_overseas_limit_order,
)


MODE = "mock"
VALIDATION_DIR = ROOT / "data" / "validation" / "tsla_auto"


def _print_json(label: str, payload: dict) -> None:
    print(f"{label}={json.dumps(payload, ensure_ascii=False, sort_keys=True)}")


def _raw_fields(raw: dict) -> dict:
    needles = ("rt", "msg", "ord", "psbl", "qty", "frcr", "usd", "amt", "odno", "ccld", "nccs")
    return {k: v for k, v in (raw or {}).items() if str(k).startswith("_") or any(n in str(k).lower() for n in needles)}


def _poll(symbol: str, exchange: str, order_id: str, *, seconds: int) -> tuple[int, int]:
    filled_qty = 0
    open_qty = 0
    deadline = time.time() + seconds
    while time.time() <= deadline:
        fills, fills_error, fills_raw = fetch_overseas_fills(MODE, symbol, exchange_code=exchange)
        open_orders, open_error, open_raw = fetch_overseas_open_orders(MODE, symbol, exchange_code=exchange)
        matched_fills = [row for row in fills if row.order_id in ("", order_id) or row.order_id == order_id]
        matched_open = [row for row in open_orders if row.order_id in ("", order_id) or row.order_id == order_id]
        filled_qty = max([row.executed_qty for row in matched_fills] or [filled_qty])
        open_qty = max([row.unfilled_qty for row in matched_open] or [0])
        _print_json("POLL", {
            "order_id": order_id,
            "filled_qty": filled_qty,
            "open_qty": open_qty,
            "fills_error": fills_error,
            "open_error": open_error,
            "fills_rt": _raw_fields(fills_raw),
            "open_rt": _raw_fields(open_raw),
        })
        if filled_qty >= 1 or open_qty >= 1:
            break
        time.sleep(3)
    return filled_qty, open_qty


def _validate_symbol(symbol: str, *, low_limit_ratio: float) -> int:
    quote_exchange = config.QUOTE_EXCHANGE_BY_SYMBOL[symbol]
    order_exchange = config.ORDER_EXCHANGE_BY_SYMBOL[symbol]
    print(f"--- {symbol} VALIDATION ---")
    print(f"QUOTE_EXCHANGE={quote_exchange} ORDER_EXCHANGE={order_exchange}")

    quote, quote_error = fetch_overseas_current_price(MODE, symbol, exchange_code=quote_exchange)
    print(f"QUOTE ok={quote is not None and quote_error is None} price={quote.price if quote else None} error={quote_error}")
    if quote is None:
        return 20

    buyable, buyable_error = fetch_overseas_buyable_quantity(MODE, symbol, exchange_code=order_exchange, price=quote.price)
    print(
        f"BUYABLE ok={buyable is not None and buyable_error is None} "
        f"qty={buyable.available_qty if buyable else None} usd={buyable.available_usd if buyable else None} "
        f"one_share_ok={bool(buyable and buyable.available_qty >= 1)} error={buyable_error}"
    )
    _print_json(f"BUYABLE_RAW_{symbol}", _raw_fields(buyable.raw if buyable else {}))
    if buyable is None or buyable.available_qty < 1:
        print("BLOCK=ORDERABLE_QTY_LT_1")
        return 21

    buy_price = round(max(quote.price * low_limit_ratio, 0.01), 2)
    buy = place_overseas_limit_order(MODE, symbol, "BUY", 1, buy_price, exchange_code=order_exchange)
    _print_json(f"BUY_RESPONSE_{symbol}", _raw_fields(buy.raw))
    print(f"BUY_ORDER_ID={buy.order_id} success={buy.success} rt_cd={buy.rt_cd} msg_cd={buy.msg_cd} msg1={buy.msg1}")
    if not buy.success or not buy.order_id:
        print("BLOCK=NO_KIS_MOCK_BUY_ORDER_ID")
        return 22

    filled_qty, open_qty = _poll(symbol, order_exchange, buy.order_id, seconds=15)
    if filled_qty <= 0:
        cancel = cancel_overseas_order(MODE, buy.order_id, symbol, exchange_code=order_exchange, qty=1)
        _print_json(f"CANCEL_RESPONSE_{symbol}", _raw_fields(cancel.raw))
        print(f"CANCEL_ORDER_ID={cancel.order_id} success={cancel.success} rt_cd={cancel.rt_cd} msg_cd={cancel.msg_cd} msg1={cancel.msg1}")
        _, open_after_cancel, _ = _poll(symbol, order_exchange, buy.order_id, seconds=6)
        print(f"FINAL_OPEN_QTY={open_after_cancel}")
        return 0 if cancel.success and open_after_cancel == 0 else 23

    sell_quote, sell_quote_error = fetch_overseas_current_price(MODE, symbol, exchange_code=quote_exchange)
    if sell_quote is None:
        print(f"BLOCK=SELL_QUOTE_FAILED error={sell_quote_error}")
        return 24
    sell_price = round(max(sell_quote.price * 0.98, 0.01), 2)
    sell = place_overseas_limit_order(MODE, symbol, "SELL", 1, sell_price, exchange_code=order_exchange)
    _print_json(f"SELL_RESPONSE_{symbol}", _raw_fields(sell.raw))
    print(f"SELL_ORDER_ID={sell.order_id} success={sell.success} rt_cd={sell.rt_cd} msg_cd={sell.msg_cd} msg1={sell.msg1}")
    if not sell.success or not sell.order_id:
        print("BLOCK=NO_KIS_MOCK_SELL_ORDER_ID")
        return 25
    _poll(symbol, order_exchange, sell.order_id, seconds=30)
    positions, _cash, balance_error = fetch_overseas_balance(MODE, exchange_code=order_exchange, currency="USD")
    qty_after = sum(int(getattr(p, "quantity", 0)) for p in positions if getattr(p, "symbol", "") == symbol)
    print(f"FINAL_STRATEGY_QTY_PROXY={qty_after} balance_error={balance_error}")
    return 0 if qty_after == 0 else 26


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", choices=("TSLL", "TSLZ", "both"), default="TSLL")
    parser.add_argument("--low-limit-ratio", type=float, default=0.80)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    print("=== tsla_auto_verify_kis_mock_order ===")
    print("ACCOUNT_MODE=MOCK")
    print(f"MASKED_ACCOUNT={masked_account(MODE)}")
    print(f"SESSION_STATUS={market_session.classify_session_status()}")
    print("REAL_ORDER_CALLS=0")
    print(f"REAL_TR_IDS_FORBIDDEN={[TR_OVERSEAS_US_BUY_REAL, TR_OVERSEAS_US_SELL_REAL, TR_OVERSEAS_US_CANCEL_REAL]}")
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    print(f"VALIDATION_DIR={VALIDATION_DIR}")

    if not args.execute:
        print("BLOCK=EXECUTE_FLAG_REQUIRED")
        return 2
    if market_session.classify_session_status() != "REGULAR":
        print("BLOCK=US_REGULAR_SESSION_REQUIRED")
        return 3

    symbols = [config.LONG_SYMBOL] if args.symbol == "TSLL" else [config.INVERSE_SYMBOL] if args.symbol == "TSLZ" else list(config.TRADE_SYMBOLS)
    status = 0
    for symbol in symbols:
        status = max(status, _validate_symbol(symbol, low_limit_ratio=args.low_limit_ratio))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
