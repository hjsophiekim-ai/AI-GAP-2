"""Quick Jul21 verification with daily-loss stop disabled for episode-gate visibility."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import hynix_switch_engine as engine
from app.trading.range_weighted_optimize import load_optimized_config
from scripts import replay_today_weighted_range as replay_mod
from scripts.replay_today_weighted_range import run_replay


def summarize(label, result):
    buys = [e for e in result["events"] if e["action"] == "매수"]
    down = [e for e in buys if "12:45" <= e["time"] <= "14:00"]
    rebound = [e for e in buys if "14:10" <= e["time"] <= "15:00"]
    inv = [e for e in buys if e.get("symbol") == "인버스"]
    pf = result["profit_factor_conservative"]
    print(f"\n=== {label} ===")
    print(f"RT={result['round_trips']} entries={result['entries']}")
    print(f"linear PnL={result['net_pnl_krw']:+,.0f} PF={result['profit_factor']}")
    print(f"cons PnL={result['net_pnl_conservative_krw']:+,.0f} PF={pf}")
    print(f"DOWN(12:45-14)= {len(down)} inv={len(inv)} rebound(14:10-15)={len(rebound)}")
    for e in buys:
        print(f"  {e['time']} {e.get('symbol')} {e.get('path')}")


def main():
    load_optimized_config()
    cache = ROOT / "data" / "cache"
    h = pd.read_csv(cache / "replay_20260721_hynix_1m.csv", parse_dates=["datetime"])
    l = pd.read_csv(cache / "replay_20260721_long_1m.csv", parse_dates=["datetime"])
    i = pd.read_csv(cache / "replay_20260721_inverse_1m.csv", parse_dates=["datetime"])
    # Isolate episode gates from daily-loss latch for this check.
    replay_mod.daily_loss_limit_reached = lambda *a, **k: False  # type: ignore
    after = run_replay(h, l, i)
    summarize("AFTER", after)

    def old(**kwargs):
        ed, nd = kwargs.get("existing_direction"), kwargs.get("new_direction")
        if not ed:
            return True
        if ed == nd:
            return False
        if not kwargs.get("live_direction_matches"):
            return False
        dirs = kwargs.get("confirm_dirs") or {}
        if dirs.get(5) != nd or dirs.get(10) != nd:
            return False
        return bool(kwargs.get("existing_structure_broken") or kwargs.get("new_etf_vwap_reclaim"))

    orig = engine.detect_opposite_episode_transition
    engine.detect_opposite_episode_transition = old  # type: ignore
    before = run_replay(h, l, i)
    summarize("BEFORE", before)
    engine.detect_opposite_episode_transition = orig
    rt = after["round_trips"]
    pnl = after["net_pnl_conservative_krw"]
    pf = after["profit_factor_conservative"]
    print("\nTarget cons: RT 6-9, PnL>0, PF>=1.3")
    print(f"Met RT={6 <= rt <= 9} PnL={pnl > 0} PF={(pf is not None and pf >= 1.3)}")


if __name__ == "__main__":
    main()
