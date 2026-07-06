"""
policy_leader_top3.py

A 유형(강세 주도장) 메인 정책 — 기존 "주도섹터 Top3" 모듈
(SectorLeaderTop3Selector)을 그대로 재사용한다. 정확도가 검증된 기존
로직을 유지하고, 결과를 PolicyCandidate로 변환만 한다.
"""

from __future__ import annotations

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.strategy.policy_base import PolicyCandidate, default_exit_prices

POLICY_NAME = "policy_leader_top3"


def _collect_inputs(market_ctx: dict, cfg=None):
    """가능하면 market_ctx의 스냅샷을 재사용하고, 부족하면 직접 수집한다."""
    snapshot = (market_ctx or {}).get("snapshot", {}) or {}
    domestic = snapshot.get("domestic", {})
    nxt_stocks = domestic.get("trading_value_top50") or []
    vs_stocks = domestic.get("change_rate_top50") or []

    if len(nxt_stocks) < 20:
        try:
            from app.data.naver_nxt_turnover_collector import collect_nxt_turnover_stocks
            nxt_stocks = collect_nxt_turnover_stocks(max_pages=5, max_stocks=100)
        except Exception as exc:
            logger.warning("[PolicyLeaderTop3] NXT 재수집 실패: %s", exc)

    if not vs_stocks:
        try:
            from app.data.naver_volume_spike_collector import collect_volume_spike_stocks
            vs_stocks = collect_volume_spike_stocks(max_pages=3, max_stocks=80)
        except Exception as exc:
            logger.warning("[PolicyLeaderTop3] 거래량급증 재수집 실패: %s", exc)

    try:
        from app.services.us_sector_strength_service import USSectorStrengthService
        us_result = USSectorStrengthService(cfg).get_us_sector_strength()
    except Exception as exc:
        logger.warning("[PolicyLeaderTop3] 미국 섹터 강도 조회 실패: %s", exc)
        us_result = {}

    try:
        from app.strategy.sector_mapper import SectorMapper
        classified = SectorMapper().classify_stocks(nxt_stocks)
    except Exception as exc:
        logger.warning("[PolicyLeaderTop3] 섹터 분류 실패: %s", exc)
        classified = nxt_stocks

    return classified, vs_stocks, us_result


def generate_candidates(market_ctx: dict, cfg=None) -> tuple[list, dict]:
    from app.strategy.sector_leader_top3_selector import SectorLeaderTop3Selector

    exit_cfg = (market_ctx or {}).get("exit_cfg", {})
    classified, vs_stocks, us_result = _collect_inputs(market_ctx, cfg)

    if not classified:
        diag = {"policy": POLICY_NAME, "reason": "NXT 거래대금 데이터 없음", "candidates_evaluated": 0}
        return [], diag

    selector = SectorLeaderTop3Selector(cfg)
    top3, diag, excluded = selector.select(classified, vs_stocks, us_result)
    diag["policy"] = POLICY_NAME
    diag["excluded_count"] = len(excluded)

    candidates: list[PolicyCandidate] = []
    for s in top3:
        entry_price = float(s.get("current_price", 0))
        if entry_price <= 0:
            continue
        stop_loss, tp1, tp2 = default_exit_prices(entry_price, exit_cfg)
        candidates.append(
            PolicyCandidate(
                symbol=s.get("symbol", ""),
                name=s.get("name", ""),
                entry_price=entry_price,
                stop_loss_price=stop_loss,
                take_profit1_price=tp1,
                take_profit2_price=tp2,
                reason=s.get("selected_reason", "주도섹터 Top3"),
                policy_name=POLICY_NAME,
                sector=s.get("sector", ""),
                meta={"final_score": s.get("final_score", 0), "rank": s.get("rank", 0)},
            )
        )

    return candidates, diag
