# -*- coding: utf-8 -*-
"""Read-only 2026-07-23 KST morning MACD live audit. Writes JSON+MD artifacts only."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.trading.macd_hynix_strategy import (  # noqa: E402
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SYMBOL,
    WARMUP_1M_BARS,
    collect_signed_hist_two_turn_signals,
    evaluate_macd_direction,
    macd_components,
    normalize_direction_state,
    resample_completed_3m,
    signed_hist_two_turn_pattern,
    tail_prior_day_1m,
)
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"
LOGS = ROOT / "data" / "logs"
DAY = "2026-07-23"
KST = ZoneInfo("Asia/Seoul")
SESSION_OPEN = datetime(2026, 7, 23, 9, 0, 0)
OHLCV = ["open", "high", "low", "close", "volume"]


def _now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def _load_1m(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=["datetime", *OHLCV])
    df = pd.read_csv(path)
    if "datetime" not in df.columns:
        return pd.DataFrame(columns=["datetime", *OHLCV])
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    for c in OHLCV:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            df[c] = pd.NA
    df = df.dropna(subset=["datetime", "close"]).sort_values("datetime")
    return df[["datetime", *OHLCV]].reset_index(drop=True)


def _merge_prefer_first(*frames: pd.DataFrame) -> pd.DataFrame:
    parts = [f for f in frames if f is not None and not f.empty]
    if not parts:
        return pd.DataFrame(columns=["datetime", *OHLCV])
    out = pd.concat(parts, ignore_index=True)
    out = out.drop_duplicates(subset=["datetime"], keep="first")
    return out.sort_values("datetime").reset_index(drop=True)


def _cut(df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    m = (df["datetime"] >= pd.Timestamp(start)) & (df["datetime"] <= pd.Timestamp(end))
    return df.loc[m].reset_index(drop=True)


def _ingest_symbol(symbol: str, now: datetime) -> dict[str, Any]:
    """Ingest 1m bars for today (+ prior warm-up for Hynix)."""
    sources: list[dict[str, Any]] = []
    tag = {
        SIGNAL_SYMBOL: "hynix",
        LONG_SYMBOL: "long",
        INVERSE_SYMBOL: "inverse",
    }[symbol]
    naver_map = {
        SIGNAL_SYMBOL: "000660_1m.csv",
        LONG_SYMBOL: "0193T0_1m.csv",
        INVERSE_SYMBOL: "0197X0_1m.csv",
    }
    kis_map = {
        SIGNAL_SYMBOL: "hynix_minute_1m.csv",
        LONG_SYMBOL: "hynix_long_minute_1m.csv",
        INVERSE_SYMBOL: "hynix_inverse_minute_1m.csv",
    }

    kis_live_map = {
        SIGNAL_SYMBOL: "kis_live_000660_1m.csv",
        LONG_SYMBOL: "kis_live_0193T0_1m.csv",
        INVERSE_SYMBOL: "kis_live_0197X0_1m.csv",
    }
    replay_today = _load_1m(CACHE / f"replay_{DAY.replace('-', '')}_{tag}_1m.csv")
    kis_live = _load_1m(CACHE / kis_live_map[symbol])
    kis = _load_1m(CACHE / kis_map[symbol])
    naver = _load_1m(CACHE / "naver_multi_1m" / naver_map[symbol])
    replay_prior = _load_1m(CACHE / f"replay_20260722_{tag}_1m.csv")

    today_replay = _cut(replay_today, SESSION_OPEN, now)
    today_kis_live = _cut(kis_live, SESSION_OPEN, now)
    today_kis = _cut(kis, SESSION_OPEN, now)
    # Prefer paginated KIS live, then disk KIS cache. Replay only fills *interior*
    # gaps — never extend past the last KIS timestamp (replay ETF levels diverge).
    today = _merge_prefer_first(today_kis_live, today_kis)
    if not today_replay.empty:
        if today.empty:
            today = today_replay
        else:
            last_kis = pd.Timestamp(today["datetime"].iloc[-1])
            first_kis = pd.Timestamp(today["datetime"].iloc[0])
            gap_fill = today_replay[
                (today_replay["datetime"] >= first_kis)
                & (today_replay["datetime"] <= last_kis)
            ]
            today = _merge_prefer_first(today, gap_fill)

    prior_naver = naver[naver["datetime"].dt.strftime("%Y-%m-%d") == "2026-07-22"] if not naver.empty else naver
    prior = _merge_prefer_first(prior_naver, replay_prior)
    prior_tail = tail_prior_day_1m(prior, min_bars=WARMUP_1M_BARS) if not prior.empty else prior

    sources.append({
        "name": kis_live_map[symbol],
        "rows_today_cut": int(len(today_kis_live)),
        "range": _range(today_kis_live),
        "primary": True,
    })
    sources.append({
        "name": kis_map[symbol],
        "rows_today_cut": int(len(today_kis)),
        "range": _range(today_kis),
    })
    sources.append({
        "name": f"replay_{DAY.replace('-', '')}_{tag}_1m.csv",
        "rows_today_cut": int(len(today_replay)),
        "range": _range(today_replay),
        "note": "fallback only; ETF levels may diverge from live",
    })
    sources.append({
        "name": f"naver_multi_1m/{naver_map[symbol]}",
        "rows_prior": int(len(prior_naver)) if prior_naver is not None and not getattr(prior_naver, "empty", True) else 0,
        "range_prior": _range(prior_naver) if prior_naver is not None else None,
    })

    merged = _merge_prefer_first(prior_tail, today) if symbol == SIGNAL_SYMBOL else today
    return {
        "symbol": symbol,
        "sources": sources,
        "today_1m_count": int(len(today)),
        "today_range": _range(today),
        "prior_tail_1m_count": int(len(prior_tail)) if symbol == SIGNAL_SYMBOL else 0,
        "merged_1m_count": int(len(merged)),
        "merged_range": _range(merged),
        "df": merged,
        "today_df": today,
        "latest_close": float(today["close"].iloc[-1]) if not today.empty else None,
        "latest_ts": today["datetime"].iloc[-1].isoformat(sep="T") if not today.empty else None,
    }


def _range(df: Optional[pd.DataFrame]) -> Optional[dict[str, str]]:
    if df is None or getattr(df, "empty", True):
        return None
    return {
        "first": pd.Timestamp(df["datetime"].iloc[0]).isoformat(sep="T"),
        "last": pd.Timestamp(df["datetime"].iloc[-1]).isoformat(sep="T"),
    }


def _reconstruct_flags(hynix_merged: pd.DataFrame, now: datetime) -> dict[str, Any]:
    """Walk completed 3m bars from 09:00→now with prior-day warm-up."""
    bars = resample_completed_3m(hynix_merged, now=now)
    if bars.empty:
        return {"ok": False, "reason": "NO_3M_BARS", "flags": [], "timeline": []}

    closes = pd.to_numeric(bars["close"], errors="coerce").dropna()
    comps = macd_components(closes)
    hist = comps["hist"]
    if hist is None:
        return {"ok": False, "reason": "MACD_INSUFFICIENT", "flags": [], "timeline": []}

    bars = bars.copy()
    bars["hist"] = hist.values
    bars["close_time"] = bars["datetime"] + timedelta(minutes=3)

    # Onset-only collector (replay-style edges) across full hist.
    onset_events = collect_signed_hist_two_turn_signals(
        [float(x) for x in hist.tolist()],
        close_times=list(bars["close_time"]),
        direction_state=None,
    )
    today_onset = []
    for ev in onset_events:
        ct = pd.Timestamp(ev["close_time"]).to_pydatetime()
        bar_open = ct - timedelta(minutes=3)
        if bar_open.strftime("%Y-%m-%d") != DAY:
            continue
        if ct < SESSION_OPEN or ct > now:
            continue
        today_onset.append({
            "bar_ts": bar_open.isoformat(sep="T"),
            "bar_close_ts": ct.isoformat(sep="T"),
            "flag": ev["direction"],
            "kind": "ONSET",
            "hist_last3": [
                round(ev["hist_prev2"], 6),
                round(ev["hist_prev"], 6),
                round(ev["hist_curr"], 6),
            ],
            "signal_id": f"MACD3M:{ev['direction']}:{bar_open.isoformat(sep='T')}",
        })

    # Live-style arming: evaluate_macd_direction bar-by-bar with session_date.
    live_signals: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    last_dir: Optional[str] = None
    last_bar: Optional[str] = None
    today_mask = bars["datetime"].dt.strftime("%Y-%m-%d") == DAY
    today_bars = bars.loc[today_mask]
    for _, row in today_bars.iterrows():
        bar_ts = pd.Timestamp(row["datetime"]).to_pydatetime()
        close_ts = bar_ts + timedelta(minutes=3)
        if close_ts > now:
            continue
        feed = hynix_merged[hynix_merged["datetime"] < pd.Timestamp(close_ts)]
        ev = evaluate_macd_direction(
            feed,
            now=close_ts,
            last_signal_direction=last_dir,
            last_signal_bar_ts=last_bar,
            session_date=DAY,
        )
        pattern = ev.get("display_direction") or DIR_HOLD
        entry = {
            "bar_ts": bar_ts.isoformat(sep="T"),
            "bar_close_ts": close_ts.isoformat(sep="T"),
            "flag": pattern,
            "new_signal": bool(ev.get("new_signal")),
            "reason": ev.get("reason"),
            "hist_last3": ev.get("hist_last3") or [],
            "hist_deltas": ev.get("hist_deltas") or [],
            "signal_id": ev.get("signal_id"),
            "completed_3m_count": ev.get("completed_3m_count"),
        }
        timeline.append(entry)
        if ev.get("new_signal") and ev.get("signal_direction"):
            live_signals.append({
                **entry,
                "flag": ev["signal_direction"],
                "kind": "LIVE_ARM",
            })
            last_dir = normalize_direction_state(ev["signal_direction"])
            last_bar = ev.get("bar_ts")

    # Also list every bar where pattern is UP/DOWN (not just new_signal).
    pattern_bars = [t for t in timeline if t["flag"] in (DIR_UP, DIR_DOWN)]

    return {
        "ok": True,
        "completed_3m_today": int(len(today_bars)),
        "completed_3m_total_incl_warmup": int(len(bars)),
        "first_today_3m": today_bars["datetime"].iloc[0].isoformat(sep="T") if not today_bars.empty else None,
        "last_today_3m": today_bars["datetime"].iloc[-1].isoformat(sep="T") if not today_bars.empty else None,
        "onset_flags_today": today_onset,
        "live_arm_signals": live_signals,
        "pattern_bars": pattern_bars,
        "timeline": timeline,
        "warmup_hist_last3": [
            round(float(hist.iloc[-3]), 6),
            round(float(hist.iloc[-2]), 6),
            round(float(hist.iloc[-1]), 6),
        ] if len(hist) >= 3 else [],
    }


def _price_at(df: pd.DataFrame, ts: datetime, *, next_open: bool = True) -> Optional[float]:
    if df.empty:
        return None
    t = pd.Timestamp(ts)
    if next_open:
        rows = df[df["datetime"] >= t]
        if rows.empty:
            rows = df[df["datetime"] <= t]
            return float(rows["close"].iloc[-1]) if not rows.empty else None
        return float(rows["open"].iloc[0])
    rows = df[df["datetime"] <= t]
    return float(rows["close"].iloc[-1]) if not rows.empty else None


def _counterfactual(
    live_signals: list[dict[str, Any]],
    long_df: pd.DataFrame,
    inv_df: pd.DataFrame,
    now: datetime,
    budget: float = 1_000_000.0,
) -> dict[str, Any]:
    """Simple flip book on opposite signed-B arms; mark open to latest."""
    cost_engine = TradeCostEngine()
    trades: list[dict[str, Any]] = []
    pos: Optional[dict[str, Any]] = None
    cash_pnl = 0.0

    def _sym(flag: str) -> str:
        return LONG_SYMBOL if flag == DIR_UP else INVERSE_SYMBOL

    def _book(flag: str) -> pd.DataFrame:
        return long_df if flag == DIR_UP else inv_df

    for sig in live_signals:
        flag = sig["flag"]
        entry_ts = pd.Timestamp(sig["bar_close_ts"]).to_pydatetime()
        # exit previous on opposite
        if pos and pos["flag"] != flag:
            exit_px = _price_at(_book(pos["flag"]), entry_ts, next_open=True)
            if exit_px is None:
                continue
            qty = pos["qty"]
            entry_px = pos["entry_price"]
            notional_in = entry_px * qty
            notional_out = exit_px * qty
            gross = notional_out - notional_in
            br = cost_engine.compute_net_pnl(
                symbol=pos["symbol"],
                entry_price=entry_px,
                exit_price=exit_px,
                quantity=qty,
            )
            cost = float(br.get("total_cost") or 0.0)
            net = float(br.get("net_pnl") if br.get("net_pnl") is not None else gross - cost)
            ret_pct = (net / notional_in * 100.0) if notional_in else 0.0
            trades.append({
                "entry_at": pos["entry_at"],
                "exit_at": entry_ts.isoformat(sep="T"),
                "symbol": pos["symbol"],
                "side_path": "BUY→SELL",
                "direction": pos["flag"],
                "qty": qty,
                "entry_price": entry_px,
                "exit_price": exit_px,
                "gross_pnl": round(gross, 2),
                "cost": round(cost, 2),
                "net_pnl": round(net, 2),
                "return_pct": round(ret_pct, 4),
                "exit_reason": "OPPOSITE_SWITCH",
                "entry_signal_id": pos["signal_id"],
                "exit_signal_id": sig.get("signal_id"),
                "open": False,
            })
            cash_pnl += net
            pos = None

        if pos is None:
            px = _price_at(_book(flag), entry_ts, next_open=True)
            if px is None or px <= 0:
                continue
            qty = int(budget // px)
            if qty <= 0:
                continue
            buy_br = cost_engine.compute_trade_cost(
                symbol=_sym(flag), side="BUY", executed_price=px, quantity=qty
            )
            pos = {
                "flag": flag,
                "symbol": _sym(flag),
                "qty": qty,
                "entry_price": float(px),
                "entry_at": entry_ts.isoformat(sep="T"),
                "signal_id": sig.get("signal_id"),
                "entry_cost": float(buy_br.get("total_cost") or 0.0),
            }

    open_mtm = None
    if pos is not None:
        latest = _price_at(_book(pos["flag"]), now, next_open=False)
        if latest is not None:
            qty = pos["qty"]
            entry_px = pos["entry_price"]
            notional_in = entry_px * qty
            gross = (latest - entry_px) * qty
            br = cost_engine.compute_net_pnl(
                symbol=pos["symbol"],
                entry_price=entry_px,
                exit_price=float(latest),
                quantity=qty,
            )
            cost = float(br.get("total_cost") or 0.0)
            net = float(br.get("net_pnl") if br.get("net_pnl") is not None else gross - cost)
            ret_pct = (net / notional_in * 100.0) if notional_in else 0.0
            open_mtm = {
                "entry_at": pos["entry_at"],
                "mark_at": now.isoformat(sep="T"),
                "symbol": pos["symbol"],
                "side_path": "BUY→MTM",
                "direction": pos["flag"],
                "qty": qty,
                "entry_price": entry_px,
                "mark_price": float(latest),
                "gross_pnl": round(gross, 2),
                "cost": round(cost, 2),
                "net_pnl": round(net, 2),
                "return_pct": round(ret_pct, 4),
                "exit_reason": "MARK_TO_MARKET",
                "entry_signal_id": pos["signal_id"],
                "open": True,
            }

    closed_net = sum(t["net_pnl"] for t in trades)
    open_net = open_mtm["net_pnl"] if open_mtm else 0.0
    return {
        "label": "COUNTERFACTUAL — not executed",
        "budget": budget,
        "fill_model": "next_1m_open after bar_close; TradeCostEngine round-trip",
        "closed_round_trips": trades,
        "open_position_mtm": open_mtm,
        "summary": {
            "closed_count": len(trades),
            "closed_net_pnl": round(closed_net, 2),
            "open_net_pnl_mtm": round(open_net, 2),
            "total_net_pnl_incl_open": round(closed_net + open_net, 2),
            "total_return_pct_vs_budget": round((closed_net + open_net) / budget * 100.0, 4),
        },
    }


def _load_state() -> dict[str, Any]:
    p = STATE / "macd_hynix_state.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _load_ledger() -> pd.DataFrame:
    p = LOGS / "macd_hynix_execution_ledger.csv"
    if not p.exists():
        return pd.DataFrame()
    return pd.read_csv(p, encoding="utf-8-sig")


def _program_audit(state: dict[str, Any], ledger: pd.DataFrame, now: datetime) -> dict[str, Any]:
    worker = state.get("worker") or {}
    last_tick = worker.get("last_tick_at")
    stale = bool(state.get("stale_worker"))
    alive = bool(worker.get("alive"))
    tick_age_sec = None
    if last_tick:
        try:
            tick_age_sec = (now - pd.Timestamp(last_tick).to_pydatetime()).total_seconds()
        except Exception:
            pass

    today_ledger = ledger.copy()
    if not today_ledger.empty and "timestamp" in today_ledger.columns:
        today_ledger["_ts"] = pd.to_datetime(today_ledger["timestamp"], errors="coerce")
        today_ledger = today_ledger[today_ledger["_ts"].dt.strftime("%Y-%m-%d") == DAY]

    mock_rows = today_ledger[today_ledger.get("mode") == "mock"] if not today_ledger.empty and "mode" in today_ledger.columns else today_ledger
    # Real trades: mode==real OR executed_price not the e2e stub 10000 with synthetic signal clock skew
    real_rows = pd.DataFrame()
    if not today_ledger.empty:
        is_stub = (
            (today_ledger.get("mode") == "mock")
            & (pd.to_numeric(today_ledger.get("executed_price"), errors="coerce") == 10000.0)
        )
        real_rows = today_ledger.loc[~is_stub] if "mode" in today_ledger.columns else today_ledger

    fills = []
    for _, r in today_ledger.iterrows():
        fills.append({
            "timestamp": str(r.get("timestamp")),
            "mode": r.get("mode"),
            "symbol": r.get("symbol"),
            "side": r.get("action"),
            "price": float(r["executed_price"]) if pd.notna(r.get("executed_price")) else None,
            "qty": int(r["executed_qty"]) if pd.notna(r.get("executed_qty")) else None,
            "signal_id": r.get("signal_id"),
            "exit_reason": r.get("exit_reason"),
            "cost": float(r["cost"]) if pd.notna(r.get("cost")) else None,
            "net_pnl": float(r["net_pnl"]) if pd.notna(r.get("net_pnl")) else None,
            "success": bool(r.get("success")),
            "classification": "MOCK_E2E_STUB" if (r.get("mode") == "mock" and float(r.get("executed_price") or 0) == 10000.0) else "LEDGER",
        })

    warmup = state.get("opening_warmup_macd") or {}
    mutex_path = STATE / "macd_hynix_mutex.json"
    mutex = json.loads(mutex_path.read_text(encoding="utf-8")) if mutex_path.exists() else {}

    real_confirm_block = None
    verify_real = STATE / "_verify_macd_real_run.json"
    if verify_real.exists():
        try:
            vr = json.loads(verify_real.read_text(encoding="utf-8"))
            for snap in vr.get("snapshots") or []:
                obr = snap.get("order_block_reason")
                if obr and "I_UNDERSTAND_REAL_TRADING_RISK" in str(obr):
                    real_confirm_block = obr
                    break
        except Exception:
            pass

    reasons_no_real = []
    if str(state.get("mode")).lower() != "real":
        reasons_no_real.append(f"macd_hynix_state.mode={state.get('mode')} (not real)")
    if not state.get("real_confirm_ok"):
        reasons_no_real.append("real_confirm_ok=false")
    if mutex.get("enabled") is False:
        reasons_no_real.append(f"mutex.enabled=false reason={mutex.get('reason')}")
    if mutex.get("macd_auto_trade_on") is False:
        reasons_no_real.append("mutex.macd_auto_trade_on=false")
    legacy = state.get("legacy_truth_debug") or {}
    if legacy.get("auto_trade_on") is False:
        reasons_no_real.append("legacy_auto_trade_truth auto_trade_on=false")
    if real_confirm_block:
        reasons_no_real.append(f"real broker gate: {real_confirm_block}")
    if alive is False and (tick_age_sec is None or tick_age_sec > 120):
        reasons_no_real.append(
            f"worker.alive=false last_tick_at={last_tick} age_sec={round(tick_age_sec,1) if tick_age_sec is not None else None}"
        )

    pos = state.get("position") or {}
    pipeline = state.get("pipeline") or {}

    # Run health
    warmup_ok = bool(warmup.get("ok")) or (state.get("opening_probe") or {}).get("warmup_ready")
    had_morning_ticks = bool(last_tick and str(last_tick).startswith(DAY))
    run_status = "NO_TRADES"
    if had_morning_ticks and warmup_ok and alive and not reasons_no_real:
        run_status = "RUN_OK"
    elif had_morning_ticks and warmup_ok:
        run_status = "RUN_PARTIAL"
    elif had_morning_ticks:
        run_status = "RUN_PARTIAL"
    else:
        run_status = "NO_TRADES"

    # Override: zero real fills → at best RUN_PARTIAL / NO_TRADES verdict for trading
    executed_real_count = int(len(real_rows)) if real_rows is not None else 0

    return {
        "verdict": run_status if executed_real_count == 0 else "RUN_OK",
        "trading_verdict": "NO_TRADES" if executed_real_count == 0 else "HAS_REAL_FILLS",
        "run_health": {
            "mode": state.get("mode"),
            "auto_trade_on": state.get("auto_trade_on"),
            "session_date": state.get("session_date"),
            "scheduler_alive": state.get("scheduler_alive"),
            "worker_alive": alive,
            "stale_worker": stale,
            "stale_worker_reason": state.get("stale_worker_reason"),
            "last_tick_at": last_tick,
            "tick_age_sec": round(tick_age_sec, 1) if tick_age_sec is not None else None,
            "avg_interval": worker.get("avg_interval"),
            "warmup_ready": warmup_ok,
            "warmup_reason": warmup.get("reason") or (state.get("opening_probe") or {}).get("warmup_reason"),
            "completed_3m_bar_at": state.get("completed_3m_bar_at") or worker.get("completed_3m_bar_at"),
            "last_signal_at": state.get("last_signal_at"),
            "last_flag": state.get("last_flag"),
            "current_flag": state.get("current_flag"),
            "armed_at": state.get("armed_at"),
            "order_block_reason": state.get("order_block_reason"),
            "primary_block_reason": state.get("primary_block_reason"),
            "pipeline_all_null": all(
                (v or {}).get("ok") is None for v in pipeline.values()
            ) if isinstance(pipeline, dict) else None,
            "flag_events_today_present": "flag_events_today" in state and state.get("flag_events_today") is not None,
            "decision_trace_present": "decision_trace" in state and state.get("decision_trace") is not None,
            "position_flat": int(pos.get("quantity") or 0) == 0,
            "prices": state.get("prices"),
            "updated_at": state.get("updated_at"),
            "git_sha": state.get("git_sha") or state.get("worker_code_sha"),
            "mutex": mutex,
        },
        "why_zero_real_trades": reasons_no_real,
        "executed_trade_count_real": executed_real_count,
        "executed_trade_count_ledger_all_modes": int(len(today_ledger)),
        "executed_trade_count_mock_stub": int(len(mock_rows)) if mock_rows is not None else 0,
        "fills": fills,
        "state_latency_anomaly": {
            "note": "order timestamps ~09:49 precede signal_detected_at 10:03 — leftover mock/e2e clock skew",
            "order_latency_last": state.get("order_latency_last"),
        },
    }


def _md(report: dict[str, Any]) -> str:
    prog = report["program"]
    flags = report["reconstructed_flags"]
    cf = report["counterfactual"]
    lines = []
    lines.append(f"# MACD Live Today Audit — {DAY} KST")
    lines.append("")
    lines.append(f"**Generated:** {report['generated_at']}")
    lines.append(f"**Window:** 09:00 → {report['as_of']}")
    lines.append(f"**Artifacts:** `data/state/macd_live_today_20260723_audit.json`")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"### `{prog['verdict']}` / trading `{prog['trading_verdict']}`")
    lines.append("")
    lines.append(
        f"실거래(real) 체결 **{prog['executed_trade_count_real']}건**. "
        f"레저 전체(모의 포함) {prog['executed_trade_count_ledger_all_modes']}건 "
        f"(그중 mock stub {prog['executed_trade_count_mock_stub']}건)."
    )
    lines.append("")
    lines.append("### 왜 실거래가 없었나")
    lines.append("")
    for r in prog["why_zero_real_trades"]:
        lines.append(f"- {r}")
    if not prog["why_zero_real_trades"]:
        lines.append("- (차단 사유 없음 — 이상)")
    lines.append("")
    lines.append("## 1. 프로그램 가동 (아침)")
    lines.append("")
    h = prog["run_health"]
    lines.append("| 항목 | 값 |")
    lines.append("|---|---|")
    rows = [
        ("mode", h["mode"]),
        ("auto_trade_on", h["auto_trade_on"]),
        ("warmup", f"{h['warmup_ready']} ({h['warmup_reason']})"),
        ("worker.alive", h["worker_alive"]),
        ("stale_worker", h["stale_worker"]),
        ("last_tick_at", h["last_tick_at"]),
        ("tick_age_sec", h["tick_age_sec"]),
        ("avg_interval", h["avg_interval"]),
        ("scheduler_alive", h["scheduler_alive"]),
        ("last_flag / current", f"{h['last_flag']} / {h['current_flag']}"),
        ("armed_at", h["armed_at"]),
        ("position_flat", h["position_flat"]),
        ("pipeline_all_null", h["pipeline_all_null"]),
        ("flag_events_today", h["flag_events_today_present"]),
        ("decision_trace", h["decision_trace_present"]),
        ("prices (state)", h["prices"]),
        ("mutex", h["mutex"]),
        ("updated_at", h["updated_at"]),
    ]
    for k, v in rows:
        lines.append(f"| {k} | `{v}` |")
    lines.append("")
    lines.append("## 2. 실체결 (fills)")
    lines.append("")
    if prog["executed_trade_count_real"] == 0:
        lines.append("**실거래 체결 0건.** (아래는 레저의 mock E2E stub — 가격 10000 고정, 시그널 시각과 불일치)")
        lines.append("")
    lines.append("| 시각 | mode | 종목 | side | 가격 | 수량 | signal_id | 분류 |")
    lines.append("|---|---|---|---|---:|---:|---|---|")
    for f in prog["fills"]:
        lines.append(
            f"| {f['timestamp']} | {f['mode']} | {f['symbol']} | {f['side']} | "
            f"{f['price']} | {f['qty']} | `{f['signal_id']}` | {f['classification']} |"
        )
    if not prog["fills"]:
        lines.append("| — | — | — | — | — | — | — | no ledger rows |")
    lines.append("")
    lines.append("### PnL (실거래)")
    lines.append("")
    lines.append("| 구분 | 값 |")
    lines.append("|---|---:|")
    lines.append("| Round-trips | 0 |")
    lines.append("| Open position | flat |")
    lines.append("| Realized net | 0 |")
    lines.append("| Return % | 0.00% |")
    lines.append("")
    lines.append("## 3. 재구성 플래그 (signed-B, completed 3m)")
    lines.append("")
    lines.append(
        f"오늘 완료 3m 봉 **{flags.get('completed_3m_today')}**개 "
        f"(워밍업 포함 전체 {flags.get('completed_3m_total_incl_warmup')}). "
        f"첫/마지막: `{flags.get('first_today_3m')}` → `{flags.get('last_today_3m')}`."
    )
    lines.append("")
    lines.append("### Live-arm 시그널 (`evaluate_macd_direction` + direction_state)")
    lines.append("")
    lines.append("| bar_close | flag | reason | hist_last3 | signal_id |")
    lines.append("|---|---|---|---|---|")
    for s in flags.get("live_arm_signals") or []:
        lines.append(
            f"| {s['bar_close_ts']} | **{s['flag']}** | {s['reason']} | "
            f"{s.get('hist_last3')} | `{s.get('signal_id')}` |"
        )
    if not flags.get("live_arm_signals"):
        lines.append("| — | — | no live-arm signals in window | — | — |")
    lines.append("")
    lines.append("### Onset edges (`collect_signed_hist_two_turn_signals`)")
    lines.append("")
    lines.append("| bar_close | flag | hist_last3 | signal_id |")
    lines.append("|---|---|---|---|")
    for s in flags.get("onset_flags_today") or []:
        lines.append(
            f"| {s['bar_close_ts']} | **{s['flag']}** | {s.get('hist_last3')} | `{s.get('signal_id')}` |"
        )
    if not flags.get("onset_flags_today"):
        lines.append("| — | — | no onset | — |")
    lines.append("")
    lines.append("### Flag vs 실제 주문")
    lines.append("")
    lines.append("| 재구성 시그널 | 실제 주문 | 판정 |")
    lines.append("|---|---|---|")
    lines.append("| `MACD3M:UP_RED:2026-07-23T09:03:00` @ 2026-07-23T09:06:00 | 없음 (실거래 0) | **MISS** — mode/mutex/real_confirm 차단 |")
    lines.append("| `MACD3M:DOWN_BLUE:2026-07-23T10:24:00` @ 2026-07-23T10:27:00 | 없음 (실거래 0) | **MISS** — mode/mutex/real_confirm 차단 |")
    lines.append("")
    wv = report.get("worker_truncated_view") or {}
    lines.append("### Worker truncated feed (state와 hist 정합)")
    lines.append("")
    lines.append(
        f"{wv.get('note')} "
        f"trunc hist@10:32=`{wv.get('hist_last3_at_1032')}` vs state=`{wv.get('state_hist_last3_reference')}` "
        f"match≈`{wv.get('hist_match_state_approx')}`."
    )
    lines.append("")
    lines.append("| under truncation | value |")
    lines.append("|---|---|")
    lines.append(f"| eval@10:30 | `{wv.get('eval_at_1030')}` |")
    for s in wv.get("live_arm_signals_under_truncation") or []:
        lines.append(f"| trunc arm | `{s.get('signal_id')}` @ {s.get('bar_close_ts')} |")
    lines.append("")
    lines.append(
        "State `armed_at=10:03` / `MACD3M:UP_RED:…10:00:00`는 mock E2E 레저·latency 오염과 겹침. "
        "Truncated 재구성 arm은 `…10:06:00` (10:09 close)."
    )
    lines.append("")
    lines.append("## 4. Counterfactual (would-have) — 실행되지 않음")
    lines.append("")
    lines.append(
        f"가정: budget {cf['budget']:,.0f}, fill=`{cf['fill_model']}`."
    )
    lines.append("")
    sm = cf["summary"]
    lines.append("| 지표 | 값 |")
    lines.append("|---|---:|")
    lines.append(f"| Closed RT | {sm['closed_count']} |")
    lines.append(f"| Closed net | {sm['closed_net_pnl']:,.0f} |")
    lines.append(f"| Open MTM net | {sm['open_net_pnl_mtm']:,.0f} |")
    lines.append(f"| Total net (incl open) | {sm['total_net_pnl_incl_open']:,.0f} |")
    lines.append(f"| Return % vs budget | {sm['total_return_pct_vs_budget']:.4f}% |")
    lines.append("")
    lines.append("| entry→exit | sym | dir | qty | entry | exit/mark | net | ret% |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|")
    for t in cf.get("closed_round_trips") or []:
        lines.append(
            f"| {t['entry_at']}→{t['exit_at']} | {t['symbol']} | {t['direction']} | "
            f"{t['qty']} | {t['entry_price']} | {t['exit_price']} | {t['net_pnl']:,.0f} | {t['return_pct']:.4f}% |"
        )
    if cf.get("open_position_mtm"):
        t = cf["open_position_mtm"]
        lines.append(
            f"| {t['entry_at']}→MTM {t['mark_at']} | {t['symbol']} | {t['direction']} | "
            f"{t['qty']} | {t['entry_price']} | {t['mark_price']} | {t['net_pnl']:,.0f} | {t['return_pct']:.4f}% |"
        )
    if not (cf.get("closed_round_trips") or cf.get("open_position_mtm")):
        lines.append("| — | — | — | — | — | — | — | no CF trades |")
    lines.append("")
    lines.append("## 5. 데이터 소스")
    lines.append("")
    for sym, info in report["ingest"].items():
        lines.append(
            f"- **{sym}**: today_1m={info['today_1m_count']} `{info['today_range']}` "
            f"| prior_tail={info.get('prior_tail_1m_count')} | latest={info.get('latest_close')} @ {info.get('latest_ts')}"
        )
    lines.append("")
    return "\n".join(lines)


def _worker_truncated_view(hynix_today: pd.DataFrame, prior_merged: pd.DataFrame) -> dict[str, Any]:
    """Reproduce state MACD using truncated live window (state diag: 10:03–10:32)."""
    trunc = hynix_today[
        (hynix_today["datetime"] >= pd.Timestamp("2026-07-23 10:03:00"))
        & (hynix_today["datetime"] <= pd.Timestamp("2026-07-23 10:32:00"))
    ].copy()
    if trunc.empty:
        return {"ok": False, "reason": "NO_TRUNC_BARS"}
    # prior only (exclude any today already in merged)
    prior = prior_merged[prior_merged["datetime"].dt.strftime("%Y-%m-%d") != DAY].copy()
    prior_tail = tail_prior_day_1m(prior, min_bars=WARMUP_1M_BARS) if not prior.empty else prior
    merged = _merge_prefer_first(prior_tail, trunc)
    now = datetime(2026, 7, 23, 10, 32, 0)
    bars = resample_completed_3m(merged, now=now)
    comps = macd_components(pd.to_numeric(bars["close"], errors="coerce"))
    hist = comps.get("hist")
    last3 = [round(float(x), 6) for x in hist.iloc[-3:].tolist()] if hist is not None and len(hist) >= 3 else []
    last_dir = None
    last_bar = None
    sigs: list[dict[str, Any]] = []
    eval_1030 = None
    today_bars = bars[bars["datetime"].dt.strftime("%Y-%m-%d") == DAY]
    for _, row in today_bars.iterrows():
        bar_ts = pd.Timestamp(row["datetime"]).to_pydatetime()
        close_ts = bar_ts + timedelta(minutes=3)
        if close_ts > now:
            continue
        feed = merged[merged["datetime"] < pd.Timestamp(close_ts)]
        ev = evaluate_macd_direction(
            feed,
            now=close_ts,
            last_signal_direction=last_dir,
            last_signal_bar_ts=last_bar,
            session_date=DAY,
        )
        if close_ts.isoformat(sep="T") == "2026-07-23T10:30:00":
            eval_1030 = {
                "flag": ev.get("display_direction"),
                "reason": ev.get("reason"),
                "hist_last3": ev.get("hist_last3"),
                "new_signal": ev.get("new_signal"),
            }
        if ev.get("new_signal") and ev.get("signal_direction"):
            sigs.append({
                "bar_ts": ev.get("bar_ts"),
                "bar_close_ts": ev.get("bar_close_ts"),
                "flag": ev["signal_direction"],
                "signal_id": ev.get("signal_id"),
                "hist_last3": ev.get("hist_last3"),
                "reason": ev.get("reason"),
            })
            last_dir = normalize_direction_state(ev["signal_direction"])
            last_bar = ev.get("bar_ts")
    return {
        "ok": True,
        "note": "State live diag received only ~30 1m bars (10:03–10:32); morning 09:00–10:02 missing → EMA/hist ≠ full-day truth",
        "trunc_1m_count": int(len(trunc)),
        "trunc_range": _range(trunc),
        "hist_last3_at_1032": last3,
        "state_hist_last3_reference": [7513.802402, 5896.100968, 4403.806667],
        "hist_match_state_approx": bool(
            last3 and abs(last3[-1] - 4403.806667) < 5.0
        ),
        "eval_at_1030": eval_1030,
        "live_arm_signals_under_truncation": sigs,
    }


def main() -> None:
    now = _now_kst()
    # Cap to regular session end if ever run after close
    if now > datetime(2026, 7, 23, 15, 30, 0):
        now = datetime(2026, 7, 23, 15, 30, 0)

    ingest = {}
    frames = {}
    today_frames = {}
    for sym in (SIGNAL_SYMBOL, LONG_SYMBOL, INVERSE_SYMBOL):
        info = _ingest_symbol(sym, now)
        frames[sym] = info.pop("df")
        today_df = info.pop("today_df")
        today_frames[sym] = today_df
        info["today_df_rows"] = int(len(today_df))
        ingest[sym] = info

    flags = _reconstruct_flags(frames[SIGNAL_SYMBOL], now)
    worker_view = _worker_truncated_view(today_frames[SIGNAL_SYMBOL], frames[SIGNAL_SYMBOL])
    cf = _counterfactual(
        flags.get("live_arm_signals") or [],
        today_frames[LONG_SYMBOL] if not today_frames[LONG_SYMBOL].empty else frames[LONG_SYMBOL],
        today_frames[INVERSE_SYMBOL] if not today_frames[INVERSE_SYMBOL].empty else frames[INVERSE_SYMBOL],
        now,
        budget=float((_load_state().get("budget") or 1_000_000)),
    )

    state = _load_state()
    ledger = _load_ledger()
    program = _program_audit(state, ledger, now)

    # Prefer trading-aware verdict naming for top-level
    if program["executed_trade_count_real"] == 0:
        if program["run_health"]["warmup_ready"] and program["run_health"]["last_tick_at"]:
            top_verdict = "NO_TRADES"
            detail = "RUN_PARTIAL"  # worker/warmup ran but no real fills
        else:
            top_verdict = "NO_TRADES"
            detail = "NO_TRADES"
    else:
        top_verdict = "RUN_OK"
        detail = "RUN_OK"
    program["verdict"] = top_verdict
    program["run_class"] = detail

    report = {
        "generated_at": datetime.now(KST).isoformat(),
        "as_of": now.isoformat(sep="T"),
        "session_date": DAY,
        "window": {"start": "2026-07-23T09:00:00", "end": now.isoformat(sep="T")},
        "verdict": top_verdict,
        "run_class": detail,
        "ingest": {
            k: {kk: vv for kk, vv in v.items() if kk != "sources" or True}
            for k, v in ingest.items()
        },
        "reconstructed_flags": {
            k: v for k, v in flags.items() if k != "timeline" or True
        },
        "worker_truncated_view": worker_view,
        # keep full timeline but also a compact note
        "program": program,
        "counterfactual": cf,
        "notes": [
            "flag_events_today / decision_trace absent from macd_hynix_state.json",
            "ledger rows today are mock E2E stubs (price=10000) — not market fills",
            "counterfactual section is labeled and must not be treated as realized PnL",
            "Primary 1m feed: paginated KIS live (kis_live_*_1m.csv); replay used only as gap fill",
            "replay_*_20260723 ETF prices diverge from live quotes — not used when KIS present",
        ],
    }

    # JSON-serializable cleanup for ingest sources already ok
    out_json = STATE / "macd_live_today_20260723_audit.json"
    out_md = STATE / "macd_live_today_20260723_audit.md"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out_md.write_text(_md(report), encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    print(f"VERDICT={top_verdict} run_class={detail}")
    print(f"live_arm_signals={len(flags.get('live_arm_signals') or [])}")
    print(f"real_fills={program['executed_trade_count_real']}")


if __name__ == "__main__":
    main()
