"""Isolated 5-second fixed-schedule worker for MACD Hynix auto trading.

Does not call Enhanced / WOC / Early / Active / Fusion / Regime / Prediction.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from app.logger import logger
from app.trading import macd_hynix_order_manager as om
from app.trading.macd_hynix_strategy import (
    CONTINUATION_REENTRY_ENABLED,
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    ENTRY_CONTINUATION,
    ENTRY_INITIAL,
    ENTRY_OPEN_IMMEDIATE,
    ENTRY_OPEN_SCALE,
    EXIT_OPPOSITE,
    EXIT_SL,
    EXIT_TP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    OPEN_IMMEDIATE_BUDGET_FRACTION,
    OPENING_PROBE_ENABLED,
    SIGNAL_SOURCE_CONTINUATION,
    SIGNAL_SOURCE_OPEN_IMMEDIATE,
    SIGNAL_SYMBOL,
    check_tp_sl,
    compute_warmup_macd,
    evaluate_continuation_reentry,
    evaluate_macd_direction,
    evaluate_opening_probe,
    first_regular_3m_bar_closed,
    in_open_probe_window,
    open_probe_window_expired,
    opening_probe_b_confirms,
    snapshot_tp_context,
    tail_prior_day_1m,
    target_symbol_for_direction,
)
from app.trading.macd_hynix_order_manager import SIGNAL_SOURCE

KST = ZoneInfo("Asia/Seoul")
TICK_SECONDS = 5.0
SESSION_START = (9, 0)
NO_NEW_SWITCH_AFTER = (14, 55)
FORCE_LIQUIDATE_AT = (15, 0)

_worker_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_wake_event = threading.Event()
_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "alive": False,
    "last_tick_at": None,
    "tick_intervals": [],
    "started_at": None,
    "last_error": None,
}


def _now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def _hhmm(dt: datetime) -> tuple[int, int]:
    return dt.hour, dt.minute


def in_trading_session(now: Optional[datetime] = None) -> bool:
    now = now or _now_kst()
    hm = _hhmm(now)
    return hm >= SESSION_START and hm < (15, 30)


def allow_new_switch(now: Optional[datetime] = None) -> bool:
    now = now or _now_kst()
    hm = _hhmm(now)
    return hm >= SESSION_START and hm < NO_NEW_SWITCH_AFTER


def should_force_liquidate(now: Optional[datetime] = None, done_date: Optional[str] = None) -> bool:
    now = now or _now_kst()
    hm = _hhmm(now)
    today = now.strftime("%Y-%m-%d")
    if done_date == today:
        return False
    return hm >= FORCE_LIQUIDATE_AT


def _p95(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _avg(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def get_worker_status() -> dict[str, Any]:
    with _status_lock:
        intervals = list(_status.get("tick_intervals") or [])
        return {
            **dict(_status),
            "tick_intervals": intervals[-10:],
            "avg_interval": _avg(intervals[-20:]),
            "p95_interval": _p95(intervals[-20:]),
            "thread_alive": bool(_worker_thread and _worker_thread.is_alive()),
        }


def _load_minute_df(mode: str, count: int = 120) -> pd.DataFrame:
    """Fetch 000660 1m bars via KIS; fall back to local cache CSV."""
    rows: list[dict] = []
    try:
        from app.trading.kis_client import create_kis_client

        client = create_kis_client(mode if mode in ("mock", "real") else "mock")
        if client is not None:
            candles = client.get_minute_candles(SIGNAL_SYMBOL, period_min=1, count=count) or []
            today = _now_kst().strftime("%Y-%m-%d")
            for c in candles:
                hhmmss = str(c.get("time") or "")
                if len(hhmmss) < 6:
                    continue
                ts = datetime.strptime(f"{today}{hhmmss[:6]}", "%Y%m%d%H%M%S")
                rows.append({
                    "datetime": ts,
                    "open": float(c.get("open") or 0),
                    "high": float(c.get("high") or 0),
                    "low": float(c.get("low") or 0),
                    "close": float(c.get("close") or 0),
                    "volume": int(c.get("volume") or 0),
                })
    except Exception as exc:
        logger.warning("[MACDHynix] minute fetch failed: %s", exc)

    if rows:
        df = pd.DataFrame(rows).drop_duplicates("datetime").sort_values("datetime")
        return df.reset_index(drop=True)

    # Cache fallback (read-only)
    from app.utils.data_paths import CACHE_DIR

    cache = CACHE_DIR / "hynix_minute_1m.csv"
    if cache.exists():
        try:
            df = pd.read_csv(cache)
            if "datetime" in df.columns:
                df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
                return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        except Exception as exc:
            logger.warning("[MACDHynix] cache read failed: %s", exc)
    return pd.DataFrame()


def _load_prior_day_minute_df(mode: str, day: str) -> pd.DataFrame:
    """Prior trading day 000660 1m from replay cache (warm-up)."""
    from app.utils.data_paths import CACHE_DIR

    tag = day.replace("-", "")
    path = CACHE_DIR / f"replay_{tag}_hynix_1m.csv"
    if path.exists():
        try:
            df = pd.read_csv(path)
            if "datetime" in df.columns:
                df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
                return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        except Exception as exc:
            logger.warning("[MACDHynix] prior-day cache read failed: %s", exc)
    return pd.DataFrame()


def _prior_session_date(today: datetime) -> str:
    return (today - timedelta(days=1)).strftime("%Y-%m-%d")


def _refresh_opening_warmup(state: dict[str, Any], df_1m: pd.DataFrame, now: datetime, mode: str) -> None:
    """Pre-09:00 warm-up MACD from prior day (≥100 completed 3m bars)."""
    op = state.setdefault("opening_probe", {})
    if op.get("warmup_ready"):
        return
    prior_day = _prior_session_date(now)
    prev_df = _load_prior_day_minute_df(mode, prior_day)
    warmup_1m = tail_prior_day_1m(prev_df)
    warm = compute_warmup_macd(warmup_1m, now=None)
    op["warmup_ready"] = bool(warm.get("ok"))
    op["warmup_reason"] = warm.get("reason")
    op["warmup_hist_last2"] = warm.get("hist_last2") or []
    op["warmup_hist_deltas"] = warm.get("hist_deltas") or []
    state["opening_warmup_macd"] = warm


def _record_hynix_sample(state: dict[str, Any], now: datetime, price: Optional[float]) -> None:
    if price is None or price <= 0:
        return
    op = state.setdefault("opening_probe", {})
    samples = list(op.get("price_samples_5s") or [])
    samples.append([now.isoformat(), float(price)])
    op["price_samples_5s"] = samples[-12:]


def _quote_from_broker(broker, symbol: str, *, retries: int = 2) -> dict[str, Any]:
    """Fetch one symbol quote; on failure return concrete error fields (never silent None-only)."""
    price = None
    change_pct = None
    bid = None
    ask = None
    last_error: Optional[str] = None
    api_fn = "broker.get_current_price"
    response_code: Optional[str] = None
    attempts = 0

    for attempt in range(1, max(1, retries) + 1):
        attempts = attempt
        try:
            if hasattr(broker, "get_current_price"):
                api_fn = f"{type(broker).__name__}.get_current_price"
                raw = broker.get_current_price(symbol)
                if isinstance(raw, dict):
                    response_code = str(
                        raw.get("rt_cd") or raw.get("msg_cd") or raw.get("code") or ""
                    ) or None
                    price = float(raw.get("current_price") or raw.get("price") or 0)
                    change_pct = raw.get("change_pct")
                    bid = raw.get("bid") or raw.get("bid_price")
                    ask = raw.get("ask") or raw.get("ask_price")
                    if raw.get("error") or raw.get("message"):
                        last_error = str(raw.get("error") or raw.get("message"))
                elif raw is not None:
                    price = float(raw)
            if price is not None and price > 0:
                break
            if price is not None and price <= 0:
                last_error = f"non-positive price={price}"
                price = None
        except Exception as exc:
            last_error = str(exc)
            price = None

        if (price is None or price <= 0) and hasattr(broker, "kis"):
            try:
                api_fn = "broker.kis.get_current_price"
                raw = broker.kis.get_current_price(symbol)
                if isinstance(raw, dict):
                    response_code = str(
                        raw.get("rt_cd") or raw.get("msg_cd") or raw.get("code") or ""
                    ) or None
                    price = float(raw.get("current_price") or raw.get("price") or 0)
                    change_pct = raw.get("change_rate") or raw.get("change_pct")
                    if raw.get("msg1") or raw.get("message"):
                        last_error = str(raw.get("msg1") or raw.get("message"))
                if price is not None and price > 0:
                    break
                if price is not None and price <= 0:
                    last_error = f"non-positive price={price}"
                    price = None
            except Exception as exc:
                last_error = str(exc)
                price = None

        if attempt < retries:
            time.sleep(0.15)

    ok = price is not None and price > 0
    result = {
        "price": price if ok else None,
        "change_pct": change_pct,
        "bid": bid,
        "ask": ask,
        "updated_at": datetime.now().isoformat(),
        "ok": ok,
        "api_function": api_fn,
        "symbol": symbol,
        "response_code": response_code,
        "error_message": None if ok else (last_error or "quote unavailable"),
        "retry_count": attempts,
    }
    return result


def _refresh_quotes(broker, state: dict[str, Any]) -> dict[str, Any]:
    hynix = _quote_from_broker(broker, SIGNAL_SYMBOL)
    long_q = _quote_from_broker(broker, LONG_SYMBOL)
    inv_q = _quote_from_broker(broker, INVERSE_SYMBOL)
    quotes = {"hynix": hynix, "long": long_q, "inverse": inv_q}
    state["prices"] = {
        "hynix": hynix.get("price"),
        "long": long_q.get("price"),
        "inverse": inv_q.get("price"),
        "updated_at": datetime.now().isoformat(),
    }
    errors = []
    for key, q in quotes.items():
        if not q.get("ok"):
            errors.append({
                "api_function": q.get("api_function"),
                "symbol": q.get("symbol"),
                "response_code": q.get("response_code"),
                "error_message": q.get("error_message"),
                "retry_count": q.get("retry_count"),
                "slot": key,
            })
    state["quote_errors"] = errors
    if errors:
        # Surface concrete quote failure — do not leave silent Nones only.
        state["order_block_reason"] = (
            state.get("order_block_reason")
            or f"QUOTE_ERROR: {errors[0].get('symbol')} via {errors[0].get('api_function')}: "
               f"{errors[0].get('error_message')} (code={errors[0].get('response_code')}, "
               f"retries={errors[0].get('retry_count')})"
        )
    elif str(state.get("order_block_reason") or "").startswith("QUOTE_ERROR:"):
        state["order_block_reason"] = None
    return quotes



def _next_action_label(state: dict[str, Any]) -> str:
    if state.get("force_liquidate_pending"):
        return "청산 대기"
    op = state.get("opening_probe") or {}
    if op.get("awaiting_09_03_confirm"):
        return "09:03 B 확인 대기"
    if op.get("window_active") and not op.get("immediate_fired_today"):
        return "09:00 개시 프로브"
    direction = state.get("pending_signal_direction") or state.get("display_direction")
    pos = state.get("position") or {}
    held = pos.get("symbol")
    target = target_symbol_for_direction(direction)
    if state.get("order_block_reason"):
        return "주문 보류(ORDER_DATA_INVALID)"
    reentry = state.get("reentry") or {}
    if reentry.get("eligible") and not held:
        return "연속재진입 대기"
    if direction == DIR_UP and held != LONG_SYMBOL:
        return "KODEX 매수"
    if direction == DIR_DOWN and held != INVERSE_SYMBOL:
        return "SOL 매수"
    if held:
        return "기존 보유 유지"
    return "대기"


def _held_etf_price(quotes: dict[str, Any], symbol: Optional[str]) -> Optional[float]:
    if symbol == LONG_SYMBOL:
        px = (quotes.get("long") or {}).get("price")
    elif symbol == INVERSE_SYMBOL:
        px = (quotes.get("inverse") or {}).get("price")
    else:
        return None
    try:
        val = float(px)
        return val if val > 0 else None
    except Exception:
        return None


def run_once(
    *,
    broker=None,
    now: Optional[datetime] = None,
    df_1m: Optional[pd.DataFrame] = None,
    state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Single worker tick. Injectable for tests/replay."""
    now = now or _now_kst()
    state = state if state is not None else om.load_state()
    mode = str(state.get("mode") or "mock")
    result: dict[str, Any] = {"ok": True, "now": now.isoformat(), "actions": []}
    reentry_enabled = bool(
        state.get("continuation_reentry_enabled")
        if state.get("continuation_reentry_enabled") is not None
        else CONTINUATION_REENTRY_ENABLED
    )
    state["continuation_reentry_enabled"] = reentry_enabled
    probe_enabled = bool(
        state.get("opening_probe_enabled")
        if state.get("opening_probe_enabled") is not None
        else OPENING_PROBE_ENABLED
    )
    state["opening_probe_enabled"] = probe_enabled

    if not state.get("auto_trade_on") and not state.get("force_liquidate_pending"):
        result["skipped"] = "auto_trade_off"
        return result

    # Build broker if needed
    own_broker = False
    if broker is None:
        try:
            broker = om.create_macd_broker(
                mode,
                real_confirm_text="",
                real_ready=bool(state.get("real_confirm_ok")),
            )
            own_broker = True
        except Exception as exc:
            state["order_block_reason"] = f"broker create failed: {exc}"
            om.save_state(state)
            result["ok"] = False
            result["error"] = str(exc)
            return result

    try:
        quotes = _refresh_quotes(broker, state)

        # Always compute MACD/direction for UI display (even outside session).
        # Orders remain gated by session / allow_new_switch below.
        if df_1m is None:
            df_1m = _load_minute_df(mode)
        eval_res = evaluate_macd_direction(
            df_1m,
            now=now,
            last_signal_direction=state.get("last_signal_direction"),
            last_signal_bar_ts=state.get("last_signal_bar_ts"),
        )
        state["display_direction"] = eval_res.get("display_direction") or DIR_HOLD
        state["macd"] = {
            "macd": eval_res.get("macd"),
            "signal": eval_res.get("signal"),
            "hist": eval_res.get("hist"),
            "hist_last3": eval_res.get("hist_last3") or [],
            "hist_deltas": eval_res.get("hist_deltas") or [],
            "reason": eval_res.get("reason"),
            "bar_ts": eval_res.get("bar_ts"),
        }
        result["macd"] = eval_res
        om.refresh_runtime_status(state, worker_alive=True)

        # Pre-open warm-up (prior-day MACD) — ready by 08:59; no direction reuse
        if probe_enabled and now.hour < 9:
            _refresh_opening_warmup(state, df_1m, now, mode)

        # 15:00 force liquidate — always highest priority
        if should_force_liquidate(now, state.get("force_liquidate_done_date")) or state.get("force_liquidate_pending"):
            state["force_liquidate_pending"] = True
            liq = om.force_liquidate_all(broker, mode=mode, quotes=quotes, state=state)
            result["actions"].append({"force_liquidate": liq})
            state["next_action"] = "청산 대기" if not liq.get("success") else "청산 완료"
            om.refresh_runtime_status(state, worker_alive=True)
            om.save_state(state)
            return result

        if not in_trading_session(now):
            result["skipped"] = "outside_session"
            state["next_action"] = _next_action_label(state)
            om.refresh_runtime_status(state, worker_alive=True)
            om.save_state(state)
            return result

        op = state.setdefault("opening_probe", {})
        hynix_px = (quotes.get("hynix") or {}).get("price")
        try:
            hynix_px_f = float(hynix_px) if hynix_px is not None else None
        except Exception:
            hynix_px_f = None

        if probe_enabled:
            if now.hour == 9 and now.minute == 0 and hynix_px_f and hynix_px_f > 0:
                if op.get("day_open_price") is None:
                    op["day_open_price"] = hynix_px_f
            if not op.get("warmup_ready"):
                _refresh_opening_warmup(state, df_1m, now, mode)
            if in_open_probe_window(now):
                op["window_active"] = True
                _record_hynix_sample(state, now, hynix_px_f)
            elif open_probe_window_expired(now) and op.get("window_active") and not op.get("immediate_fired_today"):
                op["window_abandoned"] = True
                op["window_active"] = False

        pos = state.get("position") or {}
        held_symbol = pos.get("symbol")
        held_qty = int(pos.get("quantity") or 0)
        entry_px = float(pos.get("avg_price") or 0)

        # Opposite MACD B confirm — priority over TP/SL
        opposite_pending = False
        if eval_res.get("new_signal") and eval_res.get("signal_direction") and held_symbol and held_qty > 0:
            new_dir = eval_res["signal_direction"]
            new_target = target_symbol_for_direction(new_dir)
            if new_target and new_target != held_symbol:
                sid = eval_res["signal_id"]
                processed = set(state.get("processed_signal_ids") or [])
                if sid not in processed:
                    state["pending_signal_id"] = sid
                    state["pending_signal_direction"] = new_dir
                    state["pending_signal_at"] = now.isoformat()
                    state["pending_entry_kind"] = ENTRY_INITIAL
                    state["pending_signal_source"] = SIGNAL_SOURCE
                    state["last_signal_at"] = now.isoformat()
                    state["last_signal_bar_ts"] = eval_res.get("bar_ts")
                    state["last_signal_direction"] = new_dir
                    state["last_signal_id"] = sid
                    om.set_pipeline_stage(state, "Signal", True, sid)
                    result["actions"].append({"opposite_signal": sid, "direction": new_dir})
                    opposite_pending = True
                    if op.get("awaiting_09_03_confirm"):
                        op["awaiting_09_03_confirm"] = False
                        op["confirm_checked"] = True

        # TP/SL every tick while in position (skip when opposite armed this tick)
        if not opposite_pending and held_symbol and held_qty > 0 and entry_px > 0:
            cur_px = _held_etf_price(quotes, held_symbol)
            if cur_px is not None:
                exit_hit = check_tp_sl(held_symbol, entry_px, cur_px, held_qty)
                if exit_hit:
                    tp_ctx = snapshot_tp_context(df_1m, now=now) if exit_hit == EXIT_TP else None
                    exit_res = om.exit_position_full(
                        broker,
                        mode=mode,
                        quotes=quotes,
                        state=state,
                        reason=exit_hit,
                        signal_id=f"{exit_hit}:{pos.get('signal_id') or now.isoformat()}",
                        tp_context=tp_ctx,
                    )
                    result["actions"].append({"exit": exit_res, "reason": exit_hit})
                    state["next_action"] = _next_action_label(state)
                    om.refresh_runtime_status(state, worker_alive=True)
                    om.save_state(state)
                    # After TP/SL, continue tick for re-entry eval / signals (no return)
                    if not exit_res.get("success"):
                        return result

        # Keep last_signal_direction after TP/SL/flat — same-dir re-entry forbidden;
        # a new episode arms only on opposite confirmed B signal.
        ep = state.get("direction_episode") or {}

        pos = state.get("position") or {}
        flat = not pos.get("symbol") or int(pos.get("quantity") or 0) <= 0

        # Continuation re-entry diagnostics (every tick when flat after TP)
        cont = evaluate_continuation_reentry(
            df_1m,
            direction=str(ep.get("direction") or eval_res.get("display_direction") or ""),
            episode=ep,
            now=now,
            enabled=reentry_enabled,
        )
        state["reentry"] = {
            "eligible": bool(cont.get("eligible")),
            "block_reason": cont.get("block_reason"),
            "bars_since_tp": cont.get("bars_since_tp") or 0,
            "hist_contracted": bool(cont.get("hist_contracted")),
            "hist_last3": cont.get("hist_last3") or [],
            "enabled": reentry_enabled,
            "episode_reentry_used": bool(ep.get("continuation_reentry_used")),
        }
        result["reentry"] = state["reentry"]

        # ── Opening probe: 09:00 immediate 50% ─────────────────────────────
        if (
            probe_enabled
            and in_open_probe_window(now)
            and not op.get("immediate_fired_today")
            and flat
            and not state.get("pending_signal_id")
            and (quotes.get("hynix") or {}).get("ok")
        ):
            warm = state.get("opening_warmup_macd") or {}
            day_open = op.get("day_open_price")
            if day_open and hynix_px_f and warm.get("ok"):
                samples = [(s[0], s[1]) for s in (op.get("price_samples_5s") or [])]
                probe = evaluate_opening_probe(
                    warm,
                    hynix_price=hynix_px_f,
                    day_open_price=float(day_open),
                    long_quote=quotes.get("long"),
                    inverse_quote=quotes.get("inverse"),
                    price_samples_5s=samples,
                    now=now,
                )
                op["last_eval_at"] = now.isoformat()
                op["last_eval_reason"] = probe.get("reason")
                op["last_eval_signal"] = probe.get("signal")
                result["opening_probe"] = probe
                if probe.get("ok_to_trade") and probe.get("direction"):
                    sid = f"OPEN_IMM:{probe['signal']}:{now.strftime('%Y%m%d%H%M%S')}"
                    state["pending_signal_id"] = sid
                    state["pending_signal_direction"] = probe["direction"]
                    state["pending_signal_at"] = now.isoformat()
                    state["pending_entry_kind"] = ENTRY_OPEN_IMMEDIATE
                    state["pending_signal_source"] = SIGNAL_SOURCE_OPEN_IMMEDIATE
                    state["pending_budget_fraction"] = OPEN_IMMEDIATE_BUDGET_FRACTION
                    state.setdefault("worker", {})["signal_detected_at"] = now.isoformat()
                    om.set_pipeline_stage(state, "Signal", True, sid)
                    result["actions"].append({"opening_probe": sid, "direction": probe["direction"]})

        # ── 09:03 first completed 3m bar: confirm scale or flatten ───────────
        if (
            probe_enabled
            and op.get("awaiting_09_03_confirm")
            and not op.get("confirm_checked")
            and first_regular_3m_bar_closed(now)
            and held_symbol
            and held_qty > 0
            and pos.get("opening_probe")
            and not state.get("pending_signal_id")
        ):
            probe_dir = op.get("immediate_direction") or state.get("direction_episode", {}).get("direction")
            if probe_dir and opening_probe_b_confirms(eval_res, probe_dir):
                sid = f"OPEN_SCALE:{probe_dir}:{now.strftime('%Y%m%d%H%M%S')}"
                state["pending_signal_id"] = sid
                state["pending_signal_direction"] = probe_dir
                state["pending_signal_at"] = now.isoformat()
                state["pending_entry_kind"] = ENTRY_OPEN_SCALE
                state["pending_signal_source"] = SIGNAL_SOURCE_OPEN_IMMEDIATE
                state["pending_budget_fraction"] = OPEN_IMMEDIATE_BUDGET_FRACTION
                state["pending_open_scale"] = True
                result["actions"].append({"opening_scale": sid, "direction": probe_dir})
            else:
                flat_res = om.flatten_opening_probe_unconfirmed(
                    broker, mode=mode, quotes=quotes, state=state,
                )
                result["actions"].append({"opening_unconfirmed_exit": flat_res})
                op["confirm_checked"] = True
                pos = state.get("position") or {}
                flat = not pos.get("symbol") or int(pos.get("quantity") or 0) <= 0

        # Arm new MACD first-turn signal (Strategy B unchanged)
        if (
            eval_res.get("new_signal")
            and eval_res.get("signal_id")
            and not opposite_pending
            and not (probe_enabled and op.get("awaiting_09_03_confirm") and not op.get("confirm_checked"))
        ):
            sid = eval_res["signal_id"]
            processed = set(state.get("processed_signal_ids") or [])
            if sid not in processed and sid != state.get("pending_signal_id"):
                state["pending_signal_id"] = sid
                state["pending_signal_direction"] = eval_res["signal_direction"]
                state["pending_signal_at"] = now.isoformat()
                state["pending_entry_kind"] = ENTRY_INITIAL
                state["pending_signal_source"] = SIGNAL_SOURCE
                state["last_signal_at"] = now.isoformat()
                state["last_signal_bar_ts"] = eval_res.get("bar_ts")
                # Arm direction immediately so the same UP/DOWN streak cannot re-fire.
                state["last_signal_direction"] = eval_res["signal_direction"]
                state["last_signal_id"] = sid
                state.setdefault("worker", {})["signal_detected_at"] = now.isoformat()
                om.set_pipeline_stage(state, "Signal", True, sid)
                result["actions"].append({"signal": sid, "direction": eval_res["signal_direction"]})

        # Arm continuation re-entry once (no new MACD color-flip required)
        elif (
            flat
            and reentry_enabled
            and cont.get("eligible")
            and cont.get("signal_id")
            and not state.get("pending_signal_id")
        ):
            sid = cont["signal_id"]
            processed = set(state.get("processed_signal_ids") or [])
            if sid not in processed:
                state["pending_signal_id"] = sid
                state["pending_signal_direction"] = ep.get("direction")
                state["pending_signal_at"] = now.isoformat()
                state["pending_entry_kind"] = ENTRY_CONTINUATION
                state["pending_signal_source"] = SIGNAL_SOURCE_CONTINUATION
                state.setdefault("worker", {})["signal_detected_at"] = now.isoformat()
                om.set_pipeline_stage(state, "Signal", True, sid)
                result["actions"].append({
                    "continuation_reentry": sid,
                    "direction": ep.get("direction"),
                })

        # Execute pending switch on this tick
        pending_id = state.get("pending_signal_id")
        pending_dir = state.get("pending_signal_direction")
        pending_at = state.get("pending_signal_at")
        pending_kind = state.get("pending_entry_kind") or ENTRY_INITIAL
        pending_src = state.get("pending_signal_source") or SIGNAL_SOURCE
        pending_frac = float(state.get("pending_budget_fraction") or 1.0)
        pending_scale = bool(state.get("pending_open_scale"))
        execute_now = False
        if pending_id and pending_dir:
            try:
                detected = datetime.fromisoformat(str(pending_at)) if pending_at else None
                if detected is not None and (now - detected).total_seconds() >= (TICK_SECONDS * 0.5):
                    execute_now = True
                elif detected is None:
                    execute_now = True
            except Exception:
                execute_now = True

        if execute_now and pending_id and pending_dir:
            if not allow_new_switch(now):
                state["order_block_reason"] = "NO_NEW_SWITCH_AFTER_14:55"
                result["actions"].append({"blocked": "after_14:55"})
            else:
                # Opposite MACD while holding → sell reason OPPOSITE_SWITCH
                cur_pos = state.get("position") or {}
                cur_held = cur_pos.get("symbol")
                target = target_symbol_for_direction(pending_dir)
                sell_reason = None
                if cur_held and target and cur_held != target and pending_kind in (ENTRY_INITIAL, ENTRY_OPEN_IMMEDIATE):
                    sell_reason = EXIT_OPPOSITE
                if pending_scale and pending_kind == ENTRY_OPEN_SCALE:
                    switch_res = om.scale_opening_probe(
                        broker,
                        pending_dir,
                        mode=mode,
                        budget=float(state.get("budget") or 10_000_000),
                        quotes=quotes,
                        signal_id=pending_id,
                        state=state,
                    )
                else:
                    switch_res = om.switch_to_direction(
                        broker,
                        pending_dir,
                        mode=mode,
                        budget=float(state.get("budget") or 10_000_000),
                        quotes=quotes,
                        signal_id=pending_id,
                        state=state,
                        entry_kind=pending_kind,
                        signal_source=pending_src,
                        sell_reason=sell_reason,
                        budget_fraction=pending_frac,
                    )
                result["actions"].append({"switch": switch_res})
                if switch_res.get("success") or switch_res.get("duplicate") or switch_res.get("skipped_same_direction"):
                    if pending_kind == ENTRY_OPEN_IMMEDIATE:
                        op["immediate_fired_today"] = True
                        op["immediate_direction"] = pending_dir
                        op["immediate_signal_id"] = pending_id
                        op["immediate_at"] = now.isoformat()
                        op["awaiting_09_03_confirm"] = True
                        op["window_active"] = False
                        state["last_signal_direction"] = pending_dir
                        state["last_signal_id"] = pending_id
                    state["pending_signal_id"] = None
                    state["pending_signal_direction"] = None
                    state["pending_entry_kind"] = None
                    state["pending_signal_source"] = None
                    state["pending_budget_fraction"] = None
                    state["pending_open_scale"] = None
                    if pending_kind not in (ENTRY_OPEN_IMMEDIATE,):
                        state["last_signal_direction"] = pending_dir
                        state["last_signal_id"] = pending_id
                elif switch_res.get("order_data_invalid"):
                    state["order_block_reason"] = switch_res.get("message")

        state["next_action"] = _next_action_label(state)
        om.refresh_runtime_status(state, worker_alive=True)
        om.save_state(state)
        return result
    finally:
        if own_broker:
            pass


def _worker_loop() -> None:
    logger.info("[MACDHynix] worker started (fixed %ss schedule)", TICK_SECONDS)
    with _status_lock:
        _status["alive"] = True
        _status["started_at"] = datetime.now().isoformat()
        _status["last_error"] = None

    # Align to wall-clock 5s grid
    next_tick = time.monotonic()
    last_mono = None
    while not _stop_event.is_set():
        now_mono = time.monotonic()
        if now_mono < next_tick:
            _wake_event.wait(timeout=min(0.2, next_tick - now_mono))
            _wake_event.clear()
            continue

        tick_started = time.monotonic()
        if last_mono is not None:
            interval = tick_started - last_mono
            with _status_lock:
                intervals = list(_status.get("tick_intervals") or [])
                intervals.append(interval)
                _status["tick_intervals"] = intervals[-40:]
        last_mono = tick_started

        try:
            state = om.load_state()
            state.setdefault("worker", {})
            state["worker"]["alive"] = True
            state["worker"]["last_tick_at"] = datetime.now().isoformat()
            today = _now_kst().strftime("%Y-%m-%d")
            if state.get("session_date") and state.get("session_date") != today:
                # Day change: clear mutex ownership if strategy is off
                if not state.get("auto_trade_on"):
                    om.clear_mutex(mode=str(state.get("mode") or "mock"), reason="day_change")
                om.reset_opening_probe_daily(state, session_date=today)
                state["last_signal_direction"] = None
                state["last_signal_bar_ts"] = None
            elif not state.get("session_date"):
                state["session_date"] = today
            with _status_lock:
                intervals = list(_status.get("tick_intervals") or [])
                state["worker"]["tick_intervals"] = [round(x, 3) for x in intervals[-10:]]
                state["worker"]["avg_interval"] = _avg(intervals[-20:])
                state["worker"]["p95_interval"] = _p95(intervals[-20:])
                _status["alive"] = True
                _status["last_tick_at"] = state["worker"]["last_tick_at"]
            if state.get("auto_trade_on") or state.get("force_liquidate_pending"):
                run_once(state=state)
            else:
                om.refresh_runtime_status(state, worker_alive=True)
                om.save_state(state)
        except Exception as exc:
            logger.exception("[MACDHynix] tick error: %s", exc)
            with _status_lock:
                _status["last_error"] = str(exc)

        # Fixed schedule: advance by 5s slots (catch up at most one skipped slot)
        next_tick += TICK_SECONDS
        behind = time.monotonic() - next_tick
        if behind > TICK_SECONDS:
            skipped = int(behind // TICK_SECONDS)
            next_tick += skipped * TICK_SECONDS

    with _status_lock:
        _status["alive"] = False
    try:
        state = om.load_state()
        state.setdefault("worker", {})["alive"] = False
        om.save_state(state)
    except Exception:
        pass
    logger.info("[MACDHynix] worker stopped")


def ensure_worker_running() -> dict[str, Any]:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return get_worker_status()
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="macd-hynix-worker", daemon=True)
    _worker_thread.start()
    return get_worker_status()


def stop_worker() -> None:
    _stop_event.set()
    _wake_event.set()


def start_auto_trade(
    *,
    mode: str = "mock",
    budget: float = 10_000_000,
    real_confirm_ok: bool = False,
    masked_account: str = "",
) -> dict[str, Any]:
    mode = "real" if mode == "real" else "mock"
    if mode == "real" and not real_confirm_ok:
        return {"ok": False, "message": "REAL requires confirm phrase"}

    # Force disk re-read of Enhanced auto_trade_on (no stale OR-scan of profile files).
    ok, reason = om.can_start_macd(mode)
    if not ok:
        state = om.load_state()
        state["primary_block_reason"] = reason
        state["legacy_truth_debug"] = om.legacy_auto_trade_truth(force_disk=True)
        om.refresh_runtime_status(state)
        om.save_state(state)
        return {"ok": False, "message": reason, "primary_block_reason": reason}

    state = om.load_state()
    state["auto_trade_on"] = True
    state["mode"] = mode
    state["budget"] = float(budget)
    state["stopped"] = False
    state["stopped_reason"] = None
    state["real_confirm_ok"] = bool(real_confirm_ok) if mode == "real" else False
    state["masked_account"] = masked_account
    state["session_date"] = _now_kst().strftime("%Y-%m-%d")
    state["primary_block_reason"] = None
    state["legacy_truth_debug"] = om.legacy_auto_trade_truth(force_disk=True)
    om.write_mutex(macd_on=True, mode=mode, reason="macd_started")
    om.refresh_runtime_status(state, worker_alive=True)
    om.save_state(state)
    ensure_worker_running()
    # First tick immediately after start: fetch quotes + compute MACD (do not wait 5s).
    try:
        run_once(state=state)
    except Exception as exc:
        logger.warning("[MACDHynix] immediate first tick failed: %s", exc)
        state = om.load_state()
        state["order_block_reason"] = state.get("order_block_reason") or f"first_tick_error: {exc}"
        om.refresh_runtime_status(state, worker_alive=True)
        om.save_state(state)
    _wake_event.set()
    return {"ok": True, "state": om.load_state()}


def stop_auto_trade(reason: str = "user_stop") -> dict[str, Any]:
    state = om.load_state()
    state["auto_trade_on"] = False
    state["stopped"] = True
    state["stopped_reason"] = reason
    state["pending_signal_id"] = None
    state["pending_signal_direction"] = None
    om.clear_mutex(mode=str(state.get("mode") or "mock"), reason=reason)
    om.refresh_runtime_status(state, worker_alive=True)
    om.save_state(state)
    return {"ok": True, "state": state}


def request_force_liquidate() -> dict[str, Any]:
    state = om.load_state()
    state["force_liquidate_pending"] = True
    om.save_state(state)
    ensure_worker_running()
    _wake_event.set()
    return {"ok": True}
