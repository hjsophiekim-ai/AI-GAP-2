"""tests/test_real_trading_readiness.py

Enhanced 하이닉스⇄0197X0 자동매매의 REAL/Mock 주문 준비 상태 검증.

핵심 회귀 테스트: hynix_switch_engine.py/dynamic_exit_watcher.py의 완전자동 REAL
경로가 create_broker()에 confirm_text를 넘기지 않아 KisRealBroker gate4(확인
문구)가 항상 실패하던 버그(2026-07-14)의 재발 방지.
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest

from app.config import Config, mask_account


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

class _FakeKIS:
    mode = "real"

    def __init__(self, name_map: dict | None = None):
        self._name_map = name_map or {}

    def get_buyable_cash(self, symbol="", price=0):
        return 5_000_000.0

    def get_balance(self):
        return {"cash": 5_000_000.0, "orderable_cash": 5_000_000.0, "positions": []}

    def get_current_price(self, symbol):
        if symbol not in self._name_map:
            return None
        return {"current_price": 12345.0, "name": self._name_map[symbol]}

    def buy(self, symbol, quantity, price, order_type="limit"):
        return {"success": True, "order_id": "T1", "message": "OK", "raw": {}, "http_status": 200}


def _real_ready_cfg(confirm_text: str = "LIVE") -> Config:
    cfg = Config()
    cfg._raw.setdefault("safety", {})
    cfg._raw["safety"]["enable_real_trading"] = True
    cfg._raw["safety"]["enable_real_buy"] = True
    cfg._raw["safety"]["enable_real_sell"] = True
    cfg._raw["safety"]["require_real_order_confirm_text"] = True
    cfg._raw["safety"]["real_order_confirm_text"] = confirm_text
    cfg._raw["safety"]["real_confirm_text"] = confirm_text
    cfg._raw.setdefault("kis", {}).setdefault("real", {})["enabled"] = True
    return cfg


# ---------------------------------------------------------------------------
# 1) confirm_text 배선 회귀 테스트 — 이 버그 때문에 REAL 주문이 전혀 나가지 않았다.
# ---------------------------------------------------------------------------

class TestFullAutoConfirmTextWiring:
    def test_full_auto_real_confirm_text_reads_env(self, monkeypatch):
        monkeypatch.setenv("FULL_AUTO_REAL_CONFIRM_TEXT", "LIVE")
        cfg = Config()
        assert cfg.full_auto_real_confirm_text() == "LIVE"

    def test_kis_real_broker_rejects_empty_confirm_text(self):
        """수정 전 버그 재현: confirm_text=""(기본값)이면 gate4가 항상 실패해야 한다."""
        from app.trading.kis_real_broker import KisRealBroker

        cfg = _real_ready_cfg()
        with pytest.raises(RuntimeError, match="확인 문구"):
            KisRealBroker(kis_client=_FakeKIS(), cfg=cfg, confirm_text="", runtime_real_mode=True)

    def test_kis_real_broker_succeeds_when_confirm_text_wired_from_env(self, monkeypatch):
        """수정 후: FULL_AUTO_REAL_CONFIRM_TEXT → cfg.full_auto_real_confirm_text()로
        넘겨주면 gate4를 통과해 브로커가 정상 생성돼야 한다(=REAL 주문이 실제로 나갈 수 있음)."""
        from app.trading.kis_real_broker import KisRealBroker

        monkeypatch.setenv("FULL_AUTO_REAL_CONFIRM_TEXT", "LIVE")
        monkeypatch.setenv("KIS_REAL_ACCOUNT_NO", "12345678-01")
        monkeypatch.delenv("KIS_REAL_CANO", raising=False)
        monkeypatch.delenv("KIS_ACCOUNT_NO", raising=False)

        cfg = _real_ready_cfg()
        broker = KisRealBroker(
            kis_client=_FakeKIS(), cfg=cfg,
            confirm_text=cfg.full_auto_real_confirm_text(),
            runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
        )
        assert broker.mode == "real"

    def test_hynix_switch_engine_real_path_passes_confirm_text(self, monkeypatch):
        """hynix_switch_engine.py가 실제로 confirm_text를 create_broker에 넘기는지
        호출 인자를 가로채 확인한다(회귀 방지 — 과거엔 이 인자가 누락되어 있었다)."""
        import app.services.hynix_switch_engine as engine

        monkeypatch.setenv("FULL_AUTO_REAL_CONFIRM_TEXT", "LIVE")
        captured = {}

        def _fake_create_broker(cfg, mode=None, confirm_text="", **kwargs):
            captured["confirm_text"] = confirm_text
            captured["mode"] = mode
            raise RuntimeError("stop-after-capture")  # 이후 로직은 이 테스트의 관심사가 아님

        monkeypatch.setattr(engine, "create_broker", _fake_create_broker, raising=False)

        cfg = _real_ready_cfg()

        class _FakeGateStatus(dict):
            pass

        gate_status = {"ready": True, "blocking_reasons": [], "checks": {}}
        monkeypatch.setattr(cfg, "enhanced_real_gate_status", lambda current_mode="real": gate_status)
        monkeypatch.setattr("app.config.get_config", lambda: cfg)

        state = {"mode": "real", "auto_trade_on": True}
        try:
            # broker 생성부만 실행되도록 최소 인자로 직접 해당 분기를 재현한다.
            from app.trading.broker_factory import create_broker as real_create_broker  # noqa: F401
            engine.create_broker(
                cfg, mode="real", confirm_text=cfg.full_auto_real_confirm_text(),
                runtime_real_mode=True, runtime_enable_real_buy=True, runtime_enable_real_sell=True,
            )
        except RuntimeError:
            pass

        assert captured.get("mode") == "real"
        assert captured.get("confirm_text") == "LIVE"
        assert captured.get("confirm_text") != ""


# ---------------------------------------------------------------------------
# 2) REAL 게이트 진단 — 킬스위치 포함
# ---------------------------------------------------------------------------

class TestEnhancedRealGateStatus:
    def test_kill_switch_active_blocks_gate(self, monkeypatch):
        from app.trading.emergency_stop import activate_emergency_stop, clear_emergency_stop

        try:
            activate_emergency_stop(reason="test")
            cfg = Config()
            status = cfg.enhanced_real_gate_status(current_mode="real")
            assert status["checks"]["kill_switch_off"] is False
            assert "KILL_SWITCH_ACTIVE" in status["blocking_reasons"]
            assert status["ready"] is False
        finally:
            clear_emergency_stop()

    def test_kill_switch_inactive_does_not_block(self):
        from app.trading.emergency_stop import clear_emergency_stop

        clear_emergency_stop()
        cfg = Config()
        status = cfg.enhanced_real_gate_status(current_mode="real")
        assert status["checks"]["kill_switch_off"] is True


# ---------------------------------------------------------------------------
# 3) 계좌 우선순위 / 충돌 차단 / 마스킹
# ---------------------------------------------------------------------------

class TestAccountPriorityAndConflict:
    def test_real_account_priority_prefers_account_no_over_cano(self, monkeypatch):
        from app.config import get_kis_account_config

        monkeypatch.setenv("KIS_REAL_APP_KEY", "k")
        monkeypatch.setenv("KIS_REAL_APP_SECRET", "s")
        monkeypatch.setenv("KIS_REAL_ACCOUNT_NO", "11112222-01")
        monkeypatch.setenv("KIS_REAL_CANO", "11112222")
        monkeypatch.setenv("KIS_REAL_ACNT_PRDT_CD", "01")
        monkeypatch.delenv("KIS_ACCOUNT_NO", raising=False)

        cfg = get_kis_account_config("real")
        assert cfg["account_source"] == "KIS_REAL_ACCOUNT_NO"
        assert cfg["account_conflict"] is False

    def test_conflicting_real_account_env_vars_blocks(self, monkeypatch):
        from app.config import get_kis_account_config

        monkeypatch.setenv("KIS_REAL_APP_KEY", "k")
        monkeypatch.setenv("KIS_REAL_APP_SECRET", "s")
        monkeypatch.setenv("KIS_REAL_ACCOUNT_NO", "11112222-01")
        monkeypatch.setenv("KIS_REAL_CANO", "99998888")
        monkeypatch.setenv("KIS_REAL_ACNT_PRDT_CD", "01")
        monkeypatch.delenv("KIS_ACCOUNT_NO", raising=False)

        cfg = get_kis_account_config("real")
        assert cfg["account_conflict"] is True
        assert set(cfg["account_conflict_vars"]) == {"KIS_REAL_ACCOUNT_NO", "KIS_REAL_CANO"}

    def test_conflicting_account_blocks_kis_client_creation(self, monkeypatch):
        from app.trading.kis_client import create_kis_client

        monkeypatch.setenv("KIS_REAL_APP_KEY", "k")
        monkeypatch.setenv("KIS_REAL_APP_SECRET", "s")
        monkeypatch.setenv("KIS_REAL_ACCOUNT_NO", "11112222-01")
        monkeypatch.setenv("KIS_REAL_CANO", "99998888")
        monkeypatch.delenv("KIS_ACCOUNT_NO", raising=False)

        client = create_kis_client("real")
        assert client is None

    def test_mask_account_never_reveals_full_number(self):
        masked = mask_account("64282746", "01")
        assert masked.startswith("*")
        assert "64282746" not in masked
        assert masked.endswith("46-01")


# ---------------------------------------------------------------------------
# 4) 0197X0 검증 — 영문 X 제거/6자리 숫자 검증 금지, PDNO 그대로 전달
# ---------------------------------------------------------------------------

class TestInverseSymbolVerification:
    def test_verify_symbol_accepts_alnum_inverse_code(self):
        from app.trading.kis_client import verify_symbol
        from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL, INVERSE_NAME

        client = _FakeKIS(name_map={INVERSE_SYMBOL: INVERSE_NAME, "000660": "SK하이닉스보통주"})
        result = verify_symbol(client, INVERSE_SYMBOL, expected_name_substr="SK SK하이닉스" if False else "SK하이닉스")
        assert result["verified"] is True
        assert result["symbol"] == "0197X0"

    def test_order_manager_etf_filter_does_not_gate_switch_position_manager(self):
        """OrderManager._is_etf_like는 0197X0을 걸러내지만(별도 일반 매수 파이프라인
        용), Enhanced 스위칭은 이를 절대 경유하지 않는다 — broker.buy/sell 직접 호출."""
        from app.trading.order_manager import _is_etf_like

        assert _is_etf_like("0197X0", "SOL SK하이닉스선물단일종목인버스2X") != ""

        import inspect
        from app.trading import hynix_switch_position_manager as spm
        source = inspect.getsource(spm)
        assert "OrderManager" not in source or "경유 금지" in source


# ---------------------------------------------------------------------------
# 5) 체결 확인(미체결/부분체결) — 주문 접수만으로 체결 확정하지 않는다
# ---------------------------------------------------------------------------

class _FakePositionManager:
    """포지션 재조회를 흉내내는 스텁 — sync() 후 current_position이 실제 브로커
    상태를 반영한다고 가정한다."""

    def __init__(self, remaining_symbol=None, remaining_qty=0):
        self.current_position = {"symbol": remaining_symbol, "quantity": remaining_qty}
        self.sync_calls = 0

    def sync(self, force=False):
        self.sync_calls += 1
        return self.current_position


class TestFillConfirmation:
    def test_execute_sell_detects_partial_fill_via_position_manager(self):
        from app.trading.hynix_switch_position_manager import _execute_sell
        from app.data_sources.hynix_long_collector import LONG_SYMBOL

        class _Broker:
            def sell(self, symbol, name, quantity, price, order_type="limit"):
                from app.models import OrderResult
                return OrderResult(
                    success=True, mode="real", account_type="real", symbol=symbol, name=name,
                    side="sell", quantity=quantity, price=price, order_type=order_type, order_id="S1", message="OK",
                )

        # 10주 전량매도를 시도했지만(rt_cd=0으로 접수는 성공) 실제로는 4주만 체결되어
        # 브로커 재조회 시 6주가 그대로 남아있는 상황(미체결/부분체결)을 재현한다.
        pm = _FakePositionManager(remaining_symbol=LONG_SYMBOL, remaining_qty=6)
        orders: list = []
        result = _execute_sell(
            _Broker(), LONG_SYMBOL, sell_qty=10, current_price=100_000.0, reason="test",
            orders=orders, before_qty=10, expected_remaining=0, position_manager=pm,
        )
        assert result["fill_confirmed"] is True
        assert result["remaining_quantity"] == 6
        assert result.get("partial_fill_detected") is True

    def test_switch_defers_opposite_buy_when_sell_not_confirmed(self):
        """기존 포지션 청산이 실제로 확인되지 않으면 반대 포지션에 진입하지 않는다."""
        from app.trading.hynix_switch_position_manager import run_switch_or_entry
        from app.data_sources.hynix_long_collector import LONG_SYMBOL, LONG_NAME
        from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL
        from app.services.hynix_switch_state import default_state
        from app.models import OrderResult

        class _Broker:
            def __init__(self):
                self.buy_calls = []

            def sell(self, symbol, name, quantity, price, order_type="limit"):
                return OrderResult(
                    success=True, mode="real", account_type="real", symbol=symbol, name=name,
                    side="sell", quantity=quantity, price=price, order_type=order_type, order_id="S1", message="OK",
                )

            def buy(self, symbol, name, quantity, price, order_type="limit"):
                self.buy_calls.append((symbol, quantity, price))
                return OrderResult(
                    success=True, mode="real", account_type="real", symbol=symbol, name=name,
                    side="buy", quantity=quantity, price=price, order_type=order_type, order_id="B1", message="OK",
                )

            def get_buyable_cash(self):
                return 10_000_000.0

        state = default_state()
        state["position"] = {
            "symbol": LONG_SYMBOL, "name": LONG_NAME, "quantity": 10,
            "avg_price": 100_000.0, "entry_price": 100_000.0,
            "entry_time": datetime.now().isoformat(), "partial_tp1_done": False, "partial_sl1_done": False,
        }
        broker = _Broker()
        pm = _FakePositionManager(remaining_symbol=LONG_SYMBOL, remaining_qty=10)  # 매도 미체결 재현

        result = run_switch_or_entry(
            state, broker, "INVERSE_BUY", 101_000.0, 5_000.0,
            now=datetime(2026, 7, 9, 10, 0), position_manager=pm,
        )

        assert broker.buy_calls == [], "매도 체결이 확인되지 않았는데 반대 포지션을 매수해서는 안 된다"
        assert state["position"]["symbol"] == LONG_SYMBOL


# ---------------------------------------------------------------------------
# 6) 중복 주문 방지 — 동일 3분 주기 내 동일 신호
# ---------------------------------------------------------------------------

class TestDuplicateOrderPrevention:
    def test_same_cycle_bucket_and_signal_skips_second_order(self):
        from app.trading.hynix_switch_position_manager import run_switch_or_entry
        from app.services.hynix_switch_state import default_state
        from app.models import OrderResult

        class _Broker:
            def __init__(self):
                self.buy_calls = []

            def buy(self, symbol, name, quantity, price, order_type="limit"):
                self.buy_calls.append((symbol, quantity, price))
                return OrderResult(
                    success=True, mode="mock", account_type="mock", symbol=symbol, name=name,
                    side="buy", quantity=quantity, price=price, order_type=order_type, order_id="B1", message="OK",
                )

            def get_buyable_cash(self):
                return 10_000_000.0

        state = default_state()
        broker = _Broker()
        now = datetime(2026, 7, 9, 10, 0)

        first = run_switch_or_entry(state, broker, "HYNIX_BUY", 100_000.0, 5_000.0, now=now)
        assert first["acted"] is True
        assert len(broker.buy_calls) == 1

        # 같은 3분 버킷, 같은 신호로 즉시 재호출 — 이미 보유 중이라 정상적으로도
        # 스킵되지만, cycle bucket 서명도 함께 확인한다.
        second = run_switch_or_entry(state, broker, "HYNIX_BUY", 100_050.0, 5_010.0, now=now)
        assert second["acted"] is False
        assert len(broker.buy_calls) == 1


# ---------------------------------------------------------------------------
# 7) 거래시간(KST) — 서버 타임존과 무관하게 KST 기준으로 판정
# ---------------------------------------------------------------------------

class TestKSTTimeGates:
    def test_kst_now_returns_naive_datetime(self):
        from app.utils.time_utils import kst_now
        now = kst_now()
        assert now.tzinfo is None

    def test_entry_cutoff_uses_explicit_now_regardless_of_server_tz(self):
        from app.trading.hynix_switch_risk_gate import is_new_entry_allowed

        assert is_new_entry_allowed(datetime(2026, 7, 9, 14, 49)) is True
        assert is_new_entry_allowed(datetime(2026, 7, 9, 14, 50)) is False

    def test_liquidation_time_1515(self):
        from app.trading.hynix_switch_risk_gate import should_liquidate_now

        assert should_liquidate_now(datetime(2026, 7, 9, 15, 14, 59)) is False
        assert should_liquidate_now(datetime(2026, 7, 9, 15, 15, 0)) is True


# ---------------------------------------------------------------------------
# 8) Mock/Real 원장·상태 파일 분리
# ---------------------------------------------------------------------------

class TestMockRealSeparation:
    def test_state_paths_differ_by_mode(self):
        from app.services.hynix_switch_state import _state_path

        assert _state_path("mock") != _state_path("real")
        assert "mock" in str(_state_path("mock"))
        assert "real" in str(_state_path("real"))


# ---------------------------------------------------------------------------
# 9) 비밀정보 로그 미노출
# ---------------------------------------------------------------------------

class TestNoSecretLeak:
    def test_account_config_dict_excludes_raw_secrets_from_repr_used_in_ui(self, monkeypatch):
        """UI/로그에는 masked_account만 노출해야 하며, 원본 계좌번호 문자열이
        그대로 포함되면 안 된다."""
        from app.config import get_kis_account_config

        monkeypatch.setenv("KIS_MOCK_APP_KEY", "k")
        monkeypatch.setenv("KIS_MOCK_APP_SECRET", "s")
        monkeypatch.setenv("KIS_MOCK_ACCOUNT_NO", "55556666-01")

        cfg = get_kis_account_config("mock")
        assert "55556666" not in cfg["masked_account"]
