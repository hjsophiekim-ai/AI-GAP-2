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
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SYMBOL,
    evaluate_macd_direction,
    target_symbol_for_direction,
)

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


def _quote_from_broker(broker, symbol: str) -> dict[str, Any]:
    price = None
    change_pct = None
    bid = None
    ask = None
    try:
        if hasattr(broker, "get_current_price"):
            raw = broker.get_current_price(symbol)
            if isinstance(raw, dict):
                price = float(raw.get("current_price") or raw.get("price") or 0)
                change_pct = raw.get("change_pct")
                bid = raw.get("bid") or raw.get("bid_price")
                ask = raw.get("ask") or raw.get("ask_price")
            elif raw is not None:
                price = float(raw)
    except Exception:
        price = None
    # Kis brokers often expose kis client
    if (price is None or price <= 0) and hasattr(broker, "kis"):
        try:
            raw = broker.kis.get_current_price(symbol)
            if isinstance(raw, dict):
                price = float(raw.get("current_price") or raw.get("price") or 0)
                change_pct = raw.get("change_rate") or raw.get("change_pct")
        except Exception:
            pass
    return {
        "price": price,
        "change_pct": change_pct,
        "bid": bid,
        "ask": ask,
        "updated_at": datetime.now().isoformat(),
    }


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
    return quotes


def _next_action_label(state: dict[str, Any]) -> str:
    if state.get("force_liquidate_pending"):
        return "청산 대기"
    direction = state.get("pending_signal_direction") or state.get("display_direction")
    pos = state.get("position") or {}
    held = pos.get("symbol")
    target = target_symbol_for_direction(direction)
    if state.get("order_block_reason"):
        return "주문 보류(ORDER_DATA_INVALID)"
    if direction == DIR_UP and held != LONG_SYMBOL:
        return "KODEX 매수"
    if direction == DIR_DOWN and held != INVERSE_SYMBOL:
        return "SOL 매수"
    if held:
        return "기존 보유 유지"
    return "대기"


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

        # 15:00 force liquidate — highest priority
        if should_force_liquidate(now, state.get("force_liquidate_done_date")) or state.get("force_liquidate_pending"):
            state["force_liquidate_pending"] = True
            liq = om.force_liquidate_all(broker, mode=mode, quotes=quotes, state=state)
            result["actions"].append({"force_liquidate": liq})
            state["next_action"] = "청산 대기" if not liq.get("success") else "청산 완료"
            om.save_state(state)
            return result

        if not in_trading_session(now):
            result["skipped"] = "outside_session"
            om.save_state(state)
            return result

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

        # Arm new signal (order on this or next tick after detection)
        if eval_res.get("new_signal") and eval_res.get("signal_id"):
            sid = eval_res["signal_id"]
            processed = set(state.get("processed_signal_ids") or [])
            if sid not in processed and sid != state.get("pending_signal_id"):
                state["pending_signal_id"] = sid
                state["pending_signal_direction"] = eval_res["signal_direction"]
                state["pending_signal_at"] = now.isoformat()
                state["last_signal_at"] = now.isoformat()
                state["last_signal_bar_ts"] = eval_res.get("bar_ts")
                # Arm direction immediately so the same UP/DOWN streak cannot re-fire.
                state["last_signal_direction"] = eval_res["signal_direction"]
                state["last_signal_id"] = sid
                state.setdefault("worker", {})["signal_detected_at"] = now.isoformat()
                om.set_pipeline_stage(state, "Signal", True, sid)
                result["actions"].append({"signal": sid, "direction": eval_res["signal_direction"]})

        # Execute pending switch on this tick (signal→order on next 5s tick after detect;
        # if pending was set earlier, execute now; if set this tick, leave for next tick)
        pending_id = state.get("pending_signal_id")
        pending_dir = state.get("pending_signal_direction")
        pending_at = state.get("pending_signal_at")
        execute_now = False
        if pending_id and pending_dir:
            try:
                detected = datetime.fromisoformat(str(pending_at)) if pending_at else None
                # Execute only if signal was detected on a prior tick (or age >= ~half interval)
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
                switch_res = om.switch_to_direction(
                    broker,
                    pending_dir,
                    mode=mode,
                    budget=float(state.get("budget") or 10_000_000),
                    quotes=quotes,
                    signal_id=pending_id,
                    state=state,
                )
                result["actions"].append({"switch": switch_res})
                if switch_res.get("success") or switch_res.get("duplicate") or switch_res.get("skipped_same_direction"):
                    state["pending_signal_id"] = None
                    state["pending_signal_direction"] = None
                    state["last_signal_direction"] = pending_dir
                    state["last_signal_id"] = pending_id
                elif switch_res.get("order_data_invalid"):
                    # Hold order; do NOT flip MACD display to HOLD
                    state["order_block_reason"] = switch_res.get("message")

        state["next_action"] = _next_action_label(state)
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

    ok, reason = om.can_start_macd(mode)
    if not ok:
        return {"ok": False, "message": reason}

    state = om.load_state()
    state["auto_trade_on"] = True
    state["mode"] = mode
    state["budget"] = float(budget)
    state["stopped"] = False
    state["stopped_reason"] = None
    state["real_confirm_ok"] = bool(real_confirm_ok) if mode == "real" else False
    state["masked_account"] = masked_account
    om.write_mutex(macd_on=True, mode=mode, reason="macd_started")
    om.save_state(state)
    ensure_worker_running()
    _wake_event.set()
    return {"ok": True, "state": state}


def stop_auto_trade(reason: str = "user_stop") -> dict[str, Any]:
    state = om.load_state()
    state["auto_trade_on"] = False
    state["stopped"] = True
    state["stopped_reason"] = reason
    state["pending_signal_id"] = None
    state["pending_signal_direction"] = None
    om.write_mutex(macd_on=False, mode=str(state.get("mode") or "mock"), reason=reason)
    om.save_state(state)
    return {"ok": True, "state": state}


def request_force_liquidate() -> dict[str, Any]:
    state = om.load_state()
    state["force_liquidate_pending"] = True
    om.save_state(state)
    ensure_worker_running()
    _wake_event.set()
    return {"ok": True}
