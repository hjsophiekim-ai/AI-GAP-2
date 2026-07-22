"""Analyze jul21 episode dump CSV."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rows = list(csv.DictReader((ROOT / "data/state/jul21_episode_minute_dump.csv").open(encoding="utf-8")))
summary = json.loads((ROOT / "data/state/jul21_episode_trace_summary.json").read_text(encoding="utf-8"))

print("after buys", summary["after_buys"])
print("before buys", summary["before_buys"])
print("rows", len(rows))

print("\n--- all non-empty episode_change_reason ---")
for r in rows:
    if r["episode_change_reason"]:
        print(
            r["timestamp"][11:19],
            r["episode_change_reason"],
            "cur",
            r["current_episode"],
            "cand",
            r["candidate_episode"],
            "broken",
            r["existing_structure_broken"],
            "vwap_r",
            r["vwap_cross_reclaim"],
            "swing",
            r["swing_break"],
            "5/10",
            f"{r['etf_5s']}/{r['etf_10s']}",
            "above",
            r["above_vwap"],
            "prev",
            r["prev_above_vwap"],
            "opp",
            r["opposite_confirmed"],
            "block",
            r["final_block_reason"],
        )

print("\n--- 13:20-14:40 minute rows ---")
for r in rows:
    if r["timestamp"].endswith(":00") and "13:20" <= r["timestamp"][11:16] <= "14:40":
        print(
            f"{r['timestamp'][11:19]} live={r['live_dir']:4} cur={str(r['current_episode']):4} "
            f"cand={str(r['candidate_episode'] or '-'):4} "
            f"swing={r['swing_break'][0]} vwapR={r['vwap_cross_reclaim'][0]} above={r['above_vwap'][0]} "
            f"5/10={r['etf_5s']}/{r['etf_10s']} broken={r['existing_structure_broken'][0]} "
            f"opp={r['opposite_confirmed'][0]} act={str(r['entry_action']):5} "
            f"path={str(r['entry_path'] or '-'):12} evid={r['evidence_score']} "
            f"edge={r['expected_net_edge']} rr={r['reward_risk']} "
            f"block={r['final_block_reason']} order={r['order_placed']}"
        )

print("\n--- 14:18-14:30 ENTER rows (any second) ---")
for r in rows:
    if "14:18" <= r["timestamp"][11:16] <= "14:30" and r["entry_action"] == "ENTER":
        print(
            f"{r['timestamp'][11:19]} cur={r['current_episode']} cand={r['candidate_episode']} "
            f"swing={r['swing_break']} broken={r['existing_structure_broken']} "
            f"vwapR={r['vwap_cross_reclaim']} above={r['above_vwap']} prev={r['prev_above_vwap']} "
            f"5/10={r['etf_5s']}/{r['etf_10s']} opp={r['opposite_confirmed']} "
            f"path={r['entry_path']} evid={r['evidence_score']} edge={r['expected_net_edge']} "
            f"rr={r['reward_risk']} block={r['final_block_reason']} chg={r['episode_change_reason']}"
        )

print("\n--- probe_failed / reversal_probe_done true rows ---")
for r in rows:
    if r["probe_failed"] == "True" or r["reversal_probe_done"] == "True":
        if r["timestamp"].endswith(":00") or r["order_placed"] == "Y":
            print(
                r["timestamp"][11:19],
                "status",
                r["episode_status"],
                "rev",
                r["reversal_probe_done"],
                "entry_done",
                r["entry_done"],
                "ep",
                r["episode_id"],
                "block",
                r["final_block_reason"],
            )
