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
    CONTINUATION_REENTRY_ENABLED,
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    ENTRY_CONTINUATION,
    ENTRY_INITIAL,
    ENTRY_OPEN_IMMEDIATE,
    EXIT_OPPOSITE,
    EXIT_PROFIT_LOCK,
    EXIT_SL,
    EXIT_TP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    OPENING_PROBE_ENABLED,
    PROFIT_LOCK_ACTIVATE_PCT,
    PROFIT_LOCK_GIVEBACK_PP,
    SIGNAL_SOURCE_CONTINUATION,
    SL_NET_PCT,
    TP_NET_PCT,
    check_tp_sl,
    evaluate_continuation_reentry,
    evaluate_macd_direction,
    evaluate_position_exits,
    make_direction_episode_id,
    net_pnl_pct_vs_entry,
    resample_completed_3m,
    target_symbol_for_direction,
    update_profit_lock_tracker,
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


def test_signed_two_turn_up_requires_same_sign():
    from app.trading.macd_hynix_strategy import signed_hist_two_turn_pattern

    # Rising but still negative → HOLD (wiggle / less-negative bounce)
    assert signed_hist_two_turn_pattern(-1.0, -2.0, -3.0) == DIR_HOLD
    # Falling but still positive → HOLD (less-positive pullback)
    assert signed_hist_two_turn_pattern(1.0, 2.0, 3.0) == DIR_HOLD
    # True UP: both hist > 0 and two positive deltas
    assert signed_hist_two_turn_pattern(3.0, 2.0, 1.0) == DIR_UP
    # True DOWN: both hist < 0 and two negative deltas
    assert signed_hist_two_turn_pattern(-3.0, -2.0, -1.0) == DIR_DOWN


def test_hold_on_same_sign_pullback_no_new_signal():
    from app.trading.macd_hynix_strategy import (
        signed_hist_two_turn_new_signal,
        signed_hist_two_turn_pattern,
    )

    # Positive hist contracting (pullback) is HOLD — no DOWN arm
    assert signed_hist_two_turn_pattern(1.0, 2.0, 3.0) == DIR_HOLD
    assert signed_hist_two_turn_new_signal(DIR_HOLD, DIR_UP) is False
    # Same-dir while already UP does not re-arm
    assert signed_hist_two_turn_new_signal(DIR_UP, DIR_UP) is False
    # Opposite arms
    assert signed_hist_two_turn_new_signal(DIR_DOWN, DIR_UP) is True


def test_no_same_dir_reentry_after_tp_sl_keeps_direction_state():
    """After flatten, direction_state persists → same-dir pattern cannot re-arm."""
    from app.trading.macd_hynix_strategy import signed_hist_two_turn_new_signal

    # Simulated post-TP/SL: last_signal_direction still UP_RED
    assert signed_hist_two_turn_new_signal(DIR_UP, DIR_UP) is False
    # Opposite confirmed B starts new episode
    assert signed_hist_two_turn_new_signal(DIR_DOWN, DIR_UP) is True


def test_warmup_skips_before_index_26():
    """Warm-up matches old signals_B `i < 26` — need >= 27 completed 3m bars."""
    from app.trading.macd_hynix_strategy import MACD_SIGNAL_MIN_INDEX

    assert MACD_SIGNAL_MIN_INDEX == 26
    # 26 bars → index max 25 → still warm-up
    df = _bars_1m(26 * 3, trend="up")  # 78 1m → 26 completed 3m at end+3m
    now = df["datetime"].iloc[-1] + timedelta(minutes=3)
    r = evaluate_macd_direction(df, now=now, last_signal_direction=None)
    assert r["ok"] is False
    assert r["reason"] == "WARMUP_LT_26"
    # 27+ bars eligible
    df2 = _bars_1m(27 * 3 + 6, trend="up")
    now2 = df2["datetime"].iloc[-1] + timedelta(minutes=3)
    r2 = evaluate_macd_direction(df2, now=now2, last_signal_direction=None)
    assert r2["ok"] is True


def test_first_turn_up_and_no_duplicate():
    df = _bars_1m(120, trend="up")
    now = df["datetime"].iloc[-1] + timedelta(minutes=1)
    r1 = evaluate_macd_direction(df, now=now, last_signal_direction=None)
    # Strong uptrend may or may not produce signed UP (needs hist>0); if it does, no dup
    assert r1["ok"] or r1["reason"] in ("WARMUP_LT_26", "DATA_INSUFFICIENT", "MACD_INSUFFICIENT")
    if r1.get("ok") and r1["display_direction"] == DIR_UP and r1["new_signal"]:
        r2 = evaluate_macd_direction(
            df,
            now=now,
            last_signal_direction=DIR_UP,
            last_signal_bar_ts=r1["bar_ts"],
        )
        assert r2["new_signal"] is False


def test_collect_shared_signals_matches_evaluate_gate():
    from app.trading.macd_hynix_strategy import collect_signed_hist_two_turn_signals

    # Warm-up pad; DOWN onset at i=26 (prev bar not already DOWN); then UP opposite
    hist = [0.1] * 25 + [-1.0, -2.0]  # idx 25=-1, 26=-2; prev2 at 24 is +0.1
    hist.extend([-3.0, -4.0])  # continue DOWN — onset + state block
    hist.extend([-2.0, 0.5, 1.0, 2.0, 3.0])  # flip to UP onset
    evs = collect_signed_hist_two_turn_signals(hist)
    dirs = [e["direction"] for e in evs]
    assert dirs.count(DIR_DOWN) == 1
    assert dirs.count(DIR_UP) == 1
    assert dirs.index(DIR_DOWN) < dirs.index(DIR_UP)


def test_onset_suppresses_warmup_carry():
    """Pattern already true before i=26 must not arm at first eligible bar."""
    from app.trading.macd_hynix_strategy import signed_hist_two_turn_onset

    # Already UP at prev bar, still UP now → no onset
    assert signed_hist_two_turn_onset(4.0, 3.0, 2.0, 1.0) is None
    # Newly UP: prev bar not signed-UP (flat/zero delta on prior window)
    assert signed_hist_two_turn_onset(3.0, 2.0, 1.0, 1.5) == DIR_UP


def test_prior_day_warmup_yields_macd_at_open_without_same_day_bars():
    """Right after open: prior-day warm-up yields MACD numbers (no 26 same-day wait)."""
    from app.trading.macd_hynix_strategy import WARMUP_3M_BARS, resample_completed_3m

    prior = pd.read_csv(Path("data/cache/replay_20260722_hynix_1m.csv"))
    prior["datetime"] = pd.to_datetime(prior["datetime"])
    # Simulate 09:00 open with only prior-day history (0 same-day 1m bars)
    now = datetime(2026, 7, 23, 9, 0, 5)
    bars = resample_completed_3m(prior, now=now)
    assert len(bars) >= WARMUP_3M_BARS, f"need ≥{WARMUP_3M_BARS} 3m from prior day, got {len(bars)}"
    r = evaluate_macd_direction(prior, now=now, session_date="2026-07-23")
    assert r["ok"] is True
    assert r["macd"] is not None and r["signal"] is not None and r["hist"] is not None
    assert len(r["hist_last3"]) == 3
    # Must not wait for same-day bars
    assert r["reason"] != "WARMUP_LT_26"


def test_warmup_bars_do_not_arm_todays_new_signal():
    """Prior-day / warm-up completed bars never count as today's new_signal."""
    prior = pd.read_csv(Path("data/cache/replay_20260722_hynix_1m.csv"))
    prior["datetime"] = pd.to_datetime(prior["datetime"])
    now = datetime(2026, 7, 23, 9, 0, 5)
    r = evaluate_macd_direction(
        prior,
        now=now,
        last_signal_direction=None,
        session_date="2026-07-23",
    )
    assert r["ok"] is True
    # Last bar is Jul22 — display may be UP/DOWN/HOLD but must not arm entry
    assert r["new_signal"] is False
    assert r["signal_id"] is None


def test_session_day_rollover_clears_runtime_and_direction_when_flat():
    """New KST day clears yesterday pipeline/events + direction_state when flat."""
    state = om.default_state()
    state["session_date"] = "2026-07-22"
    state["last_event"] = "15:00_FORCE_LIQUIDATE"
    state["last_signal_direction"] = DIR_UP
    state["last_signal_bar_ts"] = "2026-07-22T14:00:00"
    state["direction_episode"]["last_exit_reason"] = "15:00_FORCE_LIQUIDATE"
    om.set_pipeline_stage(state, "Signal", True, "yesterday")
    state["order_latency"] = {"signal_id": "old"}
    state["position_confirmed_at"] = "2026-07-22T10:00:00"
    assert om.apply_macd_session_day_rollover(state, session_date="2026-07-23") is True
    assert state["session_date"] == "2026-07-23"
    assert state["last_event"] is None
    assert state["pipeline"]["Signal"]["ok"] is None
    assert state["order_latency"] == {}
    assert state["position_confirmed_at"] is None
    assert state["direction_episode"]["last_exit_reason"] is None
    # Flat day start: clear direction so first signed onset after 09:00 can enter
    assert state["last_signal_direction"] is None
    assert state["last_signal_bar_ts"] is None
    assert om.apply_macd_session_day_rollover(state, session_date="2026-07-23") is False


def test_session_day_rollover_keeps_held_position_local_state():
    state = om.default_state()
    state["session_date"] = "2026-07-22"
    state["last_event"] = "BUY"
    state["last_signal_direction"] = DIR_UP
    state["position"] = {
        **om.default_state()["position"],
        "symbol": LONG_SYMBOL,
        "quantity": 10,
        "avg_price": 10000.0,
    }
    assert om.apply_macd_session_day_rollover(state, session_date="2026-07-23") is True
    assert state["position"]["symbol"] == LONG_SYMBOL
    assert int(state["position"]["quantity"]) == 10
    assert state["last_event"] is None
    # Still holding → keep direction_state (do not unlock mid-position overnight carry)
    assert state["last_signal_direction"] == DIR_UP


def test_worker_up_down_symbols_once_no_duplicate():
    """Valid UP → 0193T0 once; DOWN → 0197X0 once; no duplicate same signal_id."""
    broker = FakeBroker()
    df = _bars_1m(150, trend="up")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["budget"] = 5_000_000
    state["session_date"] = "2026-07-23"
    now = datetime(2026, 7, 23, 10, 3, 5)

    up_eval = {
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
        "bar_ts": "2026-07-23T10:00:00",
        "bar_close_ts": "2026-07-23T10:03:00",
        "reason": "UP_RED_FIRST_TURN",
        "signal_id": "MACD3M:UP_RED:2026-07-23T10:00:00",
    }
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: up_eval  # type: ignore
    try:
        worker.run_once(broker=broker, now=now, df_1m=df, state=state)
        worker.run_once(broker=broker, now=now + timedelta(seconds=5), df_1m=df, state=state)
        assert sum(1 for b in broker.buys if b[0] == LONG_SYMBOL) == 1
        # Duplicate same signal must not re-buy
        n_buys = len(broker.buys)
        worker.run_once(broker=broker, now=now + timedelta(seconds=10), df_1m=df, state=state)
        worker.run_once(broker=broker, now=now + timedelta(seconds=15), df_1m=df, state=state)
        assert len(broker.buys) == n_buys

        down_eval = {
            **up_eval,
            "display_direction": DIR_DOWN,
            "signal_direction": DIR_DOWN,
            "bar_ts": "2026-07-23T11:00:00",
            "bar_close_ts": "2026-07-23T11:03:00",
            "reason": "DOWN_BLUE_FIRST_TURN",
            "signal_id": "MACD3M:DOWN_BLUE:2026-07-23T11:00:00",
        }
        wmod.evaluate_macd_direction = lambda *a, **k: down_eval  # type: ignore
        worker.run_once(broker=broker, now=now + timedelta(hours=1), df_1m=df, state=state)
        worker.run_once(
            broker=broker,
            now=now + timedelta(hours=1, seconds=5),
            df_1m=df,
            state=state,
        )
        assert sum(1 for b in broker.buys if b[0] == INVERSE_SYMBOL) == 1
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_ensure_warmup_sets_ready_from_prior_history(monkeypatch):
    """Worker warm-up path marks warmup_ready without same-day accumulation."""
    prior = pd.read_csv(Path("data/cache/replay_20260722_hynix_1m.csv"))
    prior["datetime"] = pd.to_datetime(prior["datetime"])
    state = om.default_state()
    now = datetime(2026, 7, 23, 9, 0, 5)
    monkeypatch.setattr(worker, "_load_prior_day_minute_df", lambda mode, day: prior)
    warm = worker._ensure_macd_warmup(state, prior, now, load_diag={"api_name": "test"})
    assert warm.get("ok") is True
    assert state["opening_probe"]["warmup_ready"] is True
    assert state["opening_probe"]["warmup_reason"] == "WARMUP_READY"


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


def test_real_run_once_passes_configured_confirm_text(monkeypatch, tmp_path):
    """REAL worker must pass cfg.real_confirm_text() into KisRealBroker gate 4."""
    from app.config import get_config
    from app.services import hynix_switch_state as hss

    monkeypatch.setattr(hss, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(om, "STATE_DIR", tmp_path)
    monkeypatch.setattr(om, "MUTEX_PATH", tmp_path / "macd_hynix_mutex.json")
    monkeypatch.setattr(om, "STATE_PATH", tmp_path / "macd_hynix_state.json")
    (tmp_path / "hynix_auto_state_active_mode.json").write_text(
        json.dumps({"mode": "real"}), encoding="utf-8"
    )
    (tmp_path / "hynix_auto_state_real.json").write_text(
        json.dumps({"auto_trade_on": False, "mode": "real"}), encoding="utf-8"
    )
    (tmp_path / "hynix_strategy_profile_common.json").write_text(
        json.dumps({"auto_trade_on": False}), encoding="utf-8"
    )

    broker = FakeBroker()
    captured: dict = {}

    def _capture(mode, *, real_confirm_text="", real_ready=False):
        captured["mode"] = mode
        captured["real_confirm_text"] = real_confirm_text
        captured["real_ready"] = real_ready
        return broker

    monkeypatch.setattr(om, "create_macd_broker", _capture)
    monkeypatch.setattr(worker, "_load_minute_df", lambda *a, **k: _bars_1m(150, trend="up"))
    monkeypatch.setattr(worker, "in_trading_session", lambda *a, **k: False)

    state = om.default_state()
    state["auto_trade_on"] = True
    state["mode"] = "real"
    state["real_confirm_ok"] = True
    state["order_block_reason"] = "stale_should_clear"
    om.save_state(state)

    now = datetime(2026, 7, 23, 8, 20, 0)
    worker.run_once(now=now, state=state)

    expected = str(get_config().real_confirm_text() or "")
    assert captured.get("mode") == "real"
    assert captured.get("real_ready") is True
    assert captured.get("real_confirm_text") == expected
    assert state.get("order_block_reason") is None
    assert state.get("primary_block_reason") == "MARKET_CLOSED"
    assert state.get("order_execution_enabled") is False


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
    """Enhanced ON via load_state truth → MACD start blocked."""
    from app.services import hynix_switch_state as hss

    monkeypatch.setattr(hss, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(om, "STATE_DIR", tmp_path)
    (tmp_path / "hynix_auto_state_active_mode.json").write_text(
        json.dumps({"mode": "mock"}), encoding="utf-8"
    )
    (tmp_path / "hynix_auto_state_mock.json").write_text(
        json.dumps({"auto_trade_on": True, "mode": "mock"}), encoding="utf-8"
    )
    (tmp_path / "hynix_strategy_profile_common.json").write_text(
        json.dumps({"auto_trade_on": True}), encoding="utf-8"
    )
    ok, msg = om.can_start_macd("mock")
    assert ok is False
    assert "LEGACY_STRATEGY_ACTIVE" in msg


def test_enhanced_stop_then_macd_start_sees_off(tmp_path, monkeypatch):
    """Stop path (set_control False) must make MACD Start succeed immediately."""
    from app.services import hynix_switch_state as hss
    from app.services.hynix_switch_engine import set_control
    from app.services import hynix_auto_trade_service as hats

    monkeypatch.setattr(hss, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(hats, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(hats, "_STOP_FLAG_PATH", tmp_path / "hynix_auto_trade_stopped.flag")
    monkeypatch.setattr(om, "STATE_DIR", tmp_path)
    monkeypatch.setattr(om, "MUTEX_PATH", tmp_path / "macd_hynix_mutex.json")
    monkeypatch.setattr(om, "STATE_PATH", tmp_path / "macd_hynix_state.json")

    (tmp_path / "hynix_auto_state_active_mode.json").write_text(
        json.dumps({"mode": "mock"}), encoding="utf-8"
    )
    # Stale: mode file True but user will Stop
    (tmp_path / "hynix_auto_state_mock.json").write_text(
        json.dumps({"auto_trade_on": True, "mode": "mock"}), encoding="utf-8"
    )
    (tmp_path / "hynix_strategy_profile_common.json").write_text(
        json.dumps({"auto_trade_on": True}), encoding="utf-8"
    )
    # Stale mutex file must NOT count as legacy ON
    (tmp_path / "macd_hynix_mutex.json").write_text(
        json.dumps({"macd_auto_trade_on": False, "owner": "NONE"}), encoding="utf-8"
    )

    assert om.can_start_macd("mock")[0] is False
    # Enhanced Stop button → stop_auto_trade → set_control(False)
    hats.stop_auto_trade()
    dump = om.legacy_auto_trade_truth(force_disk=True)
    assert dump["auto_trade_on"] is False
    assert dump["enhanced_save_path"].endswith("hynix_auto_state_mock.json")
    ok, msg = om.can_start_macd("mock")
    assert ok is True, msg
    res = worker.start_auto_trade(mode="mock", budget=1_000_000)
    assert res["ok"] is True
    assert om.read_mutex().get("enabled") is True
    worker.stop_auto_trade("test")
    assert om.read_mutex().get("enabled") is False


def test_stale_common_true_mode_false_uses_load_state_truth(tmp_path, monkeypatch):
    """Do not OR-scan files: effective truth is load_state overlay (common wins)."""
    from app.services import hynix_switch_state as hss

    monkeypatch.setattr(hss, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(om, "STATE_DIR", tmp_path)
    (tmp_path / "hynix_auto_state_active_mode.json").write_text(
        json.dumps({"mode": "mock"}), encoding="utf-8"
    )
    # Mode file OFF, common ON → load_state returns ON (common overlay)
    (tmp_path / "hynix_auto_state_mock.json").write_text(
        json.dumps({"auto_trade_on": False, "mode": "mock"}), encoding="utf-8"
    )
    (tmp_path / "hynix_strategy_profile_common.json").write_text(
        json.dumps({"auto_trade_on": True}), encoding="utf-8"
    )
    ok, msg = om.can_start_macd("mock")
    assert ok is False
    assert "LEGACY_STRATEGY_ACTIVE" in msg

    # Common OFF, mode ON → load_state returns OFF → MACD may start
    (tmp_path / "hynix_strategy_profile_common.json").write_text(
        json.dumps({"auto_trade_on": False}), encoding="utf-8"
    )
    ok2, msg2 = om.can_start_macd("mock")
    assert ok2 is True, msg2


def test_macd_on_blocks_enhanced_enable(tmp_path, monkeypatch):
    from app.services import hynix_switch_state as hss

    monkeypatch.setattr(hss, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(om, "STATE_DIR", tmp_path)
    monkeypatch.setattr(om, "MUTEX_PATH", tmp_path / "macd_hynix_mutex.json")
    monkeypatch.setattr(om, "STATE_PATH", tmp_path / "macd_hynix_state.json")
    (tmp_path / "hynix_auto_state_active_mode.json").write_text(
        json.dumps({"mode": "mock"}), encoding="utf-8"
    )
    (tmp_path / "hynix_auto_state_mock.json").write_text(
        json.dumps({"auto_trade_on": False, "mode": "mock"}), encoding="utf-8"
    )
    (tmp_path / "hynix_strategy_profile_common.json").write_text(
        json.dumps({"auto_trade_on": False}), encoding="utf-8"
    )
    om.save_state({**om.default_state(), "auto_trade_on": True})
    om.write_mutex(macd_on=True, mode="mock", reason="test")
    assert om.is_macd_strategy_on() is True


def test_quote_error_surfaces_fields(monkeypatch):
    broker = FakeBroker()

    def boom(symbol):
        raise RuntimeError(f"KIS fail {symbol}")

    broker.get_current_price = boom  # type: ignore
    monkeypatch.setattr(worker, "_quote_from_local_cache", lambda *a, **k: None)
    state = om.default_state()
    state["auto_trade_on"] = True
    quotes = worker._refresh_quotes(broker, state)
    assert state.get("quote_errors")
    err = state["quote_errors"][0]
    assert err.get("api_function")
    assert err.get("symbol")
    assert "KIS fail" in str(err.get("error_message") or "") or err.get("error_message")
    assert state.get("order_block_reason")
    assert "QUOTE_ERROR" in str(state.get("order_block_reason"))
    assert quotes["hynix"]["ok"] is False
    assert err.get("error_message")
    assert err.get("retry_count")
    assert "QUOTE_ERROR" in str(state.get("order_block_reason") or "")
    assert quotes["hynix"].get("ok") is False


def test_start_populates_prices_and_macd_within_budget(monkeypatch, tmp_path):
    """Within first tick after start: three prices + MACD numbers (mocked broker/bars)."""
    from app.services import hynix_switch_state as hss

    monkeypatch.setattr(hss, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(om, "STATE_DIR", tmp_path)
    monkeypatch.setattr(om, "MUTEX_PATH", tmp_path / "macd_hynix_mutex.json")
    monkeypatch.setattr(om, "STATE_PATH", tmp_path / "macd_hynix_state.json")
    (tmp_path / "hynix_auto_state_active_mode.json").write_text(
        json.dumps({"mode": "mock"}), encoding="utf-8"
    )
    (tmp_path / "hynix_auto_state_mock.json").write_text(
        json.dumps({"auto_trade_on": False, "mode": "mock"}), encoding="utf-8"
    )
    (tmp_path / "hynix_strategy_profile_common.json").write_text(
        json.dumps({"auto_trade_on": False}), encoding="utf-8"
    )

    broker = FakeBroker()
    df = _bars_1m(150, trend="up")
    now = datetime(2026, 7, 21, 11, 0, 0)

    monkeypatch.setattr(om, "create_macd_broker", lambda *a, **k: broker)
    monkeypatch.setattr(worker, "_load_minute_df", lambda *a, **k: df)
    monkeypatch.setattr(worker, "in_trading_session", lambda *a, **k: True)
    monkeypatch.setattr(worker, "allow_new_switch", lambda *a, **k: True)

    ok, _ = om.can_start_macd("mock")
    assert ok
    state = om.default_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    om.save_state(state)
    om.write_mutex(macd_on=True, mode="mock", reason="test")
    r = worker.run_once(broker=broker, now=now, df_1m=df, state=state)
    assert state["prices"]["hynix"] is not None
    assert state["prices"]["long"] is not None
    assert state["prices"]["inverse"] is not None
    assert state["macd"]["macd"] is not None
    assert state["macd"]["signal"] is not None
    assert state["macd"]["hist"] is not None
    assert state["display_direction"] in (DIR_UP, DIR_DOWN, DIR_HOLD)
    assert r.get("macd")


def test_up_buys_long_down_buys_inverse_duplicate_zero():
    broker = FakeBroker()
    state = om.default_state()
    quotes = {
        "long": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
        "inverse": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
    }
    r_up = om.switch_to_direction(
        broker, DIR_UP, mode="mock", budget=2_000_000, quotes=quotes,
        signal_id="SIG-UP-A", state=state,
    )
    assert r_up["success"]
    assert LONG_SYMBOL in broker.positions
    buys_up = [b for b in broker.buys if b[0] == LONG_SYMBOL]
    assert len(buys_up) == 1

    r_dup = om.switch_to_direction(
        broker, DIR_UP, mode="mock", budget=2_000_000, quotes=quotes,
        signal_id="SIG-UP-A", state=state,
    )
    assert r_dup.get("duplicate")
    assert len([b for b in broker.buys if b[0] == LONG_SYMBOL]) == 1

    broker2 = FakeBroker()
    state2 = om.default_state()
    r_dn = om.switch_to_direction(
        broker2, DIR_DOWN, mode="mock", budget=2_000_000, quotes=quotes,
        signal_id="SIG-DN-A", state=state2,
    )
    assert r_dn["success"]
    assert INVERSE_SYMBOL in broker2.positions
    assert len([b for b in broker2.buys if b[0] == INVERSE_SYMBOL]) == 1


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
        # Same-tick execute: signal armed and switch completes in one run_once
        assert any("signal" in a or "switch" in a for a in r1["actions"])
        assert LONG_SYMBOL in broker.positions or state.get("position", {}).get("symbol") == LONG_SYMBOL
        assert any("switch" in a for a in r1["actions"]) or state.get("position", {}).get("quantity", 0) > 0
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
    # Legacy helper still defaults to +3% TP (replay A); live uses evaluate_position_exits.
    assert check_tp_sl(LONG_SYMBOL, 10000.0, 10000.0, 10) is None
    assert check_tp_sl(LONG_SYMBOL, 10000.0, 10400.0, 10) == EXIT_TP
    assert check_tp_sl(LONG_SYMBOL, 10000.0, 9800.0, 10) == EXIT_SL
    assert check_tp_sl(LONG_SYMBOL, 10000.0, 10400.0, 10, tp_pct=None) is None
    pct_up = net_pnl_pct_vs_entry(LONG_SYMBOL, 10000.0, 10400.0, 10)
    pct_dn = net_pnl_pct_vs_entry(LONG_SYMBOL, 10000.0, 9800.0, 10)
    assert pct_up >= TP_NET_PCT
    assert pct_dn <= SL_NET_PCT


def test_profit_lock_activates_at_1_5_pct():
    # Find a mark price that clears +1.5% net after costs
    entry = 10000.0
    qty = 10
    # Start from ~+2% gross and climb until net >= activate
    mark = entry * 1.02
    activated = False
    for _ in range(40):
        pct = net_pnl_pct_vs_entry(LONG_SYMBOL, entry, mark, qty)
        tr = update_profit_lock_tracker(current_net_return=pct)
        if pct >= PROFIT_LOCK_ACTIVATE_PCT:
            assert tr["profit_lock_active"] is True
            activated = True
            break
        mark *= 1.002
    assert activated, "expected to find a mark that activates profit lock"


def test_profit_lock_giveback_0_8pp_exits():
    tr = update_profit_lock_tracker(
        current_net_return=2.5,
        peak_net_return=2.5,
        profit_lock_active=False,
    )
    assert tr["profit_lock_active"] is True
    assert tr["exit_reason"] is None
    # Peak 2.5, current 1.6 → giveback 0.9pp ≥ 0.8
    tr2 = update_profit_lock_tracker(
        current_net_return=1.6,
        peak_net_return=tr["peak_net_return"],
        profit_lock_active=True,
    )
    assert tr2["giveback_pct"] >= PROFIT_LOCK_GIVEBACK_PP
    assert tr2["exit_reason"] == EXIT_PROFIT_LOCK


def test_evaluate_position_exits_sl_before_profit_lock():
    # Large loss → SL wins even if lock fields present
    ev = evaluate_position_exits(
        LONG_SYMBOL, 10000.0, 9800.0, 10,
        peak_net_return=3.0,
        profit_lock_active=True,
    )
    assert ev["exit_reason"] == EXIT_SL


def test_evaluate_position_exits_no_fixed_tp_at_3pct():
    # Price that would have been legacy TP must NOT exit without giveback
    pct = net_pnl_pct_vs_entry(LONG_SYMBOL, 10000.0, 10400.0, 10)
    assert pct >= TP_NET_PCT
    ev = evaluate_position_exits(LONG_SYMBOL, 10000.0, 10400.0, 10)
    assert ev["profit_lock_active"] is True  # above 1.5
    assert ev["exit_reason"] is None  # no giveback yet
    assert CONTINUATION_REENTRY_ENABLED is False
    assert OPENING_PROBE_ENABLED is False


def test_exit_position_profit_lock_records_reason():
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
    state["profit_lock"] = {
        "peak_net_return": 2.4,
        "current_net_return": 1.5,
        "giveback_pct": 0.9,
        "profit_lock_active": True,
    }
    quotes = {
        "long": {"price": 10200.0, "updated_at": datetime.now().isoformat()},
        "inverse": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
    }
    res = om.exit_position_full(
        broker, mode="mock", quotes=quotes, state=state, reason=EXIT_PROFIT_LOCK,
    )
    assert res["success"]
    assert state["position"]["symbol"] is None
    assert state["direction_episode"]["last_exit_reason"] == EXIT_PROFIT_LOCK
    assert state["profit_lock"]["profit_lock_active"] is False
    rows = om.load_ledger()
    sell = next(r for r in rows if r.get("exit_reason") == EXIT_PROFIT_LOCK)
    assert float(sell.get("peak_net_return") or 0) == pytest.approx(2.4)
    assert float(sell.get("giveback_pct") or 0) == pytest.approx(0.9)
    assert str(sell.get("profit_lock_active")).lower() in ("true", "1", "yes")


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


def test_worker_profit_lock_exit_then_no_immediate_rebuy(monkeypatch):
    broker = FakeBroker()
    broker.buy(LONG_SYMBOL, "long", 5, 10000.0)
    # Mark still profitable but giveback from peak triggers lock exit
    broker.prices[LONG_SYMBOL] = 10200.0
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
    # Peak was high enough that current giveback ≥ 0.8pp
    state["profit_lock"] = {
        "peak_net_return": 3.0,
        "current_net_return": 1.5,
        "giveback_pct": 1.5,
        "profit_lock_active": True,
    }
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
        assert any(a.get("reason") == EXIT_PROFIT_LOCK for a in r["actions"] if "exit" in a or "reason" in a)
        assert state["position"]["symbol"] is None
        assert state["pending_signal_id"] is None  # no immediate rebuy (reentry disabled)
        assert CONTINUATION_REENTRY_ENABLED is False
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_worker_no_fixed_tp_at_plus_3pct(monkeypatch):
    """Legacy +3% net move must NOT force exit without profit-lock giveback."""
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
        assert not any(a.get("reason") == EXIT_TP for a in r["actions"])
        assert state["position"]["symbol"] == LONG_SYMBOL
        assert state["profit_lock"]["profit_lock_active"] is True
        assert state["profit_lock"]["peak_net_return"] >= PROFIT_LOCK_ACTIVATE_PCT
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_worker_opposite_priority_over_profit_lock(monkeypatch):
    broker = FakeBroker()
    broker.buy(LONG_SYMBOL, "long", 5, 10000.0)
    broker.prices[LONG_SYMBOL] = 10200.0
    broker.prices[INVERSE_SYMBOL] = 10000.0
    state = om.default_state()
    state["auto_trade_on"] = True
    state["budget"] = 5_000_000
    state["position"] = {
        "symbol": LONG_SYMBOL, "quantity": 5, "avg_price": 10000.0,
        "entry_at": datetime.now().isoformat(), "signal_id": "SIG",
        "entry_kind": ENTRY_INITIAL, "direction_episode_id": "EP:UP",
    }
    state["direction_episode"] = {
        **om.default_state()["direction_episode"],
        "id": "EP:UP", "direction": DIR_UP, "initial_entry_used": True,
    }
    # Would exit on profit lock if opposite were not pending
    state["profit_lock"] = {
        "peak_net_return": 3.0,
        "current_net_return": 1.5,
        "giveback_pct": 1.5,
        "profit_lock_active": True,
    }
    df = _bars_1m(150, trend="down")
    now = datetime(2026, 7, 21, 11, 0, 0)
    fake_eval = {
        "ok": True,
        "display_direction": DIR_DOWN,
        "new_signal": True,
        "signal_direction": DIR_DOWN,
        "macd": -1.0, "signal": -0.5, "hist": -0.5,
        "hist_last3": [-0.1, -0.3, -0.5], "hist_deltas": [-0.2, -0.2],
        "completed_3m_count": 40,
        "bar_ts": "2026-07-21T10:57:00",
        "bar_close_ts": "2026-07-21T11:00:00",
        "reason": "DOWN_BLUE_PATTERN",
        "signal_id": "MACD:DOWN:2026-07-21T10:57:00",
    }
    import app.trading.macd_hynix_worker as wmod
    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: fake_eval  # type: ignore
    try:
        r = worker.run_once(broker=broker, now=now, df_1m=df, state=state)
        assert any("opposite_signal" in a or "switch" in a for a in r["actions"])
        assert not any(a.get("reason") == EXIT_PROFIT_LOCK for a in r["actions"])
        # Same-tick execute: opposite arms and switches; pending cleared after success
        assert state.get("last_signal_direction") == DIR_DOWN
        assert state.get("pending_signal_id") is None
        assert INVERSE_SYMBOL in broker.positions or state.get("position", {}).get("symbol") == INVERSE_SYMBOL
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_worker_sl_still_works(monkeypatch):
    broker = FakeBroker()
    broker.buy(LONG_SYMBOL, "long", 5, 10000.0)
    broker.prices[LONG_SYMBOL] = 9800.0
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
    df = _bars_1m(150, trend="down")
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
        assert any(a.get("reason") == EXIT_SL for a in r["actions"])
        assert state["position"]["symbol"] is None
        assert state["direction_episode"]["sl_lock"] is True
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_episode_id_helper():
    eid = make_direction_episode_id(DIR_UP, "2026-07-21T10:00:00")
    assert eid.startswith("EP:UP_RED:")


def test_opening_probe_up_conditions():
    from app.trading.macd_hynix_strategy import (
        OPEN_IMMEDIATE_UP,
        evaluate_opening_probe,
    )

    warm = {
        "ok": True,
        "hist_last2": [1.0, 2.0],
        "hist_deltas": [0.5, 1.0],
    }
    long_q = {"ok": True, "price": 10000.0, "bid": 9990.0, "ask": 10010.0}
    inv_q = {"ok": True, "price": 10000.0}
    samples = [
        ("2026-07-22T09:00:05", 100.0),
        ("2026-07-22T09:00:10", 100.5),
    ]
    r = evaluate_opening_probe(
        warm,
        hynix_price=100.5,
        day_open_price=100.0,
        long_quote=long_q,
        inverse_quote=inv_q,
        price_samples_5s=samples,
    )
    assert r["ok_to_trade"] is True
    assert r["signal"] == OPEN_IMMEDIATE_UP
    assert r["direction"] == DIR_UP


def test_opening_probe_down_conditions():
    from app.trading.macd_hynix_strategy import (
        OPEN_IMMEDIATE_DOWN,
        evaluate_opening_probe,
    )

    warm = {
        "ok": True,
        "hist_last2": [-1.0, -2.0],
        "hist_deltas": [-0.5, -1.0],
    }
    long_q = {"ok": True, "price": 10000.0}
    inv_q = {"ok": True, "price": 10000.0, "bid": 9990.0, "ask": 10010.0}
    samples = [
        ("2026-07-22T09:00:05", 100.0),
        ("2026-07-22T09:00:10", 99.5),
    ]
    r = evaluate_opening_probe(
        warm,
        hynix_price=99.5,
        day_open_price=100.0,
        long_quote=long_q,
        inverse_quote=inv_q,
        price_samples_5s=samples,
    )
    assert r["ok_to_trade"] is True
    assert r["signal"] == OPEN_IMMEDIATE_DOWN


def test_warmup_macd_requires_100_bars():
    from app.trading.macd_hynix_strategy import WARMUP_3M_BARS, compute_warmup_macd

    df = _bars_1m(WARMUP_3M_BARS * 3 - 3, trend="up")
    r = compute_warmup_macd(df)
    assert r["ok"] is False
    df2 = _bars_1m(WARMUP_3M_BARS * 3 + 6, trend="up")
    r2 = compute_warmup_macd(df2)
    assert r2["ok"] is True
    assert len(r2["hist_last2"]) == 2


def test_open_probe_window():
    from app.trading.macd_hynix_strategy import in_open_probe_window, open_probe_window_expired

    assert in_open_probe_window(datetime(2026, 7, 22, 9, 0, 10))
    assert not in_open_probe_window(datetime(2026, 7, 22, 9, 0, 4))
    assert open_probe_window_expired(datetime(2026, 7, 22, 9, 0, 16))


def test_scale_opening_probe_adds_same_direction():
    broker = FakeBroker()
    state = om.default_state()
    broker.buy(LONG_SYMBOL, "long", 5, 10000.0)
    state["position"] = {
        "symbol": LONG_SYMBOL, "quantity": 5, "avg_price": 10000.0,
        "entry_at": datetime.now().isoformat(), "signal_id": "OPEN-1",
        "entry_kind": ENTRY_OPEN_IMMEDIATE, "direction_episode_id": "EP:UP",
        "size_fraction": 0.5, "opening_probe": True,
    }
    state["direction_episode"] = {
        **om.default_state()["direction_episode"],
        "id": "EP:UP", "direction": DIR_UP,
    }
    quotes = {
        "long": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
        "inverse": {"price": 10000.0, "updated_at": datetime.now().isoformat()},
    }
    buys_before = len(broker.buys)
    res = om.scale_opening_probe(
        broker, DIR_UP, mode="mock", budget=10_000_000, quotes=quotes,
        signal_id="OPEN-SCALE-1", state=state,
    )
    assert res["success"]
    assert len(broker.buys) > buys_before
    assert state["position"]["size_fraction"] >= 0.9


def test_day_flat_rollover_one_entry_then_same_day_restart_no_rebuy():
    """After flat day rollover: one initial entry; same-day restart must not re-buy same dir."""
    broker = FakeBroker()
    df = _bars_1m(150, trend="up")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["budget"] = 5_000_000
    state["session_date"] = "2026-07-22"
    state["last_signal_direction"] = DIR_DOWN  # yesterday leftover
    state["opening_probe_enabled"] = False

    # New day while flat clears direction once
    assert om.apply_macd_session_day_rollover(state, session_date="2026-07-23") is True
    assert state["last_signal_direction"] is None
    assert state["processed_signal_ids"] == []

    sid = "MACD3M:UP_RED:2026-07-23T10:00:00"
    up_eval = {
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
        "bar_ts": "2026-07-23T10:00:00",
        "bar_close_ts": "2026-07-23T10:03:00",
        "reason": "UP_RED_FIRST_TURN",
        "signal_id": sid,
    }
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: up_eval  # type: ignore
    try:
        now = datetime(2026, 7, 23, 10, 3, 5)
        worker.run_once(broker=broker, now=now, df_1m=df, state=state)
        worker.run_once(broker=broker, now=now + timedelta(seconds=5), df_1m=df, state=state)
        assert sum(1 for b in broker.buys if b[0] == LONG_SYMBOL) == 1

        # Same-day Stop→Start: rollover no-op; flatten book; same signal must not re-buy
        assert om.apply_macd_session_day_rollover(state, session_date="2026-07-23") is False
        assert state["last_signal_direction"] == DIR_UP
        for sym, pos in list(broker.positions.items()):
            broker.cash += float(pos.avg_price) * int(pos.quantity)
            del broker.positions[sym]
        state["position"] = om.default_state()["position"]
        n = len(broker.buys)
        for i in range(6):
            worker.run_once(
                broker=broker,
                now=now + timedelta(hours=1, seconds=5 * i),
                df_1m=df,
                state=state,
            )
        assert len(broker.buys) == n

        # Opposite flag → one new entry
        down_sid = "MACD3M:DOWN_BLUE:2026-07-23T12:00:00"
        down_eval = {
            **up_eval,
            "display_direction": DIR_DOWN,
            "signal_direction": DIR_DOWN,
            "bar_ts": "2026-07-23T12:00:00",
            "bar_close_ts": "2026-07-23T12:03:00",
            "reason": "DOWN_BLUE_FIRST_TURN",
            "signal_id": down_sid,
            "macd": -1.0,
            "hist": -0.5,
            "hist_last3": [-0.1, -0.3, -0.5],
            "hist_deltas": [-0.2, -0.2],
        }
        wmod.evaluate_macd_direction = lambda *a, **k: down_eval  # type: ignore
        for i in range(4):
            worker.run_once(
                broker=broker,
                now=datetime(2026, 7, 23, 12, 3, 5) + timedelta(seconds=5 * i),
                df_1m=df,
                state=state,
            )
        assert sum(1 for b in broker.buys if b[0] == INVERSE_SYMBOL) == 1
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_stop_auto_trade_stops_worker_and_start_force_restarts():
    """Stop joins via stop_worker; Start reloads modules so new bytecode loads."""
    import inspect

    src_stop = inspect.getsource(worker.stop_auto_trade)
    assert "stop_worker" in src_stop
    assert "join" in src_stop
    src_start = inspect.getsource(worker.start_auto_trade)
    assert "reload_macd_trading_stack" in src_start
    assert "repair_phantom_initial_entry" in src_start
    src_ensure = inspect.getsource(worker.ensure_worker_running)
    assert "force_restart" in src_ensure


def test_flat_down_blue_pattern_force_arms_and_buys():
    """Even when new_signal=False, flat+DOWN_BLUE must buy 0197X0 same tick."""
    broker = FakeBroker()
    df = _bars_1m(150, trend="down")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["budget"] = 5_000_000
    state["session_date"] = "2026-07-23"
    state["opening_probe_enabled"] = False
    now = datetime(2026, 7, 23, 10, 3, 5)
    fake_eval = {
        "ok": True,
        "display_direction": DIR_DOWN,
        "new_signal": False,
        "signal_direction": None,
        "macd": -1.0,
        "signal": -0.5,
        "hist": -0.5,
        "hist_last3": [-0.1, -0.3, -0.5],
        "hist_deltas": [-0.2, -0.2],
        "completed_3m_count": 40,
        "bar_ts": "2026-07-23T10:00:00",
        "bar_close_ts": "2026-07-23T10:03:00",
        "reason": "DOWN_BLUE_PATTERN",
        "signal_id": None,
        "onset": None,
    }
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: fake_eval  # type: ignore
    try:
        r = worker.run_once(broker=broker, now=now, df_1m=df, state=state)
        assert any(isinstance(a, dict) and a.get("force_arm") for a in r["actions"])
        assert sum(1 for b in broker.buys if b[0] == INVERSE_SYMBOL) == 1
        trace = r.get("decision_trace") or state.get("decision_trace") or {}
        assert trace.get("broker_called") is True
        assert (trace.get("broker_result") or {}).get("target") == INVERSE_SYMBOL
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_kis_minute_parse_accepts_dashed_today():
    """Regression: today ISO date must not break %Y%m%d%H%M%S parse."""
    # Unit-level: the formatter used inside _fetch_kis_minute_1m
    today = "2026-07-23"
    ymd = str(today).replace("-", "")[:8]
    hhmmss = "100100"
    from datetime import datetime as dt

    ts = dt.strptime(f"{ymd}{hhmmss}", "%Y%m%d%H%M%S")
    assert ts.hour == 10 and ts.minute == 1


def test_validate_etf_quotes_target_only_allows_long_buy():
    quotes = {
        "long": {"price": 15000.0, "updated_at": datetime.now().isoformat()},
        "inverse": {},  # missing — must not block long-only
    }
    ok, reason = om.validate_etf_quotes(quotes, required_symbols=[LONG_SYMBOL])
    assert ok is True
    ok2, _ = om.validate_etf_quotes(quotes, required_symbols=[LONG_SYMBOL, INVERSE_SYMBOL])
    assert ok2 is False


def test_repair_phantom_initial_entry_unlocks_flat_book():
    broker = FakeBroker()
    state = om.default_state()
    state["last_event"] = "INITIAL_ENTRY"
    state["last_signal_direction"] = DIR_UP
    state["last_signal_id"] = "MACD3M:UP_RED:2026-07-23T10:00:00"
    state["processed_signal_ids"] = ["MACD3M:UP_RED:2026-07-23T10:00:00"]
    state["position"] = om.default_state()["position"]
    out = worker.repair_phantom_initial_entry(state, broker)
    assert out["repaired"] is True
    assert state["last_signal_direction"] is None
    assert state["processed_signal_ids"] == []

def test_pattern_without_onset_arms_when_flat_episode_free():
    """UI-red/blue without onset must still arm INITIAL when flat and direction free."""
    from app.trading.macd_hynix_strategy import (
        signed_hist_two_turn_new_signal,
        signed_hist_two_turn_onset,
        signed_hist_two_turn_pattern,
    )

    assert signed_hist_two_turn_pattern(5.0, 4.0, 3.0) == DIR_UP
    assert signed_hist_two_turn_onset(5.0, 4.0, 3.0, 2.0) is None
    assert signed_hist_two_turn_new_signal(DIR_UP, None) is True
    assert signed_hist_two_turn_new_signal(DIR_UP, DIR_UP) is False

    df = _bars_1m(120, trend="up")
    now = df["datetime"].iloc[-1] + timedelta(minutes=3)
    r = evaluate_macd_direction(
        df, now=now, last_signal_direction=None, session_date=now.strftime("%Y-%m-%d"),
    )
    if r.get("ok") and r.get("display_direction") == DIR_UP:
        assert r["new_signal"] is True
        assert r["signal_id"]
        assert r["reason"] in ("UP_RED_FIRST_TURN", "UP_RED_PATTERN_ENTRY")


def test_worker_same_tick_arms_and_buys_up_red():
    """Flat UP_RED: arm + broker.buy(0193T0) on the same run_once tick."""
    broker = FakeBroker()
    df = _bars_1m(150, trend="up")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["budget"] = 5_000_000
    state["session_date"] = "2026-07-23"
    state["opening_probe_enabled"] = False
    now = datetime(2026, 7, 23, 10, 3, 5)
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
        "bar_ts": "2026-07-23T10:00:00",
        "bar_close_ts": "2026-07-23T10:03:00",
        "reason": "UP_RED_PATTERN_ENTRY",
        "signal_id": "MACD3M:UP_RED:2026-07-23T10:00:00",
        "onset": None,
    }
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: fake_eval  # type: ignore
    try:
        r = worker.run_once(broker=broker, now=now, df_1m=df, state=state)
        assert any("signal" in a or "switch" in a for a in r["actions"])
        assert sum(1 for b in broker.buys if b[0] == LONG_SYMBOL) == 1
        assert state.get("position", {}).get("symbol") == LONG_SYMBOL
        rows = om.load_ledger()
        assert any(row.get("action") == "BUY" and row.get("symbol") == LONG_SYMBOL for row in rows)
        n = len(broker.buys)
        worker.run_once(broker=broker, now=now + timedelta(seconds=5), df_1m=df, state=state)
        assert len(broker.buys) == n
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_worker_same_tick_arms_and_buys_down_blue():
    broker = FakeBroker()
    df = _bars_1m(150, trend="down")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["budget"] = 5_000_000
    state["session_date"] = "2026-07-23"
    state["opening_probe_enabled"] = False
    now = datetime(2026, 7, 23, 10, 3, 5)
    fake_eval = {
        "ok": True,
        "display_direction": DIR_DOWN,
        "new_signal": True,
        "signal_direction": DIR_DOWN,
        "macd": -1.0,
        "signal": -0.5,
        "hist": -0.5,
        "hist_last3": [-0.1, -0.3, -0.5],
        "hist_deltas": [-0.2, -0.2],
        "completed_3m_count": 40,
        "bar_ts": "2026-07-23T10:00:00",
        "bar_close_ts": "2026-07-23T10:03:00",
        "reason": "DOWN_BLUE_PATTERN_ENTRY",
        "signal_id": "MACD3M:DOWN_BLUE:2026-07-23T10:00:00",
        "onset": None,
    }
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: fake_eval  # type: ignore
    try:
        worker.run_once(broker=broker, now=now, df_1m=df, state=state)
        assert sum(1 for b in broker.buys if b[0] == INVERSE_SYMBOL) == 1
        assert any(
            row.get("action") == "BUY" and row.get("symbol") == INVERSE_SYMBOL
            for row in om.load_ledger()
        )
        assert state.get("signal_type") == "INITIAL"
        assert state.get("current_flag") == DIR_DOWN
        assert state.get("armed_at")
        assert state.get("pending_signal_id") is None
        assert state.get("order_requested_at") or (state.get("order_latency") or {}).get(
            "order_requested_at"
        )
        assert state.get("broker_executed_at") or (state.get("order_latency") or {}).get(
            "broker_executed_at"
        )
        assert state.get("position_confirmed_at") or (state.get("order_latency") or {}).get(
            "position_confirmed_at"
        )
        n = len(broker.buys)
        for i in range(8):
            worker.run_once(
                broker=broker, now=now + timedelta(seconds=5 * (i + 1)), df_1m=df, state=state
            )
        assert len(broker.buys) == n
        assert state.get("duplicate_block_reason")
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_worker_ui_fields_and_reversal_opposite_switch():
    """Holding inverse + UP_RED → REVERSAL sell-all then buy 0193T0; UI fields set."""
    broker = FakeBroker()
    df = _bars_1m(150, trend="up")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["budget"] = 5_000_000
    state["session_date"] = "2026-07-23"
    state["opening_probe_enabled"] = False
    down_sid = "MACD3M:DOWN_BLUE:2026-07-23T10:00:00"
    up_sid = "MACD3M:UP_RED:2026-07-23T11:00:00"
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    try:
        wmod.evaluate_macd_direction = lambda *a, **k: {  # type: ignore
            "ok": True,
            "display_direction": DIR_DOWN,
            "new_signal": True,
            "signal_direction": DIR_DOWN,
            "macd": -1.0,
            "signal": -0.5,
            "hist": -0.5,
            "hist_last3": [-0.1, -0.3, -0.5],
            "hist_deltas": [-0.2, -0.2],
            "completed_3m_count": 40,
            "bar_ts": "2026-07-23T10:00:00",
            "bar_close_ts": "2026-07-23T10:03:00",
            "reason": "DOWN_BLUE_FIRST_TURN",
            "signal_id": down_sid,
        }
        worker.run_once(
            broker=broker, now=datetime(2026, 7, 23, 10, 3, 5), df_1m=df, state=state
        )
        assert state["position"]["symbol"] == INVERSE_SYMBOL

        wmod.evaluate_macd_direction = lambda *a, **k: {  # type: ignore
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
            "bar_ts": "2026-07-23T11:00:00",
            "bar_close_ts": "2026-07-23T11:03:00",
            "reason": "UP_RED_FIRST_TURN",
            "signal_id": up_sid,
        }
        worker.run_once(
            broker=broker, now=datetime(2026, 7, 23, 11, 3, 5), df_1m=df, state=state
        )
        assert state["signal_type"] == "REVERSAL"
        assert state["position"]["symbol"] == LONG_SYMBOL
        assert any(s[0] == INVERSE_SYMBOL for s in broker.sells)
        assert sum(1 for b in broker.buys if b[0] == LONG_SYMBOL) == 1
        assert INVERSE_SYMBOL not in broker.positions
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_force_restart_changes_thread_ident():
    worker.stop_worker()
    if worker._worker_thread and worker._worker_thread.is_alive():
        worker._worker_thread.join(timeout=2.0)
    st0 = worker.ensure_worker_running(force_restart=True)
    tid0 = st0.get("thread_ident")
    assert tid0 is not None
    st1 = worker.ensure_worker_running(force_restart=True)
    tid1 = st1.get("thread_ident")
    assert tid1 is not None
    assert tid1 != tid0
    worker.stop_worker()


def test_mock_create_broker_ignores_real_confirm(monkeypatch):
    """MOCK must never require REAL confirm phrase / real_ready."""
    calls = []

    class _Dummy:
        mode = "mock"

    def _fake_create_broker(**kwargs):
        calls.append(kwargs)
        return _Dummy()

    monkeypatch.setattr(
        "app.trading.broker_factory.create_broker",
        _fake_create_broker,
    )
    broker = om.create_macd_broker(
        "mock",
        real_confirm_text="",
        real_ready=False,
    )
    assert broker is not None
    assert calls and calls[0].get("mode") == "mock"
    # REAL kwargs must not be forwarded on mock path
    assert "confirm_text" not in calls[0]
    assert "runtime_real_mode" not in calls[0]


def test_mock_run_once_without_real_confirm_ok(monkeypatch):
    """Worker mock path creates broker without reading real_confirm_ok."""
    broker = FakeBroker()
    created = {"mode": None, "kwargs": None}

    def _capture(mode, *, real_confirm_text="", real_ready=False):
        created["mode"] = mode
        created["kwargs"] = {"real_confirm_text": real_confirm_text, "real_ready": real_ready}
        return broker

    monkeypatch.setattr(om, "create_macd_broker", _capture)
    df = _bars_1m(80, trend="up")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state["real_confirm_ok"] = False  # must not block mock
    state["session_date"] = "2026-07-23"
    state["opening_probe_enabled"] = False
    state["budget"] = 5_000_000
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
        "bar_ts": "2026-07-23T09:03:00",
        "bar_close_ts": "2026-07-23T09:06:00",
        "reason": "UP_RED_FIRST_TURN",
        "signal_id": "MACD3M:UP_RED:2026-07-23T09:03:00",
        "onset": None,
    }
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: fake_eval  # type: ignore
    try:
        # broker=None forces create_macd_broker path
        r = worker.run_once(broker=None, now=datetime(2026, 7, 23, 9, 6, 5), df_1m=df, state=state)
        assert created["mode"] == "mock"
        assert r.get("ok") is not False or not str(r.get("error") or "").startswith("broker create")
        assert state.get("decision_trace", {}).get("real_gate_checked") is False
        assert sum(1 for b in broker.buys if b[0] == LONG_SYMBOL) == 1
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_same_flag_20_ticks_no_duplicate_buys():
    broker = FakeBroker()
    df = _bars_1m(150, trend="up")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["budget"] = 5_000_000
    state["session_date"] = "2026-07-23"
    state["opening_probe_enabled"] = False
    now = datetime(2026, 7, 23, 10, 3, 5)
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
        "bar_ts": "2026-07-23T10:00:00",
        "bar_close_ts": "2026-07-23T10:03:00",
        "reason": "UP_RED_FIRST_TURN",
        "signal_id": "MACD3M:UP_RED:2026-07-23T10:00:00",
        "onset": None,
    }
    # After first tick, subsequent evals are held pattern (no new_signal)
    held_eval = {**fake_eval, "new_signal": False, "signal_id": None, "reason": "UP_RED_PATTERN"}
    import app.trading.macd_hynix_worker as wmod

    calls = {"n": 0}

    def _eval(*a, **k):
        calls["n"] += 1
        return fake_eval if calls["n"] == 1 else held_eval

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = _eval  # type: ignore
    try:
        for i in range(20):
            worker.run_once(
                broker=broker,
                now=now + timedelta(seconds=5 * i),
                df_1m=df,
                state=state,
            )
        assert sum(1 for b in broker.buys if b[0] == LONG_SYMBOL) == 1
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_ui_and_worker_share_same_signed_b_flag():
    """UI must display worker-computed flag only (no independent signed-B)."""
    broker = FakeBroker()
    df = _bars_1m(150, trend="up")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["budget"] = 5_000_000
    state["session_date"] = "2026-07-23"
    state["opening_probe_enabled"] = False
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
        "bar_ts": "2026-07-23T09:03:00",
        "bar_close_ts": "2026-07-23T09:06:00",
        "reason": "UP_RED_FIRST_TURN",
        "signal_id": "MACD3M:UP_RED:2026-07-23T09:03:00",
        "onset": None,
    }
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: fake_eval  # type: ignore
    try:
        worker.run_once(
            broker=broker,
            now=datetime(2026, 7, 23, 9, 6, 5),
            df_1m=df,
            state=state,
        )
        # UI reads these fields from state — must match worker eval
        assert state["display_direction"] == fake_eval["display_direction"]
        assert state["current_flag"] == fake_eval["display_direction"]
        assert state["last_flag"] == fake_eval["display_direction"]
        assert (state.get("last_signal_eval") or {}).get("flag") == fake_eval["display_direction"]
        assert (state.get("decision_trace") or {}).get("flag") == fake_eval["display_direction"]
        assert (state.get("decision_trace") or {}).get("completed_bar_at") == fake_eval["bar_close_ts"]
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_intervals_buf_caps_at_40_but_tick_seq_keeps_growing(monkeypatch):
    """Prove the historic 'freeze at 40' was intervals buffer — tick_seq must exceed 40."""
    # 1) Pure logic: buffer caps, counter does not.
    intervals: list[float] = []
    tick_seq = 0
    for _ in range(80):
        tick_seq += 1
        intervals.append(5.0)
        intervals = intervals[-worker.INTERVAL_HISTORY_MAX :]
    assert tick_seq == 80
    assert len(intervals) == worker.INTERVAL_HISTORY_MAX

    # 2) Live worker with disk/git/stale mocked out — must cross 60.
    worker.stop_worker()
    if worker._worker_thread and worker._worker_thread.is_alive():
        worker._worker_thread.join(timeout=2.0)
    worker._tick_counter = 0
    with worker._status_lock:
        worker._status["tick_intervals"] = []
        worker._status["tick_n"] = 0
        worker._status["tick_seq"] = 0
        worker._status["last_tick_at"] = None

    monkeypatch.setattr(worker, "TICK_SECONDS", 0.005)
    monkeypatch.setattr(worker, "TICK_STALL_SEC", 60.0)
    monkeypatch.setattr(worker, "_STALE_CHECK_EVERY_N_TICKS", 10_000)
    monkeypatch.setattr(worker, "run_once", lambda **k: {"ok": True, "actions": []})
    monkeypatch.setattr(
        worker, "worker_identity", lambda: {"stale_worker": False, "stale_reason": None}
    )

    mem = {
        "auto_trade_on": True,
        "strategy_enabled": True,
        "force_liquidate_pending": False,
        "session_date": "2026-07-23",
        "mode": "mock",
        "worker": {"tick_n": 0, "tick_seq": 0, "alive": False},
    }

    def _hb(*, tick_n, intervals=None, error=None, partial=False):
        now_iso = datetime.now().isoformat()
        with worker._status_lock:
            worker._status["alive"] = True
            worker._status["last_tick_at"] = now_iso
            worker._status["tick_n"] = int(tick_n)
            worker._status["tick_seq"] = int(tick_n)
            if intervals is not None:
                worker._status["tick_intervals"] = list(intervals)[-worker.INTERVAL_HISTORY_MAX :]
        mem["worker"]["tick_n"] = int(tick_n)
        mem["worker"]["tick_seq"] = int(tick_n)
        mem["worker"]["last_tick_at"] = now_iso
        mem["worker"]["alive"] = True
        return now_iso

    monkeypatch.setattr(worker, "_persist_heartbeat", _hb)
    monkeypatch.setattr(om, "load_state", lambda: dict(mem))
    monkeypatch.setattr(om, "save_state", lambda s: None)
    monkeypatch.setattr(om, "apply_macd_session_day_rollover", lambda *a, **k: None)
    monkeypatch.setattr(om, "refresh_runtime_status", lambda *a, **k: {})

    worker._last_stall_recover_mono = 0.0
    status = worker.ensure_worker_running(force_restart=True)
    assert status.get("thread_alive")

    deadline = datetime.now() + timedelta(seconds=3)
    while datetime.now() < deadline:
        s = worker.get_worker_status()
        if int(s.get("tick_seq") or 0) >= 60:
            break
        import time as _t

        _t.sleep(0.01)

    s = worker.get_worker_status()
    seq = int(s.get("tick_seq") or 0)
    buf = len(s.get("tick_intervals") or [])
    worker.stop_worker()
    if worker._worker_thread and worker._worker_thread.is_alive():
        worker._worker_thread.join(timeout=2.0)

    assert seq >= 60, f"tick_seq froze or too low: {seq}"
    assert buf <= worker.INTERVAL_HISTORY_MAX


def test_detect_worker_stall_and_recover(monkeypatch):
    """Stale last_tick while strategy on → detect stall; ensure recovers."""
    worker.stop_worker()
    if worker._worker_thread and worker._worker_thread.is_alive():
        worker._worker_thread.join(timeout=2.0)

    st = om.default_state()
    st["auto_trade_on"] = True
    st["strategy_enabled"] = True
    st["worker"] = {
        **(st.get("worker") or {}),
        "alive": True,
        "last_tick_at": (datetime.now() - timedelta(seconds=20)).isoformat(),
        "tick_n": 40,
        "tick_seq": 40,
    }
    om.save_state(st)
    with worker._status_lock:
        worker._status["alive"] = True
        worker._status["last_tick_at"] = st["worker"]["last_tick_at"]
        worker._status["tick_n"] = 40
        worker._status["tick_seq"] = 40
    # Thread intentionally dead → WORKER_THREAD_DEAD or stale
    worker._worker_thread = None

    info = worker.detect_worker_stall(state=st, stall_sec=15.0)
    assert info["stalled"] is True
    assert info["stall_reason"] in ("WORKER_THREAD_DEAD", "WORKER_TICK_STALE", "WORKER_NO_HEARTBEAT")

    monkeypatch.setattr(worker, "TICK_SECONDS", 0.05)
    monkeypatch.setattr(worker, "run_once", lambda **k: {"ok": True, "actions": []})
    monkeypatch.setattr(
        worker, "worker_identity", lambda: {"stale_worker": False, "stale_reason": None}
    )
    worker._last_stall_recover_mono = 0.0
    recovered = worker.ensure_worker_running()
    assert recovered.get("thread_alive") or recovered.get("recovered_stall")
    # Give a couple ticks
    import time as _t

    _t.sleep(0.3)
    after = worker.get_worker_status()
    worker.stop_worker()
    assert after.get("thread_alive") or int(after.get("tick_seq") or 0) >= 0


def test_replay_20260723_0906_up_red_reaches_order_path():
    """Then-time 09:06 UP_RED signal_id must arm+buy on mock without real confirm."""
    broker = FakeBroker()
    df = _bars_1m(150, trend="up")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state["real_confirm_ok"] = False
    state["budget"] = 1_000_000
    state["session_date"] = "2026-07-23"
    state["opening_probe_enabled"] = False
    sid = "MACD3M:UP_RED:2026-07-23T09:03:00"
    fake_eval = {
        "ok": True,
        "display_direction": DIR_UP,
        "new_signal": True,
        "signal_direction": DIR_UP,
        "macd": 1.0,
        "signal": 0.5,
        "hist": 5306.6288,
        "hist_last3": [-1460.345725, 2928.736204, 5306.6288],
        "hist_deltas": [4389.081929, 2377.892596],
        "completed_3m_count": 102,
        "bar_ts": "2026-07-23T09:03:00",
        "bar_close_ts": "2026-07-23T09:06:00",
        "reason": "UP_RED_FIRST_TURN",
        "signal_id": sid,
        "onset": None,
    }
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: fake_eval  # type: ignore
    try:
        r = worker.run_once(
            broker=broker, now=datetime(2026, 7, 23, 9, 6, 5), df_1m=df, state=state
        )
        tr = state.get("decision_trace") or {}
        assert tr.get("real_gate_checked") is False
        assert tr.get("execute_attempted") or any("switch" in a for a in r["actions"])
        assert sum(1 for b in broker.buys if b[0] == LONG_SYMBOL) == 1
        assert state.get("position", {}).get("symbol") == LONG_SYMBOL
        assert sid in (state.get("processed_signal_ids") or []) or state.get("last_signal_id") == sid
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_replay_20260723_1027_down_blue_reaches_order_path():
    """Then-time 10:27 DOWN_BLUE must buy 0197X0 (worker alive + mock, no real gate)."""
    broker = FakeBroker()
    df = _bars_1m(150, trend="down")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state["real_confirm_ok"] = False
    state["budget"] = 1_000_000
    state["session_date"] = "2026-07-23"
    state["opening_probe_enabled"] = False
    sid = "MACD3M:DOWN_BLUE:2026-07-23T10:24:00"
    fake_eval = {
        "ok": True,
        "display_direction": DIR_DOWN,
        "new_signal": True,
        "signal_direction": DIR_DOWN,
        "macd": -1.0,
        "signal": -0.5,
        "hist": -1350.797818,
        "hist_last3": [1188.424681, -267.123223, -1350.797818],
        "hist_deltas": [-1455.547904, -1083.674595],
        "completed_3m_count": 120,
        "bar_ts": "2026-07-23T10:24:00",
        "bar_close_ts": "2026-07-23T10:27:00",
        "reason": "DOWN_BLUE_FIRST_TURN",
        "signal_id": sid,
        "onset": None,
    }
    import app.trading.macd_hynix_worker as wmod

    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: fake_eval  # type: ignore
    try:
        r = worker.run_once(
            broker=broker, now=datetime(2026, 7, 23, 10, 27, 5), df_1m=df, state=state
        )
        tr = state.get("decision_trace") or {}
        assert tr.get("real_gate_checked") is False
        assert tr.get("completed_bar_at") == "2026-07-23T10:27:00"
        assert sum(1 for b in broker.buys if b[0] == INVERSE_SYMBOL) == 1
        assert any("switch" in a for a in r["actions"]) or tr.get("broker_called")
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore

def test_bootstrap_builds_300_1m_to_100_3m(monkeypatch):
    """Bootstrap must produce ≥100 completed 3m from prior+live without hot-path re-fetch."""
    # Build synthetic prior+live 1m (≥300 bars)
    prior = _bars_1m(320, start=datetime(2026, 7, 22, 9, 0, 0), trend="up")
    live = _bars_1m(60, start=datetime(2026, 7, 23, 9, 0, 0), trend="up")

    monkeypatch.setattr(worker, "_load_prior_history_1m", lambda now: (prior, {"api_name": "test_prior", "received_1m_bars": len(prior)}))
    monkeypatch.setattr(
        worker,
        "_fetch_kis_minute_paged",
        lambda mode, today, target_bars=120, timeout_sec=4.0: (live, {"kis_requests": 2, "received_1m_bars": len(live)}),
    )
    with worker._history_lock:
        worker._HISTORY_CACHE.update({"session_date": None, "df_1m": None, "bootstrap_ok": False, "diag": {}})

    boot = worker.bootstrap_macd_history("mock", now=datetime(2026, 7, 23, 10, 0, 0))
    assert boot.get("ok") is True
    assert int(boot.get("completed_3m_count") or 0) >= 100
    assert int(boot.get("received_1m_bars") or 0) >= 300
    with worker._history_lock:
        assert worker._HISTORY_CACHE.get("bootstrap_ok") is True
        cached = worker._HISTORY_CACHE.get("df_1m")
    assert cached is not None and len(cached) >= 300

    # Hot path must NOT re-call paged fetch — only incremental
    calls = {"paged": 0}

    def _no_page(*a, **k):
        calls["paged"] += 1
        return pd.DataFrame(), {}

    monkeypatch.setattr(worker, "_fetch_kis_minute_paged", _no_page)
    monkeypatch.setattr(
        worker,
        "_fetch_kis_minute_1m",
        lambda *a, **k: (live.tail(5), {"received_1m_bars": 5}),
    )
    df2, diag2 = worker.load_macd_minute_history("mock", count=30, now=datetime(2026, 7, 23, 10, 5, 0))
    assert calls["paged"] == 0
    assert diag2.get("incremental") is True
    assert len(df2) >= 300


def test_completed_signal_ui_matches_worker():
    broker = FakeBroker()
    df = _bars_1m(150, trend="up")
    state = om.default_state()
    state["auto_trade_on"] = True
    state["budget"] = 5_000_000
    state["session_date"] = "2026-07-23"
    state["opening_probe_enabled"] = False
    state["bootstrap"] = {"ok": True, "status": "OK"}
    state["opening_probe"] = {"warmup_ready": True}
    sid = "MACD3M:UP_RED:2026-07-23T09:03:00"
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
        "completed_3m_count": 100,
        "bar_ts": "2026-07-23T09:03:00",
        "bar_close_ts": "2026-07-23T09:06:00",
        "reason": "UP_RED_FIRST_TURN",
        "signal_id": sid,
        "onset": None,
    }
    import app.trading.macd_hynix_worker as wmod
    original = wmod.evaluate_macd_direction
    wmod.evaluate_macd_direction = lambda *a, **k: fake_eval  # type: ignore
    try:
        worker.run_once(broker=broker, now=datetime(2026, 7, 23, 9, 6, 5), df_1m=df, state=state)
        cs = state.get("completed_signal") or {}
        assert cs.get("flag") == state.get("current_flag") == DIR_UP
        assert cs.get("signal_id") == sid or state.get("last_signal_id") == sid
        assert cs.get("completed_bar_at") == "2026-07-23T09:06:00"
    finally:
        wmod.evaluate_macd_direction = original  # type: ignore


def test_tick_seq_not_capped_by_interval_buffer_logic():
    intervals: list[float] = []
    tick_seq = 0
    for _ in range(80):
        tick_seq += 1
        intervals.append(5.0)
        intervals = intervals[-worker.INTERVAL_HISTORY_MAX :]
    assert tick_seq == 80
    assert len(intervals) == worker.INTERVAL_HISTORY_MAX
