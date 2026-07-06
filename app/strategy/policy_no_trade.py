"""
policy_no_trade.py

D/E/F 유형 및 리스크 제한 시 사용하는 매매 안 함 정책.
신규 후보를 생성하지 않는다 (기존 포지션은 position_guard가 계속 관리).
"""

from __future__ import annotations

from app.strategy.policy_base import PolicyCandidate

POLICY_NAME = "policy_no_trade"


def generate_candidates(market_ctx: dict, cfg=None) -> tuple[list[PolicyCandidate], dict]:
    regime = (market_ctx or {}).get("regime_result", {}).get("regime", "")
    diag = {"policy": POLICY_NAME, "regime": regime, "reason": "신규매수 금지 정책"}
    return [], diag
