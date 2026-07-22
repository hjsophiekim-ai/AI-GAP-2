"""Count why ENTER ticks never reach allows_entry in run_replay."""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import hynix_switch_engine as engine  # noqa: E402
from app.trading.range_weighted_optimize import load_optimized_config  # noqa: E402
from scripts import replay_today_weighted_range as replay  # noqa: E402

# Patch run_replay loop by wrapping detect + evaluating skip reasons via a fork
# of the entry gate counters injected into engine helpers called from replay.


def main() -> int:
    load_optimized_config()
    cache = ROOT / "data" / "cache"
    h = pd.read_csv(cache / "replay_20260721_hynix_1m.csv", parse_dates=["datetime"])
    long_df = pd.read_csv(cache / "replay_20260721_long_1m.csv", parse_dates=["datetime"])
    inv = pd.read_csv(cache / "replay_20260721_inverse_1m.csv", parse_dates=["datetime"])

    orig_detect = engine.detect_opposite_episode_transition
    detect_stats = Counter()
    detect_false_samples = []

    def detect_wrapped(**kwargs):
        ok = orig_detect(**kwargs)
        existing = kwargs.get("existing_direction")
        new = kwargs.get("new_direction")
        if existing and new and existing != new:
            detect_stats["candidate"] += 1
            detect_stats["confirmed" if ok else "rejected"] += 1
            if ok:
                if kwargs.get("existing_structure_broken"):
                    detect_stats["via_existing_broken"] += 1
                elif kwargs.get("new_swing_breakout"):
                    detect_stats["via_new_swing"] += 1
                else:
                    detect_stats["via_vwap_path"] += 1
            elif len(detect_false_samples) < 12:
                detect_false_samples.append({
                    "ex": existing,
                    "new": new,
                    "broken": kwargs.get("existing_structure_broken"),
                    "swing": kwargs.get("new_swing_breakout"),
                    "vwap_r": kwargs.get("new_etf_vwap_reclaim"),
                    "vwap_b": kwargs.get("new_etf_vwap_break"),
                    "dirs": f"{(kwargs.get('confirm_dirs') or {}).get(5)}/{(kwargs.get('confirm_dirs') or {}).get(10)}",
                })
        return ok

    engine.detect_opposite_episode_transition = detect_wrapped  # type: ignore
    try:
        result = replay.run_replay(h, long_df, inv)
    finally:
        engine.detect_opposite_episode_transition = orig_detect  # type: ignore

    print(f"entries={result['entries']} blocked_probe={result['blocked_probe']}")
    print("opposite detect stats:", dict(detect_stats))
    print("rejected samples:")
    for s in detect_false_samples:
        print(f"  {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
