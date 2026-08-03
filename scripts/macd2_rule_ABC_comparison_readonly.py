"""READ-ONLY, MOCK-account-only verification: compare 3 confirmed-signal
detection rules against real KIS-chart ground truth. Never places an order,
never imports order_executor/worker's dispatch path, never writes any
ledger/state file. Only reads 000660 1m candles via MarketDataService
(mode='mock') and re-derives MACD purely in pandas.

Rules under test (all evaluated on the SAME completed-3m-bar series,
labeled at bar START per docs/MACD2_LOGIC.md line 151):
  A. current code  -> worker.compute_today_signal_overview() unmodified
     (histogram color + regime/onset state machine, 2026-07-31 revision)
  B. 2026-07-27 KIS-parity rule -> evaluate_macd_crossover() (plain
     MACD-line/Signal-line completed-bar crossover, previous-direction
     repeat suppressed) -- this is docs/MACD2_LOGIC.md line 172's
     currently-documented "Primary" rule, which config.SIGNAL_RULE/
     STRATEGY_VERSION have since silently drifted away from.
  C. debounce-free 2-consecutive-bar histogram color (no regime state
     machine at all) -> confirmed_macd_flag_condition(), same-direction
     repeat suppressed only.

Ground truth for TODAY (2026-08-03) is the 12 flags the user read off the
live KIS chart, passed in via GT_TODAY below -- entered ONCE, here, as a
comparison fixture; no rule's internal logic branches on these values.
Ground truth for 2026-07-31 comes from the pre-existing, previously
verified fixture in scripts/macd2_verify_20260731_color_flags.py.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config  # noqa: E402
from app.trading.macd2.market_data import MarketDataService, filter_complete_3m_bars  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
from app.trading.macd2.signal_engine import (  # noqa: E402
    calculate_macd,
    confirmed_macd_flag_condition,
    evaluate_macd_crossover,
    resample_completed_3m,
)
from app.trading.macd2 import worker  # noqa: E402

KST = config.KST
LINK_WINDOW_MIN = 15   # max minutes to still count a produced flag as "this GT event, mistimed"
EXACT_WINDOW_MIN = 3   # within one bar width = MATCH; beyond that but within LINK_WINDOW = WRONG_TIME

GT_TODAY = [
    ("09:36", "RED"), ("10:30", "BLUE"), ("10:42", "RED"), ("11:30", "BLUE"),
    ("11:39", "RED"), ("11:45", "BLUE"), ("12:00", "RED"), ("12:09", "BLUE"),
    ("12:42", "RED"), ("13:00", "BLUE"), ("13:06", "RED"), ("13:39", "BLUE"),
]
GT_20260731 = [("09:00", "RED"), ("09:15", "BLUE"), ("11:27", "RED"), ("12:45", "BLUE")]


def _dir_label(d) -> str:
    v = d.value if isinstance(d, Direction) else d
    return "RED" if v == "UP_RED" else "BLUE"


def _to_min(hhmm: str) -> int:
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------------------
# Rule builders -- each returns list[(hhmm_bar_start:str, "RED"/"BLUE")]
# ---------------------------------------------------------------------------
def rule_A_current_code(df_1m: pd.DataFrame, now: datetime) -> list[tuple[str, str]]:
    overview = worker.compute_today_signal_overview(df_1m, now=now, session_started_at=None)
    return [(row["bar_start_at"][11:16], _dir_label(row["direction"])) for row in overview]


def _bars_and_today_idx(df_1m: pd.DataFrame, now: datetime, day_ymd: str):
    bars_3m = resample_completed_3m(df_1m, now=now)
    bars_3m, _dropped = filter_complete_3m_bars(bars_3m, df_1m)
    mask = bars_3m["datetime"].dt.strftime("%Y%m%d") == day_ymd
    return bars_3m, list(bars_3m.index[mask])


def rule_B_20260727_crossover(bars_3m: pd.DataFrame, today_idx: list[int]) -> list[tuple[str, str]]:
    flags = []
    last_direction: Direction | None = None
    for pos, idx in enumerate(today_idx):
        snap = calculate_macd(bars_3m.iloc[: idx + 1])
        if snap is None:
            continue
        direction = evaluate_macd_crossover(snap, last_direction)
        if pos == 0:
            # First bar of the day: previous_diff spans into yesterday, but
            # this IS what evaluate_macd_crossover as it existed on 2026-07-27
            # would have evaluated (no special first-bar carve-out existed
            # then) -- kept faithful to that historical rule, not patched.
            pass
        if direction == Direction.HOLD:
            continue
        last_direction = direction
        flags.append((snap.bar_dt.strftime("%H:%M"), _dir_label(direction)))
    return flags


def rule_C_debounce_free(bars_3m: pd.DataFrame, today_idx: list[int]) -> list[tuple[str, str]]:
    flags = []
    last_direction: Direction | None = None
    for pos, idx in enumerate(today_idx):
        snap = calculate_macd(bars_3m.iloc[: idx + 1])
        if snap is None:
            continue
        raw = confirmed_macd_flag_condition(snap)
        if pos == 0:
            last_direction = raw if raw != Direction.HOLD else None
            continue
        if raw == Direction.HOLD or raw == last_direction:
            continue
        last_direction = raw
        flags.append((snap.bar_dt.strftime("%H:%M"), _dir_label(raw)))
    return flags


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score(gt: list[tuple[str, str]], produced: list[tuple[str, str]]):
    remaining = list(range(len(produced)))
    results = []  # per-GT: (gt_time, gt_dir, status, matched_produced_or_None, diff_min)
    used = set()
    for gt_t, gt_d in gt:
        gt_min = _to_min(gt_t)
        best = None
        best_diff = None
        for j in remaining:
            if j in used:
                continue
            p_t, p_d = produced[j]
            if p_d != gt_d:
                continue
            diff = abs(_to_min(p_t) - gt_min)
            if diff <= LINK_WINDOW_MIN and (best is None or diff < best_diff):
                best, best_diff = j, diff
        if best is not None:
            used.add(best)
            status = "MATCH" if best_diff <= EXACT_WINDOW_MIN else "WRONG_TIME"
            results.append((gt_t, gt_d, status, produced[best], best_diff))
        else:
            results.append((gt_t, gt_d, "MISSED", None, None))
    false_positives = [produced[j] for j in remaining if j not in used]
    tp = sum(1 for r in results if r[2] in ("MATCH", "WRONG_TIME"))
    fn = sum(1 for r in results if r[2] == "MISSED")
    fp = len(false_positives)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return results, false_positives, {"tp": tp, "fn": fn, "fp": fp, "precision": precision, "recall": recall, "f1": f1}


def print_rule_report(name: str, produced: list[tuple[str, str]], gt: list[tuple[str, str]]):
    print(f"\n===== RULE {name}: all produced flags (count={len(produced)}) =====")
    for t, d in produced:
        print(f"  {t} {d}")
    results, fps, metrics = score(gt, produced)
    print(f"----- RULE {name}: per-GT-event outcome -----")
    for gt_t, gt_d, status, matched, diff in results:
        m = f" -> matched {matched} (diff={diff}min)" if matched else ""
        print(f"  GT {gt_t} {gt_d}: {status}{m}")
    print(f"----- RULE {name}: FALSE_POSITIVE (produced, no GT match within {LINK_WINDOW_MIN}min) -----")
    for t, d in fps:
        print(f"  {t} {d}")
    print(f"----- RULE {name}: precision={metrics['precision']:.3f} recall={metrics['recall']:.3f} "
          f"f1={metrics['f1']:.3f} (TP={metrics['tp']} FN={metrics['fn']} FP={metrics['fp']}) -----")
    return metrics


def main():
    now = datetime.now(KST)
    print(f"NOW={now.isoformat()}")
    md = MarketDataService(mode="mock")
    boot = md.bootstrap(now=now)
    print("bootstrap ok:", boot.ok, boot.reason, "prior_day_1m_bars:", boot.prior_day_1m_bars, "today_1m_bars:", boot.today_1m_bars)
    df_1m = md.get_history_df()
    today_ymd = now.strftime("%Y%m%d")

    print("\n" + "=" * 70)
    print(f"DAY 1: TODAY {today_ymd} vs user-read KIS chart (12 flags)")
    print("=" * 70)
    a = rule_A_current_code(df_1m, now)
    bars_3m, today_idx = _bars_and_today_idx(df_1m, now, today_ymd)
    b = rule_B_20260727_crossover(bars_3m, today_idx)
    c = rule_C_debounce_free(bars_3m, today_idx)
    metrics_today = {}
    metrics_today["A"] = print_rule_report("A (current color+regime)", a, GT_TODAY)
    metrics_today["B"] = print_rule_report("B (2026-07-27 crossover)", b, GT_TODAY)
    metrics_today["C"] = print_rule_report("C (debounce-free 2-bar histogram)", c, GT_TODAY)

    # ---- Day 2: 2026-07-31 replay against the pre-existing verified fixture ----
    print("\n" + "=" * 70)
    print("DAY 2: 2026-07-31 replay (data/cache/replay_20260730_hynix_1m.csv + 20260731) vs prior verified fixture")
    print("=" * 70)
    frames = []
    for ymd in ("20260730", "20260731"):
        path = ROOT / "data" / "cache" / f"replay_{ymd}_hynix_1m.csv"
        frame = pd.read_csv(path)
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="raise").dt.tz_localize(KST)
        frames.append(frame)
    df_0731 = pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)
    now_0731 = datetime(2026, 7, 31, 15, 30, tzinfo=KST)

    a2 = rule_A_current_code(df_0731, now_0731)
    bars_3m_2, today_idx_2 = _bars_and_today_idx(df_0731, now_0731, "20260731")
    b2 = rule_B_20260727_crossover(bars_3m_2, today_idx_2)
    c2 = rule_C_debounce_free(bars_3m_2, today_idx_2)
    metrics_0731 = {}
    metrics_0731["A"] = print_rule_report("A (current color+regime)", a2, GT_20260731)
    metrics_0731["B"] = print_rule_report("B (2026-07-27 crossover)", b2, GT_20260731)
    metrics_0731["C"] = print_rule_report("C (debounce-free 2-bar histogram)", c2, GT_20260731)

    print("\n" + "=" * 70)
    print("SUMMARY (both days combined, micro-averaged over TP/FP/FN)")
    print("=" * 70)
    for key in ("A", "B", "C"):
        tp = metrics_today[key]["tp"] + metrics_0731[key]["tp"]
        fp = metrics_today[key]["fp"] + metrics_0731[key]["fp"]
        fn = metrics_today[key]["fn"] + metrics_0731[key]["fn"]
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
        print(f"  RULE {key}: TP={tp} FP={fp} FN={fn} precision={p:.3f} recall={r:.3f} f1={f1:.3f}")


if __name__ == "__main__":
    main()
