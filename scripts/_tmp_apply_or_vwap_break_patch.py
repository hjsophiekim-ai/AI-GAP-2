"""One-shot patch: restore new_etf_vwap_break OR path + call-site wiring."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "app" / "services" / "hynix_switch_engine.py"
REPLAY = ROOT / "scripts" / "replay_today_weighted_range.py"
TESTS = ROOT / "tests" / "test_hynix_switch_engine.py"

ENGINE_FN = '''def detect_opposite_episode_transition(
    *,
    existing_direction: str | None,
    new_direction: str,
    live_direction_matches: bool,
    confirm_dirs: dict,
    existing_structure_broken: bool,
    new_etf_vwap_reclaim: bool,
    new_etf_vwap_break: bool = False,
    new_swing_breakout: bool = False,
) -> bool:
    """opposite episode transition: OR of swing structure vs VWAP reclaim/break+5/10.

    1) swing structure breakout / structure broken against existing direction
       (existing structure break OR new-direction swing breakout; no 5/10 required)
    2) opposite ETF VWAP reclaim or VWAP break (aligned side) + ETF 5/10 confirm
       (5s alone insufficient)
    """
    if not existing_direction:
        return True
    if existing_direction == new_direction:
        return False
    if not live_direction_matches:
        return False
    if existing_structure_broken or new_swing_breakout:
        return True
    dirs_5_10_aligned = (
        confirm_dirs.get(5) == new_direction and confirm_dirs.get(10) == new_direction
    )
    vwap_ok = bool(new_etf_vwap_reclaim or new_etf_vwap_break)
    return bool(vwap_ok and dirs_5_10_aligned)
'''


def replace_function(text: str, func_name: str, new_fn: str) -> str:
    start = text.index(f"def {func_name}(")
    # next top-level def after this one
    next_def = text.index("\ndef ", start + 1)
    return text[:start] + new_fn + text[next_def + 1 :]


def ensure_call_kwargs(text: str, marker: str, kwargs: dict[str, str]) -> str:
    """Ensure kwargs exist inside the detect_opposite_episode_transition( call after marker context."""
    idx = 0
    while True:
        call = text.find("detect_opposite_episode_transition(", idx)
        if call < 0:
            break
        end = text.find(")", call)
        block = text[call : end + 1]
        changed = block
        for key, value in kwargs.items():
            if f"{key}=" in changed:
                # replace existing assignment loosely
                import re

                changed = re.sub(
                    rf"{key}\s*=\s*[^,\n)]+",
                    f"{key}={value}",
                    changed,
                    count=1,
                )
            else:
                # insert before closing paren
                changed = changed[:-1].rstrip()
                if not changed.endswith(","):
                    changed += ","
                changed += f"\n                {key}={value},\n            )"
        text = text[:call] + changed + text[end + 1 :]
        idx = call + len(changed)
    return text


def main() -> None:
    eng = ENGINE.read_text(encoding="utf-8")
    eng = replace_function(eng, "detect_opposite_episode_transition", ENGINE_FN)
    # production call
    old_prod = """            _opposite_episode_confirmed = detect_opposite_episode_transition(
                existing_direction=_existing_episode_direction,
                new_direction=desired_live_direction,
                live_direction_matches=live_trade.get("direction") == desired_live_direction,
                confirm_dirs=confirm_dirs,
                existing_structure_broken=_existing_structure_broken,
                new_etf_vwap_reclaim=_vwap_reclaim,
                new_swing_breakout=bool(confirm_swing_breakout),
            )"""
    new_prod = """            _opposite_episode_confirmed = detect_opposite_episode_transition(
                existing_direction=_existing_episode_direction,
                new_direction=desired_live_direction,
                live_direction_matches=live_trade.get("direction") == desired_live_direction,
                confirm_dirs=confirm_dirs,
                existing_structure_broken=_existing_structure_broken,
                new_etf_vwap_reclaim=_vwap_reclaim,
                new_etf_vwap_break=bool(confirm_above_vwap),
                new_swing_breakout=bool(confirm_swing_breakout),
            )"""
    if old_prod in eng:
        eng = eng.replace(old_prod, new_prod)
    elif "new_etf_vwap_break=bool(confirm_above_vwap)" not in eng:
        # fallback partial
        eng = eng.replace(
            "new_etf_vwap_reclaim=_vwap_reclaim,\n                new_swing_breakout=bool(confirm_swing_breakout),",
            "new_etf_vwap_reclaim=_vwap_reclaim,\n                new_etf_vwap_break=bool(confirm_above_vwap),\n                new_swing_breakout=bool(confirm_swing_breakout),",
        )
    ENGINE.write_text(eng, encoding="utf-8")
    print("patched", ENGINE)

    rep = REPLAY.read_text(encoding="utf-8")
    old_rep = """        _opposite_episode_confirmed = engine.detect_opposite_episode_transition(
            existing_direction=_existing_episode_direction,
            new_direction=live_dir,
            live_direction_matches=True,
            confirm_dirs=confirm_dirs,
            existing_structure_broken=_existing_structure_broken,
            new_etf_vwap_reclaim=vwap_reclaim,
            new_swing_breakout=swing_breakout,
        )"""
    new_rep = """        _opposite_episode_confirmed = engine.detect_opposite_episode_transition(
            existing_direction=_existing_episode_direction,
            new_direction=live_dir,
            live_direction_matches=True,
            confirm_dirs=confirm_dirs,
            existing_structure_broken=_existing_structure_broken,
            new_etf_vwap_reclaim=vwap_reclaim,
            new_etf_vwap_break=confirm_above_vwap,
            new_swing_breakout=swing_breakout,
        )"""
    if old_rep in rep:
        rep = rep.replace(old_rep, new_rep)
    elif "new_etf_vwap_break=confirm_above_vwap" not in rep:
        rep = rep.replace(
            "new_etf_vwap_reclaim=vwap_reclaim,\n            new_swing_breakout=swing_breakout,",
            "new_etf_vwap_reclaim=vwap_reclaim,\n            new_etf_vwap_break=confirm_above_vwap,\n            new_swing_breakout=swing_breakout,",
        )
    # raw dirs already expected
    if "confirm_window_directions=confirm_dirs_raw" not in rep:
        raise SystemExit("replay missing confirm_dirs_raw wiring")
    REPLAY.write_text(rep, encoding="utf-8")
    print("patched", REPLAY)

    tests = TESTS.read_text(encoding="utf-8")
    if "new_etf_vwap_break=True" not in tests:
        needle = """    # Path 2: VWAP reclaim requires both 5s and 10s in the new direction.
    assert engine.detect_opposite_episode_transition(
        existing_direction="UP",
        new_direction="DOWN",
        live_direction_matches=True,
        confirm_dirs={5: "DOWN", 10: "DOWN"},
        existing_structure_broken=False,
        new_etf_vwap_reclaim=True,
    )
    # Neither path satisfied."""
        insert = """    # Path 2a: VWAP reclaim requires both 5s and 10s in the new direction.
    assert engine.detect_opposite_episode_transition(
        existing_direction="UP",
        new_direction="DOWN",
        live_direction_matches=True,
        confirm_dirs={5: "DOWN", 10: "DOWN"},
        existing_structure_broken=False,
        new_etf_vwap_reclaim=True,
    )
    # Path 2b: VWAP break (aligned side) + 5/10 also counts without reclaim edge.
    assert engine.detect_opposite_episode_transition(
        existing_direction="UP",
        new_direction="DOWN",
        live_direction_matches=True,
        confirm_dirs={5: "DOWN", 10: "DOWN"},
        existing_structure_broken=False,
        new_etf_vwap_reclaim=False,
        new_etf_vwap_break=True,
    )
    # Neither path satisfied."""
        if needle not in tests:
            raise SystemExit("test needle not found")
        tests = tests.replace(needle, insert)
        TESTS.write_text(tests, encoding="utf-8")
        print("patched", TESTS)
    else:
        print("tests already have vwap_break case")


if __name__ == "__main__":
    main()
