"""Print conservative realized PnL path on Jul21 replay."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.trading.range_weighted_optimize import (  # noqa: E402
    get_range_weighted_config,
    load_optimized_config,
    daily_loss_limit_reached,
)
from scripts import replay_today_weighted_range as replay  # noqa: E402


def main() -> int:
    load_optimized_config()
    cfg = get_range_weighted_config()
    print(f"daily_loss_limit_pct={cfg.daily_loss_limit_pct}")
    cache = ROOT / "data" / "cache"
    h = pd.read_csv(cache / "replay_20260721_hynix_1m.csv", parse_dates=["datetime"])
    long_df = pd.read_csv(cache / "replay_20260721_long_1m.csv", parse_dates=["datetime"])
    inv = pd.read_csv(cache / "replay_20260721_inverse_1m.csv", parse_dates=["datetime"])
    result = replay.run_replay(h, long_df, inv)
    print("trades_conservative:")
    for t in result.get("trades_conservative") or []:
        print(f"  {t}")
    print("trades optimistic sells:")
    for t in result.get("trades") or []:
        if t.get("side") == "SELL":
            print(f"  {t}")
    print(f"net_pnl_conservative_krw={result['net_pnl_conservative_krw']}")
    print(f"net_pnl_krw={result['net_pnl_krw']}")
    # Reconstruct realized from conservative sells
    realized = sum(t["net_pnl"] for t in (result.get("trades_conservative") or []) if t.get("side") == "SELL")
    print(f"sum conservative sell net={realized}")
    print(f"daily_loss at that realized? {daily_loss_limit_reached(realized, replay.INITIAL_CASH, cfg)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
