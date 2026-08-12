"""MU_MACD — Micron(MU) day-session WebSocket MACD flag -> Hynix leverage/
inverse ETF trader. Completely separate from app.trading.macd2 and
app.trading.tsla_auto at the worker/state/ledger/cache/lock level (see
config.py for exact separate file paths). Reuses macd2's stateless, generic
building blocks (Direction, PositionSnapshot, order_executor, signal_engine's
pure MACD math) intentionally — those hold no mutable module-level state, so
importing them creates no data-sharing risk between the two systems.
"""
