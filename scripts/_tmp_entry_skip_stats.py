"""Instrument entry-gate skip reasons inside run_replay for Jul21."""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.trading.range_weighted_optimize import load_optimized_config  # noqa: E402
import scripts.replay_today_weighted_range as replay  # noqa: E402

# Monkeypatch by rewriting the entry block via a wrapper around run_replay internals
# is hard; instead duplicate the skip counters by patching daily_loss and detect.


def main() -> int:
    load_optimized_config()
    cache = ROOT / "data" / "cache"
    h = pd.read_csv(cache / "replay_20260721_hynix_1m.csv", parse_dates=["datetime"])
    long_df = pd.read_csv(cache / "replay_20260721_long_1m.csv", parse_dates=["datetime"])
    inv = pd.read_csv(cache / "replay_20260721_inverse_1m.csv", parse_dates=["datetime"])

    # Patch run_replay source entry section by wrapping functions it calls and
    # also patching module-level daily_loss to count.
    skip = Counter()
    orig_daily = replay.daily_loss_limit_reached

    def daily_wrap(pnl, cash, cfg):
        hit = orig_daily(pnl, cash, cfg)
        if hit:
            skip["daily_loss_true"] += 1
        return hit

    replay.daily_loss_limit_reached = daily_wrap  # type: ignore

    # Wrap evaluate to mark ENTER, then wrap allows to see reach rate
    from app.services import hynix_switch_engine as engine

    orig_eval = engine.evaluate_range_weighted_entry
    orig_allows = engine.range_episode_allows_entry
    orig_detect = engine.detect_opposite_episode_transition

    state = {"enter": 0, "last_enter": False}

    def eval_wrap(**kwargs):
        r = orig_eval(**kwargs)
        state["last_enter"] = r.get("action") == "ENTER"
        if state["last_enter"]:
            state["enter"] += 1
        return r

    def detect_wrap(**kwargs):
        ok = orig_detect(**kwargs)
        existing = kwargs.get("existing_direction")
        new = kwargs.get("new_direction")
        if state["last_enter"] and existing and new and existing != new:
            skip["enter_while_opposite_candidate"] += 1
            skip["enter_opp_confirmed" if ok else "enter_opp_rejected"] += 1
        return ok

    def allows_wrap(cont, **kwargs):
        skip["reached_allows"] += 1
        allows, reason = orig_allows(cont, **kwargs)
        skip["allows_ok" if allows else f"allows_block:{reason}"] += 1
        return allows, reason

    engine.evaluate_range_weighted_entry = eval_wrap  # type: ignore
    engine.detect_opposite_episode_transition = detect_wrap  # type: ignore
    engine.range_episode_allows_entry = allows_wrap  # type: ignore
    try:
        result = replay.run_replay(h, long_df, inv)
    finally:
        engine.evaluate_range_weighted_entry = orig_eval  # type: ignore
        engine.detect_opposite_episode_transition = orig_detect  # type: ignore
        engine.range_episode_allows_entry = orig_allows  # type: ignore
        replay.daily_loss_limit_reached = orig_daily  # type: ignore

    print(f"entries={result['entries']} blocked_probe={result['blocked_probe']}")
    print(f"evaluate_ENTER={state['enter']}")
    print("skip/gate stats:", dict(skip))
    # Approximate: ENTER that never reached allows
    # Note: detect runs before evaluate in loop, so enter_while_opposite is imperfect.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
