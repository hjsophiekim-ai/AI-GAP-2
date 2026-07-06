"""
feature_builder.py

Builds StockFeatures from StockData objects.
All scores are derived from intraday price/volume data only.
Historical (MA, volume ratio, etc.) features are left as None.
"""

import math
import os
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from app.logger import logger
from app.models import StockData, StockFeatures


class FeatureBuilder:
    """Converts a list of StockData into StockFeatures with rule-based scores."""

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def build_features(self, stocks: list[StockData]) -> list[StockFeatures]:
        """Build features for every stock; never raises – skips bad entries."""
        results: list[StockFeatures] = []
        for stock in stocks:
            try:
                results.append(self._build_single(stock))
            except Exception as exc:
                logger.warning(
                    f"[FeatureBuilder] _build_single 실패 | {stock.symbol} {stock.name}: {exc}"
                )
        return results

    def save_features(
        self, features: list[StockFeatures], date_str: str = None
    ) -> str:
        """
        Save features to data/features/YYYYMMDD_features.csv.
        Returns the absolute file path.
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        out_dir = os.path.join("data", "features")
        os.makedirs(out_dir, exist_ok=True)

        filepath = os.path.join(out_dir, f"{date_str}_features.csv")
        df = features_to_dataframe(features)
        df.to_csv(filepath, index=False, encoding="utf-8-sig")
        logger.info(f"[FeatureBuilder] features 저장: {filepath} ({len(df)}행)")
        return filepath

    # ------------------------------------------------------------------ #
    # Core builder                                                         #
    # ------------------------------------------------------------------ #

    def _build_single(self, stock: StockData) -> StockFeatures:
        """Compute all intraday features and rule-based scores for one stock."""

        # ---- Basic rate calculations ---------------------------------- #
        prev_close = stock.previous_close
        open_price = stock.open
        high = stock.high
        low = stock.low
        current = stock.current_price
        trade_value = stock.trade_value  # 거래대금 (원)
        volume = stock.volume

        # gap_rate: use supplied value if we cannot recompute
        if prev_close > 0 and open_price > 0:
            gap_rate = (open_price - prev_close) / prev_close * 100
        else:
            gap_rate = stock.gap_rate

        # open → current
        if open_price > 0:
            open_to_current_rate = (current - open_price) / open_price * 100
        else:
            open_to_current_rate = 0.0

        # high / low distance from open
        if open_price > 0:
            high_from_open_rate = (high - open_price) / open_price * 100
            low_from_open_rate = (low - open_price) / open_price * 100
        else:
            high_from_open_rate = 0.0
            low_from_open_rate = 0.0

        # current vs today's high
        if high > 0:
            current_from_high_rate = (current - high) / high * 100
        else:
            current_from_high_rate = 0.0

        # ---- Score calculations --------------------------------------- #

        # trade_value_score (0-25): log-scaled, max at 100B won (1e11)
        trade_value_score = self._trade_value_score(trade_value)

        # volume_score (0-10): relative volume; 1M shares → ~10
        volume_score = self._volume_score(volume)

        # gap_score (0-20): peaks at 5-8%, tapers off for very high gaps
        gap_score = self._gap_score(gap_rate)

        # price_strength_score (0-20): how strongly current > open
        price_strength_score = self._price_strength_score(open_to_current_rate)

        # high_break_score (0-15): current near today's high is best
        high_break_score = self._high_break_score(current_from_high_rate)

        # volatility_score (0-10): narrower (high-low)/open spread is better
        volatility_score = self._volatility_score(
            high, low, open_price
        )

        # liquidity_score (0-10): trade_value >= 3B won is ideal
        liquidity_score = self._liquidity_score(trade_value)

        # ---- Risk penalty -------------------------------------------- #
        risk_penalty = 0.0
        if gap_rate > 15.0:
            risk_penalty += 10.0
        if open_to_current_rate < -1.5:
            risk_penalty += 15.0
        if open_price > 0 and (high - low) / open_price * 100 > 5.0:
            risk_penalty += 5.0

        # ---- Total rule score ---------------------------------------- #
        raw_score = (
            trade_value_score
            + volume_score
            + gap_score
            + price_strength_score
            + high_break_score
            + volatility_score
            + liquidity_score
            - risk_penalty
        )
        total_rule_score = float(np.clip(raw_score, 0.0, 100.0))

        return StockFeatures(
            symbol=stock.symbol,
            name=stock.name,
            date=stock.date,
            gap_rate=round(gap_rate, 4),
            open_to_current_rate=round(open_to_current_rate, 4),
            high_from_open_rate=round(high_from_open_rate, 4),
            low_from_open_rate=round(low_from_open_rate, 4),
            current_from_high_rate=round(current_from_high_rate, 4),
            trade_value_score=round(trade_value_score, 4),
            volume_score=round(volume_score, 4),
            gap_score=round(gap_score, 4),
            price_strength_score=round(price_strength_score, 4),
            high_break_score=round(high_break_score, 4),
            volatility_score=round(volatility_score, 4),
            liquidity_score=round(liquidity_score, 4),
            risk_penalty=round(risk_penalty, 4),
            total_rule_score=round(total_rule_score, 4),
            # Historical features — not computed here
            ma5=None,
            ma20=None,
            ma60=None,
            ma120=None,
            close_above_ma20=None,
            close_above_ma60=None,
            ma20_slope=None,
            ma60_slope=None,
            volume_ratio_5d=None,
            volume_ratio_20d=None,
            trade_value_ratio_20d=None,
            day3_return=None,
            day5_return=None,
            day20_return=None,
            week52_high_ratio=None,
            recent_high_breakout=None,
            recent_volatility=None,
        )

    # ------------------------------------------------------------------ #
    # Score helpers                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _trade_value_score(trade_value: float) -> float:
        """
        Log-scaled 0-25 score.
        Reference: log10(1e11) = 11 → score = 25
        """
        if trade_value <= 0:
            return 0.0
        log_val = math.log10(trade_value)
        # log10(1e11) ≈ 11.0 → full score
        score = (log_val / 11.0) * 25.0
        return float(min(score, 25.0))

    @staticmethod
    def _volume_score(volume: int) -> float:
        """
        0-10 score.  1,000,000 shares → 10.
        Linear up to 1M, capped.
        """
        if volume <= 0:
            return 0.0
        score = (volume / 1_000_000) * 10.0
        return float(min(score, 10.0))

    @staticmethod
    def _gap_score(gap_rate: float) -> float:
        """
        0-20 score that peaks at 5-8% gap.
        Uses a triangular / trapezoidal shape:
          < 2%    → 0
          2-5%   → ramp up  0→20
          5-8%   → 20 (peak)
          8-15%  → ramp down 20→5
          > 15%  → 2 (very high gap, risky)
        """
        if gap_rate < 2.0:
            return 0.0
        elif gap_rate < 5.0:
            return (gap_rate - 2.0) / 3.0 * 20.0
        elif gap_rate <= 8.0:
            return 20.0
        elif gap_rate <= 15.0:
            # 20 → 5 over 7 pp
            return 20.0 - (gap_rate - 8.0) / 7.0 * 15.0
        else:
            return 2.0

    @staticmethod
    def _price_strength_score(open_to_current_rate: float) -> float:
        """
        0-20 score based on how much price has risen from open.
        Negative rate → 0.
        Caps at +5% for full score.
        """
        if open_to_current_rate <= 0:
            return 0.0
        score = (open_to_current_rate / 5.0) * 20.0
        return float(min(score, 20.0))

    @staticmethod
    def _high_break_score(current_from_high_rate: float) -> float:
        """
        0-15 score.
        current == high → 15 (best).
        Each 1% below high → -3 pts.
        Below -5% → 0.
        current_from_high_rate is always <= 0.
        """
        if current_from_high_rate >= 0:
            return 15.0
        # current_from_high_rate is negative (e.g. -2 means 2% below high)
        score = 15.0 + current_from_high_rate * 3.0  # subtract 3 per pct point
        return float(max(score, 0.0))

    @staticmethod
    def _volatility_score(high: float, low: float, open_price: float) -> float:
        """
        0-10 score.  Narrower candle body relative to open is better.
        spread = (high - low) / open * 100
        spread <= 1% → 10, spread >= 10% → 0, linear between.
        """
        if open_price <= 0 or high <= 0:
            return 0.0
        spread_pct = (high - low) / open_price * 100.0
        if spread_pct <= 1.0:
            return 10.0
        elif spread_pct >= 10.0:
            return 0.0
        else:
            return 10.0 - (spread_pct - 1.0) / 9.0 * 10.0

    @staticmethod
    def _liquidity_score(trade_value: float) -> float:
        """
        0-10 score based on trade_value >= 3B won.
        < 0.5B  → 0
        0.5-3B  → ramp 0→10
        >= 3B   → 10
        """
        threshold_low = 0.5e9   # 5억
        threshold_high = 3.0e9  # 30억
        if trade_value < threshold_low:
            return 0.0
        elif trade_value >= threshold_high:
            return 10.0
        else:
            return (trade_value - threshold_low) / (threshold_high - threshold_low) * 10.0


# --------------------------------------------------------------------------- #
# Module-level helper                                                           #
# --------------------------------------------------------------------------- #

def features_to_dataframe(features: list[StockFeatures]) -> pd.DataFrame:
    """Convert a list of StockFeatures dataclass instances to a pandas DataFrame."""
    if not features:
        return pd.DataFrame()
    rows = [f.__dict__ for f in features]
    return pd.DataFrame(rows)
