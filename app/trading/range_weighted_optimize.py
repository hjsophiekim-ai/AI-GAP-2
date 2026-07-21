"""RANGE weighted 진입 최적화 — 목적함수, 일일 리스크, 추세/애매 구간 분류."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

STATE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "state" / "range_weighted_optimizer_result.json"

DAILY_LOSS_LIMIT_PCT = -0.8
TREND_DAY_TARGET_RETURN_PCT = 3.0
COST_GROSS_LIMIT = 0.25
WRONG_DIRECTION_LIMIT = 0.10
PF_TARGET = 1.4


@dataclass
class RangeWeightedConfig:
    """evaluate_range_weighted_entry 임계값 — 최적화 대상."""

    min_net_edge: float = 0.14
    min_reward_risk: float = 1.5
    safety_buffer: float = 0.05
    evidence_weak: float = 45.0
    evidence_neutral: float = 55.0
    evidence_mid: float = 65.0
    evidence_strong: float = 75.0
    evidence_ambiguous_boost: float = 10.0
    ambiguous_block_reversal: bool = True
    trend_day_size_boost: float = 0.12
    min_score_gap_ambiguous: float = 18.0
    daily_loss_limit_pct: float = DAILY_LOSS_LIMIT_PCT

    @classmethod
    def default(cls) -> RangeWeightedConfig:
        return cls()

    @classmethod
    def from_dict(cls, data: dict) -> RangeWeightedConfig:
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


@dataclass
class OptimizePenaltyWeights:
    lambda_mdd: float = 0.6
    lambda_cost: float = 80.0
    lambda_dir: float = 120.0
    lambda_trend_miss: float = 1.5
    lambda_daily_loss_breach: float = 50.0
    lambda_duplicate: float = 200.0


_active_config: RangeWeightedConfig = RangeWeightedConfig.default()


def get_range_weighted_config() -> RangeWeightedConfig:
    return _active_config


def set_range_weighted_config(cfg: RangeWeightedConfig) -> None:
    global _active_config
    _active_config = cfg


def load_optimized_config(path: Path | None = None) -> RangeWeightedConfig | None:
    p = path or STATE_PATH
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        cfg = RangeWeightedConfig.from_dict(data.get("config") or data)
        set_range_weighted_config(cfg)
        return cfg
    except Exception:
        return None


def save_optimized_config(cfg: RangeWeightedConfig, metrics: dict, path: Path | None = None) -> None:
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"config": asdict(cfg), "metrics": metrics}
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def classify_intraday_regime(hynix_df: pd.DataFrame) -> str:
    """당일 1분봉 기준: STRONG_TREND / NORMAL / AMBIGUOUS."""
    if hynix_df is None or hynix_df.empty or len(hynix_df) < 20:
        return "AMBIGUOUS"
    close0 = float(hynix_df["close"].iloc[0])
    if close0 <= 0:
        return "AMBIGUOUS"
    ret = (float(hynix_df["close"].iloc[-1]) / close0 - 1.0) * 100.0
    high = float(hynix_df["high"].max()) if "high" in hynix_df else float(hynix_df["close"].max())
    low = float(hynix_df["low"].min()) if "low" in hynix_df else float(hynix_df["close"].min())
    intraday_range = (high - low) / close0 * 100.0
    if abs(ret) >= 2.0 or intraday_range >= 3.5:
        return "STRONG_TREND"
    if abs(ret) < 0.5 and intraday_range < 1.5:
        return "AMBIGUOUS"
    return "NORMAL"


def evidence_thresholds_for_regime(regime: str, cfg: RangeWeightedConfig) -> dict[str, float]:
    """추세일은 문턱 완화, 애매일은 상향."""
    boost = cfg.evidence_ambiguous_boost if regime == "AMBIGUOUS" else 0.0
    reduce = 3.0 if regime == "STRONG_TREND" else 0.0
    return {
        "weak": cfg.evidence_weak + boost,
        "neutral": max(cfg.evidence_neutral + boost - reduce, cfg.evidence_weak + 5),
        "mid": max(cfg.evidence_mid + boost - reduce, cfg.evidence_neutral + 5),
        "strong": max(cfg.evidence_strong + boost - reduce, cfg.evidence_mid + 5),
    }


def min_net_edge_for_regime(regime: str, cfg: RangeWeightedConfig) -> float:
    if regime == "STRONG_TREND":
        return max(0.10, cfg.min_net_edge - 0.03)
    if regime == "AMBIGUOUS":
        return cfg.min_net_edge + 0.05
    return cfg.min_net_edge


def daily_loss_limit_reached(realized_pnl_krw: float, initial_cash: float, cfg: RangeWeightedConfig) -> bool:
    if initial_cash <= 0:
        return False
    return (realized_pnl_krw / initial_cash) * 100.0 <= cfg.daily_loss_limit_pct


def daily_loss_limit_reached_from_pct(daily_return_pct: float | None, cfg: RangeWeightedConfig) -> bool:
    return float(daily_return_pct or 0.0) <= cfg.daily_loss_limit_pct


def resolve_day_regime_from_cache() -> str:
    """당일 하이닉스 1분봉 캐시로 regime 분류 (KIS 호출 없음)."""
    try:
        from app.data_sources.auto_market_collector import _load_hynix_minute_cache

        df = _load_hynix_minute_cache()
        if df is None or getattr(df, "empty", True):
            return "NORMAL"
        return classify_intraday_regime(df)
    except Exception:
        return "NORMAL"


def aggregate_metrics(day_results: list[dict], *, initial_cash: float) -> dict[str, Any]:
    total_net = sum(d.get("net_pnl_krw", 0.0) for d in day_results)
    total_gross_profit = sum(d.get("gross_profit_krw", 0.0) for d in day_results)
    total_cost = sum(d.get("total_cost_krw", 0.0) for d in day_results)
    entries = sum(d.get("entries", 0) for d in day_results)
    wrong = sum(d.get("wrong_direction_entries", 0) for d in day_results)
    dup = sum(d.get("duplicate_episode_entries", 0) for d in day_results)
    pfs = [d["profit_factor"] for d in day_results if d.get("profit_factor") not in (None, float("inf"))]
    cost_ratios = [d["cost_over_gross"] for d in day_results if d.get("cost_over_gross") is not None]
    daily_returns = [d.get("return_pct", 0.0) for d in day_results]
    mdd_pcts = [d.get("max_intraday_dd_pct", 0.0) for d in day_results]
    trend_days = [d for d in day_results if d.get("regime") == "STRONG_TREND"]
    trend_returns = [d.get("return_pct", 0.0) for d in trend_days]
    daily_loss_breaches = sum(1 for d in day_results if d.get("daily_loss_breached"))

    return {
        "days": len(day_results),
        "total_net_pnl_krw": total_net,
        "total_return_pct": (total_net / initial_cash * 100.0) if initial_cash else 0.0,
        "avg_daily_return_pct": (sum(daily_returns) / len(daily_returns)) if daily_returns else 0.0,
        "median_pf": sorted(pfs)[len(pfs) // 2] if pfs else None,
        "median_cost_over_gross": sorted(cost_ratios)[len(cost_ratios) // 2] if cost_ratios else None,
        "wrong_direction_rate": (wrong / entries) if entries else 0.0,
        "duplicate_episode_entries": dup,
        "max_drawdown_pct": min(mdd_pcts) if mdd_pcts else 0.0,
        "trend_day_count": len(trend_days),
        "trend_day_median_return_pct": sorted(trend_returns)[len(trend_returns) // 2] if trend_returns else None,
        "trend_day_miss_3pct": sum(1 for r in trend_returns if r < TREND_DAY_TARGET_RETURN_PCT),
        "daily_loss_breaches": daily_loss_breaches,
        "total_cost_krw": total_cost,
        "total_gross_profit_krw": total_gross_profit,
        "cost_over_gross_total": (total_cost / total_gross_profit) if total_gross_profit > 0 else None,
    }


def compute_objective(metrics: dict, weights: OptimizePenaltyWeights | None = None) -> float:
    """Net PnL% − MDD − 비용 − 방향오류 − 추세일 미달 패널티."""
    w = weights or OptimizePenaltyWeights()
    net_pct = float(metrics.get("total_return_pct") or 0.0)
    mdd = abs(float(metrics.get("max_drawdown_pct") or 0.0))
    cost_ratio = float(metrics.get("median_cost_over_gross") or 0.0)
    wrong = float(metrics.get("wrong_direction_rate") or 0.0)
    dup = int(metrics.get("duplicate_episode_entries") or 0)
    trend_miss = int(metrics.get("trend_day_miss_3pct") or 0)
    daily_breach = int(metrics.get("daily_loss_breaches") or 0)

    cost_pen = max(0.0, cost_ratio - COST_GROSS_LIMIT) * w.lambda_cost
    dir_pen = max(0.0, wrong - WRONG_DIRECTION_LIMIT) * w.lambda_dir
    mdd_pen = mdd * w.lambda_mdd
    trend_pen = trend_miss * w.lambda_trend_miss
    breach_pen = daily_breach * w.lambda_daily_loss_breach
    dup_pen = dup * w.lambda_duplicate

    return net_pct - mdd_pen - cost_pen - dir_pen - trend_pen - breach_pen - dup_pen


def constraints_satisfied(metrics: dict) -> bool:
    pf = metrics.get("median_pf")
    cost = metrics.get("median_cost_over_gross")
    return (
        metrics.get("duplicate_episode_entries", 0) == 0
        and float(metrics.get("wrong_direction_rate") or 0.0) <= WRONG_DIRECTION_LIMIT
        and (pf is None or pf >= PF_TARGET)
        and (cost is None or cost <= COST_GROSS_LIMIT)
        and float(metrics.get("avg_daily_return_pct") or 0.0) > 0.0
        and int(metrics.get("daily_loss_breaches") or 0) == 0
    )
