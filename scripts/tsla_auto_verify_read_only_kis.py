#!/usr/bin/env python
"""TSLA_AUTO KIS overseas READ_ONLY verification.

Calls quote, minute-candle, balance, and buyable-quantity read endpoints only.
It never imports order_executor or broker_adapter and never calls BUY/SELL,
amend, or cancel.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.tsla_auto import config
from app.trading.tsla_auto.kis_overseas_adapter import (
    fetch_overseas_balance,
    fetch_overseas_buyable_quantity,
    fetch_overseas_current_price,
    fetch_overseas_minute_candles,
)


def main() -> int:
    print("=== tsla_auto_verify_read_only_kis ===")
    any_failure = False
    quote_prices: dict[str, float] = {}

    for symbol in (config.SIGNAL_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL):
        exchange = config.QUOTE_EXCHANGE_BY_SYMBOL.get(symbol, config.EXCHANGE_CODE)
        if symbol == config.INVERSE_SYMBOL and not exchange:
            print(f"QUOTE {symbol}: ok=False error={config.TSLZ_EXCHANGE_UNRESOLVED}")
            any_failure = True
            continue
        quote, error = fetch_overseas_current_price("real", symbol, exchange_code=exchange)
        ok = quote is not None and error is None
        if ok:
            quote_prices[symbol] = quote.price
        any_failure = any_failure or not ok
        print(f"QUOTE {symbol}: ok={ok} exchange={exchange} price={quote.price if quote else None} error={error}")

    df, diag = fetch_overseas_minute_candles(
        "real", config.SIGNAL_SYMBOL, exchange_code=config.QUOTE_EXCHANGE_BY_SYMBOL[config.SIGNAL_SYMBOL], nrec=120,
    )
    ok_minutes = not df.empty
    any_failure = any_failure or not ok_minutes
    print(f"TSLA_1M: ok={ok_minutes} count={len(df)} error={diag.get('error')}")

    positions, cash, balance_error = fetch_overseas_balance(
        "real", exchange_code=config.ORDER_EXCHANGE_BY_SYMBOL.get(config.LONG_SYMBOL, "NASD"),
    )
    ok_balance = balance_error is None and cash is not None
    any_failure = any_failure or not ok_balance
    print(
        f"BALANCE: ok={ok_balance} positions={len(positions)} "
        f"usd_available={cash.available_amount if cash else None} error={balance_error}"
    )

    for symbol in (config.LONG_SYMBOL, config.INVERSE_SYMBOL):
        exchange = config.ORDER_EXCHANGE_BY_SYMBOL.get(symbol, "")
        if symbol == config.INVERSE_SYMBOL and not exchange:
            print(f"BUYABLE {symbol}: ok=False error={config.TSLZ_EXCHANGE_UNRESOLVED}")
            any_failure = True
            continue
        quote, error = fetch_overseas_buyable_quantity(
            "real", symbol, exchange_code=exchange, price=float(quote_prices.get(symbol) or 0.0),
        )
        ok = quote is not None and error is None
        any_failure = any_failure or not ok
        print(
            f"BUYABLE {symbol}: ok={ok} exchange={exchange} "
            f"qty={quote.available_qty if quote else None} usd={quote.available_usd if quote else None} error={error}"
        )

    print("ORDER_CALLS=0")
    return 1 if any_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
