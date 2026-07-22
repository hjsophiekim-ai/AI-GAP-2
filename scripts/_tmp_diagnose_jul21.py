"""Diagnose why afternoon DOWN/rebound entries are missing on Jul21 replay."""
from __future__ import annotations

import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from app.services import hynix_switch_engine as engine
from app.trading import early_trend_detector as etd
from app.trading import early_trend_live_feed as feed
from app.trading.etf_entry_confirmation import (
    compute_etf_breakouts,
    compute_etf_vwap,
    is_swing_structure_broken_against,
    resolve_window_directions,
    trade_aligned_window_directions,
)
from app.trading.hynix_fast_trend import compute_fast_trend_signal
from app.trading.hynix_symbols import LONG_SYMBOL, SHORT_SYMBOL as INVERSE_SYMBOL, SIGNAL_SYMBOL
from app.trading.hynix_switch_risk_gate import is_new_entry_allowed
from app.trading.range_weighted_optimize import classify_intraday_regime, get_range_weighted_config, load_optimized_config
from scripts._tmp_replay_jul21_shaped import apply_hynix_shape, fetch_naver_closes, fetch_yf_hynix
from scripts.replay_today_weighted_range import _enhanced_decision, _price_at, _slice_to


def main() -> int:
    load_optimized_config()
    hynix = fetch_yf_hynix()
    naver_h = fetch_naver_closes("000660")
    extra = naver_h[~naver_h["datetime"].isin(set(hynix["datetime"]))].copy()
    if len(extra):
        extra["open"] = extra["close"]
        extra["high"] = extra["close"]
        extra["low"] = extra["close"]
        hynix = pd.concat([hynix, extra], ignore_index=True).sort_values("datetime").reset_index(drop=True)
    long_df = apply_hynix_shape(fetch_naver_closes("0193T0"), hynix, 2.0)
    inv_df = apply_hynix_shape(fetch_naver_closes("0197X0"), hynix, -2.0)

    cfg = get_range_weighted_config()
    counts: Counter = Counter()
    history: dict = {}
    continuation: dict = {}
    start = max(hynix["datetime"].min(), long_df["datetime"].min(), inv_df["datetime"].min())
    start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = min(hynix["datetime"].max(), long_df["datetime"].max(), inv_df["datetime"].max()).replace(second=0, microsecond=0)
    ts = start
    while ts <= end:
        if not is_new_entry_allowed(ts):
            ts += timedelta(seconds=5)
            continue
        sp = _price_at(hynix, ts)
        lp = _price_at(long_df, ts)
        ip = _price_at(inv_df, ts)
        if sp is None or lp is None or ip is None:
            ts += timedelta(seconds=5)
            continue
        history = feed.record_price_sample(history, SIGNAL_SYMBOL, sp, ts)
        history = feed.record_price_sample(history, LONG_SYMBOL, lp, ts)
        history = feed.record_price_sample(history, INVERSE_SYMBOL, ip, ts)
        live = feed.compute_live_trade_direction(
            history, ts, signal_symbol=SIGNAL_SYMBOL, long_symbol=LONG_SYMBOL, inverse_symbol=INVERSE_SYMBOL
        )
        live_dir = live.get("direction")
        if live_dir not in ("UP", "DOWN"):
            counts["no_live_dir"] += 1
            ts += timedelta(seconds=5)
            continue
        desired = LONG_SYMBOL if live_dir == "UP" else INVERSE_SYMBOL
        px = lp if desired == LONG_SYMBOL else ip
        etf_slice = _slice_to(long_df if desired == LONG_SYMBOL else inv_df, ts)
        confirm_dirs = trade_aligned_window_directions(
            resolve_window_directions(feed.compute_live_direction(history, desired, ts)),
            symbol=desired,
        )
        oppose = INVERSE_SYMBOL if desired == LONG_SYMBOL else LONG_SYMBOL
        oppose_dirs = trade_aligned_window_directions(
            resolve_window_directions(feed.compute_live_direction(history, oppose, ts)),
            symbol=oppose,
        )
        signal_dirs = resolve_window_directions(feed.compute_live_direction(history, SIGNAL_SYMBOL, ts))
        vwap = compute_etf_vwap(etf_slice) if len(etf_slice) >= 3 else None
        confirm_above_vwap = bool(vwap is not None and px >= float(vwap))
        breakouts = compute_etf_breakouts(etf_slice, px, live_dir) if len(etf_slice) >= 3 else {}
        swing = bool(
            breakouts.get("recent_high") and px > float(breakouts["recent_high"])
            if live_dir == "UP"
            else breakouts.get("recent_low") and px < float(breakouts["recent_low"])
        )
        existing = continuation.get("direction")
        vwap_by = dict(continuation.get("prev_above_vwap_by_symbol") or {})
        prev_above = vwap_by.get(desired)
        vwap_reclaim = bool(
            confirm_above_vwap
            and prev_above is False
            and confirm_dirs.get(5) == live_dir
            and confirm_dirs.get(10) == live_dir
        )
        broken = False
        if existing and existing != live_dir:
            edf = long_df if existing == "UP" else inv_df
            ep = lp if existing == "UP" else ip
            es = _slice_to(edf, ts)
            if ep and len(es) >= 3:
                broken = is_swing_structure_broken_against(es, ep, existing)
        opp = engine.detect_opposite_episode_transition(
            existing_direction=existing,
            new_direction=live_dir,
            live_direction_matches=True,
            confirm_dirs=confirm_dirs,
            existing_structure_broken=broken,
            new_etf_vwap_reclaim=vwap_reclaim,
        )
        if existing and existing != live_dir:
            counts["dir_mismatch"] += 1
            if broken:
                counts["broken"] += 1
            if vwap_reclaim:
                counts["vwap_reclaim"] += 1
            if opp:
                counts["opp_ok"] += 1
            else:
                counts["opp_fail"] += 1
            if ts.hour >= 12 and ts.minute in (50, 51, 52, 58, 20, 21, 22) and ts.second == 0:
                print(
                    ts,
                    "live", live_dir,
                    "exist", existing,
                    "broken", broken,
                    "vwap_r", vwap_reclaim,
                    "5/10", confirm_dirs.get(5), confirm_dirs.get(10),
                    "opp", opp,
                    "above", confirm_above_vwap,
                    "prev", prev_above,
                )
        changed = False
        if continuation.get("direction") != live_dir and (not existing or opp):
            counts["episode_reset"] += 1
            changed = True
            engine.reset_range_episode_probe_state(
                continuation,
                now=ts,
                direction=live_dir,
                episode_id=f"{live_dir}:{ts.isoformat()}",
                reference_price=px,
            )
        continuation["prev_above_vwap"] = confirm_above_vwap
        vwap_by[desired] = confirm_above_vwap
        continuation["prev_above_vwap_by_symbol"] = vwap_by
        engine.update_range_episode_structural_events(
            continuation, now=ts, swing_breakout=swing, vwap_reclaim=vwap_reclaim
        )
        fast = compute_fast_trend_signal(_slice_to(hynix, ts), now=ts)
        returns = fast.get("returns") or {}
        expected_move = max(abs(float(returns.get(k) or 0.0)) for k in ("1m", "3m", "5m")) or 0.45
        cost_gate = etd.evaluate_cost_gate(desired, expected_move)
        decision = _enhanced_decision(_slice_to(hynix, ts), live_dir)
        entry = engine.evaluate_range_weighted_entry(
            decision=decision,
            direction=live_dir,
            live_direction=live_dir,
            signal_window_directions=signal_dirs,
            confirm_window_directions=confirm_dirs,
            oppose_window_directions=oppose_dirs,
            confirm_above_vwap=confirm_above_vwap,
            data_age_seconds=2.0,
            moved_pct_since_signal=None,
            expected_move_pct=expected_move,
            cost_pct=cost_gate.get("cost_pct"),
            expected_mfe_pct=expected_move,
            expected_mae_pct=abs(float(etd.FIXED_EARLY_STOP_PCT)),
            ema_slope_aligned=True,
            structure_confirmed=swing,
            structural_direction=fast.get("direction"),
            entry_path_hint=None,
            day_regime=classify_intraday_regime(hynix),
            range_config=cfg,
        )
        if entry.get("action") == "ENTER":
            counts["enter_signal"] += 1
            if existing and live_dir != existing and not opp:
                counts["enter_blocked_opp"] += 1
            else:
                allows, reason = engine.range_episode_allows_entry(
                    continuation,
                    entry_path=entry.get("entry_path"),
                    swing_breakout=swing,
                    vwap_reclaim=vwap_reclaim,
                    direction_changed=changed,
                )
                if not allows:
                    counts[f"block_{reason}"] += 1
                else:
                    counts["would_enter"] += 1
                    hm = ts.strftime("%H:%M")
                    if "12:45" <= hm <= "14:30":
                        print("AFTERNOON ENTER", ts, live_dir, entry.get("entry_path"), desired)
                        # pretend enter to unlock subsequent counters realistically
                        continuation["entry_done"] = True
                        continuation["entry_path"] = entry.get("entry_path")
                        engine.mark_range_reversal_probe_entered(
                            continuation, now=ts, entry_path=entry.get("entry_path")
                        )
        else:
            counts[f"skip_{entry.get('reason_code')}"] += 1
        ts += timedelta(seconds=5)

    print("COUNTERS")
    for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
