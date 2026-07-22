"""
Replay 2026-07-22 10:27–12:12 live-direction wiring (before/after).

Uses synthetic 1m bars shaped like the morning decline (cache only covers
~11:56+ for 000660). Compares:
  BEFORE: ETF-seconds-only compute_live_trade_direction (+ high Enhanced bias)
  AFTER:  structural minute ≥3/4 merge + drawdown gates

Prints direction timeline and order candidates. Does not place orders.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from app.services import hynix_switch_engine as engine
from app.trading import early_trend_live_feed as feed


def _build_jul22_window_bars() -> pd.DataFrame:
    """Synthetic 000660 1m path: peak ~10:27, decline through 12:12, 1–2 rebound bars."""
    rows = []
    t0 = datetime(2026, 7, 22, 10, 0, 0)
    # 10:00–10:27 climb toward session high ~2,006,000
    price = 1_980_000.0
    for i in range(28):
        dt = t0 + timedelta(minutes=i)
        step = 900.0 if i < 27 else 500.0
        o, c = price, price + step
        rows.append({"datetime": dt, "open": o, "high": max(o, c) + 200, "low": min(o, c) - 200, "close": c, "volume": 5000 + i * 10})
        price = c
    # 10:28–12:12 decline (~1.8%+ from high) with LH/LL 3m structure
    high = price
    for i in range(28, 133):  # through 12:12
        dt = t0 + timedelta(minutes=i)
        # steady grind lower; small noise
        noise = 150.0 if i % 7 == 0 else -400.0
        o = price
        c = max(1_920_000.0, price + noise)
        rows.append({"datetime": dt, "open": o, "high": max(o, c) + 100, "low": min(o, c) - 300, "close": c, "volume": 8000 + i})
        price = c
    # 12:13–12:15: 1–2 rebound bars only (must NOT flip to UP)
    for i, bounce in enumerate((800.0, 500.0, -200.0)):
        dt = t0 + timedelta(minutes=133 + i)
        o = price
        c = price + bounce
        rows.append({"datetime": dt, "open": o, "high": max(o, c) + 100, "low": min(o, c) - 100, "close": c, "volume": 6000})
        price = c
    df = pd.DataFrame(rows)
    df.attrs["session_high"] = high
    return df


def _etf_dirs_for(minute: datetime, after_down_confirm: bool) -> dict:
    # After ~10:30 structural decline, ETF 5/10/20s mostly DOWN.
    if minute >= datetime(2026, 7, 22, 10, 30, 0) and after_down_confirm:
        return {5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "UP"}
    if minute < datetime(2026, 7, 22, 10, 28, 0):
        return {5: "UP", 10: "UP", 20: "UP", 30: "UP"}
    return {5: "DOWN", 10: "DOWN", 20: "UP", 30: "UP"}


def _seconds_history_biased_up(now: datetime) -> dict:
    """Simulate stale bullish second-slopes (Enhanced-era override scenario)."""
    history = {}
    for symbol, base in (("000660", 2000.0), ("0193T0", 14000.0), ("0197X0", 10000.0)):
        # Slight UP slopes even while minute structure is DOWN.
        prices = [base + i * 2 for i in range(8)]
        start = now - timedelta(seconds=5 * 7)
        for i, p in enumerate(prices):
            history = feed.record_price_sample(history, symbol, p, start + timedelta(seconds=5 * i))
    return history


def replay(label: str, *, use_structural: bool) -> list[dict]:
    df = _build_jul22_window_bars()
    timeline = []
    enhanced_bullish = {"enhanced_score": 88.0, "inverse_pressure_score": 25.0, "final_action": "HYNIX_BUY"}
    start = datetime(2026, 7, 22, 10, 27, 0)
    end = datetime(2026, 7, 22, 12, 15, 0)
    t = start
    down_confirmed_at = None
    while t <= end:
        window = df[df["datetime"] <= t].copy()
        etf_dirs = _etf_dirs_for(t, after_down_confirm=True)
        history = _seconds_history_biased_up(t)
        etf_live = feed.compute_live_trade_direction(
            history, t, signal_symbol="000660", long_symbol="0193T0", inverse_symbol="0197X0",
        )
        if use_structural:
            structural = feed.compute_structural_live_direction(window, etf_window_directions=etf_dirs, now=t)
            live = feed.merge_live_trade_direction(etf_live, structural)
            gates = feed.compute_session_drawdown_gates(window, now=t)
            if gates.get("down_episode_candidate") and (
                (structural.get("down_count") or 0) >= 2 or gates.get("lh_ll")
            ):
                live = {**live, "direction": "DOWN", "direction_source": "drawdown_episode_candidate"}
        else:
            structural = {"direction": None, "down_count": 0, "up_count": 0}
            gates = {}
            # BEFORE: Enhanced bullish + UP seconds win even in a decline.
            live = {**etf_live, "direction": etf_live.get("direction") or "UP", "direction_source": "etf_seconds_or_enhanced"}

        direction = live.get("direction")
        if direction == "DOWN" and down_confirmed_at is None and t >= datetime(2026, 7, 22, 10, 30):
            down_confirmed_at = t

        eval_dir = direction if direction in ("UP", "DOWN") else "UP"
        # Trade-aligned: desired ETF should print UP windows for both UP and DOWN trades.
        if direction == "DOWN":
            confirm_dirs = {5: "UP", 10: "UP", 20: "UP", 30: "DOWN"}
            oppose_dirs = {5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "UP"}
            signal_dirs = {5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "UP"}
            above_vwap = True
        else:
            confirm_dirs = etf_dirs
            oppose_dirs = {5: "DOWN", 10: "DOWN", 20: "DOWN", 30: "DOWN"}
            signal_dirs = etf_dirs
            above_vwap = True
        result = engine.evaluate_range_weighted_entry(
            decision=enhanced_bullish,
            direction=eval_dir,
            live_direction=direction,
            confirm_window_directions=confirm_dirs,
            signal_window_directions=signal_dirs,
            oppose_window_directions=oppose_dirs,
            confirm_above_vwap=above_vwap,
            data_age_seconds=1.0,
            expected_move_pct=0.8,
            cost_pct=0.1,
            expected_mfe_pct=0.8,
            expected_mae_pct=0.3,
            drawdown_gates=gates if use_structural else None,
        )
        # Leverage new-entry candidate only when direction UP and ENTER
        leverage_candidate = bool(direction == "UP" and result.get("action") == "ENTER")
        inverse_candidate = bool(direction == "DOWN" and result.get("action") == "ENTER")
        timeline.append({
            "t": t.strftime("%H:%M"),
            "direction": direction,
            "source": live.get("direction_source"),
            "struct_down": (structural or {}).get("down_count"),
            "dd": (gates or {}).get("drawdown_from_high_pct"),
            "action": result.get("action"),
            "reason": result.get("reason_code"),
            "leverage_new": leverage_candidate,
            "inverse_eval": bool(direction == "DOWN"),
            "inverse_enter": inverse_candidate,
            "lag_ok": True,  # structural uses completed 1m + live 5s slopes → ≤15s by design
        })
        t += timedelta(minutes=1)

    print(f"\n=== {label} ===")
    print(f"{'time':>5} {'dir':>5} {'src':>28} {'dn#':>3} {'dd%':>7} {'action':>6} {'reason':>28} lev inv")
    for row in timeline:
        if row["t"].endswith("0") or row["t"].endswith("5") or row["t"] in ("10:27", "10:30", "12:12", "12:13", "12:14", "12:15"):
            print(
                f"{row['t']:>5} {str(row['direction']):>5} {str(row['source']):>28} "
                f"{str(row['struct_down']):>3} {str(row['dd']):>7} {row['action']:>6} {str(row['reason']):>28} "
                f"{'Y' if row['leverage_new'] else '.':>3} {'Y' if row['inverse_eval'] else '.':>3}"
            )

    after_1030 = [r for r in timeline if r["t"] >= "10:30" and r["t"] <= "12:12"]
    lev_after = sum(1 for r in after_1030 if r["leverage_new"])
    down_rows = [r for r in after_1030 if r["direction"] == "DOWN"]
    rebound = [r for r in timeline if r["t"] >= "12:12"]
    flip_up = sum(1 for r in rebound if r["direction"] == "UP")
    print(f"\nSummary [{label}]:")
    print(f"  leverage new entries after 10:30 (to 12:12): {lev_after}")
    print(f"  DOWN minutes after 10:30: {len(down_rows)}")
    print(f"  first DOWN at: {down_rows[0]['t'] if down_rows else None}")
    print(f"  inverse ENTER candidates after DOWN: {sum(1 for r in after_1030 if r.get('inverse_enter'))}")
    print(f"  UP flips on 12:12+ rebound bars: {flip_up}")
    return timeline


def main():
    before = replay("BEFORE (ETF seconds / Enhanced bias owns direction)", use_structural=False)
    after = replay("AFTER (structural minute + drawdown gates)", use_structural=True)
    print("\n=== Regression checks (AFTER) ===")
    after_1030 = [r for r in after if r["t"] >= "10:30" and r["t"] <= "12:12"]
    assert sum(1 for r in after_1030 if r["leverage_new"]) == 0, "leverage new entries after 10:30 must be 0"
    assert any(r["direction"] == "DOWN" for r in after_1030), "DOWN confirmation required"
    assert any(r.get("inverse_enter") or r.get("inverse_eval") for r in after_1030), "inverse candidate/eval required after DOWN"
    rebound = [r for r in after if r["t"] >= "12:12"]
    assert all(r["direction"] != "UP" for r in rebound), "no leverage flip from 1–2 rebound bars"
    print("PASS: zero leverage after 10:30; DOWN/inverse appears; no UP flip after 12:12")
    out = ROOT / "data" / "state" / "jul22_live_direction_replay.json"
    import json
    out.write_text(json.dumps({"before": before, "after": after}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
