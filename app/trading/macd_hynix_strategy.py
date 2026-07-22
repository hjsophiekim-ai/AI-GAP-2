"""Isolated Strategy B: completed 3-minute MACD histogram direction for 000660.

Does not call Enhanced / WOC / Early / Active / Fusion / Regime / Prediction.
Orders are never placed from this module — direction only.

Strategy B = signed histogram two-turn (same-sign/color + 2 deltas), shared with
A–F compare `signals_B`. Warm-up matches old `i < 26` skip.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

import pandas as pd

SIGNAL_SYMBOL = "000660"
LONG_SYMBOL = "0193T0"  # KODEX SK하이닉스 레버리지
INVERSE_SYMBOL = "0197X0"  # SOL SK하이닉스 인버스2X
LONG_NAME = "KODEX SK하이닉스단일종목레버리지"
INVERSE_NAME = "SOL SK하이닉스선물단일종목인버스2X"
SIGNAL_NAME = "SK하이닉스"

TRADE_SYMBOLS = (LONG_SYMBOL, INVERSE_SYMBOL)
SYMBOL_NAME = {
    SIGNAL_SYMBOL: SIGNAL_NAME,
    LONG_SYMBOL: LONG_NAME,
    INVERSE_SYMBOL: INVERSE_NAME,
}

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
# Old A–F `signals_B`: `if i < 26: continue` → first eligible bar index is 26
# (requires len(bars) >= 27). Shared by live + all B replays.
MACD_SIGNAL_MIN_INDEX = 26

DIR_UP = "UP_RED"
DIR_DOWN = "DOWN_BLUE"
DIR_HOLD = "HOLD"

# ── Exit / continuation (net PnL vs actual ETF entry) ──────────────────────
TP_NET_PCT = 3.0
SL_NET_PCT = -1.5
# Re-entry requires |hist| ≥ this fraction of max |hist| observed just before TP.
HIST_RECOVERY_RATIO = 0.70
# Chase gate: block if price is more than this % beyond the TP-time pivot
# (UP: above pivot high; DOWN: below pivot low). Testable, conservative.
CHASE_MAX_PCT = 1.5
# Live default: Jul21/22 A vs B compare → DO_NOT_ADOPT (B Net worse; MDD flat).
# Feature remains implemented; enable only via state.continuation_reentry_enabled.
CONTINUATION_REENTRY_ENABLED = False

EXIT_TP = "TP_EXIT"
EXIT_SL = "SL_EXIT"
EXIT_OPPOSITE = "OPPOSITE_SWITCH"
EXIT_SESSION = "15:00_FORCE_LIQUIDATE"
ENTRY_INITIAL = "INITIAL_ENTRY"
ENTRY_CONTINUATION = "CONTINUATION_REENTRY"
SIGNAL_SOURCE_CONTINUATION = "MACD_CONTINUATION_REENTRY"


def resample_completed_3m(
    df_1m: Optional[pd.DataFrame],
    now: Optional[datetime] = None,
) -> pd.DataFrame:
    """1m bars → completed 3m bars only. Incomplete current 3m window is excluded."""
    if df_1m is None or getattr(df_1m, "empty", True):
        return pd.DataFrame()
    work = df_1m.copy()
    if "datetime" not in work.columns:
        return pd.DataFrame()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    work = work.dropna(subset=["datetime"]).sort_values("datetime")
    if work.empty:
        return pd.DataFrame()
    indexed = work.set_index("datetime")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in indexed.columns:
        agg["volume"] = "sum"
    bars = indexed.resample("3min").agg(agg).dropna(subset=["close"]).reset_index()
    if bars.empty:
        return bars
    if now is None:
        return bars
    cutoff = now.replace(second=0, microsecond=0) if hasattr(now, "replace") else now
    return bars[bars["datetime"] + timedelta(minutes=3) <= cutoff].reset_index(drop=True)


def macd_components(closes: pd.Series) -> dict[str, Optional[pd.Series]]:
    """Return full MACD / Signal / Histogram series (or empty if insufficient)."""
    closes = pd.to_numeric(closes, errors="coerce").dropna()
    if len(closes) < MACD_SLOW:
        return {"macd": None, "signal": None, "hist": None}
    ema12 = closes.ewm(span=MACD_FAST, adjust=False).mean()
    ema26 = closes.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    hist = macd - signal
    return {"macd": macd, "signal": signal, "hist": hist}


def signed_hist_two_turn_pattern(
    hist_curr: float,
    hist_prev: float,
    hist_prev2: float,
) -> str:
    """Strategy B pattern: same-sign hist (color) + two consecutive deltas.

    Newest = hist_curr. UP requires both hist > 0 and both deltas > 0.
    DOWN requires both hist < 0 and both deltas < 0.
    Same-sign pullbacks / opposite-color slopes → HOLD (no wiggle flips).
    """
    d1 = float(hist_curr) - float(hist_prev)
    d2 = float(hist_prev) - float(hist_prev2)
    if hist_curr > 0 and hist_prev > 0 and d1 > 0 and d2 > 0:
        return DIR_UP
    if hist_curr < 0 and hist_prev < 0 and d1 < 0 and d2 < 0:
        return DIR_DOWN
    return DIR_HOLD


def signed_hist_two_turn_onset(
    hist_curr: float,
    hist_prev: float,
    hist_prev2: float,
    hist_prev3: Optional[float] = None,
) -> Optional[str]:
    """Return UP/DOWN only when the signed pattern *newly* becomes true.

    Matches old A–F `signals_B` prev_ok gate: if the prior bar already
    qualified for the same side, do not fire again (critical after warm-up
    so a pattern already true at i=25 does not arm at i=26).
    """
    pattern = signed_hist_two_turn_pattern(hist_curr, hist_prev, hist_prev2)
    if pattern == DIR_HOLD:
        return None
    if hist_prev3 is None:
        return pattern
    prev_pattern = signed_hist_two_turn_pattern(hist_prev, hist_prev2, float(hist_prev3))
    if prev_pattern == pattern:
        return None
    return pattern


def normalize_direction_state(direction: Optional[str]) -> Optional[str]:
    """Map UP/DOWN aliases onto DIR_UP / DIR_DOWN; else None / HOLD."""
    text = str(direction or "").upper().strip()
    if not text or text in ("NONE", "NULL"):
        return None
    if text in (DIR_UP, "UP", "UP_RED"):
        return DIR_UP
    if text in (DIR_DOWN, "DOWN", "DOWN_BLUE"):
        return DIR_DOWN
    if text == DIR_HOLD:
        return DIR_HOLD
    return None


def signed_hist_two_turn_new_signal(
    pattern: str,
    direction_state: Optional[str],
) -> bool:
    """Arm only when pattern is UP/DOWN and direction_state is not already that side.

    After TP/SL/session flatten, keep direction_state so same-dir cannot re-enter;
    a new episode starts only on the opposite confirmed B signal.
    """
    state = normalize_direction_state(direction_state)
    if pattern == DIR_UP and state != DIR_UP:
        return True
    if pattern == DIR_DOWN and state != DIR_DOWN:
        return True
    return False


def collect_signed_hist_two_turn_signals(
    hist: Sequence[float],
    *,
    close_times: Optional[Sequence[Any]] = None,
    min_index: int = MACD_SIGNAL_MIN_INDEX,
    direction_state: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Walk a hist series with old B warm-up + onset + direction_state gate.

    Shared by A–F `signals_B` and compare scripts so live/replay cannot drift.
    """
    events: list[dict[str, Any]] = []
    state = normalize_direction_state(direction_state)
    n = len(hist)
    for i in range(2, n):
        if i < min_index:
            continue
        prev3 = float(hist[i - 3]) if i >= 3 else None
        onset = signed_hist_two_turn_onset(
            float(hist[i]), float(hist[i - 1]), float(hist[i - 2]), prev3
        )
        if onset is None:
            continue
        if not signed_hist_two_turn_new_signal(onset, state):
            continue
        ct = None
        if close_times is not None and i < len(close_times):
            ct = close_times[i]
        events.append({
            "index": i,
            "direction": onset,
            "close_time": ct,
            "hist_curr": float(hist[i]),
            "hist_prev": float(hist[i - 1]),
            "hist_prev2": float(hist[i - 2]),
        })
        state = onset
    return events


def evaluate_macd_direction(
    df_1m: Optional[pd.DataFrame],
    *,
    now: Optional[datetime] = None,
    last_signal_direction: Optional[str] = None,
    last_signal_bar_ts: Optional[str] = None,
) -> dict[str, Any]:
    """Evaluate Strategy B on completed 3m MACD histogram of 000660.

    Signed same-sign two-turn + onset (old prev_ok) + persistent direction_state.
    Warm-up: bar index must be >= MACD_SIGNAL_MIN_INDEX (old `i < 26` skip).
    """
    empty = {
        "ok": False,
        "display_direction": DIR_HOLD,
        "new_signal": False,
        "signal_direction": None,
        "macd": None,
        "signal": None,
        "hist": None,
        "hist_last3": [],
        "hist_deltas": [],
        "completed_3m_count": 0,
        "bar_ts": None,
        "bar_close_ts": None,
        "reason": "DATA_INSUFFICIENT",
        "signal_id": None,
    }
    bars = resample_completed_3m(df_1m, now=now)
    # Need index >= 26 → at least 27 completed 3m bars (matches old signals_B).
    if len(bars) <= MACD_SIGNAL_MIN_INDEX:
        return {
            **empty,
            "completed_3m_count": int(len(bars)),
            "reason": "WARMUP_LT_26" if len(bars) >= 3 else "DATA_INSUFFICIENT",
        }

    closes = pd.to_numeric(bars["close"], errors="coerce").dropna()
    comps = macd_components(closes)
    if comps["hist"] is None or len(comps["hist"]) < 3:
        return {**empty, "completed_3m_count": int(len(bars)), "reason": "MACD_INSUFFICIENT"}

    hist = comps["hist"]
    macd = comps["macd"]
    signal = comps["signal"]
    h1, h2, h3 = float(hist.iloc[-1]), float(hist.iloc[-2]), float(hist.iloc[-3])
    h4 = float(hist.iloc[-4]) if len(hist) >= 4 else None
    pattern = signed_hist_two_turn_pattern(h1, h2, h3)
    onset = signed_hist_two_turn_onset(h1, h2, h3, h4)
    bar_ts = bars["datetime"].iloc[-1]
    bar_ts_iso = pd.Timestamp(bar_ts).isoformat()
    bar_close_ts = pd.Timestamp(bar_ts) + timedelta(minutes=3)

    last_dir = normalize_direction_state(last_signal_direction)

    new_signal = False
    signal_direction = None
    reason = "HOLD"
    if pattern == DIR_UP:
        reason = "UP_RED_PATTERN"
        if (
            onset == DIR_UP
            and signed_hist_two_turn_new_signal(onset, last_dir)
            and bar_ts_iso != str(last_signal_bar_ts or "")
        ):
            new_signal = True
            signal_direction = DIR_UP
            reason = "UP_RED_FIRST_TURN"
    elif pattern == DIR_DOWN:
        reason = "DOWN_BLUE_PATTERN"
        if (
            onset == DIR_DOWN
            and signed_hist_two_turn_new_signal(onset, last_dir)
            and bar_ts_iso != str(last_signal_bar_ts or "")
        ):
            new_signal = True
            signal_direction = DIR_DOWN
            reason = "DOWN_BLUE_FIRST_TURN"
    else:
        reason = "HOLD_NO_PATTERN"

    signal_id = None
    if new_signal and signal_direction:
        signal_id = f"MACD3M:{signal_direction}:{bar_ts_iso}"

    return {
        "ok": True,
        "display_direction": pattern,
        "new_signal": bool(new_signal),
        "signal_direction": signal_direction,
        "macd": round(float(macd.iloc[-1]), 6),
        "signal": round(float(signal.iloc[-1]), 6),
        "hist": round(h1, 6),
        "hist_last3": [round(h3, 6), round(h2, 6), round(h1, 6)],
        "hist_deltas": [round(h2 - h3, 6), round(h1 - h2, 6)],
        "completed_3m_count": int(len(bars)),
        "bar_ts": bar_ts_iso,
        "bar_close_ts": bar_close_ts.isoformat(),
        "reason": reason,
        "signal_id": signal_id,
    }


def target_symbol_for_direction(direction: Optional[str]) -> Optional[str]:
    text = str(direction or "").upper()
    if text in (DIR_UP, "UP", "UP_RED", LONG_SYMBOL):
        return LONG_SYMBOL
    if text in (DIR_DOWN, "DOWN", "DOWN_BLUE", INVERSE_SYMBOL):
        return INVERSE_SYMBOL
    return None


def opposite_symbol(symbol: Optional[str]) -> Optional[str]:
    if symbol == LONG_SYMBOL:
        return INVERSE_SYMBOL
    if symbol == INVERSE_SYMBOL:
        return LONG_SYMBOL
    return None


def make_direction_episode_id(direction: str, bar_ts: Optional[str] = None) -> str:
    ts = bar_ts or datetime.now().isoformat()
    return f"EP:{direction}:{ts}"


def net_pnl_pct_vs_entry(
    symbol: str,
    entry_price: float,
    current_price: float,
    quantity: int,
) -> float:
    """Net PnL % of entry notional (full round-trip costs via TradeCostEngine)."""
    if entry_price <= 0 or quantity <= 0 or current_price <= 0:
        return 0.0
    from app.trading.trading_cost_engine import TradeCostEngine

    breakdown = TradeCostEngine().compute_net_pnl(
        symbol=symbol,
        entry_price=float(entry_price),
        exit_price=float(current_price),
        quantity=int(quantity),
        buy_order_type="market",
        sell_order_type="market",
    )
    notional = float(entry_price) * int(quantity)
    return float(breakdown.get("net_pnl") or 0.0) / notional * 100.0


def check_tp_sl(
    symbol: str,
    entry_price: float,
    current_price: float,
    quantity: int,
    *,
    tp_pct: float = TP_NET_PCT,
    sl_pct: float = SL_NET_PCT,
) -> Optional[str]:
    """Return TP_EXIT / SL_EXIT when net PnL vs entry crosses thresholds, else None."""
    pct = net_pnl_pct_vs_entry(symbol, entry_price, current_price, quantity)
    if pct <= sl_pct:
        return EXIT_SL
    if pct >= tp_pct:
        return EXIT_TP
    return None


def _session_vwap(bars: pd.DataFrame) -> Optional[float]:
    if bars is None or bars.empty:
        return None
    work = bars.copy()
    for col in ("high", "low", "close"):
        if col not in work.columns:
            return None
    typical = (
        pd.to_numeric(work["high"], errors="coerce")
        + pd.to_numeric(work["low"], errors="coerce")
        + pd.to_numeric(work["close"], errors="coerce")
    ) / 3.0
    if "volume" in work.columns:
        vol = pd.to_numeric(work["volume"], errors="coerce").fillna(0.0).clip(lower=0.0)
        if float(vol.sum()) > 0:
            return float((typical * vol).sum() / vol.sum())
    return float(typical.mean())


def _ema_last(closes: pd.Series, span: int = 5) -> Optional[float]:
    closes = pd.to_numeric(closes, errors="coerce").dropna()
    if len(closes) < span:
        return None
    return float(closes.ewm(span=span, adjust=False).mean().iloc[-1])


def _hist_series_from_bars(bars: pd.DataFrame) -> Optional[pd.Series]:
    if bars is None or len(bars) < MACD_SLOW:
        return None
    closes = pd.to_numeric(bars["close"], errors="coerce").dropna()
    comps = macd_components(closes)
    return comps.get("hist")


def _contraction_then_reexpansion(
    post_tp_hist: list[float],
    direction: str,
) -> tuple[bool, bool]:
    """Return (hist_contracted, reexpansion_ok).

    Contraction: at least one bar after TP where |hist| declined (1–3 bar window).
    Re-expansion: last two consecutive completed-bar moves resume in episode direction.
    """
    if len(post_tp_hist) < 3:
        return False, False
    # Scan for a contraction of 1–3 bars before the final two rising/falling bars
    contracted = False
    # Look at the stretch excluding the final 2 deltas (need indices 0..-3)
    scan_end = len(post_tp_hist) - 2
    for start in range(max(0, scan_end - 3), scan_end):
        window = post_tp_hist[start:scan_end]
        if len(window) < 2:
            continue
        if all(abs(window[i + 1]) < abs(window[i]) for i in range(len(window) - 1)):
            contracted = True
            break
        # Slowed growth: successive absolute deltas shrink while still same sign
        if len(window) >= 3:
            d0 = abs(window[1] - window[0])
            d1 = abs(window[2] - window[1])
            if d1 < d0:
                contracted = True
                break
    h1, h2, h3 = post_tp_hist[-1], post_tp_hist[-2], post_tp_hist[-3]
    if direction == DIR_UP:
        reexpand = h1 > h2 > h3
    elif direction == DIR_DOWN:
        reexpand = h1 < h2 < h3
    else:
        reexpand = False
    return contracted, reexpand


def evaluate_continuation_reentry(
    df_1m: Optional[pd.DataFrame],
    *,
    direction: str,
    episode: dict[str, Any],
    now: Optional[datetime] = None,
    enabled: bool = CONTINUATION_REENTRY_ENABLED,
) -> dict[str, Any]:
    """Gate CONTINUATION_REENTRY after TP (same direction, once per episode).

    Does not alter Strategy B first-turn detection. Same color alone is never enough.
    """
    hist_last3: list[float] = []
    out: dict[str, Any] = {
        "eligible": False,
        "block_reason": "INIT",
        "bars_since_tp": 0,
        "hist_contracted": False,
        "hist_last3": hist_last3,
        "hist_recovery_ok": False,
        "above_ema5": False,
        "above_vwap": False,
        "no_lower_lows": False,
        "chase_ok": False,
        "signal_id": None,
    }
    if not enabled:
        out["block_reason"] = "REENTRY_DISABLED"
        return out
    if not episode:
        out["block_reason"] = "NO_EPISODE"
        return out
    if episode.get("sl_lock"):
        out["block_reason"] = "SL_LOCK"
        return out
    if episode.get("continuation_reentry_used"):
        out["block_reason"] = "REENTRY_ALREADY_USED"
        return out
    if not episode.get("tp_at"):
        out["block_reason"] = "NO_TP_YET"
        return out
    ep_dir = str(episode.get("direction") or "")
    if ep_dir not in (DIR_UP, DIR_DOWN) or ep_dir != direction:
        out["block_reason"] = "DIRECTION_MISMATCH"
        return out

    bars = resample_completed_3m(df_1m, now=now)
    if bars.empty:
        out["block_reason"] = "NO_BARS"
        return out

    tp_bar_ts = str(episode.get("tp_bar_ts") or "")
    if tp_bar_ts:
        try:
            tp_ts = pd.Timestamp(tp_bar_ts)
            post = bars[pd.to_datetime(bars["datetime"]) > tp_ts]
        except Exception:
            post = bars.iloc[0:0]
    else:
        post = bars.iloc[0:0]
    bars_since_tp = int(len(post))
    out["bars_since_tp"] = bars_since_tp
    if bars_since_tp < 1:
        out["block_reason"] = "NEED_1_BAR_AFTER_TP"
        return out

    hist = _hist_series_from_bars(bars)
    if hist is None or len(hist) < 3:
        out["block_reason"] = "HIST_INSUFFICIENT"
        return out
    # Align hist index with bars
    hist_vals = [float(x) for x in hist.tolist()]
    # post-TP hist: last bars_since_tp values (bars after tp correspond to tail of hist)
    post_hist = hist_vals[-bars_since_tp:] if bars_since_tp <= len(hist_vals) else hist_vals[:]
    # Include hist at TP bar as reference start for contraction scan when available
    if bars_since_tp < len(hist_vals):
        tp_hist_val = float(hist_vals[-(bars_since_tp + 1)])
        scan_hist = [tp_hist_val] + post_hist
    else:
        scan_hist = post_hist
    hist_last3 = [round(float(hist_vals[i]), 6) for i in range(-3, 0)]
    out["hist_last3"] = hist_last3

    # Display direction must still match episode (no opposite pattern)
    h1, h2, h3 = hist_vals[-1], hist_vals[-2], hist_vals[-3]
    pattern = signed_hist_two_turn_pattern(h1, h2, h3)
    if pattern != direction:
        out["block_reason"] = f"NOT_SAME_COLOR:{pattern}"
        return out

    contracted, reexpand = _contraction_then_reexpansion(scan_hist, direction)
    out["hist_contracted"] = contracted
    if not contracted:
        out["block_reason"] = "NO_HIST_CONTRACTION"
        return out
    if not reexpand:
        out["block_reason"] = "NO_HIST_REEXPANSION"
        return out

    tp_hist_max = float(episode.get("tp_hist_max_abs") or 0.0)
    cur_abs = abs(h1)
    recovery_ok = tp_hist_max <= 0 or cur_abs >= tp_hist_max * HIST_RECOVERY_RATIO
    out["hist_recovery_ok"] = recovery_ok
    if not recovery_ok:
        out["block_reason"] = (
            f"HIST_BELOW_70PCT({cur_abs:.4f}<{tp_hist_max * HIST_RECOVERY_RATIO:.4f})"
        )
        return out

    closes = pd.to_numeric(bars["close"], errors="coerce")
    ema5 = _ema_last(closes, 5)
    vwap = _session_vwap(bars)
    px = float(closes.iloc[-1])
    if direction == DIR_UP:
        above_ema = ema5 is not None and px > ema5
        above_vwap = vwap is not None and px > vwap
    else:
        # DOWN / SOL: symmetric — price below 5EMA and VWAP
        above_ema = ema5 is not None and px < ema5
        above_vwap = vwap is not None and px < vwap
    out["above_ema5"] = above_ema
    out["above_vwap"] = above_vwap
    if not above_ema:
        out["block_reason"] = "EMA5_FAIL"
        return out
    if not above_vwap:
        out["block_reason"] = "VWAP_FAIL"
        return out

    # Last 2 completed 3m bars did not make lower lows (UP) / higher highs (DOWN)
    if len(bars) < 2:
        out["block_reason"] = "NEED_2_BARS_STRUCTURE"
        return out
    low1 = float(pd.to_numeric(bars["low"].iloc[-1], errors="coerce"))
    low2 = float(pd.to_numeric(bars["low"].iloc[-2], errors="coerce"))
    high1 = float(pd.to_numeric(bars["high"].iloc[-1], errors="coerce"))
    high2 = float(pd.to_numeric(bars["high"].iloc[-2], errors="coerce"))
    if direction == DIR_UP:
        structure_ok = not (low1 < low2)  # did not lower lows
    else:
        structure_ok = not (high1 > high2)  # did not higher highs
    out["no_lower_lows"] = structure_ok
    if not structure_ok:
        out["block_reason"] = "STRUCTURE_BREAK"
        return out

    # Chase width vs TP-time pivot (documented threshold CHASE_MAX_PCT)
    pivot = episode.get("tp_pivot_price")
    try:
        pivot_f = float(pivot) if pivot is not None else None
    except Exception:
        pivot_f = None
    if pivot_f is None or pivot_f <= 0:
        pivot_f = float(episode.get("tp_hynix_price") or px)
    if direction == DIR_UP:
        chase_ok = px <= pivot_f * (1.0 + CHASE_MAX_PCT / 100.0)
    else:
        chase_ok = px >= pivot_f * (1.0 - CHASE_MAX_PCT / 100.0)
    out["chase_ok"] = chase_ok
    if not chase_ok:
        out["block_reason"] = f"CHASE_TOO_WIDE(>{CHASE_MAX_PCT}%)"
        return out

    bar_ts = pd.Timestamp(bars["datetime"].iloc[-1]).isoformat()
    ep_id = str(episode.get("id") or make_direction_episode_id(direction, bar_ts))
    out["eligible"] = True
    out["block_reason"] = None
    out["signal_id"] = f"MACD_CONT:{ep_id}:{bar_ts}"
    return out


def snapshot_tp_context(
    df_1m: Optional[pd.DataFrame],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Capture hist/pivot context at TP time for later continuation gates."""
    bars = resample_completed_3m(df_1m, now=now)
    empty = {
        "tp_bar_ts": None,
        "tp_hist_max_abs": 0.0,
        "tp_hynix_price": None,
        "tp_pivot_price": None,
        "hist_last3": [],
    }
    if bars.empty:
        return empty
    hist = _hist_series_from_bars(bars)
    hist_vals = [float(x) for x in hist.tolist()] if hist is not None else []
    # Max |hist| over last 5 completed bars (just before / at TP)
    window = hist_vals[-5:] if hist_vals else []
    tp_hist_max = max((abs(x) for x in window), default=0.0)
    last = bars.iloc[-1]
    px = float(pd.to_numeric(last["close"], errors="coerce") or 0)
    # Pivot: UP chase uses recent high; caller picks side — store both via high
    pivot_high = float(pd.to_numeric(bars["high"].iloc[-3:], errors="coerce").max()) if len(bars) >= 1 else px
    pivot_low = float(pd.to_numeric(bars["low"].iloc[-3:], errors="coerce").min()) if len(bars) >= 1 else px
    return {
        "tp_bar_ts": pd.Timestamp(last["datetime"]).isoformat(),
        "tp_hist_max_abs": float(tp_hist_max),
        "tp_hynix_price": px,
        "tp_pivot_high": pivot_high,
        "tp_pivot_low": pivot_low,
        "hist_last3": [round(x, 6) for x in hist_vals[-3:]] if hist_vals else [],
    }
