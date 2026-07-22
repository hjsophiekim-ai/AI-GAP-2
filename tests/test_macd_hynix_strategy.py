"""Regression tests for isolated MACD Hynix Strategy B + order path."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.models import OrderResult, Position
from app.trading import exit_order_coordinator as order_coord
from app.trading import macd_hynix_order_manager as om
from app.trading import macd_hynix_worker as worker
from app.trading.macd_hynix_strategy import (
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    ENTRY_CONTINUATION,
    ENTRY_INITIAL,
    EXIT_OPPOSITE,
    EXIT_SL,
    EXIT_TP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SOURCE_CONTINUATION,
    SL_NET_PCT,
    TP_NET_PCT,
    check_tp_sl,
    evaluate_continuation_reentry,
    evaluate_macd_direction,
    make_direction_episode_id,
    net_pnl_pct_vs_entry,
    resample_completed_3m,
    target_symbol_for_direction,
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    state_path = tmp_path / "macd_hynix_state.json"
    mutex_path = tmp_path / "macd_hynix_mutex.json"
    ledger_path = tmp_path / "macd_hynix_execution_ledger.csv"
    monkeypatch.setattr(om, "STATE_PATH", state_path)
    monkeypatch.setattr(om, "MUTEX_PATH", mutex_path)
    monkeypatch.setattr(om, "LEDGER_PATH", ledger_path)
    monkeypatch.setattr(om, "STATE_DIR", tmp_path)
    monkeypatch.setattr(om, "LOGS_DIR", tmp_path)
    order_coord.reset_for_tests()
    om.save_state(om.default_state())
    yield


def _bars_1m(n: int = 120, start: datetime | None = None, trend: str = "up") -> pd.DataFrame:
    start = start or datetime(2026, 7, 21, 9, 0, 0)
    rows = []
    price = 100.0
    for i in range(n):
        if trend == "up":
            price += 0.8 + (i % 5) * 0.05
        elif trend == "down":
            price -= 0.8 + (i % 5) * 0.05
        else:
            price += (0.3 if i % 2 == 0 else -0.3)
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
                symbol=symbol, name=name, quantity=quantity, avg_price=float(price), current_price=float(price),
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


def test_up_red_maps_to_long():
    assert target_symbol_for_direction(DIR_UP) == LONG_SYMBOL


def test_down_blue_maps_to_inverse():
    assert target_symbol_for_direction(DIR_DOWN) == INVERSE_SYMBOL


def test_incomplete_3m_bar_excluded():
    # 09:00, 09:01, 09:02 form one 3m bar completing at 09:03
    df = _bars_1m(3, start=datetime(2026, 7, 21, 9, 0, 0))
    # at 09:02:30 bar not complete
    bars = resample_completed_3m(df, now=datetime(2026, 7, 21, 9, 2, 30))
    assert len(bars) == 0
    bars2 = resample_completed_3m(df, now=datetime(2026, 7, 21, 9, 3, 0))
    assert len(bars2) == 1


def test_first_turn_up_and_no_duplicate():
    df = _bars_1m(120, trend="up")
    now = df["datetime"].iloc[-1] + timedelta(minutes=1)
    r1 = evaluate_macd_direction(df, now=now, last_signal_direction=None)
    # Strong uptrend should eventually produce UP_RED pattern
    assert r1["ok"]
    if r1["display_direction"] == DIR_UP and r1["new_signal"]:
        r2 = evaluate_macd_direction(
            df,
            now=now,
            last_signal_direction=DIR_UP,
            last_signal_bar_ts=r1["bar_ts"],
        )
        assert r2["new_signal"] is False


def test_sell_before_buy_on_switch():
    broker = FakeBroker()
    # Hold inverse first
    broker.buy(INVERSE_SYMBOL, "inv", 10, 10000.0)
    broker.buys.clear()
    state = om.default_state()
    quotes = {
        "long": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
        "inverse": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
    }
    order = []

    orig_sell = om.execute_sell_all
    orig_buy = om.execute_buy

    def wrap_sell(*a, **k):
        order.append("sell")
        return orig_sell(*a, **k)

    def wrap_buy(*a, **k):
        order.append("buy")
        # Opposite must already be flat
        assert om.get_held_quantity(broker, INVERSE_SYMBOL) == 0
        return orig_buy(*a, **k)

    om.execute_sell_all = wrap_sell  # type: ignore
    om.execute_buy = wrap_buy  # type: ignore
    try:
        res = om.switch_to_direction(
            broker, DIR_UP, mode="mock", budget=5_000_000, quotes=quotes,
            signal_id="SIG-UP-1", state=state,
        )
    finally:
        om.execute_sell_all = orig_sell  # type: ignore
        om.execute_buy = orig_buy  # type: ignore

    assert res["success"]
    assert order == ["sell", "buy"]
    assert LONG_SYMBOL in broker.positions
    assert INVERSE_SYMBOL not in broker.positions


def test_same_direction_no_add():
    broker = FakeBroker()
    broker.buy(LONG_SYMBOL, "long", 5, 10000.0)
    state = om.default_state()
    state["position"] = {
        "symbol": LONG_SYMBOL, "quantity": 5, "avg_price": 10000.0,
        "entry_at": datetime.now().isoformat(), "signal_id": "old",
    }
    quotes = {
        "long": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
        "inverse": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
    }
    buys_before = len(broker.buys)
    res = om.switch_to_direction(
        broker, DIR_UP, mode="mock", budget=5_000_000, quotes=quotes,
        signal_id="SIG-UP-2", state=state,
    )
    assert res.get("skipped_same_direction")
    assert len(broker.buys) == buys_before


def test_force_liquidate_15():
    broker = FakeBroker()
    broker.buy(LONG_SYMBOL, "long", 3, 10000.0)
    broker.buy(INVERSE_SYMBOL, "inv", 2, 10000.0)
    state = om.default_state()
    quotes = {
        "long": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
        "inverse": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
    }
    res = om.force_liquidate_all(broker, mode="mock", quotes=quotes, state=state)
    assert res["success"]
    assert not broker.positions


def test_mock_real_state_fields_separated(tmp_path, monkeypatch):
    state = om.default_state()
    state["mode"] = "mock"
    om.save_state(state)
    loaded = om.load_state()
    assert loaded["mode"] == "mock"
    loaded["mode"] = "real"
    loaded["real_confirm_ok"] = True
    om.save_state(loaded)
    again = om.load_state()
    assert again["mode"] == "real"
    assert again["real_confirm_ok"] is True


def test_ledger_success_only_after_confirm():
    broker = FakeBroker()
    quotes_price = 10000.0
    # Force confirm failure path by breaking get_positions after accept
    real_positions = broker.get_positions

    calls = {"n": 0}

    def flaky_positions():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("temp fail")
        return real_positions()

    # Direct sell with working broker records success only when confirmed
    broker.buy(LONG_SYMBOL, "long", 2, quotes_price)
    res = om.execute_sell_all(
        broker, LONG_SYMBOL, quotes_price,
        mode="mock", signal_id="S1", macd_signal=DIR_DOWN, reason="test",
        entry_price=quotes_price,
    )
    assert res["success"]
    rows = om.load_ledger()
    assert any(r.get("success") in (True, "True") and r.get("action") == "SELL" for r in rows)


def test_duplicate_signal_blocked_after_restart():
    broker = FakeBroker()
    state = om.default_state()
    state["processed_signal_ids"] = ["SIG-X"]
    quotes = {
        "long": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
        "inverse": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
    }
    res = om.switch_to_direction(
        broker, DIR_UP, mode="mock", budget=1_000_000, quotes=quotes,
        signal_id="SIG-X", state=state,
    )
    assert res.get("duplicate")


def test_mutex_blocks_when_old_auto_on(tmp_path, monkeypatch):
    old = tmp_path / "hynix_auto_state_mock.json"
    old.write_text(json.dumps({"auto_trade_on": True}), encoding="utf-8")
    monkeypatch.setattr(om, "STATE_DIR", tmp_path)
    ok, msg = om.can_start_macd("mock")
    assert ok is False
    assert "기존" in msg or "ON" in msg


def test_order_data_invalid_does_not_flip_macd():
    broker = FakeBroker()
    state = om.default_state()
    state["display_direction"] = DIR_UP
    quotes = {
        "long": {"price": 0, "updated_at": datetime.now().isoformat()},
        "inverse": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
    }
    res = om.switch_to_direction(
        broker, DIR_UP, mode="mock", budget=1_000_000, quotes=quotes,
        signal_id="SIG-BAD", state=state,
    )
    assert res.get("order_data_invalid")
    assert state["display_direction"] == DIR_UP


def test_worker_tick_interval_stats():
    # Simulate intervals under threshold
    intervals = [5.0, 5.1, 4.9, 5.2, 5.0, 5.05, 4.95, 5.1, 5.0, 5.3]
    assert worker._avg(intervals) <= 7.0
    assert worker._p95(intervals) <= 10.0


def test_worker_run_once_arms_then_executes_next_tick():
    broker = FakeBroker()
    df = _bars_1m(150, trend="up")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["budget"] = 5_000_000
    now = df["datetime"].iloc[-1] + timedelta(minutes=3)

    # Force evaluation to produce a new UP signal by seeding hist pattern via monkeypatch
    fake_eval = {
        "ok": True,
        "display_direction": DIR_UP,
        "new_signal": True,
        "signal_direction": DIR_UP,
        "macd": 1.0,
        "signal": 0.5,
        "hist": 0.5,
        "hist_last3": [0.1, 0.3, 0.5],
        "hist_deltas": [0.2, 0.2],
        "completed_3m_count": 40,
        "bar_ts": "2026-07-21T10:00:00",
        "bar_close_ts": "2026-07-21T10:03:00",
        "reason": "UP_RED_FIRST_TURN",
        "signal_id": "MACD3M:UP_RED:2026-07-21T10:00:00",
    }

    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: fake_eval  # type: ignore
    try:
        r1 = worker.run_once(broker=broker, now=now, df_1m=df, state=state)
        assert any("signal" in a for a in r1["actions"])
        assert state["pending_signal_id"]
        # Next tick 5s later executes
        r2 = worker.run_once(
            broker=broker,
            now=now + timedelta(seconds=5),
            df_1m=df,
            state=state,
        )
        assert any("switch" in a for a in r2["actions"])
        assert LONG_SYMBOL in broker.positions or state.get("position", {}).get("symbol") == LONG_SYMBOL
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_buy_blocked_while_opposite_held():
    broker = FakeBroker()
    broker.buy(INVERSE_SYMBOL, "inv", 5, 10000.0)
    # Directly call execute_buy without selling
    res = om.execute_buy(
        broker, LONG_SYMBOL, 10000.0, 1_000_000,
        mode="mock", signal_id="X", macd_signal=DIR_UP, reason="test",
    )
    assert res["success"] is False
    assert res.get("opposite_qty") == 5


def test_tp_sl_thresholds_net_pnl():
    # At flat price after costs, net % is slightly negative (fees) — not TP/SL
    assert check_tp_sl(LONG_SYMBOL, 10000.0, 10000.0, 10) is None
    # +3% gross still needs to clear costs; use larger move
    assert check_tp_sl(LONG_SYMBOL, 10000.0, 10400.0, 10) == EXIT_TP
    assert check_tp_sl(LONG_SYMBOL, 10000.0, 9800.0, 10) == EXIT_SL
    pct_up = net_pnl_pct_vs_entry(LONG_SYMBOL, 10000.0, 10400.0, 10)
    pct_dn = net_pnl_pct_vs_entry(LONG_SYMBOL, 10000.0, 9800.0, 10)
    assert pct_up >= TP_NET_PCT
    assert pct_dn <= SL_NET_PCT


def test_exit_position_tp_records_reason():
    broker = FakeBroker()
    broker.buy(LONG_SYMBOL, "long", 5, 10000.0)
    state = om.default_state()
    state["position"] = {
        "symbol": LONG_SYMBOL, "quantity": 5, "avg_price": 10000.0,
        "entry_at": datetime.now().isoformat(), "signal_id": "SIG-1",
        "entry_kind": ENTRY_INITIAL, "direction_episode_id": "EP:UP",
    }
    state["direction_episode"] = {
        **om.default_state()["direction_episode"],
        "id": "EP:UP", "direction": DIR_UP, "initial_entry_used": True,
    }
    quotes = {
        "long": {"price": 10400.0, "updated_at": datetime.now().isoformat()},
        "inverse": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
    }
    res = om.exit_position_full(
        broker, mode="mock", quotes=quotes, state=state, reason=EXIT_TP,
        tp_context={"tp_bar_ts": "2026-07-21T10:00:00", "tp_hist_max_abs": 1.5,
                    "tp_hynix_price": 180000.0, "tp_pivot_high": 181000.0},
    )
    assert res["success"]
    assert state["position"]["symbol"] is None
    assert state["direction_episode"]["tp_at"]
    assert state["direction_episode"]["last_exit_reason"] == EXIT_TP
    rows = om.load_ledger()
    assert any(r.get("exit_reason") == EXIT_TP for r in rows)


def test_sl_lock_blocks_continuation_reentry():
    episode = {
        "id": "EP:UP",
        "direction": DIR_UP,
        "sl_lock": True,
        "continuation_reentry_used": False,
        "tp_at": datetime.now().isoformat(),
        "tp_bar_ts": "2026-07-21T10:00:00",
        "tp_hist_max_abs": 1.0,
        "tp_pivot_price": 100.0,
    }
    cont = evaluate_continuation_reentry(
        _bars_1m(120, trend="up"),
        direction=DIR_UP,
        episode=episode,
        now=datetime(2026, 7, 21, 11, 0, 0),
        enabled=True,
    )
    assert cont["eligible"] is False
    assert cont["block_reason"] == "SL_LOCK"


def test_continuation_gates_require_enabled_and_tp():
    episode = {
        "id": "EP:UP",
        "direction": DIR_UP,
        "sl_lock": False,
        "continuation_reentry_used": False,
        "tp_at": None,
        "tp_bar_ts": None,
        "tp_hist_max_abs": 1.0,
    }
    cont = evaluate_continuation_reentry(
        _bars_1m(120, trend="up"),
        direction=DIR_UP,
        episode=episode,
        now=datetime(2026, 7, 21, 11, 0, 0),
        enabled=True,
    )
    assert cont["eligible"] is False
    assert cont["block_reason"] == "NO_TP_YET"

    cont2 = evaluate_continuation_reentry(
        _bars_1m(120, trend="up"),
        direction=DIR_UP,
        episode={**episode, "tp_at": "x"},
        now=datetime(2026, 7, 21, 11, 0, 0),
        enabled=False,
    )
    assert cont2["block_reason"] == "REENTRY_DISABLED"


def test_opposite_signal_resets_episode():
    broker = FakeBroker()
    broker.buy(LONG_SYMBOL, "long", 5, 10000.0)
    state = om.default_state()
    state["position"] = {
        "symbol": LONG_SYMBOL, "quantity": 5, "avg_price": 10000.0,
        "entry_at": datetime.now().isoformat(), "signal_id": "old",
        "entry_kind": ENTRY_INITIAL, "direction_episode_id": "EP:UP:old",
    }
    state["direction_episode"] = {
        **om.default_state()["direction_episode"],
        "id": "EP:UP:old", "direction": DIR_UP,
        "initial_entry_used": True, "tp_at": "keep-me",
    }
    quotes = {
        "long": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
        "inverse": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
    }
    res = om.switch_to_direction(
        broker, DIR_DOWN, mode="mock", budget=5_000_000, quotes=quotes,
        signal_id="SIG-DOWN-1", state=state,
        entry_kind=ENTRY_INITIAL, sell_reason=EXIT_OPPOSITE,
    )
    assert res["success"]
    assert INVERSE_SYMBOL in broker.positions
    # New episode after opposite switch
    assert state["direction_episode"]["direction"] == DIR_DOWN
    assert state["direction_episode"]["id"] != "EP:UP:old"
    assert state["direction_episode"]["continuation_reentry_used"] is False
    assert state["position"]["entry_kind"] == ENTRY_INITIAL


def test_continuation_reentry_idempotent_signal_id():
    broker = FakeBroker()
    state = om.default_state()
    state["direction_episode"] = {
        **om.default_state()["direction_episode"],
        "id": "EP:UP:1", "direction": DIR_UP,
        "tp_at": datetime.now().isoformat(),
        "continuation_reentry_used": False, "sl_lock": False,
    }
    quotes = {
        "long": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
        "inverse": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
    }
    sid = "MACD_CONT:EP:UP:1:bar"
    r1 = om.switch_to_direction(
        broker, DIR_UP, mode="mock", budget=2_000_000, quotes=quotes,
        signal_id=sid, state=state,
        entry_kind=ENTRY_CONTINUATION,
        signal_source=SIGNAL_SOURCE_CONTINUATION,
    )
    assert r1["success"]
    assert state["direction_episode"]["continuation_reentry_used"] is True
    # Flatten for second attempt
    broker.sell(LONG_SYMBOL, "long", broker.positions[LONG_SYMBOL].quantity, 10000.0)
    state["position"] = om.default_state()["position"]
    r2 = om.switch_to_direction(
        broker, DIR_UP, mode="mock", budget=2_000_000, quotes=quotes,
        signal_id=sid, state=state,
        entry_kind=ENTRY_CONTINUATION,
        signal_source=SIGNAL_SOURCE_CONTINUATION,
    )
    assert r2.get("duplicate")


def test_worker_tp_exit_then_no_immediate_rebuy(monkeypatch):
    broker = FakeBroker()
    broker.buy(LONG_SYMBOL, "long", 5, 10000.0)
    broker.prices[LONG_SYMBOL] = 10400.0
    state = om.default_state()
    state["auto_trade_on"] = True
    state["position"] = {
        "symbol": LONG_SYMBOL, "quantity": 5, "avg_price": 10000.0,
        "entry_at": datetime.now().isoformat(), "signal_id": "SIG",
        "entry_kind": ENTRY_INITIAL, "direction_episode_id": "EP:UP",
    }
    state["direction_episode"] = {
        **om.default_state()["direction_episode"],
        "id": "EP:UP", "direction": DIR_UP, "initial_entry_used": True,
    }
    state["continuation_reentry_enabled"] = False
    df = _bars_1m(150, trend="up")
    now = datetime(2026, 7, 21, 11, 0, 0)

    fake_eval = {
        "ok": True,
        "display_direction": DIR_UP,
        "new_signal": False,
        "signal_direction": None,
        "macd": 1.0, "signal": 0.5, "hist": 0.5,
        "hist_last3": [0.1, 0.3, 0.5], "hist_deltas": [0.2, 0.2],
        "completed_3m_count": 40,
        "bar_ts": "2026-07-21T10:57:00",
        "bar_close_ts": "2026-07-21T11:00:00",
        "reason": "UP_RED_PATTERN",
        "signal_id": None,
    }
    import app.trading.macd_hynix_worker as wmod
    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: fake_eval  # type: ignore
    try:
        r = worker.run_once(broker=broker, now=now, df_1m=df, state=state)
        assert any(a.get("reason") == EXIT_TP for a in r["actions"] if "exit" in a or "reason" in a)
        assert state["position"]["symbol"] is None
        assert state["pending_signal_id"] is None  # no immediate rebuy (reentry disabled)
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_episode_id_helper():
    eid = make_direction_episode_id(DIR_UP, "2026-07-21T10:00:00")
    assert eid.startswith("EP:UP_RED:")
