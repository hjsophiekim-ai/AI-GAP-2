#!/usr/bin/env python
"""2026-08-17: breakdown of the FINAL (already-locked) strategy's own trades
by direction (LONG/INVERSE) and by session x entry-seq-of-day, across
TRAIN/VAL/OOS, plus a full listing of every OOS trade for loss-pattern
narration. Pure read of the already-produced
data/validation/tw_gate_relaxed_optimization/baseline_vs_final_all_trades.csv
(strategy==FINAL rows) -- no new simulation, no parameter changes.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "data" / "validation" / "tw_gate_relaxed_optimization"
STOP_REASONS = {"TIME_WINDOW_STOP_LOSS", "TIME_WINDOW_AFTER_TP1_STOP", "TIME_WINDOW_TRAILING_STOP",
                "TIME_WINDOW_BREAKEVEN_STOP", "TIME_WINDOW_PROFIT_LOCK_STOP"}
MORNING_WINDOWS = {"W1_MORNING_AGGRESSIVE", "W2_MORNING_SECOND", "W3_MORNING_THIRD_STRICT", "W4_NO_NEW_ENTRY"}


def load_final_trades():
    rows = []
    with (BASE / "baseline_vs_final_all_trades.csv").open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["strategy"] != "FINAL":
                continue
            r["net_return_pct"] = float(r["net_return_pct"])
            r["quality_score"] = int(r["quality_score"])
            r["flag_seq_of_day"] = int(r["flag_seq_of_day"])  # actually entry_seq_of_day
            r["session"] = "MORNING" if r["window"] in MORNING_WINDOWS else "AFTERNOON"
            rows.append(r)
    return rows


def agg(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0}
    wins = [r for r in rows if r["net_return_pct"] > 0]
    losses = [r for r in rows if r["net_return_pct"] <= 0]
    cum = sum(r["net_return_pct"] for r in rows)
    compounded = 1.0
    for r in rows:
        compounded *= (1.0 + r["net_return_pct"] / 100.0)
    gross_win = sum(r["net_return_pct"] for r in wins)
    gross_loss = abs(sum(r["net_return_pct"] for r in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else float("inf"))
    return {
        "n": n, "win_rate_pct": round(len(wins) / n * 100, 2), "cum_pct": round(cum, 3),
        "compounded_pct": round((compounded - 1) * 100, 3),
        "pf": (round(pf, 3) if isinstance(pf, float) and pf != float("inf") else pf),
        "avg_pct": round(cum / n, 4),
    }


if __name__ == "__main__":
    rows = load_final_trades()
    periods = ["TRAIN", "VAL", "OOS"]

    print("=== 방향별 (LONG=UP_RED 레버리지 매수 / INV=DOWN_BLUE 인버스) ===")
    direction_table = {}
    for p in periods:
        direction_table[p] = {}
        for d, label in (("UP_RED", "LONG(레버리지)"), ("DOWN_BLUE", "INV(인버스)")):
            sub = [r for r in rows if r["period"] == p and r["direction"] == d]
            m = agg(sub)
            direction_table[p][label] = m
            print(f"  {p:5s} {label:14s}: {m}")

    print("\n=== 세션 x 당일 진입순번 (1/2/3번째) ===")
    session_seq_table = {}
    for p in periods:
        session_seq_table[p] = {}
        for session in ("MORNING", "AFTERNOON"):
            for seq in (1, 2, 3, 4, 5):
                sub = [r for r in rows if r["period"] == p and r["session"] == session and r["flag_seq_of_day"] == seq]
                if not sub:
                    continue
                m = agg(sub)
                key = f"{session}_entry{seq}"
                session_seq_table[p][key] = m
                print(f"  {p:5s} {key:22s}: {m}")

    print("\n=== OOS 23건 전체 상세 ===")
    oos_rows = sorted([r for r in rows if r["period"] == "OOS"], key=lambda r: r["entry_time"])
    for r in oos_rows:
        tag = "LOSS" if r["net_return_pct"] <= 0 else "WIN "
        dirlabel = "LONG" if r["direction"] == "UP_RED" else "INV "
        print(f"  [{tag}] {r['trading_date']} {r['entry_time'][11:16]}->{r['exit_time'][11:16]} {dirlabel} "
              f"{r['window']:<28s} Q{r['quality_score']} entry#{r['flag_seq_of_day']} "
              f"{r['entry_price']:>9s}->{r['exit_price']:>9s} {r['exit_reason']:<24s} ret={r['net_return_pct']:+.3f}%")

    loss_rows = [r for r in oos_rows if r["net_return_pct"] <= 0]
    print(f"\nOOS 손실 거래 {len(loss_rows)}/{len(oos_rows)}건 유형 집계:")
    from collections import Counter
    print("  방향별:", Counter(("LONG" if r["direction"] == "UP_RED" else "INV") for r in loss_rows))
    print("  세션별:", Counter(r["session"] for r in loss_rows))
    print("  창별:", Counter(r["window"] for r in loss_rows))
    print("  quality별:", Counter(r["quality_score"] for r in loss_rows))
    print("  entry_seq별:", Counter(r["flag_seq_of_day"] for r in loss_rows))
    print("  exit_reason별:", Counter(r["exit_reason"] for r in loss_rows))

    out = {
        "direction_breakdown": direction_table,
        "session_entryseq_breakdown": session_seq_table,
        "oos_all_trades": oos_rows,
    }
    (BASE / "final_strategy_breakdown.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nSaved -> {BASE / 'final_strategy_breakdown.json'}")
