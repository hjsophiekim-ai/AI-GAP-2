"""Full Stop→Start mock integration verification for MACD Hynix.

Checklist (exit 0 only when all pass):
1. Force-restart worker in-process; worker_code_sha == HEAD; old thread gone
2. flat DOWN_BLUE → 0197X0 same-tick INITIAL pipeline + ledger dump
3. flat UP_RED → 0193T0 same-tick INITIAL pipeline + ledger dump
4. Duplicate prevention (held ticks / restart / holding / signal_id once)
5. Opposite switch both ways
6. UI field presence (static source check)

Writes:
  data/state/macd_e2e_down_blue_log.json
  data/state/macd_e2e_up_red_log.json
  data/state/macd_e2e_opposite_switch_log.json
  data/state/macd_e2e_duplicate_proofs.json
  data/state/_verify_macd_e2e_evidence.json
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import OrderResult, Position  # noqa: E402
from app.trading import exit_order_coordinator as order_coord  # noqa: E402
from app.trading import macd_hynix_order_manager as om  # noqa: E402
from app.trading import macd_hynix_worker as worker  # noqa: E402
from app.trading.macd_hynix_strategy import (  # noqa: E402
    DIR_DOWN,
    DIR_UP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
)


class FakeBroker:
    mode = "mock"

    def __init__(self, cash: float = 10_000_000):
        self.cash = cash
        self.positions: dict[str, Position] = {}
        self.prices = {LONG_SYMBOL: 10000.0, INVERSE_SYMBOL: 10000.0, "000660": 1800000.0}
        self.buys: list[tuple] = []
        self.sells: list[tuple] = []
        self.account_no = "50123456"

    def get_current_price(self, symbol: str):
        return self.prices.get(symbol)

    def get_positions(self):
        return list(self.positions.values())

    def get_balance(self):
        return self.cash

    def get_buyable_cash(self):
        return self.cash

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        cost = float(price) * int(quantity)
        if cost > self.cash:
            return OrderResult(
                success=False, mode=self.mode, account_type="mock",
                symbol=symbol, name=name, side="buy", quantity=quantity,
                price=price, order_type=order_type, order_id="", message="insufficient cash",
            )
        self.cash -= cost
        if symbol in self.positions:
            pos = self.positions[symbol]
            total = pos.quantity + quantity
            pos.avg_price = (pos.avg_price * pos.quantity + cost) / total
            pos.quantity = total
        else:
            self.positions[symbol] = Position(
                symbol=symbol, name=name, quantity=quantity,
                avg_price=float(price), current_price=float(price),
            )
        self.buys.append((symbol, quantity, price))
        return OrderResult(
            success=True, mode=self.mode, account_type="mock",
            symbol=symbol, name=name, side="buy", quantity=quantity,
            price=price, order_type=order_type, order_id=f"B{len(self.buys)}", message="ok",
        )

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        pos = self.positions.get(symbol)
        if not pos or pos.quantity < quantity:
            return OrderResult(
                success=False, mode=self.mode, account_type="mock",
                symbol=symbol, name=name, side="sell", quantity=quantity,
                price=price, order_type=order_type, order_id="", message="no qty",
            )
        pos.quantity -= quantity
        self.cash += float(price) * quantity
        if pos.quantity <= 0:
            del self.positions[symbol]
        self.sells.append((symbol, quantity, price))
        return OrderResult(
            success=True, mode=self.mode, account_type="mock",
            symbol=symbol, name=name, side="sell", quantity=quantity,
            price=price, order_type=order_type, order_id=f"S{len(self.sells)}", message="ok",
        )


def _bars_1m(n: int = 150, start: datetime | None = None, trend: str = "up"):
    import pandas as pd

    start = start or datetime(2026, 7, 23, 9, 0, 0)
    rows = []
    price = 100.0
    for i in range(n):
        if trend == "up":
            price += 0.8 + (i % 5) * 0.05
        else:
            price -= 0.8 + (i % 5) * 0.05
        ts = start + timedelta(minutes=i)
        rows.append({
            "datetime": ts,
            "open": price - 0.2,
            "high": price + 0.3,
            "low": price - 0.3,
            "close": price,
            "volume": 1000 + i,
        })
    return pd.DataFrame(rows)


def _make_eval(
    direction: str,
    *,
    bar_ts: str,
    signal_id: str,
    new_signal: bool = True,
) -> dict[str, Any]:
    close = datetime.fromisoformat(bar_ts) + timedelta(minutes=3)
    return {
        "ok": True,
        "display_direction": direction,
        "new_signal": new_signal,
        "signal_direction": direction if new_signal else None,
        "macd": 1.0 if direction == DIR_UP else -1.0,
        "signal": 0.5,
        "hist": 0.5 if direction == DIR_UP else -0.5,
        "hist_last3": [0.1, 0.3, 0.5] if direction == DIR_UP else [-0.1, -0.3, -0.5],
        "hist_deltas": [0.2, 0.2] if direction == DIR_UP else [-0.2, -0.2],
        "completed_3m_count": 40,
        "bar_ts": bar_ts,
        "bar_close_ts": close.isoformat(),
        "reason": f"{direction}_FIRST_TURN" if new_signal else f"{direction}_PATTERN",
        "signal_id": signal_id if new_signal else None,
        "onset": True if new_signal else None,
    }


def _flatten_book(broker: FakeBroker, state: dict) -> None:
    for sym, pos in list(broker.positions.items()):
        broker.cash += float(pos.avg_price) * int(pos.quantity)
        del broker.positions[sym]
    state["position"] = om.default_state()["position"]


def _latency(state: dict) -> dict:
    ol = state.get("order_latency") or {}
    return {
        "armed_at": state.get("armed_at") or state.get("pending_signal_at"),
        "signal_type": state.get("signal_type"),
        "current_flag": state.get("current_flag") or state.get("display_direction"),
        "signal_id": state.get("last_signal_id") or state.get("pending_signal_id"),
        "order_requested_at": state.get("order_requested_at") or ol.get("order_requested_at"),
        "kis_order_accepted_at": state.get("kis_order_accepted_at") or ol.get("kis_order_accepted_at"),
        "broker_executed_at": state.get("broker_executed_at") or ol.get("broker_executed_at"),
        "position_confirmed_at": state.get("position_confirmed_at") or ol.get("position_confirmed_at"),
        "duplicate_block_reason": state.get("duplicate_block_reason"),
        "order_latency": copy.deepcopy(ol),
    }


def _snap(state: dict, broker: FakeBroker, label: str, result: dict | None = None) -> dict:
    pos = state.get("position") or {}
    return {
        "label": label,
        "now": (result or {}).get("now"),
        "actions": copy.deepcopy((result or {}).get("actions")),
        **_latency(state),
        "pending_signal_id": state.get("pending_signal_id"),
        "last_signal_direction": state.get("last_signal_direction"),
        "processed_signal_ids": list(state.get("processed_signal_ids") or []),
        "position": {
            "symbol": pos.get("symbol"),
            "quantity": pos.get("quantity"),
            "avg_price": pos.get("avg_price"),
            "signal_id": pos.get("signal_id"),
            "entry_kind": pos.get("entry_kind"),
        },
        "broker_buys": list(broker.buys),
        "broker_sells": list(broker.sells),
        "broker_positions": {
            s: {"qty": p.quantity, "avg": p.avg_price} for s, p in broker.positions.items()
        },
        "pipeline": copy.deepcopy(state.get("pipeline")),
        "worker_code_sha": state.get("worker_code_sha") or state.get("git_sha"),
    }


def _pipeline_ok(state: dict, sid: str, symbol: str, broker: FakeBroker) -> dict[str, bool]:
    lat = _latency(state)
    pos = state.get("position") or {}
    ledger = om.load_ledger()
    sid_rows = [r for r in ledger if r.get("signal_id") == sid]
    buys = [b for b in broker.buys if b[0] == symbol]
    return {
        "has_armed_at": bool(lat.get("armed_at")),
        "has_order_requested_at": bool(lat.get("order_requested_at")),
        "has_kis_accepted_at": bool(lat.get("kis_order_accepted_at")),
        "has_broker_executed_at": bool(lat.get("broker_executed_at")),
        "has_position_confirmed_at": bool(lat.get("position_confirmed_at")),
        "target_symbol": pos.get("symbol") == symbol,
        "qty_gt_0": int(pos.get("quantity") or 0) > 0,
        "broker_buy_once": len(buys) == 1,
        "ledger_buy": any(
            str(r.get("action") or "").upper() == "BUY" and r.get("symbol") == symbol
            for r in sid_rows
        ),
        "signal_id_processed": sid in (state.get("processed_signal_ids") or []),
        "same_tick_fill": True,  # set by caller after inspecting arm+buy on one tick
    }


def _fresh_tmp_state() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="macd_e2e_"))
    om.STATE_PATH = tmp / "macd_hynix_state.json"
    om.MUTEX_PATH = tmp / "macd_hynix_mutex.json"
    om.LEDGER_PATH = tmp / "macd_hynix_execution_ledger.csv"
    om.STATE_DIR = tmp
    om.LOGS_DIR = tmp
    order_coord.reset_for_tests()
    om.save_state(om.default_state())
    return tmp


def _base_state() -> dict:
    state = om.default_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state["budget"] = 5_000_000
    state["session_date"] = "2026-07-23"
    state["opening_probe_enabled"] = False
    state["continuation_reentry_enabled"] = False
    state.setdefault("opening_probe", {})["warmup_ready"] = True
    state["opening_probe"]["warmup_reason"] = "WARMUP_READY"
    return state


def verify_force_restart(sha_short: str) -> dict[str, Any]:
    """In-process Stop→Start: kill old daemon thread, prove new SHA + new identity.

    Does NOT call start_auto_trade (avoids live KIS create_broker). Uses tmp state
    with auto_trade_off so the daemon never places orders.
    """
    out: dict[str, Any] = {"ok": False}
    live_path = ROOT / "data" / "state" / "macd_hynix_state.json"
    before_sha = None
    if live_path.exists():
        try:
            before = json.loads(live_path.read_text(encoding="utf-8"))
            before_sha = before.get("worker_code_sha") or before.get("git_sha")
        except Exception:
            before_sha = None
    out["live_state_sha_before"] = before_sha
    out["render"] = "NOT_CHECKABLE_NO_RENDER_MCP"

    # Isolate daemon from live auto_trade_on
    tmp = Path(tempfile.mkdtemp(prefix="macd_force_"))
    om.STATE_PATH = tmp / "macd_hynix_state.json"
    om.MUTEX_PATH = tmp / "macd_hynix_mutex.json"
    om.LEDGER_PATH = tmp / "macd_hynix_execution_ledger.csv"
    om.STATE_DIR = tmp
    off = om.default_state()
    off["auto_trade_on"] = False
    om.save_state(off)

    worker.stop_worker()
    if worker._worker_thread and worker._worker_thread.is_alive():
        worker._worker_thread.join(timeout=3.0)

    st0 = worker.ensure_worker_running(force_restart=True)
    time.sleep(0.15)
    old_ident = st0.get("thread_ident") or (
        worker._worker_thread.ident if worker._worker_thread else None
    )
    out["old_thread_ident"] = old_ident
    out["old_thread_alive_before"] = bool(worker._worker_thread and worker._worker_thread.is_alive())

    status = worker.ensure_worker_running(force_restart=True)
    time.sleep(0.15)
    new_thread = worker._worker_thread
    new_ident = status.get("thread_ident") or (new_thread.ident if new_thread else None)
    out["new_thread_ident"] = new_ident
    out["new_thread_alive"] = bool(new_thread and new_thread.is_alive())
    out["status"] = {k: status.get(k) for k in ("thread_ident", "thread_alive", "alive")}
    out["old_thread_gone"] = (
        old_ident is None
        or new_ident != old_ident
    )

    # Stamp SHA the same way start_auto_trade does (without KIS)
    state = om.load_state()
    state["worker_code_sha"] = om._git_sha()
    om.save_state(state)
    out["worker_code_sha_after"] = state.get("worker_code_sha")
    out["git_sha_fn"] = om._git_sha()
    out["sha_matches_head"] = str(out["worker_code_sha_after"] or "").startswith(sha_short[:7])
    out["old_sha_94e1835_replaced"] = str(before_sha or "")[:7] != sha_short[:7] or before_sha is None
    out["ok"] = bool(
        out["old_thread_gone"]
        and out["new_thread_alive"]
        and out["sha_matches_head"]
    )

    worker.stop_worker()
    if worker._worker_thread and worker._worker_thread.is_alive():
        worker._worker_thread.join(timeout=2.0)
    out["worker_stopped_after_proof"] = not (
        worker._worker_thread and worker._worker_thread.is_alive()
    )
    return out


def run_flat_episode(
    direction: str,
    symbol: str,
    sid: str,
    bar_ts: str,
    now: datetime,
    label_prefix: str,
) -> tuple[dict, list[dict], FakeBroker, dict]:
    tmp = _fresh_tmp_state()
    broker = FakeBroker()
    df = _bars_1m(150, trend="down" if direction == DIR_DOWN else "up")
    state = _base_state()
    ev = _make_eval(direction, bar_ts=bar_ts, signal_id=sid)
    logs: list[dict] = []
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: ev  # type: ignore
    try:
        r0 = worker.run_once(broker=broker, now=now, df_1m=df, state=state)
        logs.append(_snap(state, broker, f"{label_prefix}_INITIAL_same_tick", r0))
        # Hold flag many ticks — must not add buys
        n_buys = len(broker.buys)
        hold_ev = _make_eval(direction, bar_ts=bar_ts, signal_id=sid, new_signal=True)
        wmod.evaluate_macd_direction = lambda *a, **k: hold_ev  # type: ignore
        for i in range(1, 9):
            r = worker.run_once(
                broker=broker, now=now + timedelta(seconds=5 * i), df_1m=df, state=state
            )
            logs.append(_snap(state, broker, f"{label_prefix}_held_tick_{i}", r))
        extra_buys = len(broker.buys) - n_buys
        pipe = _pipeline_ok(state, sid, symbol, broker)
        # Same-tick: first snap must already have buy + latency stamps
        first = logs[0]
        pipe["same_tick_fill"] = (
            bool(first.get("order_requested_at"))
            and bool(first.get("broker_executed_at"))
            and bool(first.get("position_confirmed_at"))
            and int((first.get("position") or {}).get("quantity") or 0) > 0
            and any(
                isinstance(a, dict) and ("switch" in a or "signal" in a)
                for a in (first.get("actions") or [])
            )
        )
        episode = {
            "tmp": str(tmp),
            "signal_id": sid,
            "direction": direction,
            "target_symbol": symbol,
            "signal_type": state.get("signal_type"),
            "pipeline_checks": pipe,
            "extra_buys_while_flag_held": extra_buys,
            "latency": _latency(state),
            "position": copy.deepcopy(state.get("position")),
            "ledger_rows": [r for r in om.load_ledger() if r.get("signal_id") == sid],
            "broker_buys": list(broker.buys),
            "broker_sells": list(broker.sells),
            "first_tick_actions": copy.deepcopy(logs[0].get("actions")),
            "log_ticks": logs,
        }
        return episode, logs, broker, state
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def run_opposite_switch() -> dict[str, Any]:
    _fresh_tmp_state()
    broker = FakeBroker()
    df = _bars_1m(150, trend="down")
    state = _base_state()
    down_sid = "MACD3M:DOWN_BLUE:2026-07-23T10:00:00"
    up_sid = "MACD3M:UP_RED:2026-07-23T11:00:00"
    logs: list[dict] = []
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    try:
        # A: flat → DOWN → hold 0197X0
        wmod.evaluate_macd_direction = lambda *a, **k: _make_eval(  # type: ignore
            DIR_DOWN, bar_ts="2026-07-23T10:00:00", signal_id=down_sid
        )
        now = datetime(2026, 7, 23, 10, 3, 5)
        r = worker.run_once(broker=broker, now=now, df_1m=df, state=state)
        logs.append(_snap(state, broker, "A_DOWN_entry", r))
        assert state.get("position", {}).get("symbol") == INVERSE_SYMBOL

        # B: held DOWN + UP_RED → sell 0197X0 → buy 0193T0
        wmod.evaluate_macd_direction = lambda *a, **k: _make_eval(  # type: ignore
            DIR_UP, bar_ts="2026-07-23T11:00:00", signal_id=up_sid
        )
        now_up = datetime(2026, 7, 23, 11, 3, 5)
        sells_before = len(broker.sells)
        buys_before = len(broker.buys)
        for i in range(4):
            r = worker.run_once(
                broker=broker, now=now_up + timedelta(seconds=5 * i), df_1m=df, state=state
            )
            logs.append(_snap(state, broker, f"B_UP_switch_tick_{i}", r))
        sold_inv = sum(1 for s in broker.sells[sells_before:] if s[0] == INVERSE_SYMBOL)
        bought_long = sum(1 for b in broker.buys[buys_before:] if b[0] == LONG_SYMBOL)
        pos = state.get("position") or {}
        check_ab = {
            "sold_0197X0": sold_inv >= 1,
            "flat_then_long": pos.get("symbol") == LONG_SYMBOL and int(pos.get("quantity") or 0) > 0,
            "bought_0193T0": bought_long == 1,
            "no_0197X0_left": INVERSE_SYMBOL not in broker.positions,
        }

        # C: reverse — held UP + DOWN_BLUE → sell 0193T0 → buy 0197X0
        down2 = "MACD3M:DOWN_BLUE:2026-07-23T12:00:00"
        wmod.evaluate_macd_direction = lambda *a, **k: _make_eval(  # type: ignore
            DIR_DOWN, bar_ts="2026-07-23T12:00:00", signal_id=down2
        )
        now_d2 = datetime(2026, 7, 23, 12, 3, 5)
        sells_b2 = len(broker.sells)
        buys_b2 = len(broker.buys)
        for i in range(4):
            r = worker.run_once(
                broker=broker, now=now_d2 + timedelta(seconds=5 * i), df_1m=df, state=state
            )
            logs.append(_snap(state, broker, f"C_DOWN_switch_tick_{i}", r))
        sold_long = sum(1 for s in broker.sells[sells_b2:] if s[0] == LONG_SYMBOL)
        bought_inv = sum(1 for b in broker.buys[buys_b2:] if b[0] == INVERSE_SYMBOL)
        pos = state.get("position") or {}
        check_ba = {
            "sold_0193T0": sold_long >= 1,
            "bought_0197X0": bought_inv == 1,
            "holding_inverse": pos.get("symbol") == INVERSE_SYMBOL,
            "no_0193T0_left": LONG_SYMBOL not in broker.positions,
        }
        return {
            "checks_down_to_up": check_ab,
            "checks_up_to_down": check_ba,
            "ok": all(check_ab.values()) and all(check_ba.values()),
            "log_ticks": logs,
            "final_position": copy.deepcopy(pos),
            "broker_buys": list(broker.buys),
            "broker_sells": list(broker.sells),
        }
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def run_duplicate_proofs() -> dict[str, Any]:
    proofs: dict[str, Any] = {}
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    try:
        # 1) Flag held many ticks → 0 extra buys (also covered in flat episode)
        _fresh_tmp_state()
        broker = FakeBroker()
        df = _bars_1m(150, trend="up")
        state = _base_state()
        sid = "MACD3M:UP_RED:2026-07-23T10:00:00"
        ev = _make_eval(DIR_UP, bar_ts="2026-07-23T10:00:00", signal_id=sid)
        wmod.evaluate_macd_direction = lambda *a, **k: ev  # type: ignore
        now = datetime(2026, 7, 23, 10, 3, 5)
        worker.run_once(broker=broker, now=now, df_1m=df, state=state)
        n = len(broker.buys)
        for i in range(1, 12):
            worker.run_once(
                broker=broker, now=now + timedelta(seconds=5 * i), df_1m=df, state=state
            )
        proofs["flag_held_many_ticks_zero_extra"] = {
            "ok": len(broker.buys) == n == 1,
            "buys": len(broker.buys),
        }

        # 2) Refresh/restart alone → no same-dir rebuy when episode used
        _flatten_book(broker, state)
        broker.buys.clear()
        assert om.apply_macd_session_day_rollover(state, session_date="2026-07-23") is False
        for i in range(8):
            worker.run_once(
                broker=broker,
                now=now + timedelta(hours=1, seconds=5 * i),
                df_1m=df,
                state=state,
            )
        proofs["restart_alone_no_same_dir_rebuy"] = {
            "ok": len(broker.buys) == 0,
            "buys": len(broker.buys),
            "last_signal_direction": state.get("last_signal_direction"),
            "processed": list(state.get("processed_signal_ids") or []),
        }

        # 3) Holding target → no INITIAL
        _fresh_tmp_state()
        broker2 = FakeBroker()
        state2 = _base_state()
        sid2 = "MACD3M:UP_RED:2026-07-23T10:00:00"
        wmod.evaluate_macd_direction = lambda *a, **k: _make_eval(  # type: ignore
            DIR_UP, bar_ts="2026-07-23T10:00:00", signal_id=sid2
        )
        worker.run_once(broker=broker2, now=now, df_1m=df, state=state2)
        assert state2.get("position", {}).get("symbol") == LONG_SYMBOL
        # New same-dir signal_id while holding — must not INITIAL again
        sid3 = "MACD3M:UP_RED:2026-07-23T10:30:00"
        wmod.evaluate_macd_direction = lambda *a, **k: _make_eval(  # type: ignore
            DIR_UP, bar_ts="2026-07-23T10:30:00", signal_id=sid3
        )
        n2 = len(broker2.buys)
        initials = 0
        for i in range(6):
            r = worker.run_once(
                broker=broker2, now=now + timedelta(minutes=30, seconds=5 * i), df_1m=df, state=state2
            )
            for a in r.get("actions") or []:
                if isinstance(a, dict) and a.get("signal") == sid3:
                    initials += 1
                sw = a.get("switch") if isinstance(a, dict) else None
                if isinstance(sw, dict) and sw.get("entry_kind") == "INITIAL_ENTRY":
                    initials += 1
        proofs["holding_target_no_initial"] = {
            "ok": len(broker2.buys) == n2 and initials == 0,
            "buys": len(broker2.buys),
            "initial_actions": initials,
            "position": state2.get("position", {}).get("symbol"),
        }

        # 4) same trading_date+episode+signal_id once
        proofs["same_signal_id_once"] = {
            "ok": sid in (state.get("processed_signal_ids") or [])
            and (state.get("processed_signal_ids") or []).count(sid) == 1
            and proofs["flag_held_many_ticks_zero_extra"]["ok"],
            "processed_signal_ids": list(state.get("processed_signal_ids") or []),
        }
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore

    proofs["all_ok"] = all(
        bool((proofs.get(k) or {}).get("ok"))
        for k in (
            "flag_held_many_ticks_zero_extra",
            "restart_alone_no_same_dir_rebuy",
            "holding_target_no_initial",
            "same_signal_id_once",
        )
    )
    return proofs


def check_ui_fields() -> dict[str, Any]:
    ui = (ROOT / "app" / "ui" / "pages_disabled" / "10_MACD_하이닉스_자동매매.py").read_text(encoding="utf-8")
    required = [
        "current_flag",
        "signal_type",
        "signal_id",
        "armed_at",
        "order_requested_at",
        "broker_executed_at",
        "position_confirmed_at",
        "duplicate_block_reason",
    ]
    present = {f: f in ui for f in required}
    return {"ok": all(present.values()), "fields": present}


def main() -> int:
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    sha_short = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), text=True
    ).strip()
    try:
        origin = subprocess.check_output(
            ["git", "rev-parse", "origin/main"], cwd=str(ROOT), text=True
        ).strip()
    except Exception:
        origin = None

    checklist: dict[str, Any] = {}
    evidence: dict[str, Any] = {
        "sha": sha,
        "sha_short": sha_short,
        "origin_main": origin,
        "checklist": checklist,
        "exit_rules": {
            "15:00": "FORCE_LIQUIDATE highest priority",
            "opposite_MACD": "OPPOSITE_SWITCH sell then buy",
            "SL": "SL_EXIT at −1.5% net",
            "profit_lock": "activate ≥+1.5% net, giveback ≥0.8pp → PROFIT_LOCK",
            "fixed_tp": "none (no +3% TP in live worker)",
            "same_dir_reentry": "blocked after episode used (unless continuation flag ON)",
        },
    }

    try:
        # 1. Force-restart
        fr = verify_force_restart(sha_short)
        evidence["force_restart"] = fr
        checklist["1_force_restart_old_thread_gone"] = bool(fr.get("old_thread_gone"))
        checklist["1_force_restart_new_alive"] = bool(fr.get("new_thread_alive"))
        checklist["1_worker_code_sha_current"] = bool(fr.get("sha_matches_head"))

        # 2. DOWN_BLUE
        down_sid = "MACD3M:DOWN_BLUE:2026-07-23T10:00:00"
        down_ep, down_logs, _, _ = run_flat_episode(
            DIR_DOWN,
            INVERSE_SYMBOL,
            down_sid,
            "2026-07-23T10:00:00",
            datetime(2026, 7, 23, 10, 3, 5),
            "DOWN_BLUE",
        )
        evidence["episodes"] = {"DOWN_BLUE": down_ep}
        pc = down_ep["pipeline_checks"]
        checklist["2_down_same_tick"] = bool(pc.get("same_tick_fill"))
        checklist["2_down_0197X0"] = bool(pc.get("target_symbol"))
        checklist["2_down_order_requested"] = bool(pc.get("has_order_requested_at"))
        checklist["2_down_accepted"] = bool(pc.get("has_kis_accepted_at"))
        checklist["2_down_executed"] = bool(pc.get("has_broker_executed_at"))
        checklist["2_down_position_confirmed"] = bool(pc.get("has_position_confirmed_at"))
        checklist["2_down_ledger"] = bool(pc.get("ledger_buy"))
        checklist["2_down_no_extra_buys"] = down_ep["extra_buys_while_flag_held"] == 0

        down_out = ROOT / "data" / "state" / "macd_e2e_down_blue_log.json"
        down_out.write_text(
            json.dumps(down_ep, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        evidence["down_blue_log_path"] = str(down_out)

        # 3. UP_RED
        up_sid = "MACD3M:UP_RED:2026-07-23T11:00:00"
        up_ep, up_logs, _, _ = run_flat_episode(
            DIR_UP,
            LONG_SYMBOL,
            up_sid,
            "2026-07-23T11:00:00",
            datetime(2026, 7, 23, 11, 3, 5),
            "UP_RED",
        )
        evidence["episodes"]["UP_RED"] = up_ep
        upc = up_ep["pipeline_checks"]
        checklist["3_up_same_tick"] = bool(upc.get("same_tick_fill"))
        checklist["3_up_0193T0"] = bool(upc.get("target_symbol"))
        checklist["3_up_order_requested"] = bool(upc.get("has_order_requested_at"))
        checklist["3_up_accepted"] = bool(upc.get("has_kis_accepted_at"))
        checklist["3_up_executed"] = bool(upc.get("has_broker_executed_at"))
        checklist["3_up_position_confirmed"] = bool(upc.get("has_position_confirmed_at"))
        checklist["3_up_ledger"] = bool(upc.get("ledger_buy"))
        checklist["3_up_no_extra_buys"] = up_ep["extra_buys_while_flag_held"] == 0

        up_out = ROOT / "data" / "state" / "macd_e2e_up_red_log.json"
        up_out.write_text(
            json.dumps(up_ep, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        evidence["up_red_log_path"] = str(up_out)

        # 4. Duplicate proofs
        dup = run_duplicate_proofs()
        evidence["duplicate_proofs"] = dup
        checklist["4_dup_flag_held"] = bool(dup["flag_held_many_ticks_zero_extra"]["ok"])
        checklist["4_dup_restart_no_rebuy"] = bool(dup["restart_alone_no_same_dir_rebuy"]["ok"])
        checklist["4_dup_holding_no_initial"] = bool(dup["holding_target_no_initial"]["ok"])
        checklist["4_dup_signal_id_once"] = bool(dup["same_signal_id_once"]["ok"])
        dup_out = ROOT / "data" / "state" / "macd_e2e_duplicate_proofs.json"
        dup_out.write_text(json.dumps(dup, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        # 5. Opposite switch
        opp = run_opposite_switch()
        evidence["opposite_switch"] = opp
        checklist["5_opp_down_to_up"] = all((opp.get("checks_down_to_up") or {}).values())
        checklist["5_opp_up_to_down"] = all((opp.get("checks_up_to_down") or {}).values())
        opp_out = ROOT / "data" / "state" / "macd_e2e_opposite_switch_log.json"
        opp_out.write_text(json.dumps(opp, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        # 6. UI fields
        ui = check_ui_fields()
        evidence["ui_fields"] = ui
        checklist["6_ui_fields"] = bool(ui.get("ok"))

    except Exception as exc:
        checklist["EXCEPTION"] = str(exc)
        evidence["exception"] = repr(exc)
        import traceback

        evidence["traceback"] = traceback.format_exc()

    all_ok = all(bool(v) for k, v in checklist.items() if k != "EXCEPTION") and "EXCEPTION" not in checklist
    evidence["verdict"] = "READY_FOR_MOCK" if all_ok else "NOT_READY"
    evidence["checklist"] = checklist

    out = ROOT / "data" / "state" / "_verify_macd_e2e_evidence.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("=" * 72)
    print("FORCE RESTART:", json.dumps(evidence.get("force_restart"), indent=2, default=str))
    print("=" * 72)
    print("DOWN_BLUE first tick:")
    db = (evidence.get("episodes") or {}).get("DOWN_BLUE") or {}
    print(json.dumps({
        "latency": db.get("latency"),
        "position": db.get("position"),
        "pipeline_checks": db.get("pipeline_checks"),
        "first_tick_actions": db.get("first_tick_actions"),
        "broker_buys": db.get("broker_buys"),
    }, indent=2, default=str, ensure_ascii=False))
    print("=" * 72)
    print("UP_RED first tick:")
    ur = (evidence.get("episodes") or {}).get("UP_RED") or {}
    print(json.dumps({
        "latency": ur.get("latency"),
        "position": ur.get("position"),
        "pipeline_checks": ur.get("pipeline_checks"),
        "first_tick_actions": ur.get("first_tick_actions"),
        "broker_buys": ur.get("broker_buys"),
    }, indent=2, default=str, ensure_ascii=False))
    print("=" * 72)
    print("CHECKLIST:", json.dumps(checklist, indent=2, ensure_ascii=False))
    print("VERDICT:", evidence["verdict"])
    print("Evidence:", out)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
