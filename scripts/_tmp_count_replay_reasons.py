"""Count evaluate reason codes in actual run_replay path for Jul21."""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import hynix_switch_engine as engine  # noqa: E402
from app.trading.range_weighted_optimize import load_optimized_config  # noqa: E402
from scripts import replay_today_weighted_range as replay  # noqa: E402

orig = engine.evaluate_range_weighted_entry
reasons_after_0910: Counter = Counter()
enter_after_0910 = 0
allows_calls: Counter = Counter()


def wrapped(**kwargs):
    global enter_after_0910
    result = orig(**kwargs)
    # Caller doesn't pass now; we can't see ts here easily. Count all.
    reasons_after_0910[str(result.get("reason_code") or result.get("action"))] += 1
    if result.get("action") == "ENTER":
        enter_after_0910 += 1
    return result


def wrap_allows(cont, **kwargs):
    allows, reason = engine.range_episode_allows_entry.__wrapped__(cont, **kwargs) if hasattr(engine.range_episode_allows_entry, "__wrapped__") else engine.range_episode_allows_entry(cont, **kwargs)
    # Avoid recursion — call original directly below
    return allows, reason


def main() -> int:
    load_optimized_config()
    cache = ROOT / "data" / "cache"
    h = pd.read_csv(cache / "replay_20260721_hynix_1m.csv", parse_dates=["datetime"])
    long_df = pd.read_csv(cache / "replay_20260721_long_1m.csv", parse_dates=["datetime"])
    inv = pd.read_csv(cache / "replay_20260721_inverse_1m.csv", parse_dates=["datetime"])

    orig_allows = engine.range_episode_allows_entry
    allow_stats: Counter = Counter()

    def allows_wrapped(cont, **kwargs):
        allows, reason = orig_allows(cont, **kwargs)
        key = "ALLOW" if allows else f"BLOCK:{reason}"
        allow_stats[key] += 1
        if not allows and cont.get("direction"):
            allow_stats[f"ep={cont.get('direction')}|status={cont.get('episode_status')}|done={cont.get('entry_done')}|await={cont.get('awaiting_structural_reentry')}|path={kwargs.get('entry_path')}"] += 1
        return allows, reason

    engine.evaluate_range_weighted_entry = wrapped  # type: ignore
    engine.range_episode_allows_entry = allows_wrapped  # type: ignore
    try:
        result = replay.run_replay(h, long_df, inv)
    finally:
        engine.evaluate_range_weighted_entry = orig  # type: ignore
        engine.range_episode_allows_entry = orig_allows  # type: ignore

    print(f"entries={result['entries']} blocked_probe={result['blocked_probe']}")
    print(f"evaluate ENTER count={enter_after_0910}")
    print("top evaluate reasons:")
    for k, v in reasons_after_0910.most_common(15):
        print(f"  {k}: {v}")
    print("allows_entry stats:")
    for k, v in allow_stats.most_common(20):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
