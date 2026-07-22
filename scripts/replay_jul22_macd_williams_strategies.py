"""Read-only Jul-22 replay: MACD/Williams A–E + Strategy F + weighted controller.

Primary comparison focus: 3m MACD alone (A) | Williams→MACD (C) | Strategy F | Weighted (W).

Fill realism (all strategies):
  - Signals only on completed bars (1m lead for F; 3m confirms always completed).
  - No same-timestamp fill as signal confirmation (except unrealistic immediate contrast).
  - Scenarios:
      immediate  — theoretical fill at signal bar close (UNREALISTIC contrast only)
      delay_1m   — next 1m open after signal (base)
      delay_1m_cons — next 1m open + 0.05% adverse  ← recommendation basis
      delay_2m   — first 1m open >= signal+2m + 0.10% adverse (stress)

Strategy F:
  1) 1m leading 2-of-3 → 25% probe (WR dir change; MACD hist 2 bars same dir; VWAP|5EMA break)
  2) 3m MACD same-dir confirm → scale to 100%
  3) Leading invalid within 2 minutes → exit probe
  4) 3m MACD opposite confirm → full exit then opposite ETF

Never places broker orders. Does not modify app trading code.

Usage:
    python scripts/replay_jul22_macd_williams_strategies.py
    python scripts/replay_jul22_macd_williams_strategies.py --refetch
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.services import hynix_switch_engine as engine  # noqa: E402
from app.trading import early_trend_detector as etd  # noqa: E402
from app.trading import early_trend_live_feed as feed  # noqa: E402
from app.trading.etf_entry_confirmation import (  # noqa: E402
    compute_etf_breakouts,
    compute_etf_vwap,
    is_swing_structure_broken_against,
    resolve_window_directions,
    trade_aligned_window_directions,
)
from app.trading.hynix_fast_trend import compute_fast_trend_signal  # noqa: E402
from app.trading.hynix_symbols import (  # noqa: E402
    LONG_SYMBOL,
    SHORT_SYMBOL as INVERSE_SYMBOL,
    SIGNAL_SYMBOL,
)
from app.trading.hynix_switch_risk_gate import is_new_entry_allowed  # noqa: E402
from app.trading.range_weighted_optimize import (  # noqa: E402
    classify_intraday_regime,
    daily_loss_limit_reached,
    get_range_weighted_config,
    load_optimized_config,
)

# ── constants ──────────────────────────────────────────────────────────────
SESSION_DATE = "2026-07-22"
INITIAL_CASH = 10_000_000.0
RT_COST_PCT = 0.05  # fee+spread baseline (% of entry notional), applied on close
FORCE_EXIT = datetime(2026, 7, 22, 15, 15, 0)
ENTRY_CUTOFF = datetime(2026, 7, 22, 14, 50, 0)
DOWN_MARK = datetime(2026, 7, 22, 10, 27, 0)
REBOUND_MARK = datetime(2026, 7, 22, 12, 12, 0)

CACHE_DIR = ROOT / "data" / "cache"
STATE_DIR = ROOT / "data" / "state"
CACHE_FILES = {
    SIGNAL_SYMBOL: CACHE_DIR / "replay_20260722_hynix_1m.csv",
    LONG_SYMBOL: CACHE_DIR / "replay_20260722_long_1m.csv",
    INVERSE_SYMBOL: CACHE_DIR / "replay_20260722_inverse_1m.csv",
}

# Fill scenario definitions
FILL_SCENARIOS = {
    "immediate": {
        "label": "즉시체결(비현실·대조)",
        "delay_min": 0,
        "adverse_pct": 0.0,
        "unrealistic": True,
        "note": "signal 3m close 시각에 ETF close로 체결 — 같은 시각 체결 금지 규칙 위반(대조용)",
    },
    "delay_1m": {
        "label": "1분지연(다음open)",
        "delay_min": 1,
        "adverse_pct": 0.0,
        "unrealistic": False,
        "note": "signal 확인 후 다음 1m open",
    },
    "delay_1m_cons": {
        "label": "1분지연+0.05%adverse(보수)",
        "delay_min": 1,
        "adverse_pct": 0.05,
        "unrealistic": False,
        "note": "추천 기준 — next 1m open + 0.05% adverse",
    },
    "delay_2m": {
        "label": "2분지연+0.10%adverse(스트레스)",
        "delay_min": 2,
        "adverse_pct": 0.10,
        "unrealistic": False,
        "note": "signal+2m 이후 첫 1m open + 0.10% adverse",
    },
}


# ── data load ──────────────────────────────────────────────────────────────
def _dense_hour_anchors() -> list[str]:
    """Every 10 minutes 09:10–15:30 so KIS chunk gaps are filled (script-local only)."""
    anchors: list[str] = []
    for h in range(9, 16):
        for m in range(0, 60, 10):
            if h == 9 and m == 0:
                continue
            if h == 15 and m > 30:
                break
            anchors.append(f"{h:02d}{m:02d}00")
    if "153000" not in anchors:
        anchors.append("153000")
    return anchors


def fetch_full_day_1min_dense(symbol: str, mode: str = "mock") -> pd.DataFrame:
    """Same KIS endpoint as replay_today_weighted_range.fetch_full_day_1min, denser anchors."""
    from app.trading.kis_client import create_kis_client

    client = create_kis_client(mode)
    if client is None:
        raise RuntimeError("KIS client unavailable")
    rows: dict[str, dict] = {}
    tr_id = "FHKST03010200"
    url = f"{client.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    for hour in _dense_hour_anchors():
        headers = client._auth_headers(tr_id)
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": hour,
            "FID_PW_DATA_INCU_YN": "N",
        }
        try:
            resp = client._get(url, headers=headers, params=params, timeout=(3, 12))
            resp.raise_for_status()
            for row in resp.json().get("output2", []):
                t = str(row.get("stck_cntg_hour") or "").zfill(6)
                close = float(row.get("stck_prpr") or 0)
                if close <= 0:
                    continue
                rows[t] = {
                    "time": t,
                    "open": float(row.get("stck_oprc") or close),
                    "high": float(row.get("stck_hgpr") or close),
                    "low": float(row.get("stck_lwpr") or close),
                    "close": close,
                    "volume": int(row.get("cntg_vol") or 0),
                }
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(list(rows.values()))
    df["datetime"] = pd.to_datetime(
        SESSION_DATE + " " + df["time"].str[:2] + ":" + df["time"].str[2:4] + ":" + df["time"].str[4:6],
        errors="coerce",
    )
    return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)


def fetch_and_cache(refetch: bool = False) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    need_fetch = refetch or any(not p.exists() for p in CACHE_FILES.values())
    if not need_fetch:
        for sym, path in CACHE_FILES.items():
            df = pd.read_csv(path)
            df["datetime"] = pd.to_datetime(df["datetime"])
            out[sym] = df.sort_values("datetime").reset_index(drop=True)
            print(f"  cache {sym}: {len(df)} bars {df['datetime'].iloc[0]} → {df['datetime'].iloc[-1]}")
        # If sparse, auto-refetch once
        if min(len(v) for v in out.values()) < 350:
            print("  cache sparse (<350 bars) — refetching with dense anchors…")
            need_fetch = True
        else:
            return out

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for sym, path in CACHE_FILES.items():
        df = fetch_full_day_1min_dense(sym, mode="mock")
        if df is None or df.empty:
            # fallback to existing helper
            from scripts.replay_today_weighted_range import fetch_full_day_1min

            df = fetch_full_day_1min(sym, mode="mock")
        if df is None or df.empty:
            raise RuntimeError(f"KIS fetch empty for {sym}")
        df = df.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
        df = df[df["datetime"].dt.strftime("%Y-%m-%d") == SESSION_DATE].reset_index(drop=True)
        # Merge with prior cache if denser fetch still has holes
        if path.exists():
            old = pd.read_csv(path)
            old["datetime"] = pd.to_datetime(old["datetime"])
            df = (
                pd.concat([old, df], ignore_index=True)
                .drop_duplicates("datetime", keep="last")
                .sort_values("datetime")
                .reset_index(drop=True)
            )
            df = df[df["datetime"].dt.strftime("%Y-%m-%d") == SESSION_DATE].reset_index(drop=True)
        df.to_csv(path, index=False)
        out[sym] = df
        print(f"  fetched {sym}: {len(df)} bars → {path.name}")
    return out


def resample_3m(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Resample 1m→3m and keep only buckets with all 3 constituent minutes present."""
    idx = df_1m.set_index("datetime")
    agg = idx.resample("3min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    counts = idx["close"].resample("3min").count()
    agg = agg[counts >= 3].dropna(subset=["close"]).reset_index()
    return agg


def completed_3m_asof(df_3m: pd.DataFrame, signal_close: datetime) -> pd.DataFrame:
    """Bars whose window has fully closed by signal_close (inclusive of that close)."""
    # bar datetime is window start; complete when start+3m <= signal_close
    return df_3m[df_3m["datetime"] + timedelta(minutes=3) <= signal_close].copy()


def macd_series(closes: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()
    hist = macd - sig
    return macd, sig, hist


def williams_series(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> pd.Series:
    hh = highs.rolling(period).max()
    ll = lows.rolling(period).min()
    span = (hh - ll).replace(0.0, np.nan)
    return (hh - closes) / span * -100.0


# ── fill helpers ───────────────────────────────────────────────────────────
def _open_at_or_after(df: pd.DataFrame, ts: datetime) -> Optional[tuple[datetime, float]]:
    rows = df[df["datetime"] >= ts]
    if rows.empty:
        return None
    r = rows.iloc[0]
    return r["datetime"].to_pydatetime() if hasattr(r["datetime"], "to_pydatetime") else r["datetime"], float(r["open"])


def _close_at(df: pd.DataFrame, ts: datetime) -> Optional[float]:
    minute = ts.replace(second=0, microsecond=0)
    row = df[df["datetime"] == minute]
    if row.empty:
        return None
    return float(row.iloc[0]["close"])


def resolve_fill(
    df: pd.DataFrame,
    signal_time: datetime,
    side: str,
    scenario: str,
) -> Optional[tuple[datetime, float]]:
    """Return (fill_time, fill_price) under scenario rules.

    immediate: ETF close at signal_time minute (UNREALISTIC — same-bar)
    delay_1m / delay_1m_cons: first 1m open STRICTLY after signal minute
    delay_2m: first 1m open at/after signal_minute + 2 minutes
    """
    cfg = FILL_SCENARIOS[scenario]
    sig_min = signal_time.replace(second=0, microsecond=0)

    if scenario == "immediate":
        px = _close_at(df, sig_min)
        if px is None:
            # fallback: last available close at/before
            prev = df[df["datetime"] <= sig_min]
            if prev.empty:
                return None
            px = float(prev.iloc[-1]["close"])
            fill_t = prev.iloc[-1]["datetime"]
            fill_t = fill_t.to_pydatetime() if hasattr(fill_t, "to_pydatetime") else fill_t
        else:
            fill_t = sig_min
    else:
        delay = int(cfg["delay_min"])
        if delay <= 1:
            # next 1m open AFTER signal minute (never same timestamp)
            target = sig_min + timedelta(minutes=1)
        else:
            target = sig_min + timedelta(minutes=delay)
        got = _open_at_or_after(df, target)
        if got is None:
            return None
        fill_t, px = got
        # Enforce: fill time must be strictly after signal confirmation
        if fill_t <= sig_min:
            got2 = _open_at_or_after(df, sig_min + timedelta(minutes=1))
            if got2 is None:
                return None
            fill_t, px = got2

    adv = float(cfg["adverse_pct"]) / 100.0
    if adv > 0:
        px = px * (1.0 + adv) if side == "BUY" else px * (1.0 - adv)
    return fill_t, float(px)


# ── trade / metrics ────────────────────────────────────────────────────────
@dataclass
class Trade:
    strategy: str
    scenario: str
    symbol: str
    direction: str
    signal_time: str
    order_time: str
    fill_price: float
    exit_signal_time: str
    exit_time: str
    exit_price: float
    exit_reason: str
    qty: int
    gross_pnl: float
    cost: float
    net_pnl: float
    held_seconds: float
    wrong_direction: bool
    size_pct: float = 1.0


@dataclass
class RunResult:
    strategy: str
    scenario: str
    trades: list[Trade] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    probe_starts: int = 0
    probe_successes: int = 0  # scaled to full
    probe_invalidated: int = 0
    early_capture_secs: list = field(default_factory=list)  # signal→fill lag


def _rt_cost(entry_price: float, qty: int) -> float:
    return entry_price * qty * (RT_COST_PCT / 100.0)


def _underlying_move_dir(hynix: pd.DataFrame, t0: datetime, t1: datetime) -> Optional[str]:
    a = hynix[hynix["datetime"] <= t0]
    b = hynix[hynix["datetime"] <= t1]
    if a.empty or b.empty:
        return None
    p0, p1 = float(a.iloc[-1]["close"]), float(b.iloc[-1]["close"])
    if p1 > p0 * 1.0001:
        return "UP"
    if p1 < p0 * 0.9999:
        return "DOWN"
    return None


def compute_metrics(rr: RunResult, hynix: pd.DataFrame) -> dict[str, Any]:
    trades = rr.trades
    n = len(trades)
    lev = sum(1 for t in trades if t.symbol == LONG_SYMBOL)
    inv = n - lev
    wins = sum(1 for t in trades if t.net_pnl > 0)
    gp = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
    gl = abs(sum(t.gross_pnl for t in trades if t.gross_pnl < 0))
    pf = (gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0)
    net = sum(t.net_pnl for t in trades)
    gross_all = sum(t.gross_pnl for t in trades)
    cost = sum(t.cost for t in trades)
    holds = [t.held_seconds for t in trades]
    equity = INITIAL_CASH
    peak = equity
    mdd = 0.0
    for t in sorted(trades, key=lambda x: x.exit_time):
        equity += t.net_pnl
        peak = max(peak, equity)
        mdd = min(mdd, (equity / peak - 1.0) * 100.0)

    wrong = [t for t in trades if t.wrong_direction]
    first_down = None
    first_reb = None
    for t in trades:
        try:
            st = datetime.fromisoformat(t.signal_time.replace(" ", "T") if "T" not in t.signal_time else t.signal_time)
        except Exception:
            st = datetime.strptime(t.signal_time[:19], "%Y-%m-%d %H:%M:%S")
        if first_down is None and t.direction == "DOWN" and st >= DOWN_MARK:
            first_down = t.signal_time
        if first_reb is None and t.direction == "UP" and st >= REBOUND_MARK:
            first_reb = t.signal_time

    return {
        "strategy": rr.strategy,
        "scenario": rr.scenario,
        "scenario_label": FILL_SCENARIOS[rr.scenario]["label"],
        "round_trips": n,
        "lev_trades": lev,
        "inv_trades": inv,
        "win_rate_pct": round(wins / n * 100.0, 2) if n else 0.0,
        "pf": round(pf, 3) if pf < 900 else None,
        "pf_raw": pf,
        "gross_pnl": round(gross_all, 0),
        "net_pnl": round(net, 0),
        "return_pct": round(net / INITIAL_CASH * 100.0, 4),
        "total_cost": round(cost, 0),
        "cost_gross_ratio_pct": round(cost / gp * 100.0, 2) if gp > 0 else None,
        "mdd_pct": round(mdd, 4),
        "avg_hold_sec": round(statistics.mean(holds), 1) if holds else 0.0,
        "wrong_direction_count": len(wrong),
        "wrong_direction_trades": [
            {
                "signal_time": t.signal_time,
                "direction": t.direction,
                "symbol": t.symbol,
                "net_pnl": round(t.net_pnl, 0),
                "exit_reason": t.exit_reason,
            }
            for t in wrong
        ],
        "first_down_capture_after_1027": first_down,
        "first_rebound_capture_after_1212": first_reb,
        "probe_starts": rr.probe_starts,
        "probe_successes": rr.probe_successes,
        "probe_invalidated": rr.probe_invalidated,
        "probe_success_rate_pct": round(
            rr.probe_successes / rr.probe_starts * 100.0, 1
        )
        if rr.probe_starts
        else None,
        "avg_early_capture_sec": round(statistics.mean(rr.early_capture_secs), 1)
        if rr.early_capture_secs
        else None,
        "notes": rr.notes,
        "trades": [asdict(t) for t in trades],
    }


# ── indicator signal stream (3m completed closes only) ─────────────────────
@dataclass
class SignalEvent:
    signal_time: datetime  # 3m bar close time
    direction: str  # UP / DOWN / FLAT / EXIT / SCALE
    kind: str
    size_pct: float = 1.0
    meta: dict = field(default_factory=dict)


def _build_indicator_frame(hynix_1m: pd.DataFrame) -> pd.DataFrame:
    bars = resample_3m(hynix_1m)
    # Only fully completed bars relative to last available 1m
    last_1m = hynix_1m["datetime"].iloc[-1]
    bars = bars[bars["datetime"] + timedelta(minutes=3) <= last_1m].reset_index(drop=True)
    closes = pd.to_numeric(bars["close"], errors="coerce")
    highs = pd.to_numeric(bars["high"], errors="coerce")
    lows = pd.to_numeric(bars["low"], errors="coerce")
    macd, sig, hist = macd_series(closes)
    wr = williams_series(highs, lows, closes)
    bars = bars.copy()
    bars["macd"] = macd
    bars["macd_sig"] = sig
    bars["hist"] = hist
    bars["wr"] = wr
    bars["close_time"] = bars["datetime"] + timedelta(minutes=3)
    bars["hist_delta"] = bars["hist"].diff()
    return bars


def signals_A(bars: pd.DataFrame) -> list[SignalEvent]:
    """MACD line/signal cross alone."""
    ev: list[SignalEvent] = []
    for i in range(1, len(bars)):
        if i < 26:
            continue
        row = bars.iloc[i]
        prev = bars.iloc[i - 1]
        ct = row["close_time"].to_pydatetime() if hasattr(row["close_time"], "to_pydatetime") else row["close_time"]
        if ct > ENTRY_CUTOFF and ct < FORCE_EXIT:
            # still allow exit signals after cutoff
            pass
        cross_up = prev["macd"] <= prev["macd_sig"] and row["macd"] > row["macd_sig"]
        cross_dn = prev["macd"] >= prev["macd_sig"] and row["macd"] < row["macd_sig"]
        if cross_up:
            ev.append(SignalEvent(ct, "UP", "MACD_CROSS_UP"))
        elif cross_dn:
            ev.append(SignalEvent(ct, "DOWN", "MACD_CROSS_DOWN"))
    return ev


def signals_B(bars: pd.DataFrame) -> list[SignalEvent]:
    """Signed hist 2-turn — shared with live `macd_hynix_strategy`."""
    from app.trading.macd_hynix_strategy import (
        DIR_DOWN,
        DIR_UP,
        collect_signed_hist_two_turn_signals,
    )

    hist = [float(x) for x in bars["hist"].tolist()]
    close_times = []
    for _, row in bars.iterrows():
        ct = row["close_time"]
        close_times.append(
            ct.to_pydatetime() if hasattr(ct, "to_pydatetime") else ct
        )
    events: list[SignalEvent] = []
    for ev in collect_signed_hist_two_turn_signals(hist, close_times=close_times):
        direction = "UP" if ev["direction"] == DIR_UP else "DOWN"
        kind = "HIST_2UP_TURN" if ev["direction"] == DIR_UP else "HIST_2DN_TURN"
        events.append(SignalEvent(ev["close_time"], direction, kind))
    return events


def signals_C(bars: pd.DataFrame) -> list[SignalEvent]:
    """Williams lead → MACD confirm within 2 completed bars."""
    ev: list[SignalEvent] = []
    pending: Optional[dict] = None
    for i in range(1, len(bars)):
        if i < 26:
            continue
        row = bars.iloc[i]
        prev = bars.iloc[i - 1]
        ct = row["close_time"].to_pydatetime() if hasattr(row["close_time"], "to_pydatetime") else row["close_time"]
        wr, wr_p = row["wr"], prev["wr"]
        if pd.isna(wr) or pd.isna(wr_p):
            continue
        # Williams leads
        oversold_break = wr_p <= -80 and wr > -80
        overbought_break = wr_p >= -20 and wr < -20
        macd_up = prev["macd"] <= prev["macd_sig"] and row["macd"] > row["macd_sig"]
        macd_dn = prev["macd"] >= prev["macd_sig"] and row["macd"] < row["macd_sig"]
        # also accept hist sign flip as MACD confirm
        if not macd_up:
            macd_up = prev["hist"] <= 0 and row["hist"] > 0
        if not macd_dn:
            macd_dn = prev["hist"] >= 0 and row["hist"] < 0

        if pending is not None:
            age = i - pending["i"]
            if age > 2:
                pending = None
            elif pending["dir"] == "UP" and macd_up:
                ev.append(SignalEvent(ct, "UP", "WR_LEAD_MACD_UP", meta={"lead_i": pending["i"]}))
                pending = None
                continue
            elif pending["dir"] == "DOWN" and macd_dn:
                ev.append(SignalEvent(ct, "DOWN", "WR_LEAD_MACD_DN", meta={"lead_i": pending["i"]}))
                pending = None
                continue

        if oversold_break:
            if macd_up:
                ev.append(SignalEvent(ct, "UP", "WR_LEAD_MACD_UP_SAME"))
                pending = None
            else:
                pending = {"dir": "UP", "i": i}
        elif overbought_break:
            if macd_dn:
                ev.append(SignalEvent(ct, "DOWN", "WR_LEAD_MACD_DN_SAME"))
                pending = None
            else:
                pending = {"dir": "DOWN", "i": i}
    return ev


def signals_D(bars: pd.DataFrame) -> list[SignalEvent]:
    """MACD + Williams same direction → full size; else cash (exit)."""
    ev: list[SignalEvent] = []
    prev_state: Optional[str] = None
    for i in range(len(bars)):
        if i < 26:
            continue
        row = bars.iloc[i]
        ct = row["close_time"].to_pydatetime() if hasattr(row["close_time"], "to_pydatetime") else row["close_time"]
        wr = row["wr"]
        if pd.isna(wr) or pd.isna(row["hist"]):
            continue
        macd_up = row["hist"] > 0
        macd_dn = row["hist"] < 0
        wr_up = wr < -50
        wr_dn = wr > -50
        state = None
        if macd_up and wr_up:
            state = "UP"
        elif macd_dn and wr_dn:
            state = "DOWN"
        else:
            state = "CASH"
        if state != prev_state:
            if state in ("UP", "DOWN"):
                ev.append(SignalEvent(ct, state, "MACD_WR_ALIGN", size_pct=1.0))
            elif state == "CASH" and prev_state in ("UP", "DOWN"):
                ev.append(SignalEvent(ct, "EXIT", "MACD_WR_DISAGREE"))
            prev_state = state
    return ev


def signals_E(bars: pd.DataFrame) -> list[SignalEvent]:
    """Williams lead 30% probe → MACD confirm 100%; invalidate → exit/switch."""
    ev: list[SignalEvent] = []
    pending: Optional[dict] = None
    phase: Optional[str] = None  # None / PROBE_UP / FULL_UP / PROBE_DN / FULL_DN
    for i in range(1, len(bars)):
        if i < 26:
            continue
        row = bars.iloc[i]
        prev = bars.iloc[i - 1]
        ct = row["close_time"].to_pydatetime() if hasattr(row["close_time"], "to_pydatetime") else row["close_time"]
        wr, wr_p = row["wr"], prev["wr"]
        if pd.isna(wr) or pd.isna(wr_p) or pd.isna(row["hist"]):
            continue
        oversold_break = wr_p <= -80 and wr > -80
        overbought_break = wr_p >= -20 and wr < -20
        macd_up = (prev["hist"] <= 0 and row["hist"] > 0) or (
            prev["macd"] <= prev["macd_sig"] and row["macd"] > row["macd_sig"]
        )
        macd_dn = (prev["hist"] >= 0 and row["hist"] < 0) or (
            prev["macd"] >= prev["macd_sig"] and row["macd"] < row["macd_sig"]
        )
        wr_invalid_up = wr < -80  # back to oversold after long probe
        wr_invalid_dn = wr > -20

        # Invalidate / reverse
        if phase in ("PROBE_UP", "FULL_UP"):
            if macd_dn or wr_invalid_up or overbought_break:
                if macd_dn or overbought_break:
                    # switch
                    ev.append(SignalEvent(ct, "DOWN", "E_SWITCH_DN", size_pct=0.30 if not macd_dn else 1.0))
                    phase = "PROBE_DN" if not macd_dn else "FULL_DN"
                    pending = None
                else:
                    ev.append(SignalEvent(ct, "EXIT", "E_INVALID_UP"))
                    phase = None
                continue
            if phase == "PROBE_UP" and macd_up:
                ev.append(SignalEvent(ct, "SCALE", "E_CONFIRM_UP", size_pct=1.0, meta={"target": "UP"}))
                phase = "FULL_UP"
                continue
        if phase in ("PROBE_DN", "FULL_DN"):
            if macd_up or wr_invalid_dn or oversold_break:
                if macd_up or oversold_break:
                    ev.append(SignalEvent(ct, "UP", "E_SWITCH_UP", size_pct=0.30 if not macd_up else 1.0))
                    phase = "PROBE_UP" if not macd_up else "FULL_UP"
                    pending = None
                else:
                    ev.append(SignalEvent(ct, "EXIT", "E_INVALID_DN"))
                    phase = None
                continue
            if phase == "PROBE_DN" and macd_dn:
                ev.append(SignalEvent(ct, "SCALE", "E_CONFIRM_DN", size_pct=1.0, meta={"target": "DOWN"}))
                phase = "FULL_DN"
                continue

        if phase is None:
            if oversold_break:
                ev.append(SignalEvent(ct, "UP", "E_PROBE_UP", size_pct=0.30))
                phase = "PROBE_UP"
            elif overbought_break:
                ev.append(SignalEvent(ct, "DOWN", "E_PROBE_DN", size_pct=0.30))
                phase = "PROBE_DN"
    return ev


# ── generic signal executor ────────────────────────────────────────────────
def execute_signal_strategy(
    name: str,
    events: list[SignalEvent],
    hynix: pd.DataFrame,
    long_1m: pd.DataFrame,
    inv_1m: pd.DataFrame,
    scenario: str,
) -> RunResult:
    rr = RunResult(strategy=name, scenario=scenario)
    cash = INITIAL_CASH
    pos: Optional[dict] = None

    def etf_df(direction: str) -> pd.DataFrame:
        return long_1m if direction == "UP" else inv_1m

    def symbol_of(direction: str) -> str:
        return LONG_SYMBOL if direction == "UP" else INVERSE_SYMBOL

    def close_position(sig_t: datetime, reason: str) -> None:
        nonlocal cash, pos
        if pos is None:
            return
        fill = resolve_fill(etf_df(pos["direction"]), sig_t, "SELL", scenario)
        if fill is None:
            # force mark at last available
            df = etf_df(pos["direction"])
            px = float(df.iloc[-1]["close"])
            ft = df.iloc[-1]["datetime"]
            ft = ft.to_pydatetime() if hasattr(ft, "to_pydatetime") else ft
            adv = FILL_SCENARIOS[scenario]["adverse_pct"] / 100.0
            if adv:
                px *= 1.0 - adv
            fill = (ft, px)
            reason = reason + "|NO_FILL_FALLBACK"
        ft, px = fill
        qty = pos["qty"]
        gross = (px - pos["entry_price"]) * qty
        cost = _rt_cost(pos["entry_price"], qty)
        net = gross - cost
        cash += qty * px
        udir = _underlying_move_dir(hynix, datetime.fromisoformat(pos["signal_time"]), ft)
        wrong = bool(udir and udir != pos["direction"])
        rr.trades.append(
            Trade(
                strategy=name,
                scenario=scenario,
                symbol=pos["symbol"],
                direction=pos["direction"],
                signal_time=pos["signal_time"],
                order_time=pos["order_time"],
                fill_price=pos["entry_price"],
                exit_signal_time=sig_t.isoformat(sep=" "),
                exit_time=ft.isoformat(sep=" ") if not isinstance(ft, str) else ft,
                exit_price=px,
                exit_reason=reason,
                qty=qty,
                gross_pnl=gross,
                cost=cost,
                net_pnl=net,
                held_seconds=(ft - datetime.fromisoformat(pos["order_time"])).total_seconds(),
                wrong_direction=wrong,
                size_pct=pos.get("size_pct", 1.0),
            )
        )
        pos = None

    def open_position(sig_t: datetime, direction: str, size_pct: float, kind: str) -> None:
        nonlocal cash, pos
        if sig_t > ENTRY_CUTOFF:
            return
        sym = symbol_of(direction)
        fill = resolve_fill(etf_df(direction), sig_t, "BUY", scenario)
        if fill is None:
            return
        ft, px = fill
        notional = cash * size_pct
        qty = max(1, int(notional / px))
        if qty * px > cash:
            qty = int(cash / px)
        if qty < 1:
            return
        cash -= qty * px
        pos = {
            "symbol": sym,
            "direction": direction,
            "qty": qty,
            "entry_price": px,
            "signal_time": sig_t.isoformat(sep=" "),
            "order_time": ft.isoformat(sep=" ") if not isinstance(ft, str) else ft,
            "size_pct": size_pct,
            "kind": kind,
        }
        try:
            rr.early_capture_secs.append((ft - sig_t).total_seconds())
        except Exception:
            pass

    for ev in events:
        st = ev.signal_time
        if st >= FORCE_EXIT:
            break

        if ev.direction == "EXIT":
            close_position(st, ev.kind)
            continue

        if ev.direction == "SCALE" and pos is not None:
            target = ev.meta.get("target") or pos["direction"]
            if target != pos["direction"]:
                continue
            # scale to 100%: add qty so total ~ full cash+position value * 1.0
            fill = resolve_fill(etf_df(pos["direction"]), st, "BUY", scenario)
            if fill is None:
                continue
            ft, px = fill
            # target full size relative to INITIAL_CASH
            target_notional = INITIAL_CASH * 1.0
            cur_notional = pos["qty"] * pos["entry_price"]
            add_notional = max(0.0, target_notional - cur_notional)
            add_qty = int(min(cash, add_notional) / px)
            if add_qty >= 1:
                cash -= add_qty * px
                total_cost = pos["entry_price"] * pos["qty"] + px * add_qty
                pos["qty"] += add_qty
                pos["entry_price"] = total_cost / pos["qty"]
                pos["size_pct"] = 1.0
            continue

        if ev.direction not in ("UP", "DOWN"):
            continue

        if pos is not None and pos["direction"] != ev.direction:
            # opposite: exit then enter
            close_position(st, f"OPPOSITE:{ev.kind}")
            open_position(st, ev.direction, ev.size_pct, ev.kind)
        elif pos is None:
            open_position(st, ev.direction, ev.size_pct, ev.kind)
        # same direction while in position: ignore (no pyramid except E SCALE)

    # force exit 15:15
    if pos is not None:
        close_position(FORCE_EXIT, "15:15_FORCE_CLOSE")
    return rr


# ── Strategy F: 1m lead 2-of-3 probe → 3m MACD confirm ─────────────────────
def _build_1m_indicator_frame(hynix_1m: pd.DataFrame) -> pd.DataFrame:
    df = hynix_1m.copy().sort_values("datetime").reset_index(drop=True)
    closes = pd.to_numeric(df["close"], errors="coerce")
    highs = pd.to_numeric(df["high"], errors="coerce")
    lows = pd.to_numeric(df["low"], errors="coerce")
    macd, sig, hist = macd_series(closes)
    wr = williams_series(highs, lows, closes, period=14)
    ema5 = closes.ewm(span=5, adjust=False).mean()
    # session VWAP from completed bars only (cumulative)
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    typ = (highs + lows + closes) / 3.0
    cum_pv = (typ * vol).cumsum()
    cum_v = vol.replace(0, np.nan).cumsum()
    vwap = cum_pv / cum_v
    out = df.copy()
    out["macd"] = macd
    out["macd_sig"] = sig
    out["hist"] = hist
    out["wr"] = wr
    out["ema5"] = ema5
    out["vwap"] = vwap
    out["wr_dir"] = np.where(wr < -50, 1, np.where(wr > -50, -1, 0))
    return out


def run_strategy_F(
    hynix: pd.DataFrame,
    long_1m: pd.DataFrame,
    inv_1m: pd.DataFrame,
    scenario: str,
) -> RunResult:
    """1m leading (2/3→25% probe) + 3m MACD confirm/invalid/switch."""
    rr = RunResult(
        strategy="F: 1m lead→3m MACD",
        scenario=scenario,
        notes=[
            "1m lead requires 2 of 3: WR direction change, hist 2-bar same dir, VWAP|EMA5 break",
            "Probe 25%; 3m MACD same-dir confirm → 100%; lead invalid ≤2m → exit probe",
            "3m MACD opposite → full exit then opposite ETF",
        ],
    )
    bars_1m = _build_1m_indicator_frame(hynix)
    bars_3m = _build_indicator_frame(hynix)
    # map close_time → 3m row index for completed confirms
    close_to_3m = {
        (r["close_time"].to_pydatetime() if hasattr(r["close_time"], "to_pydatetime") else r["close_time"]): i
        for i, r in bars_3m.iterrows()
    }

    cash = INITIAL_CASH
    pos: Optional[dict] = None  # may be probe or full

    def etf_df(direction: str) -> pd.DataFrame:
        return long_1m if direction == "UP" else inv_1m

    def close_position(sig_t: datetime, reason: str) -> None:
        nonlocal cash, pos
        if pos is None:
            return
        fill = resolve_fill(etf_df(pos["direction"]), sig_t, "SELL", scenario)
        if fill is None:
            return
        ft, px = fill
        qty = pos["qty"]
        gross = (px - pos["entry_price"]) * qty
        cost = _rt_cost(pos["entry_price"], qty)
        net = gross - cost
        cash += qty * px
        udir = _underlying_move_dir(hynix, datetime.fromisoformat(pos["signal_time"]), ft)
        wrong = bool(udir and udir != pos["direction"])
        if pos.get("is_probe") and "INVALID" in reason:
            rr.probe_invalidated += 1
        rr.trades.append(
            Trade(
                strategy=rr.strategy,
                scenario=scenario,
                symbol=pos["symbol"],
                direction=pos["direction"],
                signal_time=pos["signal_time"],
                order_time=pos["order_time"],
                fill_price=pos["entry_price"],
                exit_signal_time=sig_t.isoformat(sep=" "),
                exit_time=ft.isoformat(sep=" "),
                exit_price=px,
                exit_reason=reason,
                qty=qty,
                gross_pnl=gross,
                cost=cost,
                net_pnl=net,
                held_seconds=(ft - datetime.fromisoformat(pos["order_time"])).total_seconds(),
                wrong_direction=wrong,
                size_pct=pos.get("size_pct", 0.25),
            )
        )
        pos = None

    def open_probe(sig_t: datetime, direction: str) -> None:
        nonlocal cash, pos
        if sig_t > ENTRY_CUTOFF or pos is not None:
            return
        fill = resolve_fill(etf_df(direction), sig_t, "BUY", scenario)
        if fill is None:
            return
        ft, px = fill
        qty = max(1, int(cash * 0.25 / px))
        if qty * px > cash or qty < 1:
            return
        cash -= qty * px
        rr.probe_starts += 1
        rr.early_capture_secs.append((ft - sig_t).total_seconds())
        pos = {
            "symbol": LONG_SYMBOL if direction == "UP" else INVERSE_SYMBOL,
            "direction": direction,
            "qty": qty,
            "entry_price": px,
            "signal_time": sig_t.isoformat(sep=" "),
            "order_time": ft.isoformat(sep=" "),
            "size_pct": 0.25,
            "is_probe": True,
            "probe_signal_time": sig_t,
            "lead_votes": None,
        }

    def scale_to_full(sig_t: datetime) -> None:
        nonlocal cash, pos
        if pos is None or not pos.get("is_probe"):
            return
        fill = resolve_fill(etf_df(pos["direction"]), sig_t, "BUY", scenario)
        if fill is None:
            return
        ft, px = fill
        target = INITIAL_CASH * 1.0
        cur = pos["qty"] * pos["entry_price"]
        add_qty = int(min(cash, max(0.0, target - cur)) / px)
        if add_qty < 1:
            # still mark success if already near full
            pos["is_probe"] = False
            pos["size_pct"] = 1.0
            rr.probe_successes += 1
            return
        cash -= add_qty * px
        total = pos["entry_price"] * pos["qty"] + px * add_qty
        pos["qty"] += add_qty
        pos["entry_price"] = total / pos["qty"]
        pos["is_probe"] = False
        pos["size_pct"] = 1.0
        rr.probe_successes += 1

    def lead_flags(i: int) -> tuple[Optional[str], int, dict]:
        """Return (direction, votes, detail) for 1m bar i using completed bars only."""
        if i < 26:
            return None, 0, {}
        row = bars_1m.iloc[i]
        prev = bars_1m.iloc[i - 1]
        if pd.isna(row["wr"]) or pd.isna(row["hist"]) or pd.isna(prev["hist"]):
            return None, 0, {}
        # 1) Williams direction change
        wr_chg_up = int(prev["wr_dir"]) <= 0 and int(row["wr_dir"]) > 0
        wr_chg_dn = int(prev["wr_dir"]) >= 0 and int(row["wr_dir"]) < 0
        # 2) MACD hist 2 bars same dir
        h1, h0 = float(prev["hist"]), float(row["hist"])
        hist_up = h1 > 0 and h0 > 0
        hist_dn = h1 < 0 and h0 < 0
        # 3) price break VWAP or 5EMA same dir
        c, c_p = float(row["close"]), float(prev["close"])
        vwap, ema5 = float(row["vwap"]) if not pd.isna(row["vwap"]) else c, float(row["ema5"])
        vwap_p = float(prev["vwap"]) if not pd.isna(prev["vwap"]) else c_p
        ema5_p = float(prev["ema5"])
        break_up = (c_p <= vwap_p and c > vwap) or (c_p <= ema5_p and c > ema5)
        break_dn = (c_p >= vwap_p and c < vwap) or (c_p >= ema5_p and c < ema5)

        up_votes = sum([wr_chg_up, hist_up, break_up])
        dn_votes = sum([wr_chg_dn, hist_dn, break_dn])
        detail = {
            "wr_up": wr_chg_up, "hist_up": hist_up, "break_up": break_up,
            "wr_dn": wr_chg_dn, "hist_dn": hist_dn, "break_dn": break_dn,
        }
        if up_votes >= 2 and up_votes > dn_votes:
            return "UP", up_votes, detail
        if dn_votes >= 2 and dn_votes > up_votes:
            return "DOWN", dn_votes, detail
        return None, max(up_votes, dn_votes), detail

    def lead_invalid(i: int, direction: str) -> bool:
        """Probe invalidation: WR flip against, hist opposite, or lose VWAP&EMA5."""
        row = bars_1m.iloc[i]
        prev = bars_1m.iloc[i - 1] if i > 0 else row
        if pd.isna(row["wr"]) or pd.isna(row["hist"]):
            return False
        c = float(row["close"])
        vwap = float(row["vwap"]) if not pd.isna(row["vwap"]) else c
        ema5 = float(row["ema5"])
        if direction == "UP":
            wr_flip = int(row["wr_dir"]) < 0 and int(prev["wr_dir"]) > 0
            hist_opp = float(row["hist"]) < 0 and float(prev["hist"]) >= 0
            lose_struct = c < vwap and c < ema5
            return bool(wr_flip or hist_opp or lose_struct)
        wr_flip = int(row["wr_dir"]) > 0 and int(prev["wr_dir"]) < 0
        hist_opp = float(row["hist"]) > 0 and float(prev["hist"]) <= 0
        lose_struct = c > vwap and c > ema5
        return bool(wr_flip or hist_opp or lose_struct)

    def macd_3m_cross_at(ts: datetime) -> Optional[str]:
        """If a 3m bar just completed at ts, return cross direction else None."""
        if ts not in close_to_3m:
            return None
        i = close_to_3m[ts]
        if i < 1 or i < 26:
            return None
        row = bars_3m.iloc[i]
        prev = bars_3m.iloc[i - 1]
        cross_up = prev["macd"] <= prev["macd_sig"] and row["macd"] > row["macd_sig"]
        cross_dn = prev["macd"] >= prev["macd_sig"] and row["macd"] < row["macd_sig"]
        # also hist sign flip
        if not cross_up:
            cross_up = prev["hist"] <= 0 and row["hist"] > 0
        if not cross_dn:
            cross_dn = prev["hist"] >= 0 and row["hist"] < 0
        if cross_up:
            return "UP"
        if cross_dn:
            return "DOWN"
        return None

    for i in range(len(bars_1m)):
        ts = bars_1m.iloc[i]["datetime"]
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if ts.hour < 9:
            continue
        if ts >= FORCE_EXIT:
            if pos is not None:
                close_position(FORCE_EXIT, "15:15_FORCE_CLOSE")
            break

        # 3m MACD confirm / opposite (only at completed 3m close timestamps)
        macd3 = macd_3m_cross_at(ts)

        if pos is not None and macd3 is not None:
            if macd3 == pos["direction"]:
                if pos.get("is_probe"):
                    scale_to_full(ts)
            elif macd3 != pos["direction"]:
                # opposite: full exit then enter opposite
                close_position(ts, "F_3M_MACD_OPPOSITE")
                if ts <= ENTRY_CUTOFF:
                    # enter opposite at full size after opposite confirm
                    fill = resolve_fill(etf_df(macd3), ts, "BUY", scenario)
                    if fill is not None:
                        ft, px = fill
                        qty = max(1, int(cash * 1.0 / px))
                        if qty * px <= cash and qty >= 1:
                            cash -= qty * px
                            rr.early_capture_secs.append((ft - ts).total_seconds())
                            pos = {
                                "symbol": LONG_SYMBOL if macd3 == "UP" else INVERSE_SYMBOL,
                                "direction": macd3,
                                "qty": qty,
                                "entry_price": px,
                                "signal_time": ts.isoformat(sep=" "),
                                "order_time": ft.isoformat(sep=" "),
                                "size_pct": 1.0,
                                "is_probe": False,
                                "probe_signal_time": ts,
                            }
                continue

        # Probe invalidation within 2 minutes of leading signal
        if pos is not None and pos.get("is_probe"):
            pst = pos["probe_signal_time"]
            if (ts - pst).total_seconds() <= 120 and lead_invalid(i, pos["direction"]):
                close_position(ts, "F_LEAD_INVALID_2M")
                continue

        # New 1m lead probe when flat — fire only on fresh 2-of-3 qualification
        if pos is None and ts <= ENTRY_CUTOFF and i >= 1:
            direction, votes, _ = lead_flags(i)
            prev_dir, prev_votes, _ = lead_flags(i - 1)
            fresh = direction is not None and votes >= 2 and not (
                prev_dir == direction and prev_votes >= 2
            )
            if fresh and direction:
                open_probe(ts, direction)

    if pos is not None:
        close_position(FORCE_EXIT, "15:15_FORCE_CLOSE")
    return rr


# ── Weighted controller (minute-sampled, no intra-bar interp) ──────────────
def run_weighted(
    hynix: pd.DataFrame,
    long_1m: pd.DataFrame,
    inv_1m: pd.DataFrame,
    scenario: str,
) -> RunResult:
    """Approximate production weighted RANGE on completed 1m closes only.

    Limitations (documented):
      - Live 5s samples unavailable → window directions derived from successive
        1m closes (1/2/3/5 bar returns as 5/10/20/30s proxies). Coarser than live.
      - No linear interpolation inside 1m bars.
      - Entry/exit still gated by completed-bar information only; fills use scenario rules.
      - Strategy signals for WOC are evaluated at each 1m close (not only 3m), matching
        production cadence but without sub-minute data.
    """
    rr = RunResult(
        strategy="W: weighted RANGE",
        scenario=scenario,
        notes=[
            "WOC approximated on 1m closes only (no 5s tape, no intra-bar interp)",
            "5/10/20/30s window dirs proxied by 1/2/3/5 completed 1m returns",
            "Fills still follow scenario delay/slippage; no same-bar fill except immediate contrast",
        ],
    )
    load_optimized_config()
    cfg = get_range_weighted_config()
    day_regime = classify_intraday_regime(hynix)
    cash = INITIAL_CASH
    pos: Optional[dict] = None
    continuation: dict = {}
    episode_entries: set[str] = set()
    realized = 0.0

    # Precompute 3m for MACD confirm helper
    hynix_3m = resample_3m(hynix)

    times = list(hynix["datetime"])
    # Align indices
    long_idx = long_1m.set_index("datetime")
    inv_idx = inv_1m.set_index("datetime")
    h_idx = hynix.set_index("datetime")

    def px_close(df_idx: pd.DataFrame, t: datetime) -> Optional[float]:
        if t not in df_idx.index:
            return None
        return float(df_idx.loc[t]["close"])

    def minute_window_dirs(df_idx: pd.DataFrame, t: datetime) -> dict:
        """Proxy 5/10/20/30s slopes using 1/2/3/5 prior 1m closes (completed only)."""
        # only past closes
        hist = df_idx.loc[:t]["close"]
        if len(hist) < 6:
            return {5: None, 10: None, 20: None, 30: None}
        c = float(hist.iloc[-1])
        out = {}
        for sec, bars_back in ((5, 1), (10, 2), (20, 3), (30, 5)):
            p = float(hist.iloc[-(bars_back + 1)])
            if p <= 0:
                out[sec] = None
            elif c > p * 1.00005:
                out[sec] = "UP"
            elif c < p * 0.99995:
                out[sec] = "DOWN"
            else:
                out[sec] = None
        return out

    def do_exit(sig_t: datetime, reason: str) -> None:
        nonlocal cash, pos, realized
        if pos is None:
            return
        df = long_1m if pos["symbol"] == LONG_SYMBOL else inv_1m
        fill = resolve_fill(df, sig_t, "SELL", scenario)
        if fill is None:
            return
        ft, px = fill
        qty = pos["qty"]
        gross = (px - pos["entry_price"]) * qty
        cost = _rt_cost(pos["entry_price"], qty)
        net = gross - cost
        cash += qty * px
        realized += net
        udir = _underlying_move_dir(hynix, datetime.fromisoformat(pos["signal_time"]), ft)
        wrong = bool(udir and udir != pos["direction"])
        rr.trades.append(
            Trade(
                strategy=rr.strategy,
                scenario=scenario,
                symbol=pos["symbol"],
                direction=pos["direction"],
                signal_time=pos["signal_time"],
                order_time=pos["order_time"],
                fill_price=pos["entry_price"],
                exit_signal_time=sig_t.isoformat(sep=" "),
                exit_time=ft.isoformat(sep=" "),
                exit_price=px,
                exit_reason=reason,
                qty=qty,
                gross_pnl=gross,
                cost=cost,
                net_pnl=net,
                held_seconds=(ft - datetime.fromisoformat(pos["order_time"])).total_seconds(),
                wrong_direction=wrong,
            )
        )
        pos = None

    for i, ts in enumerate(times):
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if ts.hour < 9:
            continue
        if ts >= FORCE_EXIT:
            if pos is not None:
                do_exit(FORCE_EXIT, "15:15_FORCE_CLOSE")
            break
        if not is_new_entry_allowed(ts) and pos is None:
            continue

        sp = px_close(h_idx, ts)
        lp = px_close(long_idx, ts)
        ip = px_close(inv_idx, ts)
        if sp is None or lp is None or ip is None:
            continue

        # Direction from completed 1m structure (fast trend on slice ending at ts)
        h_slice = hynix[hynix["datetime"] <= ts].copy()
        if len(h_slice) < 5:
            continue
        fast = compute_fast_trend_signal(h_slice, now=ts)
        live_dir = fast.get("direction")
        if live_dir not in ("UP", "DOWN"):
            continue

        desired = LONG_SYMBOL if live_dir == "UP" else INVERSE_SYMBOL
        cur_px = lp if desired == LONG_SYMBOL else ip
        etf_slice = (long_1m if desired == LONG_SYMBOL else inv_1m)
        etf_slice = etf_slice[etf_slice["datetime"] <= ts]
        confirm_dirs_raw = minute_window_dirs(long_idx if desired == LONG_SYMBOL else inv_idx, ts)
        oppose_dirs_raw = minute_window_dirs(inv_idx if desired == LONG_SYMBOL else long_idx, ts)
        signal_dirs = minute_window_dirs(h_idx, ts)
        confirm_dirs = trade_aligned_window_directions(confirm_dirs_raw, symbol=desired)
        oppose_dirs = trade_aligned_window_directions(
            oppose_dirs_raw, symbol=INVERSE_SYMBOL if desired == LONG_SYMBOL else LONG_SYMBOL
        )

        vwap = compute_etf_vwap(etf_slice) if len(etf_slice) >= 3 else None
        confirm_above_vwap = bool(vwap is not None and cur_px >= float(vwap))
        breakouts = compute_etf_breakouts(etf_slice, cur_px, live_dir) if len(etf_slice) >= 3 else {}
        swing_breakout = bool(
            breakouts.get("recent_high") and cur_px > float(breakouts["recent_high"])
            if live_dir == "UP"
            else breakouts.get("recent_low") and cur_px < float(breakouts["recent_low"])
        )

        returns = fast.get("returns") or {}
        expected_move = max(abs(float(returns.get(k) or 0.0)) for k in ("1m", "3m", "5m")) or 0.45
        cost_gate = etd.evaluate_cost_gate(desired, expected_move)
        if live_dir == "UP":
            decision = {"final_action": "HYNIX_BUY", "enhanced_score": 72.0, "inverse_pressure_score": 28.0}
        else:
            decision = {"final_action": "INVERSE_BUY", "enhanced_score": 28.0, "inverse_pressure_score": 72.0}

        macd_conf = engine._macd_williams_confirmation(etf_slice, live_dir)

        _existing = continuation.get("direction")
        _vwap_map = dict(continuation.get("prev_above_vwap_by_symbol") or {})
        _prev_above = _vwap_map.get(desired)
        vwap_reclaim = bool(
            confirm_above_vwap
            and _prev_above is False
            and confirm_dirs.get(5) == live_dir
            and confirm_dirs.get(10) == live_dir
        )
        _struct_broken = False
        if _existing and _existing != live_dir:
            esym = LONG_SYMBOL if _existing == "UP" else INVERSE_SYMBOL
            edf = long_1m if esym == LONG_SYMBOL else inv_1m
            epx = lp if esym == LONG_SYMBOL else ip
            eslice = edf[edf["datetime"] <= ts]
            if epx and len(eslice) >= 3:
                sdir = "UP" if esym == INVERSE_SYMBOL else _existing
                _struct_broken = is_swing_structure_broken_against(eslice, epx, sdir)
        _opp = engine.detect_opposite_episode_transition(
            existing_direction=_existing,
            new_direction=live_dir,
            live_direction_matches=True,
            confirm_dirs=confirm_dirs,
            existing_structure_broken=_struct_broken,
            new_etf_vwap_reclaim=vwap_reclaim,
            new_swing_breakout=swing_breakout,
        )
        direction_episode_changed = False
        if continuation.get("direction") != live_dir and (not _existing or _opp):
            direction_episode_changed = True
            engine.reset_range_episode_probe_state(
                continuation,
                now=ts,
                direction=live_dir,
                episode_id=f"{live_dir}:{ts.isoformat()}",
                reference_price=cur_px,
            )
        continuation["prev_above_vwap"] = confirm_above_vwap
        _vwap_map[desired] = confirm_above_vwap
        continuation["prev_above_vwap_by_symbol"] = _vwap_map
        engine.update_range_episode_structural_events(
            continuation, now=ts, swing_breakout=swing_breakout, vwap_reclaim=vwap_reclaim
        )
        moved_pct = None
        if continuation.get("reference_price"):
            moved_pct = abs(cur_px / float(continuation["reference_price"]) - 1.0) * 100.0

        entry_eval = engine.evaluate_range_weighted_entry(
            decision=decision,
            direction=live_dir,
            live_direction=live_dir,
            signal_window_directions=signal_dirs,
            confirm_window_directions=confirm_dirs_raw,
            oppose_window_directions=oppose_dirs_raw,
            confirm_above_vwap=confirm_above_vwap,
            data_age_seconds=60.0,  # minute cadence
            moved_pct_since_signal=moved_pct,
            expected_move_pct=expected_move,
            cost_pct=cost_gate.get("cost_pct"),
            expected_mfe_pct=expected_move,
            expected_mae_pct=abs(float(etd.FIXED_EARLY_STOP_PCT)),
            ema_slope_aligned=True,
            structure_confirmed=swing_breakout,
            structural_direction=live_dir,
            day_regime=day_regime,
            range_config=cfg,
        )

        # Exits
        if pos is not None:
            held_px = lp if pos["symbol"] == LONG_SYMBOL else ip
            held_df = long_1m if pos["symbol"] == LONG_SYMBOL else inv_1m
            held_slice = held_df[held_df["datetime"] <= ts]
            net_ret = (held_px / pos["entry_price"] - 1.0) * 100.0
            pos["peak_net"] = max(pos.get("peak_net", 0.0), net_ret)
            held_dirs = trade_aligned_window_directions(
                minute_window_dirs(long_idx if pos["symbol"] == LONG_SYMBOL else inv_idx, ts),
                symbol=pos["symbol"],
            )
            structure_broken = is_swing_structure_broken_against(held_slice, held_px, pos["direction"])
            etf_aligned = held_dirs.get(5) == pos["direction"] and held_dirs.get(10) == pos["direction"]
            regime_reversal = (
                live_dir != pos["direction"]
                and held_dirs.get(5) != pos["direction"]
                and held_dirs.get(10) != pos["direction"]
            )
            if continuation.get("entry_path") == "CONTINUATION" or continuation.get("probe_promoted_at"):
                exit_plan = engine.evaluate_weighted_continuation_exit(
                    net_return_pct=net_ret,
                    hard_stop_pct=float(etd.FIXED_EARLY_STOP_PCT),
                    structure_reversal_confirmed=structure_broken,
                    regime_reversal_confirmed=regime_reversal,
                    held_window_dirs=held_dirs,
                    position_direction=pos["direction"],
                    tp1_taken=bool(pos.get("tp1_taken")),
                    tp2_taken=bool(pos.get("tp2_taken")),
                    confirmed_regime=etd.REGIME_FAST_REVERSAL_RANGE,
                )
            else:
                exit_plan = engine.evaluate_weighted_range_probe_exit(
                    continuation=continuation,
                    probe_direction=pos["direction"],
                    structure_reversal_confirmed=structure_broken,
                    held_window_dirs=held_dirs,
                    macd_confirmed=bool(macd_conf.get("confirmed")),
                    etf_direction_aligned=etf_aligned,
                    now=ts,
                    net_return_pct=net_ret,
                    hard_stop_pct=float(etd.FIXED_EARLY_STOP_PCT),
                )
                if exit_plan.get("action") == "PROMOTE_CONTINUATION":
                    engine.promote_reversal_probe_to_continuation(continuation, now=ts)
                    exit_plan = {"action": "HOLD", "ratio": 0.0}

            if exit_plan.get("action") in ("SELL_ALL", "SELL_PARTIAL"):
                do_exit(ts, str(exit_plan.get("reason") or "WOC_EXIT"))
                if continuation.get("entry_path") == "REVERSAL":
                    engine.mark_range_probe_exit(
                        continuation, now=ts, entry_path="REVERSAL",
                        reason=str(exit_plan.get("reason") or ""),
                        probe_failed=bool(exit_plan.get("probe_failed")),
                    )

        # Entries — signal confirmed at this 1m close; fill delayed per scenario
        if pos is None and entry_eval.get("action") == "ENTER" and ts <= ENTRY_CUTOFF:
            if daily_loss_limit_reached(realized, INITIAL_CASH, cfg):
                continue
            if (
                continuation.get("direction")
                and live_dir != continuation.get("direction")
                and not _opp
            ):
                continue
            ep_id = continuation.get("direction_episode_id") or f"{live_dir}:{ts.isoformat()}"
            entry_path = entry_eval.get("entry_path")
            allows, _ = engine.range_episode_allows_entry(
                continuation,
                entry_path=entry_path,
                swing_breakout=swing_breakout,
                vwap_reclaim=vwap_reclaim,
                direction_changed=direction_episode_changed,
            )
            if not allows:
                continue
            if ep_id in episode_entries and entry_path == "REVERSAL":
                continue
            episode_entries.add(ep_id)
            target_pct = float(entry_eval.get("target_pct") or 0.25)
            fill = resolve_fill(long_1m if desired == LONG_SYMBOL else inv_1m, ts, "BUY", scenario)
            if fill is None:
                continue
            ft, px = fill
            qty = max(1, int(cash * target_pct / px))
            if qty * px > cash:
                continue
            cash -= qty * px
            pos = {
                "symbol": desired,
                "direction": live_dir,
                "qty": qty,
                "entry_price": px,
                "signal_time": ts.isoformat(sep=" "),
                "order_time": ft.isoformat(sep=" "),
                "peak_net": 0.0,
                "entry_path": entry_path,
            }
            continuation["entry_done"] = True
            continuation["entry_path"] = entry_path
            engine.mark_range_reversal_probe_entered(continuation, now=ts, entry_path=entry_path)

        # Opposite flip while holding: exit+switch if live flips and entry would fire
        elif pos is not None and live_dir != pos["direction"] and entry_eval.get("action") == "ENTER":
            do_exit(ts, "OPPOSITE_WOC")
            if ts <= ENTRY_CUTOFF:
                target_pct = float(entry_eval.get("target_pct") or 0.25)
                fill = resolve_fill(long_1m if desired == LONG_SYMBOL else inv_1m, ts, "BUY", scenario)
                if fill is not None:
                    ft, px = fill
                    qty = max(1, int(cash * target_pct / px))
                    if qty * px <= cash and qty >= 1:
                        cash -= qty * px
                        pos = {
                            "symbol": desired,
                            "direction": live_dir,
                            "qty": qty,
                            "entry_price": px,
                            "signal_time": ts.isoformat(sep=" "),
                            "order_time": ft.isoformat(sep=" "),
                            "peak_net": 0.0,
                            "entry_path": entry_eval.get("entry_path"),
                        }

    if pos is not None:
        do_exit(FORCE_EXIT, "15:15_FORCE_CLOSE")
    return rr


# ── scoring / recommendation ───────────────────────────────────────────────
def score_conservative(m: dict) -> float:
    """Higher is better. Conservative: Net PnL, PF, MDD, wrong dirs, costs, trade count."""
    net = float(m.get("net_pnl") or 0)
    pf = float(m.get("pf_raw") or 0)
    if pf > 50:
        pf = 50
    mdd = abs(float(m.get("mdd_pct") or 0))
    wrong = int(m.get("wrong_direction_count") or 0)
    cost_r = float(m.get("cost_gross_ratio_pct") or 0)
    n = int(m.get("round_trips") or 0)
    # Prefer positive net, decent PF, shallow MDD, few wrong dirs, moderate trades
    score = 0.0
    score += net / 1000.0
    score += min(pf, 5.0) * 8.0
    score -= mdd * 15.0
    score -= wrong * 12.0
    score -= min(cost_r, 100.0) * 0.15
    if n == 0:
        score -= 5.0
    elif n > 12:
        score -= (n - 12) * 1.5
    # capture bonuses
    if m.get("first_down_capture_after_1027"):
        score += 4.0
    if m.get("first_rebound_capture_after_1212"):
        score += 3.0
    return score


def print_table(rows: list[dict], title: str) -> None:
    print(f"\n{'=' * 110}")
    print(title)
    print(f"{'=' * 110}")
    hdr = (
        f"{'Strategy':<22} {'NetPnL':>10} {'Ret%':>7} {'PF':>6} {'MDD%':>7} "
        f"{'N':>3} {'Wrong':>5} {'Probe%':>7} {'EarlyCap':>8} {'↓10:27':>8} {'↑12:12':>8}"
    )
    print(hdr)
    print("-" * 110)
    for m in rows:
        pf = m.get("pf")
        pf_s = f"{pf:.2f}" if pf is not None else ("∞" if (m.get("pf_raw") or 0) > 100 else "—")
        probe = m.get("probe_success_rate_pct")
        probe_s = f"{probe:.0f}%" if probe is not None else "—"
        early = m.get("avg_early_capture_sec")
        early_s = f"{early:.0f}s" if early is not None else "—"
        print(
            f"{m['strategy']:<22} {m['net_pnl']:>+10,.0f} {m['return_pct']:>+6.3f}% {pf_s:>6} "
            f"{m['mdd_pct']:>6.2f}% {m['round_trips']:>3} {m['wrong_direction_count']:>5} "
            f"{probe_s:>7} {early_s:>8} "
            f"{str(m.get('first_down_capture_after_1027') or '—')[-8:]:>8} "
            f"{str(m.get('first_rebound_capture_after_1212') or '—')[-8:]:>8}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    print("=" * 72)
    print(f"Jul22 A–E + F + Weighted — realistic fills — {SESSION_DATE}")
    print("Focus compare: A(3m MACD) | C(WR→MACD) | F(1m lead→3m) | W(weighted)")
    print("=" * 72)

    data = fetch_and_cache(refetch=args.refetch)
    hynix, long_1m, inv_1m = data[SIGNAL_SYMBOL], data[LONG_SYMBOL], data[INVERSE_SYMBOL]
    print(f"Bars: hynix={len(hynix)} long={len(long_1m)} inv={len(inv_1m)}")
    print(f"Range: {hynix['datetime'].iloc[0]} → {hynix['datetime'].iloc[-1]}")
    print(f"000660 open→close: {hynix['close'].iloc[0]:,.0f} → {hynix['close'].iloc[-1]:,.0f}")

    bars = _build_indicator_frame(hynix)
    print(f"Completed 3m bars: {len(bars)}")

    signal_map = {
        "A: MACD cross": signals_A(bars),
        "B: Hist 2-turn": signals_B(bars),
        "C: WR→MACD": signals_C(bars),
        "D: MACD∩WR full": signals_D(bars),
        "E: WR probe→MACD": signals_E(bars),
    }
    for k, v in signal_map.items():
        print(f"  signals {k}: {len(v)}")

    scenarios = ["immediate", "delay_1m", "delay_1m_cons", "delay_2m"]
    all_metrics: dict[str, dict[str, dict]] = {}
    FOCUS = ["A: MACD cross", "C: WR→MACD", "F: 1m lead→3m MACD", "W: weighted RANGE"]

    for scen in scenarios:
        print(f"\n--- scenario: {FILL_SCENARIOS[scen]['label']} ---")
        all_metrics[scen] = {}
        for name, evs in signal_map.items():
            rr = execute_signal_strategy(name, evs, hynix, long_1m, inv_1m, scen)
            m = compute_metrics(rr, hynix)
            all_metrics[scen][name] = m
            print(f"  {name}: N={m['round_trips']} net={m['net_pnl']:+,.0f}")
        frr = run_strategy_F(hynix, long_1m, inv_1m, scen)
        fm = compute_metrics(frr, hynix)
        all_metrics[scen][frr.strategy] = fm
        print(
            f"  {frr.strategy}: N={fm['round_trips']} net={fm['net_pnl']:+,.0f} "
            f"probe={fm.get('probe_success_rate_pct')}%"
        )
        wrr = run_weighted(hynix, long_1m, inv_1m, scen)
        wm = compute_metrics(wrr, hynix)
        all_metrics[scen][wrr.strategy] = wm
        print(f"  {wrr.strategy}: N={wm['round_trips']} net={wm['net_pnl']:+,.0f}")

    # Focused comparison tables (A / C / F / W)
    for scen, title in (
        ("immediate", "FOCUS 즉시체결 (UNREALISTIC 대조) — A / C / F / W"),
        ("delay_1m", "FOCUS 1분지연 next-open — A / C / F / W"),
        ("delay_1m_cons", "FOCUS 1분지연+0.05%adverse ★추천기준 — A / C / F / W"),
        ("delay_2m", "FOCUS 2분지연+0.10%adverse 스트레스 — A / C / F / W"),
    ):
        rows = [all_metrics[scen][n] for n in FOCUS if n in all_metrics[scen]]
        print_table(rows, title)

    # Full A–E+F+W on conservative
    print_table(
        list(all_metrics["delay_1m_cons"].values()),
        "FULL A–E+F+W — 1분지연+0.05%adverse (보수 전체)",
    )

    # Recommendation: prefer focus set on delay_1m_cons; also show full ranking
    cons = all_metrics["delay_1m_cons"]
    focus_ranked = sorted(
        [cons[n] for n in FOCUS if n in cons], key=score_conservative, reverse=True
    )
    best = focus_ranked[0]
    full_ranked = sorted(cons.values(), key=score_conservative, reverse=True)

    print("\n" + "=" * 110)
    print("FINAL RECOMMENDATION (1분지연+0.05%adverse, focus A/C/F/W — NOT max 1-day profit)")
    print("=" * 110)
    print(f"  Winner: {best['strategy']}")
    print(
        f"  Net {best['net_pnl']:+,.0f} KRW | Ret {best['return_pct']:+.3f}% | "
        f"PF {best.get('pf') or best.get('pf_raw')} | MDD {best['mdd_pct']:.2f}% | "
        f"Wrong {best['wrong_direction_count']} | Trades {best['round_trips']}"
    )
    if best.get("probe_success_rate_pct") is not None:
        print(f"  Probe success: {best['probe_success_rate_pct']}% ({best['probe_successes']}/{best['probe_starts']})")
    print(f"  ↓after 10:27: {best.get('first_down_capture_after_1027')}")
    print(f"  ↑after 12:12: {best.get('first_rebound_capture_after_1212')}")
    print("  Focus rank:")
    for i, m in enumerate(focus_ranked, 1):
        print(
            f"    {i}. {m['strategy']:<22} score={score_conservative(m):+.1f}  "
            f"net={m['net_pnl']:+,.0f} pf={m.get('pf')} mdd={m['mdd_pct']:.2f} wrong={m['wrong_direction_count']}"
        )

    print("\nFill-sensitivity Net PnL (focus):")
    print(f"{'Strategy':<22} {'즉시':>12} {'1m':>12} {'1m+0.05%':>12} {'2m+0.10%':>12}")
    for name in FOCUS:
        vals = [all_metrics[s][name]["net_pnl"] for s in ("immediate", "delay_1m", "delay_1m_cons", "delay_2m")]
        print(f"{name:<22} {vals[0]:>+12,.0f} {vals[1]:>+12,.0f} {vals[2]:>+12,.0f} {vals[3]:>+12,.0f}")

    print("\nFill-sensitivity Net PnL (all A–E+F+W):")
    print(f"{'Strategy':<22} {'즉시':>12} {'1m':>12} {'1m+0.05%':>12} {'2m+0.10%':>12}")
    for name in list(signal_map.keys()) + ["F: 1m lead→3m MACD", "W: weighted RANGE"]:
        vals = [all_metrics[s][name]["net_pnl"] for s in ("immediate", "delay_1m", "delay_1m_cons", "delay_2m")]
        print(f"{name:<22} {vals[0]:>+12,.0f} {vals[1]:>+12,.0f} {vals[2]:>+12,.0f} {vals[3]:>+12,.0f}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_date": SESSION_DATE,
        "initial_cash": INITIAL_CASH,
        "rt_cost_pct": RT_COST_PCT,
        "data_source": "KIS mock inquire-time-itemchartprice; cached data/cache/replay_20260722_*.csv",
        "fill_scenarios": FILL_SCENARIOS,
        "recommendation_basis": "delay_1m_cons",
        "focus_strategies": FOCUS,
        "recommendation": {
            "strategy": best["strategy"],
            "score": score_conservative(best),
            "metrics": {k: v for k, v in best.items() if k != "trades"},
            "focus_ranking": [
                {"strategy": m["strategy"], "score": score_conservative(m), "net_pnl": m["net_pnl"]}
                for m in focus_ranked
            ],
            "full_ranking": [
                {"strategy": m["strategy"], "score": score_conservative(m), "net_pnl": m["net_pnl"]}
                for m in full_ranked
            ],
        },
        "woc_limitations": cons.get("W: weighted RANGE", {}).get("notes"),
        "bar_counts": {
            "hynix_1m": len(hynix),
            "long_1m": len(long_1m),
            "inverse_1m": len(inv_1m),
            "hynix_3m_completed": len(bars),
            "range": [str(hynix["datetime"].iloc[0]), str(hynix["datetime"].iloc[-1])],
        },
        "results": {
            scen: {k: {kk: vv for kk, vv in m.items()} for k, m in strats.items()}
            for scen, strats in all_metrics.items()
        },
    }
    json_path = STATE_DIR / "jul22_macd_williams_strategies_replay.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    rows = []
    for name, m in cons.items():
        for t in m.get("trades") or []:
            rows.append(t)
    csv_path = STATE_DIR / "jul22_macd_williams_strategies_trades_cons.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)

    summary_path = STATE_DIR / "jul22_macd_williams_strategies_summary.md"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"# Jul22 Strategy Replay ({SESSION_DATE})\n\n")
        f.write("## Focus comparison\nA (3m MACD) | C (WR→MACD) | F (1m lead→3m) | W (weighted)\n\n")
        f.write("## Recommendation basis\n")
        f.write("**1분 지연 + 0.05% adverse** (not max 1-day profit / not immediate fill).\n\n")
        f.write(f"**Winner: {best['strategy']}**\n\n")
        f.write("| Strategy | Net | Ret% | PF | MDD% | Wrong | N | Probe% | EarlyCap | ↓10:27 | ↑12:12 |\n")
        f.write("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|\n")
        for m in focus_ranked:
            f.write(
                f"| {m['strategy']} | {m['net_pnl']:+,.0f} | {m['return_pct']:+.3f} | {m.get('pf')} | "
                f"{m['mdd_pct']:.2f} | {m['wrong_direction_count']} | {m['round_trips']} | "
                f"{m.get('probe_success_rate_pct')} | {m.get('avg_early_capture_sec')} | "
                f"{m.get('first_down_capture_after_1027')} | {m.get('first_rebound_capture_after_1212')} |\n"
            )
        f.write("\n## Re-run\n```\npython scripts/replay_jul22_macd_williams_strategies.py\n")
        f.write("python scripts/replay_jul22_macd_williams_strategies.py --refetch\n```\n")

    print(f"\nWrote {json_path}")
    print(f"Wrote {csv_path}")
    print(f"Wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
