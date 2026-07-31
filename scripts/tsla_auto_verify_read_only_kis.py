#!/usr/bin/env python
"""TSLA_AUTO KIS MOCK/REAL overseas READ_ONLY verification.

This script calls quotes, minute candles, balance, buyable quantity, and best
bid/ask endpoints only. It never submits BUY, SELL, amend, or cancel.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.tsla_auto import config, market_session
from app.trading.tsla_auto.kis_overseas_adapter import (
    fetch_overseas_asking_price,
    fetch_overseas_balance,
    fetch_overseas_buyable_quantity,
    fetch_overseas_current_price,
    fetch_overseas_minute_candles,
    _credentials_account,
    masked_account,
)


def _subset(raw: dict, needles: tuple[str, ...]) -> dict:
    out = {}
    for key, value in (raw or {}).items():
        lower = str(key).lower()
        if key.startswith("_") or any(n in lower for n in needles):
            out[key] = value
    return out


def _print_json(label: str, data: dict) -> None:
    print(f"{label}={json.dumps(data, ensure_ascii=False, sort_keys=True)}")


def _safe_text(value: object, account_mask: str, full_account: str = "") -> str:
    text = str(value)
    if full_account:
        text = text.replace(full_account, account_mask.split("-")[0])
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("mock", "real"), default="mock")
    args = parser.parse_args()

    print("=== tsla_auto_verify_read_only_kis ===")
    print(f"ACCOUNT_MODE={args.mode.upper()}")
    account_mask = masked_account(args.mode)
    cano, _product = _credentials_account(args.mode)
    print(f"MASKED_ACCOUNT={account_mask}")
    print(f"SESSION_STATUS={market_session.classify_session_status()}")
    any_failure = False
    quote_prices: dict[str, float] = {}

    for symbol in (config.SIGNAL_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL):
        exchange = config.QUOTE_EXCHANGE_BY_SYMBOL.get(symbol, config.EXCHANGE_CODE)
        quote, error = fetch_overseas_current_price(args.mode, symbol, exchange_code=exchange)
        ok = quote is not None and error is None
        if ok:
            quote_prices[symbol] = quote.price
        any_failure = any_failure or not ok
        print(f"QUOTE {symbol}: ok={ok} exchange={exchange} price={quote.price if quote else None} error={_safe_text(error, account_mask, cano)}")

        ask, ask_error = fetch_overseas_asking_price(args.mode, symbol, exchange_code=exchange)
        print(
            f"ASKING {symbol}: ok={ask is not None and ask_error is None} "
            f"bid1={ask.get('bid1') if ask else None} ask1={ask.get('ask1') if ask else None} error={_safe_text(ask_error, account_mask, cano)}"
        )

    df, diag = fetch_overseas_minute_candles(
        args.mode,
        config.SIGNAL_SYMBOL,
        exchange_code=config.QUOTE_EXCHANGE_BY_SYMBOL[config.SIGNAL_SYMBOL],
        nrec=120,
    )
    print(f"TSLA_1M: ok={not df.empty} count={len(df)} error={_safe_text(diag.get('error'), account_mask, cano)}")

    positions, cash, balance_error = fetch_overseas_balance(
        args.mode,
        exchange_code=config.ORDER_EXCHANGE_BY_SYMBOL.get(config.LONG_SYMBOL, "NASD"),
        currency="USD",
    )
    ok_balance = balance_error is None and cash is not None
    any_failure = any_failure or not ok_balance
    print(
        f"BALANCE: ok={ok_balance} positions={len(positions)} "
        f"usd_available={cash.available_amount if cash else None} error={_safe_text(balance_error, account_mask, cano)}"
    )
    _print_json("BALANCE_RAW_KEY_FIELDS", _subset(cash.raw if cash else {}, ("frcr", "ord", "psbl", "usd", "crcy", "cash", "amt")))

    for symbol in (config.LONG_SYMBOL, config.INVERSE_SYMBOL):
        exchange = config.ORDER_EXCHANGE_BY_SYMBOL.get(symbol, "")
        quote, error = fetch_overseas_buyable_quantity(
            args.mode,
            symbol,
            exchange_code=exchange,
            price=float(quote_prices.get(symbol) or 0.0),
        )
        ok = quote is not None and error is None
        any_failure = any_failure or not ok
        one_share_ok = bool(quote and quote.available_qty >= 1)
        print(
            f"BUYABLE {symbol}: ok={ok} exchange={exchange} qty={quote.available_qty if quote else None} "
            f"usd={quote.available_usd if quote else None} one_share_ok={one_share_ok} error={_safe_text(error, account_mask, cano)}"
        )
        _print_json(
            f"BUYABLE_RAW_KEY_FIELDS_{symbol}",
            _subset(quote.raw if quote else {}, ("ord", "psbl", "qty", "frcr", "usd", "amt", "cash")),
        )

    print("ORDER_CALLS=0")
    return 1 if any_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
