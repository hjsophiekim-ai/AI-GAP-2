"""READ-ONLY, MOCK-account-only raw-flag-engine parity verification.

Never places an order, never imports order_executor/broker_adapter/ledger's
write path, never touches TSLA_AUTO. Only reads 000660 1m candles (today via
MarketDataService(mode='mock'), a genuinely read-only KIS market-data call;
past days via the existing data/cache/replay_*_hynix_1m.csv fixtures already
in this repo) and re-derives MACD purely in pandas.

Compares 5 candidate ORIGINAL (raw) flag-detection rules against
data/validation/macd2/kis_expected_flags.csv (real KIS-chart ground truth,
never hardcoded into the rules themselves):

  A. current code            -> app.trading.macd2.worker.compute_today_signal_overview()
                                 (histogram color + regime/onset state machine)
  B. 2026-07-27 KIS-parity   -> signal_engine.evaluate_macd_crossover()
     rule                       (completed-bar MACD-line/Signal-line zero
                                 crossing, previous-direction repeat suppressed)
  C. histogram 2-run onset   -> signal_engine.confirmed_macd_flag_condition()
                                 (h0>h1>h2 / h0<h1<h2), same-direction repeat
                                 suppressed only (no regime debounce)
  D. histogram slope-flip    -> sign(h0-h1) vs sign(h1-h2) reversal (local
     onset                     extremum onset), same-direction repeat
                                suppressed
  E. slope-flip + 1-bar      -> D's condition, but only published once the
     confirm                   SAME new slope direction is still present on
                                the NEXT evaluated bar (uses only data
                                available as of that later bar -- no future
                                leakage; flag_time stays the ORIGINAL
                                flip-bar's bar_start)

All 3-min bars are completed bars only (no forming/provisional bar), each
completed bar evaluated exactly once, flag_time is always that bar's
bar_start_at (never bar_end, never evaluated_at).

Selection is NOT by F1 alone: the ORIGINAL flag engine's stated priority is
recall (MISSED count) and time accuracy (WRONG_TIME count) first, with
FALSE_POSITIVE and same-direction-consecutive-duplicate count as tie-breakers
-- see rank_rules() below for the exact, disclosed ordering. No date/time is
hardcoded into any rule's logic; GT is read purely from the CSV fixture.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config  # noqa: E402
from app.trading.macd2.market_data import MarketDataService, filter_complete_3m_bars  # noqa: E402
from app.trading.macd2.models import Direction, MacdSnapshot  # noqa: E402
from app.trading.macd2.signal_engine import (  # noqa: E402
    calculate_macd,
    confirmed_macd_flag_condition,
    evaluate_macd_crossover,
    resample_completed_3m,
)
from app.trading.macd2 import worker  # noqa: E402

KST = config.KST
FIXTURE_PATH = ROOT / "data" / "validation" / "macd2" / "kis_expected_flags.csv"
LINK_WINDOW_MIN = 15   # max minutes to still link a produced flag to a GT event ("this event, mistimed")
EXACT_WINDOW_MIN = 3   # within one bar width of GT time = MATCH; beyond that (still within LINK_WINDOW) = WRONG_TIME


def _dir_label(d) -> str:
    v = d.value if isinstance(d, Direction) else d
    return "UP_RED" if v == "UP_RED" else "DOWN_BLUE"


def _to_min(hhmm: str) -> int:
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


@dataclass
class ProducedFlag:
    bar_start_hhmm: str
    direction: str
    macd: float
    signal: float
    hist: float
    hist_last3: tuple
    previous_slope: float
    current_slope: float
    onset_reason: str


def _slopes(snap: MacdSnapshot) -> tuple[float, float]:
    h2, h1, h0 = snap.hist_last3
    return (h1 - h2), (h0 - h1)  # previous_slope, current_slope


# ---------------------------------------------------------------------------
# Rule builders. Each takes (bars_3m, today_idx) -- completed 3m bars (may
# include prior-day warm-up rows before today_idx[0]) and the day's own
# integer index list -- and returns list[ProducedFlag] in chronological order.
# ---------------------------------------------------------------------------
def rule_A_current_code(df_1m: pd.DataFrame, now: datetime, day_ymd: str) -> list[ProducedFlag]:
    session_started_at = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat() if day_ymd != now.strftime("%Y%m%d") else None
    overview = worker.compute_today_signal_overview(df_1m, now=now, session_started_at=session_started_at)
    out = []
    for row in overview:
        if row["bar_start_at"][:10].replace("-", "") != day_ymd:
            continue
        out.append(ProducedFlag(
            bar_start_hhmm=row["bar_start_at"][11:16], direction=_dir_label(row["direction"]),
            macd=float("nan"), signal=float("nan"), hist=float("nan"),
            hist_last3=(), previous_slope=float("nan"), current_slope=float("nan"),
            onset_reason="color+regime onset (current code, see evaluate_confirmed_macd_color_onset)",
        ))
    return out


def rule_B_crossover(bars_3m: pd.DataFrame, today_idx: list[int]) -> list[ProducedFlag]:
    out = []
    last_direction: Direction | None = None
    for idx in today_idx:
        snap = calculate_macd(bars_3m.iloc[: idx + 1])
        if snap is None:
            continue
        direction = evaluate_macd_crossover(snap, last_direction)
        if direction == Direction.HOLD:
            continue
        last_direction = direction
        prev_s, cur_s = _slopes(snap)
        out.append(ProducedFlag(
            bar_start_hhmm=snap.bar_dt.strftime("%H:%M"), direction=_dir_label(direction),
            macd=snap.macd, signal=snap.signal, hist=snap.hist, hist_last3=snap.hist_last3,
            previous_slope=prev_s, current_slope=cur_s,
            onset_reason=f"MACD-Signal diff sign flip: prev_diff={snap.previous_diff:.2f} -> diff={snap.current_diff:.2f}",
        ))
    return out


def rule_C_histogram_2run(bars_3m: pd.DataFrame, today_idx: list[int]) -> list[ProducedFlag]:
    out = []
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
        prev_s, cur_s = _slopes(snap)
        out.append(ProducedFlag(
            bar_start_hhmm=snap.bar_dt.strftime("%H:%M"), direction=_dir_label(raw),
            macd=snap.macd, signal=snap.signal, hist=snap.hist, hist_last3=snap.hist_last3,
            previous_slope=prev_s, current_slope=cur_s,
            onset_reason=f"hist 2-bar run: {snap.hist_last3[0]:.2f}->{snap.hist_last3[1]:.2f}->{snap.hist_last3[2]:.2f}",
        ))
    return out


def rule_D_slope_flip(bars_3m: pd.DataFrame, today_idx: list[int]) -> list[ProducedFlag]:
    out = []
    last_direction: Direction | None = None
    for idx in today_idx:
        snap = calculate_macd(bars_3m.iloc[: idx + 1])
        if snap is None:
            continue
        prev_s, cur_s = _slopes(snap)
        if prev_s <= 0 and cur_s > 0:
            direction = Direction.UP_RED
        elif prev_s >= 0 and cur_s < 0:
            direction = Direction.DOWN_BLUE
        else:
            continue
        if direction == last_direction:
            continue
        last_direction = direction
        out.append(ProducedFlag(
            bar_start_hhmm=snap.bar_dt.strftime("%H:%M"), direction=_dir_label(direction),
            macd=snap.macd, signal=snap.signal, hist=snap.hist, hist_last3=snap.hist_last3,
            previous_slope=prev_s, current_slope=cur_s,
            onset_reason=f"hist slope flip: prev_slope={prev_s:.2f} -> cur_slope={cur_s:.2f}",
        ))
    return out


def rule_E_slope_flip_confirmed(bars_3m: pd.DataFrame, today_idx: list[int]) -> list[ProducedFlag]:
    """D's slope-flip candidate, published only once the SAME slope direction
    is still present on the bar immediately after the flip (no future data at
    detection time -- confirmation happens on that later bar's own tick;
    flag_time stays the ORIGINAL flip bar's bar_start)."""
    out = []
    last_direction: Direction | None = None
    pending: tuple | None = None  # (flip_idx, direction, snap_at_flip)
    for idx in today_idx:
        snap = calculate_macd(bars_3m.iloc[: idx + 1])
        if snap is None:
            continue
        prev_s, cur_s = _slopes(snap)
        candidate_dir = None
        if prev_s <= 0 and cur_s > 0:
            candidate_dir = Direction.UP_RED
        elif prev_s >= 0 and cur_s < 0:
            candidate_dir = Direction.DOWN_BLUE

        if pending is not None:
            flip_idx, flip_dir, flip_snap = pending
            pending = None
            # confirm: this bar's slope (cur_s here is this bar's own hist
            # trend) still moves the same way as the flip direction implied
            still_same = (cur_s > 0) if flip_dir == Direction.UP_RED else (cur_s < 0)
            if still_same and flip_dir != last_direction:
                last_direction = flip_dir
                fp, fc = _slopes(flip_snap)
                out.append(ProducedFlag(
                    bar_start_hhmm=flip_snap.bar_dt.strftime("%H:%M"), direction=_dir_label(flip_dir),
                    macd=flip_snap.macd, signal=flip_snap.signal, hist=flip_snap.hist,
                    hist_last3=flip_snap.hist_last3, previous_slope=fp, current_slope=fc,
                    onset_reason=f"slope flip at {flip_snap.bar_dt.strftime('%H:%M')} confirmed by next bar's slope",
                ))
        if candidate_dir is not None and candidate_dir != last_direction:
            pending = (idx, candidate_dir, snap)
    return out


RULES = {
    "A": ("current color+regime", rule_A_current_code),
    "B": ("2026-07-27 crossover", rule_B_crossover),
    "C": ("histogram 2-run onset", rule_C_histogram_2run),
    "D": ("histogram slope-flip onset", rule_D_slope_flip),
    "E": ("slope-flip + 1-bar confirm", rule_E_slope_flip_confirmed),
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score(gt: list[tuple[str, str]], produced: list[ProducedFlag]):
    remaining = list(range(len(produced)))
    results = []
    used = set()
    for gt_t, gt_d in gt:
        gt_min = _to_min(gt_t)
        best, best_diff = None, None
        for j in remaining:
            if j in used or produced[j].direction != gt_d:
                continue
            diff = abs(_to_min(produced[j].bar_start_hhmm) - gt_min)
            if diff <= LINK_WINDOW_MIN and (best is None or diff < best_diff):
                best, best_diff = j, diff
        if best is not None:
            used.add(best)
            status = "MATCH" if best_diff <= EXACT_WINDOW_MIN else "WRONG_TIME"
            results.append((gt_t, gt_d, status, produced[best], best_diff))
        else:
            results.append((gt_t, gt_d, "MISSED", None, None))
    false_positives = [produced[j] for j in remaining if j not in used]
    # same-direction-consecutive-duplicate check (should always be 0 by construction,
    # verified explicitly here as a safety net)
    dup_count = sum(
        1 for i in range(1, len(produced)) if produced[i].direction == produced[i - 1].direction
    )
    tp = sum(1 for r in results if r[2] in ("MATCH", "WRONG_TIME"))
    wrong_time = sum(1 for r in results if r[2] == "WRONG_TIME")
    fn = sum(1 for r in results if r[2] == "MISSED")
    fp = len(false_positives)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    metrics = {
        "tp": tp, "wrong_time": wrong_time, "fn": fn, "fp": fp, "dup": dup_count,
        "precision": precision, "recall": recall, "f1": f1,
    }
    return results, false_positives, metrics


def explain_missed(gt_t: str, gt_d: str, bars_3m: pd.DataFrame, today_idx: list[int]) -> str:
    gt_min = _to_min(gt_t)
    nearest_idx = min(today_idx, key=lambda i: abs(_to_min(bars_3m["datetime"].iloc[i].strftime("%H:%M")) - gt_min))
    snap = calculate_macd(bars_3m.iloc[: nearest_idx + 1])
    if snap is None:
        return "insufficient bars for MACD at this point"
    prev_s, cur_s = _slopes(snap)
    return (f"nearest bar {bars_3m['datetime'].iloc[nearest_idx].strftime('%H:%M')}: "
            f"hist_last3={snap.hist_last3} diff={snap.current_diff:.2f} prev_diff={snap.previous_diff:.2f} "
            f"prev_slope={prev_s:.2f} cur_slope={cur_s:.2f} -- condition for this rule was not met at/near this bar")


def print_rule_report(key: str, name: str, produced: list[ProducedFlag], gt: list[tuple[str, str]],
                       bars_3m: pd.DataFrame, today_idx: list[int]):
    print(f"\n----- RULE {key} ({name}): all produced flags (count={len(produced)}) -----")
    for p in produced:
        print(f"  {p.bar_start_hhmm} {p.direction}  macd={p.macd:.2f} signal={p.signal:.2f} hist={p.hist:.2f} "
              f"hist_last3={p.hist_last3} prev_slope={p.previous_slope:.2f} cur_slope={p.current_slope:.2f}")
        print(f"      onset_reason: {p.onset_reason}")
    results, fps, metrics = score(gt, produced)
    print(f"  --- per-GT-event outcome ---")
    for gt_t, gt_d, status, matched, diff in results:
        if status == "MISSED":
            reason = explain_missed(gt_t, gt_d, bars_3m, today_idx)
            print(f"    GT {gt_t} {gt_d}: MISSED -- {reason}")
        else:
            print(f"    GT {gt_t} {gt_d}: {status} -> {matched.bar_start_hhmm} {matched.direction} (diff={diff}min)")
    if fps:
        print(f"  --- FALSE_POSITIVE (produced, no GT within {LINK_WINDOW_MIN}min) ---")
        for p in fps:
            print(f"    {p.bar_start_hhmm} {p.direction}: hist_last3={p.hist_last3} -- {p.onset_reason} "
                  f"(no corresponding KIS flag recorded within {LINK_WINDOW_MIN}min in fixture)")
    print(f"  --- precision={metrics['precision']:.3f} recall={metrics['recall']:.3f} f1={metrics['f1']:.3f} "
          f"(TP={metrics['tp']} WRONG_TIME={metrics['wrong_time']} FN={metrics['fn']} FP={metrics['fp']} "
          f"same_dir_dup={metrics['dup']}) ---")
    return metrics


def rank_rules(all_metrics: dict[str, dict]) -> list[str]:
    """Disclosed selection order (recall/time-accuracy first, NOT plain F1):
    1) fewest MISSED (fn)      -- recall priority
    2) fewest WRONG_TIME       -- time-accuracy priority
    3) fewest FALSE_POSITIVE
    4) fewest same-direction consecutive duplicates
    """
    return sorted(all_metrics.keys(), key=lambda k: (
        all_metrics[k]["fn"], all_metrics[k]["wrong_time"], all_metrics[k]["fp"], all_metrics[k]["dup"],
    ))


def load_fixture() -> dict[str, list[tuple[str, str]]]:
    df = pd.read_csv(FIXTURE_PATH, dtype=str)
    df = df[df["confirmed_by_user"].str.lower() == "true"]
    by_date: dict[str, list[tuple[str, str]]] = {}
    for _, row in df.iterrows():
        by_date.setdefault(row["trading_date"], []).append((row["flag_time"], row["direction"]))
    for date in by_date:
        by_date[date].sort(key=lambda x: _to_min(x[0]))
    return by_date


def load_day_1m(day_ymd: str, now_hint: datetime | None) -> tuple[pd.DataFrame, datetime, bool]:
    """Returns (df_1m, eval_now, data_incomplete)."""
    today_str = datetime.now(KST).strftime("%Y%m%d")
    if day_ymd == today_str:
        md = MarketDataService(mode="mock")
        boot = md.bootstrap(now=now_hint or datetime.now(KST))
        df_1m = md.get_history_df()
        return df_1m, (now_hint or datetime.now(KST)), not boot.ok
    # past day -- use existing cached replay fixtures (never invented/interpolated)
    prior_ymd = (datetime.strptime(day_ymd, "%Y%m%d").date() - timedelta(days=1))
    while prior_ymd.weekday() >= 5:
        prior_ymd -= timedelta(days=1)
    prior_str = prior_ymd.strftime("%Y%m%d")
    frames, incomplete = [], False
    for ymd in (prior_str, day_ymd):
        path = ROOT / "data" / "cache" / f"replay_{ymd}_hynix_1m.csv"
        if not path.exists():
            incomplete = True
            continue
        frame = pd.read_csv(path)
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="raise").dt.tz_localize(KST)
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), datetime(2000, 1, 1, tzinfo=KST), True
    df_1m = pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)
    eval_now = datetime.strptime(day_ymd, "%Y%m%d").replace(hour=15, minute=30, tzinfo=KST)
    return df_1m, eval_now, incomplete


def main():
    fixture = load_fixture()
    print(f"Loaded ground-truth fixture: {FIXTURE_PATH}")
    for date, flags in fixture.items():
        print(f"  {date}: {len(flags)} confirmed GT flags")

    combined = {k: {"tp": 0, "wrong_time": 0, "fn": 0, "fp": 0, "dup": 0} for k in RULES}

    for day_ymd, gt in fixture.items():
        print("\n" + "=" * 78)
        print(f"DAY {day_ymd}  (GT flags: {gt})")
        print("=" * 78)
        df_1m, now, incomplete = load_day_1m(day_ymd, None)
        if df_1m.empty:
            print(f"  DATA_INCOMPLETE: no 1m data available for {day_ymd} -- skipped, not interpolated")
            continue
        if incomplete:
            print(f"  DATA_INCOMPLETE warning: bootstrap/fixture for {day_ymd} did not fully succeed; "
                  f"results below may be partial, flagged explicitly")

        bars_3m = resample_completed_3m(df_1m, now=now)
        bars_3m, dropped = filter_complete_3m_bars(bars_3m, df_1m)
        if dropped:
            print(f"  dropped (incomplete) 3m bar starts: {dropped}")
        today_idx = list(bars_3m.index[bars_3m["datetime"].dt.strftime("%Y%m%d") == day_ymd])
        print(f"  completed 3m bars today: {len(today_idx)} (total incl. warm-up: {len(bars_3m)})")

        for key, (name, builder) in RULES.items():
            if key == "A":
                produced = builder(df_1m, now, day_ymd)
            else:
                produced = builder(bars_3m, today_idx)
            metrics = print_rule_report(key, name, produced, gt, bars_3m, today_idx)
            for mk in combined[key]:
                combined[key][mk] += metrics[mk]

    print("\n" + "=" * 78)
    print("COMBINED ACROSS ALL DAYS")
    print("=" * 78)
    for key, (name, _builder) in RULES.items():
        m = combined[key]
        tp, fp, fn = m["tp"], m["fp"], m["fn"]
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
        print(f"  RULE {key} ({name}): MISSED={fn} WRONG_TIME={m['wrong_time']} FALSE_POSITIVE={fp} "
              f"same_dir_dup={m['dup']} precision={p:.3f} recall={r:.3f} f1={f1:.3f}")

    order = rank_rules(combined)
    print(f"\nSELECTION ORDER (fewest MISSED, then fewest WRONG_TIME, then fewest FALSE_POSITIVE, then fewest dup):")
    print(f"  {' > '.join(order)}")
    winner = order[0]
    print(f"\nSELECTED RULE: {winner} ({RULES[winner][0]})")


if __name__ == "__main__":
    main()
