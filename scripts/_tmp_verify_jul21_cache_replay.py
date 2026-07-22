"""Verify Jul 21 weighted RANGE replay from cached Naver+shaped CSVs."""
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
    print(f"bars: h={len(h)} long={len(long_df)} inv={len(inv)}")
    result = run_replay(h, long_df, inv)
    buys = [e for e in result["events"] if e["action"] == "매수"]
    down = [e for e in buys if "13:00" <= e["time"] <= "14:20"]
    rebound = [e for e in buys if "14:10" <= e["time"] <= "15:20"]
    inv_buys = [e for e in buys if e.get("symbol") == "인버스"]
    pf_c = result.get("profit_factor_conservative")
    pf_l = result.get("profit_factor")
    print("=== AFTER (raw dirs + OR + PROBE_FAILED) ===")
    print(f"entries/RT: {result['entries']}/{result['round_trips']}")
    print(f"linear PnL: {result['net_pnl_krw']:+,.0f}")
    print(f"conservative PnL: {result['net_pnl_conservative_krw']:+,.0f}")
    print(f"linear PF: {pf_l if pf_l is None else round(float(pf_l), 2)}")
    print(f"conservative PF: {pf_c if pf_c is None else round(float(pf_c), 2)}")
    print(
        f"DOWN afternoon buys: {len(down)}  inv total: {len(inv_buys)}  rebound: {len(rebound)}"
    )
    print(f"day_regime: {result.get('day_regime')}  blocked_probe: {result.get('blocked_probe')}")
    for e in buys:
        print(
            f"  {e['time']} {e.get('symbol')} {e.get('path')} evidence={e.get('evidence')}"
        )
    print("Target: RT 6-9, Net PnL > 0, PF >= 1.3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
