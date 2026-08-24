#!/usr/bin/env python
"""READ-ONLY research: does carrying a strong 08:45-08:59 premarket MACD flag
into a 09:00/09:03 entry help, on top of the UNCHANGED, currently-shipped
TW2 filter? Never touches app/trading/macd2/* production code.

Baseline reproduces exactly what production does today: every premarket flag
(any bar before config.SESSION_OPEN) is blocked, zero exceptions. Three
carry-over candidates are compared against it -- PRE15 / PRE15+TW /
PRE15+quality (see docstrings on each simulate_* function below for the exact
rule each implements, straight from the user's spec).

Built on scripts/tw_gate_corrected_clock_engine.py (clock-semantics-correct
fills, incomplete-bar filtering) -- reuses its day-prep (prepare_cache),
Trade/OpenPosition/_close_trade/_record_partial_leg bookkeeping (via
scripts/tw_gate_relaxed_optimization.py, imported as `base` there), and the
REAL production decision functions (time_window_filter.evaluate_time_window_
entry / evaluate_tw2_extra_vetoes, time_window_position_manager.evaluate_
morning_position / evaluate_afternoon_position) -- never reimplements TW2's
own scoring/veto math. Only the premarket candidate-generation layer in
front of the gate is new, backtest-only code.

Every one of the 4 candidates (BASELINE/PRE15/PRE15+TW/PRE15+quality) runs
the EXACT SAME TW2 gate for every ordinary intraday flag -- only the
premarket layer differs -- so the comparison isolates the premarket-carry
effect alone.

Uses the same 58-trading-day window as scripts/compare_tw1_tw2_nofilter_
58day.py (reused verbatim, not re-derived).
"""
from __future__ import annotations

import sys
from datetime import time as dtime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.tw_gate_relaxed_optimization as base  # noqa: E402
import scripts.tw_gate_corrected_clock_engine as clk  # noqa: E402
from app.trading.macd2 import config, time_window_filter as twf, time_window_position_manager as twpm  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

KST = config.KST
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
OUT_DIR = PROJECT_ROOT / "data" / "validation" / "premarket_carryover_backtest"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TW2_TP2_PCT = config.TW2_MORNING_TP2 * 100.0

# ── same 58-day window as compare_tw1_tw2_nofilter_58day.py, reused verbatim ─
DATES = [
    p.stem.split("_")[1] for p in sorted(CACHE_DIR.glob("replay_*_hynix_1m.csv"))
    if "20260527" <= p.stem.split("_")[1] <= "20260821"
][2:]

WIN_08_30_44 = (dtime(8, 30), dtime(8, 45))
WIN_08_45_59 = (dtime(8, 45), dtime(9, 0))


def _target(direction: Direction) -> str:
    return base._target_symbol(direction)


def _first_close_at_or_after(df_1m: Optional[pd.DataFrame], at) -> Optional[float]:
    """Earliest real 1-minute close at/after wall-clock ``at`` -- the actual
    first tradable price once the market opens (no look-ahead: never picks
    anything the carry-over decision itself couldn't have acted on at 09:00
    open). Distinct from clk.nearest_close, which looks BACKWARD (<=at) and
    is wrong for a fill that happens right at session open with no premarket
    ETF quotes to look back to."""
    if df_1m is None or df_1m.empty:
        return None
    candidates = df_1m[df_1m["datetime"] >= at]
    if candidates.empty:
        return None
    return float(candidates.iloc[0]["close"])


def _premarket_flags_in(flags, hynix_bars_3m, start, end):
    out = []
    for idx, direction in flags:
        t = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).astimezone(KST).time()
        if start <= t < end:
            out.append((idx, direction, t))
    return out


def _carry_candidate(day: dict):
    """Shared eligibility for PRE15 / PRE15+TW / PRE15+quality: the LAST
    flag in [08:45, 09:00), cancelled if any OPPOSITE flag occurs anywhere
    before 09:00 after it (even one confirmed inside 08:45-08:59 itself, or
    between the last 08:45-08:59 flag and 09:00 -- there is no such gap here
    since bars are examined in order, but re-checked explicitly for
    clarity). A flag before 08:45 is NEVER eligible (hard rule, checked by
    construction: only flags matching WIN_08_45_59 are ever considered
    here)."""
    hynix_bars_3m, flags, start_idx = day["hynix_bars_3m"], day["flags"], day["start_idx"]
    pre_15 = _premarket_flags_in(flags, hynix_bars_3m, *WIN_08_45_59)
    if not pre_15:
        return None
    cand_idx, cand_dir, _t = pre_15[-1]
    # cancel if ANY opposite flag occurs after the candidate and before 09:00
    for idx, direction in flags:
        if idx <= cand_idx:
            continue
        t = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).astimezone(KST).time()
        if t >= dtime(9, 0):
            break
        if direction != cand_dir:
            return None  # reversed before open -- carry cancelled
    return cand_idx, cand_dir


def _has_opposite_flag_at(flags, idx, direction) -> bool:
    d = dict(flags)
    return idx in d and d[idx] != direction


def _open_idx(day: dict) -> Optional[int]:
    """The index of the first bar at/after config.SESSION_OPEN (09:00) on
    the trading day itself -- NOT day['start_idx'], which (bug found while
    validating this script) is actually the first bar of the CALENDAR day
    including NXT premarket (08:00), since hynix 1m data is continuous from
    08:00. Every premarket-carry fill/opposite-flag check below needs the
    true 09:00 bar, not 08:00."""
    hynix_bars_3m = day["hynix_bars_3m"]
    date = day["date"]
    times = hynix_bars_3m["datetime"].dt.strftime("%Y%m%d")
    mask = (times == date) & (hynix_bars_3m["datetime"].apply(lambda d: d.astimezone(KST).time()) >= config.SESSION_OPEN)
    if not mask.any():
        return None
    return int(mask.to_numpy().nonzero()[0][0])


def _has_same_flag_at(flags, idx, direction) -> bool:
    d = dict(flags)
    return idx in d and d[idx] == direction


# ── candidate simulators ─────────────────────────────────────────────────────
# All four share the identical intraday TW2 loop (copied from
# tw_gate_corrected_clock_engine.simulate_variant, entry_window=None branch,
# with TW2 extra-vetoes + TP2 override added -- clk.simulate_variant itself
# has no TW2 support, see investigation notes in the final report). Only the
# premarket pre-seed differs per candidate, injected as an OpenPosition
# BEFORE the intraday loop starts (start_idx onward), so every entry-count/
# TP-SL/opposite-signal/forced-liquidation code path downstream treats it
# exactly like any other already-open position.


def _tw2_intraday_loop(date, hynix_bars_3m, flags, complete_bar_starts, etf_1m, start_idx,
                        preseeded_position=None, morning_count0=0):
    trades: list = []
    position = preseeded_position
    flags_by_idx = dict(flags)
    pending = None
    morning_count = morning_count0
    afternoon_count = 0
    daily_entry_seq = 1 if preseeded_position is not None else 0

    def position_direction():
        return base._direction_for_symbol(position.symbol) if position is not None else None

    def fill_at(symbol, recognition_time):
        return clk.nearest_close(etf_1m.get(symbol), recognition_time)

    for idx in range(start_idx, len(hynix_bars_3m)):
        bar_start = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).to_pydatetime()
        bar_ts_raw = hynix_bars_3m["datetime"].iloc[idx]
        recognition_time = bar_start + timedelta(minutes=3)
        bar_time = bar_start.astimezone(KST).time()

        if position is not None and idx > position.entry_idx:
            if bar_ts_raw in complete_bar_starts.get(position.symbol, set()):
                close = fill_at(position.symbol, recognition_time)
                if close is not None:
                    net = base._net_pct(position.symbol, position.entry_price, close)
                    if position.session == "MORNING":
                        pm = twpm.evaluate_morning_position(net_return_pct=net, tp1_done=position.tp1_done, peak_net_return=position.peak_net_return, tp2_pct_override=TW2_TP2_PCT)
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

        if position is not None and bar_time >= config.FORCE_LIQUIDATE_AT:
            close = fill_at(position.symbol, recognition_time)
            if close is not None:
                base._close_trade(position.trade, exit_time=recognition_time, exit_price=close, reason=config.EXIT_FORCED_LIQUIDATION, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                trades.append(position.trade)
                position = None
            pending = None

        executed_this_tick = False

        if pending is not None:
            p_direction, p_idx, p_bar_start = pending
            if idx == p_idx + 1:
                pending = None
                flag_bar_dt = p_bar_start
                decision_at = bar_start + timedelta(minutes=3)
                decision = twf.evaluate_time_window_entry(
                    hynix_bars_3m.iloc[: idx + 1], p_direction, flag_bar_dt, decision_at,
                    position_direction=position_direction(), morning_entry_count=morning_count, afternoon_entry_count=afternoon_count,
                )
                approved = bool(decision.approved)
                if approved:
                    vetoed, _reason = twf.evaluate_tw2_extra_vetoes(hynix_bars_3m.iloc[: idx + 1], p_direction, flag_bar_dt, decision_at)
                    approved = not vetoed
                if approved:
                    target = _target(p_direction)
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
                            session = (decision.metrics or {}).get("session") or twf.session_for_window((decision.metrics or {}).get("window"))
                            if session == "MORNING":
                                morning_count += 1
                            else:
                                afternoon_count += 1
                            daily_entry_seq += 1
                            new_trade = base.Trade(
                                trading_date=date, direction=p_direction.value, flag_time=flag_bar_dt.isoformat(),
                                entry_time=recognition_time.isoformat(), entry_symbol=target, entry_price=fill,
                                window=(decision.metrics or {}).get("window"), quality_score=decision.score, flag_seq_of_day=daily_entry_seq,
                            )
                            position = base.OpenPosition(symbol=target, entry_idx=idx + 1, entry_price=fill, entry_time=recognition_time, session=session, trade=new_trade)
                            executed_this_tick = True
                else:
                    if position is not None and _target(p_direction) != position.symbol:
                        close_now = fill_at(position.symbol, recognition_time)
                        if close_now is not None:
                            base._close_trade(position.trade, exit_time=recognition_time, exit_price=close_now, reason=config.EXIT_OPPOSITE_SIGNAL, entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
                            trades.append(position.trade)
                            position = None
                            executed_this_tick = True

        if not executed_this_tick and idx in flags_by_idx:
            pending = (flags_by_idx[idx], idx, bar_start)

    if position is not None:
        last_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[-1]).to_pydatetime() + timedelta(minutes=3)
        close = fill_at(position.symbol, last_dt)
        if close is None:
            close = position.entry_price
        base._close_trade(position.trade, exit_time=last_dt, exit_price=close, reason="END_OF_DATA", entry_price=position.entry_price, symbol=position.symbol, remaining_fraction=position.remaining_fraction)
        trades.append(position.trade)
    return trades


def run_baseline(day):
    # 2026-08-24 bug fix (found while answering a follow-up comparison
    # question): day["start_idx"] is the first bar of the CALENDAR day
    # including NXT premarket (08:00), not session open -- same trap
    # documented on _open_idx() above. Using it here meant BASELINE's own
    # loop silently processed premarket flags too (no BEFORE_SESSION_OPEN
    # gate anywhere in _tw2_intraday_loop), so BASELINE ended up accidentally
    # replicating the exact premarket-carry behavior it was supposed to be
    # the (premarket-blocking) control for. Confirmed real-world effect:
    # 2026-07-30/08-20/08-24 all showed BASELINE's first trade with a
    # pre-09:00 flag_time and a 09:03 entry, identical to PRE15+TW's own
    # carry trade -- an artifact, not a real "no difference" finding.
    open_idx = _open_idx(day)
    if open_idx is None:
        return []
    return _tw2_intraday_loop(day["date"], day["hynix_bars_3m"], day["flags"], day["complete_bar_starts"], day["etf_1m"], open_idx)


def run_pre15(day):
    """PRE15: last 08:45-08:59 flag, still same direction at 09:00 (no
    opposite flag before 09:00) -> enters IMMEDIATELY at 09:00 using the
    first real ETF price at/after session open (no T+3 wait -- this is the
    whole point of "carrying over" a flag that's already been sitting there
    since before 08:59). Deduped against a genuine new 09:00 flag in the
    SAME direction (one entry, not two) by simply pre-seeding the position
    before the intraday loop starts, then letting the loop's own dedup-via-
    already-open-position logic handle it naturally. An OPPOSITE 09:00 flag
    supersedes the carry entirely (handled below by simply not pre-seeding
    when that's detected -- the fresh flag flows through the normal gate
    instead, exactly like BASELINE)."""
    cand = _carry_candidate(day)
    if cand is None:
        return run_baseline(day), False
    cand_idx, cand_dir = cand
    open_idx = _open_idx(day)
    if open_idx is None:
        return run_baseline(day), False
    hynix_bars_3m, flags = day["hynix_bars_3m"], day["flags"]
    if _has_opposite_flag_at(flags, open_idx, cand_dir):
        return run_baseline(day), False  # 09:00 opposite flag supersedes carry
    target = _target(cand_dir)
    fill = _first_close_at_or_after(day["etf_1m"].get(target), pd.Timestamp(hynix_bars_3m["datetime"].iloc[open_idx]).to_pydatetime())
    if fill is None:
        return run_baseline(day), False
    entry_time = pd.Timestamp(hynix_bars_3m["datetime"].iloc[open_idx]).to_pydatetime()
    trade = base.Trade(
        trading_date=day["date"], direction=cand_dir.value, flag_time=hynix_bars_3m["datetime"].iloc[cand_idx].isoformat(),
        entry_time=entry_time.isoformat(), entry_symbol=target, entry_price=fill,
        window="PRE15_CARRY", quality_score=-1, flag_seq_of_day=1,
    )
    position = base.OpenPosition(symbol=target, entry_idx=open_idx, entry_price=fill, entry_time=entry_time, session="MORNING", trade=trade)
    trades = _tw2_intraday_loop(day["date"], hynix_bars_3m, flags, day["complete_bar_starts"], day["etf_1m"], open_idx, preseeded_position=position, morning_count0=1)
    return trades, True


def run_pre15_tw(day):
    """PRE15+TW: same candidacy, but additionally requires the direction to
    still hold through the 09:00-09:03 bar (no opposite flag confirmed on
    that bar either) before entering AT 09:03 using the real fill price
    then. This is a custom equivalent of the normal T+3 re-confirm -- it
    cannot literally call evaluate_time_window_entry with flag_bar_dt=the
    original premarket bar, because that function hard-asserts bars_3m ends
    EXACTLY one bar after flag_bar_dt (see time_window_filter.py:613-618),
    and here there are TWO bars between the premarket flag and 09:03 (the
    09:00 bar and the 09:00-09:03 bar) -- so the "still holding" check is
    done directly here instead, same spirit, explicit implementation."""
    cand = _carry_candidate(day)
    if cand is None:
        return run_baseline(day), False
    cand_idx, cand_dir = cand
    open_idx = _open_idx(day)
    if open_idx is None:
        return run_baseline(day), False
    hynix_bars_3m, flags = day["hynix_bars_3m"], day["flags"]
    if open_idx + 1 >= len(hynix_bars_3m):
        return run_baseline(day), False
    # cancellation window extends through 09:03 for this variant specifically
    # (it enters at 09:03, after the 09:00-09:03 re-confirm bar) -- checked
    # AT open_idx (09:00 bar) and open_idx+1 (09:03 bar) explicitly; the
    # earlier [candidate_bar, 09:00) span is already covered by
    # _carry_candidate's own scan.
    if _has_opposite_flag_at(flags, open_idx, cand_dir) or _has_opposite_flag_at(flags, open_idx + 1, cand_dir):
        return run_baseline(day), False
    target = _target(cand_dir)
    entry_time = pd.Timestamp(hynix_bars_3m["datetime"].iloc[open_idx]).to_pydatetime() + timedelta(minutes=3)  # 09:03 = 09:00 bar's own close
    fill = clk.nearest_close(day["etf_1m"].get(target), entry_time)
    if fill is None:
        return run_baseline(day), False
    trade = base.Trade(
        trading_date=day["date"], direction=cand_dir.value, flag_time=hynix_bars_3m["datetime"].iloc[cand_idx].isoformat(),
        entry_time=entry_time.isoformat(), entry_symbol=target, entry_price=fill,
        window="PRE15_CARRY_TW", quality_score=-1, flag_seq_of_day=1,
    )
    position = base.OpenPosition(symbol=target, entry_idx=open_idx + 1, entry_price=fill, entry_time=entry_time, session="MORNING", trade=trade)
    trades = _tw2_intraday_loop(day["date"], hynix_bars_3m, flags, day["complete_bar_starts"], day["etf_1m"], open_idx + 1, preseeded_position=position, morning_count0=1)
    return trades, True


def run_pre15_quality(day):
    """PRE15+quality: same candidacy (enter at 09:00, no extra bar wait like
    PRE15), but the carried flag must ALSO pass the REAL evaluate_time_
    window_entry (quality score / gap-expansion / reset / entry-count) and
    evaluate_tw2_extra_vetoes (VWAP, recent-cross) exactly as any ordinary
    flag would. To satisfy evaluate_time_window_entry's hard one-bar-apart
    alignment assertion, flag_bar_dt is taken as the LAST complete bar
    before session open (start_idx-1, i.e. the 08:57-09:00 bar) and
    decision_at=09:00 (that bar's own close) -- this evaluates the REAL
    quality/VWAP/cross state exactly as it stood the instant the market
    opened, which is the closest faithful reading of "run the carried
    premarket flag through the same TW2 approval bar as a normal flag,
    right at 09:00" the function's contract allows. This is a documented
    design choice, not a re-derivation of the user's rule -- flagged in the
    final report."""
    cand = _carry_candidate(day)
    if cand is None:
        return run_baseline(day), False
    cand_idx, cand_dir = cand
    open_idx = _open_idx(day)
    hynix_bars_3m, flags = day["hynix_bars_3m"], day["flags"]
    if open_idx is None or open_idx < 1:
        return run_baseline(day), False
    if _has_opposite_flag_at(flags, open_idx, cand_dir):
        return run_baseline(day), False
    flag_bar_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[open_idx - 1]).to_pydatetime()
    decision_at = pd.Timestamp(hynix_bars_3m["datetime"].iloc[open_idx]).to_pydatetime()
    decision = twf.evaluate_time_window_entry(
        hynix_bars_3m.iloc[: open_idx + 1], cand_dir, flag_bar_dt, decision_at,
        position_direction=None, morning_entry_count=0, afternoon_entry_count=0,
    )
    if not decision.approved:
        return run_baseline(day), False
    vetoed, _reason = twf.evaluate_tw2_extra_vetoes(hynix_bars_3m.iloc[: open_idx + 1], cand_dir, flag_bar_dt, decision_at)
    if vetoed:
        return run_baseline(day), False
    target = _target(cand_dir)
    fill = _first_close_at_or_after(day["etf_1m"].get(target), decision_at)
    if fill is None:
        return run_baseline(day), False
    trade = base.Trade(
        trading_date=day["date"], direction=cand_dir.value, flag_time=hynix_bars_3m["datetime"].iloc[cand_idx].isoformat(),
        entry_time=decision_at.isoformat(), entry_symbol=target, entry_price=fill,
        window="PRE15_CARRY_QUALITY", quality_score=decision.score, flag_seq_of_day=1,
    )
    position = base.OpenPosition(symbol=target, entry_idx=open_idx, entry_price=fill, entry_time=decision_at, session="MORNING", trade=trade)
    trades = _tw2_intraday_loop(day["date"], hynix_bars_3m, flags, day["complete_bar_starts"], day["etf_1m"], open_idx, preseeded_position=position, morning_count0=1)
    return trades, True


# ── metrics ──────────────────────────────────────────────────────────────────
def _metrics(trades: list, n_days: int) -> dict:
    closed = [t for t in trades if t.net_return_pct is not None]
    closed_sorted = sorted(closed, key=lambda t: t.entry_time)
    wins = [t for t in closed if t.net_return_pct > 0]
    total = sum(t.net_return_pct for t in closed)
    compounded = 1.0
    equity = peak = max_dd = 0.0
    for t in closed_sorted:
        compounded *= (1.0 + t.net_return_pct / 100.0)
        equity += t.net_return_pct
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "entries": len(closed), "wins": len(wins),
        "win_rate_pct": round(len(wins) / len(closed) * 100.0, 1) if closed else 0.0,
        "total_pct": round(total, 3), "compound_pct": round((compounded - 1.0) * 100.0, 3),
        "avg_pct_per_trade": round(total / len(closed), 3) if closed else 0.0,
        "mdd_pct": round(max_dd, 3),
        "n_days": n_days,
    }


def main() -> int:
    cache, notes = clk.prepare_cache(DATES)
    print(f"Dates targeted: {len(DATES)} ({DATES[0]}..{DATES[-1]})")
    print(f"Days loaded: {len(cache)}/{len(DATES)}")
    dropped_notes = [n for n in notes if "skipped" in n]
    if dropped_notes:
        print(f"Skipped days ({len(dropped_notes)}): {dropped_notes}")
    incomplete_notes = [n for n in notes if "INCOMPLETE_BAR" in n]
    print(f"Incomplete-bar drops across all days: {len(incomplete_notes)}\n")

    variants = {
        "BASELINE": lambda d: (run_baseline(d), False),
        "PRE15": run_pre15,
        "PRE15+TW": run_pre15_tw,
        "PRE15+quality": run_pre15_quality,
    }

    all_trades = {name: [] for name in variants}
    applied_days = {name: 0 for name in variants}
    added_trades = {name: [] for name in variants}  # trades whose window tag starts with PRE15_CARRY

    for day in cache:
        base_trades = run_baseline(day)
        all_trades["BASELINE"].extend(base_trades)
        for name in ("PRE15", "PRE15+TW", "PRE15+quality"):
            trades, applied = variants[name](day)
            all_trades[name].extend(trades)
            if applied:
                applied_days[name] += 1
                added_trades[name].extend([t for t in trades if isinstance(t.window, str) and t.window.startswith("PRE15_CARRY")])

    n_days = len(cache)
    print("=== FULL-STRATEGY METRICS (TW2 baseline + premarket layer folded in) ===")
    header = f"{'variant':<15}{'days_appl':>10}{'entries':>9}{'wins':>6}{'win%':>7}{'total%':>9}{'compound%':>11}{'avg/trd%':>10}{'MDD%':>8}"
    print(header)
    full_metrics = {}
    for name in variants:
        m = _metrics(all_trades[name], n_days)
        full_metrics[name] = m
        print(f"{name:<15}{applied_days[name]:>10}{m['entries']:>9}{m['wins']:>6}{m['win_rate_pct']:>7}{m['total_pct']:>9}{m['compound_pct']:>11}{m['avg_pct_per_trade']:>10}{m['mdd_pct']:>8}")

    print("\n=== ADDED-TRADES-ONLY METRICS (the premarket carry-over trades in isolation) ===")
    print(header)
    for name in ("PRE15", "PRE15+TW", "PRE15+quality"):
        m = _metrics(added_trades[name], n_days)
        print(f"{name:<15}{applied_days[name]:>10}{m['entries']:>9}{m['wins']:>6}{m['win_rate_pct']:>7}{m['total_pct']:>9}{m['compound_pct']:>11}{m['avg_pct_per_trade']:>10}{m['mdd_pct']:>8}")

    print("\n=== DELTA vs BASELINE (full-strategy) ===")
    base_m = full_metrics["BASELINE"]
    for name in ("PRE15", "PRE15+TW", "PRE15+quality"):
        m = full_metrics[name]
        print(f"{name:<15} entries {m['entries']-base_m['entries']:+d}  total% {m['total_pct']-base_m['total_pct']:+.3f}  "
              f"compound% {m['compound_pct']-base_m['compound_pct']:+.3f}  MDD% {m['mdd_pct']-base_m['mdd_pct']:+.3f}")

    # ── 08:30-08:44 vs 08:45-08:59 supplementary comparison (curiosity only,
    # NOT part of the PRE15 family -- these are NOT gated through TW2 at all,
    # just "if we entered immediately at that bucket's own last flag's
    # direction at 09:00 open, would it have won") ─────────────────────────
    def _bucket_carry_trades(win):
        trades = []
        for day in cache:
            hynix_bars_3m, flags = day["hynix_bars_3m"], day["flags"]
            open_idx = _open_idx(day)
            if open_idx is None:
                continue
            bucket = _premarket_flags_in(flags, hynix_bars_3m, *win)
            if not bucket:
                continue
            cand_idx, cand_dir, _t = bucket[-1]
            cancelled = False
            for idx, direction in flags:
                if idx <= cand_idx:
                    continue
                t = pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]).astimezone(KST).time()
                if t >= dtime(9, 0):
                    break
                if direction != cand_dir:
                    cancelled = True
                    break
            if cancelled:
                continue
            target = _target(cand_dir)
            entry_time = pd.Timestamp(hynix_bars_3m["datetime"].iloc[open_idx]).to_pydatetime()
            fill = _first_close_at_or_after(day["etf_1m"].get(target), entry_time)
            if fill is None:
                continue
            # simple fixed-horizon mark: same-day 09:57 price (mirrors the
            # PRE15 holding pattern loosely) -- curiosity only, not run
            # through the real TP/SL ladder.
            exit_time = entry_time + timedelta(minutes=57)
            exit_price = clk.nearest_close(day["etf_1m"].get(target), exit_time) or fill
            net = base._net_pct(target, fill, exit_price)
            trades.append(type("T", (), {"net_return_pct": net, "entry_time": entry_time})())
        return trades

    print("\n=== SUPPLEMENTARY: 08:30-08:44 vs 08:45-08:59, naive same-direction 09:00 carry (NOT part of PRE15/PRE15+TW/PRE15+quality; curiosity only) ===")
    for label, win in (("08:30-08:44", WIN_08_30_44), ("08:45-08:59", WIN_08_45_59)):
        trades = _bucket_carry_trades(win)
        m = _metrics(trades, n_days)
        print(f"{label:<15} entries {m['entries']:>4}  win% {m['win_rate_pct']:>6}  total% {m['total_pct']:>8}  avg/trd% {m['avg_pct_per_trade']:>8}")

    if notes:
        (OUT_DIR / "notes.txt").write_text("\n".join(notes), encoding="utf-8")
        print(f"\n(full data-quality notes written to {OUT_DIR / 'notes.txt'})")

    for name, trades in all_trades.items():
        rows = [
            {
                "date": t.trading_date, "direction": t.direction, "window": t.window, "flag_time": t.flag_time,
                "entry_time": t.entry_time, "entry_symbol": t.entry_symbol, "entry_price": t.entry_price,
                "exit_time": t.exit_time, "exit_price": t.exit_price, "exit_reason": t.exit_reason,
                "net_return_pct": t.net_return_pct, "quality_score": t.quality_score,
            }
            for t in trades
        ]
        pd.DataFrame(rows).to_csv(OUT_DIR / f"trades_{name.replace('+', '_').replace(' ', '_')}.csv", index=False)
    print(f"\nPer-variant trade CSVs written under {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
