from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import pandas as pd

from app.models import StockData, StockLabel
from app.logger import logger


DATA_LABELS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "labels")


class LabelBuilder:
    """Generates training labels from historical OHLCV data."""

    def build_labels_from_history(
        self, ohlcv_history: list[dict], entry_price: float
    ) -> StockLabel:
        """Given OHLCV history after entry, compute labels.

        ohlcv_history: list of dicts with keys 'high', 'low', 'symbol', 'date' (date from first row).
        entry_price: the price at which the stock was bought (open price of entry day).

        Returns a StockLabel with computed label fields.
        """
        if not ohlcv_history:
            return StockLabel(symbol="", date="")

        symbol = ohlcv_history[0].get("symbol", "")
        date = ohlcv_history[0].get("date", "")

        target_3pct = entry_price * 1.03
        target_5pct = entry_price * 1.05
        stop_threshold = entry_price * (1 - 0.015)

        label_profit_3pct = 0
        label_profit_5pct = 0
        label_no_stop = 1  # assume no stop until proven otherwise

        for row in ohlcv_history:
            high = float(row.get("high", 0))
            low = float(row.get("low", 0))

            if high >= target_3pct:
                label_profit_3pct = 1

            if high >= target_5pct:
                label_profit_5pct = 1

            if low < stop_threshold:
                label_no_stop = 0

        label_good_trade = (
            1 if (label_profit_3pct or label_profit_5pct) and label_no_stop else 0
        )

        return StockLabel(
            symbol=symbol,
            date=date,
            label_profit_3pct=label_profit_3pct,
            label_profit_5pct=label_profit_5pct,
            label_no_stop=label_no_stop,
            label_good_trade=label_good_trade,
        )

    def build_labels_from_current(
        self, stocks: list[StockData]
    ) -> list[StockLabel]:
        """Real-time stocks cannot be labelled (no future data).

        Always returns an empty list and logs a warning.
        """
        logger.info(
            "Labels require historical data. Use historical CSV for training."
        )
        return []

    def load_historical_labels(self, date_str: str) -> Optional[pd.DataFrame]:
        """Load labels CSV for a specific date.

        date_str: 'YYYYMMDD'
        Returns DataFrame or None if the file does not exist.
        """
        labels_dir = os.path.abspath(DATA_LABELS_DIR)
        file_path = os.path.join(labels_dir, f"{date_str}_labels.csv")

        if not os.path.exists(file_path):
            logger.info(f"No historical labels found for {date_str}: {file_path}")
            return None

        try:
            df = pd.read_csv(file_path)
            logger.info(f"Loaded {len(df)} labels from {file_path}")
            return df
        except Exception as e:
            logger.warning(f"Failed to load labels from {file_path}: {e}")
            return None

    def can_generate_labels(self) -> bool:
        """Real-time mode cannot generate labels; triggers rule_score fallback."""
        return False


def save_labels(labels: list[StockLabel], date_str: str = None) -> str:
    """Save a list of StockLabel objects to data/labels/YYYYMMDD_labels.csv.

    Returns the path of the saved file.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    labels_dir = os.path.abspath(DATA_LABELS_DIR)
    os.makedirs(labels_dir, exist_ok=True)

    file_path = os.path.join(labels_dir, f"{date_str}_labels.csv")

    rows = [
        {
            "symbol": lbl.symbol,
            "date": lbl.date,
            "label_profit_3pct": lbl.label_profit_3pct,
            "label_profit_5pct": lbl.label_profit_5pct,
            "label_no_stop": lbl.label_no_stop,
            "label_good_trade": lbl.label_good_trade,
        }
        for lbl in labels
    ]

    df = pd.DataFrame(rows)
    df.to_csv(file_path, index=False, encoding="utf-8-sig")
    logger.info(f"Saved {len(labels)} labels to {file_path}")
    return file_path
