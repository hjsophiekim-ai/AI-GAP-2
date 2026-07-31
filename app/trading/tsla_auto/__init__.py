"""TSLA_AUTO — independent module, isolated from MACD2/MACD v1/Enhanced.

Requirements source: docs/TSLA_AUTO_REQUIREMENTS.md, docs/TSLA_AUTO_LOGIC.md,
docs/TSLA_AUTO_COPY_MAP.md. Trades KIS overseas stocks (TSLL/TSLZ) driven by
a TSLA MACD signal. Never imports from app.trading.macd2.* (state, ledger,
Worker, Service, broker_adapter, order_executor) or from app.trading.kis_client
domestic order/balance functions — see tests/tsla_auto/test_isolation.py.
"""
from __future__ import annotations
