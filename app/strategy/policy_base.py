"""
policy_base.py

Market Regime Router 정책 모듈들의 공통 인터페이스.
각 policy_*.py는 아래 시그니처를 구현한다.

    generate_candidates(market_ctx: dict, cfg=None) -> tuple[list[PolicyCandidate], dict]

market_ctx:
    {
      "snapshot": market_data_collector 수집 결과,
      "regime_result": MarketRegimeRouter.determine_regime() 결과,
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PolicyCandidate:
    symbol: str
    name: str
    entry_price: float
    stop_loss_price: float
    take_profit1_price: float
    take_profit2_price: float
    reason: str
    policy_name: str
    sector: str = ""
    meta: dict = field(default_factory=dict)


DEFAULT_TAKE_PROFIT1_PCT = 2.0
DEFAULT_TAKE_PROFIT2_PCT = 3.0
DEFAULT_STOP_LOSS_PCT = -1.2


_POLICY_MODULE_PATHS = {
    "policy_leader_top3": "app.strategy.policy_leader_top3",
    "policy_semiconductor_rebound": "app.strategy.policy_semiconductor_rebound",
    "policy_gap_support": "app.strategy.policy_gap_support",
    "policy_inverse": "app.strategy.policy_inverse",
    "policy_no_trade": "app.strategy.policy_no_trade",
}


def get_policy_module(policy_name: str):
    """policy_name 문자열로 정책 모듈을 동적 로드한다. 미지원 시 policy_no_trade로 대체."""
    import importlib

    path = _POLICY_MODULE_PATHS.get(policy_name, _POLICY_MODULE_PATHS["policy_no_trade"])
    return importlib.import_module(path)


def default_exit_prices(entry_price: float, exit_cfg: dict = None) -> tuple[float, float, float]:
    """진입가 기준 기본 손절가/1차익절가/2차익절가 산출.

    실제 매도 판정은 position_guard가 실시간 현재가로 수행하며, 이 값은
    후보 제시/승인 UI 표기용 참고치다.
    """
    exit_cfg = exit_cfg or {}
    tp1_pct = exit_cfg.get("take_profit1_pct", DEFAULT_TAKE_PROFIT1_PCT)
    tp2_pct = exit_cfg.get("take_profit2_pct", DEFAULT_TAKE_PROFIT2_PCT)
    sl_pct = exit_cfg.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)
    stop_loss = round(entry_price * (1 + sl_pct / 100), 0)
    tp1 = round(entry_price * (1 + tp1_pct / 100), 0)
    tp2 = round(entry_price * (1 + tp2_pct / 100), 0)
    return stop_loss, tp1, tp2
