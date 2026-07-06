from app.models import StockData
from app.config import get_config
from app.logger import logger
import math


class Scorer:
    def __init__(self, cfg=None):
        self.cfg = cfg or get_config()

    def score_stocks(
        self,
        stocks: list[StockData],
        disclosure_scores: dict[str, dict] | None = None,
    ) -> list[dict]:
        """
        Parameters
        ----------
        stocks : list[StockData]
        disclosure_scores : dict[symbol -> DisclosureFilter.score_result]
            DART 공시 점수. None이면 공시 점수 0으로 처리.
        """
        results = []
        disclosure_scores = disclosure_scores or {}
        for stock in stocks:
            try:
                score_dict = self._compute_score(stock)
                # DART 공시 점수 병합
                disc = disclosure_scores.get(stock.symbol, {})
                score_dict["disclosure_score"] = disc.get("disclosure_score", 0.0)
                score_dict["has_severe_risk"] = disc.get("has_severe_risk", False)
                score_dict["disclosure_summary"] = disc.get("summary", "")
                score_dict["total_score"] = max(0, min(100,
                    score_dict["total_score"] + score_dict["disclosure_score"]
                ))
                results.append(score_dict)
            except Exception as e:
                logger.info(f"scoring error for {stock.symbol}: {e}")
        return results

    def _compute_score(self, stock: StockData) -> dict:
        gap_rate = stock.gap_rate or 0.0
        trade_value = stock.trade_value or 0.0
        current_price = stock.current_price or 0.0
        open_price = stock.open or 0.0
        high = stock.high or 0.0
        low = stock.low or 0.0

        # open_to_current_rate
        if open_price > 0:
            open_to_current_rate = (current_price - open_price) / open_price * 100
        else:
            open_to_current_rate = 0.0

        # current_from_high_rate
        if high > 0:
            current_from_high_rate = (current_price - high) / high * 100
        else:
            current_from_high_rate = 0.0

        # candle_range
        if open_price > 0:
            candle_range = (high - low) / open_price * 100
        else:
            candle_range = 0.0

        # gap_score (0-20)
        if 3 <= gap_rate < 5:
            gap_score = 10
        elif 5 <= gap_rate < 8:
            gap_score = 20
        elif 8 <= gap_rate < 12:
            gap_score = 15
        elif 12 <= gap_rate < 15:
            gap_score = 10
        elif gap_rate >= 15:
            gap_score = 5
        else:
            gap_score = 0

        # trade_value_score (0-25)
        if trade_value >= 100_000_000_000:
            trade_value_score = 25
        elif trade_value >= 50_000_000_000:
            trade_value_score = 20
        elif trade_value >= 20_000_000_000:
            trade_value_score = 15
        elif trade_value >= 10_000_000_000:
            trade_value_score = 10
        elif trade_value >= 3_000_000_000:
            trade_value_score = 5
        else:
            trade_value_score = 0

        # price_strength_score (0-20)
        if open_to_current_rate >= 1.0:
            price_strength_score = 20
        elif open_to_current_rate >= 0.0:
            price_strength_score = 15
        elif open_to_current_rate >= -0.5:
            price_strength_score = 10
        elif open_to_current_rate >= -1.0:
            price_strength_score = 5
        else:
            price_strength_score = 0

        # high_break_score (0-15)
        if current_from_high_rate >= -0.5:
            high_break_score = 15
        elif current_from_high_rate >= -1.0:
            high_break_score = 10
        elif current_from_high_rate >= -2.0:
            high_break_score = 5
        else:
            high_break_score = 0

        # volume_intensity_score (0-10)
        if trade_value > 0:
            volume_intensity_score = min(10, math.log(trade_value / 1e9) * 2)
        else:
            volume_intensity_score = 0.0

        # volatility_stability_score (0-10)
        if candle_range < 2.0:
            volatility_stability_score = 10
        elif candle_range < 3.0:
            volatility_stability_score = 7
        elif candle_range < 5.0:
            volatility_stability_score = 4
        else:
            volatility_stability_score = 1

        # penalties
        penalties = 0
        if gap_rate > 15:
            penalties -= 10
        if open_to_current_rate < -1.5:
            penalties -= 15
        if candle_range > 5.0:
            penalties -= 5

        total_score = (
            gap_score
            + trade_value_score
            + price_strength_score
            + high_break_score
            + volume_intensity_score
            + volatility_stability_score
            + penalties
        )
        total_score = max(0, min(100, total_score))

        return {
            "symbol": stock.symbol,
            "name": stock.name,
            "gap_rate": gap_rate,
            "open_to_current_rate": open_to_current_rate,
            "trade_value": trade_value,
            "current_from_high_rate": current_from_high_rate,
            "candle_range": candle_range,
            "gap_score": gap_score,
            "trade_value_score": trade_value_score,
            "price_strength_score": price_strength_score,
            "high_break_score": high_break_score,
            "volume_intensity_score": volume_intensity_score,
            "volatility_stability_score": volatility_stability_score,
            "penalties": penalties,
            "total_score": total_score,
        }
