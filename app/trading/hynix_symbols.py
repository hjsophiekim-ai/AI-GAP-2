"""Canonical symbols for the Enhanced Hynix ETF switching strategy."""

from __future__ import annotations

from typing import Optional

SIGNAL_SYMBOL = "000660"
SIGNAL_NAME = "SK하이닉스"

LONG_SYMBOL = "0193T0"
LONG_NAME = "KODEX SK하이닉스단일종목레버리지"

SHORT_SYMBOL = "0197X0"
SHORT_NAME = "SOL SK하이닉스선물단일종목인버스2X"

TRADE_SYMBOLS = (LONG_SYMBOL, SHORT_SYMBOL)
TRADE_SYMBOL_NAME = {
    LONG_SYMBOL: LONG_NAME,
    SHORT_SYMBOL: SHORT_NAME,
}


def symbol_for_live_direction(direction: Optional[str]) -> Optional[str]:
    """Map underlying Live Direction to the traded ETF (single source of truth).

    UP → LONG_SYMBOL (0193T0), DOWN → SHORT_SYMBOL (0197X0).
    Applies equally to PULLBACK / REVERSAL / CONTINUATION entry paths.
    """
    text = str(direction or "").strip().upper()
    if text in ("UP", "HYNIX", "LONG", LONG_SYMBOL):
        return LONG_SYMBOL
    if text in ("DOWN", "INVERSE", "SHORT", SHORT_SYMBOL):
        return SHORT_SYMBOL
    return None


def action_for_live_direction(direction: Optional[str]) -> Optional[str]:
    """Map underlying Live Direction to the switch-engine final_action label."""
    symbol = symbol_for_live_direction(direction)
    if symbol == LONG_SYMBOL:
        return "HYNIX_BUY"
    if symbol == SHORT_SYMBOL:
        return "INVERSE_BUY"
    return None

