from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.trading.hynix_pullback_entry import detect_pullback


def test_pullback_uses_recent_segment_high_not_old_intraday_high():
    now = datetime(2026, 7, 14, 10, 20)
    rows = []
    rows.append({
        "datetime": now - timedelta(minutes=40),
        "open": 100.0, "high": 120.0, "low": 99.0, "close": 118.0, "volume": 1000,
    })
    recent_prices = [101.0, 101.5, 102.0, 102.5, 103.0, 103.4, 103.0, 102.7, 102.8, 103.0]
    for i, price in enumerate(recent_prices):
        rows.append({
            "datetime": now - timedelta(minutes=len(recent_prices) - 1 - i),
            "open": price, "high": price + 0.2, "low": price - 0.3, "close": price, "volume": 1000 + i,
        })

    result = detect_pullback(pd.DataFrame(rows))

    assert result["recent_high"] < 104.0
    assert result["pullback_pct"] < 1.0
