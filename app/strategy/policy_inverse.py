"""
policy_inverse.py

E(급락 지속장) 유형에서만 사용하는 인버스/현금 정책.
09:40 이후 신규진입을 금지한다.
"""

from __future__ import annotations

from datetime import datetime

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.strategy.policy_base import PolicyCandidate

POLICY_NAME = "policy_inverse"

INVERSE_UNIVERSE = [
    {"symbol": "252670", "name": "KODEX 200선물인버스2X"},
    {"symbol": "251340", "name": "KODEX 코스닥150선물인버스"},
]

DEFAULT_NEW_ENTRY_CUTOFF = "09:40"
DEFAULT_STOP_LOSS_PCT = -1.5
DEFAULT_TAKE_PROFIT_PCT = 3.0


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def _fetch_price(symbol: str, kis_client=None) -> float:
    try:
        from app.market.kis_market_collector import fetch_stock_snapshot
        snap = fetch_stock_snapshot(symbol, kis_client=kis_client)
        if snap.get("success") and snap.get("current_price"):
            return float(snap["current_price"])
    except Exception:
        pass
    try:
        from app.data.naver_stock_collector import fetch_naver_current_price
        snap = fetch_naver_current_price(symbol)
        if snap.get("status") == "success":
            return float(snap["current_price"])
    except Exception:
        pass
    return 0.0


def generate_candidates(market_ctx: dict, cfg=None) -> tuple[list, dict]:
    market_ctx = market_ctx or {}
    regime_result = market_ctx.get("regime_result", {})
    regime = regime_result.get("regime", "")
    policy_cfg = (cfg or {}).get("policy_inverse", {}) if isinstance(cfg, dict) else {}
    entry_cutoff = policy_cfg.get("new_entry_cutoff_time", DEFAULT_NEW_ENTRY_CUTOFF)
    stop_loss_pct = policy_cfg.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)
    take_profit_pct = policy_cfg.get("take_profit_pct", DEFAULT_TAKE_PROFIT_PCT)

    diag = {"policy": POLICY_NAME, "regime": regime, "candidates_evaluated": 0}

    if regime != "E":
        diag["reason"] = "E 유형이 아니면 인버스 후보 생성 안 함"
        return [], diag

    now_hm = _now_hm()
    if now_hm >= entry_cutoff:
        diag["reason"] = f"신규진입 금지 시간({entry_cutoff} 이후)"
        return [], diag

    kis_client = market_ctx.get("kis_client")
    candidates = []
    for item in INVERSE_UNIVERSE:
        price = _fetch_price(item["symbol"], kis_client=kis_client)
        diag["candidates_evaluated"] += 1
        if price <= 0:
            logger.warning("[PolicyInverse] %s 현재가 조회 실패", item["symbol"])
            continue
        candidates.append(
            PolicyCandidate(
                symbol=item["symbol"],
                name=item["name"],
                entry_price=price,
                stop_loss_price=round(price * (1 + stop_loss_pct / 100), 0),
                take_profit1_price=round(price * (1 + take_profit_pct / 200), 0),
                take_profit2_price=round(price * (1 + take_profit_pct / 100), 0),
                reason="E 유형(급락 지속장) 방어 — 인버스 진입",
                policy_name=POLICY_NAME,
                sector="inverse",
            )
        )

    diag["candidates_selected"] = len(candidates)
    return candidates, diag
