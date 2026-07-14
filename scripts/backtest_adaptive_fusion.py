"""backtest_adaptive_fusion.py — Adaptive Fusion 개편(기존 55% 단일 게이트 vs
신규 52% 진입사다리+모델 불일치 예외진입+거래빈도/손실관리)을 비교한다.

사용법:
    python scripts/backtest_adaptive_fusion.py

출력:
    reports/adaptive_fusion_backtest.md

방법과 한계(반드시 읽을 것):
  CYCLE_AI/PREDICTION_V2/MICRON_PROXY 등 5개 모델의 과거(60거래일치) 원본 확률
  로그가 이 저장소에 존재하지 않는다(이 기능 자체가 2026-07-14에 신설됐고, 각
  모델의 실시간 확률은 그 순간의 라이브 데이터에서만 계산되어 과거 재현이
  불가능하다 — 예: Cycle AI/Micron Proxy는 그 시각의 실시간 분봉/마이크론
  데이터가 있어야 계산된다). 따라서 이 백테스트는 "과거 실제 판단의 재생"이
  아니라, data/hynix/hynix_daily.csv의 실제 일별 변동성 통계로 보정한
  몬테카를로 시뮬레이션이다 — 모델 신뢰도/합의 패턴을 실제 시장 변동성 분포에
  맞춰 합성하고, 동일한 합성 신호를 기존 로직과 신규 로직에 동시에 통과시켜
  두 로직만의 차이(거래빈도/손익)를 비교한다. 절대수익률 자체의 정확한 예측이
  목적이 아니라 "신규 로직이 거래빈도를 늘리면서 리스크 지표를 크게 악화시키지
  않는지"를 확인하는 상대 비교 도구다.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import app.trading.hynix_adaptive_fusion_engine as fusion  # noqa: E402

RNG = np.random.default_rng(20260714)

_OLD_LADDER = [(88.0, 85.0), (80.0, 70.0), (72.0, 50.0), (65.0, 35.0), (60.0, 20.0), (55.0, 10.0)]


def _old_position_pct(dominant_probability: float) -> float:
    for floor, pct in _OLD_LADDER:
        if dominant_probability >= floor:
            return pct
    return 0.0


def _load_daily_returns() -> np.ndarray:
    path = ROOT / "data" / "hynix" / "hynix_daily.csv"
    df = pd.read_csv(path)
    df = df.sort_values("date")
    closes = df["close"].astype(float).to_numpy()
    rets = np.diff(closes) / closes[:-1]
    return rets


def _classify_regime(day_return: float, vol: float) -> str:
    if day_return <= -1.5 * vol:
        return "급락장"
    if day_return >= 1.5 * vol:
        return "급등장"
    return "횡보장"


def _simulate_day(regime: str, vol: float, n_cycles: int = 116):
    """하루치 3분 주기 사이클(09:10~14:50)의 합성 (dominant_probability, 방향, 실제수익률)
    시퀀스를 만든다. 급등/급락장은 방향성 있는 모델 합의가 자주 나오도록,
    횡보장은 모델 불일치(중립~약한 신호)가 자주 나오도록 보정한다."""
    if regime == "급등장":
        true_direction = 1
        signal_strength = RNG.normal(0.62, 0.08, n_cycles).clip(0.5, 0.95)
    elif regime == "급락장":
        true_direction = -1
        signal_strength = RNG.normal(0.62, 0.08, n_cycles).clip(0.5, 0.95)
    else:
        true_direction = RNG.choice([1, -1])
        signal_strength = RNG.normal(0.50, 0.05, n_cycles).clip(0.35, 0.70)

    # 5개 모델의 노이즈가 섞인 확률 — 실제 하루 수익률 방향과 상관되게 만들되
    # 횡보장에서는 모델 간 불일치가 더 크게(노이즈 표준편차 확대) 발생시킨다.
    noise_std = 0.05 if regime != "횡보장" else 0.14
    model_signals = []
    for _ in range(5):
        noisy = signal_strength + RNG.normal(0.0, noise_std, n_cycles)
        model_signals.append(np.clip(noisy, 0.05, 0.98))

    price_path = 1.0 + np.cumsum(RNG.normal(true_direction * vol / n_cycles, vol / np.sqrt(n_cycles), n_cycles))
    return model_signals, true_direction, price_path


def _run_cycle_old(model_probs_this_cycle: list, daily_return_pct: float) -> dict:
    dom = max(model_probs_this_cycle) * 100.0
    pct = _old_position_pct(dom) if dom >= 55.0 else 0.0
    risk = fusion.adaptive_fusion_daily_risk_ladder(daily_return_pct)
    pct = min(pct, risk["max_position_pct"])
    if not risk["entries_allowed"]:
        pct = 0.0
    return {"pct": pct, "direction": 1}


def _run_cycle_new(model_probs_this_cycle: list, daily_return_pct: float, freq_state: dict, now: datetime) -> dict:
    hynix_probs = [p * 100.0 for p in model_probs_this_cycle]
    model_results = {}
    for i, hp in enumerate(hynix_probs):
        conf = abs(hp - 50.0) * 2.0
        model_results[f"M{i}"] = fusion.build_model_result(
            model_name=f"M{i}", hynix_probability=hp, inverse_probability=100.0 - hp,
            hold_probability=0.0, confidence=conf, recommended_position_pct=0.0,
            data_quality=80.0, model_status=fusion.MODEL_STATUS_ADVISORY,
        )
    if fusion.detect_strong_signal_conflict(model_results):
        return {"pct": 0.0, "direction": 1, "entry_type": None}

    dom = max(hynix_probs)
    base_pct = fusion.position_pct_from_probability_ladder(dom)
    entry_type = None
    if base_pct <= 0:
        override = fusion.evaluate_disagreement_override(model_results)
        if override is not None:
            base_pct = override["position_pct"]
            entry_type = "EXPLORATORY"
    elif base_pct <= fusion._load_fusion_v2_config()["exploratory_max_pct"]:
        entry_type = "EXPLORATORY"
    else:
        entry_type = "NORMAL"

    time_relief = fusion.time_based_threshold_relief(now, orders_today_count=0, daily_return_pct=daily_return_pct)
    if base_pct <= 0 and time_relief["relief"] > 0:
        relaxed_dom = dom + time_relief["relief"]
        base_pct = fusion.position_pct_from_probability_ladder(relaxed_dom)
        if base_pct > 0:
            entry_type = "EXPLORATORY"

    risk = fusion.adaptive_fusion_daily_risk_ladder(daily_return_pct)
    base_pct = min(base_pct, risk["max_position_pct"])
    if not risk["entries_allowed"]:
        base_pct = 0.0

    if base_pct > 0:
        block = fusion.check_frequency_limits(freq_state, "HYNIX", now)
        if block:
            base_pct = 0.0

    return {"pct": base_pct, "direction": 1, "entry_type": entry_type}


def _simulate_logic(days: list, use_new: bool) -> dict:
    equity = 1_000_000_000.0
    equity_curve = [equity]
    daily_trade_counts = []
    trade_returns = []
    regime_pnl = {"급등장": [], "급락장": [], "횡보장": []}
    round_trip_cost_pct = 0.0006  # 왕복 수수료+세금+슬리피지 근사(0.06%)

    for day_idx, (regime, vol, model_signals, price_path) in enumerate(days):
        n_cycles = len(price_path)
        freq_state = fusion.default_frequency_state()
        position_open = False
        entry_cycle_price = None
        trades_today = 0
        daily_return_pct = 0.0
        day_start_equity = equity
        base_date = datetime(2026, 1, 5, 9, 10) + timedelta(days=day_idx)

        for c in range(n_cycles):
            now = base_date + timedelta(minutes=3 * c)
            probs_this_cycle = [signals[c] for signals in model_signals]

            if not position_open:
                if use_new:
                    decision = _run_cycle_new(probs_this_cycle, daily_return_pct, freq_state, now)
                else:
                    decision = _run_cycle_old(probs_this_cycle, daily_return_pct)
                if decision["pct"] > 0:
                    position_open = True
                    entry_cycle_price = price_path[c]
                    entry_pct = decision["pct"] / 100.0
                    if use_new:
                        freq_state = fusion.register_frequency_entry(freq_state, "HYNIX", now)
            else:
                # 다음 사이클에 단순 매도(하루 1회전 가정 근사) — 실제 시스템은 TP/SL/
                # 반대신호로 청산하지만, 이 백테스트는 로직별 "진입 판단" 차이에 집중한다.
                exit_price = price_path[c]
                raw_ret = (exit_price - entry_cycle_price) / entry_cycle_price
                net_ret = raw_ret * entry_pct - round_trip_cost_pct
                pnl = day_start_equity * net_ret
                equity += pnl
                trade_returns.append(net_ret)
                regime_pnl[regime].append(net_ret)
                daily_return_pct = (equity / day_start_equity - 1.0) * 100.0
                trades_today += 1
                position_open = False
                if use_new:
                    freq_state = fusion.register_frequency_round_trip_closed(freq_state, now)

        if position_open:
            exit_price = price_path[-1]
            raw_ret = (exit_price - entry_cycle_price) / entry_cycle_price
            net_ret = raw_ret * entry_pct - round_trip_cost_pct
            pnl = day_start_equity * net_ret
            equity += pnl
            trade_returns.append(net_ret)
            regime_pnl[regime].append(net_ret)
            trades_today += 1

        daily_trade_counts.append(trades_today)
        equity_curve.append(equity)

    equity_arr = np.array(equity_curve)
    running_max = np.maximum.accumulate(equity_arr)
    drawdown = (equity_arr - running_max) / running_max
    mdd = float(drawdown.min()) * 100.0

    wins = [r for r in trade_returns if r > 0]
    losses = [abs(r) for r in trade_returns if r < 0]
    profit_factor = (sum(wins) / sum(losses)) if losses else (float("inf") if wins else None)
    win_rate = (len(wins) / len(trade_returns) * 100.0) if trade_returns else None
    no_trade_days = sum(1 for c in daily_trade_counts if c == 0)

    return {
        "final_equity": equity, "net_return_pct": (equity / 1_000_000_000.0 - 1.0) * 100.0,
        "mdd_pct": mdd, "no_trade_day_ratio_pct": no_trade_days / len(days) * 100.0,
        "avg_trades_per_day": float(np.mean(daily_trade_counts)),
        "win_rate_pct": win_rate, "profit_factor": profit_factor,
        "total_trades": len(trade_returns),
        "regime_avg_return_pct": {
            k: (float(np.mean(v)) * 100.0 if v else None) for k, v in regime_pnl.items()
        },
    }


def main() -> None:
    daily_rets = _load_daily_returns()
    vol = float(np.std(daily_rets)) if len(daily_rets) > 1 else 0.02
    n_days = 60

    days = []
    for _ in range(n_days):
        sampled_ret = float(RNG.choice(daily_rets)) if len(daily_rets) else RNG.normal(0.0, vol)
        regime = _classify_regime(sampled_ret, vol)
        model_signals, _direction, price_path = _simulate_day(regime, vol)
        days.append((regime, vol, model_signals, price_path))

    old_result = _simulate_logic(days, use_new=False)
    new_result = _simulate_logic(days, use_new=True)

    lines = []
    lines.append("# Adaptive Fusion 백테스트 — 기존(단일 55% 게이트) vs 신규(52% 사다리+예외진입)")
    lines.append("")
    lines.append(f"- 시뮬레이션 일수: {n_days}거래일 (몬테카를로, data/hynix/hynix_daily.csv 변동성 통계로 보정)")
    lines.append(f"- 실측 일별수익률 표준편차(변동성): {vol*100:.2f}%")
    lines.append("- **주의**: 5개 모델의 과거 원본 로그가 없어 실제 판단 재생이 아니라 합성 시뮬레이션임(스크립트 상단 docstring 참조)")
    lines.append("")
    lines.append("| 지표 | 기존(55% 단일게이트) | 신규(52% 사다리+예외진입) |")
    lines.append("|---|---|---|")
    lines.append(f"| 거래없는 날 비율 | {old_result['no_trade_day_ratio_pct']:.1f}% | {new_result['no_trade_day_ratio_pct']:.1f}% |")
    lines.append(f"| 하루 평균 거래수 | {old_result['avg_trades_per_day']:.2f}회 | {new_result['avg_trades_per_day']:.2f}회 |")
    lines.append(f"| 총 거래수(60일) | {old_result['total_trades']}건 | {new_result['total_trades']}건 |")
    lines.append(f"| 순수익률(수수료 포함) | {old_result['net_return_pct']:+.2f}% | {new_result['net_return_pct']:+.2f}% |")
    lines.append(f"| 승률 | {(old_result['win_rate_pct'] or 0):.1f}% | {(new_result['win_rate_pct'] or 0):.1f}% |")
    lines.append(f"| Profit Factor | {old_result['profit_factor']:.2f} | {new_result['profit_factor']:.2f} |" if old_result['profit_factor'] and new_result['profit_factor'] else "| Profit Factor | N/A | N/A |")
    lines.append(f"| 최대낙폭(MDD) | {old_result['mdd_pct']:.2f}% | {new_result['mdd_pct']:.2f}% |")
    lines.append("")
    lines.append("## 시장 상황별 평균 거래 수익률")
    lines.append("")
    lines.append("| 장세 | 기존 | 신규 |")
    lines.append("|---|---|---|")
    for regime in ("급락장", "급등장", "횡보장"):
        o = old_result["regime_avg_return_pct"][regime]
        n = new_result["regime_avg_return_pct"][regime]
        o_s = f"{o:+.3f}%" if o is not None else "거래없음"
        n_s = f"{n:+.3f}%" if n is not None else "거래없음"
        lines.append(f"| {regime} | {o_s} | {n_s} |")
    lines.append("")

    degraded = (
        new_result["total_trades"] > old_result["total_trades"]
        and (new_result["mdd_pct"] < old_result["mdd_pct"] * 1.2 - 0.01 or new_result["net_return_pct"] < old_result["net_return_pct"] - 0.5)
    )
    if degraded:
        lines.append(
            "## 자동 재조정 판단\n\n거래수는 늘었으나 MDD 또는 순수익이 유의미하게 악화됨 — "
            "config/hynix_enhanced_weights.json의 adaptive_fusion_v2.disagreement_entry_pct를 "
            "낮추거나 entry_ladder 하위 구간 비중을 축소하는 재조정이 필요합니다."
        )
    else:
        lines.append(
            "## 자동 재조정 판단\n\n거래수 증가 대비 리스크 지표(MDD)/순수익 악화가 기준 이내로 확인되어 "
            "추가 재조정 없이 현재 설정을 유지합니다."
        )

    report_path = ROOT / "reports" / "adaptive_fusion_backtest.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[written] {report_path}")


if __name__ == "__main__":
    main()
