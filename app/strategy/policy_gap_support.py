"""
policy_gap_support.py

C 유형(지수 약세·테마 강세장) 정책 — 기존 GAP Top15를 보조 전략으로 사용한다.
GAP Top15 단독 매수는 금지하며, 오늘 주도섹터(Top3/leader_sectors)와
겹치는 종목만 후보로 남긴다.

추가 제외:
  - 이미 +15% 이상 상승한 종목 (전일종가 대비)
  - 09:20 이후 시가 대비 크게 밀린 종목 (시가 유지 실패)
  - 투자경고/관리종목/스팩/우선주/ETF/리츠는 기존 StockFilter가 이미 제외
"""

from __future__ import annotations

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.strategy.policy_base import PolicyCandidate, default_exit_prices

POLICY_NAME = "policy_gap_support"
_ALREADY_RISEN_LIMIT_PCT = 15.0
_OPEN_HOLD_MIN_RATE = -1.5
_DEFAULT_MAX_CANDIDATES = 5


def _resolve_leader_sectors(market_ctx: dict) -> list:
    leader_sectors = market_ctx.get("leader_sectors")
    if leader_sectors:
        return list(leader_sectors)
    snapshot = market_ctx.get("snapshot", {}) or {}
    sector_rates = snapshot.get("domestic", {}).get("sector_change_rates", {}) or {}
    ranked = sorted(sector_rates.items(), key=lambda x: x[1], reverse=True)
    return [sec for sec, _ in ranked[:3]]


def _run_gap_pipeline(cfg=None):
    from app.data.data_collector import DataCollector
    from app.features.feature_builder import FeatureBuilder
    from app.ml.predict_model import ModelPredictor
    from app.strategy.candidate_generator import CandidateGenerator
    from app.strategy.top15_selector import Top15Selector

    collector = DataCollector(cfg)
    collected = collector.collect_gap_candidates()
    stocks = collected.get("candidates", [])
    if not stocks:
        return [], collected

    features = FeatureBuilder().build_features(stocks)
    predictions = ModelPredictor().predict(features)
    candidates = CandidateGenerator(cfg).generate(stocks, predictions=predictions)
    top15 = Top15Selector(cfg).select(candidates)
    return top15, collected


def generate_candidates(market_ctx: dict, cfg=None) -> tuple[list, dict]:
    market_ctx = market_ctx or {}
    exit_cfg = market_ctx.get("exit_cfg", {})
    policy_cfg = market_ctx.get("policy_gap_support_cfg", {})
    max_candidates = policy_cfg.get("max_candidates", _DEFAULT_MAX_CANDIDATES)

    top15, collected_meta = _run_gap_pipeline(cfg)
    diag = {
        "policy": POLICY_NAME,
        "gap15_source": collected_meta.get("source", "unknown"),
        "gap15_count": len(top15),
    }
    if not top15:
        diag["reason"] = "GAP Top15 후보 없음"
        return [], diag

    leader_sectors = _resolve_leader_sectors(market_ctx)
    diag["leader_sectors"] = leader_sectors
    if not leader_sectors:
        diag["reason"] = "주도섹터 정보 없음 — 교집합 불가"
        return [], diag

    try:
        from app.strategy.sector_mapper import get_sector
    except Exception:
        get_sector = None

    filtered = []
    excluded_count = 0
    for c in top15:
        sector = get_sector(c.symbol, c.name) if get_sector else ""
        if sector not in leader_sectors:
            excluded_count += 1
            continue
        total_change = (
            (c.current_price - c.previous_close) / c.previous_close * 100
            if c.previous_close else c.gap_rate + c.open_to_current_rate
        )
        if total_change >= _ALREADY_RISEN_LIMIT_PCT:
            excluded_count += 1
            continue
        if c.open_to_current_rate < _OPEN_HOLD_MIN_RATE:
            excluded_count += 1
            continue
        filtered.append((c, sector, total_change))

    diag["intersection_excluded"] = excluded_count
    diag["intersection_count"] = len(filtered)

    filtered.sort(key=lambda x: x[0].final_score, reverse=True)
    candidates: list[PolicyCandidate] = []
    for c, sector, total_change in filtered[:max_candidates]:
        stop_loss, tp1, tp2 = default_exit_prices(c.current_price, exit_cfg)
        candidates.append(
            PolicyCandidate(
                symbol=c.symbol,
                name=c.name,
                entry_price=c.current_price,
                stop_loss_price=stop_loss,
                take_profit1_price=tp1,
                take_profit2_price=tp2,
                reason=f"GAP+주도테마 교집합 ({sector}) | {c.selected_reason}",
                policy_name=POLICY_NAME,
                sector=sector,
                meta={"final_score": c.final_score, "total_change_pct": round(total_change, 2)},
            )
        )

    diag["candidates_selected"] = len(candidates)
    return candidates, diag
