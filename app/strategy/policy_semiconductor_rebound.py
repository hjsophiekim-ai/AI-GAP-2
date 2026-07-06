"""
policy_semiconductor_rebound.py

B 유형(급락 후 반도체 반등장) 정책.
후보: SK하이닉스, 삼성전자, 한미반도체.

매수 조건 (모두 소프트 스코어링, 필수 하드 조건은 09:20 저점 이탈 금지):
  - 09:20 저점 이탈 금지 (하드 제외)
  - 저점 대비 회복
  - 5분봉 고점 돌파 (데이터 없으면 건너뜀 — 죽지 않음)
  - 거래대금 증가
  - 마이크론/SOX/엔비디아 중 2개 이상 양호
"""

from __future__ import annotations

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from app.market.market_data_collector import HYNIX, SAMSUNG, HANMI
from app.strategy.policy_base import PolicyCandidate, default_exit_prices

POLICY_NAME = "policy_semiconductor_rebound"
UNIVERSE = [HYNIX, SAMSUNG, HANMI]
_MIN_SCORE = 40.0


def _five_min_high_broken(symbol: str, current_price: float, kis_client=None) -> bool:
    """5분봉(근사: 최근 5개 1분봉) 고점 돌파 여부. 데이터 없으면 False(가점 없음, 차단 아님)."""
    if kis_client is None or not current_price:
        return False
    try:
        candles = kis_client.get_minute_candles(symbol, period_min=1, count=5)
        if not candles:
            return False
        recent_high = max(c.get("high", 0) for c in candles)
        return bool(recent_high) and current_price > recent_high
    except Exception as exc:
        logger.debug("[PolicySemiRebound] %s 분봉 조회 실패: %s", symbol, exc)
        return False


def _score_stock(stock_snap: dict, ref_low: float, us_rebound_count: int, kis_client=None) -> dict:
    symbol = stock_snap.get("symbol", "")
    current = stock_snap.get("current_price") or 0.0
    low = stock_snap.get("low")
    prev_close = stock_snap.get("prev_close")
    trade_value = stock_snap.get("trade_value") or 0

    hard_excluded = False
    exclude_reason = ""
    if ref_low and current and current < ref_low:
        hard_excluded = True
        exclude_reason = "09:20_low_broken"

    score = 0.0
    reasons = []

    if ref_low and current and current >= ref_low:
        recovery_pct = (current - ref_low) / ref_low * 100 if ref_low else 0
        score += min(30.0, max(0.0, recovery_pct * 10))
        if recovery_pct > 0:
            reasons.append(f"09:20 저점 대비 +{recovery_pct:.1f}% 회복")

    if _five_min_high_broken(symbol, current, kis_client=kis_client):
        score += 20.0
        reasons.append("5분봉 고점 돌파")

    if trade_value and trade_value > 0:
        score += 15.0
        reasons.append("거래대금 확인")

    us_score = {0: 0.0, 1: 10.0, 2: 25.0, 3: 35.0}.get(us_rebound_count, 0.0)
    score += us_score
    if us_rebound_count >= 2:
        reasons.append(f"미국 반도체 지표 {us_rebound_count}개 양호")

    if isinstance(stock_snap.get("day1_return"), (int, float)) and stock_snap["day1_return"] < 0:
        reasons.append(f"전일 {stock_snap['day1_return']:.1f}% 하락 후 반등 시도")

    return {
        "symbol": symbol,
        "score": round(score, 2),
        "hard_excluded": hard_excluded,
        "exclude_reason": exclude_reason,
        "reasons": reasons,
        "current_price": current,
    }


def generate_candidates(market_ctx: dict, cfg=None) -> tuple[list, dict]:
    market_ctx = market_ctx or {}
    snapshot = market_ctx.get("snapshot", {}) or {}
    regime_result = market_ctx.get("regime_result", {}) or {}
    ref_0920 = regime_result.get("ref_0920") or {}
    exit_cfg = market_ctx.get("exit_cfg", {})
    kis_client = market_ctx.get("kis_client")

    domestic = snapshot.get("domestic", {})
    overseas = snapshot.get("overseas", {})
    us_rebound_count = sum(
        1 for k in ("micron", "sox", "nvidia")
        if overseas.get(k, {}).get("success") and (overseas.get(k, {}).get("change_rate") or 0) > 0
    )

    ref_map = {"000660": ref_0920.get("hynix_low"), "005930": ref_0920.get("samsung_low")}

    diag = {"policy": POLICY_NAME, "candidates_evaluated": 0, "hard_excluded": 0, "us_rebound_count": us_rebound_count}
    scored = []
    for stock_meta in UNIVERSE:
        symbol = stock_meta["symbol"]
        snap_key = {"000660": "hynix", "005930": "samsung", "042700": "hanmi"}[symbol]
        stock_snap = dict(domestic.get(snap_key, {}))
        stock_snap["symbol"] = symbol
        stock_snap["name"] = stock_meta["name"]

        diag["candidates_evaluated"] += 1
        if not stock_snap.get("current_price"):
            continue

        result = _score_stock(stock_snap, ref_map.get(symbol), us_rebound_count, kis_client=kis_client)
        if result["hard_excluded"]:
            diag["hard_excluded"] += 1
            continue
        result["name"] = stock_meta["name"]
        scored.append(result)

    scored.sort(key=lambda x: x["score"], reverse=True)
    candidates: list[PolicyCandidate] = []
    for r in scored:
        if r["score"] < _MIN_SCORE:
            continue
        entry_price = r["current_price"]
        stop_loss, tp1, tp2 = default_exit_prices(entry_price, exit_cfg)
        candidates.append(
            PolicyCandidate(
                symbol=r["symbol"],
                name=r["name"],
                entry_price=entry_price,
                stop_loss_price=stop_loss,
                take_profit1_price=tp1,
                take_profit2_price=tp2,
                reason=" | ".join(r["reasons"]) if r["reasons"] else "반도체 반등 스코어 충족",
                policy_name=POLICY_NAME,
                sector="semiconductor",
                meta={"rebound_score": r["score"]},
            )
        )

    diag["candidates_selected"] = len(candidates)
    return candidates[:3], diag
