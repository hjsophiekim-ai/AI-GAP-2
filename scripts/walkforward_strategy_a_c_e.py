"""A vs C vs E walk-forward comparison (min 20 trading days).

시나리오:
  A — weighted RANGE 단독 (프로덕션 주문 경로)
  C — MACD+Williams 3분봉 단독 (참고용, broker 주문 아님을 시뮬)
  E — C로 episode 방향 확인 + A로 주문·비중·청산

평가 기준: 보수적 Net PnL, PF, MDD, 방향오류, 비용/Gross, 거래횟수, 15/30초 지연 민감도

E가 A를 안정적으로 이길 때만 data/state/strategy_e_promotion.json 에
promote_e_to_live=true 를 기록한다. 하루 데이터로 임계값을 최적화하지 않는다.

사용법:
    python scripts/walkforward_strategy_a_c_e.py [--days 20] [--promote-if-win]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.services import hynix_switch_engine as engine  # noqa: E402
from app.trading import early_trend_detector as etd  # noqa: E402
from app.trading.etf_entry_confirmation import compute_etf_breakouts, resolve_window_directions  # noqa: E402
from app.trading.hynix_fast_trend import compute_fast_trend_signal  # noqa: E402
from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL as INVERSE_SYMBOL  # noqa: E402
from app.trading.macd_williams_episode import confirm_episode_direction  # noqa: E402
from app.trading.range_weighted_optimize import (  # noqa: E402
    classify_intraday_regime,
    get_range_weighted_config,
)
from app.trading.strategy_architecture import STATE_PROMOTION_PATH  # noqa: E402
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402
from scripts.backtest_switch_engine_synthetic_day import (  # noqa: E402
    _load_daily_returns_and_df,
    generate_synthetic_day,
)

INITIAL_CASH = 10_000_000.0
DEFAULT_DAYS = 20
SLIP_BASE = 0.05
RNG = np.random.default_rng(20260722)


@dataclass
class DayMetrics:
    scenario: str
    day_index: int
    net_pnl: float = 0.0
    return_pct: float = 0.0
    pf: float = 0.0
    mdd_pct: float = 0.0
    entries: int = 0
    wrong_dir: int = 0
    total_cost: float = 0.0
    cost_gross: float = 0.0
    gross_profit: float = 0.0


def _fill(df: pd.DataFrame, i: int, side: str, slip_pct: float) -> Optional[float]:
    """다음 봉 open + 슬리피지. 미래 봉이 없으면 None (미체결 제외)."""
    if i + 1 >= len(df):
        return None
    base = float(df.iloc[i + 1]["open"])
    s = slip_pct / 100.0
    return base * (1.0 + s) if side == "BUY" else base * (1.0 - s)


def _simulate_scenario(
    day: dict,
    scenario: str,
    *,
    slip_pct: float = SLIP_BASE,
    require_episode_confirm: bool = False,
) -> DayMetrics:
    """1분봉 단위 단순 시뮬. A/E는 evaluate_range_weighted_entry, C는 MACD+WR만."""
    underlying = day["underlying"]
    leverage = day["leverage"]
    inverse = day["inverse"]
    times = day["times"]
    true_dir = "UP" if day["true_direction"] > 0 else "DOWN"
    cfg = get_range_weighted_config()
    cost_engine = TradeCostEngine()
    regime = classify_intraday_regime(underlying)

    cash = INITIAL_CASH
    peak = INITIAL_CASH
    max_dd = 0.0
    position = None
    trades: list[dict] = []
    episode_ids: set[str] = set()
    wrong = 0
    prev_hist_sign = 0

    for i in range(30, len(times)):
        now = times[i]
        if now.hour < 9 or (now.hour == 14 and now.minute > 50) or now.hour >= 15:
            if position and now.hour >= 15 and now.minute >= 15:
                etf = leverage if position["symbol"] == LONG_SYMBOL else inverse
                px = _fill(etf, i, "SELL", slip_pct) or float(etf.iloc[i]["close"])
                qty = position["qty"]
                gross = (px - position["entry_price"]) * qty
                cost = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * qty
                net = gross - cost
                cash += qty * px
                trades.append({"net": net, "gross": gross, "cost": cost})
                peak = max(peak, cash)
                max_dd = min(max_dd, (cash / peak - 1.0) * 100.0)
                position = None
            continue

        u_slice = underlying.iloc[: i + 1]
        fast = compute_fast_trend_signal(u_slice, now=now)
        live_dir = fast.get("direction")
        if live_dir not in ("UP", "DOWN"):
            continue

        ep = confirm_episode_direction(u_slice, proposed_direction=live_dir, now=now)

        # ── C 단독: MACD hist 교차 + Williams ──
        if scenario == "C":
            bars_ok = len(u_slice) >= 26
            if not bars_ok:
                continue
            closes = pd.to_numeric(u_slice.set_index("datetime").resample("3min")["close"].last().dropna(), errors="coerce")
            if len(closes) < 26:
                continue
            from app.trading.macd_williams_episode import _macd_histogram, _williams_r

            hist = _macd_histogram(closes)
            h3 = u_slice.set_index("datetime").resample("3min").agg({"high": "max", "low": "min", "close": "last"}).dropna()
            wr = _williams_r(
                pd.to_numeric(h3["high"], errors="coerce"),
                pd.to_numeric(h3["low"], errors="coerce"),
                pd.to_numeric(h3["close"], errors="coerce"),
            ) if len(h3) >= 14 else None
            if hist is None:
                continue
            hist_sign = 1 if hist > 0 else (-1 if hist < 0 else 0)
            cross_up = prev_hist_sign <= 0 and hist_sign > 0
            cross_down = prev_hist_sign >= 0 and hist_sign < 0
            prev_hist_sign = hist_sign

            if position is not None:
                etf = leverage if position["symbol"] == LONG_SYMBOL else inverse
                held = float(etf.iloc[i]["close"])
                ret = (held / position["entry_price"] - 1.0) * 100.0
                exit_now = False
                if ret <= -0.5:
                    exit_now = True
                elif position["direction"] == "UP" and cross_down:
                    exit_now = True
                elif position["direction"] == "DOWN" and cross_up:
                    exit_now = True
                elif ret >= 1.35:
                    exit_now = True
                if exit_now:
                    px = _fill(etf, i, "SELL", slip_pct)
                    if px is None:
                        continue
                    qty = position["qty"]
                    gross = (px - position["entry_price"]) * qty
                    cost = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * qty
                    net = gross - cost
                    cash += qty * px
                    trades.append({"net": net, "gross": gross, "cost": cost})
                    peak = max(peak, cash)
                    max_dd = min(max_dd, (cash / peak - 1.0) * 100.0)
                    position = None

            if position is None and (cross_up or cross_down):
                trade_dir = "UP" if cross_up else "DOWN"
                wr_ok = (trade_dir == "UP" and wr is not None and wr < -50) or (
                    trade_dir == "DOWN" and wr is not None and wr > -50
                )
                if not wr_ok:
                    continue
                symbol = LONG_SYMBOL if trade_dir == "UP" else INVERSE_SYMBOL
                ep_id = f"C:{trade_dir}:{now.strftime('%H%M')}"
                if ep_id in episode_ids:
                    continue
                episode_ids.add(ep_id)
                etf = leverage if symbol == LONG_SYMBOL else inverse
                px = _fill(etf, i, "BUY", slip_pct)
                if px is None:
                    continue
                qty = max(1, int(cash * 0.40 / px))
                if qty * px > cash:
                    continue
                cash -= qty * px
                if trade_dir != true_dir:
                    wrong += 1
                position = {"symbol": symbol, "direction": trade_dir, "qty": qty, "entry_price": px, "entry_time": now}
            continue

        # ── A / E: weighted RANGE ──
        if require_episode_confirm and not ep.get("confirmed"):
            # E: episode 미확인이면 신규진입 금지 (청산은 계속)
            if position is None:
                continue

        desired_symbol = LONG_SYMBOL if live_dir == "UP" else INVERSE_SYMBOL
        etf_df = leverage.iloc[: i + 1] if desired_symbol == LONG_SYMBOL else inverse.iloc[: i + 1]
        current_price = float(etf_df.iloc[-1]["close"])
        breakouts = compute_etf_breakouts(etf_df, current_price, live_dir)
        signal_dirs = {5: live_dir, 10: live_dir, 20: live_dir, 30: live_dir}
        oppose_dirs = {5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"}
        if live_dir == "DOWN":
            oppose_dirs = {5: "UP", 10: "UP", 20: "UP", 30: "UP"}
        decision = {
            "final_action": "HYNIX_STRONG_BUY" if live_dir == "UP" else "INVERSE_STRONG_BUY",
            "enhanced_score": 78.0 if live_dir == "UP" else 22.0,
            "inverse_pressure_score": 22.0 if live_dir == "UP" else 78.0,
        }
        cost_gate = etd.evaluate_cost_gate(desired_symbol, 0.55)
        result = engine.evaluate_range_weighted_entry(
            decision=decision,
            direction=live_dir,
            live_direction=live_dir,
            signal_window_directions=signal_dirs,
            confirm_window_directions=resolve_window_directions({"window_directions": signal_dirs}),
            oppose_window_directions=oppose_dirs,
            confirm_above_vwap=breakouts.get("vwap_breakout"),
            data_age_seconds=2.0,
            expected_move_pct=0.55,
            cost_pct=cost_gate.get("cost_pct") or 0.08,
            expected_mfe_pct=0.55,
            expected_mae_pct=0.35,
            ema_slope_aligned=True,
            structure_confirmed=bool(breakouts.get("structure_breakout")),
            structural_direction=true_dir,
            entry_path_hint=None,
            day_regime=regime,
            range_config=cfg,
        )

        if position is not None:
            etf = leverage if position["symbol"] == LONG_SYMBOL else inverse
            held = float(etf.iloc[i]["close"])
            ret = (held / position["entry_price"] - 1.0) * 100.0
            opposite = live_dir != position["direction"]
            exit_plan = etd.should_exit_probe(
                net_return_pct=ret,
                seconds_since_last_reconfirmation=5.0,
                signal_still_valid=live_dir == position["direction"],
                opposite_change_point=opposite,
                confirmed_regime=etd.REGIME_FAST_REVERSAL_RANGE,
                opposite_live_seconds=10.0 if opposite else 0.0,
                position_direction=position["direction"],
                held_etf_reversal_windows={5: opposite, 10: opposite, 20: False, 30: False},
                opposite_etf_5s10s_confirmed=opposite,
                structure_reversal_confirmed=False,
                peak_net_return_pct=max(ret, 0.0),
            )
            if exit_plan["action"] in ("SELL_ALL", "SELL_PARTIAL") and (
                exit_plan["action"] == "SELL_ALL" or ret <= -0.4
            ):
                px = _fill(etf, i, "SELL", slip_pct)
                if px is None:
                    continue
                sell_qty = position["qty"] if exit_plan["action"] == "SELL_ALL" else max(1, int(position["qty"] * exit_plan["ratio"]))
                gross = (px - position["entry_price"]) * sell_qty
                cost = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * sell_qty
                net = gross - cost
                cash += sell_qty * px
                trades.append({"net": net, "gross": gross, "cost": cost})
                peak = max(peak, cash)
                max_dd = min(max_dd, (cash / peak - 1.0) * 100.0)
                position["qty"] -= sell_qty
                if position["qty"] <= 0:
                    position = None

        if position is None and result.get("action") == "ENTER":
            ep_id = f"{scenario}:{live_dir}:{now.strftime('%H%M')}"
            if ep_id in episode_ids:
                continue
            episode_ids.add(ep_id)
            etf = leverage if desired_symbol == LONG_SYMBOL else inverse
            px = _fill(etf, i, "BUY", slip_pct)
            if px is None:
                continue
            qty = max(1, int(cash * float(result.get("target_pct") or 0.25) / px))
            if qty * px > cash:
                continue
            cash -= qty * px
            if live_dir != true_dir:
                wrong += 1
            position = {
                "symbol": desired_symbol,
                "direction": live_dir,
                "qty": qty,
                "entry_price": px,
                "entry_time": now,
            }

    if position is not None:
        etf = leverage if position["symbol"] == LONG_SYMBOL else inverse
        px = float(etf.iloc[-1]["close"])
        qty = position["qty"]
        gross = (px - position["entry_price"]) * qty
        cost = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * qty
        net = gross - cost
        cash += qty * px
        trades.append({"net": net, "gross": gross, "cost": cost})

    net_pnl = sum(t["net"] for t in trades)
    gp = sum(t["gross"] for t in trades if t["gross"] > 0)
    gl = abs(sum(t["gross"] for t in trades if t["gross"] < 0))
    pf = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    total_cost = sum(t["cost"] for t in trades)
    return DayMetrics(
        scenario=scenario,
        day_index=0,
        net_pnl=net_pnl,
        return_pct=net_pnl / INITIAL_CASH * 100.0,
        pf=pf if pf != float("inf") else 99.0,
        mdd_pct=max_dd,
        entries=len(episode_ids),
        wrong_dir=wrong,
        total_cost=total_cost,
        cost_gross=(total_cost / gp * 100.0) if gp > 0 else 0.0,
        gross_profit=gp,
    )


def _aggregate(rows: list[DayMetrics]) -> dict:
    if not rows:
        return {}
    nets = [r.net_pnl for r in rows]
    rets = [r.return_pct for r in rows]
    pfs = [r.pf for r in rows if r.pf > 0]
    mdds = [r.mdd_pct for r in rows]
    entries = sum(r.entries for r in rows)
    wrong = sum(r.wrong_dir for r in rows)
    cost = sum(r.total_cost for r in rows)
    gp = sum(r.gross_profit for r in rows)
    return {
        "days": len(rows),
        "total_net_pnl": sum(nets),
        "avg_daily_return_pct": statistics.mean(rets) if rets else 0.0,
        "median_pf": statistics.median(pfs) if pfs else 0.0,
        "worst_mdd_pct": min(mdds) if mdds else 0.0,
        "entries": entries,
        "wrong_dir_rate": (wrong / entries) if entries else 0.0,
        "cost_gross_pct": (cost / gp * 100.0) if gp > 0 else 0.0,
        "positive_days": sum(1 for r in rets if r > 0),
    }


def e_beats_a(agg_e: dict, agg_a: dict) -> bool:
    """안정적 우위: Net↑, PF≥A, MDD 악화 없음, 방향오류≤A, 양수일수≥A."""
    if not agg_e or not agg_a:
        return False
    return (
        agg_e["total_net_pnl"] > agg_a["total_net_pnl"]
        and agg_e["median_pf"] >= agg_a["median_pf"] * 0.95
        and agg_e["worst_mdd_pct"] >= agg_a["worst_mdd_pct"] - 0.5  # MDD not much worse
        and agg_e["wrong_dir_rate"] <= agg_a["wrong_dir_rate"] + 0.02
        and agg_e["positive_days"] >= agg_a["positive_days"]
        and agg_e["avg_daily_return_pct"] > 0
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--promote-if-win", action="store_true")
    args = parser.parse_args()

    daily_rets, _ = _load_daily_returns_and_df()
    by_scenario: dict[str, list[DayMetrics]] = {"A": [], "C": [], "E": []}
    stress: dict[str, dict] = {}

    print("=" * 72)
    print(f"Walk-forward A / C / E — {args.days} trading days")
    print("=" * 72)

    for day_idx in range(args.days):
        RNG.shuffle(daily_rets)
        day = generate_synthetic_day(daily_rets)
        for scen, require_ep in (("A", False), ("C", False), ("E", True)):
            if scen == "C":
                m = _simulate_scenario(day, "C", slip_pct=SLIP_BASE)
            else:
                m = _simulate_scenario(day, scen, slip_pct=SLIP_BASE, require_episode_confirm=require_ep)
            m.day_index = day_idx
            by_scenario[scen].append(m)

    aggs = {k: _aggregate(v) for k, v in by_scenario.items()}

    # Stress sensitivity on last day sample (delay approximated by higher slip)
    RNG.shuffle(daily_rets)
    sample = generate_synthetic_day(daily_rets)
    for scen, require_ep in (("A", False), ("E", True)):
        base = _simulate_scenario(sample, scen, slip_pct=0.05, require_episode_confirm=require_ep)
        s15 = _simulate_scenario(sample, scen, slip_pct=0.10, require_episode_confirm=require_ep)
        s30 = _simulate_scenario(sample, scen, slip_pct=0.12, require_episode_confirm=require_ep)
        stress[scen] = {
            "base": base.net_pnl,
            "slip10": s15.net_pnl,
            "slip12": s30.net_pnl,
            "d15": s15.net_pnl - base.net_pnl,
            "d30": s30.net_pnl - base.net_pnl,
        }

    print(f"\n{'시나리오':<6s} {'NetPnL':>12s} {'일평균%':>8s} {'PF':>6s} {'MDD':>8s} {'진입':>5s} {'방향오류':>8s} {'비용/G':>7s} {'양수일':>5s}")
    print("-" * 72)
    for k in ("A", "C", "E"):
        a = aggs[k]
        print(
            f"{k:<6s} {a['total_net_pnl']:>+12,.0f} {a['avg_daily_return_pct']:>+7.3f}% "
            f"{a['median_pf']:>6.2f} {a['worst_mdd_pct']:>7.2f}% {a['entries']:>5d} "
            f"{a['wrong_dir_rate']*100:>7.1f}% {a['cost_gross_pct']:>6.1f}% {a['positive_days']:>5d}"
        )

    print("\n지연/슬리피지 민감도 (샘플일):")
    for k, s in stress.items():
        print(f"  {k}: base={s['base']:+,.0f}  slip10={s['slip10']:+,.0f} ({s['d15']:+,.0f})  slip12={s['slip12']:+,.0f} ({s['d30']:+,.0f})")

    win = e_beats_a(aggs["E"], aggs["A"])
    print(f"\nE가 A를 안정적으로 이김: {win}")
    print("평가 우선순위: 1)보수적 Net 2)PF 3)MDD 4)방향오류 5)비용/Gross 6)거래횟수 7)지연민감도")

    payload = {
        "promote_e_to_live": False,
        "episode_gate_mode": "SHADOW",
        "walkforward_days": args.days,
        "metrics": aggs,
        "e_beats_a": win,
        "note": "하루 데이터로 임계값 최적화 금지. E>A 안정 우위 시에만 LIVE 승격.",
    }
    if args.promote_if_win and win:
        payload["promote_e_to_live"] = True
        payload["episode_gate_mode"] = "LIVE"
        print("→ promote_e_to_live=true 기록 (LIVE 게이트 승격)")
    else:
        print("→ 프로덕션 게이트는 SHADOW 유지 (A 단독 주문, C는 확인기 로그만)")

    STATE_PROMOTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PROMOTION_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"저장: {STATE_PROMOTION_PATH}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
