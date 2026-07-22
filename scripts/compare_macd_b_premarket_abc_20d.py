"""Compare three premarket-handling modes for MACD Strategy B (signed hist 2-turn).

Read-only. Does NOT modify production auto-trade modules. Does NOT place orders.

Modes
  A IGNORE_PREMARKET     — trade only new B flips after 09:00
  B FULL_ENTRY_AT_OPEN   — 08:50 premaket B direction → full ETF at open
  C HALF_ENTRY_THEN_CONFIRM — 50% after open confirm; scale on first same-dir B

Shared B signal helpers imported from app.trading.macd_hynix_strategy
(no forked B definition). CONTINUATION_REENTRY remains off.

Premarket data (000660, 08:00–08:50 NXT):
  1) Local cache data/cache/nxt_premarket/000660_YYYYMMDD_1m.csv
  2) Live KIS inquire-time-itemchartprice with FID_COND_MRKT_DIV_CODE=NX
     (intraday only — historical dates are not returned by this API)
  3) If missing: overnight-gap synthetic 08:00–08:50 path
     (prev close → today open), labeled synthetic_overnight_gap_proxy
     so B/C mechanics can still be measured; coverage is documented.

Regular session 1m: same universe as compare_macd_vs_williams_early_20d
(KIS replay cache / Naver fchart / synthetic_daily_anchor).

Usage:
    python scripts/compare_macd_b_premarket_abc_20d.py
    python scripts/compare_macd_b_premarket_abc_20d.py --days 20 --skip-kis-nxt
"""
from __future__ import annotations

import argparse
import json
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

from app.trading.macd_hynix_strategy import (  # noqa: E402
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    EXIT_OPPOSITE,
    EXIT_SL,
    EXIT_TP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    MACD_SIGNAL_MIN_INDEX,
    SIGNAL_SYMBOL,
    SL_NET_PCT,
    TP_NET_PCT,
    check_tp_sl,
    evaluate_macd_direction,
    macd_components,
    normalize_direction_state,
    resample_completed_3m,
    signed_hist_two_turn_new_signal,
    signed_hist_two_turn_onset,
    signed_hist_two_turn_pattern,
)
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402
from scripts.compare_macd_vs_williams_early_20d import (  # noqa: E402
    SYM_TAG,
    build_day_universe,
)

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"
NXT_CACHE = CACHE / "nxt_premarket"
OUT_JSON = STATE / "macd_b_premarket_abc_20d_compare.json"
OUT_MD = STATE / "macd_b_premarket_abc_20d_compare.md"

INITIAL_CASH = 10_000_000.0
ENTRY_CUTOFF = (14, 55)
FORCE_HM = (15, 0)
WARMUP_3M_BARS = 100
WARMUP_1M_BARS = WARMUP_3M_BARS * 3  # ≈ last 300 completed 1m of prior day
CONFIRM_SEC = 45  # mid of 30–60s open confirmation window
HALF_PCT = 0.50
MIN_PREMARKET_BARS = 20  # sparse day threshold inside 08:00–08:50

STRATEGIES = (
    "IGNORE_PREMARKET",
    "FULL_ENTRY_AT_OPEN",
    "HALF_ENTRY_THEN_CONFIRM",
)
SCENARIOS = (
    ("baseline", 1, 0.05),
    ("plus_1m_delay", 2, 0.05),
    ("plus_2m_slip10", 3, 0.10),
)


@dataclass
class Trade:
    strategy: str
    day: str
    direction: str
    symbol: str
    signal_time: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    qty: int
    gross_pnl: float
    cost: float
    net_pnl: float
    exit_reason: str
    size_pct: float
    entry_kind: str
    is_premarket_entry: bool = False
    false_premarket: bool = False


@dataclass
class DayReplay:
    strategy: str
    day: str
    premarket_source: str
    premarket_bars: int
    premarket_available: bool
    premarket_direction: Optional[str]
    first_regular_b: Optional[str]
    first_regular_b_time: Optional[str]
    premaket_held_into_regular: Optional[bool]
    first_entry_time: Optional[str]
    first_entry_direction: Optional[str]
    first_entry_kind: Optional[str]
    net_pnl: float = 0.0
    ret_pct: float = 0.0
    first_30m_pnl: float = 0.0
    false_premarket_loss: float = 0.0
    false_premarket: bool = False
    early_vs_a_sec: Optional[float] = None
    status: str = "OK"  # OK | N/A_NO_PREMARKET | SKIP
    trades: list[Trade] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ── fills / metrics ─────────────────────────────────────────────────────────
def _fill(
    df: pd.DataFrame,
    signal_ts: datetime,
    side: str,
    delay_min: int,
    adverse_pct: float,
) -> tuple[Optional[datetime], Optional[float]]:
    sig = signal_ts.replace(second=0, microsecond=0)
    target = sig + timedelta(minutes=max(1, delay_min))
    sub = df[df["datetime"] >= target]
    if sub.empty:
        return None, None
    row = sub.iloc[0]
    ts = pd.Timestamp(row["datetime"]).to_pydatetime()
    px = float(row["open"])
    if side == "BUY":
        px *= 1.0 + adverse_pct / 100.0
    else:
        px *= 1.0 - adverse_pct / 100.0
    return ts, float(px)


def _px_at(df: pd.DataFrame, ts: datetime) -> Optional[float]:
    sub = df[df["datetime"] <= ts]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


def _metrics(trades: list[Trade], cash0: float = INITIAL_CASH) -> dict[str, float]:
    if not trades:
        return {"net": 0.0, "ret": 0.0, "wr": 0.0, "pf": 0.0, "mdd": 0.0}
    nets = [t.net_pnl for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    equity = cash0
    peak = cash0
    mdd = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0 if peak else 0.0
        mdd = max(mdd, dd)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    return {
        "net": round(sum(nets), 2),
        "ret": round(sum(nets) / cash0 * 100.0, 3),
        "wr": round(len(wins) / len(nets) * 100.0, 2),
        "pf": round(pf, 3),
        "mdd": round(mdd, 3),
    }


def _session_slice(df: pd.DataFrame, day: str, start_hm: tuple[int, int], end_hm: tuple[int, int]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    work = df.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    work = work.dropna(subset=["datetime"]).sort_values("datetime")
    start = datetime.strptime(f"{day} {start_hm[0]:02d}:{start_hm[1]:02d}:00", "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(f"{day} {end_hm[0]:02d}:{end_hm[1]:02d}:00", "%Y-%m-%d %H:%M:%S")
    out = work[(work["datetime"] >= start) & (work["datetime"] <= end)].copy()
    return out.reset_index(drop=True)


def _regular_session(df: pd.DataFrame, day: str) -> pd.DataFrame:
    return _session_slice(df, day, (9, 0), (15, 30))


# ── NXT premarket load / fetch / proxy ──────────────────────────────────────
def _nxt_cache_path(day: str) -> Path:
    return NXT_CACHE / f"000660_{day.replace('-', '')}_1m.csv"


def _fetch_kis_nxt_premarket_today() -> Optional[pd.DataFrame]:
    """KIS NX market: today's 08:00–08:50 1m bars for 000660 (intraday API)."""
    try:
        from app.trading.kis_client import create_kis_client
    except Exception as exc:
        print(f"  KIS import failed: {exc}")
        return None
    client = create_kis_client("real")
    if client is None:
        print("  KIS real client unavailable")
        return None
    tr_id = "FHKST03010200"
    url = f"{client.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    hours = ["080000", "083000", "085000", "090000"]
    rows: dict[str, dict] = {}
    today = datetime.now().strftime("%Y%m%d")
    for hour in hours:
        headers = client._auth_headers(tr_id)
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "NX",
            "FID_INPUT_ISCD": SIGNAL_SYMBOL,
            "FID_INPUT_HOUR_1": hour,
            "FID_PW_DATA_INCU_YN": "N",
        }
        try:
            resp = client._get(url, headers=headers, params=params, timeout=(3, 12))
            resp.raise_for_status()
            payload = resp.json()
            for row in payload.get("output2") or []:
                bsop = str(row.get("stck_bsop_date") or today)
                t = str(row.get("stck_cntg_hour") or "").zfill(6)
                close = float(row.get("stck_prpr") or 0)
                if close <= 0 or not t:
                    continue
                if t < "080000" or t > "085000":
                    continue
                key = f"{bsop}{t}"
                rows[key] = {
                    "datetime": datetime.strptime(f"{bsop}{t}", "%Y%m%d%H%M%S"),
                    "open": float(row.get("stck_oprc") or close),
                    "high": float(row.get("stck_hgpr") or close),
                    "low": float(row.get("stck_lwpr") or close),
                    "close": close,
                    "volume": int(row.get("cntg_vol") or 0),
                }
        except Exception as exc:
            print(f"  KIS NX hour={hour} failed: {exc}")
            continue
    if not rows:
        return None
    df = pd.DataFrame(list(rows.values())).sort_values("datetime").reset_index(drop=True)
    return df


def _synthetic_gap_premarket(day: str, prev_close: float, day_open: float) -> pd.DataFrame:
    """Linear 08:00–08:50 path from prior close → today open (proxy, not NXT)."""
    start = datetime.strptime(f"{day} 08:00:00", "%Y-%m-%d %H:%M:%S")
    times = [start + timedelta(minutes=i) for i in range(0, 51)]  # inclusive 08:50
    n = len(times)
    closes = np.linspace(float(prev_close), float(day_open), n)
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes)
    lows = np.minimum(opens, closes)
    return pd.DataFrame(
        {
            "datetime": times,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.zeros(n, dtype=int),
        }
    )


def load_premarket_for_day(
    day: str,
    regular_hynix: pd.DataFrame,
    prev_regular: Optional[pd.DataFrame],
    *,
    skip_kis: bool = False,
    today_iso: Optional[str] = None,
) -> tuple[pd.DataFrame, str, dict[str, Any]]:
    """Return (premarket_df, source, meta)."""
    meta: dict[str, Any] = {"day": day}
    path = _nxt_cache_path(day)
    if path.exists():
        df = pd.read_csv(path)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        df = _session_slice(df, day, (8, 0), (8, 50))
        if len(df) >= MIN_PREMARKET_BARS:
            meta.update({"bars": len(df), "cached": True})
            return df, "kis_nx_cached", meta

    today_iso = today_iso or datetime.now().strftime("%Y-%m-%d")
    if not skip_kis and day == today_iso:
        fetched = _fetch_kis_nxt_premarket_today()
        if fetched is not None and not fetched.empty:
            fetched = _session_slice(fetched, day, (8, 0), (8, 50))
            if len(fetched) >= MIN_PREMARKET_BARS:
                NXT_CACHE.mkdir(parents=True, exist_ok=True)
                fetched.to_csv(path, index=False)
                meta.update({"bars": len(fetched), "fetched": True})
                return fetched, "kis_nx_live", meta
            meta["kis_sparse_bars"] = int(len(fetched))

    # Proxy from overnight gap
    prev_close = None
    if prev_regular is not None and not prev_regular.empty:
        prev_close = float(prev_regular.iloc[-1]["close"])
    day_open = None
    if regular_hynix is not None and not regular_hynix.empty:
        day_open = float(regular_hynix.iloc[0]["open"])
    if prev_close and day_open and prev_close > 0 and day_open > 0:
        proxy = _synthetic_gap_premarket(day, prev_close, day_open)
        meta.update(
            {
                "bars": len(proxy),
                "prev_close": prev_close,
                "day_open": day_open,
                "gap_pct": round((day_open / prev_close - 1.0) * 100.0, 4),
            }
        )
        return proxy, "synthetic_overnight_gap_proxy", meta

    meta["bars"] = 0
    return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"]), "missing", meta


def _warmup_1m(prev_regular: Optional[pd.DataFrame]) -> pd.DataFrame:
    if prev_regular is None or prev_regular.empty:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    work = prev_regular.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    work = work.dropna(subset=["datetime"]).sort_values("datetime")
    return work.tail(WARMUP_1M_BARS).reset_index(drop=True)


def _precompute_b_timeline(
    signal_1m: pd.DataFrame,
    *,
    session_open: Optional[datetime] = None,
) -> dict[str, Any]:
    """One-pass 3m hist + signed-B onsets + display-by-close (no lookahead).

    Trading events only arm after ``session_open`` with a fresh direction_state,
    while hist/onset still see warmup (+ optional premaket) bars.
    """
    if signal_1m is None or signal_1m.empty:
        return {"events": [], "display_at": {}, "bars3": pd.DataFrame()}
    end = pd.Timestamp(signal_1m["datetime"].iloc[-1]).to_pydatetime() + timedelta(minutes=3)
    bars3 = resample_completed_3m(signal_1m, now=end)
    display_at: dict[datetime, str] = {}
    events: list[tuple[datetime, str, str]] = []
    if len(bars3) <= MACD_SIGNAL_MIN_INDEX:
        return {"events": events, "display_at": display_at, "bars3": bars3}
    closes = pd.to_numeric(bars3["close"], errors="coerce")
    comps = macd_components(closes)
    hist = comps.get("hist")
    if hist is None or len(hist) < 3:
        return {"events": events, "display_at": display_at, "bars3": bars3}
    close_times: list[datetime] = []
    hist_vals: list[float] = []
    for i in range(len(bars3)):
        ct = pd.Timestamp(bars3.iloc[i]["datetime"]).to_pydatetime() + timedelta(minutes=3)
        hv = float(hist.iloc[i])
        close_times.append(ct)
        hist_vals.append(hv)
        if i >= 2:
            display_at[ct] = signed_hist_two_turn_pattern(
                hist_vals[i], hist_vals[i - 1], hist_vals[i - 2]
            )
    # Fresh direction_state at regular open — do not let prior-day warm-up
    # flips suppress the first same-dir regular onset via state alone.
    # Onset (prev_ok) still uses full hist, so already-true patterns stay quiet.
    state: Optional[str] = None
    for i in range(2, len(hist_vals)):
        if i < MACD_SIGNAL_MIN_INDEX:
            continue
        ct = close_times[i]
        if session_open is not None and ct < session_open:
            continue
        prev3 = float(hist_vals[i - 3]) if i >= 3 else None
        onset = signed_hist_two_turn_onset(
            float(hist_vals[i]), float(hist_vals[i - 1]), float(hist_vals[i - 2]), prev3
        )
        if onset is None:
            continue
        if not signed_hist_two_turn_new_signal(onset, state):
            continue
        bar_ts = (ct - timedelta(minutes=3)).isoformat()
        events.append((ct, onset, bar_ts))
        state = onset
    return {"events": events, "display_at": display_at, "bars3": bars3}


def _display_asof(display_at: dict[datetime, str], ts: datetime) -> Optional[str]:
    times = [t for t in display_at if t <= ts]
    if not times:
        return None
    return normalize_direction_state(display_at[max(times)])


def _premarket_direction(warmup: pd.DataFrame, premaket: pd.DataFrame, day: str) -> tuple[Optional[str], dict]:
    """Signed B display direction at 08:50 using warmup + premaket only."""
    if premaket is None or premaket.empty:
        return None, {"ok": False, "reason": "NO_PREMARKET"}
    cut = datetime.strptime(f"{day} 08:50:00", "%Y-%m-%d %H:%M:%S")
    hist = pd.concat([warmup, premaket], ignore_index=True)
    hist = hist[hist["datetime"] <= cut].reset_index(drop=True)
    now = cut + timedelta(minutes=1)
    ev = evaluate_macd_direction(hist, now=now)
    display = normalize_direction_state(ev.get("display_direction"))
    if display in (DIR_UP, DIR_DOWN):
        return display, ev
    return None, ev


# ── day replay ──────────────────────────────────────────────────────────────
def replay_day(
    day: str,
    strategy: str,
    data: dict[str, pd.DataFrame],
    prev_data: Optional[dict[str, pd.DataFrame]],
    *,
    delay_min: int = 1,
    adverse_pct: float = 0.05,
    skip_kis: bool = False,
    allow_proxy: bool = True,
    today_iso: Optional[str] = None,
) -> DayReplay:
    cost_engine = TradeCostEngine()
    hynix_raw = data[SIGNAL_SYMBOL]
    long_df = _regular_session(data[LONG_SYMBOL], day)
    inv_df = _regular_session(data[INVERSE_SYMBOL], day)
    regular = _regular_session(hynix_raw, day)
    prev_reg = _regular_session(prev_data[SIGNAL_SYMBOL], prev_data["__day"]) if prev_data else None
    warmup = _warmup_1m(prev_reg)

    premaket, pm_source, pm_meta = load_premarket_for_day(
        day, regular, prev_reg, skip_kis=skip_kis, today_iso=today_iso
    )
    if pm_source == "synthetic_overnight_gap_proxy" and not allow_proxy:
        premaket = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        pm_source = "missing_proxy_disabled"
        pm_meta = {"bars": 0}

    pm_avail = len(premaket) >= MIN_PREMARKET_BARS
    result = DayReplay(
        strategy=strategy,
        day=day,
        premarket_source=pm_source,
        premarket_bars=int(len(premaket)),
        premarket_available=pm_avail,
        premarket_direction=None,
        first_regular_b=None,
        first_regular_b_time=None,
        premaket_held_into_regular=None,
        first_entry_time=None,
        first_entry_direction=None,
        first_entry_kind=None,
    )

    # For A, or B/C without usable premaket when proxy disabled → A-like
    use_premarket = strategy != "IGNORE_PREMARKET" and pm_avail
    if strategy != "IGNORE_PREMARKET" and not pm_avail:
        result.status = "N/A_NO_PREMARKET"
        result.notes.append("No usable NXT/proxy premaket; falling back to IGNORE behavior for this day.")
        use_premarket = False

    pm_dir: Optional[str] = None
    pm_ev: dict = {}
    if use_premarket:
        pm_dir, pm_ev = _premarket_direction(warmup, premaket, day)
        result.premarket_direction = pm_dir
        if pm_dir is None:
            result.notes.append(f"08:50 B display HOLD/insufficient ({pm_ev.get('reason')})")

    session_open = datetime.strptime(f"{day} 09:00:00", "%Y-%m-%d %H:%M:%S")
    force_ts = datetime.strptime(f"{day} 15:00:00", "%Y-%m-%d %H:%M:%S")
    first_30_mark = datetime.strptime(f"{day} 09:30:00", "%Y-%m-%d %H:%M:%S")

    # Regular-session B timeline: warmup + regular ONLY (premaket must not
    # contaminate hist when 08:50 is HOLD / unused). Premaket direction is
    # evaluated separately on warmup+premaket.
    pieces = [p for p in (warmup, regular) if p is not None and not p.empty]
    signal_1m = pd.concat(pieces, ignore_index=True) if pieces else regular.copy()
    signal_1m["datetime"] = pd.to_datetime(signal_1m["datetime"], errors="coerce")
    signal_1m = (
        signal_1m.dropna(subset=["datetime"])
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    timeline = _precompute_b_timeline(signal_1m, session_open=session_open)
    display_at: dict[datetime, str] = timeline["display_at"]
    regular_events = list(timeline["events"])

    trades: list[Trade] = []
    realized = 0.0
    position: Optional[dict] = None
    pending_open: Optional[dict] = None
    open_confirm_deadline: Optional[datetime] = None
    scaled = False
    first_30_pnl: Optional[float] = None
    used_signal_ids: set[str] = set()
    false_pm_loss = 0.0
    false_pm = False
    event_i = 0

    if use_premarket and pm_dir in (DIR_UP, DIR_DOWN):
        if strategy == "FULL_ENTRY_AT_OPEN":
            pending_open = {
                "direction": pm_dir,
                "kind": "PREMARKET_FULL",
                "confirm": False,
            }
        elif strategy == "HALF_ENTRY_THEN_CONFIRM":
            pending_open = {
                "direction": pm_dir,
                "kind": "PREMARKET_HALF",
                "confirm": True,
            }
            open_confirm_deadline = session_open + timedelta(seconds=CONFIRM_SEC)

    minutes = [pd.Timestamp(t).to_pydatetime() for t in regular["datetime"].tolist()]
    if not minutes or minutes[0] > session_open:
        minutes = [session_open] + minutes

    def equity() -> float:
        return INITIAL_CASH + realized

    def close_pos(reason: str, signal_ts: datetime, *, mark_false: bool = False) -> None:
        nonlocal position, realized, false_pm_loss, false_pm
        if position is None:
            return
        etf = long_df if position["symbol"] == LONG_SYMBOL else inv_df
        xts, xpx = _fill(etf, signal_ts, "SELL", delay_min, adverse_pct)
        if xpx is None:
            xpx = float(position["entry_price"])
            xts = signal_ts
        bd = cost_engine.compute_net_pnl(
            position["symbol"],
            position["entry_price"],
            xpx,
            position["qty"],
            buy_order_type="market",
            sell_order_type="market",
        )
        is_pm = bool(position.get("is_premarket_entry"))
        t = Trade(
            strategy=strategy,
            day=day,
            direction=position["direction"],
            symbol=position["symbol"],
            signal_time=str(position["signal_time"]),
            entry_time=str(position["entry_time"]),
            entry_price=float(position["entry_price"]),
            exit_time=str(xts),
            exit_price=float(xpx),
            qty=int(position["qty"]),
            gross_pnl=float(bd["gross_pnl"]),
            cost=float(bd["total_cost"]),
            net_pnl=float(bd["net_pnl"]),
            exit_reason=reason,
            size_pct=float(position.get("size_pct", 1.0)),
            entry_kind=str(position.get("entry_kind", "FULL")),
            is_premarket_entry=is_pm,
            false_premarket=bool(mark_false and is_pm),
        )
        trades.append(t)
        realized += t.net_pnl
        if mark_false and is_pm and t.net_pnl < 0:
            false_pm = True
            false_pm_loss += t.net_pnl
        position = None

    def open_pos(
        direction: str,
        signal_ts: datetime,
        size_pct: float,
        kind: str,
        *,
        is_pm: bool = False,
        add_only: bool = False,
    ) -> bool:
        nonlocal position
        target = LONG_SYMBOL if direction == DIR_UP else INVERSE_SYMBOL
        etf = long_df if target == LONG_SYMBOL else inv_df
        fill_sig = max(signal_ts, session_open)
        ets, epx = _fill(etf, fill_sig, "BUY", delay_min, adverse_pct)
        if epx is None or epx <= 0:
            return False
        if add_only and position is not None and position["symbol"] == target:
            target_qty = int(equity() // epx)
            add_qty = max(0, target_qty - int(position["qty"]))
            if add_qty < 1:
                return False
            old_q = int(position["qty"])
            old_px = float(position["entry_price"])
            new_q = old_q + add_qty
            blend = (old_px * old_q + epx * add_qty) / new_q
            position["qty"] = new_q
            position["entry_price"] = blend
            position["size_pct"] = 1.0
            position["entry_kind"] = "PREMARKET_SCALED"
            return True
        qty = int((equity() * size_pct) // epx)
        if qty < 1:
            return False
        position = {
            "symbol": target,
            "direction": direction,
            "qty": qty,
            "entry_price": epx,
            "entry_time": ets,
            "signal_time": signal_ts.isoformat() if hasattr(signal_ts, "isoformat") else str(signal_ts),
            "size_pct": size_pct,
            "entry_kind": kind,
            "is_premarket_entry": is_pm,
        }
        return True

    def mark_first_entry() -> None:
        if result.first_entry_time is None and position is not None:
            result.first_entry_time = str(position["entry_time"])
            result.first_entry_direction = position["direction"]
            result.first_entry_kind = position["entry_kind"]

    for ts in minutes:
        hm = (ts.hour, ts.minute)

        if first_30_pnl is None and ts >= first_30_mark:
            mark = realized
            if position is not None:
                cur = _px_at(
                    long_df if position["symbol"] == LONG_SYMBOL else inv_df,
                    first_30_mark,
                )
                if cur is not None:
                    bd = cost_engine.compute_net_pnl(
                        position["symbol"],
                        position["entry_price"],
                        cur,
                        position["qty"],
                        buy_order_type="market",
                        sell_order_type="market",
                    )
                    mark += float(bd["net_pnl"])
            first_30_pnl = mark

        if hm >= FORCE_HM:
            if position is not None:
                close_pos("15:00_FORCE", force_ts)
            break

        if (
            pending_open
            and pending_open.get("confirm")
            and open_confirm_deadline is not None
            and ts >= open_confirm_deadline
            and position is None
        ):
            want = pending_open["direction"]
            # 1m-resolution proxy for 30–60s confirm: oppose if open→now
            # underlying move already fights the premaket flag.
            invalidated = False
            day_bars = regular[regular["datetime"] <= ts]
            if not day_bars.empty:
                o0 = float(day_bars.iloc[0]["open"])
                c1 = float(day_bars.iloc[-1]["close"])
                if o0 > 0:
                    ret = c1 / o0 - 1.0
                    if want == DIR_UP and ret < -0.0015:
                        invalidated = True
                    elif want == DIR_DOWN and ret > 0.0015:
                        invalidated = True
            display = _display_asof(display_at, ts)
            if display in (DIR_UP, DIR_DOWN) and display != want:
                # only if a completed regular 3m display exists after open
                if any(t >= session_open for t in display_at if t <= ts):
                    invalidated = True
            if invalidated:
                result.notes.append(
                    f"C invalidate at open+{CONFIRM_SEC}s display={display}"
                )
                pending_open = None
            else:
                if open_pos(want, session_open, HALF_PCT, "PREMARKET_HALF", is_pm=True):
                    mark_first_entry()
                pending_open = None

        if (
            pending_open
            and not pending_open.get("confirm")
            and position is None
            and hm >= (9, 0)
        ):
            want = pending_open["direction"]
            if open_pos(want, session_open, 1.0, "PREMARKET_FULL", is_pm=True):
                mark_first_entry()
            pending_open = None

        if position is not None:
            cur = _px_at(long_df if position["symbol"] == LONG_SYMBOL else inv_df, ts)
            if cur is not None:
                hit = check_tp_sl(
                    position["symbol"], position["entry_price"], cur, position["qty"]
                )
                if hit:
                    close_pos(hit, ts)

        while event_i < len(regular_events) and regular_events[event_i][0] <= ts:
            ct, direction, bar_ts = regular_events[event_i]
            event_i += 1
            if result.first_regular_b is None:
                result.first_regular_b = direction
                result.first_regular_b_time = ct.isoformat()
                if result.premarket_direction in (DIR_UP, DIR_DOWN):
                    result.premaket_held_into_regular = (
                        result.premarket_direction == result.first_regular_b
                    )

            sig_id = f"MACD3M:{direction}:{bar_ts}"
            if sig_id in used_signal_ids:
                continue
            used_signal_ids.add(sig_id)

            hm_sig = (ct.hour, ct.minute)
            if hm_sig >= ENTRY_CUTOFF:
                continue

            if (
                strategy == "HALF_ENTRY_THEN_CONFIRM"
                and position is not None
                and position.get("is_premarket_entry")
                and position["direction"] != direction
            ):
                close_pos(EXIT_OPPOSITE, ct, mark_false=True)
                pending_open = None

            if position is not None and position["direction"] != direction:
                close_pos(
                    EXIT_OPPOSITE,
                    ct,
                    mark_false=bool(position.get("is_premarket_entry")),
                )

            if (
                strategy == "HALF_ENTRY_THEN_CONFIRM"
                and position is not None
                and position.get("entry_kind") == "PREMARKET_HALF"
                and position["direction"] == direction
                and not scaled
            ):
                if open_pos(direction, ct, 1.0, "PREMARKET_SCALED", is_pm=True, add_only=True):
                    scaled = True
                    mark_first_entry()
                continue

            if position is not None and position["direction"] == direction:
                continue

            if open_pos(direction, ct, 1.0, "REGULAR_B", is_pm=False):
                mark_first_entry()

    if position is not None:
        close_pos("EOD_FLAT", force_ts)

    if first_30_pnl is None:
        first_30_pnl = realized

    result.trades = trades
    result.net_pnl = round(sum(t.net_pnl for t in trades), 2)
    result.ret_pct = round(result.net_pnl / INITIAL_CASH * 100.0, 4)
    result.first_30m_pnl = round(float(first_30_pnl), 2)
    result.false_premarket_loss = round(false_pm_loss, 2)
    result.false_premarket = false_pm
    if result.status != "N/A_NO_PREMARKET":
        result.status = "OK"
    result.notes.append(f"pm_meta={pm_meta}")
    return result


# ── aggregate / verdict ─────────────────────────────────────────────────────
def _aggregate(days: list[str], day_results: list[DayReplay]) -> dict[str, Any]:
    trades = [t for dr in day_results for t in dr.trades]
    m = _metrics(trades)
    daily = [
        {
            "day": dr.day,
            "status": dr.status,
            "premarket_source": dr.premarket_source,
            "premarket_bars": dr.premarket_bars,
            "premarket_direction": dr.premarket_direction,
            "first_regular_b": dr.first_regular_b,
            "first_regular_b_time": dr.first_regular_b_time,
            "premaket_held_into_regular": dr.premaket_held_into_regular,
            "first_entry_time": dr.first_entry_time,
            "first_entry_direction": dr.first_entry_direction,
            "first_entry_kind": dr.first_entry_kind,
            "net_pnl": dr.net_pnl,
            "ret_pct": dr.ret_pct,
            "first_30m_pnl": dr.first_30m_pnl,
            "false_premarket": dr.false_premarket,
            "false_premarket_loss": dr.false_premarket_loss,
            "early_vs_a_sec": dr.early_vs_a_sec,
            "round_trips": len(dr.trades),
        }
        for dr in day_results
    ]
    held = [d for d in daily if d["premaket_held_into_regular"] is not None]
    held_pct = (
        round(100.0 * sum(1 for d in held if d["premaket_held_into_regular"]) / len(held), 2)
        if held
        else None
    )
    false_days = [d for d in daily if d["false_premarket"]]
    pm_trades = [t for t in trades if t.is_premarket_entry]
    return {
        "n_days": len(days),
        "round_trips": len(trades),
        "net_pnl": m["net"],
        "ret_pct": m["ret"],
        "profit_factor": m["pf"],
        "mdd_pct": m["mdd"],
        "win_rate_pct": m["wr"],
        "first_30m_pnl_sum": round(sum(d["first_30m_pnl"] for d in daily), 2),
        "premaket_held_pct": held_pct,
        "premaket_held_n": len(held),
        "false_premarket_days": len(false_days),
        "false_premarket_loss": round(sum(d["false_premarket_loss"] for d in daily), 2),
        "premarket_entry_trades": len(pm_trades),
        "premarket_entry_net": round(sum(t.net_pnl for t in pm_trades), 2),
        "daily": daily,
        "trades": [asdict(t) for t in trades],
    }


def _early_effect(a_days: list[DayReplay], other_days: list[DayReplay]) -> dict[str, Any]:
    by_a = {d.day: d for d in a_days}
    deltas = []
    earlier = 0
    later = 0
    same = 0
    for d in other_days:
        a = by_a.get(d.day)
        if not a or not a.first_entry_time or not d.first_entry_time:
            d.early_vs_a_sec = None
            continue
        ta = pd.Timestamp(a.first_entry_time).to_pydatetime()
        tb = pd.Timestamp(d.first_entry_time).to_pydatetime()
        sec = (ta - tb).total_seconds()  # positive => other earlier than A
        d.early_vs_a_sec = sec
        deltas.append(sec)
        if sec > 30:
            earlier += 1
        elif sec < -30:
            later += 1
        else:
            same += 1
    return {
        "n_compared": len(deltas),
        "mean_sec_earlier_than_a": round(float(np.mean(deltas)), 1) if deltas else None,
        "median_sec_earlier_than_a": round(float(np.median(deltas)), 1) if deltas else None,
        "days_earlier": earlier,
        "days_later": later,
        "days_same": same,
        "first_30m_pnl_delta_vs_a": round(
            sum(d.first_30m_pnl for d in other_days) - sum(d.first_30m_pnl for d in a_days),
            2,
        ),
        "net_delta_vs_a": round(
            sum(d.net_pnl for d in other_days) - sum(d.net_pnl for d in a_days),
            2,
        ),
    }


def decide_verdict(summary: dict[str, Any]) -> dict[str, Any]:
    a = summary["IGNORE_PREMARKET"]
    b = summary["FULL_ENTRY_AT_OPEN"]
    c = summary["HALF_ENTRY_THEN_CONFIRM"]
    cov = summary["premarket_coverage"]
    reasons: list[str] = []

    real_pct = float(cov.get("real_nxt_pct") or 0)
    if real_pct < 50:
        reasons.append(
            f"Real KIS NX NXT premaket coverage only {real_pct:.0f}% of days "
            f"({cov.get('real_nxt_days')}/{cov.get('n_days')}); "
            f"remainder is {cov.get('proxy_days')} gap-proxy + {cov.get('missing_days')} missing."
        )

    def worse_mdd(x: dict, base: dict, tol: float = 0.25) -> bool:
        return float(x["mdd_pct"]) > float(base["mdd_pct"]) + tol

    false_b = int(b.get("false_premarket_days") or 0)
    false_c = int(c.get("false_premarket_days") or 0)
    false_loss_b = float(b.get("false_premarket_loss") or 0)
    false_loss_c = float(c.get("false_premarket_loss") or 0)

    # Prefer IGNORE if B/C add Net but worsen MDD / false opens materially
    prefer_ignore = False
    if b["net_pnl"] > a["net_pnl"] and worse_mdd(b, a):
        prefer_ignore = True
        reasons.append(
            f"FULL_ENTRY_AT_OPEN Net>{a['net_pnl']:.0f} but MDD worsens "
            f"({a['mdd_pct']}→{b['mdd_pct']})."
        )
    if c["net_pnl"] > a["net_pnl"] and worse_mdd(c, a):
        prefer_ignore = True
        reasons.append(
            f"HALF_ENTRY_THEN_CONFIRM Net improves but MDD worsens "
            f"({a['mdd_pct']}→{c['mdd_pct']})."
        )
    if false_b >= 3 or false_loss_b < -150_000:
        prefer_ignore = True
        reasons.append(
            f"FULL_ENTRY false premaket days={false_b} loss={false_loss_b:,.0f}."
        )
    if false_c >= 3 or false_loss_c < -150_000:
        prefer_ignore = True
        reasons.append(
            f"HALF_ENTRY false premaket days={false_c} loss={false_loss_c:,.0f}."
        )

    if prefer_ignore:
        return {"verdict": "IGNORE_PREMARKET", "reasons": reasons}

    # Score: net primary, then PF, then MDD (lower better), penalize false opens
    scores = {}
    for name, s in (
        ("IGNORE_PREMARKET", a),
        ("FULL_ENTRY_AT_OPEN", b),
        ("HALF_ENTRY_THEN_CONFIRM", c),
    ):
        score = (
            float(s["net_pnl"])
            + 50_000.0 * float(s["profit_factor"])
            - 30_000.0 * float(s["mdd_pct"])
            - 40_000.0 * float(s.get("false_premarket_days") or 0)
            + float(s.get("false_premarket_loss") or 0)  # already negative
        )
        scores[name] = score

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_s = ranked[0]
    second_s = ranked[1][1]
    reasons.append(f"Scores: { {k: round(v, 1) for k, v in scores.items()} }")

    # Low real coverage → do not crown B/C as production winner
    if real_pct < 25 and best != "IGNORE_PREMARKET":
        reasons.append(
            "Real NXT history too thin to adopt an open-entry mode; defaulting to IGNORE."
        )
        return {"verdict": "IGNORE_PREMARKET", "reasons": reasons, "scores": scores}

    if abs(best_s - second_s) < 25_000:
        reasons.append("Top-2 scores within 25k — no clear winner.")
        return {"verdict": "NO_CLEAR_WINNER", "reasons": reasons, "scores": scores}

    reasons.append(f"Best score: {best}")
    return {"verdict": best, "reasons": reasons, "scores": scores}


def render_md(report: dict[str, Any]) -> str:
    v = report["verdict"]
    cov = report["premarket_coverage"]
    lines = [
        "# MACD B — Premarket handling A/B/C (≥20d)",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Days ({len(report['days'])}): {', '.join(report['days'])}",
        f"- Capital: {INITIAL_CASH:,.0f} | TP +{TP_NET_PCT}% / SL {SL_NET_PCT}% | "
        f"fill: next 1m open + adverse + TradeCostEngine | CONTINUATION_REENTRY=off",
        f"- Signed B helpers: `evaluate_macd_direction` / `signed_hist_two_turn_*`",
        f"- Warm-up: prior day last {WARMUP_3M_BARS} completed 3m bars "
        f"(≈{WARMUP_1M_BARS} 1m)",
        f"- **Verdict: `{v['verdict']}`**",
        "",
        "## Verdict reasons",
    ]
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")

    lines += [
        "",
        "## Premarket coverage (000660 08:00–08:50)",
        "",
        f"- Real KIS NX: **{cov['real_nxt_days']}** / {cov['n_days']} "
        f"({cov['real_nxt_pct']}%)",
        f"- Overnight-gap proxy: **{cov['proxy_days']}**",
        f"- Missing: **{cov['missing_days']}**",
        f"- Notes: {cov.get('notes')}",
        "",
        "| Day | Session src | Premarket src | Bars |",
        "|-----|-------------|---------------|-----:|",
    ]
    for d in report["days"]:
        lines.append(
            f"| {d} | {report['day_sources'].get(d, '?')} | "
            f"{cov['by_day'].get(d, {}).get('source', '?')} | "
            f"{cov['by_day'].get(d, {}).get('bars', 0)} |"
        )

    lines += [
        "",
        "## Baseline summary",
        "",
        "| Metric | A IGNORE | B FULL_OPEN | C HALF_CONFIRM |",
        "|--------|---------:|------------:|---------------:|",
    ]
    a, b, c = (
        report["IGNORE_PREMARKET"],
        report["FULL_ENTRY_AT_OPEN"],
        report["HALF_ENTRY_THEN_CONFIRM"],
    )
    rows = [
        ("Net PnL", "net_pnl", "{:,.0f}"),
        ("Ret %", "ret_pct", "{}"),
        ("PF", "profit_factor", "{}"),
        ("MDD %", "mdd_pct", "{}"),
        ("Win rate %", "win_rate_pct", "{}"),
        ("Round-trips", "round_trips", "{}"),
        ("First-30m PnL Σ", "first_30m_pnl_sum", "{:,.0f}"),
        ("Premaket held %", "premaket_held_pct", "{}"),
        ("False PM days", "false_premarket_days", "{}"),
        ("False PM loss", "false_premarket_loss", "{:,.0f}"),
        ("PM entry Net", "premarket_entry_net", "{:,.0f}"),
    ]
    for label, key, fmt in rows:
        av, bv, cv = a.get(key), b.get(key), c.get(key)
        def _f(x):
            if x is None:
                return "—"
            try:
                return fmt.format(x)
            except Exception:
                return str(x)
        lines.append(f"| {label} | {_f(av)} | {_f(bv)} | {_f(cv)} |")

    ee = report.get("early_entry_effect") or {}
    lines += [
        "",
        "## Early-entry effect vs A",
        "",
        f"- B vs A: `{ee.get('FULL_ENTRY_AT_OPEN')}`",
        f"- C vs A: `{ee.get('HALF_ENTRY_THEN_CONFIRM')}`",
        "",
        "## Daily returns / first entry",
        "",
        "| Day | A Net | B Net | C Net | A 1st | B 1st | C 1st | PM dir | Held? |",
        "|-----|------:|------:|------:|-------|-------|-------|--------|-------|",
    ]
    for d in report["days"]:
        ad = next(x for x in a["daily"] if x["day"] == d)
        bd = next(x for x in b["daily"] if x["day"] == d)
        cd = next(x for x in c["daily"] if x["day"] == d)
        lines.append(
            f"| {d} | {ad['net_pnl']:,.0f} | {bd['net_pnl']:,.0f} | {cd['net_pnl']:,.0f} | "
            f"{ad.get('first_entry_direction') or '—'}@{str(ad.get('first_entry_time') or '')[11:16] or '—'} | "
            f"{bd.get('first_entry_direction') or '—'}@{str(bd.get('first_entry_time') or '')[11:16] or '—'} | "
            f"{cd.get('first_entry_direction') or '—'}@{str(cd.get('first_entry_time') or '')[11:16] or '—'} | "
            f"{bd.get('premarket_direction') or '—'} | {bd.get('premaket_held_into_regular')} |"
        )

    lines += [
        "",
        "## Stress",
        "",
        "| Scenario | A Net | B Net | C Net | A MDD | B MDD | C MDD |",
        "|----------|------:|------:|------:|------:|------:|------:|",
    ]
    for label, _, _ in SCENARIOS:
        s = report["stress"][label]
        lines.append(
            f"| {label} | {s['IGNORE_PREMARKET']['net_pnl']:,.0f} | "
            f"{s['FULL_ENTRY_AT_OPEN']['net_pnl']:,.0f} | "
            f"{s['HALF_ENTRY_THEN_CONFIRM']['net_pnl']:,.0f} | "
            f"{s['IGNORE_PREMARKET']['mdd_pct']} | "
            f"{s['FULL_ENTRY_AT_OPEN']['mdd_pct']} | "
            f"{s['HALF_ENTRY_THEN_CONFIRM']['mdd_pct']} |"
        )

    lines += [
        "",
        "## Data gaps",
        "",
        "- Naver fchart / local `replay_*_hynix_1m.csv` contain **no** bars before 09:00.",
        "- KIS `inquire-time-itemchartprice` + `FID_COND_MRKT_DIV_CODE=NX` returns "
        "**today's** NXT 08:00–08:50 only (date filter ignored).",
        "- Historical days without a cached NX file use "
        "`synthetic_overnight_gap_proxy` (prev close→open linear 08:00–08:50) "
        "so B/C entry logic is measurable; treat those days as proxy, not true NXT.",
        "",
        "## Re-run",
        "```",
        "python scripts/compare_macd_b_premarket_abc_20d.py",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--skip-kis-nxt", action="store_true")
    ap.add_argument("--no-proxy", action="store_true", help="Mark B/C N/A when real NXT missing")
    args = ap.parse_args()

    print("=" * 72)
    print("MACD B premaket A/B/C compare (≥20d)")
    print("=" * 72)

    dates, date_sources, day_data = build_day_universe(args.days, refetch_naver=False)
    print(f"Days ({len(dates)}): {dates[0]} … {dates[-1]}")

    today_iso = datetime.now().strftime("%Y-%m-%d")
    allow_proxy = not args.no_proxy

    # Coverage pass
    cov_by_day: dict[str, Any] = {}
    real_n = proxy_n = miss_n = 0
    for i, day in enumerate(dates):
        prev = dates[i - 1] if i > 0 else None
        prev_reg = _regular_session(day_data[prev][SIGNAL_SYMBOL], prev) if prev else None
        regular = _regular_session(day_data[day][SIGNAL_SYMBOL], day)
        pm, src, meta = load_premarket_for_day(
            day, regular, prev_reg, skip_kis=args.skip_kis_nxt, today_iso=today_iso
        )
        if args.no_proxy and src == "synthetic_overnight_gap_proxy":
            src = "missing_proxy_disabled"
            pm = pm.iloc[0:0]
        bars = len(pm)
        cov_by_day[day] = {"source": src, "bars": bars, "meta": meta}
        if src in ("kis_nx_cached", "kis_nx_live"):
            real_n += 1
        elif src == "synthetic_overnight_gap_proxy":
            proxy_n += 1
        else:
            miss_n += 1
        print(f"  premaket {day}: {src} bars={bars}")

    coverage = {
        "n_days": len(dates),
        "real_nxt_days": real_n,
        "real_nxt_pct": round(100.0 * real_n / max(1, len(dates)), 2),
        "proxy_days": proxy_n,
        "missing_days": miss_n,
        "by_day": cov_by_day,
        "notes": (
            "KIS NX intraday API is today-only; historical minutes use overnight-gap "
            "proxy unless a local nxt_premarket cache exists."
            if allow_proxy
            else "Proxy disabled (--no-proxy); B/C fall back to A-like on missing days."
        ),
    }

    def run_scenario(label: str, delay_min: int, adverse_pct: float) -> dict[str, Any]:
        print(f"\n## Scenario {label} delay={delay_min}m adverse={adverse_pct}%")
        out: dict[str, Any] = {}
        per_strat_days: dict[str, list[DayReplay]] = {s: [] for s in STRATEGIES}
        for strategy in STRATEGIES:
            for i, day in enumerate(dates):
                prev = dates[i - 1] if i > 0 else None
                prev_pack = None
                if prev:
                    prev_pack = {**day_data[prev], "__day": prev}
                dr = replay_day(
                    day,
                    strategy,
                    day_data[day],
                    prev_pack,
                    delay_min=delay_min,
                    adverse_pct=adverse_pct,
                    skip_kis=args.skip_kis_nxt,
                    allow_proxy=allow_proxy,
                    today_iso=today_iso,
                )
                per_strat_days[strategy].append(dr)
                print(
                    f"  {strategy[:1]} {day[-5:]} net={dr.net_pnl:,.0f} "
                    f"pm={dr.premarket_direction or '-'} "
                    f"1st={dr.first_entry_direction or '-'}"
                )
            out[strategy] = _aggregate(dates, per_strat_days[strategy])

        # early-entry vs A (mutate day dicts already inside aggregates — recompute)
        a_days = per_strat_days["IGNORE_PREMARKET"]
        early = {}
        for s in ("FULL_ENTRY_AT_OPEN", "HALF_ENTRY_THEN_CONFIRM"):
            early[s] = _early_effect(a_days, per_strat_days[s])
            # refresh early_vs_a_sec into aggregate daily
            by = {d.day: d.early_vs_a_sec for d in per_strat_days[s]}
            for row in out[s]["daily"]:
                row["early_vs_a_sec"] = by.get(row["day"])
        out["early_entry_effect"] = early
        return out

    baseline = run_scenario("baseline", 1, 0.05)
    stress = {}
    for label, delay, adv in SCENARIOS:
        if label == "baseline":
            stress[label] = {
                s: {
                    "net_pnl": baseline[s]["net_pnl"],
                    "mdd_pct": baseline[s]["mdd_pct"],
                    "profit_factor": baseline[s]["profit_factor"],
                }
                for s in STRATEGIES
            }
            continue
        sc = run_scenario(label, delay, adv)
        stress[label] = {
            s: {
                "net_pnl": sc[s]["net_pnl"],
                "mdd_pct": sc[s]["mdd_pct"],
                "profit_factor": sc[s]["profit_factor"],
            }
            for s in STRATEGIES
        }

    summary_for_verdict = {
        **{s: baseline[s] for s in STRATEGIES},
        "premarket_coverage": coverage,
    }
    verdict = decide_verdict(summary_for_verdict)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": dates,
        "day_sources": date_sources,
        "premarket_coverage": coverage,
        "rules": {
            "capital": INITIAL_CASH,
            "tp_sl": [TP_NET_PCT, SL_NET_PCT],
            "entry_cutoff": "14:55",
            "flatten": "15:00",
            "fill": "next 1m open + adverse + TradeCostEngine",
            "continuation_reentry": False,
            "signal": "signed_hist_two_turn (shared macd_hynix_strategy)",
            "warmup_3m_bars": WARMUP_3M_BARS,
            "confirm_sec": CONFIRM_SEC,
            "half_pct": HALF_PCT,
            "allow_gap_proxy": allow_proxy,
        },
        "IGNORE_PREMARKET": baseline["IGNORE_PREMARKET"],
        "FULL_ENTRY_AT_OPEN": baseline["FULL_ENTRY_AT_OPEN"],
        "HALF_ENTRY_THEN_CONFIRM": baseline["HALF_ENTRY_THEN_CONFIRM"],
        "early_entry_effect": baseline["early_entry_effect"],
        "stress": stress,
        "verdict": verdict,
    }

    # Slim trades in JSON for size (keep daily + summary; drop full trade blasts optional)
    # Keep trades — useful for audit; file may be large but OK.

    STATE.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    OUT_MD.write_text(render_md(report), encoding="utf-8")
    print("\n" + "=" * 72)
    print(f"VERDICT: {verdict['verdict']}")
    for r in verdict.get("reasons") or []:
        print(f"  - {r}")
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_MD}")


if __name__ == "__main__":
    main()
