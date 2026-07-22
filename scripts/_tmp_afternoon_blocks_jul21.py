"""Focused afternoon-window block histogram for Jul 21 cache after raw-dir fix."""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import hynix_switch_engine as engine  # noqa: E402
from app.trading import early_trend_detector as etd  # noqa: E402
from app.trading import early_trend_live_feed as feed  # noqa: E402
from app.trading.etf_entry_confirmation import (  # noqa: E402
    compute_etf_breakouts,
    compute_etf_vwap,
    is_swing_structure_broken_against,
    resolve_window_directions,
    trade_aligned_window_directions,
)
from app.trading.hynix_fast_trend import compute_fast_trend_signal  # noqa: E402
from app.trading.hynix_symbols import (  # noqa: E402
    LONG_SYMBOL,
    SHORT_SYMBOL as INVERSE_SYMBOL,
    SIGNAL_SYMBOL,
)
from app.trading.range_weighted_optimize import (  # noqa: E402
    classify_intraday_regime,
    get_range_weighted_config,
    load_optimized_config,
)
from scripts.replay_today_weighted_range import (  # noqa: E402
    _enhanced_decision,
    _price_at,
    _slice_to,
)


def main() -> int:
    load_optimized_config()
    cache = ROOT / "data" / "cache"
    hynix_1m = pd.read_csv(cache / "replay_20260721_hynix_1m.csv", parse_dates=["datetime"])
    long_1m = pd.read_csv(cache / "replay_20260721_long_1m.csv", parse_dates=["datetime"])
    inverse_1m = pd.read_csv(cache / "replay_20260721_inverse_1m.csv", parse_dates=["datetime"])
    day_regime = classify_intraday_regime(hynix_1m)
    cfg = get_range_weighted_config()
    print(f"day_regime={day_regime}")

    history: dict = {}
    continuation = engine.init_range_episode_probe_state() if hasattr(engine, "init_range_episode_probe_state") else {}
    if not continuation:
        continuation = {
            "direction": None,
            "direction_episode_id": None,
            "reference_price": None,
            "prev_above_vwap_by_symbol": {},
        }

    start = datetime(2026, 7, 21, 13, 20, 0)
    end = datetime(2026, 7, 21, 14, 30, 0)
    # Warm history from morning so slopes exist
    warm = datetime(2026, 7, 21, 12, 30, 0)
    ts = warm
    blocks = Counter()
    live_hits = 0
    enter_hits = 0
    opp_ok = 0
    samples = []

    while ts <= end:
        sp = _price_at(hynix_1m, ts)
        lp = _price_at(long_1m, ts)
        ip = _price_at(inverse_1m, ts)
        if sp is None or lp is None or ip is None:
            ts += timedelta(seconds=5)
            continue
        history = feed.record_price_sample(history, SIGNAL_SYMBOL, sp, ts)
        history = feed.record_price_sample(history, LONG_SYMBOL, lp, ts)
        history = feed.record_price_sample(history, INVERSE_SYMBOL, ip, ts)
        if ts < start:
            ts += timedelta(seconds=5)
            continue

        live_trade = feed.compute_live_trade_direction(
            history, ts, signal_symbol=SIGNAL_SYMBOL, long_symbol=LONG_SYMBOL, inverse_symbol=INVERSE_SYMBOL,
        )
        live_dir = live_trade.get("direction")
        if live_dir not in ("UP", "DOWN"):
            ts += timedelta(seconds=5)
            continue
        live_hits += 1
        desired_symbol = LONG_SYMBOL if live_dir == "UP" else INVERSE_SYMBOL
        current_etf_price = lp if desired_symbol == LONG_SYMBOL else ip
        etf_slice = _slice_to(long_1m if desired_symbol == LONG_SYMBOL else inverse_1m, ts)
        h_slice = _slice_to(hynix_1m, ts)

        confirm_dirs_raw = resolve_window_directions(feed.compute_live_direction(history, desired_symbol, ts))
        confirm_dirs = trade_aligned_window_directions(confirm_dirs_raw, symbol=desired_symbol)
        oppose_symbol = INVERSE_SYMBOL if desired_symbol == LONG_SYMBOL else LONG_SYMBOL
        oppose_dirs_raw = resolve_window_directions(feed.compute_live_direction(history, oppose_symbol, ts))
        signal_dirs = resolve_window_directions(feed.compute_live_direction(history, SIGNAL_SYMBOL, ts))

        vwap = compute_etf_vwap(etf_slice) if len(etf_slice) >= 3 else None
        confirm_above_vwap = bool(vwap is not None and current_etf_price >= float(vwap))
        breakouts = compute_etf_breakouts(etf_slice, current_etf_price, live_dir) if len(etf_slice) >= 3 else {}
        swing_breakout = bool(
            breakouts.get("recent_high") and current_etf_price > float(breakouts["recent_high"])
            if live_dir == "UP"
            else breakouts.get("recent_low") and current_etf_price < float(breakouts["recent_low"])
        )
        returns = (compute_fast_trend_signal(h_slice, now=ts).get("returns") or {})
        expected_move = max(abs(float(returns.get(k) or 0.0)) for k in ("1m", "3m", "5m")) or 0.45
        cost_gate = etd.evaluate_cost_gate(desired_symbol, expected_move)
        decision = _enhanced_decision(h_slice, live_dir)

        existing = continuation.get("direction")
        vwap_by = dict(continuation.get("prev_above_vwap_by_symbol") or {})
        prev_above = vwap_by.get(desired_symbol)
        vwap_reclaim = bool(
            confirm_above_vwap and prev_above is False
            and confirm_dirs.get(5) == live_dir and confirm_dirs.get(10) == live_dir
        )
        existing_broken = False
        if existing and existing != live_dir:
            edf = long_1m if existing == "UP" else inverse_1m
            eprice = lp if existing == "UP" else ip
            eslice = _slice_to(edf, ts)
            if eprice and len(eslice) >= 3:
                existing_broken = is_swing_structure_broken_against(eslice, eprice, existing)
        opp = engine.detect_opposite_episode_transition(
            existing_direction=existing,
            new_direction=live_dir,
            live_direction_matches=True,
            confirm_dirs=confirm_dirs,
            existing_structure_broken=existing_broken,
            new_etf_vwap_reclaim=vwap_reclaim,
            new_etf_vwap_break=confirm_above_vwap,
            new_swing_breakout=swing_breakout,
        )
        if existing and existing != live_dir and opp:
            opp_ok += 1
            engine.reset_range_episode_probe_state(
                continuation, now=ts, direction=live_dir,
                episode_id=f"{live_dir}:{ts.isoformat()}", reference_price=current_etf_price,
            )
        elif not existing:
            engine.reset_range_episode_probe_state(
                continuation, now=ts, direction=live_dir,
                episode_id=f"{live_dir}:{ts.isoformat()}", reference_price=current_etf_price,
            )
        continuation["prev_above_vwap"] = confirm_above_vwap
        vwap_by[desired_symbol] = confirm_above_vwap
        continuation["prev_above_vwap_by_symbol"] = vwap_by

        moved_pct = None
        if continuation.get("reference_price"):
            moved_pct = abs(current_etf_price / float(continuation["reference_price"]) - 1.0) * 100.0

        entry_eval = engine.evaluate_range_weighted_entry(
            decision=decision,
            direction=live_dir,
            live_direction=live_dir,
            signal_window_directions=signal_dirs,
            confirm_window_directions=confirm_dirs_raw,
            oppose_window_directions=oppose_dirs_raw,
            confirm_above_vwap=confirm_above_vwap,
            data_age_seconds=2.0,
            moved_pct_since_signal=moved_pct,
            expected_move_pct=expected_move,
            cost_pct=cost_gate.get("cost_pct"),
            expected_mfe_pct=expected_move,
            expected_mae_pct=abs(float(etd.FIXED_EARLY_STOP_PCT)),
            ema_slope_aligned=True,
            structure_confirmed=swing_breakout,
            day_regime=day_regime,
            range_config=cfg,
        )
        reason = entry_eval.get("reason_code") or entry_eval.get("action")
        if entry_eval.get("action") == "ENTER":
            enter_hits += 1
            if existing and existing != live_dir and not opp:
                blocks["ENTER_BUT_OPPOSITE_BLOCKED"] += 1
            else:
                blocks["ENTER_OK"] += 1
                if len(samples) < 8:
                    samples.append({
                        "ts": ts.strftime("%H:%M:%S"),
                        "live": live_dir,
                        "path": entry_eval.get("entry_path"),
                        "raw5_10": f"{confirm_dirs_raw.get(5)}/{confirm_dirs_raw.get(10)}",
                        "aligned5_10": f"{confirm_dirs.get(5)}/{confirm_dirs.get(10)}",
                        "swing": swing_breakout,
                        "above_vwap": confirm_above_vwap,
                        "opp": opp,
                        "existing": existing,
                        "evidence": entry_eval.get("evidence_score"),
                    })
        else:
            blocks[str(reason)] += 1
            if live_dir == "DOWN" and len(samples) < 12 and blocks[str(reason)] <= 2:
                samples.append({
                    "ts": ts.strftime("%H:%M:%S"),
                    "live": live_dir,
                    "reason": reason,
                    "raw5_10": f"{confirm_dirs_raw.get(5)}/{confirm_dirs_raw.get(10)}",
                    "aligned5_10": f"{confirm_dirs.get(5)}/{confirm_dirs.get(10)}",
                    "hard": entry_eval.get("hard_blocks"),
                    "swing": swing_breakout,
                    "above_vwap": confirm_above_vwap,
                    "opp": opp,
                    "existing": existing,
                    "moved": moved_pct,
                })

        ts += timedelta(seconds=5)

    print(f"live_hits={live_hits} enter_hits={enter_hits} opp_ok={opp_ok}")
    print("block_histogram:")
    for k, v in blocks.most_common():
        print(f"  {k}: {v}")
    print("samples:")
    for s in samples:
        print(f"  {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
