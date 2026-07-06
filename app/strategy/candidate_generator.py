"""
candidate_generator.py

Generates top 50 candidates from collected stocks by applying filters,
building features, scoring, and optionally merging ML predictions.
"""

import os
from datetime import datetime
from typing import Optional

import pandas as pd

from app.models import StockData, Candidate, StockFeatures
from app.strategy.filters import StockFilter
from app.strategy.scoring import Scorer
from app.features.feature_builder import FeatureBuilder
from app.strategy.candidate_quality_filter import CandidateQualityFilter
from app.config import get_config
from app.logger import logger

_TOP_N = 50


class CandidateGenerator:
    def __init__(self, cfg=None):
        self.cfg = cfg or get_config()
        self._filter = StockFilter(cfg=self.cfg)
        self._scorer = Scorer(cfg=self.cfg)
        self._feature_builder = FeatureBuilder()
        self._quality_filter = CandidateQualityFilter(cfg=self.cfg)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def generate(
        self,
        stocks: list[StockData],
        predictions: list[dict] = None,
        daily_prices_cache: dict[str, list[dict]] = None,
    ) -> list[Candidate]:
        """
        Main pipeline:
          1. Apply StockFilter
          2. Build StockFeatures for passed stocks
          3. Run Scorer
          4. Merge ML predictions if available
          5. Sort by final_score descending
          6. Apply CandidateQualityFilter (bonus/penalty + theme cap)
          7. Return top 50 as list[Candidate]
        """
        total_in = len(stocks)

        # Step 1: filter
        # StockFilter.filter_stocks returns (passed, excluded) where
        # excluded is a list[dict] with keys: symbol, name, reason
        passed, excluded = self._filter.filter_stocks(stocks)

        # Log each excluded stock at DEBUG level
        for item in excluded:
            if isinstance(item, dict):
                logger.debug(
                    f"[필터제외] {item.get('symbol', '')} {item.get('name', '')} - {item.get('reason', '')}"
                )
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                stock_obj, reason = item
                logger.debug(
                    f"[필터제외] {stock_obj.symbol} {stock_obj.name} - {reason}"
                )

        filtered_count = len(passed)

        # Step 2: build features
        features_list: list[StockFeatures] = self._feature_builder.build_features(passed)

        # Build lookup: symbol -> StockFeatures
        features_by_symbol: dict[str, StockFeatures] = {
            f.symbol: f for f in features_list
        }

        # Step 3: score stocks (+ optional DART disclosure scores)
        disclosure_scores = self._fetch_disclosure_scores(passed)
        score_dicts: list[dict] = self._scorer.score_stocks(
            passed, disclosure_scores=disclosure_scores
        )
        scores_by_symbol: dict[str, dict] = {
            d["symbol"]: d for d in score_dicts
        }

        # Step 4: merge ML predictions if provided
        preds_by_symbol: dict[str, dict] = {}
        use_ml = bool(predictions)
        if use_ml:
            for p in predictions:
                sym = p.get("symbol")
                if sym:
                    preds_by_symbol[sym] = p

        ml_weight = self.cfg.ml.get("ml_weight", 0.5)
        rule_weight = self.cfg.ml.get("rule_weight", 0.5)

        # Step 5: build scored list
        scored: list[tuple[float, StockData, dict, dict]] = []
        stock_by_symbol: dict[str, StockData] = {s.symbol: s for s in passed}

        for stock in passed:
            sym = stock.symbol
            score_dict = scores_by_symbol.get(sym, {})
            pred_dict = preds_by_symbol.get(sym, {})
            rule_score = float(score_dict.get("total_score", 0.0))

            if use_ml and sym in preds_by_symbol:
                ml_score = float(pred_dict.get("ml_score", rule_score))
                final_score = ml_weight * ml_score + rule_weight * rule_score
            else:
                ml_score = 0.0
                final_score = rule_score

            scored.append((final_score, ml_score, rule_score, stock, score_dict, pred_dict))

        # Sort by final_score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Step 6: pick top 50
        top = scored[:_TOP_N]
        candidates: list[Candidate] = []
        for rank, (final_score, ml_score, rule_score, stock, score_dict, pred_dict) in enumerate(top, start=1):
            merged_score_dict = dict(score_dict)
            merged_score_dict["ml_score"] = ml_score
            merged_score_dict["rule_score"] = rule_score
            merged_score_dict["final_score"] = final_score
            candidate = self._build_candidate(rank, stock, merged_score_dict, pred_dict)
            candidates.append(candidate)

        logger.info(
            f"총 {total_in}개 종목 수집 → 필터링 후 {filtered_count}개 → 후보 {len(candidates)}개 선정"
        )

        # Step 6: Quality filter (bonus/penalty + theme cap)
        # Build {symbol: StockData} for flag access in QualityFilter
        stock_data_by_symbol: dict[str, StockData] = {s.symbol: s for s in passed}
        try:
            candidates, excluded_q = self._quality_filter.filter_and_score(
                candidates,
                stock_data_by_symbol=stock_data_by_symbol,
                daily_prices_cache=daily_prices_cache or {},
            )
            if excluded_q:
                logger.info(f"[QFilter] 품질필터 추가 제외: {len(excluded_q)}개")
                for item in excluded_q:
                    logger.debug(f"  [QFilter제외] {item.get('code')} {item.get('name')} - {item.get('excluded_reason')}")
        except Exception as e:
            logger.warning(f"[QFilter] 품질필터 오류 (건너뜀): {e}")

        return candidates

    def _build_candidate(
        self,
        rank: int,
        stock: StockData,
        score_dict: dict,
        pred_dict: dict,
    ) -> Candidate:
        """Build a Candidate object from component data."""
        gap_rate = stock.gap_rate
        if gap_rate == 0.0 and stock.previous_close > 0 and stock.open > 0:
            gap_rate = (stock.open - stock.previous_close) / stock.previous_close * 100

        open_price = stock.open or 0.0
        current_price = stock.current_price or 0.0
        if open_price > 0:
            open_to_current_rate = (current_price - open_price) / open_price * 100
        else:
            open_to_current_rate = score_dict.get("open_to_current_rate", 0.0)

        trade_value = stock.trade_value or 0.0

        # selected_reason: brief human-readable summary
        tv_str = _format_trade_value(trade_value)
        strength_str = _strength_label(open_to_current_rate)
        selected_reason = f"갭상승 {gap_rate:.1f}%, 거래대금 {tv_str}, {strength_str}"

        # risk_comment: notable risks
        risk_parts = []
        if gap_rate > 12.0:
            risk_parts.append("갭 과다")
        if open_to_current_rate < -1.0:
            risk_parts.append("시가 하회")
        if trade_value < 3_000_000_000:
            risk_parts.append("거래대금 낮음")
        risk_comment = ", ".join(risk_parts)

        return Candidate(
            rank=rank,
            symbol=stock.symbol,
            name=stock.name,
            current_price=current_price,
            open=open_price,
            high=stock.high or 0.0,
            low=stock.low or 0.0,
            previous_close=stock.previous_close or 0.0,
            gap_rate=round(gap_rate, 4),
            open_to_current_rate=round(open_to_current_rate, 4),
            trade_value=trade_value,
            ml_score=round(score_dict.get("ml_score", 0.0), 4),
            rule_score=round(score_dict.get("rule_score", score_dict.get("total_score", 0.0)), 4),
            final_score=round(score_dict.get("final_score", 0.0), 4),
            selected_reason=selected_reason,
            risk_comment=risk_comment,
            exclude_reason="",
        )

    def _fetch_disclosure_scores(
        self, stocks: list[StockData]
    ) -> dict[str, dict]:
        """DART 공시 점수 조회. 설정 비활성화 또는 오류 시 빈 딕셔너리 반환."""
        dart_cfg = self.cfg.dart
        if not dart_cfg.get("enabled", True):
            return {}
        try:
            from app.data.dart_client import create_dart_client
            from app.data.disclosure_filter import DisclosureFilter
            client = create_dart_client()
            if not client.is_configured():
                return {}
            lookback = int(dart_cfg.get("lookback_days", 7))
            symbols_names = [(s.symbol, s.name) for s in stocks]
            raw = client.get_disclosures_for_symbols(symbols_names, lookback_days=lookback)
            disc_filter = DisclosureFilter(cfg=self.cfg)
            return disc_filter.score_all(raw)
        except Exception as e:
            logger.warning("[DART] 공시 점수 조회 실패 (무시): %s", e)
            return {}

    def save_candidates(
        self,
        candidates: list[Candidate],
        date_str: str = None,
    ) -> str:
        """
        Save candidates to data/candidates/YYYYMMDD_candidate50.csv.
        Returns the absolute filepath.
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        out_dir = os.path.join("data", "candidates")
        os.makedirs(out_dir, exist_ok=True)

        filepath = os.path.join(out_dir, f"{date_str}_candidate50.csv")

        rows = [c.__dict__ for c in candidates]
        df = pd.DataFrame(rows)
        df.to_csv(filepath, index=False, encoding="utf-8-sig")

        logger.info(f"[CandidateGenerator] 후보 저장: {filepath} ({len(df)}행)")
        return filepath


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #

def _format_trade_value(trade_value: float) -> str:
    """Format trade value as a human-readable string (e.g. '85B', '12억')."""
    if trade_value >= 1_000_000_000_000:
        return f"{trade_value / 1_000_000_000_000:.1f}조"
    elif trade_value >= 100_000_000_000:
        val = trade_value / 1_000_000_000
        return f"{val:.0f}B"
    elif trade_value >= 1_000_000_000:
        val = trade_value / 1_000_000_000
        return f"{val:.1f}B"
    elif trade_value >= 100_000_000:
        val = trade_value / 100_000_000
        return f"{val:.0f}억"
    else:
        val = trade_value / 100_000_000
        return f"{val:.1f}억"


def _strength_label(open_to_current_rate: float) -> str:
    """Return a brief Korean label for price strength relative to open."""
    if open_to_current_rate >= 2.0:
        return "강도매우강"
    elif open_to_current_rate >= 0.5:
        return "강도양호"
    elif open_to_current_rate >= -0.5:
        return "강도보통"
    else:
        return "강도약"
