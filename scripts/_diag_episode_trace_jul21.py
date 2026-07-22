"""READ-ONLY diagnostic: minute dump of weighted RANGE episode state on 2026-07-21.

Does not change production thresholds. Uses the same episode helpers as
`scripts/replay_today_weighted_range.py` / `run_early_trend_fast_feed_tick`.

Data: Naver fchart closes + yfinance Hynix OHLC shape (same as sibling validation
that produced the 2-trade AFTER result). Caches CSVs under data/cache/.

Usage:
    python scripts/_diag_episode_trace_jul21.py
"""
from __future__ import annotations

import csv
import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

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
from app.trading.hynix_switch_risk_gate import is_new_entry_allowed  # noqa: E402
from app.trading.range_weighted_optimize import (  # noqa: E402
    classify_intraday_regime,
    daily_loss_limit_reached,
    get_range_weighted_config,
    load_optimized_config,
)
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402
from scripts._tmp_replay_jul21_shaped import (  # noqa: E402
    apply_hynix_shape,
    fetch_naver_closes,
    fetch_yf_hynix,
)
from scripts.replay_today_weighted_range import (  # noqa: E402
    INITIAL_CASH,
    _conservative_fill_price,
    _enhanced_decision,
    _price_at,
    _slice_to,
    run_replay,
)

DUMP_START = "12:30"
DUMP_END = "14:40"
CACHE_DIR = ROOT / "data" / "cache"
OUT_DIR = ROOT / "data" / "state"
CSV_PATH = OUT_DIR / "jul21_episode_minute_dump.csv"
JSON_PATH = OUT_DIR / "jul21_episode_trace_summary.json"


def _load_or_build_bars() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    h_path = CACHE_DIR / "replay_20260721_hynix_1m.csv"
    l_path = CACHE_DIR / "replay_20260721_long_1m.csv"
    i_path = CACHE_DIR / "replay_20260721_inverse_1m.csv"
    if h_path.exists() and l_path.exists() and i_path.exists():
        h = pd.read_csv(h_path, parse_dates=["datetime"])
        l = pd.read_csv(l_path, parse_dates=["datetime"])
        i = pd.read_csv(i_path, parse_dates=["datetime"])
        if min(len(h), len(l), len(i)) >= 100:
            return h, l, i, "cache"

    print("Building Jul21 bars (Naver closes + yfinance Hynix OHLC shape)...")
    hynix = fetch_yf_hynix()
    naver_h = fetch_naver_closes("000660")
    extra = naver_h[~naver_h["datetime"].isin(set(hynix["datetime"]))].copy()
    if len(extra):
        extra["open"] = extra["close"]
        extra["high"] = extra["close"]
        extra["low"] = extra["close"]
        hynix = (
            pd.concat([hynix, extra], ignore_index=True)
            .sort_values("datetime")
            .reset_index(drop=True)
        )
    long_df = apply_hynix_shape(fetch_naver_closes("0193T0"), hynix, 2.0)
    inv_df = apply_hynix_shape(fetch_naver_closes("0197X0"), hynix, -2.0)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    hynix.to_csv(h_path, index=False)
    long_df.to_csv(l_path, index=False)
    inv_df.to_csv(i_path, index=False)
    return hynix, long_df, inv_df, "fresh"


def _old_detect_opposite_episode_transition(**kwargs) -> bool:
    """HEAD semantics before uncommitted OR fix: 5/10 required on BOTH paths."""
    existing_direction = kwargs.get("existing_direction")
    new_direction = kwargs.get("new_direction")
    live_direction_matches = kwargs.get("live_direction_matches")
    confirm_dirs = kwargs.get("confirm_dirs") or {}
    if not existing_direction:
        return True
    if existing_direction == new_direction:
        return False
    if not live_direction_matches:
        return False
    if confirm_dirs.get(5) != new_direction or confirm_dirs.get(10) != new_direction:
        return False
    return bool(kwargs.get("existing_structure_broken") or kwargs.get("new_etf_vwap_reclaim"))


def _old_range_episode_allows_entry(
    continuation_state: dict,
    *,
    entry_path: str | None,
    swing_breakout: bool,
    vwap_reclaim: bool,
    direction_changed: bool,
) -> tuple[bool, str | None]:
    """HEAD semantics before PROBE_FAILED CONTINUATION unlock."""
    if direction_changed:
        return True, None
    structural_unlock = swing_breakout or vwap_reclaim
    if continuation_state.get("episode_status") == "PROBE_FAILED" and entry_path == "REVERSAL":
        return False, "PROBE_FAILED_REVERSAL_BLOCKED"
    if entry_path == "REVERSAL" and continuation_state.get("reversal_probe_done"):
        return False, "REVERSAL_PROBE_ONCE_PER_EPISODE"
    if continuation_state.get("awaiting_structural_reentry") and not structural_unlock:
        return False, "AWAITING_STRUCTURAL_REENTRY"
    if continuation_state.get("entry_done"):
        return False, "ENTRY_DONE_FOR_EPISODE"
    return True, None


def _in_dump_window(ts: datetime) -> bool:
    hm = ts.strftime("%H:%M")
    return DUMP_START <= hm <= DUMP_END


def run_instrumented_replay(
    hynix_1m: pd.DataFrame,
    long_1m: pd.DataFrame,
    inverse_1m: pd.DataFrame,
    *,
    label: str,
    dump_minutes: bool = True,
) -> dict:
    """Mirror run_replay with per-minute (and key 5s) instrumentation."""
    day_regime = classify_intraday_regime(hynix_1m)
    cfg = get_range_weighted_config()
    cost_engine = TradeCostEngine()
    cash = INITIAL_CASH
    cash_conservative = INITIAL_CASH
    peak_equity = INITIAL_CASH
    peak_equity_conservative = INITIAL_CASH
    realized_pnl = 0.0
    realized_pnl_conservative = 0.0
    daily_loss_breached = False
    position = None
    position_conservative = None
    history: dict = {}
    continuation: dict = {}
    episode_entries: set[str] = set()
    trades: list[dict] = []
    events: list[dict] = []
    minute_rows: list[dict] = []
    transition_log: list[dict] = []
    block_log: list[dict] = []

    start = max(hynix_1m["datetime"].min(), long_1m["datetime"].min(), inverse_1m["datetime"].min())
    end = min(hynix_1m["datetime"].max(), long_1m["datetime"].max(), inverse_1m["datetime"].max())
    start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = end.replace(second=0, microsecond=0)

    ts = start
    last_dumped_minute = None
    while ts <= end:
        if not is_new_entry_allowed(ts):
            ts += timedelta(seconds=5)
            continue

        sp = _price_at(hynix_1m, ts)
        lp = _price_at(long_1m, ts)
        ip = _price_at(inverse_1m, ts)
        if sp is None or lp is None or ip is None:
            ts += timedelta(seconds=5)
            continue

        history = feed.record_price_sample(history, SIGNAL_SYMBOL, sp, ts)
        history = feed.record_price_sample(history, LONG_SYMBOL, lp, ts)
        history = feed.record_price_sample(history, INVERSE_SYMBOL, ip, ts)

        live_trade = feed.compute_live_trade_direction(
            history, ts,
            signal_symbol=SIGNAL_SYMBOL, long_symbol=LONG_SYMBOL, inverse_symbol=INVERSE_SYMBOL,
        )
        live_dir = live_trade.get("direction")
        if live_dir not in ("UP", "DOWN"):
            ts += timedelta(seconds=5)
            continue

        desired_symbol = LONG_SYMBOL if live_dir == "UP" else INVERSE_SYMBOL
        current_etf_price = lp if desired_symbol == LONG_SYMBOL else ip
        h_slice = _slice_to(hynix_1m, ts)
        etf_slice = _slice_to(long_1m if desired_symbol == LONG_SYMBOL else inverse_1m, ts)

        # Match production: episode helpers use trade-aligned; evaluator uses raw.
        confirm_dirs_raw = resolve_window_directions(
            feed.compute_live_direction(history, desired_symbol, ts)
        )
        confirm_dirs = trade_aligned_window_directions(confirm_dirs_raw, symbol=desired_symbol)
        oppose_symbol = INVERSE_SYMBOL if desired_symbol == LONG_SYMBOL else LONG_SYMBOL
        oppose_dirs_raw = resolve_window_directions(
            feed.compute_live_direction(history, oppose_symbol, ts)
        )
        oppose_dirs = trade_aligned_window_directions(oppose_dirs_raw, symbol=oppose_symbol)
        signal_dirs = resolve_window_directions(feed.compute_live_direction(history, SIGNAL_SYMBOL, ts))

        # Target ETF VWAP slope proxy from recent closes
        vwap = compute_etf_vwap(etf_slice) if len(etf_slice) >= 3 else None
        confirm_above_vwap = bool(vwap is not None and current_etf_price >= float(vwap))
        vwap_slope = None
        if vwap is not None and len(etf_slice) >= 6:
            prev_slice = etf_slice.iloc[:-3]
            prev_vwap = compute_etf_vwap(prev_slice) if len(prev_slice) >= 3 else None
            if prev_vwap:
                vwap_slope = float(vwap) - float(prev_vwap)

        breakouts = compute_etf_breakouts(etf_slice, current_etf_price, live_dir) if len(etf_slice) >= 3 else {}
        swing_breakout = bool(
            breakouts.get("recent_high") and current_etf_price > float(breakouts["recent_high"])
            if live_dir == "UP"
            else breakouts.get("recent_low") and current_etf_price < float(breakouts["recent_low"])
        )

        fast = compute_fast_trend_signal(h_slice, now=ts)
        returns = fast.get("returns") or {}
        expected_move = max(abs(float(returns.get(k) or 0.0)) for k in ("1m", "3m", "5m")) or 0.45
        cost_gate = etd.evaluate_cost_gate(desired_symbol, expected_move)
        decision = _enhanced_decision(h_slice, live_dir)

        reversal_hint = None
        pa_dirs = resolve_window_directions(feed.compute_live_direction(history, desired_symbol, ts))
        pa_opp = resolve_window_directions(feed.compute_live_direction(history, oppose_symbol, ts))
        pa_slopes = feed.compute_live_direction(history, desired_symbol, ts).get("slopes") or {}
        accel = all(float(pa_slopes.get(w) or 0.0) > 0 for w in (5, 10, 20))
        factors = {
            "slope_5s_10s_reversal": pa_dirs.get(5) == live_dir and pa_dirs.get(10) == live_dir,
            "vwap_reclaim_with_slope": confirm_above_vwap and (pa_dirs.get(5) == "UP" or pa_dirs.get(10) == "UP"),
            "swing_high_low_breakout": swing_breakout,
            "acceleration_5_10_20_strengthening": accel,
            "etf_mutual_direction_confirmed": (
                pa_dirs.get(5) == "UP" and pa_dirs.get(10) == "UP"
                and pa_opp.get(5) == "DOWN" and pa_opp.get(10) == "DOWN"
            ),
        }
        if sum(1 for ok in factors.values() if ok) >= 3:
            reversal_hint = "REVERSAL"

        macd_conf = engine._macd_williams_confirmation(etf_slice, live_dir)

        _existing_episode_direction = continuation.get("direction")
        _vwap_by_symbol = dict(continuation.get("prev_above_vwap_by_symbol") or {})
        _prev_above_vwap = _vwap_by_symbol.get(desired_symbol)
        vwap_reclaim = bool(
            confirm_above_vwap
            and _prev_above_vwap is False
            and confirm_dirs.get(5) == live_dir
            and confirm_dirs.get(10) == live_dir
        )
        _existing_structure_broken = False
        if _existing_episode_direction and _existing_episode_direction != live_dir:
            existing_df = long_1m if _existing_episode_direction == "UP" else inverse_1m
            existing_price = lp if _existing_episode_direction == "UP" else ip
            existing_slice = _slice_to(existing_df, ts)
            if existing_price and len(existing_slice) >= 3:
                existing_symbol = LONG_SYMBOL if _existing_episode_direction == "UP" else INVERSE_SYMBOL
                structure_dir = "UP" if existing_symbol == INVERSE_SYMBOL else _existing_episode_direction
                _existing_structure_broken = is_swing_structure_broken_against(
                    existing_slice, existing_price, structure_dir,
                )
        _opposite_episode_confirmed = engine.detect_opposite_episode_transition(
            existing_direction=_existing_episode_direction,
            new_direction=live_dir,
            live_direction_matches=True,
            confirm_dirs=confirm_dirs,
            existing_structure_broken=_existing_structure_broken,
            new_etf_vwap_reclaim=vwap_reclaim,
            new_etf_vwap_break=confirm_above_vwap,
            new_swing_breakout=swing_breakout,
        )
        direction_episode_changed = False
        episode_change_reason = ""
        if continuation.get("direction") != live_dir and (
            not _existing_episode_direction or _opposite_episode_confirmed
        ):
            direction_episode_changed = True
            episode_change_reason = (
                "NEW_EPISODE"
                if not _existing_episode_direction
                else (
                    "OPPOSITE_SWING_BREAK"
                    if _existing_structure_broken
                    else "OPPOSITE_VWAP_RECLAIM_5_10"
                )
            )
            engine.reset_range_episode_probe_state(
                continuation,
                now=ts,
                direction=live_dir,
                episode_id=f"{live_dir}:{ts.isoformat()}",
                reference_price=current_etf_price,
            )
            transition_log.append({
                "time": ts.isoformat(),
                "from": _existing_episode_direction,
                "to": live_dir,
                "reason": episode_change_reason,
                "broken": _existing_structure_broken,
                "vwap_reclaim": vwap_reclaim,
                "dirs_5_10": f"{confirm_dirs.get(5)}/{confirm_dirs.get(10)}",
            })
        elif (
            _existing_episode_direction
            and _existing_episode_direction != live_dir
            and not _opposite_episode_confirmed
        ):
            episode_change_reason = "OPPOSITE_CANDIDATE_BLOCKED"

        continuation["prev_above_vwap"] = confirm_above_vwap
        _vwap_by_symbol[desired_symbol] = confirm_above_vwap
        continuation["prev_above_vwap_by_symbol"] = _vwap_by_symbol
        engine.update_range_episode_structural_events(
            continuation,
            now=ts,
            swing_breakout=swing_breakout,
            vwap_reclaim=vwap_reclaim,
        )

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
            structural_direction=compute_fast_trend_signal(h_slice, now=ts).get("direction"),
            entry_path_hint=reversal_hint,
            day_regime=day_regime,
            range_config=cfg,
        )

        order_placed = "N"
        final_block = ""
        continuation_allowed = None
        episode_block = None

        # ── exits (same as run_replay) ──
        if position is not None:
            held_price = lp if position["symbol"] == LONG_SYMBOL else ip
            held_df = long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m
            held_slice = _slice_to(held_df, ts)
            net_ret = (held_price / position["entry_price"] - 1.0) * 100.0
            position["peak_net"] = max(position.get("peak_net", 0.0), net_ret)
            held_dirs = trade_aligned_window_directions(
                resolve_window_directions(feed.compute_live_direction(history, position["symbol"], ts)),
                symbol=position["symbol"],
            )
            structure_broken = is_swing_structure_broken_against(
                held_slice, held_price, position["direction"]
            )
            etf_aligned = held_dirs.get(5) == position["direction"] and held_dirs.get(10) == position["direction"]
            h_fast = compute_fast_trend_signal(h_slice, now=ts)
            regime_reversal = (
                h_fast.get("direction") in ("UP", "DOWN")
                and h_fast.get("direction") != position["direction"]
                and held_dirs.get(5) != position["direction"]
                and held_dirs.get(10) != position["direction"]
            )
            _is_reversal_probe = (
                continuation.get("entry_path") == "REVERSAL"
                and not continuation.get("scale_in_done")
                and not continuation.get("probe_promoted_at")
            )
            if _is_reversal_probe:
                exit_plan = engine.evaluate_weighted_range_probe_exit(
                    continuation=continuation,
                    probe_direction=position["direction"],
                    structure_reversal_confirmed=structure_broken,
                    held_window_dirs=held_dirs,
                    macd_confirmed=bool(macd_conf.get("confirmed")),
                    etf_direction_aligned=etf_aligned,
                    now=ts,
                    net_return_pct=net_ret,
                    hard_stop_pct=float(etd.FIXED_EARLY_STOP_PCT),
                )
                if exit_plan.get("action") == "PROMOTE_CONTINUATION":
                    engine.promote_reversal_probe_to_continuation(continuation, now=ts)
                    position["entry_path"] = "CONTINUATION"
                    continuation["entry_path"] = "CONTINUATION"
                    exit_plan = {"action": "HOLD", "ratio": 0.0, "reason": exit_plan.get("reason")}
            elif continuation.get("entry_path") == "CONTINUATION" or continuation.get("probe_promoted_at"):
                exit_plan = engine.evaluate_weighted_continuation_exit(
                    net_return_pct=net_ret,
                    hard_stop_pct=float(etd.FIXED_EARLY_STOP_PCT),
                    structure_reversal_confirmed=structure_broken,
                    regime_reversal_confirmed=regime_reversal,
                    held_window_dirs=held_dirs,
                    position_direction=position["direction"],
                    tp1_taken=bool(position.get("tp1_taken")),
                    tp2_taken=bool(position.get("tp2_taken")),
                    confirmed_regime=etd.REGIME_FAST_REVERSAL_RANGE,
                )
            else:
                held_rev = {w: held_dirs.get(w) == "DOWN" for w in (5, 10, 20, 30)}
                opposite_cp = live_dir != position["direction"]
                exit_plan = etd.should_exit_probe(
                    net_return_pct=net_ret,
                    seconds_since_last_reconfirmation=5.0,
                    signal_still_valid=live_dir == position["direction"],
                    opposite_change_point=opposite_cp,
                    confirmed_regime=etd.REGIME_FAST_REVERSAL_RANGE,
                    opposite_live_seconds=10.0 if opposite_cp else 0.0,
                    position_direction=position["direction"],
                    held_etf_reversal_windows=held_rev,
                    opposite_etf_5s10s_confirmed=oppose_dirs.get(5) == "UP" and oppose_dirs.get(10) == "UP",
                    structure_reversal_confirmed=structure_broken,
                    peak_net_return_pct=position.get("peak_net", 0.0),
                )

            if exit_plan["action"] in ("SELL_ALL", "SELL_PARTIAL"):
                if exit_plan["action"] == "SELL_PARTIAL":
                    if "TP2" in str(exit_plan.get("reason") or ""):
                        position["tp2_taken"] = True
                    else:
                        position["tp1_taken"] = True
                sell_qty = position["qty"] if exit_plan["action"] == "SELL_ALL" else max(1, int(position["qty"] * exit_plan["ratio"]))
                gross = (held_price - position["entry_price"]) * sell_qty
                cost = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * sell_qty
                net = gross - cost
                cash += sell_qty * held_price
                realized_pnl += net
                events.append({
                    "time": ts.strftime("%H:%M:%S"),
                    "action": "매도",
                    "symbol": "레버리지" if position["symbol"] == LONG_SYMBOL else "인버스",
                    "price": held_price,
                    "qty": sell_qty,
                    "reason": exit_plan.get("reason"),
                    "net_ret_pct": round(net_ret, 3),
                })
                trades.append({
                    "side": "SELL", "symbol": position["symbol"], "time": ts,
                    "price": held_price, "qty": sell_qty, "net_pnl": net,
                    "reason": exit_plan.get("reason"),
                })
                position["qty"] -= sell_qty
                if position["qty"] <= 0:
                    position = None
                    exit_reason = str(exit_plan.get("reason") or "")
                    if continuation.get("entry_path") == "REVERSAL":
                        engine.mark_range_probe_exit(
                            continuation,
                            now=ts,
                            entry_path=continuation.get("entry_path"),
                            reason=exit_reason,
                            probe_failed=bool(exit_plan.get("probe_failed")),
                        )
                    else:
                        engine.mark_range_episode_exit_awaiting_structure(
                            continuation, now=ts, reason=exit_reason,
                        )
            peak_equity = max(peak_equity, cash)
            if daily_loss_limit_reached(realized_pnl_conservative, INITIAL_CASH, cfg):
                daily_loss_breached = True

        # ── entry ──
        if position is None and entry_eval.get("action") == "ENTER":
            if daily_loss_breached or daily_loss_limit_reached(realized_pnl_conservative, INITIAL_CASH, cfg):
                final_block = "DAILY_LOSS_LIMIT"
            elif (
                continuation.get("direction")
                and live_dir != continuation.get("direction")
                and not _opposite_episode_confirmed
            ):
                final_block = "OPPOSITE_EPISODE_NOT_CONFIRMED"
            else:
                ep_id = continuation.get("direction_episode_id") or f"{live_dir}:{ts.isoformat()}"
                entry_path = entry_eval.get("entry_path")
                allows, episode_block = engine.range_episode_allows_entry(
                    continuation,
                    entry_path=entry_path,
                    swing_breakout=swing_breakout,
                    vwap_reclaim=vwap_reclaim,
                    direction_changed=direction_episode_changed,
                )
                continuation_allowed = allows
                if not allows:
                    final_block = episode_block or "EPISODE_BLOCK"
                    block_log.append({
                        "time": ts.isoformat(),
                        "live_dir": live_dir,
                        "entry_path": entry_path,
                        "block": final_block,
                        "episode_id": ep_id,
                        "episode_status": continuation.get("episode_status"),
                        "entry_done": continuation.get("entry_done"),
                        "reversal_probe_done": continuation.get("reversal_probe_done"),
                        "swing": swing_breakout,
                        "vwap_reclaim": vwap_reclaim,
                        "dir_changed": direction_episode_changed,
                    })
                else:
                    episode_entries.add(ep_id)
                    target_pct = float(entry_eval.get("target_pct") or 0.25)
                    qty = max(1, int(cash * target_pct / current_etf_price))
                    if qty * current_etf_price <= cash:
                        cash -= qty * current_etf_price
                        position = {
                            "symbol": desired_symbol,
                            "direction": live_dir,
                            "qty": qty,
                            "entry_price": current_etf_price,
                            "entry_time": ts,
                            "peak_net": 0.0,
                            "entry_path": entry_path,
                        }
                        continuation["entry_done"] = True
                        continuation["entry_path"] = entry_path
                        continuation["macd_williams_confirmation"] = macd_conf
                        engine.mark_range_reversal_probe_entered(
                            continuation, now=ts, entry_path=entry_path
                        )
                        order_placed = "Y"
                        events.append({
                            "time": ts.strftime("%H:%M:%S"),
                            "action": "매수",
                            "symbol": "레버리지" if desired_symbol == LONG_SYMBOL else "인버스",
                            "price": current_etf_price,
                            "qty": qty,
                            "pct": round(target_pct * 100, 1),
                            "path": entry_path,
                            "label": entry_eval.get("structural_signal_label"),
                            "evidence": entry_eval.get("evidence_score"),
                            "episode_id": ep_id,
                        })
                        cons_entry = _conservative_fill_price(
                            long_1m if desired_symbol == LONG_SYMBOL else inverse_1m, ts, "BUY"
                        )
                        if cons_entry:
                            cons_qty = max(1, int(cash_conservative * target_pct / cons_entry))
                            if cons_qty * cons_entry <= cash_conservative:
                                cash_conservative -= cons_qty * cons_entry
                                position_conservative = {
                                    "symbol": desired_symbol,
                                    "direction": live_dir,
                                    "qty": cons_qty,
                                    "entry_price": cons_entry,
                                    "entry_time": ts,
                                    "entry_path": entry_path,
                                }
                    else:
                        final_block = "INSUFFICIENT_CASH"
        elif entry_eval.get("action") != "ENTER":
            final_block = entry_eval.get("reason_code") or "NO_ENTER"
            if continuation_allowed is None and position is None:
                # still evaluate episode gate for diagnostics when ENTER would be interesting
                if entry_eval.get("reason_code") in (None,):
                    pass

        # scale-in omitted for dump brevity (same as run_replay when conditions met)
        elif (
            position is not None
            and position["symbol"] == desired_symbol
            and continuation.get("entry_done")
            and not continuation.get("scale_in_done")
            and macd_conf.get("confirmed")
        ):
            elapsed = (ts - datetime.fromisoformat(continuation["first_detected_at"])).total_seconds()
            if 10.0 <= elapsed <= 20.0:
                target_pct = 0.50
                add_qty = max(0, int(cash * target_pct / current_etf_price))
                if add_qty >= 1 and add_qty * current_etf_price <= cash:
                    cash -= add_qty * current_etf_price
                    position["qty"] += add_qty
                    continuation["scale_in_done"] = True
                    events.append({
                        "time": ts.strftime("%H:%M:%S"),
                        "action": "추가매수(MACD확인)",
                        "symbol": "레버리지" if desired_symbol == LONG_SYMBOL else "인버스",
                        "price": current_etf_price,
                        "qty": add_qty,
                        "pct": 50.0,
                    })

        # minute dump (one row per minute at :00, or any order/transition)
        minute_key = ts.replace(second=0, microsecond=0)
        should_dump = dump_minutes and _in_dump_window(ts) and (
            ts.second == 0 or order_placed == "Y" or direction_episode_changed
            or episode_change_reason == "OPPOSITE_CANDIDATE_BLOCKED"
            or (final_block and entry_eval.get("action") == "ENTER")
        )
        if should_dump and (minute_key != last_dumped_minute or order_placed == "Y" or direction_episode_changed):
            if ts.second == 0:
                last_dumped_minute = minute_key
            contrib = entry_eval.get("contributions") or {}
            contrib_s = ";".join(f"{k}={v}" for k, v in sorted(contrib.items()) if v)
            candidate = live_dir if (
                _existing_episode_direction and _existing_episode_direction != live_dir
            ) else ""
            row = {
                "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "label": label,
                "current_episode": continuation.get("direction"),
                "candidate_episode": candidate,
                "episode_id": continuation.get("direction_episode_id"),
                "episode_change_reason": episode_change_reason,
                "opposite_confirmed": _opposite_episode_confirmed,
                "existing_structure_broken": _existing_structure_broken,
                "reversal_probe_done": continuation.get("reversal_probe_done"),
                "probe_failed": continuation.get("episode_status") == "PROBE_FAILED",
                "episode_status": continuation.get("episode_status"),
                "entry_done": continuation.get("entry_done"),
                "awaiting_structural_reentry": continuation.get("awaiting_structural_reentry"),
                "structure_confirmed": bool(entry_eval.get("structure_confirmed")),
                "swing_break": swing_breakout,
                "target_etf": "LEV" if desired_symbol == LONG_SYMBOL else "INV",
                "etf_price": round(current_etf_price, 2),
                "vwap": round(float(vwap), 2) if vwap is not None else None,
                "above_vwap": confirm_above_vwap,
                "vwap_cross_reclaim": vwap_reclaim,
                "vwap_slope_3m": round(vwap_slope, 4) if vwap_slope is not None else None,
                "prev_above_vwap": _prev_above_vwap,
                "etf_5s": confirm_dirs.get(5),
                "etf_10s": confirm_dirs.get(10),
                "signal_5s": signal_dirs.get(5),
                "live_dir": live_dir,
                "continuation_allowed": continuation_allowed,
                "evidence_score": entry_eval.get("evidence_score"),
                "contributions": contrib_s,
                "expected_net_edge": entry_eval.get("expected_net_edge_pct"),
                "reward_risk": entry_eval.get("reward_risk"),
                "entry_action": entry_eval.get("action"),
                "entry_path": entry_eval.get("entry_path"),
                "entry_reason": entry_eval.get("reason_code"),
                "final_block_reason": final_block or (
                    None if order_placed == "Y" else entry_eval.get("reason_code")
                ),
                "order_placed": order_placed,
                "held_symbol": (
                    "LEV" if position and position["symbol"] == LONG_SYMBOL
                    else ("INV" if position else "")
                ),
            }
            minute_rows.append(row)

        ts += timedelta(seconds=5)

    buys = [e for e in events if e["action"] == "매수"]
    return {
        "label": label,
        "events": events,
        "buys": buys,
        "entries": len(buys),
        "minute_rows": minute_rows,
        "transition_log": transition_log,
        "block_log": block_log,
        "net_pnl_krw": cash - INITIAL_CASH,
        "day_regime": day_regime,
    }


def _patch_before_semantics() -> tuple:
    orig_detect = engine.detect_opposite_episode_transition
    orig_allows = engine.range_episode_allows_entry
    engine.detect_opposite_episode_transition = _old_detect_opposite_episode_transition  # type: ignore
    engine.range_episode_allows_entry = _old_range_episode_allows_entry  # type: ignore
    return orig_detect, orig_allows


def _restore(orig_detect, orig_allows) -> None:
    engine.detect_opposite_episode_transition = orig_detect
    engine.range_episode_allows_entry = orig_allows


def _analyze_missed_windows(rows: list[dict]) -> dict:
    """Summarize block chains for 13:22-14:17 DOWN and after 14:18 UP."""
    down_win = [r for r in rows if "13:22" <= r["timestamp"][11:16] <= "14:17"]
    up_win = [r for r in rows if "14:18" <= r["timestamp"][11:16] <= "14:40"]

    def _chain(window_rows: list[dict], want_dir: str) -> dict:
        live_hits = [r for r in window_rows if r.get("live_dir") == want_dir]
        enter_signals = [r for r in live_hits if r.get("entry_action") == "ENTER"]
        orders = [r for r in live_hits if r.get("order_placed") == "Y"]
        blocks = {}
        for r in live_hits:
            b = r.get("final_block_reason") or "NONE"
            blocks[b] = blocks.get(b, 0) + 1
        opp_blocked = [r for r in live_hits if r.get("episode_change_reason") == "OPPOSITE_CANDIDATE_BLOCKED"]
        opp_ok = [r for r in live_hits if r.get("episode_change_reason", "").startswith("OPPOSITE_")]
        sample = live_hits[0] if live_hits else None
        mid = live_hits[len(live_hits) // 2] if live_hits else None
        return {
            "minutes_logged": len(window_rows),
            "live_dir_hits": len(live_hits),
            "enter_signals": len(enter_signals),
            "orders": len(orders),
            "block_histogram": blocks,
            "opposite_candidate_blocked": len(opp_blocked),
            "opposite_transitions": [
                {"t": r["timestamp"], "reason": r["episode_change_reason"],
                 "broken": r["existing_structure_broken"], "vwap_r": r["vwap_cross_reclaim"],
                 "5/10": f"{r['etf_5s']}/{r['etf_10s']}", "cur": r["current_episode"]}
                for r in opp_ok if r.get("episode_change_reason", "").startswith("OPPOSITE_")
                and r["episode_change_reason"] != "OPPOSITE_CANDIDATE_BLOCKED"
            ][:10],
            "first_live": sample,
            "mid_live": mid,
            "first_enter_blocked": enter_signals[0] if enter_signals else None,
        }

    return {
        "down_13_22_14_17": _chain(down_win, "DOWN"),
        "up_after_14_18": _chain(up_win, "UP"),
    }


def main() -> int:
    load_optimized_config()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hynix, long_df, inv_df, src = _load_or_build_bars()
    print(f"Data source={src}  H={len(hynix)} L={len(long_df)} I={len(inv_df)}")
    print(f"  range {hynix['datetime'].min()} ~ {hynix['datetime'].max()}")

    # Sanity: run_replay AFTER should match instrumented entry count
    baseline = run_replay(hynix, long_df, inv_df)
    print(f"\nrun_replay(current WT) entries={baseline['entries']} "
          f"pnl={baseline['net_pnl_krw']:+,.0f} cons={baseline['net_pnl_conservative_krw']:+,.0f}")
    for e in baseline["events"]:
        if e["action"] == "매수":
            print(f"  BUY {e['time']} {e.get('symbol')} {e.get('path')} evid={e.get('evidence')}")

    print("\n=== AFTER (current working tree) instrumented ===")
    after = run_instrumented_replay(hynix, long_df, inv_df, label="AFTER")
    print(f"entries={after['entries']}")
    for e in after["buys"]:
        print(f"  BUY {e['time']} {e.get('symbol')} {e.get('path')} ep={e.get('episode_id')}")
    print(f"transitions={len(after['transition_log'])} blocks_logged={len(after['block_log'])}")
    for t in after["transition_log"]:
        if "12:30" <= t["time"][11:16] <= "14:40":
            print(f"  TRANS {t['time'][11:19]} {t['from']}->{t['to']} {t['reason']} "
                  f"broken={t['broken']} vwap_r={t['vwap_reclaim']} 5/10={t['dirs_5_10']}")

    print("\n=== BEFORE (HEAD episode semantics patched) ===")
    orig = _patch_before_semantics()
    try:
        before = run_instrumented_replay(hynix, long_df, inv_df, label="BEFORE", dump_minutes=False)
    finally:
        _restore(*orig)
    print(f"entries={before['entries']}")
    for e in before["buys"]:
        print(f"  BUY {e['time']} {e.get('symbol')} {e.get('path')}")

    # Diff vanished trades
    after_keys = {(e["time"][:5], e.get("symbol"), e.get("path")) for e in after["buys"]}
    before_keys = {(e["time"][:5], e.get("symbol"), e.get("path")) for e in before["buys"]}
    vanished = before_keys - after_keys
    gained = after_keys - before_keys
    print("\n=== 4→2 / before→after trade diff (same Naver+shape dataset) ===")
    print(f"BEFORE entries={before['entries']} AFTER entries={after['entries']}")
    print(f"vanished={sorted(vanished)}")
    print(f"gained={sorted(gained)}")

    # For each vanished trade, find block at that minute in AFTER dump by re-scanning block_log
    # Also re-run AFTER block_log around vanished times
    print("\n=== AFTER block_log around vanished windows ===")
    for b in after["block_log"]:
        hm = b["time"][11:16]
        if hm >= "09:00":
            if any(v[0] == hm or abs(int(v[0][:2]) * 60 + int(v[0][3:]) - (int(hm[:2]) * 60 + int(hm[3:]))) <= 2
                   for v in vanished) or "13:" <= hm <= "14:":
                print(f"  {b['time'][11:19]} {b['live_dir']} path={b['entry_path']} "
                      f"block={b['block']} status={b['episode_status']} entry_done={b['entry_done']} "
                      f"rev_done={b['reversal_probe_done']} swing={b['swing']} vwap_r={b['vwap_reclaim']} "
                      f"dir_chg={b['dir_changed']}")

    missed = _analyze_missed_windows(after["minute_rows"])
    print("\n=== Missed window summary (AFTER) ===")
    for name, info in missed.items():
        print(f"\n[{name}] live_hits={info['live_dir_hits']} ENTER={info['enter_signals']} orders={info['orders']}")
        print(f"  block_hist={info['block_histogram']}")
        print(f"  opp_candidate_blocked={info['opposite_candidate_blocked']} transitions={info['opposite_transitions']}")
        fe = info.get("first_enter_blocked") or info.get("mid_live") or info.get("first_live")
        if fe:
            print(
                f"  sample@{fe['timestamp'][11:19]} cur={fe['current_episode']} cand={fe['candidate_episode']} "
                f"ep={fe['episode_id']} status={fe['episode_status']} probe_failed={fe['probe_failed']} "
                f"rev_done={fe['reversal_probe_done']} swing={fe['swing_break']} vwap_r={fe['vwap_cross_reclaim']} "
                f"above={fe['above_vwap']} prev_above={fe['prev_above_vwap']} 5/10={fe['etf_5s']}/{fe['etf_10s']} "
                f"broken={fe['existing_structure_broken']} opp={fe['opposite_confirmed']} "
                f"action={fe['entry_action']} path={fe['entry_path']} evid={fe['evidence_score']} "
                f"edge={fe['expected_net_edge']} rr={fe['reward_risk']} block={fe['final_block_reason']} "
                f"order={fe['order_placed']}"
            )

    # Inverse VWAP upside check during DOWN window
    print("\n=== DOWN window inverse VWAP upside observations (AFTER dump rows) ===")
    down_rows = [
        r for r in after["minute_rows"]
        if "13:22" <= r["timestamp"][11:16] <= "14:17" and r.get("live_dir") == "DOWN"
    ]
    reclaim_true = [r for r in down_rows if r.get("vwap_cross_reclaim")]
    above = [r for r in down_rows if r.get("above_vwap")]
    print(f"  DOWN live rows={len(down_rows)} above_vwap={len(above)} reclaim_events={len(reclaim_true)}")
    for r in reclaim_true[:15]:
        print(
            f"  reclaim {r['timestamp'][11:19]} px={r['etf_price']} vwap={r['vwap']} "
            f"5/10={r['etf_5s']}/{r['etf_10s']} prev_above={r['prev_above_vwap']} "
            f"opp={r['opposite_confirmed']} cur={r['current_episode']}"
        )

    # PROBE_FAILED inheritance check via transitions
    print("\n=== PROBE_FAILED inheritance across episode reset ===")
    # Walk AFTER transitions: after reset_range_episode_probe_state, status should clear
    # Verify by looking at dump rows immediately after transition
    for t in after["transition_log"]:
        t_hm = t["time"][11:19]
        nearby = [
            r for r in after["minute_rows"]
            if r["timestamp"][11:19] >= t_hm and r["timestamp"][11:19] <= t_hm[:5] + ":59"
        ][:3]
        for r in nearby:
            print(
                f"  after reset@{t_hm} -> row@{r['timestamp'][11:19]} "
                f"ep={r['episode_id']} status={r['episode_status']} "
                f"probe_failed={r['probe_failed']} rev_done={r['reversal_probe_done']} "
                f"entry_done={r['entry_done']}"
            )

    # Write CSV
    fieldnames = list(after["minute_rows"][0].keys()) if after["minute_rows"] else [
        "timestamp", "label", "current_episode", "candidate_episode", "episode_id",
        "final_block_reason", "order_placed",
    ]
    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in after["minute_rows"]:
            w.writerow(row)
    print(f"\nWrote minute dump: {CSV_PATH} ({len(after['minute_rows'])} rows)")

    # Production vs replay function parity note
    prod_funcs = [
        "detect_opposite_episode_transition",
        "range_episode_allows_entry",
        "reset_range_episode_probe_state",
        "update_range_episode_structural_events",
        "evaluate_range_weighted_entry",
        "mark_range_reversal_probe_entered",
        "mark_range_probe_exit",
        "mark_range_episode_exit_awaiting_structure",
        "evaluate_weighted_range_probe_exit",
        "evaluate_weighted_continuation_exit",
        "promote_reversal_probe_to_continuation",
    ]
    summary = {
        "data_source": src,
        "analysis_tree": "current_working_tree",
        "run_replay_entries": baseline["entries"],
        "after_entries": after["entries"],
        "before_entries": before["entries"],
        "after_buys": after["buys"],
        "before_buys": before["buys"],
        "vanished": [list(x) for x in sorted(vanished)],
        "gained": [list(x) for x in sorted(gained)],
        "transitions_dump_window": [
            t for t in after["transition_log"]
            if "12:30" <= t["time"][11:16] <= "14:40"
        ],
        "missed_windows": {
            k: {
                **{kk: vv for kk, vv in v.items() if kk not in ("first_live", "mid_live", "first_enter_blocked")},
                "sample": {
                    kk: (v.get("first_enter_blocked") or v.get("mid_live") or v.get("first_live") or {}).get(kk)
                    for kk in (
                        "timestamp", "current_episode", "candidate_episode", "episode_id",
                        "episode_status", "probe_failed", "reversal_probe_done", "swing_break",
                        "vwap_cross_reclaim", "above_vwap", "prev_above_vwap", "etf_5s", "etf_10s",
                        "existing_structure_broken", "opposite_confirmed", "entry_action",
                        "entry_path", "evidence_score", "expected_net_edge", "reward_risk",
                        "final_block_reason", "order_placed", "episode_change_reason",
                    )
                } if (v.get("first_enter_blocked") or v.get("mid_live") or v.get("first_live")) else None,
            }
            for k, v in missed.items()
        },
        "production_replay_shared_funcs": prod_funcs,
        "csv_path": str(CSV_PATH),
        "inverse_vwap_reclaims_in_down_window": len(reclaim_true),
        "inverse_above_vwap_in_down_window": len(above),
    }
    JSON_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Wrote summary: {JSON_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
