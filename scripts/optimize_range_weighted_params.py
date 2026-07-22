"""RANGE weighted 파라미터 최적화.

목적함수: Net PnL% − MDD패널티 − 비용패널티 − 방향오류패널티 − 추세일미달패널티
제약: PF≥1.4, 비용/Gross≤25%, 방향오류≤10%, 중복episode 0, 일손실≤-0.8%

사용법:
    python scripts/optimize_range_weighted_params.py [--trials 80]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app.trading.range_weighted_optimize import (  # noqa: E402
    RangeWeightedConfig,
    aggregate_metrics,
    compute_objective,
    constraints_satisfied,
    save_optimized_config,
    set_range_weighted_config,
)
from scripts.validate_range_mock_20days import INITIAL_CASH, N_DAYS, RNG, _simulate_day  # noqa: E402
from scripts.backtest_switch_engine_synthetic_day import (  # noqa: E402
    _load_daily_returns_and_df,
    generate_synthetic_day,
)


def _sample_config(rng: np.random.Generator) -> RangeWeightedConfig:
    return RangeWeightedConfig(
        min_net_edge=float(rng.uniform(0.10, 0.18)),
        min_reward_risk=float(rng.uniform(1.3, 1.8)),
        safety_buffer=float(rng.uniform(0.03, 0.07)),
        evidence_weak=float(rng.uniform(40.0, 50.0)),
        evidence_neutral=float(rng.uniform(52.0, 58.0)),
        evidence_mid=float(rng.uniform(62.0, 68.0)),
        evidence_strong=float(rng.uniform(72.0, 78.0)),
        evidence_ambiguous_boost=float(rng.uniform(8.0, 14.0)),
        ambiguous_block_reversal=bool(rng.integers(0, 2)),
        trend_day_size_boost=float(rng.uniform(0.08, 0.16)),
        min_score_gap_ambiguous=float(rng.uniform(15.0, 22.0)),
        daily_loss_limit_pct=-0.8,
    )


def _evaluate_config(cfg: RangeWeightedConfig, daily_rets: list) -> tuple[float, dict]:
    set_range_weighted_config(cfg)
    day_results = []
    for day_idx in range(N_DAYS):
        RNG.shuffle(daily_rets)
        day = generate_synthetic_day(daily_rets)
        row = _simulate_day(day, day_idx)
        day_results.append(row)
    metrics = aggregate_metrics(day_results, initial_cash=INITIAL_CASH)
    objective = compute_objective(metrics)
    return objective, metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=80)
    args = parser.parse_args()

    daily_rets, _ = _load_daily_returns_and_df()
    rng = np.random.default_rng(20260722)

    baseline_cfg = RangeWeightedConfig.default()
    baseline_obj, baseline_metrics = _evaluate_config(baseline_cfg, daily_rets)

    best_cfg = baseline_cfg
    best_obj = baseline_obj
    best_metrics = baseline_metrics

    print("=" * 72)
    print("RANGE weighted parameter search")
    print("=" * 72)
    print(f"Baseline objective: {baseline_obj:.3f}  constraints: {constraints_satisfied(baseline_metrics)}")
    print(f"  avg daily ret: {baseline_metrics.get('avg_daily_return_pct', 0):.3f}%")
    print(f"  median PF: {baseline_metrics.get('median_pf')}")
    print(f"  wrong dir: {baseline_metrics.get('wrong_direction_rate', 0)*100:.1f}%")
    print("-" * 72)

    for trial in range(args.trials):
        cfg = _sample_config(rng)
        obj, metrics = _evaluate_config(cfg, daily_rets)
        ok = constraints_satisfied(metrics)
        bonus = 0.5 if ok else 0.0
        score = obj + bonus
        if score > best_obj + (0.5 if constraints_satisfied(best_metrics) else 0.0):
            best_obj = obj
            best_cfg = cfg
            best_metrics = metrics
            tag = "✓" if ok else "~"
            print(
                f"[{trial+1:03d}] {tag} obj={obj:.3f} net={metrics.get('total_return_pct', 0):.2f}% "
                f"PF={metrics.get('median_pf')} wrong={metrics.get('wrong_direction_rate', 0)*100:.0f}% "
                f"dup={metrics.get('duplicate_episode_entries')}"
            )

    save_optimized_config(best_cfg, best_metrics)
    set_range_weighted_config(best_cfg)

    print("-" * 72)
    print("BEST CONFIG")
    print(f"  objective: {best_obj:.3f}")
    print(f"  constraints satisfied: {constraints_satisfied(best_metrics)}")
    print(f"  total return: {best_metrics.get('total_return_pct', 0):.3f}%")
    print(f"  avg daily return: {best_metrics.get('avg_daily_return_pct', 0):.3f}%")
    print(f"  median PF: {best_metrics.get('median_pf')}")
    print(f"  cost/gross: {best_metrics.get('median_cost_over_gross')}")
    print(f"  wrong direction: {best_metrics.get('wrong_direction_rate', 0)*100:.1f}%")
    print(f"  duplicate episodes: {best_metrics.get('duplicate_episode_entries')}")
    print(f"  trend day median ret: {best_metrics.get('trend_day_median_return_pct')}")
    print(f"  saved → data/state/range_weighted_optimizer_result.json")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
