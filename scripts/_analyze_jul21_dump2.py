"""Extra slices from jul21 dump for report."""
from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rows = list(csv.DictReader((ROOT / "data/state/jul21_episode_minute_dump.csv").open(encoding="utf-8")))

print("=== DOWN live minutes 13:22-14:17 ===")
for r in rows:
    if not ("13:22" <= r["timestamp"][11:16] <= "14:17"):
        continue
    if r["live_dir"] != "DOWN":
        continue
    if not r["timestamp"].endswith(":00"):
        continue
    print(
        f"{r['timestamp'][11:19]} cur={r['current_episode']} ep={r['episode_id']} "
        f"entry_done={r['entry_done']} status={r['episode_status']} "
        f"swing={r['swing_break']} above={r['above_vwap']} vwap={r['vwap']} px={r['etf_price']} "
        f"5/10={r['etf_5s']}/{r['etf_10s']} evid={r['evidence_score']} "
        f"edge={r['expected_net_edge']} rr={r['reward_risk']} "
        f"act={r['entry_action']} block={r['final_block_reason']}"
    )

print("\n=== First UP ENTER after 14:18 with full chain ===")
for r in rows:
    if r["timestamp"][11:16] >= "14:18" and r["entry_action"] == "ENTER":
        print(dict(r))
        break

print("\n=== Count live_dir in 13:22-14:17 (:00 only) ===")
from collections import Counter
c = Counter(
    r["live_dir"]
    for r in rows
    if r["timestamp"].endswith(":00") and "13:22" <= r["timestamp"][11:16] <= "14:17"
)
print(c)

print("\n=== Count block reasons when live=DOWN vs already on DOWN ep ===")
c2 = Counter()
for r in rows:
    if "13:22" <= r["timestamp"][11:16] <= "14:17" and r["live_dir"] == "DOWN":
        c2[r["final_block_reason"]] += 1
print(c2)

print("\n=== Inverse price path 13:20-14:20 from cache ===")
import pandas as pd
inv = pd.read_csv(ROOT / "data/cache/replay_20260721_inverse_1m.csv", parse_dates=["datetime"])
sub = inv[(inv.datetime >= "2026-07-21 13:20") & (inv.datetime <= "2026-07-21 14:20")]
print(sub[["datetime", "open", "high", "low", "close"]].to_string(index=False))
