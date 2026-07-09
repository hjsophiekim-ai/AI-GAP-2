"""
test_hynix_position_sync.py — 000660/0197X0 포지션 감지·동기화·중복매수차단·
mock/real 분리·Dynamic Exit AI 연결에 대한 9가지 요구사항 검증.
"""

from __future__ import annotations

from datetime import datetime

import app.services.hynix_switch_state as state_module
import app.trading.dynamic_exit_watcher as watcher
from app.models import OrderResult, Position
from app.services.hynix_auto_trade_service import HYNIX_SYMBOL, HYNIX_NAME
from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME
from app.trading.hynix_position_common import get_hynix_auto_position, POSITION_INVERSE, POSITION_HYNIX
from app.trading.hynix_switch_position_manager import run_switch_or_entry, sync_position_from_broker
from app.services.hynix_switch_state import default_state


class DummyBroker:
    def __init__(self, buy_success=True, sell_success=True, buyable_cash=10_000_000, positions=None):
        self.buy_success = buy_success
        self.sell_success = sell_success
        self.buyable_cash = buyable_cash
        self._positions = positions or []
        self.buy_calls = []
        self.sell_calls = []

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        self.buy_calls.append((symbol, quantity, price))
        return OrderResult(success=self.buy_success, mode="mock", account_type="mock", symbol=symbol, name=name,
                            side="buy", quantity=quantity, price=price, order_type=order_type,
                            order_id="B1" if self.buy_success else "", message="ok" if self.buy_success else "실패")

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        self.sell_calls.append((symbol, quantity, price))
        return OrderResult(success=self.sell_success, mode="mock", account_type="mock", symbol=symbol, name=name,
                            side="sell", quantity=quantity, price=price, order_type=order_type,
                            order_id="S1" if self.sell_success else "", message="ok" if self.sell_success else "실패")

    def get_positions(self):
        return self._positions

    def get_balance(self):
        return self.buyable_cash

    def get_current_price(self, symbol):
        return None

    def get_buyable_cash(self):
        return self.buyable_cash


# ── 1) mock BUY 0197X0 후 UI(state) 보유종목이 0197X0으로 표시되는지 ───────────

def test_mock_buy_inverse_from_flat_shows_in_state():
    state = default_state("mock")
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "INVERSE_BUY", 101_000, 5_000, now=now)

    assert result["acted"] is True
    assert state["position"]["symbol"] == INVERSE_SYMBOL
    detected = get_hynix_auto_position([state["position"]])
    assert detected["current_position"] == POSITION_INVERSE


# ── 2) mock BUY 0197X0 후 daily_trade_count가 1 증가하는지 ─────────────────────

def test_mock_buy_inverse_increments_daily_trade_count():
    state = default_state("mock")
    assert state["daily_trade_count"] == 0
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    run_switch_or_entry(state, broker, "INVERSE_BUY", 101_000, 5_000, now=now)

    assert state["daily_trade_count"] == 1


# ── 3) 이미 0197X0 보유 중이면 추가 INVERSE_BUY가 차단되는지 ───────────────────

def test_duplicate_inverse_buy_blocked_while_holding_inverse():
    state = default_state("mock")
    state["position"] = {
        **state["position"], "symbol": INVERSE_SYMBOL, "name": INVERSE_NAME, "quantity": 100,
        "avg_price": 5_000, "entry_price": 5_000, "entry_time": datetime(2026, 7, 9, 9, 30).isoformat(),
    }
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "INVERSE_BUY", 101_000, 5_100, now=now)

    assert result["acted"] is False
    assert "이미 인버스 보유 중" in result["message"]
    assert len(broker.buy_calls) == 0 and len(broker.sell_calls) == 0


def test_duplicate_hynix_buy_blocked_while_holding_hynix():
    state = default_state("mock")
    state["position"] = {
        **state["position"], "symbol": HYNIX_SYMBOL, "name": HYNIX_NAME, "quantity": 5,
        "avg_price": 100_000, "entry_price": 100_000, "entry_time": datetime(2026, 7, 9, 9, 30).isoformat(),
    }
    broker = DummyBroker()
    now = datetime(2026, 7, 9, 10, 0)

    result = run_switch_or_entry(state, broker, "HYNIX_BUY", 101_000, 5_100, now=now)

    assert result["acted"] is False
    assert "이미 하이닉스 보유 중" in result["message"]


# ── 4)/5) 스위칭 방향 전환 (기존 test_hynix_switch_position_manager.py에서 이미 검증) ─
# test_switch_from_hynix_to_inverse_sells_then_buys / test_switch_from_inverse_to_hynix_sells_then_buys 참고.


# ── 6) mock 초기잔고가 매일 10,000,000원으로 정상 초기화되는지 ─────────────────

def test_mock_cash_resets_to_budget_on_new_day(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    stale_state = state_module.default_state("mock")
    stale_state["date"] = "20200101"
    stale_state["cash"] = 1_234.0  # 전날 소진된 잔고
    stale_state["mock_budget_krw"] = 10_000_000.0
    state_module.save_state_atomic(stale_state)

    reloaded = state_module.load_state(mode="mock")

    assert reloaded["cash"] == 10_000_000.0


def test_reset_mock_state_helper_sets_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)
    import app.trading.dry_run_broker as dry_run_broker_module
    monkeypatch.setattr(dry_run_broker_module, "_DATA_DIR", tmp_path)  # 실제 data/orders/ 삭제 방지

    reset = state_module.reset_mock_state(budget_krw=7_000_000)

    assert reset["cash"] == 7_000_000
    assert reset["mock_budget_krw"] == 7_000_000
    assert reset["position"]["symbol"] is None


# ── 7) mock/real state가 분리되는지 (기존 test_hynix_switch_state.py에서 이미 검증) ──
# test_mock_and_real_states_are_separate_files 참고.


# ── 8) 실제 real 계좌에 0197X0 보유가 있으면 state(UI 소스)에 표시되는지 ────────

def test_real_broker_inverse_position_syncs_into_state():
    state = default_state("real")
    assert state["position"]["symbol"] is None

    real_inverse_position = Position(symbol=INVERSE_SYMBOL, name=INVERSE_NAME, quantity=2, avg_price=8_700.0, current_price=8_800.0)
    broker = DummyBroker(positions=[real_inverse_position])

    sync_position_from_broker(state, broker)

    assert state["position"]["symbol"] == INVERSE_SYMBOL
    assert state["position"]["quantity"] == 2


def test_conflict_positions_flag_and_block(monkeypatch):
    state = default_state("real")
    hynix_pos = Position(symbol=HYNIX_SYMBOL, name=HYNIX_NAME, quantity=1, avg_price=100_000, current_price=100_000)
    inverse_pos = Position(symbol=INVERSE_SYMBOL, name=INVERSE_NAME, quantity=1, avg_price=5_000, current_price=5_000)
    broker = DummyBroker(positions=[hynix_pos, inverse_pos])

    sync_position_from_broker(state, broker)

    assert state["position_conflict"] is True
    assert state["critical_alert"] is not None


# ── 9) Dynamic Exit AI가 0197X0 보유 포지션을 감지하는지 ───────────────────────

def test_dynamic_exit_watcher_detects_inverse_position(tmp_path, monkeypatch):
    """Broker가 실제로 0197X0을 보유 중이라고 응답해야만 Dynamic Exit AI가 이를 인식해야 한다
    (state를 직접 조작하는 것만으로는 인식되면 안 됨 — Broker가 유일한 Source of Truth)."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    # entry_price/entry_time 등 "우리쪽 부가 기록"만 미리 넣어둔다(브로커가 모르는 정보).
    state["position"] = {
        **state["position"], "entry_price": 5_000.0,
        "entry_time": datetime.now().isoformat(),
    }
    state_module.save_state_atomic(state)

    monkeypatch.setattr(watcher, "_fetch_current_price", lambda symbol, mode: 5_155.0)  # +3.1% -> NORMAL TP
    monkeypatch.setattr(watcher, "_load_daily_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_load_minute_df", lambda symbol: None)
    monkeypatch.setattr(watcher, "_EXIT_LOG_PATH", tmp_path / "exit_engine_log.csv")

    # Broker가 실제로 0197X0 100주를 들고 있다고 응답 — 이것만이 신뢰되어야 한다.
    inverse_position = Position(symbol=INVERSE_SYMBOL, name=INVERSE_NAME, quantity=100, avg_price=5_000.0, current_price=5_155.0)
    fake_broker = DummyBroker(sell_success=True, positions=[inverse_position])
    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: fake_broker)

    decision = watcher.tick(now=datetime.now())

    assert decision is not None
    assert decision["action"] == "SELL_ALL"
    assert len(fake_broker.sell_calls) == 1 and fake_broker.sell_calls[0][0] == INVERSE_SYMBOL


def test_dynamic_exit_watcher_ignores_stale_state_without_broker_position(tmp_path, monkeypatch):
    """state 파일에 포지션이 남아 있어도 Broker가 무보유라고 응답하면 감시하지 않아야 한다."""
    monkeypatch.setattr(state_module, "_STATE_DIR", tmp_path)

    state = state_module.load_state(mode="mock")
    state["mode"] = "mock"
    state["auto_trade_on"] = True
    state["position"] = {
        **state["position"], "symbol": INVERSE_SYMBOL, "name": INVERSE_NAME, "quantity": 100,
        "avg_price": 5_000.0, "entry_price": 5_000.0, "entry_time": datetime.now().isoformat(),
    }
    state_module.save_state_atomic(state)

    fake_broker = DummyBroker(positions=[])  # 브로커는 무보유
    monkeypatch.setattr(watcher, "_get_cached_broker", lambda mode, budget: fake_broker)

    decision = watcher.tick(now=datetime.now())

    assert decision is None
    reloaded = state_module.load_state(mode="mock")
    assert reloaded["position"]["symbol"] is None  # 브로커 기준으로 정정되어야 함
