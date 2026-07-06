"""
test_hynix_auto_trade_service.py — SK하이닉스 자동매매 서비스 테스트.

브로커/데이터 수집은 fake로 대체하고, 킬스위치/모드 분기/완전자동 게이트/
로그 기록 여부를 검증한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import app.services.hynix_auto_trade_service as svc
from app.models import Position, OrderResult


class _FakeBroker:
    mode = "mock"

    def __init__(self, cash=50_000_000.0, positions=None):
        self._cash = cash
        self._positions = positions or []

    def get_buyable_cash(self):
        return self._cash

    def get_balance(self):
        return self._cash

    def get_positions(self):
        return self._positions

    def get_current_price(self, symbol):
        return 170_000.0

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="buy", quantity=quantity, price=price, order_type=order_type,
            order_id="TEST-BUY", message="OK",
        )

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        return OrderResult(
            success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
            side="sell", quantity=quantity, price=price, order_type=order_type,
            order_id="TEST-SELL", message="OK",
        )


def _fake_signal_ok():
    return {
        "blocked": False,
        "short_term_score": 62.0,
        "direction": "상승 우세",
        "recent_high": 200_000.0,
        "recent_low": 150_000.0,
        "drawdown_rate": -15.0,
        "support_levels": [180_000.0, 175_000.0, 150_000.0],
        "target_levels": [190_000.0, 185_000.0, 200_000.0],
        "target_probabilities": {"target_1": 60.0, "target_2": 40.0, "target_3": 20.0},
        "target_1": 190_000.0,
        "target_2_probability": 40.0,
        "judgement": "눌림 시 매수 가능",
        "reasons_top5": ["reason1", "reason2"],
        "volume_confirmed": True,
        "upper_wick_near_high": False,
        "news_warning": None,
        "disclaimer": "확률 기반 참고자료이며 투자판단은 사용자 책임입니다.",
        "raw_inputs": {
            "mu_regular_return": 1.0, "sox_return": -0.5,
            "hynix_today_return_pct": 1.0,
            "hynix_prev_close": 168_000.0, "hynix_current_price": 170_000.0,
            "current_price_sources": {"KIS": 170_000.0, "naver": 170_000.0, "yfinance": 170_000.0},
            "minute_last_bar_time": datetime.now().isoformat(),
        },
    }


def _fake_signal_blocked():
    return {"blocked": True, "block_reason": "필수 데이터 없음", "missing_data": ["SK하이닉스 현재가"], "disclaimer": "d"}


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(svc, "_STOP_FLAG_PATH", tmp_path / "state" / "hynix_auto_trade_stopped.flag")
    monkeypatch.setattr(svc, "_LOG_DIR", tmp_path / "logs")
    yield


class TestKillSwitch:
    def test_stopped_blocks_proposal_generation(self, monkeypatch):
        svc.stop_auto_trade()
        assert svc.is_stopped() is True
        proposal = svc.generate_trade_proposal(mode="mock")
        assert proposal["blocked"] is True
        svc.resume_auto_trade()
        assert svc.is_stopped() is False

    def test_stopped_blocks_execution(self):
        svc.stop_auto_trade()
        result = svc.execute_proposal({"action": "BUY", "blocked": False, "buy_cash_amount": 1000}, mode="mock")
        assert result["success"] is False
        assert result["error_type"] == "stopped"
        svc.resume_auto_trade()


class TestGenerateProposal:
    def test_blocked_signal_returns_blocked_proposal(self, monkeypatch):
        monkeypatch.setattr(svc, "is_stopped", lambda: False)
        monkeypatch.setattr("app.data_sources.auto_market_collector.collect_all", lambda mode=None: {})
        monkeypatch.setattr("app.models.hynix_short_term_signal.predict_hynix_signal", lambda md: _fake_signal_blocked())
        proposal = svc.generate_trade_proposal(mode="mock")
        assert proposal["blocked"] is True
        assert "SK하이닉스 현재가" in proposal["missing_data"]

    def test_ok_signal_produces_action(self, monkeypatch):
        monkeypatch.setattr(svc, "is_stopped", lambda: False)
        monkeypatch.setattr("app.data_sources.auto_market_collector.collect_all", lambda mode=None: {})
        monkeypatch.setattr("app.models.hynix_short_term_signal.predict_hynix_signal", lambda md: _fake_signal_ok())
        monkeypatch.setattr("app.trading.broker_factory.create_broker", lambda cfg, mode=None, **kw: _FakeBroker())
        proposal = svc.generate_trade_proposal(mode="mock")
        assert proposal["blocked"] is False
        assert proposal["action"] in ("BUY", "SELL", "HOLD")
        assert proposal["disclaimer"]

    def test_decision_logged_regardless_of_action(self, monkeypatch, tmp_path):
        monkeypatch.setattr(svc, "is_stopped", lambda: False)
        monkeypatch.setattr("app.data_sources.auto_market_collector.collect_all", lambda mode=None: {})
        monkeypatch.setattr("app.models.hynix_short_term_signal.predict_hynix_signal", lambda md: _fake_signal_ok())
        monkeypatch.setattr("app.trading.broker_factory.create_broker", lambda cfg, mode=None, **kw: _FakeBroker())
        svc.generate_trade_proposal(mode="mock")
        log_files = list((tmp_path / "logs").glob("hynix_auto_trade_decisions_*.jsonl"))
        assert len(log_files) == 1
        lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["mode"] == "mock"


class TestExecuteProposal:
    def test_not_actionable_rejected(self):
        result = svc.execute_proposal({"action": "HOLD", "blocked": False}, mode="mock")
        assert result["success"] is False
        assert result["error_type"] == "not_actionable"

    def test_buy_executes_and_logs(self, monkeypatch, tmp_path):
        monkeypatch.setattr("app.trading.broker_factory.create_broker", lambda cfg, mode=None, **kw: _FakeBroker())
        proposal = {
            "blocked": False, "action": "BUY", "buy_cash_amount": 1_700_000.0,
            "current_price": 170_000.0,
        }
        result = svc.execute_proposal(proposal, mode="mock")
        assert result["success"] is True
        log_files = list((tmp_path / "logs").glob("hynix_auto_trade_orders_*.csv"))
        assert len(log_files) == 1

    def test_sell_with_no_position_rejected(self, monkeypatch):
        monkeypatch.setattr("app.trading.broker_factory.create_broker", lambda cfg, mode=None, **kw: _FakeBroker(positions=[]))
        proposal = {"blocked": False, "action": "SELL", "sell_quantity_ratio": 0.5, "current_price": 170_000.0}
        result = svc.execute_proposal(proposal, mode="mock")
        assert result["success"] is False
        assert result["error_type"] == "no_position"


class TestFullAutoGate:
    def test_full_auto_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ENABLE_FULL_AUTO", raising=False)
        result = svc.run_full_auto_cycle(mode="mock")
        assert result["skipped"] is True

    def test_full_auto_real_requires_confirm(self, monkeypatch):
        monkeypatch.setenv("ENABLE_FULL_AUTO", "true")
        monkeypatch.setattr(svc, "is_stopped", lambda: False)
        monkeypatch.setattr("app.data_sources.auto_market_collector.collect_all", lambda mode=None: {})
        monkeypatch.setattr("app.models.hynix_short_term_signal.predict_hynix_signal", lambda md: _fake_signal_ok())
        monkeypatch.setattr("app.trading.broker_factory.create_broker", lambda cfg, mode=None, **kw: _FakeBroker())

        from app.config import Config
        cfg = Config()
        cfg._raw.setdefault("safety", {})
        cfg._raw["safety"]["enable_real_trading"] = True
        monkeypatch.setattr("app.config.get_config", lambda: cfg)
        monkeypatch.delenv("FULL_AUTO_REAL_CONFIRM_TEXT", raising=False)

        result = svc.run_full_auto_cycle(mode="real")
        assert result["skipped"] is True
        assert "게이트" in result["reason"]
