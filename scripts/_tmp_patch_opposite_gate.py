from pathlib import Path

p = Path("app/services/hynix_switch_engine.py")
text = p.read_text(encoding="utf-8")

start = text.index("def detect_opposite_episode_transition(")
# Find next top-level def after this function
end = text.index("\ndef range_episode_allows_entry(", start)

new_fn = '''def detect_opposite_episode_transition(
    *,
    existing_direction: str | None,
    new_direction: str,
    live_direction_matches: bool,
    confirm_dirs: dict,
    existing_structure_broken: bool,
    new_etf_vwap_reclaim: bool,
    new_swing_breakout: bool = False,
) -> bool:
    """opposite episode transition: OR of swing structure vs VWAP reclaim+5/10.

    1) swing structure breakout / structure broken against existing direction
       (existing structure break OR new-direction swing breakout; no 5/10 required)
    2) opposite ETF VWAP reclaim/break + ETF 5/10 direction confirm (5s alone insufficient)
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
    return bool(new_etf_vwap_reclaim and dirs_5_10_aligned)

'''

text = text[:start] + new_fn + text[end + 1 :]  # end already includes leading \n via pattern; keep one

# Normalize call site kwargs
text = text.replace(
    "                new_etf_vwap_reclaim=_vwap_reclaim,\n"
    "                new_etf_vwap_break=bool(confirm_above_vwap),\n"
    "                new_swing_breakout=bool(confirm_swing_breakout),\n",
    "                new_etf_vwap_reclaim=_vwap_reclaim,\n"
    "                new_swing_breakout=bool(confirm_swing_breakout),\n",
)
text = text.replace(
    "                new_etf_vwap_reclaim=_vwap_reclaim,\n"
    "                new_etf_vwap_break=bool(confirm_above_vwap),\n",
    "                new_etf_vwap_reclaim=_vwap_reclaim,\n"
    "                new_swing_breakout=bool(confirm_swing_breakout),\n",
)

# Ensure blank line before range_episode_allows_entry
text = text.replace(
    "    return bool(new_etf_vwap_reclaim and dirs_5_10_aligned)\ndef range_episode_allows_entry(",
    "    return bool(new_etf_vwap_reclaim and dirs_5_10_aligned)\n\n\ndef range_episode_allows_entry(",
)

p.write_text(text, encoding="utf-8")
print("patched ok")
sig_start = text.index("def detect_opposite_episode_transition(")
print(text[sig_start:sig_start + 700])
