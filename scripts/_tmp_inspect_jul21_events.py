"""Inspect why Jul21 full replay stops after morning entries."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.trading.range_weighted_optimize import load_optimized_config  # noqa: E402
from scripts.replay_today_weighted_range import run_replay  # noqa: E402


def main() -> int:
    load_optimized_config()
    cache = ROOT / "data" / "cache"
    h = pd.read_csv(cache / "replay_20260721_hynix_1m.csv", parse_dates=["datetime"])
    long_df = pd.read_csv(cache / "replay_20260721_long_1m.csv", parse_dates=["datetime"])
    inv = pd.read_csv(cache / "replay_20260721_inverse_1m.csv", parse_dates=["datetime"])
    result = run_replay(h, long_df, inv)
    print(f"entries={result['entries']} RT={result['round_trips']} blocked_probe={result['blocked_probe']}")
    print(f"blocked_reversal_repeat={result.get('blocked_reversal_repeat')} duplicate={result.get('duplicate_episode')}")
    print("ALL events:")
    for e in result["events"]:
        print(
            f"  {e['time']} {e['action']} {e.get('symbol')} path={e.get('path')} "
            f"reason={e.get('reason')} net_ret={e.get('net_ret_pct')} evidence={e.get('evidence')}"
        )
    sells = [e for e in result["events"] if e["action"] == "매도"]
    buys = [e for e in result["events"] if e["action"] == "매수"]
    print(f"buys={len(buys)} sells={len(sells)}")
    if buys and not sells:
        print("WARNING: bought but never sold — position held all day blocks afternoon entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
