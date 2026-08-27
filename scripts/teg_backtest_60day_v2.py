#!/usr/bin/env python
"""READ-ONLY 60-business-day A/B/C replay comparing TEG v2 (scripts/
teg_gate_v2.py -- signed net-change conditions 3/4, replacing v1's strict
absolute-monotonic version per 2026-08-27 user feedback after v1 rejected
the user's own real target flag 8/25 12:09 UP_RED) against existing
production TW2. Never touches app/trading/macd2 production code. Byte-
identical harness to scripts/teg_backtest_60day.py (v1) except for the gate
module imported and the output directory -- v1's own outputs are untouched
so both remain independently comparable.

Reuses the CORRECTED-CLOCK-SEMANTICS engine (scripts/tw_gate_corrected_clock_
engine.py, 2026-08-19/20 fix: no fill-price look-ahead, incomplete 3m bars
dropped via the real market_data.filter_complete_3m_bars) for day prep and
1-minute fills -- see that module's own docstring for exactly which two bugs
it fixes vs every earlier backtest script in this repo.

Three variants, ALL sharing the identical exit ladder (TW2's TP1=3.0%/50%
partial, TP2=TW2_MORNING_TP2(6.0%), stop=-1.7%, trailing/after-TP1 stops,
afternoon ladder, whipsaw-tolerant T+3 reversal hold) -- only the ENTRY gate
differs:

  A. 기존 TW2 (unmodified: evaluate_time_window_entry + evaluate_tw2_extra_
     vetoes, current production thresholds/entry caps).
  B. TW2 + TEG: a TW2-approved candidate must ALSO pass TEG
     (scripts/teg_gate.evaluate_teg) to actually enter. A reversal that TW2
     approves but TEG rejects: the stale opposite-side position is closed
     (OPPOSITE_SIGNAL, TW2 itself reconfirmed the reversal is real) but the
     new-direction entry is withheld (TEG says the entry quality is not
     there yet) -- goes flat rather than either holding the stale position
     or force-entering a sub-quality setup. (This specific choice is not
     spelled out in the user's original TEG spec -- flagged explicitly in
     the report.)
  C. TW2 with the entry-count cap replaced by TEG-priority: a candidate
     TW2 would reject SOLELY for exceeding MAX_MORNING_ENTRIES/MAX_
     AFTERNOON_ENTRIES/MAX_DAILY_ENTRIES (i.e. real_decision.block_reason
     == config.TW_REJECT_MAX_ENTRY_COUNT -- which, by evaluate_time_window_
     entry's own check ordering, is only ever reached after every OTHER
     check -- interval/window/quality-score/reset -- already passed) gets a
     second chance: if it also clears evaluate_tw2_extra_vetoes and TEG,
     it enters anyway, uncapped. A candidate TW2 approves within the normal
     cap enters exactly as in A (TEG is never consulted for those). A
     candidate TW2 rejects for any OTHER reason stays rejected.

Trading window: the 60 most recent business days with archived 1-minute data
(data/cache/replay_YYYYMMDD_{hynix,long,inverse}_1m.csv) as of 2026-08-27 --
2026-06-01..2026-08-26 (confirmed via directory listing; NOT 2026-08-27 since
today's session has no archive yet).
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import scripts.tw_gate_relaxed_optimization as base  # noqa: E402
import scripts.tw_gate_corrected_clock_engine as cce  # noqa: E402
import scripts.teg_gate_v2 as teg  # noqa: E402
from app.trading.macd2 import config  # noqa: E402
from app.trading.macd2 import time_window_filter as twf  # noqa: E402
from app.trading.macd2 import time_window_position_manager as twpm  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

KST = config.KST
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "teg_gate_v2_60day"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TP2_OVERRIDE_PCT = config.TW2_MORNING_TP2 * 100.0

DATES = sorted({
    p.stem.split("_")[1] for p in CACHE_DIR.glob("replay_*_hynix_1m.csv")
})[-60:]


def _target_symbol(direction: Direction) -> str:
    return base._target_symbol(direction)


def _direction_for_symbol(symbol: Optional[str]) -> Optional[Direction]:
    return base._direction_for_symbol(symbol) if symbol else None


def _net_pct(symbol: str, entry_price: float, exit_price: float) -> float:
    return base._net_pct(symbol, entry_price, exit_price)


def simulate_teg_variant(
    date: str, hynix_bars_3m, flags, complete_bar_starts: dict, etf_1m: dict,
    *, start_idx: int, variant: str,
):
    """variant in {"A", "B", "C"}. Returns (trades: list[base.Trade],
    flag_log: list[dict] -- one row per registered candidate resolution,
    used for the 5-day/2-day per-flag detail tables)."""
    assert variant in ("A", "B", "C")
    trades: list = []
    flag_log: list[dict] = []
    position: Optional[base.OpenPosition] = None
    flags_by_idx = dict(flags)
    pending = None
    morning_count = 0
    afternoon_count = 0
    daily_entry_seq = 0
    whipsaw_holds = 0

    def position_direction():
        return _direction_for_symbol(position.symbol) if position is not None else None

    def fill_at(symbol, recognition_time):
        return cce.nearest_close(etf_1m.get(symbol), recognition_time)

    for idx in range(start_idx, len(hynix_bars_3m)):
        bar_start = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).to_pydatetime()
        bar_ts_raw = hynix_bars_3m["datetime"].iloc[idx]
        recognition_time = bar_start + timedelta(minutes=3)

        # 1) position management (identical ladder across A/B/C)
        if position is not None and idx > position.entry_idx:
            if bar_ts_raw in complete_bar_starts.get(position.symbol, set()):
                close = fill_at(position.symbol, recognition_time)
                if close is not None:
                    net = _net_pct(position.symbol, position.entry_price, close)
                    if position.session == "MORNING":
                        pm = twpm.evaluate_morning_position(
                            net_return_pct=net, tp1_done=position.tp1_done,
                            peak_net_return=position.peak_net_return, tp2_pct_override=TP2_OVERRIDE_PCT,
                        )
                    else:
                        pm = twpm.evaluate_afternoon_position(net_return_pct=net, peak_net_return=position.peak_net_return)
                    position.peak_net_return = pm.peak_net_return
                    position.tp1_done = pm.tp1_done
                    if pm.exit_reason is not None:
                        if pm.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                            position.trade.tp1_hit = True
                            base._record_partial_leg(position, qty_fraction=pm.sell_fraction, price=close, reason=pm.exit_reason)
                        else:
                            if pm.exit_reason == config.EXIT_TW_TP2_FULL:
                                position.trade.tp2_hit = True
                            base._close_trade(position.trade, exit_time=recognition_time, exit_price=close, reason=pm.exit_reason, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                            trades.append(position.trade)
                            position = None

        # 2) forced liquidation at/after 15:00
        bar_time = bar_start.astimezone(KST).time()
        if position is not None and bar_time >= config.FORCE_LIQUIDATE_AT:
            close = fill_at(position.symbol, recognition_time)
            if close is not None:
                base._close_trade(position.trade, exit_time=recognition_time, exit_price=close, reason=config.EXIT_FORCED_LIQUIDATION, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                trades.append(position.trade)
                position = None
            pending = None

        executed_this_tick = False

        # 3) resolve an existing pending candidate FIRST (parity ordering)
        if pending is not None:
            p_direction, p_idx, p_bar_start = pending
            if idx == p_idx + 1:
                pending = None
                flag_bar_dt = p_bar_start
                decision_at = bar_start + timedelta(minutes=3)
                truncated = hynix_bars_3m.iloc[: idx + 1]

                decision = twf.evaluate_time_window_entry(
                    truncated, p_direction, flag_bar_dt, decision_at,
                    position_direction=position_direction(), morning_entry_count=morning_count, afternoon_entry_count=afternoon_count,
                )
                tw2_approved = bool(decision.approved)
                tw2_block_reason = decision.block_reason or ""
                if tw2_approved:
                    vetoed, veto_reason = twf.evaluate_tw2_extra_vetoes(truncated, p_direction, flag_bar_dt, decision_at)
                    if vetoed:
                        tw2_approved = False
                        tw2_block_reason = veto_reason or "TW2_VETO"

                # Self-contained -- TEG's own condition 1 re-derives T+3
                # confirmation directly (see teg_gate.py), so ONE evaluation
                # serves both variant B's gate and variant C's count-cap-
                # bypass check; no separate "bypass" re-evaluation needed.
                teg_decision = teg.evaluate_teg(truncated, p_direction, flag_bar_dt, decision_at)

                count_cap_bypass_eligible = False
                if variant == "C" and not tw2_approved and decision.block_reason == config.TW_REJECT_MAX_ENTRY_COUNT:
                    vetoed2, veto_reason2 = twf.evaluate_tw2_extra_vetoes(truncated, p_direction, flag_bar_dt, decision_at)
                    if not vetoed2:
                        count_cap_bypass_eligible = True

                # ── final per-variant entry decision ──
                if variant == "A":
                    final_approved = tw2_approved
                    final_reason = tw2_block_reason if not tw2_approved else ""
                elif variant == "B":
                    final_approved = tw2_approved and teg_decision.approved
                    if not tw2_approved:
                        final_reason = tw2_block_reason
                    elif not teg_decision.approved:
                        final_reason = "TEG_REJECTED:" + ",".join(teg_decision.reject_reasons)
                    else:
                        final_reason = ""
                else:  # C
                    if tw2_approved:
                        final_approved = True
                        final_reason = ""
                    elif count_cap_bypass_eligible and teg_decision.approved:
                        final_approved = True
                        final_reason = "COUNT_CAP_BYPASSED_VIA_TEG"
                    else:
                        final_approved = False
                        if count_cap_bypass_eligible:
                            final_reason = "TW_REJECT_MAX_ENTRY_COUNT+TEG_REJECTED:" + ",".join(teg_decision.reject_reasons)
                        else:
                            final_reason = tw2_block_reason

                is_reversal = position is not None and _target_symbol(p_direction) != position.symbol
                is_whipsaw_reject = (not tw2_approved) and (tw2_block_reason in config.TW_WHIPSAW_REJECT_REASONS)

                flag_log.append({
                    "date": date, "direction": p_direction.value,
                    "flag_bar_start": flag_bar_dt.isoformat(), "confirm_bar_start": decision_at.isoformat(),
                    "variant": variant, "tw2_approved": tw2_approved, "tw2_block_reason": tw2_block_reason,
                    "teg_approved": teg_decision.approved, "teg_conditions": dict(teg_decision.conditions),
                    "teg_metrics": {k: v for k, v in teg_decision.metrics.items()},
                    "teg_reject_reasons": list(teg_decision.reject_reasons),
                    "count_cap_bypass_eligible": count_cap_bypass_eligible,
                    "final_approved": final_approved, "final_reason": final_reason,
                    "is_reversal": is_reversal, "is_whipsaw_reject": is_whipsaw_reject,
                    "quality_score": decision.score,
                })

                do_hold = is_reversal and is_whipsaw_reject and not final_approved
                if do_hold:
                    whipsaw_holds += 1
                elif final_approved:
                    target = _target_symbol(p_direction)
                    fill = fill_at(target, recognition_time)
                    if fill is not None:
                        if position is not None and position.symbol != target:
                            close_now = fill_at(position.symbol, recognition_time)
                            if close_now is not None:
                                base._close_trade(position.trade, exit_time=recognition_time, exit_price=close_now, reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                                trades.append(position.trade)
                                position = None
                                executed_this_tick = True
                        if position is None:
                            window = (decision.metrics or {}).get("window") or twf.classify_window(decision_at.astimezone(KST).time())
                            session = twf.session_for_window(window) or "MORNING"
                            if session == "MORNING":
                                morning_count += 1
                            else:
                                afternoon_count += 1
                            daily_entry_seq += 1
                            new_trade = base.Trade(
                                trading_date=date, direction=p_direction.value, flag_time=flag_bar_dt.isoformat(),
                                entry_time=recognition_time.isoformat(), entry_symbol=target, entry_price=fill,
                                window=window, quality_score=decision.score, flag_seq_of_day=daily_entry_seq,
                            )
                            position = base.OpenPosition(symbol=target, entry_idx=idx + 1, entry_price=fill, entry_time=recognition_time, session=session, trade=new_trade)
                            executed_this_tick = True
                else:
                    # rejected (TW2 and/or TEG) and NOT a whipsaw-hold case:
                    # a reversal that was rejected purely because TEG blocked
                    # the re-entry (TW2 itself DID reconfirm the reversal) --
                    # close the stale opposite-side position and go flat,
                    # never force an entry TEG says isn't ready yet.
                    if is_reversal and position is not None:
                        close_now = fill_at(position.symbol, recognition_time)
                        if close_now is not None:
                            base._close_trade(position.trade, exit_time=recognition_time, exit_price=close_now, reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                            trades.append(position.trade)
                            position = None
                            executed_this_tick = True

        # 4) THEN register a fresh confirmed flag as the new pending candidate
        if not executed_this_tick and idx in flags_by_idx:
            direction = flags_by_idx[idx]
            pending = (direction, idx, bar_start)

    if position is not None:
        last_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[-1]).to_pydatetime() + timedelta(minutes=3)
        close = fill_at(position.symbol, last_dt)
        if close is None:
            close = position.entry_price
        base._close_trade(position.trade, exit_time=last_dt, exit_price=close, reason="END_OF_DATA", entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
        trades.append(position.trade)
    return trades, flag_log


def run_over_cache(cache: list[dict], variant: str):
    all_trades = []
    all_flag_log = []
    for day in cache:
        trades, flag_log = simulate_teg_variant(
            day["date"], day["hynix_bars_3m"], day["flags"], day["complete_bar_starts"], day["etf_1m"],
            start_idx=day["start_idx"], variant=variant,
        )
        all_trades.extend(trades)
        all_flag_log.extend(flag_log)
    return all_trades, all_flag_log


def _metrics(trades: list, n_days: int) -> dict:
    closed = [t for t in trades if t.net_return_pct is not None]
    wins = [t for t in closed if t.net_return_pct > 0]
    total = sum(t.net_return_pct for t in closed)
    compounded = 1.0
    equity = peak = max_dd = 0.0
    closed_sorted = sorted(closed, key=lambda t: t.entry_time)
    for t in closed_sorted:
        compounded *= (1.0 + t.net_return_pct / 100.0)
        equity += t.net_return_pct
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trading_days": n_days, "entries": len(closed), "wins": len(wins),
        "win_rate_pct": round(len(wins) / len(closed) * 100.0, 1) if closed else 0.0,
        "total_pct": round(total, 3), "compound_pct": round((compounded - 1.0) * 100.0, 3),
        "avg_pct_per_trade": round(total / len(closed), 3) if closed else 0.0,
        "avg_trades_per_day": round(len(closed) / n_days, 2) if n_days else 0.0,
        "mdd_pct": round(max_dd, 3),
    }


def main():
    print(f"TEG {teg.TEG_VERSION} -- {len(DATES)} business days, {DATES[0]}..{DATES[-1]}")
    cache, notes = cce.prepare_cache(DATES)
    print(f"Loaded {len(cache)}/{len(DATES)} days")
    if notes:
        print(f"Notes ({len(notes)}):")
        for n in notes[:30]:
            print(f"  {n}")
        if len(notes) > 30:
            print(f"  ... and {len(notes) - 30} more (see output json)")

    results = {}
    flag_logs = {}
    for variant in ("A", "B", "C"):
        trades, flag_log = run_over_cache(cache, variant)
        results[variant] = _metrics(trades, len(cache))
        flag_logs[variant] = flag_log
        trades_path = OUTPUT_DIR / f"trades_variant_{variant}.json"
        trades_path.write_text(json.dumps([asdict(t) for t in trades], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        flags_path = OUTPUT_DIR / f"flag_log_variant_{variant}.json"
        flags_path.write_text(json.dumps(flag_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"\n{'variant':<10}{'days':>6}{'entries':>9}{'wins':>7}{'win%':>7}{'total%':>10}{'compound%':>11}{'avg/trd%':>10}{'trd/day':>9}{'MDD%':>7}")
    for name, m in results.items():
        print(f"{name:<10}{m['trading_days']:>6}{m['entries']:>9}{m['wins']:>7}{m['win_rate_pct']:>7}{m['total_pct']:>10}{m['compound_pct']:>11}{m['avg_pct_per_trade']:>10}{m['avg_trades_per_day']:>9}{m['mdd_pct']:>7}")

    summary_path = OUTPUT_DIR / "summary_metrics.json"
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved: {summary_path}")
    print(f"Per-variant trades/flag logs under: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
