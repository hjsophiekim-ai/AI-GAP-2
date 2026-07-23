"""MACD pipeline package — 4 roles + single Worker loop (no queues/consumers)."""
from app.trading.macd_pipeline import market_data as market_data
from app.trading.macd_pipeline import order_executor as order_executor
from app.trading.macd_pipeline import runtime_store as runtime_store
from app.trading.macd_pipeline import signal_engine as signal_engine

__all__ = ["market_data", "signal_engine", "order_executor", "runtime_store"]
